# Pi installation

Pi is installed independently from Codex and Claude Code. The wrapper
`scripts/pi/install.sh` (or `install.ps1`) invokes the host installer and
writes only protocol-owned state under the Pi agent home (normally
`~/.pi/agent`; `PI_CODING_AGENT_DIR` overrides it).

## Clean-break prerequisite

This native-only protocol is not an in-place upgrade over the earlier
scheduler-based protocol. Preserve prior branch tips with annotated backup
tags and keep their ancestry reachable before installing. Install from the
rewritten checkout only after its audit and verification pass. Do not combine
runtime assets or state from the two generations in one home.

## Install

```bash
bash scripts/pi/install.sh
```

```powershell
.\scripts\pi\install.ps1
```

Python 3.11 or newer is required for the local bridge and protocol client.
Fresh installs use regular-file copies and do not require Windows
symbolic-link privileges. The installer validates the Pi agent home,
destination types, and protocol metadata before mutation. Existing agent
profiles, extensions, and unrelated files are preserved; conflicts stop
installation without partial activation. Pi's own `settings.json` is never
touched: Pi enforces through a discovered extension, not a settings file.

## Installed surface

The active agent home receives independent Pi policy, worker, bridge, and
enforcer managed regular-file copies:

```text
$PI_CODING_AGENT_DIR/rules/delegation-protocol.md
$PI_CODING_AGENT_DIR/agents/frontier-worker.md
$PI_CODING_AGENT_DIR/agents/balanced-worker.md
$PI_CODING_AGENT_DIR/agents/bulk-worker.md
$PI_CODING_AGENT_DIR/agents/quick-worker.md
$PI_CODING_AGENT_DIR/.delegation-protocol/delegation-enforcer.py
$PI_CODING_AGENT_DIR/extensions/adp-enforcer.ts
$PI_CODING_AGENT_DIR/extensions/subagent/index.ts
$PI_CODING_AGENT_DIR/extensions/subagent/agents.ts
$PI_CODING_AGENT_DIR/extensions/subagent/constants.ts
$PI_CODING_AGENT_DIR/extensions/subagent/detail-view.ts
$PI_CODING_AGENT_DIR/extensions/subagent/dispatch.ts
$PI_CODING_AGENT_DIR/extensions/subagent/format.ts
$PI_CODING_AGENT_DIR/extensions/subagent/registry.ts
$PI_CODING_AGENT_DIR/extensions/subagent/result-views.ts
$PI_CODING_AGENT_DIR/extensions/subagent/run.ts
$PI_CODING_AGENT_DIR/extensions/subagent/selector-view.ts
$PI_CODING_AGENT_DIR/extensions/subagent/types.ts
$PI_CODING_AGENT_DIR/.delegation-protocol/delegation-classifier.py
$PI_CODING_AGENT_DIR/.delegation-protocol/hook_adapter.py
```

The quick worker handles trivial, mechanical, single-step work through the
lowest tier. The bulk worker overlaps it for bounded low-risk work that needs
little interpretation. The balanced worker overlaps the bulk tier for
assignments where moderate reasoning is useful. The frontier worker overlaps
the balanced tier for bounded work needing near-parent reasoning, without
taking over parent architecture or integration. All four are ordinary native
Pi agents; the protocol observes their lifecycle, it does not launch or route
them. Spawning itself is Pi's native `subagent` tool from the official
subagent extension, which the Pi installer now deploys as a vendored copy
maintained in this repository; ADP provides no launcher and does not depend on
that extension for its own resources — when the tool is absent, required
delegation is reported blocked rather than done in the parent.

Each worker inherits the parent Pi host's tool access, including configured
MCP tools. The profiles apply no tier-specific tool allowlists or task scope
blocks. Pi permissions and other extensions still govern individual calls,
and the protocol retains ownership boundaries and strict downward worker
recursion.

The enforcer extension maps Pi events onto the shared adapter:
`before_agent_start` classifies the turn and appends the routing policy,
`tool_call` gates eligible mutations and spawns (delegation evidence, the
10-worker active cap, the one-shot owner authorization), worker sessions get
a hard per-lifetime tool-call budget keyed by their spawned identity, and
`turn_end` closes the turn's bookkeeping. Restart Pi after installing so
extension and agent discovery see the installed copies.

Shared-text limitation: the common `ROUTING_POLICY` embedded in every
worker profile and routing context mentions the Claude and Codex enforcement
mechanics by name. Pi's own runtime contract follows it in the same body;
the classifier text is deliberately left untouched to keep the other hosts'
rendered outputs byte-stable.

## Lifecycle

Before installing or upgrading, stop Pi sessions and workers using this
agent home; the extension only loads in processes started afterward.
Worker ledgers and state live under `.delegation-protocol/hook-state/` and
survive restarts. Uninstall removes only unchanged protocol-owned resources
and preserves unrelated configuration; run the read-only verifier to detect
drift between the deployed copies and this checkout:

```bash
python3 scripts/hosts/install.py verify --host pi --home "$HOME/.pi/agent" --repo .
```