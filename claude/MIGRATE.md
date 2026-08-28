# Claude Code Migration Runbook — Agent Instructions

## Purpose

You are Claude Code performing or assisting a migration of this Agent Delegation Protocol from an older Claude installation to the current structure.

The current Claude structure is:

1. a supporting user rule in the Claude rules directory;
2. a `bulk-worker` custom subagent definition;
3. lifecycle enforcement hooks installed through `settings.json`;
4. protocol settings merged into existing Claude configuration without replacing unrelated values.

This migration is **Claude-only**. You MUST NOT install, uninstall, modify, inspect for cleanup, or otherwise alter Codex configuration as part of this procedure.

## Mandatory migration rules

You MUST follow all of these rules:

- Preserve every pre-existing user instruction, hook, agent definition, permission, environment value, and setting not owned by this repository.
- Do not overwrite an unrelated file or symlink merely to make installation succeed.
- Do not replace the user's complete `settings.json` with a protocol-only file.
- Do not replace or remove `~/.claude/CLAUDE.md`, project `CLAUDE.md`, `CLAUDE.local.md`, or unrelated rules.
- Do not reset, clean, discard, or overwrite unrelated local Git changes in this repository.
- Do not use the removed legacy combined `scripts/install.*` or `scripts/uninstall.*` paths.
- Do not configure Codex as part of this migration.
- Do not set `CLAUDE_CODE_SUBAGENT_MODEL=haiku` globally. That would override per-agent/per-invocation model escalation.
- Do not silently override `disableAllHooks: true`, organization-managed policy, or conflicting user environment values.
- If a destination is occupied by an unrelated file or symlink, STOP that part of the migration and report the exact conflict instead of replacing it.

## Recognize a legacy installation

Treat any of the following as a migration case:

- `~/.claude/rules/delegation-protocol.md` exists from the old text-first design, but no enforcement hook is installed.
- `~/.claude/agents/bulk-worker.md` exists, but `settings.json` has no protocol-owned lifecycle hooks.
- The repository was previously installed through the old combined installer that configured both Codex and Claude.
- The protocol relies only on Markdown instructions and does not yet use `UserPromptSubmit`, `SubagentStart`, `SubagentStop`, `PostToolUseFailure`, `PreToolUse`, and `Stop` hooks.
- `settings.json` contains some earlier protocol values but the current hook path or settings manifest is missing.

A legacy installation is not an error. Migrate it in place without deleting working user configuration first.

## Phase 1 — Preflight inventory

Before making changes, determine:

```text
REPO_ROOT    = root of this cloned repository
CLAUDE_HOME  = $CLAUDE_CONFIG_DIR if set, otherwise ~/.claude
```

Inspect, without modifying:

```text
$CLAUDE_HOME/settings.json
$CLAUDE_HOME/CLAUDE.md
$CLAUDE_HOME/rules/
$CLAUDE_HOME/agents/
$CLAUDE_HOME/hooks/
$CLAUDE_HOME/.delegation-protocol/
```

Also inspect the repository worktree state.

If the repository has unrelated uncommitted changes, preserve them. Do not run `git reset --hard`, `git clean`, destructive checkout commands, or equivalent cleanup.

If you need the newest repository version and the worktree can be updated safely, prefer:

```bash
git pull --ff-only
```

Do not force a pull through divergent history or local changes.

## Phase 2 — Run only the Claude installer

On macOS/Linux, from the repository root:

```bash
bash scripts/claude/install.sh
```

On Windows PowerShell:

```powershell
.\scripts\claude\install.ps1
```

Python 3 is required for the enforcement hook and settings merge logic.

The installer is the canonical migration mechanism. Do not manually reconstruct its settings mutations unless the installer cannot run and you are specifically repairing a known conflict.

## Phase 3 — Expected migration result

After a successful migration, verify the following structure.

### A. Supporting semantic rule

Verify:

```text
$CLAUDE_HOME/rules/delegation-protocol.md
  -> <REPO_ROOT>/claude/rules/delegation-protocol.md
```

This rule is supplementary. It MUST NOT replace other Claude memory/rule sources.

### B. Bulk worker

Verify:

```text
$CLAUDE_HOME/agents/bulk-worker.md
  -> <REPO_ROOT>/claude/agents/bulk-worker.md
```

The worker uses the Haiku model alias for bounded mechanical/high-volume work. The parent must remain free to invoke a stronger model when a delegated unit needs more reasoning.

Do not implement this by globally setting `CLAUDE_CODE_SUBAGENT_MODEL=haiku`.

### C. Enforcement hook

Verify:

```text
$CLAUDE_HOME/hooks/delegation-enforcer.py
  -> <REPO_ROOT>/claude/hooks/delegation-enforcer.py
```

and verify that `$CLAUDE_HOME/settings.json` contains this protocol's hook handlers while retaining unrelated pre-existing hook definitions.

