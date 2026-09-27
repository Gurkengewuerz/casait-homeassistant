"""Bridge firmware releases and their installation over the network.

Releases come from the public Forgejo repository of the casaIT modules. Only
published, stable releases that carry the bridge image count. An image is
checked against the release's SHA256SUMS.txt before it is sent to the bridge,
which takes it over HTTP on its ArduinoOTA port and restarts into it.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass
import hashlib
import logging
from typing import Any

from aiohttp import ClientError, ClientSession, ClientTimeout, encode_basic_auth

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
