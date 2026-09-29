"""Settings backups, so a bridge can be set up again without configuring everything anew.

A backup is a JSON file in ``<config>/casait_smarthome_backups``. It holds the
bridge connection, every option, the bridge part of the entity IDs and the relay
wear counters. The integration writes one whenever an entry is set up, which
includes every reload after the options changed, and refreshes it daily; the
``backup_settings`` action writes one on demand. The newest few per bridge are
kept, and since they live in the configuration directory, Home Assistant's own
backups carry them too.

Restoring works in two places: a new entry offers the backups found when it is
added, and the options of an existing entry can take over the options of one.
A new entry restored from a backup keeps the bridge's old entity IDs, so
automations and dashboards keep working, and continues its relay counters.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.json import save_json
from homeassistant.helpers.selector import SelectOptionDict
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util
from homeassistant.util.json import load_json_object

from .const import CONF_RESTORE_FROM, CONF_TIMEOUT, DOMAIN
from .helpers import entry_bridge_slug, migrate_options_to_nested
from .wear import STORAGE_VERSION as WEAR_STORAGE_VERSION

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry

    from .api import CasaITApi

_LOGGER = logging.getLogger(__name__)

BACKUP_DIR = f"{DOMAIN}_backups"
BACKUP_FORMAT = 1
# Backups kept per bridge; each change of the settings starts a new one.
KEEP_BACKUPS = 10
REFRESH_INTERVAL = timedelta(days=1)
NO_BACKUP = "none"


@dataclass(frozen=True)
class SettingsBackup:
    """One backup file and what it holds."""

    name: str
    created: datetime
    title: str
    bridge_slug: str
    unique_id: str | None
    data: dict[str, Any]
    options: dict[str, Any]
    relay_wear: dict[str, Any]

    @property
    def host(self) -> str:
        """Return the address the bridge had when the backup was written."""

        return str(self.data.get(CONF_HOST) or "")

    @property
    def label(self) -> str:
        """Describe the backup for a picker; the slug tells bridges apart."""

        created = dt_util.as_local(self.created).strftime("%Y-%m-%d %H:%M")
        title = self.title if self.title == self.host else f"{self.title} ({self.host})"
        return f"{title} · {self.bridge_slug} · {created}"

    def matches(self, host: str | None, unique_id: str | None) -> bool:
        """Return whether the backup was written for this bridge."""

        if unique_id and self.unique_id:
            return unique_id == self.unique_id
        return bool(host) and host == self.host


def _backup_dir(hass: HomeAssistant) -> Path:
    return Path(hass.config.path(BACKUP_DIR))


def _parse(name: str, raw: dict[str, Any]) -> SettingsBackup | None:
    """Read a backup file's content, None when it is not one this version understands."""

    if raw.get("domain") != DOMAIN or raw.get("format") != BACKUP_FORMAT:
        return None
    try:
        created = dt_util.parse_datetime(str(raw["created"]))
        entry = raw["entry"]
        options = dict(entry["options"])
        if int(entry.get("version", 3)) < 3:
            options = migrate_options_to_nested(options)
        if created is None:
            return None
        return SettingsBackup(
            name=name,
            created=created,
            title=str(entry.get("title") or ""),
            bridge_slug=str(raw["bridge_slug"]),
            unique_id=str(entry["unique_id"]) if entry.get("unique_id") else None,
            data=dict(entry["data"]),
            options=options,
            relay_wear=dict(raw.get("relay_wear") or {}),
        )
    except KeyError, TypeError, ValueError:
        return None


def _list_backups(directory: Path) -> list[SettingsBackup]:
    backups = []
    for path in directory.glob("*.json"):
        try:
            backup = _parse(path.name, load_json_object(path))
        except Exception:  # noqa: BLE001 - a broken file must not hide the others
            _LOGGER.warning("Skipping unreadable settings backup %s", path)
            continue
        if backup is not None:
            backups.append(backup)
    return sorted(backups, key=lambda backup: backup.created, reverse=True)


