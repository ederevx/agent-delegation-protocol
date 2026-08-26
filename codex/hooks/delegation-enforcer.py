#!/usr/bin/env python3
"""Codex delegation protocol enforcement hook."""
from __future__ import annotations

import json
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

PROTOCOL_VERSION = 1
CONTINUATION_PREFIX = "DELEGATION_PROTOCOL_CONTINUE:"

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
    r"(^|[;&|]\s*)(rm\b|mv\b|cp\b|mkdir\b|rmdir\b|touch\b|sed\s+-i\b|perl\s+-pi\b|"
    r"git\s+(?:apply|checkout|switch|reset|clean|commit|merge|rebase|cherry-pick)\b|patch\b|tee\b|"
    r"npm\s+(?:install|uninstall|update|ci)\b|pnpm\s+(?:install|add|remove|update)\b|"
    r"yarn\s+(?:install|add|remove|upgrade)\b|pip(?:3)?\s+(?:install|uninstall)\b|"
    r"cargo\s+(?:add|remove|fix|fmt)\b|go\s+fmt\b)", re.IGNORECASE,
)
MUTATING_TOOL_NAME = re.compile(
    r"(?:write|edit|create|update|delete|remove|rename|move|patch|apply|replace|commit|merge|rebase)",
    re.IGNORECASE,
)
SPAWN_UNAVAILABLE = re.compile(
    r"(?:concurrent.*limit|agent.*(?:unavailable|disabled|not available)|subagent.*(?:unavailable|disabled|not available)|"
    r"model not found|no available model|unsupported model|not permitted|permission denied|unknown agent)",
    re.IGNORECASE,
)


def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))).expanduser()


def state_root() -> Path:
    root = codex_home() / ".delegation-protocol" / "hook-state"
    root.mkdir(parents=True, exist_ok=True)
    return root


def safe(value: Any) -> str:
    raw = str(value or "unknown")
    return re.sub(r"[^A-Za-z0-9_.-]", "_", raw)[:180] or "unknown"


def base(session_id: Any) -> Path:
    return state_root() / safe(session_id)


def state_path(session_id: Any) -> Path:
    return Path(str(base(session_id)) + ".json")


def active_dir(session_id: Any) -> Path:
    return Path(str(base(session_id)) + ".active")


def marker(session_id: Any, name: str) -> Path:
    return Path(str(base(session_id)) + f".{name}")


