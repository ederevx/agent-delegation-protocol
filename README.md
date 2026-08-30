# Agent Delegation Protocol

A configuration protocol that makes a frontier coding model act as coordinator while delegating bounded bulk work through an agent-agnostic backend mux-scheduler, mechanically enforced by lifecycle hooks in both Codex and Claude Code.

Codex and Claude Code are intentionally **independent installations**. There is no combined installer. Installing one agent must not modify the other agent's configuration.

The protocol is supplementary: existing applicable instructions, hooks, and settings are preserved rather than silently replaced.

## Required behavior

For eligible bulk/high-volume work, preserve frontier-model effort for planning, ambiguity, difficult reasoning, architecture, integration, conflict resolution, and final validation. Delegate bounded work to the cheapest suitable worker.

Eligibility is measured by size and shape, not only by wording. Delegation is mandatory for any task estimated at **25% or more of one compaction window** in reading, output, and tool traffic — about 50k tokens at a 200k window, and the hooks scale that number with the window the session actually reports (`CLAUDE_CODE_MAX_CONTEXT_TOKENS`, `CODEX_MAX_CONTEXT_TOKENS`) — and for any task that runs to **three or more distinct steps**. Both halves inject those thresholds into every turn so the parent applies them even when the classifier does not fire, and both re-estimate as work grows. Both halves import one classifier (`scripts/agents/delegation-classifier.py`, installed beside the scheduler under `.delegation-protocol/`), so a policy change lands in both agents at once instead of being hand-copied into each. Genuinely small, single-step, or tightly coupled work stays in the parent.

When a task contains multiple independent workstreams, use concurrent lifecycle-visible agents when runtime capacity permits it. A selected delegation queue is the exception: one lifecycle-visible dispatcher submits all workstreams to one mux-scheduler process. On a one-lane backend advertising a round-robin `queue_policy`, `virtual_slots` controls how many in-process virtual agents the scheduler interleaves on the physical lane.

```text
Frontier parent / coordinator
        ↓ bounded task or ordered batch
lifecycle-visible bulk worker
        ↓ required capabilities
agent mux-scheduler route
        ↓
external command/API adapter or native host binding
        ↓ JSON receipt
parent integration + repository-wide validation
```

Use non-overlapping ownership where practical, explicit interfaces/acceptance criteria, isolation for conflicting write-heavy work, and parent-controlled integration. The goal is useful parallelism, not maximum agent count.

## Agent metadata and routing

Every backend is described by one JSON metadata document under [`agents/catalog`](agents/catalog). The common interface declares named functions, compatibility capabilities, execution limits, and a binding; command availability is derived from that binding. A top-level `native` boolean selects between a host-native agent binding and a custom command/API adapter. The required `delegation_queue` boolean opts a custom, single-concurrency backend with the `batch` function into whole-manifest queue dispatch. An optional provider-neutral `inference` profile can declare thinking mode, effort, and a per-response output-token ceiling. The mux-scheduler validates that profile and supplies it to custom adapters as bounded JSON in `AGENT_INFERENCE_CONFIG`; each adapter translates the common settings into its provider's controls. Provider-specific behavior stays in the adapter rather than leaking into Codex, Claude, or the route selector.

[`agents/mux-scheduler.json`](agents/mux-scheduler.json) is intentionally small: each route is a membership list of backend IDs. The mux-scheduler first filters members by required capabilities, runtime, and availability, then selects the highest numeric `priority` from 0 through 100. Route order has no scheduling meaning. Equal values form an equivalent tier in which either backend is valid after caller judgment and capability filtering; backend ID supplies only a reproducible default. Change metadata priority or route membership without rewriting policy or adapter code.

External workers receive one bounded common task or batch on stdin and return a machine-readable JSON receipt. A backend that has already launched is never silently retried on another provider. Native selection happens before launch and is represented by a `native_required` receipt and exit status 69, which the lifecycle-visible host worker validates before doing the task itself.

### Delegation queue

