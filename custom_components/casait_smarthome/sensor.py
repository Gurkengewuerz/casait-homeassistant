"""Sensor platform for casaIT OneWire devices."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import timedelta
import logging
import time
from typing import Any

from homeassistant.components.sensor import (
    RestoreSensor,
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorExtraStoredData,
    SensorStateClass,
)
from homeassistant.const import (
    LIGHT_LUX,
    PERCENTAGE,
    EntityCategory,
    UnitOfElectricPotential,
    UnitOfRatio,
    UnitOfTemperature,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import CasaITConfigEntry
from .api import CasaITApi
from .const import OW_PROFILE_MULTISENSOR
from .entity import CasaITMultisensorEntity, CasaITOneWireEntity
from .helpers import (
    build_bridge_device_info,
    build_bridge_slug,
    build_entity_id,
    build_onewire_device_info,
    build_onewire_entity_id,
    default_onewire_profile,
    get_configured_onewire_profiles,
)
from .services.i2cClasses.ds2438 import DS2438Reading
from .services.i2cClasses.multisensor import MultisensorComponents, MultisensorReading

TEMP_COMP_A = 1.0546
TEMP_COMP_B = 0.00216

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 1
# Only the bridge diagnostics poll; 1-Wire values are pushed by the scheduler.
SCAN_INTERVAL = timedelta(seconds=15)


@dataclass(kw_only=True, frozen=True)
class OneWireSensorDescription(SensorEntityDescription):
    """Description of a OneWire-backed sensor."""

    profile: str
    value_fn: Callable[[Any], float | None]


class OneWireEntity(CasaITOneWireEntity, SensorEntity):
    """Base entity for OneWire sensors, fed by the 1-Wire scheduler."""

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

    def _update_from_value(self, value: Any) -> None:
        self._attr_native_value = value


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

    def _update_from_value(self, value: Any) -> None:
        self._attr_native_value = self.entity_description.value_fn(value)
        # A reading can be complete and still yield no value for one quantity,
        # such as a humidity outside the sensor's range.
        self._attr_available = self._attr_native_value is not None


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
        self._attr_device_info = build_bridge_device_info(entry.entry_id)

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


@dataclass(kw_only=True, frozen=True)
class BridgeDiagnosticDescription(SensorEntityDescription):
    """Describe one transport or poll-loop diagnostic value.

    ``value_fn`` reads the value from the API instead of the diagnostics snapshot.
    """

    section: str = ""
    source_key: str = ""
    value_fn: Callable[[CasaITApi], Any] | None = None


BRIDGE_DIAGNOSTIC_DESCRIPTIONS = (
    BridgeDiagnosticDescription(
        key="roundtrip_latency",
        translation_key="bridge_roundtrip_latency",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.MILLISECONDS,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        section="transport",
        source_key="last_roundtrip_ms",
    ),
    BridgeDiagnosticDescription(
        key="crc_errors",
        translation_key="bridge_crc_errors",
        state_class=SensorStateClass.TOTAL_INCREASING,
        entity_category=EntityCategory.DIAGNOSTIC,
        section="transport",
        source_key="crc_errors",
    ),
    BridgeDiagnosticDescription(
        key="timeouts",
        translation_key="bridge_timeouts",
        state_class=SensorStateClass.TOTAL_INCREASING,
        entity_category=EntityCategory.DIAGNOSTIC,
        section="transport",
        source_key="timeouts",
    ),
    BridgeDiagnosticDescription(
        key="send_spacing",
        translation_key="bridge_send_spacing",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.MILLISECONDS,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        section="transport",
        source_key="send_interval_ms",
    ),
    BridgeDiagnosticDescription(
        key="fast_poll_cycle",
        translation_key="bridge_fast_poll_cycle",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.MILLISECONDS,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        section="poll",
        source_key="fast_cycle_ms",
    ),
    BridgeDiagnosticDescription(
        key="full_poll_cycle",
        translation_key="bridge_full_poll_cycle",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.MILLISECONDS,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        section="poll",
        source_key="full_cycle_ms",
    ),
    BridgeDiagnosticDescription(
        key="i2c_retries",
        translation_key="bridge_i2c_retries",
        state_class=SensorStateClass.TOTAL_INCREASING,
        entity_category=EntityCategory.DIAGNOSTIC,
        section="bridge",
        source_key="i2c_retries",
    ),
    BridgeDiagnosticDescription(
        key="interlock_refusals",
        translation_key="bridge_interlock_refusals",
        state_class=SensorStateClass.TOTAL_INCREASING,
        entity_category=EntityCategory.DIAGNOSTIC,
        section="bridge",
        source_key="interlock_refusals",
    ),
    BridgeDiagnosticDescription(
        key="emergency_links",
        translation_key="bridge_emergency_links",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda api: api.emergency_stats.links if api.emergency_stats else None,
    ),
    BridgeDiagnosticDescription(
        key="emergency_actions",
        translation_key="bridge_emergency_actions",
        state_class=SensorStateClass.TOTAL_INCREASING,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda api: api.emergency_stats.actions if api.emergency_stats else None,
    ),
    BridgeDiagnosticDescription(
        key="emergency_failures",
        translation_key="bridge_emergency_failures",
        state_class=SensorStateClass.TOTAL_INCREASING,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda api: api.emergency_stats.failures if api.emergency_stats else None,
    ),
    BridgeDiagnosticDescription(
        key="emergency_last_action",
        translation_key="bridge_emergency_last_action",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda api: api.emergency_last_action,
    ),
)


class CasaITBridgeDiagnosticSensor(SensorEntity):
    """Expose one bridge diagnostic metric."""

    _attr_has_entity_name = True
    _attr_should_poll = True

    entity_description: BridgeDiagnosticDescription

    def __init__(self, api: CasaITApi, entry: CasaITConfigEntry, description: BridgeDiagnosticDescription) -> None:
        """Initialize a bridge diagnostic sensor."""

        self._api = api
        self.entity_description = description
        bridge_slug = build_bridge_slug(entry.entry_id, entry.unique_id)
        self._attr_unique_id = f"{entry.entry_id}_bridge_{description.key}"
        self.entity_id = build_entity_id("sensor", bridge_slug, description.key)
        self._attr_device_info = build_bridge_device_info(entry.entry_id)

    async def async_update(self) -> None:
        """Read the current metric from the API diagnostics snapshot."""

        if (value_fn := self.entity_description.value_fn) is not None:
            self._attr_native_value = value_fn(self._api)
        else:
            section = self._api.diagnostic_data[self.entity_description.section]
            self._attr_native_value = section[self.entity_description.source_key]
        self._attr_available = True


@dataclass(kw_only=True, frozen=True)
class MultisensorSensorDescription(SensorEntityDescription):
    """Describe one quantity a Multisensor chip reports."""

    fitted_fn: Callable[[MultisensorComponents], bool]
    value_fn: Callable[[CasaITApi, str], float | int | None]


def _reading_value(field: str) -> Callable[[CasaITApi, str], float | int | None]:
    def value(api: CasaITApi, device_id: str) -> float | int | None:
        reading: MultisensorReading | None = api.multisensor.reading(device_id)
        return getattr(reading, field) if reading is not None else None

    return value


MULTISENSOR_SENSORS: tuple[MultisensorSensorDescription, ...] = (
    MultisensorSensorDescription(
        key="temperature",
        translation_key="temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        fitted_fn=lambda parts: parts.sht41,
        value_fn=_reading_value("temperature"),
    ),
    MultisensorSensorDescription(
        key="humidity",
        translation_key="humidity",
        device_class=SensorDeviceClass.HUMIDITY,
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        fitted_fn=lambda parts: parts.sht41,
        value_fn=_reading_value("humidity"),
    ),
    MultisensorSensorDescription(
        key="co2",
        translation_key="co2",
        device_class=SensorDeviceClass.CO2,
        native_unit_of_measurement=UnitOfRatio.PARTS_PER_MILLION,
        state_class=SensorStateClass.MEASUREMENT,
        fitted_fn=lambda parts: parts.stcc4,
        value_fn=_reading_value("co2"),
    ),
    MultisensorSensorDescription(
        key="illuminance",
        translation_key="illuminance",
        device_class=SensorDeviceClass.ILLUMINANCE,
        native_unit_of_measurement=LIGHT_LUX,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        fitted_fn=lambda parts: parts.veml7700,
        value_fn=_reading_value("illuminance"),
    ),
    MultisensorSensorDescription(
        key="voc_raw",
        translation_key="voc_raw",
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        fitted_fn=lambda parts: parts.sgp40,
        value_fn=_reading_value("voc_raw"),
    ),
    MultisensorSensorDescription(
        key="co2_calibration_correction",
        translation_key="co2_calibration_correction",
        native_unit_of_measurement=UnitOfRatio.PARTS_PER_MILLION,
        entity_category=EntityCategory.DIAGNOSTIC,
        fitted_fn=lambda parts: parts.stcc4,
        value_fn=lambda api, device_id: api.multisensor.maintenance(device_id).get("frc_correction"),
    ),
)

VOC_INDEX_DESCRIPTION = MultisensorSensorDescription(
    key="voc_index",
    translation_key="voc_index",
    state_class=SensorStateClass.MEASUREMENT,
    fitted_fn=lambda parts: parts.sgp40,
    value_fn=_reading_value("voc_index"),
)


class CasaITMultisensorSensor(CasaITMultisensorEntity, SensorEntity):
    """One quantity reported by a Multisensor."""

    entity_description: MultisensorSensorDescription

    def __init__(
        self,
        api: CasaITApi,
        entry: CasaITConfigEntry,
        device_id: str,
        meta: dict[str, Any],
        description: MultisensorSensorDescription,
    ) -> None:
        """Initialize the sensor."""

        super().__init__(api, entry, device_id, meta, description, "sensor")

    def _update_from_sample(self) -> None:
        value = self.entity_description.value_fn(self._api, self._device_id)
        self._attr_native_value = value
        # The correction only exists once a calibration ran; that is not an outage.
        self._attr_available = value is not None or self.entity_description.key == "co2_calibration_correction"


@dataclass
class VocExtraStoredData(SensorExtraStoredData):
    """Sensor state plus the learned VOC baseline."""

    voc_mean: float | None = None
    voc_std: float | None = None
    saved_at: float | None = None

    def as_dict(self) -> dict[str, Any]:
        """Return a dict representation of the stored data."""

        data = super().as_dict()
        data.update({"voc_mean": self.voc_mean, "voc_std": self.voc_std, "saved_at": self.saved_at})
        return data


class CasaITVocIndexSensor(CasaITMultisensorSensor, RestoreSensor):
    """The VOC index, keeping its learned baseline across short restarts.

    The algorithm needs hours to learn a room's baseline. Restoring it after a
    Home Assistant restart avoids that relearning whenever the outage was short.
    """

    async def async_added_to_hass(self) -> None:
        """Restore the learned baseline before the first sample arrives."""

        await super().async_added_to_hass()
        if (data := await self.async_get_last_extra_data()) is None:
            return
        stored = data.as_dict()
        mean, std, saved_at = stored.get("voc_mean"), stored.get("voc_std"), stored.get("saved_at")
        if isinstance(mean, (int, float)) and isinstance(std, (int, float)) and isinstance(saved_at, (int, float)):
            self._api.multisensor.restore_voc_states(self._device_id, float(mean), float(std), time.time() - saved_at)

    @property
    def extra_restore_state_data(self) -> VocExtraStoredData:
        """Return the state and the learned baseline for storage."""

        states = self._api.multisensor.voc_states(self._device_id)
        return VocExtraStoredData(
            native_value=self._attr_native_value,
            native_unit_of_measurement=None,
            voc_mean=states[0] if states else None,
            voc_std=states[1] if states else None,
            saved_at=time.time() if states else None,
        )


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
    entities.extend(
        CasaITBridgeDiagnosticSensor(api, entry, description) for description in BRIDGE_DIAGNOSTIC_DESCRIPTIONS
    )

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

        if profile == OW_PROFILE_MULTISENSOR:
            if (parts := api.multisensor.components(device_id)) is None:
                continue
            entities.extend(
                CasaITMultisensorSensor(api, entry, device_id, meta, description)
                for description in MULTISENSOR_SENSORS
                if description.fitted_fn(parts)
            )
            if VOC_INDEX_DESCRIPTION.fitted_fn(parts):
                entities.append(CasaITVocIndexSensor(api, entry, device_id, meta, VOC_INDEX_DESCRIPTION))
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
                OneWireSensorDescription(
                    key="vdd",
                    translation_key="ds2438_vdd",
                    device_class=SensorDeviceClass.VOLTAGE,
                    native_unit_of_measurement=UnitOfElectricPotential.VOLT,
                    state_class=SensorStateClass.MEASUREMENT,
                    entity_category=EntityCategory.DIAGNOSTIC,
                    entity_registry_enabled_default=False,
                    profile=profile,
                    value_fn=lambda reading: reading.vdd,
                ),
                OneWireSensorDescription(
                    key="vad",
                    translation_key="ds2438_vad",
                    device_class=SensorDeviceClass.VOLTAGE,
                    native_unit_of_measurement=UnitOfElectricPotential.VOLT,
                    state_class=SensorStateClass.MEASUREMENT,
                    entity_category=EntityCategory.DIAGNOSTIC,
                    entity_registry_enabled_default=False,
                    profile=profile,
                    value_fn=lambda reading: reading.vad,
                ),
                OneWireSensorDescription(
                    key="vse",
                    translation_key="ds2438_vse",
                    device_class=SensorDeviceClass.VOLTAGE,
                    native_unit_of_measurement=UnitOfElectricPotential.VOLT,
                    state_class=SensorStateClass.MEASUREMENT,
                    entity_category=EntityCategory.DIAGNOSTIC,
                    entity_registry_enabled_default=False,
                    profile=profile,
                    value_fn=lambda reading: reading.vse,
                ),
            ]

            entities.extend(DS2438Sensor(api, entry, device_id, meta, description) for description in descriptions)

    if entities:
        async_add_entities(entities)
