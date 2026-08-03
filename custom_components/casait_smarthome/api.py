"""API for casaIT devices."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import AsyncIterator, Iterable, Mapping
from contextlib import asynccontextmanager
from functools import partial
import logging
import time
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_send

from .const import (
    DEFAULT_FAST_POLL_INTERVAL,
    DEFAULT_OW_POLL_INTERVAL,
    DEFAULT_OW_PROFILE,
    DEFAULT_SLOW_POLL_INTERVAL,
    I2C_ADDR_RANGES,
    SIGNAL_STATE_UPDATED,
)
from .helpers import OM117PairConfig, get_address_range
from .services.i2cClasses.dm117 import DM117, DeviceType, DM117PortConfig
from .services.i2cClasses.ds2438 import DS2438Reading
from .services.i2cClasses.led_controller import LEDConfig
from .services.i2cClasses.oneWireBus import OneWireBus
from .services.i2cClasses.pcf8574 import PCF8574
from .services.smbus_proxy import SMBus, SMBusProxyError

_LOGGER = logging.getLogger(__name__)


class CasaITApi:
    """API for casaIT devices."""

    def __init__(
        self,
        hass: HomeAssistant,
        bus: SMBus,
        entry_id: str,
        onewire_profiles: Mapping[str, str] | None = None,
        onewire_poll_intervals: Mapping[str, int] | None = None,
        om117_pair_configuration: Mapping[int, Mapping[int, OM117PairConfig]] | None = None,
        fast_poll_interval: float = DEFAULT_FAST_POLL_INTERVAL,
        slow_poll_interval: float = DEFAULT_SLOW_POLL_INTERVAL,
    ) -> None:
        """Initialize the API."""
        self.hass = hass
        self.bus = bus
        self.state_update_signal = f"{SIGNAL_STATE_UPDATED}_{entry_id}"
        self.im117_om117: dict[int, PCF8574] = {}
        self.dm117: dict[int, DM117] = {}
        self.sm117: dict[int, OneWireBus] = {}
        self.ow_ids: set[str] = set()
        self.ow_devices: dict[str, dict[str, Any]] = {}
        self._onewire_profiles = dict(onewire_profiles or {})
        self._onewire_poll_intervals = dict(onewire_poll_intervals or {})
        self.om117_pair_configuration = {
            address: dict(pairs) for address, pairs in (om117_pair_configuration or {}).items()
        }
        self.found_i2c_devices: dict[str, list[int]] = {}
        self._lock = asyncio.Lock()
        self._pcf_states: dict[int, list[int]] = {}
        self._dm117_states: dict[int, dict[int, int]] = {}
        self._read_errors: set[tuple[str, int]] = set()
        self._poll_interval = fast_poll_interval
        self._slow_poll_interval = slow_poll_interval
        self._dm_config: dict[int, dict[int, DeviceType]] = {}
        # Writes claim priority over the poll loop so a button press is not queued
        # behind a full sweep of the bus.
        self._write_pending = 0
        self._writes_idle = asyncio.Event()
        self._writes_idle.set()
        self._last_fast_cycle = 0.0
        self._last_full_cycle = 0.0
        self._stop_event: asyncio.Event | None = None
        self._poll_task: asyncio.Task | None = None
        self._init_done = asyncio.Event()
        self._init_task: asyncio.Task | None = None
        self.initialization_error: Exception | None = None

    def start_initialization(self, dm_config: Mapping[int, Mapping[int, DeviceType]] | None = None) -> None:
        """Kick off asynchronous initialization for initial scans and polling."""

        if self._init_task:
            return

        self._init_task = self.hass.async_create_background_task(
            self._async_initialize(dm_config), "casait_initialization"
        )

    async def async_wait_initialized(self, timeout: float | None = None) -> None:
        """Wait until the initial scan and setup have finished.

        Timeout can be provided for shutdown paths to avoid deadlocks.
        """

        if self._init_task is None:
            self.start_initialization()

        if timeout is None:
            await self._init_done.wait()
            return

        await asyncio.wait_for(self._init_done.wait(), timeout=timeout)

    async def _async_initialize(self, dm_config: Mapping[int, Mapping[int, DeviceType]] | None) -> None:
        """Perform initial discovery, configuration, and start polling."""

        try:
            self._dm_config = {address: dict(slots) for address, slots in (dm_config or {}).items()}

            await self.scan_devices()

            if dm_config:
                await self.async_configure_dm117(dm_config)

            await self.start_polling()
        except asyncio.CancelledError:
            self._init_done.set()
            raise
        except Exception as exc:
            self.initialization_error = exc
            _LOGGER.exception("Error initializing casaIT devices")
        finally:
            self._init_done.set()

    @property
    def pcf_states(self) -> dict[int, list[int]]:
        """Return cached PCF8574 states indexed by address."""

        return self._pcf_states

    @property
    def dm117_states(self) -> dict[int, dict[int, int]]:
        """Return cached DM117 port states indexed by address."""

        return self._dm117_states

    @property
    def diagnostic_data(self) -> dict[str, Any]:
        """Return discovery data shared by the debug entity and diagnostics."""

        return {
            "found_i2c_devices": {
                code: [f"0x{address:02X}" for address in sorted(addresses)]
                for code, addresses in self.found_i2c_devices.items()
            },
            "found_onewire_devices": sorted(self.ow_ids),
            "poll": {
                "fast_cycle_ms": round(self._last_fast_cycle * 1000, 2),
                "full_cycle_ms": round(self._last_full_cycle * 1000, 2),
                "fast_interval_ms": round(self._poll_interval * 1000, 2),
                "slow_interval_s": self._slow_poll_interval,
                "fast_addresses": [f"0x{address:02X}" for address in sorted(self._fast_pcf_addresses())],
            },
            "transport": self.bus.stats,
        }

    def address_signal(self, address: int) -> str:
        """Return the dispatcher signal carrying state changes for one module."""

        return f"{self.state_update_signal}_{address:02x}"

    def edge_signal(self, address: int) -> str:
        """Return the dispatcher signal carrying input edges for one module."""

        return f"{self.state_update_signal}_edge_{address:02x}"

    @asynccontextmanager
    async def _write_access(self) -> AsyncIterator[None]:
        """Claim the bus for a write, holding the poll loop off until it is done."""

        self._write_pending += 1
        self._writes_idle.clear()
        try:
            async with self._lock:
                yield
        finally:
            self._write_pending -= 1
            if not self._write_pending:
                self._writes_idle.set()

    async def scan_devices(
        self,
        *,
        device_codes: Iterable[str] | None = None,
    ) -> None:
        """Scan I2C bus for supported devices.

        device_codes limits scanning to the specified codes from I2C_ADDR_RANGES
        (for example, {"IM117", "OM117", "DM117", "SM117"}). When omitted,
        all codes are scanned.
        """

        target_codes = set(device_codes) if device_codes else None

        _LOGGER.info("Scanning for I2C devices")
        found_by_code: dict[str, set[int]] = defaultdict(set)

        for start, end, _, code in I2C_ADDR_RANGES:
            if target_codes and code not in target_codes:
                continue

            for addr in range(start, end + 1):
                try:
                    async with self._lock:
                        await self.hass.async_add_executor_job(self.bus.write_quick, addr)
                except SMBusProxyError, OSError:
                    continue

                found_by_code[code].add(addr)

        log_snapshot = {key: sorted(value) for key, value in found_by_code.items()}
        self.found_i2c_devices = log_snapshot
        _LOGGER.info("Found I2C devices: %s", log_snapshot)

        self._refresh_pcf8574(found_by_code)
        self._refresh_dm117(found_by_code)
        await self._refresh_sm117(found_by_code)

        await self.scan_onewire()

    async def start_polling(self) -> None:
        """Start background polling of I2C devices."""

        if self._poll_task:
            return

        self._stop_event = asyncio.Event()
        self._poll_task = self.hass.async_create_background_task(self._poll_loop(), "casait_poll_loop")

    async def stop_polling(self) -> None:
        """Stop background polling task."""

        if not self._poll_task or not self._stop_event:
            return

        self._stop_event.set()
        await self._poll_task
        self._poll_task = None
        self._stop_event = None

    async def _poll_loop(self) -> None:
        """Continuously poll devices and dispatch updates."""

        assert self._stop_event is not None
        slow_due = 0.0
        while not self._stop_event.is_set():
            try:
                include_slow = time.monotonic() >= slow_due
                await self._poll_cycle(include_slow=include_slow)
                if include_slow:
                    slow_due = time.monotonic() + self._slow_poll_interval
            except Exception:
                _LOGGER.exception("Error polling casaIT devices")
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=self._poll_interval)
            except TimeoutError:
                continue

    def _fast_pcf_addresses(self) -> set[int]:
        """Return PCF8574 addresses carrying inputs, which drive perceived latency."""

        input_range = get_address_range("IM117")
        if input_range is None:
            return set()
        return {address for address in self.im117_om117 if input_range[0] <= address <= input_range[1]}

    def _fast_dm117_addresses(self) -> set[int]:
        """Return DM117 addresses with at least one slot configured as an input."""

        return {address for address in self.dm117 if DeviceType.INPUT in self._dm_config.get(address, {}).values()}

    async def _poll_cycle(self, *, include_slow: bool) -> None:
        """Read one class of devices and publish only what actually changed.

        Outputs cannot change on their own, so a fast cycle skips them entirely and
        the slow cycle picks them up to catch drift.
        """

        started = time.monotonic()

        fast_pcf = self._fast_pcf_addresses()
        pcf_addresses = set(self.im117_om117) if include_slow else fast_pcf
        dm_addresses = set(self.dm117) if include_slow else self._fast_dm117_addresses()

        for address in sorted(pcf_addresses):
            await self._poll_pcf8574(address, is_input=address in fast_pcf)

        for address in sorted(dm_addresses):
            await self._poll_dm117(address)

        duration = time.monotonic() - started
        if include_slow:
            self._last_full_cycle = duration
        else:
            self._last_fast_cycle = duration

    async def _poll_pcf8574(self, address: int, *, is_input: bool) -> None:
        """Read one PCF8574 and publish state changes plus any input edges."""

        device = self.im117_om117.get(address)
        if device is None:
            return

        await self._writes_idle.wait()

        try:
            async with self._lock:
                reading = await self.hass.async_add_executor_job(device.read_ports, is_input)
        except Exception as exc:  # noqa: BLE001
            self._record_read_error("PCF8574", address, exc)
            self._drop_state(self._pcf_states, address)
            return

        if not reading.ok:
            self._record_read_error("PCF8574", address)
            self._drop_state(self._pcf_states, address)
            return

        self._clear_read_error("PCF8574", address)
        previous = self._pcf_states.get(address)
        self._pcf_states[address] = reading.port_states

        if reading.edges:
            async_dispatcher_send(self.hass, self.edge_signal(address), reading.edges)
        if previous != reading.port_states:
            async_dispatcher_send(self.hass, self.address_signal(address))

    async def _poll_dm117(self, address: int) -> None:
        """Read one DM117 and publish state changes."""

        device = self.dm117.get(address)
        if device is None:
            return

        await self._writes_idle.wait()

        try:
            async with self._lock:
                port_states = await self.hass.async_add_executor_job(device.read_ports)
        except Exception as exc:  # noqa: BLE001
            self._record_read_error("DM117", address, exc)
            self._drop_state(self._dm117_states, address)
            return

        if port_states is None:
            self._record_read_error("DM117", address)
            self._drop_state(self._dm117_states, address)
            return

        self._clear_read_error("DM117", address)
        previous = self._dm117_states.get(address)
        self._dm117_states[address] = dict(port_states)

        if previous != port_states:
            async_dispatcher_send(self.hass, self.address_signal(address))

    def _drop_state(self, states: dict[int, Any], address: int) -> None:
        """Forget a module's cached state and tell its entities it went away."""

        if states.pop(address, None) is not None:
            async_dispatcher_send(self.hass, self.address_signal(address))

    def _record_read_error(self, device_type: str, address: int, exc: Exception | None = None) -> None:
        """Log a device read failure only when it first becomes unavailable."""

        key = (device_type, address)
        if key in self._read_errors:
            return

        self._read_errors.add(key)
        if exc is None:
            _LOGGER.warning("%s device at 0x%02X returned no data and is unavailable", device_type, address)
        else:
            _LOGGER.warning("Error reading %s device at 0x%02X; marking unavailable: %s", device_type, address, exc)

    def _clear_read_error(self, device_type: str, address: int) -> None:
        """Log once when a previously unavailable device recovers."""

        key = (device_type, address)
        if key not in self._read_errors:
            return

        self._read_errors.remove(key)
        _LOGGER.info("%s device at 0x%02X is available again", device_type, address)

    async def async_force_refresh(self) -> None:
        """Force a full poll of every device and publish what changed."""

        await self._poll_cycle(include_slow=True)

    def _refresh_pcf8574(self, found_by_code: dict[str, set[int]]) -> None:
        found = set()
        for code in ("IM117", "OM117"):
            found.update(found_by_code.get(code, set()))

        input_addresses = found_by_code.get("IM117", set())
        for addr in found:
            if addr not in self.im117_om117:
                # Debouncing only makes sense for inputs. An output latch is driven by
                # Home Assistant, so suppressing a change there would hide a real write.
                self.im117_om117[addr] = PCF8574(self.bus, addr, debounce_time=40 if addr in input_addresses else 0)

        for addr in list(self.im117_om117):
            if addr not in found:
                del self.im117_om117[addr]

    def _refresh_dm117(self, found_by_code: dict[str, set[int]]) -> None:
        found = found_by_code.get("DM117", set())

        for addr in found:
            if addr not in self.dm117:
                self.dm117[addr] = DM117(self.bus, addr)

        for addr in list(self.dm117):
            if addr not in found:
                del self.dm117[addr]

    async def _refresh_sm117(self, found_by_code: dict[str, set[int]]) -> None:
        found = set()
        for code, addresses in found_by_code.items():
            if code.startswith("SM117"):
                found.update(addresses)

        for addr in found:
            if addr not in self.sm117:
                self.sm117[addr] = await self.hass.async_add_executor_job(OneWireBus, self.bus, addr)

        for addr in list(self.sm117):
            if addr not in found:
                del self.sm117[addr]

    async def scan_onewire(self) -> None:
        """Scan all detected SM117 bridges for 1-Wire devices."""

        if not self.sm117:
            self.ow_devices = {}
            self.ow_ids = set()
            return

        discovered: dict[str, dict[str, Any]] = {}

        for addr, ow_bus in self.sm117.items():
            try:
                async with self._lock:
                    devices = await self.hass.async_add_executor_job(ow_bus.scan_devices, True)
            except Exception as exc:  # noqa: BLE001
                _LOGGER.warning("Error scanning 1-Wire bus at 0x%02x: %s", addr, exc)
                continue

            for device_id, meta in devices.items():
                discovered[device_id] = {"bus_address": addr, **meta}

        self.ow_devices = discovered
        self.ow_ids = set(discovered)
        self._apply_onewire_intervals()

        if discovered:
            _LOGGER.info("Discovered OneWire devices: %s", list(discovered.keys()))
        else:
            _LOGGER.info("No OneWire devices discovered")

    def _apply_onewire_intervals(self) -> None:
        """Apply configured or profile-default cache intervals after a 1-Wire scan."""

        for device_id, meta in self.ow_devices.items():
            bus = self.sm117.get(meta["bus_address"])
            if bus is None:
                continue

            profile = self._onewire_profiles.get(device_id)
            if profile is None:
                family_code = meta.get("family_code")
                if isinstance(family_code, int):
                    profile = DEFAULT_OW_PROFILE.get(family_code)

            interval = self._onewire_poll_intervals.get(device_id)
            if interval is None and profile is not None:
                interval = DEFAULT_OW_POLL_INTERVAL.get(profile)
            if interval is not None:
                bus.set_interval(device_id, interval)

    async def async_configure_dm117(self, slot_config: Mapping[int, Mapping[int, DeviceType]]) -> None:
        """Configure DM117 modules based on slot configuration."""

        for address, config in slot_config.items():
            if not config:
                continue
            device = self.dm117.get(address)
            if not device:
                continue

            await self.hass.async_add_executor_job(device.configure_ports, dict(config))

    def _get_onewire_bus(self, device_id: str) -> OneWireBus | None:
        """Return the OneWire bus for a given device id."""

        meta = self.ow_devices.get(device_id)
        if not meta:
            return None

        return self.sm117.get(meta["bus_address"])

    async def read_ds18b20_temperature(self, device_id: str) -> float | None:
        """Read temperature from a DS18B20 device."""

        bus = self._get_onewire_bus(device_id)
        if not bus:
            return None

        async with self._lock:
            return await self.hass.async_add_executor_job(bus.read_temperature, device_id)

    async def read_ds2438(self, device_id: str) -> DS2438Reading | None:
        """Read values from a DS2438 device."""

        bus = self._get_onewire_bus(device_id)
        if not bus:
            return None

        async with self._lock:
            return await self.hass.async_add_executor_job(
                bus.ds2438.get_reading, device_id, bus.get_interval(device_id)
            )

    async def read_ds2413_state(self, device_id: str, channel: int, *, invert: bool = True) -> bool | None:
        """Read a binary state from a DS2413 channel."""

        bus = self._get_onewire_bus(device_id)
        if not bus:
            return None

        read_job = partial(bus.read_binary_state, device_id, channel, invert=invert)
        async with self._lock:
            return await self.hass.async_add_executor_job(read_job)

    async def write_ds2413_state(self, device_id: str, channel: int, value: bool) -> bool:
        """Write a binary state to a DS2413 channel."""

        bus = self._get_onewire_bus(device_id)
        if not bus:
            return False

        async with self._write_access():
            return await self.hass.async_add_executor_job(bus.ds2413.set_state, device_id, channel, value)

    async def read_led_config(self, device_id: str, *, use_cache: bool = True) -> LEDConfig | None:
        """Read the LED controller configuration for a device."""

        bus = self._get_onewire_bus(device_id)
        if not bus:
            return None

        read_job = partial(bus.read_led_config, device_id, use_cache)
        async with self._lock:
            return await self.hass.async_add_executor_job(read_job)

    async def write_led_config(self, device_id: str, config: LEDConfig) -> bool:
        """Write an LED controller configuration for a device."""

        bus = self._get_onewire_bus(device_id)
        if not bus:
            return False

        write_job = partial(bus.write_led_config, device_id, config)
        async with self._write_access():
            return await self.hass.async_add_executor_job(write_job)

    async def async_write_pcf_port(self, address: int, port: int, state: int) -> bool:
        """Write a PCF8574 port and publish the resulting state."""

        device = self.im117_om117.get(address)
        if device is None:
            return False

        async with self._write_access():
            written = await self.hass.async_add_executor_job(device.write_port, port, state)

        if not written:
            return False

        # write_port already read the value back to verify it, so the driver's
        # port_states are authoritative. Polling again would only add latency.
        previous = self._pcf_states.get(address)
        self._pcf_states[address] = list(device.port_states)
        if previous != device.port_states:
            async_dispatcher_send(self.hass, self.address_signal(address))
        return True

    async def async_write_dm117_port(self, address: int, config: DM117PortConfig) -> bool:
        """Write a DM117 port and publish the resulting state."""

        device = self.dm117.get(address)
        if device is None:
            return False

        async with self._write_access():
            written = await self.hass.async_add_executor_job(device.write_port, config)

        if not written:
            return False

        # The DM117 does not echo writes back, so mirror the value the driver sent.
        # For a ramped dimmer that is the target value, which is what HA should show.
        states = dict(self._dm117_states.get(address) or {})
        states[config.port] = device.last_values.get(config.port, 0)
        previous = self._dm117_states.get(address)
        self._dm117_states[address] = states
        if previous != states:
            async_dispatcher_send(self.hass, self.address_signal(address))
        return True
