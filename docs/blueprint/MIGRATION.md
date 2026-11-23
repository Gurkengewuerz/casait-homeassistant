# Migrating an Existing Custom Integration to This Blueprint

This guide is for developers who already have a custom integration and want to adopt this
blueprint's project structure, DevContainer environment, and tooling.

> [!NOTE]
> This file is part of the blueprint template and is removed automatically when you run
> `./initialize.sh`. That is deliberate — by that point the decisions below are made, and
> the rest of the migration is driven by the `blueprint-import` agent skill, which stays.
> The original lives at
> [jpawlowski/ha-integration-blueprint](https://github.com/jpawlowski/ha-integration-blueprint/blob/main/docs/blueprint/MIGRATION.md).

## The Core Idea

The only thing that really needs to come from your existing repository is the contents of
`custom_components/<your_domain>/`. Everything else — DevContainer, scripts, workflows,
configuration — comes from the blueprint and stays as-is (or is extended via hooks).

## The Two Decisions Only You Can Make

### 1. Keep your existing repository

> [!IMPORTANT]
> Issues, pull requests, stars, releases, and the catalog listing are all tied to the repository
> URL — not to the git history inside it. Creating a new repository from the template and
> abandoning the old one means losing all of that, and leaving your users subscribed to a
> repository that no longer updates.

Bring the blueprint **into** your existing repository. The recommended way overlays an
initialised blueprint onto your `main` as one pull request: your commit history stays, nothing
is force-pushed, and your integration folder is untouched. `script/import-overlay` does the
mechanical part and lists the files left to decide. The procedure, the alternatives and the
repository settings to re-check afterwards are in
[`.agents/skills/blueprint-import/references/git-strategy.md`](../../.agents/skills/blueprint-import/references/git-strategy.md).

### 2. Your domain does not change

`./initialize.sh` must be run with the domain your integration **already** has. The domain is
the primary key of every config entry and the prefix of every entity ID your users have wired
into automations and dashboards. Changing it during the migration does not migrate anything —
it orphans every existing installation.

The same caution applies to `--namespace`: passing the class prefix your code already uses
means no rename happens at all, which is the safest option.

## Step by Step, From Your Existing Project

You start where you already are: in your integration's repository, with a clean working tree. The blueprint never
becomes a second repository you work in — it is cloned to a temporary folder once, and `script/import-overlay` brings
it into your project on a new branch. Your history, issues and releases stay where they are.

### With your coding agent

Open your project with your agent and paste this prompt. It works with any agent that can run shell commands.

```text
Bring this repository onto the Home Assistant integration blueprint (jpawlowski/ha-integration-blueprint),
keeping its git history. Do not commit, push or open anything until I say so.

1. Clone the blueprint to /tmp/ha-integration-blueprint, outside this repository. Read its
   .agents/skills/blueprint-import/SKILL.md and references/git-strategy.md, and follow them over your own habits.
2. Do phase 0 of that skill: record this integration's domain, the unique ID format of the config entry and of
   every entity, the keys in entry.data and entry.options, the service action names and fields, and every
   entity's device class, state class, unit and attributes, in .agents/scratch/import-baseline.md.
   Ask me for anything you cannot derive from the code.
3. Propose the title, class prefix, repository (owner/name) and author for initialize.sh from what this
   repository already uses, and wait for my confirmation. The domain is taken from custom_components/.
4. Run /tmp/ha-integration-blueprint/script/import-overlay --target . -- with those options. It creates the
   branch chore/adopt-blueprint and stages the overlay.
5. Work through .agents/scratch/import-overlay.md: for every file in the first two groups, restore it, merge
   it, or leave it out, and keep a list of each decision with its reason.
6. Run script/setup/bootstrap, then script/lint, script/type-check, script/hassfest and script/test until they
   pass, changing the integration code as little as possible — phase 2 of the skill.
7. Sort the old agent instructions listed in the report as references/agent-knowledge.md describes, into
   .agents/scratch/import-agent-knowledge.md. Write no rule, skill or decision record yet.
8. Stop and report: your decisions, what passes, what does not, every conflict and decision from step 7, and
   what you need from me.
```

From then on the blueprint's agent instructions and skills are in your project, so the later phases — modernising
APIs, restructuring, applying the code rules — are ordinary requests to the same agent.

### By hand

1. Clone the blueprint next to your project — not into it:

   ```bash
   git clone https://github.com/jpawlowski/ha-integration-blueprint.git /tmp/ha-integration-blueprint
   ```

2. Write down your baseline (phase 0 of the [`blueprint-import`](../../.agents/skills/blueprint-import/SKILL.md)
   skill): the unique IDs, entry data keys and action names your users depend on.
3. From your project's root, on an up-to-date `main` with a clean working tree:

   ```bash
   /tmp/ha-integration-blueprint/script/import-overlay --target . -- \
     --title "My Integration" --namespace MyIntegration --repo me/my-integration --author "Jane Doe"
   ```

   Use the title, class prefix and repository your integration already has; the domain comes from your
   `custom_components/` folder.

4. Open `.agents/scratch/import-overlay.md`. For each file only your old repository had, restore it with
   `git checkout HEAD -- <path>` or leave it out; for each file both have, carry over what you added
   (`git show HEAD:<path>` shows your version).
5. Run `script/setup/bootstrap`, then `script/lint`, `script/type-check`, `script/hassfest` and `script/test`
   until they pass — or reopen the project in the dev container the blueprint brought, which does the bootstrap.
6. Commit, push the branch, and open a pull request. Merge it once its checks pass.

## Then Hand Over to the Agent Skill

Everything after the overlay — getting it green, modernising deprecated Home Assistant APIs, restructuring into the
blueprint's packages, and applying this project's code rules to code that predates them — is an ordered procedure
with a phase order that matters. It lives in the
[`blueprint-import`](../../.agents/skills/blueprint-import/SKILL.md) skill, which your agent loads automatically once
the overlay brought it into your project.

Read it yourself if you are migrating by hand. In particular, phase 0 — recording your current entity IDs, unique
IDs, entry data keys, and action names before touching anything — is what makes the rest of the migration verifiable.

## See Also

- [ARCHITECTURE.md](../development/ARCHITECTURE.md) — the package layout the blueprint expects
- [CUSTOMIZATION.md](../development/CUSTOMIZATION.md) — template sync, `.templatesyncignore`, and hook scripts
- [`blueprint-tooling`](../../.agents/skills/blueprint-tooling/SKILL.md) — the validation scripts,
  and keeping `manifest.json` requirements in sync with `requirements.txt`
