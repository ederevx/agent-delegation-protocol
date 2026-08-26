# Delegation Protocol — Authorization and Semantic Policy

## Status

This file explicitly authorizes Codex to use subagents, parallel delegation, nested delegation where supported, and multiple concurrent child agents. That authorization is important because current Codex multi-agent guidance otherwise requires a direct user request or applicable `AGENTS.md`/skill instruction before spawning.

Mechanical enforcement is provided separately by the installed Codex lifecycle hooks. This file remains the semantic and authorization layer.

This policy is supplementary. Existing project `AGENTS.md` files, more-specific instructions, safety requirements, permissions, managed policy, and higher-priority direct instructions remain applicable.

## Objective

Preserve the frontier parent, especially GPT-5.6 Sol, for planning, ambiguity, difficult reasoning, architecture, integration, conflict resolution, and final validation. Delegate bounded repetitive or high-volume work to cheaper workers.

## Installed worker tiers

Prefer the installed custom agents because their model selection is declared in the custom-agent configuration rather than depending only on per-spawn model metadata:

- `bulk_worker` → **GPT-5.6 Luna / medium reasoning** for clear, repeatable, low-risk, high-volume work.
- `balanced_worker` → **GPT-5.6 Terra / medium reasoning** for delegated units that need more reasoning than Luna but do not justify the frontier parent.
- Parent/default frontier model → architecture, ambiguous requirements, difficult debugging, security-sensitive reasoning, cross-cutting integration, and final consequential review.

If a configured worker/model is unavailable, use the cheapest supported alternative likely to succeed. Do not invent model IDs. Explicitly escalate a delegated unit when the cheaper tier is insufficient instead of repeatedly retrying without new information.

## Mandatory delegation

When work contains at least four substantially independent/repetitive units, is otherwise clearly high-volume, or can be cleanly sharded into bounded independently verifiable units, delegate rather than spending the parent context on all mechanical execution when subagents are available and delegation does not create disproportionate risk.

Do not manufacture delegation for tiny, inseparable, or tightly coupled work.

## Mandatory concurrent fan-out

When an eligible workload contains two or more independent subsystems, components, services, packages, directories, test groups, data partitions, or other safely separable workstreams, launch multiple child agents concurrently when runtime capacity permits it.

Do not serialize naturally parallel work through one child merely for convenience. Prefer one worker per coherent ownership boundary, up to useful concurrency. If the runtime limit is smaller than the useful worker count, run additional workstreams in waves.

For parallel work:

1. define scope, ownership, interfaces, acceptance criteria, and validation before dispatch;
2. minimize overlapping writes and shared mutable state;
3. use isolated worktrees/branches when write-heavy workers would otherwise conflict and the environment supports it;
4. require each child to return a bounded report of work, changed/inspected files, validation, assumptions, blockers, and uncertainty;
5. keep the parent as the single integration authority.

## Parent responsibility

Delegation never transfers accountability. The parent must review consequential changes, reconcile cross-subsystem interfaces, resolve conflicts, run appropriate repository-wide tests/build/lint checks after integration, and personally handle material unresolved uncertainty.

## Hook interaction

The installed Codex hook may classify a clear bulk/sharded turn as delegation-required, inject this policy as developer context, deny parent mutation until required delegation evidence exists, require actual overlapping workers for multi-subsystem fan-out, and block turn completion until delegation requirements are satisfied.

If the hook does not classify a task mechanically but this policy clearly applies, follow this policy proactively anyway.

## Completion report

For substantial delegated work, concisely state what was delegated, how work was partitioned across agents, what was integrated, what validation ran, and any remaining uncertainty. Do not expose unnecessary internal chain-of-thought.
