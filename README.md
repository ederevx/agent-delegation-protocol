# Agent Delegation Protocol v2

Protocol v2 lets a frontier coding model coordinate bounded work through one
backend-neutral contract. The parent retains planning, judgment, integration,
conflict resolution, and final validation. Selected backends receive exact
task envelopes and return stable JSON receipts.

v2 is a clean break. It has no compatibility runtime, queue wrapper, alternate
scheduler, or in-place state migration from v1.

## Architecture

```text
frontier parent
      │ bounded request file
      ▼
lifecycle-visible host worker
      │ run, batch, or resume
      ▼
delegationctl ─── selected adapter ─── execution backend
      │                 │
      └──── authenticated TCP loopback lane ────┘
      │
      ▼
structured receipt → parent integration and validation
```

`delegationctl` validates catalogs and requests, selects an available backend
by capability and numeric priority, and owns every provider-operation lease.
The lane service is role-blind and scheduler-owned. It provides FIFO admission,
bounded capacity, inherited-lease reentry, heartbeats, expiry after a client
crash, idle shutdown, and authenticated one-use loopback requests. Permission
work happens after the provider lease is released.

An adapter may translate the common contract into deployment-specific API or
model settings. Those details stay in the integration; they do not enter host
policy, the core catalog schema, or task manifests.

## Repository layout

```text
agents/                 v2 catalog, schemas, worker source and profiles
scripts/agents/         delegationctl, lane service, adapter and tests
scripts/hosts/          shared installer, settings and lifecycle engine
scripts/codex/          thin Codex install/uninstall wrappers
scripts/claude/         thin Claude install/uninstall wrappers
codex/                  Codex policy, hook and generated worker
claude/                 Claude policy, hook and generated worker
integrations/ci-claude/ optional external session-backend catalog fragment
docs/audit/             history rewrite ledger and convention evidence
```

Codex and Claude installations are independent. Both use the same core
contracts and classifier, while their manifests declare different lifecycle
release modes.

## Catalog and requests

[`agents/protocol-v2.json`](agents/protocol-v2.json) contains native backends,
route membership, and optional catalog includes. Each backend declares exactly
one kind (`native`, `oneshot`, or `session`), selector capabilities,
availability checks, JSON or native delivery, numeric priority, and a named
scheduler lane. Route order has no selection meaning; highest priority wins
after filtering, with backend ID as the deterministic tie break.

`run` and `batch` accept `--request-file`. Their top-level selector fields are:

- `schema_version`, fixed at `2`;
- `route`, `runtime`, `platform`, `function`, `mode`, and `workspace`;
- `task` for `run`, or non-empty `tasks` for `batch`.

Every task contains exactly `schema_version`, `id`, `mode`, `repo`, `prompt`,
`allowed_paths`, `workspace`, `validation`, and `budgets`. Budgets contain
positive `timeout_seconds`, `max_output_bytes`, and `max_steps`. Validation is
a list of exact argv arrays. Credentials and deployment settings are never task
data.

`resume` accepts exactly `schema_version`, `backend`, `token`, and a bounded
`resolution` object. It continues the selected session and never falls through
to another backend.

Stable receipt statuses are `native_required`, `ready`, `yielded`,
`permission_required`, `completed`, `failed`, and `cancelled`. A native handoff
exits 69 before any external launch. A paused receipt retains its opaque token;
terminal receipts do not authorize replay. Adapter failure never triggers a
silent native retry.

Schemas live in [`agents/contracts`](agents/contracts). A runnable neutral
adapter and conformance suite demonstrate one-shot, session, batch, pause,
resume, cancellation, and lane behavior.

## Optional external integration

[`integrations/ci-claude/catalog.json`](integrations/ci-claude/catalog.json)
adds an available `ci-claude-worker --v2` session adapter without placing its
provider, model, inference, or credential policy in this repository's core.
When that command is unavailable, selector filtering leaves the applicable
native backend as the fallback before launch.

The external deployment must use the lane descriptor passed in
`DELEGATION_LANE_ENDPOINT`. A delegated request reenters the scheduler's lease;
interactive traffic obtains its own lease from the same service. The proxy
retains credential, session registration, capacity, and forwarding duties, but
does not own a second FIFO or concurrency controller.

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
its lifecycle automatically. The hook classifier gates parent mutation and
turn completion on observed delegation and concurrent fan-out when required.

## Verify

These tests use disposable homes and do not change live configuration:

```bash
python3 scripts/agents/render-bulk-workers.py --check
python3 scripts/agents/test-protocol-v2.py
python3 integrations/ci-claude/test-integration.py
python3 scripts/hosts/test-install.py
python3 scripts/hosts/test-lifecycle.py
python3 scripts/codex/test-protocol.py
python3 scripts/claude/test-protocol.py
python3 scripts/agents/test-audit-commits.py
```

The lane suite binds a local TCP port. Run it in an environment that permits
loopback sockets.

## v1 boundary and rollback

Before adopting v2, preserve both old protocol tips with annotated backup tags.
This overhaul uses:

```text
backup/pre-v2-overhaul-main-20260831
backup/pre-v2-overhaul-ci-agents-20260831
```

An occupied non-v2 manifest is refused with instructions to run the uninstaller
from the tagged v1 checkout. Do not mix old runtime files or state into v2.
Rollback means checking out the backup tag and using its installer as a unit.

The rewrite ledger maps every retained old commit to its new hash and records
message-convention disposition. Only after the full suite and ledger audit pass
should canonical refs replace the old tips.
