"""Bridge firmware releases and their installation over the network.

Releases come from the public Forgejo repository of the casaIT modules. Only
published, stable releases that carry the bridge image count. An image is
checked against the release's SHA256SUMS.txt before it is sent to the bridge,
which takes it over HTTP on its ArduinoOTA port and restarts into it.

A bridge whose firmware is too old for the integration still gets this far: the
entry then loads in recovery mode with nothing but the update entity, so the fix
does not need PlatformIO.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass
import hashlib
import logging
import time
from typing import Any, Protocol

from aiohttp import ClientError, ClientSession, ClientTimeout, encode_basic_auth

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import DOMAIN
from .services.smbus_proxy import BridgeFirmwareError, BridgeInfo, SMBus, SMBusProxyError

_LOGGER = logging.getLogger(__name__)

FIRMWARE_RELEASES_URL = "https://git.mc8051.de/api/v1/repos/casaIT/modules/releases"
FIRMWARE_ASSET = "cb32.bin"
FIRMWARE_CHECKSUMS = "SHA256SUMS.txt"
RELEASE_PAGE_SIZE = 20

OTA_PORT = 65280
OTA_PATH = "/sketch"
# Fixed in the bridge firmware; there is nothing to configure on the bridge.
OTA_USER = "arduino"
OTA_PASSWORD = "admin-123"
OTA_CHUNK_SIZE = 8192

# First byte of every ESP32 application image.
ESP_IMAGE_MAGIC = 0xE9

DOWNLOAD_TIMEOUT = ClientTimeout(total=60)
UPLOAD_TIMEOUT = ClientTimeout(total=300, sock_connect=10)

FIRMWARE_RESTART_TIMEOUT = 120.0
FIRMWARE_RESTART_POLL = 2.0

# What the update entity shows for a bridge too old to report its version.
UNKNOWN_FIRMWARE = "unknown"
RECOVERY_KEY = "firmware_recovery"


class FirmwareError(Exception):
    """A firmware step failed; ``reason`` is the translation key that explains it."""

    def __init__(self, reason: str) -> None:
        """Store the translation key alongside a readable message."""

        super().__init__(reason)
        self.reason = reason


def normalize_version(version: str) -> str:
    """Return a version without the ``v`` a release tag carries, so tags and builds compare."""

    version = version.strip()
    if len(version) > 1 and version[0] in "vV" and version[1].isdigit():
        return version[1:]
    return version


@dataclass(frozen=True)
class FirmwareRelease:
    """One stable release that carries a bridge image."""

    version: str
    tag: str
    url: str
    notes: str
    image_url: str
    checksums_url: str | None


def parse_releases(data: Any) -> list[FirmwareRelease]:
    """Return the stable releases with a bridge image, newest first as the API lists them."""

    releases: list[FirmwareRelease] = []
    if not isinstance(data, list):
        return releases
    for item in data:
        if not isinstance(item, Mapping) or item.get("draft") or item.get("prerelease"):
            continue
        assets = {
            str(asset.get("name")): str(asset.get("browser_download_url"))
            for asset in item.get("assets") or []
            if isinstance(asset, Mapping) and asset.get("browser_download_url")
        }
        if FIRMWARE_ASSET not in assets or not (tag := str(item.get("tag_name") or "")):
            continue
        releases.append(
            FirmwareRelease(
                version=normalize_version(tag),
                tag=tag,
                url=str(item.get("html_url") or ""),
                notes=str(item.get("body") or ""),
                image_url=assets[FIRMWARE_ASSET],
                checksums_url=assets.get(FIRMWARE_CHECKSUMS),
            )
        )
    return releases


async def async_fetch_releases(session: ClientSession) -> list[FirmwareRelease]:
    """Load the stable bridge releases from Forgejo."""

    try:
        async with session.get(
            FIRMWARE_RELEASES_URL, params={"limit": RELEASE_PAGE_SIZE}, timeout=DOWNLOAD_TIMEOUT
        ) as response:
            response.raise_for_status()
            data = await response.json()
    except (ClientError, TimeoutError, ValueError) as err:
        raise FirmwareError("firmware_releases_unavailable") from err
    return parse_releases(data)


def expected_checksum(checksums: str, name: str) -> str | None:
    """Return the SHA-256 listed for ``name`` in a sha256sum style file."""

    for line in checksums.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].lstrip("*") == name:
            return parts[0].lower()
    return None


async def async_download_image(session: ClientSession, release: FirmwareRelease) -> bytes:
    """Download a release's bridge image and verify it against the release checksums."""

    if release.checksums_url is None:
        raise FirmwareError("firmware_checksum_mismatch")
    try:
        async with session.get(release.checksums_url, timeout=DOWNLOAD_TIMEOUT) as response:
            response.raise_for_status()
            checksums = await response.text()
        async with session.get(release.image_url, timeout=DOWNLOAD_TIMEOUT) as response:
            response.raise_for_status()
            image = await response.read()
    except (ClientError, TimeoutError) as err:
        raise FirmwareError("firmware_download_failed") from err

    expected = expected_checksum(checksums, FIRMWARE_ASSET)
    if expected is None or hashlib.sha256(image).hexdigest() != expected:
        raise FirmwareError("firmware_checksum_mismatch")
    if not image or image[0] != ESP_IMAGE_MAGIC:
        raise FirmwareError("firmware_invalid_image")
    return image


