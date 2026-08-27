#!/usr/bin/env python3
"""Stdlib-only template for a multiplexer command/JSON agent adapter."""
from __future__ import annotations

import json
import sys
import time
from typing import Any

SCHEMA_VERSION = 1
MAX_INPUT_BYTES = 1024 * 1024
MAX_TASKS = 32


class AdapterError(Exception):
    """A deterministic input or backend adapter failure."""


def validate_task(value: Any, index: int = 0) -> dict[str, Any]:
    """Validate the common fields while preserving provider-specific fields."""
    if not isinstance(value, dict):
        raise AdapterError(f"task {index} must be a JSON object")
    task = dict(value)
    prompt = task.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise AdapterError(f"task {index}: prompt must be a non-empty string")
    mode = task.get("mode")
    if mode not in ("read", "edit"):
        raise AdapterError(f"task {index}: mode must be 'read' or 'edit'")
    task_id = task.get("id")
    if task_id is not None and (
        not isinstance(task_id, str) or not task_id or len(task_id) > 128
        or "\0" in task_id or "\n" in task_id
    ):
        raise AdapterError(f"task {index}: id must be a bounded single-line string")
    return task


def load_manifest() -> tuple[list[dict[str, Any]], bool, bool]:
    raw = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
    if len(raw) > MAX_INPUT_BYTES:
        raise AdapterError(f"manifest exceeds {MAX_INPUT_BYTES} bytes")
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise AdapterError(f"invalid manifest JSON: {error}") from error
    if not isinstance(value, dict):
        raise AdapterError("manifest must be a JSON object")
    if "tasks" not in value:
        return [validate_task(value)], False, True
    tasks = value.get("tasks")
    if not isinstance(tasks, list) or not tasks or len(tasks) > MAX_TASKS:
        raise AdapterError(f"tasks must contain 1 to {MAX_TASKS} objects")
    stop_on_error = value.get("stop_on_error", False)
    if not isinstance(stop_on_error, bool):
        raise AdapterError("stop_on_error must be boolean")
    return [validate_task(task, index) for index, task in enumerate(tasks)], stop_on_error, False


def perform_backend_request(task: dict[str, Any]) -> Any:
    """TODO: call the custom API and return its decoded response.

    Read credentials from the environment, apply network timeouts, and keep
    provider-specific request construction here. Never embed a secret in the
    metadata or source file.
    """
    raise AdapterError("custom API call is not configured")


def cooperative_start(task: dict[str, Any], quantum: dict[str, Any]) -> str:
    """TODO: create durable provider state and return its opaque token.

    The token must identify state that a later adapter process can resume; do
    not rely on process memory. Work begins in ``cooperative_step`` so start
    does not consume a scheduler slice.
    """
    raise AdapterError("cooperative custom API start is not configured")


def cooperative_step(token: str, quantum: dict[str, Any]) -> tuple[str, Any]:
    """TODO: resume durable state for at most one quantum."""
    raise AdapterError("cooperative custom API step is not configured")


def cooperative_cancel(token: str, reason: str) -> Any:
    """TODO: cancel provider work and remove durable state idempotently."""
    raise AdapterError("cooperative custom API cancel is not configured")


def normalize_response(task: dict[str, Any], response: Any) -> dict[str, Any]:
    """Convert a provider response into the common JSON receipt envelope."""
    receipt: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "classification": "success",
        "status": "success",
        "response": response,
    }
    if task.get("id") is not None:
        receipt["task_id"] = task["id"]
    return receipt


def execute(task: dict[str, Any]) -> tuple[dict[str, Any], int]:
    started = time.monotonic()
    try:
        receipt = normalize_response(task, perform_backend_request(task))
        status = 0
    except AdapterError as error:
        receipt = {
            "schema_version": SCHEMA_VERSION,
            "classification": "backend_error",
            "status": "backend_error",
            "error": str(error),
        }
        if task.get("id") is not None:
            receipt["task_id"] = task["id"]
        status = 1
    receipt["duration_seconds"] = round(time.monotonic() - started, 3)
    return receipt, status


