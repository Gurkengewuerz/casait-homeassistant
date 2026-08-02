"""Behavioural tests for how button inputs classify edges into events."""

from types import SimpleNamespace

import pytest

from custom_components.casait_smarthome import event as event_module
from custom_components.casait_smarthome.const import (
    EVENT_DOUBLE_PRESS,
    EVENT_LONG_PRESS,
    EVENT_PRESS,
    PCF8574_MAPPED_PORTS,
)
from custom_components.casait_smarthome.event import CasaITButtonEvent
from custom_components.casait_smarthome.helpers import InputSettings

ENTRY = SimpleNamespace(entry_id="entry-test", unique_id="AA:BB:CC:DD:EE:FF")
PORT = 0
HARDWARE_PORT = PCF8574_MAPPED_PORTS[PORT]

# Chip levels: inputs are active low, so False means "held down".
PRESSED = False
RELEASED = True


@pytest.fixture
def clock(monkeypatch):
    """Drive the entity's notion of time by hand."""

    now = {"t": 0.0}
    monkeypatch.setattr(event_module.time, "monotonic", lambda: now["t"])
    return now


def _build(settings: InputSettings) -> tuple[CasaITButtonEvent, list[str]]:
    api = SimpleNamespace(pcf_states={0x38: [1] * 8})
    entity = CasaITButtonEvent(api, ENTRY, 0x38, PORT, settings)

    fired: list[str] = []
    entity._fire = fired.append  # noqa: SLF001
    return entity, fired


def _edges(*levels: bool) -> dict[int, list[bool]]:
    return {HARDWARE_PORT: list(levels)}


def test_short_press_fires_immediately_by_default(clock) -> None:
    entity, fired = _build(InputSettings(long_press_ms=500, double_click_ms=0))

    entity._handle_edges(_edges(PRESSED))  # noqa: SLF001
    clock["t"] += 0.05
    entity._handle_edges(_edges(RELEASED))  # noqa: SLF001

    assert fired == [EVENT_PRESS]


def test_holding_past_the_threshold_reports_a_long_press(clock) -> None:
    entity, fired = _build(InputSettings(long_press_ms=500, double_click_ms=0))

    entity._handle_edges(_edges(PRESSED))  # noqa: SLF001
    clock["t"] += 0.75
    entity._handle_edges(_edges(RELEASED))  # noqa: SLF001

    assert fired == [EVENT_LONG_PRESS]


def test_both_edges_in_one_read_are_still_classified(clock) -> None:
    """A tap caught in a single sample arrives as two edges at once."""

    entity, fired = _build(InputSettings(long_press_ms=500, double_click_ms=0))

    entity._handle_edges(_edges(PRESSED, RELEASED))  # noqa: SLF001

    assert fired == [EVENT_PRESS]


def test_release_without_a_preceding_press_is_ignored(clock) -> None:
    entity, fired = _build(InputSettings(long_press_ms=500, double_click_ms=0))

    entity._handle_edges(_edges(RELEASED))  # noqa: SLF001

    assert fired == []


def test_edges_on_other_ports_are_ignored(clock) -> None:
    entity, fired = _build(InputSettings(long_press_ms=500, double_click_ms=0))
    other_port = next(port for port in PCF8574_MAPPED_PORTS.values() if port != HARDWARE_PORT)

    entity._handle_edges({other_port: [PRESSED, RELEASED]})  # noqa: SLF001

    assert fired == []


def test_second_press_inside_the_window_reports_a_double_press(clock, monkeypatch) -> None:
    entity, fired = _build(InputSettings(long_press_ms=500, double_click_ms=300))
    scheduled: list[float] = []

    def fake_call_later(_hass, delay, _action):
        scheduled.append(delay)
        return lambda: None

    monkeypatch.setattr(event_module, "async_call_later", fake_call_later)

    entity._handle_edges(_edges(PRESSED, RELEASED))  # noqa: SLF001
    # The single press is held back while the double click window is open.
    assert fired == []
    assert scheduled == [0.3]

    clock["t"] += 0.1
    entity._handle_edges(_edges(PRESSED, RELEASED))  # noqa: SLF001

    assert fired == [EVENT_DOUBLE_PRESS]


def test_single_press_is_emitted_when_the_window_expires(clock, monkeypatch) -> None:
    entity, fired = _build(InputSettings(long_press_ms=500, double_click_ms=300))
    pending: list = []

    monkeypatch.setattr(
        event_module,
        "async_call_later",
        lambda _hass, _delay, action: pending.append(action) or (lambda: None),
    )

    entity._handle_edges(_edges(PRESSED, RELEASED))  # noqa: SLF001
    assert fired == []

    pending[0](None)

    assert fired == [EVENT_PRESS]


def test_long_press_cancels_a_queued_single_press(clock) -> None:
    entity, fired = _build(InputSettings(long_press_ms=500, double_click_ms=300))
    cancelled: list[bool] = []
    entity._pending_single = lambda: cancelled.append(True)  # noqa: SLF001

    entity._handle_edges(_edges(PRESSED))  # noqa: SLF001
    clock["t"] += 0.75
    entity._handle_edges(_edges(RELEASED))  # noqa: SLF001

    assert cancelled == [True]
    assert fired == [EVENT_LONG_PRESS]
