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
CI_DEPLOYMENT = ROOT.parent / "ci-claude" / "deployment.json"


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
        fake = root / "claude"
        record = root / "launch.json"
        fake.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, sys\n"
            "if 'agents' in sys.argv:\n"
            "    print('[]')\n"
            "else:\n"
            "    open(os.environ['FAKE_RECORD'], 'w').write(json.dumps({\n"
            "      'base': os.environ.get('ANTHROPIC_BASE_URL'),\n"
            "      'token': os.environ.get('ANTHROPIC_AUTH_TOKEN'),\n"
            "      'real': os.environ.get('CHEAPESTINFERENCE_API_KEY')}))\n"
            "    print(json.dumps({'num_turns': 1, 'is_error': False, "
            "'result': 'ok'}))\n",
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
            str(CI_DEPLOYMENT), "--launcher",
            str(ROOT.parent / "ci-claude" / "ci-claude-v3.sh"), "ci-claude")
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

        catalog = root / "catalog.json"
        catalog.write_text(json.dumps({
            "schema_version": 2,
            "backends": [{
                "id": "managed-test", "name": "Managed test",
                "kind": "session", "priority": 100,
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
            "schema_version": 2, "route": "bulk", "runtime": "test",
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
        assert completed["status"] == "completed"
        assert completed["backend"] == "managed-test"
        assert "lane" not in completed
        status = run(environment, "service", "status", "--deployment", "ci-claude")
        assert status["clients"] == []
        run(environment, "deployment", "uninstall", "--deployment", "ci-claude")
        print("Managed controller tests: PASS")


if __name__ == "__main__":
    main()
