"""DS2482-100 I2C to 1-Wire bridge implementation."""

import logging
import time
import traceback

_LOGGER = logging.getLogger(__name__)


class DS2482:
    """DS2482-100 I2C to 1-Wire bridge implementation."""

    # Status register bit definitions
    STATUS_1WB = 0x01  # 1-Wire Busy
    STATUS_PPD = 0x02  # Presence Pulse Detect
    STATUS_SD = 0x04  # Short Detected
    STATUS_LL = 0x08  # Logic Level
    STATUS_RST = 0x10  # Device Reset
    STATUS_SBR = 0x20  # Single Bit Result
    STATUS_TSB = 0x40  # Triplet Second Bit
    STATUS_DIR = 0x80  # Direction

    # DS2482 commands and registers
    CMD_RESET = 0xF0
    CMD_SET_READ_PTR = 0xE1
    CMD_WRITE_CONFIG = 0xD2
    CMD_1WIRE_RESET = 0xB4
    CMD_1WIRE_WRITE_BYTE = 0xA5
    CMD_1WIRE_READ_BYTE = 0x96
    CMD_1WIRE_SINGLE_BIT = 0x87
    CMD_1WIRE_TRIPLET = 0x78

    # DS2482 registers
    REG_STATUS = 0xF0
    REG_DATA = 0xE1
    REG_CONFIG = 0xC3

    def __init__(self, bus, address: int) -> None:
        """Initialize DS2482 device."""
        self.bus = bus
        self.address = address
        self._last_status = 0
        self.reset()

    def reset(self) -> bool:
        """Reset the DS2482 device."""
        try:
            # Device reset
            self.bus.write_byte(self.address, self.CMD_RESET)
            time.sleep(0.001)

            # Verify reset completed
            status = self.bus.read_byte(self.address)
            if not (status & self.STATUS_RST):
                return False

            # Configure with standard settings
            config = 0xF0  # Active pullup, strong pullup disabled
            self.bus.write_byte_data(self.address, self.CMD_WRITE_CONFIG, config)
            time.sleep(0.001)

            # Verify configuration
            self.bus.write_byte_data(self.address, self.CMD_SET_READ_PTR, self.REG_CONFIG)
            read_config = self.bus.read_byte(self.address)
            return (read_config & 0x0F) == (config & 0x0F)

        except OSError as e:
            _LOGGER.error("DS2482 reset error: %s", e)
            _LOGGER.error(traceback.format_exc())
            return False

    def _wait_busy(self, timeout: float = 0.1, retries: int = 3) -> bool:
        """Wait until the 1-Wire bus is not busy."""
        for attempt in range(retries):
            try:
                start_time = time.time()
                while time.time() - start_time < timeout:
                    status = self.bus.read_byte(self.address)
                    if not (status & self.STATUS_1WB):
                        self._last_status = status
                        return True
                    time.sleep(0.001)
            except OSError as e:
                _LOGGER.warning("Retry %s/%s: %s", attempt + 1, retries, e)
                continue
        return False

    def wire_reset(self) -> bool:
        """Reset the 1-Wire bus and check for presence pulse."""
        try:
            self.bus.write_byte(self.address, self.CMD_1WIRE_RESET)
            if not self._wait_busy():
                _LOGGER.error("Timeout waiting for 1-Wire reset")
                return False

            # Check for presence pulse
            status = self._last_status
            if not (status & self.STATUS_PPD):
                _LOGGER.warning("No presence pulse detected on 1-Wire bus")

            return bool(status & self.STATUS_PPD)

        except OSError as e:
            _LOGGER.error("1-Wire reset error: %s", e)
            _LOGGER.error(traceback.format_exc())
            return False

    def wire_write_byte(self, byte: int) -> bool:
        """Write a byte to the 1-Wire bus."""
        try:
            self.bus.write_byte_data(self.address, self.CMD_1WIRE_WRITE_BYTE, byte)
            return self._wait_busy()
        except OSError as e:
            _LOGGER.error("1-Wire write error: %s", e)
            _LOGGER.error(traceback.format_exc())
            return False

    def wire_read_byte(self) -> int | None:
        """Read a byte from the 1-Wire bus."""
        try:
            # Send read command
            self.bus.write_byte(self.address, self.CMD_1WIRE_READ_BYTE)
            if not self._wait_busy():
                return None

            # Set pointer to data register
            self.bus.write_byte_data(self.address, self.CMD_SET_READ_PTR, self.REG_DATA)

            # Read data
            return self.bus.read_byte(self.address)
        except OSError as e:
            _LOGGER.error("1-Wire read error: %s", e)
            _LOGGER.error(traceback.format_exc())
            return None

    def wire_single_bit(self, bit: bool) -> bool | None:
        """Write and read a single bit on the 1-Wire bus."""
        try:
            self.bus.write_byte_data(self.address, self.CMD_1WIRE_SINGLE_BIT, 0x80 if bit else 0x00)
            if not self._wait_busy():
                return None
            return bool(self._last_status & self.STATUS_SBR)
        except OSError as e:
            _LOGGER.error("1-Wire single bit error: %s", e)
            _LOGGER.error(traceback.format_exc())
            return None
