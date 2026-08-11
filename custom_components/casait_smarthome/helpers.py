"""Helper utilities for casaIT integration."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass, replace
import re
from typing import Any

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.util import slugify

from .const import (
    DEFAULT_BLIND_CLOSE_TIME,
    DEFAULT_BLIND_OPEN_TIME,
    DEFAULT_BLIND_OVERRUN_TIME,
    DEFAULT_BLIND_TILT_TIME,
    DEFAULT_DOUBLE_CLICK_MS,
    DEFAULT_FAST_POLL_INTERVAL,
    DEFAULT_INPUT_DEBOUNCE_MS,
    DEFAULT_INPUT_ROLE,
    DEFAULT_LONG_PRESS_MS,
    DEFAULT_MAX_SEND_INTERVAL,
    DEFAULT_OW_PROFILE,
    DEFAULT_PULSE_DURATION,
    DEFAULT_SLOW_POLL_INTERVAL,
    DOMAIN,
    DS2413_CHANNEL_INPUT,
    DS2413_CHANNEL_OUTPUT,
    I2C_ADDR_RANGES,
    INPUT_ROLE_BUTTON,
    INPUT_ROLE_CONTACT,
    INPUT_ROLE_UNUSED,
    LEGACY_INPUT_ROLE_SWITCH,
    OM117_MODE_BLIND,
    OM117_MODE_PULSE,
    OM117_MODE_SHUTTER,
    OM117_MODE_SWITCH,
    OPT_DEBOUNCE_MS,
    OPT_DOUBLE_CLICK_MS,
    OPT_FAST_POLL_INTERVAL_MS,
    OPT_INPUTS,
    OPT_LONG_PRESS_MS,
    OPT_MAX_SEND_INTERVAL_MS,
    OPT_MODULES,
    OPT_NAME,
    OPT_ONEWIRE,
    OPT_PAIRS,
    OPT_PORTS,
    OPT_SETTINGS,
    OPT_SLOTS,
    OPT_SLOW_POLL_INTERVAL,
)
from .services.i2cClasses.dm117 import DeviceType

DM117_SLOT_PREFIX = "dm117_"
DM117_SLOT_SEPARATOR = "_slot_"

VALID_INPUT_ROLES = frozenset({INPUT_ROLE_BUTTON, INPUT_ROLE_CONTACT, INPUT_ROLE_UNUSED})

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

    source = config_entry_unique_id or config_entry_id
    identifier = "".join(character for character in source.casefold() if character.isalnum())
    if config_entry_unique_id is None:
        identifier = identifier[:12]
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
                mode=(
                    mode
                    if mode in {OM117_MODE_SWITCH, OM117_MODE_BLIND, OM117_MODE_SHUTTER, OM117_MODE_PULSE}
                    else OM117_MODE_SWITCH
                ),
                open_time=_coerce_time(raw.get("open_time"), DEFAULT_BLIND_OPEN_TIME),
                close_time=_coerce_time(raw.get("close_time"), DEFAULT_BLIND_CLOSE_TIME),
                overrun_time=_coerce_time(raw.get("overrun_time"), DEFAULT_BLIND_OVERRUN_TIME),
                tilt_time=_coerce_time(raw.get("tilt_time"), DEFAULT_BLIND_TILT_TIME),
                pulse_duration=_coerce_time(raw.get("pulse_duration"), DEFAULT_PULSE_DURATION),
            )

    return pair_map


@dataclass
class OM117PairConfig:
    """Configuration for an OM117 output pair."""

    mode: str = OM117_MODE_SWITCH
    open_time: float = DEFAULT_BLIND_OPEN_TIME
    close_time: float = DEFAULT_BLIND_CLOSE_TIME
    overrun_time: float = DEFAULT_BLIND_OVERRUN_TIME
    tilt_time: float = DEFAULT_BLIND_TILT_TIME
    pulse_duration: float = DEFAULT_PULSE_DURATION


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


@dataclass
class DigitalInputConfig:
    """What a single digital input is wired to.

    Shared by every input module: IM117 ports, DM117 input slots and DS2413 input
    channels differ in how they are addressed and read, not in what can be said
    about them.
    """

    role: str = DEFAULT_INPUT_ROLE
    device_class: str | None = None
    invert: bool = False
    repeat: bool = False


@dataclass
class InputModuleSettings:
    """Settings a whole input module shares.

    Debouncing is per module rather than per port: the bridge scanner takes one
    debounce value for every address it samples, and the driver applies it to a
    whole chip.
    """

    debounce_ms: int = DEFAULT_INPUT_DEBOUNCE_MS


@dataclass
class InputSettings:
    """Timing thresholds shared by all button inputs."""

    long_press_ms: int = DEFAULT_LONG_PRESS_MS
    double_click_ms: int = DEFAULT_DOUBLE_CLICK_MS


@dataclass
class PollingSettings:
    """Polling cadence and adaptive transport limit."""

    fast_poll_interval: float = DEFAULT_FAST_POLL_INTERVAL
    slow_poll_interval: float = DEFAULT_SLOW_POLL_INTERVAL
    max_send_interval: float = DEFAULT_MAX_SEND_INTERVAL


def _parse_input_config(raw: Any, *, allow_button: bool = True) -> DigitalInputConfig | None:
    """Read one stored input entry, or None when it is not an entry at all.

    ``allow_button`` is False for inputs that are sampled too slowly to derive
    gestures from; a stored button role degrades to a contact there instead of
    producing an event entity that would miss half of what it reports.
    """

    if not isinstance(raw, Mapping):
        return None

    role = str(raw.get("role", DEFAULT_INPUT_ROLE))
    if role == LEGACY_INPUT_ROLE_SWITCH:
        # A switch was a contact without a device class; it never was its own thing.
        role = INPUT_ROLE_CONTACT
    if role not in VALID_INPUT_ROLES or (role == INPUT_ROLE_BUTTON and not allow_button):
        role = DEFAULT_INPUT_ROLE

    device_class = raw.get("device_class")
    return DigitalInputConfig(
        role=role,
        device_class=str(device_class) if role == INPUT_ROLE_CONTACT and device_class else None,
        invert=bool(raw.get("invert", False)),
        repeat=role == INPUT_ROLE_BUTTON and bool(raw.get("repeat", False)),
    )


def _input_config_entry(config: DigitalInputConfig) -> dict[str, Any]:
    """Return the storable form of one input, omitting everything left at default."""

    entry: dict[str, Any] = {"role": config.role}
    if config.role == INPUT_ROLE_CONTACT and config.device_class:
        entry["device_class"] = config.device_class
    if config.invert:
        entry["invert"] = True
    if config.role == INPUT_ROLE_BUTTON and config.repeat:
        entry["repeat"] = True
    return entry


def get_im117_port_configuration(options: Mapping[str, Any]) -> dict[int, dict[int, DigitalInputConfig]]:
    """Return the configured role of every IM117 input port, keyed by address.

    Ports without an entry are absent; callers fall back to DEFAULT_INPUT_ROLE so
    that a freshly discovered module still produces the binary sensors it always
    did.
    """

    port_map: dict[int, dict[int, DigitalInputConfig]] = defaultdict(dict)

    for address, module in _module_entries(options, "im117").items():
        for port_index, raw in _index_items(_section(module, OPT_PORTS), 8):
            if (config := _parse_input_config(raw)) is not None:
                port_map[address][port_index] = config

    return port_map


def get_dm117_input_configuration(options: Mapping[str, Any]) -> dict[int, dict[tuple[int, int], DigitalInputConfig]]:
    """Return the configured DM117 input channels, keyed by address.

    The inner key is ``(slot index, channel)`` with channel 0 for A and 1 for B.
    Only slots typed as an input carry a configuration; the slot type itself stays
    in its own section so the output platforms keep reading what they always did.
    """

    input_map: dict[int, dict[tuple[int, int], DigitalInputConfig]] = defaultdict(dict)
    slot_types = get_dm117_port_configuration(options)

    for address, module in _module_entries(options, "dm117").items():
        inputs = _section(module, OPT_INPUTS)
        for slot_index, _ in sorted(slot_types.get(address, {}).items()):
            if slot_types[address][slot_index] is not DeviceType.INPUT:
                continue
            channels = _section(inputs, str(slot_index + 1))
            for channel in range(2):
                raw = channels.get(str(channel + 1))
                config = _parse_input_config(raw) if raw is not None else DigitalInputConfig()
                if config is not None:
                    input_map[address][slot_index, channel] = config

    return input_map


def get_ds2413_input_configuration(options: Mapping[str, Any]) -> dict[str, dict[int, DigitalInputConfig]]:
    """Return the configured DS2413 input channels, keyed by device id.

    DS2413 channels are read one 1-Wire transaction at a time on a slow cadence,
    which is why they never carry a button role - see ``_parse_input_config``.
    """

    input_map: dict[str, dict[int, DigitalInputConfig]] = {}

    for device_id, channels in get_configured_ds2413_channels(options).items():
        stored = _section(_onewire_entries(options).get(device_id, {}), OPT_INPUTS)
        configured: dict[int, DigitalInputConfig] = {}
        for channel, role in channels.items():
            if role != DS2413_CHANNEL_INPUT:
                continue
            raw = stored.get(str(channel + 1))
            config = _parse_input_config(raw, allow_button=False) if raw is not None else DigitalInputConfig()
            configured[channel] = config if config is not None else DigitalInputConfig()
        if configured:
            input_map[device_id] = configured

    return input_map


def get_input_module_settings(options: Mapping[str, Any], module_kind: str) -> dict[int, InputModuleSettings]:
    """Return the per-module input settings of one I2C module kind, keyed by address."""

    settings: dict[int, InputModuleSettings] = {}
    for address, module in _module_entries(options, module_kind).items():
        debounce = _bounded_int(module.get(OPT_DEBOUNCE_MS), 0, 255)
        settings[address] = InputModuleSettings(debounce_ms=DEFAULT_INPUT_DEBOUNCE_MS if debounce is None else debounce)
    return settings


def get_input_settings(options: Mapping[str, Any]) -> InputSettings:
    """Return the button timing thresholds, falling back to the defaults."""

    settings = _section(options, OPT_SETTINGS)
    long_press = _bounded_int(settings.get(OPT_LONG_PRESS_MS), 100, 5000)
    double_click = _bounded_int(settings.get(OPT_DOUBLE_CLICK_MS), 0, 2000)
    return InputSettings(
        long_press_ms=DEFAULT_LONG_PRESS_MS if long_press is None else long_press,
        double_click_ms=DEFAULT_DOUBLE_CLICK_MS if double_click is None else double_click,
    )


def get_polling_settings(options: Mapping[str, Any]) -> PollingSettings:
    """Return polling and transport settings, falling back to defaults."""

    settings = _section(options, OPT_SETTINGS)
    fast_ms = _bounded_float(settings.get(OPT_FAST_POLL_INTERVAL_MS), 5.0, 1000.0)
    slow_seconds = _bounded_float(settings.get(OPT_SLOW_POLL_INTERVAL), 1.0, 3600.0)
    max_send_ms = _bounded_float(settings.get(OPT_MAX_SEND_INTERVAL_MS), 1.0, 20.0)
    return PollingSettings(
        fast_poll_interval=DEFAULT_FAST_POLL_INTERVAL if fast_ms is None else fast_ms / 1000,
        slow_poll_interval=DEFAULT_SLOW_POLL_INTERVAL if slow_seconds is None else slow_seconds,
        max_send_interval=DEFAULT_MAX_SEND_INTERVAL if max_send_ms is None else max_send_ms / 1000,
    )


def set_im117_ports(
    options: Mapping[str, Any],
    address: int,
    ports: Mapping[int, DigitalInputConfig],
    *,
    name: str | None = None,
    debounce_ms: int | None = None,
) -> dict[str, Any]:
    """Return options with one IM117 module's port configuration replaced."""

    updated = deepcopy(dict(options))
    section = _mutable_section(updated, OPT_MODULES, "im117", str(address))
    _set_module_name(section, name)
    if debounce_ms is not None:
        section[OPT_DEBOUNCE_MS] = debounce_ms
    section[OPT_PORTS] = {str(index + 1): _input_config_entry(config) for index, config in sorted(ports.items())}
    return updated


