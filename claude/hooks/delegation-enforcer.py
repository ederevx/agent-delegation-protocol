#!/usr/bin/env python3
"""Claude Code delegation protocol enforcement hook.

Modes:
  prompt           Classify the user turn and inject the delegation policy.
  subagent-start   Record actual worker overlap and inject worker constraints.
  subagent-stop    Remove the worker from the active overlap set.
  agent-failure    Record spawn/runtime unavailability for fail-open handling.
  pretool          Block parent mutation before required delegation occurs.
  stop             Prevent completion before required delegation occurs.

Classification is intentionally conservative and deterministic. It is delegated to the
shared `delegation-classifier.py` module (see `load_classifier()` below) so that Claude
and Codex enforce one policy instead of two hand-copies that can drift apart. Everything
below stays host-specific: how state is located, which events map to which mode, and how
a decision is worded back to Claude Code.

The supporting rule is still loaded as a semantic policy layer for cases that cannot be
inferred reliably from a single prompt.
"""
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


def load_classifier() -> Any:
    """Import the shared classifier: installed copy first, then this clone.

    The second candidate matters because this hook is installed as a symlink
    into ~/.claude/hooks, and Path(__file__).resolve() follows it back into
    the clone. If both candidates fail -- the protocol was never installed
    and this file was somehow invoked standalone outside its clone -- the
    hook must not crash or block a turn over a missing module; callers fall
    back to a no-op instead.
    """
    candidates = (
        claude_home() / ".delegation-protocol" / "delegation-classifier.py",
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


def claude_home() -> Path:
    configured = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(configured).expanduser() if configured else Path.home() / ".claude"


# Names of the environment overrides Claude Code exposes for the context window.
# Passed to the shared classifier's window/threshold helpers so both halves read
# the window the same way while each names its own host's variables.
CONTEXT_ENV = ("CLAUDE_CODE_MAX_CONTEXT_TOKENS", "CI_CLAUDE_CONTEXT_WINDOW")

shared = load_classifier()


def select_delegation_queue(runtime: str) -> dict[str, Any] | None:
    """Return safe host-facing queue details, failing closed to normal fan-out."""
    installed = claude_home() / ".delegation-protocol"
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
    root = claude_home() / ".delegation-protocol" / "sessions"
    root.mkdir(parents=True, exist_ok=True)
    return root


def safe_session_id(value: Any) -> str:
    raw = str(value or "unknown")
    return re.sub(r"[^A-Za-z0-9_.-]", "_", raw)[:160] or "unknown"


def turn_base(session_id: Any) -> Path:
    return state_root() / safe_session_id(session_id)


def state_path(session_id: Any) -> Path:
    return turn_base(session_id).with_suffix(".json")


def active_dir(session_id: Any) -> Path:
    return Path(str(turn_base(session_id)) + ".active")


def marker(session_id: Any, name: str) -> Path:
    return Path(str(turn_base(session_id)) + f".{name}")


def load_state(session_id: Any) -> dict[str, Any]:
    path = state_path(session_id)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(data, dict) or not shared.state_is_current(data):
        return {}
    return data


def save_state(session_id: Any, data: dict[str, Any]) -> None:
    path = state_path(session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, indent=2, sort_keys=True) + "\n"
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
        os.replace(tmp, path)
    finally:
        try:
            if os.path.exists(tmp):
                os.unlink(tmp)
        except OSError:
            pass


def reset_evidence(session_id: Any) -> None:
    # Remove legacy dismissal-debt directories during upgrades as well. Current
    # Claude tracking only uses `.active`, but stale debt must not accumulate.
    base = str(turn_base(session_id))
    for directory in (
        active_dir(session_id),
        Path(base + ".finished"),
        Path(base + ".dismissed"),
        Path(base + ".known"),
        Path(base + ".nagged"),
    ):
        if directory.exists():
            shutil.rmtree(directory, ignore_errors=True)
    for name in (
        "delegated",
        "fanout",
        "unavailable",
        "multi-unavailable",
        "dismissal-nagged",  # legacy
    ):
        try:
            marker(session_id, name).unlink()
        except FileNotFoundError:
            pass


def touch(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch(exist_ok=True)


def read_input() -> dict[str, Any]:
    try:
        data = json.load(sys.stdin)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def emit(data: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(data, separators=(",", ":")))


def concurrency_capacity() -> int:
    raw = os.environ.get("CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS")
    if not raw:
        return 20
    try:
        value = int(raw)
        return value if value > 0 else 20
    except ValueError:
        return 20


def policy_context(classification: dict[str, Any]) -> str:
    threshold = int(classification.get("token_threshold") or shared.token_threshold(CONTEXT_ENV))
    base = (
        "DELEGATION PROTOCOL (hook-enforced): preserve the parent frontier model for planning, ambiguity, "
        "integration, conflict resolution, and final validation. For bounded repetitive or high-volume work, "
        "delegate to the cheapest suitable supported subagent. Prefer `bulk-worker` (Haiku) for low-risk "
        "mechanical work; escalate individual units when stronger reasoning is needed. For independent "
        "subsystems/shards, fan out multiple agents concurrently unless this hook explicitly selects delegation "
        "queue. Do not serialize naturally parallel "
        "work. Give workers non-overlapping scope, acceptance criteria, validation commands, and require "
        "concise result reports. The parent remains the single integration authority. Agent teams may be used "
        "when enabled and beneficial, but ordinary subagents remain the required baseline.\n"
        "SIZE AND SHAPE THRESHOLDS (apply these yourself, whether or not this hook flagged the turn): estimate "
        f"the work before starting it. Delegate any task you estimate at {threshold}+ tokens of reading, output, "
        f"and tool traffic ({int(shared.DELEGATION_WINDOW_SHARE * 100)}% of one auto-compact window), and any task that "
        f"runs to {shared.STEP_DELEGATION_THRESHOLD} or more distinct steps. Plan and integrate here; hand the execution "
        "to workers, one bounded unit each, and re-estimate when the work turns out larger than it looked. Keep "
        "only genuinely small, single-step, or tightly coupled work in this conversation."
    )
    if classification.get("requires_delegation"):
        minimum = int(classification.get("min_agents", 1))
        reasons = ", ".join(classification.get("classification_reasons", [])) or "bulk task"
        if classification.get("delegation_queue"):
            if classification.get("delegation_queue_strategy") == "round_robin":
                slots = int(classification["delegation_queue_virtual_slots"])
                return (
                    base
                    + f"\nHOOK CLASSIFICATION: this prompt is delegation-eligible ({reasons}) and round-robin "
                    f"delegation queue selected backend `{classification['delegation_queue_backend']}`, advertising "
                    f"{slots} virtual slots. Before parent mutation, spawn one lifecycle-visible `bulk-worker` "
                    "dispatcher. Give it every independent workstream as one queue batch and explicitly instruct "
                    "it to submit the batch through "
                    "mux-scheduler `queue`. The singular scheduler process round-robins those virtual agents on its "
                    "physical provider lane in bounded waves and owns their concurrent command jobs; host-level "
                    "dispatcher overlap is not required. A queue failure must be "
                    "reported and must never be replayed on a native backend."
                )
            return (
                base
                + f"\nHOOK CLASSIFICATION: this prompt is delegation-eligible ({reasons}) and delegation queue "
                f"selected backend `{classification['delegation_queue_backend']}`. Before parent mutation, spawn "
                "one lifecycle-visible `bulk-worker`. Give it every independent unit as one ordered batch and "
                "explicitly instruct it to submit the batch through mux-scheduler `queue`; host-level worker overlap "
                "is not required. A queue failure must be reported and must never be replayed on a native backend."
            )
        overlap = " Workers must overlap in time." if minimum > 1 else ""
        return (
            base
            + f"\nHOOK CLASSIFICATION: this prompt is delegation-eligible ({reasons}). Before parent mutation, "
            f"spawn at least {minimum} {'independent subagents' if minimum > 1 else 'subagent'} when available."
            + overlap
            + " If spawning is unavailable, attempt it so the hook can observe the runtime failure."
        )
    return base


def is_main_agent(event: dict[str, Any]) -> bool:
    return not bool(event.get("agent_id"))


def tool_is_mutating(event: dict[str, Any]) -> bool:
    tool = str(event.get("tool_name") or "")
    tool_input = event.get("tool_input") or {}
    if tool in {"Edit", "Write", "NotebookEdit", "MultiEdit"}:
        return True
    if tool == "Bash":
        command = str(tool_input.get("command") or "")
        return bool(shared.MUTATING_BASH.search(command) or re.search(r"(^|[^<])>{1,2}\s*\S", command))
    if tool == "PowerShell":
        command = str(tool_input.get("command") or "")
        return bool(shared.MUTATING_POWERSHELL.search(command))
    return False


def is_relayed_message(prompt: str) -> bool:
    return bool(shared.RELAYED_MESSAGE.match(prompt or ""))


def handle_prompt(event: dict[str, Any]) -> None:
    session_id = event.get("session_id")

    # Best effort only: a sweep failure must never affect the turn it happens to
    # run alongside. This is the once-per-turn entry point, so it is also the one
    # place a periodic sweep costs nothing extra to schedule.
    try:
        shared.reap_state(state_root(), keep=safe_session_id(session_id))
    except Exception:
        pass

    previous = load_state(session_id)
    prompt = str(event.get("prompt") or "")

    # Carry the in-flight turn forward rather than reopening it. Only a real user turn
    # re-classifies and clears evidence; a relayed message must not cost the parent the
    # fan-out it already performed, nor demand fresh fan-out for the integration work
    # that reading a worker's report begins.
    if previous and not previous.get("completed") and is_relayed_message(prompt):
        emit(
            {
                "hookSpecificOutput": {
                    "hookEventName": "UserPromptSubmit",
                    "additionalContext": policy_context(previous),
                }
            }
        )
        return

    classification = shared.classify(
        prompt, previous, capacity=concurrency_capacity(), context_env=CONTEXT_ENV
    )
    classification["delegation_queue"] = False
    classification["delegation_queue_backend"] = None
    classification["delegation_queue_strategy"] = None
    classification["delegation_queue_virtual_slots"] = 0
    if classification.get("requires_multi"):
        queue = select_delegation_queue("claude")
        if queue:
            classification["delegation_queue"] = True
            classification["delegation_queue_backend"] = queue["backend"]
            classification["delegation_queue_strategy"] = queue["strategy"]
            classification["delegation_queue_virtual_slots"] = queue["virtual_slots"]
            classification["min_agents"] = 1
    reset_evidence(session_id)
    save_state(
        session_id,
        {
            "protocol_version": shared.PROTOCOL_VERSION,
            "prompt": prompt,
            **classification,
            "completed": False,
        },
    )
    emit(
        {
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": policy_context(classification),
            }
        }
    )


def handle_subagent_start(event: dict[str, Any]) -> None:
    session_id = event.get("session_id")
    agent_id = safe_session_id(event.get("agent_id") or "unknown-agent")
    directory = active_dir(session_id)
    directory.mkdir(parents=True, exist_ok=True)
    touch(directory / agent_id)
    touch(marker(session_id, "delegated"))
    try:
        if sum(1 for p in directory.iterdir() if p.is_file()) >= 2:
            touch(marker(session_id, "fanout"))
    except FileNotFoundError:
        pass

    emit(
        {
            "hookSpecificOutput": {
                "hookEventName": "SubagentStart",
                "additionalContext": (
                    "You are a delegated worker under the delegation protocol. Stay within assigned scope; "
                    "do not redesign unrelated systems. Run requested validation. Report work completed, files "
                    "changed/inspected, checks run, assumptions, failures/blockers, and uncertainty. If this "
                    "bounded assignment itself splits into independent workstreams and Agent is available within "
                    "spawn-depth limits, nested delegation is allowed."
                ),
            }
        }
    )


def handle_subagent_stop(event: dict[str, Any]) -> None:
    session_id = event.get("session_id")
    agent_id = event.get("agent_id") or "unknown-agent"
    path = active_dir(session_id) / safe_session_id(agent_id)
    try:
        path.unlink()
    except FileNotFoundError:
        pass

def handle_agent_failure(event: dict[str, Any]) -> None:
    session_id = event.get("session_id")
    error = str(event.get("error") or "")
    if not shared.SPAWN_UNAVAILABLE.search(error):
        return
    if not marker(session_id, "delegated").exists():
        touch(marker(session_id, "unavailable"))
    else:
        touch(marker(session_id, "multi-unavailable"))


def unmet_reason(session_id: Any, state: dict[str, Any]) -> str | None:
    if not state.get("requires_delegation"):
        return None
    if marker(session_id, "unavailable").exists():
        return None

    minimum = int(state.get("min_agents", 1))
    delegated = marker(session_id, "delegated").exists()
    fanout = marker(session_id, "fanout").exists()

    if minimum <= 1:
        if delegated:
            return None
        if state.get("delegation_queue"):
            return (
                "Delegation queue requires one lifecycle-visible `bulk-worker` before parent implementation. "
                "Give it all independent units as one ordered batch for mux-scheduler `queue`."
            )
        return (
            "Delegation protocol requires a subagent for this bulk/high-volume turn, but none has been started. "
            "Spawn a bounded worker (prefer `bulk-worker` for mechanical work) before parent implementation."
        )

    if fanout:
        return None
    if delegated and marker(session_id, "multi-unavailable").exists():
        return None
    return (
        "Delegation protocol requires concurrent multi-agent fan-out for this turn, but two workers have not "
        "yet been observed running at the same time. Launch separate workers for independent subsystems/shards "
        "and allow them to overlap before parent implementation."
    )


def handle_pretool(event: dict[str, Any]) -> None:
    if not is_main_agent(event):
        return
    session_id = event.get("session_id")
    tool = str(event.get("tool_name") or "")
    if not tool_is_mutating(event):
        return
    reason = unmet_reason(session_id, load_state(session_id))
    if reason:
        emit(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": reason,
                }
            }
        )


def handle_stop(event: dict[str, Any]) -> None:
    session_id = event.get("session_id")
    state = load_state(session_id)
    reason = unmet_reason(session_id, state)
    if reason:
        emit({"decision": "block", "reason": reason + " Do not stop yet; satisfy delegation first."})
        return

    if state:
        state["completed"] = True
        save_state(session_id, state)


def main() -> int:
    # No policy without the module that defines it. Degrade to a no-op rather than
    # crash or block a turn on a missing/broken installation -- the hook is an
    # enforcement guardrail, not a gate that can hold the user's session hostage.
    if shared is None:
        return 0
    if len(sys.argv) != 2:
        print(
            "usage: delegation-enforcer.py <prompt|subagent-start|subagent-stop|agent-failure|pretool|stop>",
            file=sys.stderr,
        )
        return 2
    handlers = {
        "prompt": handle_prompt,
        "subagent-start": handle_subagent_start,
        "subagent-stop": handle_subagent_stop,
        "agent-failure": handle_agent_failure,
        "pretool": handle_pretool,
        "stop": handle_stop,
    }
    handler = handlers.get(sys.argv[1])
    if handler is None:
        print(f"unknown mode: {sys.argv[1]}", file=sys.stderr)
        return 2
    handler(read_input())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
