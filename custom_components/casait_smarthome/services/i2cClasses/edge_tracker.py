"""Per-key debouncing and edge extraction shared by the input drivers."""

from __future__ import annotations

from collections.abc import Hashable, Mapping
import time


class EdgeTracker:
    """Remember the level of every tracked input and report its transitions.

    Debouncing is leading edge: a change is adopted the moment it is seen, and
    that key is then ignored for the debounce window. The trailing-edge variant
    would defer the change instead, which loses a button press that is already
    released again by the time of the next sample.

    Keys are opaque - a PCF8574 tracks bit positions, a DM117 tracks
    (slot, channel) pairs.
    """

    def __init__(self, debounce_time: int = 0) -> None:
        """Initialize an empty tracker debouncing for the given milliseconds."""

        self.debounce_time = debounce_time
        self._levels: dict[Hashable, bool] = {}
        self._changed_at: dict[Hashable, float] = {}

    @property
    def levels(self) -> dict[Hashable, bool]:
        """Return the debounced level of every key seen so far."""

        return dict(self._levels)

    def level(self, key: Hashable) -> bool | None:
        """Return the debounced level of one key, or None when never sampled."""

        return self._levels.get(key)

    def reset(self) -> None:
        """Forget every level, so the next sample is adopted without edges."""

        self._levels.clear()
        self._changed_at.clear()

    def adopt(self, key: Hashable, level: bool, timestamp_ms: float | None = None) -> None:
        """Take on a level this side caused, so the next sample is not an edge.

        Used after writing an output: the change is ours, not an input event.
        """

        self._levels[key] = level
        self._changed_at[key] = time.monotonic() * 1000 if timestamp_ms is None else timestamp_ms

    def apply(
        self,
        sample: Mapping[Hashable, bool],
        timestamp_ms: float | None = None,
    ) -> dict[Hashable, list[bool]]:
        """Adopt one sample and return the levels each key transitioned to.

        ``timestamp_ms`` lets a caller that sampled several modules in one batch
        pass a single instant for all of them, so their debounce windows do not
        drift apart by the time it takes to decode the frame.
        """

        now = time.monotonic() * 1000 if timestamp_ms is None else timestamp_ms
        edges: dict[Hashable, list[bool]] = {}

        for key, level in sample.items():
            previous = self._levels.get(key)
            if previous is None:
                # First sample of this key: adopt the level without reporting it.
                self._levels[key] = level
                self._changed_at[key] = now
                continue
            if level == previous:
                continue
            if self.debounce_time > 0 and now - self._changed_at.get(key, 0.0) < self.debounce_time:
                continue
            self._levels[key] = level
            self._changed_at[key] = now
            edges.setdefault(key, []).append(level)

        return edges
