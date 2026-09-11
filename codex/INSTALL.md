# Codex installation

Codex is installed independently from Claude. The wrapper
`scripts/codex/install.sh` (or `install.ps1`) invokes the host installer and
writes only protocol-owned state under `$CODEX_HOME` (normally `~/.codex`).

## Clean-break prerequisite

This native-only protocol is not an in-place upgrade over the earlier
scheduler-based protocol. Before installation, preserve the prior branch tips
with annotated backup tags and keep their ancestry reachable. Install from the
rewritten checkout only after its audit and verification pass. Do not combine
runtime assets or state from the two generations in one home.

## Install

```bash
bash scripts/codex/install.sh
```

```powershell
.\scripts\codex\install.ps1
```

Python 3.11 or newer is required. Set `CODEX_PYTHON` when automatic discovery
cannot find a suitable interpreter. Native Windows also requires symbolic-link
support through Developer Mode or an elevated PowerShell. If Windows denies
link creation, the installer stops transactionally and reports how to satisfy
that requirement before retrying.

The installer validates `$CODEX_HOME`, destination types, protocol metadata,
and hook configuration before mutation. Existing unrelated instructions,
settings, and hook handlers are preserved. A conflict stops installation
without partially enabling the protocol.

## Installed surface

When no global instruction file exists, the active home receives a direct
protocol-owned link at `$CODEX_HOME/AGENTS.md`. When `AGENTS.md` or
`AGENTS.override.md` already exists, the installer preserves the active content,
composes it before the protocol policy under installation state, and activates
that composition through a managed `AGENTS.override.md` link. Uninstall restores
the prior override, when one existed, and never replaces unrelated instructions.

The active home also receives the worker and protocol-owned links:

```text
$CODEX_HOME/agents/frontier_worker.toml
$CODEX_HOME/agents/balanced-worker.toml
$CODEX_HOME/agents/bulk_worker.toml
$CODEX_HOME/agents/quick_worker.toml
$CODEX_HOME/hooks/delegation-enforcer.py
$CODEX_HOME/.delegation-protocol/delegation-classifier.py
$CODEX_HOME/.delegation-protocol/hook_adapter.py
$CODEX_HOME/.delegation-protocol/lifecycle.py
```

The bulk worker is a managed regular-file copy because the Codex runtime requires
no-follow loading for selected role files. The installer records its source
revision and refreshes only an unmodified protocol-owned copy.

The installer also sets `agents.max_concurrent_threads_per_session = 10` in
`$CODEX_HOME/config.toml` so Codex's native per-session subagent concurrency
matches the protocol's shared active-worker cap. Only that one assignment is
written: every other line, table, and comment is preserved, a missing
`config.toml` or `[agents]` table is created, and a legacy `agents.max_threads`
alias is left untouched. A header spelled inside a multiline string is data, not
a table, and the parsed configuration is compared before and after the edit so
nothing outside that one key can change. The manifest records the prior value
and installation state keeps the pre-install bytes, so uninstall restores the
file exactly — removing the assignment, and a table or file the installer
created, only when nothing else remains there. A value changed by hand after
installation is left alone.

## Lifecycle and trust

The Codex profile uses a lifecycle-visible worker. The hook adapter observes
native `SubagentStart`/`SubagentStop` events for the session and gates eligible
parent mutation and turn completion on that evidence — there is no scheduler,
request file, or receipt to manage.

Codex requires user review and trust for non-managed hooks. After installation:

1. restart Codex;
2. run `/hooks`;
3. review the protocol handlers;
4. trust and enable them.

Existing configuration that disables user hooks or is organization-managed is
not silently overridden. The policy file remains supplemental to higher-level
instructions and permissions.

## Owner bypass

ADP enforcement can be lifted only per action, by explicit authorizing text
in the user's own prompt -- there is no standing bypass or marker file. See
"Owner bypass" in `codex/AGENTS.md` for the full rule.

## Verify

Run the checks from the repository:

```bash
python3 scripts/agents/render-bulk-workers.py --check
python3 scripts/hosts/test-install.py
python3 scripts/hosts/test-lifecycle.py
python3 scripts/codex/test-protocol.py
```

In a fresh session confirm that the worker is available, hooks are trusted,
and a clearly eligible task cannot mutate parent-owned files before delegation
evidence exists. Confirm that a worker report releases its host lifecycle
without requiring an unavailable post-result action.

## Uninstall

```bash
bash scripts/codex/uninstall.sh
```

```powershell
.\scripts\codex\uninstall.ps1
```

Uninstall removes only protocol-owned hooks, links, state, and an unmodified
managed worker copy. It restores preserved user configuration where recorded,
keeps unrelated files, and never modifies Claude.
