"""Direct links the bridge runs on its own while Home Assistant is away.

Every IM117 port and DM117 input channel can name an OM117 output or cover as its
emergency target. The bridge keeps the resulting list in flash. As long as Home
Assistant watches the modules, the links rest and automations decide what a
button does; once no client has watched the bridge for a while - Home Assistant
down, network gone, or right after a power cut with nobody connected yet - the
bridge reads the buttons itself and switches.

A target is stored as a string on the input:

- ``om117:<address>:<port>`` toggles a switch output, or pulses it when its pair
  is in pulse mode
- ``cover:<address>:<pair>:<up|down|toggle>`` drives a shutter or blind pair;
  ``toggle`` alternates the direction for a single button. A cover that moves
  stops at any press, the next one starts it again.

Ports and pairs are zero-based as in the rest of the options.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
import logging
import math
from typing import Any

from homeassistant.helpers.selector import SelectOptionDict

from .const import (
    INPUT_ROLE_CONTACT,
    OM117_MODE_BLIND,
    OM117_MODE_PULSE,
    OM117_MODE_SHUTTER,
    OM117_MODE_SWITCH,
    PCF8574_MAPPED_PORTS,
)
from .helpers import (
    DigitalInputConfig,
    InputModuleSettings,
    OM117PairConfig,
    get_dm117_input_configuration,
    get_im117_port_configuration,
    get_input_module_settings,
    get_module_name,
    get_om117_pair_configuration,
)
from .services.smbus_proxy import (
    LINK_ACTION_COVER_DOWN,
    LINK_ACTION_COVER_TOGGLE,
    LINK_ACTION_COVER_UP,
    LINK_ACTION_PULSE,
    LINK_ACTION_TOGGLE,
    LINK_FLAG_ACTIVE_HIGH,
    LINK_FLAG_BOTH_EDGES,
    LINK_SOURCE_DM117,
    LINK_SOURCE_PCF,
    MAX_LINK_TIME_DS,
    MAX_LINKS,
    BridgeLink,
)

_LOGGER = logging.getLogger(__name__)

EMERGENCY_NONE = "none"
COVER_ACTIONS = {"up": LINK_ACTION_COVER_UP, "down": LINK_ACTION_COVER_DOWN, "toggle": LINK_ACTION_COVER_TOGGLE}
# Language-neutral marks for the target picker; the field description explains them.
COVER_MARKS = {"up": "▲", "down": "▼", "toggle": "▲▼"}
TOGGLE_MARK = "⇄"
PULSE_MARK = "⊓"


@dataclass(frozen=True)
class EmergencyTarget:
    """An output or cover an input acts on in emergency operation."""

    kind: str  # "om117" or "cover"
    address: int
    index: int  # port for an output, pair for a cover
    action: str | None = None

    @property
    def key(self) -> str:
        """Return the string stored in the options."""

        if self.kind == "cover":
            return f"cover:{self.address}:{self.index}:{self.action}"
        return f"om117:{self.address}:{self.index}"


def parse_target(key: str | None) -> EmergencyTarget | None:
    """Read a stored target, None for no or a malformed one."""

    if not key or key == EMERGENCY_NONE:
        return None
    parts = key.split(":")
    try:
        if parts[0] == "om117" and len(parts) == 3 and 0 <= int(parts[2]) < 8:
            return EmergencyTarget("om117", int(parts[1]), int(parts[2]))
        if parts[0] == "cover" and len(parts) == 4 and 0 <= int(parts[2]) < 4 and parts[3] in COVER_ACTIONS:
            return EmergencyTarget("cover", int(parts[1]), int(parts[2]), parts[3])
    except ValueError:
        pass
    return None


def emergency_target_choices(options: Mapping[str, Any], om117_addresses: Iterable[int]) -> list[SelectOptionDict]:
    """List every target the OM117 modules offer with their current pair modes."""

    choices: list[SelectOptionDict] = [{"value": EMERGENCY_NONE, "label": "—"}]
    pairs = get_om117_pair_configuration(options)
    for address in sorted(set(om117_addresses)):
        base = f"OM117 0x{address:02X}"
        if name := get_module_name(options, "om117", address, ""):
            base = f"{base} · {name}"
        for pair in range(4):
            mode = pairs.get(address, {}).get(pair, OM117PairConfig()).mode
            if mode in {OM117_MODE_SHUTTER, OM117_MODE_BLIND}:
                for action, mark in COVER_MARKS.items():
                    target = EmergencyTarget("cover", address, pair, action)
                    choices.append({"value": target.key, "label": f"{base} · {pair * 2 + 1}/{pair * 2 + 2} {mark}"})
                continue
            mark = PULSE_MARK if mode == OM117_MODE_PULSE else TOGGLE_MARK
            for port in (pair * 2, pair * 2 + 1):
                target = EmergencyTarget("om117", address, port)
                choices.append({"value": target.key, "label": f"{base} · {port + 1} {mark}"})
    return choices


def _tenths(seconds: float) -> int:
    return max(1, min(MAX_LINK_TIME_DS, math.ceil(seconds * 10)))


def _resolve(
    target: EmergencyTarget, pairs: Mapping[int, Mapping[int, OM117PairConfig]]
) -> tuple[int, int, int, int] | None:
    """Return (action, bit A, bit B, time) for a target, None when its pair changed mode since."""

    if target.kind == "cover":
        config = pairs.get(target.address, {}).get(target.index, OM117PairConfig())
        if config.mode not in {OM117_MODE_SHUTTER, OM117_MODE_BLIND} or target.action is None:
            return None
        travel = {
            "up": config.open_time,
            "down": config.close_time,
            "toggle": max(config.open_time, config.close_time),
        }[target.action]
        return (
            COVER_ACTIONS[target.action],
            PCF8574_MAPPED_PORTS[target.index * 2],
            PCF8574_MAPPED_PORTS[target.index * 2 + 1],
            _tenths(travel + config.overrun_time),
        )

    config = pairs.get(target.address, {}).get(target.index // 2, OM117PairConfig())
    bit = PCF8574_MAPPED_PORTS[target.index]
    if config.mode == OM117_MODE_SWITCH:
        return LINK_ACTION_TOGGLE, bit, 0, 0
    if config.mode == OM117_MODE_PULSE:
        return LINK_ACTION_PULSE, bit, 0, _tenths(config.pulse_duration)
    return None


def _link(
    kind: int,
    address: int,
    bit: int,
    config: DigitalInputConfig,
    active_high: bool,
    pairs: Mapping[int, Mapping[int, OM117PairConfig]],
) -> BridgeLink | None:
    if (target := parse_target(config.emergency)) is None:
        return None
    if (resolved := _resolve(target, pairs)) is None:
        _LOGGER.warning(
            "Emergency target %s of input 0x%02X/%s no longer matches its output pair; skipped",
            target.key,
            address,
            bit,
        )
        return None
    action, bit_a, bit_b, time_ds = resolved
    flags = LINK_FLAG_ACTIVE_HIGH if active_high else 0
    if config.role == INPUT_ROLE_CONTACT:
        flags |= LINK_FLAG_BOTH_EDGES
    return BridgeLink(kind, address, bit, action, target.address, bit_a, bit_b, time_ds, flags)


def build_emergency_links(options: Mapping[str, Any]) -> tuple[list[BridgeLink], int]:
    """Return the links for the bridge and the debounce time it applies to them."""

    pairs = get_om117_pair_configuration(options)
    links: list[BridgeLink] = []
    debounce: list[int] = []

    im117_settings = get_input_module_settings(options, "im117")
    for address, ports in sorted(get_im117_port_configuration(options).items()):
        for port, config in sorted(ports.items()):
            # IM117 inputs are active low; inverting makes the high level the press.
            if link := _link(LINK_SOURCE_PCF, address, PCF8574_MAPPED_PORTS[port], config, config.invert, pairs):
                links.append(link)
                debounce.append(im117_settings.get(address, InputModuleSettings()).debounce_ms)

    dm117_settings = get_input_module_settings(options, "dm117")
    for address, channels in sorted(get_dm117_input_configuration(options).items()):
        for (slot, channel), config in sorted(channels.items()):
            if link := _link(LINK_SOURCE_DM117, address, slot * 2 + channel, config, not config.invert, pairs):
                links.append(link)
                debounce.append(dm117_settings.get(address, InputModuleSettings()).debounce_ms)

    if len(links) > MAX_LINKS:
        _LOGGER.warning("The bridge holds %s emergency links; %s are left out", MAX_LINKS, len(links) - MAX_LINKS)
        links = links[:MAX_LINKS]
    # One value for every input, as with the watch: the smallest one configured.
    return links, min(debounce, default=0)
