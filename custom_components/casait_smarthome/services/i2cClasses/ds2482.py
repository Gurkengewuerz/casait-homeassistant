"""DS2482-100 I2C to 1-Wire bridge implementation.

Every 1-Wire byte costs several dependent I2C operations on this part: issue the
command, poll the status register until the 1-Wire machine goes idle, point the
read pointer at the data register, then read it. Locally that is microseconds. Over
the TCP bridge each step was a full network round trip, which made a single
temperature reading cost roughly eighty of them.

The driver therefore expresses each 1-Wire primitive as an ``I2CBatch`` and lets
the bridge walk the dependency chain, including the busy-wait. One 1-Wire byte is
one round trip, and multi-byte transfers pack as many as fit into a frame.
"""

from __future__ import annotations

import logging

_LOGGER = logging.getLogger(__name__)

# Request bytes each primitive contributes to a batch, used to chunk long transfers
# so they stay inside one frame instead of failing at the bridge.
_WRITE_BYTE_REQUEST = 9  # write_byte_data + wait_status
_READ_BYTE_REQUEST = 14  # write_byte + wait_status + write_byte_data + read_byte
_TRIPLET_REQUEST = 9  # write_byte_data + wait_status


class DS2482:
    """DS2482-100 I2C to 1-Wire bridge implementation."""

    # Status register bit definitions
    STATUS_1WB = 0x01  # 1-Wire Busy
    STATUS_PPD = 0x02  # Presence Pulse Detect
    STATUS_SD = 0x04  # Short Detected
    STATUS_LL = 0x08  # Logic Level
    STATUS_RST = 0x10  # Device Reset
    STATUS_SBR = 0x20  # Single Bit Result
    STATUS_TSB = 0x40  # Triplet Second Bit
    STATUS_DIR = 0x80  # Direction

    # DS2482 commands and registers
    CMD_RESET = 0xF0
    CMD_SET_READ_PTR = 0xE1
    CMD_WRITE_CONFIG = 0xD2
    CMD_1WIRE_RESET = 0xB4
    CMD_1WIRE_WRITE_BYTE = 0xA5
    CMD_1WIRE_READ_BYTE = 0x96
    CMD_1WIRE_SINGLE_BIT = 0x87
    CMD_1WIRE_TRIPLET = 0x78

    # DS2482 registers
    REG_STATUS = 0xF0
    REG_DATA = 0xE1
    REG_CONFIG = 0xC3

    # A 1-Wire reset takes about 1.2 ms, every other operation well under one.
    RESET_TIMEOUT_MS = 10
    BUSY_TIMEOUT_MS = 5

    def __init__(self, bus, address: int) -> None:
        """Initialize DS2482 device."""
        self.bus = bus
        self.address = address
        self._last_status = 0

    @property
    def last_status(self) -> int:
        """Return the status register as of the last completed operation."""

        return self._last_status

    def _wait_idle(self, batch, timeout_ms: int = BUSY_TIMEOUT_MS):
        """Queue a wait for the 1-Wire machine to go idle."""

        return batch.wait_status(self.address, self.STATUS_1WB, 0x00, timeout_ms)

    async def reset(self) -> bool:
        """Reset the DS2482 device."""
        # The part wants the upper nibble to be the complement of the config bits,
        # and reads back only the lower nibble.
        config = 0xF0

        try:
            batch = (
                self.bus.new_batch()
                .write_byte(self.address, self.CMD_RESET)
                .delay(1)
                .read_byte(self.address)
                .write_byte_data(self.address, self.CMD_WRITE_CONFIG, config)
                .delay(1)
                .write_byte_data(self.address, self.CMD_SET_READ_PTR, self.REG_CONFIG)
                .read_byte(self.address)
            )
            status, read_config = await self.bus.execute_batch(batch)
        except OSError:
            _LOGGER.exception("DS2482 reset error at 0x%02X", self.address)
            return False

        if not status & self.STATUS_RST:
            return False
        return (read_config & 0x0F) == (config & 0x0F)

    async def wire_reset(self) -> bool:
        """Reset the 1-Wire bus and check for presence pulse."""
        try:
            batch = self._wait_idle(
                self.bus.new_batch().write_byte(self.address, self.CMD_1WIRE_RESET),
                self.RESET_TIMEOUT_MS,
            )
            (self._last_status,) = await self.bus.execute_batch(batch)
        except OSError:
            _LOGGER.exception("1-Wire reset error at 0x%02X", self.address)
            return False

        if not self._last_status & self.STATUS_PPD:
            _LOGGER.warning("No presence pulse detected on 1-Wire bus at 0x%02X", self.address)
            return False
        return True

    async def wire_write_byte(self, byte: int) -> bool:
        """Write a byte to the 1-Wire bus."""

        return await self.wire_write_bytes([byte])

    async def wire_write_bytes(self, data: list[int]) -> bool:
        """Write several bytes to the 1-Wire bus, packing them into few round trips."""
        if not data:
            return True

        try:
            for chunk in self._chunk(data, _WRITE_BYTE_REQUEST, results_per_item=1):
                batch = self.bus.new_batch()
                for byte in chunk:
                    self._wait_idle(batch.write_byte_data(self.address, self.CMD_1WIRE_WRITE_BYTE, byte))
                statuses = await self.bus.execute_batch(batch)
                self._last_status = statuses[-1]
        except OSError:
            _LOGGER.exception("1-Wire write error at 0x%02X", self.address)
            return False
        return True

    async def wire_read_byte(self) -> int | None:
        """Read a byte from the 1-Wire bus."""

        result = await self.wire_read_bytes(1)
        return None if result is None else result[0]

    async def wire_read_bytes(self, count: int) -> list[int] | None:
        """Read several bytes from the 1-Wire bus, packing them into few round trips.

        The read pointer returns to the status register after every 1-Wire read, so
        each byte still needs its own pointer write. Batching removes the network
        cost of that, not the operation itself.
        """
        if count <= 0:
            return []

        values: list[int] = []
        try:
            for chunk in self._chunk(range(count), _READ_BYTE_REQUEST, results_per_item=2):
                batch = self.bus.new_batch()
                for _ in chunk:
                    self._wait_idle(batch.write_byte(self.address, self.CMD_1WIRE_READ_BYTE))
                    batch.write_byte_data(self.address, self.CMD_SET_READ_PTR, self.REG_DATA)
                    batch.read_byte(self.address)

                results = await self.bus.execute_batch(batch)
                # Each byte contributes a status then its data byte
                self._last_status = results[-2]
                values.extend(results[1::2])
        except OSError:
            _LOGGER.exception("1-Wire read error at 0x%02X", self.address)
            return None
        return values

    async def wire_single_bit(self, bit: bool) -> bool | None:
        """Write and read a single bit on the 1-Wire bus."""
        try:
            batch = self._wait_idle(
                self.bus.new_batch().write_byte_data(self.address, self.CMD_1WIRE_SINGLE_BIT, 0x80 if bit else 0x00)
            )
            (self._last_status,) = await self.bus.execute_batch(batch)
        except OSError:
            _LOGGER.exception("1-Wire single bit error at 0x%02X", self.address)
            return None
        return bool(self._last_status & self.STATUS_SBR)

    async def wire_triplets(self, directions: list[bool]) -> list[int] | None:
        """Run a sequence of ROM search triplets, returning one status byte each.

        The part performs the two read bits and the direction write in hardware,
        and the supplied direction only takes effect where both read bits are zero.
        A caller can therefore decide every direction of a search pass up front and
        send the whole pass as a few batches, instead of three round trips per bit.

        Each status carries the two bits read in SBR and TSB and the branch that
        was actually taken in DIR.
        """
        if not directions:
            return []

        statuses: list[int] = []
        try:
            for chunk in self._chunk(directions, _TRIPLET_REQUEST, results_per_item=1):
                batch = self.bus.new_batch()
                for direction in chunk:
                    self._wait_idle(
                        batch.write_byte_data(self.address, self.CMD_1WIRE_TRIPLET, 0x80 if direction else 0x00)
                    )
                statuses.extend(await self.bus.execute_batch(batch))
        except OSError:
            _LOGGER.exception("1-Wire triplet error at 0x%02X", self.address)
            return None

        self._last_status = statuses[-1]
        return statuses

    def _chunk(self, items, request_bytes: int, results_per_item: int) -> list[list]:
        """Split items into groups that each fit inside one bridge frame."""

        per_batch = self.bus.new_batch().capacity_for(request_bytes=request_bytes, result_bytes=results_per_item)
        collected = list(items)
        return [collected[start : start + per_batch] for start in range(0, len(collected), per_batch)]
