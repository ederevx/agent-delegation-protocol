# Agent Delegation Protocol

A configuration protocol that makes a frontier coding model act as coordinator while delegating bounded bulk work to cheaper supported workers, mechanically enforced by lifecycle hooks in both Codex and Claude Code.

Codex and Claude Code are intentionally **independent installations**. There is no combined installer. Installing one agent must not modify the other agent's configuration.

The protocol is supplementary: existing applicable instructions, hooks, and settings are preserved rather than silently replaced.

## Required behavior

For eligible bulk/high-volume work, preserve frontier-model effort for planning, ambiguity, difficult reasoning, architecture, integration, conflict resolution, and final validation. Delegate bounded work to the cheapest suitable worker.

When a task contains multiple independent subsystems, components, modules, services, packages, directories, test groups, data partitions, or other safely separable workstreams, use **multiple concurrent agents** when runtime capacity permits it instead of serializing naturally parallel work through one worker.

```text
Frontier parent / coordinator
├── cheap worker → subsystem A
├── cheap worker → subsystem B
├── cheap worker → subsystem C
└── stronger worker → unusually difficult subsystem D
        ↓
parent integration + repository-wide validation
```

Use non-overlapping ownership where practical, explicit interfaces/acceptance criteria, isolation for conflicting write-heavy work, and parent-controlled integration. The goal is useful parallelism, not maximum agent count.

## Workers ask before conflicting

Workers run in the parent's working tree and cannot see its uncommitted state, so a worker that reaches for repository-wide state on its own can silently destroy the parent's work. Before acting outside its assigned ownership — repo-wide version-control state, another worker's files, the parent's uncommitted work, dependency changes, or anything that leaves the machine — a worker must ask the parent and wait for an answer. The parent answers; only the parent may take the question to the user.

## Worker lifecycle

A worker does not disappear when its task ends. It goes idle and keeps holding a concurrency slot, so the parent must dismiss it once its report has been read:

```
parent spawns worker → worker works → worker reports
        ↓                                    ↓
   slot occupied ←──── still idle ←──── task finished
        ↓
parent dismisses worker (TaskStop / build's stop-task call)
        ↓
   slot released → available for the next worker
```

Both hooks record each finished-but-undismissed worker and gate on it: new spawns are denied and turn completion is blocked until the outstanding workers are dismissed. The record is per-turn and cleared by the next prompt, so a worker that never reports cannot wedge the session.

## Changes belong in this repository

Installed hooks, worker definitions, rules, and instruction files are symlinked back here, so editing an installed file edits this repository. Procedural changes to delegation behavior belong in a commit here, never as an in-place patch to an agent's configuration directory: local edits are lost on reinstall, diverge between machines, and leave the other agent's half inconsistent.

## Clone once

Keep the clone at a stable path because installed metadata/hooks are symlinked back to it.

```bash
git clone git@github.com:ederevx/agent-delegation-protocol.git ~/.local/share/agent-delegation-protocol
cd ~/.local/share/agent-delegation-protocol
```

Windows users can choose another stable path:

```powershell
git clone git@github.com:ederevx/agent-delegation-protocol.git "$HOME\agent-delegation-protocol"
cd "$HOME\agent-delegation-protocol"
```

Creating symbolic links on native Windows may require Developer Mode or an elevated shell.

## Install Codex only

macOS/Linux:

```bash
bash scripts/codex/install.sh
```

Windows PowerShell:

```powershell
.\scripts\codex\install.ps1
```

Codex now uses a three-layer implementation:

1. **AGENTS authorization/semantic policy** — standing authorization for subagents and parallel delegation while preserving pre-existing global instructions.
2. **Custom worker agents** — `bulk_worker` pinned to GPT-5.6 Luna and `balanced_worker` pinned to GPT-5.6 Terra, avoiding a global cheap-model default that would also affect difficult subagents.
3. **Lifecycle hooks** — `UserPromptSubmit`, `SubagentStart`, `SubagentStop`, `PreToolUse`, `PostToolUse(Agent)`, and `Stop` mechanically gate clear bulk/sharded work.

For multi-subsystem tasks the Codex hook requires evidence of **actual overlapping workers**, not merely two sequential agent runs.

**Important:** current Codex requires non-managed hooks to be reviewed/trusted. After installation, restart Codex, run `/hooks`, review the protocol definition, and trust/enable it. Until then, the AGENTS policy/custom workers are installed but mechanical hook enforcement may be skipped.

