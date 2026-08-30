#!/usr/bin/env python3
"""Self-test the Claude delegation hook and non-destructive settings manager."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
HOOK = REPO_ROOT / "claude" / "hooks" / "delegation-enforcer.py"
SETTINGS_MANAGER = REPO_ROOT / "scripts" / "claude" / "manage-settings.py"
MUX_SCHEDULER = REPO_ROOT / "scripts" / "agents" / "mux-scheduler.py"
WORKER_RENDERER = REPO_ROOT / "scripts" / "agents" / "render-bulk-workers.py"


def install_queue_fixture(home: Path, runtime: str, condition: str) -> None:
    installed = home / ".delegation-protocol"
    catalog = installed / "catalog"
    catalog.mkdir(parents=True, exist_ok=True)
    shutil.copy2(MUX_SCHEDULER, installed / "mux-scheduler.py")
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
    (installed / "mux-scheduler.json").write_text(json.dumps({
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
    (installed / "mux-scheduler.py").write_text(
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
        "task JSON file under your scratchpad", "`Write`", "--task-file",
        "validate that it is non-empty JSON", "Do not interpolate the task through the shell",
        "queue envelope with exactly `tasks`", "even one task remains wrapped in `tasks`",
        "task fields never appear at the envelope's top level",
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
    require("Send the JSON on stdin" not in common_contract,
            "common dispatcher contract requires an unavailable stdin transport")


def call_hook(home: Path, mode: str, payload: dict[str, Any],
              *, extra_env: dict[str, str] | None = None) -> dict[str, Any] | None:
    env = dict(os.environ)
    env["CLAUDE_CONFIG_DIR"] = str(home)
    env.update(extra_env or {})
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


def test_installer_migrates_mux_scheduler_links(home: Path) -> None:
    protocol = home / ".delegation-protocol"
    protocol.mkdir(parents=True)
    legacy_mux = protocol / "multiplexer.py"
    legacy_routes = protocol / "multiplexer.json"
    legacy_mux.symlink_to(REPO_ROOT / "scripts" / "agents" / "multiplexer.py")
    legacy_routes.symlink_to(REPO_ROOT / "agents" / "multiplexer.json")
    env = dict(os.environ)
    env["CLAUDE_CONFIG_DIR"] = str(home)

    result = run(["bash", str(REPO_ROOT / "scripts" / "claude" / "install.sh")], env=env)
    require(result.returncode == 0, f"Claude installer failed: {result.stderr}")
    require(not legacy_mux.is_symlink() and not legacy_routes.is_symlink(),
            "Claude installer retained legacy multiplexer links")
    require((protocol / "mux-scheduler.py").is_symlink(),
            "Claude mux-scheduler executable link was not installed")
    require((protocol / "mux-scheduler.json").is_symlink(),
            "Claude mux-scheduler route link was not installed")
    require((protocol / "delegation-classifier.py").is_symlink(),
            "Claude shared classifier link was not installed")

    result = run(["bash", str(REPO_ROOT / "scripts" / "claude" / "uninstall.sh")], env=env)
    require(result.returncode == 0, f"Claude uninstaller failed: {result.stderr}")
    require(not (protocol / "mux-scheduler.py").is_symlink(),
            "Claude uninstaller retained mux-scheduler executable")
    require(not (protocol / "delegation-classifier.py").is_symlink(),
            "Claude uninstaller retained shared classifier link")


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
    require("mux-scheduler `queue`" in context, "singular queue contract was not injected")
    require("singular scheduler process" in context, "singular scheduler ownership was not injected")
    state = json.loads((home / ".delegation-protocol" / "sessions" / f"{session}.json").read_text())
    require(state["delegation_queue_strategy"] == "round_robin", "round-robin strategy was not recorded")
    require(state["delegation_queue_virtual_slots"] == 4, "virtual slot count was not recorded")
    require(state["min_agents"] == 1, "round-robin queue did not select one lifecycle dispatcher")
    call_hook(home, "subagent-start", {"session_id": session, "agent_id": "virtual-a"})
    allowed = call_hook(home, "pretool", {"session_id": session, "tool_name": "Write", "tool_input": {"file_path": "integration.txt"}})
    require(allowed is None, "round-robin queue did not unlock after one dispatcher")


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


def context_of(result: dict[str, Any] | None) -> str:
    require(result is not None, "hook returned no prompt context")
    return str(result["hookSpecificOutput"]["additionalContext"])


def test_classifier_mapping_smoke(home: Path) -> None:
    """Claude maps one shared-classifier decision into its mutation gate."""
    context = context_of(call_hook(home, "prompt", {
        "session_id": "size-test",
        "prompt": "Port this parser; expect about 80k tokens of work.",
    }))
    require("HOOK CLASSIFICATION" in context,
            "shared classification was not injected into Claude")
    require("SIZE AND SHAPE THRESHOLDS" in context,
            "Claude omitted standing shared thresholds")
    denied = call_hook(home, "pretool", {
        "session_id": "size-test", "tool_name": "Edit",
        "tool_input": {"file_path": "a.py"},
    })
    require(denied is not None, "Claude did not gate a classified mutation")


def test_ci_context_env_is_ignored(home: Path) -> None:
    """Main derives thresholds only from Claude Code's context setting."""
    context = context_of(call_hook(
        home,
        "prompt",
        {
            "session_id": "ci-context-env",
            "prompt": "Port this parser; expect about 40k tokens of work.",
        },
        extra_env={
            "CLAUDE_CODE_MAX_CONTEXT_TOKENS": "",
            "CI_CLAUDE_CONTEXT_WINDOW": "100000",
        },
    ))
    require("50000+ tokens" in context,
            "CI-only context setting changed main's size threshold")
    require("HOOK CLASSIFICATION" not in context,
            "CI-only context setting made a sub-threshold task eligible")


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


