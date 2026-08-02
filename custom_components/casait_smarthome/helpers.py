"""Helper utilities for casaIT integration."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from copy import deepcopy
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
    OPT_MODULES,
    OPT_ONEWIRE,
    OPT_PAIRS,
    OPT_SETTINGS,
    OPT_SLOTS,
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


def _section(options: Mapping[str, Any], *path: str) -> Mapping[str, Any]:
    """Return a nested options section, or an empty mapping when absent."""

    current: Any = options
    for key in path:
        if not isinstance(current, Mapping):
            return {}
        current = current.get(key)
    return current if isinstance(current, Mapping) else {}


def _module_entries(options: Mapping[str, Any], module: str) -> dict[int, Mapping[str, Any]]:
    """Return the configured entries for one module kind, keyed by address."""

    entries: dict[int, Mapping[str, Any]] = {}
    for raw_address, config in _section(options, OPT_MODULES, module).items():
        if not isinstance(config, Mapping):
            continue
        try:
            address = int(raw_address)
        except TypeError, ValueError:
            continue
        entries[address] = config
    return entries


def _index_items(section: Mapping[str, Any], count: int) -> list[tuple[int, Any]]:
    """Yield (zero-based index, value) for one-based string keys within range."""

    items: list[tuple[int, Any]] = []
    for raw_index, value in section.items():
        try:
            index = int(raw_index) - 1
        except TypeError, ValueError:
            continue
        if 0 <= index < count:
            items.append((index, value))
    return items


def get_om117_pair_configuration(options: Mapping[str, Any]) -> dict[int, dict[int, OM117PairConfig]]:
    """Build a mapping of OM117 addresses to configured pair modes and timings."""

    pair_map: dict[int, dict[int, OM117PairConfig]] = defaultdict(dict)

    for address, module in _module_entries(options, "om117").items():
        for pair_index, raw in _index_items(_section(module, OPT_PAIRS), 4):
            if not isinstance(raw, Mapping):
                continue
            mode = str(raw.get("mode", OM117_MODE_SWITCH))
            pair_map[address][pair_index] = OM117PairConfig(
                mode=mode if mode in {OM117_MODE_SWITCH, OM117_MODE_BLIND} else OM117_MODE_SWITCH,
                open_time=_coerce_time(raw.get("open_time"), DEFAULT_BLIND_OPEN_TIME),
                close_time=_coerce_time(raw.get("close_time"), DEFAULT_BLIND_CLOSE_TIME),
                overrun_time=_coerce_time(raw.get("overrun_time"), DEFAULT_BLIND_OVERRUN_TIME),
            )

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

    for address, module in _module_entries(options, "dm117").items():
        for slot_index, raw in _index_items(_section(module, OPT_SLOTS), 8):
            device_type = SLOT_TYPE_TO_DEVICE_TYPE.get(raw)
            if device_type is not None:
                slot_map[address][slot_index] = device_type

    return slot_map


def get_dm117_slot_types(options: Mapping[str, Any]) -> dict[int, dict[int, str]]:
    """Return the raw slot-type strings per DM117 address.

    The options flow needs the stored strings rather than the DeviceType values
    that get_dm117_port_configuration resolves them to, because unassigned slots
    ("none") have no DeviceType but still have to preselect in the form.
    """

    slot_map: dict[int, dict[int, str]] = defaultdict(dict)

    for address, module in _module_entries(options, "dm117").items():
        for slot_index, raw in _index_items(_section(module, OPT_SLOTS), 8):
            if isinstance(raw, str):
                slot_map[address][slot_index] = raw

    return slot_map


def _onewire_entries(options: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    """Return the configured 1-Wire devices, keyed by device id."""

    entries: dict[str, Mapping[str, Any]] = {}
    for device_id, config in _section(options, OPT_ONEWIRE).items():
        if device_id and isinstance(config, Mapping):
            entries[str(device_id)] = config
    return entries


def _bounded_int(value: Any, low: int, high: int) -> int | None:
    """Return value as an int when it falls inside the inclusive bounds."""

    try:
        number = int(value)
    except TypeError, ValueError:
        return None
    return number if low <= number <= high else None


def get_configured_onewire_profiles(options: Mapping[str, Any]) -> dict[str, str]:
    """Extract configured OneWire profiles from config entry options."""

    return {
        device_id: str(config["profile"])
        for device_id, config in _onewire_entries(options).items()
        if config.get("profile")
    }


def get_configured_led_counts(options: Mapping[str, Any]) -> dict[str, int]:
    """Extract configured LED counts for DS28E17 devices from options."""

    counts: dict[str, int] = {}
    for device_id, config in _onewire_entries(options).items():
        if (count := _bounded_int(config.get("led_count"), 1, 255)) is not None:
            counts[device_id] = count
    return counts


def get_configured_onewire_poll_intervals(options: Mapping[str, Any]) -> dict[str, int]:
    """Extract configured polling intervals for OneWire devices from options."""

    intervals: dict[str, int] = {}
    for device_id, config in _onewire_entries(options).items():
        if (interval := _bounded_int(config.get("poll_interval"), 1, 3600)) is not None:
            intervals[device_id] = interval
    return intervals


def _mutable_section(options: dict[str, Any], *path: str) -> dict[str, Any]:
    """Return a nested dict for writing, creating the path as needed."""

    current = options
    for key in path:
        existing = current.get(key)
        current[key] = dict(existing) if isinstance(existing, Mapping) else {}
        current = current[key]
    return current


def set_om117_pairs(
    options: Mapping[str, Any],
    address: int,
    pairs: Mapping[int, OM117PairConfig],
) -> dict[str, Any]:
    """Return options with one OM117 module's pair configuration replaced."""

    updated = deepcopy(dict(options))
    section = _mutable_section(updated, OPT_MODULES, "om117", str(address))
    section[OPT_PAIRS] = {
        str(index + 1): {
            "mode": config.mode,
            "open_time": config.open_time,
            "close_time": config.close_time,
            "overrun_time": config.overrun_time,
        }
        for index, config in sorted(pairs.items())
    }
    return updated


