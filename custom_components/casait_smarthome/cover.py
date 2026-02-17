"""Support for casaIT PCF8574-based blinds."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from typing import Any

from homeassistant.components.cover import ATTR_POSITION, CoverEntity, CoverEntityFeature
from homeassistant.const import STATE_CLOSED, STATE_OPEN
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from . import CasaITConfigEntry
from .api import CasaITApi
from .const import DOMAIN, OM117_MODE_BLIND, PCF8574_MAPPED_PORTS, SIGNAL_STATE_UPDATED
from .helpers import OM117PairConfig, get_om117_pair_configuration

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: CasaITConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up blinds configured on OM117 modules."""

    api: CasaITApi = config_entry.runtime_data
    await api.async_wait_initialized()

    om_config = get_om117_pair_configuration(config_entry.options)

    entities: list[CasaITBlindCover] = []
    for address in api.im117_om117:
        pair_configs = om_config.get(address, {})
        if not pair_configs:
            continue

        for pair_index, pair_config in pair_configs.items():
            if pair_config.mode != OM117_MODE_BLIND:
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


class CasaITBlindCover(CoverEntity, RestoreEntity):
    """Representation of a blind controlled by two OM117 outputs."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_supported_features = (
        CoverEntityFeature.OPEN | CoverEntityFeature.CLOSE | CoverEntityFeature.STOP | CoverEntityFeature.SET_POSITION
    )
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
        self._target_position: float | None = None
        self._active_direction: str | None = None
        self._movement_task: asyncio.Task | None = None

        self._attr_unique_id = f"{config_entry.entry_id}_om117_{address}_pair_{pair_index + 1}_blind"
        self._attr_name = f"Blind pair {pair_index + 1}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, str(address))},
            name=f"Output module {hex(address)}",
            manufacturer="casaIT",
            model="PCF8574 Output",
        )
        self._attr_available = True

    async def async_added_to_hass(self) -> None:
        """Restore state and register callbacks."""

        await super().async_added_to_hass()

        last_state = await self.async_get_last_state()
        if last_state and (pos := last_state.attributes.get("current_position")) is not None:
            try:
                self._position = float(pos)
            except (TypeError, ValueError):
                self._position = 0.0
        elif last_state and last_state.state in (STATE_OPEN, STATE_CLOSED):
            self._position = 100.0 if last_state.state == STATE_OPEN else 0.0

        self.async_on_remove(async_dispatcher_connect(self.hass, SIGNAL_STATE_UPDATED, self._handle_state_update))

    async def async_will_remove_from_hass(self) -> None:
        """Stop movement when entity is removed."""

        await self._stop_motion()

    @property
    def current_cover_position(self) -> int | None:
        """Return the current position of the cover (0-100)."""

        return int(round(self._position)) if self._position is not None else None

    @property
    def is_closed(self) -> bool | None:
        """Return True if the cover is fully closed."""

        if self._position is None:
            return None
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
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose calibration and runtime information."""

        return {
            "target_position": self._target_position,
            "open_time": self._pair_config.open_time,
            "close_time": self._pair_config.close_time,
            "overrun_time": self._pair_config.overrun_time,
            "active_direction": self._active_direction,
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

    async def _start_motion(self, target: float) -> None:
        """Begin moving toward the target position."""

        if not 0 <= target <= 100:
            raise HomeAssistantError("Target position must be between 0 and 100")

        if self._address not in self._api.im117_om117:
            raise HomeAssistantError("Output module not available")

        await self._stop_motion()

        current = self._position
        if abs(target - current) < 0.5:
            self._position = target
            self._active_direction = None
            self._target_position = None
            self.async_write_ha_state()
            return

        direction = "open" if target > current else "close"
        await self._async_set_outputs(direction == "open", direction == "close")

        self._target_position = target
        self._active_direction = direction
        self._movement_task = self.hass.async_create_task(
            self._run_motion(current, target, direction),
            f"casait_blind_motion_{self._address}_{self._pair_index}",
        )

    async def _stop_motion(self) -> None:
        """Cancel current motion and stop outputs."""

        if self._movement_task:
            self._movement_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._movement_task
            self._movement_task = None

        await self._async_set_outputs(False, False)
        self._active_direction = None
        self._target_position = None
        self.async_write_ha_state()

    async def _run_motion(self, start: float, target: float, direction: str) -> None:
        """Drive the blind and update estimated position."""

        start_time = time.monotonic()
        travel_time = self._pair_config.open_time if direction == "open" else self._pair_config.close_time
        distance = abs(target - start)
        duration = travel_time * (distance / 100)
        duration = max(duration, 0.01)

        try:
            while True:
                elapsed = time.monotonic() - start_time
                progress = min(1.0, elapsed / duration)
                delta = (target - start) * progress
                self._position = start + delta
                self.async_write_ha_state()

                if progress >= 1.0:
                    break

                await asyncio.sleep(0.25)

            self._position = target
            self.async_write_ha_state()

            if target in (0.0, 100.0) and self._pair_config.overrun_time > 0:
                await asyncio.sleep(self._pair_config.overrun_time)
        except asyncio.CancelledError:
            raise
        except Exception:
            _LOGGER.exception("Error while moving blind on 0x%02x pair %s", self._address, self._pair_index + 1)
        finally:
            await self._async_set_outputs(False, False)
            self._movement_task = None
            self._target_position = None
            self._active_direction = None
            self.async_write_ha_state()

    async def _async_set_outputs(self, up: bool, down: bool) -> None:
        """Update the output ports for movement."""

        if up and down:
            raise HomeAssistantError("Cannot drive blind up and down simultaneously")

        device = self._api.im117_om117.get(self._address)
        if not device:
            raise HomeAssistantError("Output module not available")

        async with self._api.lock:
            await self.hass.async_add_executor_job(device.write_port, self._hardware_up_port, 0 if up else 1)
            await self.hass.async_add_executor_job(device.write_port, self._hardware_down_port, 0 if down else 1)

        await self._api.async_force_refresh()

    @callback
    def _handle_state_update(self) -> None:
        """Update availability from API polling."""

        self._attr_available = self._address in self._api.pcf_states
        self.async_write_ha_state()
