# Agent Delegation Protocol

The protocol answers one question: did the host actually perform native
delegation before the parent is allowed to mutate or finish its turn? It has
no scheduler, provider catalog, or transport of its own. The parent retains
planning, judgment, integration, conflict resolution, and final validation;
selected work is bounded work the parent hands to native subagents the host
already knows how to run.

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
      ├── PreToolUse gate ── blocks parent mutation until delegation evidence exists
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

The generated low-tier workers handle bounded low-risk work that needs little
interpretation, including mechanical edits, straightforward audits, extraction,
repetitive processing, and text compression. Generated balanced workers handle
the same bounded shapes when moderate reasoning is useful, plus more demanding
local work. The balanced tier likewise overlaps the frontier parent at its upper
edge. The parent chooses the lowest tier with enough reasoning ability and
retains architecture, integration, conflict resolution, and final validation.

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

`PreToolUse` denies an eligible parent mutation until that evidence exists.
`Stop` denies turn completion the same way. Neither gate consults a catalog,
lane, or credential store — there is nothing external left to consult.

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
its lifecycle automatically. The hook adapter gates parent mutation and turn
completion on observed delegation and concurrent fan-out when required.

## Verify

These tests use disposable homes and do not change live configuration.
Isolation is asserted, not assumed: every store root resolves through
`DELEGATION_CONFIG_HOME` and `DELEGATION_STATE_HOME`, the only overrides
honoured on every platform. The per-platform fallbacks differ
(`LOCALAPPDATA` on Windows, `XDG_CONFIG_HOME`/`XDG_STATE_HOME`
elsewhere), so setting an XDG variable alone does not isolate a store on
Windows.

```bash
python3 scripts/agents/render-bulk-workers.py --check
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
