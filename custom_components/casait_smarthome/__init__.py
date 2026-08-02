"""The casaIT : Smart Home integration."""

from __future__ import annotations

import logging

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.typing import ConfigType

from .api import CasaITApi
from .const import CONFIG_ENTRY_VERSION, CONF_TIMEOUT, DOMAIN, PLATFORMS, SERVICE_SCAN_DEVICES
from .helpers import (
    build_device_identifier,
    get_configured_onewire_poll_intervals,
    get_configured_onewire_profiles,
    get_dm117_port_configuration,
    migrated_device_identifiers,
    migrated_entity_identity,
)
from .services.smbus_proxy import SMBus, SMBusProxyError

_LOGGER = logging.getLogger(__name__)

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


type CasaITConfigEntry = ConfigEntry[CasaITApi]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the casaIT : Smart Home component."""

    async def async_scan_devices_service(call: ServiceCall) -> None:
        """Scan for devices."""
        for entry in hass.config_entries.async_entries(DOMAIN):
            if entry.state is not ConfigEntryState.LOADED:
                continue
            # scan_devices() already performs the 1-Wire enumeration.
            await entry.runtime_data.scan_devices()

    hass.services.async_register(DOMAIN, SERVICE_SCAN_DEVICES, async_scan_devices_service, schema=vol.Schema({}))

    return True


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Migrate entity IDs to the canonical hardware-based naming scheme."""

    if entry.version > CONFIG_ENTRY_VERSION:
        _LOGGER.error(
            "Cannot migrate config entry %s from newer version %s",
            entry.entry_id,
            entry.version,
        )
        return False

    if entry.version == CONFIG_ENTRY_VERSION:
        return True

    entity_registry = er.async_get(hass)
    device_registry = dr.async_get(hass)
    migrations: list[tuple[str, str, str]] = []
    for registry_entry in er.async_entries_for_config_entry(entity_registry, entry.entry_id):
        target_identity = migrated_entity_identity(
            entry.entry_id,
            entry.unique_id,
            registry_entry.domain,
            registry_entry.unique_id,
        )
        if target_identity is None:
            continue
        target_entity_id, target_unique_id = target_identity
        if conflicting_entry := entity_registry.async_get(target_entity_id):
            if conflicting_entry.entity_id != registry_entry.entity_id:
                _LOGGER.error(
                    "Cannot migrate entity %s to %s because the target ID belongs to %s",
                    registry_entry.entity_id,
                    target_entity_id,
                    conflicting_entry.entity_id,
                )
                return False
        if conflicting_entity_id := entity_registry.async_get_entity_id(
            registry_entry.domain,
            registry_entry.platform,
            target_unique_id,
        ):
            if conflicting_entity_id != registry_entry.entity_id:
                _LOGGER.error(
                    "Cannot migrate entity %s to unique ID %s because it belongs to %s",
                    registry_entry.entity_id,
                    target_unique_id,
                    conflicting_entity_id,
                )
                return False
        if target_entity_id != registry_entry.entity_id or target_unique_id != registry_entry.unique_id:
            migrations.append((registry_entry.entity_id, target_entity_id, target_unique_id))

    device_migrations: list[tuple[str, set[tuple[str, str]]]] = []
    for device_entry in dr.async_entries_for_config_entry(device_registry, entry.entry_id):
        config_entries = getattr(device_entry, "config_entries", None)
        if config_entries and config_entries != {entry.entry_id}:
            _LOGGER.warning(
                "Skipping identifier migration for legacy device %s shared by config entries %s",
                device_entry.id,
                sorted(config_entries),
            )
            continue
        target_identifiers = migrated_device_identifiers(entry.entry_id, device_entry.identifiers)
        if target_identifiers is None:
            continue
        if conflicting_device := device_registry.async_get_device(identifiers=target_identifiers):
            if conflicting_device.id != device_entry.id:
                _LOGGER.error(
                    "Cannot migrate device %s because the target identifier belongs to %s",
                    device_entry.id,
                    conflicting_device.id,
                )
                return False
        device_migrations.append((device_entry.id, target_identifiers))

    for old_entity_id, target_entity_id, target_unique_id in migrations:
        entity_registry.async_update_entity(
            old_entity_id,
            new_entity_id=target_entity_id,
            new_unique_id=target_unique_id,
        )
    for device_id, target_identifiers in device_migrations:
        device_registry.async_update_device(device_id, new_identifiers=target_identifiers)

    hass.config_entries.async_update_entry(entry, version=CONFIG_ENTRY_VERSION)
    _LOGGER.info(
        "Migrated %s casaIT entities and %s devices to bridge-scoped identities",
        len(migrations),
        len(device_migrations),
    )
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

    if not await hass.async_add_executor_job(bus.ping):
        await hass.async_add_executor_job(bus.close)
        raise ConfigEntryNotReady("SMBus proxy did not respond to ping")

    _LOGGER.debug("Successfully connected to SMBus proxy, initializing API")

    api = CasaITApi(
        hass,
        bus,
        entry.entry_id,
        get_configured_onewire_profiles(entry.options),
        get_configured_onewire_poll_intervals(entry.options),
    )
    entry.runtime_data = api

    dm_config = get_dm117_port_configuration(entry.options)
    api.start_initialization(dm_config or None)

    _LOGGER.debug("Started casaIT initialization task")

    await api.async_wait_initialized()
    if api.initialization_error is not None:
        await hass.async_add_executor_job(api.bus.close)
        message = f"Failed to initialize casaIT devices: {api.initialization_error}"
        raise ConfigEntryNotReady(message) from api.initialization_error

    device_registry = dr.async_get(hass)
    for address in api.sm117:
        device_registry.async_get_or_create(
            config_entry_id=entry.entry_id,
            identifiers={
                (DOMAIN, build_device_identifier(entry.entry_id, "sm117", f"{address:02x}"))
            },
            name=f"SM117 0x{address:02X}",
            manufacturer="CasaIT",
            model="SM117 1-Wire bridge",
        )

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    _LOGGER.info("CasaIT : Smart Home integration setup complete")

    return True


async def async_unload_entry(hass: HomeAssistant, entry: CasaITConfigEntry) -> bool:
    """Unload a config entry."""
    api = entry.runtime_data

    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        try:
            await api.async_wait_initialized(timeout=5)
        except TimeoutError:
            _LOGGER.warning("Timeout waiting for casaIT initialization during unload; proceeding")
        await api.stop_polling()
        await hass.async_add_executor_job(api.bus.close)

    return unload_ok
