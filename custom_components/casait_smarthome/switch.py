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
from .const import DOMAIN, I2C_ADDR_RANGES, PCF8574_MAPPED_PORTS, SIGNAL_STATE_UPDATED
from .helpers import default_onewire_profile, get_configured_onewire_profiles, get_dm117_port_configuration
from .services.i2cClasses.dm117 import DeviceType, DM117PortConfig, PortConfig

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: CasaITConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the casaIT switches."""
    api: CasaITApi = config_entry.runtime_data

    await api.async_wait_initialized()

    output_range = next((start, end) for start, end, name, model in I2C_ADDR_RANGES if "OM117" in model)

    pcf_entities = [
        CasaITSwitch(api, addr, port)
        for addr, device in api.im117_om117.items()
        if output_range[0] <= addr <= output_range[1]
        for port in range(8)
    ]

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

    for device_id, meta in api.ow_devices.items():
        profile = configured_profiles.get(device_id) or default_onewire_profile(meta)
        if profile != "ds2413_out":
            continue
        ds2413_entities.extend(CasaITDS2413Switch(api, device_id, channel, meta) for channel in (0, 1))

    async_add_entities([*pcf_entities, *dm_entities, *ds2413_entities])


class CasaITSwitch(SwitchEntity):
    """Representation of a casaIT switch."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, api: CasaITApi, address: int, port: int) -> None:
        """Initialize the switch."""
        self._api = api
        self._address = address
        self._port = port
        self._hardware_port = PCF8574_MAPPED_PORTS[port]
        self._attr_unique_id = f"{DOMAIN}_{address}_{port}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, str(address))},
            name=f"Output module {hex(address)}",
            manufacturer="casaIT",
            model="PCF8574 Output",
        )
        self._update_state()

    @property
    def name(self) -> str:
        """Return the name of the switch."""
        return f"Port {self._port + 1}"

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
        self.async_on_remove(async_dispatcher_connect(self.hass, SIGNAL_STATE_UPDATED, self._handle_state_update))

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
        device = self._api.im117_om117.get(self._address)
        if device:
            async with self._api.lock:
                await self.hass.async_add_executor_job(device.write_port, self._hardware_port, state)
            await self._api.async_force_refresh()

    @property
    def available(self) -> bool:
        """Return if entity is available."""
        return self._address in self._api.pcf_states


class CasaITDM117Switch(SwitchEntity):
    """Representation of a DM117 digital output port."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(
        self,
        api: CasaITApi,
        config_entry: CasaITConfigEntry,
        address: int,
        port: int,
        channel: int,
    ) -> None:
        """Initialize the DM117 switch.

        Args:
            coordinator: The data coordinator for casaIT.
            config_entry: The configuration entry for this entity.
            address: The I2C address of the DM117 module.
            port: The port number on the DM117 module (0-7).
            channel: The channel number on the DM117 port (0 for Port A, 1 for Port B).
        """
        self._api = api
        self._address = address
        self._port = port
        self._slot = port + 1
        self._channel = channel
        self._attr_unique_id = f"{config_entry.entry_id}_dm117_{address}_{port}_output_{channel}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"dm117_{address}")},
            name=f"DM117 module {hex(address)}",
            manufacturer="casaIT",
            model="DM117",
        )
        self._update_state()

    @property
    def name(self) -> str:
        """Return the name of the switch."""
        channel_name = "A" if self._channel == 0 else "B"
        return f"Slot {self._slot} Port {channel_name}"

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
        self.async_on_remove(async_dispatcher_connect(self.hass, SIGNAL_STATE_UPDATED, self._handle_state_update))

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn on the switch."""
        await self._async_set_state(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn off the switch."""
        await self._async_set_state(False)

    async def _async_set_state(self, state: bool) -> None:
        device = self._api.dm117.get(self._address)
        if not device:
            raise HomeAssistantError("DM117 module not available")

        digital = PortConfig(
            port_a=state if self._channel == 0 else None,
            port_b=state if self._channel == 1 else None,
        )
        config = DM117PortConfig(
            port=self._port,
            device_type=DeviceType.OUTPUT,
            digital=digital,
        )

        async with self._api.lock:
            await self.hass.async_add_executor_job(device.write_port, config)

        await self._api.async_force_refresh()

    @property
    def available(self) -> bool:
        """Return if entity is available."""
        return self._address in self._api.dm117_states


class CasaITDS2413Switch(SwitchEntity):
    """Switch entity for DS2413 channels configured as outputs."""

    _attr_has_entity_name = False
    _attr_should_poll = True
    SCAN_INTERVAL = timedelta(seconds=1)

    def __init__(
        self,
        api: CasaITApi,
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
        self._attr_unique_id = f"{device_id}_channel_{channel}_output"
        self._attr_name = f"{device_id} channel {channel_name} output"
        self._attr_device_info = _build_onewire_device_info(device_id, meta)

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
            raise HomeAssistantError("Unable to set DS2413 output state")
        self._attr_is_on = state
        self.async_write_ha_state()


def _build_onewire_device_info(device_id: str, meta: dict[str, Any]) -> DeviceInfo:
    """Return DeviceInfo referencing the SM117 bus for OneWire devices."""

    bus_address = meta.get("bus_address")
    device_type = str(meta.get("device_type") or "").strip()
    if bus_address is not None:
        return DeviceInfo(
            identifiers={(DOMAIN, f"sm117_{bus_address:02x}")},
            name=f"SM117 Bus 0x{int(bus_address):02X}",
            manufacturer="CasaIT",
            model="SM117 1-Wire bridge",
        )

    return DeviceInfo(
        identifiers={(DOMAIN, f"onewire_{device_id}")},
        name=f"OneWire {device_id}",
        model=device_type or "OneWire",
        manufacturer="Maxim Integrated",
    )
