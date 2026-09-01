#!/usr/bin/env python3
"""Focused catalog boundary tests for the managed CI deployment."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest import mock

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
    assert backend["selector"]["functions"] == ["compress"]
    request = {
        "route": "bulk", "runtime": "codex", "platform": "linux",
        "mode": "read", "workspace": "shared", "function": "audit",
    }
    with mock.patch.object(CTL, "_available", return_value=True):
        selected = CTL.select_backend(catalog, request)
    assert selected["kind"] == "native"
    request["runtime"] = "claude"
    request["function"] = "compress"
    with mock.patch.object(CTL, "_available", return_value=True):
        selected = CTL.select_backend(catalog, request)
    assert selected["id"] == "ci-claude-session-v2"
    assert catalog["routes"]["bulk"][0] == "ci-claude-session-v2"
    print("ci-claude managed catalog tests: PASS")


if __name__ == "__main__":
    main()
