"""Shared entity bases for dispatcher-fed 1-Wire devices."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import Entity, EntityDescription

from . import CasaITConfigEntry
from .api import CasaITApi
from .const import DOMAIN
from .helpers import build_bridge_slug, build_onewire_device_info, build_onewire_entity_id
from .multisensor import MultisensorCommandError


class CasaITMultisensorEntity(Entity):
    """An entity fed by the Multisensor sampler rather than polling on its own."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(
        self,
        api: CasaITApi,
        entry: CasaITConfigEntry,
        device_id: str,
        meta: Mapping[str, Any],
        description: EntityDescription,
        platform_domain: str,
    ) -> None:
        """Initialize the entity for one board."""

        self._api = api
        self._device_id = device_id
        self.entity_description = description
        bridge_slug = build_bridge_slug(entry.entry_id, entry.unique_id)
        self._attr_unique_id = f"{entry.entry_id}_{device_id}_{description.key}"
        self.entity_id = build_onewire_entity_id(platform_domain, bridge_slug, device_id, meta, description.key)
        self._attr_device_info = build_onewire_device_info(entry.entry_id, device_id, meta)

    async def async_added_to_hass(self) -> None:
        """Follow the board's samples."""

        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(self.hass, self._api.multisensor.signal(self._device_id), self._handle_sample)
        )
        self._update_from_sample()

    @callback
    def _handle_sample(self) -> None:
        self._update_from_sample()
        self.async_write_ha_state()

    def _update_from_sample(self) -> None:
        """Refresh the entity's attributes from the latest sample."""


class CasaITOneWireEntity(Entity):
    """Mixin for an entity fed by the 1-Wire scheduler rather than polling on its own.

    Subclasses keep their own identity attributes and only implement
    ``_update_from_value``; availability follows whether the scheduler has a
    value for the device.
    """

    _attr_should_poll = False
    _api: CasaITApi
    _device_id: str

    async def async_added_to_hass(self) -> None:
        """Follow the device's scheduled reads."""

        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(self.hass, self._api.onewire.signal(self._device_id), self._handle_value)
        )
        self._refresh()

    @callback
    def _handle_value(self) -> None:
        self._refresh()
        self.async_write_ha_state()

    def _refresh(self) -> None:
        value = self._api.onewire.value(self._device_id)
        self._attr_available = value is not None
        if value is not None:
            self._update_from_value(value)

    def _update_from_value(self, value: Any) -> None:
        """Refresh the entity's attributes from the device's latest value."""


def raise_command_error(err: MultisensorCommandError) -> None:
    """Turn a maintenance failure into a translated Home Assistant error."""

    raise HomeAssistantError(translation_domain=DOMAIN, translation_key=err.reason) from err
