"""Tests for what newer bridge firmware offers: boot ids, capabilities, output timers and sessions."""

from __future__ import annotations

import asyncio
import time

from bridge_fakes import FakeBridge
import pytest

from custom_components.casait_smarthome import outputs as outputs_module
from custom_components.casait_smarthome.api import CasaITApi
from custom_components.casait_smarthome.const import OM117_MODE_SHUTTER, PCF8574_MAPPED_PORTS
from custom_components.casait_smarthome.cover import CasaITBlindCover
from custom_components.casait_smarthome.helpers import OM117PairConfig
from custom_components.casait_smarthome.services.i2cClasses.pcf8574 import PCF8574
from custom_components.casait_smarthome.services.smbus_proxy import (
    CMD_TIMED_OUTPUT,
    REQUIRED_CAPABILITIES,
    BridgeFirmwareError,
    BridgeInfo,
    SMBus,
)

ENTRY = type("Entry", (), {"entry_id": "entry-test", "unique_id": "AA:BB:CC:DD:EE:FF", "options": {}})()
UP = 1 << PCF8574_MAPPED_PORTS[0]


class WatchBridge(FakeBridge):
    """Bridge double that answers the watch and interlock commands."""

    def __init__(self, chips: dict[int, int], *, boot_id: int = 0x1234) -> None:
        super().__init__(chips, boot_id=boot_id)
        self.watch_configs = 0
        # Whether the bridge reports that it continues an earlier watch.
        self.resumed = True

    async def watch_config(self, modules: list[tuple[int, int]], fast_ms: int, slow_ms: int, debounce_ms: int) -> bool:
        self.watch_configs += 1
        return self.resumed

    async def interlock(self, addr: int, dead_ms: int, pairs: list[tuple[int, int]]) -> None:
        return

    def watch_ack(self, seq: int) -> bool:
        return True


async def _api(hass, bridge: FakeBridge, *addresses: int) -> CasaITApi:
    api = CasaITApi(hass, bridge, "entry-test")  # type: ignore[arg-type] - Test double for SMBus.
    for address in addresses:
        device = PCF8574(bridge, address)
        device.last_value = bridge.chips.get(address, 0xFF)
        api.im117_om117[address] = device
    api.bridge_info = await bridge.ping_info()
    return api


def _later() -> float:
    return time.monotonic() * 1000 + 1000


# ---------------------------------------------------------------------------
# Wire format
# ---------------------------------------------------------------------------


def _responding(payloads: list[bytes], sent: list[bytes]):
    async def send(payload: bytes) -> bytes:
        sent.append(payload)
        return payloads.pop(0)

    return send


@pytest.mark.unit
async def test_ping_reports_boot_id_and_uptime_and_refuses_old_firmware() -> None:
    bus = SMBus()
    sent: list[bytes] = []
    bus._send_command = _responding(  # type: ignore[method-assign]  # noqa: SLF001
        [
            bytes([0x00, 0x11, 0xAA, 0xDE, 0xAD, 0xBE, 0xEF, 0, 0, 1, 0, 6])
            + b"v0.0.1"
            + bytes([REQUIRED_CAPABILITIES, 0x01, 0xF4, 0x03, 0xE8, 0, 0, 0, 7, 0, 0, 0, 2]),
            # Firmware before the watch: the version is the last thing it sends.
            bytes([0x00, 0x11, 0xAA, 0xDE, 0xAD, 0xBE, 0xEF, 0, 0, 1, 0, 6]) + b"v0.0.1",
            bytes([0x00, 0x11, 0xAA, 0xDE, 0xAD, 0xBE, 0xEF, 0, 0, 1, 0]),
            b"",
        ],
        sent,
    )

    assert await bus.ping_info() == BridgeInfo(
        boot_id=0xDEADBEEF,
        uptime_s=256,
        version="v0.0.1",
        fast_sweep_us=500,
        slow_sweep_us=1000,
        i2c_retries=7,
        interlock_refusals=2,
    )
    with pytest.raises(BridgeFirmwareError):
        await bus.ping_info()
    with pytest.raises(BridgeFirmwareError):
        await bus.ping_info()
    assert await bus.ping_info() is None


@pytest.mark.unit
async def test_timed_output_encodes_the_duration_and_checks_the_answer() -> None:
    bus = SMBus()
    sent: list[bytes] = []
    bus._send_command = _responding([bytes([0x00, 0xFB]), bytes([0xFF])], sent)  # type: ignore[method-assign]  # noqa: SLF001

    assert await bus.timed_output(0x20, 0x06, 0x02, 0x06, 70_000) == 0xFB
    assert sent[0] == bytes([CMD_TIMED_OUTPUT, 0x20, 0x06, 0x02, 0x06, 0x00, 0x01, 0x11, 0x70])
    with pytest.raises(OSError, match="refused"):
        await bus.timed_output(0x20, 0x06, 0x02, 0x06, 100)
    with pytest.raises(ValueError, match="Timer"):
        await bus.timed_output(0x20, 0x06, 0x02, 0x06, 0)


# ---------------------------------------------------------------------------
# Reconnects and bridge restarts
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_a_network_drop_keeps_input_state_but_drops_outputs_and_timers(hass) -> None:
    bridge = WatchBridge({0x20: 0xFF, 0x38: 0xF0})
    api = await _api(hass, bridge, 0x20, 0x38)
    await api._async_start_session()  # noqa: SLF001
    assert await api.async_arm_output_timer(0x20, UP, 0, UP, 30)

    bridge.connection_generation += 1
    await api._async_resume_session()  # noqa: SLF001

    assert api.im117_om117[0x38].last_value == 0xF0
    assert api.im117_om117[0x20].last_value == -1
    assert api.outputs.diagnostics() == {}
    assert bridge.watch_configs == 2
    assert api._session_generation == bridge.connection_generation  # noqa: SLF001


