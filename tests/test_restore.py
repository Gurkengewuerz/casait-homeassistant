"""Tests for recovering from a bridge reconnect and from output modules that lost power."""

from __future__ import annotations

import asyncio
import time
from typing import Any

from bridge_fakes import FakeBridge
import pytest

from custom_components.casait_smarthome.api import CasaITApi
from custom_components.casait_smarthome.const import OM117_MODE_SHUTTER, PCF8574_MAPPED_PORTS, POWER_ON_OFF
from custom_components.casait_smarthome.helpers import (
    OM117PairConfig,
    get_power_on_policies,
    get_power_on_policy,
    set_onewire_device,
    set_power_on_policy,
)
from custom_components.casait_smarthome.restore import CasaITOutputRestorer
from custom_components.casait_smarthome.services.i2cClasses.dm117 import DeviceType
from custom_components.casait_smarthome.services.i2cClasses.led_controller import LEDConfig
from custom_components.casait_smarthome.services.i2cClasses.pcf8574 import PCF8574
from homeassistant.core import callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect


class ScanningBridge(FakeBridge):
    """Bridge double that also takes a scanner configuration and counts connections."""

    connection_generation = 1

    def __init__(self, chips: dict[int, int], *, scanner: bool = True) -> None:
        super().__init__(chips)
        self.scanner = scanner
        self.scan_configs: list[list[int]] = []

    async def scan_config(self, addresses: list[int], period_ms: int, debounce_ms: int) -> bool:
        self.scan_configs.append(list(addresses))
        return self.scanner


def _api(hass, bridge: FakeBridge, *addresses: int, policies: dict[str, str] | None = None) -> CasaITApi:
    api = CasaITApi(hass, bridge, "entry-test", power_on_policies=policies)  # type: ignore[arg-type] - Test double for SMBus.
    for address in addresses:
        device = PCF8574(bridge, address)
        device.last_value = bridge.chips.get(address, 0xFF)
        api.im117_om117[address] = device
    return api


async def _drain(restorer: CasaITOutputRestorer) -> None:
    while restorer._tasks:  # noqa: SLF001
        await asyncio.gather(*restorer._tasks)  # noqa: SLF001
        await asyncio.sleep(0)


def _power_cut(api: CasaITApi, bridge: FakeBridge, address: int) -> None:
    """Reset a module to its power-on state and let the poll loop read it."""

    bridge.chips[address] = 0xFF
    api.restorer._written_at.clear()  # noqa: SLF001
    api._publish_pcf_reading(address, api.im117_om117[address].apply_reading(0xFF, _later()))  # noqa: SLF001


def _later() -> float:
    """Return a sample time past the debounce window of the writes just made."""

    return time.monotonic() * 1000 + 1000


def _bit(port: int) -> int:
    return 1 << PCF8574_MAPPED_PORTS[port]


# ---------------------------------------------------------------------------
# Bridge reconnect
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_a_reconnect_sets_the_scanner_up_again_and_drops_cached_outputs(hass) -> None:
    bridge = ScanningBridge({0x20: 0xFE, 0x38: 0xFF})
    api = _api(hass, bridge, 0x20, 0x38)
    assert await api._async_start_input_scanner()  # noqa: SLF001
    api._session_generation = bridge.connection_generation  # noqa: SLF001

    bridge.connection_generation += 1
    await api._async_resume_session()  # noqa: SLF001

    assert bridge.scan_configs == [[0x38], [0x38]]
    assert api._session_generation == bridge.connection_generation  # noqa: SLF001
    assert api.im117_om117[0x20].last_value == -1


@pytest.mark.unit
async def test_a_failed_scanner_setup_is_retried_on_the_next_cycle(hass) -> None:
    bridge = ScanningBridge({0x38: 0xFF})
    api = _api(hass, bridge, 0x38)
    assert await api._async_start_input_scanner()  # noqa: SLF001
    api._session_generation = bridge.connection_generation  # noqa: SLF001

    bridge.connection_generation += 1
    bridge.scanner = False
    await api._async_resume_session()  # noqa: SLF001

    assert api._session_generation != bridge.connection_generation  # noqa: SLF001
    assert api._scan_addresses == []  # noqa: SLF001
    assert api._scanner_supported  # noqa: SLF001


