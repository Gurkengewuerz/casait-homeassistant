"""DM117 I2C module implementation supporting Input, Output and Dimmer ports."""

from __future__ import annotations

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

    def __init__(self, bus, address: int) -> None:
        """Initialize DM117 device."""
        self.bus = bus
        self.address = address
        self.port_config = {}  # Stores port type configuration
        self.port_states = [0] * 8  # Current port states
        self.last_values = {}  # Cache for dimmer values
        self._last_read_time = 0
        self._read_interval = 0.01  # 10ms minimum between reads

    def configure_ports(self, config: dict[int, DeviceType], commit: bool = True) -> bool:
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
            self.bus.write_i2c_block_data(self.address, data[0], data[1:])

            _LOGGER.debug(
                "Configured DM117 at address %02X with %s ports %s",
                self.address,
                len(config),
                " ".join(f"{value:02X}" for value in data),
            )

            # Store configuration
            self.port_config = dict(config)

            if commit:
                return self.commit_config()

        except OSError:
            _LOGGER.exception("Error configuring DM117")
            return False
        return True

    def commit_config(self) -> bool:
        """Commit the current configuration to the device."""
        try:
            data = bytearray([self.CMD_COMMIT])
            data.append(Crc8Smbus.calc(data))
            self.bus.write_i2c_block_data(self.address, data[0], data[1:])
            _LOGGER.debug("Committed DM117 configuration at address %02X", self.address)
        except OSError:
            _LOGGER.exception("Error committing DM117 configuration")
            return False
        return True

    def write_port(self, config: DM117PortConfig) -> bool:
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

            self.bus.write_i2c_block_data(self.address, data[0], data[1:])

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

    def read_ports(self) -> dict[int, int] | None:
        """Read all port values; returns dict of port→raw-value or None on error."""
        try:
            current_time = time.time()
            if current_time - self._last_read_time < self._read_interval:
                return self.last_values

            # Locking must be handled by the caller. This method only prepares
            # and sends the payload.
            self.bus.write_byte(self.address, self.CMD_READ)
            time.sleep(0.001)

            num_modules = self.bus.read_byte(self.address)
            if num_modules > 8:  # Sanity check
                raise ValueError(f"Invalid number of modules: {num_modules}")  # noqa: TRY301

            values: dict[int, int] = {}
            data = [num_modules]  # Start with num_modules for CRC calculation

            for i in range(num_modules):
                module_type = self.bus.read_byte(self.address)
                data.append(module_type)

                if module_type == 1:  # DAC/Dimmer
                    high = self.bus.read_byte(self.address)
                    low = self.bus.read_byte(self.address)
                    value = (high << 8) | low
                    data.extend([high, low])
                else:
                    value = self.bus.read_byte(self.address)
                    data.append(value)

                values[i] = value

            received_crc = self.bus.read_byte(self.address)

            # Verify CRC
            calculated_crc = Crc8Smbus.calc(data)
            if received_crc != calculated_crc:
                _LOGGER.debug(
                    "Reading from DM117 at address %02X with %s",
                    self.address,
                    " ".join(f"{byte:02X}" for byte in [*data, received_crc]),
                )
                _LOGGER.error("CRC validation failed")
                return None

            self.last_values = values
            self._last_read_time = current_time

        except (OSError, ValueError):
            return None
        return values

@dataclass
class DimmerConfig:
    """Configuration for a dimmer port."""

    value: int  # 0-100 percentage
    speed: DimmerSpeed = DimmerSpeed.DEFAULT

    def __post_init__(self) -> None:
        """Clamp value to valid range."""
        self.value = max(0, min(100, self.value))  # Clamp to 0-100

    @property
    def raw_value(self) -> int:
        """Convert 0-100 to 0-4095 range."""

        return int((self.value / 100.0) * 4095)

    @classmethod
    def from_raw(cls, value: int, speed: DimmerSpeed = DimmerSpeed.DEFAULT) -> DimmerConfig:
        """Create config from raw 0-4095 value."""

        if value is None or value < 0 or value > 4095:
            value = 0
        percentage = (value / 4095.0) * 100
        return cls(int(percentage), speed)

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
