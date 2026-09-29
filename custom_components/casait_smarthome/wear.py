"""Switching cycles and on-time of every OM117 relay, to tell which one wears out first.

A relay is rated for a number of switching cycles, so the count says how far along
it is; the on-time adds how long its contacts carried current. Both are counted
from the output bytes the API sees - Home Assistant's own writes, the bridge's
timers and its emergency operation alike - and persisted per bridge.

Home Assistant only sees the state it reads. A relay that switched several times
while Home Assistant was away counts at most once when it comes back, and an
on-time running across a restart continues only if the relay is still on.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
import time
from typing import TYPE_CHECKING, Any

from homeassistant.helpers.storage import Store

from .const import DOMAIN, PCF8574_MAPPED_PORTS

if TYPE_CHECKING:
    from .api import CasaITApi

STORAGE_VERSION = 1
# Counters change with every switching; the store gathers them for a while.
SAVE_DELAY = 60.0


@dataclass
class RelayWear:
    """What one relay has done so far."""

    cycles: int = 0
    # Closed on-periods, in seconds.
    on_seconds: float = 0.0
    # Wall-clock start of the running on-period, None while off or unknown.
    on_since: float | None = None
    # Whether the relay was seen on at the last reading; None before the first.
    on: bool | None = None

    def on_time(self, now: float) -> float:
        """Return the on-time including a running period, in seconds."""

        running = now - self.on_since if self.on and self.on_since is not None else 0.0
        return self.on_seconds + max(0.0, running)


class CasaITRelayWear:
    """Count switching cycles and on-time per OM117 port."""

    def __init__(self, api: CasaITApi) -> None:
        """Bind the counters to the API's store."""

        self._store: Store[dict[str, Any]] = Store(api.hass, STORAGE_VERSION, f"{DOMAIN}.{api.entry_id}.wear")
        self._relays: dict[tuple[int, int], RelayWear] = {}

    async def async_load(self) -> None:
        """Load the counters saved before the last shutdown."""

        data = await self._store.async_load() or {}
        for address, ports in data.get("om117", {}).items():
            for port, raw in ports.items():
                on_since = raw.get("on_since")
                self._relays[int(address), int(port)] = RelayWear(
                    cycles=int(raw.get("cycles", 0)),
                    on_seconds=float(raw.get("on_seconds", 0.0)),
                    on_since=float(on_since) if on_since is not None else None,
                )

    def _data_to_save(self) -> dict[str, Any]:
        om117: dict[str, dict[str, Any]] = {}
        for (address, port), wear in sorted(self._relays.items()):
            om117.setdefault(str(address), {})[str(port)] = {
                "cycles": wear.cycles,
                "on_seconds": round(wear.on_seconds, 1),
                "on_since": wear.on_since,
            }
        return {"om117": om117}

    def observe(self, address: int, port_states: Sequence[int]) -> None:
        """Take one reading of an OM117; its outputs are active low."""

        now = time.time()
        changed = False
        for port, bit in PCF8574_MAPPED_PORTS.items():
            if bit >= len(port_states):
                continue
            on = port_states[bit] == 0
            wear = self._relays.setdefault((address, port), RelayWear())
            if wear.on is None:
                # First reading since the start: a relay still on continues the
                # on-period saved before, anything else starts from here.
                wear.on = on
                wear.on_since = (wear.on_since or now) if on else None
                continue
            if on == wear.on:
                continue
            if on:
                wear.cycles += 1
                wear.on_since = now
            elif wear.on_since is not None:
                wear.on_seconds += max(0.0, now - wear.on_since)
                wear.on_since = None
            wear.on = on
            changed = True
        if changed:
            self._store.async_delay_save(self._data_to_save, SAVE_DELAY)

    def cycles(self, address: int, port: int) -> int:
        """Return how often the relay switched on."""

        wear = self._relays.get((address, port))
        return wear.cycles if wear else 0

    def on_hours(self, address: int, port: int) -> float:
        """Return how long the relay has been on, in hours."""

        wear = self._relays.get((address, port))
        return round(wear.on_time(time.time()) / 3600, 3) if wear else 0.0

    def diagnostics(self) -> dict[str, Any]:
        """Return the counters for the diagnostics download."""

        return self._data_to_save()
