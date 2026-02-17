"""Support for casaIT lights."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from homeassistant.components.light import ATTR_BRIGHTNESS, ATTR_EFFECT, ATTR_RGB_COLOR, LightEntity
from homeassistant.components.light.const import ColorMode, LightEntityFeature
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import CasaITConfigEntry
from .api import CasaITApi
from .const import DEFAULT_LED_COUNT, DOMAIN, SIGNAL_STATE_UPDATED
from .helpers import (
    default_onewire_profile,
    get_configured_led_counts,
    get_configured_onewire_profiles,
    get_dm117_port_configuration,
)
from .services.i2cClasses.dm117 import DeviceType, DimmerConfig, DM117PortConfig
from .services.i2cClasses.led_controller import AnimationMode, Color, LEDConfig

ANIMATION_EFFECTS = {
    AnimationMode.STATIC: "Static",
    AnimationMode.CHASE: "Chase",
    AnimationMode.RAINBOW: "Rainbow",
    AnimationMode.PULSE: "Pulse",
    AnimationMode.ALTERNATE: "Alternate",
}

EFFECT_TO_ANIMATION = {name: mode for mode, name in ANIMATION_EFFECTS.items()}


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: CasaITConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up casaIT lights."""

    api: CasaITApi = config_entry.runtime_data
    await api.async_wait_initialized()
    dm_config = get_dm117_port_configuration(config_entry.options)

    entities: list[LightEntity] = [
        CasaITDM117Light(api, config_entry, addr, port)
        for addr, slots in dm_config.items()
        if addr in api.dm117
        for port, device_type in slots.items()
        if device_type is DeviceType.DIMMER
    ]

    configured_profiles = get_configured_onewire_profiles(config_entry.options)
    led_counts = get_configured_led_counts(config_entry.options)

    led_entities = [
        CasaITLEDControllerLight(
            api,
            device_id,
            meta,
            led_counts.get(device_id, DEFAULT_LED_COUNT),
        )
        for device_id, meta in api.ow_devices.items()
        if (configured_profiles.get(device_id) or default_onewire_profile(meta)) == "ds28e17_led"
    ]

    entities.extend(led_entities)

    async_add_entities(entities)


class CasaITDM117Light(LightEntity):
    """Representation of a DM117 dimmer slot."""

    _attr_has_entity_name = True
    _attr_supported_color_modes = {ColorMode.BRIGHTNESS}
    _attr_color_mode = ColorMode.BRIGHTNESS
    _attr_should_poll = False

    def __init__(
        self,
        api: CasaITApi,
        config_entry: CasaITConfigEntry,
        address: int,
        port: int,
    ) -> None:
        """Initialize the light entity.

        Args:
            coordinator: The data coordinator for casaIT.
            config_entry: The configuration entry for this entity.
            address: The I2C address of the DM117 module.
            port: The port number on the DM117 module (0-7).
        """
        self._api = api
        self._address = address
        self._port = port
        self._slot = port + 1
        self._attr_unique_id = f"{config_entry.entry_id}_dm117_{address}_{port}_dimmer"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"dm117_{address}")},
            name=f"DM117 module {hex(address)}",
            manufacturer="casaIT",
            model="DM117",
        )
        self._update_state()

    @property
    def name(self) -> str:
        """Return the name of the light."""
        return f"Slot {self._slot} Dimmer"

    def _update_state(self) -> None:
        states = self._api.dm117_states.get(self._address)
        if not states or self._port not in states:
            self._attr_is_on = None
            self._attr_brightness = None
            return

        raw_value = states[self._port]
        brightness = self._raw_to_brightness(raw_value)
        self._attr_brightness = brightness
        self._attr_is_on = brightness is not None and brightness > 0
        self._attr_color_mode = ColorMode.BRIGHTNESS

    @callback
    def _handle_state_update(self) -> None:
        self._update_state()
        self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        """Register callbacks when entity is added to hass."""
        await super().async_added_to_hass()
        self.async_on_remove(async_dispatcher_connect(self.hass, SIGNAL_STATE_UPDATED, self._handle_state_update))

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn on the light with optional brightness."""
        brightness = kwargs.get(ATTR_BRIGHTNESS)
        if brightness is None:
            brightness = 255
        percentage = max(0, min(100, round(brightness * 100 / 255)))
        await self._async_write(percentage)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn off the light."""
        await self._async_write(0)

    async def _async_write(self, percentage: int) -> None:
        device = self._api.dm117.get(self._address)
        if not device:
            raise HomeAssistantError("DM117 module not available")

        dimmer = DimmerConfig(value=percentage)
        config = DM117PortConfig(
            port=self._port,
            device_type=DeviceType.DIMMER,
            dimmer=dimmer,
        )

        async with self._api.lock:
            await self.hass.async_add_executor_job(device.write_port, config)

        await self._api.async_force_refresh()

    @staticmethod
    def _raw_to_brightness(raw_value: int | None) -> int | None:
        if raw_value is None:
            return None
        return max(0, min(255, round((raw_value / 4095) * 255)))

    @property
    def available(self) -> bool:
        """Return True if the entity is available."""
        return self._address in self._api.dm117_states