async def async_list_backups(hass: HomeAssistant, *, exclude_slugs: set[str] | None = None) -> list[SettingsBackup]:
    """Return every readable backup, newest first.

    ``exclude_slugs`` leaves out the bridges whose entity IDs an entry holds.
    """

    backups = await hass.async_add_executor_job(_list_backups, _backup_dir(hass))
    return [backup for backup in backups if backup.bridge_slug not in (exclude_slugs or set())]


async def async_get_backup(hass: HomeAssistant, name: str) -> SettingsBackup | None:
    """Return one backup by its file name."""

    return next((backup for backup in await async_list_backups(hass) if backup.name == name), None)


def backup_choices(backups: list[SettingsBackup], *, include_none: bool) -> list[SelectOptionDict]:
    """Offer the backups in a select selector."""

    choices: list[SelectOptionDict] = [{"value": NO_BACKUP, "label": "—"}] if include_none else []
    choices.extend({"value": backup.name, "label": backup.label} for backup in backups)
    return choices


def _write(directory: Path, slug: str, content: dict[str, Any]) -> str:
    """Write a backup; replace the newest one when only the counters changed."""

    directory.mkdir(parents=True, exist_ok=True)
    own = sorted(directory.glob(f"{slug}_*.json"), reverse=True)
    target = directory / f"{slug}_{dt_util.utcnow().strftime('%Y%m%d-%H%M%S')}.json"
    if own:
        try:
            latest = load_json_object(own[0])
        except Exception:  # noqa: BLE001
            latest = {}
        if latest.get("entry") == content["entry"]:
            target = own[0]
    save_json(str(target), content)
    for stale in sorted(directory.glob(f"{slug}_*.json"), reverse=True)[KEEP_BACKUPS:]:
        stale.unlink(missing_ok=True)
    return target.name


async def async_write_backup(hass: HomeAssistant, entry: ConfigEntry, api: CasaITApi | None) -> str:
    """Save the entry's settings and return the file name."""

    slug = entry_bridge_slug(entry)
    content = {
        "domain": DOMAIN,
        "format": BACKUP_FORMAT,
        "created": dt_util.utcnow().isoformat(),
        "bridge_slug": slug,
        "entry": {
            "title": entry.title,
            "version": entry.version,
            "unique_id": entry.unique_id,
            "data": {key: entry.data[key] for key in (CONF_HOST, CONF_PORT, CONF_TIMEOUT) if key in entry.data},
            "options": dict(entry.options),
        },
        "relay_wear": api.wear.diagnostics() if api is not None else {},
    }
    return await hass.async_add_executor_job(_write, _backup_dir(hass), slug, content)


async def async_setup_backups(hass: HomeAssistant, entry: ConfigEntry, api: CasaITApi) -> None:
    """Back the entry up now and once a day while it is loaded."""

    async def backup(*_: Any) -> None:
        try:
            await async_write_backup(hass, entry, api)
        except OSError as err:
            _LOGGER.warning("Could not write the settings backup: %s", err)

    await backup()
    entry.async_on_unload(async_track_time_interval(hass, backup, REFRESH_INTERVAL))


async def async_prepare_restore(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """On the first setup of a restored entry, take over the backup's relay counters.

    Runs before the API loads its stores. The marker is dropped either way, so a
    missing backup costs the counters and nothing else.
    """

    if (name := entry.data.get(CONF_RESTORE_FROM)) is None:
        return
    backup = await async_get_backup(hass, str(name))
    if backup is not None and backup.relay_wear:
        store: Store[dict[str, Any]] = Store(hass, WEAR_STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}.wear")
        await store.async_save(backup.relay_wear)
        _LOGGER.info("Took over the relay counters of settings backup %s", name)
    _drop_restore_marker(hass, entry)


@callback
def _drop_restore_marker(hass: HomeAssistant, entry: ConfigEntry) -> None:
    data = {key: value for key, value in entry.data.items() if key != CONF_RESTORE_FROM}
    hass.config_entries.async_update_entry(entry, data=data)
