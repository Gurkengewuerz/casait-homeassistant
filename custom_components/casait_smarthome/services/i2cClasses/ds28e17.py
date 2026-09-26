"""DS28E17 1-Wire to I2C bridge.

The DS28E17 hangs an I2C master off a 1-Wire slave. Every I2C transfer is one
1-Wire transaction: select the chip, send a CRC16-protected command packet, poll
until the chip has finished on the I2C side, then collect a status byte and any
data it read.

Commands used here:

- ``0x4B`` write data with stop
- ``0x87`` read data with stop
- ``0x2D`` write, repeated start, read data with stop - the register read that
  chips like the VEML7700 need, which two separate transfers cannot express

The status byte tells whether the I2C slave acknowledged its address, which is
what makes the bridge usable for probing which chips are fitted.

References:
- Datasheet: https://www.analog.com/media/en/technical-documentation/data-sheets/ds28e17.pdf
"""

from __future__ import annotations

import asyncio
import logging

_LOGGER = logging.getLogger(__name__)

# Status byte bits
STATUS_CRC_ERROR = 0x01
STATUS_ADDRESS_NACK = 0x02
STATUS_START_ERROR = 0x08

# Busy polling. At 400 kHz even the longest transfer used here finishes in well
# under a millisecond, so running out of polls means the I2C side is stuck.
MAX_BUSY_POLLS = 100
BUSY_POLL_DELAY = 0.001


class DS28E17Error(Exception):
    """A DS28E17 transaction failed on the 1-Wire side or was malformed."""


class DS28E17Nack(DS28E17Error):
    """The I2C slave did not acknowledge its address or a data byte.

    Kept apart from other failures because it is an answer, not a fault: a chip
    that is not fitted, or a Sensirion sensor whose result is not ready yet,
    says so by not acknowledging.
    """


