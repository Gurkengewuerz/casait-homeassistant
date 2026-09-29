"""Automation triggers for push buttons on casaIT input modules.

``casait_smarthome.button`` fires when a button is pressed the way the trigger
asks - pressed, held down, double pressed, released or repeating. Its target is
one or more button event entities of this integration, or a device or area that
holds them, so an automation reads "button 3 held down" rather than matching an
event entity's attributes.
"""

from __future__ import annotations

from typing import override

import voluptuous as vol

from homeassistant.components.event import ATTR_EVENT_TYPE, DOMAIN as EVENT_DOMAIN
from homeassistant.const import CONF_OPTIONS
from homeassistant.core import HomeAssistant, State
from homeassistant.helpers import config_validation as cv, entity_registry as er
from homeassistant.helpers.automation import DomainSpec
from homeassistant.helpers.trigger import (
    ENTITY_STATE_TRIGGER_SCHEMA,
    NotTriggeredReasonReporter,
    StatelessEntityTriggerBase,
    Trigger,
    TriggerConfig,
)

from .const import BUTTON_EVENT_TYPES, DOMAIN

CONF_PRESS = "press"

BUTTON_TRIGGER_SCHEMA = ENTITY_STATE_TRIGGER_SCHEMA.extend(
    {
        vol.Required(CONF_OPTIONS): {
            vol.Required(CONF_PRESS): vol.All(cv.ensure_list, vol.Length(min=1), [vol.In(BUTTON_EVENT_TYPES)]),
        },
    }
)


class CasaITButtonTrigger(StatelessEntityTriggerBase):
    """Fire when a casaIT button reports one of the chosen presses."""

    _domain_specs = {EVENT_DOMAIN: DomainSpec()}
    _schema = BUTTON_TRIGGER_SCHEMA

    def __init__(self, hass: HomeAssistant, config: TriggerConfig) -> None:
        """Initialize the trigger with the presses it reacts to."""

        super().__init__(hass, config)
        self._presses = set(self._options[CONF_PRESS])

    @override
    def entity_filter(self, entities: set[str]) -> set[str]:
        """Keep the event entities this integration provides.

        A device or area in the target may hold event entities of other
        integrations; their event types mean something else.
        """

        registry = er.async_get(self._hass)
        return {
            entity_id
            for entity_id in super().entity_filter(entities)
            if (entry := registry.async_get(entity_id)) is not None and entry.platform == DOMAIN
        }

    @override
    def is_valid_state(self, state: State, report_not_triggered: NotTriggeredReasonReporter) -> bool:
        """Check whether the press the button just reported is one of the chosen."""

        return state.attributes.get(ATTR_EVENT_TYPE) in self._presses


TRIGGERS: dict[str, type[Trigger]] = {
    "button": CasaITButtonTrigger,
}


async def async_get_triggers(hass: HomeAssistant) -> dict[str, type[Trigger]]:
    """Return the triggers of this integration."""

    return TRIGGERS
