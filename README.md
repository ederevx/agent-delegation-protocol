# Agent Delegation Protocol

A configuration protocol that makes a frontier coding model act as coordinator while delegating bounded bulk work through an agent-agnostic backend multiplexer, mechanically enforced by lifecycle hooks in both Codex and Claude Code.

Codex and Claude Code are intentionally **independent installations**. There is no combined installer. Installing one agent must not modify the other agent's configuration.

The protocol is supplementary: existing applicable instructions, hooks, and settings are preserved rather than silently replaced.

## Required behavior

For eligible bulk/high-volume work, preserve frontier-model effort for planning, ambiguity, difficult reasoning, architecture, integration, conflict resolution, and final validation. Delegate bounded work to the cheapest suitable worker.

When a task contains multiple independent workstreams, use concurrent lifecycle-visible agents when runtime capacity permits it. A one-lane backend does not push that work back onto the parent: when it advertises a round-robin `queue_policy`, its `virtual_slots` set how many dispatchers may run at once and the multiplexer interleaves them on the single physical lane. Only a backend without that policy serializes its dispatchers.

```text
Frontier parent / coordinator
        ↓ bounded task or ordered batch
lifecycle-visible bulk worker
        ↓ required capabilities
agent multiplexer route
        ↓
external command/API adapter or native host binding
        ↓ JSON receipt
parent integration + repository-wide validation
```

Use non-overlapping ownership where practical, explicit interfaces/acceptance criteria, isolation for conflicting write-heavy work, and parent-controlled integration. The goal is useful parallelism, not maximum agent count.

## Agent metadata and routing

Every backend is described by one JSON metadata document under [`agents/catalog`](agents/catalog). The common interface declares named functions, compatibility capabilities, execution limits, and a binding; command availability is derived from that binding. A top-level `native` boolean selects between a host-native agent binding and a custom command/API adapter. The required `delegation_queue` boolean opts a custom, single-concurrency backend with the `batch` function into whole-manifest queue dispatch. An optional provider-neutral `inference` profile can declare thinking mode, effort, and a per-response output-token ceiling. The multiplexer validates that profile and supplies it to custom adapters as bounded JSON in `AGENT_INFERENCE_CONFIG`; each adapter translates the common settings into its provider's controls. Provider-specific behavior stays in the adapter rather than leaking into Codex, Claude, or the route selector.

[`agents/multiplexer.json`](agents/multiplexer.json) is intentionally small: each route is a membership list of backend IDs. The multiplexer first filters members by required capabilities, runtime, and availability, then selects the highest numeric `priority` from 0 through 100. Route order has no scheduling meaning. Equal values form an equivalent tier in which either backend is valid after caller judgment and capability filtering; backend ID supplies only a reproducible default. Change metadata priority or route membership without rewriting policy or adapter code.

External workers receive one bounded common task or batch on stdin and return a machine-readable JSON receipt. A backend that has already launched is never silently retried on another provider. Native selection happens before launch and is represented by a `native_required` receipt and exit status 69, which the lifecycle-visible host worker validates before doing the task itself.

### Delegation queue

The delegation queue is specialized for APIs that expose one sequential agent
stream. A queue backend declares `"delegation_queue": true`, `native: false`,
the `batch` function, and `limits.max_concurrency: 1`. These constraints are
validated together so a queue can never resolve to a native or multi-lane
backend. Without additional policy it preserves the original one-shot FIFO
contract.

