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

This implementation provides:
- Non-blocking state machine for all measurements
- 60-second result caching to reduce bus traffic
- Automatic multi-parameter reading in single cycle
- Comprehensive error handling and validation
- Debug logging for all operations

References:
- Datasheet: https://www.analog.com/media/en/technical-documentation/data-sheets/DS2438.pdf
"""

from dataclasses import dataclass
from enum import Enum, auto
import logging
import time

_LOGGER = logging.getLogger(__name__)

# Cache timeout in seconds
CACHE_TIMEOUT = 60
CONVERSION_TIME = 0.050  # 50ms conversion time


class ConversionState(Enum):
    """States for conversion state machine."""

    IDLE = auto()
    VDD_CONFIG = auto()
    VDD_CONVERT = auto()
    VDD_READ = auto()
    VAD_CONFIG = auto()
    VAD_CONVERT = auto()
    VAD_READ = auto()
    TEMP_CONVERT = auto()
    TEMP_READ = auto()


@dataclass
class DS2438Reading:
    """Container for DS2438 sensor readings."""

    vdd: float  # Supply voltage
    vad: float  # A/D voltage input
    vse: float  # Current sense voltage
    temperature: float  # Temperature in Celsius
    timestamp: float  # When reading was taken
    cache_timeout: int = CACHE_TIMEOUT

    @property
    def age(self) -> float:
        """Get age of reading in seconds."""
        return time.time() - self.timestamp

    @property
    def is_valid(self) -> bool:
        """Check if reading is still valid."""
        return self.age < self.cache_timeout


@dataclass
class DS2438State:
    """State tracking for DS2438 device."""

    state: ConversionState = ConversionState.IDLE
    last_action: float = 0
    reading: DS2438Reading | None = None
    new_vdd: float | None = None
    new_vad: float | None = None
    new_vse: float | None = None
    new_temp: float | None = None

    def start_conversion(self):
        """Start new conversion cycle."""
        self.state = ConversionState.VDD_CONFIG
        self.last_action = time.time()
        self.new_vdd = None
        self.new_vad = None
        self.new_vse = None
        self.new_temp = None

    @property
    def conversion_ready(self) -> bool:
        """Check if enough time has passed since last action."""
        return time.time() - self.last_action >= CONVERSION_TIME

    def update_timestamp(self):
        """Update last action timestamp."""
        self.last_action = time.time()


class DS2438:
    """DS2438 Smart Battery Monitor implementation with non-blocking state machine.

    The DS2438 can measure:
    - VDD (supply voltage)
    - VAD (general purpose A/D input)
    - VSE (current sense input)
    - Temperature

    This implementation uses a state machine to handle conversions without blocking.
    It will return cached values while new conversions are in progress.
    """

    # DS2438 function commands
    CMD_CONVERT_VOLTAGE = 0xB4  # Initiate voltage conversion
    CMD_CONVERT_TEMP = 0x44  # Initiate temperature conversion
    CMD_RECALL_MEMORY = 0xB8  # Recall values from EEPROM
    CMD_READ_SCRATCHPAD = 0xBE  # Read scratchpad
    CMD_WRITE_SCRATCHPAD = 0x4E  # Write scratchpad

    def __init__(self, bus_interface) -> None:
        """Initialize DS2438 instance.

        Args:
            bus_interface: Interface to 1-Wire bus (must support select_device() and other low-level operations)
        """
        self.bus = bus_interface
        self._device_states: dict[str, DS2438State] = {}  # State tracking by device ID

    def _get_state(self, device_id: str) -> DS2438State:
        """Get or create state tracker for device."""
        if device_id not in self._device_states:
            self._device_states[device_id] = DS2438State()
        return self._device_states[device_id]

    def get_reading(self, device_id: str, custom_cache: int | None = None) -> DS2438Reading | None:
        """Get reading, starting new conversion cycle if needed.

        Args:
            device_id: ROM ID of DS2438 device
            custom_cache: Override default cache timeout

        Returns:
            DS2438Reading if available (might be cached), None on error
        """
        state = self._get_state(device_id)

        # Return existing reading if still valid and not in IDLE state
        if state.reading and state.reading.is_valid and state.state != ConversionState.IDLE:
            return state.reading

        # Process current state
        if not self._process_state(device_id, state):
            return state.reading  # Return last known reading on error

        # If we completed a full cycle, create new reading
        if (
            state.state == ConversionState.IDLE
            and self._all_values_present(state)
            and state.new_vdd is not None
            and state.new_vad is not None
            and state.new_vse is not None
            and state.new_temp is not None
        ):
            state.reading = DS2438Reading(
                vdd=state.new_vdd,
                vad=state.new_vad,
                vse=state.new_vse,
                temperature=state.new_temp,
                timestamp=time.time(),
                cache_timeout=custom_cache or CACHE_TIMEOUT,
            )
            _LOGGER.debug(
                "Completed conversion for %s: VDD=%.3fV VAD=%.3fV VSE=%.3fV Temp=%.1f°C",
                device_id,
                state.new_vdd,
                state.new_vad,
                state.new_vse,
                state.new_temp,
            )

        return state.reading

    def _process_state(self, device_id: str, state: DS2438State) -> bool:
        """Process current state of conversion.

        Returns:
            bool: True if state processed successfully, False on error
        """
        try:
            # Start new conversion cycle if IDLE
            if state.state == ConversionState.IDLE:
                _LOGGER.debug("Starting new conversion cycle for device DS2438 %s", device_id)
                state.start_conversion()
                return True

            # Wait for conversion/action time
            if not state.conversion_ready:
                return True

            # Process each state
            if state.state == ConversionState.VDD_CONFIG:
                if self._write_config(device_id, 0x08):  # Enable VDD measurement
                    state.state = ConversionState.VDD_CONVERT
                    state.update_timestamp()
                    _LOGGER.debug("VDD configuration successful for %s", device_id)
                    return True

            elif state.state == ConversionState.VDD_CONVERT:
                if self._start_voltage_conversion(device_id) and self._start_temp_conversion(device_id):
                    state.state = ConversionState.VDD_READ
                    state.update_timestamp()
                    _LOGGER.debug("VDD conversion started for %s", device_id)
                    return True

            elif state.state == ConversionState.VDD_READ:
                vdd = self._read_voltage(device_id)
                if vdd is not None:
                    state.new_vdd = vdd
                    state.state = ConversionState.VAD_CONFIG
                    state.update_timestamp()
                    _LOGGER.debug(
                        "VDD read successful for %s: VDD=%.3fV",
                        device_id,
                        state.new_vdd,
                    )
                    return True

            elif state.state == ConversionState.VAD_CONFIG:
                if self._write_config(device_id, 0x00):  # Enable VAD measurement
                    state.state = ConversionState.VAD_CONVERT
                    state.update_timestamp()
                    _LOGGER.debug("VAD configuration successful for %s", device_id)
                    return True

            elif state.state == ConversionState.VAD_CONVERT:
                if self._start_voltage_conversion(device_id):
                    state.state = ConversionState.VAD_READ
                    state.update_timestamp()
                    _LOGGER.debug("VAD conversion started for %s", device_id)
                    return True

            elif state.state == ConversionState.VAD_READ:
                scratchpad = self._read_scratchpad(device_id, recall_memory=True)
                if scratchpad:
                    # Read both VAD and VSE from same scratchpad
                    state.new_vad = (scratchpad[4] << 8 | scratchpad[3]) / 100.0  # VAD
                    state.new_vse = (scratchpad[6] << 8 | scratchpad[5]) * 0.2441 / 1000.0  # VSE
                    state.state = ConversionState.TEMP_CONVERT
                    state.update_timestamp()
                    _LOGGER.debug(
                        "VAD/VSE read successful for %s: VAD=%.3fV VSE=%.3fV",
                        device_id,
                        state.new_vad,
                        state.new_vse,
                    )
                    return True

            elif state.state == ConversionState.TEMP_CONVERT:
                if self._start_temp_conversion(device_id):
                    state.state = ConversionState.TEMP_READ
                    state.update_timestamp()
                    _LOGGER.debug("Temperature conversion started for %s", device_id)
                    return True

            elif state.state == ConversionState.TEMP_READ:
                scratchpad = self._read_scratchpad(device_id)
                if scratchpad:
                    temp = (scratchpad[2] << 8 | scratchpad[1]) / 256.0
                    if temp <= 85:  # Valid reading
                        state.new_temp = temp
                        state.state = ConversionState.IDLE
                        state.update_timestamp()
                        _LOGGER.debug(
                            "Temperature read successful for %s: %.1f°C",
                            device_id,
                            temp,
                        )
                        return True

            _LOGGER.error("Failed to process state %s for device %s", state.state, device_id)
            state.state = ConversionState.IDLE  # Reset on error

        except Exception:
            _LOGGER.exception("Error processing state %s for device %s", state.state, device_id)
            state.state = ConversionState.IDLE
            return False
        return False

    def _all_values_present(self, state: DS2438State) -> bool:
        """Check if all new values are present."""
        return all(x is not None for x in [state.new_vdd, state.new_vad, state.new_vse, state.new_temp])

    def _write_config(self, device_id: str, config: int) -> bool:
        """Write configuration byte."""
        if not self.bus.select_device(device_id):
            return False

        self.bus.bridge.wire_write_byte(self.CMD_WRITE_SCRATCHPAD)
        self.bus.bridge.wire_write_byte(0x00)  # Page 0
        self.bus.bridge.wire_write_byte(config)

        return True

    def _start_voltage_conversion(self, device_id: str) -> bool:
        """Start voltage conversion."""
        if not self.bus.select_device(device_id):
            return False

        return self.bus.bridge.wire_write_byte(self.CMD_CONVERT_VOLTAGE)

    def _start_temp_conversion(self, device_id: str) -> bool:
        """Start temperature conversion."""
        if not self.bus.select_device(device_id):
            return False

        return self.bus.bridge.wire_write_byte(self.CMD_CONVERT_TEMP)

    def _read_scratchpad(self, device_id: str, recall_memory: bool = False) -> bytes | None:
        """Read 9 bytes of scratchpad memory."""
        if recall_memory:
            if not self._recall_memory(device_id):
                _LOGGER.error("Failed to recall memory for device %s", device_id)
                return None

        if not self.bus.select_device(device_id):
            return None

        self.bus.bridge.wire_write_byte(self.CMD_READ_SCRATCHPAD)
        self.bus.bridge.wire_write_byte(0x00)  # Page 0

        scratchpad = []
        for _ in range(9):
            byte = self.bus.bridge.wire_read_byte()
            if byte is None:
                return None
            scratchpad.append(byte)

        _LOGGER.debug("%s scratchpad: %s", device_id, " ".join(f"{x:02X}" for x in scratchpad))

        # Verify CRC
        if not self.bus.verify_crc8(bytes(scratchpad[:-1]), scratchpad[-1]):
            _LOGGER.error("CRC check failed for device %s", device_id)
            return None

        return bytes(scratchpad)

    def _read_voltage(self, device_id: str) -> float | None:
        """Read voltage from scratchpad."""
        scratchpad = self._read_scratchpad(device_id, recall_memory=True)
        if not scratchpad:
            return None

        # Status byte controls whether we read VDD or VAD
        # Bit 3 enables VDD measurement
        status = scratchpad[0]
        if not (status & 0x08):
            _LOGGER.error("VDD measurement not enabled")
            return None

        return ((scratchpad[4] << 8) | scratchpad[3]) / 100.0

    def _recall_memory(self, device_id: str) -> bool:
        """Recall memory from EEPROM."""
        if not self.bus.select_device(device_id):
            return False

        return self.bus.bridge.wire_write_byte(self.CMD_RECALL_MEMORY) and self.bus.bridge.wire_write_byte(0x00)