def set_dm117_inputs(
    options: Mapping[str, Any],
    address: int,
    inputs: Mapping[tuple[int, int], DigitalInputConfig],
    *,
    debounce_ms: int | None = None,
) -> dict[str, Any]:
    """Return options with one DM117 module's input channels replaced.

    The slot types stay untouched: which slots are inputs is decided in the slot
    step, this only describes what the two channels of such a slot are wired to.
    """

    updated = deepcopy(dict(options))
    section = _mutable_section(updated, OPT_MODULES, "dm117", str(address))
    if debounce_ms is not None:
        section[OPT_DEBOUNCE_MS] = debounce_ms

    stored: dict[str, dict[str, Any]] = defaultdict(dict)
    for (slot_index, channel), config in sorted(inputs.items()):
        stored[str(slot_index + 1)][str(channel + 1)] = _input_config_entry(config)
    section[OPT_INPUTS] = dict(stored)
    return updated


def set_ds2413_inputs(
    options: Mapping[str, Any],
    device_id: str,
    inputs: Mapping[int, DigitalInputConfig],
) -> dict[str, Any]:
    """Return options with one DS2413's input channel configuration replaced.

    Call this after ``set_onewire_device``, which rewrites the whole device entry.
    A button role is stored as a contact: the channel is read one 1-Wire
    transaction at a time and cannot carry a gesture.
    """

    updated = deepcopy(dict(options))
    section = _mutable_section(updated, OPT_ONEWIRE, device_id)
    section[OPT_INPUTS] = {
        str(channel + 1): _input_config_entry(
            replace(config, role=INPUT_ROLE_CONTACT) if config.role == INPUT_ROLE_BUTTON else config
        )
        for channel, config in sorted(inputs.items())
    }
    return updated


