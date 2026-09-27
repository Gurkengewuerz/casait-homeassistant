"""Config flow for the casaIT : Smart Home integration."""

from __future__ import annotations

from collections.abc import Mapping
import contextlib
import logging
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntry, ConfigFlowResult, OptionsFlow
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    TextSelector,
    TextSelectorConfig,
)
from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo

from .const import CONF_TIMEOUT, CONFIG_ENTRY_VERSION, DOMAIN
from .options_flow import OptionsFlowHandler
from .services.smbus_proxy import DEFAULT_PORT, DEFAULT_TIMEOUT, BridgeFirmwareError, SMBus, SMBusProxyError

_LOGGER = logging.getLogger(__name__)


def _bridge_data_schema(defaults: Mapping[str, Any] | None = None) -> vol.Schema:
    """Return the bridge connection schema with optional current values."""

    current = defaults or {}
    host_key = vol.Required(CONF_HOST, default=current[CONF_HOST]) if CONF_HOST in current else vol.Required(CONF_HOST)
    return vol.Schema(
        {
            host_key: vol.All(TextSelector(TextSelectorConfig()), vol.Length(min=1)),
            vol.Required(CONF_PORT, default=current.get(CONF_PORT, DEFAULT_PORT)): NumberSelector(
                NumberSelectorConfig(min=1, max=65535, step=1, mode=NumberSelectorMode.BOX)
            ),
            vol.Required(CONF_TIMEOUT, default=current.get(CONF_TIMEOUT, DEFAULT_TIMEOUT)): NumberSelector(
                NumberSelectorConfig(min=0.1, max=60, step=0.1, mode=NumberSelectorMode.BOX)
            ),
        }
    )


