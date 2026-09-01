#!/usr/bin/env python3
"""Focused tests for the protocol-owned Claude runtime profile."""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from permission_service import PermissionStore


HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location(
    "claude_runtime", HERE / "claude_runtime.py")
assert SPEC is not None and SPEC.loader is not None
runtime = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runtime)


STUB = r'''#!/usr/bin/env python3
import json, os, pathlib, sys
agents = pathlib.Path(os.environ["STUB_AGENTS"])
if sys.argv[1:3] == ["agents", "--json"]:
    print(agents.read_text() if agents.exists() else "[]")
    raise SystemExit(0)
record = pathlib.Path(os.environ["STUB_RECORD"])
record.write_text(json.dumps({
    "argv": sys.argv[1:],
    "auth": os.environ.get("ANTHROPIC_AUTH_TOKEN"),
    "base": os.environ.get("ANTHROPIC_BASE_URL"),
    "model": os.environ.get("ANTHROPIC_MODEL"),
    "tiers": [os.environ.get("ANTHROPIC_DEFAULT_" + tier + "_MODEL")
              for tier in ("OPUS", "SONNET", "HAIKU", "FABLE")],
    "config": os.environ.get("CLAUDE_CONFIG_DIR"),
    "api_key": os.environ.get("ANTHROPIC_API_KEY"),
}))
if os.environ.get("STUB_BACKGROUND"):
    agents.write_text('[{"id":"bg-1","kind":"background"}]')
raise SystemExit(int(os.environ.get("STUB_EXIT", "0")))
'''


class Binding:
    base_url = "http://127.0.0.1:43210"
    token = "dummy-client-token"

    def __init__(self) -> None:
        self.actions: list[str] = []

    def heartbeat(self) -> None:
        self.actions.append("heartbeat")

    def retain(self) -> None:
        self.actions.append("retain")

    def close(self) -> None:
        self.actions.append("close")


def deployment(executable: Path, session: Path) -> dict:
    return {
        "deployment_schema_version": 1,
        "id": "fixture",
        "runtime": {
            "profile": "claude-code",
            "executable": {"command": str(executable),
                           "environment": "FIXTURE_CLAUDE_BIN"},
            "session": {"config_dir": str(session), "max_agents": 3},
        },
        "inference": {
            "model": "fixture-model",
            "interactive_effort": "high",
            "context_tokens": 1000000,
            "max_output_tokens": 32000,
        },
    }


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def test_launch_environment_and_arguments(root: Path) -> None:
    stub = root / "claude"
    stub.write_text(STUB, encoding="utf-8")
    stub.chmod(0o755)
    session = root / "session"
    record = root / "record.json"
    agents = root / "agents.json"
    agents.write_text("[]", encoding="utf-8")
    binding = Binding()
    environment = dict(os.environ, STUB_RECORD=str(record),
                       STUB_AGENTS=str(agents), ANTHROPIC_API_KEY="must-go")
    status = runtime.launch(
        deployment(stub, session), ["--print", "hello"], gateway=binding,
        environ=environment)
    require(status == 0, f"launch returned {status}")
    seen = json.loads(record.read_text(encoding="utf-8"))
    require(seen["argv"][-2:] == ["--print", "hello"], "argv tail changed")
    require(seen["argv"][:2] == ["--settings", seen["argv"][1]],
            "managed settings were not first")
    require("--effort" in seen["argv"] and "high" in seen["argv"],
            "interactive effort was not applied")
    require(seen["auth"] == binding.token and seen["base"] == binding.base_url,
            "gateway binding was not mapped")
    require(seen["model"] == "fixture-model", "model was not mapped")
    require(seen["tiers"] == ["fixture-model"] * 4,
            "model tiers escaped the deployment")
    require(seen["api_key"] is None, "first-party credential leaked")
    require(binding.actions == ["close"], f"binding lifecycle: {binding.actions}")
    settings = json.loads((session / "settings.json").read_text(encoding="utf-8"))
    require(settings["permissions"]["defaultMode"] == "auto",
            "permission mode was not configured")
    require(settings["env"]["CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS"] == "3",
            "agent cap was not configured")
    require(settings["hooks"]["PermissionRequest"][0]["hooks"][0][
        "statusMessage"] == "delegation: permission policy",
        "protocol permission hook was not installed")
    require(len(settings["hooks"]["PreToolUse"]) == 2,
            "protocol preflight hooks were not installed")


def test_control_passthrough_needs_no_gateway(root: Path) -> None:
    stub = root / "claude-control"
    stub.write_text(STUB, encoding="utf-8")
    stub.chmod(0o755)
    session = root / "control-session"
    record = root / "control.json"
    agents = root / "control-agents.json"
    agents.write_text("[]", encoding="utf-8")
    environment = dict(os.environ, STUB_RECORD=str(record),
                       STUB_AGENTS=str(agents), ANTHROPIC_AUTH_TOKEN="old",
                       ANTHROPIC_BASE_URL="https://should-not-survive")
    status = runtime.launch(deployment(stub, session), ["logs", "bg-1"],
                            environ=environment)
    require(status == 0, f"control returned {status}")
    seen = json.loads(record.read_text(encoding="utf-8"))
    require(seen["argv"] == ["logs", "bg-1"], "control argv changed")
    require(seen["auth"] is None and seen["base"] is None,
            "control invocation inherited gateway authority")


