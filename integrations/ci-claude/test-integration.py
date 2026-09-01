#!/usr/bin/env python3
"""Focused catalog boundary tests for the optional CI session adapter."""
import importlib.util, json, os, shutil, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts/agents"))
spec = importlib.util.spec_from_file_location("delegationctl", ROOT / "scripts/agents/delegationctl.py")
ctl = importlib.util.module_from_spec(spec); spec.loader.exec_module(ctl)
def main():
  catalog = ctl.load_catalog(ROOT / "agents/protocol-v2.json")
  adapter = catalog["by_id"]["ci-claude-session-v2"]
  assert adapter["kind"] == "session" and adapter["execution"]["argv"] == ["ci-claude-worker", "--v2"]
  assert "provider" not in adapter and "model" not in adapter
  request={"route":"bulk","runtime":"codex","platform":"linux","mode":"read","workspace":"shared","function":"audit"}
  selected=ctl.select_backend(catalog, request)
  if shutil.which("ci-claude-worker") is None:
    assert selected["kind"] == "native"
  assert catalog["routes"]["bulk"][0] == "ci-claude-session-v2"
  print("ci-claude integration catalog tests: PASS")
if __name__ == "__main__": main()
