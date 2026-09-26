"""Runtime-adjustable numeric controls for casaIT hardware."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from homeassistant.components.number import (
    NumberDeviceClass,
    NumberEntity,
    NumberEntityDescription,
    NumberMode,
    RestoreNumber,
)
from homeassistant.const import CONCENTRATION_PARTS_PER_MILLION, EntityCategory, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from . import CasaITConfigEntry
from .api import CasaITApi
from .const import (
    DEFAULT_CO2_CALIBRATION_PPM,
    DOMAIN,
    OM117_MODE_BLIND,
    OM117_MODE_PULSE,
    OM117_MODE_SHUTTER,
    OW_PROFILE_MULTISENSOR,
)
from .entity import CasaITMultisensorEntity
from .helpers import (
    OM117PairConfig,
    build_bridge_slug,
    build_device_identifier,
    build_i2c_entity_id,
    build_onewire_device_info,
    build_onewire_entity_id,
    default_onewire_profile,
    get_configured_onewire_profiles,
    get_module_name,
)
from .services.i2cClasses.led_controller import LEDConfig

PARALLEL_UPDATES = 1
SCAN_INTERVAL = timedelta(seconds=10)


@dataclass(frozen=True, slots=True)
class RuntimeNumberDefinition:
    """Describe a mutable OM117 timing value."""

    key: str
    minimum: float
    maximum: float
    step: float


COVER_RUNTIME_NUMBERS = (
    RuntimeNumberDefinition("open_time", 1.0, 180.0, 0.1),
    RuntimeNumberDefinition("close_time", 1.0, 180.0, 0.1),
    RuntimeNumberDefinition("overrun_time", 0.0, 15.0, 0.1),
)
TILT_RUNTIME_NUMBER = RuntimeNumberDefinition("tilt_time", 0.1, 15.0, 0.1)
PULSE_RUNTIME_NUMBER = RuntimeNumberDefinition("pulse_duration", 0.1, 30.0, 0.1)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: CasaITConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up runtime number entities."""

    api = entry.runtime_data
    await api.async_wait_initialized()
    entities: list[NumberEntity] = []

    for address, pairs in api.om117_pair_configuration.items():
        if address not in api.im117_om117:
            continue
        for pair_index, pair_config in pairs.items():
            if pair_config.mode in {OM117_MODE_BLIND, OM117_MODE_SHUTTER}:
                entities.extend(
                    CasaITOM117RuntimeNumber(api, entry, address, pair_index, pair_config, definition)
                    for definition in COVER_RUNTIME_NUMBERS
                )
                if pair_config.mode == OM117_MODE_BLIND:
                    entities.append(
                        CasaITOM117RuntimeNumber(
                            api,
                            entry,
                            address,
                            pair_index,
                            pair_config,
                            TILT_RUNTIME_NUMBER,
                        )
                    )
            elif pair_config.mode == OM117_MODE_PULSE:
                entities.append(
                    CasaITOM117RuntimeNumber(
                        api,
                        entry,
                        address,
                        pair_index,
                        pair_config,
                        PULSE_RUNTIME_NUMBER,
                    )
                )

    configured_profiles = get_configured_onewire_profiles(entry.options)
    for device_id, meta in api.ow_devices.items():
        profile = configured_profiles.get(device_id) or default_onewire_profile(meta)
        if profile == OW_PROFILE_MULTISENSOR:
            parts = api.multisensor.components(device_id)
            if parts is not None and parts.stcc4:
                entities.append(CasaITCO2CalibrationTarget(api, entry, device_id, meta))
            continue
        if profile != "ds28e17_led":
            continue
        entities.extend(
            (
                CasaITLEDControllerNumber(api, entry, device_id, meta, "animation_speed"),
                CasaITLEDControllerNumber(api, entry, device_id, meta, "led_count"),
            )
        )

    if entities:
        async_add_entities(entities)


