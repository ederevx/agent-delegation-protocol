#!/usr/bin/env python3
"""Shared delegation classifier and protocol state contract.

Both enforcement hooks -- `claude/hooks/delegation-enforcer.py` and
`codex/hooks/delegation-enforcer.py` -- import this module instead of carrying
their own copy of the word lists, thresholds, and `classify()` implementation.
Hand-copied classifiers had already drifted apart in ways nobody intended: one
half recognized `endpoints:` counts and the other did not, the follow-up cutoff
differed by two words, and each half had signals the other lacked. One policy
enforced by two agents has to be one implementation.

Everything host-specific stays in the hooks: how state is located, which events
map to which mode, and how a decision is worded back to the host. What lives
here is the decision itself, plus the state-compatibility contract both halves
must agree on.

Installed as `<agent home>/.delegation-protocol/delegation-classifier.py`.
"""
from __future__ import annotations

import os
import re
import shutil
import time
from pathlib import Path
from typing import Any

# Hook state uses the protocol-v2 schema and is discarded on any mismatch.
PROTOCOL_VERSION = 2

# How long an untouched v2 session JSON document may sit before a sweep removes
# it. Host adapters own no sidecar marker directories.
STATE_TTL_SECONDS = 7 * 24 * 3600

STATE_ENTRY_SUFFIXES = (".json",)

BULK_WORDS = (
    "bulk", "batch", "high-volume", "high volume", "many files", "many modules",
    "many packages", "many services", "many components", "many tasks", "all files",
    "all modules", "all packages", "all services", "all components", "every file",
    "every module", "every package", "every service", "across the repo",
    "across the repository", "repo-wide", "repository-wide", "codebase-wide",
    "large-scale", "large scale",
)

# Size and shape thresholds. A turn is delegation-eligible on how much work it
# is, not only on how the user worded it: a job that will burn a large share of
# one compaction window, or that runs through several distinct steps, is cheaper
# and safer to plan in the parent and execute in workers. The size threshold is
# a share of the window rather than a fixed count, so a session configured for a
# larger or smaller window keeps the same meaning.
DELEGATION_WINDOW_SHARE = 0.25
DEFAULT_CONTEXT_WINDOW = 200_000
STEP_DELEGATION_THRESHOLD = 3
LONG_BRIEF_WORDS = 150

# A continuation is short by construction. The two halves disagreed here (12
# words against 14); the longer cutoff wins because carry-forward only ever
# fires when the previous turn already required delegation and did not finish,
# so the more permissive reading keeps enforcement on rather than dropping it.
FOLLOWUP_MAX_WORDS = 14

TOKEN_BUDGET = re.compile(
    r"\b(\d+(?:[.,]\d+)*)\s*([km])?\b[\s-]*(?:tokens?|token budget)\b", re.IGNORECASE
)

SIZE_WORDS = (
    "large task", "big task", "huge", "massive", "extensive", "comprehensive",
    "exhaustive", "end-to-end", "end to end", "entire repo", "entire repository",
    "entire codebase", "whole repo", "whole repository", "whole codebase",
    "from scratch", "overhaul", "rearchitect", "re-architect", "long-running",
    "long running", "sweep",
)

MULTI_STEP_WORDS = (
    "multi-step", "multi step", "many steps", "several steps", "multiple steps",
    "step by step", "step-by-step", "each step", "series of steps", "sequence of steps",
    "multiple phases", "several phases", "in stages", "one step at a time",
    "multi-stage", "multi stage",
)

STEP_MARKERS = (
    r"\bfirst(?:ly)?\b", r"\bsecond(?:ly)?\b", r"\bthird(?:ly)?\b", r"\bfourth\b",
    r"\bfifth\b", r"\bthen\b", r"\bnext\b", r"\bafter (?:that|which|this)\b",
    r"\bafterwards?\b", r"\bonce (?:that|it|this)\b", r"\bfollowed by\b",
    r"\bfinally\b", r"\blastly\b",
)

ENUMERATION = re.compile(r"(?m)^\s*(?:\d+[.)]|step\s+\d+\b|[-*•]\s+\S)")

ACTION_WORDS = (
    "implement", "build", "create", "add", "change", "update", "edit", "modify",
    "fix", "refactor", "migrate", "convert", "rewrite", "rename", "process",
    "analyze", "analyse", "review", "audit", "test", "document", "generate",
    "apply", "replace", "remove", "delete", "format", "lint",
)

EVALUATION_WORDS = (
    "analyze", "analyse", "review", "audit", "check", "evaluate", "inspect",
    "verify", "diagnose",
)

SHARD_WORDS = (
    "subsystem", "subsystems", "service", "services", "module", "modules", "package",
    "packages", "component", "components", "directory", "directories", "workstream",
    "workstreams", "shard", "shards", "partition", "partitions", "test suite",
    "test suites", "frontend", "front-end", "backend", "back-end", "api", "database",
    "docs", "documentation",
)

