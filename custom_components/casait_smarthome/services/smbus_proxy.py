"""Asyncio client for the casaIT SMBus TCP bridge.

The bridge is a network-connected microcontroller (for example an ESP32 with a
W5500) that performs I2C operations on behalf of Home Assistant. Its method
names follow smbus2 so the drivers read like ordinary I2C code, but every call
is a coroutine on the event loop - there is no socket in an executor thread.

Requests are answered strictly in order and only one is in flight at a time. The
bridge also pushes events on its own - what changed on the modules it watches - so
a reader task owns the socket and hands responses and events to their receivers.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import contextlib
from dataclasses import dataclass, field
import logging
import socket
import time

_LOGGER = logging.getLogger(__name__)

# Protocol commands (matching cb32.cpp)
CMD_WRITE_BYTE = 0x01
CMD_WRITE_BYTE_DATA = 0x02
CMD_READ_BYTE = 0x03
CMD_READ_BYTE_DATA = 0x04
CMD_WRITE_I2C_BLOCK_DATA = 0x05
CMD_READ_I2C_BLOCK = 0x06
CMD_BATCH = 0x07
CMD_PING = 0x11
# Sets PCF8574 bits and has the bridge put them back after a duration, so a cover
# stops on time even when Home Assistant is late or gone.
CMD_TIMED_OUTPUT = 0x14
# The bridge reads the configured modules on its own and pushes what changed, so
# Home Assistant sends nothing while nothing happens and an input edge arrives one
# network hop after the bridge sampled it.
CMD_WATCH_CONFIG = 0x15
CMD_WATCH_ACK = 0x16
# Direction pairs the bridge never switches on together, with a pause before reversing.
CMD_INTERLOCK = 0x17

# Batch sub-opcodes, numbered like the top-level commands so both sides stay readable
BOP_WRITE_BYTE = 0x01
BOP_WRITE_BYTE_DATA = 0x02
BOP_READ_BYTE = 0x03
BOP_READ_BYTE_DATA = 0x04
BOP_READ_BLOCK = 0x06
BOP_WAIT_STATUS = 0x07
BOP_DELAY = 0x08
# Whole 1-Wire primitives run on a DS2482 by the bridge, so a reset, a ROM select, a
# command and its answer fit into one round trip.
BOP_OW_RESET = 0x09
BOP_OW_WRITE = 0x0A
BOP_OW_READ = 0x0B

# Frames the bridge sends unasked start with this byte instead of a status.
EVENT_MARKER = 0xFE
MAINTENANCE_NOTICE = b"\xff\xee\x01"

# Capability bits in the ping. The integration needs all of them.
CAP_WATCH = 0x01
CAP_ONEWIRE = 0x02
CAP_INTERLOCK = 0x04
REQUIRED_CAPABILITIES = CAP_WATCH | CAP_ONEWIRE | CAP_INTERLOCK

# What a watched module is to the bridge.
WATCH_KIND_PCF_INPUT = 1
WATCH_KIND_PCF_OUTPUT = 2
WATCH_KIND_DM117 = 3
MAX_WATCH_MODULES = 32
# Set on an event when the bridge had to drop older ones.
WATCH_FLAG_OVERFLOW = 0x01
# Bridge-side limits of the interlock.
MAX_INTERLOCK_PAIRS = 4

# The bridge frames both directions as [len][payload][crc] with a single length byte
# and a 128 byte buffer, and a block read or batch spends one payload byte on status.
MAX_FRAME_PAYLOAD = 126
MAX_BLOCK_READ = MAX_FRAME_PAYLOAD - 1
MAX_BATCH_RESULTS = MAX_FRAME_PAYLOAD - 1

# The bridge clamps these too; keeping the client honest makes overruns visible here
# rather than as a silently shortened wait on the wire.
MAX_WAIT_STATUS_MS = 50
MAX_DELAY_MS = 10

# The bridge refuses longer timers.
MAX_TIMED_OUTPUT_MS = 3_600_000
# [status][command][0xAA][boot id, 4 bytes][uptime in s, 4 bytes][version length][version]
PING_HEADER_SIZE = 12
# After the version: [capabilities][fast sweep us, 2][slow sweep us, 2][I2C retries, 4]
# [interlock refusals, 4]
PING_TAIL_SIZE = 13

# Default configuration from environment variables
DEFAULT_PORT = 8555
DEFAULT_TIMEOUT = 2.0

# Adaptive spacing between consecutive frames. The bridge drops frames when it is
# flooded, so the client throttles itself. Rather than paying a fixed worst-case
# price on every round trip, start fast and back off only when the link complains.
MIN_SEND_INTERVAL = 0.001
MAX_SEND_INTERVAL = 0.005
SEND_INTERVAL_STEP = 0.001
# Consecutive error-free frames required before the spacing is relaxed one step.
SEND_INTERVAL_RECOVERY_FRAMES = 50
# Pause between send attempts, indexed by the attempt that just failed. This runs
# while the I/O lock is held, so it delays every other caller including the input
# poll loop; it exists to let the bridge settle, not to wait out an outage.
RETRY_BACKOFF = (0.05, 0.15)
# Pause after the bridge reports maintenance mode. Same constraint as above.
MAINTENANCE_BACKOFF = 0.5


class SMBusProxyError(Exception):
    """Exception raised for SMBus proxy errors."""


@dataclass(frozen=True)
class BridgeInfo:
    """What a ping tells about the bridge.

    ``boot_id`` changes with every start of the bridge, so a reconnect with the same
    id was only the network. ``version`` is the release tag the firmware was built
    from, or a commit hash for a build between releases.
    """

    boot_id: int
    uptime_s: int
    version: str
    capabilities: int = REQUIRED_CAPABILITIES
    # How long the bridge's last sweep over the inputs, and over everything, took.
    fast_sweep_us: int = 0
    slow_sweep_us: int = 0
    # Second I2C attempts since the bridge started, and writes the interlock cut.
    i2c_retries: int = 0
    interlock_refusals: int = 0


@dataclass(frozen=True)
class WatchEntry:
    """One module reading the bridge pushed.

    ``data`` is the port byte of a PCF8574 or the complete read response of a
    DM117; empty means the bridge could not read the module any more.
    """

    seq: int
    index: int
    data: bytes


@dataclass(frozen=True)
class WatchEvent:
    """One event frame: its flags and the readings it carries, oldest first."""

    flags: int
    entries: list[WatchEntry] = field(default_factory=list)


def parse_watch_event(payload: bytes) -> WatchEvent:
    """Decode an event frame, raising ValueError when it is malformed."""

    if len(payload) < 3 or payload[0] != EVENT_MARKER:
        raise ValueError("Not a watch event")
    flags, count = payload[1], payload[2]
    entries: list[WatchEntry] = []
    pos = 3
    for _ in range(count):
        if pos + 3 > len(payload):
            raise ValueError("Truncated watch event")
        seq, index, size = payload[pos : pos + 3]
        pos += 3
        if pos + size > len(payload):
            raise ValueError("Truncated watch entry")
        entries.append(WatchEntry(seq, index, bytes(payload[pos : pos + size])))
        pos += size
    return WatchEvent(flags, entries)


class BridgeFirmwareError(SMBusProxyError):
    """The bridge answers, but runs firmware this integration cannot work with."""


class I2CBatchError(OSError):
    """A batch failed, naming the operation that failed where the bridge reported it.

    Callers that pack several independent devices into one batch need to know which
    one failed so a single bad module does not discard the others' results.
    """

    def __init__(self, message: str, op_index: int | None = None) -> None:
        """Store the failing operation index alongside the message."""

        super().__init__(message)
        self.op_index = op_index


class I2CBatch:
    """A list of I2C operations the bridge executes in one round trip.

    Every operation over this transport costs a full network round trip, so
    drivers that need several dependent operations for one logical read pay for
    the network far more than for the bus. Collecting those operations here moves
    the dependency chain onto the bridge, where each step costs microseconds.

    Methods are chainable. Build a batch, hand it to ``SMBus.execute_batch``, and
    read back one entry per result-producing operation, in order.
    """

    def __init__(self) -> None:
        """Start an empty batch."""

        self._payload = bytearray([CMD_BATCH])
        self._result_count = 0
        self._op_count = 0

    def __len__(self) -> int:
        """Return the number of queued operations."""

        return self._op_count

    def __bytes__(self) -> bytes:
        """Return the wire payload for this batch."""

        return bytes(self._payload)

    @property
    def result_count(self) -> int:
        """Return how many result bytes the bridge will send back."""

        return self._result_count

    def capacity_for(self, *, request_bytes: int, result_bytes: int) -> int:
        """Return how many more operations of this shape still fit in the frame.

        Drivers that transfer a variable number of bytes use this to decide where
        to split, instead of discovering the overflow as a failed batch.
        """

        return min(
            (MAX_FRAME_PAYLOAD - len(self._payload)) // request_bytes,
            (MAX_BATCH_RESULTS - self._result_count) // result_bytes,
        )

    def _add(self, op: bytes, results: int = 0) -> I2CBatch:
        """Append one operation and account for its result bytes."""

        if len(self._payload) + len(op) > MAX_FRAME_PAYLOAD:
            raise ValueError(f"Batch exceeds the {MAX_FRAME_PAYLOAD} byte frame payload")
        if self._result_count + results > MAX_BATCH_RESULTS:
            raise ValueError(f"Batch exceeds the {MAX_BATCH_RESULTS} byte result limit")

        self._payload.extend(op)
        self._result_count += results
        self._op_count += 1
        return self

    def write_byte(self, addr: int, value: int) -> I2CBatch:
        """Queue a single byte write. Produces no result."""

        return self._add(bytes([BOP_WRITE_BYTE, addr, value]))

    def write_byte_data(self, addr: int, reg: int, value: int) -> I2CBatch:
        """Queue a register write. Produces no result."""

        return self._add(bytes([BOP_WRITE_BYTE_DATA, addr, reg, value]))

    def read_byte(self, addr: int) -> I2CBatch:
        """Queue a single byte read. Produces one result byte."""

        return self._add(bytes([BOP_READ_BYTE, addr]), results=1)

    def read_byte_data(self, addr: int, reg: int) -> I2CBatch:
        """Queue a register read. Produces one result byte."""

        return self._add(bytes([BOP_READ_BYTE_DATA, addr, reg]), results=1)

    def read_block(self, addr: int, count: int) -> I2CBatch:
        """Queue a multi-byte read in one I2C transaction. Produces count results."""

        if not 1 <= count <= MAX_BATCH_RESULTS:
            raise ValueError(f"Block read count must be between 1 and {MAX_BATCH_RESULTS}, got {count}")
        return self._add(bytes([BOP_READ_BLOCK, addr, count]), results=count)

    def wait_status(
        self,
        addr: int,
        mask: int,
        expected: int,
        timeout_ms: int = MAX_WAIT_STATUS_MS,
    ) -> I2CBatch:
        """Queue a poll of a status register until it matches, on the bridge.

        The batch fails at this operation if the timeout expires. Produces one
        result byte carrying the last status read, which callers need for the
        other flags in the same register.
        """

        if not 0 <= timeout_ms <= MAX_WAIT_STATUS_MS:
            raise ValueError(f"Wait timeout must be between 0 and {MAX_WAIT_STATUS_MS} ms, got {timeout_ms}")
        return self._add(bytes([BOP_WAIT_STATUS, addr, mask, expected, timeout_ms]), results=1)

    def delay(self, ms: int) -> I2CBatch:
        """Queue a fixed pause on the bridge. Produces no result."""

        if not 0 <= ms <= MAX_DELAY_MS:
            raise ValueError(f"Delay must be between 0 and {MAX_DELAY_MS} ms, got {ms}")
        return self._add(bytes([BOP_DELAY, ms]))

    def ow_reset(self, addr: int) -> I2CBatch:
        """Queue a 1-Wire reset on a DS2482. Fails the batch without a presence pulse."""

        return self._add(bytes([BOP_OW_RESET, addr]))

    def ow_write(self, addr: int, data: bytes | list[int]) -> I2CBatch:
        """Queue 1-Wire byte writes on a DS2482. Produces no result."""

        data = bytes(data)
        if not 1 <= len(data) <= 0xFF:
            raise ValueError(f"1-Wire write must carry 1 to 255 bytes, got {len(data)}")
        return self._add(bytes([BOP_OW_WRITE, addr, len(data)]) + data)

    def ow_read(self, addr: int, count: int) -> I2CBatch:
        """Queue 1-Wire byte reads on a DS2482. Produces count results."""

        if not 1 <= count <= MAX_BATCH_RESULTS:
            raise ValueError(f"1-Wire read count must be between 1 and {MAX_BATCH_RESULTS}, got {count}")
        return self._add(bytes([BOP_OW_READ, addr, count]), results=count)


class SMBus:
    """Connection to one casaIT SMBus bridge.

    Create it with ``await SMBus.connect(host, port, timeout)``; the constructor
    itself does no I/O. A lost connection is re-established transparently on
    the next call.
    """

    def __init__(
        self,
        host: str = "192.168.1.100",
        port: int | None = None,
        timeout: float | None = None,
        max_send_interval: float = MAX_SEND_INTERVAL,
    ) -> None:
        """Prepare a connection without opening it yet."""

        self.host = host
        self.port = port or DEFAULT_PORT
        self.timeout = timeout or DEFAULT_TIMEOUT
        self._max_send_interval = max(MIN_SEND_INTERVAL, max_send_interval)
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._io_lock = asyncio.Lock()
        self._last_send: float = 0.0
        self._min_send_interval = MIN_SEND_INTERVAL
        self._consecutive_ok = 0
        self._crc_errors = 0
        self._timeouts = 0
        self._io_errors = 0
        self._frames = 0
        self._last_rtt = 0.0
        # Counts the TCP connections opened so far. The bridge forgets per-client
        # state such as who receives its events with every connection, so callers
        # compare this against the value they set that state up under.
        self.connection_generation = 0
        # Owns the socket's read side and routes each frame to its receiver.
        self._read_task: asyncio.Task[None] | None = None
        self._pending: asyncio.Future[bytes] | None = None
        # Receives every event frame the bridge pushes, and learns when the
        # connection went away, which is when the bridge stops pushing.
        self.event_handler: Callable[[bytes], None] | None = None
        self.disconnect_handler: Callable[[], None] | None = None

    @classmethod
    async def connect(
        cls,
        host: str,
        port: int | None = None,
        timeout: float | None = None,
        max_send_interval: float = MAX_SEND_INTERVAL,
    ) -> SMBus:
        """Open a connection to the bridge, raising SMBusProxyError if it fails."""

        bus = cls(host, port, timeout, max_send_interval)
        async with bus._io_lock:
            await bus._connect()
        return bus

    async def _connect(self) -> None:
        """Open the TCP connection. Must be called while holding ``_io_lock``."""

        if self._writer is not None:
            return
        try:
            async with asyncio.timeout(self.timeout):
                reader, writer = await asyncio.open_connection(self.host, self.port)
        except (OSError, TimeoutError) as err:
            _LOGGER.debug("Failed to connect to SMBus bridge %s:%s: %s", self.host, self.port, err)
            raise SMBusProxyError(f"Failed to connect to SMBus bridge at {self.host}:{self.port}: {err}") from err

        # Frames are tiny and latency-bound; Nagle would hold every one back.
        if (sock := writer.get_extra_info("socket")) is not None:
            with contextlib.suppress(OSError):
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        self._reader, self._writer = reader, writer
        self.connection_generation += 1
        self._ensure_reader()
        _LOGGER.info("Connected to SMBus bridge at %s:%s", self.host, self.port)

    def _ensure_reader(self) -> None:
        """Start the task that reads the current connection, if it is not running."""

        if self._reader is None or (self._read_task is not None and not self._read_task.done()):
            return
        self._read_task = asyncio.get_running_loop().create_task(
            self._read_loop(self._reader), name="casait_bridge_reader"
        )

    async def _read_loop(self, reader: asyncio.StreamReader) -> None:
        """Read frames until the connection breaks, routing each to its receiver."""

        try:
            while True:
                payload = await self._receive_frame(idle=True, reader=reader)
                if payload[:1] == bytes([EVENT_MARKER]):
                    if self.event_handler is not None:
                        try:
                            self.event_handler(payload)
                        except Exception:
                            _LOGGER.exception("Error handling a bridge event")
                    continue
                if payload[:3] == MAINTENANCE_NOTICE:
                    raise SMBusProxyError("Bridge in maintenance mode")  # noqa: TRY301
                if self._pending is not None and not self._pending.done():
                    self._pending.set_result(payload)
                else:
                    _LOGGER.debug("Dropping a bridge response nobody waits for")
        except asyncio.CancelledError:
            raise
        except Exception as err:  # noqa: BLE001
            if reader is not self._reader:
                return
            _LOGGER.debug("Bridge connection lost: %s", err)
            if self._pending is not None and not self._pending.done():
                self._pending.set_exception(
                    err if isinstance(err, SMBusProxyError) else SMBusProxyError(f"Communication error: {err}")
                )
            self._read_task = None
            self._reset_socket()

    @staticmethod
    def _calc_crc8(data: bytes) -> int:
        """Compute CRC8 with polynomial 0x07 and init 0x00."""

        crc = 0
        for byte in data:
            crc ^= byte
            for _ in range(8):
                crc = ((crc << 1) ^ 0x07) & 0xFF if crc & 0x80 else (crc << 1) & 0xFF
        return crc

    async def _recv_exact(
        self, size: int, *, timeout: bool = True, reader: asyncio.StreamReader | None = None
    ) -> bytes:
        """Receive exactly ``size`` bytes or raise."""

        reader = reader or self._reader
        if reader is None:
            raise SMBusProxyError("Not connected")
        try:
            if not timeout:
                return await reader.readexactly(size)
            async with asyncio.timeout(self.timeout):
                return await reader.readexactly(size)
        except TimeoutError as err:
            self._timeouts += 1
            raise SMBusProxyError("Communication timeout") from err
        except asyncio.IncompleteReadError as err:
            raise SMBusProxyError("Communication error: connection closed") from err

    async def _receive_frame(self, *, idle: bool = False, reader: asyncio.StreamReader | None = None) -> bytes:
        """Read a frame [len][payload][crc8].

        With ``idle`` the wait for the frame to start has no timeout: between
        requests the bridge only speaks when something happened. Once a frame has
        started, the rest has to follow promptly.
        """

        length_bytes = await self._recv_exact(1, timeout=not idle, reader=reader)
        frame_len = length_bytes[0]
        payload = await self._recv_exact(frame_len, reader=reader) if frame_len else b""
        crc_recv = (await self._recv_exact(1, reader=reader))[0]

        frame = length_bytes + payload
        if crc_recv != self._calc_crc8(frame):
            self._crc_errors += 1
            raise SMBusProxyError("CRC mismatch in bridge response")
        return payload

    def _note_success(self, rtt: float) -> None:
        """Record a clean round trip and relax the spacing once it looks safe."""

        self._frames += 1
        self._last_rtt = rtt
        self._consecutive_ok += 1

        if self._consecutive_ok >= SEND_INTERVAL_RECOVERY_FRAMES and self._min_send_interval > MIN_SEND_INTERVAL:
            self._consecutive_ok = 0
            self._min_send_interval = max(MIN_SEND_INTERVAL, self._min_send_interval - SEND_INTERVAL_STEP)
            _LOGGER.debug("Relaxing SMBus send spacing to %.1f ms", self._min_send_interval * 1000)

    def _note_failure(self) -> None:
        """Record a failed round trip and back the spacing off one step."""

        self._consecutive_ok = 0
        if self._min_send_interval < self._max_send_interval:
            self._min_send_interval = min(self._max_send_interval, self._min_send_interval + SEND_INTERVAL_STEP)
            _LOGGER.debug("Backing SMBus send spacing off to %.1f ms", self._min_send_interval * 1000)

    @property
    def stats(self) -> dict[str, float | int]:
        """Return transport counters for diagnostics."""

        return {
            "send_interval_ms": round(self._min_send_interval * 1000, 3),
            "max_send_interval_ms": round(self._max_send_interval * 1000, 3),
            "last_roundtrip_ms": round(self._last_rtt * 1000, 3),
            "frames": self._frames,
            "crc_errors": self._crc_errors,
            "timeouts": self._timeouts,
            "io_errors": self._io_errors,
            "connected": self._writer is not None,
        }

    async def _send_command(self, payload: bytes) -> bytes:
        """Send a framed command and return the payload of the response."""

        async with self._io_lock:
            for attempt in range(3):
                try:
                    await self._connect()
                    assert self._writer is not None

                    delta = time.monotonic() - self._last_send
                    if delta < self._min_send_interval:
                        await asyncio.sleep(self._min_send_interval - delta)
                    send_start = time.monotonic()
                    self._last_send = send_start

                    self._ensure_reader()
                    future: asyncio.Future[bytes] = asyncio.get_running_loop().create_future()
                    self._pending = future
                    try:
                        self._writer.write(self._frame(payload))
                        await self._writer.drain()
                        async with asyncio.timeout(self.timeout):
                            response = await future
                    except TimeoutError as err:
                        self._timeouts += 1
                        raise SMBusProxyError("Communication timeout") from err
                    finally:
                        self._pending = None
                    rtt = time.monotonic() - send_start
                except SMBusProxyError as err:
                    self._note_failure()
                    _LOGGER.warning("SMBus proxy error (attempt %d/3): %s", attempt + 1, err)
                    self._reset_socket()
                    if attempt < 2:
                        # The bridge may signal maintenance; back off to avoid a
                        # busy reconnect loop.
                        maintenance = "maintenance" in str(err)
                        await asyncio.sleep(MAINTENANCE_BACKOFF if maintenance else RETRY_BACKOFF[attempt])
                        continue
                    raise
                except OSError as err:
                    self._io_errors += 1
                    self._note_failure()
                    _LOGGER.warning("SMBus proxy communication error (attempt %d/3): %s", attempt + 1, err)
                    self._reset_socket()
                    if attempt < 2:
                        await asyncio.sleep(RETRY_BACKOFF[attempt])
                        continue
                    raise SMBusProxyError(f"Communication error: {err}") from err
                else:
                    self._note_success(rtt)
                    return response
            return b""

    def _frame(self, payload: bytes) -> bytes:
        """Return a payload framed as [len][payload][crc8]."""

        frame = bytes([len(payload)]) + payload
        return frame + bytes([self._calc_crc8(frame)])

    def send_nowait(self, payload: bytes) -> bool:
        """Send a command the bridge does not answer, without waiting for anything.

        Safe next to a request in flight: the bridge handles frames in order and
        this one produces no response that could be mistaken for another's.
        """

        if self._writer is None:
            return False
        try:
            self._writer.write(self._frame(payload))
        except (OSError, RuntimeError) as err:
            _LOGGER.debug("Could not send to the bridge: %s", err)
            return False
        return True

    def _reset_socket(self) -> None:
        """Drop the current connection so the next call reconnects."""

        was_connected = self._writer is not None
        if self._writer is not None:
            with contextlib.suppress(Exception):
                self._writer.close()
        self._reader = None
        self._writer = None
        task, self._read_task = self._read_task, None
        if task is not None and task is not asyncio.current_task():
            task.cancel()
        if was_connected and self.disconnect_handler is not None:
            self.disconnect_handler()

    async def close(self) -> None:
        """Close the connection to the bridge."""

        writer = self._writer
        self.disconnect_handler = None
        self._reset_socket()
        if writer is not None:
            with contextlib.suppress(Exception):
                await writer.wait_closed()
            _LOGGER.debug("SMBus proxy connection closed")

    async def write_quick(self, addr: int):
        """Perform a quick write to probe device presence.

        This is implemented as a read_byte operation, which will
        succeed if a device responds at the address.

        Args:
            addr: I2C address (7-bit)

        Raises:
            OSError: If device doesn't respond (matching smbus2 behavior)
        """
        try:
            await self.read_byte(addr)
        except SMBusProxyError as e:
            raise OSError(f"Device at address 0x{addr:02X} not responding") from e

    async def read_byte(self, addr: int) -> int:
        """Read a single byte from device.

        Args:
            addr: I2C address (7-bit)

        Returns:
            Byte value read from device

        Raises:
            OSError: If read fails (matching smbus2 behavior)
        """
        try:
            response = await self._send_command(bytes([CMD_READ_BYTE, addr]))
            if len(response) >= 1 and response[0] == 0x00 and len(response) >= 2:
                return response[1]
            raise OSError(f"Read byte failed for address 0x{addr:02X}")
        except SMBusProxyError as e:
            raise OSError(str(e)) from e

    async def write_byte(self, addr: int, value: int):
        """Write a single byte to device.

        Args:
            addr: I2C address (7-bit)
            value: Byte value to write

        Raises:
            OSError: If write fails (matching smbus2 behavior)
        """
        try:
            response = await self._send_command(bytes([CMD_WRITE_BYTE, addr, value]))
            if len(response) >= 1 and response[0] == 0x00:
                return
            raise OSError(f"Write byte failed for address 0x{addr:02X}")
        except SMBusProxyError as e:
            raise OSError(str(e)) from e

    async def read_byte_data(self, addr: int, reg: int) -> int:
        """Read a byte from a specific register.

        Args:
            addr: I2C address (7-bit)
            reg: Register address

        Returns:
            Byte value read from register

        Raises:
            OSError: If read fails (matching smbus2 behavior)
        """
        try:
            response = await self._send_command(bytes([CMD_READ_BYTE_DATA, addr, reg]))
            if len(response) >= 1 and response[0] == 0x00 and len(response) >= 2:
                return response[1]
            raise OSError(f"Read byte data failed for address 0x{addr:02X} register 0x{reg:02X}")
        except SMBusProxyError as e:
            raise OSError(str(e)) from e

    async def write_byte_data(self, addr: int, reg: int, value: int):
        """Write a byte to a specific register.

        Args:
            addr: I2C address (7-bit)
            reg: Register address
            value: Byte value to write

        Raises:
            OSError: If write fails (matching smbus2 behavior)
        """
        try:
            response = await self._send_command(bytes([CMD_WRITE_BYTE_DATA, addr, reg, value]))
            if len(response) >= 1 and response[0] == 0x00:
                return
            raise OSError(f"Write byte data failed for address 0x{addr:02X} register 0x{reg:02X}")
        except SMBusProxyError as e:
            raise OSError(str(e)) from e

    def new_batch(self) -> I2CBatch:
        """Return an empty batch bound to this transport's frame limits.

        Drivers build batches through their bus so they do not have to know how
        the transport frames things.
        """

        return I2CBatch()

    async def execute_batch(self, batch: I2CBatch) -> list[int]:
        """Run a batch on the bridge and return its result bytes in order.

        Args:
            batch: The operations to execute

        Returns:
            One entry per result-producing operation, in queue order

        Raises:
            OSError: If any operation failed; the message names the operation index
        """
        if not len(batch):
            return []

        try:
            response = await self._send_command(bytes(batch))
        except SMBusProxyError as e:
            raise OSError(str(e)) from e

        if response and response[0] == 0x00 and len(response) >= batch.result_count + 1:
            return list(response[1 : batch.result_count + 1])
        if len(response) >= 2 and response[0] == 0xFF:
            raise I2CBatchError(f"I2C batch failed at operation {response[1]} of {len(batch)}", response[1])
        raise I2CBatchError("I2C batch returned a malformed response")

    async def read_i2c_block(self, addr: int, count: int) -> list[int]:
        """Read ``count`` bytes from a device in a single I2C transaction.

        This has no smbus2 counterpart because SMBus block reads carry a register
        and a length byte. Slaves that stream a prepared response buffer need a
        plain multi-byte read instead, and doing it in one bridge round trip is
        what makes it worth having.

        Args:
            addr: I2C address (7-bit)
            count: Number of bytes to read, at most MAX_BLOCK_READ

        Returns:
            The bytes read from the device

        Raises:
            OSError: If the read fails (matching smbus2 behavior)
            ValueError: If count is outside the supported range
        """
        if not 1 <= count <= MAX_BLOCK_READ:
            raise ValueError(f"Block read count must be between 1 and {MAX_BLOCK_READ}, got {count}")

        try:
            response = await self._send_command(bytes([CMD_READ_I2C_BLOCK, addr, count]))
            if len(response) >= count + 1 and response[0] == 0x00:
                return list(response[1 : count + 1])
            raise OSError(f"Read i2c block failed for address 0x{addr:02X}")
        except SMBusProxyError as e:
            raise OSError(str(e)) from e

    async def write_i2c_block_data(self, i2c_addr: int, register: int, data: list):
        """Write a block of byte data to a given register.

        Args:
            i2c_addr: I2C address (7-bit)
            register: Start register
            data: List of bytes
            force: Unused (for smbus2 API compatibility)

        Raises:
            OSError: If write fails (matching smbus2 behavior)
        """
        try:
            # Protocol: [CMD, ADDR, REG, DATA0, DATA1, ...]
            packet = bytes([CMD_WRITE_I2C_BLOCK_DATA, i2c_addr, register]) + bytes(data)
            response = await self._send_command(packet)
            if len(response) >= 1 and response[0] == 0x00:
                # Block writes (especially to dimmers) cause hardware transitions
                # that generate electrical noise. Add settling time.
                await asyncio.sleep(0.001)
                return
            raise OSError(f"Write i2c block data failed for address 0x{i2c_addr:02X} register 0x{register:02X}")
        except SMBusProxyError as e:
            raise OSError(str(e)) from e

    async def watch_config(self, modules: list[tuple[int, int]], fast_ms: int, slow_ms: int, debounce_ms: int) -> bool:
        """Have the bridge read the given modules on its own and push what changes.

        Events go to this connection until another client configures the watch.

        Args:
            modules: (kind, address) per module, in the order events index them
            fast_ms: How often inputs are sampled, 1 to 255 ms
            slow_ms: How often outputs are read back, in steps of 100 ms
            debounce_ms: Per-bit debounce the bridge applies to PCF8574 inputs

        Returns:
            True when the bridge already ran this configuration and resends what was
            not acknowledged yet; False when everything starts from a fresh baseline.

        Raises:
            OSError: If the bridge refused the configuration
        """

        if not modules or len(modules) > MAX_WATCH_MODULES:
            raise ValueError(f"Between 1 and {MAX_WATCH_MODULES} watched modules, got {len(modules)}")
        slow_ds = max(1, min(0xFFFF, round(slow_ms / 100)))
        payload = bytes(
            [
                CMD_WATCH_CONFIG,
                max(1, min(0xFF, fast_ms)),
                slow_ds >> 8,
                slow_ds & 0xFF,
                max(0, min(0xFF, debounce_ms)),
                len(modules),
            ]
        ) + bytes(byte for module in modules for byte in module)
        try:
            response = await self._send_command(payload)
        except SMBusProxyError as e:
            raise OSError(str(e)) from e
        if len(response) < 2 or response[0] != 0x00:
            raise OSError("Bridge refused the watch configuration")
        return response[1] == 0x01

    def watch_ack(self, seq: int) -> bool:
        """Acknowledge every pushed entry up to and including ``seq``."""

        return self.send_nowait(bytes([CMD_WATCH_ACK, seq & 0xFF]))

    async def interlock(self, addr: int, dead_ms: int, pairs: list[tuple[int, int]]) -> None:
        """Have the bridge keep each pair of output bits from running together.

        Bits are active low. A bit whose partner is on, or went off less than
        ``dead_ms`` ago, stays off, and the write that asked for it fails. An empty
        list lifts the interlock of the module.

        Raises:
            OSError: If the bridge refused the configuration
        """

        if len(pairs) > MAX_INTERLOCK_PAIRS:
            raise ValueError(f"At most {MAX_INTERLOCK_PAIRS} interlocked pairs, got {len(pairs)}")
        dead_ms = max(0, min(0xFFFF, dead_ms))
        payload = bytes([CMD_INTERLOCK, addr, dead_ms >> 8, dead_ms & 0xFF, len(pairs)]) + bytes(
            bit for pair in pairs for bit in pair
        )
        try:
            response = await self._send_command(payload)
        except SMBusProxyError as e:
            raise OSError(str(e)) from e
        if not response or response[0] != 0x00:
            raise OSError(f"Bridge refused the interlock for 0x{addr:02X}")

    async def ping_info(self) -> BridgeInfo | None:
        """Ping the bridge and return what it reports about itself, None if it did not answer.

        Raises:
            BridgeFirmwareError: If the bridge answers, but with firmware too old for
                this integration
        """

        try:
            response = await self._send_command(bytes([CMD_PING]))
        except SMBusProxyError:
            return None
        if len(response) < 3 or response[0] != 0x00 or response[1] != CMD_PING:
            return None
        if len(response) < PING_HEADER_SIZE:
            raise BridgeFirmwareError("The bridge firmware is too old for this integration")
        tail_start = PING_HEADER_SIZE + response[11]
        tail = response[tail_start : tail_start + PING_TAIL_SIZE]
        if len(tail) < PING_TAIL_SIZE or tail[0] & REQUIRED_CAPABILITIES != REQUIRED_CAPABILITIES:
            raise BridgeFirmwareError("The bridge firmware is too old for this integration")
        return BridgeInfo(
            boot_id=int.from_bytes(response[3:7], "big"),
            uptime_s=int.from_bytes(response[7:11], "big"),
            version=response[PING_HEADER_SIZE:tail_start].decode("ascii", "replace"),
            capabilities=tail[0],
            fast_sweep_us=int.from_bytes(tail[1:3], "big"),
            slow_sweep_us=int.from_bytes(tail[3:5], "big"),
            i2c_retries=int.from_bytes(tail[5:9], "big"),
            interlock_refusals=int.from_bytes(tail[9:13], "big"),
        )

    async def timed_output(self, addr: int, mask: int, value: int, revert: int, duration_ms: int) -> int:
        """Set the ``mask`` bits of a PCF8574 to ``value`` and have the bridge restore ``revert`` later.

        The other bits keep what the bridge reads from the chip. A later plain write
        that sets one of the bits differently takes that bit back from the timer.

        Returns:
            The port byte the bridge wrote and read back

        Raises:
            OSError: If the bridge refused the timer or the chip did not confirm it
        """

        if not 0 < duration_ms <= MAX_TIMED_OUTPUT_MS:
            raise ValueError(f"Timer must be between 1 and {MAX_TIMED_OUTPUT_MS} ms, got {duration_ms}")
        payload = bytes([CMD_TIMED_OUTPUT, addr, mask & 0xFF, value & 0xFF, revert & 0xFF]) + duration_ms.to_bytes(
            4, "big"
        )
        try:
            response = await self._send_command(payload)
        except SMBusProxyError as e:
            raise OSError(str(e)) from e
        if len(response) < 2 or response[0] != 0x00:
            raise OSError(f"Bridge refused the output timer for 0x{addr:02X}")
        return response[1]