def _normalize_bridge_data(data: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize selector values before validation and storage."""

    return {
        CONF_HOST: str(data[CONF_HOST]).strip(),
        CONF_PORT: int(data[CONF_PORT]),
        CONF_TIMEOUT: float(data[CONF_TIMEOUT]),
    }


async def validate_input(hass: HomeAssistant, data: dict[str, Any]) -> dict[str, Any]:
    """Validate the user input allows us to connect.

    Data contains the normalized bridge connection values.
    """
    bus: SMBus | None = None
    try:
        bus = await SMBus.connect(data[CONF_HOST], data[CONF_PORT], data[CONF_TIMEOUT])
        if await bus.ping_info() is None:
            raise CannotConnect
    except BridgeFirmwareError as exc:
        raise FirmwareOutdated from exc
    except (SMBusProxyError, OSError) as exc:
        raise CannotConnect from exc
    finally:
        if bus is not None:
            with contextlib.suppress(SMBusProxyError, OSError):
                await bus.close()

    return {"title": data[CONF_HOST]}


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for casaIT : Smart Home."""

    VERSION = CONFIG_ENTRY_VERSION

    def __init__(self) -> None:
        """Initialize the config flow."""

        super().__init__()
        self._discovered_host: str | None = None
        self._discovered_port: int = DEFAULT_PORT
        self._discovered_timeout: float = DEFAULT_TIMEOUT
        self._discovered_name: str | None = None

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Create the options flow."""
        return OptionsFlowHandler()

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Handle the initial step."""
        errors: dict[str, str] = {}
        if user_input is not None:
            data = _normalize_bridge_data(user_input)
            self._async_abort_entries_match(
                {
                    CONF_HOST: data[CONF_HOST],
                    CONF_PORT: data[CONF_PORT],
                }
            )
            try:
                info = await validate_input(self.hass, data)
            except FirmwareOutdated:
                errors["base"] = "firmware_outdated"
            except CannotConnect:
                errors["base"] = "cannot_connect"
            except Exception:
                _LOGGER.exception("Unexpected exception")
                errors["base"] = "unknown"
            else:
                return self.async_create_entry(title=info["title"], data=data)

        return self.async_show_form(step_id="user", data_schema=_bridge_data_schema(), errors=errors)

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Allow the bridge connection settings to be changed in place."""

        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            data = _normalize_bridge_data(user_input)
            self._async_abort_entries_match({CONF_HOST: data[CONF_HOST], CONF_PORT: data[CONF_PORT]})
            try:
                info = await validate_input(self.hass, data)
            except FirmwareOutdated:
                errors["base"] = "firmware_outdated"
            except CannotConnect:
                errors["base"] = "cannot_connect"
            except Exception:
                _LOGGER.exception("Unexpected exception")
                errors["base"] = "unknown"
            else:
                if entry.unique_id is not None:
                    await self.async_set_unique_id(entry.unique_id)
                    self._abort_if_unique_id_mismatch()
                return self.async_update_reload_and_abort(entry, title=info["title"], data=data)

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=_bridge_data_schema(entry.data),
            errors=errors,
        )

    @staticmethod
    def _decode_property_value(value: Any) -> str | None:
        """Decode zeroconf property values that may be bytes."""

        if isinstance(value, bytes):
            try:
                return value.decode()
            except UnicodeDecodeError:
                return None
        if isinstance(value, str):
            return value
        return None

    async def async_step_zeroconf(self, discovery_info: ZeroconfServiceInfo) -> ConfigFlowResult:
        """Handle zeroconf discovery."""

        host = discovery_info.host or None
        ip_address = getattr(discovery_info, "ip_address", None)
        ip_addresses = getattr(discovery_info, "ip_addresses", None)
        if host is None and ip_address is not None:
            host = str(ip_address)
        if host is None and ip_addresses:
            host = str(ip_addresses[0])
        if host is None:
            return self.async_abort(reason="cannot_connect")

        port = discovery_info.port or DEFAULT_PORT
        self._discovered_host = host
        self._discovered_port = port
        self._discovered_timeout = DEFAULT_TIMEOUT
        self._discovered_name = discovery_info.name.rstrip(".") if discovery_info.name else host
        self.context["title_placeholders"] = {"name": self._discovered_name}

        properties = discovery_info.properties or {}
        unique_id = None
        for key in ("id", "unique_id", "uid", "serial", "deviceid", "mac"):
            unique_id = self._decode_property_value(properties.get(key))
            if unique_id:
                break

        self._async_abort_entries_match({CONF_HOST: host, CONF_PORT: port})

        if unique_id:
            await self.async_set_unique_id(unique_id)
            self._abort_if_unique_id_configured(updates={CONF_HOST: host, CONF_PORT: port})
        else:
            await self._async_handle_discovery_without_unique_id()

        return await self.async_step_zeroconf_confirm()

    async def async_step_zeroconf_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Confirm zeroconf discovery."""

        if self._discovered_host is None:
            return self.async_abort(reason="unknown")

        errors: dict[str, str] = {}
        data_schema = _bridge_data_schema(
            {
                CONF_HOST: self._discovered_host,
                CONF_PORT: self._discovered_port,
                CONF_TIMEOUT: self._discovered_timeout,
            }
        )

        if user_input is not None:
            data = _normalize_bridge_data(user_input)
            try:
                info = await validate_input(self.hass, data)
            except FirmwareOutdated:
                errors["base"] = "firmware_outdated"
            except CannotConnect:
                errors["base"] = "cannot_connect"
            except Exception:
                _LOGGER.exception("Unexpected exception")
                errors["base"] = "unknown"
            else:
                return self.async_create_entry(title=info["title"], data=data)

        return self.async_show_form(
            step_id="zeroconf_confirm",
            data_schema=data_schema,
            errors=errors,
            description_placeholders={"host": self._discovered_host},
        )


class CannotConnect(HomeAssistantError):
    """Error to indicate we cannot connect."""


class FirmwareOutdated(HomeAssistantError):
    """Error to indicate the bridge runs firmware this integration cannot work with."""
