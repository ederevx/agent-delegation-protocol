#!/usr/bin/env python3
"""Stdlib-only template for a mux-scheduler command/JSON agent adapter."""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path, PureWindowsPath
from typing import Any

SCHEMA_VERSION = 1
MAX_INPUT_BYTES = 1024 * 1024
MAX_TASKS = 32
INFERENCE_ENV = "AGENT_INFERENCE_CONFIG"
MAX_PERMISSION_COMMANDS = 32


class AdapterError(Exception):
    """A deterministic input or backend adapter failure."""


def unsafe_path_parts(value: str) -> bool:
    """Reject traversal and Git metadata using native and Windows separators."""
    parts = (*Path(value).parts, *PureWindowsPath(value).parts)
    return ".." in parts or any(part.casefold() == ".git" for part in parts)


def has_windows_ads_component(value: str) -> bool:
    """Detect NTFS alternate-data-stream syntax without rejecting POSIX colons."""
    if os.name != "nt":
        return False
    path = PureWindowsPath(value)
    return any(
        ":" in part
        for part in path.parts
        if part not in (path.anchor, path.drive, path.root)
    )


def relative_path(value: Any, index: int) -> str:
    if not isinstance(value, str) or not value or "\0" in value:
        raise AdapterError(
            f"task {index}: allowed_paths entries must be non-empty strings"
        )
    path = Path(value)
    windows_path = PureWindowsPath(value)
    if (
        path.is_absolute()
        or bool(windows_path.anchor or windows_path.drive or windows_path.root)
        or unsafe_path_parts(value)
        or has_windows_ads_component(value)
    ):
        raise AdapterError(f"task {index}: unsafe allowed path: {value!r}")
    return path.as_posix().rstrip("/") or "."


def load_inference_config() -> dict[str, Any] | None:
    """Load the mux-scheduler-validated, provider-neutral inference profile."""
    raw = os.environ.get(INFERENCE_ENV)
    if raw is None:
        return None
    if len(raw.encode("utf-8")) > 4096:
        raise AdapterError(f"{INFERENCE_ENV} exceeds 4096 bytes")
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as error:
        raise AdapterError(f"{INFERENCE_ENV} is invalid JSON: {error}") from error
    if not isinstance(value, dict):
        raise AdapterError(f"{INFERENCE_ENV} must contain an object")
    return value


def validate_task(value: Any, index: int = 0) -> dict[str, Any]:
    """Validate the common fields while preserving provider-specific fields."""
    if not isinstance(value, dict):
        raise AdapterError(f"task {index} must be a JSON object")
    task = dict(value)
    prompt = task.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise AdapterError(f"task {index}: prompt must be a non-empty string")
    if len(prompt.encode("utf-8")) > 48 * 1024:
        raise AdapterError(f"task {index}: prompt exceeds 49152 bytes")
    mode = task.get("mode")
    if mode not in ("read", "edit"):
        raise AdapterError(f"task {index}: mode must be 'read' or 'edit'")
    task_id = task.get("id")
    if task_id is not None and (
        not isinstance(task_id, str) or not task_id or len(task_id) > 128
        or "\0" in task_id or "\n" in task_id
    ):
        raise AdapterError(f"task {index}: id must be a bounded single-line string")
    repo = task.get("repo")
    if (
        not isinstance(repo, str) or not Path(repo).is_absolute() or "\0" in repo
        or unsafe_path_parts(repo) or has_windows_ads_component(repo)
    ):
        raise AdapterError(f"task {index}: repo must be an absolute path")
    task["repo"] = str(Path(repo).resolve())
    allowed = task.get("allowed_paths", [])
    if not isinstance(allowed, list) or len(allowed) > 128:
        raise AdapterError(
            f"task {index}: allowed_paths must be a list of at most 128 paths"
        )
    task["allowed_paths"] = [relative_path(item, index) for item in allowed]
    preapproved = task.get("preapproved_commands", [])
    if not isinstance(preapproved, list) or len(preapproved) > MAX_PERMISSION_COMMANDS:
        raise AdapterError(
            f"task {index}: preapproved_commands must be a list of at most "
            f"{MAX_PERMISSION_COMMANDS} commands"
        )
    if any(
        not isinstance(command, str) or not command.strip() or "\0" in command
        or "\n" in command or len(command.encode("utf-8")) > 4096
        for command in preapproved
    ):
        raise AdapterError(
            f"task {index}: preapproved_commands must contain bounded single-line strings"
        )
    task["preapproved_commands"] = list(dict.fromkeys(preapproved))
    validations = task.get("validation", [])
    if not isinstance(validations, list) or len(validations) > 16:
        raise AdapterError(
            f"task {index}: validation must be a list of at most 16 argv arrays"
        )
    checked: list[list[str]] = []
    for command in validations:
        if not isinstance(command, list) or not command or len(command) > 32:
            raise AdapterError(
                f"task {index}: each validation must be a non-empty argv array "
                "of at most 32 strings"
            )
        if any(
            not isinstance(argument, str) or not argument or "\0" in argument
            or len(argument.encode("utf-8")) > 4096
            for argument in command
        ):
            raise AdapterError(
                f"task {index}: validation arguments must be bounded non-empty strings"
            )
        checked.append(command)
    if mode == "read" and checked:
        raise AdapterError(f"task {index}: validation commands require edit mode")
    task["validation"] = checked
    return task


