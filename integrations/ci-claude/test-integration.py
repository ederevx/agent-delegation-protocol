#!/usr/bin/env python3
"""Focused catalog boundary tests for the managed CI deployment."""
from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "agents"))
SPEC = importlib.util.spec_from_file_location(
    "delegationctl", ROOT / "scripts" / "agents" / "delegationctl.py")
assert SPEC and SPEC.loader
CTL = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CTL)


def main() -> None:
    catalog = CTL.load_catalog(ROOT / "agents" / "protocol-v2.json")
    backend = catalog["by_id"]["ci-claude-session-v2"]
    assert backend["kind"] == "session"
    assert backend["selector"]["platforms"] == ["linux", "darwin", "windows"]
    assert backend["execution"] == {
        "delivery": "managed", "deployment": "ci-claude",
        "timeout_seconds": 7200, "max_steps": 1000,
    }
    assert "lane" not in backend
    assert "provider" not in backend and "model" not in backend
    request = {
        "route": "bulk", "runtime": "codex", "platform": "linux",
        "mode": "read", "workspace": "shared", "function": "audit",
    }
    with tempfile.TemporaryDirectory() as temporary:
        prior = os.environ.get("DELEGATION_CONFIG_HOME")
        os.environ["DELEGATION_CONFIG_HOME"] = temporary
        try:
            selected = CTL.select_backend(catalog, request)
        finally:
            if prior is None:
                os.environ.pop("DELEGATION_CONFIG_HOME", None)
            else:
                os.environ["DELEGATION_CONFIG_HOME"] = prior
    assert selected["kind"] == "native"
    assert catalog["routes"]["bulk"][0] == "ci-claude-session-v2"
    print("ci-claude managed catalog tests: PASS")


if __name__ == "__main__":
    main()
