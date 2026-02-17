"""DM117 I2C module implementation supporting Input, Output and Dimmer ports."""

from __future__ import annotations

from dataclasses import dataclass
import enum
import logging
import time

from crccheck.crc import Crc8Smbus

_LOGGER = logging.getLogger(__name__)

MIN_INIT = 5


class DeviceType(enum.Enum):
    """Device types supported by DM117."""

    INPUT = "sinputh"
    OUTPUT = "output"
    DIMMER = "dimmer"


class DimmerSpeed(enum.IntEnum):
    """Speed settings for dimmer transitions."""

    INSTANT = 0
    DEFAULT = 2

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

    PORT_TYPE_INPUT = 0
    PORT_TYPE_DAC = 1
    PORT_TYPE_OUTPUT = 2

    def __init__(self, bus, address: int) -> None:
        """Initialize DM117 device.

        Args:
            bus: I2C bus instance
            address: I2C address of device
        """
        self.bus = bus
        self.address = address
        self.port_config = {}  # Stores port type configuration
        self.port_states = [0] * 8  # Current port states
        self.last_values = {}  # Cache for dimmer values
        self.port_types = {}  # Stores the type of each port
        self._last_read_time = 0
        self._read_interval = 0.01  # 10ms minimum between reads
        self._init_counter = 0

    def configure_ports(self, config: dict[int, DeviceType], commit: bool = True) -> bool:
        """Configure module ports.

        Args:
            config: Dictionary mapping port numbers to types ('input', 'output', 'dimmer')
            commit: Whether to commit configuration

        Returns:
            bool: True if configuration successful
        """
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
        """Write value to port.

        Args:
            config: Port configuration

        Returns:
            bool: True if write successful
        """
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
        """Read all port values.

        Returns:
            Dictionary mapping port numbers to values, or None on error

        Response Format (from requestEvent in dm117.cpp):
        - For each configured module:
            - 1 byte: module type (0=input, 1=dac/dimmer, 2=output)
            - Value bytes depend on type:
                - Input/Output: 1 byte state
                - DAC/Dimmer: 2 bytes (12-bit value)
        - Last byte: CRC8
        """
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
                    self.port_types[i] = self.PORT_TYPE_DAC
                else:
                    value = self.bus.read_byte(self.address)
                    data.append(value)
                    self.port_types[i] = self.PORT_TYPE_OUTPUT if module_type == 2 else self.PORT_TYPE_INPUT

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

            if self._init_counter < MIN_INIT:
                self._init_counter += 1

        except (OSError, ValueError):
            _LOGGER.exception("Error reading from DM117")
            return None
        return values

    def read_port(self, port: int) -> int | None:
        """Read single port value."""
        values = self.read_ports()
        if values is None:
            return None
        return values.get(port)

    def read_port_cached(self, port: int) -> int | None:
        """Read single port value from cache."""
        return self.last_values.get(port)

    @property
    def is_initialized(self) -> bool:
        """Check if module is initialized."""
        return self._init_counter >= MIN_INIT

    def get_port_type(self, port: int) -> int | None:
        """Get the type of port."""
        return self.port_types.get(port)


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

    @classmethod
    def from_api(cls, value: int) -> DimmerConfig:
        """Create config from API value (0-100)."""

        if value is None or value < 0 or value > 100:
            value = 0
        return cls(value)


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
        if value is None or value < 0 or value > 3:
            value = 0
        return cls(port_a=bool(value & 0x01), port_b=bool(value & 0x02))

    @classmethod
    def from_api(cls, value: tuple[bool, bool]) -> PortConfig:
        """Create config from API values."""
        if value is None or len(value) != 2:
            value = (False, False)
        return cls(port_a=value[0], port_b=value[1])


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

    @property
    def raw_value(self) -> int:
        """Get raw value for this port based on type."""
        if self.device_type == DeviceType.DIMMER and self.dimmer:
            return self.dimmer.raw_value
        if self.digital:
            return self.digital.raw_value
        return 0

    @classmethod
    def from_raw(
        cls,
        port: int,
        device_type: DeviceType,
        value: int,
        speed: DimmerSpeed = DimmerSpeed.DEFAULT,
    ) -> DM117PortConfig:
        """Create port config from raw values."""
        if device_type == DeviceType.DIMMER:
            return cls(port, device_type, dimmer=DimmerConfig.from_raw(value, speed))

        return cls(port, device_type, digital=PortConfig.from_raw(int(value)))
