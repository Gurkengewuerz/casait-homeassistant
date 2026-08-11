"""Tests for the driver-side split of bus I/O from decoding, and the 1-Wire caches."""

from __future__ import annotations

import time

from crccheck.crc import Crc8Smbus
import pytest

from custom_components.casait_smarthome.services.i2cClasses.dm117 import DM117, DeviceType
from custom_components.casait_smarthome.services.i2cClasses.ds18b20 import (
    CACHE_TIMEOUT,
    CONVERSION_TIME,
    DS18B20,
    ConversionState,
    SensorState,
    TemperatureReading,
)
from custom_components.casait_smarthome.services.i2cClasses.pcf8574 import PCF8574, SET_HIGH_REFRESH_READS


class NullBus:
    """Bus double for drivers whose I/O the test does not exercise."""

    def write_byte(self, addr: int, value: int) -> None:
        return


# ---------------------------------------------------------------------------
# PCF8574
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_apply_reading_adopts_the_first_sample_without_edges() -> None:
    device = PCF8574(NullBus(), 0x38)

    reading = device.apply_reading(0xFF)

    assert reading.ok
    assert reading.edges == {}
    assert reading.port_states == [1] * 8


@pytest.mark.unit
def test_apply_reading_reports_the_bits_that_changed() -> None:
    device = PCF8574(NullBus(), 0x38, debounce_time=0)
    device.apply_reading(0xFF, timestamp_ms=0.0)

    reading = device.apply_reading(0xFE, timestamp_ms=100.0)

    assert reading.edges == {0: [False]}
    assert reading.port_states[0] == 0


@pytest.mark.unit
def test_apply_reading_debounces_within_the_window() -> None:
    device = PCF8574(NullBus(), 0x38, debounce_time=40)
    device.apply_reading(0xFF, timestamp_ms=0.0)
    device.apply_reading(0xFE, timestamp_ms=100.0)

    # Bounces back well inside the window; the change must not be reported.
    reading = device.apply_reading(0xFF, timestamp_ms=110.0)

    assert reading.edges == {}
    assert reading.port_states[0] == 0


@pytest.mark.unit
def test_shared_timestamp_keeps_debounce_windows_aligned() -> None:
    """Modules read in one batch must debounce against the same instant."""

    first = PCF8574(NullBus(), 0x38, debounce_time=40)
    second = PCF8574(NullBus(), 0x39, debounce_time=40)
    for device in (first, second):
        device.apply_reading(0xFF, timestamp_ms=0.0)
        device.apply_reading(0xFE, timestamp_ms=50.0)

    # Both windows opened at 50 ms, so the same bounce is suppressed for both.
    assert [device.apply_reading(0xFF, timestamp_ms=80.0).edges for device in (first, second)] == [{}, {}]
    assert [device.apply_reading(0xFF, timestamp_ms=95.0).edges for device in (first, second)] == [{0: [True]}] * 2


@pytest.mark.unit
def test_rearm_thresholds_differ_between_modules() -> None:
    thresholds = {PCF8574(NullBus(), 0x38 + index)._refresh_reads for index in range(8)}  # noqa: SLF001

    # Distinct thresholds are what keeps modules from re-arming on the same cycle.
    assert len(thresholds) == 8
    assert min(thresholds) >= SET_HIGH_REFRESH_READS


@pytest.mark.unit
def test_needs_rearm_tracks_flag_and_counter() -> None:
    device = PCF8574(NullBus(), 0x38)
    assert device.needs_rearm(True)

    device.note_rearmed()
    assert not device.needs_rearm(True)

    # Outputs are never re-armed regardless of the counter.
    assert not device.needs_rearm(False)

    for _ in range(device._refresh_reads):  # noqa: SLF001
        device.apply_reading(0xFF)
    assert device.needs_rearm(True)


@pytest.mark.unit
def test_read_error_forces_the_next_rearm() -> None:
    device = PCF8574(NullBus(), 0x38)
    device.note_rearmed()

    reading = device.note_read_error()

    assert not reading.ok
    assert device.needs_rearm(True)


# ---------------------------------------------------------------------------
# DM117
# ---------------------------------------------------------------------------


def _dm117_response(types: list[DeviceType], values: list[int]) -> list[int]:
    data = [len(types)]
    for device_type, value in zip(types, values, strict=True):
        if device_type is DeviceType.DIMMER:
            data.extend([1, (value >> 8) & 0xFF, value & 0xFF])
        else:
            data.extend([0 if device_type is DeviceType.INPUT else 2, value])
    data.append(Crc8Smbus.calc(data))
    return data


@pytest.mark.unit
def test_expected_size_shrinks_once_the_layout_is_known() -> None:
    device = DM117(NullBus(), 0x10)
    assert device.expected_response_size() == DM117.READ_RESPONSE_SIZE

    device.last_port_types = dict.fromkeys(range(8), DeviceType.INPUT)
    device._force_full_read = False  # noqa: SLF001
    assert device.expected_response_size() == 18

    device.last_port_types = dict.fromkeys(range(8), DeviceType.DIMMER)
    assert device.expected_response_size() == DM117.READ_RESPONSE_SIZE