def set_input_settings(options: Mapping[str, Any], settings: InputSettings) -> dict[str, Any]:
    """Return options with the shared button timings replaced."""

    updated = deepcopy(dict(options))
    section = _mutable_section(updated, OPT_SETTINGS)
    section[OPT_LONG_PRESS_MS] = settings.long_press_ms
    section[OPT_DOUBLE_CLICK_MS] = settings.double_click_ms
    return updated


def set_polling_settings(options: Mapping[str, Any], settings: PollingSettings) -> dict[str, Any]:
    """Return options with polling and transport settings replaced."""

    updated = deepcopy(dict(options))
    section = _mutable_section(updated, OPT_SETTINGS)
    section[OPT_FAST_POLL_INTERVAL_MS] = round(settings.fast_poll_interval * 1000, 3)
    section[OPT_SLOW_POLL_INTERVAL] = settings.slow_poll_interval
    section[OPT_MAX_SEND_INTERVAL_MS] = round(settings.max_send_interval * 1000, 3)
    return updated


def get_module_name(options: Mapping[str, Any], module_kind: str, address: int, default: str) -> str:
    """Return a configured I2C module name or its supplied default."""

    module = _module_entries(options, module_kind).get(address, {})
    name = str(module.get(OPT_NAME, "")).strip()
    return name or default


