"""Tests for coalesced output writes and time-based covers built on them."""

from __future__ import annotations

import asyncio
from unittest.mock import patch

from bridge_fakes import FakeBridge
import pytest

from custom_components.casait_smarthome import cover as cover_module
from custom_components.casait_smarthome.api import CasaITApi
from custom_components.casait_smarthome.const import COVER_REFERENCE_OFF, OM117_MODE_SHUTTER, PCF8574_MAPPED_PORTS
from custom_components.casait_smarthome.cover import POSITION_UNKNOWN, CasaITBlindCover
from custom_components.casait_smarthome.helpers import OM117PairConfig
from custom_components.casait_smarthome.services.i2cClasses.pcf8574 import PCF8574

ENTRY = type("Entry", (), {"entry_id": "entry-test", "unique_id": "AA:BB:CC:DD:EE:FF", "options": {}})()


def _api(hass, bridge: FakeBridge, *addresses: int, known: bool = True) -> CasaITApi:
    api = CasaITApi(hass, bridge, "entry-test")  # type: ignore[arg-type] - Test double for SMBus.
    for address in addresses:
        device = PCF8574(bridge, address)
        if known:
            device.last_value = bridge.chips.get(address, 0xFF)
        api.im117_om117[address] = device
    return api


def _bit(port: int) -> int:
    return 1 << PCF8574_MAPPED_PORTS[port]


# ---------------------------------------------------------------------------
# Output writer
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_concurrent_writes_share_one_verified_frame(hass) -> None:
    bridge = FakeBridge({0x20: 0xFF, 0x21: 0xFF})
    api = _api(hass, bridge, 0x20, 0x21)

    results = await asyncio.gather(
        api.async_write_pcf_ports(0x20, {0: 0, 1: 1}),
        api.async_write_pcf_ports(0x21, {4: 0}),
        api.async_write_pcf_port(0x20, 2, 0),
    )

    assert results == [True, True, True]
    assert len(bridge.frames) == 1
    assert [kind for kind, _, _ in bridge.frames[0]] == ["write", "write", "delay", "read", "read"]
    assert bridge.chips == {0x20: 0xFA, 0x21: 0xEF}
    assert api.pcf_states[0x20] == [0, 1, 0, 1, 1, 1, 1, 1]
    assert api.im117_om117[0x20].last_value == 0xFA


@pytest.mark.unit
async def test_a_missing_module_does_not_block_the_others(hass) -> None:
    bridge = FakeBridge({0x20: 0xFF})
    api = _api(hass, bridge, 0x20, 0x21)
    api.im117_om117[0x21].last_value = 0xFF

    results = await asyncio.gather(
        api.async_write_pcf_ports(0x21, {0: 0}),
        api.async_write_pcf_ports(0x20, {0: 0}),
    )

    assert results == [False, True]
    assert bridge.chips[0x20] == 0xFE


@pytest.mark.unit
async def test_a_write_that_does_not_read_back_fails(hass) -> None:
    bridge = FakeBridge({0x20: 0xFF})
    bridge.stuck.add(0x20)
    api = _api(hass, bridge, 0x20)

    assert not await api.async_write_pcf_port(0x20, 0, 0)
    assert api.im117_om117[0x20].last_value == 0xFF
    assert api.im117_om117[0x20].needs_rearm()


@pytest.mark.unit
async def test_an_unknown_latch_is_read_before_it_is_changed(hass) -> None:
    bridge = FakeBridge({0x20: 0x7F})
    api = _api(hass, bridge, 0x20, known=False)

    assert await api.async_write_pcf_port(0x20, 0, 0)
    assert bridge.chips[0x20] == 0x7E


@pytest.mark.unit
async def test_shutdown_sends_queued_writes(hass) -> None:
    bridge = FakeBridge({0x20: 0xFF})
    api = _api(hass, bridge, 0x20)

    write = asyncio.ensure_future(api.async_write_pcf_port(0x20, 0, 0))
    await asyncio.sleep(0)
    await api.outputs.async_shutdown()

    assert await write
    assert bridge.chips[0x20] == 0xFE


