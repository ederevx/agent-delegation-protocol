# Delegation Protocol — Authorization and Semantic Policy

## Status

This file explicitly authorizes Codex to use subagents, parallel delegation, nested delegation where supported, and multiple concurrent child agents. That authorization is important because current Codex multi-agent guidance otherwise requires a direct user request or applicable `AGENTS.md`/skill instruction before spawning.

Mechanical enforcement is provided separately by the installed Codex lifecycle hooks. This file remains the semantic and authorization layer.

This policy is supplementary. Existing project `AGENTS.md` files, more-specific instructions, safety requirements, permissions, managed policy, and higher-priority direct instructions remain applicable.

## Objective

Preserve the frontier parent, especially GPT-5.6 Sol, for planning, ambiguity, difficult reasoning, architecture, integration, conflict resolution, and final validation. Delegate bounded repetitive or high-volume work to cheaper workers.

## Installed worker tiers and routing

Prefer the installed custom agents. `bulk_worker` is a lifecycle-visible dispatcher: it submits a bounded task to the installed agent multiplexer, then returns the external receipt or executes natively only when the selected native backend requests that behavior. `balanced_worker` remains GPT-5.6 Terra at medium reasoning for delegated units that need more judgment. The parent/default frontier model remains responsible for architecture, ambiguity, difficult debugging, security-sensitive reasoning, integration, and consequential review.

The multiplexer is agent-agnostic. Each backend has one metadata document that declares a common capability interface, availability checks, limits, and either a native runtime binding or a custom command/API adapter. The top-level `native` boolean distinguishes those bindings. Routes contain only ordered backend IDs, so priority can be rearranged without changing host policy or adapter code. Selection first filters for the task's required capabilities and runtime, then chooses the first available route entry.

Delegation queue is the explicit exception for a validated, available single-stream backend. When the hook selects it for a multi-workstream turn, one lifecycle-visible dispatcher submits all independent units as one ordered batch through multiplexer `queue`; no host-level overlap is required. Without that explicit selection, normal concurrent fan-out remains mandatory. Never silently retry an external or queued task on a native model after launch; native execution is valid only when the multiplexer selects the matching native backend before launch and returns its documented native-required receipt.

## Mandatory delegation

When work contains at least four substantially independent/repetitive units, is otherwise clearly high-volume, or can be cleanly sharded into bounded independently verifiable units, delegate rather than spending the parent context on all mechanical execution when subagents are available and delegation does not create disproportionate risk.

Do not manufacture delegation for tiny, inseparable, or tightly coupled work.

## Mandatory concurrent fan-out

When an eligible workload contains two or more independent subsystems, components, services, packages, directories, test groups, data partitions, or other safely separable workstreams, launch multiple child agents concurrently when runtime capacity permits it. The only exception is when the hook explicitly selects delegation queue for a validated single-stream backend; then launch one lifecycle-visible bulk dispatcher with one ordered batch.

Do not serialize naturally parallel work through one child merely for convenience. Prefer one worker per coherent ownership boundary, up to useful concurrency. If the runtime limit is smaller than the useful worker count, run additional workstreams in waves.

For parallel work:

1. define scope, ownership, interfaces, acceptance criteria, and validation before dispatch;
2. minimize overlapping writes and shared mutable state;
3. use isolated worktrees/branches when write-heavy workers would otherwise conflict and the environment supports it;
4. require each child to return a bounded report of work, changed/inspected files, validation, assumptions, blockers, and uncertainty;
5. keep the parent as the single integration authority.

## Parent responsibility

Delegation never transfers accountability. The parent must review consequential changes, reconcile cross-subsystem interfaces, resolve conflicts, run appropriate repository-wide tests/build/lint checks after integration, and personally handle material unresolved uncertainty.

## Worker conflict escalation

Workers share the parent's working tree and cannot see its uncommitted state. A worker must ask the parent before any action outside its assigned ownership — repository-wide version-control state (`git stash`, `git checkout --`, `git reset`, `git clean`, index or branch changes), another worker's files, the parent's uncommitted work, dependency installs or removals, and anything that leaves the machine such as a push or a deploy. It asks over whatever channel the runtime provides for reaching the parent; where none exists, it stops and reports instead of proceeding.

The parent must answer those requests rather than let a worker stall or guess, and is the only party that may escalate the question to the user. Permission granted for one action does not extend to the next.

Reduce the need for these requests up front: commit or set aside uncommitted work before delegating into a dirty tree, give each worker explicit ownership boundaries, and keep repository-wide state out of worker briefs.

## Worker lifecycle

A worker does not disappear when its task ends. It goes idle and keeps holding a concurrency slot until it is explicitly dismissed, so a turn that spawns workers and never dismisses them steadily consumes the budget for the rest of the session.

Dismiss each worker as soon as its result has been read and integrated, using whatever stop/dismiss call this build exposes for a running task or agent. Dismiss finished workers before spawning replacements rather than accumulating idle ones. Do not dismiss a worker whose output has not been collected yet, and do not keep one alive merely because it might be useful later — spawn a fresh worker when new work appears.

## Local changes belong in this repository

The installed hooks, worker definitions, and instruction files are symlinked back into the agent-delegation-protocol repository, so editing an installed file edits the repository. Any procedural change to delegation behavior — hook logic, gating conditions, worker definitions, installer or hooks-manager behavior — must be made in that repository and committed there, not patched in place in an agent's configuration directory.

Ad-hoc local edits are lost on reinstall, diverge silently between machines, and leave the other agent's half inconsistent.

## Hook interaction

The installed Codex hook may classify a clear bulk/sharded turn as delegation-required, inject this policy as developer context, deny parent mutation until required delegation evidence exists, select delegation queue only through the installed multiplexer for a validated available single-stream backend, otherwise require actual overlapping workers for multi-subsystem fan-out, block turn completion until delegation requirements are satisfied, and record each worker whose task finished so that new spawns and turn completion are gated until those workers are dismissed.

If the hook does not classify a task mechanically but this policy clearly applies, follow this policy proactively anyway.

## Completion report

For substantial delegated work, concisely state what was delegated, how work was partitioned across agents, what was integrated, what validation ran, and any remaining uncertainty. Do not expose unnecessary internal chain-of-thought.
