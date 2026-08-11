"""SMBus TCP Proxy Client.

A drop-in replacement for smbus2 that communicates with an I2C bridge.
over TCP instead of directly accessing the hardware.

This allows I2C operations to be performed remotely via a network-connected
microcontroller (e.g., ESP32 with W5500) running the SMBus Bridge firmware.
"""

from __future__ import annotations

import contextlib
import logging
import socket
import threading
import time

_LOGGER = logging.getLogger(__name__)

# Protocol commands (matching implementation.cpp)
CMD_WRITE_BYTE = 0x01
CMD_WRITE_BYTE_DATA = 0x02
CMD_READ_BYTE = 0x03
CMD_READ_BYTE_DATA = 0x04
CMD_WRITE_I2C_BLOCK_DATA = 0x05
CMD_READ_I2C_BLOCK = 0x06
CMD_BATCH = 0x07
CMD_PING = 0x11
# Autonomous input scanning. The bridge samples a configured set of PCF8574s on its
# own and latches the transitions, so a press cannot fall between two HA cycles and
# the input latency stops depending on the network round trip.
CMD_SCAN_CONFIG = 0x12
CMD_SCAN_FETCH = 0x13

# Batch sub-opcodes, numbered like the top-level commands so both sides stay readable
BOP_WRITE_BYTE = 0x01
BOP_WRITE_BYTE_DATA = 0x02
BOP_READ_BYTE = 0x03
BOP_READ_BYTE_DATA = 0x04
BOP_READ_BLOCK = 0x06
BOP_WAIT_STATUS = 0x07
BOP_DELAY = 0x08

# The bridge frames both directions as [len][payload][crc] with a single length byte
# and a 128 byte buffer, and a block read or batch spends one payload byte on status.
MAX_FRAME_PAYLOAD = 126
MAX_BLOCK_READ = MAX_FRAME_PAYLOAD - 1
MAX_BATCH_RESULTS = MAX_FRAME_PAYLOAD - 1

# The bridge clamps these too; keeping the client honest makes overruns visible here
# rather than as a silently shortened wait on the wire.
MAX_WAIT_STATUS_MS = 50
MAX_DELAY_MS = 10

# Scanner limits. Entries are (address index, sampled value) pairs and share the one
# frame with the status, flags and count bytes.
MAX_SCAN_ADDRESSES = 32
MAX_SCAN_ENTRIES = (MAX_FRAME_PAYLOAD - 3) // 2
# Set by the bridge when its transition queue overflowed and snapshots were dropped.
SCAN_FLAG_OVERFLOW = 0x01

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
# while the I/O lock is held, so it stalls every other caller including the input
# poll loop; it exists to let the bridge settle, not to wait out an outage.
RETRY_BACKOFF = (0.05, 0.15)
# Pause after the bridge reports maintenance mode. Same constraint as above.
MAINTENANCE_BACKOFF = 0.5


class SMBusProxyError(Exception):
    """Exception raised for SMBus proxy errors."""


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


