#!/usr/bin/env python3
"""Provider-neutral protocol-v2 catalog, scheduler, and lane CLI."""
from __future__ import annotations

import argparse
import contextlib
import getpass
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import threading
import tempfile
import time
import uuid
from pathlib import Path, PureWindowsPath
from typing import Any

import claude_runtime
from execution_engine import ExecutionEngine, ExecutionError
from lane_service import LaneClient, LaneError, LaneServer
from managed_service import (
    DeploymentError, credential_path, ensure_service, existing_service,
    load_deployment, process_identity, read_credential, remove_credential,
    write_credential,
)

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


def _config_root() -> Path:
    configured = os.environ.get("DELEGATION_CONFIG_HOME")
    if configured:
        return Path(configured).expanduser()
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", str(Path.home())))
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME",
                                   str(Path.home() / ".config")))
    return base / "agent-delegation-protocol"


def _state_root() -> Path:
    configured = os.environ.get("DELEGATION_STATE_HOME")
    if configured:
        return Path(configured).expanduser()
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", str(Path.home())))
    else:
        base = Path(os.environ.get("XDG_STATE_HOME",
                                   str(Path.home() / ".local" / "state")))
    return base / "agent-delegation-protocol"


def resolve_deployment(value: str | Path) -> Path:
    candidate = Path(value).expanduser()
    if candidate.is_file():
        return candidate.resolve()
    if candidate.is_absolute() or len(candidate.parts) > 1:
        raise ProtocolError(f"deployment does not exist: {candidate}")
    installed = _config_root() / "deployments" / f"{candidate}.json"
    if not installed.is_file():
        raise ProtocolError(f"deployment is not installed: {candidate}")
    return installed.resolve()


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
        "id", "name", "kind", "tier", "selector", "availability",
        "execution",
    }
    if not required <= set(backend) <= required | {"lane"}:
        raise ProtocolError(f"backend {backend.get('id', '?')}: exact v2 fields required")
    backend_id = backend["id"]
    if not isinstance(backend_id, str) or not backend_id:
        raise ProtocolError("backend.id is required")
    if backend["kind"] not in {"native", "oneshot", "session"}:
        raise ProtocolError(f"{backend_id}: invalid kind")
    if backend["tier"] not in {"low", "balanced", "parent"}:
        raise ProtocolError(f"{backend_id}: invalid tier")
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
    allowed_execution = {
        "delivery", "argv", "deployment", "timeout_seconds", "max_steps",
    }
    if not set(execution) <= allowed_execution or "delivery" not in execution:
        raise ProtocolError(f"{backend_id}: invalid execution fields")
    delivery = execution["delivery"]
    if ((backend["kind"] == "native" and delivery != "native") or
            (backend["kind"] == "oneshot" and delivery != "json") or
            (backend["kind"] == "session" and
             delivery not in {"json", "managed"})):
        raise ProtocolError(f"{backend_id}: kind/delivery mismatch")
    if delivery == "json":
        _string_list(execution.get("argv"), f"{backend_id}.execution.argv")
        if "deployment" in execution:
            raise ProtocolError(f"{backend_id}: json execution cannot name a deployment")
    elif delivery == "managed":
        deployment = execution.get("deployment")
        if (not isinstance(deployment, str) or not deployment or
                "argv" in execution):
            raise ProtocolError(f"{backend_id}: invalid managed execution")
    elif "argv" in execution or "deployment" in execution:
        raise ProtocolError(f"{backend_id}: invalid native execution")
    if ("timeout_seconds" in execution and
            (not isinstance(execution["timeout_seconds"], int) or
             execution["timeout_seconds"] < 1)):
        raise ProtocolError(f"{backend_id}: invalid timeout_seconds")
    if ("max_steps" in execution and
            (not isinstance(execution["max_steps"], int) or
             not 1 <= execution["max_steps"] <= 10_000)):
        raise ProtocolError(f"{backend_id}: invalid max_steps")
    lane = backend.get("lane")
    if delivery == "json" and lane is None:
        raise ProtocolError(f"{backend_id}: json execution requires a scheduler lane")
    if delivery == "managed" and lane is not None:
        raise ProtocolError(f"{backend_id}: managed resources belong to the deployment")
    if lane is not None and (not isinstance(lane, dict) or set(lane) != {
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
    if not set(value) <= {"schema_version", "tiers", "backends", "routes", "includes"}:
        raise ProtocolError("catalog contains unsupported fields")
    tiers = value.get("tiers")
    if not isinstance(tiers, dict) or set(tiers) != {"low", "balanced", "parent"}:
        raise ProtocolError("catalog must define low, balanced, and parent tiers")
    for tier, limits in tiers.items():
        if (not isinstance(limits, dict) or set(limits) != {
                "max_input_tokens", "max_output_tokens"}):
            raise ProtocolError(f"catalog tier {tier} limits are invalid")
    if any(tiers["low"][field] is not None for field in (
            "max_input_tokens", "max_output_tokens")):
        raise ProtocolError("catalog low tier must be unbounded")
    for field in ("max_input_tokens", "max_output_tokens"):
        balanced = tiers["balanced"][field]
        parent = tiers["parent"][field]
        if (isinstance(balanced, bool) or not isinstance(balanced, int) or
                isinstance(parent, bool) or not isinstance(parent, int) or
                not balanced > parent > 0):
            raise ProtocolError(f"catalog tiers must strictly descend for {field}")
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
    if backend["execution"]["delivery"] == "managed":
        try:
            deployment = load_deployment(resolve_deployment(
                backend["execution"]["deployment"]))
        except (ProtocolError, DeploymentError):
            return False
        profile = deployment["runtime"]["profile"]
        if profile != "claude-code":
            return False
        command = deployment["runtime"]["executable"]["command"]
        override = deployment["runtime"]["executable"]["environment"]
        requested = os.environ.get(override) or command
        if shutil.which(requested) is None and not Path(requested).is_file():
            return False
        try:
            read_credential(credential_path(
                deployment["credential"]["reference"]))
        except (OSError, DeploymentError):
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
        if backend["tier"] == request.get("tier") and all(request.get(key[:-1]) in selector[key] for key in (
            "runtimes", "platforms", "modes", "workspaces", "functions"
        )) and _available(backend):
            return backend
    raise ProtocolError("no_backend")


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
    if not Path(task["repo"]).is_absolute():
        raise ProtocolError("task.repo must be absolute")
    _string_list(task["allowed_paths"], "task.allowed_paths", allow_empty=True)
    for allowed_path in task["allowed_paths"]:
        path = Path(allowed_path)
        windows_path = PureWindowsPath(allowed_path)
        parts = {part.lower() for part in windows_path.parts}
        if (path.is_absolute() or windows_path.is_absolute() or ".." in parts or
                ".git" in parts):
            raise ProtocolError("task.allowed_paths must be safe relative paths")
    if task["workspace"] not in {"shared", "isolated"}:
        raise ProtocolError("task.workspace is invalid")
    validation = task["validation"]
    if (not isinstance(validation, list) or
            any(not isinstance(command, list) or not command or
                any(not isinstance(part, str) or not part for part in command)
                for command in validation)):
        raise ProtocolError("task.validation must contain argv arrays")
    if task["mode"] == "read" and validation:
        raise ProtocolError("read tasks cannot declare validation commands")
    budgets = task["budgets"]
    if (not isinstance(budgets, dict) or set(budgets) != {
            "timeout_seconds", "max_input_tokens", "max_output_tokens",
            "max_output_bytes", "max_steps"} or
            any((value is not None and
                 (isinstance(value, bool) or not isinstance(value, int) or value < 1))
                for value in budgets.values()) or
            any(budgets[field] is None for field in (
                "timeout_seconds", "max_output_bytes", "max_steps"))):
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
        "schema_version", "route", "tier", "runtime", "platform", "function",
        "mode", "workspace",
    }
    task_field = "tasks" if operation == "batch" else "task"
    if set(value) != common | {task_field}:
        raise ProtocolError(f"{operation} request fields are invalid")
    for key in common - {"schema_version"}:
        if not isinstance(value[key], str) or not value[key]:
            raise ProtocolError(f"request.{key} is required")
    if operation == "batch":
        if (not isinstance(value["tasks"], list) or
                not 1 <= len(value["tasks"]) <= 32):
            raise ProtocolError("batch tasks must contain 1 to 32 entries")
        value["tasks"] = [validate_task(task) for task in value["tasks"]]
    else:
        value["task"] = validate_task(value["task"])
    if value["tier"] not in {"low", "balanced", "parent"}:
        raise ProtocolError("request.tier is invalid")
    return value


def validate_tier_budgets(catalog: dict[str, Any], request: dict[str, Any]) -> None:
    limits = catalog["tiers"][request["tier"]]
    tasks = request.get("tasks", [request.get("task")])
    for task in tasks:
        if not isinstance(task, dict):
            continue
        budgets = task["budgets"]
        for field in ("max_input_tokens", "max_output_tokens"):
            if limits[field] is None:
                if budgets[field] is not None:
                    raise ProtocolError(
                        f"task.budgets.{field} must be null for unbounded low tier")
            elif budgets[field] is None or budgets[field] > limits[field]:
                raise ProtocolError(
                    f"task.budgets.{field} exceeds {request['tier']} tier limit")


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
    for index, item in enumerate(backend["execution"]["argv"]):
        rendered = item.replace("{repo}", str(ROOT))
        path = Path(rendered)
        if not path.is_absolute() and ("/" in rendered or "\\" in rendered):
            rendered = str((ROOT / rendered).resolve())
        elif index > 0 and rendered.lower().endswith(".py"):
            rendered = shutil.which(rendered) or rendered
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
        "DELEGATION_LANE_CAPACITY": str(lane["max_concurrency"]),
        "DELEGATION_LANE_LEASE_SECONDS": str(lane["lease_seconds"]),
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


def invoke_managed(
    backend: dict[str, Any], envelope: dict[str, Any],
) -> dict[str, Any]:
    deployment_path = resolve_deployment(backend["execution"]["deployment"])
    deployment = load_deployment(deployment_path)
    service = ensure_service(deployment_path)
    profile = deployment["runtime"]["profile"]
    if profile != "claude-code":
        raise ProtocolError(f"unsupported managed runtime profile: {profile}")
    runner = claude_runtime.worker_runner(deployment, service)
    engine = ExecutionEngine(
        _state_root() / "executions" / deployment["id"], runner,
    )
    operation = envelope.get("operation")
    if operation == "start":
        return engine.start(envelope.get("task"))
    token = envelope.get("token")
    if not isinstance(token, str) or not token:
        raise ProtocolError("managed session token is required")
    if operation == "step":
        return engine.step(token)
    if operation == "resume":
        return engine.resume(token, envelope.get("resolution"))
    if operation == "cancel":
        return engine.cancel(token)
    raise ProtocolError(f"unsupported managed operation: {operation}")


def invoke_backend(
    backend: dict[str, Any], envelope: dict[str, Any],
    lane_client: LaneClient | None,
) -> dict[str, Any]:
    if backend["execution"]["delivery"] == "managed":
        return invoke_managed(backend, envelope)
    if lane_client is None:
        raise ProtocolError("external adapter lane is unavailable")
    return invoke(backend, envelope, lane_client)


def execute_session(
    backend: dict[str, Any],
    task: dict[str, Any],
    client: LaneClient | None,
) -> dict[str, Any]:
    answer = invoke_backend(backend, {
        "schema_version": 2, "operation": "start", "task": task,
    }, client)
    steps = 0
    maximum = min(task["budgets"]["max_steps"],
                  backend["execution"].get("max_steps", 10_000))
    while answer["status"] in {"ready", "yielded"}:
        token = answer.get("token")
        if not isinstance(token, str) or not token:
            raise ProtocolError("session receipt omitted token")
        if steps >= maximum:
            invoke_backend(backend, {
                "schema_version": 2, "operation": "cancel", "token": token,
            }, client)
            return receipt("failed", "step_budget_exhausted",
                           backend=backend["id"], task_id=task["id"])
        answer = invoke_backend(backend, {
            "schema_version": 2, "operation": "step", "token": token,
        }, client)
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
        client = (None if backend["execution"]["delivery"] == "managed" else
                  ensure_lane_service(_lane_state_dir()))
        answer = invoke_backend(backend, {
            "schema_version": 2,
            "operation": "resume",
            "token": request["token"],
            "resolution": request["resolution"],
        }, client)
        return {**answer, "backend": backend["id"]}
    validate_tier_budgets(catalog, request)
    backend = select_backend(catalog, request)
    tasks = request["tasks"] if operation == "batch" else [request["task"]]
    if backend["kind"] == "native":
        return receipt("native_required", "native_required",
                       backend=backend["id"], runtime=request["runtime"],
                       task_ids=[task["id"] for task in tasks])
    client = (None if backend["execution"]["delivery"] == "managed" else
              ensure_lane_service(_lane_state_dir()))
    if backend["kind"] == "oneshot":
        envelope = {"schema_version": 2, "operation": operation}
        envelope["tasks" if operation == "batch" else "task"] = (
            tasks if operation == "batch" else tasks[0]
        )
        answer = invoke_backend(backend, envelope, client)
        return {**answer, "backend": backend["id"]}
    if operation == "run":
        return execute_session(backend, tasks[0], client)

    sessions: list[dict[str, Any]] = []
    completed: list[dict[str, Any]] = []
    for task in tasks:
        answer = invoke_backend(backend, {
            "schema_version": 2, "operation": "start", "task": task,
        }, client)
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
            invoke_backend(backend, {
                "schema_version": 2, "operation": "cancel", "token": token,
            }, client)
            completed.append(receipt("failed", "step_budget_exhausted",
                                     task_id=task["id"]))
            continue
        session["answer"] = invoke_backend(backend, {
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
        "tier": args.tier,
        "runtime": args.runtime,
        "platform": args.platform,
        "mode": args.mode,
        "workspace": args.workspace,
        "function": args.function,
    }


def launch_managed(deployment_value: str, arguments: list[str]) -> int:
    deployment_path = resolve_deployment(deployment_value)
    deployment = load_deployment(deployment_path)
    if deployment["runtime"]["profile"] != "claude-code":
        raise ProtocolError("deployment runtime profile is not launchable")
    configured = deployment["runtime"].get("arguments", [])
    values = [*configured, *arguments]
    control = bool(values and values[0] in claude_runtime.CONTROL_COMMANDS)
    if control:
        return claude_runtime.launch(deployment, arguments)
    service = ensure_service(deployment_path)
    binding = service.register(
        f"launcher:{os.getpid()}:{uuid.uuid4().hex}", pid=os.getpid(),
        dependency_seconds=0,
    )
    try:
        return claude_runtime.launch(deployment, arguments, gateway=binding)
    except BaseException:
        binding.close()
        raise


def _bin_root() -> Path:
    configured = os.environ.get("DELEGATION_BIN_DIR")
    if configured:
        return Path(configured).expanduser()
    if os.name == "nt":
        return (_config_root() / "bin").resolve()
    return Path.home() / ".local" / "bin"


def _digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _atomic_bytes(path: Path, value: bytes, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        if os.name != "nt":
            os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _deployment_manifest(deployment_id: str) -> Path:
    return _config_root() / "manifests" / f"{deployment_id}.json"


@contextlib.contextmanager
def _deployment_lock(deployment_id: str):
    lock = _config_root() / "locks" / f"{deployment_id}.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    claim = json.dumps({
        "pid": os.getpid(), "process_identity": process_identity(os.getpid()),
        "nonce": uuid.uuid4().hex,
    }, sort_keys=True).encode("ascii") + b"\n"
    for attempt in range(2):
        descriptor, temporary = tempfile.mkstemp(
            prefix=f".{deployment_id}.", dir=lock.parent)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(claim)
                handle.flush()
                os.fsync(handle.fileno())
            os.link(temporary, lock)
        except FileExistsError:
            try:
                owner = json.loads(lock.read_text(encoding="ascii"))
                pid = int(owner["pid"])
                identity = owner["process_identity"]
                if (not isinstance(identity, str) or
                        process_identity(pid) == identity):
                    raise ProtocolError("deployment installation is active")
            except FileNotFoundError:
                if attempt:
                    raise ProtocolError("deployment lock changed concurrently")
                continue
            except (ValueError, TypeError, json.JSONDecodeError, KeyError) as error:
                raise ProtocolError("deployment lock is corrupt") from error
            lock.unlink(missing_ok=True)
            continue
        finally:
            Path(temporary).unlink(missing_ok=True)
        break
    else:
        raise ProtocolError("could not acquire deployment lock")
    try:
        yield
    finally:
        try:
            if lock.read_bytes() == claim:
                lock.unlink()
        except FileNotFoundError:
            pass


def install_deployment(config: Path,
                       launchers: list[list[str]]) -> dict[str, Any]:
    deployment = load_deployment(config)
    with _deployment_lock(deployment["id"]):
        _preflight_deployment_install(config, launchers)
        destination = (_config_root() / "deployments" /
                       f"{deployment['id']}.json")
        if (destination.is_file() and
                destination.read_bytes() != config.read_bytes()):
            service = existing_service(destination)
            if (service is not None and
                    service.status().get("clients")):
                raise ProtocolError(
                    "cannot replace a deployment with active or retained clients")
            if service is not None and not service.stop():
                raise ProtocolError("managed service refused deployment update")
        return _install_deployment_locked(config, launchers)


def _preflight_deployment_install(
        config: Path, launchers: list[list[str]]) -> None:
    deployment = load_deployment(config)
    destination = (_config_root() / "deployments" /
                   f"{deployment['id']}.json")
    manifest_path = _deployment_manifest(deployment["id"])
    prior = _json(manifest_path) if manifest_path.is_file() else None
    if prior is not None and (not isinstance(prior, dict) or
                              prior.get("schema_version") != 1):
        raise ProtocolError("deployment ownership manifest is invalid")
    plan = [destination]
    for pair in launchers:
        source, name = Path(pair[0]), pair[1]
        if (not source.is_file() or not name or Path(name).name != name or
                name in {".", ".."}):
            raise ProtocolError("launcher must name a file and safe destination")
        plan.append(_bin_root() / name)
    if len({str(path) for path in plan}) != len(plan):
        raise ProtocolError("deployment launcher destinations must be unique")
    old_by_path = {
        item["path"]: item for item in (prior or {}).get("resources", [])
        if isinstance(item, dict) and isinstance(item.get("path"), str)
    }
    if prior is not None and set(old_by_path) != {str(path) for path in plan}:
        raise ProtocolError(
            "deployment launcher set changed; uninstall before reinstalling")
    for path in plan:
        if path.exists():
            record = old_by_path.get(str(path))
            if (record is None or not path.is_file() or
                    _digest(path.read_bytes()) != record.get("digest")):
                raise ProtocolError(
                    f"refusing to overwrite unowned deployment file: {path}")


def _install_deployment_locked(config: Path,
                                launchers: list[list[str]]) -> dict[str, Any]:
    deployment = load_deployment(config)
    deployment_id = deployment["id"]
    destination = _config_root() / "deployments" / f"{deployment_id}.json"
    manifest_path = _deployment_manifest(deployment_id)
    prior = _json(manifest_path) if manifest_path.is_file() else None
    if prior is not None and (not isinstance(prior, dict) or
                              prior.get("schema_version") != 1):
        raise ProtocolError("deployment ownership manifest is invalid")
    resources: list[dict[str, str]] = []
    plan: list[tuple[Path, bytes, int]] = [
        (destination, config.read_bytes(), 0o600),
    ]
    for pair in launchers:
        source, name = Path(pair[0]), pair[1]
        if (not source.is_file() or not name or Path(name).name != name or
                name in {".", ".."}):
            raise ProtocolError("launcher must name a file and safe destination")
        plan.append((_bin_root() / name, source.read_bytes(), 0o755))
    old_by_path = {
        item["path"]: item for item in (prior or {}).get("resources", [])
        if isinstance(item, dict) and isinstance(item.get("path"), str)
    }
    planned_paths = {str(path) for path, _value, _mode in plan}
    if prior is not None and set(old_by_path) != planned_paths:
        raise ProtocolError(
            "deployment launcher set changed; uninstall before reinstalling")
    for path, _value, _mode in plan:
        if path.exists():
            record = old_by_path.get(str(path))
            if (record is None or not path.is_file() or
                    _digest(path.read_bytes()) != record.get("digest")):
                raise ProtocolError(f"refusing to overwrite unowned deployment file: {path}")
    backups = {path: path.read_bytes() for path, _value, _mode in plan if path.exists()}
    created: list[Path] = []
    try:
        for path, value, mode in plan:
            if not path.exists():
                created.append(path)
            _atomic_bytes(path, value, mode)
            resources.append({"path": str(path), "digest": _digest(value)})
        manifest = {
            "schema_version": 1, "deployment_id": deployment_id,
            "resources": resources,
        }
        _atomic_bytes(
            manifest_path,
            (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode(),
        )
    except BaseException:
        for path, value in backups.items():
            _atomic_bytes(path, value, 0o755 if path.parent == _bin_root() else 0o600)
        for path in created:
            path.unlink(missing_ok=True)
        raise
    return {"deployment_id": deployment_id,
            "config": str(destination), "resources": resources}


def uninstall_deployment(deployment_id: str, *, keep_credential: bool) -> dict[str, Any]:
    with _deployment_lock(deployment_id):
        return _uninstall_deployment_locked(
            deployment_id, keep_credential=keep_credential)


def _uninstall_deployment_locked(
        deployment_id: str, *, keep_credential: bool) -> dict[str, Any]:
    path = resolve_deployment(deployment_id)
    deployment = load_deployment(path)
    manifest_path = _deployment_manifest(deployment["id"])
    manifest = _json(manifest_path)
    resources = manifest.get("resources") if isinstance(manifest, dict) else None
    if (not isinstance(manifest, dict) or manifest.get("schema_version") != 1
            or manifest.get("deployment_id") != deployment["id"]
            or not isinstance(resources, list)):
        raise ProtocolError("deployment ownership manifest is invalid")
    expected_config = (_config_root() / "deployments" /
                       f"{deployment['id']}.json").resolve()
    owned: list[tuple[Path, bytes, int]] = []
    seen: set[Path] = set()
    for record in resources:
        if not isinstance(record, dict):
            raise ProtocolError("deployment ownership manifest is invalid")
        resource = Path(record.get("path", "")).resolve()
        permitted = (
            resource == expected_config
            or (resource.parent == _bin_root().resolve()
                and resource.name not in {"", ".", ".."}))
        if (not permitted or resource in seen or not resource.is_file() or
                _digest(resource.read_bytes()) != record.get("digest")):
            raise ProtocolError(f"refusing to remove modified deployment file: {resource}")
        seen.add(resource)
        owned.append((resource, resource.read_bytes(), stat.S_IMODE(
            resource.stat().st_mode)))
    reference = deployment["credential"]["reference"]
    credential = credential_path(reference)
    credential_present = credential.exists() or credential.is_symlink()
    if not keep_credential and credential_present:
        read_credential(credential)
    service = existing_service(path)
    if service is not None and service.status().get("clients"):
        raise ProtocolError("deployment has active or retained clients")
    if service is not None and not service.stop():
        raise ProtocolError("managed service refused to stop")
    removed: list[str] = []
    manifest_value = manifest_path.read_bytes()
    manifest_mode = stat.S_IMODE(manifest_path.stat().st_mode)
    try:
        for resource, _value, _mode in owned:
            resource.unlink()
            removed.append(str(resource))
        manifest_path.unlink()
        credential_removed = (
            False if keep_credential or not credential_present
            else remove_credential(reference))
    except BaseException:
        for resource, value, mode in owned:
            if not resource.exists():
                _atomic_bytes(resource, value, mode)
        if not manifest_path.exists():
            _atomic_bytes(manifest_path, manifest_value, manifest_mode)
        raise
    return {"deployment_id": deployment["id"], "removed": removed,
            "credential_removed": credential_removed}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("validate")
    commands.add_parser("list")
    select = commands.add_parser("select")
    for field in ("route", "tier", "runtime", "platform", "mode", "workspace", "function"):
        select.add_argument(f"--{field}", required=True)
    for name in ("run", "batch", "resume"):
        command = commands.add_parser(name)
        command.add_argument("--request-file", type=Path, required=True)
    launch = commands.add_parser("launch")
    launch.add_argument("--deployment", required=True)
    launch.add_argument("arguments", nargs=argparse.REMAINDER)
    deployment = commands.add_parser("deployment")
    deployment_commands = deployment.add_subparsers(
        dest="deployment_command", required=True)
    deployment_validate = deployment_commands.add_parser("validate")
    deployment_validate.add_argument("--config", type=Path, required=True)
    deployment_install = deployment_commands.add_parser("install")
    deployment_install.add_argument("--config", type=Path, required=True)
    deployment_install.add_argument(
        "--launcher", nargs=2, action="append", default=[],
        metavar=("SOURCE", "DESTINATION"),
    )
    deployment_status = deployment_commands.add_parser("status")
    deployment_status.add_argument("--deployment", required=True)
    deployment_uninstall = deployment_commands.add_parser("uninstall")
    deployment_uninstall.add_argument("--deployment", required=True)
    deployment_uninstall.add_argument("--keep-credential", action="store_true")
    credential = commands.add_parser("credential")
    credential_commands = credential.add_subparsers(
        dest="credential_command", required=True)
    for name in ("set", "status", "remove"):
        item = credential_commands.add_parser(name)
        item.add_argument("--deployment", required=True)
    credential_set = credential_commands.choices["set"]
    credential_set.add_argument("--from-file", type=Path)
    service = commands.add_parser("service")
    service_commands = service.add_subparsers(dest="service_command", required=True)
    for name in ("ensure", "status", "stop"):
        item = service_commands.add_parser(name)
        item.add_argument("--deployment", required=True)
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
        if args.command == "launch":
            arguments = args.arguments[1:] if args.arguments[:1] == ["--"] else args.arguments
            try:
                return launch_managed(args.deployment, arguments)
            except claude_runtime.RuntimeProfileError as error:
                print(f"managed runtime: {error}", file=sys.stderr)
                return error.status
            except DeploymentError as error:
                print(f"managed deployment: {error}", file=sys.stderr)
                return 78
        if args.command == "deployment":
            if args.deployment_command == "validate":
                deployment = load_deployment(args.config)
                answer = receipt("completed", "deployment_valid",
                                 deployment_id=deployment["id"])
            elif args.deployment_command == "install":
                result = install_deployment(args.config, args.launcher)
                answer = receipt("completed", "deployment_installed", **result)
            elif args.deployment_command == "status":
                path = resolve_deployment(args.deployment)
                deployment = load_deployment(path)
                answer = receipt("completed", "deployment_installed",
                                 deployment_id=deployment["id"], path=str(path))
            else:
                result = uninstall_deployment(
                    args.deployment, keep_credential=args.keep_credential)
                answer = receipt("completed", "deployment_uninstalled", **result)
            print(json.dumps(answer, sort_keys=True))
            return 0
        if args.command == "credential":
            path = resolve_deployment(args.deployment)
            deployment = load_deployment(path)
            reference = deployment["credential"]["reference"]
            if args.credential_command == "set":
                if args.from_file:
                    value = read_credential(args.from_file)
                elif sys.stdin.isatty():
                    value = getpass.getpass("Provider credential: ")
                else:
                    value = sys.stdin.read().rstrip("\n")
                with _deployment_lock(deployment["id"]):
                    existing = credential_path(reference)
                    if existing.is_file():
                        service = existing_service(path)
                        if (service is not None and
                                service.status().get("clients")):
                            raise ProtocolError(
                                "credential rotation requires a drained deployment")
                        if service is not None and not service.stop():
                            raise ProtocolError(
                                "managed service refused credential rotation")
                    write_credential(reference, value)
                classification = "credential_stored"
            elif args.credential_command == "status":
                read_credential(credential_path(reference))
                classification = "credential_ready"
            else:
                with _deployment_lock(deployment["id"]):
                    service = existing_service(path)
                    if (service is not None and
                            service.status().get("clients")):
                        raise ProtocolError(
                            "deployment has active or retained clients")
                    if service is not None and not service.stop():
                        raise ProtocolError("managed service refused to stop")
                    remove_credential(reference)
                classification = "credential_removed"
            print(json.dumps(receipt("completed", classification,
                                     deployment_id=deployment["id"]),
                             sort_keys=True))
            return 0
        if args.command == "service":
            path = resolve_deployment(args.deployment)
            if args.service_command == "stop":
                client = existing_service(path)
                if client is None:
                    answer = {"status": "stopped"}
                elif client.status().get("clients"):
                    raise ProtocolError("deployment has active or retained clients")
                elif not client.stop():
                    raise ProtocolError("managed service refused to stop")
                else:
                    answer = {"status": "stopped"}
            else:
                client = ensure_service(path)
                answer = client.status()
            print(json.dumps(answer, sort_keys=True))
            return 0
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
