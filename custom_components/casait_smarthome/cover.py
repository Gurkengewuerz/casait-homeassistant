"""Support for casaIT PCF8574-based blinds."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import contextlib
import logging
import time
from typing import Any

from homeassistant.components.cover import (
    ATTR_POSITION,
    ATTR_TILT_POSITION,
    CoverDeviceClass,
    CoverEntity,
    CoverEntityFeature,
)
from homeassistant.const import STATE_CLOSED, STATE_CLOSING, STATE_OPEN, STATE_OPENING
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from . import CasaITConfigEntry
from .api import CasaITApi
from .const import COVER_REFERENCE_AUTO, DOMAIN, OM117_MODE_BLIND, OM117_MODE_SHUTTER, PCF8574_MAPPED_PORTS
from .helpers import (
    OM117PairConfig,
    build_bridge_slug,
    build_device_identifier,
    build_i2c_entity_id,
    get_address_range,
    get_module_name,
)

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 0

# Motors must stand still before they reverse; relay makers ask for a few hundred ms.
REVERSAL_PAUSE = 0.5
# Estimated position error, in percent, that a timed move adds: a fixed part for the
# motor's start and stop, and a part proportional to the distance travelled.
DRIFT_PER_MOVE = 1.0
DRIFT_PER_DISTANCE = 0.02
REFERENCE_THRESHOLD = 5.0
REFERENCE_MARGIN = 10.0
POSITION_UNKNOWN = 100.0

ATTR_POSITION_UNCERTAINTY = "position_uncertainty"


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: CasaITConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up blinds configured on OM117 modules."""

    api: CasaITApi = config_entry.runtime_data
    await api.async_wait_initialized()

    om_config = api.om117_pair_configuration
    output_range = get_address_range("OM117")

    entities: list[CasaITBlindCover] = []
    for address in api.im117_om117:
        if output_range is None or not output_range[0] <= address <= output_range[1]:
            continue

        pair_configs = om_config.get(address, {})
        if not pair_configs:
            continue

        for pair_index, pair_config in pair_configs.items():
            if pair_config.mode not in {OM117_MODE_BLIND, OM117_MODE_SHUTTER}:
                continue
            entities.append(
                CasaITBlindCover(
                    api,
                    config_entry,
                    address,
                    pair_index,
                    pair_config,
                )
            )

    if entities:
        async_add_entities(entities)


def _motor_direction(direction: str | None) -> str | None:
    """Return which relay a movement drives, so tilting counts as moving."""

    if direction in {"open", "tilt_open"}:
        return "up"
    if direction in {"close", "tilt_close"}:
        return "down"
    return None


