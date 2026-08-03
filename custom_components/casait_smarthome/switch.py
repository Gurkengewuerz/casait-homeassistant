"""Support for casaIT PCF8574 switches."""

from __future__ import annotations

from datetime import timedelta
import logging
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import CasaITConfigEntry
from .api import CasaITApi
from .const import DOMAIN, DS2413_CHANNEL_OUTPUT, OM117_MODE_SWITCH, PCF8574_MAPPED_PORTS
from .helpers import (
    build_bridge_slug,
    build_device_identifier,
    build_i2c_entity_id,
    build_onewire_device_info,
    build_onewire_entity_id,
    default_onewire_profile,
    get_address_range,
    get_configured_ds2413_channels,
    get_configured_onewire_profiles,
    get_dm117_port_configuration,
    get_module_name,
    get_om117_pair_configuration,
)
from .services.i2cClasses.dm117 import DeviceType, DM117PortConfig, PortConfig

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 1
SCAN_INTERVAL = timedelta(seconds=1)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: CasaITConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the casaIT switches."""
    api: CasaITApi = config_entry.runtime_data

    await api.async_wait_initialized()

    pcf_entities: list[SwitchEntity] = []
    om_config = get_om117_pair_configuration(config_entry.options)

    output_range = get_address_range("OM117")
    for addr in api.im117_om117:
        if output_range is None or not output_range[0] <= addr <= output_range[1]:
            continue

        pair_configs = om_config.get(addr, {})
        for pair_index in range(4):
            config = pair_configs.get(pair_index)
            if config and config.mode != OM117_MODE_SWITCH:
                continue

            base_port = pair_index * 2
            for offset in (0, 1):
                port = base_port + offset
                pcf_entities.append(CasaITSwitch(api, config_entry, addr, port))

    dm_entities: list[CasaITDM117Switch] = []
    dm_config = get_dm117_port_configuration(config_entry.options)
    for addr, slots in dm_config.items():
        if addr not in api.dm117:
            continue
        for port, device_type in slots.items():
            if device_type is not DeviceType.OUTPUT:
                continue
            dm_entities.append(CasaITDM117Switch(api, config_entry, addr, port, 0))
            dm_entities.append(CasaITDM117Switch(api, config_entry, addr, port, 1))

    ds2413_entities: list[SwitchEntity] = []
    configured_profiles = get_configured_onewire_profiles(config_entry.options)
    configured_channels = get_configured_ds2413_channels(config_entry.options)

    for device_id, meta in api.ow_devices.items():
        profile = configured_profiles.get(device_id) or default_onewire_profile(meta)
        if profile not in {"ds2413", "ds2413_in", "ds2413_out"}:
            continue
        fallback = DS2413_CHANNEL_OUTPUT if profile == "ds2413_out" else "input"
        channel_roles = configured_channels.get(device_id, {0: fallback, 1: fallback})
        ds2413_entities.extend(
            CasaITDS2413Switch(api, config_entry, device_id, channel, meta)
            for channel, role in channel_roles.items()
            if role == DS2413_CHANNEL_OUTPUT
        )

    async_add_entities([*pcf_entities, *dm_entities, *ds2413_entities])


class CasaITSwitch(SwitchEntity):
    """Representation of a casaIT switch."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_translation_key = "om117_output"

    def __init__(self, api: CasaITApi, config_entry: CasaITConfigEntry, address: int, port: int) -> None:
        """Initialize the switch."""
        self._api = api
        self._address = address
        self._port = port
        self._hardware_port = PCF8574_MAPPED_PORTS[port]
        bridge_slug = build_bridge_slug(config_entry.entry_id, config_entry.unique_id)
        self._attr_unique_id = f"{config_entry.entry_id}_om117_{address}_{port}"
        self.entity_id = build_i2c_entity_id("switch", bridge_slug, "om117", address, "output", port + 1)
        self._attr_translation_placeholders = {"port": str(port + 1)}
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, build_device_identifier(config_entry.entry_id, "om117", address))},
            name=get_module_name(config_entry.options, "om117", address, f"OM117 0x{address:02X}"),
            manufacturer="casaIT",
            model="PCF8574 Output",
            via_device=(DOMAIN, build_device_identifier(config_entry.entry_id, "bridge", "controller")),
        )
        self._update_state()

    def _update_state(self) -> None:
        """Update the state of the switch."""
        if self._address in self._api.pcf_states:
            states = self._api.pcf_states[self._address]
            if states is not None and 0 <= self._hardware_port < len(states):
                self._attr_is_on = states[self._hardware_port] == 0  # Inverted logic for PCF8574 outputs
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

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the switch on."""
        # For PCF8574 outputs, writing 0 turns the output on (active low)
        await self._async_set_state(0)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the switch off."""
        # For PCF8574 outputs, writing 1 turns the output off (active low)
        await self._async_set_state(1)

    async def _async_set_state(self, state: int) -> None:
        """Set the state of the switch."""
        # A successful write publishes the verified state, which updates this entity
        # through the dispatcher. A failed one leaves the old state in place.
        if not await self._api.async_write_pcf_port(self._address, self._hardware_port, state):
            raise HomeAssistantError(translation_domain=DOMAIN, translation_key="pcf_output_write_failed")

    @property
    def available(self) -> bool:
        """Return if entity is available."""
        return self._address in self._api.pcf_states


