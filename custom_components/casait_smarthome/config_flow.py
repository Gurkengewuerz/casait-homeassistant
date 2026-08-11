"""Config flow for the casaIT : Smart Home integration."""

from __future__ import annotations

from collections.abc import Mapping
import contextlib
import logging
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntry, ConfigFlowResult, OptionsFlow, OptionsFlowWithReload
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.selector import (
    BooleanSelector,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    TextSelector,
    TextSelectorConfig,
)
from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo

from .const import (
    CONF_TIMEOUT,
    CONFIG_ENTRY_VERSION,
    DEFAULT_LED_COUNT,
    DEFAULT_OW_POLL_INTERVAL,
    DEFAULT_OW_PROFILE,
    DOMAIN,
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
    OPT_SLOW_POLL_INTERVAL,
)
from .helpers import (
    DigitalInputConfig,
    InputModuleSettings,
    InputSettings,
    OM117PairConfig,
    PollingSettings,
    get_address_range,
    get_configured_ds2413_channels,
    get_configured_led_counts,
    get_configured_onewire_poll_intervals,
    get_configured_onewire_profiles,
    get_dm117_slot_types,
    get_im117_port_configuration,
    get_input_module_settings,
    get_input_settings,
    get_module_name,
    get_om117_pair_configuration,
    get_polling_settings,
    set_dm117_slots,
    set_im117_ports,
    set_input_settings,
    set_module_name,
    set_om117_pairs,
    set_onewire_device,
    set_polling_settings,
)
from .services.smbus_proxy import DEFAULT_PORT, DEFAULT_TIMEOUT, SMBus, SMBusProxyError

_LOGGER = logging.getLogger(__name__)

# Selector option lists must be lists: SelectSelectorConfig validates its options
# against vol.Schema([...]), which rejects tuples with "expected a list".
OM117_SLOT_TYPES = [OM117_MODE_SWITCH, OM117_MODE_SHUTTER, OM117_MODE_BLIND, OM117_MODE_PULSE]
DM117_SLOT_TYPES = ["none", "binary_input", "switch", "dimmer"]
INPUT_ROLES = [INPUT_ROLE_CONTACT, INPUT_ROLE_BUTTON, INPUT_ROLE_UNUSED]

# voluptuous cannot express "no selection", so the absence of a device class is
# carried as an explicit sentinel that is mapped back to None on save.
NO_DEVICE_CLASS = "none"
CONTACT_DEVICE_CLASS_OPTIONS = [NO_DEVICE_CLASS, *INPUT_DEVICE_CLASSES]

ONEWIRE_PROFILES = [
    "ds18b20_temp",
    "ds2438_hih4030_tept5600",
    "ds2438_hih5030_tept5600",
    "ds2413",
    "ds28e17_led",
]


def _input_config_schema(
    prefix: str,
    config: DigitalInputConfig,
    *,
    roles: list[str] = INPUT_ROLES,
) -> dict[Any, Any]:
    """Return the schema describing one digital input, whatever module it sits on.

    ``prefix`` names the input within its form, for example "port_3" or
    "slot_2_channel_a".
    """

    return {
        vol.Required(f"{prefix}_role", default=config.role): SelectSelector(
            SelectSelectorConfig(options=roles, translation_key="input_role")
        ),
        vol.Optional(f"{prefix}_device_class", default=config.device_class or NO_DEVICE_CLASS): SelectSelector(
            SelectSelectorConfig(options=CONTACT_DEVICE_CLASS_OPTIONS, translation_key="contact_device_class")
        ),
        vol.Required(f"{prefix}_invert", default=config.invert): BooleanSelector(),
    }


def _input_config_from_form(user_input: Mapping[str, Any], prefix: str) -> DigitalInputConfig:
    """Read back one digital input from a submitted form."""

    role = str(user_input[f"{prefix}_role"])
    device_class = user_input.get(f"{prefix}_device_class")
    if role != INPUT_ROLE_CONTACT or device_class == NO_DEVICE_CLASS:
        device_class = None
    return DigitalInputConfig(
        role=role,
        device_class=str(device_class) if device_class else None,
        invert=bool(user_input.get(f"{prefix}_invert", False)),
    )