SEPARABLE_PHRASES = (
    "independent subsystems", "independent services", "independent modules",
    "independent packages", "separate subsystems", "separate services",
    "separate modules", "separate packages", "parallel workstreams",
    "independent workstreams",
)

DOMAIN_FAMILIES = (
    ("frontend", "front-end"), ("backend", "back-end"), ("database",),
    ("api",), ("docs", "documentation"), ("tests", "test suite", "test suites"),
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

COUNT_PATTERNS = (
    r"\b(\d{1,4})\s+(?:files?|modules?|packages?|services?|components?|tasks?|items?|tests?"
    r"|endpoints?|directories|folders?|repos?|repositories)\b",
    r"\b(?:files?|modules?|packages?|services?|components?|tasks?|items?|tests?|endpoints?"
    r"|directories|folders?)\s*[:=]\s*(\d{1,4})\b",
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

MUTATING_TOOL_NAME = re.compile(
    r"(?:write|edit|create|update|delete|remove|rename|move|patch|apply|replace|commit|merge|rebase)",
    re.IGNORECASE,
)

# A turn opened by a relayed worker or peer message continues the obligations of
# the turn already in flight. Its text is a worker's words, not the user's, so
# classifying it would judge a report as if the user had typed it, and resetting
# evidence would discard fan-out already performed for work still in progress.
# `<task-notification` covers a background task's own completion report, which
# reaches the host the same way a relayed agent/teammate message does.
RELAYED_MESSAGE = re.compile(
    r"\s*(?:<(agent|teammate|cross-session)-message\b|<task-notification\b|Stop hook feedback:)",
    re.IGNORECASE,
)

# The union of what each half recognized as "spawning is not available here",
# so a runtime that reports unavailability in one host's wording still causes
# the other to fail open rather than block the turn forever.
SPAWN_UNAVAILABLE = re.compile(
    r"(?:concurrent.*limit|agent(?: tool)?.*(?:unavailable|disabled|not available)|"
    r"subagent.*(?:unavailable|disabled|not available)|model not found|no available model|"
    r"unsupported model|not permitted|permission denied|unknown agent)",
    re.IGNORECASE,
)

def contains_any(text: str, words: tuple[str, ...]) -> bool:
    return any(word in text for word in words)


def explicit_count(text: str) -> int:
    """Largest unit count the turn itself names, however it is written."""
    values: list[int] = []
    for pattern in COUNT_PATTERNS:
        for match in re.finditer(pattern, text, re.IGNORECASE):
            try:
                values.append(int(match.group(1)))
            except ValueError:
                pass
    return max(values, default=0)


def context_window(env_names: tuple[str, ...] = ()) -> int:
    """Tokens the parent can hold before compaction, as configured."""
    for name in env_names:
        raw = os.environ.get(name)
        if not raw:
            continue
        try:
            value = int(raw)
        except ValueError:
            continue
        if value > 0:
            return value
    return DEFAULT_CONTEXT_WINDOW


def token_threshold(env_names: tuple[str, ...] = ()) -> int:
    """Work at or above this many tokens must be delegated."""
    return max(1, int(context_window(env_names) * DELEGATION_WINDOW_SHARE))


def explicit_tokens(text: str) -> int:
    """Largest token budget the turn itself names, in tokens."""
    scale = {"k": 1_000, "m": 1_000_000}
    values: list[int] = []
    for match in TOKEN_BUDGET.finditer(text):
        try:
            amount = float(match.group(1).replace(",", ""))
        except ValueError:
            continue
        values.append(int(amount * scale.get((match.group(2) or "").lower(), 1)))
    return max(values, default=0)


def step_count(text: str) -> int:
    """How many distinct steps the turn enumerates, by ordering words or by list.

    Both readings are counted and the larger wins: a numbered brief and a prose
    "first ... then ... finally" describe the same multi-step shape.
    """
    ordered = sum(len(re.findall(pattern, text)) for pattern in STEP_MARKERS)
    if ordered and not re.search(r"\bfirst(?:ly)?\b", text):
        # "X, then Y, then Z" names two connectors but describes three steps.
        # A brief that opens with "first" already labels its own first step.
        ordered += 1
    listed = len(ENUMERATION.findall(text))
    return max(ordered, listed)


def classify(
    prompt: str,
    previous: dict[str, Any],
    *,
    capacity: int | None = None,
    continuation_prefix: str | None = None,
    context_env: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Decide whether a turn must be delegated, and to how many workers.

    `capacity` is the host's concurrent-subagent limit when it has one, and
    caps `min_agents` so the hook never demands more workers than the runtime
    will start. `continuation_prefix` marks a turn the protocol itself relayed,
    which continues the previous turn's obligations regardless of length.
    """
    text = (prompt or "").strip()
    lower = text.lower()
    words = re.findall(r"\b[\w'-]+\b", lower)

    explicit_no = any(re.search(pattern, lower) for pattern in NO_DELEGATION_PATTERNS)
    action = contains_any(lower, ACTION_WORDS)
    evaluation_signal = contains_any(lower, EVALUATION_WORDS)
    count = explicit_count(lower)
    bulk_signal = contains_any(lower, BULK_WORDS) or count >= 3
    tokens = explicit_tokens(lower)
    steps = step_count(lower)
    threshold = token_threshold(context_env)
    token_signal = tokens >= threshold
    size_signal = (
        token_signal
        or contains_any(lower, SIZE_WORDS)
        or len(words) >= LONG_BRIEF_WORDS
    )
    step_signal = (
        contains_any(lower, MULTI_STEP_WORDS) or steps >= STEP_DELEGATION_THRESHOLD
    )
    multiple_signal = "multiple" in lower and contains_any(lower, SHARD_WORDS)
    independent_signal = "independent" in lower and contains_any(lower, SHARD_WORDS)

    distinct_domains = sum(
        1 for family in DOMAIN_FAMILIES if any(token in lower for token in family)
    )
    cross_domain_signal = distinct_domains >= 2 and (
        " and " in lower or "," in lower or "/" in lower or "across" in lower
    )

    shard_signal = (
        independent_signal
        or multiple_signal
        or cross_domain_signal
        or contains_any(lower, SEPARABLE_PHRASES)
    )

    followup = len(words) <= FOLLOWUP_MAX_WORDS and any(
        re.search(pattern, lower) for pattern in FOLLOWUP_PATTERNS
    )
    relayed = bool(continuation_prefix) and text.startswith(continuation_prefix)
    carry = (
        bool(previous.get("requires_delegation"))
        and not bool(previous.get("completed"))
        and (followup or relayed)
    )
    requires = False if explicit_no else (
        (action and (bulk_signal or shard_signal or size_signal or step_signal))
        or evaluation_signal
        or token_signal
        or carry
    )
    multi = False if explicit_no else (
        requires and (shard_signal or bool(previous.get("requires_multi") and carry))
    )

    reasons: list[str] = []
    if evaluation_signal:
        reasons.append("analysis, review, or verification wording")
    if count >= 3:
        reasons.append(f"explicit unit count {count}")
    if bulk_signal and count < 3:
        reasons.append("bulk/high-volume wording")
    if token_signal:
        reasons.append(
            f"stated budget of {tokens} tokens, at or above the {threshold}-token "
            f"threshold ({int(DELEGATION_WINDOW_SHARE * 100)}% of a "
            f"{context_window(context_env)}-token window)"
        )
    elif size_signal:
        reasons.append("large-task wording or a long, detailed brief")
    if step_signal:
        reasons.append(
            f"multi-step work ({steps} steps enumerated)"
            if steps >= STEP_DELEGATION_THRESHOLD else "multi-step wording"
        )
    if shard_signal:
        reasons.append("independent/separable subsystem wording")
    if carry:
        reasons.append("continuation of an unfinished delegated turn")

    if not requires:
        min_agents = 0
    elif multi and (capacity is None or capacity >= 2):
        min_agents = 2
    else:
        min_agents = 1

    result: dict[str, Any] = {
        "requires_delegation": requires,
        "requires_multi": multi,
        "min_agents": min_agents,
        "token_threshold": threshold,
        "classification_reasons": reasons,
        "explicit_no_delegation": explicit_no,
        "carry_forward": carry,
    }
    if capacity is not None:
        result["concurrency_capacity"] = capacity
    return result


def state_is_current(data: Any) -> bool:
    """Whether stored turn state was written by this version of the protocol.

    State that predates the running protocol is discarded rather than migrated.
    It describes one turn's delegation evidence, so the cost of dropping it is a
    re-classification, while the cost of misreading a renamed or re-meant key is
    enforcing the wrong policy for the rest of the session.
    """
    return isinstance(data, dict) and data.get("protocol_version") == PROTOCOL_VERSION


def reap_state(root: Path, keep: str = "", ttl: int = STATE_TTL_SECONDS) -> int:
    """Delete session state untouched for `ttl` seconds. Returns files removed.

    `keep` names the session currently running, which is never swept regardless
    of age -- a long session's state is old by mtime while still being the state
    in force. Sweeping is best effort: a racing hook may be writing the very
    entry being removed, and losing that race costs a re-classification.
    """
    if not root.is_dir():
        return 0
    cutoff = time.time() - max(0, ttl)
    removed = 0
    def belongs_to(name: str, session: str) -> bool:
        return any(name == session + suffix for suffix in STATE_ENTRY_SUFFIXES) \
            or name.startswith(session + ".json.")

    for path in sorted(root.iterdir()):
        if keep and belongs_to(path.name, keep):
            continue
        try:
            if path.stat().st_mtime >= cutoff:
                continue
        except OSError:
            continue
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
            removed += 1
        else:
            try:
                path.unlink()
                removed += 1
            except OSError:
                pass
    return removed
