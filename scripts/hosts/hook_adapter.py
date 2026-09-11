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
        "pending_spawns": list(state.get("pending_spawns", [])),
        "denied_spawns": list(state.get("denied_spawns", [])),
        "observed": list(state.get("observed", [])),
        "peak_active": int(state.get("peak_active", 0)),
        "completed": bool(state.get("completed")),
        "pending_authorization": bool(state.get("pending_authorization")),
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


def _names_worker(payload: dict[str, Any]) -> bool:
    """Whether this event identifies an actual worker, not merely a tool call."""
    return any(isinstance(payload.get(key), str) and payload[key].strip()
               for key in ("agent_id", "agentId"))


def _is_worker_session(host: str, payload: dict[str, Any]) -> bool:
    """Use per-invocation native identity, not inherited process env.

    Lifecycle events also carry agent_id, but identify the worker whose
    evidence must be recorded in the parent session. Call this only for
    prompt, tool, and turn-stop events.
    """
    return _names_worker(payload)


def _delegating(payload: dict[str, Any], classifier: Any) -> bool:
    name = str(payload.get("tool_name") or payload.get("toolName") or "")
    return bool(classifier.AGENT_TOOL_NAME.match(name.strip()))


def _agent_type(payload: dict[str, Any]) -> str | None:
    """The declared worker-profile name Claude records for this session.

    Distinct from `_worker`, which identifies a specific worker instance
    (its agent/task id) rather than which tier profile spawned it. Only
    meaningful once `_is_worker_session` has already established the
    session belongs to a worker at all -- `agent_type` alone (with no
    `agent_id`) also occurs on a parent launched with `--agent` and must
    not be mistaken for a worker session by itself.
    """
    value = payload.get("agent_type") or payload.get("agentType")
    return value.strip() if isinstance(value, str) and value.strip() else None


def _requested_tier(payload: dict[str, Any]) -> str | None:
    """Which profile an Agent/Task call is trying to spawn, from its own args."""
    tool = payload.get("tool_input") or payload.get("toolInput") or {}
    if isinstance(tool, dict):
        value = tool.get("subagent_type") or tool.get("agent_type")
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _tier_violation(payload: dict[str, Any], classifier: Any) -> str | None:
    """Reason a worker session's own Agent/Task call must be denied.

    Returns None when the call is permitted: the caller's own tier
    (from `agent_type`) must be known and strictly above the requested
    target tier (from the tool call's own `subagent_type` argument).
    Missing or unrecognized tier information on either side fails closed.
    """
    caller_name = _agent_type(payload)
    caller = classifier.worker_tier_rank(caller_name)
    if caller is None:
        return (
            "Leaf-tier workers execute the assigned task directly; "
            "delegating to a further subagent is not permitted."
        )
    lower = classifier.lower_tiers(caller)
    if not lower:
        return f"{caller_name} is the lowest tier and cannot delegate further."
    target = classifier.worker_tier_rank(_requested_tier(payload))
    if target is not None and target < caller:
        return None
    return (
        f"a {caller_name} may only delegate to a strictly lower tier "
        f"({', '.join(lower)}), not to itself or higher."
    )


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


def _spawn_token(payload: dict[str, Any]) -> str:
    """The tool-call id a spawn reservation is keyed by, or "" when absent."""
    value = payload.get("tool_use_id") or payload.get("toolUseId")
    return value.strip() if isinstance(value, str) and value.strip() else ""


def _holds_reservation(state: dict[str, Any], payload: dict[str, Any]) -> bool:
    """Whether this exact tool call already owns a slot in this session.

    Only an exact tool-call id match counts. An anonymous reservation cannot
    be attributed to any particular call, so it can never be claimed this way.
    """
    token = _spawn_token(payload)
    return bool(token) and token in state.get("pending_spawns", [])


