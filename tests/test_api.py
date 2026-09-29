"""Unit tests for CasaIT API polling and bus arbitration."""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

from custom_components.casait_smarthome.api import MAX_READ_FAILURES, CasaITApi
from custom_components.casait_smarthome.health import LinkHealth
from custom_components.casait_smarthome.services.i2cClasses.dm117 import DeviceType
from custom_components.casait_smarthome.services.i2cClasses.pcf8574 import PCF8574Reading


class FakeBus:
    """Minimal transport used by API unit tests."""

    stats = {"connected": True}


class FakePCF:
    """Return a configurable PCF reading and count reads."""

    def __init__(self, reading: PCF8574Reading) -> None:
        self.reading = reading
        self.read_count = 0

    async def read_ports(self, set_high: bool) -> PCF8574Reading:
        self.read_count += 1
        return self.reading


@pytest.mark.unit
def test_poll_classes_only_include_real_inputs(hass) -> None:
    api = CasaITApi(hass, FakeBus(), "entry-test")
    api.im117_om117 = {0x20: object(), 0x38: object(), 0x39: object()}
    api.dm117 = {0x10: object(), 0x11: object()}
    api._dm_config = {  # noqa: SLF001
        0x10: {0: DeviceType.INPUT},
        0x11: {0: DeviceType.OUTPUT},
    }

    assert api._fast_pcf_addresses() == {0x38, 0x39}  # noqa: SLF001
    assert api._fast_dm117_addresses() == {0x10}  # noqa: SLF001


@pytest.mark.unit
async def test_pcf_dispatches_only_when_state_changes(hass) -> None:
    api = CasaITApi(hass, FakeBus(), "entry-test")
    device = FakePCF(PCF8574Reading([1] * 8, 0xFF))
    api.im117_om117[0x38] = device

    with patch("custom_components.casait_smarthome.api.async_dispatcher_send") as dispatch:
        await api._poll_pcf8574(0x38, is_input=True)  # noqa: SLF001
        await api._poll_pcf8574(0x38, is_input=True)  # noqa: SLF001

    assert dispatch.call_count == 1
    assert api.pcf_states[0x38] == [1] * 8


@pytest.mark.unit
async def test_read_failure_is_latched_and_not_redispatched(hass) -> None:
    api = CasaITApi(hass, FakeBus(), "entry-test")
    api._pcf_states[0x38] = [1] * 8  # noqa: SLF001
    api.im117_om117[0x38] = FakePCF(PCF8574Reading([], -1))

    with patch("custom_components.casait_smarthome.api.async_dispatcher_send") as dispatch:
        for _ in range(MAX_READ_FAILURES + 1):
            await api._poll_pcf8574(0x38, is_input=True)  # noqa: SLF001

    assert dispatch.call_count == 1
    assert 0x38 not in api.pcf_states


@pytest.mark.unit
async def test_isolated_read_failures_keep_the_module_available(hass) -> None:
    api = CasaITApi(hass, FakeBus(), "entry-test")
    good = PCF8574Reading([1] * 8, 0xFF)
    device = FakePCF(good)
    api.im117_om117[0x38] = device
    api._pcf_states[0x38] = [1] * 8  # noqa: SLF001

    with patch("custom_components.casait_smarthome.api.async_dispatcher_send") as dispatch:
        for _ in range(3):
            device.reading = PCF8574Reading([], -1)
            for _ in range(MAX_READ_FAILURES - 1):
                await api._poll_pcf8574(0x38, is_input=True)  # noqa: SLF001
            device.reading = good
            await api._poll_pcf8574(0x38, is_input=True)  # noqa: SLF001

    dispatch.assert_not_called()
    assert api.pcf_states[0x38] == [1] * 8


@pytest.mark.unit
async def test_pending_write_holds_poll_read(hass) -> None:
    api = CasaITApi(hass, FakeBus(), "entry-test")
    device = FakePCF(PCF8574Reading([1] * 8, 0xFF))
    api.im117_om117[0x38] = device

    async with api.write_access():
        task = hass.async_create_task(api._poll_pcf8574(0x38, is_input=True))  # noqa: SLF001
        await asyncio.sleep(0)
        assert device.read_count == 0

    await task
    assert device.read_count == 1


@pytest.mark.unit
def test_link_health_counts_and_smooths_latency() -> None:
    health = LinkHealth()
    health.success(1000.0, 0.010)
    health.success(1001.0, 0.020)
    health.failure(1002.0, "timeout")

    data = health.as_dict()
    assert (data["reads"], data["errors"], data["consecutive_errors"]) == (3, 1, 1)
    assert data["last_latency_ms"] == 20.0
    assert data["average_latency_ms"] == 11.0
    assert data["last_error"] == "timeout"


@pytest.mark.unit
async def test_bus_topology_lists_modules_with_their_health(hass) -> None:
    api = CasaITApi(hass, FakeBus(), "entry-test")
    api.im117_om117[0x38] = FakePCF(PCF8574Reading([0] * 8, 0x00))
    api.found_i2c_devices = {"IM117": [0x38], "OM117": [0x20]}

    await api._poll_pcf8574(0x38, is_input=True)  # noqa: SLF001

    topology = api.bus_topology
    by_address = {entry["address"]: entry for entry in topology["i2c"]}
    assert by_address["0x38"]["polled"] == "every cycle"
    assert by_address["0x38"]["health"]["reads"] == 1
    assert by_address["0x38"]["health"]["last_latency_ms"] is not None
    # Never read yet, so there is nothing to report.
    assert by_address["0x20"]["health"] is None
    assert topology["onewire"] == []