def _bridge_data_schema(defaults: Mapping[str, Any] | None = None) -> vol.Schema:
    """Return the bridge connection schema with optional current values."""

    current = defaults or {}
    host_key = vol.Required(CONF_HOST, default=current[CONF_HOST]) if CONF_HOST in current else vol.Required(CONF_HOST)
    return vol.Schema(
        {
            host_key: vol.All(TextSelector(TextSelectorConfig()), vol.Length(min=1)),
            vol.Required(CONF_PORT, default=current.get(CONF_PORT, DEFAULT_PORT)): NumberSelector(
                NumberSelectorConfig(min=1, max=65535, step=1, mode=NumberSelectorMode.BOX)
            ),
            vol.Required(CONF_TIMEOUT, default=current.get(CONF_TIMEOUT, DEFAULT_TIMEOUT)): NumberSelector(
                NumberSelectorConfig(min=0.1, max=60, step=0.1, mode=NumberSelectorMode.BOX)
            ),
        }
    )


def _normalize_bridge_data(data: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize selector values before validation and storage."""

    return {
        CONF_HOST: str(data[CONF_HOST]).strip(),
        CONF_PORT: int(data[CONF_PORT]),
        CONF_TIMEOUT: float(data[CONF_TIMEOUT]),
    }


async def validate_input(hass: HomeAssistant, data: dict[str, Any]) -> dict[str, Any]:
    """Validate the user input allows us to connect.

    Data contains the normalized bridge connection values.
    """
    bus: SMBus | None = None
    try:
        connected_bus = await hass.async_add_executor_job(
            SMBus,
            1,
            data[CONF_HOST],
            data[CONF_PORT],
            data[CONF_TIMEOUT],
        )
        bus = connected_bus
        if not await hass.async_add_executor_job(connected_bus.ping):
            raise CannotConnect
    except (SMBusProxyError, OSError) as exc:
        raise CannotConnect from exc
    finally:
        if bus is not None:
            with contextlib.suppress(SMBusProxyError, OSError):
                await hass.async_add_executor_job(bus.close)

    return {"title": data[CONF_HOST]}


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for casaIT : Smart Home."""

    VERSION = CONFIG_ENTRY_VERSION

    def __init__(self) -> None:
        """Initialize the config flow."""

        super().__init__()
        self._discovered_host: str | None = None
        self._discovered_port: int = DEFAULT_PORT
        self._discovered_timeout: float = DEFAULT_TIMEOUT
        self._discovered_name: str | None = None

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Create the options flow."""
        return OptionsFlowHandler()

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Handle the initial step."""
        errors: dict[str, str] = {}
        if user_input is not None:
            data = _normalize_bridge_data(user_input)
            self._async_abort_entries_match(
                {
                    CONF_HOST: data[CONF_HOST],
                    CONF_PORT: data[CONF_PORT],
                }
            )
            try:
                info = await validate_input(self.hass, data)
            except CannotConnect:
                errors["base"] = "cannot_connect"
            except Exception:
                _LOGGER.exception("Unexpected exception")
                errors["base"] = "unknown"
            else:
                return self.async_create_entry(title=info["title"], data=data)

        return self.async_show_form(step_id="user", data_schema=_bridge_data_schema(), errors=errors)

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Allow the bridge connection settings to be changed in place."""

        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            data = _normalize_bridge_data(user_input)
            self._async_abort_entries_match({CONF_HOST: data[CONF_HOST], CONF_PORT: data[CONF_PORT]})
            try:
                info = await validate_input(self.hass, data)
            except CannotConnect:
                errors["base"] = "cannot_connect"
            except Exception:
                _LOGGER.exception("Unexpected exception")
                errors["base"] = "unknown"
            else:
                if entry.unique_id is not None:
                    await self.async_set_unique_id(entry.unique_id)
                    self._abort_if_unique_id_mismatch()
                return self.async_update_reload_and_abort(entry, title=info["title"], data=data)

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=_bridge_data_schema(entry.data),
            errors=errors,
        )

    @staticmethod
    def _decode_property_value(value: Any) -> str | None:
        """Decode zeroconf property values that may be bytes."""

        if isinstance(value, bytes):
            try:
                return value.decode()
            except UnicodeDecodeError:
                return None
        if isinstance(value, str):
            return value
        return None

    async def async_step_zeroconf(self, discovery_info: ZeroconfServiceInfo) -> ConfigFlowResult:
        """Handle zeroconf discovery."""

        host = discovery_info.host or None
        ip_address = getattr(discovery_info, "ip_address", None)
        ip_addresses = getattr(discovery_info, "ip_addresses", None)
        if host is None and ip_address is not None:
            host = str(ip_address)
        if host is None and ip_addresses:
            host = str(ip_addresses[0])
        if host is None:
            return self.async_abort(reason="cannot_connect")

        port = discovery_info.port or DEFAULT_PORT
        self._discovered_host = host
        self._discovered_port = port
        self._discovered_timeout = DEFAULT_TIMEOUT
        self._discovered_name = discovery_info.name.rstrip(".") if discovery_info.name else host
        self.context["title_placeholders"] = {"name": self._discovered_name}

        properties = discovery_info.properties or {}
        unique_id = None
        for key in ("id", "unique_id", "uid", "serial", "deviceid", "mac"):
            unique_id = self._decode_property_value(properties.get(key))
            if unique_id:
                break

        self._async_abort_entries_match({CONF_HOST: host, CONF_PORT: port})

        if unique_id:
            await self.async_set_unique_id(unique_id)
            self._abort_if_unique_id_configured(updates={CONF_HOST: host, CONF_PORT: port})
        else:
            await self._async_handle_discovery_without_unique_id()

        return await self.async_step_zeroconf_confirm()

    async def async_step_zeroconf_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Confirm zeroconf discovery."""

        if self._discovered_host is None:
            return self.async_abort(reason="unknown")

        errors: dict[str, str] = {}
        data_schema = _bridge_data_schema(
            {
                CONF_HOST: self._discovered_host,
                CONF_PORT: self._discovered_port,
                CONF_TIMEOUT: self._discovered_timeout,
            }
        )

        if user_input is not None:
            data = _normalize_bridge_data(user_input)
            try:
                info = await validate_input(self.hass, data)
            except CannotConnect:
                errors["base"] = "cannot_connect"
            except Exception:
                _LOGGER.exception("Unexpected exception")
                errors["base"] = "unknown"
            else:
                return self.async_create_entry(title=info["title"], data=data)

        return self.async_show_form(
            step_id="zeroconf_confirm",
            data_schema=data_schema,
            errors=errors,
            description_placeholders={"host": self._discovered_host},
        )


class OptionsFlowHandler(OptionsFlowWithReload):
    """Handle options flow for casaIT."""

    def __init__(self) -> None:
        """Initialize options flow."""
        self._selected_im117_addr: int | None = None
        self._selected_om117_addr: int | None = None
        self._selected_dm117_addr: int | None = None
        self._selected_sm117_addr: int | None = None
        self._selected_ow_id: str | None = None
        self._selected_ow_profile: str | None = None
        self._pending_om117_modes: dict[int, str] | None = None
        self._pending_om117_name: str | None = None
        self._staged_options: dict[str, Any] | None = None

    @property
    def _options(self) -> Mapping[str, Any]:
        """Return the options being edited, including changes not yet written.

        Every sub-flow reads its defaults from here so that a module edited
        earlier in the same session shows the staged values, not the ones the
        config entry still holds.
        """
        return self.config_entry.options if self._staged_options is None else self._staged_options

    async def _stage(self, options: dict[str, Any]) -> ConfigFlowResult:
        """Keep one module's edit in memory and return to the menu.

        Writing the config entry here would end the options flow and reload the
        integration after every single module, so changes are collected and
        written once in async_step_save.
        """
        self._staged_options = options
        return await self.async_step_init()

    @property
    def _runtime_data(self):
        """Helper function to access the running API instance.

        We need to know which devices were FOUND on the bus.

        The attribute only exists while the entry is loaded, so an entry that is
        still retrying its setup would raise AttributeError here. Callers expect
        None in that case and abort with "integration_not_ready".
        """
        return getattr(self.config_entry, "runtime_data", None)

    def _default_profile_for_device(self, device_id: str) -> str:
        """Return default OneWire profile based on family code if available."""

        api = self._runtime_data
        if not api:
            return list(ONEWIRE_PROFILES)[0]

        family_code = api.ow_devices.get(device_id, {}).get("family_code")
        if family_code is None:
            return list(ONEWIRE_PROFILES)[0]

        profile = DEFAULT_OW_PROFILE.get(family_code, list(ONEWIRE_PROFILES)[0])
        return "ds2413" if profile in {"ds2413_in", "ds2413_out"} else profile

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Manage the options menu."""
        return self.async_show_menu(
            step_id="init",
            menu_options=[
                "im117_select",
                "om117_select",
                "dm117_select",
                "sm117_select",
                "onewire_select",
                "global_settings",
                "save",
            ],
        )

    async def async_step_save(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Write every staged change in one go and close the flow.

        This is the only step that touches the config entry, so the integration
        reloads once no matter how many modules were configured.
        """
        return self.async_create_entry(title="", data=dict(self._options))

    def _module_selector_options(self, module_kind: str, addresses: list[int]) -> list[SelectOptionDict]:
        """Return language-neutral labels for dynamically detected modules."""

        module_code = module_kind.upper()
        return [
            {
                "value": str(address),
                "label": get_module_name(
                    self._options,
                    module_kind,
                    address,
                    f"{module_code} 0x{address:02X}",
                ),
            }
            for address in sorted(addresses)
        ]

    # ------------------------------------------------------------------
    # IM117 CONFIGURATION
    # ------------------------------------------------------------------

    async def async_step_im117_select(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Step 1: Selection of the IM117 module."""

        api = self._runtime_data
        if not api:
            return self.async_abort(reason="integration_not_ready")

        input_range = get_address_range("IM117")
        detected_modules = (
            [addr for addr in api.im117_om117 if input_range[0] <= addr <= input_range[1]] if input_range else []
        )

        if not detected_modules:
            return self.async_abort(reason="no_im117_found")

        if user_input is not None:
            self._selected_im117_addr = int(user_input["selected_module"])
            return await self.async_step_im117_config()

        return self.async_show_form(
            step_id="im117_select",
            data_schema=vol.Schema(
                {
                    vol.Required("selected_module"): SelectSelector(
                        SelectSelectorConfig(options=self._module_selector_options("im117", detected_modules))
                    )
                }
            ),
        )

    async def async_step_im117_config(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Step 2: Role assignment for the 8 input ports of the selected module."""

        if (addr := self._selected_im117_addr) is None:
            return self.async_abort(reason="integration_not_ready")

        if user_input is not None:
            ports = {index - 1: _input_config_from_form(user_input, f"port_{index}") for index in range(1, 9)}
            return await self._stage(
                set_im117_ports(
                    self._options,
                    addr,
                    ports,
                    name=str(user_input["module_name"]),
                    debounce_ms=int(user_input[OPT_DEBOUNCE_MS]),
                )
            )

        configured = get_im117_port_configuration(self._options).get(addr, {})
        module_settings = get_input_module_settings(self._options, "im117").get(addr, InputModuleSettings())
        module_name = get_module_name(self._options, "im117", addr, f"IM117 0x{addr:02X}")
        schema: dict[Any, Any] = {
            vol.Required("module_name", default=module_name): TextSelector(TextSelectorConfig()),
            vol.Required(OPT_DEBOUNCE_MS, default=module_settings.debounce_ms): NumberSelector(
                NumberSelectorConfig(min=0, max=255, step=1, mode=NumberSelectorMode.BOX)
            ),
        }
        for index in range(1, 9):
            schema.update(_input_config_schema(f"port_{index}", configured.get(index - 1, DigitalInputConfig())))

        return self.async_show_form(
            step_id="im117_config",
            data_schema=vol.Schema(schema),
            description_placeholders={"module_name": module_name},
        )

    # ------------------------------------------------------------------
    # SHARED INPUT SETTINGS
    # ------------------------------------------------------------------

    async def async_step_input_settings(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Forward older in-progress option flows to global settings."""

        return await self.async_step_global_settings(user_input)

    async def async_step_global_settings(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Configure button timing, polling cadence, and transport limits."""

        if user_input is not None:
            input_settings = InputSettings(
                long_press_ms=int(user_input[OPT_LONG_PRESS_MS]),
                double_click_ms=int(user_input[OPT_DOUBLE_CLICK_MS]),
            )
            polling_settings = PollingSettings(
                fast_poll_interval=float(user_input[OPT_FAST_POLL_INTERVAL_MS]) / 1000,
                slow_poll_interval=float(user_input[OPT_SLOW_POLL_INTERVAL]),
                max_send_interval=float(user_input[OPT_MAX_SEND_INTERVAL_MS]) / 1000,
            )
            options = set_input_settings(self._options, input_settings)
            return await self._stage(set_polling_settings(options, polling_settings))

        current_input = get_input_settings(self._options)
        current_polling = get_polling_settings(self._options)

        return self.async_show_form(
            step_id="global_settings",
            data_schema=vol.Schema(
                {
                    vol.Required(OPT_LONG_PRESS_MS, default=current_input.long_press_ms): NumberSelector(
                        NumberSelectorConfig(min=100, max=5000, step=10, mode=NumberSelectorMode.BOX)
                    ),
                    vol.Required(OPT_DOUBLE_CLICK_MS, default=current_input.double_click_ms): NumberSelector(
                        NumberSelectorConfig(min=0, max=2000, step=10, mode=NumberSelectorMode.BOX)
                    ),
                    vol.Required(
                        OPT_FAST_POLL_INTERVAL_MS,
                        default=round(current_polling.fast_poll_interval * 1000, 3),
                    ): NumberSelector(NumberSelectorConfig(min=5, max=1000, step=1, mode=NumberSelectorMode.BOX)),
                    vol.Required(
                        OPT_SLOW_POLL_INTERVAL,
                        default=current_polling.slow_poll_interval,
                    ): NumberSelector(NumberSelectorConfig(min=1, max=3600, step=1, mode=NumberSelectorMode.BOX)),
                    vol.Required(
                        OPT_MAX_SEND_INTERVAL_MS,
                        default=round(current_polling.max_send_interval * 1000, 3),
                    ): NumberSelector(NumberSelectorConfig(min=1, max=20, step=1, mode=NumberSelectorMode.BOX)),
                }
            ),
        )

    # ------------------------------------------------------------------
    # OM117 CONFIGURATION
    # ------------------------------------------------------------------

    async def async_step_om117_select(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Step 1: Selection of the OM117 module."""

        api = self._runtime_data
        if not api:
            return self.async_abort(reason="integration_not_ready")

        output_range = get_address_range("OM117")
        detected_modules = (
            [addr for addr in api.im117_om117 if output_range[0] <= addr <= output_range[1]] if output_range else []
        )

        if not detected_modules:
            return self.async_abort(reason="no_om117_found")

        if user_input is not None:
            self._selected_om117_addr = int(user_input["selected_module"])
            return await self.async_step_om117_config()

        return self.async_show_form(
            step_id="om117_select",
            data_schema=vol.Schema(
                {
                    vol.Required("selected_module"): SelectSelector(
                        SelectSelectorConfig(options=self._module_selector_options("om117", detected_modules))
                    )
                }
            ),
        )

    async def async_step_om117_config(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Step 2: Configuration of the 4 output pairs for the selected module."""

        if self._selected_om117_addr is None:
            return self.async_abort(reason="integration_not_ready")

        addr = self._selected_om117_addr
        existing = get_om117_pair_configuration(self._options).get(addr, {})

        if user_input is not None:
            self._pending_om117_name = str(user_input["module_name"])
            self._pending_om117_modes = {
                pair_index - 1: str(user_input[f"pair_{pair_index}_mode"]) for pair_index in range(1, 5)
            }
            if any(
                mode in {OM117_MODE_BLIND, OM117_MODE_SHUTTER, OM117_MODE_PULSE}
                for mode in self._pending_om117_modes.values()
            ):
                return await self.async_step_om117_timing()
            pairs = {
                pair_index: OM117PairConfig(
                    mode=mode,
                    open_time=existing.get(pair_index, OM117PairConfig()).open_time,
                    close_time=existing.get(pair_index, OM117PairConfig()).close_time,
                    overrun_time=existing.get(pair_index, OM117PairConfig()).overrun_time,
                    tilt_time=existing.get(pair_index, OM117PairConfig()).tilt_time,
                    pulse_duration=existing.get(pair_index, OM117PairConfig()).pulse_duration,
                )
                for pair_index, mode in self._pending_om117_modes.items()
            }
            return await self._stage(
                set_om117_pairs(
                    self._options,
                    addr,
                    pairs,
                    name=self._pending_om117_name,
                )
            )

        module_name = get_module_name(self._options, "om117", addr, f"OM117 0x{addr:02X}")
        schema: dict[Any, Any] = {vol.Required("module_name", default=module_name): TextSelector(TextSelectorConfig())}
        for idx in range(1, 5):
            config: OM117PairConfig = existing.get(idx - 1, OM117PairConfig())
            schema[vol.Required(f"pair_{idx}_mode", default=config.mode)] = SelectSelector(
                SelectSelectorConfig(options=OM117_SLOT_TYPES, translation_key="om117_mode")
            )

        return self.async_show_form(
            step_id="om117_config",
            data_schema=vol.Schema(schema),
            description_placeholders={"module_name": module_name},
        )

    async def async_step_om117_timing(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Configure timings only for OM117 pairs operating as blinds."""

        if (addr := self._selected_om117_addr) is None or self._pending_om117_modes is None:
            return self.async_abort(reason="integration_not_ready")

        existing = get_om117_pair_configuration(self._options).get(addr, {})
        if user_input is not None:
            pairs: dict[int, OM117PairConfig] = {}
            for pair_index, mode in self._pending_om117_modes.items():
                current = existing.get(pair_index, OM117PairConfig())
                field_index = pair_index + 1
                pairs[pair_index] = OM117PairConfig(
                    mode=mode,
                    open_time=float(user_input.get(f"pair_{field_index}_open_time", current.open_time)),
                    close_time=float(user_input.get(f"pair_{field_index}_close_time", current.close_time)),
                    overrun_time=float(user_input.get(f"pair_{field_index}_overrun_time", current.overrun_time)),
                    tilt_time=float(user_input.get(f"pair_{field_index}_tilt_time", current.tilt_time)),
                    pulse_duration=float(user_input.get(f"pair_{field_index}_pulse_duration", current.pulse_duration)),
                )
            return await self._stage(
                set_om117_pairs(
                    self._options,
                    addr,
                    pairs,
                    name=self._pending_om117_name,
                )
            )

        schema: dict[Any, Any] = {}
        for pair_index, mode in self._pending_om117_modes.items():
            if mode == OM117_MODE_SWITCH:
                continue
            current = existing.get(pair_index, OM117PairConfig())
            field_index = pair_index + 1
            if mode == OM117_MODE_PULSE:
                schema[vol.Required(f"pair_{field_index}_pulse_duration", default=current.pulse_duration)] = (
                    NumberSelector(NumberSelectorConfig(min=0.1, max=30, step=0.1, mode=NumberSelectorMode.BOX))
                )
                continue
            schema[vol.Required(f"pair_{field_index}_open_time", default=current.open_time)] = NumberSelector(
                NumberSelectorConfig(min=1, max=180, step=0.1, mode=NumberSelectorMode.BOX)
            )
            schema[vol.Required(f"pair_{field_index}_close_time", default=current.close_time)] = NumberSelector(
                NumberSelectorConfig(min=1, max=180, step=0.1, mode=NumberSelectorMode.BOX)
            )
            schema[vol.Required(f"pair_{field_index}_overrun_time", default=current.overrun_time)] = NumberSelector(
                NumberSelectorConfig(min=0, max=15, step=0.1, mode=NumberSelectorMode.BOX)
            )
            if mode == OM117_MODE_BLIND:
                schema[vol.Required(f"pair_{field_index}_tilt_time", default=current.tilt_time)] = NumberSelector(
                    NumberSelectorConfig(min=0.1, max=15, step=0.1, mode=NumberSelectorMode.BOX)
                )

        module_name = self._pending_om117_name or f"OM117 0x{addr:02X}"
        return self.async_show_form(
            step_id="om117_timing",
            data_schema=vol.Schema(schema),
            description_placeholders={"module_name": module_name},
        )

    # ------------------------------------------------------------------
    # DM117 CONFIGURATION
    # ------------------------------------------------------------------

    async def async_step_dm117_select(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Step 1: Selection of the DM117 module."""

        # Access the live detected devices from the I2C scan
        api = self._runtime_data
        if not api:
            return self.async_abort(reason="integration_not_ready")

        detected_modules = list(api.dm117.keys()) if api.dm117 else []

        if not detected_modules:
            return self.async_abort(reason="no_dm117_found")

        # If the user has made a selection
        if user_input is not None:
            self._selected_dm117_addr = int(user_input["selected_module"])
            return await self.async_step_dm117_config()

        return self.async_show_form(
            step_id="dm117_select",
            data_schema=vol.Schema(
                {
                    vol.Required("selected_module"): SelectSelector(
                        SelectSelectorConfig(options=self._module_selector_options("dm117", detected_modules))
                    )
                }
            ),
        )

    async def async_step_dm117_config(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Step 2: Configuration of the 8 slots for the selected module."""

        if (addr := self._selected_dm117_addr) is None:
            return self.async_abort(reason="integration_not_ready")

        if user_input is not None:
            slots = {index - 1: user_input[f"slot_{index}"] for index in range(1, 9) if f"slot_{index}" in user_input}
            return await self._stage(
                set_dm117_slots(
                    self._options,
                    addr,
                    slots,
                    name=str(user_input["module_name"]),
                )
            )

        configured = get_dm117_slot_types(self._options).get(addr, {})
        module_name = get_module_name(self._options, "dm117", addr, f"DM117 0x{addr:02X}")
        schema: dict[Any, Any] = {vol.Required("module_name", default=module_name): TextSelector(TextSelectorConfig())}
        for index in range(1, 9):
            schema[vol.Required(f"slot_{index}", default=configured.get(index - 1, "none"))] = SelectSelector(
                SelectSelectorConfig(options=DM117_SLOT_TYPES, translation_key="dm117_slot_type")
            )

        return self.async_show_form(
            step_id="dm117_config",
            data_schema=vol.Schema(schema),
            description_placeholders={"module_name": module_name},
        )

    # ------------------------------------------------------------------
    # SM117 CONFIGURATION
    # ------------------------------------------------------------------

    async def async_step_sm117_select(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Select a detected SM117 module."""

        api = self._runtime_data
        if not api:
            return self.async_abort(reason="integration_not_ready")

        detected_modules = list(api.sm117)
        if not detected_modules:
            return self.async_abort(reason="no_sm117_found")

        if user_input is not None:
            self._selected_sm117_addr = int(user_input["selected_module"])
            return await self.async_step_sm117_config()

        return self.async_show_form(
            step_id="sm117_select",
            data_schema=vol.Schema(
                {
                    vol.Required("selected_module"): SelectSelector(
                        SelectSelectorConfig(options=self._module_selector_options("sm117", detected_modules))
                    )
                }
            ),
        )

    async def async_step_sm117_config(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Configure the display name of one SM117 module."""

        if (addr := self._selected_sm117_addr) is None:
            return self.async_abort(reason="integration_not_ready")

        if user_input is not None:
            return await self._stage(
                set_module_name(
                    self._options,
                    "sm117",
                    addr,
                    str(user_input["module_name"]),
                )
            )

        module_name = get_module_name(self._options, "sm117", addr, f"SM117 0x{addr:02X}")
        return self.async_show_form(
            step_id="sm117_config",
            data_schema=vol.Schema(
                {vol.Required("module_name", default=module_name): TextSelector(TextSelectorConfig())}
            ),
            description_placeholders={"module_name": module_name},
        )

    # ------------------------------------------------------------------
    # ONE WIRE CONFIGURATION
    # ------------------------------------------------------------------

    async def async_step_onewire_select(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Step 1: Selection of the OneWire device."""

        api = self._runtime_data
        if not api:
            return self.async_abort(reason="integration_not_ready")

        await api.scan_onewire()

        # Assuming api.detected_onewire is a list of IDs ["28.AABBCC", "26.112233"]
        detected_devices = list(api.ow_devices) if api.ow_devices else []

        if not detected_devices:
            return self.async_abort(reason="no_onewire_found")

        if user_input is not None:
            self._selected_ow_id = user_input["selected_device"]
            return await self.async_step_onewire_config()

        options: list[SelectOptionDict] = []
        for dev_id in sorted(detected_devices):
            meta = api.ow_devices.get(dev_id, {})
            device_type = str(meta.get("device_type") or "OneWire")
            options.append({"value": dev_id, "label": f"{device_type} · {dev_id}"})

        return self.async_show_form(
            step_id="onewire_select",
            data_schema=vol.Schema(
                {vol.Required("selected_device"): SelectSelector(SelectSelectorConfig(options=options))}
            ),
        )

    async def async_step_onewire_config(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Step 2: Profile assignment for the selected OW device."""

        if (dev_id := self._selected_ow_id) is None:
            return self.async_abort(reason="integration_not_ready")

        if user_input is not None:
            self._selected_ow_profile = str(user_input["profile"])
            return await self.async_step_onewire_settings()

        default_val = get_configured_onewire_profiles(self._options).get(
            dev_id, self._default_profile_for_device(dev_id)
        )
        if default_val in {"ds2413_in", "ds2413_out"}:
            default_val = "ds2413"

        return self.async_show_form(
            step_id="onewire_config",
            data_schema=vol.Schema(
                {
                    vol.Required("profile", default=default_val): SelectSelector(
                        SelectSelectorConfig(options=ONEWIRE_PROFILES, translation_key="onewire_profile")
                    ),
                }
            ),
            description_placeholders={"device_id": dev_id},
        )

    async def async_step_onewire_settings(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Configure polling and profile-specific OneWire settings."""

        if (dev_id := self._selected_ow_id) is None or (profile := self._selected_ow_profile) is None:
            return self.async_abort(reason="integration_not_ready")

        if user_input is not None:
            channels = None
            stored_profile = profile
            if profile == "ds2413":
                channels = {
                    0: str(user_input["channel_1_profile"]),
                    1: str(user_input["channel_2_profile"]),
                }
            return await self._stage(
                set_onewire_device(
                    self._options,
                    dev_id,
                    stored_profile,
                    led_count=int(user_input["led_count"]) if profile == "ds28e17_led" else None,
                    poll_interval=int(user_input["poll_interval"]),
                    ds2413_channels=channels,
                )
            )

        poll_interval_default = get_configured_onewire_poll_intervals(self._options).get(
            dev_id, DEFAULT_OW_POLL_INTERVAL.get(profile, 60)
        )
        schema: dict[Any, Any] = {
            vol.Required("poll_interval", default=poll_interval_default): NumberSelector(
                NumberSelectorConfig(min=1, max=3600, step=1, mode=NumberSelectorMode.BOX)
            )
        }
        if profile == "ds28e17_led":
            led_count_default = get_configured_led_counts(self._options).get(dev_id, DEFAULT_LED_COUNT)
            schema[vol.Required("led_count", default=led_count_default)] = NumberSelector(
                NumberSelectorConfig(min=1, max=255, step=1, mode=NumberSelectorMode.BOX)
            )
        elif profile == "ds2413":
            channel_defaults = get_configured_ds2413_channels(self._options).get(
                dev_id,
                {0: DS2413_CHANNEL_INPUT, 1: DS2413_CHANNEL_INPUT},
            )
            channel_options = [DS2413_CHANNEL_INPUT, DS2413_CHANNEL_OUTPUT]
            for index in range(2):
                schema[vol.Required(f"channel_{index + 1}_profile", default=channel_defaults[index])] = SelectSelector(
                    SelectSelectorConfig(options=channel_options, translation_key="ds2413_channel_profile")
                )

        return self.async_show_form(
            step_id="onewire_settings",
            data_schema=vol.Schema(schema),
            description_placeholders={"device_id": dev_id},
        )


class CannotConnect(HomeAssistantError):
    """Error to indicate we cannot connect."""
