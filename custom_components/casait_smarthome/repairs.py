"""Repair flows for casaIT hardware and bridge issues."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant import data_entry_flow
from homeassistant.components.repairs import RepairsFlow
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr, issue_registry as ir

from .config_flow import validate_input
from .const import DOMAIN


class CasaITRepairFlow(RepairsFlow):
    """Confirm a physical repair and verify it against live hardware."""

    def __init__(self, issue_id: str, data: dict[str, Any]) -> None:
        """Initialize the repair flow."""

        self._issue_id = issue_id
        self._issue_data = data

    async def async_step_init(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> data_entry_flow.FlowResult:
        """Open the step this issue is answered with."""

        if self._issue_id.startswith("device_gone_"):
            return await self.async_step_device_gone()
        return await self.async_step_confirm(user_input)

    async def async_step_device_gone(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> data_entry_flow.FlowResult:
        """Ask whether the device should be looked for again or given up on."""

        return self.async_show_menu(
            step_id="device_gone",
            menu_options=["rescan", "forget"],
            description_placeholders=self._device_placeholders,
        )

    async def async_step_rescan(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> data_entry_flow.FlowResult:
        """Scan the bus again and close the issue once the device answers."""

        if await self._async_device_returned():
            ir.async_delete_issue(self.hass, DOMAIN, self._issue_id)
            return self.async_create_entry(title="", data={})

        return self.async_show_form(
            step_id="still_missing",
            data_schema=vol.Schema({}),
            errors={"base": "still_missing"},
            description_placeholders=self._device_placeholders,
        )

    async def async_step_still_missing(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> data_entry_flow.FlowResult:
        """Return to the choice after a scan that did not find the device."""

        return await self.async_step_device_gone()

    async def async_step_forget(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> data_entry_flow.FlowResult:
        """Remove the device from the registry once the user confirms it is gone."""

        if user_input is None:
            return self.async_show_form(
                step_id="forget",
                data_schema=vol.Schema({}),
                description_placeholders=self._device_placeholders,
            )

        self._forget_device()
        ir.async_delete_issue(self.hass, DOMAIN, self._issue_id)
        return self.async_create_entry(title="", data={})

    @property
    def _device_placeholders(self) -> dict[str, str]:
        """Return the name every step of the device_gone flow refers to."""

        identifier = str(self._issue_data.get("identifier") or "")
        return {"name": str(self._issue_data.get("name") or identifier)}

    async def _async_device_returned(self) -> bool:
        """Rescan the bus and report whether the missing device is back on it."""

        entry_id = str(self._issue_data.get("entry_id") or "")
        entry = self.hass.config_entries.async_get_entry(entry_id)
        if entry is None or entry.state is not ConfigEntryState.LOADED:
            return False

        api = entry.runtime_data
        await api.scan_devices()
        identifier = str(self._issue_data.get("identifier") or "")
        return (DOMAIN, identifier) in api.current_device_identifiers

    def _forget_device(self) -> None:
        """Drop the registry device, taking its entities and history with it."""

        identifier = str(self._issue_data.get("identifier") or "")
        device_registry = dr.async_get(self.hass)
        device = device_registry.async_get_device(identifiers={(DOMAIN, identifier)})
        if device is not None:
            device_registry.async_remove_device(device.id)

    async def async_step_confirm(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> data_entry_flow.FlowResult:
        """Verify that the hardware issue has been resolved."""

        errors: dict[str, str] = {}
        if user_input is not None:
            if await self._async_issue_resolved():
                ir.async_delete_issue(self.hass, DOMAIN, self._issue_id)
                return self.async_create_entry(title="", data={})
            errors["base"] = "still_unresolved"

        return self.async_show_form(step_id="confirm", data_schema=vol.Schema({}), errors=errors)

    async def _async_issue_resolved(self) -> bool:
        """Probe the bridge or bus and report whether the issue disappeared."""

        entry_id = str(self._issue_data.get("entry_id") or "")
        entry = self.hass.config_entries.async_get_entry(entry_id)
        if entry is None:
            return True

        if self._issue_id.startswith("bridge_unavailable_"):
            try:
                await validate_input(self.hass, dict(entry.data))
            except HomeAssistantError:
                return False
            await self.hass.config_entries.async_reload(entry.entry_id)
            return True

        if entry.state is not ConfigEntryState.LOADED:
            return False

        api = entry.runtime_data
        if self._issue_id.startswith("module_missing_"):
            await api.scan_devices()
            module = str(self._issue_data["module"]).upper()
            address = int(self._issue_data["address"])
            return address in api.found_i2c_devices.get(module, [])

        if self._issue_id.startswith("dm117_config_mismatch_"):
            address = int(self._issue_data["address"])
            slot = int(self._issue_data["slot"])
            await api.async_force_refresh()
            device = api.dm117.get(address)
            if device is None or (actual := device.last_port_types.get(slot)) is None:
                return False
            return actual.value == self._issue_data.get("expected")

        return False


async def async_create_fix_flow(
    hass: HomeAssistant,
    issue_id: str,
    data: dict[str, str | int | float | None] | None,
) -> RepairsFlow:
    """Create a repair flow for a casaIT issue."""

    return CasaITRepairFlow(issue_id, dict(data or {}))
