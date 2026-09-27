"""Bring outputs back to their last commanded state after a module lost power.

Output modules start in their power-on state when the bus supply was cut: every
relay released, dimmers at zero, the LED controller on its defaults. The poll
loop reads the outputs back anyway, so a reading that no longer matches what
Home Assistant last commanded, long after that command, means the module was
reset. Depending on the module's power-on policy the commanded state is then
written again, or the outputs are switched off and that becomes the new state.

The commanded state is persisted, so a reset that happened while Home
Assistant itself was down is caught on the first reading after it starts.
Cover and pulse outputs are never switched on again; a moving cover is told its
motor stopped instead.
"""

from __future__ import annotations

from collections.abc import Coroutine, Mapping
from copy import deepcopy
import logging
import time
from typing import TYPE_CHECKING, Any

from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.storage import Store

from .const import DOMAIN, OM117_MODE_SWITCH, PCF8574_MAPPED_PORTS, POWER_ON_OFF, POWER_ON_RESTORE
from .helpers import OM117PairConfig, power_on_key
from .services.i2cClasses.dm117 import DeviceType, DimmerConfig, DM117PortConfig, PortConfig
from .services.i2cClasses.led_controller import AnimationMode, Color, LEDConfig

if TYPE_CHECKING:
    import asyncio

    from .api import CasaITApi

_LOGGER = logging.getLogger(__name__)

STORAGE_VERSION = 1
SAVE_DELAY = 5.0
# A reading this soon after our own write may still show the old value: DM117
# dimmers ramp towards their target, and a poll read can predate the write.
RESTORE_GRACE = 10.0
# A module that keeps losing its state is restored at most this often.
RESTORE_COOLDOWN = 60.0


def _led_to_dict(config: LEDConfig) -> dict[str, Any]:
    return {
        "led_count": config.led_count,
        "state": config.state,
        "brightness": config.brightness,
        "animation": config.animation.value,
        "animation_speed": config.animation_speed,
        "colors": [[color.red, color.green, color.blue] for color in config.colors],
    }


def _led_from_dict(data: Mapping[str, Any]) -> LEDConfig | None:
    try:
        return LEDConfig(
            led_count=int(data["led_count"]),
            state=bool(data["state"]),
            brightness=int(data["brightness"]),
            animation=AnimationMode(int(data["animation"])),
            animation_speed=int(data["animation_speed"]),
            colors=[Color(*(int(part) for part in color)) for color in data["colors"]],
        )
    except KeyError, TypeError, ValueError:
        return None


