"""Support for casaIT lights."""

from __future__ import annotations

from typing import Any

from homeassistant.components.light import ATTR_BRIGHTNESS, ATTR_EFFECT, ATTR_RGB_COLOR, ATTR_TRANSITION, LightEntity
from homeassistant.components.light.const import ColorMode, LightEntityFeature
from homeassistant.const import STATE_ON
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from . import CasaITConfigEntry
from .api import CasaITApi
from .const import DEFAULT_LED_COUNT, DOMAIN
from .entity import CasaITOneWireEntity
from .helpers import (
    build_bridge_slug,
    build_device_identifier,
    build_i2c_entity_id,
    build_onewire_device_info,
    build_onewire_entity_id,
    default_onewire_profile,
    get_configured_led_counts,
    get_configured_onewire_profiles,
    get_dm117_port_configuration,
    get_module_name,
)
from .services.i2cClasses.dm117 import DeviceType, DimmerConfig, DimmerSpeed, DM117PortConfig
from .services.i2cClasses.led_controller import AnimationMode, Color, LEDConfig

PARALLEL_UPDATES = 1

DM117_TRANSITION_SECONDS = {
    DimmerSpeed.INSTANT: 0.0,
    DimmerSpeed.FAST: 1.7,
    DimmerSpeed.SLOW: 5.1,
}

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
            config_entry,
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
    _attr_supported_features = LightEntityFeature.TRANSITION
    _attr_translation_key = "dm117_dimmer"

    def __init__(
        self,
        api: CasaITApi,
        config_entry: CasaITConfigEntry,
        address: int,
        port: int,
    ) -> None:
        """Initialize the light entity."""
        self._api = api
        self._address = address
        self._port = port
        self._slot = port + 1
        bridge_slug = build_bridge_slug(config_entry.entry_id, config_entry.unique_id)
        self._attr_unique_id = f"{config_entry.entry_id}_dm117_{address}_{port}_dimmer"
        self.entity_id = build_i2c_entity_id("light", bridge_slug, "dm117", address, "slot", self._slot, "dimmer")
        self._attr_translation_placeholders = {"slot": str(self._slot)}
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, build_device_identifier(config_entry.entry_id, "dm117", address))},
            name=get_module_name(config_entry.options, "dm117", address, f"DM117 0x{address:02X}"),
            manufacturer="casaIT",
            model="DM117",
            via_device=(DOMAIN, build_device_identifier(config_entry.entry_id, "bridge", "controller")),
        )
        self._update_state()

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
        self.async_on_remove(
            async_dispatcher_connect(self.hass, self._api.address_signal(self._address), self._handle_state_update)
        )

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn on the light with optional brightness."""
        brightness = kwargs.get(ATTR_BRIGHTNESS)
        if brightness is None:
            brightness = 255
        raw_value = max(0, min(4095, round(brightness * 4095 / 255)))
        await self._async_write(raw_value, self._transition_speed(kwargs.get(ATTR_TRANSITION)))

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn off the light."""
        await self._async_write(0, self._transition_speed(kwargs.get(ATTR_TRANSITION)))

    async def _async_write(self, raw_value: int, speed: DimmerSpeed) -> None:
        dimmer = DimmerConfig(value=raw_value, speed=speed)
        config = DM117PortConfig(
            port=self._port,
            device_type=DeviceType.DIMMER,
            dimmer=dimmer,
        )

        if not await self._api.async_write_dm117_port(self._address, config):
            raise HomeAssistantError(translation_domain=DOMAIN, translation_key="dimmer_write_failed")

    @staticmethod
    def _transition_speed(transition: Any) -> DimmerSpeed:
        """Map a Home Assistant transition duration to the closest firmware ramp."""

        if transition is None:
            return DimmerSpeed.DEFAULT
        try:
            seconds = max(0.0, float(transition))
        except TypeError, ValueError:
            return DimmerSpeed.DEFAULT
        return min(DM117_TRANSITION_SECONDS, key=lambda speed: abs(DM117_TRANSITION_SECONDS[speed] - seconds))

    @staticmethod
    def _raw_to_brightness(raw_value: int | None) -> int | None:
        if raw_value is None:
            return None
        return max(0, min(255, round((raw_value / 4095) * 255)))

    @property
    def available(self) -> bool:
        """Return True if the entity is available."""
        return self._address in self._api.dm117_states


class CasaITLEDControllerLight(CasaITOneWireEntity, LightEntity, RestoreEntity):
    """Representation of a DS28E17-based LED controller."""

    _attr_has_entity_name = True
    _attr_supported_color_modes = {ColorMode.RGB}
    _attr_color_mode = ColorMode.RGB
    _attr_supported_features = LightEntityFeature.EFFECT
    _attr_translation_key = "led_controller"

    def __init__(
        self,
        api: CasaITApi,
        config_entry: CasaITConfigEntry,
        device_id: str,
        meta: dict[str, Any],
        led_count: int,
    ) -> None:
        """Initialize the LED controller light."""

        self._api = api
        self._device_id = device_id
        self._meta = meta
        self._config: LEDConfig | None = None
        self._led_count = led_count or DEFAULT_LED_COUNT
        self._attr_effect_list = list(ANIMATION_EFFECTS.values())
        bridge_slug = build_bridge_slug(config_entry.entry_id, config_entry.unique_id)
        self._attr_unique_id = f"{config_entry.entry_id}_{device_id}_led_controller"
        self.entity_id = build_onewire_entity_id("light", bridge_slug, device_id, meta, "led", "controller")
        self._attr_device_info = build_onewire_device_info(config_entry.entry_id, device_id, meta)
        self._attr_assumed_state = True

    async def async_added_to_hass(self) -> None:
        """Restore the last UI state until the controller responds."""

        await super().async_added_to_hass()
        if self._config is not None or (last_state := await self.async_get_last_state()) is None:
            return

        config = LEDConfig.create_default()
        config.state = last_state.state == STATE_ON
        if (brightness := last_state.attributes.get(ATTR_BRIGHTNESS)) is not None:
            config.brightness = max(0, min(255, int(brightness)))
        if (rgb := last_state.attributes.get(ATTR_RGB_COLOR)) is not None and len(rgb) == 3:
            self._set_primary_color(config, *rgb)
        if (effect := last_state.attributes.get(ATTR_EFFECT)) in EFFECT_TO_ANIMATION:
            config.animation = EFFECT_TO_ANIMATION[effect]
        self._apply_config(config, from_read=False)

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

    def _refresh(self) -> None:
        # Until the first read the restored state stays usable, so the light
        # does not show as unavailable for its first interval.
        if (config := self._api.onewire.value(self._device_id)) is not None:
            self._update_from_value(config)
        elif self._config is not None and not self._attr_assumed_state:
            self._attr_available = False

    def _update_from_value(self, value: Any) -> None:
        config = LEDConfig(
            led_count=value.led_count,
            state=value.state,
            brightness=value.brightness,
            animation=value.animation,
            animation_speed=value.animation_speed,
            colors=list(value.colors),
        )
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
            raise HomeAssistantError(translation_domain=DOMAIN, translation_key="invalid_led_configuration")

        success = await self._api.write_led_config(self._device_id, config)
        if not success:
            raise HomeAssistantError(translation_domain=DOMAIN, translation_key="led_update_failed")

        # The scheduler publishes the written configuration, which lands in
        # _update_from_value; this only covers the time until it arrives.
        self._led_count = config.led_count or self._led_count
        self._apply_config(config, from_read=True)
