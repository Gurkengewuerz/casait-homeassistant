"""1-Wire bus implementation using DS2482 bridge."""

from __future__ import annotations

import enum
import logging
import time
from typing import Any

from .ds18b20 import DS18B20
from .ds28e17 import DS28E17
from .ds2413 import DS2413
from .ds2438 import DS2438
from .ds2482 import DS2482
from .led_controller import LEDConfig, LEDController
from .multisensor import Multisensor

_LOGGER = logging.getLogger(__name__)

MAX_FAILURES = 3
TIMEOUT_DURATION = 300  # 5 minutes
ROM_BITS = 64
# One pass yields one device. The cap only exists so a bus that keeps reporting
# discrepancies cannot spin here while holding the hardware lock.
MAX_SEARCH_PASSES = 64


class OneWireType(enum.Enum):
    """Enumeration of supported 1-Wire device types."""

    DS18XB20 = "DS18XB20"  # temperature sensor
    DS2438 = "DS2438"  # a/d-c sensor
    DS2413 = "DS2413"  # 1-wire dual channel addressable switch
    DS28E17 = "DS28E17"  # 1-wire to I2C bridge


class OneWireBus:
    """1-Wire bus implementation using DS2482-100."""

    # ROM commands
    CMD_SEARCH_ROM = 0xF0
    CMD_MATCH_ROM = 0x55

    # DS28E17 Commands
    CMD_WRITE_DATA_STOP = 0x4B  # Write data with stop
    CMD_READ_DATA_STOP = 0x87  # Read data with stop

    def __init__(self, bus, bridge_address: int) -> None:
        """Initialize 1-Wire bus with DS2482 bridge."""
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
        self.ds28e17 = DS28E17(self)
        self.multisensor = Multisensor(self.ds28e17)
        self.last_scan_time = 0
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
        """Scan the 1-Wire bus for devices using the DS2482 search triplet.

        The bridge resolves each bit position in hardware, and the direction a pass
        takes at every position follows from the previous pass alone. A whole
        64-bit pass is therefore computed up front and issued as a handful of
        batches, rather than three network round trips per bit.

        A partial scan leaves the cached device list untouched, so a transient bus
        fault does not make known devices disappear.
        """
        _LOGGER.info("Scanning 1-Wire bus %02x for devices", self.bridge.address)
        devices: dict[str, dict[str, Any]] = {}
        rom_no = bytearray(8)  # 64-bit ROM code
        last_discrepancy = 0

        for _ in range(MAX_SEARCH_PASSES):
            if not self.bridge.wire_reset():
                return devices
            if not self.bridge.wire_write_byte(self.CMD_SEARCH_ROM):
                return devices

            statuses = self.bridge.wire_triplets(self._search_directions(rom_no, last_discrepancy))
            if statuses is None or len(statuses) != ROM_BITS:
                return devices

            outcome = self._apply_search_pass(statuses)
            if outcome is None:
                # Both read bits high at some position: nothing answered
                return devices
            rom_no, last_zero = outcome

            if self.calc_crc8(bytes(rom_no[:-1])) == rom_no[7]:
                device_id = "".join(f"{x:02x}" for x in rom_no)
                family_code = rom_no[0]
                devices[device_id] = {
                    "family_code": family_code,
                    "device_type": self._get_device_type(family_code),
                    "rom": list(rom_no),
                }
            else:
                _LOGGER.warning("Discarding 1-Wire ROM code with a bad CRC on bus %02x", self.bridge.address)

            last_discrepancy = last_zero
            if last_discrepancy == 0:
                break
        else:
            _LOGGER.warning(
                "1-Wire search on bus %02x did not terminate within %d passes",
                self.bridge.address,
                MAX_SEARCH_PASSES,
            )
            return devices

        self.devices = devices
        _LOGGER.info("1-Wire bus scan found %d devices", len(devices))
        return devices

    @staticmethod
    def _search_directions(rom_no: bytes | bytearray, last_discrepancy: int) -> list[bool]:
        """Decide the branch to take at every bit position of the next pass.

        Positions below the last discrepancy repeat the previous pass, and the rest
        follow from the loop index, so no result of the pass itself is needed. That
        is what allows the pass to be batched.
        """
        directions = []
        for bit_number in range(1, ROM_BITS + 1):
            if bit_number < last_discrepancy:
                index = bit_number - 1
                directions.append(bool(rom_no[index // 8] >> (index % 8) & 0x01))
            else:
                directions.append(bit_number == last_discrepancy)
        return directions

    @staticmethod
    def _apply_search_pass(statuses: list[int]) -> tuple[bytearray, int] | None:
        """Build the ROM code a pass discovered, plus where it last branched low.

        Returns None when a position reports both bits high, which means no device
        drove the bus.
        """
        rom_no = bytearray(8)
        last_zero = 0

        for index, status in enumerate(statuses):
            id_bit = bool(status & DS2482.STATUS_SBR)
            cmp_id_bit = bool(status & DS2482.STATUS_TSB)
            direction = bool(status & DS2482.STATUS_DIR)

            if id_bit and cmp_id_bit:
                return None

            if not id_bit and not cmp_id_bit and not direction:
                last_zero = index + 1

            if direction:
                rom_no[index // 8] |= 1 << (index % 8)

        return rom_no, last_zero

    def _get_device_type(self, family_code: int) -> str:
        """Map family code to device type string."""
        family_types = {
            0x19: OneWireType.DS28E17.value,
            0x28: OneWireType.DS18XB20.value,
            0x26: OneWireType.DS2438.value,
            0x3A: OneWireType.DS2413.value,
        }
        return family_types.get(family_code, "Unknown")

    def calc_crc8(self, data: bytes) -> int:
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

        # Command plus the eight ROM bytes go out as one batch, so selecting a
        # device costs one round trip instead of nine.
        if not self.bridge.wire_write_bytes([self.CMD_MATCH_ROM, *self.devices[device_id]["rom"]]):
            _LOGGER.error("Failed to address device %s", device_id)
            self._increment_failures(device_id)
            return False
        return True

    def _increment_failures(self, device_id: str) -> None:
        _, count = self._timeout_cache.get(device_id, (time.time(), 0))
        self._timeout_cache[device_id] = (time.time(), count + 1)

    def write_led_config(self, device_id: str, config: LEDConfig) -> bool:
        """Write LED configuration to device."""
        return self.led_controller.write_config(device_id, config)

    def read_led_config(self, device_id: str, use_cache: bool = True) -> LEDConfig | None:
        """Read LED configuration from device."""
        return self.led_controller.read_config(device_id, use_cache=use_cache)