def execute_cooperative(value: dict[str, Any]) -> tuple[dict[str, Any], int]:
    """Implement the cooperative-v1 start/step/cancel envelope."""
    operation = value.get("operation")
    if operation not in ("start", "step", "cancel"):
        raise AdapterError("operation must be start, step, or cancel")
    quantum = value.get("quantum")
    if (not isinstance(quantum, dict) or quantum.get("unit") != "agent_turn"
            or not isinstance(quantum.get("value"), int)
            or isinstance(quantum.get("value"), bool) or quantum["value"] < 1):
        raise AdapterError("quantum must contain a positive agent_turn value")
    if value.get("adapter_protocol") != "cooperative-v1":
        raise AdapterError("adapter_protocol must be cooperative-v1")
    token = value.get("token")
    if operation != "start" and (
        not isinstance(token, str) or not token or len(token.encode()) > 4096
    ):
        raise AdapterError("step/cancel requires a bounded token")
    try:
        if operation == "start":
            task = validate_task(value.get("task"))
            state, payload = "ready", cooperative_start(task, quantum)
        elif operation == "step":
            state, payload = cooperative_step(token, quantum)
        else:
            payload = cooperative_cancel(token, str(value.get("reason", "cancelled")))
            state = "complete"
        if state not in ("ready", "yielded", "complete"):
            raise AdapterError("cooperative implementation returned an invalid state")
        receipt: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION, "classification": "success",
            "status": "success", "state": state,
        }
        if state in ("ready", "yielded"):
            if not isinstance(payload, str) or not payload:
                raise AdapterError("yielded state requires an opaque string token")
            receipt["token"] = payload
        else:
            receipt["response"] = payload
        return receipt, 0
    except AdapterError as error:
        return ({
            "schema_version": SCHEMA_VERSION, "classification": "backend_error",
            "status": "backend_error", "state": "failed", "error": str(error),
        }, 1)


def main() -> int:
    raw = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
    if len(raw) > MAX_INPUT_BYTES:
        print(json.dumps({"schema_version": SCHEMA_VERSION,
                          "classification": "invalid_task", "status": "invalid_task",
                          "error": f"manifest exceeds {MAX_INPUT_BYTES} bytes"}, sort_keys=True))
        return 64
    try:
        initial = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        print(json.dumps({"schema_version": SCHEMA_VERSION,
                          "classification": "invalid_task", "status": "invalid_task",
                          "error": f"invalid manifest JSON: {error}"}, sort_keys=True))
        return 64
    if isinstance(initial, dict) and "operation" in initial:
        try:
            receipt, status = execute_cooperative(initial)
        except AdapterError as error:
            receipt, status = ({"schema_version": SCHEMA_VERSION,
                                "classification": "invalid_task", "status": "invalid_task",
                                "state": "failed", "error": str(error)}, 64)
        print(json.dumps(receipt, sort_keys=True))
        return status
    try:
        # Reuse the one-shot parser without requiring seekable stdin.
        import io
        original_stdin = sys.stdin
        sys.stdin = io.TextIOWrapper(io.BytesIO(raw), encoding="utf-8")
        try:
            tasks, stop_on_error, single = load_manifest()
        finally:
            sys.stdin = original_stdin
    except AdapterError as error:
        print(json.dumps({
            "schema_version": SCHEMA_VERSION,
            "classification": "invalid_task",
            "status": "invalid_task",
            "error": str(error),
        }, sort_keys=True))
        return 64
    if single:
        receipt, status = execute(tasks[0])
        print(json.dumps(receipt, sort_keys=True))
        return status
    jobs: list[dict[str, Any]] = []
    failures = 0
    for index, task in enumerate(tasks):
        receipt, status = execute(task)
        receipt["queue_index"] = index
        receipt["exit_code"] = status
        jobs.append(receipt)
        if status:
            failures += 1
            if stop_on_error:
                break
    print(json.dumps({
        "schema_version": SCHEMA_VERSION,
        "classification": "success" if not failures else "partial_failure",
        "status": "success" if not failures else "partial_failure",
        "sequential": True,
        "stop_on_error": stop_on_error,
        "counts": {
            "requested": len(tasks),
            "completed": len(jobs),
            "succeeded": len(jobs) - failures,
            "failed": failures,
            "skipped": len(tasks) - len(jobs),
        },
        "jobs": jobs,
    }, sort_keys=True))
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
