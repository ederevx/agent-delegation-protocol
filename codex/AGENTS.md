# Mandatory Delegation and Cost-Routing Protocol

## Status

This file is an explicit authorization to use Codex subagents, delegation, parallel agent work, and multiple concurrent child agents. Treat this protocol as mandatory whenever it does not conflict with higher-priority instructions, safety requirements, repository-specific instructions, or unavailable runtime capabilities.

This policy is supplementary. Never delete, ignore, or weaken pre-existing applicable instructions merely because this file is present.

## Objective

When the parent is a frontier model, especially GPT-5.6 Sol, preserve the parent for planning, ambiguity resolution, difficult reasoning, integration, and final review. Delegate bounded, repetitive, independently verifiable work to the cheapest compatible supported subagent model that is likely to succeed. When work spans independent subsystems or shards, fan it out across multiple child agents so those units can progress in parallel.

## Mandatory delegation trigger

You MUST delegate instead of performing the work entirely in the parent context when all of the following are true:

1. The work contains at least four substantially independent or repetitive units, or is otherwise clearly high-volume.
2. Each delegated unit can be given a bounded scope and objective acceptance criteria.
3. Subagent spawning is available and permitted.
4. A cheaper compatible child model is exposed for `spawn_agent`, or delegation still provides meaningful parallelism/context isolation.
5. Delegation will not create disproportionate merge, safety, security, or coordination risk.

Do not manufacture delegation when the task is tiny or inseparable. The purpose is cost-effective throughput, not agent count.

## Mandatory multi-agent fan-out

When an eligible delegated workload contains **two or more independent subsystems, components, modules, services, packages, directories, test groups, data partitions, or other safely separable shards**, you MUST consider them separate workstreams and use multiple child agents concurrently when the runtime permits it.

If multiple workstreams can proceed without blocking each other, you MUST NOT serialize all of them through one child merely for convenience. Prefer one child per coherent subsystem or shard, up to the useful concurrency supported by the client/runtime.

For multi-agent work:

1. Define subsystem boundaries, expected interfaces, ownership, and acceptance criteria before spawning workers.
2. Give each child a non-overlapping primary scope. Avoid multiple writers to the same file or shared mutable state unless deliberate isolation and merge ownership are established.
3. Launch independent children concurrently. If the runtime concurrency limit is lower than the useful worker count, run them in waves rather than collapsing the workload back into one worker.
4. Keep tightly coupled changes together under one worker when splitting them would create more coordination cost than useful parallelism.
5. Prefer isolated worktrees/branches or equivalent isolation for parallel write-heavy tasks when available and appropriate.
6. Require every child to return a bounded result that the parent can review and integrate independently.
7. The parent remains the single integration authority and must reconcile interfaces, cross-subsystem behavior, and final repository state.

Examples that normally justify multiple workers include independent backend/frontend changes, separate services, unrelated packages, independent migration batches, disjoint test suites, multiple documentation areas, or large mechanical edits split by directory/file ownership.

## Model routing

- Prefer the **cheapest supported model likely to complete the delegated unit correctly**.
- When exposed and suitable, prefer **GPT-5.6 Luna** for low-risk bulk work such as search, classification, extraction, repetitive edits, mechanical refactors, documentation updates, test generation, lint fixes, and straightforward implementation.
- Use an intermediate model such as **GPT-5.6 Terra** when Luna is insufficient but Sol-level reasoning is unnecessary.
- Reserve **GPT-5.6 Sol** for architecture, ambiguous requirements, difficult debugging, security-sensitive reasoning, cross-cutting integration, conflict resolution, and final high-value review.
- Do not invent model IDs. Inspect the models actually exposed by the current `spawn_agent` interface and choose only a supported override.
- If no cheaper compatible model override is available, use an available child model when parallelism/context isolation still helps; otherwise perform the work in the parent.

## Delegation procedure

Before doing eligible bulk work yourself, you MUST:

1. Partition it into non-overlapping units that minimize concurrent edits to the same files.
2. Identify which units can run independently and fan those units out across multiple child agents when useful concurrency is available.
3. Give each child a precise task, explicit file/scope boundaries, acceptance criteria, validation commands, and required return format.
4. Run independent units concurrently when doing so is safe and useful; do not create a single-child bottleneck for naturally parallel work.
5. Require each child to report changed files, tests/checks run, failures, assumptions, and unresolved uncertainty.
6. Escalate failed or ambiguous units to a stronger model rather than repeatedly spending cheap-model attempts without progress.
7. Keep final integration and repository-wide validation under the parent model's responsibility.

## Parent responsibility

Delegation never transfers accountability. The parent MUST:

- inspect results before accepting consequential changes;
- review every security-sensitive, architectural, migration, data-loss, or externally visible change;
- reconcile conflicting child outputs and cross-subsystem interfaces;
- run the appropriate integration/build/test/lint checks after combining work;
- verify that delegated work complied with all more-specific `AGENTS.md` instructions in the files it touched;
- use Sol-level reasoning for the final decision when uncertainty remains material.

## Parallelism guardrails

Parallelize read-only research, searches, independent subsystems, independent modules, independent test files, disjoint directory/file ownership, and clearly separable data/work batches. Avoid concurrent workers on the same file, shared mutable state, or tightly coupled interfaces unless isolation, ownership, and a deliberate merge plan are provided.

Do not maximize agent count blindly. Use the smallest number of workers that captures the meaningful parallelism of the task. When concurrency is bounded, queue additional independent workstreams in waves.

## Failure policy

- First failure: improve scope/context and retry once if the failure is clearly recoverable.
- Repeated failure, unclear behavior, or cross-cutting consequences: escalate model capability.
- Never hide a failed delegated task by silently completing a materially different task.

## Completion report

For substantial delegated work, the final parent response should concisely state what was delegated, how work was partitioned across agents, what was integrated, what validation was run, and any remaining uncertainty. Do not expose unnecessary internal chain-of-thought.