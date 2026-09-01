# Delegation Protocol v2

Claude Code keeps the strongest parent context for planning, ambiguity,
architecture, difficult debugging, integration, conflict resolution, and final
validation. Route routine bounded work needing little interpretation to
`bulk-worker`. Route bounded work needing moderate reasoning to
`balanced-worker` when the task has three or more distinct steps or reaches 25%
of the active context window. Adjacent tiers intentionally overlap; choose the
lowest tier with enough reasoning ability. Keep work that needs parent-level
judgment with the parent.

For independent workstreams, use concurrent workers when capacity permits.
Give each worker exclusive ownership, acceptance criteria, validation commands,
and a concise evidence report. The parent remains the single integration
authority.

## v2 request contract

Workers create file-backed v2 requests. Each task contains exactly
`schema_version: 2`, `id`, `mode`, `repo`, `prompt`, `allowed_paths`,
`workspace`, `validation`, and `budgets`; budgets contain positive timeout,
output-byte, and step limits. Resolve the active
Claude config directory and invoke
its absolute `.delegation-protocol/delegationctl` launcher on POSIX or
`.delegation-protocol/delegationctl.cmd` on Windows. Use `run`, `batch`, or
`resume`, always with `--request-file`.

The scheduler authenticates the local loopback session, owns the provider lane,
selects an available backend by declared capability and runtime, and returns
stable receipts. A selected request is never silently retried elsewhere.
Relay `permission_required` receipts to the parent with their exact request ID,
backend, token, and operation; resume only after a parent decision.

## Conflict boundary

Native shared-workspace workers can see current working-tree changes; isolated
protocol workers cannot see uncommitted parent changes. Either kind asks
through `SendMessage` before touching repository-wide version-control state,
another worker's files, dependencies, branches, indexes, or external systems.
The parent answers each request separately.

## Lifecycle

Claude automatically releases a foreground Agent when its result returns.
Collect and integrate the report normally; do not issue a stop operation for a
completed foreground worker. Use a stop operation only for a running
background task that requires cancellation.

Hooks enforce the deterministic delegation thresholds and request boundary.
This rule supplies judgment for ambiguity and safety without duplicating
provider or transport policy.
