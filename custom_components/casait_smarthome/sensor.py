"""Sensor platform for casaIT OneWire devices."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import timedelta
import logging
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorEntityDescription, SensorStateClass
from homeassistant.const import LIGHT_LUX, PERCENTAGE, EntityCategory, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import CasaITConfigEntry
from .api import CasaITApi
from .const import DOMAIN
from .helpers import (
    build_bridge_slug,
    build_device_identifier,
    build_entity_id,
    build_onewire_device_info,
    build_onewire_entity_id,
    default_onewire_profile,
    get_configured_onewire_profiles,
)
from .services.i2cClasses.ds2438 import DS2438Reading

TEMP_COMP_A = 1.0546
TEMP_COMP_B = 0.00216

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 1
SCAN_INTERVAL = timedelta(seconds=15)


@dataclass(kw_only=True, frozen=True)
class OneWireSensorDescription(SensorEntityDescription):
    """Description of a OneWire-backed sensor."""

    profile: str
    value_fn: Callable[[Any], float | None]


class OneWireEntity(SensorEntity):
    """Base entity for OneWire sensors."""

    _attr_has_entity_name = True

    entity_description: OneWireSensorDescription

    def __init__(
        self,
        entry: CasaITConfigEntry,
        device_id: str,
        meta: dict[str, Any],
        description: OneWireSensorDescription,
    ) -> None:
        """Initialize the entity."""
        self._device_id = device_id
        self._meta = meta
        self._bus_address: int | None = meta.get("bus_address")
        self.entity_description = description
        bridge_slug = build_bridge_slug(entry.entry_id, entry.unique_id)
        self._attr_unique_id = f"{entry.entry_id}_{device_id}_{description.key}"
        self.entity_id = build_onewire_entity_id("sensor", bridge_slug, device_id, meta, description.key)
        self._attr_device_class = description.device_class
        self._attr_native_unit_of_measurement = description.native_unit_of_measurement
        self._attr_state_class = description.state_class
        if self._bus_address is None:
            _LOGGER.warning(
                "OneWire device %s has no bus address; it will not be grouped under a common device in Home Assistant",
                device_id,
            )
        self._attr_device_info = build_onewire_device_info(entry.entry_id, device_id, meta)


class DS18B20TemperatureSensor(OneWireEntity):
    """Temperature sensor for DS18B20 devices."""

    def __init__(self, api: CasaITApi, entry: CasaITConfigEntry, device_id: str, meta: dict[str, Any]) -> None:
        """Initialize the DS18B20 temperature sensor entity."""
        super().__init__(
            entry,
            device_id,
            meta,
            OneWireSensorDescription(
                key="temperature",
                translation_key="temperature",
                device_class=SensorDeviceClass.TEMPERATURE,
                native_unit_of_measurement=UnitOfTemperature.CELSIUS,
                state_class=SensorStateClass.MEASUREMENT,
                profile="ds18b20_temp",
                value_fn=lambda reading: float(reading) if isinstance(reading, (int, float)) else None,
            ),
        )
        self._api = api

    async def async_update(self) -> None:
        """Fetch the latest temperature reading."""
        self._attr_available = False
        value = await self._api.read_ds18b20_temperature(self._device_id)
        if value is None:
            return
        self._attr_native_value = value
        self._attr_available = True


class DS2438Sensor(OneWireEntity):
    """Sensor entity backed by a DS2438 reading."""

    entity_description: OneWireSensorDescription

    def __init__(
        self,
        api: CasaITApi,
        entry: CasaITConfigEntry,
        device_id: str,
        meta: dict[str, Any],
        description: OneWireSensorDescription,
    ) -> None:
        """Initialize the DS2438 sensor entity."""
        super().__init__(entry, device_id, meta, description)
        self._api = api

    async def async_update(self) -> None:
        """Fetch the latest reading and update the sensor state."""
        self._attr_available = False
        reading = await self._api.read_ds2438(self._device_id)
        if reading is None:
            return
        self._attr_native_value = self.entity_description.value_fn(reading)
        if self._attr_native_value is None:
            return
        self._attr_available = True


class CasaITDebugSensor(SensorEntity):
    """Debug sensor exposing discovered I2C and OneWire devices."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_translation_key = "diagnostics"

    def __init__(self, api: CasaITApi, entry: CasaITConfigEntry) -> None:
        """Initialize the debug sensor."""

        self._api = api
        self._attr_unique_id = f"{entry.entry_id}_debug"
        bridge_slug = build_bridge_slug(entry.entry_id, entry.unique_id)
        self.entity_id = build_entity_id("sensor", bridge_slug, "diagnostics")
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, build_device_identifier(entry.entry_id, "bridge", "controller"))},
            name="casaIT bridge",
            manufacturer="CasaIT",
            model="SMBus proxy",
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return detailed discovery information."""

        return self._api.diagnostic_data

    async def async_update(self) -> None:
        """Update the debug sensor state."""

        i2c_count = sum(len(addresses) for addresses in self._api.found_i2c_devices.values())
        onewire_count = len(self._api.ow_devices)
        self._attr_native_value = i2c_count + onewire_count
        self._attr_available = True


def _humidity_hih4030(reading: DS2438Reading) -> float | None:
    """Calculate humidity using HIH4030 formula."""
    if reading.vdd in (None, 0) or reading.vad is None:
        return None

    val = round(
        (161.29 * reading.vad / reading.vdd - 25.8065) / (TEMP_COMP_A - TEMP_COMP_B * reading.temperature),
        2,
    )
    if val < 0 or val > 100:
        _LOGGER.warning("Invalid humidity value: %s", val)
        return None
    return val


def _humidity_hih5030(reading: DS2438Reading) -> float | None:
    """Calculate humidity using HIH5030 formula."""
    if reading.vdd in (None, 0) or reading.vad is None:
        return None

    val = round(
        (157.233 * reading.vad / reading.vdd - 23.2808) / (TEMP_COMP_A - TEMP_COMP_B * reading.temperature),
        2,
    )
    if val < 0 or val > 100:
        _LOGGER.warning("Invalid humidity value: %s", val)
        return None
    return val


def _illuminance_from_reading(reading: DS2438Reading) -> float | None:
    """Calculate illuminance for TEPT5600 photodiode."""
    voltage = reading.vse if reading.vse is not None else reading.vad
    if voltage is None:
        return None

    lux = round(voltage * 1000, 2)
    if lux < 0 or lux > 5000:
        _LOGGER.warning("Invalid light value: %s", lux)
        return None
    return lux


async def async_setup_entry(
    hass: HomeAssistant,
    entry: CasaITConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up OneWire sensors from a config entry."""

    api = entry.runtime_data

    await api.async_wait_initialized()

    entities: list[SensorEntity] = []

    entities.append(CasaITDebugSensor(api, entry))

    configured_profiles = get_configured_onewire_profiles(entry.options)

    devices_by_bus: dict[int, list[tuple[str, dict]]] = defaultdict(list)
    unassigned_devices: list[tuple[str, dict]] = []

    for device_id, meta in api.ow_devices.items():
        bus_address = meta.get("bus_address")
        if bus_address is None:
            unassigned_devices.append((device_id, meta))
            continue
        devices_by_bus[bus_address].append((device_id, meta))

    def _iter_sorted_devices() -> Iterable[tuple[str, dict]]:
        for bus_address in sorted(devices_by_bus):
            for device_id, meta in sorted(devices_by_bus[bus_address], key=lambda item: item[0]):
                yield device_id, meta
        for device_id, meta in sorted(unassigned_devices, key=lambda item: item[0]):
            yield device_id, meta

    for device_id, meta in _iter_sorted_devices():
        profile = configured_profiles.get(device_id) or default_onewire_profile(meta)

        if profile is None:
            continue

        if profile == "ds18b20_temp":
            entities.append(DS18B20TemperatureSensor(api, entry, device_id, meta))
            continue

        if profile in {"ds2438_hih4030_tept5600", "ds2438_hih5030_tept5600"}:
            descriptions = [
                OneWireSensorDescription(
                    key="temperature",
                    translation_key="temperature",
                    device_class=SensorDeviceClass.TEMPERATURE,
                    native_unit_of_measurement=UnitOfTemperature.CELSIUS,
                    state_class=SensorStateClass.MEASUREMENT,
                    profile=profile,
                    value_fn=lambda reading: reading.temperature,
                ),
                OneWireSensorDescription(
                    key="humidity",
                    translation_key="humidity",
                    device_class=SensorDeviceClass.HUMIDITY,
                    native_unit_of_measurement=PERCENTAGE,
                    state_class=SensorStateClass.MEASUREMENT,
                    profile=profile,
                    value_fn=(_humidity_hih4030 if "4030" in profile else _humidity_hih5030),
                ),
                OneWireSensorDescription(
                    key="illuminance",
                    translation_key="illuminance",
                    device_class=SensorDeviceClass.ILLUMINANCE,
                    native_unit_of_measurement=LIGHT_LUX,
                    state_class=SensorStateClass.MEASUREMENT,
                    profile=profile,
                    value_fn=_illuminance_from_reading,
                ),
            ]

            entities.extend(DS2438Sensor(api, entry, device_id, meta, description) for description in descriptions)

    if entities:
        async_add_entities(entities)
