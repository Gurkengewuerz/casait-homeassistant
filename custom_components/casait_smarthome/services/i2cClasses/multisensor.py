"""casaIT Multisensor: SHT41, SGP40, STCC4 and VEML7700 behind one DS28E17.

The board can be populated with any subset of the four chips, so which ones are
there is found out by probing their I2C addresses rather than assumed.

Every method here is one DS28E17 transaction and never sleeps for a sensor's
measurement time. The Sensirion chips need 10 to 90 ms between a command and
its result; waiting that out while holding the bus would stall the input poll,
so the caller issues the command, releases the bus, sleeps, and comes back for
the result.

Sensirion chips protect every 16-bit word with a CRC-8 (polynomial 0x31, init
0xFF). A read answered before the result is ready is not acknowledged, which the
bridge reports as a NACK.

References:
- SHT41:    https://sensirion.com/products/catalog/SHT41
- SGP40:    https://sensirion.com/products/catalog/SGP40
- STCC4:    https://sensirion.com/products/catalog/STCC4
- VEML7700: https://www.vishay.com/docs/84286/veml7700.pdf
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass, field
import logging
import time
from typing import Any

from .ds28e17 import DS28E17, DS28E17Error, DS28E17Nack
from .gas_index import VocGasIndexAlgorithm

_LOGGER = logging.getLogger(__name__)

CHIP_SHT41 = "sht41"
CHIP_SGP40 = "sgp40"
CHIP_STCC4 = "stcc4"
CHIP_VEML7700 = "veml7700"
CHIPS = (CHIP_SHT41, CHIP_SGP40, CHIP_STCC4, CHIP_VEML7700)

SHT41_ADDRESS = 0x44
SGP40_ADDRESS = 0x59
STCC4_ADDRESSES = (0x64, 0x65)
VEML7700_ADDRESS = 0x10
# The LED controller firmware on the same bridge chip answers here.
LED_CONTROLLER_ADDRESS = 0x42

# SHT41
SHT41_CMD_MEASURE_HIGH = 0xFD
SHT41_CMD_SERIAL = 0x89
SHT41_MEASURE_TIME = 0.010

# SGP40
SGP40_CMD_MEASURE_RAW = (0x26, 0x0F)
SGP40_CMD_SERIAL = (0x36, 0x82)
SGP40_MEASURE_TIME = 0.030
# Compensation defaults from the datasheet: 50 %RH and 25 °C.
SGP40_DEFAULT_RH_TICKS = 0x8000
SGP40_DEFAULT_T_TICKS = 0x6666

# STCC4
STCC4_CMD_START_CONTINUOUS = 0x218B
STCC4_CMD_STOP_CONTINUOUS = 0x3F86
STCC4_CMD_READ_MEASUREMENT = 0xEC05
STCC4_CMD_SET_RHT_COMPENSATION = 0xE000
STCC4_CMD_FORCED_RECALIBRATION = 0x362F
STCC4_CMD_SELF_TEST = 0x278C
STCC4_CMD_CONDITIONING = 0x29BC
STCC4_CMD_FACTORY_RESET = 0x3632
STCC4_STOP_TIME = 1.2
STCC4_FRC_TIME = 0.09
STCC4_SELF_TEST_TIME = 0.36
STCC4_CONDITIONING_TIME = 22.0 + 2.0  # plus the recommended settling time
STCC4_FACTORY_RESET_TIME = 0.09
STCC4_COMMAND_TIME = 0.001
STCC4_FRC_FAILED = 0xFFFF

# VEML7700 registers and configuration
VEML7700_REG_ALS_CONF = 0x00
VEML7700_REG_ALS = 0x04
# Resolution in lux per count at gain x2 and 800 ms integration. Everything
# else scales linearly from here.
VEML7700_BASE_RESOLUTION = 0.0042
VEML7700_GAIN_BITS = {2.0: 0b01, 1.0: 0b00, 0.25: 0b11, 0.125: 0b10}
VEML7700_IT_BITS = {25: 0b1100, 50: 0b1000, 100: 0b0000, 200: 0b0001, 400: 0b0010, 800: 0b0011}
# (gain, integration time ms) from most to least sensitive. Automatic ranging
# walks this list, one step per sample.
VEML7700_RANGES: tuple[tuple[float, int], ...] = (
    (2.0, 800),
    (2.0, 400),
    (2.0, 200),
    (2.0, 100),
    (1.0, 100),
    (0.25, 100),
    (0.125, 100),
    (0.125, 50),
    (0.125, 25),
)
VEML7700_DEFAULT_RANGE = 4  # gain x1, 100 ms: sensible for a room before the first sample
VEML7700_RAW_HIGH = 20000
VEML7700_RAW_LOW = 100


def sensirion_crc(data: bytes) -> int:
    """Return the Sensirion CRC-8 of one data word."""

    crc = 0xFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = ((crc << 1) ^ 0x31) & 0xFF if crc & 0x80 else (crc << 1) & 0xFF
    return crc


def encode_words(*words: int) -> bytes:
    """Encode 16-bit words as Sensirion wants them: big endian, each with its CRC."""

    out = bytearray()
    for word in words:
        chunk = bytes([(word >> 8) & 0xFF, word & 0xFF])
        out += chunk + bytes([sensirion_crc(chunk)])
    return bytes(out)


def decode_words(data: bytes) -> list[int]:
    """Decode CRC-protected 16-bit words, raising on a CRC mismatch."""

    if len(data) % 3:
        raise DS28E17Error(f"unexpected answer length {len(data)}")
    words = []
    for offset in range(0, len(data), 3):
        chunk = data[offset : offset + 2]
        if sensirion_crc(chunk) != data[offset + 2]:
            raise DS28E17Error("Sensirion CRC mismatch")
        words.append((chunk[0] << 8) | chunk[1])
    return words


def command(code: int, *words: int) -> bytes:
    """Return a 16-bit Sensirion command followed by its CRC-protected arguments."""

    return bytes([(code >> 8) & 0xFF, code & 0xFF]) + encode_words(*words)


def to_int16(word: int) -> int:
    """Interpret a 16-bit word as two's complement."""

    return word - 0x10000 if word & 0x8000 else word