def test_pretool_matcher_covers_only_parent_mutation(home: Path) -> None:
    """The delegation gate must not intercept lifecycle-owned tools."""
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
    for tool in ("Edit", "Write", "Bash"):
        require(tool in joined, f"PreToolUse matcher does not route {tool}")
    require("Agent" not in joined, "protocol still intercepts lifecycle-owned Agent")
    require("TaskStop" not in joined, "protocol still intercepts runtime-owned TaskStop")


def test_foreground_lifecycle(home: Path) -> None:
    """Completed foreground agents create no synthetic dismissal debt."""

    # A foreground worker is released by Claude Code when its Agent result returns.
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
    require(blocked is None, "completed foreground worker created stop debt")

    # Repeated Stop events remain allowed.
    again = call_hook(home, "stop", {"session_id": session1})
    require(again is None or again.get("decision") != "block", "stop blocked a second time on the same debt")

    # A stray TaskStop hook event is irrelevant to a completed foreground Agent.
    session1b = "dismissal-case1b"
    call_hook(home, "prompt", {"session_id": session1b, "prompt": "Do some work"})
    call_hook(home, "subagent-start", {"session_id": session1b, "agent_id": "aworker-one-353c3b7231845f11"})
    call_hook(home, "subagent-stop", {"session_id": session1b, "agent_id": "aworker-one-353c3b7231845f11"})
    call_hook(home, "pretool", {
        "session_id": session1b,
        "tool_name": "TaskStop",
        "tool_input": {"task_id": "worker-one"},
    })
    require(call_hook(home, "stop", {"session_id": session1b}) is None, "foreground runtime id created debt")

    # A completed foreground worker must not prevent another wave of agents.
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
    require(denied is None, "completed foreground worker blocked a later Agent spawn")

    # TaskStop is not required for either bare or runtime-shaped Agent ids.
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
    require(allowed is None, "bare Agent id created lifecycle debt")

    # A session-shaped Agent id is equally released at foreground completion.
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
    require(allowed is None, "session-shaped Agent id created lifecycle debt")

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
    """All SubagentStop events only update overlap; none accrue lifecycle debt."""
    session = "phantom-debt"
    call_hook(home, "prompt", {"session_id": session, "prompt": "Do some work"})
    for index in range(4):
        call_hook(home, "subagent-stop", {"session_id": session, "agent_id": f"a{index:016x}"})
        out = call_hook(home, "stop", {"session_id": session})
        require(out is None, f"unlaunched agent #{index} created a dismissal debt: {out}")

    # A worker the protocol did launch is equally runtime-owned after completion.
    call_hook(home, "subagent-start", {"session_id": session, "agent_id": "real-worker"})
    call_hook(home, "subagent-stop", {"session_id": session, "agent_id": "real-worker"})
    blocked = call_hook(home, "stop", {"session_id": session})
    require(blocked is None, "a launched foreground worker created dismissal debt")


def test_completed_worker_allows_later_wave(home: Path) -> None:
    """A completed worker neither blocks stop nor later agent waves."""
    session = "debt-reported-once"
    call_hook(home, "prompt", {"session_id": session, "prompt": "Do some work"})
    call_hook(home, "subagent-start", {"session_id": session, "agent_id": "w1"})
    call_hook(home, "subagent-stop", {"session_id": session, "agent_id": "w1"})

    first = call_hook(home, "stop", {"session_id": session})
    require(first is None, "completed worker blocked stop")
    for attempt in range(3):
        extra = call_hook(home, "stop", {"session_id": session})
        require(extra is None, f"stop re-reported an already-reported debt (attempt {attempt}): {extra}")

    # A later foreground wave remains available.
    denied = call_hook(
        home, "pretool", {"session_id": session, "tool_name": "Agent", "tool_input": {}}
    )
    require(denied is None, "completed worker blocked a later Agent wave")


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


