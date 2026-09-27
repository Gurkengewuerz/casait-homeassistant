"""Options flow for the casaIT : Smart Home integration.

The flow is deliberately flat: the menu offers one device picker listing every
module and 1-Wire chip found on the bus, and each device is configured in a
single form. Repeated groups - the eight ports of an IM117, the four pairs of an
OM117 - are collapsible sections, so a form stays short however much hardware
a module carries.

Fields that only apply to one mode are only shown while that mode is selected.
When a submit changes a mode, the form comes back once with the fields that
now apply instead of branching into a second step.

Edits are staged and written together by the save step, so the integration
reloads once however many devices were changed.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any

import voluptuous as vol

from homeassistant.config_entries import ConfigFlowResult, OptionsFlowWithReload
from homeassistant.data_entry_flow import section
from homeassistant.helpers.selector import (
    BooleanSelector,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    TextSelector,
)

from .const import (
    COVER_REFERENCE_MODES,
    DEFAULT_LED_COUNT,
    DEFAULT_OW_POLL_INTERVAL,
    DS28E17_FAMILY,
    DS2413_CHANNEL_INPUT,
    DS2413_CHANNEL_OUTPUT,
    INPUT_DEVICE_CLASSES,
    INPUT_ROLE_BUTTON,
    INPUT_ROLE_CONTACT,
    INPUT_ROLE_UNUSED,
    OM117_MODE_BLIND,
    OM117_MODE_PULSE,
    OM117_MODE_SHUTTER,
    OM117_MODE_SWITCH,
    OPT_DEBOUNCE_MS,
    OPT_DOUBLE_CLICK_MS,
    OPT_FAST_POLL_INTERVAL_MS,
    OPT_LONG_PRESS_MS,
    OPT_MAX_SEND_INTERVAL_MS,
    OPT_REPEAT_INTERVAL_MS,
    OPT_SLOW_POLL_INTERVAL,
    OPT_TOPOLOGY_MISSING_SCANS,
    OPT_TOPOLOGY_SCAN_INTERVAL,
    OW_PROFILE_LED,
    OW_PROFILE_MULTISENSOR,
)
from .helpers import (
    ONEWIRE_BOARD_MODELS,
    DigitalInputConfig,
    InputModuleSettings,
    InputSettings,
    OM117PairConfig,
    PollingSettings,
    TopologySettings,
    default_onewire_profile,
    get_address_range,
    get_configured_ds2413_channels,
    get_configured_led_counts,
    get_configured_onewire_poll_intervals,
    get_configured_onewire_profiles,
    get_dm117_input_configuration,
    get_dm117_slot_types,
    get_ds2413_input_configuration,
    get_im117_port_configuration,
    get_input_module_settings,
    get_input_settings,
    get_module_name,
    get_om117_pair_configuration,
    get_onewire_names,
    get_polling_settings,
    get_topology_settings,
    set_dm117_inputs,
    set_dm117_slots,
    set_ds2413_inputs,
    set_im117_ports,
    set_input_settings,
    set_module_name,
    set_om117_pairs,
    set_onewire_device,
    set_polling_settings,
    set_topology_settings,
)

if TYPE_CHECKING:
    from .api import CasaITApi

# Selector option lists must be lists: SelectSelectorConfig validates its options
# against vol.Schema([...]), which rejects tuples with "expected a list".
OM117_MODES = [OM117_MODE_SWITCH, OM117_MODE_SHUTTER, OM117_MODE_BLIND, OM117_MODE_PULSE]
DM117_SLOT_TYPES = ["none", "binary_input", "switch", "dimmer"]
INPUT_ROLES = [INPUT_ROLE_CONTACT, INPUT_ROLE_BUTTON, INPUT_ROLE_UNUSED]
DS2413_DIRECTIONS = [DS2413_CHANNEL_INPUT, DS2413_CHANNEL_OUTPUT]

# voluptuous cannot express "no selection", so the absence of a device class is
# carried as an explicit sentinel that is mapped back to None on save.
NO_DEVICE_CLASS = "none"
CONTACT_DEVICE_CLASS_OPTIONS = [NO_DEVICE_CLASS, *INPUT_DEVICE_CLASSES]

# Profiles a user can choose between, by family code. A family with a single
# profile gets no choice in the form at all.
ONEWIRE_PROFILE_CHOICES: dict[int, list[str]] = {
    0x28: ["ds18b20_temp"],
    0x26: ["ds2438_hih5030_tept5600", "ds2438_hih4030_tept5600"],
    0x3A: ["ds2413"],
    DS28E17_FAMILY: [OW_PROFILE_LED, OW_PROFILE_MULTISENSOR],
}
# Every profile the options may hold, for the translated selector.
ONEWIRE_PROFILES = [profile for profiles in ONEWIRE_PROFILE_CHOICES.values() for profile in profiles]

MODULE_KINDS = ("im117", "om117", "dm117", "sm117")
ONEWIRE_KEY = "onewire"

NAME_FIELD = "name"
DEVICE_FIELD = "device"


def _box(minimum: float, maximum: float, step: float = 1) -> NumberSelector:
    return NumberSelector(NumberSelectorConfig(min=minimum, max=maximum, step=step, mode=NumberSelectorMode.BOX))


def _select(options: list[str], translation_key: str) -> SelectSelector:
    return SelectSelector(SelectSelectorConfig(options=options, translation_key=translation_key))


def _collapsed(fields: dict[Any, Any]) -> section:
    return section(vol.Schema(fields), {"collapsed": True})


def _input_fields(config: DigitalInputConfig, prefix: str = "", *, with_role: bool = True) -> dict[Any, Any]:
    """Return the fields describing one digital input, whatever module it sits on."""

    fields: dict[Any, Any] = {}
    if with_role:
        fields[vol.Required(f"{prefix}role", default=config.role)] = _select(INPUT_ROLES, "input_role")
    fields[vol.Required(f"{prefix}device_class", default=config.device_class or NO_DEVICE_CLASS)] = _select(
        CONTACT_DEVICE_CLASS_OPTIONS, "contact_device_class"
    )
    fields[vol.Required(f"{prefix}invert", default=config.invert)] = BooleanSelector()
    if with_role:
        fields[vol.Required(f"{prefix}repeat", default=config.repeat)] = BooleanSelector()
    return fields


def _input_from_form(
    data: Mapping[str, Any],
    current: DigitalInputConfig,
    prefix: str = "",
    *,
    role: str | None = None,
) -> DigitalInputConfig:
    """Read back one digital input, keeping anything the form did not send."""

    role = str(data.get(f"{prefix}role", current.role)) if role is None else role
    device_class = data.get(f"{prefix}device_class", current.device_class)
    if role != INPUT_ROLE_CONTACT or device_class == NO_DEVICE_CLASS:
        device_class = None
    return DigitalInputConfig(
        role=role,
        device_class=str(device_class) if device_class else None,
        invert=bool(data.get(f"{prefix}invert", current.invert)),
        repeat=bool(data.get(f"{prefix}repeat", current.repeat)),
    )


def _section_data(user_input: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = user_input.get(key)
    return value if isinstance(value, Mapping) else {}


class OptionsFlowHandler(OptionsFlowWithReload):
    """Handle the options of one casaIT bridge."""

    def __init__(self) -> None:
        """Initialize options flow."""

        self._staged_options: dict[str, Any] | None = None
        self._edited: set[str] = set()
        self._device_key: str | None = None

    # ------------------------------------------------------------------
    # Plumbing
    # ------------------------------------------------------------------

    @property
    def _options(self) -> Mapping[str, Any]:
        """Return the options being edited, including changes not yet written."""

        return self.config_entry.options if self._staged_options is None else self._staged_options

    @property
    def _api(self) -> CasaITApi | None:
        """Return the running API, or None while the entry is not loaded.

        The device list comes from the latest bus scan, which only a loaded
        entry has.
        """

        return getattr(self.config_entry, "runtime_data", None)

    async def _stage(self, options: dict[str, Any], key: str) -> ConfigFlowResult:
        """Keep one edit in memory and return to the menu.

        Writing the entry here would end the flow and reload the integration
        after every device, so changes are collected and written by the save
        step.
        """

        self._staged_options = options
        self._edited.add(key)
        return await self.async_step_init()

    # ------------------------------------------------------------------
    # Menu
    # ------------------------------------------------------------------

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Show the top-level menu."""

        return self.async_show_menu(
            step_id="init",
            menu_options=["device", "input_settings", "advanced_settings", "save"],
            description_placeholders={"pending": str(len(self._edited))},
        )

    async def async_step_save(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Write every staged change in one go and close the flow."""

        return self.async_create_entry(title="", data=dict(self._options))

    # ------------------------------------------------------------------
    # Device picker
    # ------------------------------------------------------------------

    def _device_choices(self, api: CasaITApi) -> list[SelectOptionDict]:
        """List every device found on the bus, I2C modules first, then 1-Wire."""

        choices: list[SelectOptionDict] = []
        for kind in MODULE_KINDS:
            for address in sorted(self._module_addresses(api, kind)):
                key = f"{kind}:{address}"
                choices.append({"value": key, "label": self._device_label(api, key)})

        for device_id in sorted(api.ow_devices, key=lambda rom: (api.ow_devices[rom].get("bus_address", 0), rom)):
            key = f"{ONEWIRE_KEY}:{device_id}"
            choices.append({"value": key, "label": self._device_label(api, key)})
        return choices

    @staticmethod
    def _module_addresses(api: CasaITApi, kind: str) -> list[int]:
        if kind == "dm117":
            return list(api.dm117)
        if kind == "sm117":
            return list(api.sm117)
        address_range = get_address_range(kind.upper())
        if address_range is None:
            return []
        return [address for address in api.im117_om117 if address_range[0] <= address <= address_range[1]]

    def _device_label(self, api: CasaITApi, key: str) -> str:
        """Return a language-neutral label: what it is, where it is, what it is called."""

        kind, _, ident = key.partition(":")
        if kind == ONEWIRE_KEY:
            meta = api.ow_devices.get(ident, {})
            profile = self._onewire_profile(api, ident)
            model = ONEWIRE_BOARD_MODELS.get(profile or "") or str(meta.get("device_type") or "1-Wire")
            base = f"1-Wire {model} {ident}"
            name = get_onewire_names(self._options).get(ident)
        else:
            address = int(ident)
            base = f"{kind.upper()} 0x{address:02X}"
            name = get_module_name(self._options, kind, address, "")
        label = f"{base} · {name}" if name else base
        # Marks devices edited in this session but not saved yet.
        return f"{label} *" if key in self._edited else label

    async def async_step_device(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Pick the device to configure."""

        if (api := self._api) is None:
            return self.async_abort(reason="integration_not_ready")
        if user_input is None:
            # Picks up 1-Wire chips plugged in since the last scan.
            await api.scan_onewire()

        choices = self._device_choices(api)
        if not choices:
            return self.async_abort(reason="no_devices_found")

        if user_input is not None:
            self._device_key = str(user_input[DEVICE_FIELD])
            kind = self._device_key.partition(":")[0]
            step: Callable[[], Any] = {
                "im117": self.async_step_im117,
                "om117": self.async_step_om117,
                "dm117": self.async_step_dm117,
                "sm117": self.async_step_sm117,
                ONEWIRE_KEY: self.async_step_onewire,
            }[kind]
            return await step()

        return self.async_show_form(
            step_id="device",
            data_schema=vol.Schema({vol.Required(DEVICE_FIELD): SelectSelector(SelectSelectorConfig(options=choices))}),
        )

    def _selected(self, kind: str) -> str | None:
        """Return the identifier part of the picked device when it is of this kind."""

        if self._device_key is None:
            return None
        picked_kind, _, ident = self._device_key.partition(":")
        return ident if picked_kind == kind else None

    async def _refresh_or_stage(
        self,
        options: dict[str, Any],
        *,
        layout_changed: bool,
        show: Callable[[dict[str, str]], ConfigFlowResult],
    ) -> ConfigFlowResult:
        """Stage an edit, or show the form again when a mode change brought new fields."""

        key = self._device_key or ""
        if layout_changed:
            self._staged_options = options
            self._edited.add(key)
            return show({"base": "fields_updated"})
        return await self._stage(options, key)

    # ------------------------------------------------------------------
    # IM117
    # ------------------------------------------------------------------

    async def async_step_im117(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Configure one input module: name, debounce, and what each port is wired to."""

        if (ident := self._selected("im117")) is None:
            return self.async_abort(reason="integration_not_ready")
        address = int(ident)
        configured = get_im117_port_configuration(self._options).get(address, {})
        settings = get_input_module_settings(self._options, "im117").get(address, InputModuleSettings())
        name = get_module_name(self._options, "im117", address, "")

        if user_input is not None:
            ports = {
                port: _input_from_form(
                    _section_data(user_input, f"port_{port + 1}"), configured.get(port, DigitalInputConfig())
                )
                for port in range(8)
            }
            options = set_im117_ports(
                self._options,
                address,
                ports,
                name=str(user_input.get(NAME_FIELD, "")),
                debounce_ms=int(user_input[OPT_DEBOUNCE_MS]),
            )
            return await self._stage(options, f"im117:{address}")

        schema: dict[Any, Any] = {
            vol.Optional(NAME_FIELD, description={"suggested_value": name}): TextSelector(),
            vol.Required(OPT_DEBOUNCE_MS, default=settings.debounce_ms): _box(0, 255),
        }
        for port in range(8):
            schema[vol.Required(f"port_{port + 1}")] = _collapsed(
                _input_fields(configured.get(port, DigitalInputConfig()))
            )
        return self.async_show_form(
            step_id="im117",
            data_schema=vol.Schema(schema),
            description_placeholders={"device": f"IM117 0x{address:02X}"},
        )

    # ------------------------------------------------------------------
    # OM117
    # ------------------------------------------------------------------

    async def async_step_om117(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Configure one output module: name, and what each output pair drives."""

        if (ident := self._selected("om117")) is None:
            return self.async_abort(reason="integration_not_ready")
        address = int(ident)
        existing = get_om117_pair_configuration(self._options).get(address, {})

        if user_input is not None:
            pairs: dict[int, OM117PairConfig] = {}
            for pair in range(4):
                data = _section_data(user_input, f"pair_{pair + 1}")
                current = existing.get(pair, OM117PairConfig())
                pairs[pair] = OM117PairConfig(
                    mode=str(data.get("mode", current.mode)),
                    open_time=float(data.get("open_time", current.open_time)),
                    close_time=float(data.get("close_time", current.close_time)),
                    overrun_time=float(data.get("overrun_time", current.overrun_time)),
                    tilt_time=float(data.get("tilt_time", current.tilt_time)),
                    pulse_duration=float(data.get("pulse_duration", current.pulse_duration)),
                    reference_mode=str(data.get("reference_mode", current.reference_mode)),
                )
            options = set_om117_pairs(self._options, address, pairs, name=str(user_input.get(NAME_FIELD, "")))
            changed = any(pairs[pair].mode != existing.get(pair, OM117PairConfig()).mode for pair in range(4))
            needs_timing = any(config.mode != OM117_MODE_SWITCH for config in pairs.values())
            return await self._refresh_or_stage(
                options,
                layout_changed=changed and needs_timing,
                show=lambda errors: self._show_om117(address, errors),
            )

        return self._show_om117(address)

    def _show_om117(self, address: int, errors: dict[str, str] | None = None) -> ConfigFlowResult:
        existing = get_om117_pair_configuration(self._options).get(address, {})
        name = get_module_name(self._options, "om117", address, "")
        schema: dict[Any, Any] = {vol.Optional(NAME_FIELD, description={"suggested_value": name}): TextSelector()}
        for pair in range(4):
            config = existing.get(pair, OM117PairConfig())
            fields: dict[Any, Any] = {vol.Required("mode", default=config.mode): _select(OM117_MODES, "om117_mode")}
            if config.mode == OM117_MODE_PULSE:
                fields[vol.Required("pulse_duration", default=config.pulse_duration)] = _box(0.1, 30, 0.1)
            elif config.mode in {OM117_MODE_SHUTTER, OM117_MODE_BLIND}:
                fields[vol.Required("open_time", default=config.open_time)] = _box(1, 180, 0.1)
                fields[vol.Required("close_time", default=config.close_time)] = _box(1, 180, 0.1)
                fields[vol.Required("overrun_time", default=config.overrun_time)] = _box(0, 15, 0.1)
                fields[vol.Required("reference_mode", default=config.reference_mode)] = _select(
                    list(COVER_REFERENCE_MODES), "cover_reference_mode"
                )
                if config.mode == OM117_MODE_BLIND:
                    fields[vol.Required("tilt_time", default=config.tilt_time)] = _box(0.1, 15, 0.1)
            schema[vol.Required(f"pair_{pair + 1}")] = _collapsed(fields)
        return self.async_show_form(
            step_id="om117",
            data_schema=vol.Schema(schema),
            errors=errors,
            description_placeholders={"device": f"OM117 0x{address:02X}"},
        )

    # ------------------------------------------------------------------
    # DM117
    # ------------------------------------------------------------------

    async def async_step_dm117(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Configure one DM117: name, debounce, slot types and the input channels."""

        if (ident := self._selected("dm117")) is None:
            return self.async_abort(reason="integration_not_ready")
        address = int(ident)
        slot_types = get_dm117_slot_types(self._options).get(address, {})
        inputs = get_dm117_input_configuration(self._options).get(address, {})

        if user_input is not None:
            new_types: dict[int, str] = {}
            new_inputs: dict[tuple[int, int], DigitalInputConfig] = {}
            for slot in range(8):
                data = _section_data(user_input, f"slot_{slot + 1}")
                new_types[slot] = str(data.get("type", slot_types.get(slot, "none")))
                if new_types[slot] != "binary_input":
                    continue
                for channel, prefix in ((0, "a_"), (1, "b_")):
                    current = inputs.get((slot, channel), DigitalInputConfig())
                    new_inputs[slot, channel] = _input_from_form(data, current, prefix)

            options = set_dm117_slots(self._options, address, new_types, name=str(user_input.get(NAME_FIELD, "")))
            options = set_dm117_inputs(options, address, new_inputs, debounce_ms=int(user_input[OPT_DEBOUNCE_MS]))
            became_input = any(
                kind == "binary_input" and slot_types.get(slot) != "binary_input" for slot, kind in new_types.items()
            )
            return await self._refresh_or_stage(
                options,
                layout_changed=became_input,
                show=lambda errors: self._show_dm117(address, errors),
            )

        return self._show_dm117(address)

    def _show_dm117(self, address: int, errors: dict[str, str] | None = None) -> ConfigFlowResult:
        slot_types = get_dm117_slot_types(self._options).get(address, {})
        inputs = get_dm117_input_configuration(self._options).get(address, {})
        settings = get_input_module_settings(self._options, "dm117").get(address, InputModuleSettings())
        name = get_module_name(self._options, "dm117", address, "")
        schema: dict[Any, Any] = {
            vol.Optional(NAME_FIELD, description={"suggested_value": name}): TextSelector(),
            vol.Required(OPT_DEBOUNCE_MS, default=settings.debounce_ms): _box(0, 255),
        }
        for slot in range(8):
            slot_type = slot_types.get(slot, "none")
            fields: dict[Any, Any] = {
                vol.Required("type", default=slot_type): _select(DM117_SLOT_TYPES, "dm117_slot_type")
            }
            if slot_type == "binary_input":
                for channel, prefix in ((0, "a_"), (1, "b_")):
                    fields.update(_input_fields(inputs.get((slot, channel), DigitalInputConfig()), prefix))
            schema[vol.Required(f"slot_{slot + 1}")] = _collapsed(fields)
        return self.async_show_form(
            step_id="dm117",
            data_schema=vol.Schema(schema),
            errors=errors,
            description_placeholders={"device": f"DM117 0x{address:02X}"},
        )

    # ------------------------------------------------------------------
    # SM117
    # ------------------------------------------------------------------

    async def async_step_sm117(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Name one SM117 1-Wire bus."""

        if (ident := self._selected("sm117")) is None:
            return self.async_abort(reason="integration_not_ready")
        address = int(ident)

        if user_input is not None:
            options = set_module_name(self._options, "sm117", address, str(user_input.get(NAME_FIELD, "")))
            return await self._stage(options, f"sm117:{address}")

        name = get_module_name(self._options, "sm117", address, "")
        return self.async_show_form(
            step_id="sm117",
            data_schema=vol.Schema({vol.Optional(NAME_FIELD, description={"suggested_value": name}): TextSelector()}),
            description_placeholders={"device": f"SM117 0x{address:02X}"},
        )

    # ------------------------------------------------------------------
    # 1-Wire
    # ------------------------------------------------------------------

    def _onewire_profile(self, api: CasaITApi, device_id: str) -> str | None:
        configured = get_configured_onewire_profiles(self._options).get(device_id)
        if configured in {"ds2413_in", "ds2413_out"}:
            return "ds2413"
        return configured or default_onewire_profile(api.ow_devices.get(device_id, {}))

    async def async_step_onewire(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Configure one 1-Wire chip: name, profile, and what the profile needs."""

        if (device_id := self._selected(ONEWIRE_KEY)) is None or (api := self._api) is None:
            return self.async_abort(reason="integration_not_ready")
        profile = self._onewire_profile(api, device_id) or ""

        if user_input is None:
            return self._show_onewire(api, device_id, profile)

        new_profile = str(user_input.get("profile", profile))
        channels: dict[int, str] | None = None
        inputs: dict[int, DigitalInputConfig] = {}
        if new_profile == "ds2413":
            stored_channels = get_configured_ds2413_channels(self._options).get(
                device_id, {0: DS2413_CHANNEL_INPUT, 1: DS2413_CHANNEL_INPUT}
            )
            stored_inputs = get_ds2413_input_configuration(self._options).get(device_id, {})
            channels = {}
            for channel in range(2):
                data = _section_data(user_input, f"channel_{channel + 1}")
                channels[channel] = str(data.get("direction", stored_channels.get(channel, DS2413_CHANNEL_INPUT)))
                inputs[channel] = _input_from_form(
                    data, stored_inputs.get(channel, DigitalInputConfig()), role=INPUT_ROLE_CONTACT
                )

        led_count = user_input.get("led_count") if new_profile == OW_PROFILE_LED else None
        poll_interval = user_input.get("poll_interval")
        options = set_onewire_device(
            self._options,
            device_id,
            new_profile,
            name=str(user_input.get(NAME_FIELD, "")),
            led_count=int(led_count) if led_count is not None else None,
            poll_interval=int(poll_interval) if poll_interval is not None else None,
            ds2413_channels=channels,
        )
        if channels is not None:
            options = set_ds2413_inputs(options, device_id, inputs)

        return await self._refresh_or_stage(
            options,
            layout_changed=new_profile != profile,
            show=lambda errors: self._show_onewire(api, device_id, new_profile, errors),
        )

    def _show_onewire(
        self,
        api: CasaITApi,
        device_id: str,
        profile: str,
        errors: dict[str, str] | None = None,
    ) -> ConfigFlowResult:
        meta = api.ow_devices.get(device_id, {})
        family = meta.get("family_code")
        name = get_onewire_names(self._options).get(device_id, "")

        schema: dict[Any, Any] = {vol.Optional(NAME_FIELD, description={"suggested_value": name}): TextSelector()}
        choices = ONEWIRE_PROFILE_CHOICES.get(family, []) if isinstance(family, int) else []
        if len(choices) > 1:
            schema[vol.Required("profile", default=profile if profile in choices else choices[0])] = _select(
                choices, "onewire_profile"
            )

        if profile != OW_PROFILE_MULTISENSOR:
            # A Multisensor samples at a fixed rate its VOC algorithm is built for.
            interval = get_configured_onewire_poll_intervals(self._options).get(
                device_id, DEFAULT_OW_POLL_INTERVAL.get(profile, 60)
            )
            schema[vol.Required("poll_interval", default=interval)] = _box(1, 3600)

        if profile == OW_PROFILE_LED:
            count = get_configured_led_counts(self._options).get(device_id, DEFAULT_LED_COUNT)
            schema[vol.Required("led_count", default=count)] = _box(1, 255)
        elif profile == "ds2413":
            directions = get_configured_ds2413_channels(self._options).get(
                device_id, {0: DS2413_CHANNEL_INPUT, 1: DS2413_CHANNEL_INPUT}
            )
            inputs = get_ds2413_input_configuration(self._options).get(device_id, {})
            for channel in range(2):
                direction = directions.get(channel, DS2413_CHANNEL_INPUT)
                fields: dict[Any, Any] = {
                    vol.Required("direction", default=direction): _select(DS2413_DIRECTIONS, "ds2413_channel_profile")
                }
                # Contact settings are shown for both directions: switching a
                # channel to input in this same form should not need a second
                # round just to set its device class.
                fields.update(_input_fields(inputs.get(channel, DigitalInputConfig()), with_role=False))
                schema[vol.Required(f"channel_{channel + 1}")] = _collapsed(fields)

        details = str(meta.get("device_type") or "1-Wire")
        if components := meta.get("components"):
            details = f"{details}: {', '.join(components)}"
        return self.async_show_form(
            step_id="onewire",
            data_schema=vol.Schema(schema),
            errors=errors,
            description_placeholders={"device": device_id, "details": details},
        )

    # ------------------------------------------------------------------
    # Global settings
    # ------------------------------------------------------------------

    async def async_step_input_settings(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Configure the button gesture timing shared by every input."""

        if user_input is not None:
            settings = InputSettings(
                long_press_ms=int(user_input[OPT_LONG_PRESS_MS]),
                double_click_ms=int(user_input[OPT_DOUBLE_CLICK_MS]),
                repeat_interval_ms=int(user_input[OPT_REPEAT_INTERVAL_MS]),
            )
            return await self._stage(set_input_settings(self._options, settings), "input_settings")

        current = get_input_settings(self._options)
        return self.async_show_form(
            step_id="input_settings",
            data_schema=vol.Schema(
                {
                    vol.Required(OPT_LONG_PRESS_MS, default=current.long_press_ms): _box(100, 5000, 10),
                    vol.Required(OPT_DOUBLE_CLICK_MS, default=current.double_click_ms): _box(0, 2000, 10),
                    vol.Required(OPT_REPEAT_INTERVAL_MS, default=current.repeat_interval_ms): _box(50, 5000, 10),
                }
            ),
        )

    async def async_step_advanced_settings(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Configure polling cadence, transport limits and the topology watch."""

        if user_input is not None:
            polling = PollingSettings(
                fast_poll_interval=float(user_input[OPT_FAST_POLL_INTERVAL_MS]) / 1000,
                slow_poll_interval=float(user_input[OPT_SLOW_POLL_INTERVAL]),
                max_send_interval=float(user_input[OPT_MAX_SEND_INTERVAL_MS]) / 1000,
            )
            topology = TopologySettings(
                scan_interval=int(user_input[OPT_TOPOLOGY_SCAN_INTERVAL]),
                missing_scans=int(user_input[OPT_TOPOLOGY_MISSING_SCANS]),
            )
            options = set_polling_settings(self._options, polling)
            return await self._stage(set_topology_settings(options, topology), "advanced_settings")

        polling = get_polling_settings(self._options)
        topology = get_topology_settings(self._options)
        return self.async_show_form(
            step_id="advanced_settings",
            data_schema=vol.Schema(
                {
                    vol.Required(OPT_FAST_POLL_INTERVAL_MS, default=round(polling.fast_poll_interval * 1000, 3)): _box(
                        5, 1000
                    ),
                    vol.Required(OPT_SLOW_POLL_INTERVAL, default=polling.slow_poll_interval): _box(1, 3600),
                    vol.Required(OPT_MAX_SEND_INTERVAL_MS, default=round(polling.max_send_interval * 1000, 3)): _box(
                        1, 20
                    ),
                    vol.Required(OPT_TOPOLOGY_SCAN_INTERVAL, default=topology.scan_interval): _box(0, 86400, 60),
                    vol.Required(OPT_TOPOLOGY_MISSING_SCANS, default=topology.missing_scans): _box(1, 20),
                }
            ),
        )
