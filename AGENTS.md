# AI Agent Instructions

This document provides guidance for AI coding agents working on this Home Assistant custom integration project.

## Project Overview

This is a Home Assistant custom integration that was generated from a blueprint template. The integration follows Home Assistant Core development patterns and quality standards.

**Integration details:**

- **Domain:** `casait_smarthome`
- **Title:** casaIT : Smart Home
- **Repository:** Gurkengewuerz/casait-homeassistant

**Key directories:**

- `custom_components/casait_smarthome/` - Main integration code
- `config/` - Home Assistant configuration for local testing
- `tests/` - Unit and integration tests
- `script/` - Development and validation scripts

**Local Home Assistant instance:**

**Always use the project's scripts** — do NOT craft your own `hass`, `pip`, `pytest`, or similar commands. The scripts handle environment setup, virtual environments, port management, and cleanup that raw commands miss. Agents that bypass scripts frequently break.

**Devcontainer CLI tools:** The devcontainer provides agent-facing tools including `bat`, `delta`/`git-delta`,
`eza`, `fd`, `fzf`, `httpie`, `hyperfine`, `ipython`, `jq`, `jo`, `miller`, `rg`, `shellcheck`, `shfmt`,
`sqlite3`, `tree`, `yq`, and `yamllint`. Prefer these explicit tools over assuming an editor extension exposes
an equivalent CLI. The installed `yq` is the Mike Farah variant.

**Start Home Assistant:**

```bash
./script/develop
```

**Force restart (when HA is unresponsive or port conflicts):**

```bash
pkill -f "hass --config" || true && pkill -f "debugpy.*5678" || true && ./script/develop
```

- Kills any existing instance (hass + debugpy on port 5678) and starts fresh
- Avoids state confusion and port conflicts

**When to restart:** After modifying Python files, `manifest.json`, `services.yaml`, translations, or config flow changes

**Reading logs:**

- Live: Terminal where `./script/develop` runs
- File: `config/home-assistant.log` (most recent), `config/home-assistant.log.1` (previous)

**Adjusting log levels:**

- Integration logs: `custom_components.casait_smarthome: debug` in `config/configuration.yaml`
- You can modify log levels when debugging - just restart HA after changes

**Context-specific instructions:**

If you're using GitHub Copilot, path-specific instructions in `.github/instructions/*.instructions.md` provide additional guidance for specific file types (Python, YAML, JSON, etc.). This document serves as the primary reference for all agents.

**Other agent entry points:**

- **Claude Code:** See [`CLAUDE.md`](CLAUDE.md) (pointer to this file)
- **ChatGPT Codex:** See [`CODEX.md`](CODEX.md) (pointer to this file)
- **Gemini:** See [`GEMINI.md`](GEMINI.md) (pointer to this file)
- **GitHub Copilot:** See [`.github/copilot-instructions.md`](.github/copilot-instructions.md) (compact version of this file)

## Working With Developers

**For workflow basics (small changes, translations, tests, session management):** See `.github/copilot-instructions.md` for quick-reference guidance.

### When Instructions Conflict With Requests

If a developer requests something that contradicts these instructions:

1. **Clarify the intent** - Ask if they want you to deviate from the documented guidelines
2. **Confirm understanding** - Restate what you understood to avoid misinterpretation
3. **Suggest instruction updates** - If this represents a permanent change in approach, offer to update these instructions
4. **Proceed once confirmed** - Follow the developer's explicit direction after clarification

### Maintaining These Instructions

**This project was recently initialized from a template.** Instructions should evolve as the project matures:

- Refine guidelines based on actual project needs
- Remove outdated rules that no longer apply
- Consolidate redundant sections to prevent bloat
- Keep files focused - Move architectural decisions to `docs/development/`

**Propose updates when:**

- You notice repeated deviations from documented patterns
- Instructions become outdated or contradict actual code
- New patterns emerge that should be standardized

### Documentation vs. Instructions

**Three types of content with clear separation:**

