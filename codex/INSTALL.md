# Codex installation

Codex is installed independently from Claude. The Codex installer touches only `$CODEX_HOME` (normally `~/.codex`) plus the clone's ignored `.runtime/codex` composition file when existing global instructions must be preserved.

## Migration from an older structure

If you are Codex upgrading an AGENTS-only, partially migrated, or legacy combined installation, you MUST follow [`MIGRATE.md`](MIGRATE.md) before improvising cleanup. The migration runbook is written directly for the agent and requires preflight inventory, preservation of existing configuration, independent Codex-only installation, verification, failure handling, and rollback.

## Why the implementation uses three layers

Current Codex supports stronger enforcement than an `AGENTS.md`-only design, but `AGENTS.md` still serves an important purpose.

The protocol therefore uses:

1. **Global AGENTS authorization/semantic policy** — explicitly authorizes subagents and parallel delegation, while preserving pre-existing global instructions.
2. **Custom worker agents** — pin the cheap worker tiers to Luna/Terra without globally forcing every subagent onto a cheap model.
3. **Lifecycle hooks** — mechanically classify clear bulk/sharded turns, observe real subagent starts/stops, block parent mutation until delegation requirements are met, and block turn completion until required delegation/fan-out evidence exists.

This is stronger than using text instructions alone and safer than globally setting every subagent's default model to Luna.

## Install

macOS/Linux:

```bash
bash scripts/codex/install.sh
```

Windows PowerShell:

```powershell
.\scripts\codex\install.ps1
```

Python 3 is required for the local enforcement hook.

## Global instruction installation

Codex reads one global instruction file from `$CODEX_HOME`: `AGENTS.override.md` if present, otherwise `AGENTS.md`. An override at that level shadows the default; Codex does not concatenate both global files automatically.

### No existing global Codex instructions

```text
$CODEX_HOME/AGENTS.md -> <clone>/codex/AGENTS.md
```

### Existing global Codex instructions

The installer preserves the currently active global instruction content verbatim, appends this repository's protocol, writes the result to an ignored runtime file in the clone, and activates it through:

```text
$CODEX_HOME/AGENTS.override.md -> <clone>/.runtime/codex/AGENTS.composed.md
```

If an `AGENTS.override.md` already existed, it is moved to a backup under `$CODEX_HOME/.delegation-protocol/` and restored by uninstall. If only `AGENTS.md` existed, it is left untouched and its content is placed first in the composed file.

This composition remains useful even with hooks because current Codex multi-agent behavior treats a direct user request or applicable `AGENTS.md`/skill instruction as authorization to spawn. Hooks provide enforcement; AGENTS provides explicit standing authorization and broader semantic guidance.

## Custom workers

The installer adds symlinks:

```text
~/.codex/agents/bulk-worker.toml
  -> <clone>/codex/agents/bulk-worker.toml

~/.codex/agents/balanced-worker.toml
  -> <clone>/codex/agents/balanced-worker.toml
```

Declared agent names/model tiers:

- `bulk_worker` — `gpt-5.6-luna`, medium reasoning; mechanical/repetitive/high-volume work.
- `balanced_worker` — `gpt-5.6-terra`, medium reasoning; moderately difficult delegated units.

Custom-agent files are preferred over setting `agents.default_subagent_model = "gpt-5.6-luna"` globally. A global default would make Luna the inherited model for all otherwise-unspecified subagents, including tasks that need more reasoning. Custom roles preserve cheap routing while leaving escalation available.

The protocol deliberately does not require per-call `model` overrides for the normal cheap-worker path. That reduces dependence on client surfaces where model/agent metadata may be hidden or awkward to express. Explicit model overrides remain available when the current runtime exposes them.

## Hook enforcement

The installer adds:

```text
~/.codex/hooks/delegation-enforcer.py
  -> <clone>/codex/hooks/delegation-enforcer.py
```

and non-destructively merges protocol-owned handlers into:

```text
~/.codex/hooks.json
```

Existing hook groups and unrelated JSON fields are preserved.

Installed events:

- `UserPromptSubmit` — conservatively classifies the turn and injects delegation/routing policy as developer context.
- `SubagentStart` — records actual delegation, tracks active workers with race-safe per-agent marker files, and injects bounded-worker requirements.
- `SubagentStop` — removes the worker from the active set.
- `PreToolUse` — denies parent mutation on a classified bulk/sharded turn until required delegation evidence exists. Codex can apply this to shell commands, `apply_patch`, MCP calls, and other local function tools.
- `PostToolUse` matching `Agent` — observes failed Agent/spawn results so enforcement can fail open when the requested worker/runtime is genuinely unavailable.
- `Stop` — prevents the parent turn from completing until required delegation requirements are met. For multi-subsystem work, the hook requires evidence that at least two workers overlapped in time.

The hook is a guardrail, not a sandbox. Specialized tool paths may opt out of normal tool hooks; the `Stop` gate provides a second enforcement point.

## Hook trust is mandatory after installation

Codex requires non-managed user hooks to be reviewed and trusted before they run. The installer does **not** bypass or forge this trust decision.

After installation:

1. restart Codex;
2. run `/hooks`;
3. review the Agent Delegation Protocol hook definition;
4. trust/enable it.

Until that review is completed, the AGENTS policy and custom workers are installed, but the mechanical hook enforcement may be skipped.

Hooks are enabled by default in current Codex releases. If your `config.toml` explicitly disables hooks, the installer warns rather than silently overriding your existing setting. Likewise, if organization-managed configuration disables user hooks, user-level installation cannot supersede it.

## Multi-agent behavior

For clear independent subsystems, the policy and hook require concurrent fan-out rather than merely two sequential subagent runs. Atomic marker files record active `SubagentStart`/`SubagentStop` state so simultaneous hook processes do not race on one shared counter.

The protocol does not set a fixed global concurrency cap because Codex already manages a default and existing users may have deliberately configured `agents.max_concurrent_threads_per_session`. Existing concurrency policy is preserved.

Because a worker keeps holding a slot after its task ends, `SubagentStop` records it as finished but still held, and the hook gates new spawns and turn completion until it is dismissed. Codex's dismissal call is not fixed across builds, so the hook matches it by shape — a stop/kill/dismiss verb paired with a task/agent/worker noun — and, until it has observed such a call, warns rather than blocking so a build without one cannot be wedged. The stop gate blocks at most once per turn for the same reason: a worker the runtime already tore down can never be dismissed.

## Verify

Run the repository self-test:

```bash
python3 scripts/codex/test-protocol.py
```

Then start a fresh Codex session and confirm:

1. `/hooks` shows the protocol handlers as trusted/enabled.
2. `bulk_worker` and `balanced_worker` are available custom agents.
3. a clear bulk request cannot perform parent mutation before spawning a worker;
4. an independent frontend/backend/test request requires overlapping workers before parent mutation;
5. existing global/project instructions remain present and applicable.

## Uninstall

macOS/Linux:

```bash
bash scripts/codex/uninstall.sh
```

Windows PowerShell:

```powershell
.\scripts\codex\uninstall.ps1
```

Uninstall removes only protocol-owned hook handlers/symlinks/state and restores a preserved prior `AGENTS.override.md` where applicable. It does not modify Claude.
