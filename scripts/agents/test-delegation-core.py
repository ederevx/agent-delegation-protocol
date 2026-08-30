#!/usr/bin/env python3
"""Host-neutral classifier and delegation-queue contract tests."""
from __future__ import annotations

import importlib.util
import json
import os
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]


def load(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise AssertionError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_classifier() -> None:
    classifier = load(
        "delegation_classifier", ROOT / "scripts/agents/delegation-classifier.py"
    )
    classify = classifier.classify
    assert classify(
        "Port this parser; expect about 80k tokens of work.", {}
    )["requires_delegation"]
    assert classify(
        "First update the schema, then regenerate the client, then migrate.", {}
    )["requires_delegation"]
    assert not classify("Rename this helper and then update one caller.", {})[
        "requires_delegation"
    ]
    os.environ["CORE_TEST_CONTEXT"] = "1000000"
    try:
        assert not classify(
            "Port this parser; expect about 80k tokens of work.",
            {},
            context_env=("CORE_TEST_CONTEXT",),
        )["requires_delegation"]
    finally:
        os.environ.pop("CORE_TEST_CONTEXT", None)
    assert not classify(
        "First update, then regenerate, then migrate. Do not delegate.", {}
    )["requires_delegation"]


def test_queue_adapter(root: Path) -> None:
    adapter = load("delegation_queue", ROOT / "scripts/agents/delegation_queue.py")
    scheduler = root / "mux-scheduler.py"
    scheduler.write_text(
        "import json\n"
        "def select_queue_backend(catalog, routes, route, runtime, platform=None):\n"
        "    return json.loads((routes.parent / 'selected.json').read_text())\n",
        encoding="utf-8",
    )
    (root / "catalog").mkdir()
    (root / "mux-scheduler.json").write_text("{}", encoding="utf-8")

    def selected(value: Any) -> dict[str, Any] | None:
        (root / "selected.json").write_text(json.dumps(value), encoding="utf-8")
        return adapter.select(root, "codex")

    assert selected({"id": "fifo"}) == {
        "backend": "fifo", "strategy": "fifo", "virtual_slots": 1
    }
    assert selected({
        "id": "rr",
        "queue_policy": {"strategy": "round_robin", "virtual_slots": 4},
    }) == {"backend": "rr", "strategy": "round_robin", "virtual_slots": 4}
    assert selected({"id": "INVALID"}) is None
    assert selected({
        "id": "rr",
        "queue_policy": {"strategy": "round_robin", "virtual_slots": True},
    }) is None
    assert selected({"id": "fifo", "queue_policy": {"strategy": "fifo"}}) is None


def main() -> int:
    test_classifier()
    with tempfile.TemporaryDirectory(prefix="delegation-core-test-") as tmp:
        test_queue_adapter(Path(tmp))
    print("Delegation core self-test: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