# ---------------------------------------------------------------------------
# Covers
# ---------------------------------------------------------------------------


def _cover(hass, api: CasaITApi, address: int, pair: int = 0, **config) -> CasaITBlindCover:
    settings = {"open_time": 1.0, "close_time": 1.0, "overrun_time": 0.05} | config
    entity = CasaITBlindCover(api, ENTRY, address, pair, OM117PairConfig(mode=OM117_MODE_SHUTTER, **settings))
    entity.hass = hass
    entity.async_write_ha_state = lambda: None  # type: ignore[method-assign] - Not added to a platform.
    return entity


async def _settle(entity: CasaITBlindCover) -> None:
    if (task := entity._movement_task) is not None:  # noqa: SLF001
        await task


@pytest.fixture
def quick_reversal():
    with patch.object(cover_module, "REVERSAL_PAUSE", 0.01):
        yield


@pytest.mark.unit
async def test_covers_moved_together_start_and_stop_in_one_frame(hass) -> None:
    bridge = FakeBridge({0x20: 0xFF, 0x21: 0xFF})
    api = _api(hass, bridge, 0x20, 0x21)
    first, second = _cover(hass, api, 0x20), _cover(hass, api, 0x21, pair=1)
    for entity in (first, second):
        entity._uncertainty = 0.0  # noqa: SLF001

    await asyncio.gather(first.async_set_cover_position(position=20), second.async_set_cover_position(position=20))
    await asyncio.gather(_settle(first), _settle(second))

    assert bridge.writes() == [
        [(0x20, 0xFF & ~_bit(0)), (0x21, 0xFF & ~_bit(2))],
        [(0x20, 0xFF), (0x21, 0xFF)],
    ]
    assert first.current_cover_position == 20
    assert second.current_cover_position == 20


@pytest.mark.unit
async def test_partial_moves_accumulate_drift_and_an_end_run_clears_it(hass) -> None:
    bridge = FakeBridge({0x20: 0xFF})
    api = _api(hass, bridge, 0x20)
    entity = _cover(hass, api, 0x20)
    entity._uncertainty = 0.0  # noqa: SLF001

    await entity.async_set_cover_position(position=50)
    await _settle(entity)
    assert entity._uncertainty == pytest.approx(cover_module.DRIFT_PER_MOVE + 50 * cover_module.DRIFT_PER_DISTANCE)  # noqa: SLF001

    await entity.async_close_cover()
    await _settle(entity)
    assert entity._uncertainty == 0.0  # noqa: SLF001
    assert entity.current_cover_position == 0
    assert bridge.chips[0x20] == 0xFF


@pytest.mark.unit
@pytest.mark.parametrize(
    ("uncertainty", "mode", "target", "legs"),
    [
        (2.0, "auto", 5.0, [5.0]),
        (8.0, "auto", 5.0, [0.0, 5.0]),
        (8.0, "auto", 95.0, [100.0, 95.0]),
        (8.0, "auto", 50.0, [50.0]),
        (POSITION_UNKNOWN, "auto", 30.0, [0.0, 30.0]),
        (POSITION_UNKNOWN, "auto", 70.0, [100.0, 70.0]),
        (8.0, "auto", 0.0, [0.0]),
        (8.0, COVER_REFERENCE_OFF, 5.0, [5.0]),
        (8.0, "manual", 5.0, [5.0]),
    ],
)
def test_reference_is_planned_only_when_due(hass, uncertainty, mode, target, legs) -> None:
    api = _api(hass, FakeBridge({0x20: 0xFF}), 0x20)
    entity = _cover(hass, api, 0x20, reference_mode=mode)
    entity._position = 40.0  # noqa: SLF001
    entity._uncertainty = uncertainty  # noqa: SLF001

    assert entity._plan_legs(target) == legs  # noqa: SLF001


