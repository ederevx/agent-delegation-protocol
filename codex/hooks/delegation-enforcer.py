#!/usr/bin/env python3
"""Codex delegation protocol enforcement hook."""
from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

sys.dont_write_bytecode = True


def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))).expanduser()


def load_classifier() -> Any:
    """Import the shared classifier: installed copy first, then this clone."""
    candidates = (
        codex_home() / ".delegation-protocol" / "delegation-classifier.py",
        Path(__file__).resolve().parents[2] / "scripts" / "agents" / "delegation-classifier.py",
    )
    for path in candidates:
        try:
            spec = importlib.util.spec_from_file_location("_delegation_classifier", path)
            if spec is None or spec.loader is None:
                continue
            module = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = module
            spec.loader.exec_module(module)
            return module
        except Exception:
            continue
    return None


# The second candidate above matters because the hook is installed as a symlink
# into $CODEX_HOME/hooks, and Path(__file__).resolve() follows it back into the
# clone -- so a session running straight out of the repository, before install,
# still resolves the module. Resolved once at import time: every mode below is a
# fail-open guardrail, never a gate that can wedge a turn, so a missing or broken
# shared module must make the whole hook a no-op rather than raise (see main())
# instead of crashing or silently enforcing half a policy.
shared = load_classifier()

CONTINUATION_PREFIX = "DELEGATION_PROTOCOL_CONTINUE:"

# Env vars the shared classifier reads to size the parent's context window. Kept
# here because which vars matter is Codex-specific configuration, not shared
# policy.
CONTEXT_ENV = ("CODEX_MAX_CONTEXT_TOKENS", "CODEX_CONTEXT_WINDOW")


def select_delegation_queue(runtime: str) -> dict[str, Any] | None:
    """Return safe host-facing queue details, failing closed to normal fan-out."""
    installed = codex_home() / ".delegation-protocol"
    candidates = (
        installed / "delegation_queue.py",
        Path(__file__).resolve().parents[2] / "scripts" / "agents" / "delegation_queue.py",
    )
    for module_path in candidates:
        try:
            spec = importlib.util.spec_from_file_location("_delegation_queue", module_path)
            if spec is None or spec.loader is None:
                continue
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module.select(installed, runtime)
        except Exception:
            continue
    return None


def state_root() -> Path:
    root = codex_home() / ".delegation-protocol" / "hook-state"
    root.mkdir(parents=True, exist_ok=True)
    return root


def safe(value: Any) -> str:
    raw = str(value or "unknown")
    return re.sub(r"[^A-Za-z0-9_.-]", "_", raw)[:180] or "unknown"


