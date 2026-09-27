"""The casaIT : Smart Home integration."""

from __future__ import annotations

import logging

import voluptuous as vol

from homeassistant.components.cover import DOMAIN as COVER_DOMAIN
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.exceptions import ConfigEntryNotReady, HomeAssistantError, ServiceValidationError
from homeassistant.helpers import (
    config_validation as cv,
    device_registry as dr,
    entity_registry as er,
    issue_registry as ir,
    service,
)
from homeassistant.helpers.typing import ConfigType

from .api import CasaITApi
from .const import (
    CONF_TIMEOUT,
    CONFIG_ENTRY_VERSION,
    DOMAIN,
    PLATFORMS,
    SERVICE_CALIBRATE_CO2,
    SERVICE_REFERENCE_RUN,
    SERVICE_SCAN_DEVICES,
    SERVICE_SET_LED_PALETTE,
)
from .helpers import (
    build_device_identifier,
    get_configured_module_addresses,
    get_configured_onewire_poll_intervals,
    get_configured_onewire_profiles,
    get_dm117_port_configuration,
    get_input_module_settings,
    get_module_name,
    get_om117_pair_configuration,
    get_onewire_names,
    get_polling_settings,
    get_power_on_policies,
    get_topology_settings,
    migrate_options_to_nested,
    migrated_device_identifiers,
    migrated_entity_identity,
)
from .multisensor import MultisensorCommandError
from .services.i2cClasses.led_controller import Color, LEDConfig
from .services.smbus_proxy import SMBus, SMBusProxyError

_LOGGER = logging.getLogger(__name__)

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)
SETUP_FAILURES_KEY = "setup_failures"
BRIDGE_REPAIR_THRESHOLD = 3


type CasaITConfigEntry = ConfigEntry[CasaITApi]

RGB_COLOR_SCHEMA = vol.All(
    cv.ensure_list,
    [vol.All(vol.Coerce(int), vol.Range(min=0, max=255))],
    vol.Length(min=3, max=3),
)
SET_LED_PALETTE_SCHEMA = vol.Schema(
    {
        vol.Required("device_id"): cv.string,
        vol.Required("color_1"): RGB_COLOR_SCHEMA,
        vol.Optional("color_2"): RGB_COLOR_SCHEMA,
        vol.Optional("color_3"): RGB_COLOR_SCHEMA,
        vol.Optional("color_4"): RGB_COLOR_SCHEMA,
        vol.Optional("color_5"): RGB_COLOR_SCHEMA,
    }
)
CALIBRATE_CO2_SCHEMA = vol.Schema(
    {
        vol.Required("device_id"): cv.string,
        vol.Optional("target_ppm"): vol.All(vol.Coerce(int), vol.Range(min=300, max=5000)),
    }
)