def get_configured_module_addresses(options: Mapping[str, Any]) -> dict[str, set[int]]:
    """Return module addresses which have an explicit options entry."""

    return {
        module_kind: set(_module_entries(options, module_kind)) for module_kind in ("im117", "om117", "dm117", "sm117")
    }


def _set_module_name(section: dict[str, Any], name: str | None) -> None:
    """Update a module name when one was supplied by a caller."""

    if name is None:
        return
    if cleaned := name.strip():
        section[OPT_NAME] = cleaned
    else:
        section.pop(OPT_NAME, None)


def set_module_name(options: Mapping[str, Any], module_kind: str, address: int, name: str) -> dict[str, Any]:
    """Return options with the display name for one I2C module replaced."""

    updated = deepcopy(dict(options))
    section = _mutable_section(updated, OPT_MODULES, module_kind, str(address))
    _set_module_name(section, name)
    return updated


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


def _bounded_float(value: Any, low: float, high: float) -> float | None:
    """Return value as a float when it falls inside the inclusive bounds."""

    try:
        number = float(value)
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


def get_configured_ds2413_channels(options: Mapping[str, Any]) -> dict[str, dict[int, str]]:
    """Extract the independently configured role of each DS2413 channel.

    Legacy whole-device profiles are expanded to both channels so existing
    installations keep the same entities after upgrading.
    """

    configured: dict[str, dict[int, str]] = {}
    valid_roles = {DS2413_CHANNEL_INPUT, DS2413_CHANNEL_OUTPUT}
    for device_id, config in _onewire_entries(options).items():
        profile = str(config.get("profile") or "")
        if profile not in {"ds2413", "ds2413_in", "ds2413_out"}:
            continue

        fallback = DS2413_CHANNEL_OUTPUT if profile == "ds2413_out" else DS2413_CHANNEL_INPUT
        channels: dict[int, str] = {}
        raw_channels = config.get("channels")
        if isinstance(raw_channels, Mapping):
            for index, raw_role in _index_items(raw_channels, 2):
                role = str(raw_role)
                channels[index] = role if role in valid_roles else fallback
        for index in range(2):
            channels.setdefault(index, fallback)
        configured[device_id] = channels
    return configured


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
    *,
    name: str | None = None,
) -> dict[str, Any]:
    """Return options with one OM117 module's pair configuration replaced."""

    updated = deepcopy(dict(options))
    section = _mutable_section(updated, OPT_MODULES, "om117", str(address))
    _set_module_name(section, name)
    section[OPT_PAIRS] = {
        str(index + 1): {
            "mode": config.mode,
            "open_time": config.open_time,
            "close_time": config.close_time,
            "overrun_time": config.overrun_time,
            "tilt_time": config.tilt_time,
            "pulse_duration": config.pulse_duration,
        }
        for index, config in sorted(pairs.items())
    }
    return updated


def set_dm117_slots(
    options: Mapping[str, Any],
    address: int,
    slots: Mapping[int, str],
    *,
    name: str | None = None,
) -> dict[str, Any]:
    """Return options with one DM117 module's slot configuration replaced."""

    updated = deepcopy(dict(options))
    section = _mutable_section(updated, OPT_MODULES, "dm117", str(address))
    _set_module_name(section, name)
    section[OPT_SLOTS] = {str(index + 1): slot_type for index, slot_type in sorted(slots.items())}
    return updated


def set_onewire_device(
    options: Mapping[str, Any],
    device_id: str,
    profile: str,
    *,
    led_count: int | None = None,
    poll_interval: int | None = None,
    ds2413_channels: Mapping[int, str] | None = None,
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
    if ds2413_channels is not None:
        device["channels"] = {
            str(index + 1): role
            for index, role in sorted(ds2413_channels.items())
            if role in {DS2413_CHANNEL_INPUT, DS2413_CHANNEL_OUTPUT}
        }
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
