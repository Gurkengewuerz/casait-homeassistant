"""Diagnostics support for casaIT : Smart Home."""

from __future__ import annotations

from typing import Any

from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant
from homeassistant.helpers.redact import async_redact_data

from . import CasaITConfigEntry

TO_REDACT = {CONF_HOST, CONF_PORT}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant,
    entry: CasaITConfigEntry,
) -> dict[str, Any]:
    """Return redacted diagnostics for a config entry."""

    api = entry.runtime_data
    return {
        "entry_data": async_redact_data(dict(entry.data), TO_REDACT),
        **api.diagnostic_data,
        "bus_topology": api.bus_topology,
    }