class DS28E17:
    """DS28E17 1-Wire to I2C bridge."""

    CMD_WRITE_DATA = 0x4B  # Write data with stop
    CMD_WRITE_DATA_NO_STOP = 0x5A  # Write data only
    CMD_WRITE_READ_DATA = 0x2D  # Write, read data with stop
    CMD_READ_DATA = 0x87  # Read data with stop
    CMD_READ_DATA_NO_STOP = 0x91  # Read data only
    CMD_WRITE_CONFIG = 0xD2  # Write configuration

    def __init__(self, bus_interface) -> None:
        """Initialize the bridge on one 1-Wire bus."""
        self.bus = bus_interface

    # ------------------------------------------------------------------
    # Raising API
    # ------------------------------------------------------------------

    async def write(self, device_id: str, address: int, data: bytes) -> None:
        """Write bytes to an I2C slave, ending with a stop condition."""

        _check_address(address)
        _check_length(len(data))
        packet = bytes([self.CMD_WRITE_DATA, address << 1, len(data)]) + data
        (status, write_status), _ = await self._transact(device_id, packet, status_bytes=2)
        _raise_for_status(status, address, write_status)

    async def read(self, device_id: str, address: int, count: int) -> bytes:
        """Read bytes from an I2C slave, ending with a stop condition."""

        _check_address(address)
        _check_length(count)
        packet = bytes([self.CMD_READ_DATA, (address << 1) | 0x01, count])
        (status,), data = await self._transact(device_id, packet, status_bytes=1, read_count=count)
        _raise_for_status(status, address)
        return data

    async def write_read(self, device_id: str, address: int, data: bytes, count: int) -> bytes:
        """Write bytes, issue a repeated start and read the answer."""

        _check_address(address)
        _check_length(len(data))
        _check_length(count)
        packet = bytes([self.CMD_WRITE_READ_DATA, address << 1, len(data)]) + data + bytes([count])
        (status, write_status), result = await self._transact(device_id, packet, status_bytes=2, read_count=count)
        _raise_for_status(status, address, write_status)
        return result

    async def probe(self, device_id: str, address: int, data: bytes) -> bool:
        """Return whether a slave acknowledges a write of ``data``.

        A NACK means no chip answers at that address. Any other failure is
        raised, because a bus fault must not be mistaken for an empty socket.
        """

        try:
            await self.write(device_id, address, data)
        except DS28E17Nack:
            return False
        return True

    # ------------------------------------------------------------------
    # Boolean API kept for the LED controller
    # ------------------------------------------------------------------

    async def write_data(self, device_id: str, address: int, data: bytes) -> bool:
        """Write data to an I2C device, returning success instead of raising."""

        try:
            await self.write(device_id, address, data)
        except DS28E17Error as err:
            _LOGGER.debug("DS28E17 %s write to 0x%02X failed: %s", device_id, address, err)
            return False
        return True

    async def read_data(self, device_id: str, address: int, num_bytes: int) -> bytes | None:
        """Read data from an I2C device, returning None instead of raising."""

        try:
            return await self.read(device_id, address, num_bytes)
        except DS28E17Error as err:
            _LOGGER.debug("DS28E17 %s read from 0x%02X failed: %s", device_id, address, err)
            return None

    # ------------------------------------------------------------------
    # Transport
    # ------------------------------------------------------------------

    async def _transact(
        self,
        device_id: str,
        packet: bytes,
        *,
        status_bytes: int,
        read_count: int = 0,
    ) -> tuple[tuple[int, ...], bytes]:
        """Run one bridge command and return its status bytes and any data read."""

        crc = ~self.bus.calc_crc16(packet) & 0xFFFF
        framed = packet + bytes([crc & 0xFF, crc >> 8])

        # select_device resets the bus itself before addressing the chip.
        if not await self.bus.select_device(device_id):
            raise DS28E17Error(f"cannot select {device_id}")
        if not await self.bus.bridge.wire_write_bytes(list(framed)):
            raise DS28E17Error("command packet not sent")

        for _ in range(MAX_BUSY_POLLS):
            busy = await self.bus.bridge.wire_single_bit(True)
            if busy is None:
                raise DS28E17Error("busy poll failed")
            if not busy:
                break
            await asyncio.sleep(BUSY_POLL_DELAY)
        else:
            raise DS28E17Error("I2C transfer did not finish")

        status = await self.bus.bridge.wire_read_bytes(status_bytes)
        if status is None or len(status) != status_bytes:
            raise DS28E17Error("status not received")

        if not read_count:
            return tuple(status), b""

        # The chip only clocks out data when the transfer went through; on a
        # NACK the status is all there is.
        if status[0] & (STATUS_ADDRESS_NACK | STATUS_CRC_ERROR | STATUS_START_ERROR):
            return tuple(status), b""
        if status_bytes > 1 and status[1]:
            return tuple(status), b""

        values = await self.bus.bridge.wire_read_bytes(read_count)
        if values is None or len(values) != read_count:
            raise DS28E17Error(f"expected {read_count} data bytes")
        return tuple(status), bytes(values)


def _check_address(address: int) -> None:
    if not 0 <= address <= 0x7F:
        raise ValueError(f"invalid I2C address 0x{address:02X}")


def _check_length(length: int) -> None:
    if not 1 <= length <= 255:
        raise ValueError(f"invalid I2C transfer length {length}")


def _raise_for_status(status: int, address: int, write_status: int = 0) -> None:
    """Turn a status byte pair into an exception, or return on success."""

    if status & STATUS_CRC_ERROR:
        raise DS28E17Error("bridge reported a CRC error")
    if status & STATUS_START_ERROR:
        raise DS28E17Error("bridge could not start the I2C transfer")
    if status & STATUS_ADDRESS_NACK:
        raise DS28E17Nack(f"no acknowledge from 0x{address:02X}")
    if write_status:
        raise DS28E17Nack(f"0x{address:02X} did not acknowledge byte {write_status}")
