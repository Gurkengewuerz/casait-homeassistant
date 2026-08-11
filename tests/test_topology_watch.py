"""Topology watch: tolerance for missed scans and the device_gone repair flow."""

from __future__ import annotations

from collections import defaultdict

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.casait_smarthome.api import CasaITApi
from custom_components.casait_smarthome.const import DOMAIN
from custom_components.casait_smarthome.helpers import TopologySettings, build_device_identifier
from custom_components.casait_smarthome.repairs import CasaITRepairFlow
from homeassistant.helpers import device_registry as dr, issue_registry as ir


class FakeBus:
    """Minimal transport used by the topology unit tests."""

    stats = {"connected": True}


def _api(hass, *, missing_scans: int = 3, entry_id: str = "entry-test") -> CasaITApi:
    """Return an API with the topology watch enabled."""

    return CasaITApi(
        hass,
        FakeBus(),
        entry_id,
        topology_settings=TopologySettings(scan_interval=300, missing_scans=missing_scans),
    )


def _found(**codes: list[int]) -> defaultdict[str, set[int]]:
    """Build the scan result shape scan_devices passes around."""

    found: defaultdict[str, set[int]] = defaultdict(set)
    for code, addresses in codes.items():
        found[code].update(addresses)
    return found


@pytest.mark.unit
def test_missing_module_is_kept_until_the_threshold_is_reached(hass) -> None:
    """A module below the miss threshold stays in the topology."""

    api = _api(hass, missing_scans=3)

    for expected_misses in (1, 2):
        api.found_i2c_devices = {"IM117": [0x38]}
        found = _found()
        api._apply_miss_tolerance(found, tolerate=True)  # noqa: SLF001
        assert found["IM117"] == {0x38}
        assert api._missing_scans[("IM117", 0x38)] == expected_misses  # noqa: SLF001

    api.found_i2c_devices = {"IM117": [0x38]}
    found = _found()
    api._apply_miss_tolerance(found, tolerate=True)  # noqa: SLF001
    assert 0x38 not in found.get("IM117", set())


@pytest.mark.unit
def test_reappearing_module_resets_the_miss_counter(hass) -> None:
    """One successful scan clears the accumulated misses."""

    api = _api(hass, missing_scans=3)

    api.found_i2c_devices = {"IM117": [0x38]}
    api._apply_miss_tolerance(_found(), tolerate=True)  # noqa: SLF001
    assert api._missing_scans  # noqa: SLF001

    api.found_i2c_devices = {"IM117": [0x38]}
    found = _found(IM117=[0x38])
    api._apply_miss_tolerance(found, tolerate=True)  # noqa: SLF001

    assert found["IM117"] == {0x38}
    assert api._missing_scans == {}  # noqa: SLF001


@pytest.mark.unit
def test_explicit_scan_reports_what_the_bus_answered(hass) -> None:
    """Without tolerance a missing module drops out immediately."""

    api = _api(hass, missing_scans=3)
    api.found_i2c_devices = {"IM117": [0x38]}

    found = _found()
    api._apply_miss_tolerance(found, tolerate=False)  # noqa: SLF001

    assert "IM117" not in found
    assert api._missing_scans == {}  # noqa: SLF001


@pytest.mark.unit
def test_missing_onewire_chip_is_kept_until_the_threshold(hass) -> None:
    """1-Wire chips get the same tolerance as the I2C modules."""

    api = _api(hass, missing_scans=2)
    api.ow_devices = {"28abc": {"bus_address": 0x18}}

    discovered: dict[str, dict[str, object]] = {}
    api._apply_onewire_miss_tolerance(discovered, tolerate=True)  # noqa: SLF001
    assert "28abc" in discovered

    discovered = {}
    api._apply_onewire_miss_tolerance(discovered, tolerate=True)  # noqa: SLF001
    assert discovered == {}


@pytest.mark.unit
def test_topology_watch_is_off_by_default() -> None:
    """The watch has to be switched on; it does not cost bus time unasked."""

    assert TopologySettings().enabled is False
    assert TopologySettings(scan_interval=300).enabled is True


def _registered_device(hass, entry: MockConfigEntry, identifier: str) -> dr.DeviceEntry:
    """Create one registry device belonging to the config entry."""

    return dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, identifier)},
        name="IM117 0x38",
    )


@pytest.mark.unit
async def test_disappeared_device_is_reported_and_not_removed(hass) -> None:
    """A device the bus stopped answering for keeps its registry entry."""

    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    identifier = build_device_identifier(entry.entry_id, "im117", 0x38)
    device = _registered_device(hass, entry, identifier)

    api = _api(hass, entry_id=entry.entry_id)
    api.found_i2c_devices = {}
    api._sync_disappeared_device_issues()  # noqa: SLF001

    assert dr.async_get(hass).async_get(device.id) is not None
    issue = ir.async_get(hass).async_get_issue(DOMAIN, f"device_gone_{entry.entry_id}_{identifier}")
    assert issue is not None
    assert issue.translation_key == "device_gone"


@pytest.mark.unit
async def test_issue_is_cleared_once_the_device_answers_again(hass) -> None:
    """The issue disappears on its own when the module comes back."""

    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    identifier = build_device_identifier(entry.entry_id, "im117", 0x38)
    _registered_device(hass, entry, identifier)

    api = _api(hass, entry_id=entry.entry_id)
    api.found_i2c_devices = {}
    api._sync_disappeared_device_issues()  # noqa: SLF001

    api.found_i2c_devices = {"IM117": [0x38]}
    api._sync_disappeared_device_issues()  # noqa: SLF001

    assert ir.async_get(hass).async_get_issue(DOMAIN, f"device_gone_{entry.entry_id}_{identifier}") is None


@pytest.mark.unit
async def test_forget_step_removes_the_device_and_closes_the_issue(hass) -> None:
    """Answering the repair with forget is what actually deletes anything."""

    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    identifier = build_device_identifier(entry.entry_id, "im117", 0x38)
    device = _registered_device(hass, entry, identifier)

    api = _api(hass, entry_id=entry.entry_id)
    api.found_i2c_devices = {}
    api._sync_disappeared_device_issues()  # noqa: SLF001

    issue_id = f"device_gone_{entry.entry_id}_{identifier}"
    flow = CasaITRepairFlow(issue_id, {"entry_id": entry.entry_id, "identifier": identifier, "name": "IM117 0x38"})
    flow.hass = hass

    confirm = await flow.async_step_forget()
    assert confirm["type"] == "form"
    assert confirm["step_id"] == "forget"

    await flow.async_step_forget({})

    assert dr.async_get(hass).async_get(device.id) is None
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None


@pytest.mark.unit
async def test_device_gone_flow_offers_rescan_and_forget(hass) -> None:
    """The repair asks rather than deciding for the user."""

    flow = CasaITRepairFlow("device_gone_entry-test_x", {"entry_id": "entry-test", "identifier": "x", "name": "IM117"})
    flow.hass = hass

    result = await flow.async_step_init()

    assert result["type"] == "menu"
    assert result["menu_options"] == ["rescan", "forget"]
    assert result["description_placeholders"] == {"name": "IM117"}
