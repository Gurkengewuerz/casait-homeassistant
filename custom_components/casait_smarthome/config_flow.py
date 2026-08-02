"""Config flow for the casaIT : Smart Home integration."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntry, ConfigFlowResult, OptionsFlow, OptionsFlowWithReload
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo

from .const import (
    CONF_TIMEOUT,
    CONFIG_ENTRY_VERSION,
    DEFAULT_BLIND_CLOSE_TIME,
    DEFAULT_BLIND_OPEN_TIME,
    DEFAULT_BLIND_OVERRUN_TIME,
    DEFAULT_LED_COUNT,
    DEFAULT_OW_POLL_INTERVAL,
    DEFAULT_OW_PROFILE,
    DOMAIN,
    OM117_MODE_BLIND,
    OM117_MODE_SWITCH,
)
from .helpers import (
    OM117PairConfig,
    get_address_range,
    get_configured_led_counts,
    get_configured_onewire_poll_intervals,
    get_configured_onewire_profiles,
    get_dm117_slot_types,
    get_om117_pair_configuration,
    set_dm117_slots,
    set_om117_pairs,
    set_onewire_device,
)
from .services.smbus_proxy import DEFAULT_PORT, DEFAULT_TIMEOUT, SMBus, SMBusProxyError

_LOGGER = logging.getLogger(__name__)

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOST): str,
        vol.Required(CONF_PORT, default=DEFAULT_PORT): int,
        vol.Required(CONF_TIMEOUT, default=DEFAULT_TIMEOUT): vol.All(vol.Coerce(float), vol.Range(min=0)),
    }
)

OM117_SLOT_TYPES = {
    OM117_MODE_SWITCH: "Digitaler Ausgang",
    OM117_MODE_BLIND: "Jalousie / Rollladen",
}

DM117_SLOT_TYPES = {
    "none": "Nicht belegt",
    "binary_input": "Digitaler Eingang (24V)",
    "switch": "Digitaler Ausgang (24V)",
    "dimmer": "Analog Ausgang (Dimmer 0-10V)",
}

ONEWIRE_PROFILES = {
    "ds18b20_temp": "DS18B20: Temperatursensor",
    "ds2438_hih4030_tept5600": "DS2438: HIH4030 / TEPT5600",
    "ds2438_hih5030_tept5600": "DS2438: HIH5030 / TEPT5600",
    "ds2413_out": "DS2413: 2 digitale Ausgänge",
    "ds2413_in": "DS2413: 2 digitale Eingänge",
    "ds28e17_led": "DS28E17: LED Controller",
}


async def validate_input(hass: HomeAssistant, data: dict[str, Any]) -> dict[str, Any]:
    """Validate the user input allows us to connect.

    Data has the keys from STEP_USER_DATA_SCHEMA with values provided by the user.
    """
    try:
        bus = await hass.async_add_executor_job(SMBus, 1, data[CONF_HOST], data[CONF_PORT], data[CONF_TIMEOUT])
        await hass.async_add_executor_job(bus.close)
    except SMBusProxyError as exc:
        raise CannotConnect from exc

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
            self._async_abort_entries_match(
                {
                    CONF_HOST: user_input[CONF_HOST],
                    CONF_PORT: user_input[CONF_PORT],
                }
            )
            try:
                info = await validate_input(self.hass, user_input)
            except CannotConnect:
                errors["base"] = "cannot_connect"
            except Exception:
                _LOGGER.exception("Unexpected exception")
                errors["base"] = "unknown"
            else:
                return self.async_create_entry(title=info["title"], data=user_input)

        return self.async_show_form(step_id="user", data_schema=STEP_USER_DATA_SCHEMA, errors=errors)

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
        data_schema = vol.Schema(
            {
                vol.Required(CONF_HOST, default=self._discovered_host): str,
                vol.Required(CONF_PORT, default=self._discovered_port): int,
                vol.Required(CONF_TIMEOUT, default=self._discovered_timeout): vol.All(
                    vol.Coerce(float), vol.Range(min=0)
                ),
            }
        )

        if user_input is not None:
            try:
                info = await validate_input(self.hass, user_input)
            except CannotConnect:
                errors["base"] = "cannot_connect"
            except Exception:
                _LOGGER.exception("Unexpected exception")
                errors["base"] = "unknown"
            else:
                return self.async_create_entry(title=info["title"], data=user_input)

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
        self._selected_om117_addr: int | None = None
        self._selected_dm117_addr: int | None = None
        self._selected_ow_id: str | None = None

    @property
    def _runtime_data(self):
        """Helper function to access the running API instance.

        We need to know which devices were FOUND on the bus.
        """
        return self.config_entry.runtime_data

    def _default_profile_for_device(self, device_id: str) -> str:
        """Return default OneWire profile based on family code if available."""

        api = self._runtime_data
        if not api:
            return list(ONEWIRE_PROFILES)[0]

        family_code = api.ow_devices.get(device_id, {}).get("family_code")
        if family_code is None:
            return list(ONEWIRE_PROFILES)[0]

        return DEFAULT_OW_PROFILE.get(family_code, list(ONEWIRE_PROFILES)[0])

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Manage the options menu."""
        return self.async_show_menu(
            step_id="init",
            menu_options=["om117_select", "dm117_select", "onewire_select"],
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

        options = {str(addr): f"OM117 at Address {addr} (0x{int(addr):02x})" for addr in detected_modules}

        return self.async_show_form(
            step_id="om117_select",
            data_schema=vol.Schema({vol.Required("selected_module"): vol.In(options)}),
        )

    async def async_step_om117_config(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Step 2: Configuration of the 4 output pairs for the selected module."""

        if self._selected_om117_addr is None:
            return self.async_abort(reason="integration_not_ready")

        addr = self._selected_om117_addr
        existing = get_om117_pair_configuration(self.config_entry.options).get(addr, {})

        if user_input is not None:
            pairs = {
                pair_index - 1: OM117PairConfig(
                    mode=user_input[f"pair_{pair_index}_mode"],
                    open_time=float(user_input[f"pair_{pair_index}_open_time"]),
                    close_time=float(user_input[f"pair_{pair_index}_close_time"]),
                    overrun_time=float(user_input[f"pair_{pair_index}_overrun_time"]),
                )
                for pair_index in range(1, 5)
            }
            return self.async_create_entry(title="", data=set_om117_pairs(self.config_entry.options, addr, pairs))

        schema: dict[Any, Any] = {}
        for idx in range(1, 5):
            config: OM117PairConfig = existing.get(idx - 1, OM117PairConfig())
            schema[vol.Required(f"pair_{idx}_mode", default=config.mode)] = vol.In(OM117_SLOT_TYPES)
            schema[
                vol.Optional(
                    f"pair_{idx}_open_time",
                    default=config.open_time,
                )
            ] = vol.All(vol.Coerce(float), vol.Range(min=1, max=180))
            schema[
                vol.Optional(
                    f"pair_{idx}_close_time",
                    default=config.close_time,
                )
            ] = vol.All(vol.Coerce(float), vol.Range(min=1, max=180))
            schema[
                vol.Optional(
                    f"pair_{idx}_overrun_time",
                    default=config.overrun_time,
                )
            ] = vol.All(vol.Coerce(float), vol.Range(min=0, max=15))

        return self.async_show_form(
            step_id="om117_config",
            data_schema=vol.Schema(schema),
            description_placeholders={
                "module_name": f"Address {addr}",
                "default_open": str(DEFAULT_BLIND_OPEN_TIME),
                "default_close": str(DEFAULT_BLIND_CLOSE_TIME),
                "default_overrun": str(DEFAULT_BLIND_OVERRUN_TIME),
            },
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

        # Show form
        options = {str(addr): f"DM117 at Address {addr} (0x{int(addr):02x})" for addr in detected_modules}

        return self.async_show_form(
            step_id="dm117_select",
            data_schema=vol.Schema({vol.Required("selected_module"): vol.In(options)}),
        )

    async def async_step_dm117_config(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Step 2: Configuration of the 8 slots for the selected module."""

        if (addr := self._selected_dm117_addr) is None:
            return self.async_abort(reason="integration_not_ready")

        if user_input is not None:
            slots = {index - 1: user_input[f"slot_{index}"] for index in range(1, 9) if f"slot_{index}" in user_input}
            return self.async_create_entry(title="", data=set_dm117_slots(self.config_entry.options, addr, slots))

        configured = get_dm117_slot_types(self.config_entry.options).get(addr, {})
        schema = {
            vol.Required(f"slot_{index}", default=configured.get(index - 1, "none")): vol.In(DM117_SLOT_TYPES)
            for index in range(1, 9)
        }

        return self.async_show_form(
            step_id="dm117_config",
            data_schema=vol.Schema(schema),
            description_placeholders={"module_name": f"Address {addr}"},
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

        # List the devices with the current profile name (if already configured)
        configured_profiles = get_configured_onewire_profiles(self.config_entry.options)
        options = {}
        for dev_id in detected_devices:
            current_profile_key = configured_profiles.get(dev_id)
            profile_name = (
                ONEWIRE_PROFILES.get(current_profile_key, "Unconfigured") if current_profile_key else "Unconfigured"
            )
            meta = api.ow_devices.get(dev_id, {})
            device_type = meta.get("device_type", "Unknown")
            options[dev_id] = f"{dev_id} ({device_type} / {profile_name})"

        return self.async_show_form(
            step_id="onewire_select",
            data_schema=vol.Schema({vol.Required("selected_device"): vol.In(options)}),
        )

    async def async_step_onewire_config(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Step 2: Profile assignment for the selected OW device."""

        if (dev_id := self._selected_ow_id) is None:
            return self.async_abort(reason="integration_not_ready")

        if user_input is not None:
            profile = user_input["profile"]
            return self.async_create_entry(
                title="",
                data=set_onewire_device(
                    self.config_entry.options,
                    dev_id,
                    profile,
                    # The LED count only means anything for the strip controller.
                    led_count=user_input.get("led_count") if profile == "ds28e17_led" else None,
                    poll_interval=user_input.get("poll_interval"),
                ),
            )

        default_val = get_configured_onewire_profiles(self.config_entry.options).get(
            dev_id, self._default_profile_for_device(dev_id)
        )

        led_count_default = get_configured_led_counts(self.config_entry.options).get(dev_id, DEFAULT_LED_COUNT)
        poll_interval_default = get_configured_onewire_poll_intervals(self.config_entry.options).get(
            dev_id, DEFAULT_OW_POLL_INTERVAL.get(default_val, 60)
        )

        return self.async_show_form(
            step_id="onewire_config",
            data_schema=vol.Schema(
                {
                    vol.Required("profile", default=default_val): vol.In(ONEWIRE_PROFILES),
                    vol.Optional("led_count", default=led_count_default): vol.All(
                        vol.Coerce(int), vol.Range(min=1, max=255)
                    ),
                    vol.Optional("poll_interval", default=poll_interval_default): vol.All(
                        vol.Coerce(int), vol.Range(min=1, max=3600)
                    ),
                }
            ),
            description_placeholders={"device_id": dev_id},
        )


class CannotConnect(HomeAssistantError):
    """Error to indicate we cannot connect."""