def sht_temperature(ticks: int) -> float:
    """Convert SHT4x/STCC4 temperature ticks to °C."""

    return -45.0 + 175.0 * ticks / 65535.0


def sht_humidity(ticks: int) -> float:
    """Convert SHT4x/STCC4 humidity ticks to %RH, clamped to the physical range."""

    return min(100.0, max(0.0, -6.0 + 125.0 * ticks / 65535.0))


def veml7700_lux(raw: int, gain: float, integration_ms: int) -> float:
    """Convert a VEML7700 light count to lux, including the high-range correction."""

    resolution = VEML7700_BASE_RESOLUTION * (2.0 / gain) * (800.0 / integration_ms)
    lux = raw * resolution
    if gain < 1.0:
        # Vishay's correction for the non-linearity at the low gains.
        lux = 6.0135e-13 * lux**4 - 9.3924e-9 * lux**3 + 8.1488e-5 * lux**2 + 1.0023 * lux
    return lux


def veml7700_config(range_index: int) -> int:
    """Return the configuration register value for one ranging step, powered on, no interrupts."""

    gain, integration_ms = VEML7700_RANGES[range_index]
    return (VEML7700_GAIN_BITS[gain] << 11) | (VEML7700_IT_BITS[integration_ms] << 6)


@dataclass(frozen=True)
class MultisensorComponents:
    """Which chips a board is populated with."""

    sht41: bool = False
    sgp40: bool = False
    stcc4_address: int | None = None
    veml7700: bool = False

    @property
    def stcc4(self) -> bool:
        """Return whether a CO2 sensor is fitted."""

        return self.stcc4_address is not None

    @property
    def any(self) -> bool:
        """Return whether at least one sensor answered."""

        return self.sht41 or self.sgp40 or self.stcc4 or self.veml7700

    def has(self, chip: str) -> bool:
        """Return whether one chip, by its CHIP_* name, is fitted."""

        return {
            CHIP_SHT41: self.sht41,
            CHIP_SGP40: self.sgp40,
            CHIP_STCC4: self.stcc4,
            CHIP_VEML7700: self.veml7700,
        }[chip]

    def union(self, other: MultisensorComponents) -> MultisensorComponents:
        """Return the chips fitted in either set."""

        return MultisensorComponents(
            sht41=self.sht41 or other.sht41,
            sgp40=self.sgp40 or other.sgp40,
            stcc4_address=self.stcc4_address if self.stcc4_address is not None else other.stcc4_address,
            veml7700=self.veml7700 or other.veml7700,
        )

    def without(self, chip: str) -> MultisensorComponents:
        """Return the same set minus one chip."""

        return MultisensorComponents(
            sht41=self.sht41 and chip != CHIP_SHT41,
            sgp40=self.sgp40 and chip != CHIP_SGP40,
            stcc4_address=None if chip == CHIP_STCC4 else self.stcc4_address,
            veml7700=self.veml7700 and chip != CHIP_VEML7700,
        )

    def to_dict(self) -> dict[str, bool | int | None]:
        """Return a JSON-safe form for storage."""

        return {
            CHIP_SHT41: self.sht41,
            CHIP_SGP40: self.sgp40,
            "stcc4_address": self.stcc4_address,
            CHIP_VEML7700: self.veml7700,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> MultisensorComponents:
        """Rebuild from the stored form, ignoring anything malformed."""

        address = data.get("stcc4_address")
        return cls(
            sht41=bool(data.get(CHIP_SHT41)),
            sgp40=bool(data.get(CHIP_SGP40)),
            stcc4_address=address if isinstance(address, int) else None,
            veml7700=bool(data.get(CHIP_VEML7700)),
        )

    def as_list(self) -> list[str]:
        """Return the fitted chips by name, for diagnostics."""

        names = []
        if self.sht41:
            names.append("SHT41")
        if self.sgp40:
            names.append("SGP40")
        if self.stcc4:
            names.append(f"STCC4@0x{self.stcc4_address:02X}")
        if self.veml7700:
            names.append("VEML7700")
        return names


@dataclass
class MultisensorReading:
    """The latest value of every quantity the board can report.

    A value stays None until its chip has produced one, and goes back to None
    when the chip stops answering.
    """

    temperature: float | None = None
    humidity: float | None = None
    co2: int | None = None
    voc_index: int | None = None
    voc_raw: int | None = None
    illuminance: float | None = None


@dataclass
class MultisensorState:
    """Everything one board remembers between samples."""

    components: MultisensorComponents
    voc: VocGasIndexAlgorithm
    reading: MultisensorReading = field(default_factory=MultisensorReading)
    # Raw SHT41 ticks, forwarded to the SGP40 and STCC4 for compensation.
    t_ticks: int | None = None
    rh_ticks: int | None = None
    veml_range: int = VEML7700_DEFAULT_RANGE
    veml_configured: bool = False
    # Whether the STCC4 is in continuous mode, and when it may be started again
    # after a stop.
    stcc4_running: bool = False
    stcc4_ready_at: float = 0.0
    # Results of the last maintenance commands, for the diagnostic entities.
    last_frc_correction: int | None = None
    self_test_passed: bool | None = None


class Multisensor:
    """Drive the chips of any number of Multisensor boards on one 1-Wire bus."""

    def __init__(self, bridge: DS28E17) -> None:
        """Initialize with the bus's DS28E17 bridge."""

        self.bridge = bridge

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    async def is_led_controller(self, device_id: str) -> bool:
        """Return whether the LED controller firmware answers behind this bridge."""

        try:
            await self.bridge.read(device_id, LED_CONTROLLER_ADDRESS, 1)
        except DS28E17Nack:
            return False
        return True

    async def detect(self, device_id: str) -> MultisensorComponents:
        """Probe every chip the board may carry.

        Raises DS28E17Error when the bridge itself does not answer, so that a
        bus fault is not mistaken for an empty board.
        """

        # Writing the VEML7700 configuration doubles as its probe and powers it up.
        return MultisensorComponents(
            sht41=await self.probe_chip(device_id, CHIP_SHT41) is not None,
            sgp40=await self.probe_chip(device_id, CHIP_SGP40) is not None,
            stcc4_address=await self.probe_chip(device_id, CHIP_STCC4),
            veml7700=await self.probe_chip(device_id, CHIP_VEML7700) is not None,
        )

    async def probe_chip(self, device_id: str, chip: str) -> int | None:
        """Probe one chip; return its I2C address when it answers, else None."""

        if chip == CHIP_SHT41:
            return SHT41_ADDRESS if await self._probe_sht41(device_id) else None
        if chip == CHIP_SGP40:
            return SGP40_ADDRESS if await self._probe_sgp40(device_id) else None
        if chip == CHIP_STCC4:
            for address in STCC4_ADDRESSES:
                if await self._probe_stcc4(device_id, address):
                    return address
            return None
        if chip == CHIP_VEML7700:
            config = bytes([VEML7700_REG_ALS_CONF]) + veml7700_config(VEML7700_DEFAULT_RANGE).to_bytes(2, "little")
            return VEML7700_ADDRESS if await self.bridge.probe(device_id, VEML7700_ADDRESS, config) else None
        raise ValueError(f"unknown chip {chip}")

    async def _probe_sht41(self, device_id: str) -> bool:
        if not await self.bridge.probe(device_id, SHT41_ADDRESS, bytes([SHT41_CMD_SERIAL])):
            return False
        await asyncio.sleep(SHT41_MEASURE_TIME)
        # Reading the serial back proves it is really an SHT4x.
        try:
            decode_words(await self.bridge.read(device_id, SHT41_ADDRESS, 6))
        except DS28E17Nack:
            return False
        return True

    async def _probe_sgp40(self, device_id: str) -> bool:
        if not await self.bridge.probe(device_id, SGP40_ADDRESS, bytes(SGP40_CMD_SERIAL)):
            return False
        await asyncio.sleep(0.001)
        try:
            decode_words(await self.bridge.read(device_id, SGP40_ADDRESS, 9))
        except DS28E17Nack:
            return False
        return True

    async def _probe_stcc4(self, device_id: str, address: int) -> bool:
        # Stopping is the one command the chip accepts in every state, so it is
        # both the probe and the reset into a known state. The caller has to
        # leave the chip alone for STCC4_STOP_TIME afterwards.
        return await self.bridge.probe(device_id, address, command(STCC4_CMD_STOP_CONTINUOUS))

    # ------------------------------------------------------------------
    # SHT41
    # ------------------------------------------------------------------

    async def sht41_trigger(self, device_id: str) -> None:
        """Start a high-precision temperature and humidity measurement."""

        await self.bridge.write(device_id, SHT41_ADDRESS, bytes([SHT41_CMD_MEASURE_HIGH]))

    async def sht41_fetch(self, device_id: str, state: MultisensorState) -> None:
        """Collect the measurement started by sht41_trigger."""

        t_ticks, rh_ticks = decode_words(await self.bridge.read(device_id, SHT41_ADDRESS, 6))
        state.t_ticks, state.rh_ticks = t_ticks, rh_ticks
        state.reading.temperature = round(sht_temperature(t_ticks), 2)
        state.reading.humidity = round(sht_humidity(rh_ticks), 2)

    # ------------------------------------------------------------------
    # SGP40
    # ------------------------------------------------------------------

    async def sgp40_trigger(self, device_id: str, state: MultisensorState) -> None:
        """Start a raw VOC measurement, compensated with the SHT41 values if known."""

        rh_ticks = state.rh_ticks if state.rh_ticks is not None else SGP40_DEFAULT_RH_TICKS
        t_ticks = state.t_ticks if state.t_ticks is not None else SGP40_DEFAULT_T_TICKS
        payload = bytes(SGP40_CMD_MEASURE_RAW) + encode_words(rh_ticks, t_ticks)
        await self.bridge.write(device_id, SGP40_ADDRESS, payload)

    async def sgp40_fetch(self, device_id: str, state: MultisensorState) -> None:
        """Collect the raw signal and advance the VOC index algorithm."""

        (sraw,) = decode_words(await self.bridge.read(device_id, SGP40_ADDRESS, 3))
        state.reading.voc_raw = sraw
        index = state.voc.process(sraw)
        # The algorithm reports 0 while it is still in its initial blackout.
        state.reading.voc_index = index or None

    # ------------------------------------------------------------------
    # VEML7700
    # ------------------------------------------------------------------

    async def veml7700_sample(self, device_id: str, state: MultisensorState) -> None:
        """Read the ambient light and adjust the range for the next sample.

        The range is changed after a reading, never before it: the new setting
        only takes effect after a full integration, which the time until the
        next sample easily covers.
        """

        if not state.veml_configured:
            await self._veml7700_write_config(device_id, state)
            return

        data = await self.bridge.write_read(device_id, VEML7700_ADDRESS, bytes([VEML7700_REG_ALS]), 2)
        raw = int.from_bytes(data, "little")
        gain, integration_ms = VEML7700_RANGES[state.veml_range]

        if raw < 0xFFFF:
            state.reading.illuminance = round(veml7700_lux(raw, gain, integration_ms), 1)

        new_range = state.veml_range
        if raw > VEML7700_RAW_HIGH and state.veml_range < len(VEML7700_RANGES) - 1:
            new_range += 1
        elif raw < VEML7700_RAW_LOW and state.veml_range > 0:
            new_range -= 1
        if new_range != state.veml_range:
            state.veml_range = new_range
            await self._veml7700_write_config(device_id, state)

    async def _veml7700_write_config(self, device_id: str, state: MultisensorState) -> None:
        value = veml7700_config(state.veml_range)
        await self.bridge.write(
            device_id, VEML7700_ADDRESS, bytes([VEML7700_REG_ALS_CONF]) + value.to_bytes(2, "little")
        )
        state.veml_configured = True

    # ------------------------------------------------------------------
    # STCC4
    # ------------------------------------------------------------------

    async def stcc4_sample(self, device_id: str, state: MultisensorState) -> None:
        """Hand over compensation values and collect the latest CO2 reading.

        Runs the chip in continuous mode, which measures once a second on its
        own; a sample only reads out the most recent result. After a stop the
        chip needs a while before it takes the start command, so the first
        samples after a probe or a maintenance command may do nothing at all.
        """

        address = state.components.stcc4_address
        if address is None:
            return

        if not state.stcc4_running:
            if time.monotonic() < state.stcc4_ready_at:
                return
            await self.bridge.write(device_id, address, command(STCC4_CMD_START_CONTINUOUS))
            state.stcc4_running = True
            # The first result arrives about a second from now.
            return

        if state.t_ticks is not None and state.rh_ticks is not None:
            await self.bridge.write(
                device_id, address, command(STCC4_CMD_SET_RHT_COMPENSATION, state.t_ticks, state.rh_ticks)
            )
            await asyncio.sleep(STCC4_COMMAND_TIME)

        await self.bridge.write(device_id, address, command(STCC4_CMD_READ_MEASUREMENT))
        await asyncio.sleep(STCC4_COMMAND_TIME)
        try:
            data = await self.bridge.read(device_id, address, 12)
        except DS28E17Nack:
            # No new result since the last read; keep the previous value.
            return
        co2_raw, _t_raw, _rh_raw, _status = decode_words(data)
        co2 = to_int16(co2_raw)
        state.reading.co2 = co2 if co2 >= 0 else None

    async def stcc4_stop(self, device_id: str, state: MultisensorState) -> None:
        """Stop continuous measurement. Leave the chip alone for STCC4_STOP_TIME after."""

        address = self._stcc4_address(state)
        await self.bridge.write(device_id, address, command(STCC4_CMD_STOP_CONTINUOUS))
        state.stcc4_running = False
        state.stcc4_ready_at = time.monotonic() + STCC4_STOP_TIME

    async def stcc4_send(self, device_id: str, state: MultisensorState, code: int, *words: int) -> None:
        """Send one maintenance command to a stopped STCC4."""

        await self.bridge.write(device_id, self._stcc4_address(state), command(code, *words))

    async def stcc4_fetch_word(self, device_id: str, state: MultisensorState) -> int:
        """Read the one-word answer of a maintenance command."""

        (word,) = decode_words(await self.bridge.read(device_id, self._stcc4_address(state), 3))
        return word

    @staticmethod
    def _stcc4_address(state: MultisensorState) -> int:
        address = state.components.stcc4_address
        if address is None:
            raise DS28E17Error("no STCC4 fitted")
        return address
