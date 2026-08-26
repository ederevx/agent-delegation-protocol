#!/usr/bin/env python3
"""Claude Code delegation protocol hook.

Modes:
  prompt           Classify each user prompt, persist enforcement state, inject policy context.
  subagent-start   Track active subagents and inject bounded-worker context.
  subagent-stop    Track active subagents as they finish.
  agent-failure    Record Agent-tool failures and fail open only when spawning is unavailable.
  pretool          Block parent write/mutation tools until required delegation has occurred.
  stop             Prevent the parent from stopping before required delegation has occurred.

The hook uses conservative deterministic classification. The injected policy remains the semantic
layer for cases that cannot be inferred reliably from a single prompt.
"""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any

PROTOCOL_VERSION = 3

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


def state_dir() -> Path:
    root = claude_home() / ".delegation-protocol" / "sessions"
    root.mkdir(parents=True, exist_ok=True)
    return root


def safe_session_id(value: Any) -> str:
    raw = str(value or "unknown")
    return re.sub(r"[^A-Za-z0-9_.-]", "_", raw)[:160] or "unknown"


def state_path(session_id: Any) -> Path:
    return state_dir() / f"{safe_session_id(session_id)}.json"


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
            ("frontend", "front-end"),
            ("backend", "back-end"),
            ("database",),
            ("api",),
            ("docs", "documentation"),
            ("tests", "test suite", "test suites"),
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
    min_agents = 0
    if requires:
        min_agents = 2 if multi and capacity >= 2 else 1

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
        "DELEGATION PROTOCOL (hook-enforced): preserve the parent frontier model for planning, "
        "ambiguity, integration, conflict resolution, and final validation. For bounded repetitive "
        "or high-volume work, delegate to the cheapest suitable supported subagent. Prefer the "
        "`bulk-worker` (Haiku) for low-risk mechanical work; escalate individual units when stronger "
        "reasoning is needed. For independent subsystems/shards, fan out multiple agents concurrently "
        "rather than serializing naturally parallel work. Give workers non-overlapping scope, acceptance "
        "criteria, validation commands, and require concise result reports. The parent remains the single "
        "integration authority. Agent teams may be used when enabled and beneficial, but ordinary subagents "
        "remain the required baseline because they are broadly available."
    )
    if classification.get("requires_delegation"):
        minimum = int(classification.get("min_agents", 1))
        reasons = ", ".join(classification.get("classification_reasons", [])) or "bulk task"
        multi_note = (
            " Reach the required worker count concurrently, not merely sequentially."
            if minimum > 1
            else ""
        )
        return (
            base
            + f"\nHOOK CLASSIFICATION: this prompt is delegation-eligible ({reasons}). "
            f"Before the parent performs mutating implementation work, spawn at least {minimum} "
            f"{'independent subagents' if minimum > 1 else 'subagent'} when the Agent tool is available."
            + multi_note
            + " If delegation is unavailable, attempt it so the hook can detect the runtime failure."
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
    new_state = {
        "version": PROTOCOL_VERSION,
        "prompt": str(event.get("prompt") or ""),
        **classification,
        "subagents_started": 0,
        "active_agent_ids": [],
        "max_concurrent_agents": 0,
        "agent_types": [],
        "agent_failures": 0,
        "delegation_unavailable": False,
        "multi_unavailable": False,
        "completed": False,
    }
    save_state(session_id, new_state)
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
    state = load_state(session_id)
    state.setdefault("version", PROTOCOL_VERSION)
    state["subagents_started"] = int(state.get("subagents_started", 0)) + 1

    active = list(state.get("active_agent_ids") or [])
    agent_id = str(event.get("agent_id") or "")
    if agent_id and agent_id not in active:
        active.append(agent_id)
    state["active_agent_ids"] = active
    state["max_concurrent_agents"] = max(int(state.get("max_concurrent_agents", 0)), len(active))

    types = list(state.get("agent_types") or [])
    agent_type = str(event.get("agent_type") or "unknown")
    types.append(agent_type)
    state["agent_types"] = types[-100:]
    save_state(session_id, state)

    emit(
        {
            "hookSpecificOutput": {
                "hookEventName": "SubagentStart",
                "additionalContext": (
                    "You are a delegated worker under the delegation protocol. Stay within the assigned "
                    "scope; do not redesign unrelated systems. Run requested validation. Report work completed, "
                    "files changed/inspected, checks run, assumptions, failures/blockers, and uncertainty. "
                    "If your assigned task itself splits into genuinely independent workstreams and the Agent "
                    "tool is available within spawn-depth limits, nested delegation is allowed; otherwise complete "
                    "the bounded unit yourself."
                ),
            }
        }
    )


def handle_subagent_stop(event: dict[str, Any]) -> None:
    session_id = event.get("session_id")
    state = load_state(session_id)
    active = list(state.get("active_agent_ids") or [])
    agent_id = str(event.get("agent_id") or "")
    if agent_id in active:
        active.remove(agent_id)
    state["active_agent_ids"] = active
    save_state(session_id, state)


def handle_agent_failure(event: dict[str, Any]) -> None:
    session_id = event.get("session_id")
    state = load_state(session_id)
    state["agent_failures"] = int(state.get("agent_failures", 0)) + 1
    error = str(event.get("error") or "")
    started = int(state.get("subagents_started", 0))
    if started == 0 and SPAWN_UNAVAILABLE.search(error):
        state["delegation_unavailable"] = True
    elif state.get("requires_multi") and started > 0 and SPAWN_UNAVAILABLE.search(error):
        state["multi_unavailable"] = True
    save_state(session_id, state)


def unmet_reason(state: dict[str, Any]) -> str | None:
    if not state.get("requires_delegation"):
        return None
    if state.get("delegation_unavailable"):
        return None

    minimum = int(state.get("min_agents", 1))
    started = int(state.get("subagents_started", 0))
    max_concurrent = int(state.get("max_concurrent_agents", 0))

    if minimum <= 1:
        if started >= 1:
            return None
        return (
            "Delegation protocol requires a subagent for this bulk/high-volume turn, but none has been started. "
            "Spawn a bounded worker (prefer `bulk-worker` for mechanical work) before parent implementation."
        )

    if state.get("multi_unavailable") and started >= 1:
        return None
    if max_concurrent >= minimum:
        return None
    return (
        f"Delegation protocol requires concurrent multi-agent fan-out for this turn: peak concurrency was "
        f"{max_concurrent}/{minimum}. Launch separate workers for independent subsystems/shards and allow them "
        "to overlap (prefer `bulk-worker` for mechanical work) before parent implementation."
    )


def handle_pretool(event: dict[str, Any]) -> None:
    if not is_main_agent(event) or not tool_is_mutating(event):
        return
    state = load_state(event.get("session_id"))
    reason = unmet_reason(state)
    if not reason:
        return
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
    reason = unmet_reason(state)
    if reason:
        emit({"decision": "block", "reason": reason + " Do not stop yet; satisfy delegation first."})
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
    mode = sys.argv[1]
    event = read_input()
    handlers = {
        "prompt": handle_prompt,
        "subagent-start": handle_subagent_start,
        "subagent-stop": handle_subagent_stop,
        "agent-failure": handle_agent_failure,
        "pretool": handle_pretool,
        "stop": handle_stop,
    }
    handler = handlers.get(mode)
    if handler is None:
        print(f"unknown mode: {mode}", file=sys.stderr)
        return 2
    handler(event)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
