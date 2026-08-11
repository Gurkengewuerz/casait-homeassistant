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
    get_im117_port_configuration,
    get_input_module_settings,
    get_module_name,
)
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


@pytest.mark.unit
@pytest.mark.parametrize(
    ("menu_step", "expected_step"),
    [
        ("im117_select", "im117_select"),
        ("om117_select", "om117_select"),
        ("dm117_select", "dm117_select"),
        ("sm117_select", "sm117_select"),
        ("onewire_select", "onewire_select"),
        ("global_settings", "global_settings"),
    ],
)
async def test_every_options_branch_is_reachable(hass, menu_step: str, expected_step: str) -> None:
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_HOST: "bridge.local", CONF_PORT: 8555}, options={})
    entry.runtime_data = SimpleNamespace(
        im117_om117={0x38: object(), 0x20: object()},
        dm117={0x10: object()},
        sm117={0x18: object()},
        ow_ids={"2800000000000001"},
        ow_devices={"2800000000000001": {"family_code": 0x28, "device_type": "DS18B20"}},
        scan_onewire=AsyncMock(),
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.MENU

    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": menu_step})

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == expected_step


@pytest.mark.unit
async def test_im117_config_round_trips_the_input_model(hass) -> None:
    """Everything the IM117 form collects has to survive the save."""

    entry = MockConfigEntry(domain=DOMAIN, data={CONF_HOST: "bridge.local", CONF_PORT: 8555}, options={})
    entry.runtime_data = SimpleNamespace(
        im117_om117={0x38: object()},
        dm117={},
        sm117={},
        ow_ids=set(),
        ow_devices={},
        scan_onewire=AsyncMock(),
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "im117_select"})
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"selected_module": "56"})
    assert result["step_id"] == "im117_config"

    form = {
        "module_name": "Hallway",
        "debounce_ms": 65,
        "port_1_role": INPUT_ROLE_BUTTON,
        "port_1_invert": True,
        "port_2_role": INPUT_ROLE_CONTACT,
        "port_2_device_class": "window",
        "port_2_invert": False,
    }
    for index in range(3, 9):
        form[f"port_{index}_role"] = INPUT_ROLE_UNUSED
        form[f"port_{index}_invert"] = False

    result = await hass.config_entries.options.async_configure(result["flow_id"], form)
    # Staged edits are written by the save step, not by the module form itself.
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "save"})
    assert result["type"] is FlowResultType.CREATE_ENTRY

    ports = get_im117_port_configuration(result["data"])[0x38]
    assert ports[0].role == INPUT_ROLE_BUTTON
    assert ports[0].invert is True
    assert ports[1].device_class == "window"
    assert ports[7].role == INPUT_ROLE_UNUSED
    assert get_input_module_settings(result["data"], "im117")[0x38].debounce_ms == 65
    assert get_module_name(result["data"], "im117", 0x38, "fallback") == "Hallway"
