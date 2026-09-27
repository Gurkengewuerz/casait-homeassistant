"""Protocol and adaptive throttling tests for the asyncio SMBus bridge client."""

from __future__ import annotations

import asyncio

import pytest

from custom_components.casait_smarthome.services import smbus_proxy
from custom_components.casait_smarthome.services.smbus_proxy import (
    MAX_SEND_INTERVAL,
    MIN_SEND_INTERVAL,
    SEND_INTERVAL_RECOVERY_FRAMES,
    SMBus,
    SMBusProxyError,
)


class FakeWriter:
    """StreamWriter double that records what was sent."""

    def __init__(self) -> None:
        self.sent = bytearray()
        self.closed = False

    def write(self, data: bytes) -> None:
        self.sent.extend(data)

    async def drain(self) -> None:
        return

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        return

    def get_extra_info(self, name: str) -> None:
        return None


def _bus(incoming: bytes = b"", *, eof: bool = False) -> tuple[SMBus, FakeWriter]:
    """Return a client wired to an in-memory stream instead of a socket."""

    bus = SMBus("bridge.test", timeout=0.05)
    reader = asyncio.StreamReader()
    reader.feed_data(incoming)
    if eof:
        reader.feed_eof()
    writer = FakeWriter()
    bus._reader, bus._writer = reader, writer  # type: ignore[assignment]  # noqa: SLF001
    return bus, writer


def _frame(payload: bytes) -> bytes:
    frame = bytes([len(payload)]) + payload
    return frame + bytes([SMBus._calc_crc8(frame)])  # noqa: SLF001


@pytest.mark.unit
def test_crc8_known_vector() -> None:
    assert SMBus._calc_crc8(b"123456789") == 0xF4  # noqa: SLF001


@pytest.mark.unit
async def test_send_command_frames_request_and_response() -> None:
    bus, writer = _bus(_frame(b"\xaa\x55"))

    response = await bus._send_command(b"\x11")  # noqa: SLF001

    assert response == b"\xaa\x55"
    assert bytes(writer.sent) == _frame(b"\x11")


@pytest.mark.unit
async def test_ping_checks_the_echoed_command() -> None:
    bus, _ = _bus(_frame(bytes([0x00, smbus_proxy.CMD_PING, 0xAA, 0, 0, 0, 7, 0, 0, 0, 9])))

    assert await bus.ping_info() == smbus_proxy.BridgeInfo(boot_id=7, uptime_s=9)


@pytest.mark.unit
async def test_bad_crc_is_counted() -> None:
    bus, _ = _bus(b"\x01\xaa\x00")

    with pytest.raises(SMBusProxyError, match="CRC mismatch"):
        await bus._receive_frame()  # noqa: SLF001

    assert bus.stats["crc_errors"] == 1


@pytest.mark.unit
async def test_send_retries_and_increases_spacing(monkeypatch: pytest.MonkeyPatch) -> None:
    bus, _ = _bus()
    attempts = 0

    async def receive() -> bytes:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise SMBusProxyError("temporary failure")
        return b"\x01"

    async def reconnect() -> None:
        bus._writer = FakeWriter()  # type: ignore[assignment]  # noqa: SLF001

    monkeypatch.setattr(bus, "_receive_frame", receive)
    monkeypatch.setattr(bus, "_connect", reconnect)
    monkeypatch.setattr(smbus_proxy, "RETRY_BACKOFF", (0, 0))

    assert await bus._send_command(b"\x11") == b"\x01"  # noqa: SLF001
    assert attempts == 3
    assert bus.stats["send_interval_ms"] == 3.0


@pytest.mark.unit
def test_adaptive_spacing_recovers_after_clean_frames() -> None:
    bus = SMBus()
    for _ in range(10):
        bus._note_failure()  # noqa: SLF001
    assert bus._min_send_interval == MAX_SEND_INTERVAL  # noqa: SLF001

    for _ in range(SEND_INTERVAL_RECOVERY_FRAMES):
        bus._note_success(0.001)  # noqa: SLF001

    assert MIN_SEND_INTERVAL <= bus._min_send_interval < MAX_SEND_INTERVAL  # noqa: SLF001


@pytest.mark.unit
async def test_a_silent_bridge_counts_as_a_timeout() -> None:
    bus, _ = _bus()

    with pytest.raises(SMBusProxyError, match="Communication timeout"):
        await bus._recv_exact(1)  # noqa: SLF001

    assert bus.stats["timeouts"] == 1


@pytest.mark.unit
async def test_a_closed_connection_is_reported() -> None:
    bus, _ = _bus(b"\x02", eof=True)

    with pytest.raises(SMBusProxyError, match="connection closed"):
        await bus._recv_exact(2)  # noqa: SLF001


@pytest.mark.unit
async def test_close_drops_the_connection() -> None:
    bus, writer = _bus()

    await bus.close()

    assert writer.closed
    assert bus.stats["connected"] is False


@pytest.mark.unit
async def test_connect_failure_is_a_proxy_error(monkeypatch: pytest.MonkeyPatch) -> None:
    async def refuse(host: str, port: int) -> None:
        raise ConnectionRefusedError(111, "Connection refused")

    monkeypatch.setattr(smbus_proxy.asyncio, "open_connection", refuse)

    with pytest.raises(SMBusProxyError, match="Failed to connect"):
        await SMBus.connect("bridge.test", 8555, timeout=0.5)
