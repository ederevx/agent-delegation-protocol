# Codex installation

## Supported instruction location

Codex reads one global instruction file from `$CODEX_HOME` (normally `~/.codex`): `AGENTS.override.md` if present, otherwise `AGENTS.md`. An override at that level shadows the default; Codex does not concatenate both global files.

Because of that behavior, a new global protocol cannot always be added as a second independent global file. This repository therefore uses two modes:

### No existing global Codex instructions

The installer creates:

```text
$CODEX_HOME/AGENTS.md -> <clone>/codex/AGENTS.md
```

### Existing global Codex instructions

The installer preserves the currently active global instruction content verbatim, appends this repository's protocol, writes the result to an ignored runtime file in the clone, and activates it through:

```text
$CODEX_HOME/AGENTS.override.md -> <clone>/.runtime/codex/AGENTS.composed.md
```

If an `AGENTS.override.md` already existed, it is moved to a backup under `$CODEX_HOME/.delegation-protocol/` and restored by uninstall. If only `AGENTS.md` existed, it is left untouched and its content is placed first in the composed file.

This is deliberate: it preserves the effective pre-existing instructions while satisfying Codex's one-global-file rule. Re-run the installer after changing those pre-existing global instructions so the composed file is refreshed.

## Mandatory behavior

`codex/AGENTS.md` explicitly authorizes subagent delegation. This matters because Codex's multi-agent tooling otherwise instructs the model not to spawn subagents unless the user or applicable agent metadata authorizes delegation/parallel work.

The policy also requires the parent to choose only model overrides actually exposed by `spawn_agent`; it must not assume that Luna, Terra, or any historical model is available in every Codex build/account.

## Verify

After installation, start a fresh Codex session and ask it to summarize its active delegation policy. For an eligible bulk task, confirm that it considers subagent delegation before doing all repetitive work in the parent context.

## Priority

This policy is supplementary. More-specific project `AGENTS.md` files and higher-priority direct instructions continue to apply. The installer never intentionally removes their content.
