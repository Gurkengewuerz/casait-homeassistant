"""Multisensor drivers against an emulated DS28E17 with Sensirion and Vishay chips behind it."""

from __future__ import annotations

from collections.abc import Callable
from types import SimpleNamespace

import pytest

from custom_components.casait_smarthome.multisensor import (
    CHIP_MISSING_SAMPLES,
    CasaITMultisensorManager,
    MultisensorCommandError,
)
from custom_components.casait_smarthome.services.i2cClasses.ds28e17 import DS28E17, DS28E17Nack
from custom_components.casait_smarthome.services.i2cClasses.gas_index import VocGasIndexAlgorithm
from custom_components.casait_smarthome.services.i2cClasses.multisensor import (
    LED_CONTROLLER_ADDRESS,
    SGP40_ADDRESS,
    SHT41_ADDRESS,
    VEML7700_ADDRESS,
    Multisensor,
    MultisensorComponents,
    MultisensorState,
    decode_words,
    encode_words,
    sensirion_crc,
    veml7700_config,
    veml7700_lux,
)
from homeassistant.helpers import issue_registry as ir

DEVICE = "1900000000000001"


def crc16(data: bytes) -> int:
    crc = 0
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc


class Slave:
    """An I2C chip: gets written bytes, answers reads, or NACKs."""

    def __init__(self) -> None:
        self.writes: list[bytes] = []
        self.pending: bytes | None = None

    def write(self, data: bytes) -> None:
        self.writes.append(data)

    def read(self, count: int) -> bytes | None:
        data, self.pending = self.pending, None
        return data[:count] if data is not None else None


class SHT41(Slave):
    def __init__(self, t_ticks: int, rh_ticks: int) -> None:
        super().__init__()
        self.t_ticks, self.rh_ticks = t_ticks, rh_ticks

    def write(self, data: bytes) -> None:
        super().write(data)
        if data == bytes([0xFD]):
            self.pending = encode_words(self.t_ticks, self.rh_ticks)
        elif data == bytes([0x89]):
            self.pending = encode_words(0x1234, 0x5678)


class SGP40(Slave):
    def write(self, data: bytes) -> None:
        super().write(data)
        if data[:2] == bytes([0x26, 0x0F]):
            self.pending = encode_words(30000)
        elif data[:2] == bytes([0x36, 0x82]):
            self.pending = encode_words(1, 2, 3)


class STCC4(Slave):
    def __init__(self) -> None:
        super().__init__()
        self.running = False

    def write(self, data: bytes) -> None:
        super().write(data)
        code = int.from_bytes(data[:2], "big")
        if code == 0x218B:
            self.running = True
        elif code == 0x3F86:
            self.running = False
        elif code == 0xEC05 and self.running:
            self.pending = encode_words(612, 0x6666, 0x8000, 0)
        elif code == 0x362F:
            self.pending = encode_words(0xFFF6)  # -10 ppm
        elif code == 0x278C:
            self.pending = encode_words(0)


class VEML7700(Slave):
    def __init__(self, raw: int) -> None:
        super().__init__()
        self.raw = raw
        self.config: int | None = None

    def write(self, data: bytes) -> None:
        super().write(data)
        if data[0] == 0x00:
            self.config = int.from_bytes(data[1:3], "little")
        elif data == bytes([0x04]):
            self.pending = self.raw.to_bytes(2, "little")


