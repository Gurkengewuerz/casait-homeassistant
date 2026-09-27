"""Tests for batched bus access: frame packing, result slicing and failure isolation."""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

from custom_components.casait_smarthome.api import CasaITApi
from custom_components.casait_smarthome.services.i2cClasses.dm117 import DM117, DeviceType
from custom_components.casait_smarthome.services.i2cClasses.pcf8574 import PCF8574
from custom_components.casait_smarthome.services.smbus_proxy import (
    CMD_SCAN_CONFIG,
    CMD_SCAN_FETCH,
    MAX_BATCH_RESULTS,
    MAX_FRAME_PAYLOAD,
    MAX_SCAN_ADDRESSES,
    MAX_SCAN_ENTRIES,
    SCAN_FLAG_OVERFLOW,
    I2CBatch,
    I2CBatchError,
    SMBus,
)


class FakeBus:
    """Transport double that records batches and replays canned results."""

    stats = {"connected": True}

    def __init__(self, results: list[int] | None = None, error: Exception | None = None) -> None:
        self.results = results or []
        self.error = error
        self.executed: list[I2CBatch] = []
        self.reads: list[int] = []

    def new_batch(self) -> I2CBatch:
        return I2CBatch()

    async def execute_batch(self, batch: I2CBatch) -> list[int]:
        self.executed.append(batch)
        if self.error is not None:
            raise self.error
        return self.results[: batch.result_count]

    async def write_byte(self, addr: int, value: int) -> None:
        return

    async def read_byte(self, addr: int) -> int:
        self.reads.append(addr)
        return 0xFF


def _pcf(api: CasaITApi, address: int, bus: FakeBus, *, armed: bool = True) -> PCF8574:
    device = PCF8574(bus, address)
    if armed:
        device.note_rearmed()
    api.im117_om117[address] = device
    return device


# ---------------------------------------------------------------------------
# I2CBatch limits
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_batch_reports_capacity_and_refuses_overflow() -> None:
    batch = I2CBatch()
    assert batch.capacity_for(request_bytes=2, result_bytes=1) > 0

    for _ in range(8):
        batch.read_byte(0x38)
    assert batch.result_count == 8
    assert len(bytes(batch)) == 1 + 8 * 2

    with pytest.raises(ValueError, match="Block read count"):
        batch.read_block(0x10, MAX_BATCH_RESULTS + 1)


@pytest.mark.unit
def test_batch_stops_at_the_frame_payload_limit() -> None:
    batch = I2CBatch()

    def fill() -> None:
        for _ in range(MAX_FRAME_PAYLOAD):
            batch.write_byte_data(0x10, 0x01, 0x02)

    with pytest.raises(ValueError, match="frame payload"):
        fill()


@pytest.mark.unit
def test_capacity_reaches_zero_before_the_result_limit() -> None:
    batch = I2CBatch()
    batch.read_block(0x10, MAX_BATCH_RESULTS - 1)
    assert batch.capacity_for(request_bytes=2, result_bytes=1) == 1
    batch.read_byte(0x38)
    assert batch.capacity_for(request_bytes=2, result_bytes=1) == 0


# ---------------------------------------------------------------------------
# execute_batch / read_i2c_block wire handling
# ---------------------------------------------------------------------------


def _bus(monkeypatch) -> SMBus:
    return SMBus()


def _respond(payload: bytes):
    """Return a stand-in for _send_command that answers with ``payload``."""

    async def send(_payload: bytes) -> bytes:
        return payload

    return send


@pytest.mark.unit
async def test_execute_batch_slices_results_in_order(monkeypatch) -> None:
    bus = _bus(monkeypatch)
    batch = bus.new_batch().read_byte(0x38).read_block(0x10, 3)
    monkeypatch.setattr(bus, "_send_command", _respond(b"\x00\xaa\x01\x02\x03"))

    assert await bus.execute_batch(batch) == [0xAA, 0x01, 0x02, 0x03]


@pytest.mark.unit
async def test_execute_batch_reports_the_failing_operation_index(monkeypatch) -> None:
    bus = _bus(monkeypatch)
    batch = bus.new_batch().read_byte(0x38).read_byte(0x39)
    monkeypatch.setattr(bus, "_send_command", _respond(b"\xff\x01"))

    with pytest.raises(I2CBatchError) as err:
        await bus.execute_batch(batch)

    assert err.value.op_index == 1


