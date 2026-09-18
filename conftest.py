"""Repository-wide pytest configuration for the hyphenated test suites.

`scripts/claude/test-protocol.py` and `scripts/codex/test-protocol.py` are
direct-run harnesses: their test functions take their fixtures as arguments
from their own `main()`, so pytest must not collect them (the harnesses stay
covered through scripts/hosts/test-harnesses.py).

The `sys.path` insert is permanently required, not redundant with
`scripts/hosts/__init__.py`: under importlib collection pytest inserts
nothing, the hyphenated test filenames can never be imported
package-relatively, and the package branch of `install.py`'s settings import
still needs `__init__.py` to exist.
"""
import sys
from pathlib import Path

collect_ignore_glob = [
    "scripts/claude/test-protocol.py",
    "scripts/codex/test-protocol.py",
]

sys.path.insert(0, str(Path(__file__).parent / "scripts" / "hosts"))
