"""Behavioural tests for how button inputs classify edges into events."""

from types import SimpleNamespace

import pytest

from custom_components.casait_smarthome import event as event_module
from custom_components.casait_smarthome.const import (
    EVENT_DOUBLE_PRESS,
    EVENT_LONG_PRESS,
    EVENT_LONG_RELEASE,
    EVENT_REPEAT,
    EVENT_SINGLE_PRESS,
    EVENT_SINGLE_RELEASE,
    PCF8574_MAPPED_PORTS,
)
from custom_components.casait_smarthome.event import CasaITButtonEvent, CasaITDM117ButtonEvent
from custom_components.casait_smarthome.helpers import InputSettings

ENTRY = SimpleNamespace(entry_id="entry-test", unique_id="AA:BB:CC:DD:EE:FF", options={})
PORT = 0
HARDWARE_PORT = PCF8574_MAPPED_PORTS[PORT]

# Chip levels: inputs are active low, so False means "held down".
PRESSED = False
RELEASED = True


class _Timers:
    """Collect the timers the entity arms instead of running them for real."""

    def __init__(self) -> None:
        self.scheduled: list[SimpleNamespace] = []

    def call_later(self, _hass, delay, action):
        timer = SimpleNamespace(delay=delay, action=action, cancelled=False)
        self.scheduled.append(timer)

        def cancel() -> None:
            timer.cancelled = True

        return cancel

    @property
    def delays(self) -> list[float]:
        """Return the delays of every timer still waiting to run."""

        return [timer.delay for timer in self.scheduled if not timer.cancelled]

    def run(self, delay: float) -> None:
        """Run the pending timer armed for the given delay."""

        for timer in self.scheduled:
            if not timer.cancelled and timer.delay == pytest.approx(delay):
                timer.cancelled = True
                timer.action(None)
                return
        raise AssertionError(f"no pending timer for {delay}s in {self.delays}")


@pytest.fixture
def timers(monkeypatch) -> _Timers:
    """Drive the entity's timers by hand."""

    collected = _Timers()
    monkeypatch.setattr(event_module, "async_call_later", collected.call_later)
    return collected


def _build(
    settings: InputSettings, *, invert: bool = False, repeat: bool = False
) -> tuple[CasaITButtonEvent, list[str]]:
    api = SimpleNamespace(pcf_states={0x38: [1] * 8})
    entity = CasaITButtonEvent(api, ENTRY, 0x38, PORT, settings, invert=invert, repeat=repeat)

    fired: list[str] = []
    entity._fire = fired.append  # noqa: SLF001
    return entity, fired


def _edges(*levels: bool) -> dict[int, list[bool]]:
    return {HARDWARE_PORT: list(levels)}


def test_a_tap_reports_press_and_release(timers) -> None:
    entity, fired = _build(InputSettings(long_press_ms=500, double_click_ms=0))

    entity._handle_edges(_edges(PRESSED))  # noqa: SLF001
    assert fired == [EVENT_SINGLE_PRESS]

    entity._handle_edges(_edges(RELEASED))  # noqa: SLF001

    assert fired == [EVENT_SINGLE_PRESS, EVENT_SINGLE_RELEASE]
    # Releasing in time must disarm the hold timer.
    assert timers.delays == []


def test_long_press_is_reported_while_the_button_is_held(timers) -> None:
    """The event fires when the threshold passes, not on release."""

    entity, fired = _build(InputSettings(long_press_ms=500, double_click_ms=0))

    entity._handle_edges(_edges(PRESSED))  # noqa: SLF001
    assert timers.delays == [0.5]
    assert fired == [EVENT_SINGLE_PRESS]

    timers.run(0.5)
    assert fired == [EVENT_SINGLE_PRESS, EVENT_LONG_PRESS]

    entity._handle_edges(_edges(RELEASED))  # noqa: SLF001

    assert fired == [EVENT_SINGLE_PRESS, EVENT_LONG_PRESS, EVENT_LONG_RELEASE]


def test_a_hold_does_not_report_a_short_release(timers) -> None:
    entity, fired = _build(InputSettings(long_press_ms=500, double_click_ms=0))

    entity._handle_edges(_edges(PRESSED))  # noqa: SLF001
    timers.run(0.5)
    entity._handle_edges(_edges(RELEASED))  # noqa: SLF001

    assert EVENT_SINGLE_RELEASE not in fired


def test_a_second_hold_reports_again(timers) -> None:
    entity, fired = _build(InputSettings(long_press_ms=500, double_click_ms=0))

    for _ in range(2):
        entity._handle_edges(_edges(PRESSED))  # noqa: SLF001
        timers.run(0.5)
        entity._handle_edges(_edges(RELEASED))  # noqa: SLF001

    assert fired == [
        EVENT_SINGLE_PRESS,
        EVENT_LONG_PRESS,
        EVENT_LONG_RELEASE,
        EVENT_SINGLE_PRESS,
        EVENT_LONG_PRESS,
        EVENT_LONG_RELEASE,
    ]


def test_both_edges_in_one_read_are_still_classified(timers) -> None:
    """A tap caught in a single sample arrives as two edges at once."""

    entity, fired = _build(InputSettings(long_press_ms=500, double_click_ms=0))

    entity._handle_edges(_edges(PRESSED, RELEASED))  # noqa: SLF001

    assert fired == [EVENT_SINGLE_PRESS, EVENT_SINGLE_RELEASE]
    assert timers.delays == []


def test_release_without_a_preceding_press_is_ignored(timers) -> None:
    entity, fired = _build(InputSettings(long_press_ms=500, double_click_ms=0))

    entity._handle_edges(_edges(RELEASED))  # noqa: SLF001

    assert fired == []


