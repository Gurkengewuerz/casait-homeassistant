"""Diagnostics support for casaIT : Smart Home."""

from __future__ import annotations

from typing import Any

from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant
from homeassistant.helpers.redact import async_redact_data

from . import CasaITConfigEntry
from .firmware import get_firmware_recovery

TO_REDACT = {CONF_HOST, CONF_PORT}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant,
    entry: CasaITConfigEntry,
) -> dict[str, Any]:
    """Return redacted diagnostics for a config entry."""

    if (recovery := get_firmware_recovery(hass, entry.entry_id)) is not None:
        return {
            "entry_data": async_redact_data(dict(entry.data), TO_REDACT),
            "firmware_recovery": {"firmware": recovery.firmware_version},
        }
    api = entry.runtime_data
    return {
        "entry_data": async_redact_data(dict(entry.data), TO_REDACT),
        **api.diagnostic_data,
        "bus_topology": api.bus_topology,
    }