def _resolve_onewire_target(hass: HomeAssistant, target: str) -> tuple[CasaITApi, str] | None:
    """Find the bridge and ROM ID behind a service target.

    The target is a Home Assistant device ID, as the device picker sends it, or
    a bare 1-Wire ROM ID for scripts written before the picker existed.
    """

    loaded = [entry for entry in hass.config_entries.async_entries(DOMAIN) if entry.state is ConfigEntryState.LOADED]
    if (device := dr.async_get(hass).async_get(target)) is not None:
        for entry in loaded:
            prefix = build_device_identifier(entry.entry_id, "onewire", "")
            for domain, identifier in device.identifiers:
                if domain == DOMAIN and identifier.startswith(prefix):
                    return entry.runtime_data, identifier.removeprefix(prefix)
        return None
    rom_id = target.strip().lower()
    return next(((entry.runtime_data, rom_id) for entry in loaded if rom_id in entry.runtime_data.ow_devices), None)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the casaIT : Smart Home component."""

    hass.data.setdefault(DOMAIN, {}).setdefault(SETUP_FAILURES_KEY, {})

    async def async_scan_devices_service(call: ServiceCall) -> None:
        """Scan for devices."""
        for entry in hass.config_entries.async_entries(DOMAIN):
            if entry.state is not ConfigEntryState.LOADED:
                continue
            # Reload after enumeration so newly discovered or removed hardware
            # is reflected by every entity platform immediately.
            await entry.runtime_data.async_rescan_devices()

    async def async_set_led_palette_service(call: ServiceCall) -> None:
        """Write up to five colors to one DS28E17 LED controller."""

        if (resolved := _resolve_onewire_target(hass, call.data["device_id"])) is None:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="led_controller_unavailable",
                translation_placeholders={"device_id": call.data["device_id"]},
            )
        api, device_id = resolved

        config = await api.read_led_config(device_id, use_cache=False) or LEDConfig.create_default()
        colors = [Color(*call.data[f"color_{index}"]) for index in range(1, 6) if f"color_{index}" in call.data]
        while len(colors) < 5:
            colors.append(Color(0, 0, 0))
        config.colors = colors
        if not config.validate():
            raise ServiceValidationError(translation_domain=DOMAIN, translation_key="invalid_led_palette")
        if not await api.write_led_config(device_id, config):
            raise HomeAssistantError(translation_domain=DOMAIN, translation_key="led_palette_update_failed")

    async def async_calibrate_co2_service(call: ServiceCall) -> ServiceResponse:
        """Run a forced recalibration of one Multisensor's CO2 sensor."""

        resolved = _resolve_onewire_target(hass, call.data["device_id"])
        if resolved is None or (parts := resolved[0].multisensor.components(resolved[1])) is None or not parts.stcc4:
            raise ServiceValidationError(translation_domain=DOMAIN, translation_key="co2_sensor_unavailable")
        api, device_id = resolved
        target = call.data.get("target_ppm", api.multisensor.calibration_target(device_id))
        try:
            correction = await api.multisensor.async_forced_recalibration(device_id, target)
        except MultisensorCommandError as err:
            raise HomeAssistantError(translation_domain=DOMAIN, translation_key=err.reason) from err
        return {"correction_ppm": correction}

    if not hass.services.has_service(DOMAIN, SERVICE_CALIBRATE_CO2):
        hass.services.async_register(
            DOMAIN,
            SERVICE_CALIBRATE_CO2,
            async_calibrate_co2_service,
            schema=CALIBRATE_CO2_SCHEMA,
            supports_response=SupportsResponse.OPTIONAL,
        )
    if not hass.services.has_service(DOMAIN, SERVICE_SCAN_DEVICES):
        hass.services.async_register(DOMAIN, SERVICE_SCAN_DEVICES, async_scan_devices_service, schema=vol.Schema({}))
    if not hass.services.has_service(DOMAIN, SERVICE_SET_LED_PALETTE):
        hass.services.async_register(
            DOMAIN,
            SERVICE_SET_LED_PALETTE,
            async_set_led_palette_service,
            schema=SET_LED_PALETTE_SCHEMA,
        )
    service.async_register_platform_entity_service(
        hass,
        DOMAIN,
        SERVICE_REFERENCE_RUN,
        entity_domain=COVER_DOMAIN,
        schema={vol.Optional("return_to_position", default=True): cv.boolean},
        func="async_reference_run",
    )

    return True