def parse_input(raw: bytes) -> Any:
    if len(raw) > MAX_INPUT_BYTES:
        raise AdapterError(f"manifest exceeds {MAX_INPUT_BYTES} bytes")
    try:
        return json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise AdapterError(f"invalid manifest JSON: {error}") from error


def load_manifest(value: Any) -> tuple[list[dict[str, Any]], bool, bool]:
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


def perform_backend_request(task: dict[str, Any],
                            inference: dict[str, Any] | None) -> Any:
    """TODO: call the custom API and return its decoded response.

    Read credentials from the environment, apply network timeouts, and keep
    provider-specific request construction here. Translate ``inference`` only
    where the provider has an equivalent control. Never embed a secret in the
    metadata or source file.
    """
    raise AdapterError("custom API call is not configured")


def cooperative_start(task: dict[str, Any], quantum: dict[str, Any],
                      inference: dict[str, Any] | None) -> str:
    """TODO: create durable provider state and return its opaque token.

    The token must identify state that a later adapter process can resume; do
    not rely on process memory. Persist ``inference`` with that state so later
    steps cannot drift. Work begins in ``cooperative_step`` so start does not
    consume a scheduler slice.
    """
    raise AdapterError("cooperative custom API start is not configured")


def cooperative_step(token: str, quantum: dict[str, Any],
                     permission_resolution: dict[str, Any] | None = None
                     ) -> tuple[str, Any]:
    """TODO: resume durable state for at most one quantum.

    To delegate an already-authorized command, return ``permission_required``
    with a bounded request containing ``request_id``, ``tool_name: Bash``,
    ``tool_input``, and ``mux_execution`` with exact ``argv`` and absolute
    ``cwd``. On the next step, consume the correlated ``handled`` resolution.
    Requests without ``mux_execution`` retain the parent permission flow.
    """
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
        receipt = normalize_response(task, perform_backend_request(
            task, load_inference_config()
        ))
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
    identity = {
        "schema_version": SCHEMA_VERSION,
        "adapter_protocol": "cooperative-v1",
        "operation": operation,
    }
    if (value.get("schema_version", SCHEMA_VERSION) != SCHEMA_VERSION
            or value.get("adapter_protocol") != "cooperative-v1"):
        return ({
            "schema_version": SCHEMA_VERSION,
            "classification": "unsupported_adapter_contract",
            "status": "unsupported_adapter_contract",
            "error": "unsupported cooperative adapter contract",
        }, 64)
    quantum = value.get("quantum")
    if (not isinstance(quantum, dict) or set(quantum) != {"unit", "value"}
            or quantum.get("unit") != "agent_turn"
            or not isinstance(quantum.get("value"), int)
            or isinstance(quantum.get("value"), bool)
            or not 1 <= quantum["value"] <= 100):
        raise AdapterError("quantum must contain an agent_turn value from 1 to 100")
    scheduler = value.get("scheduler")
    capabilities = scheduler.get("capabilities") if isinstance(scheduler, dict) else None
    if (not isinstance(scheduler, dict)
            or set(scheduler) != {"protocol_version", "capabilities"}
            or scheduler.get("protocol_version") != 1
            or not isinstance(capabilities, list) or not 1 <= len(capabilities) <= 32
            or any(not isinstance(item, str) or not item
                   or len(item.encode("utf-8")) > 128
                   or "\0" in item for item in capabilities)
            or len(capabilities) != len(set(capabilities))
            or "mux-command-execution-v1" not in capabilities):
        raise AdapterError("scheduler negotiation is unsupported")
    token = value.get("token")
    if operation == "start" and "token" in value:
        raise AdapterError("start must not include a token")
    if operation != "start" and (
        not isinstance(token, str) or not token or len(token.encode()) > 4096
        or "\0" in token
    ):
        raise AdapterError("step/cancel requires a bounded token")
    if operation != "start" and "task" in value:
        raise AdapterError("step/cancel must not include a task")
    try:
        if operation == "start":
            task = validate_task(value.get("task"))
            state, payload = "ready", cooperative_start(
                task, quantum, load_inference_config()
            )
        elif operation == "step":
            state, payload = cooperative_step(
                token, quantum, value.get("permission_resolution")
            )
        else:
            payload = cooperative_cancel(token, str(value.get("reason", "cancelled")))
            state = "cancelled"
        allowed_states = {
            "start": ("ready",),
            "step": ("yielded", "permission_required", "complete"),
            "cancel": ("cancelled",),
        }[operation]
        if state not in allowed_states:
            raise AdapterError("cooperative implementation returned an invalid state")
        receipt: dict[str, Any] = {
            **identity, "classification": "success", "status": "success",
            "state": state,
        }
        if state in ("ready", "yielded"):
            if not isinstance(payload, str) or not payload:
                raise AdapterError("yielded state requires an opaque string token")
            receipt["token"] = payload
        elif state == "permission_required":
            if not isinstance(payload, dict):
                raise AdapterError("permission_required requires a request object")
            receipt["token"] = token
            receipt["request"] = payload
        else:
            receipt["response"] = payload
            receipt["exit_code"] = 0
        return receipt, 0
    except AdapterError as error:
        return ({
            **identity, "classification": "backend_error", "status": "backend_error",
            "state": "failed", "exit_code": 1, "error": str(error),
        }, 1)


def main() -> int:
    raw = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
    try:
        initial = parse_input(raw)
    except AdapterError as error:
        print(json.dumps({"schema_version": SCHEMA_VERSION,
                          "classification": "invalid_task", "status": "invalid_task",
                          "error": str(error)}, sort_keys=True))
        return 64
    if isinstance(initial, dict) and "operation" in initial:
        try:
            receipt, status = execute_cooperative(initial)
        except AdapterError as error:
            operation = initial.get("operation")
            receipt = {
                "schema_version": SCHEMA_VERSION,
                "classification": "invalid_task", "status": "invalid_task",
                "state": "failed", "exit_code": 64, "error": str(error),
            }
            if operation in ("start", "step", "cancel"):
                receipt.update({
                    "adapter_protocol": "cooperative-v1",
                    "operation": operation,
                })
            status = 64
        print(json.dumps(receipt, sort_keys=True))
        return status
    try:
        tasks, stop_on_error, single = load_manifest(initial)
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
