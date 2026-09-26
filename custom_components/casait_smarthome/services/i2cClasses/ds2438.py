"""DS2438 Smart Battery Monitor.

A sophisticated battery management IC that integrates three measurement functions:
- Voltage: Measures VDD (supply voltage), VAD (A/D input), VSE (current sense input)
- Temperature: Built-in direct-to-digital thermal sensor
- Current: High-precision current measurement using external sense resistor

Key Features:
- Direct-to-digital temperature sensor: -40°C to +85°C ±2°C
- Battery voltage measurement: 0 to 10V ±10mV
- Current measurement: Configurable via sense resistor
- 40 bytes of nonvolatile EEPROM memory
- Supports multiple conversion modes and resolutions
- 1-Wire interface for minimal connection requirements

This driver offers single transactions only; the caller sequences them and
waits for the conversions with the bus released.

References:
- Datasheet: https://www.analog.com/media/en/technical-documentation/data-sheets/DS2438.pdf
"""

from __future__ import annotations

from dataclasses import dataclass
import logging

_LOGGER = logging.getLogger(__name__)

# Conversion time of one voltage or temperature conversion.
CONVERSION_TIME = 0.012


@dataclass
class DS2438Reading:
    """One complete set of DS2438 measurements."""

    vdd: float  # Supply voltage
    vad: float  # A/D voltage input
    vse: float  # Current sense voltage
    temperature: float  # Temperature in Celsius


@dataclass
class DS2438Page:
    """Page 0 of a DS2438: the latest conversion results."""

    status: int
    temperature: float
    voltage: float
    current_voltage: float


class DS2438:
    """DS2438 smart battery monitors on one 1-Wire bus.

    A full reading takes two voltage conversions: the A/D input measures either
    VDD or VAD, selected by a configuration bit. The caller sequences them and
    waits CONVERSION_TIME after each start with the bus released:

    1. start(vdd=True, temperature=True), wait, read_page() -> VDD and temperature
    2. start(vdd=False), wait, read_page() -> VAD and the current sense voltage
    """

    # DS2438 function commands
    CMD_CONVERT_VOLTAGE = 0xB4  # Initiate voltage conversion
    CMD_CONVERT_TEMP = 0x44  # Initiate temperature conversion
    CMD_RECALL_MEMORY = 0xB8  # Recall values from EEPROM
    CMD_READ_SCRATCHPAD = 0xBE  # Read scratchpad
    CMD_WRITE_SCRATCHPAD = 0x4E  # Write scratchpad

    def __init__(self, bus_interface) -> None:
        """Initialize for one bus."""
        self.bus = bus_interface

    def start(self, device_id: str, *, vdd: bool, temperature: bool = False) -> bool:
        """Select the voltage input and start the conversions."""

        # Configuration bit 3 (AD) selects VDD instead of VAD.
        if not self._command(device_id, [self.CMD_WRITE_SCRATCHPAD, 0x00, 0x08 if vdd else 0x00]):
            return False
        if not self._command(device_id, [self.CMD_CONVERT_VOLTAGE]):
            return False
        return not temperature or self._command(device_id, [self.CMD_CONVERT_TEMP])

    def read_page(self, device_id: str) -> DS2438Page | None:
        """Recall page 0 into the scratchpad and read it."""

        if not self._command(device_id, [self.CMD_RECALL_MEMORY, 0x00]):
            return None
        if not self._command(device_id, [self.CMD_READ_SCRATCHPAD, 0x00]):
            return None
        scratchpad = self.bus.bridge.wire_read_bytes(9)
        if scratchpad is None:
            return None
        if self.bus.calc_crc8(bytes(scratchpad[:-1])) != scratchpad[-1]:
            _LOGGER.debug("DS2438 %s scratchpad CRC mismatch", device_id)
            return None

        return DS2438Page(
            status=scratchpad[0],
            # Temperature and current are signed; one LSB is 1/256 °C and 0.2441 mV.
            temperature=_int16(scratchpad[2] << 8 | scratchpad[1]) / 256.0,
            voltage=(scratchpad[4] << 8 | scratchpad[3]) / 100.0,
            current_voltage=_int16(scratchpad[6] << 8 | scratchpad[5]) * 0.2441 / 1000.0,
        )

    def _command(self, device_id: str, data: list[int]) -> bool:
        return self.bus.select_device(device_id) and bool(self.bus.bridge.wire_write_bytes(data))


def _int16(word: int) -> int:
    return word - 0x10000 if word & 0x8000 else word
