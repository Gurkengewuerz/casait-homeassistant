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

This implementation provides:
- Non-blocking state machine for conversions
- 60-second result caching
- Resolution detection and handling
- Parasitic power compatibility
- Comprehensive error checks

Timing characteristics:
- 9-bit resolution: 93.75ms
- 10-bit resolution: 187.5ms
- 11-bit resolution: 375ms
- 12-bit resolution: 750ms

References:
- Datasheet: https://datasheets.maximintegrated.com/en/ds/DS18B20.pdf
"""

from dataclasses import dataclass
from enum import Enum, auto
import logging
import time

_LOGGER = logging.getLogger(__name__)

CACHE_TIMEOUT = 60
CONVERSION_TIME = 0.750  # 750ms for 12-bit conversion


class ConversionState(Enum):
    """State machine for DS18B20 conversion process."""

    IDLE = auto()
    CONVERTING = auto()
    READING = auto()


@dataclass
class TemperatureReading:
    """Data class for storing temperature reading and timestamp."""

    temperature: float
    timestamp: float
    cache_time: float = CACHE_TIMEOUT

    @property
    def age(self) -> float:
        """Calculate age of the reading in seconds."""
        return time.time() - self.timestamp

    @property
    def is_valid(self) -> bool:
        """Check if the reading is still valid based on cache time."""
        return self.age < self.cache_time


@dataclass
class SensorState:
    """Class to track the state of each DS18B20 sensor."""

    state: ConversionState = ConversionState.IDLE
    last_action: float = 0
    reading: TemperatureReading | None = None

    @property
    def conversion_ready(self) -> bool:
        """Check if enough time has passed for conversion to be ready."""
        return time.time() - self.last_action >= CONVERSION_TIME

    def update_timestamp(self):
        """Update the timestamp for the last action."""
        self.last_action = time.time()


class DS18B20:
    """DS18B20 Temperature Sensor with non-blocking state machine."""

    CMD_CONVERT_T = 0x44
    CMD_READ_SCRATCHPAD = 0xBE

    def __init__(self, bus_interface) -> None:
        """Initialize DS18B20 instance."""
        self.bus = bus_interface
        self._sensor_states: dict[str, SensorState] = {}

    def _get_state(self, device_id: str) -> SensorState:
        if device_id not in self._sensor_states:
            self._sensor_states[device_id] = SensorState()
        return self._sensor_states[device_id]

    def get_temperature(self, device_id: str, custom_cache: int | None = None) -> float | None:
        """Get temperature reading, starting new conversion if needed."""
        state = self._get_state(device_id)

        if state.reading and state.reading.is_valid and state.state != ConversionState.IDLE:
            return state.reading.temperature

        if not self._process_state(device_id, state, custom_cache):
            return state.reading.temperature if state.reading else None

        return state.reading.temperature if state.reading else None

    def _process_state(self, device_id: str, state: SensorState, custom_cache: int | None = None) -> bool:
        try:
            if state.state == ConversionState.IDLE:
                _LOGGER.debug("Starting conversion for DS18B20 %s", device_id)
                if not self._start_conversion(device_id):
                    return False
                state.state = ConversionState.CONVERTING
                state.update_timestamp()
                return True

            if not state.conversion_ready:
                _LOGGER.debug(
                    "Waiting for conversion, elapsed: %.1fms",
                    (time.time() - state.last_action) * 1000,
                )
                return True

            if state.state == ConversionState.CONVERTING:
                _LOGGER.debug("Conversion complete, reading scratchpad")
                state.state = ConversionState.READING
                return True

            if state.state == ConversionState.READING:
                temperature = self._read_temperature(device_id)
                if temperature is not None:
                    state.reading = TemperatureReading(
                        temperature=temperature,
                        timestamp=time.time(),
                        cache_time=custom_cache or CACHE_TIMEOUT,
                    )
                    state.state = ConversionState.IDLE
                    _LOGGER.debug("Temperature read successful: %.1f°C", temperature)
                    return True
        except Exception:
            _LOGGER.exception("Error processing state %s for %s", state.state, device_id)
            state.state = ConversionState.IDLE
            return False
        return False

    def _start_conversion(self, device_id: str) -> bool:
        """Start temperature conversion. Returns True if command was sent successfully."""
        if not self.bus.select_device(device_id):
            return False

        return self.bus.bridge.wire_write_byte(self.CMD_CONVERT_T)

    def _read_temperature(self, device_id: str) -> float | None:
        """Read temperature from scratchpad. Returns temperature in °C or None on error."""
        if not self.bus.select_device(device_id):
            return None

        self.bus.bridge.wire_write_byte(self.CMD_READ_SCRATCHPAD)
        scratchpad = []
        for _ in range(9):
            byte = self.bus.bridge.wire_read_byte()
            if byte is None:
                return None
            scratchpad.append(byte)

        if not self.bus.verify_crc8(bytes(scratchpad[:-1]), scratchpad[-1]):
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
