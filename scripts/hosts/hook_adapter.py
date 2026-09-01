#!/usr/bin/env python3
"""Shared host adapter for protocol-v2 classification and lifecycle evidence."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

try:
    from .lifecycle import LifecycleState
except ImportError:
    from lifecycle import LifecycleState


def _home(host: str) -> Path:
    variable = "CLAUDE_CONFIG_DIR" if host == "claude" else "CODEX_HOME"
    default = ".claude" if host == "claude" else ".codex"
    return Path(os.environ.get(variable, str(Path.home() / default))).expanduser()


def _classifier(home: Path):
    candidates = (
        home / ".delegation-protocol" / "delegation-classifier.py",
        Path(__file__).resolve().parents[1] / "agents" / "delegation-classifier.py",
    )
    for path in candidates:
        if not path.is_file():
            continue
        specification = importlib.util.spec_from_file_location(
            "protocol_v2_classifier", path
        )
        if specification and specification.loader:
            module = importlib.util.module_from_spec(specification)
            specification.loader.exec_module(module)
            return module
    raise RuntimeError("protocol-v2 classifier is not installed")


def _release_mode(home: Path) -> str:
    try:
        manifest = json.loads(
            (home / ".delegation-protocol" / "manifest.json").read_text(
                encoding="utf-8"
            )
        )
        mode = manifest.get("release")
    except (OSError, json.JSONDecodeError):
        mode = None
    return mode if mode in {
        "automatic_release", "explicit_release", "session_release"
    } else "session_release"


def _session(payload: dict[str, Any]) -> str | None:
    value = payload.get("session_id") or payload.get("sessionId")
    return value.strip() if isinstance(value, str) and value.strip() else None


def _worker(payload: dict[str, Any]) -> str | None:
    for key in ("agent_id", "agentId", "task_id", "taskId", "tool_use_id"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    tool = payload.get("tool_input")
    if isinstance(tool, dict):
        for key in ("agent_id", "task_id", "name"):
            value = tool.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def _paths(home: Path, session: str) -> tuple[Path, Path]:
    root = home / ".delegation-protocol" / "hook-state"
    key = hashlib.sha256(session.encode()).hexdigest()
    return root / f"{key}.json", root / f"{key}.lock"


@contextmanager
def _locked(lock: Path) -> Iterator[None]:
    lock.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + 2
    while True:
        try:
            lock.mkdir()
            break
        except FileExistsError:
            if time.monotonic() >= deadline:
                raise TimeoutError("protocol hook state is busy")
            time.sleep(0.01)
    try:
        yield
    finally:
        try:
            lock.rmdir()
        except OSError:
            pass


def _load(path: Path, mode: str) -> dict[str, Any]:
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        state = {}
    if not isinstance(state, dict) or state.get("schema_version") != 2:
        state = {}
    return {
        "schema_version": 2,
        "requires_delegation": bool(state.get("requires_delegation")),
        "requires_multi": bool(state.get("requires_multi")),
        "min_agents": int(state.get("min_agents", 0)),
        "active": list(state.get("active", [])),
        "finished": list(state.get("finished", [])),
        "observed": list(state.get("observed", [])),
        "peak_active": int(state.get("peak_active", 0)),
        "completed": bool(state.get("completed")),
        "mode": mode,
    }


def _save(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.",
                                             dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(state, handle, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _mutating(payload: dict[str, Any], classifier: Any) -> bool:
    name = str(payload.get("tool_name") or payload.get("toolName") or "")
    if classifier.MUTATING_TOOL_NAME.search(name):
        return True
    tool = payload.get("tool_input") or payload.get("toolInput") or {}
    command = tool.get("command", "") if isinstance(tool, dict) else ""
    return bool(classifier.MUTATING_BASH.search(str(command)) or
                classifier.MUTATING_POWERSHELL.search(str(command)))


def _unmet(state: dict[str, Any]) -> str | None:
    if not state["requires_delegation"]:
        return None
    observed = len(set(state["observed"]))
    minimum = state["min_agents"]
    if observed < minimum:
        return f"Delegate this turn to at least {minimum} lifecycle-visible worker(s)."
    if minimum > 1 and state["peak_active"] < minimum:
        return f"Launch at least {minimum} independent workers concurrently."
    return None


def _deny(reason: str) -> dict[str, Any]:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


def run(host: str, event: str, payload: dict[str, Any]) -> dict[str, Any] | None:
    """Apply one normalized hook event and return host-compatible feedback."""
    if host not in {"claude", "codex"} or not isinstance(payload, dict):
        return None
    session = _session(payload)
    if session is None:
        return None
    home = _home(host)
    path, lock = _paths(home, session)
    mode = _release_mode(home)
    classifier = _classifier(home)
    with _locked(lock):
        state = _load(path, mode)
        lifecycle = LifecycleState(
            mode,
            set(state["active"]),
            set(state["finished"]),
        )
        output = None
        if event == "prompt":
            prompt = payload.get("prompt") or payload.get("user_prompt") or ""
            decision = classifier.classify(
                str(prompt),
                state,
                context_env=("CLAUDE_CODE_MAX_CONTEXT_TOKENS",)
                if host == "claude" else ("CODEX_MAX_CONTEXT_TOKENS",),
            )
            carry = bool(decision.get("carry_forward"))
            if not carry:
                lifecycle = LifecycleState(mode)
                state["observed"] = []
                state["peak_active"] = 0
            state.update({
                "requires_delegation": bool(decision["requires_delegation"]),
                "requires_multi": bool(decision["requires_multi"]),
                "min_agents": int(decision["min_agents"]),
                "completed": False,
            })
            if decision["requires_delegation"]:
                reasons = "; ".join(decision["classification_reasons"])
                context = (
                    f"Delegation protocol v2 requires {decision['min_agents']} "
                    f"lifecycle-visible worker(s) for this turn: {reasons}."
                )
                output = {"hookSpecificOutput": {
                    "hookEventName": "UserPromptSubmit",
                    "additionalContext": context,
                }}
        elif event == "worker-start":
            worker = _worker(payload)
            if worker:
                lifecycle.start(worker)
                observed = set(state["observed"])
                observed.add(worker)
                state["observed"] = sorted(observed)
                state["peak_active"] = max(
                    state["peak_active"], len(lifecycle.active)
                )
        elif event == "worker-complete":
            worker = _worker(payload)
            if worker:
                lifecycle.complete(worker)
        elif event == "worker-release":
            worker = _worker(payload)
            if worker:
                lifecycle.release(worker)
        elif event == "session-end":
            lifecycle.end_session()
            state["completed"] = True
        elif event == "pre-mutation":
            if _mutating(payload, classifier):
                reason = _unmet(state)
                if reason:
                    output = _deny(reason)
        elif event == "turn-stop":
            reason = _unmet(state)
            if reason:
                output = {"decision": "block", "reason": reason}
            elif mode == "explicit_release" and lifecycle.finished:
                output = {
                    "decision": "block",
                    "reason": "Release completed workers before ending this turn.",
                }
            else:
                state["completed"] = True
        state.update({
            "active": sorted(lifecycle.active),
            "finished": sorted(lifecycle.finished),
            "mode": mode,
        })
        _save(path, state)
        return output
