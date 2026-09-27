"""Coalesced PCF8574 output writes for casaIT output modules.

Writes that arrive within a short window go out as one batch frame: every
module gets its new port byte back to back on the bridge, then each is read
back once to verify it. Shutters driven by one group call or one automation
therefore start and stop together instead of one network round trip apart.

A caller can also hand the bridge the moment to switch bits back. The writer mirrors those timers, so a byte it
composes after one ran out does not switch the bits on again.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
import logging
import time
from typing import TYPE_CHECKING, Any

from .services.smbus_proxy import MAX_TIMED_OUTPUT_MS, I2CBatch

if TYPE_CHECKING:
    from .api import CasaITApi
    from .services.i2cClasses.pcf8574 import PCF8574

_LOGGER = logging.getLogger(__name__)

OUTPUT_COALESCE_WINDOW = 0.025
OUTPUT_SETTLE_MS = 2
# A timer this close to running out counts as run out: a write composed now reaches
# the bridge after it switched the bits back, and must not switch them on again.
TIMER_MARGIN = 0.1
# Readings this soon after a timer ran out may still predate our own stop.
TIMER_SETTLE = 2.0


@dataclass
class _OutputRequest:
    """Port levels one caller asked for, and the future that reports the outcome."""

    changes: dict[int, int]
    future: asyncio.Future[bool]


@dataclass
class _OutputTimer:
    """Bits the bridge holds at ``value`` and restores to ``revert`` at ``deadline``."""

    mask: int
    value: int
    revert: int
    deadline: float


class CasaITOutputWriter:
    """Collect output writes for a moment and send them as one verified batch."""

    def __init__(self, api: CasaITApi) -> None:
        """Bind the writer to the API that owns the bus and the drivers."""

        self._api = api
        self._pending: dict[int, list[_OutputRequest]] = {}
        self._flush_handle: asyncio.TimerHandle | None = None
        self._flushes: set[asyncio.Task[None]] = set()
        self._timers: dict[int, list[_OutputTimer]] = {}

    async def async_write(self, address: int, changes: Mapping[int, int]) -> bool:
        """Queue port levels for one module and wait until they are on the bus.

        Levels are raw chip levels per hardware port. Returns False when the
        module is gone, the write failed or the read-back did not match.
        """

        loop = asyncio.get_running_loop()
        request = _OutputRequest(dict(changes), loop.create_future())
        self._pending.setdefault(address, []).append(request)
        if self._flush_handle is None:
            self._flush_handle = loop.call_later(OUTPUT_COALESCE_WINDOW, self._start_flush)
        return await request.future

    async def async_arm_timer(self, address: int, mask: int, value: int, revert: int, seconds: float) -> bool:
        """Set bits of a module and have the bridge restore them after ``seconds``.

        Returns False, without touching anything, when the bridge refused the
        timer; the caller stops the outputs itself either way.
        """

        info = self._api.bridge_info
        device = self._api.im117_om117.get(address)
        duration_ms = round(seconds * 1000)
        if info is None or device is None:
            return False
        if not 0 < duration_ms <= MAX_TIMED_OUTPUT_MS:
            return False
        try:
            async with self._api.write_access():
                written = await self._api.bus.timed_output(address, mask, value, revert, duration_ms)
        except OSError as exc:
            _LOGGER.debug("Bridge did not take the output timer for 0x%02X: %s", address, exc)
            return False

        timers = [timer for timer in self._timers.get(address, []) if timer.mask & ~mask]
        for timer in timers:
            timer.mask &= ~mask
        timers.append(_OutputTimer(mask, value & mask, revert & mask, time.monotonic() + seconds))
        self._timers[address] = timers
        device.note_written(written)
        self._api.publish_pcf_write(address)
        return True

    def timer_bits(self, address: int) -> int:
        """Return the bits of a module a bridge timer holds or just switched back.

        Their level on the chip may differ from the last byte written without
        anything being wrong, so a reading is not compared on them.
        """

        now = time.monotonic()
        bits = 0
        for timer in self._timers.get(address, []):
            if now < timer.deadline + TIMER_SETTLE:
                bits |= timer.mask
        return bits

    def forget_timers(self) -> None:
        """Drop all timers, which the bridge releases when the connection goes."""

        self._timers.clear()

    def diagnostics(self) -> dict[str, Any]:
        """Return the running timers for the diagnostics download."""

        now = time.monotonic()
        return {
            f"0x{address:02X}": [
                {"mask": f"0x{timer.mask:02X}", "remaining_s": round(timer.deadline - now, 2)} for timer in timers
            ]
            for address, timers in self._timers.items()
            if timers
        }

    def _compose(self, address: int, current: int, requests: list[_OutputRequest]) -> int:
        """Apply the requests to a port byte in the order they arrived.

        Bits of a timer that ran out start from the level the bridge restored, and
        a request that sets a timer's bits differently takes them back from it, as
        the bridge does.
        """

        now = time.monotonic()
        value = current & 0xFF
        running: list[_OutputTimer] = []
        for timer in self._timers.get(address, []):
            if now >= timer.deadline - TIMER_MARGIN:
                value = (value & ~timer.mask) | timer.revert
            else:
                running.append(timer)
        for request in requests:
            for port, state in request.changes.items():
                value = value | (1 << port) if state else value & ~(1 << port)
        for timer in running:
            timer.mask &= ~((value ^ timer.value) & timer.mask)
        self._timers[address] = [timer for timer in running if timer.mask]
        return value & 0xFF

    async def async_shutdown(self) -> None:
        """Send whatever is still queued and wait for writes in flight."""

        if self._flush_handle is not None:
            self._flush_handle.cancel()
            self._start_flush()
        if self._flushes:
            await asyncio.gather(*self._flushes, return_exceptions=True)

    def _start_flush(self) -> None:
        """Hand the collected requests to a flush task."""

        self._flush_handle = None
        pending, self._pending = self._pending, {}
        if not pending:
            return
        task = self._api.hass.async_create_background_task(self._flush(pending), "casait_output_flush")
        self._flushes.add(task)
        task.add_done_callback(self._flushes.discard)

    async def _flush(self, pending: dict[int, list[_OutputRequest]]) -> None:
        """Write every requested module in one frame and resolve the callers."""

        devices: dict[int, PCF8574] = {}
        outcome: dict[int, bool] = {}
        for address in pending:
            if (device := self._api.im117_om117.get(address)) is None:
                outcome[address] = False
            else:
                devices[address] = device

        try:
            if devices:
                async with self._api.write_access():
                    for address in await self._load_unknown(devices):
                        outcome[address] = False
                        devices.pop(address)
                    values = {
                        address: self._compose(address, device.last_value, pending[address])
                        for address, device in devices.items()
                    }
                    written = await self._write_values(values) if values else {}
                for address, ok in written.items():
                    outcome[address] = ok
                    if ok:
                        devices[address].note_written(values[address])
                        self._api.publish_pcf_write(address)
                    else:
                        devices[address].invalidate()
        finally:
            for address, requests in pending.items():
                for request in requests:
                    if not request.future.done():
                        request.future.set_result(outcome.get(address, False))

    async def _load_unknown(self, devices: Mapping[int, PCF8574]) -> list[int]:
        """Read the current byte of modules without a cached one; return those that failed."""

        failed: list[int] = []
        for address, device in devices.items():
            if 0 <= device.last_value <= 0xFF:
                continue
            try:
                device.last_value = await self._api.bus.read_byte(address)
            except OSError as exc:
                _LOGGER.warning("Could not read OM117 0x%02X before writing: %s", address, exc)
                failed.append(address)
        return failed

    async def _write_values(self, values: dict[int, int]) -> dict[int, bool]:
        """Write the port bytes back to back, then verify each; split on failure."""

        batch = I2CBatch()
        for address, value in values.items():
            batch.write_byte(address, value)
        batch.delay(OUTPUT_SETTLE_MS)
        for address in values:
            batch.read_byte(address)

        try:
            readback = await self._api.bus.execute_batch(batch)
        except OSError as exc:
            if len(values) == 1:
                address = next(iter(values))
                _LOGGER.warning("Output write to 0x%02X failed: %s", address, exc)
                return {address: False}
            # One absent module fails the whole frame; the others still deserve their write.
            results: dict[int, bool] = {}
            for address, value in values.items():
                results |= await self._write_values({address: value})
            return results

        results = {}
        for (address, value), read in zip(values.items(), readback, strict=True):
            results[address] = read == value
            if read != value:
                _LOGGER.warning(
                    "Output write verification failed on 0x%02X: expected 0x%02X, got 0x%02X", address, value, read
                )
        return results
