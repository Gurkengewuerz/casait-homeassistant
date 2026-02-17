"""Config flow for the casaIT : Smart Home integration."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntry, ConfigFlowResult, OptionsFlow
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError

from .const import CONF_TIMEOUT, DEFAULT_LED_COUNT, DEFAULT_OW_PROFILE, DOMAIN
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
    "switch": "Digitaler Ausgang",
    "blind": "Jalousie / Rollladen",
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

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Create the options flow."""
        return OptionsFlowHandler(config_entry)

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


class OptionsFlowHandler(OptionsFlow):
    """Handle options flow for casaIT."""

    def __init__(self, config_entry: ConfigEntry) -> None:
        """Initialize options flow."""
        # Temporary storage for selection in the flow
        self._selected_dm117_addr = None
        self._selected_ow_id = None

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
            menu_options=["dm117_select", "onewire_select"],
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
            self._selected_dm117_addr = user_input["selected_module"]
            return await self.async_step_dm117_config()

        # Show form
        options = {str(addr): f"DM117 at Address {addr} (0x{int(addr):02x})" for addr in detected_modules}

        return self.async_show_form(
            step_id="dm117_select",
            data_schema=vol.Schema({vol.Required("selected_module"): vol.In(options)}),
        )

    async def async_step_dm117_config(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Step 2: Configuration of the 8 slots for the selected module."""

        if user_input is not None:
            # Merge slot options for the selected module without touching others.
            addr = self._selected_dm117_addr
            new_options = dict(self.config_entry.options)
            for i in range(1, 9):
                slot_key = f"slot_{i}"
                option_key = f"dm117_{addr}_slot_{i}"
                if slot_key in user_input:
                    new_options[option_key] = user_input[slot_key]

            return self.async_create_entry(title="", data=new_options)

        # Build schema
        schema = {}
        current_options = self.config_entry.options
        addr = self._selected_dm117_addr

        for i in range(1, 9):  # Slot 1 to 8
            # Key example: dm117_32_slot_1 (where 32 is the decimal address)
            option_key = f"dm117_{addr}_slot_{i}"
            default_val = current_options.get(option_key, "none")

            schema[vol.Required(f"slot_{i}", default=default_val)] = vol.In(DM117_SLOT_TYPES)

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
        options = {}
        for dev_id in detected_devices:
            current_profile_key = self.config_entry.options.get(f"ow_{dev_id}_profile")
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

        if user_input is not None:
            new_options = dict(self.config_entry.options)
            option_key = f"ow_{self._selected_ow_id}_profile"
            led_count_key = f"ow_{self._selected_ow_id}_led_count"
            profile = user_input["profile"]

            new_options[option_key] = profile

            if profile == "ds28e17_led":
                if "led_count" in user_input:
                    new_options[led_count_key] = user_input["led_count"]
            else:
                new_options.pop(led_count_key, None)
            return self.async_create_entry(title="", data=new_options)

        dev_id = self._selected_ow_id
        if dev_id is None:
            return self.async_abort(reason="integration_not_ready")
        key = f"ow_{dev_id}_profile"
        default_val = self.config_entry.options.get(key, self._default_profile_for_device(dev_id))

        led_count_default = self.config_entry.options.get(f"ow_{dev_id}_led_count", DEFAULT_LED_COUNT)

        return self.async_show_form(
            step_id="onewire_config",
            data_schema=vol.Schema(
                {
                    vol.Required("profile", default=default_val): vol.In(ONEWIRE_PROFILES),
                    vol.Optional("led_count", default=led_count_default): vol.All(
                        vol.Coerce(int), vol.Range(min=1, max=255)
                    ),
                }
            ),
            description_placeholders={"device_id": dev_id},
        )


class CannotConnect(HomeAssistantError):
    """Error to indicate we cannot connect."""
