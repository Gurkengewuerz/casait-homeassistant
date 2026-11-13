"""Guided measurement of the travel times of an OM117 cover.

The options flow drives the cover's relays itself and the user says when an end
position is reached: first fully closed, which is where both measurements start,
then fully open and fully closed again. The elapsed times become the pair's open
and close times in the entry options, and the cover starts from a referenced,
closed position.

While a measurement runs, the cover entity stands aside and refuses commands.
Every run hands the bridge a timer that releases the relay after
``CALIBRATION_MAX_RUN`` seconds, so a closed browser tab does not leave a motor on.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any

import probatio

from homeassistant.config_entries import ConfigFlowResult
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.selector import SelectOptionDict, SelectSelector, SelectSelectorConfig, SelectSelectorMode

from .const import OM117_MODE_BLIND, OM117_MODE_SHUTTER, PCF8574_MAPPED_PORTS
from .helpers import get_module_name, get_om117_pair_configuration, set_om117_pairs

if TYPE_CHECKING:
    from .api import CasaITApi

_LOGGER = logging.getLogger(__name__)

# Longest a relay runs during a measurement before the bridge releases it.
CALIBRATION_MAX_RUN = 300.0
# Pause before the motor reverses, as the cover entity waits it.
CALIBRATION_REVERSAL_PAUSE = 0.5
# The range the travel time numbers accept.
MIN_TRAVEL_TIME = 1.0
MAX_TRAVEL_TIME = 180.0

CONF_COVER = "cover"
PHASE_START = "start"
PHASE_DONE = "done"
PHASE_ABORTED = "aborted"


class CoverCalibration:
    """Drive the two relays of one OM117 pair and time the runs."""

    def __init__(self, api: CasaITApi, address: int, pair: int) -> None:
        """Bind the measurement to one pair."""

        self.api = api
        self.address = address
        self.pair = pair
        self._up_bit = PCF8574_MAPPED_PORTS[pair * 2]
        self._down_bit = PCF8574_MAPPED_PORTS[pair * 2 + 1]
        self._started_at: float | None = None
        self.running: str | None = None

    async def async_begin(self) -> None:
        """Have the cover entity stand aside before the relays are touched."""

        self.api.calibrating.add((self.address, self.pair))
        async_dispatcher_send(self.api.hass, self.api.calibration_signal(self.address), self.pair, PHASE_START)

    async def async_run(self, direction: str) -> bool:
        """Start the motor up or down and return whether the relays switched."""

        if self.running is not None and self.running != direction:
            await self.async_stop()
            await asyncio.sleep(CALIBRATION_REVERSAL_PAUSE)
        up = direction == "up"
        relay = 1 << (self._up_bit if up else self._down_bit)
        written = await self.api.async_write_pcf_ports(
            self.address, {self._up_bit: 0 if up else 1, self._down_bit: 1 if up else 0}
        )
        if not written:
            return False
        await self.api.async_arm_output_timer(self.address, relay, 0, relay, CALIBRATION_MAX_RUN)
        self.running = direction
        self._started_at = time.monotonic()
        return True

    async def async_stop(self) -> float | None:
        """Release both relays and return how long the motor ran, None if it did not."""

        elapsed = None if self._started_at is None else time.monotonic() - self._started_at
        self._started_at = None
        self.running = None
        if not await self.api.async_write_pcf_ports(self.address, {self._up_bit: 1, self._down_bit: 1}):
            _LOGGER.error("Could not release OM117 0x%02X pair %s after calibrating", self.address, self.pair + 1)
        return elapsed

    async def async_finish(self, *, closed: bool) -> None:
        """Release the relays and hand the cover back, closed and referenced when ``closed``."""

        await self.async_stop()
        self.api.calibrating.discard((self.address, self.pair))
        async_dispatcher_send(
            self.api.hass,
            self.api.calibration_signal(self.address),
            self.pair,
            PHASE_DONE if closed else PHASE_ABORTED,
        )


def calibration_choices(api: CasaITApi, options: Any) -> list[SelectOptionDict]:
    """List every shutter and blind pair the bridge can drive right now."""

    choices: list[SelectOptionDict] = []
    for address, pairs in sorted(api.om117_pair_configuration.items()):
        if address not in api.im117_om117:
            continue
        for pair, config in sorted(pairs.items()):
            if config.mode not in {OM117_MODE_SHUTTER, OM117_MODE_BLIND}:
                continue
            base = f"OM117 0x{address:02X}"
            if name := get_module_name(options, "om117", address, ""):
                base = f"{base} · {name}"
            choices.append({"value": f"{address}:{pair}", "label": f"{base} · {pair * 2 + 1}/{pair * 2 + 2}"})
    return choices


class CoverCalibrationFlowMixin:
    """Options flow steps that measure a cover's travel times.

    The host flow provides ``_api``, ``_options`` and ``_staged_options`` and
    returns to its menu through ``async_step_init``.
    """

    _calibration: CoverCalibration | None = None
    _calibration_open_time: float | None = None

    async def async_step_calibrate_cover(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Pick the cover to measure."""

        api: CasaITApi | None = self._api  # type: ignore[attr-defined]
        if api is None:
            return self.async_abort(reason="integration_not_ready")  # type: ignore[attr-defined]
        choices = calibration_choices(api, self._options)  # type: ignore[attr-defined]
        if not choices:
            return self.async_abort(reason="no_covers")  # type: ignore[attr-defined]
        errors: dict[str, str] = {}
        if user_input is not None:
            address, _, pair = str(user_input[CONF_COVER]).partition(":")
            calibration = CoverCalibration(api, int(address), int(pair))
            await calibration.async_begin()
            if await calibration.async_run("down"):
                self._calibration = calibration
                return await self.async_step_calibrate_closing()
            await calibration.async_finish(closed=False)
            errors["base"] = "cover_write_failed"
        return self.async_show_form(  # type: ignore[attr-defined]
            step_id="calibrate_cover",
            data_schema=probatio.Schema(
                {
                    probatio.Required(CONF_COVER): SelectSelector(
                        SelectSelectorConfig(options=choices, mode=SelectSelectorMode.DROPDOWN)
                    )
                }
            ),
            errors=errors,
        )

    async def async_step_calibrate_closing(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Close the cover to its end position, then start timing the way up."""

        return await self._async_calibration_step("calibrate_closing", user_input, next_direction="up")

    async def async_step_calibrate_opening(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Time the way up, then start timing the way down."""

        return await self._async_calibration_step("calibrate_opening", user_input, next_direction="down")

    async def async_step_calibrate_closing_timed(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Time the way down and store both times."""

        return await self._async_calibration_step("calibrate_closing_timed", user_input, next_direction=None)

    async def async_step_calibrate_done(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Return to the menu once the user has seen the result."""

        return await self.async_step_init()  # type: ignore[attr-defined]

    async def _async_calibration_step(
        self, step_id: str, user_input: dict[str, Any] | None, *, next_direction: str | None
    ) -> ConfigFlowResult:
        calibration = self._calibration
        if calibration is None:
            return await self.async_step_calibrate_cover()
        errors: dict[str, str] = {}
        if user_input is not None:
            elapsed = await calibration.async_stop()
            if step_id == "calibrate_opening":
                self._calibration_open_time = elapsed
            if next_direction is None:
                return await self._async_store_calibration(elapsed)
            await asyncio.sleep(CALIBRATION_REVERSAL_PAUSE)
            if await calibration.async_run(next_direction):
                next_step = "calibrate_opening" if next_direction == "up" else "calibrate_closing_timed"
                return await getattr(self, f"async_step_{next_step}")()
            errors["base"] = "cover_write_failed"
        return self.async_show_form(  # type: ignore[attr-defined]
            step_id=step_id, data_schema=probatio.Schema({}), errors=errors
        )

    async def _async_store_calibration(self, close_time: float | None) -> ConfigFlowResult:
        calibration = self._calibration
        assert calibration is not None
        open_time = self._calibration_open_time
        self._calibration = None
        self._calibration_open_time = None
        if open_time is None or close_time is None:
            await calibration.async_finish(closed=False)
            return await self.async_step_calibrate_cover()
        times = {
            "open_time": round(min(MAX_TRAVEL_TIME, max(MIN_TRAVEL_TIME, open_time)), 1),
            "close_time": round(min(MAX_TRAVEL_TIME, max(MIN_TRAVEL_TIME, close_time)), 1),
        }
        calibration.api.set_pair_timing(calibration.address, calibration.pair, times)
        if (staged := self._staged_options) is not None:  # type: ignore[attr-defined]
            # Saving the menu's other edits must not put the old times back.
            pairs = get_om117_pair_configuration(staged).get(calibration.address, {})
            if (config := pairs.get(calibration.pair)) is not None:
                config.open_time, config.close_time = times["open_time"], times["close_time"]
                self._staged_options = set_om117_pairs(staged, calibration.address, pairs)  # type: ignore[attr-defined]
        await calibration.async_finish(closed=True)
        return self.async_show_form(  # type: ignore[attr-defined]
            step_id="calibrate_done",
            data_schema=probatio.Schema({}),
            description_placeholders={key: f"{value:.1f}" for key, value in times.items()},
        )

    def async_remove(self) -> None:
        """Stop the motor when the dialog is closed in the middle of a measurement."""

        super().async_remove()  # type: ignore[misc]
        if (calibration := self._calibration) is not None:
            self._calibration = None
            calibration.api.hass.async_create_background_task(
                calibration.async_finish(closed=False), "casait_calibration_abort"
            )