@pytest.mark.unit
async def test_a_changed_boot_id_is_logged_as_a_restart(hass, caplog) -> None:
    bridge = WatchBridge({0x38: 0xFF})
    api = await _api(hass, bridge, 0x38)
    bridge.boot_id = 0x9999
    bridge.connection_generation += 1

    await api._async_resume_session()  # noqa: SLF001

    assert "Bridge restarted" in caplog.text
    assert api.bridge_info == BridgeInfo(boot_id=0x9999, uptime_s=0, version="v0.0.1")


@pytest.mark.unit
async def test_a_restart_seen_by_the_heartbeat_sets_the_session_up_again(hass) -> None:
    bridge = WatchBridge({0x38: 0xFF})
    api = await _api(hass, bridge, 0x38)
    await api._async_start_session()  # noqa: SLF001
    assert api._watch_ready  # noqa: SLF001

    await api._async_heartbeat()  # noqa: SLF001
    assert api._watch_ready  # noqa: SLF001

    bridge.boot_id = 0x9999
    await api._async_heartbeat()  # noqa: SLF001
    assert not api._watch_ready  # noqa: SLF001


# ---------------------------------------------------------------------------
# Output timers
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_a_refused_timer_leaves_the_outputs_alone(hass) -> None:
    bridge = FakeBridge({0x20: 0xFF})
    api = await _api(hass, bridge, 0x20)
    bridge.chips.pop(0x20)

    assert not await api.async_arm_output_timer(0x20, UP, 0, UP, 5)
    assert not await api.async_arm_output_timer(0x21, UP, 0, UP, 5)
    assert api.outputs.diagnostics() == {}


@pytest.mark.unit
async def test_a_write_after_a_timer_ran_out_does_not_switch_it_on_again(hass) -> None:
    bridge = FakeBridge({0x20: 0xFF})
    api = await _api(hass, bridge, 0x20)
    assert await api.async_arm_output_timer(0x20, UP, 0, UP, 0.05)
    assert bridge.chips[0x20] == 0xFF & ~UP

    await asyncio.sleep(0.06)
    bridge.expire_timers()
    assert await api.async_write_pcf_port(0x20, 7, 0)

    assert bridge.chips[0x20] == 0xFF & ~(1 << 7)


@pytest.mark.unit
async def test_a_write_that_changes_timer_bits_takes_them_back(hass) -> None:
    bridge = FakeBridge({0x20: 0xFF})
    api = await _api(hass, bridge, 0x20)
    assert await api.async_arm_output_timer(0x20, UP, 0, UP, 30)

    assert await api.async_write_pcf_ports(0x20, {PCF8574_MAPPED_PORTS[0]: 1})

    assert bridge.timers == []
    assert api.outputs.diagnostics() == {}


@pytest.mark.unit
async def test_a_neighbouring_write_keeps_the_timer(hass) -> None:
    bridge = FakeBridge({0x20: 0xFF})
    api = await _api(hass, bridge, 0x20)
    assert await api.async_arm_output_timer(0x20, UP, 0, UP, 30)

    assert await api.async_write_pcf_port(0x20, 7, 0)

    assert len(bridge.timers) == 1
    assert "0x20" in api.outputs.diagnostics()


@pytest.mark.unit
async def test_bits_a_timer_released_are_not_mistaken_for_a_power_cut(hass) -> None:
    bridge = FakeBridge({0x20: 0xFF})
    api = await _api(hass, bridge, 0x20)
    api.om117_pair_configuration = {0x20: {0: OM117PairConfig(mode=OM117_MODE_SHUTTER)}}
    assert await api.async_arm_output_timer(0x20, UP, 0, UP, 0.01)
    bridge.expire_timers()
    api.restorer._written_at.clear()  # noqa: SLF001

    api._publish_pcf_reading(0x20, api.im117_om117[0x20].apply_reading(0xFF, _later()))  # noqa: SLF001

    assert api.restorer._tasks == set()  # noqa: SLF001
    assert api.restorer._restored_at == {}  # noqa: SLF001


@pytest.mark.unit
async def test_a_cover_hands_its_stop_to_the_bridge(hass) -> None:
    bridge = FakeBridge({0x20: 0xFF})
    api = await _api(hass, bridge, 0x20)
    config = OM117PairConfig(mode=OM117_MODE_SHUTTER, open_time=2.0, close_time=2.0, overrun_time=0.5)
    cover = CasaITBlindCover(api, ENTRY, 0x20, 0, config)
    cover.hass = hass
    cover.async_write_ha_state = lambda: None  # type: ignore[method-assign] - Not added to a platform.
    cover._uncertainty = 0.0  # noqa: SLF001

    await cover.async_set_cover_position(position=50)
    await asyncio.sleep(0.05)

    assert len(bridge.timers) == 1
    addr, mask, value, revert, duration_ms = bridge.timers[0]
    assert (addr, mask, value, revert) == (0x20, UP, 0, UP)
    assert 900 <= duration_ms <= 1000

    await cover.async_stop_cover()
    assert bridge.timers == []
    assert bridge.chips[0x20] == 0xFF


@pytest.mark.unit
def test_the_timer_margin_covers_the_coalesce_window() -> None:
    assert outputs_module.TIMER_MARGIN > outputs_module.OUTPUT_COALESCE_WINDOW
