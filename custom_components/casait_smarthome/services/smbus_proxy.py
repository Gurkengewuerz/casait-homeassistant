"""SMBus TCP Proxy Client.

A drop-in replacement for smbus2 that communicates with an I2C bridge.
over TCP instead of directly accessing the hardware.

This allows I2C operations to be performed remotely via a network-connected
microcontroller (e.g., ESP32 with W5500) running the SMBus Bridge firmware.
"""

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
CMD_SET_DEBUG = 0x10
CMD_PING = 0x11

# Default configuration from environment variables
DEFAULT_PORT = 8555
DEFAULT_TIMEOUT = 2.0


class SMBusProxyError(Exception):
    """Exception raised for SMBus proxy errors."""


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
    ) -> None:
        """Initialize SMBus proxy connection.

        Args:
            bus: Bus number (ignored, kept for compatibility with smbus2)
            host: Optional host override (default: from I2C_PROXY_HOST env)
            port: Optional port override (default: from I2C_PROXY_PORT env)
            timeout: Optional timeout override (default: from I2C_PROXY_TIMEOUT env)
        """
        self._bus = bus  # Kept for compatibility
        self.host = host
        self.port = port or DEFAULT_PORT
        self.timeout = timeout or DEFAULT_TIMEOUT
        self._sock: socket.socket | None = None
        self._io_lock = threading.Lock()
        self._last_send: float = 0.0
        self._min_send_interval = 0.005  # 5 ms spacing to avoid flooding bridge
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
            raise SMBusProxyError("CRC mismatch in bridge response")
        return payload

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
                    self._last_send = time.monotonic()

                    frame = bytes([len(payload)]) + payload
                    crc = self._calc_crc8(frame)
                    self._sock.sendall(frame + bytes([crc]))
                    response = self._receive_frame()

                    # Bridge may signal maintenance; back off to avoid busy reconnect loops
                    if len(response) >= 3 and response[:3] == b"\xff\xee\x01":
                        self._reset_socket()
                        time.sleep(2)
                        raise SMBusProxyError("Bridge in maintenance mode")  # noqa: TRY301
                except TimeoutError as e:
                    _LOGGER.warning(
                        "SMBus proxy communication timeout (attempt %d/%d)",
                        attempt + 1,
                        3,
                    )
                    self._reset_socket()
                    if attempt < 2:
                        time.sleep(1.0)
                        continue
                    raise SMBusProxyError("Communication timeout") from e
                except SMBusProxyError as e:
                    _LOGGER.warning(
                        "SMBus proxy error (attempt %d/%d): %s",
                        attempt + 1,
                        3,
                        e,
                    )
                    self._reset_socket()
                    if attempt < 2:
                        time.sleep(1.0)
                        continue
                    raise
                except OSError as e:
                    _LOGGER.warning(
                        "SMBus proxy communication error (attempt %d/%d): %s",
                        attempt + 1,
                        3,
                        e,
                    )
                    self._reset_socket()
                    if attempt < 2:
                        time.sleep(1.0)
                        continue
                    raise SMBusProxyError(f"Communication error: {e}") from e
                else:
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
                time.sleep(0.05)
                return
            raise OSError(f"Write i2c block data failed for address 0x{i2c_addr:02X} register 0x{register:02X}")
        except SMBusProxyError as e:
            raise OSError(str(e)) from e

    def set_debug(self, enabled: bool) -> bool:
        """Enable/disable debug mode on the bridge.

        When debug mode is enabled, the client receives broadcast data
        from other clients' operations.

        Args:
            enabled: True to enable debug mode

        Returns:
            True if debug mode was set successfully
        """
        try:
            response = self._send_command(bytes([CMD_SET_DEBUG, 1 if enabled else 0]))
            return len(response) == 2 and response[0] == 0x00
        except SMBusProxyError:
            return False

    def ping(self) -> bool:
        """Send a keep-alive ping to the bridge."""

        try:
            response = self._send_command(bytes([CMD_PING]))
            return len(response) >= 3 and response[0] == 0x00 and response[1] == CMD_PING
        except SMBusProxyError:
            return False
