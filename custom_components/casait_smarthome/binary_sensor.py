"""Support for casaIT PCF8574 binary sensors."""

from __future__ import annotations

from datetime import timedelta
import logging
from typing import Any

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import CasaITConfigEntry
from .api import CasaITApi
from .const import DOMAIN, I2C_ADDR_RANGES, PCF8574_MAPPED_PORTS, SIGNAL_STATE_UPDATED
from .helpers import default_onewire_profile, get_configured_onewire_profiles, get_dm117_port_configuration
from .services.i2cClasses.dm117 import DeviceType, PortConfig

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: CasaITConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the casaIT binary sensors."""
    api: CasaITApi = config_entry.runtime_data

    await api.async_wait_initialized()

    input_range = next((start, end) for start, end, name, model in I2C_ADDR_RANGES if "IM117" in model)

    pcf_entities = [
        CasaITBinarySensor(api, addr, port)
        for addr, device in api.im117_om117.items()
        if input_range[0] <= addr <= input_range[1]
        for port in range(8)
    ]

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

    for device_id, meta in api.ow_devices.items():
        profile = configured_profiles.get(device_id) or default_onewire_profile(meta)
        if profile != "ds2413_in":
            continue
        ds2413_entities.extend(CasaITDS2413BinarySensor(api, device_id, channel, meta) for channel in (0, 1))

    async_add_entities([*pcf_entities, *dm_entities, *ds2413_entities])


class CasaITBinarySensor(BinarySensorEntity):
    """Representation of a casaIT binary sensor."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, api: CasaITApi, address: int, port: int) -> None:
        """Initialize the binary sensor."""
        self._api = api
        self._address = address
        self._port = port
        self._hardware_port = PCF8574_MAPPED_PORTS[port]
        self._attr_unique_id = f"{DOMAIN}_{address}_{port}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, str(address))},
            name=f"Input module {hex(address)}",
            manufacturer="casaIT",
            model="PCF8574 Input",
        )
        self._update_state()

    @property
    def name(self) -> str:
        """Return the name of the sensor."""
        return f"Port {self._port + 1}"

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
        self.async_on_remove(async_dispatcher_connect(self.hass, SIGNAL_STATE_UPDATED, self._handle_state_update))

    @property
    def available(self) -> bool:
        """Return if entity is available."""
        return self._address in self._api.pcf_states


class CasaITDM117BinarySensor(BinarySensorEntity):
    """Binary sensor representing a DM117 digital input port."""

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
        """Initialize the DM117 binary sensor."""

        self._api = api
        self._address = address
        self._port = port
        self._slot = port + 1
        self._channel = channel  # 0 for port A, 1 for port B
        self._attr_unique_id = f"{config_entry.entry_id}_dm117_{address}_{port}_input_{channel}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"dm117_{address}")},
            name=f"DM117 module {hex(address)}",
            manufacturer="casaIT",
            model="DM117",
        )
        self._update_state()

    @property
    def name(self) -> str:
        """Return the entity name."""

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

    @property
    def available(self) -> bool:
        """Return if entity is available."""
        return self._address in self._api.dm117_states


class CasaITDS2413BinarySensor(BinarySensorEntity):
    """Binary sensor for DS2413 channels configured as inputs."""

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
        """Initialize the DS2413 binary sensor."""

        self._api = api
        self._device_id = device_id
        self._channel = channel
        self._meta = meta
        channel_name = "A" if channel == 0 else "B"
        self._attr_unique_id = f"{device_id}_channel_{channel}_input"
        self._attr_name = f"{device_id} channel {channel_name} input"
        self._attr_device_info = _build_onewire_device_info(device_id, meta)

    async def async_update(self) -> None:
        """Poll the DS2413 input state."""

        self._attr_available = False
        state = await self._api.read_ds2413_state(self._device_id, self._channel)
        if state is None:
            return
        self._attr_is_on = state
        self._attr_available = True


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