The protocol currently uses these lifecycle events:

- `UserPromptSubmit`
- `SubagentStart`
- `SubagentStop`
- `PostToolUseFailure` for `Agent`
- `PreToolUse` for mutation gating
- `Stop`

The enforcement layer must be settings/hooks driven. The Markdown rule is supporting context, not the primary enforcement mechanism.

### D. Settings merge

When absent, the installer may add protocol defaults equivalent to:

```json
{
  "env": {
    "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS": "1",
    "CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH": "3",
    "CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS": "20"
  }
}
```

Existing values MUST be preserved rather than overwritten. A conflicting existing value is a condition to report, not a reason to silently replace the user setting.

The installer MUST NOT add a global `CLAUDE_CODE_SUBAGENT_MODEL` override.

## Phase 4 — Enforcement behavior

After migration, the intended behavior is:

- classify clear bulk/high-volume or independently sharded tasks at prompt time;
- inject the delegation/fan-out protocol into the active turn;
- require at least one subagent for qualifying bulk work;
- require multiple actually overlapping workers for qualifying independent multi-subsystem work when runtime capacity permits it;
- block recognized parent mutation before required delegation occurs;
- block normal turn completion until required delegation/fan-out evidence exists;
- fail open only when delegation is genuinely unavailable due to runtime/model/tool failure or higher-priority policy;
- keep the parent responsible for integration, conflict resolution, and final validation.

A delegated task may require permissions the backend cannot grant itself. The multiplexer returns a `permission_required` receipt containing the exact request and a resume token. The dispatcher relays that receipt to the parent and resumes using `multiplexer.py resume --resolution-file` with an `allow`, `deny`, or `handled` decision. Waiting does not consume the task timeout.

Do not weaken this by converting the implementation back into text-only instructions.

## Phase 5 — Existing configuration preservation

You MUST verify all of the following after installation:

1. unrelated `settings.json` keys remain unchanged;
2. unrelated hook groups remain present;
3. existing permission settings remain present;
4. existing environment values remain present;
5. pre-existing Claude rules and memory files remain present;
6. the protocol-owned hooks/settings were added without replacing user-owned configuration.

If `disableAllHooks: true` is already configured, report that the protocol hook cannot enforce behavior until the user changes that setting. Do not silently flip it.

If organization-managed policy disables or constrains user hooks, report that limitation. User-level migration cannot supersede managed policy.

## Phase 6 — Verification

Run the repository's Claude self-test when Python 3 is available:

```bash
python3 scripts/claude/test-protocol.py
```

Use the platform-appropriate Python executable if it is named differently.

Then verify all of the following:

1. the supporting rule resolves to this clone;
2. `bulk-worker` resolves to this clone;
3. the enforcement hook resolves to this clone;
4. `settings.json` contains protocol hook handlers alongside unrelated existing handlers;
5. protocol default env values were added only when absent;
6. a clear bulk task requires delegation before parent mutation;
7. a clearly independent multi-subsystem task requires concurrent fan-out rather than one serialized worker;
8. the parent remains responsible for integration and final validation.

Start a fresh Claude Code session for runtime verification after configuration changes.

Do not declare migration complete merely because files exist. Preservation and effective hook behavior are required.

## Legacy mixed-installer cleanup

The old combined installer no longer exists and MUST NOT be recreated.

If the old installation left Claude-owned links or state, the current Claude installer is designed to migrate the Claude portion in place. Do not remove Codex artifacts while cleaning Claude state.

If you discover a stale path that is clearly owned by a removed version of this repository but conflicts with the current installer, identify it precisely and prefer a reversible move/backup over deletion. Never infer ownership solely from a familiar filename; verify its target/content first.

## Failure handling

If migration fails:

1. stop further mutation;
2. preserve the current working state;
3. report the exact path, setting, hook entry, or environment value causing the conflict;
4. distinguish repository-owned state from unrelated user state;
5. repair only repository-owned state when ownership is certain;
6. rerun the Claude installer and verification after repair.

Do not solve an installation conflict by deleting unrelated configuration.

## Rollback

To remove only the current Claude protocol installation:

macOS/Linux:

```bash
bash scripts/claude/uninstall.sh
```

Windows PowerShell:

```powershell
.\scripts\claude\uninstall.ps1
```

Rollback MUST NOT uninstall or modify Codex.

The Claude uninstaller should remove only repository-owned links, hook handlers, state, and settings values that this protocol added and that the user has not subsequently changed.

## Completion report

When you finish a migration, report concisely:

- whether the installation was legacy text-only, partially migrated, or already current;
- which Claude structures were added or refreshed;
- whether existing rules/hooks/settings/permissions were preserved;
- self-test result;
- whether hooks are disabled by local or managed policy;
- any conflicting existing environment setting that limits the protocol.

Do not claim mechanical enforcement is active when hooks are disabled, blocked by managed policy, or otherwise unable to execute.
