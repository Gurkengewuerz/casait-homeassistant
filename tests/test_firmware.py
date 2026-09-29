"""Tests for bridge firmware releases, their verification, the OTA upload and the update entity."""

from __future__ import annotations

import hashlib
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

from aiohttp import web
from aiohttp.test_utils import TestServer
from bridge_fakes import FakeBridge
import pytest

from custom_components.casait_smarthome import firmware as firmware_module
from custom_components.casait_smarthome.api import CasaITApi
from custom_components.casait_smarthome.firmware import (
    FIRMWARE_RELEASES_URL,
    FirmwareError,
    FirmwareRelease,
    async_download_image,
    async_fetch_releases,
    async_upload_image,
    expected_checksum,
    normalize_version,
    parse_releases,
)
from custom_components.casait_smarthome.services.smbus_proxy import BridgeInfo
from custom_components.casait_smarthome.update import CasaITBridgeFirmwareUpdate
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.aiohttp_client import async_get_clientsession

IMAGE = bytes([0xE9]) + bytes(range(256)) * 40
BASE = "https://git.mc8051.de/casaIT/modules/releases/download"


def _release(tag: str, *, prerelease: bool = False, draft: bool = False, image: bool = True) -> dict[str, Any]:
    assets = [{"name": "SHA256SUMS.txt", "browser_download_url": f"{BASE}/{tag}/SHA256SUMS.txt"}]
    if image:
        assets.append({"name": "cb32.bin", "browser_download_url": f"{BASE}/{tag}/cb32.bin"})
    assets.append({"name": "dm117.hex", "browser_download_url": f"{BASE}/{tag}/dm117.hex"})
    return {
        "tag_name": tag,
        "html_url": f"https://git.mc8051.de/casaIT/modules/releases/tag/{tag}",
        "body": f"Firmware {tag}",
        "draft": draft,
        "prerelease": prerelease,
        "assets": assets,
    }


def _sums(image: bytes = IMAGE) -> str:
    return (
        "ea1a5abc61c432193652bfd7a7d9d5033cf29abeafe00fbb3fdaf348f860b226  led.hex\n"
        f"{hashlib.sha256(image).hexdigest()}  cb32.bin\n"
    )


# ---------------------------------------------------------------------------
# Releases
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_only_stable_releases_with_a_bridge_image_count() -> None:
    releases = parse_releases(
        [
            _release("v0.2.0", prerelease=True),
            _release("v0.1.1", draft=True),
            _release("v0.1.0", image=False),
            _release("v0.0.2"),
            _release("v0.0.1"),
        ]
    )

    assert [release.version for release in releases] == ["0.0.2", "0.0.1"]
    assert releases[0].image_url == f"{BASE}/v0.0.2/cb32.bin"
    assert releases[0].checksums_url == f"{BASE}/v0.0.2/SHA256SUMS.txt"
    assert parse_releases({"message": "not found"}) == []


@pytest.mark.unit
@pytest.mark.parametrize(
    ("raw", "normalized"),
    [("v0.0.1", "0.0.1"), ("0.0.1", "0.0.1"), ("1afd286", "1afd286"), ("dev", "dev"), ("vendor", "vendor")],
)
def test_versions_compare_without_the_tag_prefix(raw: str, normalized: str) -> None:
    assert normalize_version(raw) == normalized


@pytest.mark.unit
def test_checksums_are_read_in_sha256sum_format() -> None:
    sums = _sums()

    assert expected_checksum(sums, "cb32.bin") == hashlib.sha256(IMAGE).hexdigest()
    assert expected_checksum(sums, "inreg.hex") is None


@pytest.mark.unit
async def test_releases_are_fetched_from_forgejo(hass, aioclient_mock) -> None:
    aioclient_mock.get(FIRMWARE_RELEASES_URL, json=[_release("v0.0.1")])

    releases = await async_fetch_releases(async_get_clientsession(hass))

    assert [release.tag for release in releases] == ["v0.0.1"]


@pytest.mark.unit
async def test_an_unreachable_forgejo_is_reported(hass, aioclient_mock) -> None:
    aioclient_mock.get(FIRMWARE_RELEASES_URL, status=502)

    with pytest.raises(FirmwareError, match="firmware_releases_unavailable"):
        await async_fetch_releases(async_get_clientsession(hass))


def _stable() -> FirmwareRelease:
    return parse_releases([_release("v0.0.1")])[0]


