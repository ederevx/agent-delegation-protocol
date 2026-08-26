# Mandatory Delegation and Cost-Routing Protocol

## Status

This rule explicitly authorizes proactive Claude Code subagent delegation. Treat it as mandatory whenever it does not conflict with higher-priority instructions, safety requirements, project rules, permission policy, or unavailable runtime capabilities.

It is supplementary. Existing `CLAUDE.md`, `CLAUDE.local.md`, project rules, and more-specific instructions remain applicable.

## Objective

Preserve the strongest parent model for planning, ambiguity, difficult reasoning, integration, and final review. Use cheaper subagents for bounded bulk work whenever doing so maintains acceptable correctness.

## Mandatory delegation trigger

You MUST delegate when all of these are true:

1. The work contains at least four substantially independent or repetitive units, or is otherwise clearly high-volume.
2. Units can be assigned with bounded scope and objective acceptance criteria.
3. The Agent tool/subagents are available and permitted.
4. A cheaper suitable subagent such as `bulk-worker` is available, or delegation provides meaningful parallelism/context isolation.
5. Delegation will not create disproportionate merge, security, safety, or coordination risk.

Do not spawn agents merely to increase agent count when direct execution is cheaper and simpler.

## Routing

- Prefer the configured `bulk-worker` for mechanical, low-risk, repetitive, or high-volume work.
- Prefer the cheapest model likely to complete a delegated unit correctly. Do not force a blocked or unavailable model.
- Keep the strongest parent model for architecture, ambiguous requirements, difficult debugging, security-sensitive logic, repository-wide integration, conflict resolution, and final consequential review.
- If a cheap worker fails because the task requires stronger reasoning, escalate rather than repeatedly retrying without new information.

## Delegation procedure

Before performing eligible bulk work entirely in the parent context, you MUST:

1. Partition it into non-overlapping units that minimize simultaneous edits to the same files.
2. Delegate independent units concurrently when useful and safe.
3. Give every worker explicit scope, file boundaries, acceptance criteria, validation commands, and return requirements.
4. Require changed-file, test/check, failure, assumption, and uncertainty reporting.
5. Use worktree isolation when parallel write-heavy tasks would otherwise conflict.
6. Integrate and validate the combined result in the parent context.

Nested delegation is allowed when it materially improves throughput and remains within Claude Code's configured spawn-depth and concurrency limits. Do not create recursive delegation for work that is already small or well-bounded.

## Parent responsibility

The parent MUST review consequential output, reconcile conflicts, run integration/build/test/lint checks, and personally handle any material unresolved uncertainty. Delegation never transfers responsibility for the final result.

## Completion report

For substantial delegated work, briefly report what was delegated, what was integrated, what validation ran, and any unresolved uncertainty. Do not expose unnecessary internal chain-of-thought.
