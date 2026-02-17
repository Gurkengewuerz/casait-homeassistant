"""PCF8574 I2C I/O expander implementation for CasaIT Smart Home integration."""

import logging
import time
import traceback

logger = logging.getLogger(__name__)

MIN_INIT = 5


class PCF8574:
    """PCF8574 I2C I/O expander implementation."""

    def __init__(self, bus, address: int, debounce_time: int = 40) -> None:
        """Initialize PCF8574 instance."""
        self.bus = bus
        self.address = address
        self.last_value = -1
        self.debounce_time = debounce_time  # ms
        self.port_states = [0] * 8
        self._new_value = 0
        self._new_value_time = 0
        self._init_counter = 0

    def read_ports(self, set_high: bool = True) -> tuple[list[int], int]:
        """Read all ports with debouncing."""
        try:
            # Locking this early to prevent multiple writes from different threads at the same time with different values
            # Set all ports high first
            if set_high:
                self.bus.write_byte(self.address, 0xFF)
                time.sleep(0.005)  # 5ms delay for I2C bus to settle

            # Read current value
            value = self.bus.read_byte(self.address)
            curr_time = time.time() * 1000

            port_values = [(value & (1 << i)) >> i for i in range(8)]
            if self.debounce_time > 0:
                # Debounce logic
                if value != self.last_value:
                    if value != self._new_value:
                        # First detection of new value
                        self._new_value = value
                        self._new_value_time = curr_time
                        value = self.last_value
                    # Check if debounce time passed
                    elif curr_time - self._new_value_time >= self.debounce_time:
                        # Update port states
                        self.port_states = port_values
                        self.last_value = value
            else:
                # No debounce
                self.port_states = port_values
                self.last_value = value

            if self._init_counter < MIN_INIT:
                self._init_counter += 1
        except OSError as ex:
            logger.error("PCF8574 read error: %s", ex)
            logger.error(traceback.format_exc())
            return self.port_states, self.last_value
        return self.port_states, value

    def write_port(self, port: int, state: int, verify: bool = True) -> bool:
        """Write to specific port with optional verification."""
        if not 0 <= port <= 7:
            raise ValueError("Port must be 0-7")

        try:
            # Ensure we have a valid last_value before doing bit operations.
            # If last_value is invalid (-1 or out of range), read current state first.
            if not 0 <= self.last_value <= 255:
                logger.debug(
                    "PCF8574 0x%02X: last_value invalid (%s), reading current state",
                    self.address,
                    self.last_value,
                )
                self.bus.write_byte(self.address, 0xFF)
                time.sleep(0.002)
                self.last_value = self.bus.read_byte(self.address)
                logger.debug(
                    "PCF8574 0x%02X: read current state = 0x%02X",
                    self.address,
                    self.last_value,
                )

            # Calculate new value with explicit masking to ensure valid byte
            current = self.last_value & 0xFF
            if state:
                new_value = current | (1 << port)
            else:
                new_value = current & ~(1 << port)
            new_value &= 0xFF  # Ensure valid byte range

            # Write the new value
            self.bus.write_byte(self.address, new_value)

            # When turning ON (state=0, active low), relay energizes causing
            # electrical noise. Give more settling time before verification.
            if verify:
                settle_time = 0.05 if state == 0 else 0.02
                time.sleep(settle_time)
                read_value = self.bus.read_byte(self.address)
                if read_value != new_value:
                    logger.warning(
                        "PCF8574 write verification failed: expected 0x%02X, got 0x%02X",
                        new_value,
                        read_value,
                    )
                    return False

            self.last_value = new_value
            self.port_states[port] = state

        except OSError as ex:
            logger.error("PCF8574 write error: %s", ex)
            logger.error(traceback.format_exc())
            return False
        return True

    @property
    def is_initialized(self) -> bool:
        """Check if the device has been initialized with enough reads."""
        return self._init_counter >= MIN_INIT
