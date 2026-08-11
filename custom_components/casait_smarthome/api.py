"""API for casaIT devices."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import AsyncIterator, Iterable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from functools import partial
import logging
import time
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, issue_registry as ir
from homeassistant.helpers.dispatcher import async_dispatcher_send

from .const import (
    DEFAULT_FAST_POLL_INTERVAL,
    DEFAULT_INPUT_DEBOUNCE_MS,
    DEFAULT_OW_POLL_INTERVAL,
    DEFAULT_OW_PROFILE,
    DEFAULT_SLOW_POLL_INTERVAL,
    DOMAIN,
    I2C_ADDR_RANGES,
    SIGNAL_STATE_UPDATED,
)
from .helpers import OM117PairConfig, build_device_identifier, get_address_range
from .services.i2cClasses.dm117 import DM117, DeviceType, DM117PortConfig
from .services.i2cClasses.ds2438 import DS2438Reading
from .services.i2cClasses.led_controller import LEDConfig
from .services.i2cClasses.oneWireBus import OneWireBus
from .services.i2cClasses.pcf8574 import PCF8574, PCF8574Reading
from .services.smbus_proxy import SCAN_FLAG_OVERFLOW, I2CBatch, I2CBatchError, SMBus, SMBusProxyError

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
        self._read_errors: set[tuple[str, int]] = set()
        self._connection_failure_cycles = 0
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
                "frames_per_cycle": self._frames_last_cycle,
                "fast_interval_ms": round(self._poll_interval * 1000, 2),
                "slow_interval_s": self._slow_poll_interval,
                "fast_addresses": [f"0x{address:02X}" for address in sorted(self._fast_pcf_addresses())],
                "bridge_scanned_addresses": [f"0x{address:02X}" for address in self._scan_addresses],
            },
            "transport": self.bus.stats,
        }

    def debounce_time(self, module_kind: str, address: int) -> int:
        """Return the configured debounce window of one input module."""

        return self._input_debounce_ms.get(module_kind, {}).get(address, DEFAULT_INPUT_DEBOUNCE_MS)

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
    ) -> None:
        """Scan I2C bus for supported devices.

        device_codes limits scanning to the specified codes from I2C_ADDR_RANGES
        (for example, {"IM117", "OM117", "DM117", "SM117"}). When omitted,
        all codes are scanned.
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
        self._sync_missing_module_issues(found_by_code)
        self._remove_stale_registry_devices()

    async def async_rescan_devices(self) -> None:
        """Scan for topology changes and reload platforms to expose them."""

        await self.scan_devices()
        await self.hass.config_entries.async_reload(self.entry_id)

    async def start_polling(self) -> None:
        """Start background polling of I2C devices."""

        if self._poll_task:
            return

        await self._async_start_input_scanner()

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

    async def _async_start_input_scanner(self) -> None:
        """Hand the input addresses to the bridge, if it can sample them itself.

        Probed once per config entry. A bridge on older firmware does not answer the
        command, so this costs the transport's whole retry budget once before falling
        back to reading the inputs here - which is why it is never retried.
        """

        self._scan_addresses = []
        addresses = sorted(self._fast_pcf_addresses())
        if not addresses:
            return

        period_ms = max(1, min(255, round(self._poll_interval * 1000)))
        # One debounce value covers every scanned address, so the bridge gets the
        # smallest one configured. A module asking for more keeps the difference in
        # its driver below.
        debounce_ms = min(255, *(self.debounce_time("im117", address) for address in addresses))
        config_job = partial(self.bus.scan_config, addresses, period_ms, debounce_ms)
        try:
            async with self._write_access():
                accepted = await self.hass.async_add_executor_job(config_job)
        except Exception:
            _LOGGER.exception("Failed to configure the bridge input scanner")
            return

        if not accepted:
            return

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

    async def _fetch_scanned_inputs(self) -> None:
        """Collect and publish the transitions the bridge latched for us."""

        await self._writes_idle.wait()

        try:
            async with self._lock:
                flags, entries = await self.hass.async_add_executor_job(self.bus.scan_fetch)
        except Exception as exc:  # noqa: BLE001
            for address in self._scan_addresses:
                self._record_read_error("PCF8574", address, exc)
                self._drop_state(self._pcf_states, address)
            return

        self._frames_last_cycle += 1

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
                results = await self.hass.async_add_executor_job(self.bus.execute_batch, batch)
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
                reading = await self.hass.async_add_executor_job(device.read_ports, is_input)
        except Exception as exc:  # noqa: BLE001
            self._record_read_error("PCF8574", address, exc)
            self._drop_state(self._pcf_states, address)
            return

        self._publish_pcf_reading(address, reading)

    def _publish_pcf_reading(self, address: int, reading: PCF8574Reading) -> None:
        """Cache one PCF8574 reading and dispatch state changes plus input edges."""

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

        self._publish_dm117_reading(address, port_states, device)

    def _publish_dm117_reading(self, address: int, port_states: dict[int, int] | None, device: DM117) -> None:
        """Cache one DM117 reading and dispatch when its port values changed."""

        if port_states is None:
            self._record_read_error("DM117", address)
            self._drop_state(self._dm117_states, address)
            return

        self._clear_read_error("DM117", address)
        previous = self._dm117_states.get(address)
        self._dm117_states[address] = dict(port_states)

        if previous != port_states:
            async_dispatcher_send(self.hass, self.address_signal(address))
        self._sync_dm117_configuration_issues(address, device)

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

    def _remove_stale_registry_devices(self) -> None:
        """Remove registry devices that disappeared from a complete scan."""

        device_registry = dr.async_get(self.hass)
        current = self.current_device_identifiers
        for device in dr.async_entries_for_config_entry(device_registry, self.entry_id):
            integration_identifiers = {identifier for identifier in device.identifiers if identifier[0] == DOMAIN}
            if integration_identifiers and integration_identifiers.isdisjoint(current):
                device_registry.async_remove_device(device.id)

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

        async with self._background_access():
            return await self.hass.async_add_executor_job(bus.read_temperature, device_id)

    async def read_ds2438(self, device_id: str) -> DS2438Reading | None:
        """Read values from a DS2438 device."""

        bus = self._get_onewire_bus(device_id)
        if not bus:
            return None

        async with self._background_access():
            return await self.hass.async_add_executor_job(
                bus.ds2438.get_reading, device_id, bus.get_interval(device_id)
            )

    async def read_ds2413_state(self, device_id: str, channel: int, *, invert: bool = True) -> bool | None:
        """Read a binary state from a DS2413 channel."""

        bus = self._get_onewire_bus(device_id)
        if not bus:
            return None

        read_job = partial(bus.read_binary_state, device_id, channel, invert=invert)
        async with self._background_access():
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
        async with self._background_access():
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
