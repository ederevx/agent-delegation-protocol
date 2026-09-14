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
cannot find a suitable interpreter. Fresh installs use regular-file copies and
do not require Windows symbolic-link privileges. If a legacy protocol symlink
is present, installation migrates it transactionally; restoring the link after
a failed upgrade may require symbolic-link support.

The installer validates `$CODEX_HOME`, destination types, protocol metadata,
and hook configuration before mutation. Existing unrelated instructions,
settings, and hook handlers are preserved. A conflict stops installation
without partially enabling the protocol.

## Installed surface

When no global instruction file exists, the active home receives a direct
protocol-owned copy at `$CODEX_HOME/AGENTS.md`. When `AGENTS.md` or
`AGENTS.override.md` already exists, the installer preserves the active content,
composes it before the protocol policy under installation state, and activates
that composition through a managed `AGENTS.override.md` copy. Uninstall restores
the prior override, when one existed, and never replaces unrelated instructions.

The active home also receives managed regular-file copies of the worker and
protocol-owned resources:

```text
$CODEX_HOME/agents/frontier_worker.toml
$CODEX_HOME/agents/balanced-worker.toml
$CODEX_HOME/agents/bulk_worker.toml
$CODEX_HOME/agents/quick_worker.toml
$CODEX_HOME/hooks/delegation-enforcer.py
$CODEX_HOME/.delegation-protocol/delegation-classifier.py
$CODEX_HOME/.delegation-protocol/hook_adapter.py
```

The installer records source hashes for every managed copy and refreshes only
an unmodified protocol-owned copy. Installed code runs independently of the
source checkout; reinstall after changing the checkout to adopt those changes.

Each worker inherits the parent Codex host's tool access, including configured
MCP tools. The profiles apply no tier-specific tool allowlists or task scope
blocks. Codex permissions and hooks still govern individual calls, and the
protocol retains ownership boundaries and strict downward worker recursion.

The installer sets `agents.max_concurrent_threads_per_session = 10` in
`$CODEX_HOME/config.toml`. This native open-thread cap is separate from the
hook's 10-worker active cap. V2 limits executing agents and resident child
threads separately and can evict completed idle residents. The parent also
closes completed subtrees it will not resume. Existing
sessions retain their startup capacity, so start a new Codex session after an
installation or upgrade to use the new value. Only that one assignment is
written: every other line, table, and comment is preserved, a missing
`config.toml` or `[agents]` table is created, and a legacy
`agents.max_threads` alias is left untouched. A header spelled inside a multiline string is data, not
a table, and the parsed configuration is compared before and after the edit so
nothing outside that one key can change. The manifest records the prior value
and installation state keeps the pre-install bytes, so uninstall restores the
file exactly — removing the assignment, and a table or file the installer
created, only when nothing else remains there. A value changed by hand after
installation is left alone.

## Lifecycle and trust

For the upgrade from directory locks to OS advisory locks, stop all Codex
sessions and workers using this home and let their hook processes exit before
installing. Start fresh sessions afterward: old and new lock formats cannot
run together safely. Existing ledgers and legacy lock directories are retained;
identities with a legacy lock remain blocked pending separate state recovery.

The Codex profile uses a lifecycle-visible worker. The hook adapter observes
native `SubagentStart`/`SubagentStop` events for the session and gates eligible
parent mutation on delegation evidence. `Stop` expires unused authorization
and marks the turn complete; it does not gate turn completion. There is no
scheduler, request file, or receipt to manage.

After collecting and validating a completed worker subtree's reports, promptly
close it if it will not be resumed. Prefer a direct native close operation;
the verified Codex 0.154.0 V2 app-server route is
`mcp__codex_tui__set_thread_archived({archived:true, threadId:<exact owned
child UUID>})`. Verify every descendant is complete before cascading archive,
obtain IDs from native metadata or a read-only parent-child mapping, and then
confirm the subtree is absent from `list_agents` and unloaded by `read_thread`.
Completion frees the hook's active-worker slot and does not close the host
thread. V2 can report a
native limit error while pruning residency entries left by archival; refresh
live status and retry once, then report any repeated failure.
V1 requires its native `close_agent`; archival does not release its counted
spawn slot. No model, model catalog, or interface switch is installed here.
If closure is unavailable or fails, report the concrete blocker rather than
changing capacity or using files, SQLite, ledger edits, or process termination
as a substitute.

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
bookkeeping, then close a completed subtree through the native route and
confirm that it disappears from `list_agents` and is unloaded by `read_thread`.

## Uninstall

```bash
bash scripts/codex/uninstall.sh
```

```powershell
.\scripts\codex\uninstall.ps1
```

Uninstall removes only unchanged protocol-owned handlers, copies, and state.
When retiring an older install, `lifecycle.py` is removed transactionally only
when it is an unchanged protocol-owned copy; altered or foreign files are
preserved. Legacy manifest release metadata is retained but ignored by the
runtime.
It restores preserved user configuration where recorded, keeps unrelated files,
and never modifies Claude.
