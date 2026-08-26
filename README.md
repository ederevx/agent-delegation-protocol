# Agent Delegation Protocol

A small policy repository for making a frontier coding model act as the coordinator while delegating bounded bulk work to cheaper supported subagents.

This repository is intentionally **supplementary**. It must not erase or silently replace existing Codex or Claude instructions. The installer uses symlinks where each client supports them and preserves any existing active Codex global instructions when Codex's single-global-file behavior requires composition.

## What it installs

- **Codex:** `codex/AGENTS.md` as global delegation policy.
- **Claude Code:** `claude/rules/delegation-protocol.md` as a user-level rule plus `claude/agents/bulk-worker.md` as the cheap bulk subagent definition.

The policy explicitly authorizes proactive delegation and makes cheap-model routing mandatory for eligible bulk work when subagents and compatible cheaper models are available. The parent model remains responsible for decomposition, integration, review, and final validation.

## Install from a private clone

Keep the clone in a stable location because the installed metadata is symlinked back to this repository.

```bash
git clone git@github.com:<OWNER>/agent-delegation-protocol.git ~/.local/share/agent-delegation-protocol
cd ~/.local/share/agent-delegation-protocol
./scripts/install.sh
```

Windows PowerShell users can clone to a stable path and run:

```powershell
git clone git@github.com:<OWNER>/agent-delegation-protocol.git "$HOME\agent-delegation-protocol"
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

- [`codex/AGENTS.md`](codex/AGENTS.md) — mandatory Codex routing/delegation policy.
- [`codex/INSTALL.md`](codex/INSTALL.md) — Codex-specific installation behavior and limitations.
- [`claude/rules/delegation-protocol.md`](claude/rules/delegation-protocol.md) — mandatory Claude Code routing/delegation rule.
- [`claude/agents/bulk-worker.md`](claude/agents/bulk-worker.md) — Haiku bulk-worker subagent metadata.
- [`claude/INSTALL.md`](claude/INSTALL.md) — Claude-specific installation behavior and verification.

## Enforcement boundary

These files are agent instructions, not a security boundary. They are mandatory **within the applicable instruction hierarchy**, but higher-priority system/developer/user instructions, unavailable tools/models, permission policy, or client-side enforcement can supersede or prevent them. Use hooks/settings when a behavior must be mechanically blocked or guaranteed by the client.
