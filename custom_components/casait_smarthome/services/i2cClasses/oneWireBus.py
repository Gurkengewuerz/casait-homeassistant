"""1-Wire bus implementation using DS2482 bridge."""

from __future__ import annotations

import enum
import logging
import time
from typing import Any

from .ds18b20 import DS18B20
from .ds2413 import DS2413
from .ds2438 import DS2438
from .ds2482 import DS2482
from .led_controller import LEDConfig, LEDController

_LOGGER = logging.getLogger(__name__)

MAX_FAILURES = 3
TIMEOUT_DURATION = 300  # 5 minutes


class OneWireType(enum.Enum):
    """Enumeration of supported 1-Wire device types."""

    DS18XB20 = "DS18XB20"  # temperature sensor
    DS2438 = "DS2438"  # a/d-c sensor
    DS2413 = "DS2413"  # 1-wire dual channel addressable switch
    DS28E17 = "DS28E17"  # 1-wire memory


class OneWireBus:
    """1-Wire bus implementation using DS2482-100."""

    # ROM commands
    CMD_SEARCH_ROM = 0xF0
    CMD_MATCH_ROM = 0x55

    # DS28E17 Commands
    CMD_WRITE_DATA_STOP = 0x4B  # Write data with stop
    CMD_READ_DATA_STOP = 0x87  # Read data with stop

    # LED controller registers (matching Arduino code)
    RGB_ADDRESS = 0x42  # I2C address of LED controller
    REG_LED_COUNT = 0
    REG_LED_STATE = 1
    REG_BRIGHTNESS = 2
    REG_ANIM_CODE = 3
    REG_ANIM_SPEED = 4
    REG_COLORS = 5  # Colors start from this address (3 bytes per color)

    def __init__(self, bus, bridge_address: int) -> None:
        """Initialize 1-Wire bus with DS2482 bridge.

        Args:
            bus: I2C bus interface (e.g. smbus2.SMBus instance)
            bridge_address: I2C address of the DS2482 bridge
        """
        _LOGGER.info(
            "Initializing 1-Wire bus with DS2482 at address %02x",
            bridge_address,
        )
        self.bridge = DS2482(bus, bridge_address)
        self.devices: dict[str, dict[str, Any]] = {}
        self.ds2438 = DS2438(self)
        self.ds18b20 = DS18B20(self)
        self.ds2413 = DS2413(self)
        self.led_controller = LEDController(self)
        self.last_scan_time = 0
        self._interval_cache: dict[str, int | None] = {}
        self._timeout_cache: dict[str, tuple[float, int]] = {}
        self._scan_bus()

    def scan_devices(self, force: bool = False) -> dict:
        """Scan 1-Wire bus for devices with optional force refresh."""
        current_time = time.time()

        # Return cached results if less than 60 seconds old and not forced
        if not force and (current_time - self.last_scan_time) < 60:
            return self.devices

        self._scan_bus()
        self.last_scan_time = current_time
        return self.devices

    def _scan_bus(self):
        """Scan 1-Wire bus for devices using proper search algorithm."""
        if not self.bridge.wire_reset():
            return {}

        _LOGGER.info("Scanning 1-Wire bus %02x for devices", self.bridge.address)
        devices = {}
        rom_no = bytearray(8)  # 64-bit ROM code
        last_discrepancy = 0
        last_device_flag = False

        while not last_device_flag:
            # Initialize for search
            self.bridge.wire_reset()
            self.bridge.wire_write_byte(self.CMD_SEARCH_ROM)

            last_zero = 0
            id_bit_number = 1

            # Search all 64 bits of ROM code
            while id_bit_number <= 64:
                # Read two bits and get their complement
                id_bit = self.bridge.wire_single_bit(True)
                cmp_id_bit = self.bridge.wire_single_bit(True)

                if id_bit is None or cmp_id_bit is None:
                    return devices

                # Check for no devices on the bus
                if id_bit and cmp_id_bit:
                    return devices

                # Determine search direction
                if id_bit != cmp_id_bit:
                    search_direction = id_bit  # Bits differ, use actual
                else:
                    # Bits are both 0 or both 1
                    if id_bit_number == last_discrepancy:
                        search_direction = 1
                    elif id_bit_number > last_discrepancy:
                        search_direction = 0
                    else:
                        search_direction = (rom_no[(id_bit_number - 1) // 8] >> ((id_bit_number - 1) % 8)) & 0x01

                    if search_direction == 0:
                        last_zero = id_bit_number

                # Set or clear bit in ROM byte
                byte_index = (id_bit_number - 1) // 8
                bit_mask = 1 << ((id_bit_number - 1) % 8)

                if search_direction:
                    rom_no[byte_index] |= bit_mask
                else:
                    rom_no[byte_index] &= ~bit_mask

                # Write the search direction bit
                self.bridge.wire_single_bit(bool(search_direction))
                id_bit_number += 1

            # Check if valid device found
            if id_bit_number < 65:
                last_device_flag = True
            else:
                # Valid device found, process ROM code
                crc8 = self._calc_crc8(bytes(rom_no[:-1]))
                if crc8 == rom_no[7]:  # CRC check
                    device_id = "".join(f"{x:02x}" for x in rom_no)
                    family_code = rom_no[0]

                    devices[device_id] = {
                        "family_code": family_code,
                        "device_type": self._get_device_type(family_code),
                        "rom": list(rom_no),
                    }

                last_discrepancy = last_zero
                if last_discrepancy == 0:
                    last_device_flag = True

        self.devices = devices
        _LOGGER.info("1-Wire bus scan found %d devices", len(devices))
        return devices

    def _get_device_type(self, family_code: int) -> str:
        """Map family code to device type string."""
        family_types = {
            0x19: OneWireType.DS28E17.value,
            0x28: OneWireType.DS18XB20.value,
            0x26: OneWireType.DS2438.value,
            0x3A: OneWireType.DS2413.value,
        }
        return family_types.get(family_code, "Unknown")

    def _calc_crc8(self, data: bytes) -> int:
        """Calculate CRC8 using polynomial x^8 + x^5 + x^4 + 1."""
        crc = 0
        for byte in data:
            crc ^= byte
            for _ in range(8):
                if crc & 0x01:
                    crc = (crc >> 1) ^ 0x8C
                else:
                    crc >>= 1
        return crc

    def calc_crc16(self, data: bytes) -> int:
        """Calculate CRC16 using polynomial 0xA001 (modbus)."""
        crc = 0
        for byte in data:
            crc ^= byte
            for _ in range(8):
                if crc & 0x01:
                    crc = (crc >> 1) ^ 0xA001
                else:
                    crc >>= 1
        return crc

    def verify_crc8(self, data: bytes, crc: int) -> bool:
        """Verify CRC8 of data."""
        return self._calc_crc8(data) == crc

    def select_device(self, device_id: str, use_lock: bool = True) -> bool:
        """Select a device on the bus."""
        # output as error which device could not be selected
        # if the device couldn't be selected multiple times create a timeout cache for the device
        # and return false if the device is in the cache
        # this should prevent _scan_bus from being called multiple times and block the bus for a long noticeable time
        if device_id not in self.devices:
            _LOGGER.warning("Device %s not found in cache, rescanning bus", device_id)
            self._scan_bus()
            if device_id not in self.devices:
                _LOGGER.error("Device %s not found after bus scan", device_id)
                return False

        # Check if device is in timeout cache
        current_time = time.time()
        if device_id in self._timeout_cache:
            timestamp, failures = self._timeout_cache[device_id]
            if failures >= MAX_FAILURES and current_time - timestamp < TIMEOUT_DURATION:
                _LOGGER.warning("Device %s is in timeout cache", device_id)
                return False
            if current_time - timestamp >= TIMEOUT_DURATION:
                del self._timeout_cache[device_id]

        if not self.bridge.wire_reset():
            _LOGGER.error("Wire reset failed for device %s", device_id)
            self._increment_failures(device_id)
            return False

        self.bridge.wire_write_byte(self.CMD_MATCH_ROM)
        for byte in self.devices[device_id]["rom"]:
            if not self.bridge.wire_write_byte(byte):
                _LOGGER.error("Failed to write ROM byte for device %s", device_id)
                self._increment_failures(device_id)
                return False
        return True

    def _increment_failures(self, device_id: str) -> None:
        _, count = self._timeout_cache.get(device_id, (time.time(), 0))
        self._timeout_cache[device_id] = (time.time(), count + 1)

    def set_intervals(self, device_list: list[Any]) -> None:
        """Set polling intervals for devices."""
        new_cache = {}
        for device in device_list:
            value = device.polling_interval.value if device.polling_interval is not None else None
            if device.onewire_id not in new_cache:
                new_cache[device.onewire_id] = value
                continue
            if value is not None and (new_cache[device.onewire_id] is None or value < new_cache[device.onewire_id]):
                new_cache[device.onewire_id] = value

        self._interval_cache = new_cache

    def get_interval(self, device_id: str) -> int | None:
        """Get polling interval for device."""
        return self._interval_cache.get(device_id, None)

    def read_temperature(self, device_id: str) -> float | None:
        """Read temperature from DS18B20 sensor."""
        try:
            return self.ds18b20.get_temperature(device_id, self.get_interval(device_id))
        except Exception:
            _LOGGER.exception("Error reading temperature")
            return None

    def read_voltage(self, device_id: str, port: int = 0) -> dict | None:
        """Read voltage and temperature from DS2438.

        Args:
            device_id: ROM ID of the DS2438
            port: Port number (0 for VAD, 1 for VSE)

        Returns:
            Dictionary containing temperature and voltage readings or None on failure
        """
        try:
            # Get reading from DS2438 manager
            reading = self.ds2438.get_reading(device_id, self.get_interval(device_id))
            if not reading:
                return None

            # Select appropriate voltage based on port
            voltage = reading.vad if port == 0 else reading.vse

        except Exception:
            _LOGGER.exception("Error reading DS2438 %s", device_id)
            return None

        return {
            "voltage": voltage,
            "temperature": reading.temperature,
            "vdd": reading.vdd,
        }

    def read_binary_state(self, device_id: str, channel: int = 0, *, invert: bool = True) -> bool | None:
        """Read binary state from DS2413."""
        try:
            state = self.ds2413.get_state(device_id, channel, self.get_interval(device_id))
            if state is None:
                return None
        except Exception:
            _LOGGER.exception("Error reading binary state")
            return None
        if invert:
            return not state
        return state

    def write_led_config(self, device_id: str, config: LEDConfig) -> bool:
        """Write LED configuration to device.

        Args:
            device_id: ROM ID of the DS28E17 device
            config: LED configuration to write

        Returns:
            bool: True if write successful
        """
        return self.led_controller.write_config(device_id, config, custom_cache=self.get_interval(device_id))

    def read_led_config(self, device_id: str, use_cache: bool = True) -> LEDConfig | None:
        """Read LED configuration from device.

        Args:
            device_id: ROM ID of the DS28E17 device
            use_cache: Whether to use cached config if available

        Returns:
            LEDConfig if successful, None on error
        """
        return self.led_controller.read_config(
            device_id, use_cache=use_cache, custom_cache=self.get_interval(device_id)
        )
