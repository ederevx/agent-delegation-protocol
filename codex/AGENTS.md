# Delegation Protocol v2

## Purpose

Keep the frontier Codex session responsible for planning, ambiguity,
architecture, integration, conflict resolution, and final validation. Route
routine bounded work needing little interpretation to `bulk_worker`; route
bounded work needing moderate reasoning to `balanced_worker`.

## Required delegation

Delegate work that has three or more distinct steps or is estimated at 25% or
more of the active context window. Adjacent tiers intentionally overlap: choose
the lowest tier with enough reasoning ability for the assignment. Keep difficult
debugging, ambiguity, architecture, integration, and final validation with the
parent. For independent workstreams, use concurrent workers when capacity
permits. Give each worker exclusive ownership, acceptance criteria, validation
commands, and a required evidence report. The parent is the integration
authority.

## v2 execution contract

Workers use the common v2 task fields: `schema_version: 2`, `id`, `mode`,
`repo`, `prompt`, `allowed_paths`, `workspace`, `validation`, and `budgets`.
Budgets contain positive `timeout_seconds`, `max_output_bytes`, and `max_steps`.
Requests are written to a file and executed only through the
absolute launcher under the active Codex home. Use `delegationctl` on POSIX or
`delegationctl.cmd` on Windows:

```text
<active-Codex-home>/.delegation-protocol/delegationctl[.cmd] run --request-file <path>
<active-Codex-home>/.delegation-protocol/delegationctl[.cmd] batch --request-file <path>
<active-Codex-home>/.delegation-protocol/delegationctl[.cmd] resume --request-file <path>
```

The scheduler authenticates the local session, owns the provider lane, and
returns stable structured receipts. Never retry an accepted request through a
different backend. Treat `permission_required` as a parent decision and
preserve its exact request ID, backend, token, and operation for `resume`.

## Conflict boundary

Native shared-workspace workers can see current working-tree changes; isolated
protocol workers cannot see uncommitted parent changes. Either kind must ask
the parent before repository-wide version-control actions, another worker's
files, dependency changes, branch or index changes, or any operation leaving
the machine. The parent decides each request separately.

## Lifecycle

Codex workers report their result and end their host session. The parent
collects the report, integrates only verified evidence, and runs final
repository-wide checks. Do not require an unavailable post-result worker
operation or block completion on one.
