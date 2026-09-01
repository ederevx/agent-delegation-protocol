"""Shared v2 event adapter; host files only select home, mode, and output shape."""
from __future__ import annotations
import importlib.util, json, os, tempfile
from pathlib import Path
try:
    from .lifecycle import LifecycleState
except ImportError:
    from lifecycle import LifecycleState

def run(host: str, event: str, payload: dict) -> dict | None:
    home_var = "CLAUDE_CONFIG_DIR" if host == "claude" else "CODEX_HOME"
    home = Path(os.environ.get(home_var, str(Path.home() / (".claude" if host == "claude" else ".codex")))).expanduser()
    state_dir = home / ".delegation-protocol" / "hook-state"
    state_dir.mkdir(parents=True, exist_ok=True)
    sid = payload.get("session_id")
    if not isinstance(sid, str) or not sid.strip():
        return None
    path = state_dir / (sid.replace("/", "_") + ".json")
    try: state = json.loads(path.read_text(encoding="utf-8"))
    except Exception: state = {"requires": False, "active": [], "finished": [], "mode": "session_release"}
    lifecycle = LifecycleState(state.get("mode", "session_release"), set(state.get("active", [])), set(state.get("finished", [])))
    if event == "prompt":
        classifier = importlib.util.spec_from_file_location("protocol_classifier", Path(__file__).resolve().parents[1] / "agents" / "delegation-classifier.py")
        if classifier and classifier.loader:
            module = importlib.util.module_from_spec(classifier); classifier.loader.exec_module(module)
            decision = module.classify(str(payload.get("prompt", "")), state, context_env=("CLAUDE_CODE_MAX_CONTEXT_TOKENS",) if host == "claude" else ("CODEX_MAX_CONTEXT_TOKENS",))
            state["requires"] = bool(decision.get("requires_delegation")); state["min_agents"] = decision.get("min_agents", 1)
    elif event == "start": lifecycle.start(str(payload.get("agent_id", "")))
    elif event == "stop": lifecycle.complete(str(payload.get("agent_id", "")))
    elif event == "release": lifecycle.release(str(payload.get("agent_id", "")))
    elif event == "session-end": lifecycle.end_session()
    state.update({"active": sorted(lifecycle.active), "finished": sorted(lifecycle.finished)})
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", dir=state_dir)
    with os.fdopen(fd, "w", encoding="utf-8") as handle: json.dump(state, handle, sort_keys=True); handle.flush(); os.fsync(handle.fileno())
    os.replace(tmp, path)
    if event == "prompt": return {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": "Delegation protocol v2: delegate according to the classified task shape."}}
    if event == "stop" and state.get("requires") and lifecycle.active and lifecycle.mode == "explicit_release":
        return {"decision": "block", "reason": "Release finished workers explicitly before ending this turn."}
    return None
