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
delegationctl ─── selected adapter or managed execution engine
      │
      └── managed service ── authenticated gateway ── provider API
                  └──────── named FIFO resource ──────┘
      │
      ▼
structured receipt → parent integration and validation
```

`delegationctl` validates catalogs and requests and selects an available
backend by capability and numeric priority. External JSON adapters use the
role-blind scheduler lane. Managed deployments instead route interactive and
delegated traffic through one authenticated gateway. That service owns FIFO
admission for each actual provider request; runtimes receive only scoped dummy
credentials and never receive or reenter a shared lane lease.

An adapter may translate the common contract into deployment-specific API or
model settings. Those details stay in the integration; they do not enter host
policy, the core catalog schema, or task manifests.

## Repository layout

```text
agents/                 v2 catalog, schemas, worker source and profiles
scripts/agents/         controller, managed runtime, lane, adapter and tests
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
availability checks, JSON, managed, or native delivery, and numeric priority.
External JSON adapters declare a scheduler lane; managed backends name a
separately installed deployment whose resource configuration is authoritative.
Route order has no selection meaning; highest priority wins after filtering,
with backend ID as the deterministic tie break.

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

## Managed deployments

[`agents/contracts/deployment-v1.schema.json`](agents/contracts/deployment-v1.schema.json)
defines provider-neutral gateway, resource, runtime, inference, execution, and
credential-reference policy. Secret values live only in the protected protocol
credential store. The managed service owns singleton election, client
registration, credential injection, restart-safe background bindings, request
admission, streaming, and idle retirement.

The ci-claude catalog fragment names the `ci-claude` deployment. Its external
repository contains only deployment JSON and launch shims; all operational
management lives here. When the deployment or runtime executable is absent,
selection falls back to the applicable native backend before launch.

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

Install a deployment and its launch shim, then enroll its credential without
placing the secret on an argument vector:

```bash
delegationctl deployment install --config deployment.json \
  --launcher ci-claude.sh ci-claude
delegationctl credential set --deployment ci-claude
delegationctl launch --deployment ci-claude -- --help
```

`deployment uninstall` refuses active or retained clients and removes its
credential by default; `--keep-credential` preserves it for rollback.
Installation and removal use digest-owned manifests and never remove modified
or unrelated files.

Codex uses `session_release`; a completed worker never creates an impossible
dismissal warning. Claude uses `automatic_release`; a foreground result clears
its lifecycle automatically. The hook classifier gates parent mutation and
turn completion on observed delegation and concurrent fan-out when required.

## Verify

These tests use disposable homes and do not change live configuration:

```bash
python3 scripts/agents/render-bulk-workers.py --check
python3 scripts/agents/test-protocol-v2.py
python3 scripts/agents/test-managed-service.py
python3 scripts/agents/test-managed-controller.py
python3 scripts/agents/test-claude-runtime.py
python3 scripts/agents/test-execution-engine.py
python3 scripts/agents/test-permission-service.py
python3 integrations/ci-claude/test-integration.py
python3 scripts/hosts/test-install.py
python3 scripts/hosts/test-lifecycle.py
python3 scripts/codex/test-protocol.py
python3 scripts/claude/test-protocol.py
python3 scripts/agents/test-audit-commits.py
```

The lane, gateway, and managed-controller suites bind local TCP ports. Run them
in an environment that permits loopback sockets.

## v1 boundary and rollback

Before adopting v2, preserve both old protocol tips with annotated backup tags.
This overhaul uses:

```text
backup/pre-v2-overhaul-main-20260831
backup/pre-v2-overhaul-ci-agents-20260831
backup/pre-managed-runtime-v3-20260901
```

An occupied non-v2 manifest is refused with instructions to run the uninstaller
from the tagged v1 checkout. Do not mix old runtime files or state into v2.
Rollback means checking out the backup tag and using its installer as a unit.

The rewrite ledger maps every retained old commit to its new hash and records
message-convention disposition. Only after the full suite and ledger audit pass
should canonical refs replace the old tips.
