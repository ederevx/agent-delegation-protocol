"""Repository-wide pytest configuration for the hyphenated test suites.

`scripts/claude/test-protocol.py` and `scripts/codex/test-protocol.py` are
direct-run harnesses: their test functions take their fixtures as arguments
from their own `main()`, so pytest must not collect them. The hosts package
tests import `hook_adapter` and `install` as top-level modules, which only
resolves with that directory on `sys.path`.
"""
import sys
from pathlib import Path

collect_ignore_glob = [
    "scripts/claude/test-protocol.py",
    "scripts/codex/test-protocol.py",
]

sys.path.insert(0, str(Path(__file__).parent / "scripts" / "hosts"))
