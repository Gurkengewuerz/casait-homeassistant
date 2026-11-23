---
id: 20261123-optional-skills-as-plugins
status: accepted
---

# Ship Optional Agent Workflow Skills as Plugins from a Separate Marketplace

## Context

The skills in `.agents/skills/` load in every session of every repository made from the blueprint, and a developer can
turn none of them off; overriding one with an own skill costs context too. Interviewing, planning, the review pass and
backlog triage are working habits, not knowledge of this repository's layout, and a developer with a flow of their own
rejects them as a whole.

## Decision

These skills live in [ha-custom-integration-skills](https://github.com/jpawlowski/ha-custom-integration-skills), one
plugin per unit a developer would turn off together: `haci-plan`, `haci-review` (both on by default) and `haci-triage`
(available). They know no template; the blueprint's `AGENTS.md` names what they read from it. `.claude/settings.json`
pins the marketplace tag, `.github/copilot/settings.json` and `.codex/config.toml` repeat it,
`script/setup/agent-plugins` checks that the three agree and installs from bootstrap, and Renovate raises all three in
one pull request. The review pass is no longer a rule in `AGENTS.md`; it lives only in `haci-review`.

## Considered options

**Move every skill.** Most `ha-*` skills follow `script/*` and `.agents/instructions/`, which a plugin cannot ship and
which release with the blueprint. Generalising them is a separate step that keeps the path-scoped rules.

**Keep everything in the repository.** Nothing could be turned off, and nobody outside the blueprint could use them.

## Consequences

- OpenCode has no plugin marketplace that carries skills, so these skills do not reach it.
- Claude Code reads a marketplace from project settings only in a trusted folder, so bootstrap installs at local scope.
- A plugin release that expects something new from a project waits for the blueprint release that provides it.
