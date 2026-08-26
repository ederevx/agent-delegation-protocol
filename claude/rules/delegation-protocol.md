# Delegation Protocol — Supporting Semantic Rule

## Status

Claude Code delegation is **mechanically enforced by the installed hooks and settings** in this repository. This rule is a supplementary semantic layer for judgment calls the deterministic hook classifier cannot safely infer from one prompt.

Existing `CLAUDE.md`, `CLAUDE.local.md`, project rules, managed policy, permissions, and higher-priority instructions remain applicable.

## Required intent

Preserve the strongest parent model for planning, ambiguity, architecture, difficult debugging, integration, conflict resolution, and final validation. Delegate bounded repetitive or high-volume work to the cheapest suitable supported subagent.

Prefer `bulk-worker` for low-risk mechanical work. Escalate a delegated unit to a stronger model when its reasoning requirements exceed the cheap worker's capability. Do not set or assume a global subagent model that prevents escalation.

## Parallel fan-out

When an eligible task contains two or more independent subsystems, services, modules, packages, directories, test groups, data partitions, or other safely separable workstreams, use multiple subagents concurrently when runtime capacity permits it.

Do not serialize naturally parallel work through one worker merely for convenience. Give workers non-overlapping primary ownership, explicit boundaries, acceptance criteria, and validation commands. Use worktree/equivalent isolation when parallel write-heavy work would otherwise conflict.

The parent remains the single integration authority and must reconcile interfaces, review consequential output, and run repository-wide validation after combining results.

## Hook interaction

The installed Claude hook may:

- classify a clear bulk/sharded prompt as delegation-required;
- inject the delegation/fan-out policy into the current context;
- deny parent mutation until required delegation evidence exists;
- require actual overlapping workers for multi-subsystem fan-out;
- block turn completion until the required delegation evidence exists;
- fail open only when the Agent runtime/model/concurrency path is observed to be unavailable.

If the hook does not classify a task mechanically but this rule clearly applies, follow this rule proactively anyway.

## Guardrails

Do not maximize agent count blindly. Keep tightly coupled work together when splitting would increase coordination or merge risk. Do not use delegation to bypass safety, permissions, managed policy, or more-specific project instructions.
