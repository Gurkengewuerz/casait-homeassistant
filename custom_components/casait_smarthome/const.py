"""Constants for the casaIT : Smart Home integration."""

from typing import Final

DOMAIN: Final = "casait_smarthome"

CONF_TIMEOUT: Final = "timeout"

# I2C address ranges for device scanning
I2C_ADDR_RANGES: Final = [
    (0x38, 0x3F, "Input modules (PCF8574)", "IM117"),
    (0x20, 0x27, "Output modules (PCF8574)", "OM117"),
    (0x10, 0x17, "Digital modules (ATMega8)", "DM117"),
    (0x18, 0x1B, "Sensor modules (DS2482)", "SM117"),
]

# Platforms
PLATFORMS: Final = ["binary_sensor", "cover", "light", "sensor", "switch"]

# Services
SERVICE_SCAN_DEVICES: Final = "scan_devices"

# Output module defaults
OM117_MODE_SWITCH: Final = "switch"
OM117_MODE_BLIND: Final = "blind"
DEFAULT_BLIND_OPEN_TIME: Final = 25.0
DEFAULT_BLIND_CLOSE_TIME: Final = 25.0
DEFAULT_BLIND_OVERRUN_TIME: Final = 2.0

# Default profiles for 1-Wire devices by family code
DEFAULT_OW_PROFILE: Final = {
    0x28: "ds18b20_temp",  # DS18B20
    0x26: "ds2438_hih5030_tept5600",  # DS2438
    0x3A: "ds2413_in",  # DS2413
    0x19: "ds28e17_led",  # DS28E17
}

DEFAULT_LED_COUNT: Final = 30

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