def test_background_handoff_retains_binding(root: Path) -> None:
    stub = root / "claude-background"
    stub.write_text(STUB, encoding="utf-8")
    stub.chmod(0o755)
    session = root / "background-session"
    record = root / "background.json"
    agents = root / "background-agents.json"
    agents.write_text("[]", encoding="utf-8")
    binding = Binding()
    environment = dict(os.environ, STUB_RECORD=str(record),
                       STUB_AGENTS=str(agents), STUB_BACKGROUND="1")
    status = runtime.launch(deployment(stub, session), [], gateway=binding,
                            environ=environment)
    require(status == 0, f"background launch returned {status}")
    require(binding.actions == ["retain"],
            f"background binding was not retained: {binding.actions}")
    identifiers = runtime.background_session_ids(
        deployment(stub, session), environ=environment)
    require(identifiers == {"bg-1"}, f"background probe failed: {identifiers}")


def test_validation(root: Path) -> None:
    stub = root / "claude-validation"
    stub.write_text(STUB, encoding="utf-8")
    stub.chmod(0o755)
    try:
        runtime.validate_arguments(["--settings=bad.json"])
    except runtime.RuntimeProfileError:
        pass
    else:
        raise AssertionError("caller --settings was accepted")
    try:
        runtime.build_environment(deployment(stub, root / "session"),
                                  root / "session", environ={})
    except runtime.RuntimeProfileError as error:
        require(error.status == runtime.RUNTIME_ERROR,
                "missing gateway used the wrong status")
    else:
        raise AssertionError("launch without a gateway was accepted")


def test_worker_runner_uses_bounded_headless_contract(root: Path) -> None:
    stub = root / "claude-worker"
    stub.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, pathlib, sys\n"
        "prompt = sys.stdin.read()\n"
        "pathlib.Path(os.environ['WORKER_RECORD']).write_text(json.dumps({\n"
        " 'argv': sys.argv[1:], 'prompt': prompt,\n"
        " 'permission': os.environ.get('DELEGATION_PERMISSION_STATE'),\n"
        " 'workspace': os.environ.get('DELEGATION_WORKSPACE_ROOT')}))\n"
        "print(json.dumps({'type':'result','result':'done','num_turns':2}))\n",
        encoding="utf-8")
    stub.chmod(0o755)
    record = root / "worker.json"
    task = {
        "schema_version": 2, "id": "task-1", "mode": "read",
        "repo": str(root), "prompt": "audit this", "allowed_paths": [],
        "workspace": "shared", "validation": [],
        "budgets": {"timeout_seconds": 30, "max_output_bytes": 100000,
                    "max_steps": 5},
    }
    permissions = PermissionStore(root / "permissions.json", "session-1")
    context = {
        "token": "session-1", "step": 0, "remaining_seconds": 30.0,
        "remaining_steps": 5, "continuation": None,
        "permissions": permissions,
    }
    binding = Binding()
    configured = deployment(stub, root / "interactive-session")
    configured["inference"]["worker_effort"] = "low"
    configured["inference"]["thinking"] = {"type": "adaptive"}
    old = os.environ.get("WORKER_RECORD")
    os.environ["WORKER_RECORD"] = str(record)
    try:
        outcome = runtime.worker_runner(
            configured, lambda _task, _context: binding)(task, root, context)
    finally:
        if old is None:
            os.environ.pop("WORKER_RECORD", None)
        else:
            os.environ["WORKER_RECORD"] = old
    require(outcome["classification"] == "success" and outcome["completed"],
            f"worker outcome failed: {outcome}")
    require(outcome["steps_used"] == 2, "worker turn count was not normalized")
    seen = json.loads(record.read_text(encoding="utf-8"))
    require(seen["argv"][:5] == ["-p", "--output-format", "json",
                                  "--max-turns", "5"],
            f"headless argv changed: {seen['argv']}")
    require("--strict-mcp-config" in seen["argv"] and
            '{"mcpServers":{}}' in seen["argv"], "MCP was not emptied")
    require(seen["permission"] == str(permissions.path),
            "permission state was not handed to the hook")
    require(seen["workspace"] == str(root), "workspace was not bounded")
    require(binding.actions == ["close"], "worker did not close its binding")


def test_permission_codec_issues_normalized_parent_request(root: Path) -> None:
    state = root / "permission-state.json"
    PermissionStore(state, "permission-session")
    event = {
        "hook_event_name": "PreToolUse", "tool_name": "Bash",
        "tool_input": {"command": "make test"}, "cwd": str(root),
    }
    environment = dict(
        os.environ, DELEGATION_PERMISSION_STATE=str(state),
        DELEGATION_TASK_ID="permission-session",
        DELEGATION_WORKSPACE_ROOT=str(root), DELEGATION_TASK_MODE="edit",
        DELEGATION_ALLOWED_PATHS="[]")
    result = subprocess.run(
        [sys.executable, str(HERE / "claude_runtime.py"), "hook", "permission"],
        input=json.dumps(event), text=True, capture_output=True,
        env=environment, check=False)
    require(result.returncode == 0, f"permission codec failed: {result.stderr}")
    output = json.loads(result.stdout)
    require(output["hookSpecificOutput"]["permissionDecision"] == "deny",
            "parent-required operation was not paused")
    pending = PermissionStore(state, "permission-session").pending()
    require(pending is not None and pending["operation"] == "shell",
            f"normalized permission was not persisted: {pending}")
    require(pending["arguments"] == {"command": "make test"},
            "permission arguments changed")


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="claude-runtime-test-") as temporary:
        root = Path(temporary)
        tests = [test_launch_environment_and_arguments,
                 test_control_passthrough_needs_no_gateway,
                 test_background_handoff_retains_binding,
                 test_validation,
                 test_worker_runner_uses_bounded_headless_contract,
                 test_permission_codec_issues_normalized_parent_request]
        for index, test in enumerate(tests):
            case = root / str(index)
            case.mkdir()
            test(case)
    print(f"ok: {len(tests)} Claude runtime tests")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
