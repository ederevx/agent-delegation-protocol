# Codex installation

Codex is installed independently from Claude. The Codex installer writes mutable installation state only under `$CODEX_HOME` (normally `~/.codex`); repository-backed hook, worker, and mux-scheduler assets remain symlinked to the clone.

## Migration from an older structure

If you are Codex upgrading an AGENTS-only, partially migrated, or legacy combined installation, you MUST follow [`MIGRATE.md`](MIGRATE.md) before improvising cleanup. The migration runbook is written directly for the agent and requires preflight inventory, preservation of existing configuration, independent Codex-only installation, verification, failure handling, and rollback.

## Why the implementation uses four layers

Current Codex supports stronger enforcement than an `AGENTS.md`-only design, but `AGENTS.md` still serves an important purpose.

The protocol therefore uses:

1. **Global AGENTS authorization/semantic policy** — explicitly authorizes subagents and parallel delegation, while preserving pre-existing global instructions.
2. **Custom worker agents** — make `bulk_worker` a lifecycle-visible mux-scheduler dispatcher while preserving `balanced_worker` as a stronger native tier.
3. **Agent mux-scheduler** — selects the first available backend that supplies the task's required capabilities from an easily reordered priority list.
4. **Lifecycle hooks** — mechanically classify clear bulk/sharded turns, observe real subagent starts/stops, block parent mutation until delegation requirements are met, and block turn completion until required delegation/fan-out evidence exists.

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

Python 3.11 or newer is required by the installer and self-test. Set `CODEX_PYTHON` to a valid interpreter when automatic discovery cannot find one; on Windows, Store execution aliases are not treated as usable runtimes.

Native Windows installation also requires symbolic-link capability. Enable Windows Developer Mode or run PowerShell as Administrator; the installer probes this capability before creating persistent installation state and reports an actionable error if it is unavailable.

Before changing active instructions or managed files, both platform installers validate the Codex-home directory layout, protocol state paths, managed destinations, and `hooks.json`. Unsafe symlinks, destination conflicts, malformed JSON, and incompatible existing hook shapes fail during this non-mutating preflight.

## Global instruction installation

Codex reads one global instruction file from `$CODEX_HOME`: `AGENTS.override.md` if present, otherwise `AGENTS.md`. An override at that level shadows the default; Codex does not concatenate both global files automatically.

### No existing global Codex instructions

```text
$CODEX_HOME/AGENTS.md -> <clone>/codex/AGENTS.md
```

### Existing global Codex instructions

The installer preserves the currently active global instruction content verbatim, appends this repository's protocol, writes the result to per-home installation state, and activates it through:

```text
$CODEX_HOME/AGENTS.override.md -> $CODEX_HOME/.delegation-protocol/AGENTS.composed.md
```

If an `AGENTS.override.md` already existed, it is moved to a backup under `$CODEX_HOME/.delegation-protocol/` and restored by uninstall. If only `AGENTS.md` existed, it is left untouched and its content is placed first in the composed file.

Keeping the composed file under the active home isolates multiple `CODEX_HOME` installations. Reinstalling or uninstalling one home cannot overwrite or remove another home's effective instructions. A verified legacy link to `<clone>/.runtime/codex/AGENTS.composed.md` is repointed automatically; the shared legacy file is left in place because another home may still reference it.

This composition remains useful even with hooks because current Codex multi-agent behavior treats a direct user request or applicable `AGENTS.md`/skill instruction as authorization to spawn. Hooks provide enforcement; AGENTS provides explicit standing authorization and broader semantic guidance.

## Custom workers

The installer adds a managed regular-file copy:

```text
~/.codex/agents/bulk_worker.toml
  (copied from <clone>/codex/agents/bulk_worker.toml)

~/.codex/agents/balanced-worker.toml
  -> <clone>/codex/agents/balanced-worker.toml
```

The regular file is intentional. Current Codex discovers agent definitions through symlinks but
reopens the selected role with no-follow protection when spawning it, so a symlink is advertised
yet fails at runtime with `agent type is currently not available`. The installer records the
copy's hash, refreshes an unmodified protocol-owned copy on reinstall, and refuses to overwrite a
user-modified or unrelated file.

Declared agent names/model tiers:

- `bulk_worker` — lifecycle-visible bulk dispatcher; its Luna configuration is used only when the mux-scheduler selects `native-codex-bulk`.
- `balanced_worker` — `gpt-5.6-terra`, medium reasoning; moderately difficult delegated units.