def test_edges_on_other_ports_are_ignored(timers) -> None:
    entity, fired = _build(InputSettings(long_press_ms=500, double_click_ms=300))
    other_port = next(port for port in PCF8574_MAPPED_PORTS.values() if port != HARDWARE_PORT)

    entity._handle_edges({other_port: [PRESSED, RELEASED]})  # noqa: SLF001

    assert fired == []
    assert timers.delays == []


def test_second_press_inside_the_window_reports_a_double_press(timers) -> None:
    entity, fired = _build(InputSettings(long_press_ms=500, double_click_ms=300))

    entity._handle_edges(_edges(PRESSED, RELEASED))  # noqa: SLF001
    assert fired == [EVENT_SINGLE_PRESS, EVENT_SINGLE_RELEASE]
    # The release opens the window in which a second press counts as a double.
    assert timers.delays == [0.3]

    entity._handle_edges(_edges(PRESSED))  # noqa: SLF001

    assert fired == [
        EVENT_SINGLE_PRESS,
        EVENT_SINGLE_RELEASE,
        EVENT_SINGLE_PRESS,
        EVENT_DOUBLE_PRESS,
    ]


def test_second_press_after_the_window_is_a_plain_press(timers) -> None:
    entity, fired = _build(InputSettings(long_press_ms=500, double_click_ms=300))

    entity._handle_edges(_edges(PRESSED, RELEASED))  # noqa: SLF001
    timers.run(0.3)
    entity._handle_edges(_edges(PRESSED, RELEASED))  # noqa: SLF001

    assert EVENT_DOUBLE_PRESS not in fired


def test_double_click_window_stays_closed_when_disabled(timers) -> None:
    entity, fired = _build(InputSettings(long_press_ms=500, double_click_ms=0))

    entity._handle_edges(_edges(PRESSED, RELEASED))  # noqa: SLF001
    entity._handle_edges(_edges(PRESSED, RELEASED))  # noqa: SLF001

    assert EVENT_DOUBLE_PRESS not in fired
    assert timers.delays == []


def test_holding_the_second_press_reports_double_and_long(timers) -> None:
    """Pressing twice and holding is a double press followed by a hold."""

    entity, fired = _build(InputSettings(long_press_ms=500, double_click_ms=300))

    entity._handle_edges(_edges(PRESSED, RELEASED))  # noqa: SLF001
    fired.clear()

    entity._handle_edges(_edges(PRESSED))  # noqa: SLF001
    timers.run(0.5)
    entity._handle_edges(_edges(RELEASED))  # noqa: SLF001

    assert fired == [
        EVENT_SINGLE_PRESS,
        EVENT_DOUBLE_PRESS,
        EVENT_LONG_PRESS,
        EVENT_LONG_RELEASE,
    ]


def test_inverted_button_swaps_the_edges(timers) -> None:
    """A button wired as a normally closed contact presses on the rising edge."""

    entity, fired = _build(InputSettings(long_press_ms=500, double_click_ms=0), invert=True)

    entity._handle_edges(_edges(RELEASED))  # noqa: SLF001
    assert fired == [EVENT_SINGLE_PRESS]

    entity._handle_edges(_edges(PRESSED))  # noqa: SLF001
    assert fired == [EVENT_SINGLE_PRESS, EVENT_SINGLE_RELEASE]


def test_dm117_button_presses_on_the_high_level(timers) -> None:
    """A DM117 input reports a closed contact as a set bit, unlike the IM117."""

    api = SimpleNamespace(dm117_states={0x10: {0: 0}}, pcf_states={})
    entity = CasaITDM117ButtonEvent(api, ENTRY, 0x10, 0, 1, InputSettings(long_press_ms=500, double_click_ms=0))
    fired: list[str] = []
    entity._fire = fired.append  # noqa: SLF001

    # Slot 0 channel B, so edges for channel A must not reach this entity.
    entity._handle_edges({(0, 0): [True]})  # noqa: SLF001
    assert fired == []

    entity._handle_edges({(0, 1): [True]})  # noqa: SLF001
    entity._handle_edges({(0, 1): [False]})  # noqa: SLF001

    assert fired == [EVENT_SINGLE_PRESS, EVENT_SINGLE_RELEASE]
    assert entity.available is True


def test_repeat_starts_only_after_the_hold_threshold(timers) -> None:
    """A tap must never repeat; a hold keeps repeating until released."""

    entity, fired = _build(InputSettings(long_press_ms=500, double_click_ms=0, repeat_interval_ms=200), repeat=True)

    entity._handle_edges(_edges(PRESSED))  # noqa: SLF001
    assert timers.delays == [0.5]

    timers.run(0.5)
    assert fired == [EVENT_SINGLE_PRESS, EVENT_LONG_PRESS]

    timers.run(0.2)
    timers.run(0.2)
    assert fired == [EVENT_SINGLE_PRESS, EVENT_LONG_PRESS, EVENT_REPEAT, EVENT_REPEAT]

    entity._handle_edges(_edges(RELEASED))  # noqa: SLF001

    assert fired[-1] == EVENT_LONG_RELEASE
    # Releasing has to stop the repeats, not just the hold timer.
    assert timers.delays == []


def test_repeat_stays_off_unless_enabled(timers) -> None:
    entity, fired = _build(InputSettings(long_press_ms=500, double_click_ms=0, repeat_interval_ms=200))

    entity._handle_edges(_edges(PRESSED))  # noqa: SLF001
    timers.run(0.5)

    assert fired == [EVENT_SINGLE_PRESS, EVENT_LONG_PRESS]
    assert timers.delays == []
