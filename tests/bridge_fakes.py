"""Bridge double that runs batches against simulated PCF8574 latches."""

from __future__ import annotations

from custom_components.casait_smarthome.services.smbus_proxy import (
    BOP_DELAY,
    BOP_READ_BYTE,
    BOP_WRITE_BYTE,
    BridgeInfo,
    I2CBatch,
    I2CBatchError,
)


class FakeBridge:
    """Bridge double that runs batches against simulated PCF8574 output latches."""

    stats = {"connected": True}

    connection_generation = 1

    def __init__(self, chips: dict[int, int], *, boot_id: int = 1, version: str = "v0.0.1") -> None:
        self.chips = dict(chips)
        self.frames: list[list[tuple[str, int, int]]] = []
        self.stuck: set[int] = set()
        self.boot_id = boot_id
        self.version = version
        # [addr, mask, value, revert, duration_ms] of every timer the firmware holds.
        self.timers: list[list[int]] = []

    async def ping_info(self) -> BridgeInfo | None:
        return BridgeInfo(boot_id=self.boot_id, uptime_s=0, version=self.version)

    async def timed_output(self, addr: int, mask: int, value: int, revert: int, duration_ms: int) -> int:
        if addr not in self.chips:
            raise OSError("timer refused")
        self.timers = [timer for timer in self.timers if timer[0] != addr or timer[1] & ~mask]
        for timer in self.timers:
            if timer[0] == addr:
                timer[1] &= ~mask
        written = (self.chips[addr] & ~mask) | (value & mask)
        self.chips[addr] = written
        self.timers.append([addr, mask, value & mask, revert & mask, duration_ms])
        return written

    def expire_timers(self) -> None:
        """Let every timer run out, as the firmware's loop would."""

        for addr, mask, _value, revert, _ms in self.timers:
            self.chips[addr] = (self.chips[addr] & ~mask) | revert
        self.timers = []

    def _note_write(self, addr: int, value: int) -> None:
        for timer in self.timers:
            if timer[0] == addr:
                timer[1] &= ~((value ^ timer[2]) & timer[1])
        self.timers = [timer for timer in self.timers if timer[1]]

    async def read_byte(self, addr: int) -> int:
        if addr not in self.chips:
            raise OSError(f"NACK from 0x{addr:02X}")
        return self.chips[addr]

    async def execute_batch(self, batch: I2CBatch) -> list[int]:
        payload = bytes(batch)[1:]
        frame: list[tuple[str, int, int]] = []
        results: list[int] = []
        index = op = 0
        while index < len(payload):
            code = payload[index]
            if code == BOP_WRITE_BYTE:
                addr, value = payload[index + 1], payload[index + 2]
                if addr not in self.chips:
                    raise I2CBatchError("write failed", op)
                if addr not in self.stuck:
                    self.chips[addr] = value
                self._note_write(addr, value)
                frame.append(("write", addr, value))
                index += 3
            elif code == BOP_READ_BYTE:
                addr = payload[index + 1]
                if addr not in self.chips:
                    raise I2CBatchError("read failed", op)
                results.append(self.chips[addr])
                frame.append(("read", addr, self.chips[addr]))
                index += 2
            elif code == BOP_DELAY:
                frame.append(("delay", 0, payload[index + 1]))
                index += 2
            else:
                raise AssertionError(f"unexpected op 0x{code:02X}")
            op += 1
        self.frames.append(frame)
        return results

    def writes(self) -> list[list[tuple[int, int]]]:
        """Return the written (address, value) pairs of each frame."""

        return [[(addr, value) for kind, addr, value in frame if kind == "write"] for frame in self.frames]
