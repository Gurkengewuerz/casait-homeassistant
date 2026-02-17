"""Helper utilities for casaIT integration."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from typing import Any

from .const import DEFAULT_OW_PROFILE
from .services.i2cClasses.dm117 import DeviceType

DM117_SLOT_PREFIX = "dm117_"
DM117_SLOT_SEPARATOR = "_slot_"

SLOT_TYPE_TO_DEVICE_TYPE: dict[str, DeviceType] = {
    "binary_input": DeviceType.INPUT,
    "switch": DeviceType.OUTPUT,
    "dimmer": DeviceType.DIMMER,
}


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
        except (ValueError, AttributeError):
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
        except (TypeError, ValueError):
            continue

        if 1 <= count <= 255:
            counts[device_id] = count

    return counts


def default_onewire_profile(meta: Mapping[str, Any]) -> str | None:
    """Return the default OneWire profile for the provided metadata."""

    family_code = meta.get("family_code")
    if family_code is None:
        return None
    return DEFAULT_OW_PROFILE.get(family_code)
