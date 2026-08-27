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


def main() -> int:
    try:
        tasks, stop_on_error, single = load_manifest()
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