class SMBus:
    """SMBus TCP Proxy - drop-in replacement for smbus2.SMBus.

    Connects to an I2C bridge server over TCP and translates SMBus
    operations into the bridge protocol.

    Usage:
        # Environment variables:
        # I2C_PROXY_HOST - IP address of the bridge (default: 192.168.1.100)
        # I2C_PROXY_PORT - TCP port (default: 8555)
        # I2C_PROXY_TIMEOUT - Socket timeout in seconds (default: 2.0)

        bus = SMBus(1)  # bus number is ignored, uses TCP connection
        value = bus.read_byte_data(0x20, 0x00)
        bus.write_byte_data(0x20, 0x00, 0xFF)
        bus.close()

        # Or as context manager:
        with SMBus(1) as bus:
            value = bus.read_byte_data(0x20, 0x00)
    """

    def __init__(
        self,
        bus: int = 1,
        host: str = "192.168.1.100",
        port: int | None = None,
        timeout: float | None = None,
        max_send_interval: float = MAX_SEND_INTERVAL,
    ) -> None:
        """Initialize SMBus proxy connection.

        Args:
            bus: Bus number (ignored, kept for compatibility with smbus2)
            host: Optional host override (default: from I2C_PROXY_HOST env)
            port: Optional port override (default: from I2C_PROXY_PORT env)
            timeout: Optional timeout override (default: from I2C_PROXY_TIMEOUT env)
            max_send_interval: Maximum adaptive spacing between frames in seconds
        """
        self._bus = bus  # Kept for compatibility
        self.host = host
        self.port = port or DEFAULT_PORT
        self.timeout = timeout or DEFAULT_TIMEOUT
        self._max_send_interval = max(MIN_SEND_INTERVAL, max_send_interval)
        self._sock: socket.socket | None = None
        self._io_lock = threading.Lock()
        self._last_send: float = 0.0
        self._min_send_interval = MIN_SEND_INTERVAL
        self._consecutive_ok = 0
        self._crc_errors = 0
        self._timeouts = 0
        self._io_errors = 0
        self._frames = 0
        self._last_rtt = 0.0
        _LOGGER.debug(
            "Initializing SMBusProxy with host=%s, port=%s, timeout=%s",
            self.host,
            self.port,
            self.timeout,
        )
        with self._io_lock:
            self._connect()

    def __enter__(self):
        """Context manager entry."""
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit."""
        self.close()
        return False

    def _connect(self):
        """Establish TCP connection to the bridge.

        Must be called while holding ``_io_lock`` (or during __init__
        before any other thread can access the instance).
        """
        if self._sock is not None:
            return  # Already connected

        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(self.timeout)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            sock.connect((self.host, self.port))
            self._sock = sock
            _LOGGER.info("Connected to SMBus bridge at %s:%s", self.host, self.port)
        except OSError as e:
            self._sock = None
            _LOGGER.error("Failed to connect to SMBus bridge: %s", e)
            raise SMBusProxyError(f"Failed to connect to SMBus bridge at {self.host}:{self.port}: {e}") from e

    def _ensure_connected(self):
        """Ensure we have an active connection, reconnect if needed.

        Must be called while holding ``_io_lock``.
        """
        if self._sock is None:
            self._connect()

    @staticmethod
    def _calc_crc8(data: bytes) -> int:
        """Compute CRC8 with polynomial 0x07 and init 0x00."""

        crc = 0
        for byte in data:
            crc ^= byte
            for _ in range(8):
                crc = ((crc << 1) ^ 0x07) & 0xFF if crc & 0x80 else (crc << 1) & 0xFF
        return crc

    def _recv_exact(self, size: int) -> bytes:
        """Receive exactly ``size`` bytes or raise."""

        if self._sock is None:
            raise SMBusProxyError("Socket is not connected")

        chunks = bytearray()
        while len(chunks) < size:
            try:
                chunk = self._sock.recv(size - len(chunks))
            except TimeoutError as err:
                self._timeouts += 1
                raise SMBusProxyError("Communication timeout") from err
            if not chunk:
                raise SMBusProxyError("Communication error: empty response")
            chunks.extend(chunk)
        return bytes(chunks)

    def _receive_frame(self) -> bytes:
        """Read a framed response [len][payload][crc8]."""

        length_bytes = self._recv_exact(1)
        frame_len = length_bytes[0]
        payload = self._recv_exact(frame_len) if frame_len else b""
        crc_recv = self._recv_exact(1)[0]

        frame = length_bytes + payload
        crc_expected = self._calc_crc8(frame)
        if crc_recv != crc_expected:
            self._crc_errors += 1
            raise SMBusProxyError("CRC mismatch in bridge response")
        return payload

    def _note_success(self, rtt: float) -> None:
        """Record a clean round trip and relax the spacing once it looks safe.

        Must be called while holding ``_io_lock``.
        """

        self._frames += 1
        self._last_rtt = rtt
        self._consecutive_ok += 1

        if self._consecutive_ok >= SEND_INTERVAL_RECOVERY_FRAMES and self._min_send_interval > MIN_SEND_INTERVAL:
            self._consecutive_ok = 0
            self._min_send_interval = max(MIN_SEND_INTERVAL, self._min_send_interval - SEND_INTERVAL_STEP)
            _LOGGER.debug("Relaxing SMBus send spacing to %.1f ms", self._min_send_interval * 1000)

    def _note_failure(self) -> None:
        """Record a failed round trip and back the spacing off one step.

        Must be called while holding ``_io_lock``.
        """

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
            "connected": self._sock is not None,
        }

    def _send_command(self, payload: bytes) -> bytes:
        """Send a framed command and return payload of response."""

        with self._io_lock:
            for attempt in range(3):
                self._ensure_connected()

                try:
                    if self._sock is None:
                        raise SMBusProxyError("Socket connection failed")  # noqa: TRY301

                    now = time.monotonic()
                    delta = now - self._last_send
                    if delta < self._min_send_interval:
                        time.sleep(self._min_send_interval - delta)
                    send_start = time.monotonic()
                    self._last_send = send_start

                    frame = bytes([len(payload)]) + payload
                    crc = self._calc_crc8(frame)
                    self._sock.sendall(frame + bytes([crc]))
                    response = self._receive_frame()
                    rtt = time.monotonic() - send_start

                    # Bridge may signal maintenance; back off to avoid busy reconnect
                    # loops. Kept short because this sleep holds the I/O lock and so
                    # stalls the input poll loop along with everything else.
                    if len(response) >= 3 and response[:3] == b"\xff\xee\x01":
                        self._reset_socket()
                        time.sleep(MAINTENANCE_BACKOFF)
                        raise SMBusProxyError("Bridge in maintenance mode")  # noqa: TRY301
                except TimeoutError as e:
                    self._timeouts += 1
                    self._note_failure()
                    _LOGGER.warning(
                        "SMBus proxy communication timeout (attempt %d/%d)",
                        attempt + 1,
                        3,
                    )
                    self._reset_socket()
                    if attempt < 2:
                        time.sleep(RETRY_BACKOFF[attempt])
                        continue
                    raise SMBusProxyError("Communication timeout") from e
                except SMBusProxyError as e:
                    self._note_failure()
                    _LOGGER.warning(
                        "SMBus proxy error (attempt %d/%d): %s",
                        attempt + 1,
                        3,
                        e,
                    )
                    self._reset_socket()
                    if attempt < 2:
                        time.sleep(RETRY_BACKOFF[attempt])
                        continue
                    raise
                except OSError as e:
                    self._io_errors += 1
                    self._note_failure()
                    _LOGGER.warning(
                        "SMBus proxy communication error (attempt %d/%d): %s",
                        attempt + 1,
                        3,
                        e,
                    )
                    self._reset_socket()
                    if attempt < 2:
                        time.sleep(RETRY_BACKOFF[attempt])
                        continue
                    raise SMBusProxyError(f"Communication error: {e}") from e
                else:
                    self._note_success(rtt)
                    return response
            return b""

    def _reset_socket(self) -> None:
        """Close and clear the current socket so next call reconnects."""

        if self._sock:
            with contextlib.suppress(Exception):
                self._sock.close()
        self._sock = None

    def close(self):
        """Close the connection to the bridge."""
        if self._sock:
            with contextlib.suppress(Exception):
                self._sock.close()
            self._sock = None
            _LOGGER.debug("SMBus proxy connection closed")

    def write_quick(self, addr: int):
        """Perform a quick write to probe device presence.

        This is implemented as a read_byte operation, which will
        succeed if a device responds at the address.

        Args:
            addr: I2C address (7-bit)

        Raises:
            OSError: If device doesn't respond (matching smbus2 behavior)
        """
        try:
            self.read_byte(addr)
        except SMBusProxyError as e:
            raise OSError(f"Device at address 0x{addr:02X} not responding") from e

    def read_byte(self, addr: int) -> int:
        """Read a single byte from device.

        Args:
            addr: I2C address (7-bit)

        Returns:
            Byte value read from device

        Raises:
            OSError: If read fails (matching smbus2 behavior)
        """
        try:
            response = self._send_command(bytes([CMD_READ_BYTE, addr]))
            if len(response) >= 1 and response[0] == 0x00 and len(response) >= 2:
                return response[1]
            raise OSError(f"Read byte failed for address 0x{addr:02X}")
        except SMBusProxyError as e:
            raise OSError(str(e)) from e

    def write_byte(self, addr: int, value: int):
        """Write a single byte to device.

        Args:
            addr: I2C address (7-bit)
            value: Byte value to write

        Raises:
            OSError: If write fails (matching smbus2 behavior)
        """
        try:
            response = self._send_command(bytes([CMD_WRITE_BYTE, addr, value]))
            if len(response) >= 1 and response[0] == 0x00:
                return
            raise OSError(f"Write byte failed for address 0x{addr:02X}")
        except SMBusProxyError as e:
            raise OSError(str(e)) from e

    def read_byte_data(self, addr: int, reg: int) -> int:
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
            response = self._send_command(bytes([CMD_READ_BYTE_DATA, addr, reg]))
            if len(response) >= 1 and response[0] == 0x00 and len(response) >= 2:
                return response[1]
            raise OSError(f"Read byte data failed for address 0x{addr:02X} register 0x{reg:02X}")
        except SMBusProxyError as e:
            raise OSError(str(e)) from e

    def write_byte_data(self, addr: int, reg: int, value: int):
        """Write a byte to a specific register.

        Args:
            addr: I2C address (7-bit)
            reg: Register address
            value: Byte value to write

        Raises:
            OSError: If write fails (matching smbus2 behavior)
        """
        try:
            response = self._send_command(bytes([CMD_WRITE_BYTE_DATA, addr, reg, value]))
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

    def execute_batch(self, batch: I2CBatch) -> list[int]:
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
            response = self._send_command(bytes(batch))
        except SMBusProxyError as e:
            raise OSError(str(e)) from e

        if response and response[0] == 0x00 and len(response) >= batch.result_count + 1:
            return list(response[1 : batch.result_count + 1])
        if len(response) >= 2 and response[0] == 0xFF:
            raise I2CBatchError(f"I2C batch failed at operation {response[1]} of {len(batch)}", response[1])
        raise I2CBatchError("I2C batch returned a malformed response")

    def read_i2c_block(self, addr: int, count: int) -> list[int]:
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
            response = self._send_command(bytes([CMD_READ_I2C_BLOCK, addr, count]))
            if len(response) >= count + 1 and response[0] == 0x00:
                return list(response[1 : count + 1])
            raise OSError(f"Read i2c block failed for address 0x{addr:02X}")
        except SMBusProxyError as e:
            raise OSError(str(e)) from e

    def write_i2c_block_data(self, i2c_addr: int, register: int, data: list):
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
            response = self._send_command(packet)
            if len(response) >= 1 and response[0] == 0x00:
                # Block writes (especially to dimmers) cause hardware transitions
                # that generate electrical noise. Add settling time.
                time.sleep(0.001)
                return
            raise OSError(f"Write i2c block data failed for address 0x{i2c_addr:02X} register 0x{register:02X}")
        except SMBusProxyError as e:
            raise OSError(str(e)) from e

    def scan_config(self, addresses: list[int], period_ms: int, debounce_ms: int) -> bool:
        """Hand the bridge the input addresses to sample on its own.

        Returns False when the bridge does not implement the scanner, which is the
        signal for the caller to keep polling the inputs itself. A firmware without
        this command does not answer at all, so the probe costs the full retry budget
        once - never call it from a path that runs repeatedly.

        Args:
            addresses: PCF8574 addresses to sample, in the order fetch results index
            period_ms: How often the bridge samples the whole set
            debounce_ms: Per-bit debounce the bridge applies before latching an edge

        Returns:
            True if the bridge accepted the configuration
        """

        if not addresses:
            return False
        if len(addresses) > MAX_SCAN_ADDRESSES:
            raise ValueError(f"At most {MAX_SCAN_ADDRESSES} scan addresses, got {len(addresses)}")

        payload = bytes([CMD_SCAN_CONFIG, period_ms & 0xFF, debounce_ms & 0xFF, len(addresses), *addresses])
        try:
            response = self._send_command(payload)
        except SMBusProxyError:
            _LOGGER.info("Bridge does not support autonomous input scanning; polling inputs from Home Assistant")
            return False
        return bool(response) and response[0] == 0x00

    def scan_fetch(self) -> tuple[int, list[tuple[int, int]]]:
        """Collect the transitions the bridge latched since the last fetch.

        Returns:
            The flags byte and the latched (address index, port value) snapshots in
            the order they were sampled. Several snapshots for one address mean the
            input changed more than once between fetches.

        Raises:
            OSError: If the fetch fails or the response is malformed
        """

        try:
            response = self._send_command(bytes([CMD_SCAN_FETCH]))
        except SMBusProxyError as e:
            raise OSError(str(e)) from e

        if len(response) < 3 or response[0] != 0x00:
            raise OSError("Scan fetch returned a malformed response")

        flags = response[1]
        count = response[2]
        if count > MAX_SCAN_ENTRIES or len(response) < 3 + count * 2:
            raise OSError("Scan fetch returned a truncated entry list")

        entries = response[3 : 3 + count * 2]
        return flags, [(entries[index], entries[index + 1]) for index in range(0, count * 2, 2)]

    def ping(self) -> bool:
        """Send a keep-alive ping to the bridge."""

        try:
            response = self._send_command(bytes([CMD_PING]))
            return len(response) >= 3 and response[0] == 0x00 and response[1] == CMD_PING
        except SMBusProxyError:
            return False
