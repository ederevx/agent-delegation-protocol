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
        "analysis_signal": bool(state.get("analysis_signal")),
        "execution_signal": bool(state.get("execution_signal")),
        "min_agents": int(state.get("min_agents", 0)),
        "active": list(state.get("active", [])),
        "finished": list(state.get("finished", [])),
        "concurrent": list(state.get("concurrent", [])),
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


def _is_worker_session(host: str, payload: dict[str, Any]) -> bool:
    """Use Claude's per-invocation identity, not inherited process env.

    Lifecycle events also carry agent_id, but identify the worker whose
    evidence must be recorded in the parent session. Call this only for
    prompt, tool, and turn-stop events.
    """
    worker = payload.get("agent_id")
    return host == "claude" and isinstance(worker, str) and bool(worker.strip())


def _delegating(payload: dict[str, Any]) -> bool:
    name = str(payload.get("tool_name") or payload.get("toolName") or "")
    return name.strip().lower() in {"agent", "task"}


def _mutating(payload: dict[str, Any], classifier: Any) -> bool:
    name = str(payload.get("tool_name") or payload.get("toolName") or "")
    if classifier.MUTATING_TOOL_NAME.search(name):
        return True
    tool = payload.get("tool_input") or payload.get("toolInput") or {}
    command = (
        tool.get("command") or tool.get("cmd", "")
        if isinstance(tool, dict) else ""
    )
    return bool(classifier.MUTATING_BASH.search(str(command)) or
                classifier.MUTATING_POWERSHELL.search(str(command)))


def _context_pulling(payload: dict[str, Any], classifier: Any,
                      state: dict[str, Any]) -> bool:
    name = str(payload.get("tool_name") or payload.get("toolName") or "")
    if name.strip().lower() == "bash" and not state.get("analysis_signal"):
        # Plain (non-mutating) Bash execution is exempt from the
        # context-pulling gate so the parent can run direct user orders as
        # shell commands without a worker having started first -- but only
        # while this turn carries no "analysis, review, or verification"
        # wording. An analysis-flagged turn falls through to the
        # command-field check below instead, same as any other exec-shaped
        # tool. Mutating bash commands are still caught separately by
        # `_mutating` above regardless, unaffected by this exemption since
        # that check runs first in the elif-chain.
        return False
    if classifier.CONTEXT_PULLING_TOOL_NAME.search(name):
        return True
    tool = payload.get("tool_input") or payload.get("toolInput") or {}
    if isinstance(tool, dict) and (tool.get("command") or tool.get("cmd")):
        return True
    return False


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


def _execution_unmet(state: dict[str, Any]) -> str | None:
    """Execution's own floor, independent of `_unmet` above.

    `execution_signal` clears at a lower bar than `requires_delegation` (see
    EXECUTION_WINDOW_SHARE in delegation-classifier.py), so a turn too small
    to need general delegation can still be too big to execute inline. Only
    a bare floor of one worker applies here -- no `min_agents`/concurrency
    requirement, since those belong to the broader delegation decision, not
    to this narrower one.
    """
    if not state.get("execution_signal") or state["observed"]:
        return None
    return (
        "Execution beyond a small, non-research-requiring change is "
        "reserved for delegated agents; route this to a worker first."
    )


def _deny(reason: str) -> dict[str, Any]:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


def _bypass(home: Path) -> bool:
    """Global convention: the user may unconditionally lift any
    hook-enforced convention -- the delegation requirement, the release
    gate, the commit/tag gate -- by creating this marker file by hand.
    Agents must never create, edit, or script around it themselves; it
    exists solely for the human owner to invoke directly."""
    return (home / ".delegation-protocol" / "bypass").is_file()


def run(host: str, event: str, payload: dict[str, Any]) -> dict[str, Any] | None:
    """Apply one normalized hook event and return host-compatible feedback."""
    if host not in {"claude", "codex"} or not isinstance(payload, dict):
        return None
    session = _session(payload)
    if session is None:
        return None
    home = _home(host)
    if event in {"prompt", "pre-mutation", "turn-stop"} and _is_worker_session(host, payload):
        # Worker tool calls can share their parent's session_id. They must
        # neither enforce parent delegation floors nor rewrite parent state.
        if event == "pre-mutation" and _delegating(payload) and not _bypass(home):
            return _deny(
                "Leaf-tier workers execute the assigned task directly; "
                "delegating to a further subagent is not permitted."
            )
        return None
    path, lock = _paths(home, session)
    mode = _release_mode(home)
    classifier = _classifier(home)
    with _locked(lock):
        state = _load(path, mode)
        lifecycle = LifecycleState(
            mode,
            set(state["active"]),
            set(state["finished"]),
            set(state["concurrent"]),
        )
        output = None
        if event == "prompt":
            prompt = payload.get("prompt") or payload.get("user_prompt") or ""
            if classifier.RELAYED_MESSAGE.match(str(prompt)):
                # A relayed worker/peer message or a background task's own
                # notification, not something the user typed -- leave
                # whatever obligation is already in flight untouched rather
                # than classifying its text or resetting evidence collected
                # so far (see RELAYED_MESSAGE's own docstring).
                return None
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
                "analysis_signal": bool(decision.get("analysis_signal")),
                "execution_signal": bool(decision.get("execution_signal")),
                "min_agents": int(decision["min_agents"]),
                "completed": False,
            })
        elif event == "worker-start":
            worker = _worker(payload)
            if worker:
                lifecycle.start(worker)
                observed = set(state["observed"])
                observed.add(worker)
                state["observed"] = sorted(observed)
                state["peak_active"] = max(
                    state["peak_active"], len(lifecycle.concurrent)
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
            if not _bypass(home):
                if _mutating(payload, classifier):
                    reason = _unmet(state) or _execution_unmet(state)
                    if reason:
                        output = _deny(reason)
                elif _context_pulling(payload, classifier, state):
                    if state.get("analysis_signal"):
                        # No floor escape here, unlike the branch below: once
                        # a turn is analysis-flagged, the parent never pulls
                        # content into its own context for the rest of the
                        # turn, no matter how many workers have started.
                        # Analysis stays reserved for delegated agents.
                        output = _deny(
                            "Analysis is reserved for delegated agents; "
                            "route this to a worker instead of pulling "
                            "content into the parent's own context."
                        )
                    else:
                        floor = max(state["min_agents"], 1) if state["requires_delegation"] else 1
                        observed = len(set(state["observed"]))
                        if observed < floor:
                            output = _deny(
                                "Route this to a worker before pulling content "
                                f"into context (requires at least {floor} "
                                "lifecycle-visible worker(s))."
                            )
        elif event == "turn-stop":
            if _bypass(home):
                state["completed"] = True
            else:
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
            "concurrent": sorted(lifecycle.concurrent),
            "mode": mode,
        })
        _save(path, state)
        return output