1. **Agent Instructions** - How AI should write code (`.github/instructions/`, `AGENTS.md`)
2. **Developer Documentation** - Architecture and design decisions (`docs/development/`)
3. **User Documentation** - End-user guides (`docs/user/`)

**AI Planning:** Use `.ai-scratch/` for temporary notes (never committed)

**Rules:**

- ❌ **NEVER** create random markdown files in code directories
- ❌ **NEVER** create documentation in `.github/` unless it's a GitHub-specified file
- ✅ **ALWAYS ask first** before creating permanent documentation
- ✅ **Prefer module docstrings** over separate markdown files

See `.github/copilot-instructions.md` for detailed documentation strategy.

### Session and Context Management

**Commit suggestions:**

When a task completes and the developer moves to a new topic, suggest committing changes. Offer a commit message based on the work done.

**Commit rules (CRITICAL):**

- Never commit automatically; commit only when the developer explicitly requests it
- A previous commit request is not standing permission for a later task
- Never ask about pushing; the developer handles `git push`

**Commit message format:** Follow [Conventional Commits](https://www.conventionalcommits.org/) and
`.github/instructions/blueprint.commit-message.instructions.md`.

**Common types:** `feat:`, `fix:`, `chore:`, `refactor:`, `docs:`

See `.github/copilot-instructions.md` for commit message examples and context monitoring guidance.

## Custom Integration Flexibility

**This is a CUSTOM integration, not a Home Assistant Core integration.** While we follow Core patterns for quality and maintainability, we have more flexibility in implementation decisions:

**Third-party libraries (PyPI):**

- ✅ Prefer existing PyPI libraries when maintained and fit the use case
- ✅ Build custom API client when:
  - Device/service uses simple REST API or GraphQL (HTTP, JSON)
  - Available libraries are unmaintained, bloated, or poorly designed
  - Using aiohttp + json is more maintainable than a framework

**Decision process:**

1. Research available libraries (PyPI, GitHub)
2. Evaluate: Maintained? Async? Well-documented? Dependency footprint?
3. Consider protocol: Simple REST → aiohttp; Complex OAuth2 → library; Standard (MQTT) → industry library
4. Document decision in `docs/development/DECISIONS.md`

**Quality Scale expectations:**

As an AI agent, **aim for Silver or Gold Quality Scale** when generating code:

- ✅ **Always implement:** Type hints, async patterns, proper error handling, service registration in `async_setup()`, diagnostics with `async_redact_data()`, device info
- 🎯 **When applicable:** Config flow with validation, reauth flow, discovery support, repair flows
- 📋 **Can defer:** Multiple config entries, advanced discovery, YAML import, extensive test coverage

**Developer expectation:** Generate production-ready code. Implement HA standards with reasonable effort.

**Other flexibility:** Discovery can be added later; breaking changes allowed with documentation; experimental features acceptable.

## Code Style and Quality

**Python:** 4 spaces, 120 char lines, double quotes, full type hints, async for all I/O

**YAML:** 2 spaces, modern HA syntax (no legacy `platform:` style)

**JSON:** 2 spaces, no trailing commas, no comments

**Validation:** Run `script/check` before committing (runs type-check + lint + spell)

**hassfest validation:** Run `script/hassfest` to validate against Home Assistant standards

- Validates manifest.json, translations, services.yaml, and integration structure
- Uses official Home Assistant Core validation scripts locally
- First run downloads ~27 MB, subsequent runs are fast with `--no-update`

**For comprehensive standards, see:**

- `.github/instructions/blueprint.python.instructions.md` - Python patterns, imports, type hints
- `.github/instructions/blueprint.yaml.instructions.md` - YAML structure and HA-specific patterns
- `.github/instructions/blueprint.json.instructions.md` - JSON formatting and schema validation
- `.github/instructions/blueprint.shell.instructions.md` - Shell style, `shfmt`, and `shellcheck`
- `.github/instructions/blueprint.commit-message.instructions.md` - commit message conventions

**GitHub Copilot users:** These instruction files are automatically provided based on file type.

## Project-Specific Rules

### Integration Identifiers

This integration uses the following identifiers consistently:

- **Domain:** `casait_smarthome`
- **Title:** casaIT : Smart Home
- **Class prefix:** `CasaIT`

**When creating new files:**

- Use the domain `casait_smarthome` for all DOMAIN references
- Prefix integration-specific classes with `CasaIT`
- Use "casaIT : Smart Home" as the display title
- Never hardcode different values

### Integration Structure

**Current package organization:**

- `api.py` - integration API, discovery, shared polling loop, state caches, and serialized writes
- `config_flow.py` - config flow and options flow
- `helpers.py` - option parsers and shared device-info helpers
- `binary_sensor.py`, `cover.py`, `light.py`, `sensor.py`, `switch.py` - entity platforms
- `services/smbus_proxy.py` - TCP bridge client
- `services/i2cClasses/` - synchronous I2C and 1-Wire hardware drivers
- `diagnostics.py` - redacted config-entry diagnostics

**Do NOT create:**

- `coordinator/` - the integration uses a free-running dispatcher-based poll loop, not a `DataUpdateCoordinator`
- `helpers/`, `ha_helpers/`, `common/`, `shared/`, or `lib/` packages - use the existing flat modules
- New top-level packages without explicit approval

**Key patterns:**

- Entities → `CasaITApi` → synchronous hardware drivers (never skip the API layer)
- Dispatcher-driven entities read API state caches; polling entities call async API methods
- Hardware drivers never own Home Assistant entities or access `hass`
- Keep the existing flat platform modules unless an approved refactor changes the architecture
- Use `EntityDescription` dataclasses for static entity metadata

**Code organization principles:**

- Keep files focused (200-400 lines per file)
- Split large modules into smaller ones when needed

**For detailed patterns, see:**

- `.github/instructions/blueprint.entities.instructions.md` - Entity platform patterns
- `.github/instructions/blueprint.api.instructions.md` - API client patterns

### Device Info

All entities should provide consistent device info. I2C modules are devices; each 1-Wire chip is a child device linked through its SM117 bus.

### Integration Manifest

**Key fields in `manifest.json`:**

**integration_type** (CRITICAL):

- `hub` - Gateway to multiple devices/services (e.g., Philips Hue bridge)
- `device` - Single device per config entry (e.g., ESPHome device)
- `service` - Single service per config entry (e.g., DuckDNS)
- `helper` - Helper entity (e.g., input_boolean, group)
- `virtual` - Points to another integration/IoT standard (not for custom integrations)

**Rule:** Hub vs Service/Device is defined by nature: Hub = gateway to multiple devices/services; Service/Device = one per config entry.

**quality_scale:**

- Required for Core integrations (minimum `bronze`)
- Optional for custom integrations (not displayed in HA UI)
- Levels: `bronze`, `silver`, `gold`, `platinum`, `internal`
- If included, serves as self-documentation of code quality goals
- See [Integration Quality Scale](https://developers.home-assistant.io/docs/core/integration-quality-scale)

**iot_class:**

- `cloud_polling`, `cloud_push`, `local_polling`, `local_push`, `assumed_state`, `calculated`

**dependencies vs after_dependencies:**

- `dependencies` - Required, integration won't load without them
- `after_dependencies` - Optional, waits if configured

**Discovery methods:** `bluetooth`, `dhcp`, `homekit`, `mqtt`, `ssdp`, `usb`, `zeroconf`

- Define matchers in manifest
- Requires corresponding `async_step_<method>()` in config flow
- Unique ID required for discovery

**single_config_entry:** Set `true` to allow only one config entry per integration

See `.github/instructions/blueprint.manifest.instructions.md` for comprehensive manifest documentation.

### Config Flow Best Practices

**Reserved step names:**

- Discovery: `bluetooth`, `dhcp`, `homekit`, `mqtt`, `ssdp`, `usb`, `zeroconf`
- System: `user`, `reauth`, `reconfigure`, `import`

**Unique ID requirements (CRITICAL):**

- Acceptable: Serial number, MAC address, device ID, account ID
- Unacceptable: IP address, device name, hostname, URL

**Reconfigure vs Reauth:**

- `reconfigure` - Change config data (host, settings)
- `reauth` - Handle expired credentials

**Config entry migration:**

- Define `VERSION` and `MINOR_VERSION` in ConfigFlow
- Implement `async_migrate_entry()` in `__init__.py`
- Update entry with `hass.config_entries.async_update_entry()`
- Return `False` to reject downgrades

**Scaffold commands:**

```bash
python3 -m script.scaffold config_flow_discovery  # Discoverable, no auth
python3 -m script.scaffold config_flow_oauth2     # OAuth2 flow
```

## Home Assistant Patterns

**Config flow:**

- Implement in `config_flow_handler/` package
- Support user setup, discovery, reauth, reconfigure
- Always set unique_id for discovered entries

See `.github/instructions/blueprint.config_flow.instructions.md` for comprehensive patterns.

**Service actions:**

- Define in `services.yaml` with full descriptions (legacy filename)
- Implement simple integration-wide handlers in `async_setup()`; introduce a separate module only when complexity warrants it
- **Register in `async_setup()`** - NOT in `async_setup_entry()` (Quality Scale!)
- Format: `<integration_domain>.<action_name>`

**Polling and API:**

- The integration intentionally uses a free-running poll loop (`fast_poll_interval`, 20 ms by default) with
  entry-scoped dispatcher signals. One cycle reads every module through as few batched frames as fit.
- Acquire the private hardware lock for one device transaction at a time so 1-Wire calls receive bus time
- Entities call async `CasaITApi` methods and never access driver objects or the hardware lock directly
- Do not introduce a `DataUpdateCoordinator` unless the polling architecture itself is deliberately replaced

**Entities:**

- Inherit from the Home Assistant platform entity base
- Read shared I2C state from `CasaITApi` caches or call its async 1-Wire methods
- Use `EntityDescription` for static metadata
- Every digital input - IM117 port, DM117 input slot, DS2413 input channel - is described by one
  `DigitalInputConfig` (role, device class, inversion, repeat); the role decides whether it becomes a
  binary sensor, an event entity, or nothing

See `.github/instructions/blueprint.entities.instructions.md` for entity patterns.

**Repairs:**

- Create `repairs.py` in integration root (Gold Quality Scale)
- Use `async_create_issue()` with severity levels (WARNING, ERROR, CRITICAL)
- Implement `RepairsFlow` for guided user fixes
- Delete issues after successful repair

See `.github/instructions/blueprint.repairs.instructions.md` for comprehensive patterns.

**Entity availability:**

- Set `_attr_available = False` when device is unreachable
- Update availability based on API state presence or the result of the entity's API read
- Don't raise exceptions from `@property` methods

**State updates:**

- Use `self.async_write_ha_state()` for immediate updates
- Let the API poll loop handle I2C updates and Home Assistant platform polling handle 1-Wire entities
- Minimize API calls (batch requests when possible)

**Setup failure handling:**

- `ConfigEntryNotReady` - Device offline/timeout, auto-retry, don't log manually (HA logs at debug)
- `ConfigEntryAuthFailed` - Expired credentials, triggers reauth flow, alternative: `entry.async_start_reauth()`

**Diagnostics:**

- **CRITICAL:** Use `async_redact_data()` from `homeassistant.helpers.redact` to remove sensitive data
- Redact: Passwords, API keys, tokens, location data, personal information

**YAML Configuration:**

⚠️ **DEPRECATED** for integrations communicating with devices/services (ADR-0010)

- New integrations MUST use config flow
- Existing YAML integrations should migrate to config flow
- Only helpers and system integrations may use YAML

## Validation Scripts

**Before committing, always run the full check-only suite:**

```bash
script/check
```

For focused work, use the targeted scripts:

| Changed files | Fix command       | Check command                               |
| ------------- | ----------------- | ------------------------------------------- |
| Python        | `script/python`   | `script/python-check` + `script/type-check` |
| YAML          | —                 | `script/yaml-check`                         |
| Shell         | `script/shell`    | `script/shell-check`                        |
| Markdown      | `script/markdown` | `script/markdown-check`                     |
| Spelling      | `script/spell`    | `script/spell-check`                        |

Use `script/lint` to format and check all supported file types. `script/lint-check` and `script/check` are
check-only and must not modify files.

**Configured tools:**

- **Ruff** - Fast Python linter and formatter ([Rules Reference](https://docs.astral.sh/ruff/rules/))
- **Pyright** - Type checker configured for "basic" mode ([Docs](https://microsoft.github.io/pyright/))
- **pytest** - Test runner with async support ([Docs](https://docs.pytest.org/))

**Generate code that passes these checks on first run.** As an AI agent, you should produce higher quality code than manual development:

- Type hints are trivial for you to generate
- Async patterns are well-known to you
- Import management is automatic for you
- Naming conventions can be applied consistently

Aim for zero validation errors in generated code. The developer expects production-ready output.

See `.github/instructions/blueprint.python.instructions.md` for linter overrides and error recovery strategies.

- You may use `# noqa: CODE` or `# type: ignore` when genuinely necessary
- Use sparingly and only with good reason (e.g., false positives, external library issues)
  See `.github/instructions/blueprint.python.instructions.md` for linter overrides and error recovery strategies.

### Error Recovery Strategy

**When validation fails (`script/check` errors):**

1. **First attempt** - Fix the specific error reported by the tool
2. **Second attempt** - If it fails again, reconsider your approach (maybe your understanding was wrong)
3. **Third attempt** - If still failing, ask for clarification rather than looping indefinitely
4. **After 3 failed attempts** - Stop and explain what you tried and why it's not working

**When tool operations fail:**

- **File read/write errors** - Verify path exists, check for typos, try once more
- **Terminal timeouts** - Don't retry automatically; inform the user and suggest manual intervention
- **API/network timeouts in tests** - Mention in response, don't silently ignore
- **Git operations fail** - Report the error immediately; don't attempt to work around it

**When gathering context:**

- Start with semantic_search (1-2 queries maximum)
- Read 3-5 most relevant files based on search results
- If still unclear, read 2-3 more specific files
- **After ~10 file reads, you should have enough context** - make a decision or ask for clarification
- Don't fall into infinite research loops

**Context gathering strategy:**

1. **First pass** - semantic_search to find relevant areas (1-2 queries)
2. **Second pass** - Read the 3-5 most relevant files identified
3. **Evaluate** - Do you have enough context to proceed? If yes, start implementation
4. **Third pass (if needed)** - Read 2-3 additional specific files for missing details
5. **Decision point** - After ~10 file reads total, you must either:
   - Proceed with implementation based on available context
   - Ask the developer specific questions about what's unclear
   - Never continue searching indefinitely without making progress

## Testing

**Test structure:**

- `tests/` mirrors `custom_components/casait_smarthome/` structure
- Use fixtures for common setup (Home Assistant mock, API, bus, etc.)
- Mock external API calls

**Running tests:**

```bash
script/test                           # All tests
script/test --cov-html                # With coverage report
script/test --snapshot-update         # Update Syrupy snapshots
```

See `.github/instructions/blueprint.tests.instructions.md` for comprehensive testing patterns.

## Breaking Changes

**Always warn the developer before making changes that:**

- Change entity IDs or unique IDs (users' automations will break)
- Modify config entry data structure (existing installations will fail)
- Change state values or attributes format (dashboards and automations affected)
- Alter service call signatures (user scripts will break)
- Remove or rename config options (users must reconfigure)

**Never do without explicit approval:**

- Removing config options (even if "unused")
- Changing service parameters or return values
- Modifying how data is stored in config entries
- Renaming entities or changing their device classes
- Changing unique_id generation logic

**How to warn:**

> "⚠️ This change will modify the entity ID format from `sensor.device_name` to `sensor.device_name_sensor`. Existing users' automations and dashboards will break. Should I proceed, or would you prefer a migration path?"

**When breaking changes are necessary:**

- Document the breaking change in commit message (`BREAKING CHANGE:` footer)
- Consider providing migration instructions
- Suggest version bump (major version change)
- Update documentation if it exists

## File Changes

**Scope Management:**

**Single logical feature or fix:**

- Implement completely even if it spans 5-8 files
- Example: New sensor needs entity class + platform init + code → implement all together
- Example: Bug fix requires changes in API + entity + error handling → do all at once

**Multiple independent features:**

- Implement one at a time
- After completing each feature, suggest committing before proceeding to the next

**Large refactoring (>10 files or architectural changes):**

- Propose a plan first before starting implementation
- Get explicit confirmation from developer

**Important: Do NOT create or modify tests unless explicitly requested.** Focus on implementing functionality. The developer decides when and if tests are needed.

**Translation strategy:**

- Use placeholders in code (e.g., `"config.step.user.title"`) - functionality works without translations
- Update `en.json` only when asked or at major feature completion
- NEVER update other language files automatically - extremely time-consuming
- Ask before updating multiple translation files
- Priority: Business logic first, translations later

See `.github/copilot-instructions.md` for detailed workflow guidance.

## Research and Validation

**When uncertain, consult official documentation:**

- **Always check current patterns** in [Home Assistant Developer Docs](https://developers.home-assistant.io/)
- **Read the blog** at [Home Assistant Developer Blog](https://developers.home-assistant.io/blog/) for recent changes and best practices
- **Search for examples** using Google: `site:developers.home-assistant.io [your topic]`
- **Verify with tools** before assuming - run `script/check` to catch issues early

**Don't rely on assumptions:**

- Home Assistant APIs and patterns evolve frequently
- What worked in older versions may be deprecated
- Use official docs and working examples over guesswork
- When in doubt, search for recent integration examples in Home Assistant Core

**Tool documentation:**

- [Ruff Rules](https://docs.astral.sh/ruff/rules/) - Understand what each rule checks
- [Pyright Configuration](https://microsoft.github.io/pyright/#/configuration) - Type checking options
- Don't hesitate to look up specific error codes when validation fails

## Tool Parallelization

**Safe to call in parallel:**

- Multiple `read_file` operations (different files or different sections of same file)
- `file_search` + `read_file` + `grep_search` (independent read-only operations)
- `semantic_search` followed by parallel `read_file` of results (but only 1 semantic_search at a time)

**Never call in parallel:**

- Multiple `run_in_terminal` commands (execute sequentially, wait for output)
- Multiple `replace_string_in_file` on the same file (use `multi_replace_string_in_file` instead)
- `semantic_search` with other `semantic_search` (execute one at a time)

**Best practices:**

- Batch independent read operations together in one parallel call
- After gathering context in parallel, provide brief progress update before proceeding
- For file edits, use `multi_replace_string_in_file` when making multiple changes
- Terminal commands must always be sequential to see output before next command

## Additional Resources

- [Home Assistant Developer Docs](https://developers.home-assistant.io/) - Primary reference
- [Integration Quality Scale](https://developers.home-assistant.io/docs/integration_quality_scale_index)
- [Architecture Docs](https://developers.home-assistant.io/docs/architecture_index)
- [Ruff Rules](https://docs.astral.sh/ruff/rules/) - Linter documentation
- [Pyright Configuration](https://microsoft.github.io/pyright/#/configuration) - Type checker documentation
- [pytest Documentation](https://docs.pytest.org/) - Testing framework
- See `CONTRIBUTING.md` for contribution guidelines