def set_dm117_slots(options: Mapping[str, Any], address: int, slots: Mapping[int, str]) -> dict[str, Any]:
    """Return options with one DM117 module's slot configuration replaced."""

    updated = deepcopy(dict(options))
    section = _mutable_section(updated, OPT_MODULES, "dm117", str(address))
    section[OPT_SLOTS] = {str(index + 1): slot_type for index, slot_type in sorted(slots.items())}
    return updated


def set_onewire_device(
    options: Mapping[str, Any],
    device_id: str,
    profile: str,
    *,
    led_count: int | None = None,
    poll_interval: int | None = None,
) -> dict[str, Any]:
    """Return options with one 1-Wire device's configuration replaced."""

    updated = deepcopy(dict(options))
    device = _mutable_section(updated, OPT_ONEWIRE, device_id)
    device.clear()
    device["profile"] = profile
    if led_count is not None:
        device["led_count"] = led_count
    if poll_interval is not None:
        device["poll_interval"] = poll_interval
    return updated


def migrate_options_to_nested(options: Mapping[str, Any]) -> dict[str, Any]:
    """Convert the flat option namespace used up to entry version 2.

    Keys that match no known pattern are carried over untouched, so a stray
    option is never silently dropped. Already-nested sections are preserved,
    which makes the conversion safe to run more than once.
    """

    migrated = deepcopy(dict(options))
    modules = _mutable_section(migrated, OPT_MODULES)
    onewire = _mutable_section(migrated, OPT_ONEWIRE)

    def module_section(kind: str, address: int, group: str) -> dict[str, Any]:
        by_address = modules.setdefault(kind, {})
        module = by_address.setdefault(str(address), {})
        return module.setdefault(group, {})

    for key, value in options.items():
        if key in (OPT_MODULES, OPT_ONEWIRE, OPT_SETTINGS):
            continue

        if key.startswith("om117_") and "_pair_" in key:
            try:
                address_part, rest = key.removeprefix("om117_").split("_pair_", 1)
                address = int(address_part)
                pair_part, field = rest.split("_", 1)
                pair = int(pair_part)
            except ValueError:
                continue
            if field in {"mode", "open_time", "close_time", "overrun_time"} and 1 <= pair <= 4:
                module_section("om117", address, OPT_PAIRS).setdefault(str(pair), {})[field] = value
                migrated.pop(key, None)
            continue

        if key.startswith(DM117_SLOT_PREFIX) and DM117_SLOT_SEPARATOR in key:
            try:
                address_part, slot_part = key.removeprefix(DM117_SLOT_PREFIX).split(DM117_SLOT_SEPARATOR)
                address = int(address_part)
                slot = int(slot_part)
            except ValueError:
                continue
            if 1 <= slot <= 8:
                module_section("dm117", address, OPT_SLOTS)[str(slot)] = value
                migrated.pop(key, None)
            continue

        if key.startswith("ow_"):
            for suffix, field in (
                ("_poll_interval", "poll_interval"),
                ("_led_count", "led_count"),
                ("_profile", "profile"),
            ):
                if key.endswith(suffix) and (device_id := key[3 : -len(suffix)]):
                    onewire.setdefault(device_id, {})[field] = value
                    migrated.pop(key, None)
                    break

    for section_key in (OPT_MODULES, OPT_ONEWIRE):
        if not migrated.get(section_key):
            migrated.pop(section_key, None)

    return migrated


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