class FakeBridge:
    """DS2482 plus DS28E17: decodes the command packet and plays the I2C side."""

    def __init__(self, slaves: dict[int, Slave]) -> None:
        self.slaves = slaves
        self.responses: list[int] = []
        self.transactions = 0

    async def wire_write_bytes(self, data: list[int]) -> bool:
        packet = bytes(data)
        body, crc = packet[:-2], packet[-2] | (packet[-1] << 8)
        self.transactions += 1
        if crc != ~crc16(body) & 0xFFFF:
            self.responses = [0x01, 0x00]
            return True

        command, address_byte = body[0], body[1]
        slave = self.slaves.get(address_byte >> 1)
        if command == 0x4B:  # write with stop
            if slave is None:
                self.responses = [0x02, 0x00]
            else:
                slave.write(body[3 : 3 + body[2]])
                self.responses = [0x00, 0x00]
        elif command == 0x87:  # read with stop
            answer = slave.read(body[2]) if slave is not None else None
            self.responses = [0x02] if answer is None else [0x00, *answer]
        elif command == 0x2D:  # write, read with stop
            write_len = body[2]
            read_len = body[3 + write_len]
            if slave is None:
                self.responses = [0x02, 0x00]
            else:
                slave.write(body[3 : 3 + write_len])
                answer = slave.read(read_len)
                self.responses = [0x02, 0x00] if answer is None else [0x00, 0x00, *answer]
        return True

    async def wire_single_bit(self, bit: bool) -> bool:
        return False  # never busy

    async def wire_read_bytes(self, count: int) -> list[int] | None:
        out, self.responses = self.responses[:count], self.responses[count:]
        return out if len(out) == count else None


class FakeOneWireBus:
    def __init__(self, slaves: dict[int, Slave]) -> None:
        self.bridge = FakeBridge(slaves)
        self.multisensor = Multisensor(DS28E17(self))

    @staticmethod
    def calc_crc16(data: bytes) -> int:
        return crc16(data)

    async def select_device(self, device_id: str) -> bool:
        return device_id == DEVICE


def _full_board() -> dict[int, Slave]:
    # 25 °C and 50 %RH, as ticks.
    return {
        SHT41_ADDRESS: SHT41(0x6666, 0x7333),
        SGP40_ADDRESS: SGP40(),
        0x64: STCC4(),
        VEML7700_ADDRESS: VEML7700(1000),
    }


def _state(components: MultisensorComponents) -> MultisensorState:
    return MultisensorState(components=components, voc=VocGasIndexAlgorithm(10.0))


# ---------------------------------------------------------------------------
# Encoding
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_sensirion_crc_matches_the_datasheet_example() -> None:
    assert sensirion_crc(b"\xbe\xef") == 0x92


@pytest.mark.unit
def test_words_round_trip_and_reject_a_bad_crc() -> None:
    encoded = encode_words(0x1234, 0xBEEF)
    assert decode_words(encoded) == [0x1234, 0xBEEF]

    corrupted = bytearray(encoded)
    corrupted[2] ^= 0xFF
    with pytest.raises(Exception, match="CRC"):
        decode_words(bytes(corrupted))


@pytest.mark.unit
def test_lux_scales_with_gain_and_integration_time() -> None:
    # Gain x1 at 100 ms is 16 times less sensitive than gain x2 at 800 ms.
    assert veml7700_lux(1000, 1.0, 100) == pytest.approx(1000 * 0.0042 * 16)
    # Low gains get Vishay's non-linearity correction on top.
    assert veml7700_lux(1000, 0.125, 25) > 1000 * 0.0042 * 16 * 32


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_detect_finds_exactly_the_fitted_chips() -> None:
    bus = FakeOneWireBus({SHT41_ADDRESS: SHT41(0, 0), VEML7700_ADDRESS: VEML7700(0)})

    components = await bus.multisensor.detect(DEVICE)

    assert components == MultisensorComponents(sht41=True, veml7700=True)
    assert not await bus.multisensor.is_led_controller(DEVICE)


@pytest.mark.unit
async def test_detect_finds_the_co2_sensor_on_its_alternate_address() -> None:
    bus = FakeOneWireBus({0x65: STCC4()})

    assert (await bus.multisensor.detect(DEVICE)).stcc4_address == 0x65


@pytest.mark.unit
async def test_led_controller_is_told_apart_from_a_multisensor() -> None:
    led = Slave()
    led.pending = b"\x1e"
    bus = FakeOneWireBus({LED_CONTROLLER_ADDRESS: led})

    assert await bus.multisensor.is_led_controller(DEVICE)