@pytest.mark.unit
async def test_execute_batch_rejects_a_short_response(monkeypatch) -> None:
    bus = _bus(monkeypatch)
    batch = bus.new_batch().read_block(0x10, 4)
    monkeypatch.setattr(bus, "_send_command", _respond(b"\x00\x01\x02"))

    with pytest.raises(I2CBatchError):
        await bus.execute_batch(batch)


@pytest.mark.unit
async def test_empty_batch_does_not_reach_the_wire(monkeypatch) -> None:
    bus = _bus(monkeypatch)

    async def fail(_payload: bytes) -> bytes:
        raise AssertionError("empty batch must not be sent")

    monkeypatch.setattr(bus, "_send_command", fail)
    assert await bus.execute_batch(bus.new_batch()) == []


@pytest.mark.unit
async def test_scan_fetch_parses_entries_and_flags(monkeypatch) -> None:
    bus = _bus(monkeypatch)
    monkeypatch.setattr(bus, "_send_command", _respond(b"\x00\x01\x02\x00\xfe\x01\xff"))

    flags, entries = await bus.scan_fetch()

    assert flags & SCAN_FLAG_OVERFLOW
    assert entries == [(0, 0xFE), (1, 0xFF)]


@pytest.mark.unit
def test_scan_limits_match_the_firmware() -> None:
    """Guard the constants shared with modules/src/cb32.cpp.

    The bridge derives its own limits from a 128 byte client buffer. Both sides have to
    agree or a full queue silently truncates on the wire.
    """

    client_rx_buffer = 128
    assert client_rx_buffer - 2 == MAX_FRAME_PAYLOAD
    # Firmware: (((CLIENT_RX_BUFFER - 2) - 3) / 2)
    assert MAX_SCAN_ENTRIES == ((client_rx_buffer - 2) - 3) // 2 == 61
    assert MAX_SCAN_ADDRESSES == 32
    assert SCAN_FLAG_OVERFLOW == 0x01
    assert (CMD_SCAN_CONFIG, CMD_SCAN_FETCH) == (0x12, 0x13)


@pytest.mark.unit
async def test_scan_fetch_accepts_a_full_queue(monkeypatch) -> None:
    bus = _bus(monkeypatch)
    entries = bytes(range(MAX_SCAN_ENTRIES)) + bytes(MAX_SCAN_ENTRIES)
    payload = bytes([0x00, 0x00, MAX_SCAN_ENTRIES]) + bytes(
        byte for index in range(MAX_SCAN_ENTRIES) for byte in (entries[index], 0xAA)
    )
    assert len(payload) <= MAX_FRAME_PAYLOAD
    monkeypatch.setattr(bus, "_send_command", _respond(payload))

    flags, parsed = await bus.scan_fetch()

    assert flags == 0
    assert len(parsed) == MAX_SCAN_ENTRIES


@pytest.mark.unit
async def test_scan_config_refuses_more_addresses_than_the_bridge_holds(monkeypatch) -> None:
    bus = _bus(monkeypatch)
    monkeypatch.setattr(bus, "_send_command", _respond(b"\x00"))

    with pytest.raises(ValueError, match="scan addresses"):
        await bus.scan_config(list(range(MAX_SCAN_ADDRESSES + 1)), 20, 40)


@pytest.mark.unit
async def test_scan_config_frames_the_request_the_bridge_expects(monkeypatch) -> None:
    bus = _bus(monkeypatch)
    sent: list[bytes] = []

    async def capture(payload: bytes) -> bytes:
        sent.append(payload)
        return b"\x00"

    monkeypatch.setattr(bus, "_send_command", capture)

    assert await bus.scan_config([0x38, 0x39], 20, 40)
    assert sent == [bytes([CMD_SCAN_CONFIG, 20, 40, 2, 0x38, 0x39])]


@pytest.mark.unit
async def test_scan_fetch_rejects_a_truncated_entry_list(monkeypatch) -> None:
    bus = _bus(monkeypatch)
    monkeypatch.setattr(bus, "_send_command", _respond(b"\x00\x00\x04\x00\xfe"))

    with pytest.raises(OSError, match="truncated"):
        await bus.scan_fetch()