@pytest.mark.unit
async def test_a_download_is_checked_against_the_release_checksums(hass, aioclient_mock) -> None:
    release = _stable()
    aioclient_mock.get(release.checksums_url, text=_sums())
    aioclient_mock.get(release.image_url, content=IMAGE)

    assert await async_download_image(async_get_clientsession(hass), release) == IMAGE


@pytest.mark.unit
@pytest.mark.parametrize(
    ("sums", "image", "reason"),
    [
        (_sums(), IMAGE[:-1], "firmware_checksum_mismatch"),
        ("0000  led.hex\n", IMAGE, "firmware_checksum_mismatch"),
        (_sums(b"PK\x03\x04"), b"PK\x03\x04", "firmware_invalid_image"),
    ],
)
async def test_a_bad_download_is_refused(hass, aioclient_mock, sums: str, image: bytes, reason: str) -> None:
    release = _stable()
    aioclient_mock.get(release.checksums_url, text=sums)
    aioclient_mock.get(release.image_url, content=image)

    with pytest.raises(FirmwareError, match=reason):
        await async_download_image(async_get_clientsession(hass), release)


# ---------------------------------------------------------------------------
# Upload
# ---------------------------------------------------------------------------


class OtaServer:
    """Stand-in for the bridge's ArduinoOTA endpoint that records what arrived."""

    def __init__(self, status: int = 200) -> None:
        self.status = status
        self.requests: list[dict[str, Any]] = []

    async def handle(self, request: web.Request) -> web.Response:
        self.requests.append(
            {
                "path": request.path,
                "auth": request.headers.get("Authorization"),
                "length": request.headers.get("Content-Length"),
                "chunked": request.headers.get("Transfer-Encoding"),
                "body": await request.read(),
            }
        )
        return web.Response(status=self.status)


async def _serve(ota: OtaServer) -> TestServer:
    app = web.Application()
    app.router.add_post("/sketch", ota.handle)
    server = TestServer(app, host="127.0.0.1")
    await server.start_server()
    return server


@pytest.mark.unit
async def test_the_image_is_posted_whole_with_the_bridge_credentials(hass, socket_enabled) -> None:
    ota = OtaServer()
    server = await _serve(ota)
    shares: list[float] = []
    try:
        with patch("custom_components.casait_smarthome.firmware.OTA_PORT", server.port):
            await async_upload_image(async_get_clientsession(hass), "127.0.0.1", IMAGE, shares.append)
    finally:
        await server.close()

    request = ota.requests[0]
    assert request["path"] == "/sketch"
    assert request["auth"] == "Basic YXJkdWlubzphZG1pbi0xMjM="
    assert request["length"] == str(len(IMAGE))
    assert request["chunked"] is None
    assert request["body"] == IMAGE
    assert shares[-1] == 1.0


@pytest.mark.unit
async def test_a_refused_image_fails_the_upload(hass, socket_enabled) -> None:
    server = await _serve(OtaServer(status=401))
    try:
        with (
            patch("custom_components.casait_smarthome.firmware.OTA_PORT", server.port),
            pytest.raises(FirmwareError, match="firmware_upload_failed"),
        ):
            await async_upload_image(async_get_clientsession(hass), "127.0.0.1", IMAGE, lambda _share: None)
    finally:
        await server.close()


# ---------------------------------------------------------------------------
# Restart into the new firmware
# ---------------------------------------------------------------------------


class RestartingBridge(FakeBridge):
    """Bridge double that comes back with another boot id and version after the upload."""

    host = "bridge.local"

    def __init__(self, version_after: str) -> None:
        super().__init__({}, boot_id=1, version="1afd286")
        self.version_after = version_after

    def restart(self, *_args: Any) -> None:
        self.boot_id, self.version = 2, self.version_after


async def _installing_api(hass, bridge: RestartingBridge) -> CasaITApi:
    api = CasaITApi(hass, bridge, "entry-test")  # type: ignore[arg-type] - Test double for SMBus.
    api.bridge_info = await bridge.ping_info()
    return api


@pytest.mark.unit
async def test_an_installation_waits_for_the_bridge_to_run_the_new_version(hass) -> None:
    bridge = RestartingBridge("v0.0.1")
    api = await _installing_api(hass, bridge)
    upload = AsyncMock(side_effect=lambda *args: bridge.restart())

    with (
        patch.object(firmware_module, "async_upload_image", upload),
        patch.object(firmware_module, "FIRMWARE_RESTART_POLL", 0.01),
    ):
        await api.async_install_firmware(IMAGE, "0.0.1", lambda _share: None)

    assert upload.await_args.args[1:3] == ("bridge.local", IMAGE)
    assert api.bridge_info == BridgeInfo(boot_id=2, uptime_s=0, version="v0.0.1")


