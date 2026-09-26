"""Sampling and maintenance of casaIT Multisensor boards.

A Multisensor is a DS28E17 1-Wire to I2C bridge with any subset of SHT41
(temperature, humidity), SGP40 (VOC), STCC4 (CO2) and VEML7700 (light) behind
it. Unlike the other 1-Wire chips it is not polled by its entities:

- The VOC index algorithm learns a baseline and has to be fed at a fixed rate,
  which entity polling does not guarantee.
- The chips feed each other. The SHT41 compensates the SGP40 and the STCC4, so
  one sample has to run them in order.

So one background task samples every board at SAMPLE_INTERVAL and pushes the
result to the entities over the dispatcher.

Every bus access is one short DS28E17 transaction taken through the API's
background lane. The sensors' measurement times are slept out with the bus
released, so an input edge never waits behind a 30 ms VOC measurement.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import suppress
import logging
import time
from typing import TYPE_CHECKING, Any

from homeassistant.helpers import entity_registry as er, issue_registry as ir
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.storage import Store

from .const import DEFAULT_CO2_CALIBRATION_PPM, DOMAIN, OW_PROFILE_LED, OW_PROFILE_MULTISENSOR
from .services.i2cClasses.ds28e17 import DS28E17Error
from .services.i2cClasses.gas_index import VocGasIndexAlgorithm
from .services.i2cClasses.multisensor import (
    CHIP_SGP40,
    CHIP_SHT41,
    CHIP_STCC4,
    CHIP_VEML7700,
    CHIPS,
    SGP40_MEASURE_TIME,
    SHT41_MEASURE_TIME,
    STCC4_CMD_CONDITIONING,
    STCC4_CMD_FACTORY_RESET,
    STCC4_CMD_FORCED_RECALIBRATION,
    STCC4_CMD_SELF_TEST,
    STCC4_CONDITIONING_TIME,
    STCC4_FACTORY_RESET_TIME,
    STCC4_FRC_FAILED,
    STCC4_FRC_TIME,
    STCC4_SELF_TEST_TIME,
    STCC4_STOP_TIME,
    Multisensor,
    MultisensorComponents,
    MultisensorReading,
    MultisensorState,
    to_int16,
)

if TYPE_CHECKING:
    from .api import CasaITApi

_LOGGER = logging.getLogger(__name__)

# Seconds between two samples of a board. Fixed rather than configurable: the
# VOC index algorithm is built for this rate and Sensirion validates 10 s as its
# low-power mode, which keeps the 1-Wire bus mostly free for everything else.
SAMPLE_INTERVAL = 10.0
# A forced recalibration is only accurate once the STCC4 has been measuring
# continuously for this long.
STCC4_FRC_WARMUP = 180.0
# Sensirion only recommends restoring learned VOC states after short outages.
VOC_STATE_MAX_AGE = 600.0
# Samples in a row a chip may fail before it is reported as missing: one minute.
# A single failed read is noise on a long 1-Wire line, not a missing sensor.
CHIP_MISSING_SAMPLES = 6
STORE_VERSION = 1

# The entities each chip provides, by description key. Forgetting a chip removes
# exactly these.
CHIP_ENTITY_KEYS: dict[str, tuple[str, ...]] = {
    CHIP_SHT41: ("temperature", "humidity"),
    CHIP_SGP40: ("voc_index", "voc_raw"),
    CHIP_STCC4: (
        "co2",
        "co2_calibration_correction",
        "co2_calibration_target",
        "co2_calibrate",
        "co2_self_test",
        "co2_self_test_result",
        "co2_conditioning",
        "co2_factory_reset",
    ),
    CHIP_VEML7700: ("illuminance",),
}
CHIP_NAMES = {CHIP_SHT41: "SHT41", CHIP_SGP40: "SGP40", CHIP_STCC4: "STCC4", CHIP_VEML7700: "VEML7700"}


class MultisensorCommandError(Exception):
    """A maintenance command could not be carried out."""

    def __init__(self, reason: str) -> None:
        """Initialize with a translation key describing the failure."""

        super().__init__(reason)
        self.reason = reason


class CasaITMultisensorManager:
    """Own the state, sampling and maintenance of every Multisensor on one bridge."""

    def __init__(self, api: CasaITApi) -> None:
        """Initialize the manager for one API instance."""

        self._api = api
        self._states: dict[str, MultisensorState] = {}
        # Held for a whole sample or maintenance command. A 24 s conditioning
        # run must not have samples squeezed in between its steps.
        self._locks: dict[str, asyncio.Lock] = {}
        self._failing: set[str] = set()
        self._stcc4_started: dict[str, float] = {}
        # Reference concentration per board, set by its number entity.
        self._calibration_targets: dict[str, int] = {}
        self._task: asyncio.Task | None = None
        # Chips each board has ever been seen with. A chip that stops answering
        # keeps its entities and raises a repair issue instead of silently
        # disappearing on the next restart; only the repair flow forgets it.
        self._store: Store[dict[str, dict[str, Any]]] = Store(
            api.hass, STORE_VERSION, f"{DOMAIN}.{api.entry_id}.multisensor"
        )
        self._known: dict[str, MultisensorComponents] | None = None
        self._chip_failures: dict[tuple[str, str], int] = {}

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    async def async_detect(self, device_id: str) -> tuple[str, MultisensorComponents | None] | None:
        """Work out what firmware or chips sit behind one DS28E17.

        Returns the profile plus, for a Multisensor, its fitted chips; None when
        the bridge did not answer and nothing can be said yet.
        """

        try:
            if await self._job(device_id, lambda ms: ms.is_led_controller(device_id)):
                return OW_PROFILE_LED, None
            components = await self._job(device_id, lambda ms: ms.detect(device_id))
        except DS28E17Error as err:
            _LOGGER.warning("Could not identify DS28E17 %s: %s", device_id, err)
            return None

        if not components.any:
            _LOGGER.warning("DS28E17 %s answers, but neither an LED controller nor a sensor does", device_id)
            return None

        _LOGGER.info("DS28E17 %s is a Multisensor with %s", device_id, ", ".join(components.as_list()))
        return OW_PROFILE_MULTISENSOR, components

    async def async_load(self) -> None:
        """Load the chips every board was seen with before."""

        if self._known is not None:
            return
        stored = await self._store.async_load() or {}
        self._known = {
            device_id: MultisensorComponents.from_dict(data)
            for device_id, data in stored.items()
            if isinstance(data, dict)
        }

    def known_components(self, device_id: str) -> MultisensorComponents | None:
        """Return the chips a board was seen with before, if it ever was."""

        return (self._known or {}).get(device_id)

    async def async_remember(self, device_id: str, found: MultisensorComponents) -> MultisensorComponents:
        """Merge freshly detected chips with the ones seen before and store them.

        Returns the merged set: a chip that did not answer this time is kept, so
        its entities stay and the sampler notices it and raises the issue.
        """

        await self.async_load()
        assert self._known is not None
        previous = self._known.get(device_id)
        merged = found if previous is None else previous.union(found)
        if merged != previous:
            self._known[device_id] = merged
            await self._async_save()
        return merged

    async def _async_save(self) -> None:
        assert self._known is not None
        await self._store.async_save({device_id: parts.to_dict() for device_id, parts in self._known.items()})

    def register(self, device_id: str, components: MultisensorComponents) -> None:
        """Start tracking one board, or update the chips it carries."""

        state = self._states.get(device_id)
        if state is not None and state.components == components:
            return
        self._states[device_id] = MultisensorState(
            components=components,
            voc=VocGasIndexAlgorithm(SAMPLE_INTERVAL),
            # The STCC4 probe stopped continuous measurement.
            stcc4_ready_at=time.monotonic() + STCC4_STOP_TIME,
        )
        self._locks.setdefault(device_id, asyncio.Lock())

    def unregister_missing(self, present: set[str]) -> None:
        """Forget boards that are no longer on the bus."""

        for device_id in list(self._states):
            if device_id not in present:
                self._states.pop(device_id)
                self._stcc4_started.pop(device_id, None)

    # ------------------------------------------------------------------
    # Accessors for entities
    # ------------------------------------------------------------------

    def state(self, device_id: str) -> MultisensorState | None:
        """Return everything known about one board."""

        return self._states.get(device_id)

    def components(self, device_id: str) -> MultisensorComponents | None:
        """Return the chips fitted to one board."""

        state = self._states.get(device_id)
        return state.components if state else None

    def reading(self, device_id: str) -> MultisensorReading | None:
        """Return the latest reading of one board."""

        state = self._states.get(device_id)
        return state.reading if state else None

    def maintenance(self, device_id: str) -> dict[str, Any]:
        """Return the results of the last maintenance commands."""

        state = self._states.get(device_id)
        if state is None:
            return {}
        return {"frc_correction": state.last_frc_correction, "self_test_passed": state.self_test_passed}

    def calibration_target(self, device_id: str) -> int:
        """Return the CO2 concentration a recalibration assumes, in ppm."""

        return self._calibration_targets.get(device_id, DEFAULT_CO2_CALIBRATION_PPM)

    def set_calibration_target(self, device_id: str, ppm: int) -> None:
        """Set the CO2 concentration the next recalibration assumes."""

        self._calibration_targets[device_id] = ppm

    def signal(self, device_id: str) -> str:
        """Return the dispatcher signal announcing a new sample of one board."""

        return f"{self._api.state_update_signal}_multisensor_{device_id}"

    def voc_states(self, device_id: str) -> tuple[float, float] | None:
        """Return the learned VOC baseline for persisting across restarts."""

        state = self._states.get(device_id)
        if state is None or not state.voc.has_baseline:
            return None
        return state.voc.get_states()

    def restore_voc_states(self, device_id: str, mean: float, std: float, age: float) -> None:
        """Restore a learned VOC baseline if it is recent enough to still apply."""

        state = self._states.get(device_id)
        if state is None or age > VOC_STATE_MAX_AGE:
            return
        state.voc.set_states(mean, std)
        _LOGGER.debug("Restored VOC baseline of %s from %.0f s ago", device_id, age)

    @property
    def diagnostic_data(self) -> dict[str, Any]:
        """Return the fitted chips and latest reading of every board."""

        return {
            device_id: {
                "components": state.components.as_list(),
                "reading": vars(state.reading),
                "stcc4_running": state.stcc4_running,
                "veml7700_range": state.veml_range,
            }
            for device_id, state in self._states.items()
        }

    # ------------------------------------------------------------------
    # Sampling
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start the sampling task if there is anything to sample."""

        if self._task is None and self._states:
            self._task = self._api.hass.async_create_background_task(self._sample_loop(), "casait_multisensor")

    async def stop(self) -> None:
        """Stop the sampling task."""

        if self._task is None:
            return
        self._task.cancel()
        with suppress(asyncio.CancelledError):
            await self._task
        self._task = None

    async def _sample_loop(self) -> None:
        next_due = time.monotonic()
        while True:
            for device_id in list(self._states):
                try:
                    await self.async_sample(device_id)
                except Exception:
                    _LOGGER.exception("Error sampling Multisensor %s", device_id)
            next_due += SAMPLE_INTERVAL
            now = time.monotonic()
            if next_due < now:
                # A slow bus made us miss a slot. Skip ahead instead of
                # sampling in a burst, which would feed the VOC algorithm
                # samples closer together than it expects.
                next_due = now + SAMPLE_INTERVAL
            await asyncio.sleep(next_due - now)

    async def async_sample(self, device_id: str) -> None:
        """Read every chip of one board once and publish the result."""

        state = self._states.get(device_id)
        lock = self._locks.get(device_id)
        if state is None or lock is None or lock.locked():
            # A maintenance command owns the board right now.
            return

        async with lock:
            reading = state.reading
            parts = state.components
            failures: list[str] = []
            answered: set[str] = set()

            if parts.sht41:
                try:
                    await self._job(device_id, lambda ms: ms.sht41_trigger(device_id))
                    await asyncio.sleep(SHT41_MEASURE_TIME)
                    await self._job(device_id, lambda ms: ms.sht41_fetch(device_id, state))
                    answered.add(CHIP_SHT41)
                except DS28E17Error as err:
                    failures.append(f"SHT41: {err}")
                    reading.temperature = reading.humidity = None
                    state.t_ticks = state.rh_ticks = None

            sgp_started: float | None = None
            if parts.sgp40:
                try:
                    await self._job(device_id, lambda ms: ms.sgp40_trigger(device_id, state))
                    sgp_started = time.monotonic()
                except DS28E17Error as err:
                    failures.append(f"SGP40: {err}")
                    reading.voc_index = reading.voc_raw = None

            # The light and CO2 reads fit into the SGP40's measurement time.
            if parts.veml7700:
                try:
                    await self._job(device_id, lambda ms: ms.veml7700_sample(device_id, state))
                    answered.add(CHIP_VEML7700)
                except DS28E17Error as err:
                    failures.append(f"VEML7700: {err}")
                    reading.illuminance = None
                    state.veml_configured = False

            if parts.stcc4:
                was_running = state.stcc4_running
                try:
                    await self._job(device_id, lambda ms: ms.stcc4_sample(device_id, state))
                    answered.add(CHIP_STCC4)
                except DS28E17Error as err:
                    failures.append(f"STCC4: {err}")
                    reading.co2 = None
                if state.stcc4_running and not was_running:
                    self._stcc4_started[device_id] = time.monotonic()

            if sgp_started is not None:
                await asyncio.sleep(max(0.0, SGP40_MEASURE_TIME - (time.monotonic() - sgp_started)))
                try:
                    await self._job(device_id, lambda ms: ms.sgp40_fetch(device_id, state))
                    answered.add(CHIP_SGP40)
                except DS28E17Error as err:
                    failures.append(f"SGP40: {err}")
                    reading.voc_index = reading.voc_raw = None

        self._log_health(device_id, failures)
        self._track_chips(device_id, parts, answered)
        async_dispatcher_send(self._api.hass, self.signal(device_id))

    def _track_chips(self, device_id: str, parts: MultisensorComponents, answered: set[str]) -> None:
        """Raise a repair issue for a chip that stopped answering, clear it when it is back."""

        for chip in CHIPS:
            if not parts.has(chip):
                continue
            key = (device_id, chip)
            if chip in answered:
                if self._chip_failures.pop(key, 0) >= CHIP_MISSING_SAMPLES:
                    _LOGGER.info("%s on Multisensor %s answers again", CHIP_NAMES[chip], device_id)
                ir.async_delete_issue(self._api.hass, DOMAIN, self.chip_issue_id(device_id, chip))
                continue
            failures = self._chip_failures[key] = self._chip_failures.get(key, 0) + 1
            if failures == CHIP_MISSING_SAMPLES:
                self._raise_chip_issue(device_id, chip)

    def chip_issue_id(self, device_id: str, chip: str) -> str:
        """Return the repair issue id for one missing chip."""

        return f"multisensor_chip_missing_{self._api.entry_id}_{device_id}_{chip}"

    def _raise_chip_issue(self, device_id: str, chip: str) -> None:
        meta = self._api.ow_devices.get(device_id, {})
        name = str(meta.get("name") or f"Multisensor {device_id}")
        _LOGGER.warning("%s on Multisensor %s stopped answering", CHIP_NAMES[chip], device_id)
        ir.async_create_issue(
            self._api.hass,
            DOMAIN,
            self.chip_issue_id(device_id, chip),
            data={"entry_id": self._api.entry_id, "device_id": device_id, "chip": chip, "name": name},
            is_fixable=True,
            is_persistent=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key="multisensor_chip_missing",
            translation_placeholders={"name": name, "chip": CHIP_NAMES[chip]},
        )

    async def async_probe_chip(self, device_id: str, chip: str) -> bool:
        """Look for one chip right now; clear its issue if it answers."""

        state = self._states.get(device_id)
        lock = self._locks.get(device_id)
        if state is None or lock is None:
            return False
        async with lock:
            try:
                address = await self._job(device_id, lambda ms: ms.probe_chip(device_id, chip))
            except DS28E17Error:
                return False
            if chip == CHIP_STCC4:
                # The probe stopped continuous measurement.
                state.stcc4_running = False
                state.stcc4_ready_at = time.monotonic() + STCC4_STOP_TIME
                if address is not None and address != state.components.stcc4_address:
                    state.components = state.components.without(CHIP_STCC4).union(
                        MultisensorComponents(stcc4_address=address)
                    )
            if chip == CHIP_VEML7700:
                state.veml_configured = False
        if address is None:
            return False
        self._chip_failures.pop((device_id, chip), None)
        ir.async_delete_issue(self._api.hass, DOMAIN, self.chip_issue_id(device_id, chip))
        return True

    async def async_forget_chip(self, device_id: str, chip: str) -> None:
        """Drop a chip for good: from memory, and with its entities from the registry."""

        await self.async_load()
        assert self._known is not None
        if (known := self._known.get(device_id)) is not None:
            self._known[device_id] = known.without(chip)
            await self._async_save()

        registry = er.async_get(self._api.hass)
        for key in CHIP_ENTITY_KEYS[chip]:
            unique_id = f"{self._api.entry_id}_{device_id}_{key}"
            for domain in ("sensor", "binary_sensor", "button", "number"):
                if entity_id := registry.async_get_entity_id(domain, DOMAIN, unique_id):
                    registry.async_remove(entity_id)
        self._chip_failures.pop((device_id, chip), None)
        ir.async_delete_issue(self._api.hass, DOMAIN, self.chip_issue_id(device_id, chip))

    def _log_health(self, device_id: str, failures: list[str]) -> None:
        """Log a failing board once, and once more when it recovers."""

        if failures and device_id not in self._failing:
            self._failing.add(device_id)
            _LOGGER.warning("Multisensor %s could not be read: %s", device_id, "; ".join(failures))
        elif not failures and device_id in self._failing:
            self._failing.discard(device_id)
            _LOGGER.info("Multisensor %s is readable again", device_id)

    # ------------------------------------------------------------------
    # STCC4 maintenance
    # ------------------------------------------------------------------

    async def async_forced_recalibration(self, device_id: str, target_ppm: int) -> int:
        """Recalibrate the CO2 sensor to a known concentration and return the correction."""

        state = self._stcc4_state(device_id)
        started = self._stcc4_started.get(device_id)
        if not state.stcc4_running or started is None or time.monotonic() - started < STCC4_FRC_WARMUP:
            raise MultisensorCommandError("co2_not_warmed_up")

        word = await self._stcc4_command(
            device_id, STCC4_CMD_FORCED_RECALIBRATION, STCC4_FRC_TIME, target_ppm, answer=True
        )
        if word is None or word == STCC4_FRC_FAILED:
            raise MultisensorCommandError("co2_calibration_failed")
        state.last_frc_correction = to_int16(word)
        async_dispatcher_send(self._api.hass, self.signal(device_id))
        return state.last_frc_correction

    async def async_self_test(self, device_id: str) -> bool:
        """Run the CO2 sensor's self test and return whether it passed."""

        state = self._stcc4_state(device_id)
        word = await self._stcc4_command(device_id, STCC4_CMD_SELF_TEST, STCC4_SELF_TEST_TIME, answer=True)
        state.self_test_passed = word == 0
        async_dispatcher_send(self._api.hass, self.signal(device_id))
        return state.self_test_passed

    async def async_conditioning(self, device_id: str) -> None:
        """Condition the CO2 sensor, as recommended after a long power-off."""

        self._stcc4_state(device_id)
        await self._stcc4_command(device_id, STCC4_CMD_CONDITIONING, STCC4_CONDITIONING_TIME)

    async def async_factory_reset(self, device_id: str) -> None:
        """Reset the CO2 sensor's calibration history to the factory state."""

        state = self._stcc4_state(device_id)
        word = await self._stcc4_command(device_id, STCC4_CMD_FACTORY_RESET, STCC4_FACTORY_RESET_TIME, answer=True)
        if word:
            raise MultisensorCommandError("co2_command_failed")
        state.last_frc_correction = None
        async_dispatcher_send(self._api.hass, self.signal(device_id))

    def _stcc4_state(self, device_id: str) -> MultisensorState:
        state = self._states.get(device_id)
        if state is None or not state.components.stcc4:
            raise MultisensorCommandError("co2_sensor_unavailable")
        return state

    async def _stcc4_command(
        self,
        device_id: str,
        code: int,
        duration: float,
        *words: int,
        answer: bool = False,
    ) -> int | None:
        """Stop continuous measurement, run one command, and hand back to the sampler."""

        state = self._stcc4_state(device_id)
        async with self._locks[device_id]:
            try:
                await self._job(device_id, lambda ms: ms.stcc4_stop(device_id, state), write=True)
                self._stcc4_started.pop(device_id, None)
                await asyncio.sleep(STCC4_STOP_TIME)
                await self._job(device_id, lambda ms: ms.stcc4_send(device_id, state, code, *words), write=True)
                await asyncio.sleep(duration)
                if not answer:
                    return None
                return await self._job(device_id, lambda ms: ms.stcc4_fetch_word(device_id, state), write=True)
            except DS28E17Error as err:
                _LOGGER.warning("STCC4 command 0x%04X on %s failed: %s", code, device_id, err)
                raise MultisensorCommandError("co2_command_failed") from err
            finally:
                # The sampler restarts continuous measurement on its next turn.
                state.stcc4_running = False
                state.stcc4_ready_at = time.monotonic()

    # ------------------------------------------------------------------
    # Bus access
    # ------------------------------------------------------------------

    async def _job[T](self, device_id: str, func: Callable[[Multisensor], T], *, write: bool = False) -> T:
        """Run one transaction against the board's bus."""

        return await self._api.async_onewire_job(device_id, lambda bus: func(bus.multisensor), write=write)
