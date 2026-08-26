# Claude Code installation

## User-level supplementary rule

Claude Code loads personal rules from `~/.claude/rules/` (or the configured Claude directory), and the rules directory supports symlinks. The installer therefore adds this protocol without touching an existing user `CLAUDE.md`:

```text
~/.claude/rules/delegation-protocol.md
  -> <clone>/claude/rules/delegation-protocol.md
```

## Bulk subagent

Claude Code recursively scans user subagents from `~/.claude/agents/`. The installer adds:

```text
~/.claude/agents/bulk-worker.md
  -> <clone>/claude/agents/bulk-worker.md
```

The worker uses the `haiku` model alias. Claude Code may substitute or inherit another model if organization/model policy does not permit the requested family. The parent must not assume a specific version is always available.

Claude can choose a model per subagent invocation, and current Claude Code also supports nested subagents subject to configured depth/concurrency limits. The protocol permits nesting only when it materially helps a decomposable task.

## Existing instructions

Do not replace `~/.claude/CLAUDE.md`, project `CLAUDE.md`, `CLAUDE.local.md`, or existing rules. Claude concatenates applicable instruction sources rather than treating this user rule as a replacement. Project-level rules can be more specific and therefore may take precedence in practice.

If the destination filename already exists and is not this repository's symlink, the installer stops instead of overwriting it.

## Verify

Start a fresh Claude Code session and run `/context`; confirm the delegation rule appears among loaded memory/rule sources. Confirm the `bulk-worker` subagent is visible, then give Claude a clearly repetitive multi-unit task and verify it considers delegation.

## Enforcement boundary

CLAUDE/rule content guides model behavior; it is not a mechanical security boundary. Use Claude Code permissions, settings, sandboxing, or hooks for actions that must be technically blocked or guaranteed.
