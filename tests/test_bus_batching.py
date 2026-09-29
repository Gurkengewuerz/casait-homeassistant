"""Tests for bus access: batch framing, the bridge watch and its events."""

from __future__ import annotations

import asyncio
from unittest.mock import patch

from crccheck.crc import Crc8Smbus
import pytest

from custom_components.casait_smarthome.api import INTERLOCK_DEAD_MS, CasaITApi
from custom_components.casait_smarthome.const import OM117_MODE_SHUTTER, PCF8574_MAPPED_PORTS
from custom_components.casait_smarthome.helpers import OM117PairConfig
from custom_components.casait_smarthome.services.i2cClasses.dm117 import DM117, DeviceType
from custom_components.casait_smarthome.services.i2cClasses.pcf8574 import PCF8574
from custom_components.casait_smarthome.services.smbus_proxy import (
    CMD_INTERLOCK,
    CMD_WATCH_CONFIG,
    EVENT_MARKER,
    MAX_BATCH_RESULTS,
    MAX_FRAME_PAYLOAD,
    MAX_WATCH_MODULES,
    WATCH_FLAG_OVERFLOW,
    WATCH_KIND_DM117,
    WATCH_KIND_PCF_INPUT,
    WATCH_KIND_PCF_OUTPUT,
    I2CBatch,
    I2CBatchError,
    SMBus,
    parse_watch_event,
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
async def test_watch_config_frames_the_request_the_bridge_expects(monkeypatch) -> None:
    bus = _bus(monkeypatch)
    sent: list[bytes] = []

    async def send(payload: bytes) -> bytes:
        sent.append(payload)
        return b"\x00\x01"

    monkeypatch.setattr(bus, "_send_command", send)

    assert await bus.watch_config([(WATCH_KIND_PCF_INPUT, 0x38), (WATCH_KIND_DM117, 0x10)], 20, 5000, 40)
    assert sent == [bytes([CMD_WATCH_CONFIG, 20, 0, 50, 40, 2, WATCH_KIND_PCF_INPUT, 0x38, WATCH_KIND_DM117, 0x10])]


@pytest.mark.unit
async def test_a_refused_watch_is_an_error(monkeypatch) -> None:
    bus = _bus(monkeypatch)
    monkeypatch.setattr(bus, "_send_command", _respond(b"\xff"))

    with pytest.raises(OSError, match="refused"):
        await bus.watch_config([(WATCH_KIND_PCF_INPUT, 0x38)], 20, 5000, 40)
    with pytest.raises(ValueError, match="watched modules"):
        await bus.watch_config([(WATCH_KIND_PCF_INPUT, 0x38)] * (MAX_WATCH_MODULES + 1), 20, 5000, 40)


@pytest.mark.unit
async def test_interlock_frames_the_pairs(monkeypatch) -> None:
    bus = _bus(monkeypatch)
    sent: list[bytes] = []

    async def send(payload: bytes) -> bytes:
        sent.append(payload)
        return b"\x00"

    monkeypatch.setattr(bus, "_send_command", send)

    await bus.interlock(0x20, 300, [(0, 1), (3, 2)])
    assert sent == [bytes([CMD_INTERLOCK, 0x20, 0x01, 0x2C, 2, 0, 1, 3, 2])]


@pytest.mark.unit
def test_watch_events_decode_and_reject_truncation() -> None:
    event = parse_watch_event(_event((5, 0, [0xFE]), (6, 1, []), flags=WATCH_FLAG_OVERFLOW))

    assert event.flags == WATCH_FLAG_OVERFLOW
    assert [(entry.seq, entry.index, entry.data) for entry in event.entries] == [(5, 0, b"\xfe"), (6, 1, b"")]
    with pytest.raises(ValueError, match="Truncated"):
        parse_watch_event(_event((5, 0, [0xFE, 0x01]))[:-1])


# ---------------------------------------------------------------------------
# Bus priority
# ---------------------------------------------------------------------------


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
# Bridge watch
# ---------------------------------------------------------------------------


class WatchBus(FakeBus):
    """Transport double exposing the watch and interlock commands."""

    connection_generation = 1

    def __init__(self, *, resumed: bool = False) -> None:
        super().__init__()
        self.resumed = resumed
        self.watch: tuple[list[tuple[int, int]], int, int, int] | None = None
        self.interlocks: dict[int, tuple[int, list[tuple[int, int]]]] = {}
        self.acks: list[int] = []
        # Pushed by the bridge right behind its answer to the watch configuration.
        self.early: list[bytes] = []
        self.api: CasaITApi | None = None

    async def watch_config(self, modules: list[tuple[int, int]], fast_ms: int, slow_ms: int, debounce_ms: int) -> bool:
        self.watch = (list(modules), fast_ms, slow_ms, debounce_ms)
        assert self.api is not None
        for payload in self.early:
            self.api._handle_bridge_event(payload)  # noqa: SLF001
        return self.resumed

    async def interlock(self, addr: int, dead_ms: int, pairs: list[tuple[int, int]]) -> None:
        self.interlocks[addr] = (dead_ms, list(pairs))

    def watch_ack(self, seq: int) -> bool:
        self.acks.append(seq)
        return True


def _event(*entries: tuple[int, int, list[int]], flags: int = 0) -> bytes:
    payload = bytes([EVENT_MARKER, flags, len(entries)])
    for seq, index, data in entries:
        payload += bytes([seq, index, len(data)]) + bytes(data)
    return payload


def _watch_api(hass, bus: WatchBus, **kwargs) -> CasaITApi:
    api = CasaITApi(hass, bus, "entry-test", **kwargs)
    bus.api = api
    return api


def _edges(dispatch) -> list:
    return [call.args[2] for call in dispatch.call_args_list if len(call.args) > 2]


@pytest.mark.unit
async def test_the_session_hands_every_module_to_the_bridge(hass) -> None:
    bus = WatchBus()
    api = _watch_api(hass, bus, om117_pair_configuration={0x20: {0: OM117PairConfig(mode=OM117_MODE_SHUTTER)}})
    _pcf(api, 0x20, bus)
    _pcf(api, 0x21, bus)
    _pcf(api, 0x38, bus)
    api.dm117[0x10] = DM117(bus, 0x10)

    await api._async_start_session()  # noqa: SLF001

    assert bus.watch is not None
    assert bus.watch[0] == [
        (WATCH_KIND_PCF_OUTPUT, 0x20),
        (WATCH_KIND_PCF_OUTPUT, 0x21),
        (WATCH_KIND_PCF_INPUT, 0x38),
        (WATCH_KIND_DM117, 0x10),
    ]
    # The shutter pair is interlocked; the module without covers has its interlock lifted.
    assert bus.interlocks == {
        0x20: (INTERLOCK_DEAD_MS, [(PCF8574_MAPPED_PORTS[0], PCF8574_MAPPED_PORTS[1])]),
        0x21: (0, []),
    }
    assert api._watch_ready  # noqa: SLF001


@pytest.mark.unit
async def test_the_bridge_debounces_at_the_shared_floor(hass) -> None:
    """The bridge takes one value for every input module, the driver keeps the rest."""

    bus = WatchBus()
    api = _watch_api(hass, bus, input_debounce_ms={"im117": {0x38: 20, 0x39: 50}})
    quick = _pcf(api, 0x38, bus)
    slow = _pcf(api, 0x39, bus)

    await api._async_start_session()  # noqa: SLF001

    assert bus.watch is not None
    assert bus.watch[3] == 20
    assert quick.debounce_time == 0
    assert slow.debounce_time == 30


@pytest.mark.unit
async def test_pushed_readings_become_edges_and_are_acknowledged(hass) -> None:
    bus = WatchBus()
    api = _watch_api(hass, bus)
    _pcf(api, 0x38, bus)
    await api._async_start_session()  # noqa: SLF001

    with patch("custom_components.casait_smarthome.api.async_dispatcher_send") as dispatch:
        # Baseline, pressed, released.
        api._handle_bridge_event(_event((0, 0, [0xFF]), (1, 0, [0xFE]), (2, 0, [0xFF])))  # noqa: SLF001

    assert _edges(dispatch) == [{0: [False]}, {0: [True]}]
    assert bus.acks == [2]


@pytest.mark.unit
async def test_entries_resent_after_a_reconnect_are_not_replayed(hass) -> None:
    bus = WatchBus()
    api = _watch_api(hass, bus)
    _pcf(api, 0x38, bus)
    await api._async_start_session()  # noqa: SLF001
    api._handle_bridge_event(_event((0, 0, [0xFF]), (1, 0, [0xFE])))  # noqa: SLF001

    bus.resumed = True
    bus.early = [_event((1, 0, [0xFE]), (2, 0, [0xFF]))]
    with patch("custom_components.casait_smarthome.api.async_dispatcher_send") as dispatch:
        await api._async_start_session()  # noqa: SLF001

    # Entry 1 was handled before the drop, only the release is new.
    assert _edges(dispatch) == [{0: [True]}]
    assert bus.acks[-1] == 2


@pytest.mark.unit
async def test_a_fresh_watch_starts_from_a_baseline(hass) -> None:
    bus = WatchBus()
    api = _watch_api(hass, bus)
    device = _pcf(api, 0x38, bus)
    device.apply_reading(0xFF)
    # The bridge restarted: it counts from zero again and pushes its baseline at once.
    bus.early = [_event((0, 0, [0x00]))]

    with patch("custom_components.casait_smarthome.api.async_dispatcher_send") as dispatch:
        await api._async_start_session()  # noqa: SLF001

    assert _edges(dispatch) == []
    assert api.pcf_states[0x38] == [0] * 8


@pytest.mark.unit
async def test_overflow_rebaselines_instead_of_reporting_edges(hass) -> None:
    bus = WatchBus()
    api = _watch_api(hass, bus)
    _pcf(api, 0x38, bus)
    await api._async_start_session()  # noqa: SLF001
    api._handle_bridge_event(_event((0, 0, [0xFF])))  # noqa: SLF001

    with patch("custom_components.casait_smarthome.api.async_dispatcher_send") as dispatch:
        api._handle_bridge_event(_event((1, 0, [0x00]), flags=WATCH_FLAG_OVERFLOW))  # noqa: SLF001

    assert _edges(dispatch) == []
    assert api.pcf_states[0x38] == [0] * 8


@pytest.mark.unit
async def test_a_module_the_bridge_gave_up_on_is_unavailable_at_once(hass) -> None:
    bus = WatchBus()
    api = _watch_api(hass, bus)
    _pcf(api, 0x38, bus)
    await api._async_start_session()  # noqa: SLF001
    api._handle_bridge_event(_event((0, 0, [0xFF])))  # noqa: SLF001
    assert 0x38 in api.pcf_states

    api._handle_bridge_event(_event((1, 0, [])))  # noqa: SLF001

    assert 0x38 not in api.pcf_states


@pytest.mark.unit
async def test_a_pushed_dm117_response_is_decoded(hass) -> None:
    bus = WatchBus()
    api = _watch_api(hass, bus)
    api.dm117[0x10] = DM117(bus, 0x10)
    await api._async_start_session()  # noqa: SLF001
    block = [2, 0, 0x01, 2, 0x02]
    block.append(Crc8Smbus.calc(bytes(block)))

    api._handle_bridge_event(_event((0, 0, block)))  # noqa: SLF001

    assert api.dm117_states[0x10] == {0: 0x01, 1: 0x02}


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
