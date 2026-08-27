# Delegation Protocol — Supporting Semantic Rule

## Status

Claude Code delegation is **mechanically enforced by the installed hooks and settings** in this repository. This rule is a supplementary semantic layer for judgment calls the deterministic hook classifier cannot safely infer from one prompt.

Existing `CLAUDE.md`, `CLAUDE.local.md`, project rules, managed policy, permissions, and higher-priority instructions remain applicable.

## Required intent

Preserve the strongest parent model for planning, ambiguity, architecture, difficult debugging, integration, conflict resolution, and final validation. Delegate bounded repetitive or high-volume work to the cheapest suitable supported subagent.

Prefer `bulk-worker` for low-risk mechanical work. It is a lifecycle-visible dispatcher that submits a bounded task to the installed agent multiplexer, then returns the external receipt or executes natively only when the selected native backend requests it. Escalate a delegated unit when the selected backend's declared capabilities are insufficient.

The multiplexer is agent-agnostic. Each backend has one metadata document declaring a common capability interface, availability checks, limits, and either a native runtime binding or a custom command/API adapter. Its top-level `native` boolean distinguishes those bindings. Routes are ordered lists of backend IDs, so changing priority does not require provider logic in this rule or the worker definition. Selection filters by required capabilities and runtime before taking the first available route entry.

Never silently retry an external task on the native model after it launches. Native execution is valid only when the multiplexer selects the matching native backend before launch and returns its documented native-required receipt.

## Parallel fan-out

When an eligible task contains two or more independent subsystems, services, modules, packages, directories, test groups, data partitions, or other safely separable workstreams, use multiple subagents concurrently when runtime capacity permits it. A selected FIFO delegation queue uses one lifecycle-visible bulk dispatcher with one ordered `queue` batch. A selected round-robin queue exposes a virtual dispatcher pool: launch one lifecycle-visible dispatcher per independent workstream, up to the minimum of useful workstreams, advertised virtual slots, and available host child slots. Each dispatcher submits its own bounded task through multiplexer `run`, and the multiplexer interleaves them on the single physical provider lane.

Do not serialize naturally parallel work through one worker merely for convenience. Give workers non-overlapping primary ownership, explicit boundaries, acceptance criteria, and validation commands. Use worktree/equivalent isolation when parallel write-heavy work would otherwise conflict.

The parent remains the single integration authority and must reconcile interfaces, review consequential output, and run repository-wide validation after combining results.

## Worker lifecycle

A worker does not disappear when its task ends. It goes idle and keeps holding a subagent slot until it is explicitly dismissed, so a turn that spawns workers and never dismisses them steadily consumes the concurrency budget for the rest of the session.

Dismiss each worker with `TaskStop` (pass the worker name or its `name@session` id as `task_id`) as soon as its result has been read and integrated. Dismiss finished workers before spawning replacements rather than accumulating idle ones. Do not dismiss a worker whose output has not been collected yet, and do not keep one alive merely because it might be useful later — spawn a fresh worker when new work appears.

## Local changes belong in this repository

The installed hooks, agent definitions, and rules are symlinks back into this repository, so editing an installed file edits the repository. Any procedural change to delegation behavior — hook logic, gating conditions, worker definitions, installer or settings-merge behavior — must be made in this repository and committed here, not patched in place in an agent's configuration directory.

Ad-hoc local edits are lost on reinstall, diverge silently between machines, and leave the other agent's half inconsistent. If a change is worth making locally, it is worth committing and pushing here.

## Worker conflict escalation

Workers share the parent's working tree and cannot see its uncommitted state, so they are required to ask before acting outside their assigned ownership — repository-wide version-control state, another worker's files, the parent's uncommitted work, dependency changes, or anything that leaves the machine. Workers ask over `SendMessage`; the parent is addressable by the name `ListAgents` reports.

The parent must answer those requests rather than let a worker stall or guess, and is the only party that may escalate the question to the user. Granting permission for one action does not grant it for the next.

Reduce the need for these requests up front: commit or set aside uncommitted work before delegating into a dirty tree, give each worker explicit ownership boundaries, and keep repository-wide state out of worker briefs.

## Hook interaction

The installed Claude hook may:

- classify a clear bulk/sharded prompt as delegation-required;
- inject the delegation/fan-out policy into the current context;
- deny parent mutation until required delegation evidence exists;
- select delegation queue only through the installed multiplexer for a validated available single-stream backend, require actual overlapping workers for round-robin virtual pools and ordinary multi-subsystem fan-out, and preserve the one-dispatcher exception for FIFO queues;
- block turn completion until the required delegation evidence exists;
- record each worker whose task finished, and block new spawns and turn completion until those workers are dismissed with `TaskStop`;
- fail open only when the Agent runtime/model/concurrency path is observed to be unavailable.

If the hook does not classify a task mechanically but this rule clearly applies, follow this rule proactively anyway.

## Guardrails

Do not maximize agent count blindly. Keep tightly coupled work together when splitting would increase coordination or merge risk. Do not use delegation to bypass safety, permissions, managed policy, or more-specific project instructions.
