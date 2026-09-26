"""Per-device link health for the bus overview in the diagnostics."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

# Weight of the newest sample in the smoothed latency. Low enough that one slow
# frame does not dominate, high enough that a degrading link shows within a
# minute of fast polling.
LATENCY_SMOOTHING = 0.1


def _iso(timestamp: float | None) -> str | None:
    return None if timestamp is None else datetime.fromtimestamp(timestamp, UTC).isoformat(timespec="seconds")


@dataclass
class LinkHealth:
    """How reliably and how fast one module or chip answers."""

    reads: int = 0
    errors: int = 0
    consecutive_errors: int = 0
    last_ok: float | None = None
    last_error_at: float | None = None
    last_error: str | None = None
    last_latency_ms: float | None = None
    average_latency_ms: float | None = None

    def success(self, now: float, latency_s: float | None = None) -> None:
        """Record a read that answered, and how long its round trip took."""

        self.reads += 1
        self.consecutive_errors = 0
        self.last_ok = now
        if latency_s is None:
            return
        latency_ms = latency_s * 1000
        self.last_latency_ms = round(latency_ms, 2)
        self.average_latency_ms = round(
            latency_ms
            if self.average_latency_ms is None
            else self.average_latency_ms + LATENCY_SMOOTHING * (latency_ms - self.average_latency_ms),
            2,
        )

    def failure(self, now: float, reason: str | None = None) -> None:
        """Record a read that failed."""

        self.reads += 1
        self.errors += 1
        self.consecutive_errors += 1
        self.last_error_at = now
        if reason:
            self.last_error = reason

    def as_dict(self) -> dict[str, Any]:
        """Return the counters in a JSON-safe form."""

        return {
            "reads": self.reads,
            "errors": self.errors,
            "error_rate": round(self.errors / self.reads, 4) if self.reads else None,
            "consecutive_errors": self.consecutive_errors,
            "last_ok": _iso(self.last_ok),
            "last_error_at": _iso(self.last_error_at),
            "last_error": self.last_error,
            "last_latency_ms": self.last_latency_ms,
            "average_latency_ms": self.average_latency_ms,
        }
