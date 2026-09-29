"""Golden contract for public entity identity and grouping."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from custom_components.casait_smarthome.binary_sensor import (
    CasaITBinarySensor,
    CasaITBridgeConnectionSensor,
    CasaITDM117BinarySensor,
    CasaITDS2413BinarySensor,
)
from custom_components.casait_smarthome.button import CasaITPulseButton, CasaITRescanButton
from custom_components.casait_smarthome.const import OM117_MODE_BLIND
from custom_components.casait_smarthome.cover import CasaITBlindCover
from custom_components.casait_smarthome.event import CasaITButtonEvent
from custom_components.casait_smarthome.helpers import InputSettings, OM117PairConfig
from custom_components.casait_smarthome.light import CasaITDM117Light, CasaITLEDControllerLight
from custom_components.casait_smarthome.number import (
    COVER_RUNTIME_NUMBERS,
    CasaITLEDControllerNumber,
    CasaITOM117RuntimeNumber,
)
from custom_components.casait_smarthome.sensor import (
    BRIDGE_DIAGNOSTIC_DESCRIPTIONS,
    CasaITBridgeDiagnosticSensor,
    CasaITDebugSensor,
    DS18B20TemperatureSensor,
    DS2438Sensor,
    OneWireSensorDescription,
)
from custom_components.casait_smarthome.switch import CasaITDM117Switch, CasaITDS2413Switch, CasaITSwitch

FIXTURES_DIR = Path(__file__).parent / "fixtures"
ENTRY = SimpleNamespace(entry_id="entry-test", unique_id="AA:BB:CC:DD:EE:FF", options={})


def _api() -> SimpleNamespace:
    return SimpleNamespace(pcf_states={}, dm117_states={}, bus=SimpleNamespace(stats={"connected": True}))


def _meta(device_type: str) -> dict[str, Any]:
    return {"bus_address": 0x18, "device_type": device_type}


def _entities() -> dict[str, Any]:
    api = _api()
    blind_config = OM117PairConfig(mode=OM117_MODE_BLIND)
    return {
        "bridge_connection": CasaITBridgeConnectionSensor(api, ENTRY),
        "im117_binary_sensor": CasaITBinarySensor(api, ENTRY, 0x38, 0),
        "dm117_binary_sensor": CasaITDM117BinarySensor(api, ENTRY, 0x10, 0, 0),
        "ds2413_binary_sensor": CasaITDS2413BinarySensor(api, ENTRY, "3a00000000000001", 0, _meta("DS2413")),
        "om117_switch": CasaITSwitch(api, ENTRY, 0x20, 0),
        "dm117_switch": CasaITDM117Switch(api, ENTRY, 0x10, 0, 0),
        "ds2413_switch": CasaITDS2413Switch(api, ENTRY, "3a00000000000001", 0, _meta("DS2413")),
        "dm117_light": CasaITDM117Light(api, ENTRY, 0x10, 0),
        "led_controller_light": CasaITLEDControllerLight(api, ENTRY, "1900000000000001", _meta("DS28E17"), 30),
        "om117_cover": CasaITBlindCover(api, ENTRY, 0x20, 0, blind_config),
        "im117_event": CasaITButtonEvent(api, ENTRY, 0x38, 0, InputSettings()),
        "rescan_button": CasaITRescanButton(api, ENTRY),
        "om117_pulse_button": CasaITPulseButton(api, ENTRY, 0x20, 0, 0),
        "om117_open_time": CasaITOM117RuntimeNumber(
            api,
            ENTRY,
            0x20,
            0,
            blind_config,
            COVER_RUNTIME_NUMBERS[0],
        ),
        "led_count_number": CasaITLEDControllerNumber(
            api,
            ENTRY,
            "1900000000000001",
            _meta("DS28E17"),
            "led_count",
        ),
        "ds18b20_temperature": DS18B20TemperatureSensor(api, ENTRY, "2800000000000001", _meta("DS18B20")),
        "ds2438_sensor": DS2438Sensor(
            api,
            ENTRY,
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
        "bridge_roundtrip": CasaITBridgeDiagnosticSensor(api, ENTRY, BRIDGE_DIAGNOSTIC_DESCRIPTIONS[0]),
    }


def _normalize_entity(case: str, entity: Any) -> dict[str, Any]:
    device_info = entity.device_info
    assert device_info is not None
    identifiers = sorted([list(identifier) for identifier in device_info["identifiers"]])
    strings_path = Path(__file__).parents[1] / "custom_components" / "casait_smarthome" / "strings.json"
    strings = json.loads(strings_path.read_text(encoding="utf-8"))
    if (translation_key := entity.translation_key) is not None:
        platform = entity.entity_id.split(".", 1)[0]
        template = strings["entity"][platform][translation_key]["name"]
        name = template.format(**getattr(entity, "_attr_translation_placeholders", {}))
    else:
        name = entity.name
    normalized = {
        "case": case,
        "entity_id": entity.entity_id,
        "unique_id": entity.unique_id,
        "name": name,
        "device_identifiers": identifiers,
        # A device named through the translations carries a key instead of a name.
        "device_name": device_info.get("name") or strings["device"][device_info["translation_key"]]["name"],
    }
    if via_device := device_info.get("via_device"):
        normalized["via_device"] = list(via_device)
    return normalized


def test_entity_contract_matches_golden_file() -> None:
    expected = json.loads((FIXTURES_DIR / "entity_contract.json").read_text(encoding="utf-8"))
    entities = _entities()

    assert [_normalize_entity(case, entities[case]) for case in entities] == expected
