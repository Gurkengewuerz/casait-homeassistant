# Claude Code Instructions

This repository uses a shared AI agent instruction system. **All instructions are in [`AGENTS.md`](AGENTS.md).**

Read `AGENTS.md` completely before starting any work. It contains:

- Project overview and integration identifiers
- Package structure and architectural rules
- Code style, validation commands, and quality expectations
- Home Assistant patterns (config flow, API polling, entities, services)
- Error recovery strategy and breaking change policy
- Workflow rules (scope management, translations, documentation)

## Quick Reference

- **Domain:** `casait_smarthome`
- **Title:** casaIT : Smart Home
- **Class prefix:** `CasaIT`
- **Main code:** `custom_components/casait_smarthome/`
- **Validate:** `script/check` (type-check + lint + spell)
- **Test:** `script/test`
- **Run HA:** `./script/develop`

## Path-Specific Instructions

Additional domain-specific guidance is available in `.github/instructions/*.instructions.md`.
These files use `applyTo` globs to indicate which files they cover.
Consult the relevant instruction file when working on specific file types:

- `blueprint.python.instructions.md` — Python style, async patterns, HA imports
- `blueprint.entities.instructions.md` — Entity platform patterns, inheritance
- `blueprint.config_flow.instructions.md` — Config flow, reauth, discovery
- `blueprint.api.instructions.md` — casaIT API and serialized hardware access
- `blueprint.services_yaml.instructions.md` — Service action definitions
- `blueprint.translations.instructions.md` — Translation file structure
- `blueprint.commit-message.instructions.md` — Conventional Commits format (applies to
  every commit, not to a file glob)