class CasaITDM117Switch(SwitchEntity):
    """Representation of a DM117 digital output port."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_translation_key = "dm117_output"

    def __init__(
        self,
        api: CasaITApi,
        config_entry: CasaITConfigEntry,
        address: int,
        port: int,
        channel: int,
    ) -> None:
        """Initialize the DM117 switch."""
        self._api = api
        self._address = address
        self._port = port
        self._slot = port + 1
        self._channel = channel
        bridge_slug = build_bridge_slug(config_entry.entry_id, config_entry.unique_id)
        self._attr_unique_id = f"{config_entry.entry_id}_dm117_{address}_{port}_output_{channel}"
        channel_name = "A" if channel == 0 else "B"
        self.entity_id = build_i2c_entity_id(
            "switch", bridge_slug, "dm117", address, "slot", self._slot, "output", channel_name
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

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn on the switch."""
        await self._async_set_state(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn off the switch."""
        await self._async_set_state(False)

    async def _async_set_state(self, state: bool) -> None:
        digital = PortConfig(
            port_a=state if self._channel == 0 else None,
            port_b=state if self._channel == 1 else None,
        )
        config = DM117PortConfig(
            port=self._port,
            device_type=DeviceType.OUTPUT,
            digital=digital,
        )

        if not await self._api.async_write_dm117_port(self._address, config):
            raise HomeAssistantError(translation_domain=DOMAIN, translation_key="dm117_output_write_failed")

    @property
    def available(self) -> bool:
        """Return if entity is available."""
        return self._address in self._api.dm117_states


class CasaITDS2413Switch(SwitchEntity):
    """Switch entity for DS2413 channels configured as outputs."""

    _attr_has_entity_name = True
    _attr_should_poll = True
    _attr_translation_key = "ds2413_output"

    def __init__(
        self,
        api: CasaITApi,
        config_entry: CasaITConfigEntry,
        device_id: str,
        channel: int,
        meta: dict[str, Any],
    ) -> None:
        """Initialize the DS2413 switch."""

        self._api = api
        self._device_id = device_id
        self._channel = channel
        self._meta = meta
        channel_name = "A" if channel == 0 else "B"
        bridge_slug = build_bridge_slug(config_entry.entry_id, config_entry.unique_id)
        self._attr_unique_id = f"{config_entry.entry_id}_{device_id}_channel_{channel}_output"
        self.entity_id = build_onewire_entity_id("switch", bridge_slug, device_id, meta, "output", channel_name)
        self._attr_translation_placeholders = {"channel": channel_name}
        self._attr_device_info = build_onewire_device_info(config_entry.entry_id, device_id, meta)

    async def async_update(self) -> None:
        """Poll current DS2413 output state."""

        self._attr_available = False
        state = await self._api.read_ds2413_state(self._device_id, self._channel, invert=False)
        if state is None:
            return
        self._attr_is_on = state
        self._attr_available = True

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the DS2413 output on."""

        await self._async_set_state(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the DS2413 output off."""

        await self._async_set_state(False)

    async def _async_set_state(self, state: bool) -> None:
        if not await self._api.write_ds2413_state(self._device_id, self._channel, state):
            raise HomeAssistantError(translation_domain=DOMAIN, translation_key="ds2413_output_write_failed")
        self._attr_is_on = state
        self.async_write_ha_state()
