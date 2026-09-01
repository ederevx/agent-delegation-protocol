# Codex v2 installation

Codex is installed independently from Claude. The wrapper
`scripts/codex/install.sh` (or `install.ps1`) invokes the host installer and
writes only protocol-owned state under `$CODEX_HOME` (normally `~/.codex`).

## Clean-break prerequisite

v2 is not an in-place upgrade. Before installation, preserve the prior branch
tips with annotated backup tags and keep their ancestry reachable. Install
from the rewritten v2 checkout only after its audit and verification pass.
Do not combine v1 and v2 runtime assets or state in one home.

## Install

```bash
bash scripts/codex/install.sh
```

```powershell
.\scripts\codex\install.ps1
```

Python 3.11 or newer is required. Set `CODEX_PYTHON` when automatic discovery
cannot find a suitable interpreter. Native Windows also requires symbolic-link
support through Developer Mode or an elevated shell.

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
$CODEX_HOME/agents/bulk_worker.toml
$CODEX_HOME/agents/balanced-worker.toml
$CODEX_HOME/hooks/delegation-enforcer.py
$CODEX_HOME/.delegation-protocol/delegationctl[.cmd]
$CODEX_HOME/.delegation-protocol/delegationctl.py
$CODEX_HOME/.delegation-protocol/lane_service.py
$CODEX_HOME/.delegation-protocol/protocol-v2.json
$CODEX_HOME/.delegation-protocol/delegation-classifier.py
$CODEX_HOME/.delegation-protocol/hook_adapter.py
$CODEX_HOME/.delegation-protocol/lifecycle.py
```

The bulk worker is a managed regular-file copy because the Codex runtime requires
no-follow loading for selected role files. The installer records its source
revision and refreshes only an unmodified protocol-owned copy.

On Windows, the controller launcher avoids a worker dependency on `python` or
`python3` being discoverable through `PATH`. It records the trusted interpreter
that ran the installer; workers invoke the absolute launcher under the active
Codex home. Reinstall refreshes an unmodified launcher, and uninstall removes
it through the ownership manifest.

## Lifecycle and trust

The Codex profile uses a lifecycle-visible worker. The scheduler accepts a
file-backed v2 request through `delegationctl run`, `batch`, or `resume`; it
authenticates the local loopback session and owns the provider lane. The
worker classifies stable receipts and never retries an accepted request under
another backend.

Codex requires user review and trust for non-managed hooks. After installation:

1. restart Codex;
2. run `/hooks`;
3. review the protocol handlers;
4. trust and enable them.

Existing configuration that disables user hooks or is organization-managed is
not silently overridden. The policy file remains supplemental to higher-level
instructions and permissions.

## Verify

Run the checks from the repository:

```bash
python3 scripts/agents/render-bulk-workers.py --check
python3 scripts/agents/test-protocol-v2.py
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
