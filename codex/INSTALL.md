# Codex installation

Codex is installed independently from Claude. The Codex installer touches only `$CODEX_HOME` (normally `~/.codex`) plus the clone's ignored `.runtime/codex` composition file when needed.

## Install

macOS/Linux:

```bash
./scripts/codex/install.sh
```

Windows PowerShell:

```powershell
.\scripts\codex\install.ps1
```

## Supported instruction location

Codex reads one global instruction file from `$CODEX_HOME`: `AGENTS.override.md` if present, otherwise `AGENTS.md`. An override at that level shadows the default; Codex does not concatenate both global files.

Because of that behavior, this repository uses two installation modes.

### No existing global Codex instructions

```text
$CODEX_HOME/AGENTS.md -> <clone>/codex/AGENTS.md
```

### Existing global Codex instructions

The installer preserves the currently active global instruction content verbatim, appends this repository's protocol, writes the result to an ignored runtime file in the clone, and activates it through:

```text
$CODEX_HOME/AGENTS.override.md -> <clone>/.runtime/codex/AGENTS.composed.md
```

If an `AGENTS.override.md` already existed, it is moved to a backup under `$CODEX_HOME/.delegation-protocol/` and restored by uninstall. If only `AGENTS.md` existed, it is left untouched and its content is placed first in the composed file.

This preserves the effective pre-existing instructions while satisfying Codex's one-global-file rule. Re-run the Codex installer after changing pre-existing global instructions so the composed file is refreshed.

## Mandatory behavior

`codex/AGENTS.md` explicitly authorizes subagent delegation, cheap-model routing, and concurrent multi-agent fan-out for independent subsystems. The parent remains responsible for boundaries, integration, conflict resolution, and final validation.

The policy requires the parent to choose only model overrides actually exposed by the current `spawn_agent` interface; it must not assume Luna, Terra, or any historical model is available in every Codex build/account.

## Verify

After installation, start a fresh Codex session and ask it to summarize its active delegation policy. For an eligible bulk task, confirm that it considers delegation before doing repetitive work in the parent context. For independent subsystem work, confirm it considers multiple concurrent child agents.

## Uninstall

macOS/Linux:

```bash
./scripts/codex/uninstall.sh
```

Windows PowerShell:

```powershell
.\scripts\codex\uninstall.ps1
```

This does not uninstall or modify Claude Code.

## Priority

This policy is supplementary. More-specific project `AGENTS.md` files and higher-priority direct instructions continue to apply. The installer never intentionally removes their content.
