#!/usr/bin/env python3
"""Provider-neutral protocol-v2 catalog, scheduler, and lane CLI."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from lane_service import LaneClient, LaneError, LaneServer

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CATALOG = ROOT / "agents" / "protocol-v2.json"
TERMINAL = {"completed", "failed", "cancelled", "permission_required"}
RECEIPT_STATUSES = {
    "native_required", "ready", "yielded", "permission_required",
    "completed", "failed", "cancelled",
}


class ProtocolError(ValueError):
    """A stable configuration or request error."""


def _json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ProtocolError(f"cannot read JSON {path}: {error}") from error


def _string_list(value: Any, field: str, *, allow_empty: bool = False) -> list[str]:
    if (not isinstance(value, list) or
            (not value and not allow_empty) or
            any(not isinstance(item, str) or not item for item in value) or
            len(value) != len(set(value))):
        raise ProtocolError(f"{field} must be a unique string array")
    return value


def _validate_backend(backend: Any) -> dict[str, Any]:
    if not isinstance(backend, dict):
        raise ProtocolError("backend must be an object")
    required = {
        "id", "name", "kind", "priority", "selector", "availability",
        "execution", "lane",
    }
    if set(backend) != required:
        raise ProtocolError(f"backend {backend.get('id', '?')}: exact v2 fields required")
    backend_id = backend["id"]
    if not isinstance(backend_id, str) or not backend_id:
        raise ProtocolError("backend.id is required")
    if backend["kind"] not in {"native", "oneshot", "session"}:
        raise ProtocolError(f"{backend_id}: invalid kind")
    if (not isinstance(backend["priority"], int) or
            not 0 <= backend["priority"] <= 100):
        raise ProtocolError(f"{backend_id}: invalid priority")
    selector = backend["selector"]
    if not isinstance(selector, dict) or set(selector) != {
        "runtimes", "platforms", "modes", "workspaces", "functions"
    }:
        raise ProtocolError(f"{backend_id}: invalid selector")
    for key in sorted(selector):
        _string_list(selector[key], f"{backend_id}.selector.{key}")
    availability = backend["availability"]
    if not isinstance(availability, dict) or set(availability) != {
        "commands", "environment"
    }:
        raise ProtocolError(f"{backend_id}: invalid availability")
    for key in sorted(availability):
        _string_list(
            availability[key],
            f"{backend_id}.availability.{key}",
            allow_empty=True,
        )
    execution = backend["execution"]
    if not isinstance(execution, dict):
        raise ProtocolError(f"{backend_id}: invalid execution")
    allowed_execution = {"delivery", "argv", "timeout_seconds", "max_steps"}
    if not set(execution) <= allowed_execution or "delivery" not in execution:
        raise ProtocolError(f"{backend_id}: invalid execution fields")
    expected = "native" if backend["kind"] == "native" else "json"
    if execution["delivery"] != expected:
        raise ProtocolError(f"{backend_id}: kind/delivery mismatch")
    if expected == "json":
        _string_list(execution.get("argv"), f"{backend_id}.execution.argv")
    if ("timeout_seconds" in execution and
            (not isinstance(execution["timeout_seconds"], int) or
             execution["timeout_seconds"] < 1)):
        raise ProtocolError(f"{backend_id}: invalid timeout_seconds")
    if ("max_steps" in execution and
            (not isinstance(execution["max_steps"], int) or
             not 1 <= execution["max_steps"] <= 10_000)):
        raise ProtocolError(f"{backend_id}: invalid max_steps")
    lane = backend["lane"]
    if (not isinstance(lane, dict) or set(lane) != {
            "id", "max_concurrency", "lease_seconds"} or
            not isinstance(lane["id"], str) or not lane["id"] or
            not isinstance(lane["max_concurrency"], int) or
            lane["max_concurrency"] < 1 or
            not isinstance(lane["lease_seconds"], int) or
            lane["lease_seconds"] < 1):
        raise ProtocolError(f"{backend_id}: invalid scheduler lane")
    return backend


def load_catalog(path: Path) -> dict[str, Any]:
    path = path.resolve()
    value = _json(path)
    if not isinstance(value, dict) or value.get("schema_version") != 2:
        raise ProtocolError("catalog must declare schema_version 2")
    if not set(value) <= {"schema_version", "backends", "routes", "includes"}:
        raise ProtocolError("catalog contains unsupported fields")
    backends = value.get("backends")
    routes = value.get("routes")
    includes = value.get("includes", [])
    if not isinstance(backends, list) or not backends:
        raise ProtocolError("backends must be a non-empty array")
    _string_list(includes, "includes", allow_empty=True)
    combined = list(backends)
    for include in includes:
        include_path = (path.parent / include).resolve()
        document = _json(include_path)
        if (not isinstance(document, dict) or
                document.get("schema_version") != 2 or
                set(document) != {"schema_version", "backends"} or
                not isinstance(document["backends"], list)):
            raise ProtocolError(f"invalid integration catalog: {include}")
        combined.extend(document["backends"])
    validated = [_validate_backend(backend) for backend in combined]
    by_id = {backend["id"]: backend for backend in validated}
    if len(by_id) != len(validated):
        raise ProtocolError("backend ids must be unique")
    if not isinstance(routes, dict) or not routes:
        raise ProtocolError("routes must be a non-empty object")
    for route, members in routes.items():
        if not isinstance(route, str) or not route:
            raise ProtocolError("route names must be non-empty strings")
        _string_list(members, f"route {route}")
        unknown = [member for member in members if member not in by_id]
        if unknown:
            raise ProtocolError(f"route {route}: unknown backend {unknown[0]}")
    return {**value, "backends": validated, "by_id": by_id}


def _available(backend: dict[str, Any]) -> bool:
    checks = backend["availability"]
    if any(shutil.which(command) is None for command in checks["commands"]):
        return False
    if any(not os.environ.get(name) for name in checks["environment"]):
        return False
    return True


def select_backend(catalog: dict[str, Any], request: dict[str, Any]) -> dict[str, Any]:
    route = request.get("route")
    members = catalog["routes"].get(route)
    if members is None:
        raise ProtocolError(f"unknown route: {route}")
    matches = []
    for backend_id in members:
        backend = catalog["by_id"][backend_id]
        selector = backend["selector"]
        if all(request.get(key[:-1]) in selector[key] for key in (
            "runtimes", "platforms", "modes", "workspaces", "functions"
        )) and _available(backend):
            matches.append(backend)
    if not matches:
        raise ProtocolError("no_backend")
    return sorted(matches, key=lambda item: (-item["priority"], item["id"]))[0]


def validate_task(task: Any) -> dict[str, Any]:
    if not isinstance(task, dict):
        raise ProtocolError("task must be an object")
    allowed = {
        "schema_version", "id", "mode", "repo", "prompt", "allowed_paths",
        "workspace", "validation", "budgets",
    }
    required = {"schema_version", "id", "mode", "repo", "prompt",
                "allowed_paths", "workspace", "validation", "budgets"}
    if set(task) != required or not set(task) <= allowed:
        raise ProtocolError("task must contain the exact protocol-v2 fields")
    if task["schema_version"] != 2:
        raise ProtocolError("task schema_version must be 2")
    if task["mode"] not in {"read", "edit"}:
        raise ProtocolError("task.mode must be read or edit")
    for field in ("id", "repo", "prompt"):
        if not isinstance(task[field], str) or not task[field].strip():
            raise ProtocolError(f"task.{field} is required")
    _string_list(task["allowed_paths"], "task.allowed_paths", allow_empty=True)
    if task["workspace"] not in {"shared", "isolated"}:
        raise ProtocolError("task.workspace is invalid")
    validation = task["validation"]
    if (not isinstance(validation, list) or
            any(not isinstance(command, list) or not command or
                any(not isinstance(part, str) or not part for part in command)
                for command in validation)):
        raise ProtocolError("task.validation must contain argv arrays")
    budgets = task["budgets"]
    if (not isinstance(budgets, dict) or set(budgets) != {
            "timeout_seconds", "max_output_bytes", "max_steps"} or
            any(not isinstance(value, int) or value < 1
                for value in budgets.values())):
        raise ProtocolError("task.budgets is invalid")
    return task


def validate_request(value: Any, operation: str) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("schema_version") != 2:
        raise ProtocolError("request must declare schema_version 2")
    if operation == "resume":
        required = {"schema_version", "backend", "token", "resolution"}
        if set(value) != required:
            raise ProtocolError("resume request fields are invalid")
        if not all(isinstance(value[key], str) and value[key]
                   for key in ("backend", "token")):
            raise ProtocolError("resume backend and token are required")
        if not isinstance(value["resolution"], dict):
            raise ProtocolError("resume resolution must be an object")
        return value
    common = {
        "schema_version", "route", "runtime", "platform", "function",
        "mode", "workspace",
    }
    task_field = "tasks" if operation == "batch" else "task"
    if set(value) != common | {task_field}:
        raise ProtocolError(f"{operation} request fields are invalid")
    for key in common - {"schema_version"}:
        if not isinstance(value[key], str) or not value[key]:
            raise ProtocolError(f"request.{key} is required")
    if operation == "batch":
        if not isinstance(value["tasks"], list) or not value["tasks"]:
            raise ProtocolError("batch tasks must be non-empty")
        value["tasks"] = [validate_task(task) for task in value["tasks"]]
    else:
        value["task"] = validate_task(value["task"])
    return value


def receipt(status: str, classification: str, **fields: Any) -> dict[str, Any]:
    if status not in RECEIPT_STATUSES:
        raise ProtocolError(f"invalid receipt status: {status}")
    return {"schema_version": 2, "status": status,
            "classification": classification, **fields}


def _lane_state_dir() -> Path:
    configured = os.environ.get("DELEGATION_LANE_STATE_DIR")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".delegation-protocol" / "lane"


def ensure_lane_service(state_dir: Path) -> LaneClient:
    endpoint = state_dir / "lane.json"
    try:
        client = LaneClient(endpoint, 1)
        if client.request("status").get("status") == "completed":
            return client
    except (OSError, ValueError, LaneError, json.JSONDecodeError):
        pass
    command = [sys.executable, str(Path(__file__).resolve()), "lane", "serve",
               "--state-dir", str(state_dir)]
    options: dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": os.name != "nt",
    }
    if os.name == "nt":
        options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
    else:
        options["start_new_session"] = True
    subprocess.Popen(command, **options)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            client = LaneClient(endpoint, 1)
            if client.request("status").get("status") == "completed":
                return client
        except (OSError, ValueError, LaneError, json.JSONDecodeError):
            time.sleep(0.05)
    raise ProtocolError("lane service failed to start")


def _argv(backend: dict[str, Any]) -> list[str]:
    result = []
    for item in backend["execution"]["argv"]:
        rendered = item.replace("{repo}", str(ROOT))
        path = Path(rendered)
        if not path.is_absolute() and ("/" in rendered or "\\" in rendered):
            rendered = str((ROOT / rendered).resolve())
        result.append(rendered)
    return result


def invoke(
    backend: dict[str, Any],
    envelope: dict[str, Any],
    lane_client: LaneClient,
) -> dict[str, Any]:
    lane = backend["lane"]
    owner = f"scheduler:{os.getpid()}:{uuid.uuid4().hex}"
    acquired = lane_client.request(
        "acquire",
        lane_id=lane["id"],
        capacity=lane["max_concurrency"],
        lease_seconds=lane["lease_seconds"],
        owner=owner,
        timeout_seconds=backend["execution"].get("timeout_seconds", 900),
    )
    if acquired.get("status") != "ready":
        raise ProtocolError(f"lane acquisition failed: {acquired.get('status')}")
    lease_token = acquired["token"]
    stopped = threading.Event()

    def heartbeat() -> None:
        interval = max(1.0, lane["lease_seconds"] / 3)
        while not stopped.wait(interval):
            answer = lane_client.request(
                "heartbeat",
                lane_id=lane["id"],
                capacity=lane["max_concurrency"],
                lease_seconds=lane["lease_seconds"],
                owner=owner,
                token=lease_token,
            )
            if answer.get("status") != "ready":
                return

    thread = threading.Thread(target=heartbeat, daemon=True)
    thread.start()
    environment = {
        **os.environ,
        "DELEGATION_LANE_ENDPOINT": str(lane_client.endpoint_path),
        "DELEGATION_LANE_ID": lane["id"],
        "DELEGATION_LANE_OWNER": owner,
        "DELEGATION_LANE_LEASE_TOKEN": lease_token,
    }
    task_values = []
    if isinstance(envelope.get("task"), dict):
        task_values.append(envelope["task"])
    if isinstance(envelope.get("tasks"), list):
        task_values.extend(item for item in envelope["tasks"]
                           if isinstance(item, dict))
    timeout = backend["execution"].get("timeout_seconds", 900)
    output_limit = 8 * 1024 * 1024
    for task in task_values:
        budgets = task.get("budgets", {})
        timeout = min(timeout, budgets.get("timeout_seconds", timeout))
        output_limit = min(output_limit,
                           budgets.get("max_output_bytes", output_limit))
    try:
        process = subprocess.run(
            _argv(backend),
            input=json.dumps(envelope),
            text=True,
            capture_output=True,
            timeout=timeout,
            env=environment,
        )
        if (len(process.stdout.encode()) > output_limit or
                len(process.stderr.encode()) > output_limit):
            return receipt("failed", "output_budget_exhausted")
        try:
            answer = json.loads(process.stdout)
        except json.JSONDecodeError as error:
            raise ProtocolError(f"adapter returned invalid JSON: {error}") from error
        if (not isinstance(answer, dict) or answer.get("schema_version") != 2 or
                answer.get("status") not in RECEIPT_STATUSES or
                not isinstance(answer.get("classification"), str)):
            raise ProtocolError("adapter returned an invalid v2 receipt")
        if process.returncode and answer["status"] not in {
                "failed", "cancelled", "permission_required"}:
            raise ProtocolError(f"adapter exited {process.returncode}")
        return answer
    except subprocess.TimeoutExpired as error:
        return receipt("failed", "adapter_timeout", error=str(error))
    finally:
        stopped.set()
        thread.join(timeout=1)
        lane_client.request(
            "release",
            lane_id=lane["id"],
            capacity=lane["max_concurrency"],
            lease_seconds=lane["lease_seconds"],
            owner=owner,
            token=lease_token,
        )


def execute_session(
    backend: dict[str, Any],
    task: dict[str, Any],
    client: LaneClient,
) -> dict[str, Any]:
    answer = invoke(backend, {"schema_version": 2, "operation": "start",
                              "task": task}, client)
    steps = 0
    maximum = min(task["budgets"]["max_steps"],
                  backend["execution"].get("max_steps", 10_000))
    while answer["status"] in {"ready", "yielded"}:
        token = answer.get("token")
        if not isinstance(token, str) or not token:
            raise ProtocolError("session receipt omitted token")
        if steps >= maximum:
            invoke(backend, {"schema_version": 2, "operation": "cancel",
                             "token": token}, client)
            return receipt("failed", "step_budget_exhausted",
                           backend=backend["id"], task_id=task["id"])
        answer = invoke(backend, {"schema_version": 2, "operation": "step",
                                  "token": token}, client)
        steps += 1
    return {**answer, "backend": backend["id"], "task_id": task["id"]}


def run_operation(
    catalog: dict[str, Any],
    operation: str,
    request: dict[str, Any],
) -> dict[str, Any]:
    if operation == "resume":
        backend = catalog["by_id"].get(request["backend"])
        if backend is None or backend["kind"] != "session" or not _available(backend):
            raise ProtocolError("resume backend is unavailable")
        client = ensure_lane_service(_lane_state_dir())
        answer = invoke(backend, {
            "schema_version": 2,
            "operation": "resume",
            "token": request["token"],
            "resolution": request["resolution"],
        }, client)
        return {**answer, "backend": backend["id"]}
    backend = select_backend(catalog, request)
    tasks = request["tasks"] if operation == "batch" else [request["task"]]
    if backend["kind"] == "native":
        return receipt("native_required", "native_required",
                       backend=backend["id"], task_ids=[task["id"] for task in tasks])
    client = ensure_lane_service(_lane_state_dir())
    if backend["kind"] == "oneshot":
        envelope = {"schema_version": 2, "operation": operation}
        envelope["tasks" if operation == "batch" else "task"] = (
            tasks if operation == "batch" else tasks[0]
        )
        answer = invoke(backend, envelope, client)
        return {**answer, "backend": backend["id"]}
    if operation == "run":
        return execute_session(backend, tasks[0], client)

    sessions: list[dict[str, Any]] = []
    completed: list[dict[str, Any]] = []
    for task in tasks:
        answer = invoke(backend, {"schema_version": 2, "operation": "start",
                                  "task": task}, client)
        sessions.append({"task": task, "answer": answer, "steps": 0})
    while sessions:
        session = sessions.pop(0)
        answer = session["answer"]
        task = session["task"]
        if answer["status"] in TERMINAL:
            completed.append({**answer, "task_id": task["id"]})
            continue
        token = answer.get("token")
        maximum = min(task["budgets"]["max_steps"],
                      backend["execution"].get("max_steps", 10_000))
        if not isinstance(token, str) or not token:
            raise ProtocolError("session receipt omitted token")
        if session["steps"] >= maximum:
            invoke(backend, {"schema_version": 2, "operation": "cancel",
                             "token": token}, client)
            completed.append(receipt("failed", "step_budget_exhausted",
                                     task_id=task["id"]))
            continue
        session["answer"] = invoke(backend, {
            "schema_version": 2, "operation": "step", "token": token,
        }, client)
        session["steps"] += 1
        sessions.append(session)
    status = "completed" if all(item["status"] == "completed"
                                for item in completed) else "failed"
    return receipt(status, "batch_complete", backend=backend["id"],
                   results=completed)


def _selector_from_args(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "route": args.route,
        "runtime": args.runtime,
        "platform": args.platform,
        "mode": args.mode,
        "workspace": args.workspace,
        "function": args.function,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("validate")
    commands.add_parser("list")
    select = commands.add_parser("select")
    for field in ("route", "runtime", "platform", "mode", "workspace", "function"):
        select.add_argument(f"--{field}", required=True)
    for name in ("run", "batch", "resume"):
        command = commands.add_parser(name)
        command.add_argument("--request-file", type=Path, required=True)
    lane = commands.add_parser("lane")
    lane_commands = lane.add_subparsers(dest="lane_command", required=True)
    serve = lane_commands.add_parser("serve")
    serve.add_argument("--state-dir", type=Path, default=_lane_state_dir())
    serve.add_argument("--port", type=int, default=0)
    serve.add_argument("--idle-seconds", type=int, default=300)
    status = lane_commands.add_parser("status")
    status.add_argument("--state-dir", type=Path, default=_lane_state_dir())
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        if args.command == "lane":
            if args.lane_command == "serve":
                LaneServer(args.state_dir).serve_forever(
                    port=args.port, idle_seconds=args.idle_seconds
                )
                return 0
            answer = LaneClient(args.state_dir / "lane.json", 2).request("status")
            print(json.dumps(answer, sort_keys=True))
            return 0
        catalog = load_catalog(args.catalog)
        if args.command == "validate":
            answer = receipt("completed", "catalog_valid",
                             backends=len(catalog["backends"]))
        elif args.command == "list":
            answer = receipt("completed", "catalog_list",
                             backends=catalog["backends"], routes=catalog["routes"])
        elif args.command == "select":
            answer = select_backend(catalog, _selector_from_args(args))
        else:
            request = validate_request(_json(args.request_file), args.command)
            answer = run_operation(catalog, args.command, request)
        print(json.dumps(answer, sort_keys=True))
        return 69 if answer.get("status") == "native_required" else 0
    except ProtocolError as error:
        classification = "no_backend" if str(error) == "no_backend" else "configuration_error"
        print(json.dumps(receipt("failed", classification, error=str(error)),
                         sort_keys=True))
        return 69 if classification == "no_backend" else 64
    except (OSError, ValueError, json.JSONDecodeError, LaneError) as error:
        print(json.dumps(receipt("failed", "runtime_error", error=str(error)),
                         sort_keys=True))
        return 70


if __name__ == "__main__":
    raise SystemExit(main())