`multiplexer.py queue` accepts only a manifest containing `tasks` (1–32 JSON
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
the next process, while complete and failed receipts are terminal. The
multiplexer owns a fair,
cross-process ticket queue and releases the provider lane after every slice,
so several lifecycle-visible dispatchers make interleaved progress without
ever issuing concurrent requests to the one-lane provider. Dead-process
tickets are pruned. Virtual slots advertise host fan-out capacity, not extra
provider throughput. A yielded adapter may include a bounded
`retry_after_seconds` delay; this lets temporary physical-lane contention
re-enter the fair queue without consuming an agent-turn budget or spinning.

A step may instead return `permission_required` with its retained token and
exact request. The multiplexer returns exit 9 without cancelling that state.
After the parent decides, pass the backend, token, and matching
`permission_resolution` object to `multiplexer.py resume` on stdin. The resume
operation accepts `allow`, `deny`, or a bounded parent-supplied `handled`
result, then continues normal cooperative scheduling. A later exceptional
operation may pause the same task again.

### Add a custom API

1. Copy [`agents/templates/custom-agent.json`](agents/templates/custom-agent.json) into `agents/catalog/` and fill in the backend identity, `native` and `delegation_queue` values, capabilities, availability, limits, and adapter binding.
2. Copy [`scripts/agents/custom-adapter-template.py`](scripts/agents/custom-adapter-template.py), implement the bounded stdin-to-receipt API call, and reference it from the metadata. Keep credentials in environment or a credential manager, never in metadata.
3. Set its integer `priority` from 0 through 100 and add the metadata ID to the desired route in [`agents/multiplexer.json`](agents/multiplexer.json).
4. Run the multiplexer validation/tests, then rerun the applicable host installer so the installed links are present.

That metadata document, one adapter, and one route membership are the complete extension surface for a custom API.

The bundled `bulk` route contains the matching `native-codex-bulk` and
`native-claude-bulk` entries. Capability and runtime filtering make the same
route usable from both hosts. Optional API-backed bindings can be
maintained on separate branches without coupling provider details to `main`.

## Workers ask before conflicting

Workers run in the parent's working tree and cannot see its uncommitted state, so a worker that reaches for repository-wide state on its own can silently destroy the parent's work. Before acting outside its assigned ownership — repo-wide version-control state, another worker's files, the parent's uncommitted work, dependency changes, or anything that leaves the machine — a worker must ask the parent and wait for an answer. The parent answers; only the parent may take the question to the user.

## Worker lifecycle

A worker does not disappear when its task ends. It goes idle and keeps holding a concurrency slot, so the parent must dismiss it once its report has been read:

```
parent spawns worker → worker works → worker reports
        ↓                                    ↓
   slot occupied ←──── still idle ←──── task finished
        ↓
parent dismisses worker (TaskStop / build's stop-task call)
        ↓
   slot released → available for the next worker
```

Both hooks record each finished-but-undismissed worker and gate on it: new spawns are denied and turn completion is blocked until the outstanding workers are dismissed. The record is per-turn and cleared by the next prompt, and the stop gate blocks at most once per turn, so a worker that never reports — or one the runtime already tore down — cannot wedge the session.

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

Creating symbolic links on native Windows may require Developer Mode or an elevated shell.

## Install Codex only

macOS/Linux:

```bash
bash scripts/codex/install.sh
```

Windows PowerShell:

```powershell
.\scripts\codex\install.ps1
```

Codex now uses a four-layer implementation:

1. **AGENTS authorization/semantic policy** — standing authorization for subagents and parallel delegation while preserving pre-existing global instructions.
2. **Custom worker agents** — `bulk_worker` is the lifecycle-visible multiplexer dispatcher; `balanced_worker` remains pinned to GPT-5.6 Terra for work that needs more judgment.
3. **Agent multiplexer** — capability-filtered, priority-ordered routing across native bindings and custom command/API adapters.
4. **Lifecycle hooks** — `UserPromptSubmit`, `SubagentStart`, `SubagentStop`, `PreToolUse`, `PostToolUse(Agent)`, and `Stop` mechanically gate clear bulk/sharded work.

For multi-subsystem tasks the Codex hook requires evidence of **actual overlapping workers**, not merely two sequential agent runs. A one-lane backend still satisfies that requirement when it advertises round-robin virtual slots: the workers overlap up to that slot count while the multiplexer interleaves their provider calls on the one lane.

**Important:** current Codex requires non-managed hooks to be reviewed/trusted. After installation, restart Codex, run `/hooks`, review the protocol definition, and trust/enable it. Until then, the AGENTS policy/custom workers are installed but mechanical hook enforcement may be skipped.

See [`codex/INSTALL.md`](codex/INSTALL.md).

If an agent is upgrading an older AGENTS-only, partial, or legacy combined installation, it MUST follow [`codex/MIGRATE.md`](codex/MIGRATE.md). That runbook is written directly for Codex and requires non-destructive inventory, migration, verification, and rollback without touching Claude.

## Install Claude only

macOS/Linux:

```bash
bash scripts/claude/install.sh
```

Windows PowerShell:

```powershell
.\scripts\claude\install.ps1
```

Claude installation manages only the configured Claude home (normally `~/.claude`) and installs:

- a lifecycle-visible `bulk-worker` dispatcher with Haiku as its native binding;
- the shared agent catalog, route configuration, and multiplexer;
- a supplementary semantic rule;
- a local enforcement hook;
- non-destructively merged lifecycle hooks in `settings.json`;
- explicit subagent concurrency/depth defaults when absent;
- experimental agent teams when absent, as an optional additional coordination capability.

Claude enforcement is not text-only. The hook classifies clear bulk/sharded requests, records actual worker starts/stops, denies parent mutation before required delegation, and blocks turn completion until delegation requirements are satisfied. Independent-subsystem work uses overlapping lifecycle-visible workers; a one-lane backend advertising round-robin virtual slots supports that overlap up to its slot count, with the multiplexer interleaving the calls on its single lane.

See [`claude/INSTALL.md`](claude/INSTALL.md).

If an agent is upgrading an older text-only, partial, or legacy combined installation, it MUST follow [`claude/MIGRATE.md`](claude/MIGRATE.md). That runbook is written directly for Claude Code and requires non-destructive settings/hook migration without touching Codex.

## Agent-facing migration runbooks

These files are operational instructions for the agents themselves, not merely human release notes:

- [`codex/MIGRATE.md`](codex/MIGRATE.md) — migrate Codex from legacy AGENTS-only/partial structures into AGENTS + custom workers + hooks.
- [`claude/MIGRATE.md`](claude/MIGRATE.md) — migrate Claude from legacy text-first/partial structures into rule + worker + hooks + merged settings.

Both runbooks make preservation, independent installation, self-test verification, failure handling, and rollback mandatory.

## Self-tests

These tests use temporary config directories and do not modify your live Codex or Claude configuration:

```bash
python3 scripts/agents/render-bulk-workers.py --check
python3 scripts/agents/test-multiplexer.py
python3 scripts/codex/test-protocol.py
python3 scripts/claude/test-protocol.py
```

They verify non-destructive hook/settings merge behavior plus worker gating. The agent multiplexer tests cover metadata validation, capability selection, ordered priority, native handoff, and external receipts.

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
  multiplexer.json
  templates/
    custom-agent.json
scripts/
  agents/
    render-bulk-workers.py
    multiplexer.py
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
    test-protocol.py
```

## Enforcement boundary

Both agents now use mechanical lifecycle hooks plus a supporting semantic policy. Codex retains AGENTS because current Codex subagent workflows still treat direct user requests or applicable `AGENTS.md`/skill instructions as spawning authorization. Neither implementation can override higher-priority system/developer/user instructions, managed organization policy, unavailable tools/models, hook trust requirements, or platform safety controls.

## License

Licensed under [Creative Commons Attribution 4.0 International (CC BY 4.0)](https://creativecommons.org/licenses/by/4.0/) — see [LICENSE](LICENSE) for the full legal text.

You are free to share and adapt this work for any purpose, including commercially, as long as you give appropriate credit. Attribution must name the original author and be preserved in every copy or derivative, including further edits or forks.
