# GitHub Copilot Instructions

> **Comprehensive guidance:** Read [`AGENTS.md`](../AGENTS.md) before making changes.
> Path-specific `blueprint.*.instructions.md` files add rules for matching file types.

## Project Identity

- **Domain:** `casait_smarthome`
- **Title:** casaIT : Smart Home
- **Class prefix:** `CasaIT`
- **Main code:** `custom_components/casait_smarthome/`
- **Validate:** `script/check`
- **Test:** `script/test`
- **Run Home Assistant:** `./script/develop`

Use these identifiers consistently. Do not introduce template placeholders or alternative class prefixes.

## Architecture

The integration deliberately uses a flat, API-mediated hardware architecture:

```text
Home Assistant entities
        ↓
CasaITApi state caches and async methods
        ↓
serialized asyncio I2C and 1-Wire drivers
```

- `api.py` owns discovery, the free-running poll loop, caches, dispatcher signals, and serialized writes.
- Platform modules remain flat: `binary_sensor.py`, `cover.py`, `light.py`, `sensor.py`, and `switch.py`.
- Entities never access driver objects or the private hardware lock directly.
- I2C entities consume dispatcher-driven API state; 1-Wire entities use async API methods.
- Do not introduce `DataUpdateCoordinator`, `coordinator/`, `entity/`, `entity_utils/`, or `service_actions/`
  packages unless the architecture is explicitly redesigned.

## Workflow

- Use project scripts instead of raw `pytest`, `hass`, `pip`, Ruff, or Pyright commands.
- Use `script/lint` for fix-mode formatting and linting.
- Use `script/check` for the complete check-only suite.
- Use targeted scripts such as `script/python`, `script/shell`, and `script/markdown` when appropriate.
- Run `script/test` for tests and `script/hassfest` for Home Assistant validation.
- Preserve unrelated local changes and never commit unless the developer explicitly requests it for the current task.
- Commit messages follow `.github/instructions/blueprint.commit-message.instructions.md`.

## Implementation Rules

- Python uses 4 spaces, double quotes, complete type hints, 120-character lines, and async I/O.
- Keep hardware access behind `CasaITApi` and one transaction at a time under its lock.
- Use `EntityDescription` dataclasses for static metadata.
- Keep entity, unique, and device identifiers scoped to their config entry/bridge.
- Use `ConfigEntryNotReady` for unavailable hardware and avoid exceptions from entity properties.
- Diagnostics must use `async_redact_data()` for sensitive config-entry data.
- Service actions are registered from integration-wide `async_setup()`.
- Do not modify tests or permanent documentation unless the developer asks for that scope.

## Blueprint Maintenance

Tooling is synchronized from `jpawlowski/hacs.integration_blueprint`. Files protected by
`.templatesyncignore` are project-owned and require a manual merge. Keep the Home Assistant version aligned
across `hacs.json`, `requirements_test.txt`, and `.devcontainer/.env` with `script/ha-version-sync`.
