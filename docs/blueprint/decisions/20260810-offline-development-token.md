---
id: 20260810-offline-development-token
status: accepted
migrated-from: docs/blueprint/DECISIONS.md
---

# Mint the Development Access Token Offline, Through Home Assistant's Own AuthManager

## Context

Anything that wants to read the development instance — `script/ha`, an AI coding agent, a throwaway script — needs an access token. Creating one by hand in _Settings → Security → Long-lived access tokens_ makes every fresh environment depend on a human, and `script/setup/reset` wipes it again.

## Decision

`script/setup/seed-auth` boots Home Assistant's own `AuthManager` against the **stopped** config directory, the way `homeassistant/scripts/auth.py` does, and lets it write `config/.storage/auth`. The token lands in `config/.storage/dev_access_token` (mode `0600`), rotating automatically once fewer than 7 days of its 30-day life remain.

## Rationale

- Works on an already-onboarded instance without knowing the password, unlike the live `/api/onboarding/users` → `/auth/token` route.
- Home Assistant writes the store, so the schema, the JWT claim set, atomicity, and file mode stay its responsibility across version bumps. This deliberately diverges from `script/setup/seed-http-config`, which hand-crafts its store only because the `http` store has no equivalent API.
- The token file is read by `script/ha` itself, so no credential enters a command line, an environment variable, or an agent's context.

## Consequences

- **Home Assistant must be stopped.** `AuthStore.async_load()` schedules an unconditional save 300 s after boot that rewrites the whole store from memory, so a token injected into a running instance is silently discarded. `seed-auth` takes Home Assistant's own `.ha_run.lock` for the whole operation, which makes the race impossible in both directions.
- `hass.async_stop(force=True)` is required to flush: a freshly constructed `HomeAssistant` sits in `CoreState.not_running`, where the unforced `async_stop()` returns early without firing `EVENT_HOMEASSISTANT_FINAL_WRITE`.
- A Home Assistant **admin** credential exists in the devcontainer, which is effectively code execution inside it. Mitigated by the short lifespan, `0600`, a gitignored and sync-excluded location, redacted CLI output, and read-deny rules for Claude Code.
- On a fresh environment the browser onboarding still has to happen once; the token appears on the **second** `script/develop`.
