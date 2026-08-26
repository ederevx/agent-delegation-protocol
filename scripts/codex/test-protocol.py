#!/usr/bin/env python3
"""Self-test Codex delegation hooks and non-destructive hooks.json merging."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
HOOK = REPO_ROOT / "codex" / "hooks" / "delegation-enforcer.py"
MANAGER = REPO_ROOT / "scripts" / "codex" / "manage-hooks.py"


def run(cmd: list[str], *, env: dict[str, str] | None = None, stdin: dict[str, Any] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, input=(json.dumps(stdin) if stdin is not None else None), text=True, capture_output=True, env=env, check=False)


def require(value: bool, message: str) -> None:
    if not value:
        raise AssertionError(message)


def call_hook(home: Path, mode: str, payload: dict[str, Any]) -> dict[str, Any] | None:
    env = dict(os.environ)
    env["CODEX_HOME"] = str(home)
    result = run([sys.executable, str(HOOK), mode], env=env, stdin=payload)
    require(result.returncode == 0, f"hook {mode} failed: {result.stderr}")
    text = result.stdout.strip()
    return json.loads(text) if text else None


def test_hooks_merge(home: Path) -> None:
    home.mkdir(parents=True, exist_ok=True)
    hooks_path = home / "hooks.json"
    original = {
        "description": "existing hooks",
        "hooks": {
            "PreToolUse": [
                {
                    "matcher": "^Bash$",
                    "hooks": [{"type": "command", "command": "echo existing", "statusMessage": "Existing hook"}],
                }
            ]
        },
    }
    hooks_path.write_text(json.dumps(original, indent=2) + "\n", encoding="utf-8")

    result = run([sys.executable, str(MANAGER), "install", "--codex-home", str(home), "--hook-path", str(HOOK), "--python", sys.executable])
    require(result.returncode == 0, f"hook install failed: {result.stderr}")
    installed = json.loads(hooks_path.read_text(encoding="utf-8"))
    require(installed["description"] == "existing hooks", "top-level metadata was changed")
    require(any(h.get("statusMessage") == "Existing hook" for g in installed["hooks"]["PreToolUse"] for h in g.get("hooks", [])), "existing hook was removed")
    require(any(str(h.get("statusMessage", "")).startswith("Delegation protocol:") for groups in installed["hooks"].values() for g in groups for h in g.get("hooks", [])), "protocol hooks were not installed")

    result = run([sys.executable, str(MANAGER), "uninstall", "--codex-home", str(home), "--hook-path", str(HOOK), "--python", sys.executable])
    require(result.returncode == 0, f"hook uninstall failed: {result.stderr}")
    after = json.loads(hooks_path.read_text(encoding="utf-8"))
    require(any(h.get("statusMessage") == "Existing hook" for g in after["hooks"]["PreToolUse"] for h in g.get("hooks", [])), "existing hook was not preserved")
    require(not any(str(h.get("statusMessage", "")).startswith("Delegation protocol:") for groups in after.get("hooks", {}).values() for g in groups for h in g.get("hooks", [])), "protocol hooks remained after uninstall")


def test_single_gate(home: Path) -> None:
    session = "single"
    call_hook(home, "prompt", {"session_id": session, "turn_id": "t1", "prompt": "Update 12 files with this mechanical rename."})
    denied = call_hook(home, "pretool", {"session_id": session, "turn_id": "t1", "tool_name": "apply_patch", "tool_input": {"command": "*** Begin Patch"}})
    require(denied is not None and denied["hookSpecificOutput"]["permissionDecision"] == "deny", "parent patch was not blocked before delegation")
    call_hook(home, "subagent-start", {"session_id": session, "turn_id": "t1", "agent_id": "worker-a", "agent_type": "bulk_worker"})
    allowed = call_hook(home, "pretool", {"session_id": session, "turn_id": "t1", "tool_name": "apply_patch", "tool_input": {"command": "*** Begin Patch"}})
    require(allowed is None, "parent patch stayed blocked after required delegation")


def test_multi_overlap(home: Path) -> None:
    session = "multi"
    call_hook(home, "prompt", {"session_id": session, "turn_id": "t1", "prompt": "Implement independent frontend and backend subsystems plus separate tests."})
    call_hook(home, "subagent-start", {"session_id": session, "turn_id": "t1", "agent_id": "front", "agent_type": "bulk_worker"})
    denied = call_hook(home, "pretool", {"session_id": session, "turn_id": "t1", "tool_name": "Bash", "tool_input": {"command": "touch integration.txt"}})
    require(denied is not None, "multi-agent task was not blocked after only one worker")
    call_hook(home, "subagent-start", {"session_id": session, "turn_id": "t1", "agent_id": "back", "agent_type": "bulk_worker"})
    allowed = call_hook(home, "pretool", {"session_id": session, "turn_id": "t1", "tool_name": "Bash", "tool_input": {"command": "touch integration.txt"}})
    require(allowed is None, "multi-agent task did not unlock after overlapping workers")
    call_hook(home, "subagent-stop", {"session_id": session, "turn_id": "t1", "agent_id": "front", "agent_type": "bulk_worker"})
    call_hook(home, "subagent-stop", {"session_id": session, "turn_id": "t1", "agent_id": "back", "agent_type": "bulk_worker"})
    require(call_hook(home, "stop", {"session_id": session, "turn_id": "t1", "stop_hook_active": False}) is None, "completed fan-out task was blocked")


def test_stop_continuation(home: Path) -> None:
    session = "continuation"
    call_hook(home, "prompt", {"session_id": session, "turn_id": "t1", "prompt": "Apply this change across 20 files."})
    blocked = call_hook(home, "stop", {"session_id": session, "turn_id": "t1", "stop_hook_active": False})
    require(blocked is not None and blocked.get("decision") == "block", "stop did not continue an undelegated bulk turn")
    reason = blocked["reason"]
    call_hook(home, "prompt", {"session_id": session, "turn_id": "t2", "prompt": reason})
    denied = call_hook(home, "pretool", {"session_id": session, "turn_id": "t2", "tool_name": "apply_patch", "tool_input": {"command": "patch"}})
    require(denied is not None, "continuation lost delegation requirement")


def test_opt_out(home: Path) -> None:
    session = "optout"
    call_hook(home, "prompt", {"session_id": session, "turn_id": "t1", "prompt": "Update 20 files but do not delegate or spawn agents."})
    require(call_hook(home, "pretool", {"session_id": session, "turn_id": "t1", "tool_name": "apply_patch", "tool_input": {"command": "patch"}}) is None, "explicit no-delegation instruction was ignored")


def main() -> int:
    require(HOOK.exists(), f"missing {HOOK}")
    require(MANAGER.exists(), f"missing {MANAGER}")
    with tempfile.TemporaryDirectory(prefix="codex-delegation-test-") as tmp:
        root = Path(tmp)
        test_hooks_merge(root / "merge")
        test_single_gate(root / "single")
        test_multi_overlap(root / "multi")
        test_stop_continuation(root / "continuation")
        test_opt_out(root / "optout")
    print("Codex delegation protocol self-test: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