@pytest.mark.unit
async def test_a_nack_is_distinguished_from_a_bus_fault() -> None:
    bus = FakeOneWireBus({})
    bridge = bus.multisensor.bridge

    with pytest.raises(DS28E17Nack):
        await bridge.write(DEVICE, SHT41_ADDRESS, b"\xfd")
    assert await bridge.probe(DEVICE, SHT41_ADDRESS, b"\xfd") is False
    with pytest.raises(Exception, match="select"):
        await bridge.write("2800000000000000", SHT41_ADDRESS, b"\xfd")


# ---------------------------------------------------------------------------
# Sampling
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_sht41_values_compensate_the_voc_sensor() -> None:
    slaves = _full_board()
    bus = FakeOneWireBus(slaves)
    state = _state(MultisensorComponents(sht41=True, sgp40=True))

    await bus.multisensor.sht41_trigger(DEVICE)
    await bus.multisensor.sht41_fetch(DEVICE, state)
    await bus.multisensor.sgp40_trigger(DEVICE, state)
    await bus.multisensor.sgp40_fetch(DEVICE, state)

    assert state.reading.temperature == pytest.approx(25.0, abs=0.01)
    assert state.reading.humidity == pytest.approx(50.2, abs=0.1)
    # The SGP40 got humidity first, then temperature, both as the SHT41's ticks.
    assert slaves[SGP40_ADDRESS].writes[-1] == bytes([0x26, 0x0F]) + encode_words(0x7333, 0x6666)
    assert state.reading.voc_raw == 30000
    # Still inside the algorithm's initial blackout.
    assert state.reading.voc_index is None


@pytest.mark.unit
async def test_stcc4_is_started_once_and_then_read_with_compensation() -> None:
    slaves = _full_board()
    bus = FakeOneWireBus(slaves)
    state = _state(MultisensorComponents(sht41=True, stcc4_address=0x64))
    state.t_ticks, state.rh_ticks = 0x6666, 0x7333

    await bus.multisensor.stcc4_sample(DEVICE, state)
    assert state.stcc4_running
    assert state.reading.co2 is None

    await bus.multisensor.stcc4_sample(DEVICE, state)
    assert state.reading.co2 == 612
    stcc4 = slaves[0x64]
    assert stcc4.writes[1] == bytes([0xE0, 0x00]) + encode_words(0x6666, 0x7333)
    assert stcc4.writes[2] == bytes([0xEC, 0x05])


@pytest.mark.unit
async def test_veml7700_ranges_down_in_bright_light() -> None:
    slaves = _full_board()
    veml = slaves[VEML7700_ADDRESS]
    assert isinstance(veml, VEML7700)
    veml.raw = 40000
    bus = FakeOneWireBus(slaves)
    state = _state(MultisensorComponents(veml7700=True))

    await bus.multisensor.veml7700_sample(DEVICE, state)  # configures only
    assert veml.config == veml7700_config(state.veml_range)
    start = state.veml_range

    await bus.multisensor.veml7700_sample(DEVICE, state)

    assert state.reading.illuminance is not None
    assert state.veml_range == start + 1
    assert veml.config == veml7700_config(start + 1)


# ---------------------------------------------------------------------------
# Manager
# ---------------------------------------------------------------------------


def _manager(hass, bus: FakeOneWireBus) -> CasaITMultisensorManager:
    async def job(device_id: str, func: Callable, *, write: bool = False):
        return await func(bus)

    api = SimpleNamespace(
        hass=hass, state_update_signal="casait_test", async_onewire_job=job, entry_id="entry", ow_devices={}
    )
    return CasaITMultisensorManager(api)  # type: ignore[arg-type]


