#!/usr/bin/env python3
"""Self-test the Claude delegation hook and non-destructive settings manager."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
HOOK = REPO_ROOT / "claude" / "hooks" / "delegation-enforcer.py"
SETTINGS_MANAGER = REPO_ROOT / "scripts" / "claude" / "manage-settings.py"


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
    stopped = call_hook(home, "stop", {"session_id": session})
    require(stopped is None, "completed fan-out task was blocked from stopping")


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


def main() -> int:
    require(HOOK.exists(), f"missing hook: {HOOK}")
    require(SETTINGS_MANAGER.exists(), f"missing settings manager: {SETTINGS_MANAGER}")
    with tempfile.TemporaryDirectory(prefix="delegation-protocol-test-") as tmp:
        root = Path(tmp)
        test_settings_merge(root / "settings-home")
        test_single_agent_gate(root / "single-home")
        test_multi_agent_overlap_gate(root / "multi-home")
        test_explicit_opt_out(root / "opt-out-home")
    print("Claude delegation protocol self-test: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
