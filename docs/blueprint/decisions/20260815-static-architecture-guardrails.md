---
id: 20260815-static-architecture-guardrails
status: accepted
migrated-from: docs/blueprint/DECISIONS.md
---

# Synchronize Static Architecture Guardrails with the Tooling

## Context

Some project rules describe recognizable source patterns rather than runtime behaviour: an
`EntityDescription` with a hardcoded `name=` or `icon=`, a frozen device-automation platform file, or an unscoped
device-registry lookup. The integration tests under `tests/` cannot carry these checks forward: that directory is
excluded by `.templatesyncignore` because initialization makes its imports domain-specific.

## Decision

Keep a conservative AST-based checker in `script/.lib/architecture_check.py`, expose it through
`script/architecture-check`, and run it from `script/lint` and `script/lint-check`. Discover tracked and untracked,
non-ignored integration sources with `git ls-files --cached --others --exclude-standard -z --
custom_components/*.py`. Keep the checker's regression tests in synchronized `script/tests/`, which `script/test`
also collects.

## Rationale

- `script/` is template-managed except for `script/hooks/`, so checker fixes and their tests are proposed together in
  downstream template-sync pull requests.
- AST inspection is more precise than text matching while requiring neither Home Assistant startup nor a domain- or
  class-prefix-specific test.
- The checker follows import aliases, local subclasses, literal dictionary unpacking, and known `DeviceRegistry`
  provenance. It does not reject an unrelated API client merely because it also defines `async_get_device()`.
- Ruff has no supported repository-local custom-rule interface; adding another lint framework would cost more than
  this small, dependency-free checker.

## Consequences

- The checker is a guardrail, not proof of the architecture. General `**kwargs`, factories, and values assembled by
  arbitrary data flow remain review concerns.
- Runtime behaviour still belongs in the domain-specific `tests/` suite; `script/tests/` is only for synchronized
  development tooling.
- Downstream repositories receive these changes only after their template-sync workflow opens a pull request and a
  maintainer merges it.
- New rules belong here only when they can be expressed conservatively with a low false-positive rate.