The delegation queue is specialized for APIs that expose one sequential agent
stream. A queue backend declares `"delegation_queue": true`, `native: false`,
the `batch` function, and `limits.max_concurrency: 1`. These constraints are
validated together so a queue can never resolve to a native or multi-lane
backend. Without additional policy it preserves the original one-shot FIFO
contract.

`mux-scheduler.py queue` accepts only a manifest containing `tasks` (1–32 JSON
objects) and optional boolean `stop_on_error`. It selects one explicit queue
backend. A one-shot adapter holds the backend's single-lane lock for the entire
manifest and is invoked once. It never replays or falls through after
selection; use `select --delegation-queue` for a non-executing dispatcher
preflight.

A resumable backend can instead set `binding.protocol` to `cooperative-v1`,
advertise both `batch` and `resumable-batch`, and provide a `queue_policy` with
`strategy: round_robin`, `virtual_slots` (1–32), and a positive `agent_turn`
quantum. Both `run` and `queue` then use bounded `start`, `step`, and `cancel`
adapter envelopes tagged with `adapter_protocol: cooperative-v1`. Start returns
`ready` plus an opaque `token`; each yielded step returns the token needed by
the next process, while complete, cancelled, and failed receipts are terminal
and never retain a token. Every response to a valid cooperative request echoes
`schema_version`, `adapter_protocol`, and `operation`; adapters use the stable
`unsupported_adapter_contract` classification for incompatible version or
protocol negotiation instead of requiring callers to parse diagnostic text.
Malformed envelopes and unknown operations produce non-contract `invalid_task`
diagnostics without a fabricated protocol identity. The canonical schema and
conformance corpus live under [`agents/contracts`](agents/contracts). The
mux-scheduler owns a fair,
cross-process ticket queue and releases the provider lane after every slice,
so one queued batch of virtual agents makes interleaved progress without
ever issuing concurrent requests to the one-lane provider. Dead-process
tickets are pruned. Virtual slots bound in-process virtual-agent concurrency,
not provider throughput. A yielded adapter may include a bounded
`retry_after_seconds` delay; this lets temporary physical-lane contention
re-enter the fair queue without consuming an agent-turn budget or spinning.
Protocol v1 bounds an `agent_turn` quantum to 1–100 and negotiates the required
`mux-command-execution-v1` scheduler capability in every request.

The same policy may set `command_concurrency` (1–32) and
`command_timeout_seconds`. A cooperative adapter can pause with
`permission_required` and attach a validated `mux_execution` containing exact
`argv` and `cwd`. The mux-scheduler releases the provider lane, executes several
authorized commands concurrently without a shell, bounds both output streams,
removes provider credentials from their environment, and resumes each retained
agent with a correlated `handled` result. An agent waiting on a command therefore
does not occupy the provider lane. Command failure, timeout, and spawn failure are
results delivered to that agent rather than reasons to replay the task.

A step may instead return `permission_required` with its retained token and
exact request. Requests without an authorized `mux_execution` remain parent
decisions: the mux-scheduler returns exit 9 without cancelling that state.
After the parent decides, pass the backend, token, and matching
`permission_resolution` object to `mux-scheduler.py resume` on stdin. The resume
operation accepts `allow`, `deny`, or a bounded parent-supplied `handled`
result, then continues normal cooperative scheduling. A later exceptional
operation may pause the same task again.

### Add a custom API

1. Copy [`agents/templates/custom-agent.json`](agents/templates/custom-agent.json) into `agents/catalog/` and fill in the backend identity, `native` and `delegation_queue` values, capabilities, availability, limits, and adapter binding.
2. Copy [`scripts/agents/custom-adapter-template.py`](scripts/agents/custom-adapter-template.py), implement the bounded stdin-to-receipt API call, and reference it from the metadata. Keep credentials in environment or a credential manager, never in metadata.
3. Set its integer `priority` from 0 through 100 and add the metadata ID to the desired route in [`agents/mux-scheduler.json`](agents/mux-scheduler.json).
4. Run the mux-scheduler validation/tests, then rerun the applicable host installer so the installed links are present.

That metadata document, one adapter, and one route membership are the complete extension surface for a custom API.