# ---------------------------------------------------------------------------
# Poll cycle packing
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_full_cycle_packs_into_two_frames(hass) -> None:
    bus = FakeBus()
    api = CasaITApi(hass, bus, "entry-test")

    for index in range(8):
        _pcf(api, 0x38 + index, bus)
    for index in range(8):
        address = 0x10 + index
        device = DM117(bus, address)
        device.last_port_types = dict.fromkeys(range(8), DeviceType.INPUT)
        device._force_full_read = False  # noqa: SLF001
        api.dm117[address] = device

    pcf = sorted(api.im117_om117)
    plan = api._plan_poll_batches(pcf, sorted(api.dm117), set(pcf))  # noqa: SLF001

    assert len(plan) == 2
    for batch, modules in plan:
        assert len(bytes(batch)) <= MAX_FRAME_PAYLOAD
        assert batch.result_count <= MAX_BATCH_RESULTS
        cursor = 0
        for module in modules:
            assert module.result_start == cursor
            cursor += module.result_count
        assert cursor == batch.result_count


@pytest.mark.unit
def test_rearms_are_capped_per_cycle(hass) -> None:
    bus = FakeBus()
    api = CasaITApi(hass, bus, "entry-test")
    for index in range(8):
        _pcf(api, 0x38 + index, bus, armed=False)

    addresses = sorted(api.im117_om117)
    plan = api._plan_poll_batches(addresses, [], set(addresses))  # noqa: SLF001

    rearmed = [module for _, modules in plan for module in modules if module.rearmed]
    assert len(rearmed) == 2
    # The deferred modules are still read this cycle, just without re-arming.
    assert sum(len(modules) for _, modules in plan) == 8


@pytest.mark.unit
def test_cached_dm117_is_left_out_of_the_batch(hass) -> None:
    bus = FakeBus()
    api = CasaITApi(hass, bus, "entry-test")
    device = DM117(bus, 0x10)
    device.last_values = {0: 1}
    device._last_read_time = float("inf")  # noqa: SLF001
    api.dm117[0x10] = device

    assert api._plan_poll_batches([], [0x10], set()) == []  # noqa: SLF001


# ---------------------------------------------------------------------------
# Failure isolation
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_batch_failure_blames_one_module_and_rereads_the_others(hass) -> None:
    bus = FakeBus(error=I2CBatchError("boom", 1))
    api = CasaITApi(hass, bus, "entry-test")
    _pcf(api, 0x38, bus)
    _pcf(api, 0x39, bus)
    api._pcf_states[0x38] = [1] * 8  # noqa: SLF001
    api._pcf_states[0x39] = [1] * 8  # noqa: SLF001

    addresses = sorted(api.im117_om117)
    plan = api._plan_poll_batches(addresses, [], set(addresses))  # noqa: SLF001
    batch, modules = plan[0]

    with patch("custom_components.casait_smarthome.api.async_dispatcher_send"):
        await api._run_poll_batch(batch, modules)  # noqa: SLF001

    # The named module lost its state, the other one was read on its own.
    assert 0x39 not in api.pcf_states
    assert api.pcf_states[0x38] == [1] * 8
    assert bus.reads == [0x38]


@pytest.mark.unit
async def test_transport_failure_drops_every_module_in_the_frame(hass) -> None:
    bus = FakeBus(error=OSError("link down"))
    api = CasaITApi(hass, bus, "entry-test")
    _pcf(api, 0x38, bus)
    _pcf(api, 0x39, bus)
    api._pcf_states[0x38] = [1] * 8  # noqa: SLF001
    api._pcf_states[0x39] = [1] * 8  # noqa: SLF001

    addresses = sorted(api.im117_om117)
    batch, modules = api._plan_poll_batches(addresses, [], set(addresses))[0]  # noqa: SLF001

    with patch("custom_components.casait_smarthome.api.async_dispatcher_send"):
        await api._run_poll_batch(batch, modules)  # noqa: SLF001

    assert api.pcf_states == {}
    assert bus.reads == []


# ---------------------------------------------------------------------------
# Bus priority
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_background_read_waits_for_a_running_poll_cycle(hass) -> None:
    api = CasaITApi(hass, FakeBus(), "entry-test")
    entered = False

    async def background() -> None:
        nonlocal entered
        async with api._background_access():  # noqa: SLF001
            entered = True

    api._poll_idle.clear()  # noqa: SLF001
    task = hass.async_create_task(background())
    await asyncio.sleep(0)
    assert not entered

    api._poll_idle.set()  # noqa: SLF001
    await task
    assert entered