def _active_cap_violation(state: dict[str, Any], classifier: Any,
                          payload: dict[str, Any]) -> str | None:
    """Reason a further worker spawn must be denied for an over-full session.

    `concurrent` holds the workers genuinely in flight right now (see
    `lifecycle.LifecycleState`), so a completed worker stops counting against
    the cap even under a release mode that still holds it in `active`. Nested
    workers run under their parent's `session_id`, so this one per-session set
    already counts the whole delegation tree rather than a single level of it.

    `pending_spawns` closes the window between an admitted spawn and the
    `SubagentStart` that records it: without it, several spawn calls issued in
    one round each read the same free slot and all pass. A reservation counts
    against the cap exactly like a running worker until its start consumes it.

    A re-delivery of a call that already holds a reservation is admitted
    without consulting the cap at all: it is the same spawn, already paid for,
    so re-testing it would deny an admitted call purely for being delivered
    twice. Both the parent path and the nested-worker path go through here so
    the two hosts and the two paths cannot drift apart.
    """
    if _holds_reservation(state, payload):
        return None
    running = (len(state.get("concurrent", []))
               + len(state.get("pending_spawns", [])))
    cap = classifier.MAX_ACTIVE_WORKERS
    if running >= cap:
        return (
            f"Active worker cap reached ({running}/{cap}); wait for a running "
            "worker to finish before spawning another."
        )
    return None


def _reserve_spawn(state: dict[str, Any], payload: dict[str, Any]) -> None:
    """Hold a slot for a spawn that was admitted but has not started yet.

    A repeated delivery of the same tool-call id is the same spawn, not a
    second one, so it reuses the reservation it already holds. A call with no
    id cannot be matched later and is held as an anonymous entry instead,
    consumed oldest-first by the next unmatched start.
    """
    if _holds_reservation(state, payload):
        return
    pending = list(state.get("pending_spawns", []))
    pending.append(_spawn_token(payload))
    state["pending_spawns"] = pending


def _consume_reservation(state: dict[str, Any], payload: dict[str, Any]) -> None:
    """Retire the reservation a starting worker was admitted under."""
    pending = list(state.get("pending_spawns", []))
    if not pending:
        return
    token = _spawn_token(payload)
    if token and token in pending:
        pending.remove(token)
    else:
        pending.pop(0)
    state["pending_spawns"] = pending


def _record_denied_spawn(state: dict[str, Any], payload: dict[str, Any]) -> bool:
    """Remember a spawn this hook refused, so its failure is not double-counted.

    A denied Agent call never took a slot, but the host may still report it as
    a failed tool call. Without this ledger that report is indistinguishable
    from a failed *admitted* spawn and would free someone else's slot.
    Anonymous denials are not recorded: with no id they could never be matched
    back to a failure event anyway.
    """
    token = _spawn_token(payload)
    denied = list(state.get("denied_spawns", []))
    if not token or token in denied:
        return False
    denied.append(token)
    state["denied_spawns"] = denied
    return True


def _release_failed_reservation(state: dict[str, Any],
                                payload: dict[str, Any]) -> None:
    """Free the slot of a spawn that failed instead of producing a worker.

    Claude wires `PostToolUseFailure` (matcher `Agent`) to the same
    worker-complete event; that payload names the failed call by
    `tool_use_id` and carries no `agent_id`, because no worker ever existed.

    What is kept here is a count, not an identity map: `SubagentStart` carries
    no `tool_use_id`, so a start consumes the oldest entry rather than its
    own. With two spawns admitted, whichever starts first therefore consumes
    the other's entry, and an exact-match-only release would find nothing left
    to free when the other later fails -- leaving the started worker counted
    twice, as running and as pending, until the next prompt. So the invariant
    is `len(pending_spawns) == admitted - started - failed`, and an unmatched
    failure pops the oldest entry by the same rule a start uses.

    Two reports must decrement nothing. A failure for a spawn this hook itself
    denied never took a slot; `denied_spawns` records those, and seeing one
    here retires it without touching `pending_spawns`. And a genuine
    `SubagentStop` names a worker that consumed its reservation back when it
    started, so it is already accounted for -- only an event with no worker
    identity at all is the failed-call report this unmatched pop is meant for.
    """
    token = _spawn_token(payload)
    denied = list(state.get("denied_spawns", []))
    if token and token in denied:
        denied.remove(token)
        state["denied_spawns"] = denied
        return
    pending = list(state.get("pending_spawns", []))
    if not pending:
        return
    if token and token in pending:
        pending.remove(token)
    elif _names_worker(payload):
        return
    else:
        pending.pop(0)
    state["pending_spawns"] = pending


def _deny(reason: str) -> dict[str, Any]:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


class _SkipSave(Exception):
    """Raised by a handler to signal the in-flight state must not be persisted."""


