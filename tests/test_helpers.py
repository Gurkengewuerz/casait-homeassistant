"""Contract tests for option-key parsers."""

from custom_components.casait_smarthome.const import (
    DEFAULT_BLIND_CLOSE_TIME,
    DEFAULT_BLIND_OPEN_TIME,
    DEFAULT_BLIND_OVERRUN_TIME,
    DEFAULT_DOUBLE_CLICK_MS,
    DEFAULT_IM117_ROLE,
    DEFAULT_LONG_PRESS_MS,
    IM117_ROLE_BUTTON,
    IM117_ROLE_CONTACT,
    IM117_ROLE_SWITCH,
    IM117_ROLE_UNUSED,
    OM117_MODE_BLIND,
    OM117_MODE_SWITCH,
)
from custom_components.casait_smarthome.helpers import (
    IM117PortConfig,
    InputSettings,
    OM117PairConfig,
    build_bridge_slug,
    get_configured_led_counts,
    get_configured_onewire_poll_intervals,
    get_configured_onewire_profiles,
    get_dm117_port_configuration,
    get_im117_port_configuration,
    get_input_settings,
    get_om117_pair_configuration,
    migrate_options_to_nested,
    migrated_device_identifiers,
    migrated_entity_identity,
    set_dm117_slots,
    set_im117_ports,
    set_input_settings,
    set_om117_pairs,
    set_onewire_device,
)
from custom_components.casait_smarthome.services.i2cClasses.dm117 import DeviceType


def test_get_om117_pair_configuration_contract() -> None:
    options = {
        "modules": {
            "om117": {
                "32": {
                    "pairs": {
                        "1": {
                            "mode": OM117_MODE_BLIND,
                            "open_time": "31.5",
                            "close_time": 29,
                            "overrun_time": "3",
                        },
                        "4": {"mode": "invalid", "open_time": object()},
                        "0": {"mode": OM117_MODE_BLIND},
                        "9": {"mode": OM117_MODE_BLIND},
                    }
                },
                "invalid": {"pairs": {"1": {"mode": OM117_MODE_BLIND}}},
            }
        },
        "unrelated": True,
    }

    parsed = get_om117_pair_configuration(options)

    assert set(parsed) == {32}
    assert set(parsed[32]) == {0, 3}
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
        "modules": {
            "dm117": {
                "16": {"slots": {"1": "binary_input", "2": "switch", "8": "dimmer", "0": "switch", "9": "switch"}},
                "17": {"slots": {"1": "none"}},
                "invalid": {"slots": {"1": "switch"}},
            }
        },
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
        "onewire": {
            "2800000000000001": {"profile": "ds18b20_temp", "led_count": 30},
            "3a00000000000001": {"profile": "ds2413_in"},
            "2600000000000001": {"poll_interval": 15},
        },
        "unrelated": True,
    }

    assert get_configured_onewire_profiles(options) == {
        "2800000000000001": "ds18b20_temp",
        "3a00000000000001": "ds2413_in",
    }


def test_get_configured_led_counts_contract() -> None:
    options = {
        "onewire": {
            "1900000000000001": {"profile": "ds28e17_led", "led_count": "60"},
            "1900000000000002": {"led_count": 255},
            "1900000000000003": {"led_count": 0},
            "1900000000000004": {"led_count": 256},
            "1900000000000005": {"led_count": "invalid"},
        },
        "unrelated": True,
    }

    assert get_configured_led_counts(options) == {
        "1900000000000001": 60,
        "1900000000000002": 255,
    }


def test_get_configured_onewire_poll_intervals_contract() -> None:
    options = {
        "onewire": {
            "3a00000000000001": {"poll_interval": "1"},
            "1900000000000001": {"poll_interval": 10},
            "2800000000000001": {"profile": "ds18b20_temp", "poll_interval": 3600},
            "2800000000000002": {"poll_interval": 0},
            "2800000000000003": {"poll_interval": 3601},
            "2800000000000004": {"poll_interval": "invalid"},
        },
        "unrelated": True,
    }

    assert get_configured_onewire_poll_intervals(options) == {
        "3a00000000000001": 1,
        "1900000000000001": 10,
        "2800000000000001": 3600,
    }


def test_get_im117_port_configuration_contract() -> None:
    options = {
        "modules": {
            "im117": {
                "56": {
                    "ports": {
                        "1": {"role": "button"},
                        "2": {"role": "contact", "device_class": "window"},
                        "3": {"role": "unused"},
                        "4": {"role": "bogus"},
                        # A device class only means anything for the contact role.
                        "5": {"role": "switch", "device_class": "door"},
                        "0": {"role": "button"},
                        "9": {"role": "button"},
                    }
                },
                "invalid": {"ports": {"1": {"role": "button"}}},
            }
        }
    }

    parsed = get_im117_port_configuration(options)

    assert set(parsed) == {56}
    assert set(parsed[56]) == {0, 1, 2, 3, 4}
    assert parsed[56][0].role == IM117_ROLE_BUTTON
    assert parsed[56][1].role == IM117_ROLE_CONTACT
    assert parsed[56][1].device_class == "window"
    assert parsed[56][2].role == IM117_ROLE_UNUSED
    assert parsed[56][3].role == DEFAULT_IM117_ROLE
    assert parsed[56][4].device_class is None


