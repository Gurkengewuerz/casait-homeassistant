"""Contract tests for option-key parsers."""

from custom_components.casait_smarthome.const import (
    DEFAULT_BLIND_CLOSE_TIME,
    DEFAULT_BLIND_OPEN_TIME,
    DEFAULT_BLIND_OVERRUN_TIME,
    OM117_MODE_BLIND,
    OM117_MODE_SWITCH,
)
from custom_components.casait_smarthome.helpers import (
    get_configured_led_counts,
    get_configured_onewire_profiles,
    get_dm117_port_configuration,
    get_om117_pair_configuration,
)
from custom_components.casait_smarthome.services.i2cClasses.dm117 import DeviceType


def test_get_om117_pair_configuration_contract() -> None:
    options = {
        "om117_32_pair_1_mode": OM117_MODE_BLIND,
        "om117_32_pair_1_open_time": "31.5",
        "om117_32_pair_1_close_time": 29,
        "om117_32_pair_1_overrun_time": "3",
        "om117_32_pair_4_mode": "invalid",
        "om117_32_pair_4_open_time": object(),
        "om117_32_pair_0_mode": OM117_MODE_BLIND,
        "om117_invalid_pair_1_mode": OM117_MODE_BLIND,
        "unrelated": True,
    }

    parsed = get_om117_pair_configuration(options)

    assert set(parsed) == {32}
    assert parsed[32][0].mode == OM117_MODE_BLIND
    assert parsed[32][0].open_time == 31.5
    assert parsed[32][0].close_time == 29
    assert parsed[32][0].overrun_time == 3
    assert parsed[32][3].mode == OM117_MODE_SWITCH
    assert parsed[32][3].open_time == DEFAULT_BLIND_OPEN_TIME
    assert parsed[32][3].close_time == DEFAULT_BLIND_CLOSE_TIME
    assert parsed[32][3].overrun_time == DEFAULT_BLIND_OVERRUN_TIME


def test_get_dm117_port_configuration_contract() -> None:
    options = {
        "dm117_16_slot_1": "binary_input",
        "dm117_16_slot_2": "switch",
        "dm117_16_slot_8": "dimmer",
        "dm117_16_slot_0": "switch",
        "dm117_invalid_slot_1": "switch",
        "dm117_17_slot_1": "none",
        "unrelated": True,
    }

    parsed = get_dm117_port_configuration(options)

    assert dict(parsed) == {
        16: {
            0: DeviceType.INPUT,
            1: DeviceType.OUTPUT,
            7: DeviceType.DIMMER,
        }
    }


def test_get_configured_onewire_profiles_contract() -> None:
    options = {
        "ow_2800000000000001_profile": "ds18b20_temp",
        "ow_3a00000000000001_profile": "ds2413_in",
        "ow__profile": "ignored",
        "ow_2800000000000001_led_count": 30,
        "unrelated": True,
    }

    assert get_configured_onewire_profiles(options) == {
        "2800000000000001": "ds18b20_temp",
        "3a00000000000001": "ds2413_in",
    }


def test_get_configured_led_counts_contract() -> None:
    options = {
        "ow_1900000000000001_led_count": "60",
        "ow_1900000000000002_led_count": 255,
        "ow_1900000000000003_led_count": 0,
        "ow_1900000000000004_led_count": 256,
        "ow_1900000000000005_led_count": "invalid",
        "ow__led_count": 30,
        "ow_1900000000000001_profile": "ds28e17_led",
        "unrelated": True,
    }

    assert get_configured_led_counts(options) == {
        "1900000000000001": 60,
        "1900000000000002": 255,
    }
