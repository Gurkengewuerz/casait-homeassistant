---
id: 20260810-own-development-cli
status: accepted
migrated-from: docs/blueprint/DECISIONS.md
---

# Own Development CLI Instead of `homeassistant-cli`

## Context

`script/ha` overlaps with [`homeassistant-cli`](https://github.com/home-assistant-ecosystem/home-assistant-cli) (`hass-cli`), which reached 1.0.0 in April 2026 and covers states, services, templates, config entries, and a `raw` escape hatch.

## Decision

Keep an in-repo CLI with no third-party dependency; `aiohttp` is already in the Home Assistant venv.

## Rationale

Evaluated against this devcontainer at HA 2026.8 / Python 3.14:

- Every WebSocket-backed `hass-cli` command (`entity list`, `device list`, `area list`, `event watch`, `raw ws`) fails with `RuntimeError: There is no current event loop` — `asyncio.get_event_loop()` at `remote.py:152`, removed behaviour in Python 3.12+.
- Its bounds `packaging<26.0` and `regex<2025.0.0` conflict with the venv's 26.3 and 2026.7.19, so installing it **downgrades two packages in the venv Home Assistant itself runs from**. It would need its own venv and a wrapper.
- It has no diagnostics download, no config/options/reconfigure flow driving, no `reason` on a config entry, no structured `system_log/list`, and no scoping to this repository's domain — the things integration development specifically needs.
- It solves none of the token problem, so `script/setup/seed-auth` is required either way.

## Consequences

This repository owns the CLI's maintenance, including tracking Home Assistant API changes. The endpoints it uses are the ones the frontend uses, which makes them unlikely to move quietly. Should `hass-cli` fix the Python 3.14 bug and relax its pins, this is worth revisiting.
