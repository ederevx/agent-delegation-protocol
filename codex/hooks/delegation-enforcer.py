#!/usr/bin/env python3
import json, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "hosts"))
from hook_adapter import run
payload = json.load(sys.stdin)
mode = sys.argv[1] if len(sys.argv) > 1 else "prompt"
print(json.dumps(run("codex", mode, payload) or {}))
