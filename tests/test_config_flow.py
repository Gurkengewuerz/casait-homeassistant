"""Config, discovery, reconfigure, and options flow coverage."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.casait_smarthome.const import (
    CONF_TIMEOUT,
    DOMAIN,
    INPUT_ROLE_BUTTON,
    INPUT_ROLE_CONTACT,
    INPUT_ROLE_UNUSED,
)
from custom_components.casait_smarthome.helpers import (
    get_dm117_input_configuration,
    get_dm117_slot_types,
    get_im117_port_configuration,
    get_input_module_settings,
    get_module_name,
)
from custom_components.casait_smarthome.services.smbus_proxy import BridgeFirmwareError
from homeassistant import config_entries
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.data_entry_flow import FlowResultType


@pytest.mark.unit
async def test_user_flow_creates_entry(hass) -> None:
    with patch(
        "custom_components.casait_smarthome.config_flow.validate_input",
        AsyncMock(return_value={"title": "bridge.local"}),
    ):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "user"

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_HOST: " bridge.local ", CONF_PORT: 8555, CONF_TIMEOUT: 2.0},
        )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {CONF_HOST: "bridge.local", CONF_PORT: 8555, CONF_TIMEOUT: 2.0}


@pytest.mark.unit
async def test_user_flow_accepts_outdated_bridge_firmware(hass) -> None:
    """The entry loads in firmware recovery mode and offers the update."""

    bus = SimpleNamespace(
        ping_info=AsyncMock(side_effect=BridgeFirmwareError("old")),
        close=AsyncMock(),
    )
    with patch(
        "custom_components.casait_smarthome.config_flow.SMBus.connect",
        AsyncMock(return_value=bus),
    ):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_HOST: "bridge.local", CONF_PORT: 8555, CONF_TIMEOUT: 2.0},
        )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    bus.close.assert_awaited_once()


@pytest.mark.unit
async def test_zeroconf_flow_uses_discovered_values(hass) -> None:
    discovery = SimpleNamespace(
        host="bridge.local.",
        ip_address=None,
        ip_addresses=[],
        port=8555,
        name="casaithome._http._tcp.local.",
        properties={"id": "bridge-1"},
    )

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_ZEROCONF},
        data=discovery,
    )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "zeroconf_confirm"
    assert result["description_placeholders"] == {"host": "bridge.local."}


@pytest.mark.unit
async def test_reconfigure_preserves_options(hass) -> None:
    options = {"modules": {"om117": {"32": {"name": "Living room"}}}}
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_HOST: "old.local", CONF_PORT: 8555, CONF_TIMEOUT: 2.0},
        options=options,
        unique_id="bridge-1",
    )
    entry.add_to_hass(hass)

    with (
        patch(
            "custom_components.casait_smarthome.config_flow.validate_input",
            AsyncMock(return_value={"title": "new.local"}),
        ),
        patch.object(hass.config_entries, "async_reload", AsyncMock(return_value=True)),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": config_entries.SOURCE_RECONFIGURE, "entry_id": entry.entry_id},
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_HOST: "new.local", CONF_PORT: 9555, CONF_TIMEOUT: 3.0},
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.data == {CONF_HOST: "new.local", CONF_PORT: 9555, CONF_TIMEOUT: 3.0}
    assert entry.options == options


def _runtime(**overrides) -> SimpleNamespace:
    """Return a stand-in for the running API with nothing on the bus."""

    values = {
        "im117_om117": {},
        "dm117": {},
        "sm117": {},
        "ow_ids": set(),
        "ow_devices": {},
        "scan_onewire": AsyncMock(),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


async def _open_device(hass, runtime: SimpleNamespace, device: str, options: dict | None = None):
    """Open the options flow and pick one device from the list."""

    entry = MockConfigEntry(domain=DOMAIN, data={CONF_HOST: "bridge.local", CONF_PORT: 8555}, options=options or {})
    entry.runtime_data = runtime
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.MENU
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "device"})
    assert result["step_id"] == "device"
    return await hass.config_entries.options.async_configure(result["flow_id"], {"device": device})


@pytest.mark.unit
async def test_device_list_covers_every_module_kind(hass) -> None:
    """One picker lists all hardware; there is no per-type submenu any more."""

    entry = MockConfigEntry(domain=DOMAIN, data={CONF_HOST: "bridge.local", CONF_PORT: 8555}, options={})
    entry.runtime_data = _runtime(
        im117_om117={0x38: object(), 0x20: object()},
        dm117={0x10: object()},
        sm117={0x18: object()},
        ow_devices={"2800000000000001": {"family_code": 0x28, "device_type": "DS18B20", "bus_address": 0x18}},
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "device"})

    selector = result["data_schema"].schema["device"]
    values = [option["value"] for option in selector.config["options"]]
    assert values == ["im117:56", "om117:32", "dm117:16", "sm117:24", "onewire:2800000000000001"]


@pytest.mark.unit
@pytest.mark.parametrize(
    ("device", "expected_step"),
    [
        ("im117:56", "im117"),
        ("om117:32", "om117"),
        ("dm117:16", "dm117"),
        ("sm117:24", "sm117"),
        ("onewire:2800000000000001", "onewire"),
    ],
)
async def test_every_device_opens_its_form(hass, device: str, expected_step: str) -> None:
    runtime = _runtime(
        im117_om117={0x38: object(), 0x20: object()},
        dm117={0x10: object()},
        sm117={0x18: object()},
        ow_devices={"2800000000000001": {"family_code": 0x28, "device_type": "DS18B20"}},
    )

    result = await _open_device(hass, runtime, device)

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == expected_step


@pytest.mark.unit
@pytest.mark.parametrize("menu_step", ["input_settings", "advanced_settings"])
async def test_global_settings_are_split(hass, menu_step: str) -> None:
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_HOST: "bridge.local", CONF_PORT: 8555}, options={})
    entry.runtime_data = _runtime()
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": menu_step})

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == menu_step


@pytest.mark.unit
async def test_im117_config_round_trips_the_input_model(hass) -> None:
    """Everything the IM117 form collects has to survive the save."""

    result = await _open_device(hass, _runtime(im117_om117={0x38: object()}), "im117:56")
    assert result["step_id"] == "im117"

    form = {
        "name": "Hallway",
        "debounce_ms": 65,
        "port_1": {"role": INPUT_ROLE_BUTTON, "device_class": "none", "invert": True, "repeat": False},
        "port_2": {"role": INPUT_ROLE_CONTACT, "device_class": "window", "invert": False, "repeat": False},
    }
    for index in range(3, 9):
        form[f"port_{index}"] = {"role": INPUT_ROLE_UNUSED, "device_class": "none", "invert": False, "repeat": False}

    result = await hass.config_entries.options.async_configure(result["flow_id"], form)
    # Staged edits are written by the save step, not by the module form itself.
    assert result["type"] is FlowResultType.MENU
    assert result["description_placeholders"] == {"pending": "1"}
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "save"})
    assert result["type"] is FlowResultType.CREATE_ENTRY

    ports = get_im117_port_configuration(result["data"])[0x38]
    assert ports[0].role == INPUT_ROLE_BUTTON
    assert ports[0].invert is True
    assert ports[1].device_class == "window"
    assert ports[7].role == INPUT_ROLE_UNUSED
    assert get_input_module_settings(result["data"], "im117")[0x38].debounce_ms == 65
    assert get_module_name(result["data"], "im117", 0x38, "fallback") == "Hallway"


@pytest.mark.unit
async def test_dm117_shows_channels_once_a_slot_becomes_an_input(hass) -> None:
    """Input channels appear in the same form after the slot type is switched."""

    result = await _open_device(hass, _runtime(dm117={0x10: object()}), "dm117:16")
    assert result["step_id"] == "dm117"
    assert "a_role" not in {str(key) for key in result["data_schema"].schema["slot_1"].schema.schema}

    form: dict = {"name": "Cellar", "debounce_ms": 30, "slot_1": {"type": "binary_input"}, "slot_2": {"type": "switch"}}
    form.update({f"slot_{index}": {"type": "none"} for index in range(3, 9)})
    result = await hass.config_entries.options.async_configure(result["flow_id"], form)

    # The same form comes back, now with the channels of the new input slot.
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "dm117"
    assert result["errors"] == {"base": "fields_updated"}
    slot_1 = {str(key) for key in result["data_schema"].schema["slot_1"].schema.schema}
    slot_2 = {str(key) for key in result["data_schema"].schema["slot_2"].schema.schema}
    assert {"a_role", "b_role", "a_device_class"} <= slot_1
    assert slot_2 == {"type"}

    form["slot_1"] = {
        "type": "binary_input",
        "a_role": INPUT_ROLE_BUTTON,
        "a_device_class": "none",
        "a_invert": False,
        "a_repeat": True,
        "b_role": INPUT_ROLE_CONTACT,
        "b_device_class": "door",
        "b_invert": True,
        "b_repeat": False,
    }
    result = await hass.config_entries.options.async_configure(result["flow_id"], form)
    assert result["type"] is FlowResultType.MENU
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "save"})
    assert result["type"] is FlowResultType.CREATE_ENTRY

    channels = get_dm117_input_configuration(result["data"])[0x10]
    assert channels[0, 0].role == INPUT_ROLE_BUTTON
    assert channels[0, 0].repeat is True
    assert channels[0, 1].device_class == "door"
    assert channels[0, 1].invert is True
    assert get_input_module_settings(result["data"], "dm117")[0x10].debounce_ms == 30
    slot_types = get_dm117_slot_types(result["data"])[0x10]
    assert (slot_types[0], slot_types[1]) == ("binary_input", "switch")


@pytest.mark.unit
async def test_dm117_without_inputs_saves_in_one_submit(hass) -> None:
    result = await _open_device(hass, _runtime(dm117={0x10: object()}), "dm117:16")

    form: dict = {"name": "Cellar", "debounce_ms": 40, "slot_1": {"type": "switch"}}
    form.update({f"slot_{index}": {"type": "none"} for index in range(2, 9)})
    result = await hass.config_entries.options.async_configure(result["flow_id"], form)

    assert result["type"] is FlowResultType.MENU


@pytest.mark.unit
async def test_onewire_name_survives_a_profile_change(hass) -> None:
    """A DS28E17 switched to the Multisensor profile keeps its name and drops LED fields."""

    runtime = _runtime(
        ow_devices={
            "1900000000000001": {
                "family_code": 0x19,
                "device_type": "DS28E17",
                "detected_profile": "ds28e17_led",
            }
        }
    )
    result = await _open_device(hass, runtime, "onewire:1900000000000001")
    fields = {str(key) for key in result["data_schema"].schema}
    assert {"name", "profile", "poll_interval", "led_count"} <= fields

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {"name": "Living room", "profile": "ds28e17_multisensor", "poll_interval": 10, "led_count": 30},
    )
    assert result["errors"] == {"base": "fields_updated"}
    fields = {str(key) for key in result["data_schema"].schema}
    assert "led_count" not in fields
    assert "poll_interval" not in fields

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"name": "Living room", "profile": "ds28e17_multisensor"}
    )
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "save"})

    assert result["data"]["onewire"]["1900000000000001"] == {"profile": "ds28e17_multisensor", "name": "Living room"}
