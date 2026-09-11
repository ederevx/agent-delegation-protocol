# Agent Delegation Protocol

The protocol enforces native delegation and worker budgets on Codex and Claude.
The parent retains planning, judgment, integration, conflict resolution, and
final validation. All tiers can analyze and execute with their normal tools,
subject to workload delegation rules and worker budgets. ADP has no scheduler,
provider catalog, or transport of its own.

This is a clean break from the earlier scheduler-based generation of this
protocol. It has no compatibility runtime, request-file transport, managed
deployment, or in-place state migration from that generation.

## Architecture

```text
frontier parent
      │ prompt submitted
      ▼
deterministic classifier ── requires_delegation / requires_multi / min_agents
      │
      ▼
host-native subagent lifecycle (SubagentStart / SubagentStop)
      │
      ├── PreToolUse gate ── checks delegation and Codex tool-call budgets
      │
      └── Stop gate ── blocks turn completion until delegation evidence exists
```

The classifier is a deterministic, host-agnostic function of the prompt text:
bulk/size/multi-step/shard/domain-family signals and an explicit token-budget
threshold (25% of the active context window) decide whether delegation is
required, whether it must fan out to multiple agents, and the minimum agent
count. Nothing here talks to an external provider, gateway, or credential
store — native agent concurrency is unconstrained except by the host's own
capabilities.

Route work from the lowest capable tier upward: quick for trivial mechanical
work, bulk for routine bounded work, balanced for moderate reasoning, and
frontier for demanding reasoning. Escalate when evidence shows a higher tier
is needed; there is no compulsory attempt or retry at every lower tier.
Workers request upward escalation through the parent because worker recursion
remains strictly downward. The parent's job is to consolidate worker evidence
into a general view of the task: it reconciles focused single-topic workers
instead of doing mixed-topic work itself, keeps topic-specific detail out of
its own context, and every tier that can delegate pushes bounded subtasks
down. Choose the minimum adequate supported reasoning
effort; the low/medium/high/xhigh tier defaults remain available and can be
overridden. The common classifier's `ROUTING_POLICY` supplies generated worker
instructions and context injected at `UserPromptSubmit` and `SubagentStart`.

## Worker budgets

| Tier | Agentic-turn budget | Codex hook-covered tool-call budget |
|------|---------------------|--------------------------------------|
| quick | 128 | 128 |
| bulk | 64 | 64 |
| balanced | 32 | 32 |
| frontier | 16 | 16 |

Both hosts also share a hard cap of 10 concurrently active workers per parent
session, counting nested workers; the hook denies a spawn while the session's
active set is full, and the parent waits for a worker to finish or fans out in
smaller waves.

An agentic turn is a model round within a worker's task, not the entire task,
a parent prompt, or an individual tool call. One turn can produce several
tool calls. Claude enforces agentic rounds through native `maxTurns` on each
worker invocation; resuming a worker may start a fresh native budget. The hook
rejects explicit `max_turns` above the tier cap and accepts lower values.

Codex has two separate mechanisms: generated instructions set an advisory
agentic-turn budget, and the shared hook enforces a hard budget of intercepted
tool-call attempts, including attempts later denied by another hook or the
host. Equal numbers do not make these equivalent units. The Codex ledger is
keyed by worker id and persists across resumes and new parent prompts. Its
budget is pinned at the first native start or tool hook; an identified worker
with an unknown or missing tier gets a conservative limit of 16. Later type
changes cannot raise or reset it. Repeated events with the same tool-call id
count once; events without a call id count on each hook invocation. Corrupt,
unwritable, or locked ledgers deny further covered calls. Workers can still
return a plain final report and stop after exhaustion, but cannot make more
covered tool calls.
The common `WORKER_TURN_LIMITS` defines these per-worker values; parent
sessions are not capped. All tiers retain their normal tools before the cap.

## Repository layout

