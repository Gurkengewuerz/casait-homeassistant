"""Support for casaIT PCF8574 binary sensors."""

from __future__ import annotations

from datetime import timedelta
import logging
from typing import Any

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import CasaITConfigEntry
from .api import CasaITApi
from .const import DOMAIN, DS2413_CHANNEL_INPUT, INPUT_ROLE_CONTACT, PCF8574_MAPPED_PORTS
from .helpers import (
    DigitalInputConfig,
    build_bridge_slug,
    build_device_identifier,
    build_entity_id,
    build_i2c_entity_id,
    build_onewire_device_info,
    build_onewire_entity_id,
    default_onewire_profile,
    get_address_range,
    get_configured_ds2413_channels,
    get_configured_onewire_profiles,
    get_dm117_port_configuration,
    get_im117_port_configuration,
    get_module_name,
)
from .services.i2cClasses.dm117 import DeviceType, PortConfig

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 1
SCAN_INTERVAL = timedelta(seconds=1)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: CasaITConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the casaIT binary sensors."""
    api: CasaITApi = config_entry.runtime_data

    await api.async_wait_initialized()

    pcf_entities: list[BinarySensorEntity] = []
    if (input_range := get_address_range("IM117")) is not None:
        port_config = get_im117_port_configuration(config_entry.options)
        for addr in api.im117_om117:
            if not input_range[0] <= addr <= input_range[1]:
                continue
            configured = port_config.get(addr, {})
            for port in range(8):
                # Ports default to a plain binary sensor, which is what every
                # port used to be before roles existed.
                config = configured.get(port, DigitalInputConfig())
                if config.role != INPUT_ROLE_CONTACT:
                    continue
                pcf_entities.append(CasaITBinarySensor(api, config_entry, addr, port, config))

    dm_entities: list[CasaITDM117BinarySensor] = []
    dm_config = get_dm117_port_configuration(config_entry.options)
    for addr, slots in dm_config.items():
        if addr not in api.dm117:
            continue
        for port, device_type in slots.items():
            if device_type is not DeviceType.INPUT:
                continue
            dm_entities.append(CasaITDM117BinarySensor(api, config_entry, addr, port, 0))
            dm_entities.append(CasaITDM117BinarySensor(api, config_entry, addr, port, 1))

    ds2413_entities: list[BinarySensorEntity] = []
    configured_profiles = get_configured_onewire_profiles(config_entry.options)
    configured_channels = get_configured_ds2413_channels(config_entry.options)

    for device_id, meta in api.ow_devices.items():
        profile = configured_profiles.get(device_id) or default_onewire_profile(meta)
        if profile not in {"ds2413", "ds2413_in", "ds2413_out"}:
            continue
        fallback = DS2413_CHANNEL_INPUT if profile != "ds2413_out" else "output"
        channel_roles = configured_channels.get(device_id, {0: fallback, 1: fallback})
        ds2413_entities.extend(
            CasaITDS2413BinarySensor(api, config_entry, device_id, channel, meta)
            for channel, role in channel_roles.items()
            if role == DS2413_CHANNEL_INPUT
        )

    async_add_entities([CasaITBridgeConnectionSensor(api, config_entry), *pcf_entities, *dm_entities, *ds2413_entities])


class CasaITBridgeConnectionSensor(BinarySensorEntity):
    """Report the current TCP bridge connection state."""

    _attr_has_entity_name = True
    _attr_should_poll = True
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_translation_key = "bridge_connection"

    def __init__(self, api: CasaITApi, entry: CasaITConfigEntry) -> None:
        """Initialize the bridge connection sensor."""

        self._api = api
        bridge_slug = build_bridge_slug(entry.entry_id, entry.unique_id)
        self._attr_unique_id = f"{entry.entry_id}_bridge_connection"
        self.entity_id = build_entity_id("binary_sensor", bridge_slug, "connection")
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, build_device_identifier(entry.entry_id, "bridge", "controller"))},
            name="casaIT bridge",
            manufacturer="casaIT",
            model="SMBus proxy",
        )

    async def async_update(self) -> None:
        """Read the transport connection flag without performing I/O."""

        self._attr_is_on = bool(self._api.bus.stats["connected"])
        self._attr_available = True


class CasaITBinarySensor(BinarySensorEntity):
    """Representation of a casaIT binary sensor."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_translation_key = "im117_input"

    def __init__(
        self,
        api: CasaITApi,
        config_entry: CasaITConfigEntry,
        address: int,
        port: int,
        config: DigitalInputConfig | None = None,
    ) -> None:
        """Initialize the binary sensor."""
        self._api = api
        self._address = address
        self._port = port
        self._hardware_port = PCF8574_MAPPED_PORTS[port]
        if config is not None and config.device_class:
            self._attr_device_class = BinarySensorDeviceClass(config.device_class)
        bridge_slug = build_bridge_slug(config_entry.entry_id, config_entry.unique_id)
        self._attr_unique_id = f"{config_entry.entry_id}_im117_{address}_{port}"
        self.entity_id = build_i2c_entity_id("binary_sensor", bridge_slug, "im117", address, "input", port + 1)
        self._attr_translation_placeholders = {"port": str(port + 1)}
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, build_device_identifier(config_entry.entry_id, "im117", address))},
            name=get_module_name(config_entry.options, "im117", address, f"IM117 0x{address:02X}"),
            manufacturer="casaIT",
            model="PCF8574 Input",
            via_device=(DOMAIN, build_device_identifier(config_entry.entry_id, "bridge", "controller")),
        )
        self._update_state()

    def _update_state(self) -> None:
        """Update the state of the sensor."""
        if self._address in self._api.pcf_states:
            states = self._api.pcf_states[self._address]
            if states is not None and 0 <= self._hardware_port < len(states):
                self._attr_is_on = states[self._hardware_port] == 0  # Inverted logic for PCF8574 inputs
            else:
                self._attr_is_on = None
        else:
            self._attr_is_on = None

    @callback
    def _handle_state_update(self) -> None:
        """Handle updated data from shared poller."""
        self._update_state()
        self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        """Register callbacks when entity is added to hass."""
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(self.hass, self._api.address_signal(self._address), self._handle_state_update)
        )

    @property
    def available(self) -> bool:
        """Return if entity is available."""
        return self._address in self._api.pcf_states