class TurnEventHandler:
    """Applies one normalized hook event against a single turn's saved state.

    Each `handle_*` method owns exactly one event's decision logic; `handle`
    is the dispatcher and the only place that knows the event-name-to-method
    mapping. `state` is mutated in place by design, matching the previous
    function's behavior of updating the caller-owned dict directly.
    """

    def __init__(self, host: str, classifier: Any, mode: str,
                 state: dict[str, Any]) -> None:
        self.host = host
        self.classifier = classifier
        self.mode = mode
        self.state = state
        self.lifecycle = LifecycleState(
            mode,
            set(state["active"]),
            set(state["finished"]),
            set(state["concurrent"]),
        )

    def handle(self, event: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        handlers = {
            "prompt": self._handle_prompt,
            "worker-start": self._handle_worker_start,
            "worker-complete": self._handle_worker_complete,
            "worker-release": self._handle_worker_release,
            "session-end": self._handle_session_end,
            "pre-mutation": self._handle_pre_mutation,
            "turn-stop": self._handle_turn_stop,
        }
        handler = handlers.get(event)
        output = handler(payload) if handler else None
        self.state.update({
            "active": sorted(self.lifecycle.active),
            "finished": sorted(self.lifecycle.finished),
            "concurrent": sorted(self.lifecycle.concurrent),
            "mode": self.mode,
        })
        return output

    def _handle_prompt(self, payload: dict[str, Any]) -> dict[str, Any]:
        prompt = payload.get("prompt") or payload.get("user_prompt") or ""
        if self.classifier.RELAYED_MESSAGE.match(str(prompt)):
            # A relayed worker/peer message or a background task's own
            # notification, not something the user typed -- leave
            # whatever obligation is already in flight untouched rather
            # than classifying its text or resetting evidence collected
            # so far (see RELAYED_MESSAGE's own docstring).
            return _routing_context("UserPromptSubmit", self.classifier)
        decision = self.classifier.classify(
            str(prompt),
            self.state,
            context_env=("CLAUDE_CODE_MAX_CONTEXT_TOKENS",)
            if self.host == "claude" else ("CODEX_MAX_CONTEXT_TOKENS",),
        )
        carry = bool(decision.get("carry_forward"))
        # A reservation only spans the gap between an admitted spawn and its
        # start. By the time the user types again -- continuation or not --
        # every spawn of the previous round has either started or failed, so
        # anything still held here is a leak. Clearing it on both paths is
        # also what bounds that leak on Codex, which has no failure hook.
        self.state["pending_spawns"] = []
        self.state["denied_spawns"] = []
        if not carry:
            # A new turn clears the previous turn's delegation evidence, but
            # background workers still genuinely in flight are not evidence --
            # they are running processes, and forgetting them would let the
            # active-worker cap be reset simply by typing another prompt.
            self.lifecycle = LifecycleState(
                self.mode, concurrent=set(self.lifecycle.concurrent)
            )
            self.state["observed"] = []
            self.state["peak_active"] = 0
        self.state.update({
            "requires_delegation": bool(decision["requires_delegation"]),
            "requires_multi": bool(decision["requires_multi"]),
            "analysis_signal": bool(decision.get("analysis_signal")),
            "execution_signal": bool(decision.get("execution_signal")),
            "min_agents": int(decision["min_agents"]),
            "completed": False,
            # A one-shot authorization is set fresh from this prompt's own
            # text only -- never carried from the previous turn's value --
            # so it cannot silently persist across turns. It is consumed
            # (cleared) the first time it overrides a denial in
            # `_handle_pre_mutation`, or cleared unused at `_handle_turn_stop`.
            "pending_authorization": bool(decision.get("explicit_authorization")),
        })
        return _routing_context("UserPromptSubmit", self.classifier)

    def _handle_worker_start(self, payload: dict[str, Any]) -> dict[str, Any]:
        worker = _worker(payload)
        if worker:
            if self.host == "codex":
                # A native start pins the initial limit before any tool call.
                # Corrupt ledgers remain untouched and deny at PreToolUse.
                _worker_tool_budget(_home(self.host), payload, self.classifier, charge=False)
            self.lifecycle.start(worker)
            _consume_reservation(self.state, payload)
            observed = set(self.state["observed"])
            observed.add(worker)
            self.state["observed"] = sorted(observed)
            self.state["peak_active"] = max(
                self.state["peak_active"], len(self.lifecycle.concurrent)
            )
        return _routing_context("SubagentStart", self.classifier)

    def _handle_worker_complete(self, payload: dict[str, Any]) -> None:
        worker = _worker(payload)
        if worker:
            self.lifecycle.complete(worker)
        # Also reached by Claude's Agent-failure signal, where the spawn never
        # became a worker and its reservation would otherwise never be freed.
        _release_failed_reservation(self.state, payload)
        return None

    def _handle_worker_release(self, payload: dict[str, Any]) -> None:
        worker = _worker(payload)
        if worker:
            self.lifecycle.release(worker)
        return None

    def _handle_session_end(self, payload: dict[str, Any]) -> None:
        self.lifecycle.end_session()
        self.state["pending_spawns"] = []
        self.state["denied_spawns"] = []
        self.state["completed"] = True
        return None

    def _handle_pre_mutation(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        # Workload delegation applies regardless of model. Reading, analysis,
        # and ordinary tool use have no tier-specific capability restrictions.
        reason = _spawn_budget_violation(payload, self.classifier)
        delegating = _delegating(payload, self.classifier)
        if reason is None and delegating:
            reason = _active_cap_violation(self.state, self.classifier, payload)
        if reason is None and _mutating(payload, self.classifier):
            reason = _unmet(self.state)
        if reason is not None:
            if not self.state.get("pending_authorization"):
                if delegating:
                    _record_denied_spawn(self.state, payload)
                return _deny(reason)
            # Consumed here, once: this specific denial is the one
            # subsequent decision the user's explicit authorization named.
            # Enforcement reverts to normal for every action after this one,
            # including an immediate repeat of the same tool call.
            self.state["pending_authorization"] = False
        if delegating:
            # Admitted, so the slot is spoken for from here until the worker's
            # own start event arrives -- including a spawn admitted by the
            # one-shot authorization above.
            _reserve_spawn(self.state, payload)
        return None

    def _handle_turn_stop(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        # An authorization granted but never consumed by a blocked action
        # must not survive past the turn it was granted in.
        self.state["pending_authorization"] = False
        reason = _unmet(self.state)
        if reason:
            return {"decision": "block", "reason": reason}
        if self.mode == "explicit_release" and self.lifecycle.finished:
            return {
                "decision": "block",
                "reason": "Release completed workers before ending this turn.",
            }
        self.state["completed"] = True
        return None


def _routing_context(event: str, classifier: Any) -> dict[str, Any]:
    return {"hookSpecificOutput": {
        "hookEventName": event,
        "additionalContext": classifier.ROUTING_POLICY,
    }}


def _spawn_budget_violation(payload: dict[str, Any], classifier: Any) -> str | None:
    if not _delegating(payload, classifier):
        return None
    tool = payload.get("tool_input") or payload.get("toolInput") or {}
    requested = tool.get("max_turns") if isinstance(tool, dict) else None
    if requested is None:
        return None
    tier = classifier.canonical_worker_name(_requested_tier(payload))
    limit = classifier.WORKER_TURN_LIMITS.get(tier, min(classifier.WORKER_TURN_LIMITS.values()))
    if type(requested) is not int or not 1 <= requested <= limit:
        return f"Requested max_turns must be a positive integer at most {limit} for this worker."
    return None


def _worker_tool_budget(home: Path, payload: dict[str, Any],
                        classifier: Any, *, charge: bool = True) -> dict[str, Any] | None:
    try:
        return _worker_tool_budget_locked(home, payload, classifier, charge=charge)
    except (OSError, ValueError, TypeError):
        # Hook exceptions can be treated as non-blocking by the host. Return
        # an explicit decision when persistence or lock acquisition fails.
        return _deny("Worker tool-call budget could not be verified or saved; report the ledger/lock error to the parent without further tool calls.")


def _worker_tool_budget_locked(home: Path, payload: dict[str, Any],
                               classifier: Any, *, charge: bool = True) -> dict[str, Any] | None:
    """Count distinct hook-covered Codex tool-call attempts per native worker.

    This is not a model-turn counter or a count of successful tool executions.
    Missing call ids count each hook invocation conservatively. The lifetime
    ledger survives parent prompt resets and resumes. Unknown tiers receive
    the smallest budget; missing native identity cannot establish a ledger.
    """
    worker = payload.get("agent_id") or payload.get("agentId")
    if not isinstance(worker, str) or not worker.strip():
        return None
    path, lock = _paths(home, "worker-tool-budget:" + worker.strip())
    with _locked(lock):
        try:
            ledger = json.loads(path.read_text())
        except FileNotFoundError:
            tier = classifier.canonical_worker_name(_agent_type(payload))
            ledger = {"tier": tier, "limit": classifier.WORKER_TURN_LIMITS.get(
                tier, min(classifier.WORKER_TURN_LIMITS.values())), "used": 0, "seen": []}
        except (OSError, json.JSONDecodeError):
            return _deny("Worker tool-call budget ledger is unreadable or corrupt; report this to the parent without continuing tool calls.")
        if (not isinstance(ledger, dict) or type(ledger.get("limit")) is not int or
                ledger["limit"] <= 0 or type(ledger.get("used")) is not int or
                not 0 <= ledger["used"] <= ledger["limit"] or
                not isinstance(ledger.get("seen"), list) or
                not all(isinstance(item, str) for item in ledger["seen"])):
            return _deny("Worker tool-call budget ledger is invalid; report this to the parent without resetting the budget.")
        limit = ledger["limit"]
        if not charge:
            _save(path, ledger)
            return None
        seen = set(ledger["seen"])
        used = ledger["used"]
        call_id = payload.get("tool_use_id") or payload.get("toolUseId")
        call_id = call_id.strip() if isinstance(call_id, str) else ""
        if call_id and call_id in seen:
            return None
        if used >= limit:
            return _deny(
                f"Worker tool-call budget exhausted ({used}/{limit}; 0 remaining). "
                "Return a plain final report with evidence and remaining work; "
                "do not make further tool calls. Completion is permitted."
            )
        if call_id:
            seen.add(call_id)
        ledger.update(used=used + 1, seen=sorted(seen))
        _save(path, ledger)
    return None


def run(host: str, event: str, payload: dict[str, Any]) -> dict[str, Any] | None:
    """Apply one normalized hook event and return host-compatible feedback."""
    if host not in {"claude", "codex"} or not isinstance(payload, dict):
        return None
    session = _session(payload)
    home = _home(host)
    classifier = _classifier(home)
    if event in {"prompt", "pre-mutation", "turn-stop"} and _is_worker_session(host, payload):
        # Worker activity does not rewrite parent obligations. Tool budgets
        # have their own persistent ledger and never block final completion.
        if event == "prompt":
            return _routing_context("UserPromptSubmit", classifier)
        if event == "pre-mutation":
            if host == "codex":
                denial = _worker_tool_budget(home, payload, classifier)
                if denial:
                    return denial
            reason = _spawn_budget_violation(payload, classifier)
            if reason is None and _delegating(payload, classifier):
                reason = _tier_violation(payload, classifier)
            if _delegating(payload, classifier) and session:
                # A nested spawn shares the parent's session_id, so the cap is
                # read from -- and its reservation written to -- the parent's
                # ledger, all under one lock so parallel spawns cannot each
                # claim the same slot. `pending_spawns` and `denied_spawns`
                # are the only fields a worker event ever writes there; the
                # parent turn's own obligations, evidence, and authorization
                # state are left untouched.
                try:
                    path, lock = _paths(home, session)
                    with _locked(lock):
                        parent_state = _load(path, _release_mode(home))
                        if reason is None:
                            reason = _active_cap_violation(
                                parent_state, classifier, payload)
                        if reason is None:
                            _reserve_spawn(parent_state, payload)
                            _save(path, parent_state)
                        elif _record_denied_spawn(parent_state, payload):
                            _save(path, parent_state)
                except (OSError, ValueError):
                    return _deny(
                        "Active worker ledger could not be read or updated to "
                        "verify the concurrent-worker cap; report this to the "
                        "parent instead of spawning another worker."
                    )
            return _deny(reason) if reason else None
        return None
    if session is None:
        return None
    path, lock = _paths(home, session)
    mode = _release_mode(home)
    with _locked(lock):
        state = _load(path, mode)
        handler = TurnEventHandler(host, classifier, mode, state)
        try:
            output = handler.handle(event, payload)
        except _SkipSave:
            return None
        _save(path, state)
        return output
