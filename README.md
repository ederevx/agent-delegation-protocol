# Agent Delegation Protocol

A private policy/configuration repository for making a frontier coding model act as coordinator while delegating bounded bulk work to cheaper supported workers.

Codex and Claude Code are intentionally **independent installations**. There is no combined installer. Installing one agent must not modify the other agent's configuration.

The protocol is supplementary: it preserves existing applicable instructions and configuration instead of silently replacing them.

## Behavior enforced

For eligible bulk/high-volume work, the parent should preserve frontier-model effort for planning, ambiguity, difficult reasoning, integration, conflict resolution, and final validation while delegating bounded work to the cheapest suitable supported worker.

When a task contains multiple independent subsystems, components, modules, services, packages, directories, test groups, data partitions, or other safely separable shards, the parent must use **multiple concurrent agents** when runtime capacity permits it rather than serializing naturally parallel work through one worker.

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

## Clone once

Keep the clone at a stable path because installed metadata/hooks are symlinked back to it.

```bash
git clone git@github.com:ederevx/agent-delegation-protocol.git ~/.local/share/agent-delegation-protocol
cd ~/.local/share/agent-delegation-protocol
```

Windows users can choose another stable path, for example:

```powershell
git clone git@github.com:ederevx/agent-delegation-protocol.git "$HOME\agent-delegation-protocol"
cd "$HOME\agent-delegation-protocol"
```

Creating symbolic links on native Windows may require Developer Mode or an elevated shell.

## Install Codex only

macOS/Linux:

```bash
./scripts/codex/install.sh
```

Windows PowerShell:

```powershell
.\scripts\codex\install.ps1
```

Codex installation manages only `$CODEX_HOME` (normally `~/.codex`) and the clone's ignored `.runtime/codex` composition file when existing global instructions must be preserved.

See [`codex/INSTALL.md`](codex/INSTALL.md).

## Install Claude only

macOS/Linux:

```bash
./scripts/claude/install.sh
```

Windows PowerShell:

```powershell
.\scripts\claude\install.ps1
```

Claude installation manages only the configured Claude home (normally `~/.claude`). It installs:

- a symlinked `bulk-worker` using the `haiku` model alias;
- a supplementary rule;
- a symlinked local enforcement hook;
- non-destructively merged lifecycle hooks in `settings.json`;
- explicit subagent concurrency/depth defaults when those settings are not already present;
- experimental agent teams when not already configured, as an optional additional coordination capability.

Claude enforcement is not text-only. The hook classifies clear bulk/sharded requests, records actual worker starts/stops, denies parent mutation before required delegation, and blocks turn completion until delegation requirements are satisfied. For independent-subsystem work it requires evidence that at least two subagents actually overlapped in time.

See [`claude/INSTALL.md`](claude/INSTALL.md).

## Update

Pull the repository, then rerun only the installer for the agent you actually use:

```bash
git pull --ff-only
./scripts/codex/install.sh   # Codex only
# or
./scripts/claude/install.sh  # Claude only
```

For Codex composed-global mode, rerunning installation refreshes the composed file. Claude symlinked metadata/hooks pick up repository changes immediately, while rerunning its installer refreshes protocol-owned `settings.json` hook entries/settings.

## Uninstall independently

Codex only:

```bash
./scripts/codex/uninstall.sh
```

Claude only:

```bash
./scripts/claude/uninstall.sh
```

PowerShell equivalents are in the same per-agent directories. Neither uninstaller intentionally touches the other agent.

## Repository layout

```text
codex/
  AGENTS.md
  INSTALL.md
claude/
  INSTALL.md
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
  claude/
    install.sh
    uninstall.sh
    install.ps1
    uninstall.ps1
    manage-settings.py
```

## Enforcement boundary

Codex currently relies on agent instructions and the multi-agent runtime it exposes. Claude adds mechanical lifecycle hooks/settings on top of its supporting rule. Neither mechanism can override higher-priority system/developer/user instructions, managed organization policy, unavailable tools/models, or platform safety controls.