class CasaITOM117RuntimeNumber(NumberEntity, RestoreEntity):
    """Runtime calibration number shared with an OM117 cover or pulse output."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_mode = NumberMode.BOX
    _attr_device_class = NumberDeviceClass.DURATION
    _attr_native_unit_of_measurement = UnitOfTime.SECONDS
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(
        self,
        api: CasaITApi,
        entry: CasaITConfigEntry,
        address: int,
        pair_index: int,
        pair_config: OM117PairConfig,
        definition: RuntimeNumberDefinition,
    ) -> None:
        """Initialize one runtime calibration control."""

        self._api = api
        self._address = address
        self._pair_config = pair_config
        self._field = definition.key
        self._attr_translation_key = f"om117_{definition.key}"
        self._attr_native_min_value = definition.minimum
        self._attr_native_max_value = definition.maximum
        self._attr_native_step = definition.step
        self._attr_native_value = float(getattr(pair_config, definition.key))
        bridge_slug = build_bridge_slug(entry.entry_id, entry.unique_id)
        self._attr_unique_id = f"{entry.entry_id}_om117_{address}_pair_{pair_index + 1}_{definition.key}"
        self.entity_id = build_i2c_entity_id(
            "number", bridge_slug, "om117", address, "pair", pair_index + 1, definition.key
        )
        self._attr_translation_placeholders = {"pair": str(pair_index + 1)}
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, build_device_identifier(entry.entry_id, "om117", address))},
            name=get_module_name(entry.options, "om117", address, f"OM117 0x{address:02X}"),
            manufacturer="casaIT",
            model="PCF8574 Output",
            via_device=(DOMAIN, build_device_identifier(entry.entry_id, "bridge", "controller")),
        )

    async def async_added_to_hass(self) -> None:
        """Restore the last runtime calibration value."""

        await super().async_added_to_hass()
        if (last_state := await self.async_get_last_state()) is None:
            return
        try:
            value = float(last_state.state)
        except ValueError:
            return
        if self.native_min_value <= value <= self.native_max_value:
            self._set_value(value)

    async def async_set_native_value(self, value: float) -> None:
        """Apply a new runtime calibration value."""

        self._set_value(value)
        self.async_write_ha_state()

    def _set_value(self, value: float) -> None:
        """Update the shared pair configuration."""

        bounded = max(self.native_min_value, min(self.native_max_value, float(value)))
        setattr(self._pair_config, self._field, bounded)
        self._attr_native_value = bounded

    @property
    def available(self) -> bool:
        """Return whether the module remains available."""

        return self._address in self._api.im117_om117


class CasaITLEDControllerNumber(NumberEntity):
    """Writable LED controller configuration register."""

    _attr_has_entity_name = True
    _attr_should_poll = True
    _attr_mode = NumberMode.SLIDER
    _attr_entity_category = EntityCategory.CONFIG
    _attr_native_min_value = 0
    _attr_native_max_value = 255
    _attr_native_step = 1

    def __init__(
        self,
        api: CasaITApi,
        entry: CasaITConfigEntry,
        device_id: str,
        meta: dict[str, Any],
        field: str,
    ) -> None:
        """Initialize one LED controller register."""

        self._api = api
        self._device_id = device_id
        self._field = field
        self._attr_translation_key = "led_animation_speed" if field == "animation_speed" else "led_count"
        if field == "led_count":
            self._attr_native_min_value = 1
        bridge_slug = build_bridge_slug(entry.entry_id, entry.unique_id)
        self._attr_unique_id = f"{entry.entry_id}_{device_id}_{field}"
        self.entity_id = build_onewire_entity_id("number", bridge_slug, device_id, meta, "led", field)
        self._attr_device_info = build_onewire_device_info(entry.entry_id, device_id, meta)

    async def async_update(self) -> None:
        """Read the current register value."""

        config = await self._api.read_led_config(self._device_id, use_cache=False)
        self._attr_available = config is not None
        if config is not None:
            self._attr_native_value = int(getattr(config, self._field))

    async def async_set_native_value(self, value: float) -> None:
        """Write the register while preserving the remaining LED configuration."""

        config = await self._api.read_led_config(self._device_id, use_cache=False) or LEDConfig.create_default()
        setattr(config, self._field, round(value))
        if not config.validate():
            raise HomeAssistantError(translation_domain=DOMAIN, translation_key="invalid_led_configuration")
        if not await self._api.write_led_config(self._device_id, config):
            raise HomeAssistantError(translation_domain=DOMAIN, translation_key="led_update_failed")
        self._attr_native_value = int(getattr(config, self._field))
        self._attr_available = True
        self.async_write_ha_state()


CO2_CALIBRATION_TARGET = NumberEntityDescription(
    key="co2_calibration_target",
    translation_key="co2_calibration_target",
    entity_category=EntityCategory.CONFIG,
    device_class=NumberDeviceClass.CO2,
    native_unit_of_measurement=CONCENTRATION_PARTS_PER_MILLION,
    native_min_value=300,
    native_max_value=5000,
    native_step=1,
    mode=NumberMode.BOX,
)


class CasaITCO2CalibrationTarget(CasaITMultisensorEntity, RestoreNumber):
    """The reference CO2 concentration the calibrate button assumes.

    Lives in Home Assistant only; the sensor is told the value when a
    calibration actually runs.
    """

    def __init__(self, api: CasaITApi, entry: CasaITConfigEntry, device_id: str, meta: dict[str, Any]) -> None:
        """Initialize the calibration target."""

        super().__init__(api, entry, device_id, meta, CO2_CALIBRATION_TARGET, "number")
        self._attr_native_value = DEFAULT_CO2_CALIBRATION_PPM

    async def async_added_to_hass(self) -> None:
        """Restore the last target."""

        await super().async_added_to_hass()
        if (data := await self.async_get_last_number_data()) is not None and data.native_value is not None:
            self._attr_native_value = data.native_value
        self._api.multisensor.set_calibration_target(self._device_id, int(self._attr_native_value or 0))

    async def async_set_native_value(self, value: float) -> None:
        """Store a new target."""

        self._attr_native_value = value
        self._api.multisensor.set_calibration_target(self._device_id, int(value))
        self.async_write_ha_state()
