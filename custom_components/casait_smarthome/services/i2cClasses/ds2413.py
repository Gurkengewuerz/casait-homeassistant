"""DS2413 Dual Channel Addressable Switch.

Two-channel programmable I/O port with open-drain outputs and input-sensing
capability. Each channel can be independently configured and accessed through
a 1-Wire interface.

Key Features:
- Two independently controlled I/O pins
- Open-drain output drivers (external pull-up required)
- Input voltage sensing capability
- Strong pull-down (4mA @ 0.4V)
- Verification of state changes
- Unique 64-bit serial number

This driver reads and writes the pins; when to read is the caller's business.
Pin levels are reported raw: an output that is switched on pulls its pin low,
and an input with a pull-up reads low while its contact is closed.

Each I/O pin features:
- Output: Strong pull-down / floating
- Input: Voltage sense capability
- Activity indicator
- State verification

References:
- Datasheet: https://datasheets.maximintegrated.com/en/ds/DS2413.pdf
"""

from __future__ import annotations

import logging

_LOGGER = logging.getLogger(__name__)


class DS2413:
    """DS2413 dual channel addressable switches on one 1-Wire bus."""

    CMD_PIO_ACCESS_READ = 0xF5
    CMD_PIO_ACCESS_WRITE = 0x5A
    CMD_PIO_WRITE_VALIDATE = 0xA5

    def __init__(self, bus_interface) -> None:
        """Initialize for one bus."""
        self.bus = bus_interface

    async def read_status(self, device_id: str) -> int | None:
        """Read the PIO status byte, validated against its complement nibble.

        Bit 0 is the PIOA pin level, bit 1 the PIOA output latch, bits 2 and 3
        the same for PIOB. A latch bit of 1 means the output transistor is off.
        """

        if not await self.bus.select_device(device_id):
            return None
        if not await self.bus.bridge.wire_write_byte(self.CMD_PIO_ACCESS_READ):
            return None
        state = await self.bus.bridge.wire_read_byte()
        if state is None:
            return None
        if (state >> 4) != (~state & 0x0F):
            _LOGGER.debug("Invalid DS2413 status %02X from %s", state, device_id)
            return None
        return state

    async def read_ports(self, device_id: str) -> tuple[bool, bool] | None:
        """Return the (A, B) pin levels, True meaning high."""

        state = await self.read_status(device_id)
        if state is None:
            return None
        return bool(state & 0x01), bool(state & 0x04)

    async def set_state(self, device_id: str, channel: int, value: bool) -> tuple[bool, bool] | None:
        """Switch one output channel on or off and return the resulting pin levels.

        On means the output transistor conducts, which pulls the pin low. The
        other channel keeps its latch; it is taken from the latch bits, not the
        pin levels, so an input channel is never driven by accident.
        """

        state = await self.read_status(device_id)
        if state is None:
            return None
        on = [not state & 0x02, not state & 0x08]
        on[channel] = value
        # Bits 0 and 1 are the A and B latches (1 = off); the upper six must be 1.
        data = 0xFC | (0 if on[0] else 0x01) | (0 if on[1] else 0x02)

        for _ in range(2):
            if not await self.bus.select_device(device_id):
                return None
            if not await self.bus.bridge.wire_write_bytes([self.CMD_PIO_ACCESS_WRITE, data, ~data & 0xFF]):
                continue
            confirm = await self.bus.bridge.wire_read_byte()
            if confirm == 0xAA:
                break
            _LOGGER.debug("DS2413 %s did not confirm the write: %s", device_id, confirm)
        return await self.read_ports(device_id)
