#!/usr/bin/env python3
"""Pi host installation and native lifecycle smoke tests."""
import json, os, shutil, subprocess, sys, tempfile, importlib.util
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
ENGINE = ROOT / "scripts/hosts/install.py"
HOOK = ROOT / "pi/.delegation-protocol/delegation-enforcer.py"
SETTINGS = ROOT / "scripts/hosts/settings.py"
ALL_TIERS = ("quick-worker", "bulk-worker", "balanced-worker", "frontier-worker")


def main():
  root = Path(tempfile.mkdtemp(prefix="adp-pi-test-"))
  home = root / "home"
  env = dict(os.environ, PI_CODING_AGENT_DIR=str(home))

  def invoke(event, payload, hook=HOOK, environment=env):
    result = subprocess.run([sys.executable, str(hook), event],
        input=json.dumps(payload), env=environment, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout or "{}")

  def denied(body):
    assert body["hookSpecificOutput"]["permissionDecision"] == "deny", body
    return body["hookSpecificOutput"]["permissionDecisionReason"]

  # Install into a disposable home; the Pi host owns no settings file.
  installed = subprocess.run([sys.executable, str(ENGINE), "install", "--host",
      "pi", "--home", str(home), "--repo", str(ROOT)], env=env,
      capture_output=True, text=True)
  assert installed.returncode == 0, installed.stderr
  assert not (home / "settings.json").exists()
  for name in ("rules/delegation-protocol.md", "agents/frontier-worker.md",
      "agents/balanced-worker.md", "agents/bulk-worker.md",
      "agents/quick-worker.md", ".delegation-protocol/delegation-enforcer.py",
      "extensions/adp-subagent/enforcer.ts",
      ".delegation-protocol/delegation-protocol.py" if False else
      ".delegation-protocol/delegation-classifier.py",
      ".delegation-protocol/hook_adapter.py", ".delegation-protocol/manifest.json"):
    assert (home / name).is_file() and not (home / name).is_symlink(), name
  manifest = json.loads((home / ".delegation-protocol/manifest.json").read_text())
  assert manifest["host"] == "pi" and manifest["version"] == 3, manifest
  assert ".delegation-protocol/delegation-enforcer.py" in " ".join(manifest["owned"])
  assert "extensions/adp-subagent/enforcer.ts" in " ".join(manifest["owned"])

  # Routing context and budget texts flow through the shared classifier.
  body = invoke("prompt", {"session_id": "route", "prompt": "Review this module."})
  routing = body["hookSpecificOutput"]["additionalContext"]
  for text in ("lowest capable worker", "Skip unnecessary tiers", "strictly downward",
      "128", "64", "32", "16"):
    assert text in routing, (text, routing)

  # Pi's native delegation tool is named `subagent`; the requested tier comes
  # from the subagent_type field the enforcer derives from the `agent` arg.
  for tier in ALL_TIERS:
    assert invoke("pre-mutation", {"session_id": "spawn", "tool_name": "subagent",
        "tool_input": {"agent": tier, "subagent_type": tier}}) == {}, tier

  # Delegation evidence gates eligible mutations, and evidence clears them.
  invoke("prompt", {"session_id": "gate", "prompt":
      "Update every file across the repository. First do this, then that, then more."})
  assert "Delegate this turn" in denied(invoke("pre-mutation",
      {"session_id": "gate", "tool_name": "edit", "tool_input": {"path": "x"}}))
  assert invoke("pre-mutation", {"session_id": "gate", "tool_name": "read",
      "tool_input": {"path": "x"}}) == {}
  invoke("worker-start", {"session_id": "gate", "agent_id": "gate-child",
      "agent_type": "bulk-worker"})
  assert invoke("pre-mutation", {"session_id": "gate", "tool_name": "bash",
      "tool_input": {"command": "git commit -m x"}}) == {}
  invoke("worker-complete", {"session_id": "gate"})

  # Worker tool-call budget: Pi checks at pre-mutation without charging;
  # executed calls charge at post-tool-use, repeats count once, and the
  # ledger pins at the first covered call and denies when exhausted.
  worker = {"session_id": "budget", "agent_id": "pi:abc", "agent_type": "quick-worker"}
  for call in range(3):
    assert invoke("pre-mutation", dict(worker, tool_name="bash",
        tool_input={"command": f"echo {call}"}, tool_use_id=f"call-{call}")) == {}
    assert invoke("post-tool-use", dict(worker, tool_name="bash",
        tool_input={"command": f"echo {call}"}, tool_use_id=f"call-{call}")) == {}
  assert invoke("pre-mutation", dict(worker, tool_name="bash",
      tool_input={"command": "echo repeat"}, tool_use_id="call-1")) == {}
  assert invoke("post-tool-use", dict(worker, tool_name="bash",
      tool_input={"command": "echo repeat"}, tool_use_id="call-1")) == {}
  import hashlib as _hashlib
  key = _hashlib.sha256("worker-tool-budget:pi:abc".encode()).hexdigest()
  ledger_dir = home / ".delegation-protocol/hook-state"
  ledger = json.loads((ledger_dir / f"{key}.json").read_text())
  assert ledger["used"] == 3 and ledger["limit"] == 128, ledger

  # An identified worker with an unknown tier gets the conservative limit 16.
  unknown = {"session_id": "unknown", "agent_id": "pi:xyz"}
  assert invoke("pre-mutation", dict(unknown, tool_name="read",
      tool_input={"path": "x"}, tool_use_id="u-1")) == {}
  key = _hashlib.sha256("worker-tool-budget:pi:xyz".encode()).hexdigest()
  pinned = json.loads((ledger_dir / f"{key}.json").read_text())
  assert pinned["limit"] == 16 and pinned["used"] == 0, pinned

  # At the limit the pre-mutation check denies terminally and never charges.
  ledger_path = ledger_dir / (
      _hashlib.sha256("worker-tool-budget:pi:abc".encode()).hexdigest() + ".json")
  ledger_path.write_text(json.dumps(dict(ledger, limit=3)))
  body = invoke("pre-mutation", dict(worker, tool_name="bash",
      tool_input={"command": "echo more"}, tool_use_id="call-more"))
  output = body["hookSpecificOutput"]
  assert output["permissionDecision"] == "deny" and output["terminal"] is True, body
  assert "exhausted (3/3; 0 remaining)" in output["permissionDecisionReason"], body
  assert json.loads(ledger_path.read_text())["used"] == 3

  # Strictly downward worker recursion, fail-closed on unknown tiers.
  bulk_payload = {"session_id": "tier", "agent_id": "leaf-b", "agent_type": "bulk-worker"}
  for target in ("bulk-worker", "balanced-worker", "frontier-worker"):
    assert "strictly lower tier" in denied(invoke("pre-mutation",
        dict(bulk_payload, tool_name="subagent",
             tool_input={"agent": target, "subagent_type": target})))
  assert "cannot delegate" in denied(invoke("pre-mutation",
      dict({"session_id": "tier", "agent_id": "leaf-q", "agent_type": "quick-worker"},
           tool_name="subagent",
           tool_input={"agent": "quick-worker", "subagent_type": "quick-worker"})))
  for target in ALL_TIERS:
    assert invoke("pre-mutation", {"session_id": "pm", "tool_name": "subagent",
        "tool_input": {"agent": target, "subagent_type": target}}) == {}

  # The one-shot owner authorization works through the Pi bridge.
  invoke("prompt", {"session_id": "byp", "prompt":
      "Update 12 files across independent modules. I explicitly authorize this action."})
  assert invoke("pre-mutation", {"session_id": "byp", "tool_name": "edit",
      "tool_input": {"path": "x"}}) == {}
  assert "Delegate this turn" in denied(invoke("pre-mutation",
      {"session_id": "byp", "tool_name": "edit", "tool_input": {"path": "x"}}))

  # The installed bridge uses its configured home's copied runtime tree.
  adapter = home / ".delegation-protocol/hook_adapter.py"
  original = adapter.read_bytes()
  adapter.write_text('raise RuntimeError("installed adapter was selected")\n')
  try:
    body = invoke("prompt", {"session_id": "installed-runtime", "prompt": "Say hi."})
  finally:
    adapter.write_bytes(original)
  assert body["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit", body

  # Uninstall removes only unchanged protocol-owned resources and never
  # creates or edits a Pi settings file.
  uninstall = subprocess.run([sys.executable, str(ENGINE), "uninstall", "--host",
      "pi", "--home", str(home), "--repo", str(ROOT)], env=env,
      capture_output=True, text=True)
  assert uninstall.returncode == 0, uninstall.stderr
  assert not (home / "settings.json").exists()
  for name in ("agents/frontier-worker.md", "agents/quick-worker.md",
      ".delegation-protocol/delegation-enforcer.py", "extensions/adp-subagent/enforcer.ts",
      ".delegation-protocol/manifest.json"):
    assert not (home / name).exists(), name

  # The settings manager itself never touches a None-settings host.
  spec = importlib.util.spec_from_file_location("settings", SETTINGS)
  mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
  assert mod.HOST_SETTINGS_FILE["pi"] is None
  mod.uninstall("pi", home)  # no-op path must not raise

  shutil.rmtree(root, ignore_errors=True)
  print("Pi host tests: PASS")

if __name__ == "__main__":
  main()