#!/usr/bin/env python3
"""Claude Code delegation protocol enforcement hook.

Modes:
  prompt           Classify the user turn and inject the delegation policy.
  subagent-start   Record actual worker overlap and inject worker constraints.
  subagent-stop    Remove the worker from the active set and record it as finished-but-not-dismissed.
  agent-failure    Record spawn/runtime unavailability for fail-open handling.
  pretool          Block parent mutation before required delegation occurs, observe worker
                   dismissals, and block new spawns while finished workers are still held.
  stop             Prevent completion before required delegation occurs.

Classification is intentionally conservative and deterministic. The supporting rule is still loaded
as a semantic policy layer for cases that cannot be inferred reliably from a single prompt.
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

PROTOCOL_VERSION = 6

BULK_WORDS = (
    "bulk", "batch", "high-volume", "high volume", "many files", "many modules",
    "many packages", "many services", "many components", "many tasks", "all files",
    "all modules", "all packages", "all services", "all components", "every file",
    "every module", "every package", "every service", "across the repo",
    "across the repository", "repo-wide", "repository-wide", "codebase-wide",
    "large-scale", "large scale",
)

ACTION_WORDS = (
    "implement", "build", "create", "add", "change", "update", "edit", "modify",
    "fix", "refactor", "migrate", "convert", "rewrite", "rename", "process",
    "analyze", "analyse", "review", "audit", "test", "document", "generate",
    "apply", "replace", "remove", "delete", "format", "lint",
)

SHARD_WORDS = (
    "subsystem", "subsystems", "service", "services", "module", "modules", "package",
    "packages", "component", "components", "directory", "directories", "workstream",
    "workstreams", "shard", "shards", "partition", "partitions", "test suite",
    "test suites", "frontend", "front-end", "backend", "back-end", "api", "database",
    "docs", "documentation",
)

NO_DELEGATION_PATTERNS = (
    r"\bdo not (?:delegate|spawn|use (?:sub)?agents?)\b",
    r"\bdon['’]t (?:delegate|spawn|use (?:sub)?agents?)\b",
    r"\bwithout (?:delegation|subagents?|agents?)\b",
    r"\bno (?:delegation|subagents?|agents?)\b",
)

FOLLOWUP_PATTERNS = (
    r"^\s*(?:yes|ok(?:ay)?|sure|continue|proceed|go ahead|do it|keep going|finish it|same|also)\b",
)

MUTATING_BASH = re.compile(
    r"(^|[;&|]\s*)("
    r"rm\b|mv\b|cp\b|mkdir\b|rmdir\b|touch\b|"
    r"sed\s+-i\b|perl\s+-pi\b|"
    r"git\s+(?:apply|checkout|switch|reset|clean|commit|merge|rebase|cherry-pick)\b|"
    r"patch\b|tee\b|"
    r"npm\s+(?:install|uninstall|update|ci)\b|"
    r"pnpm\s+(?:install|add|remove|update)\b|"
    r"yarn\s+(?:install|add|remove|upgrade)\b|"
    r"pip(?:3)?\s+(?:install|uninstall)\b|"
    r"cargo\s+(?:add|remove|fix|fmt)\b|"
    r"go\s+fmt\b"
    r")",
    re.IGNORECASE,
)

MUTATING_POWERSHELL = re.compile(
    r"\b(?:Set-Content|Add-Content|Out-File|Remove-Item|Move-Item|Copy-Item|"
    r"New-Item|Rename-Item|Set-Item|Clear-Content)\b",
    re.IGNORECASE,
)

SPAWN_UNAVAILABLE = re.compile(
    r"(?:concurrent subagent limit reached|agent tool.*(?:unavailable|disabled|not available)|"
    r"subagent.*(?:unavailable|disabled|not available)|model not found|no available model|"
    r"not permitted|permission denied)",
    re.IGNORECASE,
)


def claude_home() -> Path:
    configured = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(configured).expanduser() if configured else Path.home() / ".claude"


def select_delegation_queue(runtime: str) -> str | None:
    """Return the installed queue backend id, failing closed to normal fan-out."""
    installed = claude_home() / ".delegation-protocol"
    module_path = installed / "multiplexer.py"
    try:
        spec = importlib.util.spec_from_file_location(
            "_installed_delegation_multiplexer", module_path
        )
        if spec is None or spec.loader is None:
            return None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        selected = module.select_queue_backend(
            installed / "catalog",
            installed / "multiplexer.json",
            "bulk",
            runtime,
            platform=None,
        )
        if not isinstance(selected, dict):
            return None
        backend_id = selected.get("id")
        if not isinstance(backend_id, str) or not re.fullmatch(
            r"[a-z0-9][a-z0-9._-]{0,63}", backend_id
        ):
            return None
        return backend_id
    except Exception:
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


def finished_dir(session_id: Any) -> Path:
    return Path(str(turn_base(session_id)) + ".finished")


def dismissed_dir(session_id: Any) -> Path:
    return Path(str(turn_base(session_id)) + ".dismissed")


def known_dir(session_id: Any) -> Path:
    """Workers this protocol actually launched, for the life of the session.

    SubagentStop fires for agents the protocol never started -- runtime internals
    that carry a nameless id no dismissal call can target. Only an agent recorded
    here at SubagentStart may create a dismissal obligation.
    """
    return Path(str(turn_base(session_id)) + ".known")


def nagged_dir(session_id: Any) -> Path:
    """Workers whose outstanding debt has already been reported this turn."""
    return Path(str(turn_base(session_id)) + ".nagged")


def worker_key(value: Any) -> str:
    """Normalize an agent identity so a spawn id and a dismissal target compare equal.

    Runtimes report a worker under an internal id that is not what a dismissal call
    accepts: Claude Code reports `a<name>-<hex>` on SubagentStop while TaskStop takes
    the bare name, and other builds use `name@session`. Only the name is common to
    both, so strip the session suffix here and let matches() handle the rest.
    """
    raw = str(value or "").strip()
    if not raw:
        return ""
    return safe_session_id(raw.split("@", 1)[0].lower())


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
    names = []
    for path in finished.iterdir():
        if not path.is_file():
            continue
        if any(keys_match(path.name, target) for target in targets):
            continue
        names.append((path.stat().st_mtime, path.name))
    return [name for _, name in sorted(names)]


def dismissal_reason(names: list[str], action: str) -> str:
    listed = ", ".join(names)
    return (
        f"Delegation protocol: {len(names)} finished worker(s) are still held and occupying "
        f"subagent capacity ({listed}). A worker stays alive and idle after its task completes; "
        f"it is only released by TaskStop. Dismiss each one with TaskStop (pass the worker name "
        f"as task_id) {action}."
    )


def load_state(session_id: Any) -> dict[str, Any]:
    path = state_path(session_id)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


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
    for directory in (
        active_dir(session_id),
        finished_dir(session_id),
        dismissed_dir(session_id),
        nagged_dir(session_id),
    ):
        if directory.exists():
            shutil.rmtree(directory, ignore_errors=True)
    for name in ("delegated", "fanout", "unavailable", "multi-unavailable", "dismissal-nagged"):
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


def contains_any(text: str, words: tuple[str, ...]) -> bool:
    return any(word in text for word in words)


def explicit_count(text: str) -> int:
    patterns = (
        r"\b(\d{1,4})\s+(?:files?|modules?|packages?|services?|components?|tasks?|items?|tests?|endpoints?|directories|folders?|repos?|repositories)\b",
        r"\b(?:files?|modules?|packages?|services?|components?|tasks?|items?|tests?|endpoints?|directories|folders?)\s*[:=]\s*(\d{1,4})\b",
    )
    values: list[int] = []
    for pattern in patterns:
        for match in re.finditer(pattern, text, re.IGNORECASE):
            try:
                values.append(int(match.group(1)))
            except ValueError:
                pass
    return max(values, default=0)


def concurrency_capacity() -> int:
    raw = os.environ.get("CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS")
    if not raw:
        return 20
    try:
        value = int(raw)
        return value if value > 0 else 20
    except ValueError:
        return 20


def classify(prompt: str, previous: dict[str, Any]) -> dict[str, Any]:
    text = (prompt or "").strip()
    lower = text.lower()
    words = re.findall(r"\b[\w'-]+\b", lower)

    explicit_no = any(re.search(pattern, lower) for pattern in NO_DELEGATION_PATTERNS)
    action = contains_any(lower, ACTION_WORDS)
    count = explicit_count(lower)
    bulk_signal = contains_any(lower, BULK_WORDS) or count >= 4
    multiple_signal = "multiple" in lower and contains_any(lower, SHARD_WORDS)
    independent_signal = "independent" in lower and contains_any(lower, SHARD_WORDS)

    distinct_domains = sum(
        1
        for family in (
            ("frontend", "front-end"), ("backend", "back-end"), ("database",),
            ("api",), ("docs", "documentation"), ("tests", "test suite", "test suites"),
        )
        if any(token in lower for token in family)
    )
    cross_domain_signal = distinct_domains >= 2 and (
        " and " in lower or "," in lower or "/" in lower or "across" in lower
    )

    shard_signal = independent_signal or multiple_signal or cross_domain_signal
    if contains_any(
        lower,
        (
            "independent subsystems", "independent services", "independent modules",
            "independent packages", "separate subsystems", "separate services",
            "separate modules", "separate packages", "parallel workstreams",
            "independent workstreams",
        ),
    ):
        shard_signal = True

    followup = len(words) <= 12 and any(re.search(p, lower) for p in FOLLOWUP_PATTERNS)
    carry = bool(previous.get("requires_delegation")) and not bool(previous.get("completed")) and followup
    requires = False if explicit_no else ((action and (bulk_signal or shard_signal)) or carry)
    multi = False if explicit_no else (
        requires and (shard_signal or bool(previous.get("requires_multi") and carry))
    )

    reasons: list[str] = []
    if count >= 4:
        reasons.append(f"explicit unit count {count}")
    if bulk_signal and count < 4:
        reasons.append("bulk/high-volume wording")
    if shard_signal:
        reasons.append("independent/separable subsystem wording")
    if carry:
        reasons.append("short continuation of an unfinished delegated turn")

    capacity = concurrency_capacity()
    min_agents = 0 if not requires else (2 if multi and capacity >= 2 else 1)
    return {
        "requires_delegation": requires,
        "requires_multi": multi,
        "min_agents": min_agents,
        "concurrency_capacity": capacity,
        "classification_reasons": reasons,
        "explicit_no_delegation": explicit_no,
    }


def policy_context(classification: dict[str, Any]) -> str:
    base = (
        "DELEGATION PROTOCOL (hook-enforced): preserve the parent frontier model for planning, ambiguity, "
        "integration, conflict resolution, and final validation. For bounded repetitive or high-volume work, "
        "delegate to the cheapest suitable supported subagent. Prefer `bulk-worker` (Haiku) for low-risk "
        "mechanical work; escalate individual units when stronger reasoning is needed. For independent "
        "subsystems/shards, fan out multiple agents concurrently unless this hook explicitly selects delegation "
        "queue. Do not serialize naturally parallel "
        "work. Give workers non-overlapping scope, acceptance criteria, validation commands, and require "
        "concise result reports. The parent remains the single integration authority. Agent teams may be used "
        "when enabled and beneficial, but ordinary subagents remain the required baseline."
    )
    if classification.get("requires_delegation"):
        minimum = int(classification.get("min_agents", 1))
        reasons = ", ".join(classification.get("classification_reasons", [])) or "bulk task"
        if classification.get("delegation_queue"):
            return (
                base
                + f"\nHOOK CLASSIFICATION: this prompt is delegation-eligible ({reasons}) and delegation queue "
                f"selected backend `{classification['delegation_queue_backend']}`. Before parent mutation, spawn "
                "one lifecycle-visible `bulk-worker`. Give it every independent unit as one ordered batch and "
                "explicitly instruct it to submit the batch through multiplexer `queue`; host-level worker overlap "
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
        return bool(MUTATING_BASH.search(command) or re.search(r"(^|[^<])>{1,2}\s*\S", command))
    if tool == "PowerShell":
        command = str(tool_input.get("command") or "")
        return bool(MUTATING_POWERSHELL.search(command))
    return False


def handle_prompt(event: dict[str, Any]) -> None:
    session_id = event.get("session_id")
    previous = load_state(session_id)
    classification = classify(str(event.get("prompt") or ""), previous)
    classification["delegation_queue"] = False
    classification["delegation_queue_backend"] = None
    if classification.get("requires_multi"):
        backend_id = select_delegation_queue("claude")
        if backend_id:
            classification["delegation_queue"] = True
            classification["delegation_queue_backend"] = backend_id
            classification["min_agents"] = 1
    reset_evidence(session_id)
    save_state(
        session_id,
        {
            "version": PROTOCOL_VERSION,
            "prompt": str(event.get("prompt") or ""),
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
    key = worker_key(event.get("agent_id"))
    if key:
        touch(known_dir(session_id) / key)
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

    # The task ended, but the worker itself is still alive and idle until it is dismissed.
    # Only for a worker this protocol launched: the runtime also stops agents of its own,
    # under nameless ids that no dismissal call can name, and charging the parent for those
    # accrues a debt that can never be paid.
    key = worker_key(agent_id)
    if key and (known_dir(session_id) / key).exists():
        touch(finished_dir(session_id) / key)


def handle_agent_failure(event: dict[str, Any]) -> None:
    session_id = event.get("session_id")
    error = str(event.get("error") or "")
    if not SPAWN_UNAVAILABLE.search(error):
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
                "Give it all independent units as one ordered batch for multiplexer `queue`."
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
    tool_input = event.get("tool_input") or {}

    # Observe dismissals at intent rather than at outcome. TaskStop against a worker that is
    # already gone still clears the obligation, so a failed call can never wedge the session.
    if tool == "TaskStop":
        key = worker_key(tool_input.get("task_id") or tool_input.get("shell_id"))
        if key:
            touch(dismissed_dir(session_id) / key)
        return

    # Reclaim finished workers before creating new ones.
    if tool == "Agent":
        outstanding = outstanding_workers(session_id)
        if outstanding:
            emit(
                {
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "deny",
                        "permissionDecisionReason": dismissal_reason(
                            outstanding, "before spawning another worker"
                        ),
                    }
                }
            )
        return

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

    outstanding = outstanding_workers(session_id)
    if outstanding:
        # Block once, then let the turn end. A worker that the runtime has already
        # torn down can never be dismissed, so an unconditional block would loop the
        # Stop hook forever on a debt nothing can pay.
        nagged = marker(session_id, "dismissal-nagged")
        if not nagged.exists():
            touch(nagged)
            for name in outstanding:
                touch(nagged_dir(session_id) / name)
            emit(
                {
                    "decision": "block",
                    "reason": dismissal_reason(outstanding, "before ending the turn"),
                }
            )
            return

        # Past the one block, surface only debts not yet reported. Repeating a debt
        # the parent can no longer pay just nags it on every Stop; the spawn gate
        # still holds the obligation either way.
        unreported = [w for w in outstanding if not (nagged_dir(session_id) / w).exists()]
        if not unreported:
            return
        for name in unreported:
            touch(nagged_dir(session_id) / name)
        emit(
            {
                "hookSpecificOutput": {
                    "hookEventName": "Stop",
                    "additionalContext": dismissal_reason(
                        unreported,
                        "if they are still alive; they are released at session end otherwise",
                    ),
                }
            }
        )
        return

    if state:
        state["completed"] = True
        save_state(session_id, state)


def main() -> int:
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
