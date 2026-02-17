"""LED controller using the DS28E17 bridge."""

from __future__ import annotations

from dataclasses import dataclass, field
import enum
import logging
import time

from .ds28e17 import DS28E17

_LOGGER = logging.getLogger(__name__)

CACHE_TIMEOUT = 20  # Cache timeout in seconds


class AnimationMode(enum.Enum):
    """Supported LED animation modes."""

    STATIC = 0
    CHASE = 1
    RAINBOW = 2
    PULSE = 3
    ALTERNATE = 4


@dataclass
class Color:
    """RGB color representation."""

    red: int
    green: int
    blue: int

    def to_bytes(self) -> bytes:
        """Convert color to bytes for transmission."""
        return bytes([self.red, self.green, self.blue])

    @classmethod
    def from_bytes(cls, data: bytes) -> Color:
        """Create Color instance from bytes."""
        return cls(red=data[0], green=data[1], blue=data[2])


@dataclass
class LEDConfig:
    """Configuration payload for the LED controller."""

    led_count: int = 0
    state: bool = False
    brightness: int = 0  # 1-31 in hardware, 0-255 in validation
    animation: AnimationMode = AnimationMode.STATIC
    animation_speed: int = 0  # 0-255
    colors: list[Color] = field(default_factory=list)

    @classmethod
    def create_default(cls) -> LEDConfig:
        """Create default configuration."""
        return cls(
            led_count=30,
            state=False,
            brightness=128,
            animation=AnimationMode.STATIC,
            animation_speed=50,
            colors=[
                Color(255, 255, 255),  # White
                Color(0, 0, 0),
                Color(0, 0, 0),
                Color(0, 0, 0),
                Color(0, 0, 0),
            ],
        )

    def validate(self) -> bool:
        """Validate configuration values."""

        if not 1 <= self.led_count <= 255:
            return False
        if not 0 <= self.brightness <= 255:
            return False
        if not 0 <= self.animation_speed <= 255:
            return False
        if len(self.colors) != 5:
            return False

        for color in self.colors:
            if not (0 <= color.red <= 255 and 0 <= color.green <= 255 and 0 <= color.blue <= 255):
                return False

        return True


@dataclass
class CachedConfig:
    """Cache container for LED configuration."""

    config: LEDConfig
    timestamp: float
    cache_time: float = CACHE_TIMEOUT

    @property
    def is_valid(self) -> bool:
        """Check if cached config is still valid."""
        return time.time() - self.timestamp < self.cache_time


class LEDController:
    """LED Controller implementation using DS28E17 bridge."""

    # I2C registers (matching Arduino implementation)
    I2C_ADDRESS = 0x42
    REG_LED_COUNT = 0x00
    REG_LED_STATE = 0x01
    REG_BRIGHTNESS = 0x02
    REG_ANIM_MODE = 0x03
    REG_ANIM_SPEED = 0x04
    REG_COLORS = 0x05  # Colors start here (3 bytes per color)

    def __init__(self, bus_interface) -> None:
        """Initialize LED Controller.

        Args:
            bus_interface: Interface to 1-Wire bus.
        """

        self.bridge = DS28E17(bus_interface)
        self.bus = bus_interface
        self._config_cache: dict[str, CachedConfig] = {}

    def write_config(self, device_id: str, config: LEDConfig, custom_cache: int | None = None) -> bool:
        """Write LED configuration."""
        if not config.validate():
            _LOGGER.error("Invalid LED configuration")
            return False

        try:
            # Prepare data packet starting with register address
            data = bytearray(
                [
                    self.REG_LED_COUNT,  # Start register
                    config.led_count,
                    1 if config.state else 0,
                    config.brightness,
                    config.animation.value,
                    config.animation_speed,
                ]
            )

            # Add color data
            for color in config.colors:
                data.extend([color.red, color.green, color.blue])

            retries = 4
            while retries > 0:
                if self.bridge.write_data(device_id, self.I2C_ADDRESS, bytes(data)):
                    break

                retries -= 1
                _LOGGER.warning(
                    "Failed to write LED configuration; retries remaining: %s",
                    retries,
                )
                time.sleep(0.2)

            if retries == 0:
                _LOGGER.error("Failed to write LED configuration")
                return False

            # Update cache with new configuration
            self._config_cache[device_id] = CachedConfig(
                config=config,
                timestamp=time.time(),
                cache_time=custom_cache or CACHE_TIMEOUT,
            )

            _LOGGER.info("Successfully wrote LED configuration for device %s", device_id)

        except Exception:
            _LOGGER.exception("Error writing LED configuration")
            return False
        return True

    def read_config(self, device_id: str, custom_cache: int | None = None, use_cache: bool = True) -> LEDConfig | None:
        """Read current LED configuration."""
        try:
            # Check cache first if requested
            if use_cache and device_id in self._config_cache:
                cached = self._config_cache[device_id]
                if cached.is_valid:
                    return cached.config

            # Cache miss or bypass - read from device
            # Calculate total bytes to read:
            # - 5 bytes for basic config (count, state, brightness, mode, speed)
            # - 15 bytes for colors (5 colors × 3 bytes each)
            total_bytes = 20

            # Read data through bridge
            data = self.bridge.read_data(device_id, self.I2C_ADDRESS, total_bytes)
            if not data or len(data) != total_bytes:
                _LOGGER.error("Failed to read configuration data")
                return None

            # Parse configuration
            led_count = data[self.REG_LED_COUNT]
            led_state = bool(data[self.REG_LED_STATE])
            brightness = data[self.REG_BRIGHTNESS]
            animation_code = data[self.REG_ANIM_MODE]
            animation_speed = data[self.REG_ANIM_SPEED]

            # Parse colors (5 colors, 3 bytes each)
            colors = []
            for i in range(5):
                offset = self.REG_COLORS + (i * 3)
                colors.append(Color(red=data[offset], green=data[offset + 1], blue=data[offset + 2]))

            try:
                animation = AnimationMode(animation_code)
            except ValueError:
                _LOGGER.error("Invalid animation code %s from device", animation_code)
                return None

            config = LEDConfig(
                led_count=led_count,
                state=led_state,
                brightness=brightness,
                animation=animation,
                animation_speed=animation_speed,
                colors=colors,
            )

            if not config.validate():
                _LOGGER.error("Invalid LED configuration read from device")
                return None

            # Update cache with new configuration
            self._config_cache[device_id] = CachedConfig(
                config=config,
                timestamp=time.time(),
                cache_time=custom_cache or CACHE_TIMEOUT,
            )

        except Exception:
            _LOGGER.exception("Error reading LED configuration")
            return None
        return config

    def get_cached_config(self, device_id: str) -> LEDConfig | None:
        """Get configuration from cache if available and valid."""
        if device_id not in self._config_cache:
            return None

        cached = self._config_cache[device_id]
        return cached.config if cached.is_valid else None

    def invalidate_cache(self, device_id: str | None = None) -> None:
        """Invalidate cache for specific device or all devices."""
        if device_id is None:
            self._config_cache.clear()
        elif device_id in self._config_cache:
            del self._config_cache[device_id]