@pytest.mark.unit
def test_decode_response_reads_values_and_learns_the_layout() -> None:
    device = DM117(NullBus(), 0x10)
    block = _dm117_response([DeviceType.INPUT, DeviceType.DIMMER], [0x05, 0x0123])

    values = device.decode_response(block)

    assert values == {0: 0x05, 1: 0x0123}
    assert device.last_port_types == {0: DeviceType.INPUT, 1: DeviceType.DIMMER}
    assert device.expected_response_size() == 2 + 2 + 3


@pytest.mark.unit
def test_decode_response_rejects_a_bad_crc_and_falls_back_to_full_reads() -> None:
    device = DM117(NullBus(), 0x10)
    device.last_port_types = dict.fromkeys(range(2), DeviceType.INPUT)
    device._force_full_read = False  # noqa: SLF001

    block = _dm117_response([DeviceType.INPUT], [0x05])
    block[-1] ^= 0xFF

    assert device.decode_response(block) is None
    assert device.expected_response_size() == DM117.READ_RESPONSE_SIZE


@pytest.mark.unit
def test_decode_response_survives_a_truncated_block() -> None:
    device = DM117(NullBus(), 0x10)

    # Claims eight slots but carries data for none of them.
    assert device.decode_response([8, 0]) is None
    assert device.expected_response_size() == DM117.READ_RESPONSE_SIZE


@pytest.mark.unit
def test_decode_response_rejects_an_impossible_module_count() -> None:
    device = DM117(NullBus(), 0x10)
    assert device.decode_response([9, 0, 0, 0]) is None


@pytest.mark.unit
def test_cached_ports_expire_with_the_read_interval() -> None:
    device = DM117(NullBus(), 0x10)
    assert device.cached_ports() is None

    device.decode_response(_dm117_response([DeviceType.INPUT], [0x05]))
    assert device.cached_ports() == {0: 0x05}

    device._last_read_time -= device._read_interval * 2  # noqa: SLF001
    assert device.cached_ports() is None


# ---------------------------------------------------------------------------
# DS18B20 cache and broadcast conversion
# ---------------------------------------------------------------------------


class FakeOneWire:
    """1-Wire bus double that counts conversions and serves a fixed temperature."""

    def __init__(self) -> None:
        self.bridge = self
        self.selected: list[str] = []
        self.written: list[list[int]] = []
        self.resets = 0

    def wire_reset(self) -> bool:
        self.resets += 1
        return True

    def wire_write_bytes(self, data: list[int]) -> bool:
        self.written.append(list(data))
        return True

    def select_device(self, device_id: str) -> bool:
        self.selected.append(device_id)
        return True


@pytest.mark.unit
def test_valid_cache_is_served_from_idle_without_touching_the_bus() -> None:
    bus = FakeOneWire()
    sensor = DS18B20(bus)
    sensor._sensor_states["a"] = SensorState(  # noqa: SLF001
        state=ConversionState.IDLE,
        reading=TemperatureReading(temperature=21.5, timestamp=time.time()),
    )

    assert sensor.get_temperature("a") == 21.5
    assert bus.resets == 0
    assert bus.written == []


@pytest.mark.unit
def test_stale_cache_starts_a_new_conversion() -> None:
    bus = FakeOneWire()
    sensor = DS18B20(bus)
    sensor._sensor_states["a"] = SensorState(  # noqa: SLF001
        state=ConversionState.IDLE,
        reading=TemperatureReading(temperature=21.5, timestamp=time.time() - CACHE_TIMEOUT - 1),
    )

    sensor.get_temperature("a")

    assert bus.written == [[DS18B20.CMD_SKIP_ROM, DS18B20.CMD_CONVERT_T]]


@pytest.mark.unit
def test_one_broadcast_conversion_covers_every_sensor() -> None:
    bus = FakeOneWire()
    sensor = DS18B20(bus)

    for device_id in ("a", "b", "c"):
        sensor.get_temperature(device_id)

    # One SKIP ROM convert for the whole strand, not one per sensor.
    assert bus.written == [[DS18B20.CMD_SKIP_ROM, DS18B20.CMD_CONVERT_T]]
    assert bus.selected == []

    states = [sensor._sensor_states[device_id] for device_id in ("a", "b", "c")]  # noqa: SLF001
    assert all(state.state is ConversionState.CONVERTING for state in states)
    # Joining sensors adopt the conversion's start time instead of waiting again.
    assert len({state.last_action for state in states}) == 1


@pytest.mark.unit
def test_a_new_conversion_starts_once_the_previous_one_finished() -> None:
    bus = FakeOneWire()
    sensor = DS18B20(bus)
    sensor.get_temperature("a")
    sensor._broadcast_at = time.time() - CONVERSION_TIME - 0.1  # noqa: SLF001

    sensor.get_temperature("b")

    assert len(bus.written) == 2