class CasaITBlindCover(CoverEntity, RestoreEntity):
    """Representation of a blind controlled by two OM117 outputs.

    The position is estimated from travel times. Every timed move adds to an
    estimated position error, and a run into an end position with overrun
    clears it again; beyond a threshold the cover references itself.
    """

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_assumed_state = True

    def __init__(
        self,
        api: CasaITApi,
        config_entry: CasaITConfigEntry,
        address: int,
        pair_index: int,
        pair_config: OM117PairConfig,
    ) -> None:
        """Initialize the blind entity."""

        self._api = api
        self._address = address
        self._pair_index = pair_index
        self._pair_config = pair_config

        # Pair indices are zero-based internally; each pair controls two consecutive ports.
        self._up_port = pair_index * 2
        self._down_port = self._up_port + 1
        self._hardware_up_port = PCF8574_MAPPED_PORTS[self._up_port]
        self._hardware_down_port = PCF8574_MAPPED_PORTS[self._down_port]

        self._position: float = 0.0
        self._uncertainty: float = POSITION_UNKNOWN
        self._tilt_position: float = 0.0
        self._target_position: float | None = None
        self._target_tilt_position: float | None = None
        self._active_direction: str | None = None
        self._movement_task: asyncio.Task | None = None
        self._handover = False
        self._advance: Callable[[], float] | None = None

        is_blind = pair_config.mode == OM117_MODE_BLIND
        self._attr_device_class = CoverDeviceClass.BLIND if is_blind else CoverDeviceClass.SHUTTER
        self._attr_translation_key = "om117_blind" if is_blind else "om117_shutter"
        self._attr_supported_features = (
            CoverEntityFeature.OPEN
            | CoverEntityFeature.CLOSE
            | CoverEntityFeature.STOP
            | CoverEntityFeature.SET_POSITION
        )
        if is_blind:
            self._attr_supported_features |= CoverEntityFeature.SET_TILT_POSITION

        bridge_slug = build_bridge_slug(config_entry.entry_id, config_entry.unique_id)
        cover_kind = "blind" if is_blind else "shutter"
        self._attr_unique_id = f"{config_entry.entry_id}_om117_{address}_pair_{pair_index + 1}_{cover_kind}"
        self.entity_id = build_i2c_entity_id("cover", bridge_slug, "om117", address, cover_kind, pair_index + 1)
        self._attr_translation_placeholders = {"pair": str(pair_index + 1)}
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, build_device_identifier(config_entry.entry_id, "om117", address))},
            name=get_module_name(config_entry.options, "om117", address, f"OM117 0x{address:02X}"),
            manufacturer="casaIT",
            model="PCF8574 Output",
            via_device=(DOMAIN, build_device_identifier(config_entry.entry_id, "bridge", "controller")),
        )
        self._attr_available = True

    async def async_added_to_hass(self) -> None:
        """Restore state and register callbacks."""

        await super().async_added_to_hass()

        if (last_state := await self.async_get_last_state()) is not None:
            attributes = last_state.attributes
            if (pos := attributes.get("current_position")) is not None:
                with contextlib.suppress(TypeError, ValueError):
                    self._position = float(pos)
            elif last_state.state in (STATE_OPEN, STATE_CLOSED):
                self._position = 100.0 if last_state.state == STATE_OPEN else 0.0
            if (tilt := attributes.get("current_tilt_position")) is not None:
                with contextlib.suppress(TypeError, ValueError):
                    self._tilt_position = float(tilt)
            # A restart in the middle of a move leaves the motor wherever it stopped.
            if last_state.state not in (STATE_OPENING, STATE_CLOSING):
                with contextlib.suppress(TypeError, ValueError):
                    self._uncertainty = min(POSITION_UNKNOWN, float(attributes[ATTR_POSITION_UNCERTAINTY]))

        self.async_on_remove(
            async_dispatcher_connect(self.hass, self._api.address_signal(self._address), self._handle_state_update)
        )
        self.async_on_remove(
            async_dispatcher_connect(self.hass, self._api.power_loss_signal(self._address), self._handle_power_loss)
        )

    async def async_will_remove_from_hass(self) -> None:
        """Stop movement when entity is removed."""

        await self._stop_motion()

    @property
    def current_cover_position(self) -> int | None:
        """Return the current position of the cover (0-100)."""

        return int(round(self._position))

    @property
    def is_closed(self) -> bool | None:
        """Return True if the cover is fully closed."""

        return self._position <= 0

    @property
    def is_closing(self) -> bool:
        """Return True if the cover is closing."""

        return self._active_direction == "close"

    @property
    def is_opening(self) -> bool:
        """Return True if the cover is opening."""

        return self._active_direction == "open"

    @property
    def current_cover_tilt_position(self) -> int | None:
        """Return the estimated slat tilt position."""

        if self._pair_config.mode != OM117_MODE_BLIND:
            return None
        return int(round(self._tilt_position))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose calibration and runtime information."""

        return {
            "target_position": self._target_position,
            "open_time": self._pair_config.open_time,
            "close_time": self._pair_config.close_time,
            "overrun_time": self._pair_config.overrun_time,
            "tilt_time": self._pair_config.tilt_time,
            "target_tilt_position": self._target_tilt_position,
            "active_direction": self._active_direction,
            ATTR_POSITION_UNCERTAINTY: round(self._uncertainty, 1),
            "reference_mode": self._pair_config.reference_mode,
        }

    async def async_open_cover(self, **kwargs: Any) -> None:
        """Open the cover fully."""

        await self._start_motion(100.0)

    async def async_close_cover(self, **kwargs: Any) -> None:
        """Close the cover fully."""

        await self._start_motion(0.0)

    async def async_set_cover_position(self, **kwargs: Any) -> None:
        """Move the cover to a specific position."""

        if (position := kwargs.get(ATTR_POSITION)) is None:
            return
        await self._start_motion(float(position))

    async def async_stop_cover(self, **kwargs: Any) -> None:
        """Stop the cover."""

        await self._stop_motion()

    async def async_set_cover_tilt_position(self, **kwargs: Any) -> None:
        """Move the blind slats to a time-estimated tilt position."""

        if (position := kwargs.get(ATTR_TILT_POSITION)) is None:
            return
        await self._start_tilt_motion(float(position))

    async def async_reference_run(self, return_to_position: bool = True) -> None:
        """Run into the nearer end position to clear the position error.

        With ``return_to_position`` the cover goes back to where it was believed
        to be before, so a scheduled reference does not leave it somewhere else.
        """

        if self._pair_config.overrun_time <= 0:
            raise HomeAssistantError(translation_domain=DOMAIN, translation_key="reference_needs_overrun")
        self._refresh_position()
        resume = self._target_position if self._target_position is not None else self._position
        end = 0.0 if resume <= 50 else 100.0
        legs = [end]
        if return_to_position and abs(resume - end) >= 0.5:
            legs.append(resume)
        await self._start_legs(legs, force_reference=True)

    async def _start_motion(self, target: float) -> None:
        """Begin moving toward the target position, referencing first when due."""

        if not 0 <= target <= 100:
            raise HomeAssistantError(translation_domain=DOMAIN, translation_key="position_out_of_range")
        self._refresh_position()
        await self._start_legs(self._plan_legs(target))

    def _refresh_position(self) -> None:
        """Bring the estimate of a running move up to this instant."""

        if self._advance is not None:
            self._advance()

    def _plan_legs(self, target: float) -> list[float]:
        """Return the positions to drive through, with a reference end first if due."""

        if (
            self._pair_config.reference_mode != COVER_REFERENCE_AUTO
            or self._pair_config.overrun_time <= 0
            or self._uncertainty <= REFERENCE_THRESHOLD
            or target in (0.0, 100.0)
        ):
            return [target]
        if target <= REFERENCE_MARGIN:
            return [0.0, target]
        if target >= 100 - REFERENCE_MARGIN:
            return [100.0, target]
        if self._uncertainty >= POSITION_UNKNOWN:
            return [0.0 if target < 50 else 100.0, target]
        return [target]

    def _needs_travel(self, target: float, *, force_reference: bool = False) -> bool:
        """Return True when reaching the target needs the motor at all."""

        if abs(target - self._position) >= 0.5:
            return True
        return target in (0.0, 100.0) and (force_reference or self._uncertainty > REFERENCE_THRESHOLD)

    async def _start_legs(self, legs: list[float], *, force_reference: bool = False) -> None:
        """Switch the motor on for the first leg and run all legs in a task."""

        if self._address not in self._api.im117_om117:
            raise HomeAssistantError(translation_domain=DOMAIN, translation_key="output_module_unavailable")

        if not self._needs_travel(legs[0], force_reference=force_reference):
            await self._stop_motion()
            self._position = legs[-1]
            self.async_write_ha_state()
            return

        direction = self._leg_direction(legs[0])
        await self._halt(direction)
        await self._async_set_outputs(direction == "open", direction == "close")

        self._target_position = legs[-1]
        self._active_direction = direction
        self._movement_task = self.hass.async_create_task(
            self._run_legs(legs),
            f"casait_blind_motion_{self._address}_{self._pair_index}",
        )
        self.async_write_ha_state()

    def _leg_direction(self, target: float) -> str:
        """Return the direction toward a leg's target; an end position decides a tie."""

        if abs(target - self._position) < 0.5:
            return "open" if target >= 50 else "close"
        return "open" if target > self._position else "close"

    async def _start_tilt_motion(self, target: float) -> None:
        """Begin moving the slats toward a tilt target."""

        if self._pair_config.mode != OM117_MODE_BLIND:
            raise HomeAssistantError(translation_domain=DOMAIN, translation_key="tilt_unsupported")
        if not 0 <= target <= 100:
            raise HomeAssistantError(translation_domain=DOMAIN, translation_key="tilt_position_out_of_range")
        if self._address not in self._api.im117_om117:
            raise HomeAssistantError(translation_domain=DOMAIN, translation_key="output_module_unavailable")

        current = self._tilt_position
        if abs(target - current) < 0.5:
            await self._stop_motion()
            self._tilt_position = target
            self.async_write_ha_state()
            return

        direction = "tilt_open" if target > current else "tilt_close"
        await self._halt(direction)
        await self._async_set_outputs(direction == "tilt_open", direction == "tilt_close")
        self._target_tilt_position = target
        self._active_direction = direction
        self._movement_task = self.hass.async_create_task(
            self._run_tilt_motion(current, target),
            f"casait_blind_tilt_{self._address}_{self._pair_index}",
        )

    async def _halt(self, next_direction: str) -> None:
        """End the current movement so a new one can take over the relays.

        A movement in the same motor direction hands its relay over as it is, so
        the motor keeps running. Reversing drops the relay and waits first.
        """

        previous = _motor_direction(self._active_direction)
        if self._movement_task:
            self._handover = True
            self._movement_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._movement_task
            self._handover = False
            self._movement_task = None
            self._target_position = None
            self._target_tilt_position = None
        if previous is not None and previous != _motor_direction(next_direction):
            await self._async_set_outputs(False, False)
            self._active_direction = None
            await asyncio.sleep(REVERSAL_PAUSE)

    async def _stop_motion(self) -> None:
        """Cancel current motion and stop outputs."""

        if self._movement_task:
            # The task releases the relays itself on its way out.
            self._movement_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._movement_task
            self._movement_task = None
        else:
            await self._async_set_outputs(False, False)

        self._active_direction = None
        self._target_position = None
        self._target_tilt_position = None
        self.async_write_ha_state()

    async def _run_legs(self, legs: list[float]) -> None:
        """Drive through each leg in turn, reversing the motor between them."""

        try:
            for index, target in enumerate(legs):
                direction = self._leg_direction(target)
                if index:
                    if _motor_direction(direction) != _motor_direction(self._active_direction):
                        await self._async_set_outputs(False, False)
                        self._active_direction = None
                        self.async_write_ha_state()
                        await asyncio.sleep(REVERSAL_PAUSE)
                    await self._async_set_outputs(direction == "open", direction == "close")
                    self._active_direction = direction
                await self._run_leg(target, direction)
        except asyncio.CancelledError:
            raise
        except Exception:
            _LOGGER.exception("Error while moving blind on 0x%02x pair %s", self._address, self._pair_index + 1)
        finally:
            if not self._handover:
                await self._async_set_outputs(False, False)
                self._target_position = None
                self._active_direction = None
                self._movement_task = None
            self.async_write_ha_state()

    async def _run_leg(self, target: float, direction: str) -> None:
        """Drive to one target, then account for the position error it leaves."""

        start = self._position
        travel_time = self._pair_config.open_time if direction == "open" else self._pair_config.close_time
        into_end = target in (0.0, 100.0)
        # Into an end position the motor runs on by the current error, so it
        # arrives even if the estimate was short.
        extra = self._uncertainty if into_end else 0.0
        duration = max(0.01, travel_time * (abs(target - start) + extra) / 100)
        started = time.monotonic()
        arrived = referenced = False

        def advance() -> float:
            progress = min(1.0, (time.monotonic() - started) / duration)
            self._position = start + (target - start) * progress
            return progress

        self._advance = advance
        try:
            while (progress := advance()) < 1.0:
                self.async_write_ha_state()
                await asyncio.sleep(min(0.25, duration * (1.0 - progress)))
            arrived = True
            self.async_write_ha_state()

            if into_end and self._pair_config.overrun_time > 0:
                await asyncio.sleep(self._pair_config.overrun_time)
                referenced = True
        finally:
            self._advance = None
            if not arrived:
                advance()
            if referenced:
                self._uncertainty = 0.0
            else:
                moved = abs(self._position - start)
                self._uncertainty = min(
                    POSITION_UNKNOWN, self._uncertainty + DRIFT_PER_MOVE + DRIFT_PER_DISTANCE * moved
                )

    async def _run_tilt_motion(self, start: float, target: float) -> None:
        """Drive the relays briefly and estimate the resulting slat angle."""

        started = time.monotonic()
        duration = max(0.01, self._pair_config.tilt_time * abs(target - start) / 100)
        try:
            while True:
                progress = min(1.0, (time.monotonic() - started) / duration)
                self._tilt_position = start + (target - start) * progress
                self.async_write_ha_state()
                if progress >= 1.0:
                    break
                await asyncio.sleep(min(0.1, duration * (1.0 - progress)))
            self._tilt_position = target
        finally:
            # Turning the slats moves the whole hanging a little as well.
            self._uncertainty = min(POSITION_UNKNOWN, self._uncertainty + DRIFT_PER_MOVE)
            if not self._handover:
                await self._async_set_outputs(False, False)
                self._movement_task = None
                self._target_tilt_position = None
                self._active_direction = None
            self.async_write_ha_state()

    async def _async_set_outputs(self, up: bool, down: bool) -> None:
        """Set both relays of the pair in one write."""

        if up and down:
            raise HomeAssistantError(translation_domain=DOMAIN, translation_key="opposing_outputs")

        written = await self._api.async_write_pcf_ports(
            self._address,
            {self._hardware_up_port: 0 if up else 1, self._hardware_down_port: 0 if down else 1},
        )

        if not written:
            # Releasing the outputs also runs from teardown and from the motion task's
            # finally block, where raising would only mask the original failure.
            if not (up or down):
                _LOGGER.error("Failed to release blind outputs on 0x%02x pair %s", self._address, self._pair_index + 1)
            else:
                raise HomeAssistantError(translation_domain=DOMAIN, translation_key="cover_write_failed")

    @callback
    def _handle_power_loss(self) -> None:
        """Stop tracking a move whose motor stopped with the module's power."""

        if self._movement_task is None:
            return
        self._uncertainty = POSITION_UNKNOWN
        self.hass.async_create_task(self._stop_motion(), f"casait_blind_power_loss_{self._address}_{self._pair_index}")

    @callback
    def _handle_state_update(self) -> None:
        """Update availability from API polling."""

        self._attr_available = self._address in self._api.pcf_states
        self.async_write_ha_state()
