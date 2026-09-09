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

# Execution (mutating work) is reserved for delegated agents by a lower bar
# than the general delegation threshold above: a much smaller share of the
# window is enough to push execution to a worker, on the theory that even a
# "small" edit is cheap to hand off while a genuinely small one still clears
# this bar. Same window-relative reasoning as DELEGATION_WINDOW_SHARE.
EXECUTION_WINDOW_SHARE = 0.05

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

# Open-ended investigation/exploration, as distinct from EVALUATION_WORDS:
# assessing something already understood (review/check/verify) versus digging
# in to understand something that isn't yet. Either one names in-depth
# research that execution should not attempt to do inline.
RESEARCH_WORDS = (
    "investigate", "research", "explore", "dig into", "look into",
    "find out", "figure out", "discover", "uncover", "understand how",
    "understand why", "root cause", "track down", "deep dive",
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

# Any tool that pulls file, search, or command content into the parent's own
# context costs that context the same way whether or not it mutates anything
# -- so it is judged by the tool call itself, independent of how the turn's
# prompt text was classified. Matched loosely by substring, same style as
# MUTATING_TOOL_NAME, since host tool names vary and a name is only one of
# the two signals used -- see `_context_pulling` in hook_adapter.py for the
# command-field signal that covers exec-shaped tools regardless of their
# name. Claude's "Bash" is deliberately excluded from this regex: plain
# (non-mutating) shell execution is exempted from this gate by an explicit
# tool-name check in `_context_pulling` itself, so the parent can run direct
# user orders as shell commands without a worker having started first --
# but only while the turn's `analysis_signal` (above) is false. A turn
# carrying "analysis, review, or verification wording" loses that exemption:
# `_context_pulling` falls through to the command-field signal below, the
# same path Codex's "exec_command" always takes, since analysis work is
# exactly the case this gate exists to cover. A mutating bash command is
# unaffected either way, still caught by the separate `_mutating` check.
CONTEXT_PULLING_TOOL_NAME = re.compile(
    r"(?:read|grep|glob|webfetch|websearch)",
    re.IGNORECASE,
)

# The delegation tool itself (Claude's "Agent"/"Task"). Matched as a tight
# exact name rather than the loose substring style used by
# MUTATING_TOOL_NAME/CONTEXT_PULLING_TOOL_NAME above, since "agent" and
# "task" are common English words/tool-name fragments that would
# false-positive under substring matching against unrelated tool names.
# Used by `_is_worker_session`/pre-mutation handling in hook_adapter.py to
# stop a leaf-tier worker's own session from spawning a further subagent,
# independent of whatever the delegation/context-pulling gates above decide.
AGENT_TOOL_NAME = re.compile(r"^(?:agent|task)$", re.IGNORECASE)

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


def token_threshold(
    env_names: tuple[str, ...] = (), share: float = DELEGATION_WINDOW_SHARE
) -> int:
    """Work at or above this many tokens must be delegated."""
    return max(1, int(context_window(env_names) * share))


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


class _TurnClassifier:
    """One turn's classification, split into signal/decision/reason stages.

    `_compute_signals` extracts every independent wording/size/shape signal
    from the prompt text. `_decide` combines those signals (plus carry-over
    from the previous turn) into the actual requires/multi/analysis/execution
    booleans. `_build_reasons` renders the human-readable explanation of
    `_decide`'s outcome. Each stage only reads state set up by the ones
    before it, matching the original function's top-to-bottom data flow.
    """

    def __init__(self, prompt: str, previous: dict[str, Any],
                 context_env: tuple[str, ...]) -> None:
        self.previous = previous
        self.context_env = context_env
        self.text = (prompt or "").strip()
        self.lower = self.text.lower()
        self.words = re.findall(r"\b[\w'-]+\b", self.lower)

    def _compute_signals(self) -> None:
        lower, words = self.lower, self.words
        self.explicit_no = any(
            re.search(pattern, lower) for pattern in NO_DELEGATION_PATTERNS
        )
        self.action = contains_any(lower, ACTION_WORDS)
        self.evaluation_signal = contains_any(lower, EVALUATION_WORDS)
        self.research_signal = contains_any(lower, RESEARCH_WORDS)
        self.count = explicit_count(lower)
        self.bulk_signal = contains_any(lower, BULK_WORDS) or self.count >= 3
        self.tokens = explicit_tokens(lower)
        self.steps = step_count(lower)
        self.threshold = token_threshold(self.context_env)
        self.execution_threshold = token_threshold(
            self.context_env, EXECUTION_WINDOW_SHARE
        )
        self.token_signal = self.tokens >= self.threshold
        self.size_signal = (
            self.token_signal
            or contains_any(lower, SIZE_WORDS)
            or len(words) >= LONG_BRIEF_WORDS
        )
        self.step_signal = (
            contains_any(lower, MULTI_STEP_WORDS)
            or self.steps >= STEP_DELEGATION_THRESHOLD
        )
        multiple_signal = "multiple" in lower and contains_any(lower, SHARD_WORDS)
        independent_signal = "independent" in lower and contains_any(lower, SHARD_WORDS)

        distinct_domains = sum(
            1 for family in DOMAIN_FAMILIES if any(token in lower for token in family)
        )
        cross_domain_signal = distinct_domains >= 2 and (
            " and " in lower or "," in lower or "/" in lower or "across" in lower
        )

        self.shard_signal = (
            independent_signal
            or multiple_signal
            or cross_domain_signal
            or contains_any(lower, SEPARABLE_PHRASES)
        )

        followup = len(words) <= FOLLOWUP_MAX_WORDS and any(
            re.search(pattern, lower) for pattern in FOLLOWUP_PATTERNS
        )
        self.carry = (
            bool(self.previous.get("requires_delegation"))
            and not bool(self.previous.get("completed"))
            and followup
        )

    def _decide(self) -> None:
        explicit_no, carry = self.explicit_no, self.carry
        self.requires = False if explicit_no else (
            (self.action and (self.bulk_signal or self.shard_signal
                               or self.size_signal or self.step_signal))
            or self.evaluation_signal
            or self.token_signal
            or carry
        )
        self.multi = False if explicit_no else (
            self.requires and (
                self.shard_signal
                or bool(self.previous.get("requires_multi") and carry)
            )
        )
        # Carried forward the same way `multi` is: a short continuation like
        # "continue" carries no analysis wording of its own, but the task it
        # continues is still the analysis task that started it.
        self.analysis = self.evaluation_signal or bool(
            self.previous.get("analysis_signal") and carry
        )
        # Execution clears delegation at a much lower bar than `requires`
        # above: a stated budget at or above EXECUTION_WINDOW_SHARE (rather
        # than the full DELEGATION_WINDOW_SHARE), or in-depth-research
        # wording, pushes even a turn otherwise too small for `requires` to a
        # worker. Anything that already set `requires` clears this lower bar
        # automatically.
        self.execution = False if explicit_no else (
            self.requires or self.research_signal
            or self.tokens >= self.execution_threshold
            or bool(self.previous.get("execution_signal") and carry)
        )
        if not self.requires:
            self.min_agents = 0
        elif self.multi:
            self.min_agents = 2
        else:
            self.min_agents = 1

    def _build_reasons(self) -> list[str]:
        reasons: list[str] = []
        if self.evaluation_signal:
            reasons.append("analysis, review, or verification wording")
        if self.research_signal:
            reasons.append("in-depth research wording")
        if self.count >= 3:
            reasons.append(f"explicit unit count {self.count}")
        if self.bulk_signal and self.count < 3:
            reasons.append("bulk/high-volume wording")
        if self.token_signal:
            reasons.append(
                f"stated budget of {self.tokens} tokens, at or above the "
                f"{self.threshold}-token threshold "
                f"({int(DELEGATION_WINDOW_SHARE * 100)}% of a "
                f"{context_window(self.context_env)}-token window)"
            )
        elif self.size_signal:
            reasons.append("large-task wording or a long, detailed brief")
        if self.step_signal:
            reasons.append(
                f"multi-step work ({self.steps} steps enumerated)"
                if self.steps >= STEP_DELEGATION_THRESHOLD else "multi-step wording"
            )
        if self.shard_signal:
            reasons.append("independent/separable subsystem wording")
        if self.carry:
            reasons.append("continuation of an unfinished delegated turn")
        return reasons

    def classify(self) -> dict[str, Any]:
        self._compute_signals()
        self._decide()
        reasons = self._build_reasons()
        return {
            "requires_delegation": self.requires,
            "requires_multi": self.multi,
            "analysis_signal": self.analysis,
            "execution_signal": self.execution,
            "min_agents": self.min_agents,
            "token_threshold": self.threshold,
            "execution_token_threshold": self.execution_threshold,
            "classification_reasons": reasons,
            "explicit_no_delegation": self.explicit_no,
            "carry_forward": self.carry,
        }


def classify(
    prompt: str,
    previous: dict[str, Any],
    *,
    context_env: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Decide whether a turn must be delegated, and to how many workers."""
    return _TurnClassifier(prompt, previous, context_env).classify()


