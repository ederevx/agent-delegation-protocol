# Agent Delegation Protocol

A small policy repository for making a frontier coding model act as the coordinator while delegating bounded bulk work to cheaper supported subagents.

This repository is intentionally **supplementary**. It must not erase or silently replace existing Codex or Claude instructions. The installer uses symlinks where each client supports them and preserves any existing active Codex global instructions when Codex's single-global-file behavior requires composition.

## What it installs

- **Codex:** `codex/AGENTS.md` as global delegation policy.
- **Claude Code:** `claude/rules/delegation-protocol.md` as a user-level rule plus `claude/agents/bulk-worker.md` as the cheap bulk subagent definition.

The policy explicitly authorizes proactive delegation and makes cheap-model routing mandatory for eligible bulk work when subagents and compatible cheaper models are available. The parent model remains responsible for decomposition, integration, review, and final validation.

## Mandatory parallel fan-out

Bulk delegation is not limited to one child agent. When a task contains multiple independent subsystems, components, modules, services, packages, directories, test groups, data partitions, or other safely separable shards, the parent is instructed to launch **multiple subagents concurrently** when the runtime permits it.

The intended pattern is:

```text
Frontier parent / coordinator
├── cheap worker → subsystem A
├── cheap worker → subsystem B
├── cheap worker → subsystem C
└── stronger worker if needed → difficult subsystem D
        ↓
parent integration + repository-wide validation
```

The protocol requires non-overlapping ownership where practical, explicit interface and acceptance criteria, concurrent execution for independent work, worktree/equivalent isolation for conflicting write-heavy tasks when available, and parent-controlled integration. If the client concurrency limit is smaller than the useful worker count, additional independent workstreams should run in waves rather than being collapsed into a single-worker bottleneck.

The goal is **useful parallelism**, not maximum agent count. Tightly coupled work should remain together when splitting it would increase coordination or merge risk.

## Install from a private clone

Keep the clone in a stable location because the installed metadata is symlinked back to this repository.

```bash
git clone git@github.com:ederevx/agent-delegation-protocol.git ~/.local/share/agent-delegation-protocol
cd ~/.local/share/agent-delegation-protocol
./scripts/install.sh
```

Windows PowerShell users can clone to a stable path and run:

```powershell
git clone git@github.com:ederevx/agent-delegation-protocol.git "$HOME\agent-delegation-protocol"
cd "$HOME\agent-delegation-protocol"
.\scripts\install.ps1
```

Creating symbolic links on native Windows may require Developer Mode or an elevated shell.

## Update

```bash
cd ~/.local/share/agent-delegation-protocol
git pull --ff-only
./scripts/install.sh
```

Re-running the installer is important for Codex only when a composed global file is in use, because Codex does not provide an include directive for global `AGENTS.md` content.

## Uninstall

```bash
./scripts/uninstall.sh
```

or on PowerShell:

```powershell
.\scripts\uninstall.ps1
```

The uninstall scripts remove only links/state created by this repository and restore a backed-up Codex global override when one was present before installation.

## Files

- [`codex/AGENTS.md`](codex/AGENTS.md) — mandatory Codex routing, delegation, and multi-agent fan-out policy.
- [`codex/INSTALL.md`](codex/INSTALL.md) — Codex-specific installation behavior and limitations.
- [`claude/rules/delegation-protocol.md`](claude/rules/delegation-protocol.md) — mandatory Claude Code routing, delegation, and multi-agent fan-out rule.
- [`claude/agents/bulk-worker.md`](claude/agents/bulk-worker.md) — Haiku bulk-worker subagent metadata; multiple instances may run concurrently for independent workstreams.
- [`claude/INSTALL.md`](claude/INSTALL.md) — Claude-specific installation behavior and verification.

## Enforcement boundary

These files are agent instructions, not a security boundary. They are mandatory **within the applicable instruction hierarchy**, but higher-priority system/developer/user instructions, unavailable tools/models, permission policy, or client-side enforcement can supersede or prevent them. Use hooks/settings when a behavior must be mechanically blocked or guaranteed by the client.