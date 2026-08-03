"""Constants for the casaIT : Smart Home integration."""

from typing import Final

CONFIG_ENTRY_VERSION: Final = 3
DOMAIN: Final = "casait_smarthome"

CONF_TIMEOUT: Final = "timeout"
CONF_SUBTYPE: Final = "subtype"

# Top-level sections of the config entry options. Everything below them is keyed
# by module address (decimal, as a string) or by 1-Wire device id. A flat
# namespace was used up to entry version 2; see migrate_options_to_nested.
OPT_MODULES: Final = "modules"
OPT_ONEWIRE: Final = "onewire"
OPT_SETTINGS: Final = "settings"

# Per-module sub-keys
OPT_PAIRS: Final = "pairs"
OPT_SLOTS: Final = "slots"
OPT_PORTS: Final = "ports"
OPT_NAME: Final = "name"

# I2C address ranges for device scanning
I2C_ADDR_RANGES: Final = [
    (0x38, 0x3F, "Input modules (PCF8574)", "IM117"),
    (0x20, 0x27, "Output modules (PCF8574)", "OM117"),
    (0x10, 0x17, "Digital modules (ATMega8)", "DM117"),
    (0x18, 0x1B, "Sensor modules (DS2482)", "SM117"),
]

# Platforms
PLATFORMS: Final = ["binary_sensor", "button", "cover", "event", "light", "number", "sensor", "switch"]

# What an IM117 input port is wired to. The default keeps every discovered port
# a plain binary sensor, which is how the integration behaved before roles
# existed - upgrading must not silently delete anyone's entities.
IM117_ROLE_SWITCH: Final = "switch"
IM117_ROLE_BUTTON: Final = "button"
IM117_ROLE_CONTACT: Final = "contact"
IM117_ROLE_UNUSED: Final = "unused"
DEFAULT_IM117_ROLE: Final = IM117_ROLE_SWITCH

# Device classes offered for the "contact" role.
IM117_CONTACT_DEVICE_CLASSES: Final = [
    "door",
    "window",
    "garage_door",
    "motion",
    "occupancy",
    "smoke",
    "moisture",
    "gas",
    "problem",
    "safety",
    "tamper",
]

# Event types published by button inputs. Presses report the moment the edge is
# seen and releases report when the button comes back up, so an automation can
# react while the user is still holding the button.
EVENT_SINGLE_PRESS: Final = "single_press"
EVENT_SINGLE_RELEASE: Final = "single_release"
EVENT_DOUBLE_PRESS: Final = "double_press"
EVENT_LONG_PRESS: Final = "long_press"
EVENT_LONG_RELEASE: Final = "long_release"
BUTTON_EVENT_TYPES: Final = [
    EVENT_SINGLE_PRESS,
    EVENT_SINGLE_RELEASE,
    EVENT_DOUBLE_PRESS,
    EVENT_LONG_PRESS,
    EVENT_LONG_RELEASE,
]

# Button timing. Double click defaults to off: a second press is only reported
# as a double press while this window is open, and most inputs are plain wall
# switches where the extra gesture is not wanted.
OPT_LONG_PRESS_MS: Final = "long_press_ms"
OPT_DOUBLE_CLICK_MS: Final = "double_click_ms"
OPT_FAST_POLL_INTERVAL_MS: Final = "fast_poll_interval_ms"
OPT_SLOW_POLL_INTERVAL: Final = "slow_poll_interval"
OPT_MAX_SEND_INTERVAL_MS: Final = "max_send_interval_ms"
DEFAULT_LONG_PRESS_MS: Final = 500
DEFAULT_DOUBLE_CLICK_MS: Final = 0

# Shared I2C poll loop. Inputs decide how responsive the system feels, so they are
# read every cycle. Outputs only ever change because Home Assistant changed them,
# so they are re-read on a slow cadence purely to catch drift.
DEFAULT_FAST_POLL_INTERVAL: Final = 0.02
DEFAULT_SLOW_POLL_INTERVAL: Final = 5.0
DEFAULT_MAX_SEND_INTERVAL: Final = 0.005

# Button events mirrored to the Home Assistant event bus for device triggers.
EVENT_BUTTON: Final = f"{DOMAIN}_button_event"
EVENT_DATA_EVENT_TYPE: Final = "event_type"
EVENT_DATA_SUBTYPE: Final = "subtype"

# Services
SERVICE_SCAN_DEVICES: Final = "scan_devices"
SERVICE_SET_LED_PALETTE: Final = "set_led_palette"

# Output module defaults
OM117_MODE_SWITCH: Final = "switch"
OM117_MODE_BLIND: Final = "blind"
OM117_MODE_SHUTTER: Final = "shutter"
OM117_MODE_PULSE: Final = "pulse"
DEFAULT_BLIND_OPEN_TIME: Final = 25.0
DEFAULT_BLIND_CLOSE_TIME: Final = 25.0
DEFAULT_BLIND_OVERRUN_TIME: Final = 2.0
DEFAULT_BLIND_TILT_TIME: Final = 1.5
DEFAULT_PULSE_DURATION: Final = 0.5

# Per-channel roles for the independently configurable DS2413 pins.
DS2413_CHANNEL_INPUT: Final = "input"
DS2413_CHANNEL_OUTPUT: Final = "output"

# Default profiles for 1-Wire devices by family code
DEFAULT_OW_PROFILE: Final = {
    0x28: "ds18b20_temp",  # DS18B20
    0x26: "ds2438_hih5030_tept5600",  # DS2438
    0x3A: "ds2413_in",  # DS2413
    0x19: "ds28e17_led",  # DS28E17
}

DEFAULT_LED_COUNT: Final = 30

DEFAULT_OW_POLL_INTERVAL: Final = {
    "ds2413_in": 1,
    "ds2413_out": 1,
    "ds2413": 1,
    "ds28e17_led": 10,
    "ds2438_hih4030_tept5600": 15,
    "ds2438_hih5030_tept5600": 15,
    "ds18b20_temp": 60,
}

# Dispatcher signals
SIGNAL_STATE_UPDATED: Final = "casait_state_updated"

PCF8574_MAPPED_PORTS: Final = {
    0: 2,
    1: 1,
    2: 0,
    3: 7,
    4: 6,
    5: 5,
    6: 3,
    7: 4,
}
