#!/usr/bin/env python3
"""End-to-end controller tests for managed deployments."""
from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CTL = ROOT / "scripts" / "agents" / "delegationctl.py"


def deployment_fixture(root: Path) -> tuple[Path, Path]:
    launcher = root / "ci-claude"
    launcher.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    launcher.chmod(0o755)
    deployment = root / "deployment.json"
    deployment.write_text(json.dumps({
        "schema_version": 1,
        "id": "ci-claude",
        "selector": {
            "runtimes": ["codex", "claude"],
            "platforms": ["linux", "darwin", "windows"],
            "modes": ["read", "edit"],
            "workspaces": ["shared", "isolated"],
            "functions": ["audit", "edit", "batch", "compress"],
            "tier": "low",
        },
        "provider": {"id": "cheapestinference"},
        "credential": {
            "kind": "protocol_store", "reference": "cheapestinference",
        },
        "gateway": {
            "upstream": "https://api.cheapestinference.com/anthropic",
            "allowed_methods": ["POST"],
            "allowed_paths": ["/v1/messages", "/v1/messages/count_tokens"],
            "credential_header": "Authorization",
            "credential_scheme": "Bearer",
            "resource": "cheapestinference-account",
            "timeout_seconds": 3600,
            "max_request_bytes": 67108864,
        },
        "resources": [{
            "id": "cheapestinference-account", "capacity": 1,
            "lease_seconds": 30, "wait_seconds": 600,
        }],
        "service": {
            "idle_seconds": 300, "max_clients": 16,
            "max_dependency_seconds": 86400, "retention_probe_seconds": 5,
        },
        "runtime": {
            "profile": "claude-code",
            "executable": {"command": "claude", "environment": "CI_CLAUDE_BIN"},
            "session": {
                "config_dir": {"posix": "~/.config/test-session",
                               "windows": "%LOCALAPPDATA%/test-session"},
                "environment": "CI_CLAUDE_SESSION_DIR",
                "max_agents": 4,
                "permission_mode": "auto",
            },
            "environment": {},
            "arguments": [],
        },
        "inference": {
            "model": "deepseek-v4-flash",
            "thinking": {"type": "adaptive"},
            "worker_effort": "max",
            "interactive_effort": "max",
            "context_tokens": 1000000,
            "max_output_tokens": 131072,
        },
        "execution": {
            "workspace_engine": "git-isolated-v1",
            "evidence": "git-patch-v1",
            "permission_policy": "bounded-coding-v1",
        },
    }), encoding="utf-8")
    return deployment, launcher


