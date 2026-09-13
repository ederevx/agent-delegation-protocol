#!/usr/bin/env python3
import json, os, sys
from pathlib import Path

HOST = "codex"


def runtime() -> Path:
    """Use the checkout runtime in a checkout, otherwise the installed copy."""
    checkout = Path(__file__).resolve().parents[2] / "scripts" / "hosts"
    if (checkout / "hook_adapter.py").is_file():
        return checkout
    home = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))).expanduser()
    installed = home / ".delegation-protocol"
    if (installed / "hook_adapter.py").is_file():
        return installed
    return checkout


sys.path.insert(0, str(runtime()))
from hook_adapter import run
payload = json.load(sys.stdin)
mode = sys.argv[1] if len(sys.argv) > 1 else "prompt"
print(json.dumps(run(HOST, mode, payload) or {}))
