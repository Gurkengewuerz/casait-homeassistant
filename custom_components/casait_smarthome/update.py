"""Firmware update for the casaIT bridge."""

from __future__ import annotations

from datetime import timedelta
import logging
from typing import Any

from homeassistant.components.update import UpdateDeviceClass, UpdateEntity, UpdateEntityFeature
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import CasaITConfigEntry
from .const import DOMAIN
from .firmware import (
    FirmwareError,
    FirmwareRelease,
    FirmwareTarget,
    async_download_image,
    async_fetch_releases,
    get_firmware_recovery,
    normalize_version,
)
from .helpers import build_bridge_device_info, build_bridge_slug, build_entity_id

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 1
SCAN_INTERVAL = timedelta(hours=6)

# The upload is most of the work; the rest is the bridge restarting into the image.
UPLOAD_SHARE = 90


async def async_setup_entry(
    hass: HomeAssistant,
    entry: CasaITConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the bridge firmware update, also for a bridge in firmware recovery."""

    target: FirmwareTarget | None = get_firmware_recovery(hass, entry.entry_id)
    if target is None:
        api = entry.runtime_data
        await api.async_wait_initialized()
        target = api
    async_add_entities([CasaITBridgeFirmwareUpdate(target, entry)], update_before_add=True)


class CasaITBridgeFirmwareUpdate(UpdateEntity):
    """Offer the latest stable bridge firmware and install any stable release.

    Any difference to the latest release counts as an update, not just a newer
    version: a bridge running a build between releases, or a newer one, gets back
    to the official release in one step. Older releases are installed by version.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "bridge_firmware"
    _attr_device_class = UpdateDeviceClass.FIRMWARE
    _attr_entity_category = EntityCategory.CONFIG
    _attr_supported_features = (
        UpdateEntityFeature.INSTALL
        | UpdateEntityFeature.PROGRESS
        | UpdateEntityFeature.SPECIFIC_VERSION
        | UpdateEntityFeature.RELEASE_NOTES
    )

    def __init__(self, target: FirmwareTarget, entry: CasaITConfigEntry) -> None:
        """Initialize the update entity for one bridge."""

        self._target = target
        self._entry = entry
        self._releases: list[FirmwareRelease] = []
        bridge_slug = build_bridge_slug(entry.entry_id, entry.unique_id)
        self._attr_unique_id = f"{entry.entry_id}_bridge_firmware"
        self.entity_id = build_entity_id("update", bridge_slug, "firmware")
        self._attr_device_info = build_bridge_device_info(entry.entry_id)

    @property
    def installed_version(self) -> str | None:
        """Return the firmware the bridge reported in its last ping."""

        version = self._target.firmware_version
        return normalize_version(version) if version else None

    @property
    def latest_version(self) -> str | None:
        """Return the newest stable release."""

        return self._releases[0].version if self._releases else None

    @property
    def release_url(self) -> str | None:
        """Return the page of the newest stable release."""

        return self._releases[0].url if self._releases else None

    def version_is_newer(self, latest_version: str, installed_version: str) -> bool:
        """Offer the latest release whenever the bridge runs anything else."""

        return latest_version != installed_version

    async def async_release_notes(self) -> str | None:
        """Return the notes of the newest stable release."""

        return self._releases[0].notes if self._releases else None

    async def async_update(self) -> None:
        """Look for stable releases."""

        try:
            self._releases = await async_fetch_releases(async_get_clientsession(self.hass))
        except FirmwareError as err:
            _LOGGER.debug("Could not load bridge firmware releases: %s", err.__cause__)

    async def async_install(self, version: str | None, backup: bool, **kwargs: Any) -> None:
        """Download a stable release, flash it and wait for the bridge to run it."""

        if not self._releases:
            await self.async_update()
        wanted = normalize_version(version) if version is not None else self.latest_version
        release = next((release for release in self._releases if release.version == wanted), None)
        if release is None:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="firmware_release_unknown",
                translation_placeholders={"version": str(version)},
            )

        self._report_progress(0)
        try:
            image = await async_download_image(async_get_clientsession(self.hass), release)
        except FirmwareError as err:
            self._report_progress(None)
            raise HomeAssistantError(translation_domain=DOMAIN, translation_key=err.reason) from err

        try:
            await self._target.async_install_firmware(
                image, release.version, lambda share: self._report_progress(share * UPLOAD_SHARE)
            )
        except FirmwareError as err:
            raise HomeAssistantError(translation_domain=DOMAIN, translation_key=err.reason) from err
        finally:
            self._report_progress(None)
            # Polling stopped for the upload; a reload sets the bridge up from scratch.
            self.hass.config_entries.async_schedule_reload(self._entry.entry_id)

    def _report_progress(self, percentage: float | None) -> None:
        """Show whether an installation runs, and how far it got."""

        self._attr_in_progress = percentage is not None
        self._attr_update_percentage = None if percentage is None else round(percentage)
        self.async_write_ha_state()
