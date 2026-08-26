# Codex Migration Runbook — Agent Instructions

## Purpose

You are Codex performing or assisting a migration of this Agent Delegation Protocol from an older Codex installation to the current structure.

The current Codex structure is:

1. supplementary global `AGENTS.md` authorization and semantic policy;
2. custom worker agents in `$CODEX_HOME/agents/`;
3. lifecycle enforcement hooks in `$CODEX_HOME/hooks.json` plus `$CODEX_HOME/hooks/`.

This migration is **Codex-only**. You MUST NOT install, uninstall, modify, inspect for cleanup, or otherwise alter Claude Code configuration as part of this procedure.

## Mandatory migration rules

You MUST follow all of these rules:

- Preserve every pre-existing user instruction, hook, agent definition, and configuration value that is not owned by this repository.
- Do not overwrite an unrelated file or symlink merely to make installation succeed.
- Do not delete or replace an existing `AGENTS.md` or `AGENTS.override.md` without preserving its effective content through the repository's supported composition mechanism.
- Do not reset, clean, discard, or overwrite unrelated local Git changes in this repository.
- Do not use the removed legacy combined `scripts/install.*` or `scripts/uninstall.*` paths.
- Do not configure Claude as part of this migration.
- Do not globally force all Codex subagents to Luna. In particular, do not set `agents.default_subagent_model = "gpt-5.6-luna"` merely to implement this protocol.
- Do not bypass Codex hook trust/review requirements.
- If a destination is occupied by an unrelated file or symlink, STOP that part of the migration and report the exact conflict instead of replacing it.

## Recognize a legacy installation

Treat any of the following as a migration case:

- `$CODEX_HOME/AGENTS.md` is symlinked directly to this repository's `codex/AGENTS.md`, with no custom workers or enforcement hooks installed.
- An older composed `AGENTS.override.md` from this repository exists, but custom worker agents or hooks are missing.
- The repository was previously installed through the old combined installer that configured both Codex and Claude.
- The protocol exists only as text instructions and has not yet installed `bulk-worker.toml`, `balanced-worker.toml`, or `delegation-enforcer.py`.
- `hooks.json` exists but does not contain this protocol's owned lifecycle handlers.

A legacy installation is not an error. Migrate it in place without removing working supplementary instructions first.

## Phase 1 — Preflight inventory

Before making changes, determine:

```text
REPO_ROOT   = root of this cloned repository
CODEX_HOME  = $CODEX_HOME if set, otherwise ~/.codex
```

Inspect, without modifying:

```text
$CODEX_HOME/AGENTS.md
$CODEX_HOME/AGENTS.override.md
$CODEX_HOME/config.toml
$CODEX_HOME/hooks.json
$CODEX_HOME/agents/
$CODEX_HOME/hooks/
$CODEX_HOME/.delegation-protocol/
<REPO_ROOT>/.runtime/codex/
```

Also inspect the repository worktree state.

If the repository has unrelated uncommitted changes, preserve them. Do not run `git reset --hard`, `git clean`, destructive checkout commands, or equivalent cleanup.

If you need the newest repository version and the worktree can be updated safely, prefer:

```bash
git pull --ff-only
```

Do not force a pull through divergent history or local changes.

## Phase 2 — Run only the Codex installer

On macOS/Linux, from the repository root:

```bash
bash scripts/codex/install.sh
```

On Windows PowerShell:

```powershell
.\scripts\codex\install.ps1
```

Python 3 is required for hook enforcement.

The installer is the canonical migration mechanism. Do not manually reproduce its filesystem mutations unless the installer cannot run and you are specifically repairing a known installation conflict.

## Phase 3 — Expected migration result

After a successful migration, verify the following structure.

### A. Global authorization / semantic policy

If no prior global Codex instructions existed:

```text
$CODEX_HOME/AGENTS.md
  -> <REPO_ROOT>/codex/AGENTS.md
```

If prior global instructions existed, they MUST remain effective. The installer may activate a composed override:

```text
$CODEX_HOME/AGENTS.override.md
  -> <REPO_ROOT>/.runtime/codex/AGENTS.composed.md
```

The composed file MUST place the preserved pre-existing active global instructions before this repository's supplementary delegation protocol.

Do not simplify this into replacement of the user's instructions.