@pytest.mark.unit
async def test_manager_samples_every_fitted_chip(hass, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("custom_components.casait_smarthome.multisensor.STCC4_STOP_TIME", 0.0)
    bus = FakeOneWireBus(_full_board())
    manager = _manager(hass, bus)

    identity = await manager.async_detect(DEVICE)
    assert identity is not None
    profile, components = identity
    assert profile == "ds28e17_multisensor"
    assert components is not None
    manager.register(DEVICE, components)

    for _ in range(3):
        await manager.async_sample(DEVICE)

    reading = manager.reading(DEVICE)
    assert reading is not None
    assert reading.temperature == pytest.approx(25.0, abs=0.01)
    assert reading.co2 == 612
    assert reading.illuminance is not None
    assert reading.voc_raw == 30000


@pytest.mark.unit
async def test_calibration_waits_for_the_co2_sensor_to_warm_up(hass, monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("STCC4_STOP_TIME", "STCC4_FRC_TIME", "STCC4_SELF_TEST_TIME"):
        monkeypatch.setattr(f"custom_components.casait_smarthome.multisensor.{name}", 0.0)
    bus = FakeOneWireBus(_full_board())
    manager = _manager(hass, bus)
    manager.register(DEVICE, MultisensorComponents(stcc4_address=0x64))
    state = manager.state(DEVICE)
    assert state is not None
    state.stcc4_ready_at = 0.0
    await manager.async_sample(DEVICE)

    with pytest.raises(MultisensorCommandError) as err:
        await manager.async_forced_recalibration(DEVICE, 420)
    assert err.value.reason == "co2_not_warmed_up"

    monkeypatch.setattr("custom_components.casait_smarthome.multisensor.STCC4_FRC_WARMUP", 0.0)
    assert await manager.async_forced_recalibration(DEVICE, 420) == -10
    assert manager.maintenance(DEVICE)["frc_correction"] == -10
    # The sampler takes the sensor back into continuous mode on its next turn.
    assert not state.stcc4_running
    await manager.async_sample(DEVICE)
    assert state.stcc4_running

    assert await manager.async_self_test(DEVICE) is True


@pytest.mark.unit
async def test_a_chip_that_stops_answering_raises_and_clears_an_issue(hass) -> None:
    slaves = _full_board()
    bus = FakeOneWireBus(slaves)
    manager = _manager(hass, bus)
    manager.register(DEVICE, MultisensorComponents(sht41=True, veml7700=True))
    issue_id = manager.chip_issue_id(DEVICE, "veml7700")
    registry = ir.async_get(hass)

    veml = slaves.pop(VEML7700_ADDRESS)
    for _ in range(CHIP_MISSING_SAMPLES - 1):
        await manager.async_sample(DEVICE)
    assert registry.async_get_issue("casait_smarthome", issue_id) is None

    await manager.async_sample(DEVICE)
    issue = registry.async_get_issue("casait_smarthome", issue_id)
    assert issue is not None
    assert issue.translation_placeholders == {"name": f"Multisensor {DEVICE}", "chip": "VEML7700"}
    # The chip that still answers is not reported.
    assert registry.async_get_issue("casait_smarthome", manager.chip_issue_id(DEVICE, "sht41")) is None

    slaves[VEML7700_ADDRESS] = veml
    assert await manager.async_probe_chip(DEVICE, "veml7700")
    assert registry.async_get_issue("casait_smarthome", issue_id) is None


@pytest.mark.unit
async def test_chips_seen_before_are_kept_when_they_do_not_answer(hass) -> None:
    manager = _manager(hass, FakeOneWireBus({}))

    first = await manager.async_remember(DEVICE, MultisensorComponents(sht41=True, stcc4_address=0x64))
    second = await manager.async_remember(DEVICE, MultisensorComponents(sht41=True))

    assert first == second == MultisensorComponents(sht41=True, stcc4_address=0x64)

    await manager.async_forget_chip(DEVICE, "stcc4")
    assert manager.known_components(DEVICE) == MultisensorComponents(sht41=True)