def valid_session_id(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def base(session_id: Any) -> Path:
    return state_root() / safe(session_id)


def state_path(session_id: Any) -> Path:
    return Path(str(base(session_id)) + ".json")


def active_dir(session_id: Any) -> Path:
    return Path(str(base(session_id)) + ".active")


def marker(session_id: Any, name: str) -> Path:
    return Path(str(base(session_id)) + f".{name}")


def finished_dir(session_id: Any) -> Path:
    return Path(str(base(session_id)) + ".finished")


def dismissed_dir(session_id: Any) -> Path:
    return Path(str(base(session_id)) + ".dismissed")


def known_dir(session_id: Any) -> Path:
    """Workers this protocol actually launched, for the life of the session.

    SubagentStop fires for agents the protocol never started -- runtime internals
    that carry a nameless id no dismissal call can target. Only an agent recorded
    here at SubagentStart may create a dismissal obligation.
    """
    return Path(str(base(session_id)) + ".known")


def nagged_dir(session_id: Any) -> Path:
    """Workers whose outstanding debt has already been reported this turn."""
    return Path(str(base(session_id)) + ".nagged")


def pending_attempts_dir(session_id: Any, *, after_delegation: bool) -> Path:
    """Agent calls awaiting the success-only PostToolUse event.

    Codex does not emit PostToolUse when spawning fails. PreToolUse therefore
    records each attempt, and a successful PostToolUse clears that call id. The
    separate directories retain whether a worker had already started, which is
    the distinction between failing open a first delegation and a later fan-out
    attempt.
    """
    suffix = "multi-attempts" if after_delegation else "attempts"
    return Path(str(base(session_id)) + f".{suffix}")


def worker_key(value: Any) -> str:
    """Normalize an agent identity so a spawn id and a dismissal target compare equal."""
    raw = str(value or "").strip()
    if not raw:
        return ""
    return safe(raw.split("@", 1)[0].lower())


def is_dismissal_tool(name: str) -> bool:
    """Codex's dismissal primitive is not fixed across builds, so match the shape.

    Any tool whose name pairs a stop/kill/dismiss verb with a task/agent noun
    counts. ``interrupt_agent`` deliberately does not count: that call leaves
    the agent available and therefore does not release its lifecycle slot.
    """
    return bool(shared.DISMISSAL_TOOL.search(name))


def is_agent_tool(name: str) -> bool:
    """Whether a hook tool name denotes Codex's spawn-agent operation.

    ``Agent`` is the hooks matcher alias; current deliveries carry the
    canonical ``spawn_agent`` name, optionally namespace-qualified.
    """
    return name == "Agent" or name.rsplit(".", 1)[-1] == "spawn_agent"


def keys_match(finished: str, target: str) -> bool:
    """Whether a dismissal target names a finished worker.

    The two never have to be identical: a runtime id wraps the worker name in a
    prefix and a random suffix, so containment either way is the reliable test.
    Short targets are required to match exactly so a stray id cannot clear
    everything.
    """
    if finished == target:
        return True
    if len(target) < 4 or len(finished) < 4:
        return False
    return target in finished or finished in target


def outstanding_workers(session_id: Any) -> list[str]:
    """Workers whose task finished but which were never dismissed, oldest first."""
    finished = finished_dir(session_id)
    if not finished.exists():
        return []
    dismissed = dismissed_dir(session_id)
    targets = [p.name for p in dismissed.iterdir() if p.is_file()] if dismissed.exists() else []
    rows = []
    for path in finished.iterdir():
        if path.is_file() and not any(keys_match(path.name, target) for target in targets):
            rows.append((path.stat().st_mtime, path.name))
    return [name for _, name in sorted(rows)]


def dismissal_reason(names: list[str], action: str) -> str:
    return (
        f"Delegation protocol: {len(names)} finished worker(s) are still held and occupying subagent "
        f"capacity ({', '.join(names)}). A worker stays alive and idle after its task completes; it is "
        f"only released by an explicit stop/dismiss call. Dismiss each one {action}."
    )


def load_state(session_id: Any) -> dict[str, Any]:
    p = state_path(session_id)
    if not p.exists():
        return {}
    try:
        value = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}
    # State written by a different protocol version describes a turn's evidence
    # under a meaning this build may not share, so it is discarded outright
    # rather than interpreted -- see shared.state_is_current.
    if not isinstance(value, dict) or not shared.state_is_current(value):
        return {}
    return value


