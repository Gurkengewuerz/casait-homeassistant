"""One schedule for every 1-Wire device on a bridge.

1-Wire devices used to be polled by their entities, each on its platform's
interval, with every driver running its own time-based state machine so a
conversion could span several polls. That had the bus visited at uncoordinated
moments, readings that were up to a whole poll interval stale, and a separate
cache per driver.

Here one scheduler owns the timing instead:

- Every device, or group of devices, is a job with an interval and a due time.
- A job is a short sequence of bus transactions. Conversion times are slept
  out with the bus released, so the input poll never waits behind them.
- All DS18B20s on one bus share a job: one broadcast conversion serves them all.
- Results land in one cache and are pushed to the entities over the dispatcher.
- A device is only reported unavailable after several failed reads in a row,
  so a single disturbed transaction does not make entities flicker.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass, field
import logging
import time
from typing import TYPE_CHECKING, Any

from homeassistant.helpers.dispatcher import async_dispatcher_send

from .const import DEFAULT_OW_POLL_INTERVAL, OW_PROFILE_LED, OW_PROFILE_MULTISENSOR
from .multisensor import SAMPLE_INTERVAL
from .services.i2cClasses.ds18b20 import CONVERSION_TIME as DS18B20_CONVERSION_TIME
from .services.i2cClasses.ds2438 import CONVERSION_TIME as DS2438_CONVERSION_TIME, DS2438Reading

if TYPE_CHECKING:
    from .api import CasaITApi

_LOGGER = logging.getLogger(__name__)

# Failed reads in a row before a device's value is dropped and its entities
# become unavailable.
MAX_FAILURES = 3
# Longest the scheduler sleeps between checks, so a job added by a rescan does
# not wait for a long-interval job to come due first.
MAX_IDLE = 1.0
DS18B20_PROFILE = "ds18b20_temp"
DS2438_PROFILES = frozenset({"ds2438_hih4030_tept5600", "ds2438_hih5030_tept5600"})
DS2413_PROFILES = frozenset({"ds2413", "ds2413_in", "ds2413_out"})
FALLBACK_INTERVAL = 60


@dataclass
class _Job:
    """One recurring piece of 1-Wire work."""

    key: str
    interval: float
    run: Callable[[], Awaitable[None]]
    next_due: float = 0.0
    task: asyncio.Task | None = field(default=None, repr=False)


class CasaITOneWireScheduler:
    """Read every 1-Wire device of one bridge on its own interval."""

    def __init__(self, api: CasaITApi) -> None:
        """Initialize for one API instance."""

        self._api = api
        self._values: dict[str, Any] = {}
        self._failures: dict[str, int] = {}
        self._jobs: dict[str, _Job] = {}
        self._task: asyncio.Task | None = None
        self._wake = asyncio.Event()

    # ------------------------------------------------------------------
    # Entities
    # ------------------------------------------------------------------

    def signal(self, device_id: str) -> str:
        """Return the dispatcher signal announcing a new value of one device."""

        return f"{self._api.state_update_signal}_onewire_{device_id}"

    def value(self, device_id: str) -> Any:
        """Return the latest value of one device, or None while it is unavailable.

        The type depends on the device: a float for a DS18B20, a DS2438Reading,
        the (A, B) pin levels of a DS2413, or the LEDConfig of an LED controller.
        """

        return self._values.get(device_id)

    def set_value(self, device_id: str, value: Any) -> None:
        """Publish a value learned outside a scheduled read, typically from a write."""

        self._failures.pop(device_id, None)
        self._values[device_id] = value
        async_dispatcher_send(self._api.hass, self.signal(device_id))

    def request_refresh(self, device_id: str) -> None:
        """Read one device as soon as possible instead of at its next due time."""

        for job in self._jobs.values():
            if job.key == device_id or job.key.endswith(f"/{device_id}"):
                job.next_due = 0.0
        self._wake.set()

    @property
    def diagnostic_data(self) -> dict[str, Any]:
        """Return the schedule for diagnostics."""

        now = time.monotonic()
        return {
            job.key: {
                "interval_s": job.interval,
                "due_in_s": round(max(0.0, job.next_due - now), 1),
                "running": job.task is not None,
            }
            for job in self._jobs.values()
        }

    # ------------------------------------------------------------------
    # Schedule
    # ------------------------------------------------------------------

    def configure(self, profiles: dict[str, str], intervals: dict[str, int]) -> None:
        """Rebuild the job list from the devices the last scan found.

        ``profiles`` maps each device to its effective profile, ``intervals`` holds
        per-device overrides. Jobs that survive keep their due time, so a rescan
        does not make everything read at once.
        """

        jobs: dict[str, _Job] = {}
        ds18b20_by_bus: dict[int, list[str]] = defaultdict(list)

        for device_id, profile in profiles.items():
            interval = float(intervals.get(device_id) or DEFAULT_OW_POLL_INTERVAL.get(profile, FALLBACK_INTERVAL))
            if profile == DS18B20_PROFILE:
                bus = self._api.ow_devices.get(device_id, {}).get("bus_address")
                if isinstance(bus, int):
                    ds18b20_by_bus[bus].append(device_id)
                continue
            if profile in DS2438_PROFILES:
                job = _Job(f"ds2438/{device_id}", interval, self._ds2438_job(device_id))
            elif profile in DS2413_PROFILES:
                job = _Job(f"ds2413/{device_id}", interval, self._ds2413_job(device_id))
            elif profile == OW_PROFILE_LED:
                job = _Job(f"led/{device_id}", interval, self._led_job(device_id))
            elif profile == OW_PROFILE_MULTISENSOR:
                if self._api.multisensor.state(device_id) is None:
                    continue
                # Fixed: the VOC algorithm is built for this rate.
                job = _Job(
                    f"multisensor/{device_id}",
                    SAMPLE_INTERVAL,
                    self._multisensor_job(device_id),
                )
            else:
                continue
            jobs[job.key] = job

        for bus, device_ids in ds18b20_by_bus.items():
            # One broadcast conversion serves every sensor on the strand, so
            # the strand is read as often as its most demanding sensor asks.
            interval = min(
                float(intervals.get(device_id) or DEFAULT_OW_POLL_INTERVAL[DS18B20_PROFILE]) for device_id in device_ids
            )
            job = _Job(f"ds18b20@{bus:02x}", interval, self._ds18b20_job(sorted(device_ids)))
            jobs[job.key] = job

        now = time.monotonic()
        for index, (key, job) in enumerate(sorted(jobs.items())):
            if (previous := self._jobs.get(key)) is not None:
                job.next_due = previous.next_due
                job.task = previous.task
            else:
                # Spread first reads out a little instead of starting everything
                # in the same instant.
                job.next_due = now + 0.2 * index
        for key, job in self._jobs.items():
            if key not in jobs and job.task is not None:
                job.task.cancel()

        self._jobs = jobs
        present = set(profiles)
        for device_id in [device_id for device_id in self._values if device_id not in present]:
            self._values.pop(device_id)
            self._failures.pop(device_id, None)
        self._wake.set()

    def start(self) -> None:
        """Start the scheduler loop."""

        if self._task is None:
            self._task = self._api.hass.async_create_background_task(self._loop(), "casait_onewire")

    async def stop(self) -> None:
        """Stop the loop and every job still running."""

        tasks = [job.task for job in self._jobs.values() if job.task is not None]
        if self._task is not None:
            tasks.append(self._task)
        for task in tasks:
            task.cancel()
        for task in tasks:
            with suppress(asyncio.CancelledError):
                await task
        self._task = None
        for job in self._jobs.values():
            job.task = None

    async def _loop(self) -> None:
        while True:
            now = time.monotonic()
            for job in list(self._jobs.values()):
                if job.task is not None or job.next_due > now:
                    continue
                # Measured from the planned slot, not from now, so the rate holds;
                # but never catch up in a burst after a stall.
                job.next_due = max(job.next_due + job.interval, now + job.interval / 2)
                job.task = self._api.hass.async_create_background_task(self._run_job(job), f"casait_onewire_{job.key}")

            pending = [job.next_due for job in self._jobs.values() if job.task is None]
            delay = min([MAX_IDLE, *(due - now for due in pending)])
            self._wake.clear()
            with suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), timeout=max(0.01, delay))

    async def _run_job(self, job: _Job) -> None:
        try:
            await job.run()
        except asyncio.CancelledError:
            raise
        except Exception:
            _LOGGER.exception("Error running 1-Wire job %s", job.key)
        finally:
            job.task = None
            self._wake.set()

    # ------------------------------------------------------------------
    # Results
    # ------------------------------------------------------------------

    def _succeeded(self, device_id: str, value: Any) -> None:
        if self._failures.pop(device_id, 0) >= MAX_FAILURES:
            _LOGGER.info("1-Wire device %s answers again", device_id)
        previous = self._values.get(device_id)
        self._values[device_id] = value
        if previous != value:
            async_dispatcher_send(self._api.hass, self.signal(device_id))

    def _failed(self, device_id: str, reason: str) -> None:
        failures = self._failures[device_id] = self._failures.get(device_id, 0) + 1
        _LOGGER.debug("Reading 1-Wire device %s failed (%s), %s in a row", device_id, reason, failures)
        if failures == MAX_FAILURES:
            _LOGGER.warning("1-Wire device %s failed %s reads in a row; marking it unavailable", device_id, failures)
            if self._values.pop(device_id, None) is not None:
                async_dispatcher_send(self._api.hass, self.signal(device_id))

    async def _read(self, device_id: str, func: Callable[[Any], Any]) -> Any:
        """Run one transaction; transport errors count as a None result."""

        try:
            return await self._api.async_onewire_job(device_id, func)
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("1-Wire transaction on %s failed: %s", device_id, err)
            return None

    # ------------------------------------------------------------------
    # Jobs
    # ------------------------------------------------------------------

    def _ds18b20_job(self, device_ids: list[str]) -> Callable[[], Awaitable[None]]:
        async def run() -> None:
            if not await self._read(device_ids[0], lambda bus: bus.ds18b20.start_conversion()):
                for device_id in device_ids:
                    self._failed(device_id, "conversion not started")
                return
            await asyncio.sleep(DS18B20_CONVERSION_TIME)
            for device_id in device_ids:
                temperature = await self._read(device_id, lambda bus, rom=device_id: bus.ds18b20.read_temperature(rom))
                if temperature is None:
                    self._failed(device_id, "no temperature")
                else:
                    self._succeeded(device_id, round(temperature, 2))

        return run

    def _ds2438_job(self, device_id: str) -> Callable[[], Awaitable[None]]:
        async def run() -> None:
            if not await self._read(device_id, lambda bus: bus.ds2438.start(device_id, vdd=True, temperature=True)):
                self._failed(device_id, "VDD conversion not started")
                return
            await asyncio.sleep(DS2438_CONVERSION_TIME)
            supply = await self._read(device_id, lambda bus: bus.ds2438.read_page(device_id))
            if supply is None or not supply.status & 0x08:
                self._failed(device_id, "no VDD reading")
                return

            if not await self._read(device_id, lambda bus: bus.ds2438.start(device_id, vdd=False)):
                self._failed(device_id, "VAD conversion not started")
                return
            await asyncio.sleep(DS2438_CONVERSION_TIME)
            analog = await self._read(device_id, lambda bus: bus.ds2438.read_page(device_id))
            if analog is None:
                self._failed(device_id, "no VAD reading")
                return

            self._succeeded(
                device_id,
                DS2438Reading(
                    vdd=supply.voltage,
                    vad=analog.voltage,
                    vse=analog.current_voltage,
                    temperature=round(supply.temperature, 2),
                ),
            )

        return run

    def _ds2413_job(self, device_id: str) -> Callable[[], Awaitable[None]]:
        async def run() -> None:
            pins = await self._read(device_id, lambda bus: bus.ds2413.read_ports(device_id))
            if pins is None:
                self._failed(device_id, "no pin levels")
            else:
                self._succeeded(device_id, pins)

        return run

    def _led_job(self, device_id: str) -> Callable[[], Awaitable[None]]:
        async def run() -> None:
            config = await self._read(device_id, lambda bus: bus.read_led_config(device_id, use_cache=False))
            if config is None:
                self._failed(device_id, "no configuration")
            else:
                self._succeeded(device_id, config)

        return run

    def _multisensor_job(self, device_id: str) -> Callable[[], Awaitable[None]]:
        async def run() -> None:
            # The manager tracks the health of each chip itself.
            await self._api.multisensor.async_sample(device_id)

        return run