Custom-agent files are preferred over setting `agents.default_subagent_model = "gpt-5.6-luna"` globally. A global default would make Luna the inherited model for all otherwise-unspecified subagents, including tasks that need more reasoning. Custom roles preserve cheap routing while leaving escalation available.

The protocol deliberately does not require per-call `model` overrides for the normal cheap-worker path. That reduces dependence on client surfaces where model/agent metadata may be hidden or awkward to express. Explicit model overrides remain available when the current runtime exposes them.

## Agent mux-scheduler

The installer also adds symlinks under `$CODEX_HOME/.delegation-protocol/`:

```text
mux-scheduler.py   -> <clone>/scripts/agents/mux-scheduler.py
catalog          -> <clone>/agents/catalog
mux-scheduler.json -> <clone>/agents/mux-scheduler.json
```

The bulk worker submits one bounded common JSON task or ordered batch with:

```bash
python3 "$CODEX_HOME/.delegation-protocol/mux-scheduler.py" \
  run --route bulk --runtime codex
```

Each catalog entry uses the same named-function and compatibility interface and declares a top-level `native` boolean plus either a native Codex binding or a custom command/API adapter. Each backend declares its own numeric `priority`; routes in `agents/mux-scheduler.json` are membership lists with no scheduling order. A one-lane backend advertising a round-robin `queue_policy` accepts one queue batch and interleaves its in-process virtual agents up to `virtual_slots` on the single lane.

The dispatcher executes natively only for a valid `native_required` receipt with exit status 69 selecting the Codex-native backend. Once an external adapter launches, its receipt is returned without automatic native retry.

Codex's managed permission profile remains authoritative for a spawned custom
agent; role-file fields cannot add the scheduler's shared state root or provider
network access. The CI overlay therefore invokes only the trusted installed
mux-scheduler command with `sandbox_permissions: "require_escalated"`. That
command receives the global `~/.cache/agent-delegation-protocol` state root and
configured provider network. The worker never tries it sandboxed first, never
requests a reusable Python prefix rule, and never replays a task after a denied
escalation or launch failure.

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
- `Stop` — prevents the parent turn from completing until required delegation requirements are met. For multi-subsystem work, the hook requires overlapping workers; backend calls may still be serialized by the mux-scheduler.

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

For clear independent subsystems, the policy and hook ordinarily require concurrent lifecycle-visible fan-out. A selected delegation queue is the exception: one dispatcher submits one batch, and a round-robin one-lane backend interleaves its in-process virtual agents up to the advertised slot limit rather than opening overlapping provider requests. Atomic marker files record active `SubagentStart`/`SubagentStop` state so simultaneous hook processes do not race on one shared counter.

The protocol does not set a fixed global concurrency cap because Codex already manages a default and existing users may have deliberately configured `agents.max_concurrent_threads_per_session`. Existing concurrency policy is preserved.

Because a worker keeps holding a slot after its task ends, `SubagentStop` records it as finished but still held, and the hook gates new spawns and turn completion until it is dismissed. Codex's dismissal call is not fixed across builds, so the hook matches it by shape — a stop/kill/dismiss verb paired with a task/agent/worker noun. A delivered `FINAL_ANSWER` or final-status notification counts as result collection, so the injected policy tells the parent to make that dismissal its next lifecycle action. `collaboration.interrupt_agent` deliberately does not count when its contract says the agent remains available. Until the hook has observed a true dismissal call, it warns rather than blocking so a build without one cannot be wedged. The stop gate blocks at most once per turn for the same reason: a worker the runtime already tore down can never be dismissed.

## Verify

Run the repository self-test:

```bash
python3 scripts/codex/test-protocol.py
```

Then start a fresh Codex session and confirm:

1. `/hooks` shows the protocol handlers as trusted/enabled.
2. `bulk_worker` and `balanced_worker` are available custom agents.
3. a clear bulk request cannot perform parent mutation before spawning a worker;
4. an independent frontend/backend/test request requires overlapping workers before parent mutation, bounded by the selected backend's virtual slots when that backend is a round-robin one-lane backend;
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

Uninstall removes only protocol-owned hook handlers, symlinks, an unmodified managed worker copy,
and known state files, and restores a preserved prior `AGENTS.override.md` where applicable. A
modified worker copy and unrelated files inside `.delegation-protocol` are preserved. It does not
modify Claude.
