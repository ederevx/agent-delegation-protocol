# Delegation Protocol

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

Delegation is proven only by Codex's own native subagent lifecycle
(`SubagentStart`/`SubagentStop`) — there is no request format, launcher, or
scheduler to route through.

## Model tiers

`bulk_worker` runs `gpt-5.6-luna`; `balanced_worker` runs `gpt-5.6-terra`; the
parent frontier session runs `gpt-6-astra`. These are explicit slugs, not
aliases that auto-track new generations — re-verify them by asking Codex
directly whenever its model lineup changes (it knows its own capability tiers
better than external documentation), and update this note and the mirrored
Claude-side note in `claude/rules/delegation-protocol.md` together.

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

Resuming a worker session continues it on its original topic only. When the
next task is a different topic from its original deployment, start a fresh
worker instead of reusing the existing session.

## Owner bypass

The user may override all ADP rules and disable all ADP hook enforcement,
including delegation, recursion, and lifecycle requirements. On explicit user
authorization, an assistant may create or remove
`<host-config-dir>/.delegation-protocol/bypass`; agents must never enable
bypass autonomously or infer authorization from a blocked operation.

The marker defaults to `~/.claude/.delegation-protocol/bypass` for Claude and
`~/.codex/.delegation-protocol/bypass` for Codex; `CLAUDE_CONFIG_DIR` and
`CODEX_HOME` select the respective host configuration directory. Its presence
alone activates bypass for all sessions of that host, and it persists until
removed; contents are an optional note. While present, all ADP rules are
waived and its hooks permit every event. Removing it restores ADP enforcement.
This authority does not override host permissions or other protocols.
