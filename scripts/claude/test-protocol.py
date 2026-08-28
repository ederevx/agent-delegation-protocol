#!/usr/bin/env python3
"""Self-test the Claude delegation hook and non-destructive settings manager."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
HOOK = REPO_ROOT / "claude" / "hooks" / "delegation-enforcer.py"
SETTINGS_MANAGER = REPO_ROOT / "scripts" / "claude" / "manage-settings.py"
MULTIPLEXER = REPO_ROOT / "scripts" / "agents" / "multiplexer.py"
WORKER_RENDERER = REPO_ROOT / "scripts" / "agents" / "render-bulk-workers.py"


def install_queue_fixture(home: Path, runtime: str, condition: str) -> None:
    installed = home / ".delegation-protocol"
    catalog = installed / "catalog"
    catalog.mkdir(parents=True, exist_ok=True)
    shutil.copy2(MULTIPLEXER, installed / "multiplexer.py")
    executable = sys.executable if condition != "unavailable" else "missing-queue-adapter-for-test"
    metadata: dict[str, Any] = {
        "schema_version": 1,
        "id": "test-queue",
        "name": "Test queue",
        "description": "Isolated single-stream queue fixture.",
        "native": False,
        "delegation_queue": True,
        "priority": 100,
        "provider": "test",
        "model": "test",
        "binding": {
            "argv": [executable, "-c", "pass"],
            "max_input_bytes": 1048576,
            "max_output_bytes": 2097152,
            "timeout_seconds": 30,
        },
        "capabilities": {
            "functions": ["audit", "edit", "batch"],
            "runtimes": [runtime],
            "platforms": ["linux", "darwin", "windows"],
            "modes": ["read", "edit"],
            "workspaces": ["shared", "isolated"],
            "deliveries": ["json-receipt"],
        },
        "limits": {"max_concurrency": 1},
    }
    if condition == "invalid":
        metadata["limits"] = {"max_concurrency": 2}
    (catalog / "test-queue.json").write_text(json.dumps(metadata), encoding="utf-8")
    members = ["missing-backend"] if condition == "misconfigured" else ["test-queue"]
    (installed / "multiplexer.json").write_text(json.dumps({
        "schema_version": 1, "routes": {"bulk": members},
    }), encoding="utf-8")


def install_round_robin_queue_fixture(home: Path, runtime: str, slots: int = 4) -> None:
    """Install a hook-level fixture that isolates selected metadata handling."""
    install_queue_fixture(home, runtime, "valid")
    installed = home / ".delegation-protocol"
    metadata_path = installed / "catalog" / "test-queue.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["queue_policy"] = {
        "strategy": "round_robin",
        "virtual_slots": slots,
        "quantum": {"unit": "agent_turn", "value": 4},
    }
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    (installed / "multiplexer.py").write_text(
        "import json\n"
        "def select_queue_backend(catalog, routes, route, runtime, platform=None):\n"
        "    return json.loads((catalog / 'test-queue.json').read_text(encoding='utf-8'))\n",
        encoding="utf-8",
    )


def run(cmd: list[str], *, env: dict[str, str] | None = None, stdin: dict[str, Any] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        input=(json.dumps(stdin) if stdin is not None else None),
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def test_generated_workers() -> None:
    result = run([sys.executable, str(WORKER_RENDERER), "--check"])
    require(result.returncode == 0, f"generated bulk workers are stale: {result.stderr}")
    instructions = (REPO_ROOT / "claude" / "agents" / "bulk-worker.md").read_text(
        encoding="utf-8"
    )
    for contract in (
        "For `native_required`", "whose `runtime` is `claude`",
        "When an external backend launches", "Never redo that task natively",
        "Redirect stdout to a receipt file under your scratchpad",
        "`timeout: 600000`",
        "`run_in_background: true`",
        "Read the receipt capture before reacting to a foreground timeout",
        "parse the receipt before interpreting a non-zero exit status",
        "Classify from the receipt",
        "report an execution failure only when no receipt was produced",
    ):
        require(contract in instructions,
                f"Claude dispatcher lacks required waiting contract: {contract}")
    for codex_only in ("`exec_command`", "`write_stdin`", "`yield_time_ms`"):
        require(codex_only not in instructions,
                f"Claude dispatcher incorrectly uses Codex mechanic: {codex_only}")
    common_contract = (REPO_ROOT / "agents" / "bulk-worker-common.md.tmpl").read_text(
        encoding="utf-8"
    )
    for provider_specific in ("DeepSeek", "CheapestInference", "deepseek-ci", "7200"):
        require(provider_specific not in common_contract,
                f"common dispatcher contract embeds provider-specific policy: {provider_specific}")


def call_hook(home: Path, mode: str, payload: dict[str, Any]) -> dict[str, Any] | None:
    env = dict(os.environ)
    env["CLAUDE_CONFIG_DIR"] = str(home)
    result = run([sys.executable, str(HOOK), mode], env=env, stdin=payload)
    require(result.returncode == 0, f"hook {mode} failed: {result.stderr}")
    text = result.stdout.strip()
    return json.loads(text) if text else None


def test_settings_merge(home: Path) -> None:
    settings_path = home / "settings.json"
    original = {
        "permissions": {"allow": ["Read"]},
        "env": {"EXISTING_VALUE": "keep-me", "CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS": "7"},
        "hooks": {
            "PreToolUse": [
                {
                    "matcher": "Bash",
                    "hooks": [
                        {
                            "type": "command",
                            "command": "echo existing",
                            "statusMessage": "Existing hook",
                        }
                    ],
                }
            ]
        },
    }
    home.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(json.dumps(original, indent=2) + "\n", encoding="utf-8")

    result = run(
        [
            sys.executable,
            str(SETTINGS_MANAGER),
            "install",
            "--claude-home",
            str(home),
            "--hook-path",
            str(HOOK),
            "--python",
            sys.executable,
        ]
    )
    require(result.returncode == 0, f"settings install failed: {result.stderr}")
    installed = json.loads(settings_path.read_text(encoding="utf-8"))
    require(installed["permissions"] == original["permissions"], "permissions were changed")
    require(installed["env"]["EXISTING_VALUE"] == "keep-me", "unrelated env was changed")
    require(installed["env"]["CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS"] == "7", "existing concurrency override was overwritten")
    require(installed["env"]["CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS"] == "1", "agent teams default was not added")
    require(installed["env"]["CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH"] == "3", "spawn depth default was not added")
    require(any(
        h.get("statusMessage") == "Existing hook"
        for group in installed["hooks"]["PreToolUse"]
        for h in group.get("hooks", [])
    ), "existing hook was removed")
    require(any(
        str(h.get("statusMessage", "")).startswith("Delegation protocol:")
        for groups in installed["hooks"].values()
        for group in groups
        for h in group.get("hooks", [])
    ), "protocol hooks were not installed")

    result = run(
        [
            sys.executable,
            str(SETTINGS_MANAGER),
            "uninstall",
            "--claude-home",
            str(home),
            "--hook-path",
            str(HOOK),
            "--python",
            sys.executable,
        ]
    )
    require(result.returncode == 0, f"settings uninstall failed: {result.stderr}")
    after = json.loads(settings_path.read_text(encoding="utf-8"))
    require(after["permissions"] == original["permissions"], "permissions changed after uninstall")
    require(after["env"]["EXISTING_VALUE"] == "keep-me", "unrelated env changed after uninstall")
    require(after["env"]["CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS"] == "7", "pre-existing concurrency override was removed")
    require("CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS" not in after["env"], "protocol-added agent teams value was not removed")
    require("CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH" not in after["env"], "protocol-added depth value was not removed")
    require(any(
        h.get("statusMessage") == "Existing hook"
        for group in after["hooks"]["PreToolUse"]
        for h in group.get("hooks", [])
    ), "existing hook was not preserved after uninstall")
    require(not any(
        str(h.get("statusMessage", "")).startswith("Delegation protocol:")
        for groups in after.get("hooks", {}).values()
        for group in groups
        for h in group.get("hooks", [])
    ), "protocol hooks remained after uninstall")


def test_single_agent_gate(home: Path) -> None:
    session = "single-agent-test"
    prompt = call_hook(home, "prompt", {
        "session_id": session,
        "prompt": "Update 10 files to apply this mechanical rename across the repository.",
    })
    require(prompt is not None and "HOOK CLASSIFICATION" in prompt["hookSpecificOutput"]["additionalContext"], "bulk prompt was not classified")

    denied = call_hook(home, "pretool", {
        "session_id": session,
        "tool_name": "Edit",
        "tool_input": {"file_path": "example.py"},
    })
    require(denied is not None and denied["hookSpecificOutput"]["permissionDecision"] == "deny", "parent mutation was not blocked before delegation")

    call_hook(home, "subagent-start", {
        "session_id": session,
        "agent_id": "worker-a",
        "agent_type": "bulk-worker",
    })
    allowed = call_hook(home, "pretool", {
        "session_id": session,
        "tool_name": "Edit",
        "tool_input": {"file_path": "example.py"},
    })
    require(allowed is None, "parent mutation stayed blocked after required delegation")


def test_multi_agent_overlap_gate(home: Path) -> None:
    session = "multi-agent-test"
    call_hook(home, "prompt", {
        "session_id": session,
        "prompt": "Implement independent frontend and backend subsystems, plus their separate tests.",
    })

    call_hook(home, "subagent-start", {
        "session_id": session,
        "agent_id": "frontend-worker",
        "agent_type": "bulk-worker",
    })
    denied = call_hook(home, "pretool", {
        "session_id": session,
        "tool_name": "Write",
        "tool_input": {"file_path": "integration.txt"},
    })
    require(denied is not None and denied["hookSpecificOutput"]["permissionDecision"] == "deny", "multi-agent task was not blocked after only one worker")

    call_hook(home, "subagent-start", {
        "session_id": session,
        "agent_id": "backend-worker",
        "agent_type": "bulk-worker",
    })
    allowed = call_hook(home, "pretool", {
        "session_id": session,
        "tool_name": "Write",
        "tool_input": {"file_path": "integration.txt"},
    })
    require(allowed is None, "multi-agent task did not unlock after overlapping workers were observed")

    call_hook(home, "subagent-stop", {"session_id": session, "agent_id": "frontend-worker"})
    call_hook(home, "subagent-stop", {"session_id": session, "agent_id": "backend-worker"})
    # Dismiss both workers before stop is allowed (PROTOCOL_VERSION 5 dismissal enforcement)
    call_hook(home, "pretool", {
        "session_id": session,
        "tool_name": "TaskStop",
        "tool_input": {"task_id": "frontend-worker"},
    })
    call_hook(home, "pretool", {
        "session_id": session,
        "tool_name": "TaskStop",
        "tool_input": {"task_id": "backend-worker"},
    })
    stopped = call_hook(home, "stop", {"session_id": session})
    require(stopped is None, "completed fan-out task was blocked from stopping")


def test_delegation_queue(home: Path) -> None:
    install_queue_fixture(home, "claude", "valid")
    session = "delegation-queue"
    prompt = call_hook(home, "prompt", {
        "session_id": session,
        "prompt": "Implement independent frontend and backend subsystems, plus their separate tests.",
    })
    context = prompt["hookSpecificOutput"]["additionalContext"]
    require("delegation queue selected backend `test-queue`" in context, "queue selection was not injected")
    state = json.loads((home / ".delegation-protocol" / "sessions" / f"{session}.json").read_text())
    require(state["requires_multi"] is True, "queue selection cleared the multi-workstream classification")
    require(state["delegation_queue"] is True and state["delegation_queue_backend"] == "test-queue", "queue state was not recorded")
    require(state["min_agents"] == 1, "queue selection did not reduce lifecycle-visible workers to one")
    call_hook(home, "subagent-start", {"session_id": session, "agent_id": "queue-dispatcher"})
    allowed = call_hook(home, "pretool", {"session_id": session, "tool_name": "Write", "tool_input": {"file_path": "integration.txt"}})
    require(allowed is None, "valid queue did not unlock after one dispatcher")


def test_round_robin_delegation_queue(home: Path) -> None:
    install_round_robin_queue_fixture(home, "claude", slots=4)
    session = "round-robin-delegation-queue"
    prompt = call_hook(home, "prompt", {
        "session_id": session,
        "prompt": "Implement independent frontend and backend subsystems, plus their separate tests.",
    })
    context = prompt["hookSpecificOutput"]["additionalContext"]
    require("round-robin delegation queue selected backend `test-queue`" in context,
            "round-robin queue selection was not injected")
    require("advertising 4 virtual slots" in context, "virtual slot count was not injected")
    require("multiplexer `run`" in context, "per-dispatcher run contract was not injected")
    state = json.loads((home / ".delegation-protocol" / "sessions" / f"{session}.json").read_text())
    require(state["delegation_queue_strategy"] == "round_robin", "round-robin strategy was not recorded")
    require(state["delegation_queue_virtual_slots"] == 4, "virtual slot count was not recorded")
    require(state["min_agents"] == 2, "round-robin queue did not preserve overlap enforcement")
    call_hook(home, "subagent-start", {"session_id": session, "agent_id": "virtual-a"})
    denied = call_hook(home, "pretool", {"session_id": session, "tool_name": "Write", "tool_input": {"file_path": "integration.txt"}})
    require(denied is not None, "round-robin queue unlocked after only one virtual dispatcher")
    call_hook(home, "subagent-start", {"session_id": session, "agent_id": "virtual-b"})
    allowed = call_hook(home, "pretool", {"session_id": session, "tool_name": "Write", "tool_input": {"file_path": "integration.txt"}})
    require(allowed is None, "round-robin queue did not unlock after overlapping dispatchers")


def test_queue_fallbacks(root: Path) -> None:
    for condition in ("invalid", "unavailable", "misconfigured"):
        home = root / condition
        install_queue_fixture(home, "claude", condition)
        session = f"queue-{condition}"
        prompt = call_hook(home, "prompt", {
            "session_id": session,
            "prompt": "Implement independent frontend and backend subsystems, plus their separate tests.",
        })
        require("delegation queue selected" not in prompt["hookSpecificOutput"]["additionalContext"], f"{condition} queue was selected")
        call_hook(home, "subagent-start", {"session_id": session, "agent_id": "only-worker"})
        denied = call_hook(home, "pretool", {"session_id": session, "tool_name": "Write", "tool_input": {"file_path": "integration.txt"}})
        require(denied is not None, f"{condition} queue bypassed overlap enforcement")


def test_explicit_opt_out(home: Path) -> None:
    session = "opt-out-test"
    call_hook(home, "prompt", {
        "session_id": session,
        "prompt": "Update 20 files, but do not delegate or spawn agents for this task.",
    })
    result = call_hook(home, "pretool", {
        "session_id": session,
        "tool_name": "Edit",
        "tool_input": {"file_path": "example.py"},
    })
    require(result is None, "explicit no-delegation instruction was not respected")


def test_pretool_matcher_covers_dismissal(home: Path) -> None:
    """The gate is unreachable unless the installed matcher routes Agent and TaskStop."""
    home.mkdir(parents=True, exist_ok=True)
    result = run([sys.executable, str(SETTINGS_MANAGER), "install", "--claude-home", str(home),
                  "--hook-path", str(HOOK), "--python", sys.executable])
    require(result.returncode == 0, f"settings install failed: {result.stderr}")
    settings = json.loads((home / "settings.json").read_text(encoding="utf-8"))
    matchers = [
        g.get("matcher", "")
        for g in settings.get("hooks", {}).get("PreToolUse", [])
        if any(str(h.get("statusMessage", "")).startswith("Delegation protocol:") for h in g.get("hooks", []))
    ]
    require(bool(matchers), "no protocol-owned PreToolUse hook was installed")
    joined = "|".join(matchers).split("|")
    for tool in ("Edit", "Write", "Bash", "Agent", "TaskStop"):
        require(tool in joined, f"PreToolUse matcher does not route {tool}")


def test_dismissal_lifecycle(home: Path) -> None:
    """Test worker dismissal enforcement across all lifecycle cases."""

    # Case 1: Worker finishes -> `stop` blocks
    session1 = "dismissal-case1"
    call_hook(home, "prompt", {
        "session_id": session1,
        "prompt": "Do some work",
    })
    call_hook(home, "subagent-start", {
        "session_id": session1,
        "agent_id": "w1",
    })
    call_hook(home, "subagent-stop", {
        "session_id": session1,
        "agent_id": "w1",
    })
    blocked = call_hook(home, "stop", {"session_id": session1})
    require(blocked is not None and blocked.get("decision") == "block", "stop did not block for outstanding worker")

    # Case 1a: it blocks at most once, so an undismissable worker cannot loop the hook
    again = call_hook(home, "stop", {"session_id": session1})
    require(again is None or again.get("decision") != "block", "stop blocked a second time on the same debt")

    # Case 1b: a runtime id (`a<name>-<hex>`) is cleared by TaskStop on the bare name
    session1b = "dismissal-case1b"
    call_hook(home, "prompt", {"session_id": session1b, "prompt": "Do some work"})
    call_hook(home, "subagent-start", {"session_id": session1b, "agent_id": "aworker-one-353c3b7231845f11"})
    call_hook(home, "subagent-stop", {"session_id": session1b, "agent_id": "aworker-one-353c3b7231845f11"})
    call_hook(home, "pretool", {
        "session_id": session1b,
        "tool_name": "TaskStop",
        "tool_input": {"task_id": "worker-one"},
    })
    require(call_hook(home, "stop", {"session_id": session1b}) is None, "bare-name TaskStop did not clear a runtime-id worker")

    # Case 2: With outstanding worker, `pretool` Agent spawn is denied
    session2 = "dismissal-case2"
    call_hook(home, "prompt", {
        "session_id": session2,
        "prompt": "Do some work",
    })
    call_hook(home, "subagent-start", {
        "session_id": session2,
        "agent_id": "w2",
    })
    call_hook(home, "subagent-stop", {
        "session_id": session2,
        "agent_id": "w2",
    })
    denied = call_hook(home, "pretool", {
        "session_id": session2,
        "tool_name": "Agent",
        "tool_input": {},
    })
    require(denied is not None and denied["hookSpecificOutput"]["permissionDecision"] == "deny", "Agent spawn was not denied with outstanding worker")

    # Case 3: `pretool` TaskStop with bare name clears it -> `stop` then passes
    session3 = "dismissal-case3"
    call_hook(home, "prompt", {
        "session_id": session3,
        "prompt": "Do some work",
    })
    call_hook(home, "subagent-start", {
        "session_id": session3,
        "agent_id": "w3",
    })
    call_hook(home, "subagent-stop", {
        "session_id": session3,
        "agent_id": "w3",
    })
    call_hook(home, "pretool", {
        "session_id": session3,
        "tool_name": "TaskStop",
        "tool_input": {"task_id": "w3"},
    })
    allowed = call_hook(home, "stop", {"session_id": session3})
    require(allowed is None, "stop was blocked after dismissing worker with bare name")

    # Case 4: `pretool` TaskStop with full "name@session" id also clears it
    session4 = "dismissal-case4"
    call_hook(home, "prompt", {
        "session_id": session4,
        "prompt": "Update 10 files",
    })
    call_hook(home, "subagent-start", {
        "session_id": session4,
        "agent_id": "w4@session-xyz",
    })
    call_hook(home, "subagent-stop", {
        "session_id": session4,
        "agent_id": "w4@session-xyz",
    })
    call_hook(home, "pretool", {
        "session_id": session4,
        "tool_name": "TaskStop",
        "tool_input": {"task_id": "w4@session-xyz"},
    })
    allowed = call_hook(home, "stop", {"session_id": session4})
    require(allowed is None, "stop was blocked after dismissing worker with full id")

    # Case 5: Worker that started but NOT stopped is not outstanding -> `stop` passes
    session5 = "dismissal-case5"
    call_hook(home, "prompt", {
        "session_id": session5,
        "prompt": "Update 10 files",
    })
    call_hook(home, "subagent-start", {
        "session_id": session5,
        "agent_id": "w5",
    })
    # Note: NOT calling subagent-stop
    allowed = call_hook(home, "stop", {"session_id": session5})
    require(allowed is None, "stop was blocked for active (not finished) worker")

    # Case 6: New `prompt` clears stale outstanding workers
    session6 = "dismissal-case6"
    call_hook(home, "prompt", {
        "session_id": session6,
        "prompt": "Update 10 files",
    })
    call_hook(home, "subagent-start", {
        "session_id": session6,
        "agent_id": "w6",
    })
    call_hook(home, "subagent-stop", {
        "session_id": session6,
        "agent_id": "w6",
    })
    # New prompt should reset evidence
    call_hook(home, "prompt", {
        "session_id": session6,
        "prompt": "Different task now",
    })
    allowed = call_hook(home, "stop", {"session_id": session6})
    require(allowed is None, "stop was blocked after new prompt reset evidence")

    # Case 7: `pretool` event with non-empty agent_id (subagent's own call) is ignored
    session7 = "dismissal-case7"
    call_hook(home, "prompt", {
        "session_id": session7,
        "prompt": "Update 10 files",
    })
    call_hook(home, "subagent-start", {
        "session_id": session7,
        "agent_id": "w7",
    })
    call_hook(home, "subagent-stop", {
        "session_id": session7,
        "agent_id": "w7",
    })
    # Event from a subagent (non-empty agent_id) should be ignored
    ignored = call_hook(home, "pretool", {
        "session_id": session7,
        "agent_id": "w7",
        "tool_name": "Agent",
        "tool_input": {},
    })
    require(ignored is None, "subagent pretool event was not ignored (should not emit deny)")


def test_unlaunched_agent_creates_no_debt(home: Path) -> None:
    """A SubagentStop for an agent the protocol never started must not accrue a debt.

    The runtime stops agents of its own under nameless ids that no TaskStop can
    target. Charging the parent for those escalated a fresh, unpayable worker
    every turn and nagged the Stop hook forever.
    """
    session = "phantom-debt"
    call_hook(home, "prompt", {"session_id": session, "prompt": "Do some work"})
    for index in range(4):
        call_hook(home, "subagent-stop", {"session_id": session, "agent_id": f"a{index:016x}"})
        out = call_hook(home, "stop", {"session_id": session})
        require(out is None, f"unlaunched agent #{index} created a dismissal debt: {out}")

    # A worker the protocol did launch is still tracked normally.
    call_hook(home, "subagent-start", {"session_id": session, "agent_id": "real-worker"})
    call_hook(home, "subagent-stop", {"session_id": session, "agent_id": "real-worker"})
    blocked = call_hook(home, "stop", {"session_id": session})
    require(
        blocked is not None and blocked.get("decision") == "block",
        "a genuinely launched worker no longer creates a dismissal debt",
    )


def test_outstanding_debt_reported_once(home: Path) -> None:
    """An undismissable debt is surfaced once, not re-reported on every Stop."""
    session = "debt-reported-once"
    call_hook(home, "prompt", {"session_id": session, "prompt": "Do some work"})
    call_hook(home, "subagent-start", {"session_id": session, "agent_id": "w1"})
    call_hook(home, "subagent-stop", {"session_id": session, "agent_id": "w1"})

    first = call_hook(home, "stop", {"session_id": session})
    require(first is not None and first.get("decision") == "block", "first stop did not block")
    for attempt in range(3):
        extra = call_hook(home, "stop", {"session_id": session})
        require(extra is None, f"stop re-reported an already-reported debt (attempt {attempt}): {extra}")

    # The debt itself still stands: a new spawn is denied until it is dismissed.
    denied = call_hook(
        home, "pretool", {"session_id": session, "tool_name": "Agent", "tool_input": {}}
    )
    require(
        denied is not None and denied["hookSpecificOutput"]["permissionDecision"] == "deny",
        "reporting a debt once must not release the spawn gate",
    )


def test_relayed_message_continues_turn(home: Path) -> None:
    """A worker's report arrives as a prompt; it must not reopen the turn."""
    session = "relayed-message-test"
    call_hook(home, "prompt", {
        "session_id": session,
        "prompt": "Implement independent frontend and backend subsystems, plus their separate tests.",
    })
    for agent in ("frontend-worker", "backend-worker"):
        call_hook(home, "subagent-start", {
            "session_id": session,
            "agent_id": agent,
            "agent_type": "bulk-worker",
        })
    allowed = call_hook(home, "pretool", {
        "session_id": session,
        "tool_name": "Write",
        "tool_input": {"file_path": "integration.txt"},
    })
    require(allowed is None, "fan-out did not unlock parent mutation")

    # The relayed report itself carries shard wording. Re-classifying it would judge the
    # worker's words as the user's, and resetting evidence would revoke the fan-out above.
    call_hook(home, "prompt", {
        "session_id": session,
        "prompt": (
            '<agent-message from="frontend-worker">\n'
            "Finished the independent frontend and backend modules and their separate tests.\n"
            "</agent-message>"
        ),
    })
    still_allowed = call_hook(home, "pretool", {
        "session_id": session,
        "tool_name": "Write",
        "tool_input": {"file_path": "integration.txt"},
    })
    require(still_allowed is None, "a relayed worker report revoked fan-out evidence and re-blocked integration")

    # A genuine user turn must still reclassify and gate normally.
    call_hook(home, "prompt", {
        "session_id": session,
        "prompt": "Now refactor independent frontend and backend subsystems, plus their separate tests.",
    })
    denied = call_hook(home, "pretool", {
        "session_id": session,
        "tool_name": "Write",
        "tool_input": {"file_path": "integration.txt"},
    })
    require(
        denied is not None and denied["hookSpecificOutput"]["permissionDecision"] == "deny",
        "a real user turn failed to reset delegation evidence",
    )


def main() -> int:
    require(HOOK.exists(), f"missing hook: {HOOK}")
    require(SETTINGS_MANAGER.exists(), f"missing settings manager: {SETTINGS_MANAGER}")
    require(WORKER_RENDERER.exists(), f"missing worker renderer: {WORKER_RENDERER}")
    test_generated_workers()
    with tempfile.TemporaryDirectory(prefix="delegation-protocol-test-") as tmp:
        root = Path(tmp)
        test_settings_merge(root / "settings-home")
        test_single_agent_gate(root / "single-home")
        test_multi_agent_overlap_gate(root / "multi-home")
        test_delegation_queue(root / "queue-home")
        test_round_robin_delegation_queue(root / "round-robin-queue-home")
        test_queue_fallbacks(root / "queue-fallbacks")
        test_explicit_opt_out(root / "opt-out-home")
        test_pretool_matcher_covers_dismissal(root / "matcher-home")
        test_dismissal_lifecycle(root / "dismissal-home")
        test_unlaunched_agent_creates_no_debt(root / "phantom-home")
        test_outstanding_debt_reported_once(root / "once-home")
        test_relayed_message_continues_turn(root / "relayed-home")
    print("Claude delegation protocol self-test: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
