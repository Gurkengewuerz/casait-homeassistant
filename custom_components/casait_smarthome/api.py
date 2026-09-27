"""API for casaIT devices."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Mapping
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
import logging
import time
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, issue_registry as ir
from homeassistant.helpers.dispatcher import async_dispatcher_send

from .const import (
    DEFAULT_FAST_POLL_INTERVAL,
    DEFAULT_INPUT_DEBOUNCE_MS,
    DEFAULT_SLOW_POLL_INTERVAL,
    DOMAIN,
    DS28E17_FAMILY,
    I2C_ADDR_RANGES,
    OW_PROFILE_MULTISENSOR,
    SIGNAL_STATE_UPDATED,
)
from .health import LinkHealth
from .helpers import (
    OM117PairConfig,
    TopologySettings,
    build_device_identifier,
    default_onewire_profile,
    get_address_range,
    get_module_name,
)
from .multisensor import CasaITMultisensorManager
from .onewire import CasaITOneWireScheduler
from .outputs import CasaITOutputWriter
from .restore import CasaITOutputRestorer
from .services.i2cClasses.dm117 import DM117, DeviceType, DM117PortConfig, PortConfig
from .services.i2cClasses.ds28e17 import DS28E17Error
from .services.i2cClasses.edge_tracker import EdgeTracker
from .services.i2cClasses.led_controller import LEDConfig
from .services.i2cClasses.multisensor import MultisensorComponents
from .services.i2cClasses.oneWireBus import OneWireBus
from .services.i2cClasses.pcf8574 import PCF8574, PCF8574Reading
from .services.smbus_proxy import (
    SCAN_FLAG_OVERFLOW,
    SCAN_FLAG_UNCONFIGURED,
    BridgeInfo,
    I2CBatch,
    I2CBatchError,
    SMBus,
    SMBusProxyError,
)

_LOGGER = logging.getLogger(__name__)

# Bridge-side settle time after re-arming a PCF8574 latch, in milliseconds. Matches
# the delay the single-device path sleeps for client side.
PCF_REARM_SETTLE_MS = 5
# Re-arms run on the bridge and each one stalls the frame for the settle time above.
# Capping them per cycle bounds that cost; a deferred re-arm keeps its flag and is
# picked up by one of the next cycles, which is harmless for a periodic safety net.
MAX_REARMS_PER_CYCLE = 2


@dataclass
class _PolledModule:
    """One module's place in a batched poll: which ops it owns and where its results are."""

    kind: str
    address: int
    first_op: int
    result_start: int
    result_count: int
    rearmed: bool = False
    is_input: bool = False


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
        configured_module_addresses: Mapping[str, set[int]] | None = None,
        input_debounce_ms: Mapping[str, Mapping[int, int]] | None = None,
        topology_settings: TopologySettings | None = None,
        onewire_names: Mapping[str, str] | None = None,
        power_on_policies: Mapping[str, str] | None = None,
    ) -> None:
        """Initialize the API."""
        self.hass = hass
        self.bus = bus
        self.entry_id = entry_id
        self.state_update_signal = f"{SIGNAL_STATE_UPDATED}_{entry_id}"
        self.im117_om117: dict[int, PCF8574] = {}
        self.dm117: dict[int, DM117] = {}
        self.sm117: dict[int, OneWireBus] = {}
        self.ow_ids: set[str] = set()
        self.ow_devices: dict[str, dict[str, Any]] = {}
        self._onewire_profiles = dict(onewire_profiles or {})
        self._onewire_poll_intervals = dict(onewire_poll_intervals or {})
        self._onewire_names = dict(onewire_names or {})
        self.om117_pair_configuration = {
            address: dict(pairs) for address, pairs in (om117_pair_configuration or {}).items()
        }
        self._input_debounce_ms = {
            module_kind: dict(addresses) for module_kind, addresses in (input_debounce_ms or {}).items()
        }
        self._configured_module_addresses = {
            module_kind: set(addresses) for module_kind, addresses in (configured_module_addresses or {}).items()
        }
        self.found_i2c_devices: dict[str, list[int]] = {}
        self._lock = asyncio.Lock()
        self._pcf_states: dict[int, list[int]] = {}
        self._dm117_states: dict[int, dict[int, int]] = {}
        # The DM117 reports levels, so its input edges are derived here rather than
        # inside a driver that latches them.
        self._dm117_edges: dict[int, EdgeTracker[tuple[int, int]]] = {}
        self._read_errors: set[tuple[str, int]] = set()
        # Link health per I2C module for the bus overview, keyed like _read_errors.
        self._health: dict[tuple[str, int], LinkHealth] = defaultdict(LinkHealth)
        # Round trip of the frame whose results are being published right now.
        self._frame_latency: float | None = None
        self._connection_failure_cycles = 0
        self._topology = topology_settings or TopologySettings()
        # How many scans in a row each known module or 1-Wire chip has been
        # missing from. Only the topology watch counts; an explicit scan reports
        # what it just saw.
        self._missing_scans: dict[tuple[str, int], int] = {}
        self._ow_missing_scans: dict[str, int] = {}
        self._topology_task: asyncio.Task | None = None
        self._poll_interval = fast_poll_interval
        self._slow_poll_interval = slow_poll_interval
        self._dm_config: dict[int, dict[int, DeviceType]] = {}
        # Writes claim priority over the poll loop so a button press is not queued
        # behind a full sweep of the bus.
        self._write_pending = 0
        self._writes_idle = asyncio.Event()
        self._writes_idle.set()
        # Background reads (1-Wire) yield to the input poll the same way the poll
        # yields to writes, so a temperature conversion cannot stall the inputs.
        self._poll_idle = asyncio.Event()
        self._poll_idle.set()
        self._last_fast_cycle = 0.0
        self._last_full_cycle = 0.0
        self._frames_last_cycle = 0
        # Addresses the bridge samples for us; empty means Home Assistant reads them.
        self._scan_addresses: list[int] = []
        # The bridge connection the session state above was set up on.
        self._session_generation = 0
        # Set when the bridge reports that its scanner is not configured.
        self._scanner_lost = False
        self.bridge_info: BridgeInfo | None = None
        self._stop_event: asyncio.Event | None = None
        self._poll_task: asyncio.Task | None = None
        self._init_done = asyncio.Event()
        self._init_task: asyncio.Task | None = None
        self.initialization_error: Exception | None = None
        # What probing found behind each DS28E17, kept across topology scans so
        # a board is only identified once per setup.
        self._ds28e17_identity: dict[str, tuple[str, MultisensorComponents | None]] = {}
        self.multisensor = CasaITMultisensorManager(self)
        self.onewire = CasaITOneWireScheduler(self)
        self.outputs = CasaITOutputWriter(self)
        self.restorer = CasaITOutputRestorer(self, power_on_policies)

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
            await self.restorer.async_load()

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
                "frames_per_cycle": self._frames_last_cycle,
                "fast_interval_ms": round(self._poll_interval * 1000, 2),
                "slow_interval_s": self._slow_poll_interval,
                "fast_addresses": [f"0x{address:02X}" for address in sorted(self._fast_pcf_addresses())],
                "bridge_scanned_addresses": [f"0x{address:02X}" for address in self._scan_addresses],
            },
            "topology": {
                "scan_interval_s": self._topology.scan_interval,
                "missing_scans_threshold": self._topology.missing_scans,
                "missing_scans": {
                    f"{code} 0x{address:02X}": count for (code, address), count in sorted(self._missing_scans.items())
                }
                | dict(sorted(self._ow_missing_scans.items())),
            },
            "bridge": {
                "boot_id": f"{self.bridge_info.boot_id:08X}"
                if self.bridge_info and self.bridge_info.boot_id is not None
                else None,
                "uptime_s": self.bridge_info.uptime_s if self.bridge_info else None,
                "active_output_timers": self.outputs.diagnostics(),
            },
            "multisensors": self.multisensor.diagnostic_data,
            "power_on": self.restorer.diagnostics(),
            "onewire_schedule": self.onewire.diagnostic_data,
            "transport": self.bus.stats,
        }

    @property
    def bus_topology(self) -> dict[str, Any]:
        """Return every module and 1-Wire chip with its address and link health.

        Meant for the diagnostics download: it answers which device on the bus
        is slow or unreliable, where the transport counters only tell that
        something is.
        """

        entry = self.hass.config_entries.async_get_entry(self.entry_id)
        options = entry.options if entry is not None else {}
        driver_names = {"IM117": "PCF8574", "OM117": "PCF8574", "DM117": "DM117", "SM117": "DS2482"}

        i2c: list[dict[str, Any]] = []
        for code, addresses in sorted(self.found_i2c_devices.items()):
            for address in sorted(addresses):
                health = self._health.get((driver_names.get(code, code), address))
                i2c.append(
                    {
                        "module": code,
                        "address": f"0x{address:02X}",
                        "name": get_module_name(options, code.lower(), address, ""),
                        "polled": "every cycle"
                        if address in self._fast_pcf_addresses() | self._fast_dm117_addresses()
                        else "slow cycle",
                        "sampled_by_bridge": address in self._scan_addresses,
                        "health": health.as_dict() if health is not None else None,
                    }
                )

        onewire: list[dict[str, Any]] = []
        for bus_address in sorted(self.sm117):
            chips = [
                {
                    "rom": device_id,
                    "chip": meta.get("device_type"),
                    "profile": self.onewire_profile(device_id),
                    "name": meta.get("name"),
                    "components": meta.get("components"),
                    **self.onewire.device_diagnostics(device_id),
                }
                for device_id, meta in sorted(self.ow_devices.items())
                if meta.get("bus_address") == bus_address
            ]
            onewire.append(
                {
                    "bus": f"0x{bus_address:02X}",
                    "name": get_module_name(options, "sm117", bus_address, ""),
                    "chips": chips,
                }
            )

        return {"transport": self.bus.stats, "i2c": i2c, "onewire": onewire}

    def debounce_time(self, module_kind: str, address: int) -> int:
        """Return the configured debounce window of one input module."""

        return self._input_debounce_ms.get(module_kind, {}).get(address, DEFAULT_INPUT_DEBOUNCE_MS)

    def address_signal(self, address: int) -> str:
        """Return the dispatcher signal carrying state changes for one module."""

        return f"{self.state_update_signal}_{address:02x}"

    def power_loss_signal(self, address: int) -> str:
        """Return the dispatcher signal telling that an output module was reset."""

        return f"{self.state_update_signal}_power_loss_{address:02x}"

    def dm117_slot_types(self, address: int) -> dict[int, DeviceType]:
        """Return the slot types configured for one DM117."""

        return dict(self._dm_config.get(address, {}))

    @staticmethod
    def is_output_module(address: int) -> bool:
        """Return True when a PCF8574 address belongs to an OM117."""

        output_range = get_address_range("OM117")
        return output_range is not None and output_range[0] <= address <= output_range[1]

    def edge_signal(self, address: int) -> str:
        """Return the dispatcher signal carrying input edges for one module."""

        return f"{self.state_update_signal}_edge_{address:02x}"

    @asynccontextmanager
    async def write_access(self) -> AsyncIterator[None]:
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

    @asynccontextmanager
    async def _background_access(self) -> AsyncIterator[None]:
        """Claim the bus for a background read, behind both writes and the poll loop.

        1-Wire transactions are long and cannot be interleaved - a ROM select has to
        stay with the transfer it belongs to. Taking the lock only once the poll loop
        is between cycles keeps a temperature conversion from delaying an input edge
        by the length of a whole transaction.
        """

        while True:
            await self._writes_idle.wait()
            await self._poll_idle.wait()
            if self._writes_idle.is_set():
                break

        async with self._lock:
            yield

    async def scan_devices(
        self,
        *,
        device_codes: Iterable[str] | None = None,
        tolerate_misses: bool = False,
    ) -> None:
        """Scan I2C bus for supported devices.

        device_codes limits scanning to the specified codes from I2C_ADDR_RANGES
        (for example, {"IM117", "OM117", "DM117", "SM117"}). When omitted,
        all codes are scanned.

        tolerate_misses keeps a known module in the topology until it has been
        absent from several scans in a row. The topology watch sets it because a
        single missed probe is far more likely to be a busy bus than a module
        that left; a scan the user asked for reports what the bus just said.
        """

        target_codes = set(device_codes) if device_codes else None

        _LOGGER.info("Scanning for I2C devices")
        found_by_code: dict[str, set[int]] = defaultdict(set)
        if target_codes is not None:
            for code, addresses in self.found_i2c_devices.items():
                if code not in target_codes:
                    found_by_code[code].update(addresses)

        for start, end, _, code in I2C_ADDR_RANGES:
            if target_codes and code not in target_codes:
                continue

            for addr in range(start, end + 1):
                try:
                    async with self._lock:
                        await self.bus.write_quick(addr)
                except SMBusProxyError, OSError:
                    continue

                found_by_code[code].add(addr)

        self._apply_miss_tolerance(found_by_code, tolerate=tolerate_misses)

        log_snapshot = {key: sorted(value) for key, value in found_by_code.items()}
        self.found_i2c_devices = log_snapshot
        _LOGGER.info("Found I2C devices: %s", log_snapshot)

        self._refresh_pcf8574(found_by_code)
        self._refresh_dm117(found_by_code)
        await self._refresh_sm117(found_by_code)

        await self.scan_onewire(tolerate_misses=tolerate_misses)
        self._sync_missing_module_issues(found_by_code)
        self._sync_disappeared_device_issues()

    def _apply_miss_tolerance(self, found_by_code: dict[str, set[int]], *, tolerate: bool) -> None:
        """Hold a known module in the topology until it has been absent often enough.

        Modules only leave the bus when someone unplugs one, so a module that
        answered a moment ago and does not answer now is far more likely to have
        lost a probe than to be gone. Dropping it right away would tear down its
        entities and put them back seconds later.
        """

        threshold = self._topology.missing_scans
        for code, previous in self.found_i2c_devices.items():
            for address in previous:
                key = (code, address)
                if address in found_by_code.get(code, ()) or not tolerate:
                    self._missing_scans.pop(key, None)
                    continue

                misses = self._missing_scans[key] = self._missing_scans.get(key, 0) + 1
                if misses < threshold:
                    found_by_code[code].add(address)
                    _LOGGER.debug(
                        "%s at 0x%02X missed %s of %s scans; keeping it for now",
                        code,
                        address,
                        misses,
                        threshold,
                    )
                else:
                    _LOGGER.warning(
                        "%s at 0x%02X has been absent from %s scans in a row; treating it as gone",
                        code,
                        address,
                        misses,
                    )

        still_known = {(code, address) for code, addresses in found_by_code.items() for address in addresses}
        self._missing_scans = {key: count for key, count in self._missing_scans.items() if key in still_known}

    async def async_rescan_devices(self) -> None:
        """Scan for topology changes and reload platforms to expose them."""

        await self.scan_devices()
        await self.hass.config_entries.async_reload(self.entry_id)

    async def start_polling(self) -> None:
        """Start background polling of I2C devices."""

        if self._poll_task:
            return

        self.bridge_info = await self.bus.ping_info()
        if not await self._async_start_input_scanner():
            self._scanner_lost = True
        self._session_generation = self.bus.connection_generation

        self._stop_event = asyncio.Event()
        self._poll_task = self.hass.async_create_background_task(self._poll_loop(), "casait_poll_loop")
        if self._topology.enabled:
            self._topology_task = self.hass.async_create_background_task(self._topology_loop(), "casait_topology_watch")
        self.onewire.start()

    async def stop_polling(self) -> None:
        """Stop background polling task."""

        await self.outputs.async_shutdown()
        if not self._poll_task or not self._stop_event:
            return

        self._stop_event.set()
        await self.onewire.stop()
        await self._poll_task
        self._poll_task = None
        if self._topology_task:
            # Cancelled rather than awaited: the watch may be halfway through a
            # 1-Wire enumeration, and an unload must not sit and wait for that.
            # It only reads, so there is no half-finished write to land.
            self._topology_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._topology_task
            self._topology_task = None
        self._stop_event = None

    async def _topology_loop(self) -> None:
        """Rescan the bus on a slow cadence so a module that left gets noticed.

        Deliberately does not reload the config entry: a reload drops and rebuilds
        every entity, which is far too heavy to happen behind the user's back on a
        timer. Finding out is the job here; acting on it is the repair flow's.
        """

        assert self._stop_event is not None
        while not self._stop_event.is_set():
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=self._topology.scan_interval)
            except TimeoutError:
                pass
            else:
                return

            try:
                await self.scan_devices(tolerate_misses=True)
            except Exception:
                _LOGGER.exception("Error scanning casaIT bus topology")

    async def _poll_loop(self) -> None:
        """Continuously poll devices and dispatch updates."""

        assert self._stop_event is not None
        slow_due = 0.0
        while not self._stop_event.is_set():
            try:
                if self._scanner_lost or self.bus.connection_generation != self._session_generation:
                    await self._async_resume_session()
                    slow_due = 0.0
                include_slow = time.monotonic() >= slow_due
                self._poll_idle.clear()
                await self._poll_cycle(include_slow=include_slow)
                if include_slow:
                    slow_due = time.monotonic() + self._slow_poll_interval
            except Exception:
                _LOGGER.exception("Error polling casaIT devices")
            finally:
                # Background reads get their turn in the gap between cycles.
                self._poll_idle.set()
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

        Every module in the cycle is read through as few batches as the frame limits
        allow. One operation per network round trip is what made the cycle time scale
        with the number of modules; the bus itself was never the bottleneck.
        """

        started = time.monotonic()
        self._frames_last_cycle = 0

        fast_pcf = self._fast_pcf_addresses()
        pcf_addresses = sorted(set(self.im117_om117) if include_slow else fast_pcf)
        dm_addresses = sorted(set(self.dm117) if include_slow else self._fast_dm117_addresses())

        if self._scan_addresses:
            # The bridge samples these itself; fetching its latched transitions
            # replaces reading them here.
            await self._fetch_scanned_inputs()
            pcf_addresses = [address for address in pcf_addresses if address not in self._scan_addresses]

        for batch, modules in self._plan_poll_batches(pcf_addresses, dm_addresses, fast_pcf):
            await self._run_poll_batch(batch, modules)

        duration = time.monotonic() - started
        if include_slow:
            self._last_full_cycle = duration
        else:
            self._last_fast_cycle = duration
        self._sync_bridge_connection_issue()

    async def _async_resume_session(self) -> None:
        """Set the bridge up again after a reconnect, or after it lost its scanner.

        The ping tells a restart of the bridge, which may have cut the modules'
        power as well, from a network drop. Either way the bridge released every
        output timer when the connection went, so cached output bytes are dropped
        and the caller follows up with a full read. Input modules keep their state:
        a bridge that kept scanning hands over the edges from the gap as a
        continuation of it.
        """

        generation = self.bus.connection_generation
        try:
            info = await self.bus.ping_info()
        except SMBusProxyError:
            _LOGGER.exception("Bridge answers with firmware this integration cannot work with")
            return
        if info is None:
            return
        previous, self.bridge_info = self.bridge_info, info
        if previous is not None and info.boot_id is not None and info.boot_id == previous.boot_id:
            _LOGGER.info("Bridge connection was re-established; setting up the session again")
        else:
            _LOGGER.warning("Bridge restarted; setting up the session again and re-reading every module")
        self.outputs.forget_timers()
        for address, device in self.im117_om117.items():
            device.invalidate()
            if self.is_output_module(address):
                device.last_value = -1
        # If setting the scanner up fails, the bridge is still unreachable and the
        # next cycle tries again; until then the inputs are read directly.
        if not await self._async_start_input_scanner():
            return
        self._scanner_lost = False
        self._session_generation = generation

    async def _async_start_input_scanner(self) -> bool:
        """Hand the input addresses to the bridge, which samples them on its own.

        Returns True when the bridge samples every input module, or there is none.
        Until it does, the poll loop reads the inputs itself with the drivers' full
        debounce window.
        """

        self._scan_addresses = []
        addresses = sorted(self._fast_pcf_addresses())
        for address in addresses:
            if (device := self.im117_om117.get(address)) is not None:
                device.debounce_time = self.debounce_time("im117", address)
        if not addresses:
            return True

        period_ms = max(1, min(255, round(self._poll_interval * 1000)))
        # One debounce value covers every scanned address, so the bridge gets the
        # smallest one configured. A module asking for more keeps the difference in
        # its driver below.
        debounce_ms = min(255, *(self.debounce_time("im117", address) for address in addresses))
        try:
            async with self.write_access():
                accepted = await self.bus.scan_config(addresses, period_ms, debounce_ms)
        except OSError as exc:
            _LOGGER.warning("Could not set up the bridge input scanner: %s", exc)
            return False

        if not accepted:
            _LOGGER.error("Bridge refused the input scanner for %s", ", ".join(f"0x{a:02X}" for a in addresses))
            return False

        self._scan_addresses = addresses
        # The bridge debounces with a clock that is not subject to network jitter,
        # so the driver must not debounce the same window a second time - only the
        # part the bridge did not cover, which is nothing unless this module was
        # configured above the shared floor.
        for address in addresses:
            device = self.im117_om117.get(address)
            if device is not None:
                device.debounce_time = max(0, self.debounce_time("im117", address) - debounce_ms)
        _LOGGER.info(
            "Bridge samples %s input modules every %s ms; Home Assistant only collects the edges",
            len(addresses),
            period_ms,
        )
        return True

    async def _fetch_scanned_inputs(self) -> None:
        """Collect and publish the transitions the bridge latched for us."""

        await self._writes_idle.wait()

        try:
            async with self._lock:
                started = time.monotonic()
                flags, entries = await self.bus.scan_fetch()
                self._frame_latency = time.monotonic() - started
        except Exception as exc:  # noqa: BLE001
            for address in self._scan_addresses:
                self._record_read_error("PCF8574", address, exc)
                self._drop_state(self._pcf_states, address)
            return

        self._frames_last_cycle += 1

        if flags & SCAN_FLAG_UNCONFIGURED:
            _LOGGER.warning("Bridge no longer scans the inputs; setting its scanner up again")
            self._scanner_lost = True
            return

        if flags & SCAN_FLAG_OVERFLOW:
            # Snapshots were dropped, so the edges no longer form a complete
            # sequence. Re-baseline instead of reporting transitions that would be
            # wrong, the same way a driver treats its very first read.
            _LOGGER.warning("Bridge input queue overflowed; re-baselining input state")
            for address in self._scan_addresses:
                device = self.im117_om117.get(address)
                if device is not None:
                    device.last_value = -1

        sampled_at = time.monotonic() * 1000
        for index, value in entries:
            if index >= len(self._scan_addresses):
                continue
            address = self._scan_addresses[index]
            device = self.im117_om117.get(address)
            if device is None:
                continue
            self._publish_pcf_reading(address, device.apply_reading(value, sampled_at))
        self._frame_latency = None

    def _plan_poll_batches(
        self,
        pcf_addresses: list[int],
        dm_addresses: list[int],
        fast_pcf: set[int],
    ) -> list[tuple[I2CBatch, list[_PolledModule]]]:
        """Pack the cycle's reads into as few frames as the batch limits allow.

        Splitting is driven by ``capacity_for`` rather than by counting bytes here, so
        the frame and result limits stay owned by the transport.
        """

        planned: list[tuple[I2CBatch, list[_PolledModule]]] = []
        batch = self.bus.new_batch()
        modules: list[_PolledModule] = []
        results = 0
        rearms_left = MAX_REARMS_PER_CYCLE

        def flush() -> None:
            nonlocal batch, modules, results
            if modules:
                planned.append((batch, modules))
            batch = self.bus.new_batch()
            modules = []
            results = 0

        for address in pcf_addresses:
            device = self.im117_om117.get(address)
            if device is None:
                continue

            is_input = address in fast_pcf
            rearm = device.needs_rearm(is_input) and rearms_left > 0
            if rearm:
                rearms_left -= 1
            # write_byte + delay + read_byte, or just read_byte when already armed.
            request_bytes = 3 + 2 + 2 if rearm else 2
            if not batch.capacity_for(request_bytes=request_bytes, result_bytes=1):
                flush()

            modules.append(_PolledModule("pcf", address, len(batch), results, 1, rearmed=rearm, is_input=is_input))
            if rearm:
                batch.write_byte(address, 0xFF).delay(PCF_REARM_SETTLE_MS)
            batch.read_byte(address)
            results += 1

        for address in dm_addresses:
            device = self.dm117.get(address)
            if device is None or device.cached_ports() is not None:
                continue

            size = device.expected_response_size()
            # write_byte + delay + read_block
            if not batch.capacity_for(request_bytes=3 + 2 + 3, result_bytes=size):
                flush()

            modules.append(_PolledModule("dm117", address, len(batch), results, size))
            batch.write_byte(address, device.CMD_READ).delay(1).read_block(address, size)
            results += size

        flush()
        return planned

    async def _run_poll_batch(self, batch: I2CBatch, modules: list[_PolledModule]) -> None:
        """Execute one batch and publish each module's result."""

        await self._writes_idle.wait()

        try:
            async with self._lock:
                started = time.monotonic()
                results = await self.bus.execute_batch(batch)
                self._frame_latency = time.monotonic() - started
        except I2CBatchError as exc:
            await self._recover_failed_batch(modules, exc)
            return
        except Exception as exc:  # noqa: BLE001
            # The whole frame was lost, so nothing can be attributed to one module.
            for module in modules:
                self._fail_module(module, exc)
            return

        self._frames_last_cycle += 1
        sampled_at = time.monotonic() * 1000
        for module in modules:
            values = results[module.result_start : module.result_start + module.result_count]
            self._publish_module(module, values, sampled_at)
        self._frame_latency = None

    async def _recover_failed_batch(self, modules: list[_PolledModule], exc: I2CBatchError) -> None:
        """Attribute a batch failure to one module and re-read the rest on their own.

        The bridge aborts the whole batch at the first failing operation, so the
        results of healthy modules in the same frame are lost even though their reads
        would have succeeded. Reading them individually keeps one bad module from
        dropping everyone else's state.
        """

        self._frames_last_cycle += 1
        culprit = self._module_for_op(modules, exc.op_index)
        if culprit is not None:
            self._fail_module(culprit, exc)

        for module in modules:
            if module is culprit:
                continue
            if module.kind == "pcf":
                await self._poll_pcf8574(module.address, is_input=module.is_input)
            else:
                await self._poll_dm117(module.address)

    @staticmethod
    def _module_for_op(modules: list[_PolledModule], op_index: int | None) -> _PolledModule | None:
        """Return the module owning the given batch operation index."""

        if op_index is None:
            return None

        culprit: _PolledModule | None = None
        for module in modules:
            if module.first_op <= op_index:
                culprit = module
            else:
                break
        return culprit

    def _fail_module(self, module: _PolledModule, exc: Exception | None = None) -> None:
        """Record a failed read and drop the module's cached state."""

        if module.kind == "pcf":
            device = self.im117_om117.get(module.address)
            if device is not None:
                device.note_read_error()
            self._record_read_error("PCF8574", module.address, exc)
            self._drop_state(self._pcf_states, module.address)
            return

        self._record_read_error("DM117", module.address, exc)
        self._drop_state(self._dm117_states, module.address)

    def _publish_module(self, module: _PolledModule, values: list[int], sampled_at: float) -> None:
        """Decode one module's batch results and dispatch what changed."""

        if module.kind == "pcf":
            device = self.im117_om117.get(module.address)
            if device is None:
                return
            if module.rearmed:
                device.note_rearmed()
            self._publish_pcf_reading(module.address, device.apply_reading(values[0], sampled_at))
            return

        device = self.dm117.get(module.address)
        if device is None:
            return
        self._publish_dm117_reading(module.address, device.decode_response(values), device)

    def _sync_bridge_connection_issue(self) -> None:
        """Raise a repair issue when the live bridge remains disconnected."""

        issue_id = f"bridge_unavailable_{self.entry_id}"
        if bool(self.bus.stats["connected"]):
            self._connection_failure_cycles = 0
            ir.async_delete_issue(self.hass, DOMAIN, issue_id)
            return

        self._connection_failure_cycles += 1
        if self._connection_failure_cycles < 3:
            return
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            issue_id,
            data={"entry_id": self.entry_id},
            is_fixable=True,
            is_persistent=True,
            severity=ir.IssueSeverity.ERROR,
            translation_key="bridge_unavailable",
        )

    async def _poll_pcf8574(self, address: int, *, is_input: bool) -> None:
        """Read one PCF8574 and publish state changes plus any input edges."""

        device = self.im117_om117.get(address)
        if device is None:
            return

        await self._writes_idle.wait()

        try:
            async with self._lock:
                started = time.monotonic()
                reading = await device.read_ports(is_input)
                self._frame_latency = time.monotonic() - started
        except Exception as exc:  # noqa: BLE001
            self._record_read_error("PCF8574", address, exc)
            self._drop_state(self._pcf_states, address)
            return

        self._publish_pcf_reading(address, reading)
        self._frame_latency = None

    def _publish_pcf_reading(self, address: int, reading: PCF8574Reading) -> None:
        """Cache one PCF8574 reading and dispatch state changes plus input edges."""

        if not reading.ok:
            self._record_read_error("PCF8574", address)
            self._drop_state(self._pcf_states, address)
            return

        self._clear_read_error("PCF8574", address)
        previous = self._pcf_states.get(address)
        self._pcf_states[address] = reading.port_states
        if self.is_output_module(address):
            self.restorer.check_pcf(address, reading.value)

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
                started = time.monotonic()
                port_states = await device.read_ports()
                self._frame_latency = time.monotonic() - started
        except Exception as exc:  # noqa: BLE001
            self._record_read_error("DM117", address, exc)
            self._drop_state(self._dm117_states, address)
            return

        self._publish_dm117_reading(address, port_states, device)
        self._frame_latency = None

    def _publish_dm117_reading(self, address: int, port_states: dict[int, int] | None, device: DM117) -> None:
        """Cache one DM117 reading and dispatch its state changes plus input edges."""

        if port_states is None:
            self._record_read_error("DM117", address)
            self._drop_state(self._dm117_states, address)
            self._dm117_edges.pop(address, None)
            return

        self._clear_read_error("DM117", address)
        previous = self._dm117_states.get(address)
        self._dm117_states[address] = dict(port_states)
        slots_lost = any(
            device.last_port_types.get(slot) is not expected
            for slot, expected in self._dm_config.get(address, {}).items()
        )
        self.restorer.check_dm117(address, port_states, slots_lost)

        if edges := self._dm117_input_edges(address, port_states):
            async_dispatcher_send(self.hass, self.edge_signal(address), edges)
        if previous != port_states:
            async_dispatcher_send(self.hass, self.address_signal(address))
        self._sync_dm117_configuration_issues(address, device)

    def _dm117_input_edges(self, address: int, port_states: Mapping[int, int]) -> dict[tuple[int, int], list[bool]]:
        """Debounce the input slots of one DM117 and return their transitions.

        Unlike the PCF8574, the DM117 reports a level rather than latching edges,
        so the transitions have to be derived here - which is the same work the
        input driver does, done in the same way.
        """

        input_slots = [slot for slot, kind in self._dm_config.get(address, {}).items() if kind is DeviceType.INPUT]
        if not input_slots:
            return {}

        tracker = self._dm117_edges.get(address)
        if tracker is None:
            tracker = self._dm117_edges[address] = EdgeTracker[tuple[int, int]](self.debounce_time("dm117", address))

        sample: dict[tuple[int, int], bool] = {}
        for slot in input_slots:
            if (raw := port_states.get(slot)) is None:
                continue
            channels = PortConfig.from_raw(raw)
            sample[slot, 0] = bool(channels.port_a)
            sample[slot, 1] = bool(channels.port_b)

        return tracker.apply(sample)

    def _sync_missing_module_issues(self, found_by_code: Mapping[str, set[int]]) -> None:
        """Create or clear repair issues for explicitly configured modules."""

        code_by_kind = {"im117": "IM117", "om117": "OM117", "dm117": "DM117", "sm117": "SM117"}
        for module_kind, addresses in self._configured_module_addresses.items():
            found = found_by_code.get(code_by_kind[module_kind], set())
            for address in addresses:
                issue_id = f"module_missing_{self.entry_id}_{module_kind}_{address}"
                if address in found:
                    ir.async_delete_issue(self.hass, DOMAIN, issue_id)
                    continue
                ir.async_create_issue(
                    self.hass,
                    DOMAIN,
                    issue_id,
                    data={"entry_id": self.entry_id, "module": module_kind, "address": address},
                    is_fixable=True,
                    is_persistent=False,
                    severity=ir.IssueSeverity.ERROR,
                    translation_key="module_missing",
                    translation_placeholders={"module": module_kind.upper(), "address": f"0x{address:02X}"},
                )

    def _sync_dm117_configuration_issues(self, address: int, device: DM117) -> None:
        """Compare configured DM117 slot roles with the module response."""

        for slot, expected in self._dm_config.get(address, {}).items():
            actual = device.last_port_types.get(slot)
            issue_id = f"dm117_config_mismatch_{self.entry_id}_{address}_{slot}"
            if actual is expected:
                ir.async_delete_issue(self.hass, DOMAIN, issue_id)
                continue
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                issue_id,
                data={
                    "entry_id": self.entry_id,
                    "address": address,
                    "slot": slot,
                    "expected": expected.value,
                },
                is_fixable=True,
                is_persistent=False,
                severity=ir.IssueSeverity.ERROR,
                translation_key="dm117_config_mismatch",
                translation_placeholders={
                    "address": f"0x{address:02X}",
                    "slot": str(slot + 1),
                    "expected": expected.value,
                    "actual": actual.value if actual is not None else "missing",
                },
            )

    @property
    def current_device_identifiers(self) -> set[tuple[str, str]]:
        """Return identifiers for every device currently present on the bridge."""

        identifiers = {(DOMAIN, build_device_identifier(self.entry_id, "bridge", "controller"))}
        for code, addresses in self.found_i2c_devices.items():
            module_kind = code.lower()
            for address in addresses:
                identifier_address: str | int = f"{address:02x}" if module_kind == "sm117" else address
                identifiers.add((DOMAIN, build_device_identifier(self.entry_id, module_kind, identifier_address)))
        identifiers.update(
            (DOMAIN, build_device_identifier(self.entry_id, "onewire", device_id)) for device_id in self.ow_ids
        )
        return identifiers

    def _sync_disappeared_device_issues(self) -> None:
        """Report registry devices the bus no longer answers for.

        Nothing is deleted here. A module goes missing either because someone
        removed it on purpose or because a connector worked loose, and only the
        user can tell those apart - so the device keeps its entities, its history
        and its place in automations until the repair flow is answered.
        """

        device_registry = dr.async_get(self.hass)
        current = self.current_device_identifiers
        for device in dr.async_entries_for_config_entry(device_registry, self.entry_id):
            integration_identifiers = {identifier for identifier in device.identifiers if identifier[0] == DOMAIN}
            if not integration_identifiers:
                continue

            identifier = min(value for _, value in integration_identifiers)
            issue_id = f"device_gone_{self.entry_id}_{identifier}"
            if not integration_identifiers.isdisjoint(current):
                ir.async_delete_issue(self.hass, DOMAIN, issue_id)
                continue

            # The name is carried in the issue data as well as in the placeholders:
            # the placeholders only reach the issue itself, while the repair flow
            # has to fill the same name into its own steps.
            name = device.name_by_user or device.name or identifier
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                issue_id,
                data={"entry_id": self.entry_id, "identifier": identifier, "name": name},
                is_fixable=True,
                is_persistent=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key="device_gone",
                translation_placeholders={"name": name},
            )

    def _drop_state(self, states: dict[int, Any], address: int) -> None:
        """Forget a module's cached state and tell its entities it went away."""

        if states.pop(address, None) is not None:
            async_dispatcher_send(self.hass, self.address_signal(address))

    def _record_read_error(self, device_type: str, address: int, exc: Exception | None = None) -> None:
        """Log a device read failure only when it first becomes unavailable."""

        key = (device_type, address)
        self._health[key].failure(time.time(), str(exc) if exc is not None else "no data")
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
        self._health[key].success(time.time(), self._frame_latency)
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
                self.im117_om117[addr] = PCF8574(
                    self.bus, addr, debounce_time=self.debounce_time("im117", addr) if addr in input_addresses else 0
                )

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
                self.sm117[addr] = await OneWireBus.create(self.bus, addr)

        for addr in list(self.sm117):
            if addr not in found:
                del self.sm117[addr]

    async def scan_onewire(self, *, tolerate_misses: bool = False) -> None:
        """Scan all detected SM117 bridges for 1-Wire devices.

        tolerate_misses has the same meaning as in scan_devices: a chip keeps its
        place until it has been absent from several enumerations in a row.
        """

        if not self.sm117:
            self.ow_devices = {}
            self.ow_ids = set()
            self._ow_missing_scans.clear()
            return

        discovered: dict[str, dict[str, Any]] = {}

        for addr, ow_bus in self.sm117.items():
            try:
                async with self._lock:
                    devices = await ow_bus.scan_devices(True)
            except Exception as exc:  # noqa: BLE001
                _LOGGER.warning("Error scanning 1-Wire bus at 0x%02x: %s", addr, exc)
                continue

            for device_id, meta in devices.items():
                discovered[device_id] = {"bus_address": addr, **meta}
                if name := self._onewire_names.get(device_id):
                    discovered[device_id]["name"] = name

        self._apply_onewire_miss_tolerance(discovered, tolerate=tolerate_misses)

        self.ow_devices = discovered
        self.ow_ids = set(discovered)
        await self._identify_ds28e17()
        self._configure_onewire_schedule()

        if discovered:
            _LOGGER.info("Discovered OneWire devices: %s", list(discovered.keys()))
        else:
            _LOGGER.info("No OneWire devices discovered")

    async def _identify_ds28e17(self) -> None:
        """Find out what sits behind every DS28E17 and set up the Multisensors.

        The family code only names the bridge chip. An LED controller and a
        Multisensor are told apart by which I2C addresses answer behind it.
        """

        await self.multisensor.async_load()
        for device_id, meta in self.ow_devices.items():
            if meta.get("family_code") != DS28E17_FAMILY:
                continue
            identity = self._ds28e17_identity.get(device_id)
            if identity is None:
                identity = await self.multisensor.async_detect(device_id)
                known = self.multisensor.known_components(device_id)
                if identity is None and known is not None and known.any:
                    # None of its sensors answered this time, but it has been a
                    # Multisensor before. Keep it one, so its entities stay and
                    # the missing chips get reported instead of the board
                    # turning into an LED controller.
                    identity = (OW_PROFILE_MULTISENSOR, known)
                if identity is None:
                    continue
                profile, found = identity
                if profile == OW_PROFILE_MULTISENSOR and found is not None:
                    identity = (profile, await self.multisensor.async_remember(device_id, found))
                self._ds28e17_identity[device_id] = identity
            meta["detected_profile"], components = identity

            profile = self._onewire_profiles.get(device_id) or default_onewire_profile(meta)
            if profile != OW_PROFILE_MULTISENSOR:
                continue
            if components is None:
                # Configured as a Multisensor although the LED firmware answered,
                # or detection never ran: probe the sensors now.
                try:
                    components = await self.async_onewire_job(
                        device_id, lambda bus, rom=device_id: bus.multisensor.detect(rom)
                    )
                except DS28E17Error as err:
                    _LOGGER.warning("Could not probe the sensors of %s: %s", device_id, err)
                    continue
                self._ds28e17_identity[device_id] = (meta["detected_profile"], components)
            meta["components"] = components.as_list()
            self.multisensor.register(device_id, components)

        self.multisensor.unregister_missing(set(self.ow_devices))

    def onewire_profile(self, device_id: str) -> str | None:
        """Return the effective profile of one 1-Wire chip: configured, detected, or by family."""

        meta = self.ow_devices.get(device_id)
        if meta is None:
            return None
        return self._onewire_profiles.get(device_id) or default_onewire_profile(meta)

    async def async_onewire_job[T](
        self,
        device_id: str,
        func: Callable[[OneWireBus], Awaitable[T]],
        *,
        write: bool = False,
    ) -> T:
        """Run one transaction against the bus a 1-Wire chip sits on.

        Writes take the priority lane, everything else waits for a gap between
        poll cycles like the other 1-Wire reads.
        """

        bus = self._get_onewire_bus(device_id)
        if bus is None:
            raise DS28E17Error(f"1-Wire device {device_id} is not on any bus")

        access = self.write_access() if write else self._background_access()
        async with access:
            return await func(bus)

    def _apply_onewire_miss_tolerance(self, discovered: dict[str, dict[str, Any]], *, tolerate: bool) -> None:
        """Keep a known 1-Wire chip listed until it has been absent often enough.

        A 1-Wire enumeration is the most failure-prone thing on the bus - one
        marginal contact is enough to lose a chip for a single pass - so this
        tolerance matters more here than it does for the I2C modules.
        """

        threshold = self._topology.missing_scans
        for device_id, meta in self.ow_devices.items():
            if device_id in discovered or not tolerate:
                self._ow_missing_scans.pop(device_id, None)
                continue

            misses = self._ow_missing_scans[device_id] = self._ow_missing_scans.get(device_id, 0) + 1
            if misses < threshold:
                discovered[device_id] = meta
                _LOGGER.debug(
                    "1-Wire device %s missed %s of %s scans; keeping it for now", device_id, misses, threshold
                )
            else:
                _LOGGER.warning(
                    "1-Wire device %s has been absent from %s scans in a row; treating it as gone",
                    device_id,
                    misses,
                )

        self._ow_missing_scans = {
            device_id: count for device_id, count in self._ow_missing_scans.items() if device_id in discovered
        }

    def _configure_onewire_schedule(self) -> None:
        """Hand the scheduler every chip the last scan found, with its profile."""

        profiles = {
            device_id: profile
            for device_id in self.ow_devices
            if (profile := self.onewire_profile(device_id)) is not None
        }
        self.onewire.configure(profiles, self._onewire_poll_intervals)

    async def async_configure_dm117(self, slot_config: Mapping[int, Mapping[int, DeviceType]]) -> None:
        """Configure DM117 modules based on slot configuration."""

        for address, config in slot_config.items():
            if not config:
                continue
            device = self.dm117.get(address)
            if not device:
                continue

            await device.configure_ports(dict(config))

    def _get_onewire_bus(self, device_id: str) -> OneWireBus | None:
        """Return the OneWire bus for a given device id."""

        meta = self.ow_devices.get(device_id)
        if not meta:
            return None

        return self.sm117.get(meta["bus_address"])

    async def write_ds2413_state(self, device_id: str, channel: int, value: bool) -> bool:
        """Write a binary state to a DS2413 channel."""

        bus = self._get_onewire_bus(device_id)
        if not bus:
            return False

        async with self.write_access():
            pins = await bus.ds2413.set_state(device_id, channel, value)
        if pins is None:
            return False
        self.restorer.note_ds2413_written(device_id, channel, value)
        self.onewire.set_value(device_id, pins)
        return True

    async def read_led_config(self, device_id: str, *, use_cache: bool = True) -> LEDConfig | None:
        """Read the LED controller configuration for a device."""

        bus = self._get_onewire_bus(device_id)
        if not bus:
            return None

        async with self._background_access():
            return await bus.read_led_config(device_id, use_cache)

    async def write_led_config(self, device_id: str, config: LEDConfig) -> bool:
        """Write an LED controller configuration for a device."""

        bus = self._get_onewire_bus(device_id)
        if not bus:
            return False

        async with self.write_access():
            written = await bus.write_led_config(device_id, config)
        if written:
            self.restorer.note_led_written(device_id, config)
            self.onewire.set_value(device_id, config)
        return written

    async def async_write_pcf_port(self, address: int, port: int, state: int) -> bool:
        """Write one PCF8574 port and publish the resulting state."""

        return await self.async_write_pcf_ports(address, {port: state})

    async def async_write_pcf_ports(self, address: int, changes: Mapping[int, int]) -> bool:
        """Write several ports of one PCF8574 at once and publish the resulting state.

        Writes from concurrent callers are coalesced into one frame, so outputs
        switched together, even on different modules, change within milliseconds.
        """

        if address not in self.im117_om117:
            return False
        return await self.outputs.async_write(address, changes)

    async def async_arm_output_timer(self, address: int, mask: int, value: int, revert: int, seconds: float) -> bool:
        """Have the bridge restore ``revert`` on the ``mask`` bits of a module after ``seconds``.

        Returns False when the bridge cannot do it, which leaves stopping to the caller.
        """

        return await self.outputs.async_arm_timer(address, mask, value, revert, seconds)

    def publish_pcf_write(self, address: int) -> None:
        """Publish the port states of a module whose write was just verified."""

        if (device := self.im117_om117.get(address)) is None:
            return
        # The write was read back, so the driver's port_states are authoritative.
        # Polling again would only add latency.
        if self.is_output_module(address):
            self.restorer.note_pcf_written(address, device.last_value)
        previous = self._pcf_states.get(address)
        self._pcf_states[address] = list(device.port_states)
        if previous != device.port_states:
            async_dispatcher_send(self.hass, self.address_signal(address))

    async def async_write_dm117_port(self, address: int, config: DM117PortConfig) -> bool:
        """Write a DM117 port and publish the resulting state."""

        device = self.dm117.get(address)
        if device is None:
            return False

        async with self.write_access():
            written = await device.write_port(config)

        if not written:
            return False

        # The DM117 does not echo writes back, so mirror the value the driver sent.
        # For a ramped dimmer that is the target value, which is what HA should show.
        states = dict(self._dm117_states.get(address) or {})
        states[config.port] = device.last_values.get(config.port, 0)
        self.restorer.note_dm117_written(address, config.port, states[config.port])
        previous = self._dm117_states.get(address)
        self._dm117_states[address] = states
        if previous != states:
            async_dispatcher_send(self.hass, self.address_signal(address))
        return True