@pytest.mark.unit
async def test_a_bridge_without_scanner_is_not_probed_again(hass) -> None:
    bridge = ScanningBridge({0x38: 0xFF}, scanner=False)
    api = _api(hass, bridge, 0x38)
    assert not await api._async_start_input_scanner()  # noqa: SLF001

    bridge.connection_generation += 1
    await api._async_resume_session()  # noqa: SLF001

    assert bridge.scan_configs == [[0x38]]
    assert api._session_generation == bridge.connection_generation  # noqa: SLF001


# ---------------------------------------------------------------------------
# OM117
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_switch_outputs_come_back_after_a_power_cut(hass) -> None:
    bridge = FakeBridge({0x20: 0xFF})
    api = _api(hass, bridge, 0x20)
    assert await api.async_write_pcf_ports(0x20, {PCF8574_MAPPED_PORTS[0]: 0, PCF8574_MAPPED_PORTS[3]: 0})
    commanded = bridge.chips[0x20]

    _power_cut(api, bridge, 0x20)
    await _drain(api.restorer)

    assert bridge.chips[0x20] == commanded
    assert api.pcf_states[0x20][PCF8574_MAPPED_PORTS[0]] == 0


@pytest.mark.unit
async def test_the_off_policy_leaves_outputs_off_and_adopts_it(hass) -> None:
    bridge = FakeBridge({0x20: 0xFF})
    api = _api(hass, bridge, 0x20, policies={"om117:32": POWER_ON_OFF})
    assert await api.async_write_pcf_port(0x20, PCF8574_MAPPED_PORTS[0], 0)
    frames = len(bridge.frames)

    _power_cut(api, bridge, 0x20)
    await _drain(api.restorer)

    assert len(bridge.frames) == frames
    assert api.restorer._pcf[0x20] == 0xFF  # noqa: SLF001


@pytest.mark.unit
async def test_cover_outputs_stay_off_and_moving_covers_are_told(hass) -> None:
    bridge = FakeBridge({0x20: 0xFF})
    api = _api(hass, bridge, 0x20)
    api.om117_pair_configuration = {0x20: {0: OM117PairConfig(mode=OM117_MODE_SHUTTER)}}
    assert await api.async_write_pcf_ports(0x20, {PCF8574_MAPPED_PORTS[0]: 0, PCF8574_MAPPED_PORTS[2]: 0})
    signalled: list[bool] = []

    @callback
    def record() -> None:
        signalled.append(True)

    async_dispatcher_connect(hass, api.power_loss_signal(0x20), record)
    _power_cut(api, bridge, 0x20)
    await _drain(api.restorer)

    assert signalled == [True]
    assert bridge.chips[0x20] == 0xFF & ~_bit(2)


@pytest.mark.unit
async def test_a_first_reading_is_adopted_and_our_own_writes_are_not_mismatches(hass) -> None:
    bridge = FakeBridge({0x20: 0xF0})
    api = _api(hass, bridge, 0x20)
    frames = len(bridge.frames)

    api._publish_pcf_reading(0x20, api.im117_om117[0x20].apply_reading(0xF0, _later()))  # noqa: SLF001
    assert api.restorer._pcf[0x20] == 0xF0  # noqa: SLF001

    assert await api.async_write_pcf_port(0x20, 0, 0)
    api._publish_pcf_reading(0x20, api.im117_om117[0x20].apply_reading(0xF0, _later()))  # noqa: SLF001
    await _drain(api.restorer)
    assert len(bridge.frames) == frames + 1


@pytest.mark.unit
async def test_a_module_that_keeps_resetting_is_restored_once_per_cooldown(hass) -> None:
    bridge = FakeBridge({0x20: 0xFF})
    api = _api(hass, bridge, 0x20)
    assert await api.async_write_pcf_port(0x20, PCF8574_MAPPED_PORTS[0], 0)

    _power_cut(api, bridge, 0x20)
    await _drain(api.restorer)
    frames = len(bridge.frames)
    bridge.chips[0x20] = 0xFF
    api.restorer._written_at.clear()  # noqa: SLF001
    api._publish_pcf_reading(0x20, api.im117_om117[0x20].apply_reading(0xFF, _later()))  # noqa: SLF001
    await _drain(api.restorer)

    assert len(bridge.frames) == frames


@pytest.mark.unit
async def test_the_commanded_state_survives_a_restart(hass, hass_storage) -> None:
    bridge = FakeBridge({0x20: 0xFF})
    api = _api(hass, bridge, 0x20)
    api.restorer.note_pcf_written(0x20, 0xFA)
    api.restorer.note_ds2413_written("3a01", 1, True)
    api.restorer.note_led_written("1901", LEDConfig.create_default())
    await api.restorer._store.async_save(api.restorer._data_to_save())  # noqa: SLF001

    fresh = CasaITOutputRestorer(api)
    await fresh.async_load()

    assert fresh._pcf == {0x20: 0xFA}  # noqa: SLF001
    assert fresh._ds2413 == {"3a01": {1: True}}  # noqa: SLF001
    assert fresh._led == {"1901": LEDConfig.create_default()}  # noqa: SLF001


