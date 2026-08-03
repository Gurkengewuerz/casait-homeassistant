"""Protocol and adaptive throttling tests for the SMBus TCP proxy."""

from __future__ import annotations

import pytest

from custom_components.casait_smarthome.services.smbus_proxy import (
    MAX_SEND_INTERVAL,
    MIN_SEND_INTERVAL,
    SEND_INTERVAL_RECOVERY_FRAMES,
    SMBus,
    SMBusProxyError,
)


class FakeSocket:
    """Small socket double with deterministic receive bytes."""

    def __init__(self, incoming: bytes = b"") -> None:
        self.incoming = bytearray(incoming)
        self.sent = bytearray()

    def sendall(self, data: bytes) -> None:
        self.sent.extend(data)

    def recv(self, size: int) -> bytes:
        data = self.incoming[:size]
        del self.incoming[:size]
        return bytes(data)

    def close(self) -> None:
        return


def _bus(monkeypatch) -> SMBus:
    monkeypatch.setattr(SMBus, "_connect", lambda self: None)
    return SMBus()


@pytest.mark.unit
def test_crc8_known_vector() -> None:
    assert SMBus._calc_crc8(b"123456789") == 0xF4  # noqa: SLF001


@pytest.mark.unit
def test_send_command_frames_request_and_response(monkeypatch) -> None:
    bus = _bus(monkeypatch)
    payload = b"\xaa\x55"
    response_frame = bytes([len(payload)]) + payload
    socket = FakeSocket(response_frame + bytes([bus._calc_crc8(response_frame)]))  # noqa: SLF001
    bus._sock = socket  # noqa: SLF001

    response = bus._send_command(b"\x11")  # noqa: SLF001

    request_frame = b"\x01\x11"
    assert response == payload
    assert bytes(socket.sent) == request_frame + bytes([bus._calc_crc8(request_frame)])  # noqa: SLF001


@pytest.mark.unit
def test_bad_crc_is_counted(monkeypatch) -> None:
    bus = _bus(monkeypatch)
    bus._sock = FakeSocket(b"\x01\xaa\x00")  # noqa: SLF001

    with pytest.raises(SMBusProxyError, match="CRC mismatch"):
        bus._receive_frame()  # noqa: SLF001

    assert bus.stats["crc_errors"] == 1


@pytest.mark.unit
def test_send_retries_and_increases_spacing(monkeypatch) -> None:
    bus = _bus(monkeypatch)
    bus._sock = FakeSocket()  # noqa: SLF001
    attempts = 0

    def receive() -> bytes:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise SMBusProxyError("temporary failure")
        return b"\x01"

    monkeypatch.setattr(bus, "_receive_frame", receive)
    monkeypatch.setattr(bus, "_reset_socket", lambda: None)
    monkeypatch.setattr("custom_components.casait_smarthome.services.smbus_proxy.time.sleep", lambda _delay: None)

    assert bus._send_command(b"\x11") == b"\x01"  # noqa: SLF001
    assert attempts == 3
    assert bus.stats["send_interval_ms"] == 3.0


@pytest.mark.unit
def test_adaptive_spacing_recovers_after_clean_frames(monkeypatch) -> None:
    bus = _bus(monkeypatch)
    for _ in range(10):
        bus._note_failure()  # noqa: SLF001
    assert bus._min_send_interval == MAX_SEND_INTERVAL  # noqa: SLF001

    for _ in range(SEND_INTERVAL_RECOVERY_FRAMES):
        bus._note_success(0.001)  # noqa: SLF001

    assert MIN_SEND_INTERVAL <= bus._min_send_interval < MAX_SEND_INTERVAL  # noqa: SLF001


@pytest.mark.unit
def test_socket_timeout_is_counted(monkeypatch) -> None:
    bus = _bus(monkeypatch)

    class TimeoutSocket(FakeSocket):
        def recv(self, size: int) -> bytes:
            raise TimeoutError

    bus._sock = TimeoutSocket()  # noqa: SLF001
    with pytest.raises(SMBusProxyError, match="Communication timeout"):
        bus._recv_exact(1)  # noqa: SLF001

    assert bus.stats["timeouts"] == 1
