"""The casaIT : Smart Home integration."""

from __future__ import annotations

import logging

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.typing import ConfigType

from .api import CasaITApi
from .const import CONF_TIMEOUT, DOMAIN, PLATFORMS, SERVICE_SCAN_DEVICES
from .helpers import get_dm117_port_configuration
from .services.smbus_proxy import SMBus, SMBusProxyError

_LOGGER = logging.getLogger(__name__)

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


type CasaITConfigEntry = ConfigEntry[CasaITApi]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the casaIT : Smart Home component."""

    async def async_scan_devices_service(call: ServiceCall) -> None:
        """Scan for devices."""
        for entry in hass.config_entries.async_entries(DOMAIN):
            api = entry.runtime_data
            await api.scan_devices()
            await api.scan_onewire()

    hass.services.async_register(DOMAIN, SERVICE_SCAN_DEVICES, async_scan_devices_service, schema=vol.Schema({}))

    return True


async def async_setup_entry(hass: HomeAssistant, entry: CasaITConfigEntry) -> bool:
    """Set up casaIT : Smart Home from a config entry."""
    try:
        bus = await hass.async_add_executor_job(
            SMBus,
            1,
            entry.data[CONF_HOST],
            entry.data[CONF_PORT],
            entry.data.get(CONF_TIMEOUT),
        )
    except SMBusProxyError as e:
        raise ConfigEntryNotReady(f"Failed to connect to SMBus proxy: {e}") from e

    _LOGGER.debug("Successfully connected to SMBus proxy, initializing API")

    api = CasaITApi(hass, bus)
    entry.runtime_data = api

    dm_config = get_dm117_port_configuration(entry.options)
    api.start_initialization(dm_config or None)

    _LOGGER.debug("Started casaIT initialization task")

    async def _finish_platform_setup() -> None:
        await api.async_wait_initialized()
        try:
            await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
        except Exception:
            _LOGGER.exception("Error setting up casaIT platforms")

    setup_task = hass.async_create_background_task(_finish_platform_setup(), "casait_forward_entry_setups")

    def _cancel_setup_task() -> None:
        setup_task.cancel()

    entry.async_on_unload(_cancel_setup_task)

    _LOGGER.info("CasaIT : Smart Home integration setup complete")

    return True


async def async_unload_entry(hass: HomeAssistant, entry: CasaITConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        api = entry.runtime_data
        try:
            await api.async_wait_initialized(timeout=5)
        except TimeoutError:
            _LOGGER.warning("Timeout waiting for casaIT initialization during unload; proceeding")
        await api.stop_polling()
        await hass.async_add_executor_job(api.bus.close)

    return unload_ok
