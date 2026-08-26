# Claude Code installation

Claude is installed independently from Codex. The Claude installer touches only the configured Claude home (normally `~/.claude`) and this cloned repository.

## Migration from an older structure

If you are Claude Code upgrading a text-only, partially migrated, or legacy combined installation, you MUST follow [`MIGRATE.md`](MIGRATE.md) before improvising cleanup. The migration runbook is written directly for the agent and requires preflight inventory, preservation of existing configuration, independent Claude-only installation, verification, failure handling, and rollback.

## Install

macOS/Linux:

```bash
bash scripts/claude/install.sh
```

Windows PowerShell:

```powershell
.\scripts\claude\install.ps1
```

Python 3 is required because the enforcement hook is a small local Python program.

## Installed symlinks

The installer refuses to overwrite an unrelated file or symlink at any destination.

```text
~/.claude/rules/delegation-protocol.md
  -> <clone>/claude/rules/delegation-protocol.md

~/.claude/agents/bulk-worker.md
  -> <clone>/claude/agents/bulk-worker.md

~/.claude/hooks/delegation-enforcer.py
  -> <clone>/claude/hooks/delegation-enforcer.py
```

The Markdown rule is a semantic/supporting policy layer. Mechanical enforcement is performed by hooks and settings.

## Hooks installed into settings.json

The installer merges protocol-owned entries into `~/.claude/settings.json`; it does not replace unrelated settings or hooks.

The hook participates in these lifecycle events:

- `UserPromptSubmit` — conservatively classifies the turn and injects the mandatory delegation/fan-out policy into Claude's context.
- `SubagentStart` — records delegation, tracks active workers, and injects bounded-worker requirements into each spawned subagent.
- `SubagentStop` — removes the worker from the active set and records it as finished but still held.
- `PostToolUseFailure` for `Agent` — detects runtime/model/concurrency failures so enforcement can fail open only when delegation is actually unavailable.
- `PreToolUse` for core mutation tools, `Agent`, and `TaskStop` — denies parent mutation on an eligible bulk task until required delegation has occurred, denies new worker spawns while finished workers are still held, and records each `TaskStop` as a dismissal.
- `Stop` — blocks the parent from ending an eligible turn until required delegation has occurred and every finished worker has been dismissed.

For multi-subsystem work, enforcement requires evidence that at least two subagents actually overlapped in time, not merely that two workers ran sequentially. Atomic per-agent marker files avoid races between simultaneous lifecycle hook processes.

## Settings configured

When the following environment entries are absent from `settings.json`, the installer adds them:

```json
{
  "env": {
    "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS": "1",
    "CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH": "3",
    "CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS": "20"
  }
}
```

Existing values are preserved rather than overwritten; the installer warns when an existing value conflicts with the protocol default.

The installer deliberately does **not** set `CLAUDE_CODE_SUBAGENT_MODEL`. That variable outranks per-invocation and agent-definition model selection, so globally pinning it to Haiku would prevent escalation. Instead, `bulk-worker.md` specifies `model: haiku`, while the parent remains free to choose a stronger model when necessary.

Agent teams are optional. The mandatory baseline uses ordinary subagents because they are broadly available and directly observable through `SubagentStart`/`SubagentStop`. Teams may be used for complex independent subsystems that benefit from peer-to-peer coordination.

## Existing Claude configuration

The protocol is supplementary:

- existing `~/.claude/CLAUDE.md`, project `CLAUDE.md`, `CLAUDE.local.md`, and unrelated rules are untouched;
- existing hook groups are preserved and the protocol handlers are appended;
- existing settings and environment overrides are preserved;
- the installer saves a safety copy of the pre-install settings on the first install under `~/.claude/.delegation-protocol/`;
- uninstall removes only hook handlers and settings values that this protocol added and that have not subsequently been changed by the user.

If `disableAllHooks: true` is already configured, the installer does not silently override it and emits a warning. Managed organization policy can also prevent user-level hooks from running.

## Enforcement scope

The hook uses a conservative deterministic classifier. It mechanically gates clear bulk/high-volume or independently sharded implementation requests, while the Markdown rule supplies broader semantic guidance.

The `PreToolUse` gate covers Claude Code's core file mutation tools and common mutating shell/PowerShell operations. The `Stop` gate is the backstop: an eligible turn cannot normally finish without the required delegation evidence even if a mutation path was not recognized by the pre-tool heuristic.

The hook also enforces worker dismissal. `SubagentStop` records the worker as finished but still held, since a worker stays alive and idle until it is dismissed. While any such worker is outstanding, `PreToolUse` denies new `Agent` spawns and `Stop` blocks turn completion, so finished workers are released with `TaskStop` before more are created. Dismissal is recorded when the `TaskStop` call is made rather than when it succeeds, so stopping an already-gone worker still clears the obligation. The record is per-turn and a new prompt clears it, and the `Stop` gate blocks at most once per turn, so a worker the runtime already tore down — which can never be dismissed — cannot loop the hook or wedge the session.

A direct higher-priority user/system restriction against delegation, unavailable Agent tooling, managed policy, or runtime/model failure can supersede or prevent the protocol. The hook is an execution guardrail, not a security sandbox.

## Verify

First run the isolated self-test:

```bash
python3 scripts/claude/test-protocol.py
```

It uses temporary configuration directories and verifies non-destructive settings merge/unmerge plus single-worker and concurrent-fan-out gating.

Then start a fresh Claude Code session and confirm:

1. `~/.claude/settings.json` contains the protocol hook handlers alongside existing hooks.
2. `bulk-worker` is visible as a custom subagent.
3. A clearly bulk request triggers a required subagent before parent mutation.
4. A request spanning independent frontend/backend/test work triggers concurrent fan-out rather than a single serialized worker.
5. `/context` still shows all pre-existing applicable instructions plus the supplementary rule.

## Uninstall

macOS/Linux:

```bash
bash scripts/claude/uninstall.sh
```

Windows PowerShell:

```powershell
.\scripts\claude\uninstall.ps1
```

This does not uninstall or modify Codex.