@pytest.mark.unit
async def test_opportunistic_reference_runs_through_the_end_first(hass, quick_reversal) -> None:
    bridge = FakeBridge({0x20: 0xFF})
    api = _api(hass, bridge, 0x20)
    entity = _cover(hass, api, 0x20)
    entity._position = 30.0  # noqa: SLF001
    entity._uncertainty = 8.0  # noqa: SLF001

    await entity.async_set_cover_position(position=5)
    await _settle(entity)

    down, up = 0xFF & ~_bit(1), 0xFF & ~_bit(0)
    assert [frame[0][1] for frame in bridge.writes()] == [down, 0xFF, up, 0xFF]
    assert entity.current_cover_position == 5
    assert entity._uncertainty == pytest.approx(cover_module.DRIFT_PER_MOVE + 5 * cover_module.DRIFT_PER_DISTANCE)  # noqa: SLF001


@pytest.mark.unit
async def test_an_uncertain_cover_runs_into_the_end_it_believes_it_is_at(hass) -> None:
    bridge = FakeBridge({0x20: 0xFF})
    api = _api(hass, bridge, 0x20)
    entity = _cover(hass, api, 0x20, close_time=0.2)
    entity._uncertainty = 20.0  # noqa: SLF001

    await entity.async_close_cover()
    assert entity._movement_task is not None  # noqa: SLF001
    await _settle(entity)

    assert entity._uncertainty == 0.0  # noqa: SLF001


@pytest.mark.unit
async def test_reversing_drops_the_relay_and_pauses(hass, quick_reversal) -> None:
    bridge = FakeBridge({0x20: 0xFF})
    api = _api(hass, bridge, 0x20)
    entity = _cover(hass, api, 0x20, open_time=5.0, close_time=5.0)
    entity._uncertainty = 0.0  # noqa: SLF001

    await entity.async_open_cover()
    await asyncio.sleep(0.1)
    with patch.object(cover_module.asyncio, "sleep", wraps=asyncio.sleep) as sleep:
        await entity.async_close_cover()
    await entity.async_stop_cover()

    down, up = 0xFF & ~_bit(1), 0xFF & ~_bit(0)
    assert [frame[0][1] for frame in bridge.writes()] == [up, 0xFF, down, 0xFF]
    sleep.assert_any_call(0.01)


@pytest.mark.unit
async def test_extending_a_move_keeps_the_motor_running(hass) -> None:
    bridge = FakeBridge({0x20: 0xFF})
    api = _api(hass, bridge, 0x20)
    entity = _cover(hass, api, 0x20, open_time=5.0)
    entity._uncertainty = 0.0  # noqa: SLF001

    await entity.async_set_cover_position(position=40)
    await asyncio.sleep(0.05)
    await entity.async_set_cover_position(position=60)
    await entity.async_stop_cover()

    up = 0xFF & ~_bit(0)
    assert [frame[0][1] for frame in bridge.writes()] == [up, up, 0xFF]


@pytest.mark.unit
async def test_reference_run_returns_to_the_previous_position(hass, quick_reversal) -> None:
    bridge = FakeBridge({0x20: 0xFF})
    api = _api(hass, bridge, 0x20)
    entity = _cover(hass, api, 0x20)
    entity._position = 70.0  # noqa: SLF001
    entity._uncertainty = 3.0  # noqa: SLF001

    await entity.async_reference_run()
    await _settle(entity)

    assert entity._uncertainty == pytest.approx(cover_module.DRIFT_PER_MOVE + 30 * cover_module.DRIFT_PER_DISTANCE)  # noqa: SLF001
    assert entity.current_cover_position == 70
    up, down = 0xFF & ~_bit(0), 0xFF & ~_bit(1)
    assert [frame[0][1] for frame in bridge.writes()] == [up, 0xFF, down, 0xFF]


@pytest.mark.unit
async def test_reference_run_needs_an_overrun(hass) -> None:
    api = _api(hass, FakeBridge({0x20: 0xFF}), 0x20)
    entity = _cover(hass, api, 0x20, overrun_time=0.0)

    with pytest.raises(cover_module.HomeAssistantError):
        await entity.async_reference_run()
