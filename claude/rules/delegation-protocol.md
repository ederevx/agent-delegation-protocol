# Mandatory Delegation and Cost-Routing Protocol

## Status

This rule explicitly authorizes proactive Claude Code subagent delegation, parallel execution, and multiple concurrent subagents. Treat it as mandatory whenever it does not conflict with higher-priority instructions, safety requirements, project rules, permission policy, or unavailable runtime capabilities.

It is supplementary. Existing `CLAUDE.md`, `CLAUDE.local.md`, project rules, and more-specific instructions remain applicable.

## Objective

Preserve the strongest parent model for planning, ambiguity, difficult reasoning, integration, and final review. Use cheaper subagents for bounded bulk work whenever doing so maintains acceptable correctness. When a task contains independent subsystems or safely separable shards, distribute them across multiple subagents so they can progress concurrently.

## Mandatory delegation trigger

You MUST delegate when all of these are true:

1. The work contains at least four substantially independent or repetitive units, or is otherwise clearly high-volume.
2. Units can be assigned with bounded scope and objective acceptance criteria.
3. The Agent tool/subagents are available and permitted.
4. A cheaper suitable subagent such as `bulk-worker` is available, or delegation provides meaningful parallelism/context isolation.
5. Delegation will not create disproportionate merge, security, safety, or coordination risk.

Do not spawn agents merely to increase agent count when direct execution is cheaper and simpler.

## Mandatory multi-agent fan-out

When an eligible delegated workload contains **two or more independent subsystems, components, modules, services, packages, directories, test groups, data partitions, or other safely separable shards**, you MUST treat them as separate workstreams and use multiple subagents concurrently when the runtime permits it.

If workstreams can proceed without blocking one another, you MUST NOT funnel all of them through one subagent merely for convenience. Prefer one subagent per coherent subsystem or shard, up to useful client/runtime concurrency.

For multi-agent work:

1. Define subsystem boundaries, expected interfaces, ownership, and acceptance criteria before dispatch.
2. Give each subagent a non-overlapping primary scope and explicit file/directory ownership when writes are involved.
3. Launch independent subagents concurrently. If concurrency limits prevent launching all useful workers at once, run additional workers in waves.
4. Keep tightly coupled work together when splitting it would increase coordination risk or integration cost.
5. Use worktree or equivalent isolation for parallel write-heavy tasks when available and appropriate.
6. Require each subagent to return a result that can be reviewed and integrated independently.
7. Keep the parent as the single integration authority for cross-subsystem interfaces, conflicts, and final validation.

Examples that normally justify multiple workers include independent frontend/backend changes, separate services, unrelated packages, independent migration batches, disjoint test suites, multiple documentation areas, or large mechanical edits split by directory/file ownership.

## Routing

- Prefer the configured `bulk-worker` for mechanical, low-risk, repetitive, or high-volume work. Multiple `bulk-worker` instances may be used concurrently for independent workstreams.
- Prefer the cheapest model likely to complete a delegated unit correctly. Do not force a blocked or unavailable model.
- Keep the strongest parent model for architecture, ambiguous requirements, difficult debugging, security-sensitive logic, repository-wide integration, conflict resolution, and final consequential review.
- If a cheap worker fails because the task requires stronger reasoning, escalate rather than repeatedly retrying without new information.

## Delegation procedure

Before performing eligible bulk work entirely in the parent context, you MUST:

1. Partition it into non-overlapping units that minimize simultaneous edits to the same files.
2. Identify independent workstreams and fan them out across multiple subagents when useful concurrency is available.
3. Give every worker explicit scope, file boundaries, acceptance criteria, validation commands, and return requirements.
4. Delegate independent units concurrently when useful and safe; do not create a single-worker bottleneck for naturally parallel work.
5. Require changed-file, test/check, failure, assumption, and uncertainty reporting.
6. Use worktree isolation when parallel write-heavy tasks would otherwise conflict.
7. Integrate and validate the combined result in the parent context.

Nested delegation is allowed when it materially improves throughput and remains within Claude Code's configured spawn-depth and concurrency limits. Do not create recursive delegation for work that is already small or well-bounded.

## Parent responsibility

The parent MUST review consequential output, reconcile conflicts and cross-subsystem interfaces, run integration/build/test/lint checks, and personally handle any material unresolved uncertainty. Delegation never transfers responsibility for the final result.

## Parallelism guardrails

Use the smallest number of workers that captures meaningful parallelism. Do not maximize agent count blindly. Avoid concurrent writers to the same files or shared mutable state unless isolation, ownership, and a deliberate merge plan are established. When concurrency is bounded, queue additional independent workstreams in waves.

## Completion report

For substantial delegated work, briefly report what was delegated, how work was partitioned across agents, what was integrated, what validation ran, and any unresolved uncertainty. Do not expose unnecessary internal chain-of-thought.