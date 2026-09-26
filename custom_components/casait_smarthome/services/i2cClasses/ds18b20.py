"""DS18B20 Digital Temperature .

High-precision digital thermometer providing 9 to 12-bit temperature readings
through a 1-Wire interface. Each sensor has a unique 64-bit serial code enabling
multiple sensors on a single 1-Wire bus.

Key Features:
- Temperature range: -55°C to +125°C
- Accuracy: ±0.5°C from -10°C to +85°C
- Programmable resolution: 9 to 12 bits (0.5°C to 0.0625°C)
- Parasitic power mode supported
- Unique 64-bit serial number
- Configurable temperature alarms

This driver only offers the two transactions a reading consists of. Waiting
for the conversion and deciding when to read belong to the caller, which can
release the bus in between:

- start_conversion() converts every DS18B20 on the strand at once
- read_temperature() reads one sensor's scratchpad afterwards

Timing characteristics:
- 9-bit resolution: 93.75ms
- 10-bit resolution: 187.5ms
- 11-bit resolution: 375ms
- 12-bit resolution: 750ms

References:
- Datasheet: https://datasheets.maximintegrated.com/en/ds/DS18B20.pdf
"""

from __future__ import annotations

import logging

_LOGGER = logging.getLogger(__name__)

# Conversion time at the default 12-bit resolution.
CONVERSION_TIME = 0.750


class DS18B20:
    """DS18B20 temperature sensors on one 1-Wire bus."""

    CMD_CONVERT_T = 0x44
    CMD_READ_SCRATCHPAD = 0xBE
    CMD_SKIP_ROM = 0xCC

    def __init__(self, bus_interface) -> None:
        """Initialize for one bus."""
        self.bus = bus_interface

    def start_conversion(self) -> bool:
        """Start a conversion in every sensor on the strand at once.

        CONVERT T after SKIP ROM reaches all DS18B20s simultaneously and they all
        take the same time, so addressing them one by one would pay the wait
        once per sensor for nothing.
        """

        if not self.bus.bridge.wire_reset():
            _LOGGER.debug("1-Wire reset failed before a DS18B20 conversion")
            return False
        return bool(self.bus.bridge.wire_write_bytes([self.CMD_SKIP_ROM, self.CMD_CONVERT_T]))

    def read_temperature(self, device_id: str) -> float | None:
        """Read temperature from scratchpad. Returns temperature in °C or None on error."""
        if not self.bus.select_device(device_id):
            return None

        self.bus.bridge.wire_write_byte(self.CMD_READ_SCRATCHPAD)
        scratchpad = self.bus.bridge.wire_read_bytes(9)
        if scratchpad is None:
            return None

        if self.bus.calc_crc8(bytes(scratchpad[:-1])) != scratchpad[-1]:
            _LOGGER.error("CRC check failed for %s", device_id)
            return None

        raw = (scratchpad[1] << 8) | scratchpad[0]
        if raw & 0x8000:  # Handle negative temperatures
            raw = -((~raw + 1) & 0xFFFF)

        resolution = ((scratchpad[4] >> 5) & 0x03) + 9
        raw = raw & (-1 << (12 - resolution))
        temp = raw * (0.0625 * (1 << (12 - resolution)))

        if temp == 85.0:  # Power-on value, likely invalid
            return None

        return temp