class CasaITDM117BinarySensor(BinarySensorEntity):
    """Binary sensor representing a DM117 digital input port."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_translation_key = "dm117_input"

    def __init__(
        self,
        api: CasaITApi,
        config_entry: CasaITConfigEntry,
        address: int,
        port: int,
        channel: int,
    ) -> None:
        """Initialize the DM117 binary sensor."""

        self._api = api
        self._address = address
        self._port = port
        self._slot = port + 1
        self._channel = channel  # 0 for port A, 1 for port B
        bridge_slug = build_bridge_slug(config_entry.entry_id, config_entry.unique_id)
        self._attr_unique_id = f"{config_entry.entry_id}_dm117_{address}_{port}_input_{channel}"
        channel_name = "A" if channel == 0 else "B"
        self.entity_id = build_i2c_entity_id(
            "binary_sensor", bridge_slug, "dm117", address, "slot", self._slot, "input", channel_name
        )
        self._attr_translation_placeholders = {"slot": str(self._slot), "channel": channel_name}
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, build_device_identifier(config_entry.entry_id, "dm117", address))},
            name=get_module_name(config_entry.options, "dm117", address, f"DM117 0x{address:02X}"),
            manufacturer="casaIT",
            model="DM117",
            via_device=(DOMAIN, build_device_identifier(config_entry.entry_id, "bridge", "controller")),
        )
        self._update_state()

    def _update_state(self) -> None:
        states = self._api.dm117_states.get(self._address)
        if not states or self._port not in states:
            self._attr_is_on = None
            return

        raw_value = states[self._port]
        # DM117 input responses use physical D/C for bits 0/1, while output responses
        # use A/B. The installed wiring deliberately compensates for that firmware
        # asymmetry, so the logical channel order here must remain unchanged.
        port_config = PortConfig.from_raw(raw_value)
        value = port_config.port_a if self._channel == 0 else port_config.port_b
        self._attr_is_on = bool(value)

    @callback
    def _handle_state_update(self) -> None:
        self._update_state()
        self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        """Register callbacks when entity is added to hass."""
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(self.hass, self._api.address_signal(self._address), self._handle_state_update)
        )

    @property
    def available(self) -> bool:
        """Return if entity is available."""
        return self._address in self._api.dm117_states


class CasaITDS2413BinarySensor(BinarySensorEntity):
    """Binary sensor for DS2413 channels configured as inputs."""

    _attr_has_entity_name = True
    _attr_should_poll = True
    _attr_translation_key = "ds2413_input"

    def __init__(
        self,
        api: CasaITApi,
        config_entry: CasaITConfigEntry,
        device_id: str,
        channel: int,
        meta: dict[str, Any],
    ) -> None:
        """Initialize the DS2413 binary sensor."""

        self._api = api
        self._device_id = device_id
        self._channel = channel
        self._meta = meta
        channel_name = "A" if channel == 0 else "B"
        bridge_slug = build_bridge_slug(config_entry.entry_id, config_entry.unique_id)
        self._attr_unique_id = f"{config_entry.entry_id}_{device_id}_channel_{channel}_input"
        self.entity_id = build_onewire_entity_id("binary_sensor", bridge_slug, device_id, meta, "input", channel_name)
        self._attr_translation_placeholders = {"channel": channel_name}
        self._attr_device_info = build_onewire_device_info(config_entry.entry_id, device_id, meta)

    async def async_update(self) -> None:
        """Poll the DS2413 input state."""

        self._attr_available = False
        state = await self._api.read_ds2413_state(self._device_id, self._channel)
        if state is None:
            return
        self._attr_is_on = state
        self._attr_available = True