### B. Custom worker tiers

Verify:

```text
$CODEX_HOME/agents/bulk-worker.toml
  -> <REPO_ROOT>/codex/agents/bulk-worker.toml

$CODEX_HOME/agents/balanced-worker.toml
  -> <REPO_ROOT>/codex/agents/balanced-worker.toml
```

The intended routing is:

- `bulk_worker` → GPT-5.6 Luna for bounded mechanical/high-volume work;
- `balanced_worker` → GPT-5.6 Terra for moderately difficult delegated units;
- parent/frontier model → architecture, ambiguity, integration, conflict resolution, difficult reasoning, and final validation.

Do not weaken escalation by setting a global Luna default for every unspecified subagent.

### C. Enforcement hook

Verify:

```text
$CODEX_HOME/hooks/delegation-enforcer.py
  -> <REPO_ROOT>/codex/hooks/delegation-enforcer.py
```

and verify that `$CODEX_HOME/hooks.json` contains this protocol's handlers while retaining unrelated pre-existing hook definitions.

The protocol currently uses these events:

- `UserPromptSubmit`
- `SubagentStart`
- `SubagentStop`
- `PreToolUse`
- `PostToolUse` for `Agent`
- `Stop`

Do not replace the entire `hooks.json` file with a protocol-only file.

## Phase 4 — Hook trust and runtime capability

Mechanical enforcement is not complete until Codex trusts the installed non-managed hooks.

You MUST tell the user to restart Codex and run:

```text
/hooks
```

The user must review and trust/enable the Agent Delegation Protocol hook definition. Do not attempt to forge, bypass, or silently manufacture this trust decision.

Inspect `config.toml` only to diagnose runtime capability. If hooks or multi-agent behavior are explicitly disabled, report that condition. Do not silently override an explicit user or organization policy merely to make the protocol active.

## Phase 5 — Verification

Run the repository's Codex self-test when Python 3 is available:

```bash
python3 scripts/codex/test-protocol.py
```

Use the platform-appropriate Python executable if it is named differently.

Then verify all of the following:

1. existing global Codex instructions still apply;
2. this protocol is also active as supplementary guidance;
3. both custom worker definitions resolve to this clone;
4. the hook resolves to this clone;
5. unrelated `hooks.json` entries remain present;
6. a clear bulk task requires delegation before parent mutation;
7. a clearly independent multi-subsystem task requires multiple workers and evidence of real fan-out;
8. the parent remains responsible for integration and final validation.

Do not declare migration complete merely because files exist. The effective behavior and preservation requirements must also be satisfied.

## Legacy mixed-installer cleanup

The old combined installer no longer exists and MUST NOT be recreated.

If the old installation left Codex-owned state, the current Codex installer is designed to migrate the Codex portion in place. Do not remove Claude artifacts while cleaning Codex state.

If you discover a stale path that is clearly owned by a removed version of this repository but conflicts with the current installer, identify it precisely and prefer a reversible move/backup over deletion. Never infer ownership solely from a familiar filename; verify its target/content first.

## Failure handling

If migration fails:

1. stop further mutation;
2. preserve the current working state;
3. report the exact path, configuration value, or hook entry causing the conflict;
4. distinguish repository-owned state from unrelated user state;
5. repair only repository-owned state when ownership is certain;
6. rerun the Codex installer and verification after repair.

Do not solve an installation conflict by deleting unrelated configuration.

## Rollback

To remove only the current Codex protocol installation:

macOS/Linux:

```bash
bash scripts/codex/uninstall.sh
```

Windows PowerShell:

```powershell
.\scripts\codex\uninstall.ps1
```

Rollback MUST NOT uninstall or modify Claude Code.

The Codex uninstaller should remove only repository-owned links/hook entries/state and restore preserved Codex global override content when applicable.

## Completion report

When you finish a migration, report concisely:

- whether the installation was legacy AGENTS-only, partially migrated, or already current;
- which Codex structures were added or refreshed;
- whether existing instructions/hooks/configuration were preserved;
- self-test result;
- whether `/hooks` trust is still required;
- any explicit Codex configuration or managed policy preventing full enforcement.

Do not claim mechanical enforcement is active until the hook configuration is installed **and** the required Codex trust/runtime conditions are satisfied.