def test_classifier_loader_falls_back_to_clone(home: Path) -> None:
    """With no installed classifier, the hook must fall back to the repo clone.

    Nothing in this fixture ever creates
    `.delegation-protocol/delegation-classifier.py`, so a working classification
    here can only have come from load_classifier()'s second candidate: the clone
    path resolved from the hook's own (symlink-followed) location.
    """
    session = "loader-fallback-test"
    context = context_of(call_hook(home, "prompt", {
        "session_id": session,
        "prompt": "Update 10 files to apply this mechanical rename across the repository.",
    }))
    require("HOOK CLASSIFICATION" in context, "classifier fallback to the repo clone did not work")


def test_state_version_mismatch_is_discarded(home: Path) -> None:
    """State stamped with a stale protocol_version must be treated as absent.

    This is the fix for the audit finding that PROTOCOL_VERSION was write-only:
    a state file whose version disagrees with the running classifier can no
    longer keep an old requires_delegation gate in force.
    """
    session = "stale-version-test"
    sessions_dir = home / ".delegation-protocol" / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    (sessions_dir / f"{session}.json").write_text(json.dumps({
        "protocol_version": -1,
        "requires_delegation": True,
        "min_agents": 1,
        "completed": False,
    }), encoding="utf-8")
    allowed = call_hook(home, "pretool", {
        "session_id": session,
        "tool_name": "Edit",
        "tool_input": {"file_path": "example.py"},
    })
    require(allowed is None, "stale-versioned state was honored instead of discarded")


def test_reap_removes_stale_sessions(home: Path) -> None:
    """An old session's state is swept while the session in progress is kept."""
    stale_session = "reap-stale-session"
    current_session = "reap-current-session"
    call_hook(home, "prompt", {"session_id": stale_session, "prompt": "Do some work"})
    call_hook(home, "prompt", {"session_id": current_session, "prompt": "Do some other work"})

    sessions_dir = home / ".delegation-protocol" / "sessions"
    stale_path = sessions_dir / f"{stale_session}.json"
    current_path = sessions_dir / f"{current_session}.json"
    require(stale_path.exists() and current_path.exists(), "fixture state was not written")

    old_time = time.time() - (8 * 24 * 3600)
    os.utime(stale_path, (old_time, old_time))

    # The prompt handler sweeps on every turn; the current session is exempted by
    # name regardless of its own mtime, since a long session is old by mtime while
    # still being the state in force.
    call_hook(home, "prompt", {"session_id": current_session, "prompt": "Continue the other work"})

    require(not stale_path.exists(), "reap did not remove an old session's state")
    require(current_path.exists(), "reap removed the state of the session in progress")


def main() -> int:
    require(HOOK.exists(), f"missing hook: {HOOK}")
    require(SETTINGS_MANAGER.exists(), f"missing settings manager: {SETTINGS_MANAGER}")
    require(WORKER_RENDERER.exists(), f"missing worker renderer: {WORKER_RENDERER}")
    test_generated_workers()
    with tempfile.TemporaryDirectory(prefix="delegation-protocol-test-") as tmp:
        root = Path(tmp)
        test_settings_merge(root / "settings-home")
        test_installer_migrates_mux_scheduler_links(root / "installer-home")
        test_single_agent_gate(root / "single-home")
        test_multi_agent_overlap_gate(root / "multi-home")
        test_delegation_queue(root / "queue-home")
        test_round_robin_delegation_queue(root / "round-robin-queue-home")
        test_queue_fallbacks(root / "queue-fallbacks")
        test_classifier_mapping_smoke(root / "classifier-smoke-home")
        test_ci_context_env_is_ignored(root / "ci-context-env-home")
        test_explicit_opt_out(root / "opt-out-home")
        test_pretool_matcher_covers_only_parent_mutation(root / "matcher-home")
        test_foreground_lifecycle(root / "foreground-home")
        test_unlaunched_agent_creates_no_debt(root / "phantom-home")
        test_completed_worker_allows_later_wave(root / "later-wave-home")
        test_relayed_message_continues_turn(root / "relayed-home")
        test_classifier_loader_falls_back_to_clone(root / "loader-fallback-home")
        test_state_version_mismatch_is_discarded(root / "stale-version-home")
        test_reap_removes_stale_sessions(root / "reap-home")
    print("Claude delegation protocol self-test: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