@pytest.mark.unit
async def test_a_bridge_back_on_another_version_fails_the_installation(hass) -> None:
    bridge = RestartingBridge("1afd286")
    api = await _installing_api(hass, bridge)

    with (
        patch.object(firmware_module, "async_upload_image", AsyncMock(side_effect=lambda *args: bridge.restart())),
        patch.object(firmware_module, "FIRMWARE_RESTART_POLL", 0.01),
        pytest.raises(FirmwareError, match="firmware_not_confirmed"),
    ):
        await api.async_install_firmware(IMAGE, "0.0.1", lambda _share: None)


@pytest.mark.unit
async def test_a_bridge_that_never_restarts_fails_the_installation(hass) -> None:
    bridge = RestartingBridge("v0.0.1")
    api = await _installing_api(hass, bridge)

    with (
        patch.object(firmware_module, "async_upload_image", AsyncMock()),
        patch.object(firmware_module, "FIRMWARE_RESTART_POLL", 0.01),
        patch.object(firmware_module, "FIRMWARE_RESTART_TIMEOUT", 0.05),
        pytest.raises(FirmwareError, match="firmware_not_confirmed"),
    ):
        await api.async_install_firmware(IMAGE, "0.0.1", lambda _share: None)


# ---------------------------------------------------------------------------
# Update entity
# ---------------------------------------------------------------------------

ENTRY = SimpleNamespace(entry_id="entry-test", unique_id="AA:BB:CC:DD:EE:FF", options={})


def _entity(hass, version: str, releases: list[FirmwareRelease]) -> tuple[CasaITBridgeFirmwareUpdate, Any]:
    api = SimpleNamespace(firmware_version=version, async_install_firmware=AsyncMock())
    entity = CasaITBridgeFirmwareUpdate(api, ENTRY)  # type: ignore[arg-type] - Test double for CasaITApi.
    entity.hass = hass
    entity.async_write_ha_state = lambda: None  # type: ignore[method-assign] - Not added to a platform.
    entity._releases = releases  # noqa: SLF001
    return entity, api


def _releases(*tags: str) -> list[FirmwareRelease]:
    return parse_releases([_release(tag) for tag in tags])


@pytest.mark.unit
@pytest.mark.parametrize(
    ("installed", "offered"),
    [("v0.0.2", False), ("0.0.2", False), ("1afd286", True), ("v0.0.1", True), ("v0.0.3", True)],
)
def test_any_other_firmware_is_offered_the_latest_release(hass, installed: str, offered: bool) -> None:
    entity, _ = _entity(hass, installed, _releases("v0.0.2", "v0.0.1"))

    assert entity.latest_version == "0.0.2"
    assert (entity.state == "on") is offered


@pytest.mark.unit
async def test_installing_a_specific_release_flashes_it_and_reloads(hass) -> None:
    entity, api = _entity(hass, "1afd286", _releases("v0.0.2", "v0.0.1"))

    with (
        patch("custom_components.casait_smarthome.update.async_download_image", AsyncMock(return_value=IMAGE)),
        patch.object(hass.config_entries, "async_schedule_reload") as reload,
    ):
        await entity.async_install("v0.0.1", backup=False)

    assert api.async_install_firmware.await_args.args[:2] == (IMAGE, "0.0.1")
    reload.assert_called_once_with("entry-test")
    assert entity.in_progress is False


@pytest.mark.unit
async def test_an_unknown_release_is_refused_before_anything_happens(hass) -> None:
    entity, api = _entity(hass, "1afd286", _releases("v0.0.1"))

    with (
        patch.object(hass.config_entries, "async_schedule_reload") as reload,
        pytest.raises(HomeAssistantError),
    ):
        await entity.async_install("v9.9.9", backup=False)

    api.async_install_firmware.assert_not_awaited()
    reload.assert_not_called()


@pytest.mark.unit
async def test_a_failed_download_leaves_the_bridge_alone(hass) -> None:
    entity, api = _entity(hass, "1afd286", _releases("v0.0.1"))

    with (
        patch(
            "custom_components.casait_smarthome.update.async_download_image",
            AsyncMock(side_effect=FirmwareError("firmware_checksum_mismatch")),
        ),
        patch.object(hass.config_entries, "async_schedule_reload") as reload,
        pytest.raises(HomeAssistantError),
    ):
        await entity.async_install(None, backup=False)

    api.async_install_firmware.assert_not_awaited()
    reload.assert_not_called()
    assert entity.in_progress is False
