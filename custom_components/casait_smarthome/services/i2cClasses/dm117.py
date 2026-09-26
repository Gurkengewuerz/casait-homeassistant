"""DM117 I2C module implementation supporting Input, Output and Dimmer ports."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import enum
import logging
import time

from crccheck.crc import Crc8Smbus

_LOGGER = logging.getLogger(__name__)


class DeviceType(enum.Enum):
    """Device types supported by DM117."""

    INPUT = "input"
    OUTPUT = "output"
    DIMMER = "dimmer"


class DimmerSpeed(enum.IntEnum):
    """Speed settings for dimmer transitions."""

    INSTANT = 0
    SLOW = 1
    FAST = 2
    DEFAULT = FAST

    @classmethod
    def _missing_(cls, value: object) -> DimmerSpeed:
        """Return the default speed when an unknown value is provided."""

        return cls.DEFAULT


class DM117:
    """DM117 I2C module implementation supporting Input, Output and Dimmer ports."""

    # Command set
    CMD_CONFIG = 0x01
    CMD_COMMIT = 0x10
    CMD_WRITE = 0x02
    CMD_READ = 0x03

    # Module count byte, then up to 8 slots of one type byte plus one value byte
    # (two for a dimmer), then the CRC. The slave caps its own buffer at 32.
    READ_RESPONSE_SIZE = 26

    def __init__(self, bus, address: int) -> None:
        """Initialize DM117 device."""
        self.bus = bus
        self.address = address
        self.port_config = {}  # Stores port type configuration
        self.port_states = [0] * 8  # Current port states
        self.last_values = {}  # Cache for dimmer values
        self.last_port_types: dict[int, DeviceType] = {}
        self._last_read_time = 0
        self._read_interval = 0.01  # 10ms minimum between reads
        self._force_full_read = True

    async def configure_ports(self, config: dict[int, DeviceType], commit: bool = True) -> bool:
        """Configure module ports."""
        if not config:
            _LOGGER.warning("No ports configured")
            return False

        if len(config) > 8:
            _LOGGER.error("Too many ports configured: %s", len(config))
            return False

        try:
            # Prepare configuration data
            data = bytearray([self.CMD_CONFIG, len(config)])

            # Add port configurations
            for device_type in (config[index] for index in sorted(config)):
                if device_type == DeviceType.INPUT:
                    data.append(0)
                elif device_type == DeviceType.OUTPUT:
                    data.append(2)
                elif device_type == DeviceType.DIMMER:
                    data.append(1)
                else:
                    _LOGGER.error("Invalid port type: %s", device_type)
                    return False

            # Add CRC8
            data.append(Crc8Smbus.calc(data))

            # Send configuration
            await self.bus.write_i2c_block_data(self.address, data[0], data[1:])

            _LOGGER.debug(
                "Configured DM117 at address %02X with %s ports %s",
                self.address,
                len(config),
                " ".join(f"{value:02X}" for value in data),
            )

            # Store configuration
            self.port_config = dict(config)

            if commit:
                return await self.commit_config()

        except OSError:
            _LOGGER.exception("Error configuring DM117")
            return False
        return True

    async def commit_config(self) -> bool:
        """Commit the current configuration to the device."""
        try:
            data = bytearray([self.CMD_COMMIT])
            data.append(Crc8Smbus.calc(data))
            await self.bus.write_i2c_block_data(self.address, data[0], data[1:])
            _LOGGER.debug("Committed DM117 configuration at address %02X", self.address)
        except OSError:
            _LOGGER.exception("Error committing DM117 configuration")
            return False
        return True

    async def write_port(self, config: DM117PortConfig) -> bool:
        """Write value to port."""
        try:
            port = config.port
            if port not in self.port_config:
                _LOGGER.error("Port %s not configured", port)
                return False

            # Locking must be handled by the caller. This method only prepares
            # and sends the payload.
            value = 0
            speed = DimmerSpeed.DEFAULT.value
            if config.device_type == DeviceType.DIMMER and config.dimmer:
                value = config.dimmer.raw_value
                speed = config.dimmer.speed.value
            elif config.digital:
                config.digital.init_value = self.last_values.get(port, value)
                value = config.digital.raw_value

            data = bytearray(
                [
                    self.CMD_WRITE,
                    port,
                    (value >> 8) & 0xFF,  # High byte
                    value & 0xFF,  # Low byte
                    speed,
                ]
            )
            data.append(Crc8Smbus.calc(data))

            await self.bus.write_i2c_block_data(self.address, data[0], data[1:])

            self.last_values[port] = value

            _LOGGER.debug(
                "Writing %s to port %s with speed %s on DM117 at address %02X with %s",
                value,
                port,
                speed,
                self.address,
                " ".join(f"{byte:02X}" for byte in data),
            )
        except OSError:
            _LOGGER.exception("Error writing to DM117")
            return False
        return True

    def expected_response_size(self) -> int:
        """Return how many bytes the next read has to fetch.

        Once the slot layout is known, the response is shorter than the worst case:
        a module count byte, then a type byte plus one value byte per slot (two for a
        dimmer), then the CRC. Reading only that much lets more modules share one
        batch. Falls back to the worst case until the layout has been seen, and after
        any failed decode, so a re-configured module recovers on the next cycle
        instead of failing forever against a truncated response.
        """

        types = self.last_port_types or self.port_config
        if self._force_full_read or not types:
            return self.READ_RESPONSE_SIZE

        size = 2 + sum(3 if device_type == DeviceType.DIMMER else 2 for device_type in types.values())
        return min(size, self.READ_RESPONSE_SIZE)

    def cached_ports(self) -> dict[int, int] | None:
        """Return the cached values while the minimum read interval has not elapsed."""

        if time.time() - self._last_read_time < self._read_interval:
            return self.last_values
        return None

    async def read_ports(self) -> dict[int, int] | None:
        """Read all port values; returns dict of port→raw-value or None on error.

        Convenience wrapper for single-device access. The poll loop instead batches
        the bus traffic for every module into one frame and calls ``decode_response``.
        """
        try:
            if (cached := self.cached_ports()) is not None:
                return cached

            # Locking must be handled by the caller. This method only prepares
            # and sends the payload.
            await self.bus.write_byte(self.address, self.CMD_READ)
            await asyncio.sleep(0.001)

            # The slave streams its whole prepared buffer from a single transaction
            # and answers 0xFF once it runs out, so reading the worst-case length in
            # one go is safe and costs one round trip instead of up to 26.
            block = await self.bus.read_i2c_block(self.address, self.expected_response_size())
        except OSError:
            return None
        return self.decode_response(block)

    def decode_response(self, block: list[int]) -> dict[int, int] | None:
        """Parse and CRC-check a read response; returns None when it is not usable."""

        try:
            num_modules = block[0]
            if num_modules > 8:  # Sanity check
                raise ValueError(f"Invalid number of modules: {num_modules}")  # noqa: TRY301

            values: dict[int, int] = {}
            port_types: dict[int, DeviceType] = {}
            data = [num_modules]  # Start with num_modules for CRC calculation
            offset = 1

            for i in range(num_modules):
                module_type = block[offset]
                offset += 1
                data.append(module_type)

                type_map = {0: DeviceType.INPUT, 1: DeviceType.DIMMER, 2: DeviceType.OUTPUT}
                if (device_type := type_map.get(module_type)) is not None:
                    port_types[i] = device_type

                if module_type == 1:  # DAC/Dimmer
                    high, low = block[offset], block[offset + 1]
                    offset += 2
                    value = (high << 8) | low
                    data.extend([high, low])
                else:
                    value = block[offset]
                    offset += 1
                    data.append(value)

                values[i] = value

            received_crc = block[offset]

            # Verify CRC
            calculated_crc = Crc8Smbus.calc(data)
            if received_crc != calculated_crc:
                self._force_full_read = True
                return None

            self.last_values = values
            self.last_port_types = port_types
            self._last_read_time = time.time()
            self._force_full_read = False

        except IndexError, ValueError:
            # A slot layout change truncates a shortened read. Fetch the worst case
            # next time so the new layout can be learned.
            self._force_full_read = True
            return None
        return values


@dataclass
class DimmerConfig:
    """Configuration for a dimmer port."""

    value: int  # Native 12-bit DAC value (0-4095)
    speed: DimmerSpeed = DimmerSpeed.DEFAULT

    def __post_init__(self) -> None:
        """Clamp value to valid range."""
        self.value = max(0, min(4095, self.value))

    @property
    def raw_value(self) -> int:
        """Return the native 12-bit DAC value."""

        return self.value

    @classmethod
    def from_raw(cls, value: int, speed: DimmerSpeed = DimmerSpeed.DEFAULT) -> DimmerConfig:
        """Create config from raw 0-4095 value."""

        return cls(value if 0 <= value <= 4095 else 0, speed)


@dataclass
class PortConfig:
    """Configuration for a digital input/output port."""

    port_a: bool | None = None
    port_b: bool | None = None
    init_value = 0

    @staticmethod
    def set_bit(v, index, x):
        """Set bit at index based on truthiness of x."""

        mask = 1 << index
        v &= ~mask
        if x:
            v |= mask
        return v

    @property
    def raw_value(self) -> int:
        """Convert to raw byte value."""
        value = self.init_value
        if self.port_a is not None:
            value = PortConfig.set_bit(value, 0, 1 if self.port_a else 0)
        if self.port_b is not None:
            value = PortConfig.set_bit(value, 1, 1 if self.port_b else 0)
        return value

    @classmethod
    def from_raw(cls, value: int) -> PortConfig:
        """Create config from raw byte value."""
        # Bits 0/1 represent A/B for outputs but physical D/C in DM117 input
        # responses. Callers intentionally retain this order for deployed wiring.
        if value is None or value < 0 or value > 3:
            value = 0
        return cls(port_a=bool(value & 0x01), port_b=bool(value & 0x02))


@dataclass
class DM117PortConfig:
    """Complete configuration for a DM117 port."""

    port: int  # Port number 0-7
    device_type: DeviceType
    dimmer: DimmerConfig | None = None  # For DIMMER type
    digital: PortConfig | None = None  # For INPUT/OUTPUT type

    def __post_init__(self) -> None:
        """Validate configuration."""
        if self.device_type == DeviceType.DIMMER:
            if self.dimmer is None:
                self.dimmer = DimmerConfig(0)
            self.digital = None
        else:
            if self.digital is None:
                self.digital = PortConfig()
            self.dimmer = None
