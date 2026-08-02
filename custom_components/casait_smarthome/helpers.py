"""Helper utilities for casaIT integration."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
import re
from typing import Any

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.util import slugify

from .const import (
    DEFAULT_BLIND_CLOSE_TIME,
    DEFAULT_BLIND_OPEN_TIME,
    DEFAULT_BLIND_OVERRUN_TIME,
    DEFAULT_OW_PROFILE,
    DOMAIN,
    I2C_ADDR_RANGES,
    OM117_MODE_BLIND,
    OM117_MODE_SWITCH,
)
from .services.i2cClasses.dm117 import DeviceType

DM117_SLOT_PREFIX = "dm117_"
DM117_SLOT_SEPARATOR = "_slot_"

SLOT_TYPE_TO_DEVICE_TYPE: dict[str, DeviceType] = {
    "binary_input": DeviceType.INPUT,
    "switch": DeviceType.OUTPUT,
    "dimmer": DeviceType.DIMMER,
}

ONEWIRE_MODEL_BY_FAMILY = {
    "19": "DS28E17",
    "26": "DS2438",
    "28": "DS18B20",
    "3a": "DS2413",
}


def build_entity_id(entity_domain: str, *parts: str | int) -> str:
    """Return a canonical Home Assistant entity ID."""

    object_id = slugify("_".join(str(part) for part in parts))
    return f"{entity_domain}.{object_id}"


def build_bridge_slug(config_entry_id: str, config_entry_unique_id: str | None) -> str:
    """Return a stable, entity-ID-safe bridge identifier."""

    source = config_entry_unique_id or config_entry_id[:12]
    identifier = "".join(character for character in source.casefold() if character.isalnum())
    if not identifier:
        identifier = "".join(character for character in config_entry_id.casefold() if character.isalnum())[:12]
    return f"bridge_{identifier}"


def build_device_identifier(config_entry_id: str, device_kind: str, device_id: str | int) -> str:
    """Return a device-registry identifier scoped to one bridge config entry."""

    return f"{config_entry_id}_{device_kind}_{device_id}"


def build_i2c_entity_id(
    entity_domain: str,
    bridge_slug: str,
    module: str,
    address: int,
    *parts: str | int,
) -> str:
    """Return a canonical entity ID for an I2C module function."""

    return build_entity_id(entity_domain, bridge_slug, module, f"0x{address:02x}", *parts)


def build_onewire_entity_id(
    entity_domain: str,
    bridge_slug: str,
    device_id: str,
    meta: Mapping[str, Any],
    *parts: str | int,
) -> str:
    """Return a canonical entity ID for a 1-Wire device function."""

    model = str(meta.get("device_type") or ONEWIRE_MODEL_BY_FAMILY.get(device_id[:2].lower(), "onewire"))
    return build_entity_id(entity_domain, bridge_slug, model, device_id, *parts)


def migrated_entity_identity(
    config_entry_id: str,
    config_entry_unique_id: str | None,
    entity_domain: str,
    unique_id: str,
) -> tuple[str, str] | None:
    """Return bridge-scoped entity and unique IDs for a legacy unique ID."""

    bridge_slug = build_bridge_slug(config_entry_id, config_entry_unique_id)

    if match := re.fullmatch(rf"{re.escape(DOMAIN)}_(\d+)_(\d+)", unique_id):
        address, port = (int(value) for value in match.groups())
        if entity_domain == "binary_sensor":
            return (
                build_i2c_entity_id(entity_domain, bridge_slug, "im117", address, "input", port + 1),
                f"{config_entry_id}_im117_{address}_{port}",
            )
        if entity_domain == "switch":
            return (
                build_i2c_entity_id(entity_domain, bridge_slug, "om117", address, "output", port + 1),
                f"{config_entry_id}_om117_{address}_{port}",
            )

    entry_prefix = re.escape(config_entry_id)
    if entity_domain == "binary_sensor" and (
        match := re.fullmatch(rf"{entry_prefix}_dm117_(\d+)_(\d+)_input_(\d+)", unique_id)
    ):
        address, port, channel = (int(value) for value in match.groups())
        return (
            build_i2c_entity_id(
                "binary_sensor",
                bridge_slug,
                "dm117",
                address,
                "slot",
                port + 1,
                "input",
                "a" if channel == 0 else "b",
            ),
            unique_id,
        )
    if entity_domain == "switch" and (
        match := re.fullmatch(rf"{entry_prefix}_dm117_(\d+)_(\d+)_output_(\d+)", unique_id)
    ):
        address, port, channel = (int(value) for value in match.groups())
        return (
            build_i2c_entity_id(
                "switch",
                bridge_slug,
                "dm117",
                address,
                "slot",
                port + 1,
                "output",
                "a" if channel == 0 else "b",
            ),
            unique_id,
        )
    if entity_domain == "light" and (match := re.fullmatch(rf"{entry_prefix}_dm117_(\d+)_(\d+)_dimmer", unique_id)):
        address, port = (int(value) for value in match.groups())
        return (
            build_i2c_entity_id("light", bridge_slug, "dm117", address, "slot", port + 1, "dimmer"),
            unique_id,
        )
    if entity_domain == "cover" and (match := re.fullmatch(rf"{entry_prefix}_om117_(\d+)_pair_(\d+)_blind", unique_id)):
        address, pair = (int(value) for value in match.groups())
        return (build_i2c_entity_id("cover", bridge_slug, "om117", address, "blind", pair), unique_id)
    if entity_domain == "sensor" and unique_id == f"{config_entry_id}_debug":
        return (build_entity_id("sensor", bridge_slug, "diagnostics"), unique_id)

    if match := re.fullmatch(r"([0-9a-fA-F]+)_channel_(\d+)_(input|output)", unique_id):
        device_id, channel_raw, direction = match.groups()
        channel = "a" if int(channel_raw) == 0 else "b"
        target_platform = "binary_sensor" if direction == "input" else "switch"
        if entity_domain == target_platform:
            return (
                build_entity_id(target_platform, bridge_slug, "ds2413", device_id, direction, channel),
                f"{config_entry_id}_{unique_id}",
            )
    if entity_domain == "light" and (match := re.fullmatch(r"([0-9a-fA-F]+)_led_controller", unique_id)):
        return (
            build_entity_id("light", bridge_slug, "ds28e17", match.group(1), "led", "controller"),
            f"{config_entry_id}_{unique_id}",
        )
    if entity_domain == "sensor" and (
        match := re.fullmatch(r"([0-9a-fA-F]+)_(temperature|humidity|illuminance)", unique_id)
    ):
        device_id, measurement = match.groups()
        model = ONEWIRE_MODEL_BY_FAMILY.get(device_id[:2].lower(), "onewire")
        return (
            build_entity_id("sensor", bridge_slug, model, device_id, measurement),
            f"{config_entry_id}_{unique_id}",
        )

    return None


def migrated_device_identifiers(
    config_entry_id: str,
    identifiers: set[tuple[str, str]],
) -> set[tuple[str, str]] | None:
    """Return bridge-scoped identifiers for a legacy device registry entry."""

    migrated = set(identifiers)
    changed = False
    input_range = get_address_range("IM117")
    output_range = get_address_range("OM117")

    for identifier in identifiers:
        identifier_domain, legacy_id = identifier
        if identifier_domain != DOMAIN:
            continue

        device_kind: str | None = None
        device_id: str | int = legacy_id
        if legacy_id == config_entry_id:
            device_kind, device_id = "bridge", "controller"
        elif legacy_id.isdecimal():
            address = int(legacy_id)
            if input_range and input_range[0] <= address <= input_range[1]:
                device_kind = "im117"
            elif output_range and output_range[0] <= address <= output_range[1]:
                device_kind = "om117"
            device_id = address
        else:
            for prefix in ("dm117_", "sm117_", "onewire_"):
                if legacy_id.startswith(prefix):
                    device_kind = prefix.removesuffix("_")
                    device_id = legacy_id.removeprefix(prefix)
                    break

        if device_kind is None:
            continue

        migrated.remove(identifier)
        migrated.add((DOMAIN, build_device_identifier(config_entry_id, device_kind, device_id)))
        changed = True

    return migrated if changed else None


def get_address_range(code: str) -> tuple[int, int] | None:
    """Return the (start, end) I2C address range configured for a module code."""

    return next(((start, end) for start, end, _, module_code in I2C_ADDR_RANGES if module_code == code), None)


def _coerce_time(value: Any, default: float) -> float:
    """Return a float value, falling back to default on error."""

    try:
        return float(value)
    except TypeError, ValueError:
        return default


def get_om117_pair_configuration(options: Mapping[str, Any]) -> dict[int, dict[int, OM117PairConfig]]:
    """Build a mapping of OM117 addresses to configured pair modes and timings."""

    pair_map: dict[int, dict[int, OM117PairConfig]] = defaultdict(dict)

    for key, value in options.items():
        if not key.startswith("om117_") or "_pair_" not in key:
            continue

        try:
            addr_part, rest = key.removeprefix("om117_").split("_pair_", 1)
            address = int(addr_part)
            pair_part, field = rest.split("_", 1)
            pair_index = int(pair_part) - 1
        except ValueError, AttributeError:
            continue

        if pair_index < 0 or pair_index > 3:
            continue

        config = pair_map[address].get(pair_index, OM117PairConfig())

        if field == "mode":
            mode = str(value)
            config.mode = mode if mode in {OM117_MODE_SWITCH, OM117_MODE_BLIND} else OM117_MODE_SWITCH
        elif field == "open_time":
            config.open_time = _coerce_time(value, DEFAULT_BLIND_OPEN_TIME)
        elif field == "close_time":
            config.close_time = _coerce_time(value, DEFAULT_BLIND_CLOSE_TIME)
        elif field == "overrun_time":
            config.overrun_time = _coerce_time(value, DEFAULT_BLIND_OVERRUN_TIME)

        pair_map[address][pair_index] = config

    return pair_map


@dataclass
class OM117PairConfig:
    """Configuration for an OM117 output pair."""

    mode: str = OM117_MODE_SWITCH
    open_time: float = DEFAULT_BLIND_OPEN_TIME
    close_time: float = DEFAULT_BLIND_CLOSE_TIME
    overrun_time: float = DEFAULT_BLIND_OVERRUN_TIME


def get_dm117_port_configuration(
    options: Mapping[str, Any],
) -> dict[int, dict[int, DeviceType]]:
    """Build a mapping of DM117 addresses to configured port types."""

    slot_map: dict[int, dict[int, DeviceType]] = defaultdict(dict)
    for key, value in options.items():
        if not key.startswith(DM117_SLOT_PREFIX) or DM117_SLOT_SEPARATOR not in key:
            continue

        try:
            addr_part, slot_part = key.removeprefix(DM117_SLOT_PREFIX).split(DM117_SLOT_SEPARATOR)
            address = int(addr_part)
            slot_index = int(slot_part)
        except ValueError, AttributeError:
            continue

        device_type = SLOT_TYPE_TO_DEVICE_TYPE.get(value)
        if device_type is None:
            continue
        if slot_index <= 0:
            continue

        slot_map[address][slot_index - 1] = device_type

    return slot_map


def get_configured_onewire_profiles(options: Mapping[str, Any]) -> dict[str, str]:
    """Extract configured OneWire profiles from config entry options."""

    profiles: dict[str, str] = {}
    for key, profile in options.items():
        if not key.startswith("ow_") or not key.endswith("_profile"):
            continue
        device_id = key[3:-8]
        if device_id:
            profiles[device_id] = profile
    return profiles


def get_configured_led_counts(options: Mapping[str, Any]) -> dict[str, int]:
    """Extract configured LED counts for DS28E17 devices from options."""

    counts: dict[str, int] = {}
    for key, value in options.items():
        if not key.startswith("ow_") or not key.endswith("_led_count"):
            continue

        device_id = key[3:-10]
        if not device_id:
            continue

        try:
            count = int(value)
        except TypeError, ValueError:
            continue

        if 1 <= count <= 255:
            counts[device_id] = count

    return counts


def get_configured_onewire_poll_intervals(options: Mapping[str, Any]) -> dict[str, int]:
    """Extract configured polling intervals for OneWire devices from options."""

    intervals: dict[str, int] = {}
    for key, value in options.items():
        if not key.startswith("ow_") or not key.endswith("_poll_interval"):
            continue

        device_id = key[3:-14]
        if not device_id:
            continue

        try:
            interval = int(value)
        except TypeError, ValueError:
            continue

        if 1 <= interval <= 3600:
            intervals[device_id] = interval

    return intervals


def default_onewire_profile(meta: Mapping[str, Any]) -> str | None:
    """Return the default OneWire profile for the provided metadata."""

    family_code = meta.get("family_code")
    if family_code is None:
        return None
    return DEFAULT_OW_PROFILE.get(family_code)


def build_onewire_device_info(config_entry_id: str, device_id: str, meta: Mapping[str, Any]) -> DeviceInfo:
    """Return DeviceInfo for a 1-Wire device linked through its SM117 bus."""

    bus_address = meta.get("bus_address")
    device_type = str(meta.get("device_type") or "").strip()
    device_info = DeviceInfo(
        identifiers={(DOMAIN, build_device_identifier(config_entry_id, "onewire", device_id))},
        name=f"{device_type or 'OneWire'} {device_id}",
        model=device_type or "OneWire",
        manufacturer="Maxim Integrated",
    )
    if bus_address is not None:
        device_info["via_device"] = (
            DOMAIN,
            build_device_identifier(config_entry_id, "sm117", f"{int(bus_address):02x}"),
        )
    return device_info
