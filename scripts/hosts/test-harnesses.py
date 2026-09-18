#!/usr/bin/env python3
"""Bring the direct-run host harnesses under plain `pytest`.

`scripts/claude/test-protocol.py` and `scripts/codex/test-protocol.py` are
self-contained runners whose test functions take fixtures from their own
`main()`; conftest.py excludes them from collection. Running them as
subprocesses here keeps both suites covered by the same `pytest`
invocation, so they cannot rot silently.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_claude_protocol_harness() -> None:
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts/claude/test-protocol.py")],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr


def test_codex_protocol_harness() -> None:
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts/codex/test-protocol.py")],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
