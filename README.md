# Agent Delegation Protocol v2

Protocol v2 gives a frontier coding model a small, auditable delegation
surface. The parent keeps planning, judgment, integration, and final
validation. A backend executes bounded tasks described by one common manifest
and returns structured receipts.

This is a clean break from the earlier protocol. v2 has one backend-neutral
contract, one scheduler-owned execution lane, and independent host
integrations. Provider behavior belongs behind a backend adapter; it does not
belong in policy, host hooks, or task manifests.

## Architecture

```text
frontier parent
      │ common task manifest
      ▼
host lifecycle integration
      │ authenticated local request
      ▼
protocol scheduler ─── backend adapter ─── provider
      │ structured receipt
      ▼
parent integration and validation
```

The scheduler owns admission, request authentication, cancellation, deadlines,
and the provider lane. A backend never bypasses the scheduler or causes a
second provider request for a task already accepted by another backend.

The scheduler exposes an authenticated loopback lane. It binds only to the
local interface, authenticates each session and request, rejects unknown or
replayed credentials, and serializes provider work according to the selected
backend's declared limits. The loopback transport is an implementation detail;
the manifest and receipt contracts are the stable interface.

## Repository layout

The v2 integration boundary is explicit:

```text
agents/                 backend catalog and common task definitions
scripts/agents/         scheduler, adapters, rendering, and contract tests
integrations/           host lifecycle profiles and installation surfaces
  common/               shared policy wiring and verification
  codex/                Codex hooks, worker profile, install, uninstall
  claude/               Claude hooks, worker profile, install, uninstall
docs/audit/             rewrite and message-convention evidence
```

Host profiles declare only lifecycle facts: event wiring, state paths,
worker transport, supported runtimes, and how a receipt is surfaced. They do
not select providers or duplicate scheduler policy. Codex and Claude remain
independent installations; installing, updating, or removing one never
changes the other's configuration.

## Common manifest

Every `run` or `batch` request is a bounded JSON manifest. It identifies the
schema version, repository, ownership boundary, task prompt, execution mode,
workspace, and any exact preapproved commands or edit validation required by
the host. A batch contains independently verifiable task entries and a
declared ordering strategy. Paths are repository-relative; secrets and
credentials are never manifest data.

Backends receive this contract without provider names or provider-specific
options. The catalog supplies backend capabilities, runtime compatibility,
limits, and adapter binding. Selection filters by capability and availability,
then applies the configured priority; route membership is not an execution
order.

## Operations and receipts

The scheduler supports three operations:

- `run` accepts one task and returns a receipt for acceptance, completion,
  cancellation, failure, or a required parent decision.
- `batch` accepts one manifest of tasks and returns per-task receipts plus the
  batch outcome. It preserves the manifest's declared ordering semantics.
- `resume` continues a paused request using the exact authenticated session
  and parent decision. It never replays a task under another backend.

Receipts are machine-readable and include the schema version, request ID,
backend, operation, status, and bounded diagnostics. Terminal statuses retain
no resumable credential. A `permission_required` receipt contains only the
exact operation requiring a parent decision; the parent either denies it or
provides a bounded handled result before calling `resume`. Provider errors,
timeouts, cancellation, and command failures are receipts, not implicit
fallback instructions.

## Backend contract

Add a backend by adding its catalog entry and adapter behind the common
interface. The entry declares its capabilities, runtime, limits, availability
probe, priority, and binding. The adapter translates the common manifest and
returns the common receipt; it owns provider-specific authentication, model
parameters, retry interpretation, and response normalization.

Before changing a backend contract, run the catalog, scheduler, adapter, and
host integration tests together. A selected backend is never silently retried
on another provider. Changes to provider behavior must leave the manifest,
receipt schema, and host lifecycle contract unchanged unless the schema is
versioned deliberately.

## Install and uninstall

Install one host integration from a clean v2 checkout:

```bash
bash integrations/codex/install.sh
# or
bash integrations/claude/install.sh
```

The installer verifies the repository, host profile, scheduler, and existing
destination types before changing the host configuration. It records the
installed source revision and performs a self-test before enabling hooks.

Uninstall only the host being removed:

```bash
bash integrations/codex/uninstall.sh
# or
bash integrations/claude/uninstall.sh
```

Uninstallation removes only protocol-owned files and settings, preserves
unrelated host configuration, and leaves the repository and backend catalog
untouched. Windows hosts use the corresponding PowerShell script in the same
integration directory.

## Verification

Run the complete local checks before adopting an integration:

```bash
python3 scripts/agents/render-bulk-workers.py --check
python3 scripts/agents/test-audit-commits.py
python3 scripts/agents/test-delegation-core.py
python3 scripts/codex/test-protocol.py
python3 scripts/claude/test-protocol.py
```

The commit audit reports every active-ref commit and checks author/sign-off,
assistant identity syntax, forbidden or duplicate trailers, explanatory body,
trailer order, and 80-column prose. Keep its output with the rewrite ledger
when reviewing a release.

## v1 boundary and rollback

v1 is not upgraded in place. Before adopting v2, preserve the old branch tips
with annotated backup tags and keep their complete ancestry reachable. The
v2 rewrite uses the backup tags as its comparison and rollback boundary:

```text
backup/pre-v2-overhaul-main-<date>
backup/pre-v2-overhaul-ci-agents-<date>
```

Do not copy old runtime files into v2 or reinterpret old state automatically.
Review settings and task history explicitly, install v2 into a fresh host
profile, run the full verification suite, and adopt the rewritten refs only
after the audit ledger maps every retained commit. Rollback means restoring
the tagged v1 checkout and its host integration; it does not mean mixing v1
and v2 protocol assets in one installation.
