#!/usr/bin/env python3
"""Regression checks for classifier diagnostics retirement."""
from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


classifier = load_module(
    "delegation_classifier", ROOT / "scripts/agents/delegation-classifier.py"
)
sys.path.insert(0, str(ROOT / "scripts/hosts"))
adapter = load_module("hook_adapter", ROOT / "scripts/hosts/hook_adapter.py")


def test_retired_diagnostics_do_not_affect_classification() -> None:
    prior = {
        "requires_delegation": True,
        "requires_multi": True,
        "completed": False,
        "analysis_signal": True,
        "execution_signal": True,
    }
    decision = classifier.classify("continue", prior)
    assert decision["requires_delegation"] is True
    assert decision["requires_multi"] is True
    assert decision["carry_forward"] is True
    assert "analysis_signal" not in decision
    assert "execution_signal" not in decision
    assert "execution_token_threshold" not in decision


def test_legacy_state_ignores_retired_fields_without_losing_enforcement_state() -> None:
    legacy = {
        "schema_version": 2,
        "requires_delegation": True,
        "requires_multi": True,
        "analysis_signal": True,
        "execution_signal": True,
        "min_agents": 2,
        "mode": "session_release",
        "active": ["active"],
        "finished": ["finished"],
        "concurrent": ["concurrent"],
        "pending_spawns": ["pending"],
        "denied_spawns": ["denied"],
        "observed": ["observed"],
        "peak_active": 3,
        "completed": False,
        "pending_authorization": True,
    }
    with tempfile.TemporaryDirectory(prefix="adp-classifier-") as raw:
        path = Path(raw) / "state.json"
        path.write_text(json.dumps(legacy), encoding="utf-8")
        state = adapter._load(path)
    assert state == {
        "schema_version": 3,
        "requires_delegation": legacy["requires_delegation"],
        "requires_multi": legacy["requires_multi"],
        "min_agents": legacy["min_agents"],
        # Bare schema-2 ids migrate with an unknown (epoch-zero) start time so
        # the staleness sweep frees them instead of leaking the slot forever.
        "concurrent": [{"id": "concurrent", "started_at": 0.0}],
        "pending_spawns": ["pending"],
        "denied_spawns": ["denied"],
        "observed": ["observed"],
        "peak_active": legacy["peak_active"],
        "completed": legacy["completed"],
        "pending_authorization": legacy["pending_authorization"],
    }


def test_routing_policy_covers_native_turn_budgets() -> None:
    policy = classifier.ROUTING_POLICY
    assert "Claude enforces native maxTurns turn budgets" in policy
    assert "the Pi subagent extension enforces maxTurns turn budgets" in policy
    assert "Codex has advisory agentic-turn budgets" in policy
    assert "Before reaching your turn limit" in policy


def test_routing_policy_encourages_concurrent_spawning() -> None:
    """Concurrent spawning up to the cap replaces the old serialize rule."""
    policy = classifier.ROUTING_POLICY
    assert "Spawn independent workers concurrently up to that cap" in policy
    assert ("the enforcer admits independent calls while their combined "
            "reserved footprint fits within the cap") in policy
    assert "parallel tasks inside one call each count against the cap" in policy
    assert "Wait for a running worker to finish before spawning more" not in policy


if __name__ == "__main__":
    test_retired_diagnostics_do_not_affect_classification()
    test_legacy_state_ignores_retired_fields_without_losing_enforcement_state()
    test_routing_policy_covers_native_turn_budgets()
    test_routing_policy_encourages_concurrent_spawning()
