"""Golden contract for public entity identity and grouping."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from custom_components.casait_smarthome.binary_sensor import (
    CasaITBinarySensor,
    CasaITDM117BinarySensor,
    CasaITDS2413BinarySensor,
)
from custom_components.casait_smarthome.cover import CasaITBlindCover
from custom_components.casait_smarthome.helpers import OM117PairConfig
from custom_components.casait_smarthome.light import CasaITDM117Light, CasaITLEDControllerLight
from custom_components.casait_smarthome.sensor import (
    CasaITDebugSensor,
    DS18B20TemperatureSensor,
    DS2438Sensor,
    OneWireSensorDescription,
)
from custom_components.casait_smarthome.switch import CasaITDM117Switch, CasaITDS2413Switch, CasaITSwitch

FIXTURES_DIR = Path(__file__).parent / "fixtures"
ENTRY = SimpleNamespace(entry_id="entry-test")
PLATFORM_BY_CASE = {
    "ds2413_binary_sensor": "binary_sensor",
    "ds2413_switch": "switch",
    "led_controller_light": "light",
    "ds18b20_temperature": "sensor",
    "ds2438_sensor": "sensor",
}


def _api() -> SimpleNamespace:
    return SimpleNamespace(pcf_states={}, dm117_states={})


def _meta(device_type: str) -> dict[str, Any]:
    return {"bus_address": 0x18, "device_type": device_type}


def _entities() -> dict[str, Any]:
    api = _api()
    return {
        "im117_binary_sensor": CasaITBinarySensor(api, 0x38, 0),
        "dm117_binary_sensor": CasaITDM117BinarySensor(api, ENTRY, 0x10, 0, 0),
        "ds2413_binary_sensor": CasaITDS2413BinarySensor(api, "3a00000000000001", 0, _meta("DS2413")),
        "om117_switch": CasaITSwitch(api, 0x20, 0),
        "dm117_switch": CasaITDM117Switch(api, ENTRY, 0x10, 0, 0),
        "ds2413_switch": CasaITDS2413Switch(api, "3a00000000000001", 0, _meta("DS2413")),
        "dm117_light": CasaITDM117Light(api, ENTRY, 0x10, 0),
        "led_controller_light": CasaITLEDControllerLight(api, "1900000000000001", _meta("DS28E17"), 30),
        "om117_cover": CasaITBlindCover(api, ENTRY, 0x20, 0, OM117PairConfig()),
        "ds18b20_temperature": DS18B20TemperatureSensor(api, "2800000000000001", _meta("DS18B20")),
        "ds2438_sensor": DS2438Sensor(
            api,
            "2600000000000001",
            _meta("DS2438"),
            OneWireSensorDescription(
                key="humidity",
                translation_key="humidity",
                profile="ds2438_hih5030_tept5600",
                value_fn=lambda reading: reading,
            ),
        ),
        "debug_sensor": CasaITDebugSensor(api, ENTRY),
    }


def _normalize_entity(case: str, entity: Any) -> dict[str, Any]:
    device_info = entity.device_info
    assert device_info is not None
    identifiers = sorted([list(identifier) for identifier in device_info["identifiers"]])
    name = entity.name
    if (platform := PLATFORM_BY_CASE.get(case)) is not None:
        strings_path = Path(__file__).parents[1] / "custom_components" / "casait_smarthome" / "strings.json"
        strings = json.loads(strings_path.read_text(encoding="utf-8"))
        template = strings["entity"][platform][entity.translation_key]["name"]
        name = template.format(**getattr(entity, "_attr_translation_placeholders", {}))
    normalized = {
        "case": case,
        "unique_id": entity.unique_id,
        "name": name,
        "device_identifiers": identifiers,
        "device_name": device_info["name"],
    }
    if via_device := device_info.get("via_device"):
        normalized["via_device"] = list(via_device)
    return normalized


def test_entity_contract_matches_golden_file() -> None:
    expected = json.loads((FIXTURES_DIR / "entity_contract.json").read_text(encoding="utf-8"))
    entities = _entities()

    assert [_normalize_entity(case, entities[case]) for case in entities] == expected
