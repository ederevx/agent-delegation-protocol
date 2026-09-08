# Delegation Protocol

Claude Code keeps the strongest parent context for planning, ambiguity,
architecture, difficult debugging, integration, conflict resolution, and final
validation. Route routine bounded work needing little interpretation to
`bulk-worker`. Route bounded work needing moderate reasoning to
`balanced-worker` when the task has three or more distinct steps or reaches 25%
of the active context window. Adjacent tiers intentionally overlap; choose the
lowest tier with enough reasoning ability. Keep work that needs parent-level
judgment with the parent.

For independent workstreams, use concurrent native subagents when capacity
permits. Give each worker exclusive ownership, acceptance criteria, validation
commands, and a concise evidence report, scoped to a single topic; a task
that carries different context gets its own subagent rather than being
folded into an existing worker's scope. The parent remains the single
integration authority. A worker's report states findings or the completed
result, not a raw dump of what it read or ran; the parent does not repeat a
worker's completed task to re-obtain the same information, redoing it only to
independently review, verify, or correct that worker's own claims. Delegation
is proven only by the host's own native subagent lifecycle
(`SubagentStart`/`SubagentStop`) — there is no other delegation channel,
request format, or scheduler to route through.

## Model tiers

`bulk-worker` and `balanced-worker` bind to the `haiku` and `sonnet` model
aliases, which track the current Claude generation automatically (Haiku 4.5
and Sonnet 5 as of this writing); the parent stays on the frontier model for
the active session (Opus 5). Codex has no alias layer, so its bindings are
explicit slugs: `bulk_worker` → `gpt-5.6-luna`, `balanced_worker` →
`gpt-5.6-terra`, parent → `gpt-6-astra`. Re-verify Codex's slugs by asking
Codex directly whenever its lineup changes — it knows its own capability
tiers better than external documentation — and update this note and
`codex/AGENTS.md` together.

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

Resuming a released worker (via `SendMessage` to its id or name) continues
it on its original topic only. When the next task is a different topic from
what the worker was originally deployed on, spawn a fresh worker instead of
reusing an existing one.

Hooks enforce the deterministic delegation thresholds and request boundary.
This rule supplies judgment for ambiguity and safety without duplicating
provider or transport policy.

## Hook-supplying repo hygiene

This is a convention, not a hook-enforced gate — nothing in `hook_adapter.py`
checks it, so it never blocks a tool call. Before doing any work in a
hook-supplying repo, confirm this checkout sits exactly on the latest `v1.x`
tag reachable from `origin/main`, and check it for stale branches against
`origin/main`; reconcile anything with unmerged value into `main` first, then
drop the stale branch. Never push to or merge directly into `main` yourself —
reconcile through a PR and let the user land it.

Iterating on a change to hooks/rules may happen in an isolated test
checkout that is not the one actually installed. That test environment
never counts as done on its own: land the change on `origin/main` via a
PR, cut the next `v1.x` tag on the merged HEAD, and reinstall the actual
host(s) from that tag so the installed checkout matches it before relying
on the change.

This repo carries two tag schemes over an overlapping range: `protocol-v1`
through `protocol-v11`, and `v1.0` through `v1.4`, backfilled onto the same
five most recent commits so the two schemes are fully parallel there —
`v1.0`=`protocol-v7`, `v1.1`=`protocol-v8`, `v1.2`=`protocol-v9`,
`v1.3`=`protocol-v10`, `v1.4`=`protocol-v11`. `protocol-v1` through
`protocol-v6` predate the backfill and have no `v1.x` counterpart.
`protocol-v*` is retired as of this rule: every tag cut from here forward
uses `v1.x` only, continuing from `v1.4` (the next tag is `v1.5`, not a
restart). Every existing tag under either scheme is retained unchanged as
history — none are renamed, moved, or deleted — so a checkout pinned to a
`protocol-v*`-only tag (`protocol-v1` through `protocol-v6`) predates this
rule, which is expected, not a mismatch to reconcile.

## Owner bypass

The user may unconditionally lift any hook-enforced convention here —
delegation or release — by creating
`<host-config-dir>/.delegation-protocol/bypass`; presence alone is enough,
its contents are just an optional note. Agents must never create, edit, or
script around this file themselves; it exists solely for the human owner to
invoke by hand.
