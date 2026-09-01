# Claude Code v2 installation

Claude Code is installed independently from Codex. The wrapper
`scripts/claude/install.sh` (or `install.ps1`) invokes the host installer and
writes only protocol-owned state under the Claude home (normally `~/.claude`).

## Clean-break prerequisite

v2 is not an in-place upgrade. Preserve prior branch tips with annotated
backup tags and keep their ancestry reachable before installing. Install from
the rewritten v2 checkout only after its audit and verification pass. Do not
combine v1 and v2 runtime assets or state in one home.

## Install

```bash
bash scripts/claude/install.sh
```

```powershell
.\scripts\claude\install.ps1
```

Python 3.11 or newer is required for the local hook and protocol client. The
installer validates the Claude home, destination types, protocol metadata, and
settings before mutation. Existing settings, rules, and unrelated handlers are
preserved; conflicts stop installation without partial activation.

## Installed surface

The active home receives independent Claude policy, worker, hook, and client
links:

```text
$CLAUDE_CONFIG_DIR/rules/delegation-protocol.md
$CLAUDE_CONFIG_DIR/agents/bulk-worker.md
$CLAUDE_CONFIG_DIR/agents/balanced-worker.md
$CLAUDE_CONFIG_DIR/hooks/delegation-enforcer.py
$CLAUDE_CONFIG_DIR/.delegation-protocol/delegationctl[.cmd]
$CLAUDE_CONFIG_DIR/.delegation-protocol/delegationctl.py
$CLAUDE_CONFIG_DIR/.delegation-protocol/lane_service.py
$CLAUDE_CONFIG_DIR/.delegation-protocol/protocol-v2.json
$CLAUDE_CONFIG_DIR/.delegation-protocol/delegation-classifier.py
$CLAUDE_CONFIG_DIR/.delegation-protocol/hook_adapter.py
$CLAUDE_CONFIG_DIR/.delegation-protocol/lifecycle.py
```

The bulk worker is restricted to read-only large-text compression through the
managed cheap tier. The balanced worker handles bounded work that needs
moderate reasoning without taking over parent architecture or integration.

The worker sends file-backed v2 requests through the absolute platform
launcher under the active Claude config directory. On Windows, the launcher
records the trusted interpreter used for installation, so execution does not
depend on `python` or `python3` being present on `PATH`. The scheduler
authenticates the local loopback session, owns the provider lane, and returns
stable structured receipts. An accepted request is never silently retried
under another backend.

## Settings and lifecycle

Protocol-owned settings are merged into `settings.json` without replacing
unrelated values. Existing environment overrides are retained; explicit
disablement or organization-managed policy is reported rather than silently
overridden.

Claude's lifecycle profile observes worker start and completion events and
gates eligible parent mutation and turn completion on delegation evidence.
Foreground Agent results automatically release the worker lifecycle. A
completed foreground worker does not require a further stop action; a stop
action is reserved for a running background task that needs cancellation.

## Verify

```bash
python3 scripts/agents/render-bulk-workers.py --check
python3 scripts/agents/test-protocol-v2.py
python3 scripts/hosts/test-install.py
python3 scripts/hosts/test-lifecycle.py
python3 scripts/claude/test-protocol.py
```

In a fresh session confirm that the worker is visible, settings contain the
protocol handlers beside existing handlers, and a clearly eligible task cannot
mutate parent-owned files before delegation evidence exists. Confirm that a
foreground worker result permits another worker wave without lifecycle debt.

## Uninstall

```bash
bash scripts/claude/uninstall.sh
```

```powershell
.\scripts\claude\uninstall.ps1
```

Uninstall removes only protocol-owned handlers, links, state, and settings
values that remain unchanged since installation. It preserves unrelated
configuration and never modifies Codex.
