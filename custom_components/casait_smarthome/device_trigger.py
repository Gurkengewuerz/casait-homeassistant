"""Device automation triggers for casaIT IM117 push buttons."""

from __future__ import annotations

import re
from typing import Final

import voluptuous as vol

from homeassistant.components.device_automation import DEVICE_TRIGGER_BASE_SCHEMA
from homeassistant.components.homeassistant.triggers import event as event_trigger
from homeassistant.const import (
    ATTR_DEVICE_ID,
    CONF_DEVICE_ID,
    CONF_DOMAIN,
    CONF_EVENT,
    CONF_EVENT_DATA,
    CONF_PLATFORM,
    CONF_TYPE,
)
from homeassistant.core import CALLBACK_TYPE, HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.trigger import TriggerActionType, TriggerInfo
from homeassistant.helpers.typing import ConfigType

from .const import BUTTON_EVENT_TYPES, CONF_SUBTYPE, DOMAIN, EVENT_BUTTON, EVENT_DATA_EVENT_TYPE, EVENT_DATA_SUBTYPE

BUTTON_TRIGGER_SUBTYPES: Final = {f"button_{port}" for port in range(1, 9)}
BUTTON_UNIQUE_ID_PATTERN: Final = re.compile(r"_im117_\d+_(\d+)_button$")

TRIGGER_SCHEMA: Final = DEVICE_TRIGGER_BASE_SCHEMA.extend(
    {
        vol.Required(CONF_TYPE): vol.In(BUTTON_EVENT_TYPES),
        vol.Required(CONF_SUBTYPE): vol.In(BUTTON_TRIGGER_SUBTYPES),
    }
)


async def async_get_triggers(hass: HomeAssistant, device_id: str) -> list[dict[str, str]]:
    """Return the triggers exposed by configured IM117 button entities."""

    entity_registry = er.async_get(hass)
    subtypes = {
        f"button_{int(match.group(1)) + 1}"
        for entry in er.async_entries_for_device(entity_registry, device_id)
        if entry.domain == "event"
        and entry.platform == DOMAIN
        and (match := BUTTON_UNIQUE_ID_PATTERN.search(entry.unique_id)) is not None
    }
    return [
        {
            CONF_PLATFORM: "device",
            CONF_DEVICE_ID: device_id,
            CONF_DOMAIN: DOMAIN,
            CONF_TYPE: event_type,
            CONF_SUBTYPE: subtype,
        }
        for subtype in sorted(subtypes)
        for event_type in BUTTON_EVENT_TYPES
    ]


async def async_attach_trigger(
    hass: HomeAssistant,
    config: ConfigType,
    action: TriggerActionType,
    trigger_info: TriggerInfo,
) -> CALLBACK_TYPE:
    """Attach a device trigger to the integration's button event."""

    event_config = event_trigger.TRIGGER_SCHEMA(
        {
            CONF_PLATFORM: CONF_EVENT,
            event_trigger.CONF_EVENT_TYPE: EVENT_BUTTON,
            CONF_EVENT_DATA: {
                ATTR_DEVICE_ID: config[CONF_DEVICE_ID],
                EVENT_DATA_EVENT_TYPE: config[CONF_TYPE],
                EVENT_DATA_SUBTYPE: config[CONF_SUBTYPE],
            },
        }
    )
    return await event_trigger.async_attach_trigger(
        hass,
        event_config,
        action,
        trigger_info,
        platform_type="device",
    )