```text
agents/                 worker profiles and rendering templates
scripts/agents/          classifier and worker-rendering tooling
scripts/hosts/           shared installer, settings and lifecycle engine
scripts/codex/           thin Codex install/uninstall wrappers
scripts/claude/          thin Claude install/uninstall wrappers
codex/                   Codex policy, hook and generated worker
claude/                  Claude policy, hook and generated worker
docs/audit/              history rewrite ledger and convention evidence
```

Codex and Claude installations are independent. Both use the same core
classifier and hook adapter, while their manifests declare different lifecycle
release modes.

## Delegation evidence

There is no request schema, receipt, or backend selection. The only proof of
delegation the protocol recognizes is the host's own native subagent
lifecycle: a `SubagentStart` event opens a worker slot for the session, and a
matching `SubagentStop` (or, on Claude, a foreground Agent result) closes it.
`scripts/hosts/hook_adapter.py` tracks per-session lifecycle state under
`.delegation-protocol/hook-state/`; `scripts/agents/delegation-classifier.py`
decides, purely from the prompt, whether delegation is required at all and
how many concurrent workers it must reach.

`PreToolUse` checks parent delegation evidence and Codex worker tool-call
budgets; `Stop` checks required delegation evidence. Native lifecycle identity
is needed to attribute a tool call to a worker budget. Missing worker identity
or missing hook events limit what the ledger can enforce.

Hooks enforce only calls delivered to them and are not a security sandbox.
Codex's `write_stdin` input and polling have no `PreToolUse` hook, specialized
tool paths may bypass interception, and hosted WebSearch does not emit hooks;
see [Codex tool coverage](https://learn.chatgpt.com/docs/hooks#tool-coverage).
Protocol tests verify supplied hook events; they do not prove interception of
every live host tool. The advisory agentic-turn budget remains separate from
this partial tool-call coverage.

## Install one host

Python 3.11 or newer is required.

Codex:

```bash
bash scripts/codex/install.sh
```

Claude Code:

```bash
bash scripts/claude/install.sh
```

Use the corresponding `.ps1` wrapper on Windows. The shared installer
preflights every source, destination, manifest, and host JSON file before
mutation. It uses a lock, atomic settings writes, rollback, and a complete
ownership manifest. Uninstall removes only unchanged protocol-owned resources
and preserves unrelated configuration.

Codex uses `session_release`; a completed worker never creates an impossible
dismissal warning. Claude uses `automatic_release`; a foreground result clears
its lifecycle automatically. The hook adapter checks worker budgets, observed
delegation, and concurrent fan-out when required.

## Owner bypass

ADP enforcement can be lifted only per action, by explicit authorizing text
in the user's own prompt -- there is no standing bypass or marker file. See
"Owner bypass" in `claude/rules/delegation-protocol.md` (mirrored in
`codex/AGENTS.md`) for the full rule.

## Verify

These tests use disposable homes and do not change live configuration.
Isolation is asserted, not assumed: each host reads its own configuration
directory. Claude resolves through `CLAUDE_CONFIG_DIR` (defaulting to
`$HOME/.claude`), and Codex resolves through `CODEX_HOME` (defaulting to
`$HOME/.codex`). The delegation protocol state is stored under the host's
configuration directory in `.delegation-protocol/`.

```bash
python3 scripts/agents/render-bulk-workers.py --check
python3 scripts/agents/test-render-workers.py
python3 scripts/hosts/test-install.py
python3 scripts/hosts/test-lifecycle.py
python3 scripts/codex/test-protocol.py
python3 scripts/claude/test-protocol.py
```

## Rollback

Before adopting this native-only generation, preserve the prior protocol tip
with an annotated backup tag. An occupied non-matching manifest is refused
with instructions to run the uninstaller from the tagged prior checkout. Do
not mix runtime files or state from the two generations. Rollback means
checking out the backup tag and using its installer as a unit.

The rewrite ledger under `docs/audit/` maps every retained old commit to its
new hash and records message-convention disposition from the last history
rewrite; it is historical record, not a step this rewrite repeats.
