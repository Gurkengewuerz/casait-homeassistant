"""PCF8574 I2C I/O expander implementation for CasaIT Smart Home integration."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import logging
import time

from .edge_tracker import EdgeTracker

_LOGGER = logging.getLogger(__name__)

# Reads between forced re-arming of the quasi-bidirectional inputs. Writes and
# errors re-arm immediately; this is only a safety net against a latch that
# drifted low without anyone noticing.
SET_HIGH_REFRESH_READS = 50


@dataclass
class PCF8574Reading:
    """Result of a single port read.

    ``edges`` maps a hardware port to the raw levels it transitioned to since the
    previous read, in order. The values are chip levels, not logical states -
    callers apply their own active-low interpretation.
    """

    port_states: list[int]
    value: int
    edges: dict[int, list[bool]] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        """Return True when the read produced usable data."""

        return bool(self.port_states)


class PCF8574:
    """PCF8574 I2C I/O expander implementation."""

    def __init__(self, bus, address: int, debounce_time: int = 40) -> None:
        """Initialize PCF8574 instance."""
        self.bus = bus
        self.address = address
        self.last_value = -1
        self._edges = EdgeTracker[int](debounce_time)
        self.port_states = [0] * 8
        self._needs_set_high = True
        self._reads_since_set_high = 0
        # Give each module its own refresh threshold. When a whole cycle is read in
        # one batch, modules re-arming on the same cycle stack their settle delays
        # into a single frame; differing thresholds keep them drifting apart instead
        # of lining up again after every shared re-arm.
        self._refresh_reads = SET_HIGH_REFRESH_READS + (address % 8)

    @property
    def debounce_time(self) -> int:
        """Return the per-bit debounce window in milliseconds."""

        return self._edges.debounce_time

    @debounce_time.setter
    def debounce_time(self, value: int) -> None:
        """Set the per-bit debounce window, for example when the bridge takes over."""

        self._edges.debounce_time = value

    def invalidate(self) -> None:
        """Force the next read to re-arm the inputs before sampling."""

        self._needs_set_high = True

    def needs_rearm(self, set_high: bool = True) -> bool:
        """Return True when the next sample has to re-arm the latch first.

        Quasi-bidirectional ports only need re-arming after a write, after an error,
        or periodically as a safety net. Doing it on every read costs a round trip
        plus a 5 ms settle for no gain.
        """

        return bool(set_high) and (self._needs_set_high or self._reads_since_set_high >= self._refresh_reads)

    def note_rearmed(self) -> None:
        """Record that the latch was just re-armed by the caller."""

        self._needs_set_high = False
        self._reads_since_set_high = 0

    def note_read_error(self) -> PCF8574Reading:
        """Record a failed sample and return the empty reading that signals it."""

        self._needs_set_high = True
        return PCF8574Reading([], -1)

    async def read_ports(self, set_high: bool = True) -> PCF8574Reading:
        """Read all ports, debounce per bit and report the observed edges.

        Convenience wrapper for single-device access. The poll loop instead batches
        the bus traffic for every module into one frame and calls ``apply_reading``.
        """
        try:
            if self.needs_rearm(set_high):
                await self.bus.write_byte(self.address, 0xFF)
                await asyncio.sleep(0.005)  # 5ms delay for I2C bus to settle
                self.note_rearmed()

            value = await self.bus.read_byte(self.address)
        except OSError:
            return self.note_read_error()

        return self.apply_reading(value)

    def apply_reading(self, value: int, timestamp_ms: float | None = None) -> PCF8574Reading:
        """Debounce a sampled port byte per bit and report the observed edges.

        ``timestamp_ms`` lets a caller that sampled several modules in one batch pass
        a single instant for all of them, so their debounce windows do not drift
        apart by the time it takes to decode the frame.
        """

        self._reads_since_set_high += 1
        curr_time = time.monotonic() * 1000 if timestamp_ms is None else timestamp_ms
        first_read = self.last_value < 0

        edges = self._edges.apply({bit: bool(value & (1 << bit)) for bit in range(8)}, curr_time)
        self.port_states = [int(self._edges.level(bit) or False) for bit in range(8)]
        self.last_value = sum(state << bit for bit, state in enumerate(self.port_states))

        if first_read:
            # The tracker already adopted the levels without reporting edges.
            return PCF8574Reading(list(self.port_states), value)

        return PCF8574Reading(list(self.port_states), self.last_value, edges)

    async def write_port(self, port: int, state: int, verify: bool = True) -> bool:
        """Write to specific port with optional verification."""
        if not 0 <= port <= 7:
            raise ValueError("Port must be 0-7")

        try:
            # Ensure we have a valid last_value before doing bit operations.
            # If last_value is invalid (-1 or out of range), read current state first.
            if not 0 <= self.last_value <= 255:
                _LOGGER.debug(
                    "PCF8574 0x%02X: last_value invalid (%s), reading current state",
                    self.address,
                    self.last_value,
                )
                await self.bus.write_byte(self.address, 0xFF)
                await asyncio.sleep(0.002)
                self.last_value = await self.bus.read_byte(self.address)
                _LOGGER.debug(
                    "PCF8574 0x%02X: read current state = 0x%02X",
                    self.address,
                    self.last_value,
                )

            # Calculate new value with explicit masking to ensure valid byte
            current = self.last_value & 0xFF
            if state:
                new_value = current | (1 << port)
            else:
                new_value = current & ~(1 << port)
            new_value &= 0xFF  # Ensure valid byte range

            # Write the new value
            await self.bus.write_byte(self.address, new_value)
            # The latch no longer holds the all-high pattern the inputs are sampled
            # against, so the next read has to re-arm it.
            self._needs_set_high = True

            # When turning ON (state=0, active low), relay energizes causing
            # electrical noise. Give more settling time before verification.
            if verify:
                settle_time = 0.002
                await asyncio.sleep(settle_time)
                read_value = await self.bus.read_byte(self.address)
                if read_value != new_value:
                    _LOGGER.warning(
                        "PCF8574 write verification failed: expected 0x%02X, got 0x%02X",
                        new_value,
                        read_value,
                    )
                    return False

            self.last_value = new_value
            self.port_states[port] = state
            # Own writes are not input edges; hand the new level to the tracker so
            # the next read does not report the change we just made.
            self._edges.adopt(port, bool(state))

        except OSError:
            self._needs_set_high = True
            _LOGGER.exception("PCF8574 write error at 0x%02X port %s", self.address, port)
            return False
        return True
