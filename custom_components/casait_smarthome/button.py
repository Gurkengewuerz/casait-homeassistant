"""Button platform for casaIT maintenance and pulse outputs."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import CasaITConfigEntry
from .api import CasaITApi
from .const import DOMAIN, OM117_MODE_PULSE, OW_PROFILE_MULTISENSOR, PCF8574_MAPPED_PORTS
from .entity import CasaITMultisensorEntity, raise_command_error
from .helpers import (
    build_bridge_device_info,
    build_bridge_slug,
    build_device_identifier,
    build_entity_id,
    build_i2c_entity_id,
    get_module_name,
)
from .multisensor import MultisensorCommandError

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: CasaITConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up maintenance and pulse buttons."""

    api = entry.runtime_data
    await api.async_wait_initialized()

    entities: list[ButtonEntity] = [CasaITRescanButton(api, entry)]
    for address, pairs in api.om117_pair_configuration.items():
        if address not in api.im117_om117:
            continue
        for pair_index, pair_config in pairs.items():
            if pair_config.mode != OM117_MODE_PULSE:
                continue
            entities.extend(
                CasaITPulseButton(api, entry, address, pair_index, pair_index * 2 + offset) for offset in (0, 1)
            )

    for device_id, meta in api.ow_devices.items():
        if api.onewire_profile(device_id) != OW_PROFILE_MULTISENSOR:
            continue
        parts = api.multisensor.components(device_id)
        if parts is None or not parts.stcc4:
            continue
        entities.extend(
            CasaITCO2MaintenanceButton(api, entry, device_id, meta, description) for description in CO2_BUTTONS
        )

    async_add_entities(entities)


@dataclass(kw_only=True, frozen=True)
class CO2ButtonDescription(ButtonEntityDescription):
    """Describe one STCC4 maintenance command."""

    press_fn: Callable[[CasaITApi, str], Awaitable[Any]]


async def _calibrate(api: CasaITApi, device_id: str) -> None:
    await api.multisensor.async_forced_recalibration(device_id, api.multisensor.calibration_target(device_id))


CO2_BUTTONS: tuple[CO2ButtonDescription, ...] = (
    CO2ButtonDescription(
        key="co2_calibrate",
        translation_key="co2_calibrate",
        entity_category=EntityCategory.CONFIG,
        press_fn=_calibrate,
    ),
    CO2ButtonDescription(
        key="co2_self_test",
        translation_key="co2_self_test",
        entity_category=EntityCategory.DIAGNOSTIC,
        press_fn=lambda api, device_id: api.multisensor.async_self_test(device_id),
    ),
    CO2ButtonDescription(
        key="co2_conditioning",
        translation_key="co2_conditioning",
        entity_category=EntityCategory.CONFIG,
        entity_registry_enabled_default=False,
        press_fn=lambda api, device_id: api.multisensor.async_conditioning(device_id),
    ),
    CO2ButtonDescription(
        key="co2_factory_reset",
        translation_key="co2_factory_reset",
        entity_category=EntityCategory.CONFIG,
        entity_registry_enabled_default=False,
        press_fn=lambda api, device_id: api.multisensor.async_factory_reset(device_id),
    ),
)


class CasaITCO2MaintenanceButton(CasaITMultisensorEntity, ButtonEntity):
    """Run one maintenance command on a Multisensor's STCC4."""

    entity_description: CO2ButtonDescription

    def __init__(
        self,
        api: CasaITApi,
        entry: CasaITConfigEntry,
        device_id: str,
        meta: dict[str, Any],
        description: CO2ButtonDescription,
    ) -> None:
        """Initialize the button."""

        super().__init__(api, entry, device_id, meta, description, "button")

    async def async_press(self) -> None:
        """Run the command; it takes the CO2 sensor offline for a few seconds."""

        try:
            await self.entity_description.press_fn(self._api, self._device_id)
        except MultisensorCommandError as err:
            raise_command_error(err)


class CasaITRescanButton(ButtonEntity):
    """Button that scans both the I2C and 1-Wire buses again."""

    _attr_has_entity_name = True
    _attr_translation_key = "rescan_bus"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, api: CasaITApi, entry: CasaITConfigEntry) -> None:
        """Initialize the rescan button."""

        self._api = api
        bridge_slug = build_bridge_slug(entry.entry_id, entry.unique_id)
        self._attr_unique_id = f"{entry.entry_id}_rescan_bus"
        self.entity_id = build_entity_id("button", bridge_slug, "rescan_bus")
        self._attr_device_info = build_bridge_device_info(entry.entry_id)

    async def async_press(self) -> None:
        """Scan all supported buses."""

        await self._api.async_rescan_devices()


class CasaITPulseButton(ButtonEntity):
    """Momentarily activate one OM117 output."""

    _attr_has_entity_name = True
    _attr_translation_key = "om117_pulse"

    def __init__(
        self,
        api: CasaITApi,
        entry: CasaITConfigEntry,
        address: int,
        pair_index: int,
        port: int,
    ) -> None:
        """Initialize a pulse output button."""

        self._api = api
        self._address = address
        self._pair_index = pair_index
        self._port = port
        self._hardware_port = PCF8574_MAPPED_PORTS[port]
        self._pulse_lock = asyncio.Lock()
        bridge_slug = build_bridge_slug(entry.entry_id, entry.unique_id)
        self._attr_unique_id = f"{entry.entry_id}_om117_{address}_{port}_pulse"
        self.entity_id = build_i2c_entity_id("button", bridge_slug, "om117", address, "pulse", port + 1)
        self._attr_translation_placeholders = {"port": str(port + 1)}
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, build_device_identifier(entry.entry_id, "om117", address))},
            name=get_module_name(entry.options, "om117", address, f"OM117 0x{address:02X}"),
            manufacturer="casaIT",
            model="PCF8574 Output",
            via_device=(DOMAIN, build_device_identifier(entry.entry_id, "bridge", "controller")),
        )

    @property
    def available(self) -> bool:
        """Return whether the output module is available."""

        return self._address in self._api.pcf_states

    async def async_press(self) -> None:
        """Activate the output for its configured pulse duration."""

        async with self._pulse_lock:
            if not await self._api.async_write_pcf_port(self._address, self._hardware_port, 0):
                raise HomeAssistantError(translation_domain=DOMAIN, translation_key="pulse_activate_failed")
            try:
                duration = self._api.om117_pair_configuration[self._address][self._pair_index].pulse_duration
                await asyncio.sleep(duration)
            finally:
                if not await self._api.async_write_pcf_port(self._address, self._hardware_port, 1):
                    raise HomeAssistantError(translation_domain=DOMAIN, translation_key="pulse_release_failed")