class CasaITLEDControllerLight(LightEntity):
    """Representation of a DS28E17-based LED controller."""

    _attr_should_poll = True
    _attr_supported_color_modes = {ColorMode.RGB}
    _attr_color_mode = ColorMode.RGB
    _attr_supported_features = LightEntityFeature.EFFECT
    SCAN_INTERVAL = timedelta(seconds=10)

    def __init__(self, api: CasaITApi, device_id: str, meta: dict[str, Any], led_count: int) -> None:
        """Initialize the LED controller light."""

        self._api = api
        self._device_id = device_id
        self._meta = meta
        self._config: LEDConfig | None = None
        self._led_count = led_count or DEFAULT_LED_COUNT
        self._attr_effect_list = list(ANIMATION_EFFECTS.values())
        self._attr_unique_id = f"{device_id}_led_controller"
        self._attr_name = f"{device_id} LED controller"
        self._attr_device_info = _build_onewire_device_info(device_id, meta)
        self._attr_assumed_state = True

    @property
    def is_on(self) -> bool | None:
        """Return True if the light is on."""

        return None if self._config is None else self._config.state

    @property
    def brightness(self) -> int | None:
        """Return brightness 0-255."""

        return None if self._config is None else self._config.brightness

    @property
    def rgb_color(self) -> tuple[int, int, int] | None:
        """Return RGB color."""

        if not self._config or not self._config.colors:
            return None
        first = self._config.colors[0]
        return (first.red, first.green, first.blue)

    @property
    def effect(self) -> str | None:
        """Return the active effect."""

        if not self._config:
            return None
        return ANIMATION_EFFECTS.get(self._config.animation)

    async def async_update(self) -> None:
        """Poll the LED controller configuration."""

        config = await self._api.read_led_config(self._device_id, use_cache=False)
        if config is None:
            self._attr_available = False
            return

        self._led_count = config.led_count or self._led_count
        self._apply_config(config, from_read=True)

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn on the LED controller with optional parameters."""

        config = self._build_target_config()
        config.state = True
        config.led_count = self._led_count

        brightness = kwargs.get(ATTR_BRIGHTNESS)
        if brightness is not None:
            config.brightness = max(0, min(255, int(brightness)))
        elif config.brightness == 0:
            config.brightness = 255

        if ATTR_RGB_COLOR in kwargs:
            r, g, b = kwargs[ATTR_RGB_COLOR]
            self._set_primary_color(config, r, g, b)

        if ATTR_EFFECT in kwargs:
            effect_name = kwargs[ATTR_EFFECT]
            if animation := EFFECT_TO_ANIMATION.get(effect_name):
                config.animation = animation

        await self._async_write_config(config)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the LED controller off."""

        config = self._build_target_config()
        config.state = False
        await self._async_write_config(config)

    def _build_target_config(self) -> LEDConfig:
        base = self._config or LEDConfig.create_default()
        colors = base.colors or LEDConfig.create_default().colors
        colors = [Color(color.red, color.green, color.blue) for color in colors]

        return LEDConfig(
            led_count=base.led_count or self._led_count or DEFAULT_LED_COUNT,
            state=base.state,
            brightness=base.brightness,
            animation=base.animation,
            animation_speed=base.animation_speed,
            colors=colors,
        )

    def _set_primary_color(self, config: LEDConfig, red: int, green: int, blue: int) -> None:
        colors = config.colors or []
        red = max(0, min(255, int(red)))
        green = max(0, min(255, int(green)))
        blue = max(0, min(255, int(blue)))

        if colors:
            colors[0] = Color(red, green, blue)
        else:
            colors = [Color(red, green, blue)]

        self._ensure_colors(config, colors)

    def _ensure_colors(self, config: LEDConfig, colors: list[Color] | None = None) -> None:
        palette = list(colors or config.colors or [])
        while len(palette) < 5:
            palette.append(Color(0, 0, 0))
        config.colors = palette[:5]

    def _apply_config(self, config: LEDConfig, *, from_read: bool) -> None:
        self._ensure_colors(config)

        self._config = config
        self._attr_available = True
        self._attr_assumed_state = not from_read
        self._attr_is_on = config.state
        self._attr_brightness = config.brightness
        self._attr_color_mode = ColorMode.RGB
        self._attr_effect = ANIMATION_EFFECTS.get(config.animation)
        if config.colors:
            first = config.colors[0]
            self._attr_rgb_color = (first.red, first.green, first.blue)

    async def _async_write_config(self, config: LEDConfig) -> None:
        self._ensure_colors(config)

        if not config.validate():
            raise HomeAssistantError("Invalid LED configuration")

        success = await self._api.write_led_config(self._device_id, config)
        if not success:
            raise HomeAssistantError("Unable to update LED controller")

        self._led_count = config.led_count or self._led_count
        self._apply_config(config, from_read=False)


def _build_onewire_device_info(device_id: str, meta: dict[str, Any]) -> DeviceInfo:
    """Return DeviceInfo referencing the SM117 bus for OneWire devices."""

    bus_address = meta.get("bus_address")
    device_type = str(meta.get("device_type") or "").strip()
    if bus_address is not None:
        return DeviceInfo(
            identifiers={(DOMAIN, f"sm117_{bus_address:02x}")},
            name=f"SM117 Bus 0x{int(bus_address):02X}",
            manufacturer="CasaIT",
            model="SM117 1-Wire bridge",
        )

    return DeviceInfo(
        identifiers={(DOMAIN, f"onewire_{device_id}")},
        name=f"OneWire {device_id}",
        model=device_type or "OneWire",
        manufacturer="Maxim Integrated",
    )