The bundled `bulk` route contains the matching `native-codex-bulk` and
`native-claude-bulk` entries. Capability and runtime filtering make the same
route usable from both hosts. Optional API-backed bindings can be
maintained on separate branches without coupling provider details to `main`.

## Workers ask before conflicting

Workers run in the parent's working tree and cannot see its uncommitted state, so a worker that reaches for repository-wide state on its own can silently destroy the parent's work. Before acting outside its assigned ownership — repo-wide version-control state, another worker's files, the parent's uncommitted work, dependency changes, or anything that leaves the machine — a worker must ask the parent and wait for an answer. The parent answers; only the parent may take the question to the user.

## Worker lifecycle

Worker lifetime is host-specific. Codex workers go idle and keep holding a concurrency slot, so the Codex parent must dismiss one after reading its report:

```
parent spawns worker → worker works → worker reports
        ↓                                    ↓
   slot occupied ←──── still idle ←──── task finished
        ↓
Codex parent dismisses worker (the build's true dismiss call)
        ↓
   slot released → available for the next worker
```

The Codex hook records each finished-but-undismissed worker and gates on it. Claude Code instead releases a foreground `Agent` when its result returns; `TaskStop` addresses live background-task IDs, not completed foreground Agent IDs. The Claude hook therefore tracks overlap only and never invents dismissal debt after `SubagentStop`.

## Changes belong in this repository

Installed hooks, rules, and instruction files are symlinked back here. Claude's worker definition is
also symlinked; Codex's worker is a managed regular-file copy because Codex rejects symlinked role
files at spawn time. Procedural changes to delegation behavior belong in a commit here and are
applied through the installer, never as an in-place patch to an agent's configuration directory:
local edits are lost on reinstall, diverge between machines, and leave the other agent's half
inconsistent.

The two host worker files are generated artifacts. Edit
[`agents/bulk-worker-common.md.tmpl`](agents/bulk-worker-common.md.tmpl) for
shared semantics and [`agents/bulk-worker-profiles.json`](agents/bulk-worker-profiles.json)
for host transport or lifecycle differences, then regenerate both definitions:

```bash
python3 scripts/agents/render-bulk-workers.py
python3 scripts/agents/render-bulk-workers.py --check
```

Both host installers and protocol test suites run `--check` and refuse stale
artifacts, preventing a fix from landing in only the Claude or Codex worker.

## Clone once

Keep the clone at a stable path because installed metadata/hooks are symlinked back to it.

```bash
git clone git@github.com:ederevx/agent-delegation-protocol.git ~/.local/share/agent-delegation-protocol
cd ~/.local/share/agent-delegation-protocol
```

Windows users can choose another stable path:

```powershell
git clone git@github.com:ederevx/agent-delegation-protocol.git "$HOME\agent-delegation-protocol"
cd "$HOME\agent-delegation-protocol"
```

The Codex installer and self-test require Python 3.11 or newer; set `CODEX_PYTHON` to a valid interpreter if it cannot discover one. Creating symbolic links on native Windows also requires Developer Mode or an elevated shell.

Codex installation preflights managed destinations, instruction/state path types, and `hooks.json` before changing the active Codex home, so known conflicts and malformed configuration fail without leaving a partial installation.

## Install Codex only

macOS/Linux:

```bash
bash scripts/codex/install.sh
```

Windows PowerShell:

```powershell
.\scripts\codex\install.ps1
```

See [`codex/INSTALL.md`](codex/INSTALL.md) for host-specific composition,
hook trust, verification, and uninstall details. Older installations must use
the non-destructive [`codex/MIGRATE.md`](codex/MIGRATE.md) runbook.

## Install Claude only

macOS/Linux:

```bash
bash scripts/claude/install.sh
```

Windows PowerShell:

```powershell
.\scripts\claude\install.ps1
```

See [`claude/INSTALL.md`](claude/INSTALL.md) for host-specific settings,
lifecycle behavior, verification, and uninstall details. Older installations
must use the non-destructive [`claude/MIGRATE.md`](claude/MIGRATE.md) runbook.

## Agent-facing migration runbooks