def load_state(session_id: Any) -> dict[str, Any]:
    p = state_path(session_id)
    if not p.exists():
        return {}
    try:
        value = json.loads(p.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


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
    d = active_dir(session_id)
    if d.exists():
        shutil.rmtree(d, ignore_errors=True)
    for name in ("delegated", "fanout", "unavailable", "multi-unavailable"):
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


def explicit_count(text: str) -> int:
    pattern = r"\b(\d{1,4})\s+(?:files?|modules?|packages?|services?|components?|tasks?|items?|tests?|directories|folders?)\b"
    values = [int(m.group(1)) for m in re.finditer(pattern, text, re.IGNORECASE)]
    return max(values, default=0)


def classify(prompt: str, previous: dict[str, Any]) -> dict[str, Any]:
    text = (prompt or "").strip()
    lower = text.lower()
    words = re.findall(r"\b[\w'-]+\b", lower)
    explicit_no = any(re.search(p, lower) for p in NO_DELEGATION_PATTERNS)
    action = any(w in lower for w in ACTION_WORDS)
    count = explicit_count(lower)
    bulk = any(w in lower for w in BULK_WORDS) or count >= 4
    independent = "independent" in lower and any(w in lower for w in SHARD_WORDS)
    multiple = "multiple" in lower and any(w in lower for w in SHARD_WORDS)
    domains = sum(1 for family in (
        ("frontend", "front-end"), ("backend", "back-end"), ("database",),
        ("api",), ("docs", "documentation"), ("tests", "test suite", "test suites"),
    ) if any(x in lower for x in family))
    cross_domain = domains >= 2 and (" and " in lower or "," in lower or "/" in lower or "across" in lower)
    shard = independent or multiple or cross_domain or any(x in lower for x in (
        "independent subsystems", "independent services", "independent modules", "independent packages",
        "separate subsystems", "separate services", "parallel workstreams", "independent workstreams",
    ))
    followup = len(words) <= 14 and any(re.search(p, lower) for p in FOLLOWUP_PATTERNS)
    protocol_continuation = text.startswith(CONTINUATION_PREFIX)
    carry = bool(previous.get("requires_delegation")) and not bool(previous.get("completed")) and (followup or protocol_continuation)
    requires = False if explicit_no else ((action and (bulk or shard)) or carry)
    multi = False if explicit_no else (requires and (shard or bool(previous.get("requires_multi") and carry)))
    reasons: list[str] = []
    if count >= 4:
        reasons.append(f"explicit unit count {count}")
    if bulk and count < 4:
        reasons.append("bulk/high-volume wording")
    if shard:
        reasons.append("independent/separable subsystem wording")
    if carry:
        reasons.append("continuation of unfinished delegated work")
    return {
        "requires_delegation": requires,
        "requires_multi": multi,
        "min_agents": 2 if multi else (1 if requires else 0),
        "classification_reasons": reasons,
        "explicit_no_delegation": explicit_no,
        "carry_forward": carry,
    }


def policy_context(c: dict[str, Any]) -> str:
    base_text = (
        "DELEGATION PROTOCOL (hook-enforced): preserve the frontier parent for planning, ambiguity, difficult reasoning, "
        "integration, conflict resolution, and final validation. Prefer the installed `bulk_worker` custom agent "
        "(GPT-5.6 Luna) for low-risk repetitive/high-volume work and `balanced_worker` (GPT-5.6 Terra) when a delegated "
        "unit needs more reasoning. For independent subsystems/shards, launch multiple workers concurrently rather than "
        "serializing naturally parallel work. Give workers non-overlapping scope, acceptance criteria, validation commands, "
        "and require concise evidence reports. The parent is the single integration authority."
    )
    if not c.get("requires_delegation"):
        return base_text
    reasons = ", ".join(c.get("classification_reasons", [])) or "bulk task"
    minimum = int(c.get("min_agents", 1))
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
    if name == "Agent":
        return False
    if name == "apply_patch" or name in {"Edit", "Write"}:
        return True
    if name == "Bash":
        command = str(data.get("command") or "") if isinstance(data, dict) else str(data)
        return bool(MUTATING_BASH.search(command) or re.search(r"(^|[^<])>{1,2}\s*\S", command))
    return bool(MUTATING_TOOL_NAME.search(name))


def handle_prompt(event: dict[str, Any]) -> None:
    session = event.get("session_id")
    previous = load_state(session)
    c = classify(str(event.get("prompt") or ""), previous)
    if not c.get("carry_forward"):
        reset_evidence(session)
    save_state(session, {
        "version": PROTOCOL_VERSION,
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
    p = active_dir(event.get("session_id")) / safe(event.get("agent_id") or "unknown-agent")
    try:
        p.unlink()
    except FileNotFoundError:
        pass


def handle_agent_result(event: dict[str, Any]) -> None:
    session = event.get("session_id")
    response = event.get("tool_response")
    text = json.dumps(response, ensure_ascii=False) if not isinstance(response, str) else response
    if not SPAWN_UNAVAILABLE.search(text):
        return
    if marker(session, "delegated").exists():
        touch(marker(session, "multi-unavailable"))
    else:
        touch(marker(session, "unavailable"))


def unmet(session: Any, state: dict[str, Any]) -> str | None:
    if not state.get("requires_delegation") or marker(session, "unavailable").exists():
        return None
    minimum = int(state.get("min_agents", 1))
    delegated = marker(session, "delegated").exists()
    fanout = marker(session, "fanout").exists()
    if minimum <= 1:
        return None if delegated else (
            "Delegation protocol requires a bounded subagent for this bulk/high-volume turn. Start `bulk_worker` when suitable "
            "or another supported worker before parent implementation."
        )
    if fanout or (delegated and marker(session, "multi-unavailable").exists()):
        return None
    return (
        "Delegation protocol requires concurrent multi-agent fan-out for this turn. Two independent workers have not yet been "
        "observed running at the same time. Launch separate agents for independent subsystems/shards and let them overlap before parent implementation."
    )


def handle_pretool(event: dict[str, Any]) -> None:
    if not is_parent(event) or not mutating_tool(event):
        return
    session = event.get("session_id")
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
    if state:
        state["completed"] = True
        save_state(session, state)


def main() -> int:
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
    handler(read_event())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