@pytest.mark.unit
async def test_background_read_waits_for_a_pending_write(hass) -> None:
    api = CasaITApi(hass, FakeBus(), "entry-test")
    entered = False

    async def background() -> None:
        nonlocal entered
        async with api._background_access():  # noqa: SLF001
            entered = True

    async with api.write_access():
        task = hass.async_create_task(background())
        await asyncio.sleep(0)
        assert not entered

    await task
    assert entered


# ---------------------------------------------------------------------------
# Bridge input scanner
# ---------------------------------------------------------------------------


class ScanBus(FakeBus):
    """Transport double exposing the scanner commands."""

    def __init__(self, accept: bool, fetch: tuple[int, list[tuple[int, int]]] | None = None) -> None:
        super().__init__()
        self.accept = accept
        self.fetch = fetch or (0, [])

    async def scan_config(self, addresses: list[int], period_ms: int, debounce_ms: int) -> bool:
        self.configured = (list(addresses), period_ms, debounce_ms)
        return self.accept

    async def scan_fetch(self) -> tuple[int, list[tuple[int, int]]]:
        return self.fetch


@pytest.mark.unit
async def test_old_firmware_falls_back_to_polling(hass) -> None:
    bus = ScanBus(accept=False)
    api = CasaITApi(hass, bus, "entry-test")
    device = _pcf(api, 0x38, bus)

    await api._async_start_input_scanner()  # noqa: SLF001

    assert api._scan_addresses == []  # noqa: SLF001
    # Debounce stays with the driver when the bridge is not doing it.
    assert device.debounce_time > 0


@pytest.mark.unit
async def test_accepted_scanner_takes_over_debounce_and_addresses(hass) -> None:
    bus = ScanBus(accept=True)
    api = CasaITApi(hass, bus, "entry-test")
    device = _pcf(api, 0x38, bus)

    await api._async_start_input_scanner()  # noqa: SLF001

    assert api._scan_addresses == [0x38]  # noqa: SLF001
    assert device.debounce_time == 0
    assert bus.configured[0] == [0x38]


@pytest.mark.unit
async def test_scanner_debounces_at_the_shared_floor(hass) -> None:
    """The bridge takes one value for every address, the driver keeps the rest."""

    bus = ScanBus(accept=True)
    api = CasaITApi(hass, bus, "entry-test", input_debounce_ms={"im117": {0x38: 20, 0x39: 50}})
    quick = _pcf(api, 0x38, bus)
    slow = _pcf(api, 0x39, bus)

    await api._async_start_input_scanner()  # noqa: SLF001

    assert bus.configured == ([0x38, 0x39], 20, 20)
    assert quick.debounce_time == 0
    assert slow.debounce_time == 30


@pytest.mark.unit
async def test_fetched_snapshots_are_replayed_as_edges(hass) -> None:
    # Two snapshots for one module: pressed, then released again.
    bus = ScanBus(accept=True, fetch=(0, [(0, 0xFE), (0, 0xFF)]))
    api = CasaITApi(hass, bus, "entry-test")
    device = _pcf(api, 0x38, bus)
    device.apply_reading(0xFF)
    await api._async_start_input_scanner()  # noqa: SLF001

    with patch("custom_components.casait_smarthome.api.async_dispatcher_send") as dispatch:
        await api._fetch_scanned_inputs()  # noqa: SLF001

    edges = [call.args[2] for call in dispatch.call_args_list if len(call.args) > 2]
    assert edges == [{0: [False]}, {0: [True]}]


@pytest.mark.unit
async def test_overflow_rebaselines_instead_of_reporting_edges(hass) -> None:
    bus = ScanBus(accept=True, fetch=(SCAN_FLAG_OVERFLOW, [(0, 0x00)]))
    api = CasaITApi(hass, bus, "entry-test")
    device = _pcf(api, 0x38, bus)
    device.apply_reading(0xFF)
    await api._async_start_input_scanner()  # noqa: SLF001

    with patch("custom_components.casait_smarthome.api.async_dispatcher_send") as dispatch:
        await api._fetch_scanned_inputs()  # noqa: SLF001

    edges = [call.args[2] for call in dispatch.call_args_list if len(call.args) > 2]
    assert edges == []
    assert api.pcf_states[0x38] == [0] * 8


@pytest.mark.unit
def test_scanned_addresses_leave_the_batch(hass) -> None:
    bus = ScanBus(accept=True)
    api = CasaITApi(hass, bus, "entry-test")
    _pcf(api, 0x38, bus)
    _pcf(api, 0x39, bus)
    api._scan_addresses = [0x38]  # noqa: SLF001

    addresses = [a for a in sorted(api.im117_om117) if a not in api._scan_addresses]  # noqa: SLF001
    plan = api._plan_poll_batches(addresses, [], set(api.im117_om117))  # noqa: SLF001

    assert [module.address for _, modules in plan for module in modules] == [0x39]


