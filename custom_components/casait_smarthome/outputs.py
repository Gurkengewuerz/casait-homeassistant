"""Coalesced PCF8574 output writes for casaIT output modules.

Writes that arrive within a short window go out as one batch frame: every
module gets its new port byte back to back on the bridge, then each is read
back once to verify it. Shutters driven by one group call or one automation
therefore start and stop together instead of one network round trip apart.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
import logging
from typing import TYPE_CHECKING

from .services.smbus_proxy import I2CBatch

if TYPE_CHECKING:
    from .api import CasaITApi
    from .services.i2cClasses.pcf8574 import PCF8574

_LOGGER = logging.getLogger(__name__)

OUTPUT_COALESCE_WINDOW = 0.025
OUTPUT_SETTLE_MS = 2


@dataclass
class _OutputRequest:
    """Port levels one caller asked for, and the future that reports the outcome."""

    changes: dict[int, int]
    future: asyncio.Future[bool]


class CasaITOutputWriter:
    """Collect output writes for a moment and send them as one verified batch."""

    def __init__(self, api: CasaITApi) -> None:
        """Bind the writer to the API that owns the bus and the drivers."""

        self._api = api
        self._pending: dict[int, list[_OutputRequest]] = {}
        self._flush_handle: asyncio.TimerHandle | None = None
        self._flushes: set[asyncio.Task[None]] = set()

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
                        address: _compose(device.last_value, pending[address]) for address, device in devices.items()
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


def _compose(current: int, requests: list[_OutputRequest]) -> int:
    """Apply the requests to a port byte in the order they arrived."""

    value = current & 0xFF
    for request in requests:
        for port, state in request.changes.items():
            value = value | (1 << port) if state else value & ~(1 << port)
    return value & 0xFF