These files are operational instructions for the agents themselves, not merely human release notes:

- [`codex/MIGRATE.md`](codex/MIGRATE.md) — migrate Codex from legacy AGENTS-only/partial structures into AGENTS + custom workers + hooks.
- [`claude/MIGRATE.md`](claude/MIGRATE.md) — migrate Claude from legacy text-first/partial structures into rule + worker + hooks + merged settings.

Both runbooks make preservation, independent installation, self-test verification, failure handling, and rollback mandatory.

## Self-tests

These tests use temporary config directories and do not modify your live Codex or Claude configuration:

```bash
python3 scripts/agents/render-bulk-workers.py --check
python3 scripts/agents/test-delegation-core.py
python3 scripts/agents/test-cooperative-contract.py
python3 scripts/agents/test-mux-scheduler.py
python3 scripts/codex/test-protocol.py
python3 scripts/claude/test-protocol.py
```

They verify non-destructive hook/settings merge behavior plus worker gating. The agent mux-scheduler tests cover metadata validation, capability selection, ordered priority, native handoff, and external receipts.

## Update

Pull the repository, then rerun only the installer for the agent you use:

```bash
git pull --ff-only
bash scripts/codex/install.sh   # Codex only
# or
bash scripts/claude/install.sh  # Claude only
```

Codex composed-global mode and its managed worker copy need reinstall to refresh. Symlinked
hook/agent assets pick up repository changes immediately, while rerunning the appropriate installer
refreshes copied files and protocol-owned hook/settings entries.

Composed Codex instructions are stored per installation at
`$CODEX_HOME/.delegation-protocol/AGENTS.composed.md`. This keeps alternate `CODEX_HOME` values
isolated: updating or uninstalling one home cannot overwrite or remove another home's composed
instructions. The installer safely repoints a verified legacy repository-runtime link but retains
the old shared file in case another home still uses it.

For upgrades from a materially older installation layout, use the applicable `MIGRATE.md` rather than improvising cleanup.

## Uninstall independently

Codex only:

```bash
bash scripts/codex/uninstall.sh
```

Claude only:

```bash
bash scripts/claude/uninstall.sh
```

PowerShell equivalents are in the same per-agent directories. Neither uninstaller intentionally touches the other agent.

## Repository layout

```text
codex/
  AGENTS.md
  INSTALL.md
  MIGRATE.md
  agents/
    bulk-worker.toml
    balanced-worker.toml
  hooks/
    delegation-enforcer.py
claude/
  INSTALL.md
  MIGRATE.md
  agents/
    bulk-worker.md
  hooks/
    delegation-enforcer.py
  rules/
    delegation-protocol.md
agents/
  bulk-worker-common.md.tmpl
  bulk-worker-profiles.json
  catalog/
  mux-scheduler.json
  templates/
    custom-agent.json
scripts/
  agents/
    render-bulk-workers.py
    mux-scheduler.py
    delegation-classifier.py
    delegation_queue.py
    hook_settings.py
    test-delegation-core.py
    custom-adapter-template.py
  codex/
    install.sh
    uninstall.sh
    install.ps1
    uninstall.ps1
    manage-hooks.py
    test-protocol.py
  claude/
    install.sh
    uninstall.sh
    install.ps1
    uninstall.ps1
    manage-settings.py
    manage-install.py
    test-protocol.py
```

## Enforcement boundary

Both agents now use mechanical lifecycle hooks plus a supporting semantic policy. Codex retains AGENTS because current Codex subagent workflows still treat direct user requests or applicable `AGENTS.md`/skill instructions as spawning authorization. Neither implementation can override higher-priority system/developer/user instructions, managed organization policy, unavailable tools/models, hook trust requirements, or platform safety controls.

## License

Licensed under [Creative Commons Attribution 4.0 International (CC BY 4.0)](https://creativecommons.org/licenses/by/4.0/) — see [LICENSE](LICENSE) for the full legal text.

You are free to share and adapt this work for any purpose, including commercially, as long as you give appropriate credit. Attribution must name the original author and be preserved in every copy or derivative, including further edits or forks.
