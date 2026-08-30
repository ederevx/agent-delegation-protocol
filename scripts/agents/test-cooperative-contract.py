#!/usr/bin/env python3
"""Check the canonical cooperative-v1 schema, fixtures, and runtime validator."""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCHEMA = ROOT / "agents" / "contracts" / "cooperative-v1.schema.json"
FIXTURES = ROOT / "agents" / "contracts" / "cooperative-v1-fixtures.json"
MUX = ROOT / "scripts" / "agents" / "mux-scheduler.py"


def load_mux():
    spec = importlib.util.spec_from_file_location("mux_scheduler", MUX)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def validate_request(value: object) -> None:
    assert isinstance(value, dict)
    assert value.get("schema_version") == 1
    assert value.get("adapter_protocol") == "cooperative-v1"
    operation = value.get("operation")
    assert operation in ("start", "step", "cancel")
    quantum = value.get("quantum")
    assert isinstance(quantum, dict)
    assert quantum.get("unit") == "agent_turn"
    assert isinstance(quantum.get("value"), int)
    assert not isinstance(quantum["value"], bool) and 1 <= quantum["value"] <= 100
    scheduler = value.get("scheduler")
    assert isinstance(scheduler, dict) and scheduler.get("protocol_version") == 1
    capabilities = scheduler.get("capabilities")
    assert isinstance(capabilities, list) and 1 <= len(capabilities) <= 32
    assert len(capabilities) == len(set(capabilities))
    assert "mux-command-execution-v1" in capabilities
    if operation == "start":
        assert isinstance(value.get("task"), dict) and "token" not in value
    else:
        token = value.get("token")
        assert isinstance(token, str) and token and "task" not in value


def cross_check_ci_worker(test_path: Path, mux) -> None:
    spec = importlib.util.spec_from_file_location("ci_worker_tests", test_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(test_path.parent))
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.pop(0)
    with tempfile.TemporaryDirectory(prefix="cooperative-cross-contract-") as temp:
        case = module.Case(Path(temp) / "ci-worker")
        start = {
            "schema_version": 1, "adapter_protocol": "cooperative-v1",
            "operation": "start", "scheduler": module.SCHEDULER,
            "quantum": {"unit": "agent_turn", "value": 4},
            "task": case.task(),
        }
        result, ready = case.cooperative(start)
        assert result.returncode == 0
        mux._cooperative_state(ready, "start")

        step = {
            "schema_version": 1, "adapter_protocol": "cooperative-v1",
            "operation": "step", "scheduler": module.SCHEDULER,
            "quantum": {"unit": "agent_turn", "value": 4},
            "token": ready["token"],
        }
        result, complete = case.cooperative(step)
        assert result.returncode == 0
        mux._cooperative_state(complete, "step")

        result, ready = case.cooperative({**start, "task": case.task("edit")})
        assert result.returncode == 0
        cancel = {
            "schema_version": 1, "adapter_protocol": "cooperative-v1",
            "operation": "cancel", "scheduler": module.SCHEDULER,
            "quantum": {"unit": "agent_turn", "value": 4},
            "token": ready["token"],
        }
        result, cancelled = case.cooperative(cancel)
        assert result.returncode == 0
        mux._cooperative_state(cancelled, "cancel")

        diagnostics = (
            ("{broken", "invalid_task"),
            (json.dumps({**start, "operation": "unsupported"}), "invalid_task"),
            (json.dumps({**start, "schema_version": 2}),
             "unsupported_adapter_contract"),
        )
        for raw, classification in diagnostics:
            result = subprocess.run(
                [sys.executable, str(module.WORKER), "--cooperative"],
                input=raw, text=True, capture_output=True, check=False,
            )
            receipt = json.loads(result.stdout)
            assert result.returncode == 64
            assert receipt["classification"] == classification
            assert "adapter_protocol" not in receipt
            assert "operation" not in receipt


def main() -> None:
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    fixtures = json.loads(FIXTURES.read_text(encoding="utf-8"))
    assert schema["$defs"]["identity"]["properties"]["schema_version"] == {
        "const": fixtures["schema_version"]
    }
    assert schema["$defs"]["identity"]["properties"]["adapter_protocol"] == {
        "const": fixtures["adapter_protocol"]
    }
    for case in fixtures["requests"]:
        assert case["valid"], case["name"]
        validate_request(case["value"])

    try:
        import jsonschema
    except ImportError:
        jsonschema = None
    if jsonschema is not None:
        for section in ("requests", "responses"):
            for case in fixtures[section]:
                try:
                    jsonschema.validate(case["value"], schema)
                except jsonschema.ValidationError:
                    assert not case["valid"], case["name"]
                else:
                    assert case["valid"], case["name"]

    mux = load_mux()
    for case in fixtures["responses"]:
        try:
            mux._cooperative_state(case["value"], case["operation"])
        except mux.InputError:
            assert not case["valid"], case["name"]
        else:
            assert case["valid"], case["name"]

    for case in fixtures["non_contract_diagnostics"]:
        diagnostic = case["value"]
        expected = (
            "unsupported_adapter_contract"
            if case["name"] == "unsupported-contract-marker" else "invalid_task"
        )
        assert diagnostic["classification"] == expected, case["name"]
        assert "adapter_protocol" not in diagnostic, case["name"]
        assert "operation" not in diagnostic, case["name"]
    if len(sys.argv) == 2:
        cross_check_ci_worker(Path(sys.argv[1]).resolve(), mux)
    else:
        assert len(sys.argv) == 1, "usage: test-cooperative-contract.py [ci worker test]"
    print("cooperative-v1 contract tests passed")


if __name__ == "__main__":
    main()