async def async_upload_image(
    session: ClientSession, host: str, image: bytes, progress: Callable[[float], None]
) -> None:
    """Send an image to the bridge's OTA endpoint, reporting the share sent so far.

    The bridge restarts once it has the whole image, sometimes before its answer
    is out. A connection that drops after the last byte left is therefore not an
    error here; the caller confirms the update by the bridge coming back with it.
    """

    sent = 0

    async def body() -> AsyncIterator[bytes]:
        nonlocal sent
        for offset in range(0, len(image), OTA_CHUNK_SIZE):
            chunk = image[offset : offset + OTA_CHUNK_SIZE]
            yield chunk
            sent += len(chunk)
            progress(sent / len(image))

    try:
        async with session.post(
            f"http://{host}:{OTA_PORT}{OTA_PATH}",
            data=body(),
            # The OTA server reads a Content-Length and cannot take a chunked body.
            headers={
                "Authorization": encode_basic_auth(OTA_USER, OTA_PASSWORD),
                "Content-Length": str(len(image)),
                "Content-Type": "application/octet-stream",
            },
            timeout=UPLOAD_TIMEOUT,
        ) as response:
            if response.status >= 400:
                _LOGGER.error("Bridge refused the firmware image: HTTP %s", response.status)
                raise FirmwareError("firmware_upload_failed")
    except (ClientError, TimeoutError) as err:
        if sent < len(image):
            raise FirmwareError("firmware_upload_failed") from err
        _LOGGER.debug("Bridge closed the connection after the image: %s", err)


async def async_flash_bridge(
    session: ClientSession,
    bus: SMBus,
    image: bytes,
    version: str,
    previous_boot_id: int | None,
    progress: Callable[[float], None],
) -> BridgeInfo | None:
    """Send an image to the bridge and wait until it restarted into ``version``.

    Returns None when the bridge runs ``version`` but that release is too old for
    this integration; the reload that follows sets the entry up in recovery mode.
    """

    await async_upload_image(session, bus.host, image, progress)

    deadline = time.monotonic() + FIRMWARE_RESTART_TIMEOUT
    while time.monotonic() < deadline:
        await asyncio.sleep(FIRMWARE_RESTART_POLL)
        try:
            info = await bus.ping_info()
        except BridgeFirmwareError as err:
            if err.boot_id == previous_boot_id:
                continue
            if normalize_version(err.version or "") != normalize_version(version):
                raise FirmwareError("firmware_not_confirmed") from err
            _LOGGER.info("Bridge runs firmware %s, which is too old for this integration", err.version)
            return None
        except SMBusProxyError as err:
            raise FirmwareError("firmware_not_confirmed") from err
        if info is None or info.boot_id == previous_boot_id:
            continue
        if normalize_version(info.version) != normalize_version(version):
            _LOGGER.error("Bridge restarted with firmware %s instead of %s", info.version, version)
            raise FirmwareError("firmware_not_confirmed")
        _LOGGER.info("Bridge runs firmware %s", info.version)
        return info
    raise FirmwareError("firmware_not_confirmed")


class FirmwareTarget(Protocol):
    """What the update entity needs from a bridge: its firmware, and a way to replace it."""

    @property
    def firmware_version(self) -> str | None:
        """Return the firmware the bridge reported, None before it answered."""

    async def async_install_firmware(self, image: bytes, version: str, progress: Callable[[float], None]) -> None:
        """Flash the bridge and wait until it runs ``version``."""


class CasaITFirmwareRecovery:
    """A bridge whose firmware is too old for anything but being updated."""

    def __init__(self, hass: HomeAssistant, bus: SMBus, version: str | None, boot_id: int | None) -> None:
        """Keep the connection the setup opened; the firmware check needs it afterwards."""

        self.hass = hass
        self.bus = bus
        self._version = version
        self._boot_id = boot_id

    @property
    def firmware_version(self) -> str | None:
        """Return what the old ping reported, or a placeholder when it carried no version."""

        return self._version or UNKNOWN_FIRMWARE

    async def async_install_firmware(self, image: bytes, version: str, progress: Callable[[float], None]) -> None:
        """Flash the bridge; the caller reloads the entry, which then sets up normally."""

        info = await async_flash_bridge(
            async_get_clientsession(self.hass), self.bus, image, version, self._boot_id, progress
        )
        if info is not None:
            self._version = info.version
            self._boot_id = info.boot_id


def get_firmware_recovery(hass: HomeAssistant, entry_id: str) -> CasaITFirmwareRecovery | None:
    """Return the recovery session of an entry that loaded in recovery mode."""

    return hass.data.get(DOMAIN, {}).get(RECOVERY_KEY, {}).get(entry_id)