@pytest.mark.unit
def test_module_for_op_maps_indices_to_owners(hass) -> None:
    bus = FakeBus()
    api = CasaITApi(hass, bus, "entry-test")
    _pcf(api, 0x38, bus, armed=False)
    _pcf(api, 0x39, bus)

    addresses = sorted(api.im117_om117)
    _, modules = api._plan_poll_batches(addresses, [], set(addresses))[0]  # noqa: SLF001

    # 0x38 re-arms, so it owns three operations before 0x39 starts.
    assert api._module_for_op(modules, 0).address == 0x38  # noqa: SLF001
    assert api._module_for_op(modules, 2).address == 0x38  # noqa: SLF001
    assert api._module_for_op(modules, 3).address == 0x39  # noqa: SLF001
    assert api._module_for_op(modules, None) is None  # noqa: SLF001


# ---------------------------------------------------------------------------
# DM117 input edges
# ---------------------------------------------------------------------------


def _dm117_input(api: CasaITApi, bus: FakeBus, address: int, slot: int) -> DM117:
    device = DM117(bus, address)
    device.last_port_types = {slot: DeviceType.INPUT}
    api.dm117[address] = device
    api._dm_config = {address: {slot: DeviceType.INPUT}}  # noqa: SLF001
    return device


@pytest.mark.unit
def test_dm117_publishes_input_edges(hass) -> None:
    """The DM117 reports levels, so the API has to derive the transitions."""

    bus = FakeBus()
    api = CasaITApi(hass, bus, "entry-test", input_debounce_ms={"dm117": {0x10: 0}})
    device = _dm117_input(api, bus, 0x10, slot=0)

    with patch("custom_components.casait_smarthome.api.async_dispatcher_send") as dispatch:
        # First reading only establishes the baseline.
        api._publish_dm117_reading(0x10, {0: 0x00}, device)  # noqa: SLF001
        assert [call.args[2] for call in dispatch.call_args_list if len(call.args) > 2] == []

        dispatch.reset_mock()
        api._publish_dm117_reading(0x10, {0: 0x01}, device)  # noqa: SLF001
        api._publish_dm117_reading(0x10, {0: 0x03}, device)  # noqa: SLF001

    edges = [call.args[2] for call in dispatch.call_args_list if len(call.args) > 2]
    assert edges == [{(0, 0): [True]}, {(0, 1): [True]}]


@pytest.mark.unit
def test_dm117_output_slots_produce_no_edges(hass) -> None:
    bus = FakeBus()
    api = CasaITApi(hass, bus, "entry-test")
    device = DM117(bus, 0x10)
    device.last_port_types = {0: DeviceType.OUTPUT}
    api.dm117[0x10] = device
    api._dm_config = {0x10: {0: DeviceType.OUTPUT}}  # noqa: SLF001

    with patch("custom_components.casait_smarthome.api.async_dispatcher_send") as dispatch:
        api._publish_dm117_reading(0x10, {0: 0x00}, device)  # noqa: SLF001
        api._publish_dm117_reading(0x10, {0: 0x03}, device)  # noqa: SLF001

    assert [call.args[2] for call in dispatch.call_args_list if len(call.args) > 2] == []


@pytest.mark.unit
def test_dm117_edges_respect_the_module_debounce(hass) -> None:
    """Readings inside the configured window must not each become an edge."""

    bus = FakeBus()
    api = CasaITApi(hass, bus, "entry-test", input_debounce_ms={"dm117": {0x10: 250}})
    device = _dm117_input(api, bus, 0x10, slot=0)

    with patch("custom_components.casait_smarthome.api.async_dispatcher_send") as dispatch:
        api._publish_dm117_reading(0x10, {0: 0x00}, device)  # noqa: SLF001
        dispatch.reset_mock()
        # These land microseconds apart, far inside the 250 ms window.
        api._publish_dm117_reading(0x10, {0: 0x01}, device)  # noqa: SLF001
        api._publish_dm117_reading(0x10, {0: 0x00}, device)  # noqa: SLF001

    assert [call.args[2] for call in dispatch.call_args_list if len(call.args) > 2] == []
