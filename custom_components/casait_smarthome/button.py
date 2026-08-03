"""Button platform for casaIT maintenance and pulse outputs."""

from __future__ import annotations

import asyncio

from homeassistant.components.button import ButtonEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import CasaITConfigEntry
from .api import CasaITApi
from .const import DOMAIN, OM117_MODE_PULSE, PCF8574_MAPPED_PORTS
from .helpers import build_bridge_slug, build_device_identifier, build_entity_id, build_i2c_entity_id, get_module_name

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

    async_add_entities(entities)


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
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, build_device_identifier(entry.entry_id, "bridge", "controller"))},
            name="casaIT bridge",
            manufacturer="casaIT",
            model="SMBus proxy",
        )

    async def async_press(self) -> None:
        """Scan all supported buses."""

        await self._api.scan_devices()


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
        )

    @property
    def available(self) -> bool:
        """Return whether the output module is available."""

        return self._address in self._api.pcf_states

    async def async_press(self) -> None:
        """Activate the output for its configured pulse duration."""

        async with self._pulse_lock:
            if not await self._api.async_write_pcf_port(self._address, self._hardware_port, 0):
                raise HomeAssistantError("Unable to activate pulse output")
            try:
                duration = self._api.om117_pair_configuration[self._address][self._pair_index].pulse_duration
                await asyncio.sleep(duration)
            finally:
                if not await self._api.async_write_pcf_port(self._address, self._hardware_port, 1):
                    raise HomeAssistantError("Unable to release pulse output")