def run(environment: dict[str, str], *arguments: str,
        expected: int = 0) -> dict:
    result = subprocess.run(
        [sys.executable, str(CTL), *arguments], env=environment,
        text=True, capture_output=True, timeout=30, check=False)
    assert result.returncode == expected, (result.stdout, result.stderr)
    return json.loads(result.stdout) if result.stdout else {}


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="managed-controller-") as raw:
        root = Path(raw)
        deployment, launcher = deployment_fixture(root)
        fake = root / "claude"
        record = root / "launch.json"
        fake.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, sys\n"
            "if 'agents' in sys.argv:\n"
            "    print('[]')\n"
            "    raise SystemExit(0)\n"
            "else:\n"
            "    open(os.environ['FAKE_RECORD'], 'w').write(json.dumps({\n"
            "      'base': os.environ.get('ANTHROPIC_BASE_URL'),\n"
            "      'token': os.environ.get('ANTHROPIC_AUTH_TOKEN'),\n"
            "      'real': os.environ.get('CHEAPESTINFERENCE_API_KEY')}))\n"
            "    print(json.dumps({'num_turns': 1, 'is_error': False, "
            "'result': 'ok'}))\n"
            "raise SystemExit(int(os.environ.get('FAKE_EXIT', '0')))\n",
            encoding="utf-8")
        fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
        source_credential = root / "source-credential"
        source_credential.write_text("provider-secret\n", encoding="utf-8")
        source_credential.chmod(0o600)
        environment = {
            **os.environ,
            "DELEGATION_CONFIG_HOME": str(root / "config"),
            "DELEGATION_STATE_HOME": str(root / "state"),
            "DELEGATION_BIN_DIR": str(root / "bin"),
            "XDG_CONFIG_HOME": str(root / "xdg-config"),
            "XDG_STATE_HOME": str(root / "xdg-state"),
            "CI_CLAUDE_BIN": str(fake),
            "CI_CLAUDE_SESSION_DIR": str(root / "claude-session"),
            "FAKE_RECORD": str(record),
        }
        installed = run(
            environment, "deployment", "install", "--config",
            str(deployment), "--launcher", str(launcher), "ci-claude")
        assert installed["classification"] == "deployment_installed"
        run(environment, "credential", "set", "--deployment", "ci-claude",
            "--from-file", str(source_credential))

        launched = subprocess.run(
            [sys.executable, str(CTL), "launch", "--deployment", "ci-claude",
             "--", "--print", "hello"], env=environment,
            text=True, capture_output=True, timeout=30, check=False)
        assert launched.returncode == 0, (launched.stdout, launched.stderr)
        binding = json.loads(record.read_text(encoding="utf-8"))
        assert binding["base"].startswith("http://127.0.0.1:")
        assert binding["token"] and binding["real"] is None
        failed_launch = subprocess.run(
            [sys.executable, str(CTL), "launch", "--deployment", "ci-claude",
             "--", "--print", "fail"], env={**environment, "FAKE_EXIT": "23"},
            text=True, capture_output=True, timeout=30, check=False)
        assert failed_launch.returncode == 23, (
            failed_launch.stdout, failed_launch.stderr)

        catalog = root / "catalog.json"
        catalog.write_text(json.dumps({
            "schema_version": 2,
            "backends": [{
                "id": "managed-test", "name": "Managed test",
                "kind": "session", "tier": "low",
                "selector": {
                    "runtimes": ["test"], "platforms": ["linux"],
                    "modes": ["read"], "workspaces": ["shared"],
                    "functions": ["audit"],
                },
                "availability": {"commands": [], "environment": []},
                "execution": {
                    "delivery": "managed", "deployment": "ci-claude",
                    "timeout_seconds": 30, "max_steps": 5,
                },
            }],
            "routes": {"bulk": ["managed-test"]}, "includes": [],
        }), encoding="utf-8")
        request = root / "request.json"
        request.write_text(json.dumps({
            "schema_version": 2, "route": "bulk", "tier": "low", "runtime": "test",
            "platform": "linux", "function": "audit", "mode": "read",
            "workspace": "shared", "task": {
                "schema_version": 2, "id": "managed-run", "mode": "read",
                "repo": str(ROOT), "prompt": "return ok", "allowed_paths": [],
                "workspace": "shared", "validation": [], "budgets": {
                    "timeout_seconds": 20, "max_output_bytes": 65536,
                    "max_steps": 5,
                },
            },
        }), encoding="utf-8")
        completed = run(
            environment, "--catalog", str(catalog), "run", "--request-file",
            str(request))
        assert completed["status"] == "completed", completed
        assert completed["backend"] == "managed-test"
        assert "lane" not in completed
        status = run(environment, "service", "status", "--deployment", "ci-claude")
        assert status["clients"] == []
        service_descriptor = (root / "xdg-state" /
                              "agent-delegation-protocol" / "services" /
                              "ci-claude" / "service.json")
        old_pid = json.loads(
            service_descriptor.read_text(encoding="utf-8"))["pid"]
        rotated = root / "rotated-credential"
        rotated.write_text("rotated-provider-secret\n", encoding="utf-8")
        rotated.chmod(0o600)
        run(environment, "credential", "set", "--deployment", "ci-claude",
            "--from-file", str(rotated))
        assert not service_descriptor.exists()
        relaunched = subprocess.run(
            [sys.executable, str(CTL), "launch", "--deployment", "ci-claude",
             "--", "--print", "after rotation"], env=environment,
            text=True, capture_output=True, timeout=30, check=False)
        assert relaunched.returncode == 0, (relaunched.stdout, relaunched.stderr)
        assert json.loads(service_descriptor.read_text(encoding="utf-8"))["pid"] != old_pid
        installed_launcher = root / "bin" / "ci-claude"
        original_launcher = installed_launcher.read_bytes()
        installed_launcher.write_bytes(original_launcher + b"# modified\n")
        changed_config = root / "changed-deployment.json"
        changed = json.loads(deployment.read_text(encoding="utf-8"))
        changed["inference"]["model"] = "changed-model"
        changed_config.write_text(json.dumps(changed), encoding="utf-8")
        refused_install = run(
            environment, "deployment", "install", "--config",
            str(changed_config), "--launcher",
            str(launcher), "ci-claude",
            expected=64)
        assert refused_install["classification"] == "configuration_error"
        assert run(
            environment, "service", "status",
            "--deployment", "ci-claude")["clients"] == []
        refused = run(
            environment, "deployment", "uninstall", "--deployment",
            "ci-claude", expected=64)
        assert refused["classification"] == "configuration_error"
        credential = (root / "xdg-config" / "agent-delegation-protocol" /
                      "credentials" / "cheapestinference")
        assert credential.is_file()
        installed_launcher.write_bytes(original_launcher)
        installed_launcher.chmod(0o755)
        removed_credential = run(
            environment, "credential", "remove", "--deployment", "ci-claude")
        assert removed_credential["classification"] == "credential_removed"
        assert not credential.exists()
        stopped = run(
            environment, "service", "stop", "--deployment", "ci-claude")
        assert stopped["status"] == "stopped"
        removed = run(
            environment, "deployment", "uninstall", "--deployment", "ci-claude")
        assert removed["credential_removed"] is False
        assert not credential.exists()
        print("Managed controller tests: PASS")


if __name__ == "__main__":
    main()