# ---------------------------------------------------------------------------
# DM117, DS2413 and the LED controller
# ---------------------------------------------------------------------------


class RecordingApi:
    """Just enough API for the restorer to write through."""

    def __init__(self, hass, slots: dict[int, DeviceType] | None = None) -> None:
        self.hass = hass
        self.entry_id = "entry-test"
        self.om117_pair_configuration: dict[int, Any] = {}
        self.calls: list[tuple[Any, ...]] = []
        self._slots = slots or {}

    def dm117_slot_types(self, address: int) -> dict[int, DeviceType]:
        return dict(self._slots)

    async def async_configure_dm117(self, config: dict[int, dict[int, DeviceType]]) -> None:
        self.calls.append(("configure", config))

    async def async_write_dm117_port(self, address: int, config: Any) -> bool:
        value = config.dimmer.raw_value if config.dimmer else config.digital.raw_value
        self.calls.append(("dm117", address, config.port, value))
        return True

    async def write_ds2413_state(self, device_id: str, channel: int, on: bool) -> bool:
        self.calls.append(("ds2413", device_id, channel, on))
        return True

    async def write_led_config(self, device_id: str, config: LEDConfig) -> bool:
        self.calls.append(("led", device_id, config.state))
        return True


@pytest.mark.unit
async def test_a_reset_dm117_is_reconfigured_and_its_outputs_restored(hass) -> None:
    api = RecordingApi(hass, {0: DeviceType.DIMMER, 1: DeviceType.OUTPUT, 2: DeviceType.INPUT})
    restorer = CasaITOutputRestorer(api)  # type: ignore[arg-type] - Test double for CasaITApi.
    restorer.note_dm117_written(0x10, 0, 2000)
    restorer.note_dm117_written(0x10, 1, 3)
    restorer._written_at.clear()  # noqa: SLF001

    restorer.check_dm117(0x10, {0: 0, 1: 0, 2: 1}, slots_lost=True)
    await _drain(restorer)

    assert api.calls == [
        ("configure", {0x10: {0: DeviceType.DIMMER, 1: DeviceType.OUTPUT, 2: DeviceType.INPUT}}),
        ("dm117", 0x10, 0, 2000),
        ("dm117", 0x10, 1, 3),
    ]


@pytest.mark.unit
async def test_a_ramping_dimmer_is_left_alone(hass) -> None:
    api = RecordingApi(hass, {0: DeviceType.DIMMER})
    restorer = CasaITOutputRestorer(api)  # type: ignore[arg-type] - Test double for CasaITApi.
    restorer.note_dm117_written(0x10, 0, 2000)

    restorer.check_dm117(0x10, {0: 800}, slots_lost=False)
    await _drain(restorer)

    assert api.calls == []


@pytest.mark.unit
async def test_ds2413_and_led_follow_their_policies(hass) -> None:
    api = RecordingApi(hass)
    restorer = CasaITOutputRestorer(api, {"onewire:1901": POWER_ON_OFF})  # type: ignore[arg-type] - Test double for CasaITApi.
    restorer.note_ds2413_written("3a01", 0, True)
    lit = LEDConfig.create_default()
    lit.state = True
    restorer.note_led_written("1901", lit)
    restorer._written_at.clear()  # noqa: SLF001

    restorer.check_ds2413("3a01", (True, True))
    restorer.check_led("1901", LEDConfig.create_default())
    await _drain(restorer)

    assert api.calls == [("ds2413", "3a01", 0, True), ("led", "1901", False)]


# ---------------------------------------------------------------------------
# Options
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_power_on_policies_default_to_restore_and_survive_profile_edits() -> None:
    options = set_power_on_policy({}, "om117", 0x20, POWER_ON_OFF)
    options = set_power_on_policy(options, "onewire", "1901", POWER_ON_OFF)
    options = set_onewire_device(options, "1901", "ds28e17_led")

    assert get_power_on_policies(options) == {"om117:32": POWER_ON_OFF, "onewire:1901": POWER_ON_OFF}
    assert get_power_on_policy(options, "dm117", 0x10) == "restore"