def test_get_input_settings_contract() -> None:
    assert get_input_settings({}) == InputSettings(
        long_press_ms=DEFAULT_LONG_PRESS_MS,
        double_click_ms=DEFAULT_DOUBLE_CLICK_MS,
    )

    configured = get_input_settings({"settings": {"long_press_ms": 800, "double_click_ms": 250}})
    assert configured == InputSettings(long_press_ms=800, double_click_ms=250)

    # Out-of-range and malformed values fall back rather than propagate.
    fallback = get_input_settings({"settings": {"long_press_ms": 10, "double_click_ms": "nope"}})
    assert fallback == InputSettings(
        long_press_ms=DEFAULT_LONG_PRESS_MS,
        double_click_ms=DEFAULT_DOUBLE_CLICK_MS,
    )


def test_set_im117_ports_round_trip() -> None:
    options = set_im117_ports(
        {},
        56,
        {
            0: IM117PortConfig(role=IM117_ROLE_BUTTON),
            1: IM117PortConfig(role=IM117_ROLE_CONTACT, device_class="door"),
            2: IM117PortConfig(role=IM117_ROLE_SWITCH, device_class="door"),
        },
    )

    parsed = get_im117_port_configuration(options)[56]
    assert parsed[0].role == IM117_ROLE_BUTTON
    assert parsed[1].device_class == "door"
    # A device class on a non-contact role is not persisted.
    assert parsed[2].device_class is None


def test_set_input_settings_round_trip() -> None:
    options = set_input_settings({"unrelated": True}, InputSettings(long_press_ms=900, double_click_ms=300))

    assert get_input_settings(options) == InputSettings(long_press_ms=900, double_click_ms=300)
    assert options["unrelated"] is True


def test_migrate_options_to_nested_contract() -> None:
    legacy = {
        "om117_32_pair_1_mode": OM117_MODE_BLIND,
        "om117_32_pair_1_open_time": 31.5,
        "om117_32_pair_1_close_time": 29,
        "om117_32_pair_1_overrun_time": 3,
        "dm117_16_slot_1": "binary_input",
        "dm117_16_slot_8": "dimmer",
        "ow_2800000000000001_profile": "ds18b20_temp",
        "ow_2800000000000001_poll_interval": 60,
        "ow_1900000000000001_profile": "ds28e17_led",
        "ow_1900000000000001_led_count": 60,
        "keep_me": "untouched",
    }

    migrated = migrate_options_to_nested(legacy)

    assert migrated["modules"]["om117"]["32"]["pairs"]["1"] == {
        "mode": OM117_MODE_BLIND,
        "open_time": 31.5,
        "close_time": 29,
        "overrun_time": 3,
    }
    assert migrated["modules"]["dm117"]["16"]["slots"] == {"1": "binary_input", "8": "dimmer"}
    assert migrated["onewire"]["2800000000000001"] == {"profile": "ds18b20_temp", "poll_interval": 60}
    assert migrated["onewire"]["1900000000000001"] == {"profile": "ds28e17_led", "led_count": 60}
    # Unknown keys survive; legacy ones are gone.
    assert migrated["keep_me"] == "untouched"
    assert not [key for key in migrated if key.startswith(("om117_", "dm117_", "ow_"))]


def test_migrate_options_to_nested_is_idempotent() -> None:
    legacy = {
        "om117_32_pair_1_mode": OM117_MODE_BLIND,
        "dm117_16_slot_1": "binary_input",
        "ow_2800000000000001_profile": "ds18b20_temp",
    }

    once = migrate_options_to_nested(legacy)

    assert migrate_options_to_nested(once) == once


def test_option_writers_round_trip() -> None:
    options = set_om117_pairs({}, 32, {0: OM117PairConfig(mode=OM117_MODE_BLIND, open_time=12.0)})
    options = set_dm117_slots(options, 16, {0: "dimmer"})
    options = set_onewire_device(options, "1900000000000001", "ds28e17_led", led_count=42, poll_interval=10)

    assert get_om117_pair_configuration(options)[32][0].open_time == 12.0
    assert get_dm117_port_configuration(options)[16][0] is DeviceType.DIMMER
    assert get_configured_led_counts(options) == {"1900000000000001": 42}
    assert get_configured_onewire_poll_intervals(options) == {"1900000000000001": 10}

    # Writing one module must not disturb another.
    options = set_dm117_slots(options, 17, {1: "switch"})
    assert get_dm117_port_configuration(options)[16][0] is DeviceType.DIMMER


def test_set_onewire_device_drops_stale_fields() -> None:
    options = set_onewire_device({}, "1900000000000001", "ds28e17_led", led_count=42, poll_interval=10)
    options = set_onewire_device(options, "1900000000000001", "ds18b20_temp", poll_interval=60)

    assert get_configured_led_counts(options) == {}
    assert get_configured_onewire_profiles(options) == {"1900000000000001": "ds18b20_temp"}


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
