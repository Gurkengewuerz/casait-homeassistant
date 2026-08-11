"""Contract tests for the shared input debouncing and edge extraction."""

import pytest

from custom_components.casait_smarthome.services.i2cClasses.edge_tracker import EdgeTracker


@pytest.mark.unit
def test_first_sample_is_adopted_without_edges() -> None:
    tracker = EdgeTracker()

    assert tracker.apply({"a": True, "b": False}, timestamp_ms=0.0) == {}
    assert tracker.levels == {"a": True, "b": False}


@pytest.mark.unit
def test_a_change_is_reported_immediately() -> None:
    tracker = EdgeTracker(debounce_time=40)
    tracker.apply({"a": True}, timestamp_ms=0.0)

    assert tracker.apply({"a": False}, timestamp_ms=50.0) == {"a": [False]}
    assert tracker.level("a") is False


@pytest.mark.unit
def test_the_first_sample_opens_the_window() -> None:
    """Adopting a level starts its debounce window, as the driver always did."""

    tracker = EdgeTracker(debounce_time=40)
    tracker.apply({"a": True}, timestamp_ms=0.0)

    assert tracker.apply({"a": False}, timestamp_ms=10.0) == {}


@pytest.mark.unit
def test_a_bounce_inside_the_window_is_suppressed() -> None:
    tracker = EdgeTracker(debounce_time=40)
    tracker.apply({"a": True}, timestamp_ms=0.0)
    tracker.apply({"a": False}, timestamp_ms=100.0)

    assert tracker.apply({"a": True}, timestamp_ms=120.0) == {}
    assert tracker.apply({"a": True}, timestamp_ms=141.0) == {"a": [True]}


@pytest.mark.unit
def test_keys_debounce_independently() -> None:
    tracker = EdgeTracker(debounce_time=40)
    tracker.apply({"a": True, "b": True}, timestamp_ms=0.0)
    tracker.apply({"a": False, "b": True}, timestamp_ms=100.0)

    # "a" is still inside its window while "b" has not moved at all.
    assert tracker.apply({"a": True, "b": False}, timestamp_ms=110.0) == {"b": [False]}


@pytest.mark.unit
def test_zero_debounce_reports_every_change() -> None:
    tracker = EdgeTracker()
    tracker.apply({"a": True}, timestamp_ms=0.0)

    assert tracker.apply({"a": False}, timestamp_ms=0.0) == {"a": [False]}
    assert tracker.apply({"a": True}, timestamp_ms=0.0) == {"a": [True]}


@pytest.mark.unit
def test_adopted_levels_are_not_edges() -> None:
    """A level this side wrote must not come back as an input transition."""

    tracker = EdgeTracker()
    tracker.apply({"a": False}, timestamp_ms=0.0)
    tracker.adopt("a", True, timestamp_ms=10.0)

    assert tracker.apply({"a": True}, timestamp_ms=20.0) == {}


@pytest.mark.unit
def test_reset_forgets_every_level() -> None:
    tracker = EdgeTracker()
    tracker.apply({"a": True}, timestamp_ms=0.0)
    tracker.reset()

    assert tracker.levels == {}
    assert tracker.apply({"a": False}, timestamp_ms=10.0) == {}
