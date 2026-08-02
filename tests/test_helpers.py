"""Contract tests for option-key parsers."""

from custom_components.casait_smarthome.const import (
    DEFAULT_BLIND_CLOSE_TIME,
    DEFAULT_BLIND_OPEN_TIME,
    DEFAULT_BLIND_OVERRUN_TIME,
    OM117_MODE_BLIND,
    OM117_MODE_SWITCH,
)
from custom_components.casait_smarthome.helpers import (
    build_bridge_slug,
    get_configured_led_counts,
    get_configured_onewire_poll_intervals,
    get_configured_onewire_profiles,
    get_dm117_port_configuration,
    get_om117_pair_configuration,
    migrated_device_identifiers,
    migrated_entity_identity,
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


def test_get_configured_onewire_poll_intervals_contract() -> None:
    options = {
        "ow_3a00000000000001_poll_interval": "1",
        "ow_1900000000000001_poll_interval": 10,
        "ow_2800000000000001_poll_interval": 3600,
        "ow_2800000000000002_poll_interval": 0,
        "ow_2800000000000003_poll_interval": 3601,
        "ow_2800000000000004_poll_interval": "invalid",
        "ow__poll_interval": 15,
        "ow_2800000000000001_profile": "ds18b20_temp",
        "unrelated": True,
    }

    assert get_configured_onewire_poll_intervals(options) == {
        "3a00000000000001": 1,
        "1900000000000001": 10,
        "2800000000000001": 3600,
    }


def test_build_bridge_slug_contract() -> None:
    assert build_bridge_slug("entry-test", "AA:BB:CC:DD:EE:FF") == "bridge_aabbccddeeff"
    assert build_bridge_slug("12345678-90ab-cdef", None) == "bridge_1234567890ab"


def test_migrated_device_identifiers_contract() -> None:
    assert migrated_device_identifiers("entry-test", {("casait_smarthome", "56")}) == {
        ("casait_smarthome", "entry-test_im117_56")
    }
    assert migrated_device_identifiers("entry-test", {("casait_smarthome", "32")}) == {
        ("casait_smarthome", "entry-test_om117_32")
    }
    assert migrated_device_identifiers("entry-test", {("casait_smarthome", "sm117_18")}) == {
        ("casait_smarthome", "entry-test_sm117_18")
    }
    assert migrated_device_identifiers("entry-test", {("casait_smarthome", "onewire_2800000000000001")}) == {
        ("casait_smarthome", "entry-test_onewire_2800000000000001")
    }


def test_migrated_entity_identity_contract() -> None:
    legacy_entities = {
        ("binary_sensor", "casait_smarthome_56_0"): (
            "binary_sensor.bridge_aabbccddeeff_im117_0x38_input_1",
            "entry-test_im117_56_0",
        ),
        ("binary_sensor", "entry-test_dm117_16_0_input_0"): (
            "binary_sensor.bridge_aabbccddeeff_dm117_0x10_slot_1_input_a",
            "entry-test_dm117_16_0_input_0",
        ),
        ("binary_sensor", "3a00000000000001_channel_0_input"): (
            "binary_sensor.bridge_aabbccddeeff_ds2413_3a00000000000001_input_a",
            "entry-test_3a00000000000001_channel_0_input",
        ),
        ("switch", "casait_smarthome_32_0"): (
            "switch.bridge_aabbccddeeff_om117_0x20_output_1",
            "entry-test_om117_32_0",
        ),
        ("switch", "entry-test_dm117_16_0_output_0"): (
            "switch.bridge_aabbccddeeff_dm117_0x10_slot_1_output_a",
            "entry-test_dm117_16_0_output_0",
        ),
        ("switch", "3a00000000000001_channel_0_output"): (
            "switch.bridge_aabbccddeeff_ds2413_3a00000000000001_output_a",
            "entry-test_3a00000000000001_channel_0_output",
        ),
        ("light", "entry-test_dm117_16_0_dimmer"): (
            "light.bridge_aabbccddeeff_dm117_0x10_slot_1_dimmer",
            "entry-test_dm117_16_0_dimmer",
        ),
        ("light", "1900000000000001_led_controller"): (
            "light.bridge_aabbccddeeff_ds28e17_1900000000000001_led_controller",
            "entry-test_1900000000000001_led_controller",
        ),
        ("cover", "entry-test_om117_32_pair_1_blind"): (
            "cover.bridge_aabbccddeeff_om117_0x20_blind_1",
            "entry-test_om117_32_pair_1_blind",
        ),
        ("sensor", "2800000000000001_temperature"): (
            "sensor.bridge_aabbccddeeff_ds18b20_2800000000000001_temperature",
            "entry-test_2800000000000001_temperature",
        ),
        ("sensor", "2600000000000001_humidity"): (
            "sensor.bridge_aabbccddeeff_ds2438_2600000000000001_humidity",
            "entry-test_2600000000000001_humidity",
        ),
        ("sensor", "entry-test_debug"): (
            "sensor.bridge_aabbccddeeff_diagnostics",
            "entry-test_debug",
        ),
    }

    assert {
        identity: migrated_entity_identity("entry-test", "AA:BB:CC:DD:EE:FF", *identity) for identity in legacy_entities
    } == legacy_entities
