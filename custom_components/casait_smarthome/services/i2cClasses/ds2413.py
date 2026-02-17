"""DS2413 Dual Channel Addressable Switch.

Two-channel programmable I/O port with open-drain outputs and input-sensing
capability. Each channel can be independently configured and accessed through
a 1-Wire interface.

Key Features:
- Two independently controlled I/O pins
- Open-drain output drivers (external pull-up required)
- Input voltage sensing capability
- Strong pull-down (4mA @ 0.4V)
- Verification of state changes
- Unique 64-bit serial number

This implementation provides:
- Non-blocking state machine for reads
- 25-second result caching
- Dual port state tracking
- Write verification
- Input validation using complement bits
- Automatic retry on verification failures

Each I/O pin features:
- Output: Strong pull-down / floating
- Input: Voltage sense capability
- Activity indicator
- State verification

References:
- Datasheet: https://datasheets.maximintegrated.com/en/ds/DS2413.pdf
"""

from dataclasses import dataclass
from enum import Enum, auto
import logging
import time

_LOGGER = logging.getLogger(__name__)

CACHE_TIMEOUT = 25
CONVERSION_TIME = 0.005  # 5ms between reads


class ConversionState(Enum):
    """State machine for DS2413 read/write process."""

    IDLE = auto()
    READING = auto()


@dataclass
class BinaryReading:
    """Data class for storing binary reading and timestamp."""

    port_a: bool
    port_b: bool
    timestamp: float
    cache_time: float = CACHE_TIMEOUT

    @property
    def age(self) -> float:
        """Return the age of the reading in seconds."""
        return time.time() - self.timestamp

    @property
    def is_valid(self) -> bool:
        """Check if the reading is still valid based on cache time."""
        return self.age < self.cache_time


@dataclass
class SensorState:
    """Data class for tracking sensor state and timing."""

    state: ConversionState = ConversionState.IDLE
    last_action: float = 0
    reading: BinaryReading | None = None

    @property
    def read_ready(self) -> bool:
        """Check if enough time has passed for read to be ready."""
        return time.time() - self.last_action >= CONVERSION_TIME

    def update_timestamp(self):
        """Update the timestamp for the last action."""
        self.last_action = time.time()


class DS2413:
    """DS2413 Dual Channel Addressable Switch with non-blocking state machine."""

    CMD_PIO_ACCESS_READ = 0xF5
    CMD_PIO_ACCESS_WRITE = 0x5A
    CMD_PIO_WRITE_VALIDATE = 0xA5

    def __init__(self, bus_interface) -> None:
        """Initialize DS2413 instance."""
        self.bus = bus_interface
        self._sensor_states: dict[str, SensorState] = {}

    def _get_state(self, device_id: str) -> SensorState:
        if device_id not in self._sensor_states:
            self._sensor_states[device_id] = SensorState()
        return self._sensor_states[device_id]

    def get_state(self, device_id: str, channel: int = 0, custom_cache: int | None = None) -> bool | None:
        """Get binary state for specified channel."""
        state = self._get_state(device_id)

        if state.reading and state.reading.is_valid and state.state != ConversionState.IDLE:
            return state.reading.port_a if channel == 0 else state.reading.port_b

        if not self._process_state(device_id, state, custom_cache):
            if not state.reading:
                return None
            return state.reading.port_a if channel == 0 else state.reading.port_b

        if not state.reading:
            return None

        return state.reading.port_a if channel == 0 else state.reading.port_b

    def _process_state(self, device_id: str, state: SensorState, custom_cache: int | None = None) -> bool:
        try:
            if state.state == ConversionState.IDLE:
                _LOGGER.debug("Starting read for DS2413 %s", device_id)
                state.state = ConversionState.READING
                state.update_timestamp()
                return True

            if not state.read_ready:
                _LOGGER.debug(
                    "Waiting for read delay, elapsed: %.1fms",
                    (time.time() - state.last_action) * 1000,
                )
                return True

            if state.state == ConversionState.READING:
                states = self._read_ports(device_id)
                if states is not None:
                    port_a, port_b = states
                    state.reading = BinaryReading(
                        port_a=port_a,
                        port_b=port_b,
                        timestamp=time.time(),
                        cache_time=custom_cache or CACHE_TIMEOUT,
                    )
                    state.state = ConversionState.IDLE
                    _LOGGER.debug("State read successful: A=%s, B=%s", port_a, port_b)
                    return True

        except Exception:
            _LOGGER.exception("Error processing state %s for %s", state.state, device_id)
            state.state = ConversionState.IDLE
            return False
        return False

    def _read_ports(self, device_id: str) -> tuple[bool, bool] | None:
        """Read both ports."""
        if not self.bus.select_device(device_id):
            return None

        # Send read command
        self.bus.bridge.wire_write_byte(self.CMD_PIO_ACCESS_READ)

        # Read state byte
        state = self.bus.bridge.wire_read_byte()
        if state is None:
            return None

        # Validate complement bits
        if (state >> 4) != (~state & 0x0F):
            _LOGGER.error("Invalid state read: %02X", state)
            return None

        # Extract individual port states
        return bool(state & 0x01), bool(state & 0x04)

    def set_state(self, device_id: str, channel: int, value: bool) -> bool:
        """Set binary state for specified channel."""
        _LOGGER.debug("Setting state for device %s channel %s to %s", device_id, channel, value)

        if not self.bus.select_device(device_id):
            return False

        # First read current state
        states = self._read_ports(device_id)
        if states is None:
            return False

        # Prepare new state byte
        current_a, current_b = states
        new_a = value if channel == 0 else current_a
        new_b = value if channel == 1 else current_b

        state_byte = 0
        if not new_a:  # Port A output
            state_byte |= 0x01
        if not new_b:  # Port B output
            state_byte |= 0x04

        expected_state = (bool(state_byte & 0x01), bool(state_byte & 0x04))

        for attempt in range(2):
            if attempt and not self.bus.select_device(device_id):
                return False

            try:
                # Write command sequence
                self.bus.bridge.wire_write_byte(self.CMD_PIO_ACCESS_WRITE)
                self.bus.bridge.wire_write_byte(state_byte)
                self.bus.bridge.wire_write_byte(~state_byte & 0xFF)  # Complement

                confirm = self.bus.bridge.wire_read_byte()
                if confirm != 0xAA:
                    _LOGGER.warning(
                        "Write not confirmed: %02X (attempt %s)",
                        confirm,
                        attempt + 1,
                    )
                    verified = self._read_ports(device_id)
                    if verified == expected_state:
                        self._cache_state(device_id, expected_state)
                        return True
                    continue

                self._cache_state(device_id, expected_state)

            except Exception:
                _LOGGER.exception("Error setting state")
                return False

        return True

    def _cache_state(self, device_id: str, states: tuple[bool, bool], cache_time: int | None = None) -> None:
        """Update cached reading after a write or verification."""

        state = self._get_state(device_id)
        state.reading = BinaryReading(
            port_a=states[0],
            port_b=states[1],
            timestamp=time.time(),
            cache_time=cache_time or CACHE_TIMEOUT,
        )
        state.state = ConversionState.IDLE
        state.update_timestamp()