class CasaITOutputRestorer:
    """Remember what every output was last commanded to and enforce it after a reset."""

    def __init__(self, api: CasaITApi, policies: Mapping[str, str] | None = None) -> None:
        """Bind the restorer to its API and the configured power-on policies."""

        self._api = api
        self._policies = dict(policies or {})
        self._store: Store[dict[str, Any]] = Store(api.hass, STORAGE_VERSION, f"{DOMAIN}.{api.entry_id}.outputs")
        self._pcf: dict[int, int] = {}
        self._dm117: dict[int, dict[int, int]] = {}
        self._ds2413: dict[str, dict[int, bool]] = {}
        self._led: dict[str, LEDConfig] = {}
        self._written_at: dict[str, float] = {}
        self._restored_at: dict[str, float] = {}
        self._tasks: set[asyncio.Task[None]] = set()

    def policy(self, kind: str, ident: int | str) -> str:
        """Return what a module does after it lost power."""

        return self._policies.get(power_on_key(kind, ident), POWER_ON_RESTORE)

    async def async_load(self) -> None:
        """Load the commanded states saved before the last shutdown."""

        data = await self._store.async_load() or {}
        self._pcf = {int(address): int(value) & 0xFF for address, value in data.get("pcf", {}).items()}
        self._dm117 = {
            int(address): {int(port): int(value) for port, value in ports.items()}
            for address, ports in data.get("dm117", {}).items()
        }
        self._ds2413 = {
            device_id: {int(channel): bool(on) for channel, on in channels.items()}
            for device_id, channels in data.get("ds2413", {}).items()
        }
        self._led = {
            device_id: config
            for device_id, raw in data.get("led", {}).items()
            if (config := _led_from_dict(raw)) is not None
        }

    def _data_to_save(self) -> dict[str, Any]:
        return {
            "pcf": {str(address): value for address, value in self._pcf.items()},
            "dm117": {
                str(address): {str(port): value for port, value in ports.items()}
                for address, ports in self._dm117.items()
            },
            "ds2413": {
                device_id: {str(channel): on for channel, on in channels.items()}
                for device_id, channels in self._ds2413.items()
            },
            "led": {device_id: _led_to_dict(config) for device_id, config in self._led.items()},
        }

    def _save(self) -> None:
        self._store.async_delay_save(self._data_to_save, SAVE_DELAY)

    def diagnostics(self) -> dict[str, Any]:
        """Return the commanded states and policies for the diagnostics download."""

        return {"policies": dict(self._policies), "commanded": self._data_to_save()}

    # ------------------------------------------------------------------
    # Commanded state, recorded after every verified write
    # ------------------------------------------------------------------

    def _note(self, key: str) -> None:
        self._written_at[key] = time.monotonic()
        self._save()

    def note_pcf_written(self, address: int, value: int) -> None:
        """Record the port byte just written to an output module."""

        self._pcf[address] = value & 0xFF
        self._note(f"pcf:{address}")

    def note_dm117_written(self, address: int, port: int, value: int) -> None:
        """Record the value just written to a DM117 output or dimmer slot."""

        self._dm117.setdefault(address, {})[port] = value
        self._note(f"dm117:{address}")

    def note_ds2413_written(self, device_id: str, channel: int, on: bool) -> None:
        """Record the state just written to a DS2413 output channel."""

        self._ds2413.setdefault(device_id, {})[channel] = on
        self._note(f"ds2413:{device_id}")

    def note_led_written(self, device_id: str, config: LEDConfig) -> None:
        """Record the configuration just written to an LED controller."""

        self._led[device_id] = deepcopy(config)
        self._note(f"led:{device_id}")

    # ------------------------------------------------------------------
    # Readings, compared against the commanded state
    # ------------------------------------------------------------------

    def _due(self, key: str) -> bool:
        """Return True when a mismatch on this module may be acted on now."""

        now = time.monotonic()
        if now - self._written_at.get(key, -RESTORE_GRACE) < RESTORE_GRACE:
            return False
        if now - self._restored_at.get(key, -RESTORE_COOLDOWN) < RESTORE_COOLDOWN:
            _LOGGER.debug("Outputs of %s changed again within %s s; not restoring", key, RESTORE_COOLDOWN)
            return False
        self._restored_at[key] = now
        return True

    def _run(self, coro: Coroutine[Any, Any, None], name: str) -> None:
        task = self._api.hass.async_create_background_task(coro, name)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    @staticmethod
    def switch_mask(pairs: Mapping[int, OM117PairConfig]) -> int:
        """Return the port bits of an OM117 that drive plain switches."""

        mask = 0
        for pair in range(4):
            if pairs.get(pair, OM117PairConfig()).mode == OM117_MODE_SWITCH:
                mask |= (1 << PCF8574_MAPPED_PORTS[pair * 2]) | (1 << PCF8574_MAPPED_PORTS[pair * 2 + 1])
        return mask

    def check_pcf(self, address: int, value: int) -> None:
        """Compare an output module's port byte with what was last written to it."""

        commanded = self._pcf.get(address)
        if commanded is None:
            self._pcf[address] = value
            self._save()
            return
        # Bits under a bridge timer may already be back off before our own stop lands.
        held = self._api.outputs.timer_bits(address)
        if not (commanded ^ value) & ~held & 0xFF or not self._due(f"pcf:{address}"):
            return

        switches = self.switch_mask(self._api.om117_pair_configuration.get(address, {}))
        # Active low: a set bit is a released relay, which is the power-on state.
        target = commanded if self.policy("om117", address) == POWER_ON_RESTORE else 0xFF
        changes = {
            bit: (target >> bit) & 1 for bit in range(8) if switches & (1 << bit) and ((value ^ target) >> bit) & 1
        }
        _LOGGER.warning(
            "OM117 0x%02X reads 0x%02X instead of the commanded 0x%02X; it probably lost power",
            address,
            value,
            commanded,
        )
        self._pcf[address] = (value & ~switches) | (target & switches)
        self._save()
        if (value ^ commanded) & ~switches & 0xFF:
            async_dispatcher_send(self._api.hass, self._api.power_loss_signal(address))
        if changes:
            self._run(self._restore_pcf(address, changes), f"casait_restore_om117_{address:02x}")

    async def _restore_pcf(self, address: int, changes: Mapping[int, int]) -> None:
        if not await self._api.async_write_pcf_ports(address, changes):
            _LOGGER.warning("Could not restore the switch outputs of OM117 0x%02X", address)

    def check_dm117(self, address: int, values: Mapping[int, int], slots_lost: bool) -> None:
        """Compare a DM117's output slots with what was last written to them.

        ``slots_lost`` tells that the module no longer reports the configured slot
        types, which only a reset does; the slots are then configured again first.
        """

        commanded = self._dm117.get(address, {})
        mismatched = {port: value for port, value in commanded.items() if values.get(port) != value}
        if not (mismatched or slots_lost) or not self._due(f"dm117:{address}"):
            return

        _LOGGER.warning("DM117 0x%02X lost its configured slots or output values; it probably lost power", address)
        restore = self.policy("dm117", address) == POWER_ON_RESTORE
        targets = {port: value if restore else 0 for port, value in commanded.items()}
        self._dm117[address] = targets
        self._save()
        self._run(self._restore_dm117(address, targets, slots_lost), f"casait_restore_dm117_{address:02x}")

    async def _restore_dm117(self, address: int, targets: Mapping[int, int], slots_lost: bool) -> None:
        slots = self._api.dm117_slot_types(address)
        if slots_lost and slots:
            await self._api.async_configure_dm117({address: slots})
        for port, value in targets.items():
            device_type = slots.get(port)
            if device_type is DeviceType.DIMMER:
                config = DM117PortConfig(port, device_type, dimmer=DimmerConfig(value))
            elif device_type is DeviceType.OUTPUT:
                config = DM117PortConfig(port, device_type, digital=PortConfig.from_raw(value))
            else:
                continue
            if not await self._api.async_write_dm117_port(address, config):
                _LOGGER.warning("Could not restore DM117 0x%02X slot %s", address, port + 1)

    def check_ds2413(self, device_id: str, pins: tuple[bool, bool] | list[bool]) -> None:
        """Compare a DS2413's output channels with what was last written to them."""

        commanded = self._ds2413.get(device_id, {})
        # A switched-on output pulls its pin low.
        mismatched = {channel: on for channel, on in commanded.items() if on == bool(pins[channel])}
        if not mismatched or not self._due(f"ds2413:{device_id}"):
            return

        _LOGGER.warning("DS2413 %s lost its output state; it probably lost power", device_id)
        restore = self.policy("onewire", device_id) == POWER_ON_RESTORE
        targets = {channel: on and restore for channel, on in commanded.items()}
        self._ds2413[device_id] = targets
        self._save()
        self._run(self._restore_ds2413(device_id, targets), f"casait_restore_ds2413_{device_id}")

    async def _restore_ds2413(self, device_id: str, targets: Mapping[int, bool]) -> None:
        for channel, on in targets.items():
            if not await self._api.write_ds2413_state(device_id, channel, on):
                _LOGGER.warning("Could not restore DS2413 %s channel %s", device_id, channel + 1)

    def check_led(self, device_id: str, config: LEDConfig) -> None:
        """Compare an LED controller's configuration with what was last written."""

        commanded = self._led.get(device_id)
        if commanded is None or commanded == config or not self._due(f"led:{device_id}"):
            return

        _LOGGER.warning("LED controller %s lost its configuration; it probably lost power", device_id)
        target = deepcopy(commanded)
        if self.policy("onewire", device_id) == POWER_ON_OFF:
            target.state = False
        self._led[device_id] = target
        self._save()
        self._run(self._restore_led(device_id, target), f"casait_restore_led_{device_id}")

    async def _restore_led(self, device_id: str, config: LEDConfig) -> None:
        if not await self._api.write_led_config(device_id, config):
            _LOGGER.warning("Could not restore LED controller %s", device_id)