def save_state(session_id: Any, value: dict[str, Any]) -> None:
    p = state_path(session_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=p.name + ".", dir=str(p.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(value, indent=2, sort_keys=True) + "\n")
        os.replace(tmp, p)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def reset_evidence(session_id: Any) -> None:
    for directory in (
        active_dir(session_id),
        finished_dir(session_id),
        dismissed_dir(session_id),
        nagged_dir(session_id),
        pending_attempts_dir(session_id, after_delegation=False),
        pending_attempts_dir(session_id, after_delegation=True),
    ):
        if directory.exists():
            shutil.rmtree(directory, ignore_errors=True)
    for name in ("delegated", "fanout", "unavailable", "multi-unavailable", "dismissal-tool", "dismissal-nagged"):
        try:
            marker(session_id, name).unlink()
        except FileNotFoundError:
            pass


def touch(p: Path) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.touch(exist_ok=True)


def read_event() -> dict[str, Any]:
    try:
        value = json.load(sys.stdin)
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def emit(value: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(value, separators=(",", ":")))


def policy_context(c: dict[str, Any]) -> str:
    threshold = int(c.get("token_threshold") or shared.token_threshold(CONTEXT_ENV))
    base_text = (
        "DELEGATION PROTOCOL (hook-enforced): preserve the frontier parent for planning, ambiguity, difficult reasoning, "
        "integration, conflict resolution, and final validation. Prefer the installed `bulk_worker` custom agent "
        "(GPT-5.6 Luna) for low-risk repetitive/high-volume work and `balanced_worker` (GPT-5.6 Terra) when a delegated "
        "unit needs more reasoning. For independent subsystems/shards, launch multiple workers concurrently unless this "
        "hook explicitly selects delegation queue. Give workers non-overlapping scope, acceptance criteria, validation commands, "
        "and require concise evidence reports. The parent is the single integration authority. "
        "A delivered FINAL_ANSWER or final-status notification counts as collecting a worker's result; immediately after "
        "reading it, call the build's true stop/dismiss primitive before doing more work. `interrupt_agent` is not a "
        "dismissal when its contract says the agent remains available.\n"
        "SIZE AND SHAPE THRESHOLDS (apply these yourself, whether or not this hook flagged the turn): estimate the "
        f"work before starting it. Delegate any task you estimate at {threshold}+ tokens of reading, output, and "
        f"tool traffic ({int(shared.DELEGATION_WINDOW_SHARE * 100)}% of one compaction window), and any task that runs to "
        f"{shared.STEP_DELEGATION_THRESHOLD} or more distinct steps. Plan and integrate in the parent; hand the execution "
        "to workers, one bounded unit each, and re-estimate when the work turns out larger than it looked. Keep "
        "only genuinely small, single-step, or tightly coupled work in the parent."
    )
    if not c.get("requires_delegation"):
        return base_text
    reasons = ", ".join(c.get("classification_reasons", [])) or "bulk task"
    minimum = int(c.get("min_agents", 1))
    if c.get("delegation_queue"):
        if c.get("delegation_queue_strategy") == "round_robin":
            slots = int(c["delegation_queue_virtual_slots"])
            return base_text + (
                f"\nHOOK CLASSIFICATION: this turn is delegation-eligible ({reasons}) and round-robin delegation "
                f"queue selected backend `{c['delegation_queue_backend']}`, advertising {slots} virtual slots. "
                "Before parent mutation, start one lifecycle-visible `bulk_worker` dispatcher. Give it every "
                "independent workstream as one queue batch and "
                "explicitly instruct it to submit the batch through mux-scheduler `queue`. The singular scheduler "
                "process admits virtual agents in bounded waves, round-robins them on its physical provider lane, "
                "and owns their concurrent command jobs; host-level dispatcher overlap is not required. A "
                "queue failure must be reported and must never be replayed on a native backend."
            )
        return base_text + (
            f"\nHOOK CLASSIFICATION: this turn is delegation-eligible ({reasons}) and delegation queue selected "
            f"backend `{c['delegation_queue_backend']}`. Before parent mutation, start one lifecycle-visible "
            "`bulk_worker` dispatcher. Give it every independent unit as one ordered batch and explicitly instruct "
            "it to submit the batch through mux-scheduler `queue`; host-level worker overlap is not required. A queue "
            "failure must be reported and must never be replayed on a native backend."
        )
    overlap = " The workers must overlap in time." if minimum > 1 else ""
    return base_text + (
        f"\nHOOK CLASSIFICATION: this turn is delegation-eligible ({reasons}). Before parent mutation, start at least "
        f"{minimum} {'independent subagents' if minimum > 1 else 'subagent'}.{overlap} If the preferred custom worker "
        "is unavailable, use the cheapest supported alternative or explicitly attempt Agent spawning so runtime failure can be observed."
    )


def is_parent(event: dict[str, Any]) -> bool:
    return not bool(event.get("agent_id"))


def mutating_tool(event: dict[str, Any]) -> bool:
    name = str(event.get("tool_name") or "")
    data = event.get("tool_input") or {}
    if is_agent_tool(name):
        return False
    if name == "apply_patch" or name in {"Edit", "Write"}:
        return True
    if name == "Bash":
        command = str(data.get("command") or "") if isinstance(data, dict) else str(data)
        return bool(shared.MUTATING_BASH.search(command) or re.search(r"(^|[^<])>{1,2}\s*\S", command))
    return bool(shared.MUTATING_TOOL_NAME.search(name))


def handle_prompt(event: dict[str, Any]) -> None:
    session = event.get("session_id")
    correlated = valid_session_id(session)
    # Nothing else in this protocol ever swept old session state, so it grows
    # without bound. Reap once per turn, best effort: a sweep failure must never
    # affect the turn itself.
    try:
        shared.reap_state(state_root(), keep=safe(session) if correlated else "")
    except Exception:
        pass
    previous = load_state(session) if correlated else {}
    c = shared.classify(
        str(event.get("prompt") or ""), previous,
        continuation_prefix=CONTINUATION_PREFIX, context_env=CONTEXT_ENV,
    )
    c["delegation_queue"] = False
    c["delegation_queue_backend"] = None
    c["delegation_queue_strategy"] = None
    c["delegation_queue_virtual_slots"] = 0
    if c.get("requires_multi"):
        queue = select_delegation_queue("codex")
        if queue:
            c["delegation_queue"] = True
            c["delegation_queue_backend"] = queue["backend"]
            c["delegation_queue_strategy"] = queue["strategy"]
            c["delegation_queue_virtual_slots"] = queue["virtual_slots"]
            c["min_agents"] = 1
    if correlated:
        if not c.get("carry_forward"):
            reset_evidence(session)
        save_state(session, {
            "protocol_version": shared.PROTOCOL_VERSION,
            "prompt": str(event.get("prompt") or ""),
            "turn_id": str(event.get("turn_id") or ""),
            **c,
            "completed": False,
        })
    emit({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": policy_context(c)}})


def handle_subagent_start(event: dict[str, Any]) -> None:
    session = event.get("session_id")
    aid = safe(event.get("agent_id") or "unknown-agent")
    d = active_dir(session)
    d.mkdir(parents=True, exist_ok=True)
    touch(d / aid)
    key = worker_key(event.get("agent_id"))
    if key:
        touch(known_dir(session) / key)
    touch(marker(session, "delegated"))
    try:
        if sum(1 for p in d.iterdir() if p.is_file()) >= 2:
            touch(marker(session, "fanout"))
    except FileNotFoundError:
        pass
    emit({"hookSpecificOutput": {"hookEventName": "SubagentStart", "additionalContext": (
        "You are a delegated worker under the delegation protocol. Stay within assigned ownership, avoid unrelated redesign, "
        "run requested validation, and return a concise report of work, files, checks, assumptions, blockers, and uncertainty. "
        "The parent owns cross-subsystem integration and final acceptance."
    )}})


def handle_subagent_stop(event: dict[str, Any]) -> None:
    session = event.get("session_id")
    aid = event.get("agent_id") or "unknown-agent"
    p = active_dir(session) / safe(aid)
    try:
        p.unlink()
    except FileNotFoundError:
        pass

    # The task ended, but the worker itself is still alive until it is dismissed.
    # Only for a worker this protocol launched: the runtime also stops agents of its own,
    # under nameless ids that no dismissal call can name, and charging the parent for those
    # accrues a debt that can never be paid.
    key = worker_key(aid)
    if key and (known_dir(session) / key).exists():
        touch(finished_dir(session) / key)


def handle_agent_result(event: dict[str, Any]) -> None:
    """Clear the matching attempt after Codex reports a successful spawn.

    Codex 0.151.0 only emits PostToolUse for successful tool calls. Failed
    spawns have no delivery here, so their PreToolUse attempt remains as the
    payload-independent fail-open signal. Never inspect worker prose for error
    wording: a successful worker can legitimately quote the same diagnostics.
    """
    session = event.get("session_id")
    call_id = safe(event.get("tool_use_id"))
    if not call_id or call_id == "unknown":
        return
    for after_delegation in (False, True):
        try:
            (pending_attempts_dir(session, after_delegation=after_delegation) / call_id).unlink()
        except FileNotFoundError:
            pass


def has_pending_attempt(session: Any, *, after_delegation: bool) -> bool:
    directory = pending_attempts_dir(session, after_delegation=after_delegation)
    try:
        return any(path.is_file() for path in directory.iterdir())
    except FileNotFoundError:
        return False


def unmet(session: Any, state: dict[str, Any]) -> str | None:
    if (not state.get("requires_delegation") or marker(session, "unavailable").exists()
            or has_pending_attempt(session, after_delegation=False)):
        return None
    minimum = int(state.get("min_agents", 1))
    delegated = marker(session, "delegated").exists()
    fanout = marker(session, "fanout").exists()
    if minimum <= 1:
        if delegated:
            return None
        if state.get("delegation_queue"):
            return (
                "Delegation queue requires one lifecycle-visible `bulk_worker` dispatcher before parent implementation. "
                "Give it all independent units as one ordered batch for mux-scheduler `queue`."
            )
        return (
            "Delegation protocol requires a bounded subagent for this bulk/high-volume turn. Start `bulk_worker` when suitable "
            "or another supported worker before parent implementation."
        )
    if fanout or (delegated and (
            marker(session, "multi-unavailable").exists()
            or has_pending_attempt(session, after_delegation=True))):
        return None
    return (
        "Delegation protocol requires concurrent multi-agent fan-out for this turn. Two independent workers have not yet been "
        "observed running at the same time. Launch separate agents for independent subsystems/shards and let them overlap before parent implementation."
    )


def handle_pretool(event: dict[str, Any]) -> None:
    if not is_parent(event):
        return
    session = event.get("session_id")
    name = str(event.get("tool_name") or "")
    data = event.get("tool_input") if isinstance(event.get("tool_input"), dict) else {}

    # Observe dismissals at intent rather than at outcome, so a failed stop cannot wedge the turn.
    if is_dismissal_tool(name):
        touch(marker(session, "dismissal-tool"))
        for key in ("task_id", "agent_id", "id", "name", "target"):
            k = worker_key(data.get(key))
            if k:
                touch(dismissed_dir(session) / k)
                break
        return

    # Reclaim finished workers before creating new ones.
    if is_agent_tool(name):
        held = outstanding_workers(session)
        if held and marker(session, "dismissal-tool").exists():
            emit({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                         "permissionDecisionReason": dismissal_reason(held, "before spawning another worker")}})
            return
        call_id = safe(event.get("tool_use_id"))
        if call_id and call_id != "unknown":
            after_delegation = marker(session, "delegated").exists()
            touch(pending_attempts_dir(session, after_delegation=after_delegation) / call_id)
        return

    if not mutating_tool(event):
        return
    reason = unmet(session, load_state(session))
    if reason:
        emit({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": reason}})


def handle_stop(event: dict[str, Any]) -> None:
    session = event.get("session_id")
    state = load_state(session)
    reason = unmet(session, state)
    if reason:
        emit({"decision": "block", "reason": f"{CONTINUATION_PREFIX} {reason} Continue the task after satisfying delegation."})
        return

    held = outstanding_workers(session)
    if held:
        # Only enforce once this build has been observed to expose a dismissal tool.
        # Otherwise there is no actionable way to satisfy the requirement; warning
        # about an unavailable primitive only creates permanent, false lifecycle debt.
        # Block at most once either way: a worker the runtime already tore down can
        # never be dismissed, so an unconditional block would loop the stop hook forever.
        nagged = marker(session, "dismissal-nagged")
        if marker(session, "dismissal-tool").exists() and not nagged.exists():
            touch(nagged)
            for name in held:
                touch(nagged_dir(session) / name)
            emit({"decision": "block", "reason": f"{CONTINUATION_PREFIX} {dismissal_reason(held, 'before ending the turn')}"})
            return

        return

    if state:
        state["completed"] = True
        save_state(session, state)


def main() -> int:
    if shared is None:
        # Neither the installed copy nor this clone's own module could be
        # loaded. Emitting nothing and returning success degrades the hook to
        # a no-op for every mode rather than crash or block a turn on a
        # missing dependency.
        return 0
    if len(sys.argv) != 2:
        print("usage: delegation-enforcer.py <prompt|subagent-start|subagent-stop|agent-result|pretool|stop>", file=sys.stderr)
        return 2
    handlers = {
        "prompt": handle_prompt,
        "subagent-start": handle_subagent_start,
        "subagent-stop": handle_subagent_stop,
        "agent-result": handle_agent_result,
        "pretool": handle_pretool,
        "stop": handle_stop,
    }
    handler = handlers.get(sys.argv[1])
    if handler is None:
        print(f"unknown mode: {sys.argv[1]}", file=sys.stderr)
        return 2
    event = read_event()
    if sys.argv[1] != "prompt" and not valid_session_id(event.get("session_id")):
        return 0
    handler(event)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
