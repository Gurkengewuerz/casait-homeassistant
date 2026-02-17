"""API for casaIT devices."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import Iterable, Mapping
from functools import partial
import logging
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_send

from .const import I2C_ADDR_RANGES, SIGNAL_STATE_UPDATED
from .services.i2cClasses.dm117 import DM117, DeviceType
from .services.i2cClasses.led_controller import LEDConfig
from .services.i2cClasses.oneWireBus import OneWireBus
from .services.i2cClasses.pcf8574 import PCF8574
from .services.smbus_proxy import SMBus, SMBusProxyError

_LOGGER = logging.getLogger(__name__)


class CasaITApi:
    """API for casaIT devices."""

    def __init__(self, hass: HomeAssistant, bus: SMBus) -> None:
        """Initialize the API."""
        self.hass = hass
        self.bus = bus
        self.im117_om117: dict[int, PCF8574] = {}
        self.dm117: dict[int, DM117] = {}
        self.sm117: dict[int, OneWireBus] = {}
        self.ow_ids: set[str] = set()
        self.ow_devices: dict[str, dict[str, Any]] = {}
        self.found_i2c_devices: dict[str, list[int]] = {}
        self.lock = asyncio.Lock()
        self._pcf_states: dict[int, list[int]] = {}
        self._dm117_states: dict[int, dict[int, int]] = {}
        self._poll_interval = 0.002
        self._stop_event: asyncio.Event | None = None
        self._poll_task: asyncio.Task | None = None
        self._init_done = asyncio.Event()
        self._init_task: asyncio.Task | None = None

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
            await self.scan_devices()

            if dm_config:
                await self.async_configure_dm117(dm_config)

            await self.start_polling()
        except asyncio.CancelledError:
            self._init_done.set()
            raise
        except Exception:
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
                    await self.hass.async_add_executor_job(self.bus.write_quick, addr)
                except (SMBusProxyError, OSError):
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
        while not self._stop_event.is_set():
            try:
                await self._poll_once()
            except Exception:
                _LOGGER.exception("Error polling casaIT devices")
                await asyncio.sleep(self._poll_interval)
                continue
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=self._poll_interval)
            except TimeoutError:
                continue

    async def _poll_once(self) -> None:
        """Read all PCF8574 and DM117 devices once and broadcast state."""

        pcf_states: dict[int, list[int]] = {}
        dm_states: dict[int, dict[int, int]] = {}

        async with self.lock:
            for addr, device in self.im117_om117.items():
                set_high = 0x38 <= addr <= 0x3F
                try:
                    port_states, _ = await self.hass.async_add_executor_job(device.read_ports, set_high)
                    pcf_states[addr] = port_states
                except Exception as result:  # noqa: BLE001
                    _LOGGER.warning("Error reading from device %s: %s", hex(addr), result)
                    if addr in self._pcf_states:
                        pcf_states[addr] = self._pcf_states[addr]
                # Small delay between devices to avoid overwhelming the I2C bridge
                await asyncio.sleep(0.02)

            for addr, device in self.dm117.items():
                try:
                    port_states = await self.hass.async_add_executor_job(device.read_ports)
                except Exception as result:  # noqa: BLE001
                    _LOGGER.warning("Error reading from DM117 device %s: %s", hex(addr), result)
                    if addr in self._dm117_states:
                        dm_states[addr] = self._dm117_states[addr]
                else:
                    if port_states is not None:
                        dm_states[addr] = port_states
                    elif addr in self._dm117_states:
                        dm_states[addr] = self._dm117_states[addr]
                # Small delay between devices to avoid overwhelming the I2C bridge
                await asyncio.sleep(0.02)

        self._pcf_states = pcf_states
        self._dm117_states = dm_states
        async_dispatcher_send(self.hass, SIGNAL_STATE_UPDATED)

    async def async_force_refresh(self) -> None:
        """Force a single poll and dispatch."""

        await self._poll_once()

    def _refresh_pcf8574(self, found_by_code: dict[str, set[int]]) -> None:
        found = set()
        for code in ("IM117", "OM117"):
            found.update(found_by_code.get(code, set()))

        for addr in found:
            if addr not in self.im117_om117:
                self.im117_om117[addr] = PCF8574(self.bus, addr)

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
                if self.lock:
                    async with self.lock:
                        devices = await self.hass.async_add_executor_job(ow_bus.scan_devices, True)
                else:
                    devices = await self.hass.async_add_executor_job(ow_bus.scan_devices, True)
            except Exception as exc:  # noqa: BLE001
                _LOGGER.warning("Error scanning 1-Wire bus at 0x%02x: %s", addr, exc)
                continue

            for device_id, meta in devices.items():
                discovered[device_id] = {"bus_address": addr, **meta}

        self.ow_devices = discovered
        self.ow_ids = set(discovered)

        if discovered:
            _LOGGER.info("Discovered OneWire devices: %s", list(discovered.keys()))
        else:
            _LOGGER.info("No OneWire devices discovered")

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

        if self.lock:
            async with self.lock:
                return await self.hass.async_add_executor_job(bus.read_temperature, device_id)

        return await self.hass.async_add_executor_job(bus.read_temperature, device_id)

    async def read_ds2438(self, device_id: str):
        """Read values from a DS2438 device."""

        bus = self._get_onewire_bus(device_id)
        if not bus:
            return None

        if self.lock:
            async with self.lock:
                return await self.hass.async_add_executor_job(
                    bus.ds2438.get_reading, device_id, bus.get_interval(device_id)
                )

        return await self.hass.async_add_executor_job(bus.ds2438.get_reading, device_id, bus.get_interval(device_id))

    async def read_ds2413_state(self, device_id: str, channel: int, *, invert: bool = True) -> bool | None:
        """Read a binary state from a DS2413 channel."""

        bus = self._get_onewire_bus(device_id)
        if not bus:
            return None

        read_job = partial(bus.read_binary_state, device_id, channel, invert=invert)

        if self.lock:
            async with self.lock:
                return await self.hass.async_add_executor_job(read_job)

        return await self.hass.async_add_executor_job(read_job)

    async def write_ds2413_state(self, device_id: str, channel: int, value: bool) -> bool:
        """Write a binary state to a DS2413 channel."""

        bus = self._get_onewire_bus(device_id)
        if not bus:
            return False

        async with self.lock:
            return await self.hass.async_add_executor_job(bus.ds2413.set_state, device_id, channel, value)

    async def read_led_config(self, device_id: str, *, use_cache: bool = True) -> LEDConfig | None:
        """Read the LED controller configuration for a device."""

        bus = self._get_onewire_bus(device_id)
        if not bus:
            return None

        read_job = partial(bus.read_led_config, device_id, use_cache)

        if self.lock:
            async with self.lock:
                return await self.hass.async_add_executor_job(read_job)

        return await self.hass.async_add_executor_job(read_job)

    async def write_led_config(self, device_id: str, config: LEDConfig) -> bool:
        """Write an LED controller configuration for a device."""

        bus = self._get_onewire_bus(device_id)
        if not bus:
            return False

        write_job = partial(bus.write_led_config, device_id, config)

        if self.lock:
            async with self.lock:
                return await self.hass.async_add_executor_job(write_job)

        return await self.hass.async_add_executor_job(write_job)