See [`codex/INSTALL.md`](codex/INSTALL.md).

If an agent is upgrading an older AGENTS-only, partial, or legacy combined installation, it MUST follow [`codex/MIGRATE.md`](codex/MIGRATE.md). That runbook is written directly for Codex and requires non-destructive inventory, migration, verification, and rollback without touching Claude.

## Install Claude only

macOS/Linux:

```bash
bash scripts/claude/install.sh
```

Windows PowerShell:

```powershell
.\scripts\claude\install.ps1
```

Claude installation manages only the configured Claude home (normally `~/.claude`) and installs:

- a `bulk-worker` custom subagent using the Haiku model alias;
- a supplementary semantic rule;
- a local enforcement hook;
- non-destructively merged lifecycle hooks in `settings.json`;
- explicit subagent concurrency/depth defaults when absent;
- experimental agent teams when absent, as an optional additional coordination capability.

Claude enforcement is not text-only. The hook classifies clear bulk/sharded requests, records actual worker starts/stops, denies parent mutation before required delegation, and blocks turn completion until delegation requirements are satisfied. Independent-subsystem work requires actual overlapping workers.

See [`claude/INSTALL.md`](claude/INSTALL.md).

If an agent is upgrading an older text-only, partial, or legacy combined installation, it MUST follow [`claude/MIGRATE.md`](claude/MIGRATE.md). That runbook is written directly for Claude Code and requires non-destructive settings/hook migration without touching Codex.

## Agent-facing migration runbooks

These files are operational instructions for the agents themselves, not merely human release notes:

- [`codex/MIGRATE.md`](codex/MIGRATE.md) — migrate Codex from legacy AGENTS-only/partial structures into AGENTS + custom workers + hooks.
- [`claude/MIGRATE.md`](claude/MIGRATE.md) — migrate Claude from legacy text-first/partial structures into rule + worker + hooks + merged settings.

Both runbooks make preservation, independent installation, self-test verification, failure handling, and rollback mandatory.

## Self-tests

These tests use temporary config directories and do not modify your live Codex or Claude configuration:

```bash
python3 scripts/codex/test-protocol.py
python3 scripts/claude/test-protocol.py
```

They verify non-destructive hook/settings merge behavior plus single-worker and concurrent-fan-out gating.

## Update

Pull the repository, then rerun only the installer for the agent you use:

```bash
git pull --ff-only
bash scripts/codex/install.sh   # Codex only
# or
bash scripts/claude/install.sh  # Claude only
```

Codex composed-global mode needs reinstall to refresh the composed instruction file. Both agents' symlinked hook/agent files pick up repository changes immediately, while rerunning the appropriate installer refreshes protocol-owned hook/settings entries.

For upgrades from a materially older installation layout, use the applicable `MIGRATE.md` rather than improvising cleanup.

## Uninstall independently

Codex only:

```bash
bash scripts/codex/uninstall.sh
```

Claude only:

```bash
bash scripts/claude/uninstall.sh
```

PowerShell equivalents are in the same per-agent directories. Neither uninstaller intentionally touches the other agent.

## Repository layout

```text
codex/
  AGENTS.md
  INSTALL.md
  MIGRATE.md
  agents/
    bulk-worker.toml
    balanced-worker.toml
  hooks/
    delegation-enforcer.py
claude/
  INSTALL.md
  MIGRATE.md
  agents/
    bulk-worker.md
  hooks/
    delegation-enforcer.py
  rules/
    delegation-protocol.md
scripts/
  codex/
    install.sh
    uninstall.sh
    install.ps1
    uninstall.ps1
    manage-hooks.py
    test-protocol.py
  claude/
    install.sh
    uninstall.sh
    install.ps1
    uninstall.ps1
    manage-settings.py
    test-protocol.py
```

## Enforcement boundary

Both agents now use mechanical lifecycle hooks plus a supporting semantic policy. Codex retains AGENTS because current Codex subagent workflows still treat direct user requests or applicable `AGENTS.md`/skill instructions as spawning authorization. Neither implementation can override higher-priority system/developer/user instructions, managed organization policy, unavailable tools/models, hook trust requirements, or platform safety controls.

## License

Licensed under [Creative Commons Attribution 4.0 International (CC BY 4.0)](https://creativecommons.org/licenses/by/4.0/) — see [LICENSE](LICENSE) for the full legal text.

You are free to share and adapt this work for any purpose, including commercially, as long as you give appropriate credit. Attribution must name the original author and be preserved in every copy or derivative, including further edits or forks.