def _record_bridge_setup_failure(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Count setup failures and create a repair issue after repeated outages."""

    failures: dict[str, int] = hass.data.setdefault(DOMAIN, {}).setdefault(SETUP_FAILURES_KEY, {})
    failures[entry.entry_id] = failures.get(entry.entry_id, 0) + 1
    if failures[entry.entry_id] < BRIDGE_REPAIR_THRESHOLD:
        return
    ir.async_create_issue(
        hass,
        DOMAIN,
        f"bridge_unavailable_{entry.entry_id}",
        data={"entry_id": entry.entry_id},
        is_fixable=True,
        is_persistent=True,
        severity=ir.IssueSeverity.ERROR,
        translation_key="bridge_unavailable",
    )


def _clear_bridge_setup_failure(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Clear setup failure state after the bridge recovers."""

    failures: dict[str, int] = hass.data.setdefault(DOMAIN, {}).setdefault(SETUP_FAILURES_KEY, {})
    failures.pop(entry.entry_id, None)
    ir.async_delete_issue(hass, DOMAIN, f"bridge_unavailable_{entry.entry_id}")


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Migrate a config entry to the current version."""

    if entry.version > CONFIG_ENTRY_VERSION:
        _LOGGER.error(
            "Cannot migrate config entry %s from newer version %s",
            entry.entry_id,
            entry.version,
        )
        return False

    if entry.version == CONFIG_ENTRY_VERSION:
        return True

    # v1 -> v2 rewrote entity and device identifiers to be bridge-scoped.
    if entry.version < 2 and not _migrate_entity_identities(hass, entry):
        return False

    # v2 -> v3 replaced the flat option namespace with nested sections.
    options = migrate_options_to_nested(entry.options) if entry.version < 3 else dict(entry.options)

    hass.config_entries.async_update_entry(entry, options=options, version=CONFIG_ENTRY_VERSION)
    _LOGGER.info(
        "Migrated casaIT config entry %s from version %s to %s",
        entry.entry_id,
        entry.version,
        CONFIG_ENTRY_VERSION,
    )
    return True


def _migrate_entity_identities(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Rewrite legacy entity and device identifiers to the bridge-scoped scheme."""

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
        conflicting_device = next(
            (
                found
                for identifier in target_identifiers
                if (found := device_registry.async_get_device_by_identifier(identifier, entry.entry_id)) is not None
            ),
            None,
        )
        if conflicting_device is not None:
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

    _LOGGER.info(
        "Migrated %s casaIT entities and %s devices to bridge-scoped identities",
        len(migrations),
        len(device_migrations),
    )
    return True


async def async_setup_entry(hass: HomeAssistant, entry: CasaITConfigEntry) -> bool:
    """Set up casaIT : Smart Home from a config entry."""
    polling_settings = get_polling_settings(entry.options)
    try:
        bus = await SMBus.connect(
            entry.data[CONF_HOST],
            entry.data[CONF_PORT],
            entry.data.get(CONF_TIMEOUT),
            polling_settings.max_send_interval,
        )
    except (SMBusProxyError, OSError) as err:
        _record_bridge_setup_failure(hass, entry)
        raise ConfigEntryNotReady(f"Failed to connect to SMBus proxy: {err}") from err

    try:
        responded = await bus.ping()
    except (SMBusProxyError, OSError) as err:
        await bus.close()
        _record_bridge_setup_failure(hass, entry)
        raise ConfigEntryNotReady(f"Failed to ping SMBus proxy: {err}") from err
    if not responded:
        await bus.close()
        _record_bridge_setup_failure(hass, entry)
        raise ConfigEntryNotReady("SMBus proxy did not respond to ping")

    _clear_bridge_setup_failure(hass, entry)

    _LOGGER.debug("Successfully connected to SMBus proxy, initializing API")

    api = CasaITApi(
        hass,
        bus,
        entry.entry_id,
        get_configured_onewire_profiles(entry.options),
        get_configured_onewire_poll_intervals(entry.options),
        get_om117_pair_configuration(entry.options),
        polling_settings.fast_poll_interval,
        polling_settings.slow_poll_interval,
        get_configured_module_addresses(entry.options),
        input_debounce_ms={
            module_kind: {
                address: settings.debounce_ms
                for address, settings in get_input_module_settings(entry.options, module_kind).items()
            }
            for module_kind in ("im117", "dm117")
        },
        topology_settings=get_topology_settings(entry.options),
        onewire_names=get_onewire_names(entry.options),
        power_on_policies=get_power_on_policies(entry.options),
    )
    entry.runtime_data = api

    dm_config = get_dm117_port_configuration(entry.options)
    api.start_initialization(dm_config or None)

    _LOGGER.debug("Started casaIT initialization task")

    await api.async_wait_initialized()
    if api.initialization_error is not None:
        await api.bus.close()
        message = f"Failed to initialize casaIT devices: {api.initialization_error}"
        raise ConfigEntryNotReady(message) from api.initialization_error

    device_registry = dr.async_get(hass)
    bridge_identifier = (DOMAIN, build_device_identifier(entry.entry_id, "bridge", "controller"))
    device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={bridge_identifier},
        name="casaIT bridge",
        manufacturer="casaIT",
        model="SMBus proxy",
    )
    for address in api.sm117:
        device_registry.async_get_or_create(
            config_entry_id=entry.entry_id,
            identifiers={(DOMAIN, build_device_identifier(entry.entry_id, "sm117", f"{address:02x}"))},
            name=get_module_name(entry.options, "sm117", address, f"SM117 0x{address:02X}"),
            manufacturer="CasaIT",
            model="SM117 1-Wire bridge",
            via_device=bridge_identifier,
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
        await api.bus.close()

    return unload_ok


async def async_remove_config_entry_device(
    hass: HomeAssistant,
    config_entry: CasaITConfigEntry,
    device_entry: dr.DeviceEntry,
) -> bool:
    """Allow removing a device only when it is absent from the latest scan."""

    return not any(
        identifier in config_entry.runtime_data.current_device_identifiers for identifier in device_entry.identifiers
    )
