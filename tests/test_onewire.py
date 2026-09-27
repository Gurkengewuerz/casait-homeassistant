"""The 1-Wire scheduler and the single-transaction drivers it sequences."""

from __future__ import annotations

from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import pytest

from custom_components.casait_smarthome import onewire as onewire_module
from custom_components.casait_smarthome.onewire import MAX_FAILURES, CasaITOneWireScheduler
from custom_components.casait_smarthome.services.i2cClasses.ds2413 import DS2413
from custom_components.casait_smarthome.services.i2cClasses.ds2438 import DS2438, DS2438Page, DS2438Reading
from homeassistant.core import callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect


def crc8(data: bytes) -> int:
    crc = 0
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0x8C if crc & 1 else crc >> 1
    return crc


class Wire:
    """Records 1-Wire writes and answers reads from a queue."""

    def __init__(self, reads: list[int] | None = None) -> None:
        self.written: list[list[int]] = []
        self.reads = list(reads or [])

    async def wire_reset(self) -> bool:
        return True

    async def wire_write_byte(self, byte: int) -> bool:
        self.written.append([byte])
        return True

    async def wire_write_bytes(self, data: list[int]) -> bool:
        self.written.append(list(data))
        return True

    async def wire_read_byte(self) -> int | None:
        return self.reads.pop(0) if self.reads else None

    async def wire_read_bytes(self, count: int) -> list[int] | None:
        out, self.reads = self.reads[:count], self.reads[count:]
        return out if len(out) == count else None


class Bus:
    def __init__(self, reads: list[int] | None = None) -> None:
        self.bridge = Wire(reads)

    async def select_device(self, device_id: str) -> bool:
        return True

    @staticmethod
    def calc_crc8(data: bytes) -> int:
        return crc8(data)


def _status(pin_a: bool, latch_a: bool, pin_b: bool, latch_b: bool) -> int:
    low = int(pin_a) | int(latch_a) << 1 | int(pin_b) << 2 | int(latch_b) << 3
    return low | ((~low & 0x0F) << 4)


# ---------------------------------------------------------------------------
# DS2413
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_ds2413_writes_channel_b_to_bit_one_and_keeps_the_other_latch() -> None:
    # A is an input (latch off, pin pulled up), B is an output that is off.
    before = _status(True, True, True, True)
    after = _status(True, True, False, False)
    bus = Bus([before, 0xAA, after])

    pins = await DS2413(bus).set_state("3a", 1, True)

    # Bit 0 (A latch) stays 1 so the input is not driven, bit 1 (B) goes to 0.
    data = 0xFC | 0x01
    assert bus.bridge.written[-2] == [DS2413.CMD_PIO_ACCESS_WRITE, data, ~data & 0xFF]
    assert pins == (True, False)


@pytest.mark.unit
async def test_ds2413_rejects_a_status_that_fails_its_complement_check() -> None:
    assert await DS2413(Bus([0xFF])).read_ports("3a") is None


# ---------------------------------------------------------------------------
# DS2438
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_ds2438_page_decodes_signed_values() -> None:
    # status VDD, -0.5 °C, 5.00 V, a negative current sense voltage
    page = [0x08, 0x80, 0xFF, 0xF4, 0x01, 0xF6, 0xFF, 0x00]
    bus = Bus([*page, crc8(bytes(page))])

    result = await DS2438(bus).read_page("26")

    assert result == DS2438Page(status=0x08, temperature=-0.5, voltage=5.0, current_voltage=pytest.approx(-0.002441))


# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------


class FakeApi:
    """Just enough API for the scheduler: devices, a job runner, and a hass."""

    def __init__(self, hass, devices: dict[str, dict[str, Any]], answers: dict[str, Callable[[str], Any]]) -> None:
        self.hass = hass
        self.state_update_signal = "casait_test"
        self.ow_devices = devices
        self.multisensor = SimpleNamespace(state=lambda device_id: None)
        self.restorer = SimpleNamespace(check_ds2413=lambda *_: None, check_led=lambda *_: None)
        self.calls: list[tuple[str, str]] = []
        self._answers = answers

    async def async_onewire_job(self, device_id: str, func: Callable[[Any], Any], *, write: bool = False) -> Any:
        bus = SimpleNamespace(
            ds18b20=SimpleNamespace(
                start_conversion=lambda: self._call("convert", device_id),
                read_temperature=lambda rom: self._call("temperature", rom),
            ),
            ds2413=SimpleNamespace(read_ports=lambda rom: self._call("pins", rom)),
        )
        return await func(bus)

    async def _call(self, kind: str, device_id: str) -> Any:
        self.calls.append((kind, device_id))
        return self._answers[kind](device_id)


@pytest.fixture(autouse=True)
def fast_conversions(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(onewire_module, "DS18B20_CONVERSION_TIME", 0.0)


@pytest.mark.unit
async def test_one_conversion_serves_every_ds18b20_on_a_bus(hass) -> None:
    devices = {
        "28a": {"bus_address": 0x18},
        "28b": {"bus_address": 0x18},
        "28c": {"bus_address": 0x19},
    }
    temperatures = {"28a": 21.5, "28b": 19.0, "28c": 4.25}
    api = FakeApi(hass, devices, {"convert": lambda _: True, "temperature": temperatures.__getitem__})
    scheduler = CasaITOneWireScheduler(api)  # type: ignore[arg-type]
    scheduler.configure(dict.fromkeys(devices, "ds18b20_temp"), {"28b": 30})

    assert set(scheduler.diagnostic_data) == {"ds18b20@18", "ds18b20@19"}
    # The strand runs at the interval of its most demanding sensor.
    assert scheduler.diagnostic_data["ds18b20@18"]["interval_s"] == 30

    for job in scheduler._jobs.values():  # noqa: SLF001
        await job.run()

    assert [call for call in api.calls if call[0] == "convert"] == [("convert", "28a"), ("convert", "28c")]
    assert {device: scheduler.value(device) for device in devices} == temperatures


@pytest.mark.unit
async def test_a_device_turns_unavailable_only_after_repeated_failures(hass) -> None:
    answers: dict[str, Callable[[str], Any]] = {"pins": lambda _: (True, False)}
    api = FakeApi(hass, {"3a": {"bus_address": 0x18}}, answers)
    scheduler = CasaITOneWireScheduler(api)  # type: ignore[arg-type]
    scheduler.configure({"3a": "ds2413"}, {})
    job = scheduler._jobs["ds2413/3a"]  # noqa: SLF001
    updates: list[Any] = []

    @callback
    def record() -> None:
        updates.append(scheduler.value("3a"))

    async_dispatcher_connect(hass, scheduler.signal("3a"), record)

    await job.run()
    assert scheduler.value("3a") == (True, False)

    answers["pins"] = lambda _: None
    for _ in range(MAX_FAILURES - 1):
        await job.run()
    assert scheduler.value("3a") == (True, False)

    await job.run()
    await hass.async_block_till_done()
    assert scheduler.value("3a") is None
    assert updates == [(True, False), None]


@pytest.mark.unit
async def test_a_write_result_is_published_without_waiting_for_a_read(hass) -> None:
    api = FakeApi(hass, {"26": {"bus_address": 0x18}}, {})
    scheduler = CasaITOneWireScheduler(api)  # type: ignore[arg-type]
    reading = DS2438Reading(vdd=5.0, vad=2.1, vse=0.0, temperature=21.0)

    scheduler.set_value("26", reading)

    assert scheduler.value("26") is reading
