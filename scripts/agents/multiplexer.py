#!/usr/bin/env python3
"""Validate, select, and run priority-ordered delegation workers."""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Iterator

SCHEMA_VERSION = 1
DEFAULT_MAX_INPUT = 1024 * 1024
DEFAULT_MAX_OUTPUT = 2 * 1024 * 1024
ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
INFERENCE_ENV = "AGENT_INFERENCE_CONFIG"


class ConfigurationError(Exception):
    """A catalog or route is malformed."""


class InputError(Exception):
    """A task or command-line request is malformed."""


def _reject_unknown(value: dict[str, Any], allowed: set[str], where: str) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ConfigurationError(f"{where}: unknown fields: {', '.join(unknown)}")


def _strings(value: Any, where: str) -> list[str]:
    if not isinstance(value, list) or not value:
        raise ConfigurationError(f"{where} must be a non-empty string array")
    if any(not isinstance(item, str) or not item or "\0" in item for item in value):
        raise ConfigurationError(f"{where} must be a non-empty string array")
    if len(value) != len(set(value)):
        raise ConfigurationError(f"{where} contains duplicate values")
    return value


def _positive_int(value: Any, where: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ConfigurationError(f"{where} must be a positive integer")
    return value


def _validate_inference(value: Any, source: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ConfigurationError(f"{source}: inference must be an object")
    _reject_unknown(value, {"thinking", "effort", "max_output_tokens"},
                    f"{source}: inference")
    if not value:
        raise ConfigurationError(f"{source}: inference must not be empty")
    effort = value.get("effort")
    if effort is not None and effort not in ("low", "medium", "high", "xhigh", "max"):
        raise ConfigurationError(
            f"{source}: inference.effort must be low, medium, high, xhigh, or max"
        )
    maximum = value.get("max_output_tokens")
    if maximum is not None:
        _positive_int(maximum, f"{source}: inference.max_output_tokens")
        if maximum > 131072:
            raise ConfigurationError(
                f"{source}: inference.max_output_tokens must not exceed 131072"
            )
    thinking = value.get("thinking")
    if thinking is not None:
        if not isinstance(thinking, dict):
            raise ConfigurationError(f"{source}: inference.thinking must be an object")
        _reject_unknown(thinking, {"type", "budget_tokens"},
                        f"{source}: inference.thinking")
        thinking_type = thinking.get("type")
        if thinking_type not in ("adaptive", "disabled", "enabled"):
            raise ConfigurationError(
                f"{source}: inference.thinking.type must be adaptive, disabled, or enabled"
            )
        budget = thinking.get("budget_tokens")
        if thinking_type == "enabled":
            _positive_int(budget, f"{source}: inference.thinking.budget_tokens")
            if budget > 131072:
                raise ConfigurationError(
                    f"{source}: inference.thinking.budget_tokens must not exceed 131072"
                )
            if maximum is not None and budget >= maximum:
                raise ConfigurationError(
                    f"{source}: inference.thinking.budget_tokens must be below "
                    "inference.max_output_tokens"
                )
        elif budget is not None:
            raise ConfigurationError(
                f"{source}: inference.thinking.budget_tokens requires type='enabled'"
            )
    return value


def validate_agent(agent: Any, source: str) -> dict[str, Any]:
    if not isinstance(agent, dict):
        raise ConfigurationError(f"{source}: metadata must be a JSON object")
    _reject_unknown(agent, {
        "schema_version", "id", "name", "description", "native",
        "delegation_queue", "provider", "model", "binding", "capabilities",
        "limits", "queue_policy", "inference",
    }, source)
    required = {
        "schema_version", "id", "name", "description", "native",
        "delegation_queue", "provider", "model", "binding", "capabilities",
        "limits",
    }
    missing = sorted(required - set(agent))
    if missing:
        raise ConfigurationError(f"{source}: missing fields: {', '.join(missing)}")
    if agent["schema_version"] != SCHEMA_VERSION:
        raise ConfigurationError(f"{source}: unsupported schema_version")
    agent_id = agent["id"]
    if not isinstance(agent_id, str) or not ID_PATTERN.fullmatch(agent_id):
        raise ConfigurationError(f"{source}: invalid id")
    if not isinstance(agent["native"], bool):
        raise ConfigurationError(f"{source}: native must be boolean")
    if not isinstance(agent["delegation_queue"], bool):
        raise ConfigurationError(f"{source}: delegation_queue must be boolean")
    for field in ("name", "description", "provider", "model"):
        if not isinstance(agent[field], str) or not agent[field].strip():
            raise ConfigurationError(f"{source}: {field} must be a non-empty string")

    if "inference" in agent:
        _validate_inference(agent["inference"], source)

    binding = agent["binding"]
    if not isinstance(binding, dict):
        raise ConfigurationError(f"{source}: binding must be an object")
    if not agent["native"]:
        _reject_unknown(binding, {
            "argv", "max_input_bytes", "max_output_bytes", "timeout_seconds",
            "protocol",
        }, f"{source}: binding")
        required_binding = {
            "argv", "max_input_bytes", "max_output_bytes", "timeout_seconds",
        }
        missing = sorted(required_binding - set(binding))
        if missing:
            raise ConfigurationError(
                f"{source}: binding missing fields: {', '.join(missing)}"
            )
        argv = _strings(binding["argv"], f"{source}: binding.argv")
        if len(argv) > 64 or any(len(item) > 4096 for item in argv):
            raise ConfigurationError(f"{source}: binding.argv exceeds bounds")
        _positive_int(binding["max_input_bytes"], f"{source}: binding.max_input_bytes")
        _positive_int(binding["max_output_bytes"], f"{source}: binding.max_output_bytes")
        _positive_int(binding["timeout_seconds"], f"{source}: binding.timeout_seconds")
        protocol = binding.get("protocol", "oneshot")
        if protocol not in ("oneshot", "cooperative-v1"):
            raise ConfigurationError(
                f"{source}: binding.protocol must be 'oneshot' or 'cooperative-v1'"
            )
    else:
        _reject_unknown(
            binding, {"runtime", "agent_type", "reasoning_effort"},
            f"{source}: binding",
        )
        for field in ("runtime", "agent_type", "reasoning_effort"):
            if not isinstance(binding.get(field), str) or not binding[field]:
                raise ConfigurationError(f"{source}: binding.{field} is invalid")

    capabilities = agent["capabilities"]
    if not isinstance(capabilities, dict):
        raise ConfigurationError(f"{source}: capabilities must be an object")
    expected_capabilities = {
        "functions", "runtimes", "platforms", "modes", "workspaces",
        "deliveries",
    }
    _reject_unknown(capabilities, expected_capabilities, f"{source}: capabilities")
    missing = sorted(expected_capabilities - set(capabilities))
    if missing:
        raise ConfigurationError(
            f"{source}: capabilities missing fields: {', '.join(missing)}"
        )
    for field in sorted(expected_capabilities):
        _strings(capabilities[field], f"{source}: capabilities.{field}")

    limits = agent["limits"]
    if not isinstance(limits, dict):
        raise ConfigurationError(f"{source}: limits must be an object")
    _reject_unknown(limits, {"max_concurrency"}, f"{source}: limits")
    _positive_int(limits.get("max_concurrency"), f"{source}: limits.max_concurrency")
    if agent["delegation_queue"] and (
        agent["native"]
        or "batch" not in capabilities["functions"]
        or limits["max_concurrency"] != 1
    ):
        raise ConfigurationError(
            f"{source}: delegation_queue requires native=false, the batch "
            "function, and limits.max_concurrency=1"
        )
    policy = agent.get("queue_policy")
    if (not agent["native"]
            and agent["binding"].get("protocol", "oneshot") == "cooperative-v1"
            and policy is None):
        raise ConfigurationError(
            f"{source}: binding.protocol='cooperative-v1' requires queue_policy"
        )
    if policy is not None:
        if not isinstance(policy, dict):
            raise ConfigurationError(f"{source}: queue_policy must be an object")
        _reject_unknown(policy, {"strategy", "virtual_slots", "quantum"},
                        f"{source}: queue_policy")
        if policy.get("strategy") != "round_robin":
            raise ConfigurationError(
                f"{source}: queue_policy.strategy must be 'round_robin'"
            )
        slots = _positive_int(policy.get("virtual_slots"),
                              f"{source}: queue_policy.virtual_slots")
        if slots > 32:
            raise ConfigurationError(
                f"{source}: queue_policy.virtual_slots must not exceed 32"
            )
        quantum = policy.get("quantum")
        if not isinstance(quantum, dict):
            raise ConfigurationError(f"{source}: queue_policy.quantum must be an object")
        _reject_unknown(quantum, {"unit", "value"}, f"{source}: queue_policy.quantum")
        if quantum.get("unit") != "agent_turn":
            raise ConfigurationError(
                f"{source}: queue_policy.quantum.unit must be 'agent_turn'"
            )
        _positive_int(quantum.get("value"), f"{source}: queue_policy.quantum.value")
        functions = capabilities["functions"]
        if (agent["native"] or not agent["delegation_queue"]
                or limits["max_concurrency"] != 1
                or "batch" not in functions or "resumable-batch" not in functions
                or agent["binding"].get("protocol", "oneshot") != "cooperative-v1"):
            raise ConfigurationError(
                f"{source}: round_robin requires native=false, delegation_queue=true, "
                "batch and resumable-batch functions, limits.max_concurrency=1, "
                "and binding.protocol='cooperative-v1'"
            )
    return agent


def load_catalog(catalog_dir: Path) -> dict[str, dict[str, Any]]:
    if not catalog_dir.is_dir():
        raise ConfigurationError(f"catalog directory not found: {catalog_dir}")
    agents: dict[str, dict[str, Any]] = {}
    paths = sorted(catalog_dir.glob("*.json"))
    if not paths:
        raise ConfigurationError(f"catalog contains no JSON metadata: {catalog_dir}")
    for path in paths:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ConfigurationError(f"{path}: invalid JSON: {error}") from error
        agent = validate_agent(value, str(path))
        agent_id = agent["id"]
        if agent_id in agents:
            raise ConfigurationError(f"duplicate agent id: {agent_id}")
        agents[agent_id] = agent
    return agents


def load_routes(path: Path, agents: dict[str, dict[str, Any]]) -> dict[str, list[str]]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ConfigurationError(f"{path}: invalid JSON: {error}") from error
    if not isinstance(value, dict):
        raise ConfigurationError(f"{path}: route configuration must be an object")
    _reject_unknown(value, {"schema_version", "routes"}, str(path))
    if value.get("schema_version") != SCHEMA_VERSION:
        raise ConfigurationError(f"{path}: unsupported schema_version")
    routes = value.get("routes")
    if not isinstance(routes, dict) or not routes:
        raise ConfigurationError(f"{path}: routes must be a non-empty object")
    checked: dict[str, list[str]] = {}
    for name, members in sorted(routes.items()):
        if not isinstance(name, str) or not ID_PATTERN.fullmatch(name):
            raise ConfigurationError(f"{path}: invalid route name {name!r}")
        members = _strings(members, f"{path}: route {name}")
        missing = [agent_id for agent_id in members if agent_id not in agents]
        if missing:
            raise ConfigurationError(
                f"{path}: route {name} references missing agent ids: {', '.join(missing)}"
            )
        checked[name] = members
    return checked


def is_available(agent: dict[str, Any]) -> bool:
    if agent["native"]:
        return True
    value = agent["binding"]["argv"][0]
    if os.path.sep in value or (os.path.altsep and os.path.altsep in value):
        return Path(value).is_file() and os.access(value, os.X_OK)
    return shutil.which(value) is not None


def matches(agent: dict[str, Any], filters: dict[str, str | None],
            required: list[str]) -> bool:
    capabilities = agent["capabilities"]
    mapping = {
        "runtime": "runtimes", "platform": "platforms", "mode": "modes",
        "workspace": "workspaces", "delivery": "deliveries",
    }
    return (
        all(value is None or value in capabilities[mapping[name]]
            for name, value in filters.items())
        and all(item in capabilities["functions"] for item in required)
    )


def candidates(route: str, routes: dict[str, list[str]], agents: dict[str, dict[str, Any]],
               filters: dict[str, str | None], required: list[str]) -> list[dict[str, Any]]:
    if route not in routes:
        raise InputError(f"unknown route: {route}")
    return [agents[agent_id] for agent_id in routes[route]
            if matches(agents[agent_id], filters, required)
            and is_available(agents[agent_id])]


def runtime_platform() -> str:
    return {"win32": "windows", "darwin": "darwin"}.get(
        sys.platform, sys.platform
    )


def select_queue_backend(catalog_dir: Path, routes_path: Path, route: str,
                         runtime: str, platform: str | None = None
                         ) -> dict[str, Any] | None:
    """Return the first available queue backend on a validated route."""
    agents = load_catalog(catalog_dir)
    routes = load_routes(routes_path, agents)
    filters = {
        "runtime": runtime, "platform": platform or runtime_platform(), "mode": None,
        "workspace": None, "delivery": None,
    }
    selected = candidates(route, routes, agents, filters, ["batch"])
    return next(
        (agent for agent in selected if agent["delegation_queue"]), None
    )


def state_root() -> Path:
    configured = os.environ.get("AGENT_MULTIPLEXER_STATE_DIR")
    if configured:
        return Path(configured).expanduser()
    xdg = os.environ.get("XDG_STATE_HOME")
    if xdg:
        return Path(xdg).expanduser() / "agent-delegation-protocol"
    return Path.home() / ".cache" / "agent-delegation-protocol"


@contextlib.contextmanager
def _file_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+b")
    try:
        if os.name == "nt":
            import msvcrt
            handle.seek(0)
            handle.write(b"0")
            handle.flush()
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        if os.name == "nt":
            import msvcrt
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


@contextlib.contextmanager
def concurrency_lock(agent: dict[str, Any]) -> Iterator[None]:
    if agent["limits"]["max_concurrency"] != 1:
        yield
        return
    root = state_root() / "locks"
    root.mkdir(parents=True, exist_ok=True)
    lock_path = root / f"{agent['id']}.lock"
    with _file_lock(lock_path):
        yield


def _pid_alive(pid: Any) -> bool:
    if not isinstance(pid, int) or isinstance(pid, bool) or pid < 1:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _load_tickets(path: Path) -> list[dict[str, Any]]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return []
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)
            and isinstance(item.get("id"), str) and _pid_alive(item.get("pid"))]


def _store_tickets(path: Path, tickets: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(tickets, separators=(",", ":")), encoding="utf-8")


@contextlib.contextmanager
def fair_step_lock(agent: dict[str, Any], deadline: float) -> Iterator[None]:
    """Acquire the single provider lane in cross-process FIFO ticket order."""
    root = state_root() / "round-robin" / agent["id"]
    guard = root / "tickets.lock"
    tickets_path = root / "tickets.json"
    lane = root / "lane.lock"
    ticket = {"id": uuid.uuid4().hex, "pid": os.getpid()}
    with _file_lock(guard):
        tickets = _load_tickets(tickets_path)
        tickets.append(ticket)
        _store_tickets(tickets_path, tickets)
    acquired = False
    try:
        while not acquired:
            if time.monotonic() >= deadline:
                raise TimeoutError("timed out waiting for cooperative provider lane")
            with _file_lock(guard):
                tickets = _load_tickets(tickets_path)
                if tickets and tickets[0].get("id") == ticket["id"]:
                    # Holding the ticket guard prevents a later waiter from racing
                    # ahead between removal of the head ticket and lane acquisition.
                    lane_context = _file_lock(lane)
                    lane_context.__enter__()
                    acquired = True
                    _store_tickets(tickets_path, tickets[1:])
            if not acquired:
                time.sleep(0.01)
        try:
            yield
        finally:
            lane_context.__exit__(None, None, None)
    finally:
        if not acquired:
            with _file_lock(guard):
                tickets = _load_tickets(tickets_path)
                _store_tickets(tickets_path, [
                    item for item in tickets if item.get("id") != ticket["id"]
                ])


def read_task(path: str | None, limit: int) -> bytes:
    try:
        if path:
            with Path(path).open("rb") as handle:
                raw = handle.read(limit + 1)
        else:
            raw = sys.stdin.buffer.read(limit + 1)
    except OSError as error:
        raise InputError(f"could not read task: {error}") from error
    if len(raw) > limit:
        raise InputError(f"task exceeds {limit} bytes")
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise InputError(f"task is not valid JSON: {error}") from error
    if not isinstance(value, dict):
        raise InputError("task must be a JSON object")
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def read_queue_manifest(path: str | None, limit: int) -> bytes:
    raw = read_task(path, limit)
    value = json.loads(raw)
    unknown = sorted(set(value) - {"tasks", "stop_on_error"})
    if unknown:
        raise InputError(f"queue manifest has unknown fields: {', '.join(unknown)}")
    tasks = value.get("tasks")
    if (not isinstance(tasks, list) or not 1 <= len(tasks) <= 32
            or any(not isinstance(task, dict) for task in tasks)):
        raise InputError("queue tasks must contain 1 to 32 JSON objects")
    stop_on_error = value.get("stop_on_error", False)
    if not isinstance(stop_on_error, bool):
        raise InputError("queue stop_on_error must be boolean")
    manifest = {"tasks": tasks, "stop_on_error": stop_on_error}
    encoded = json.dumps(
        manifest, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    if len(encoded) > limit:
        raise InputError(f"task exceeds {limit} bytes")
    return encoded


def read_resume_request(path: str | None, limit: int) -> dict[str, Any]:
    value = json.loads(read_task(path, limit))
    unknown = sorted(set(value) - {"backend", "token", "permission_resolution"})
    if unknown:
        raise InputError(f"resume request has unknown fields: {', '.join(unknown)}")
    backend = value.get("backend")
    if not isinstance(backend, str) or not ID_PATTERN.fullmatch(backend):
        raise InputError("resume backend must be a valid agent id")
    token = value.get("token")
    if (not isinstance(token, str) or not token
            or len(token.encode("utf-8")) > 4096 or "\0" in token):
        raise InputError("resume token must be a bounded non-empty string")
    resolution = value.get("permission_resolution")
    if not isinstance(resolution, dict):
        raise InputError("permission_resolution must be an object")
    unknown_resolution = sorted(
        set(resolution) - {"request_id", "decision", "result"}
    )
    if unknown_resolution:
        raise InputError(
            "permission_resolution has unknown fields: "
            + ", ".join(unknown_resolution)
        )
    request_id = resolution.get("request_id")
    if (not isinstance(request_id, str) or not request_id
            or len(request_id.encode("utf-8")) > 4096 or "\0" in request_id):
        raise InputError("permission_resolution request_id must be bounded")
    decision = resolution.get("decision")
    if decision not in ("allow", "deny", "handled"):
        raise InputError("permission_resolution decision must be allow, deny, or handled")
    if decision == "handled":
        if not isinstance(resolution.get("result"), dict):
            raise InputError("handled permission_resolution requires a result object")
    elif "result" in resolution:
        raise InputError("permission_resolution result is valid only for handled")
    return value


def invoke(agent: dict[str, Any], raw_task: bytes,
           timeout_seconds: float | None = None) -> tuple[dict[str, Any], int, bool]:
    binding = agent["binding"]
    argv = binding["argv"]
    scratch = state_root() / "capture"
    scratch.mkdir(parents=True, exist_ok=True)
    try:
        with tempfile.TemporaryFile(dir=scratch) as stdout_file, \
                tempfile.TemporaryFile(dir=scratch) as stderr_file:
            child_env = os.environ.copy()
            inference = agent.get("inference")
            if inference is None:
                child_env.pop(INFERENCE_ENV, None)
            else:
                child_env[INFERENCE_ENV] = json.dumps(
                    inference, separators=(",", ":"), sort_keys=True
                )
            process = subprocess.Popen(
                argv, stdin=subprocess.PIPE, stdout=stdout_file, stderr=stderr_file,
                env=child_env, start_new_session=(os.name != "nt"),
            )
            try:
                process.communicate(
                    raw_task,
                    timeout=timeout_seconds if timeout_seconds is not None
                    else binding["timeout_seconds"],
                )
            except subprocess.TimeoutExpired:
                if os.name != "nt":
                    os.killpg(process.pid, signal.SIGTERM)
                else:
                    process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    if os.name != "nt":
                        os.killpg(process.pid, signal.SIGKILL)
                    else:
                        process.kill()
                    process.wait()
                stderr_file.seek(0)
                return ({
                    "schema_version": 1, "classification": "timeout",
                    "status": "timeout", "backend": agent["id"],
                    "stderr": stderr_file.read(65536).decode("utf-8", "replace"),
                }, 124, True)
            stdout_size = stdout_file.tell()
            max_output = binding.get("max_output_bytes", DEFAULT_MAX_OUTPUT)
            if stdout_size > max_output:
                return ({
                    "schema_version": 1, "classification": "invalid_receipt",
                    "status": "invalid_receipt", "backend": agent["id"],
                    "error": f"receipt exceeds {max_output} bytes",
                }, 65, True)
            stdout_file.seek(0)
            stdout = stdout_file.read(max_output + 1)
            stderr_file.seek(0)
            stderr = stderr_file.read(65536)
    except OSError as error:
        return ({
            "schema_version": 1, "classification": "launch_failed",
            "status": "launch_failed", "backend": agent["id"], "error": str(error),
        }, 71, False)

    # Once Popen succeeds this backend owns the attempt. Never replay the task on
    # another route member, even if it times out or returns a malformed receipt.
    try:
        receipt = json.loads(stdout)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        return ({
            "schema_version": 1, "classification": "invalid_receipt",
            "status": "invalid_receipt", "backend": agent["id"],
            "error": f"backend returned invalid JSON: {error}",
            "stderr": stderr[:65536].decode("utf-8", "replace"),
        }, 65, True)
    if not isinstance(receipt, dict):
        return ({
            "schema_version": 1, "classification": "invalid_receipt",
            "status": "invalid_receipt", "backend": agent["id"],
            "error": "backend receipt must be a JSON object",
        }, 65, True)
    return receipt, process.returncode, True


def _cooperative_envelope(operation: str, quantum: dict[str, Any], *,
                          task: dict[str, Any] | None = None,
                          token: str | None = None,
                          reason: str | None = None,
                          permission_resolution: dict[str, Any] | None = None) -> bytes:
    value: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION, "adapter_protocol": "cooperative-v1",
        "operation": operation,
        "quantum": quantum,
    }
    if task is not None:
        value["task"] = task
    if token is not None:
        value["token"] = token
    if reason is not None:
        value["reason"] = reason
    if permission_resolution is not None:
        value["permission_resolution"] = permission_resolution
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _cooperative_state(receipt: dict[str, Any], operation: str) -> tuple[str, str | None]:
    state = receipt.get("state")
    allowed = ("ready", "failed") if operation == "start" else (
        "yielded", "permission_required", "complete", "failed"
    )
    if state not in allowed:
        raise InputError(
            f"cooperative {operation} receipt has invalid state"
        )
    token = receipt.get("token")
    if state in ("ready", "yielded", "permission_required") and (
        not isinstance(token, str) or not token or len(token.encode("utf-8")) > 4096
        or "\0" in token
    ):
        raise InputError(
            f"cooperative {state} receipt requires a bounded token"
        )
    if token is not None and not isinstance(token, str):
        raise InputError("cooperative token must be a string")
    return state, token


def _cancel_cooperative(agent: dict[str, Any], token: str,
                        quantum: dict[str, Any], deadline: float,
                        reason: str) -> dict[str, Any]:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return {"state": "failed", "classification": "cancel_timeout"}
    try:
        with fair_step_lock(agent, deadline):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("timed out before cooperative cancel")
            receipt, _, launched = invoke(
                agent, _cooperative_envelope(
                    "cancel", quantum, token=token, reason=reason
                ), remaining,
            )
    except TimeoutError:
        return {"state": "failed", "classification": "cancel_timeout"}
    if not launched:
        return receipt
    return receipt


def run_cooperative(agent: dict[str, Any], tasks: list[dict[str, Any]],
                    stop_on_error: bool,
                    resume: dict[str, Any] | None = None) -> tuple[dict[str, Any], int]:
    """Run tasks through cooperative-v1, rotating after every bounded slice."""
    policy = agent["queue_policy"]
    quantum = policy["quantum"]
    if resume is None:
        pending: list[dict[str, Any]] = [
            {"index": index, "task": task, "operation": "start", "token": None,
             "slices": 0, "deadline": None, "permission_resolution": None}
            for index, task in enumerate(tasks)
        ]
    else:
        pending = [{
            "index": 0, "task": None, "operation": "step",
            "token": resume["token"], "slices": 0, "deadline": None,
            "permission_resolution": resume["permission_resolution"],
        }]
    requested = len(pending)
    jobs: list[dict[str, Any]] = []
    terminal_failure = False
    while pending:
        item = pending.pop(0)
        if item["deadline"] is None:
            item["deadline"] = (
                time.monotonic() + agent["binding"]["timeout_seconds"]
            )
        deadline = item["deadline"]
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            receipt = {
                "schema_version": 1, "state": "failed",
                "classification": "timeout", "status": "timeout",
                "error": "cooperative task exceeded the multiplexer timeout",
            }
            launched = True
            status = 124
        else:
            envelope = _cooperative_envelope(
                item["operation"], quantum,
                task=item["task"] if item["operation"] == "start" else None,
                token=item["token"],
                permission_resolution=item["permission_resolution"],
            )
            item["permission_resolution"] = None
            if len(envelope) > agent["binding"]["max_input_bytes"]:
                receipt = {
                    "schema_version": 1, "state": "failed",
                    "classification": "invalid_request", "status": "invalid_request",
                    "error": "cooperative envelope exceeds backend input limit",
                }
                status, launched = 64, True
            else:
                try:
                    with fair_step_lock(agent, deadline):
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise TimeoutError("timed out before cooperative step")
                        receipt, status, launched = invoke(agent, envelope, remaining)
                except TimeoutError as error:
                    receipt = {
                        "schema_version": 1, "state": "failed",
                        "classification": "timeout", "status": "timeout",
                        "error": str(error),
                    }
                    status, launched = 124, True
        if item["operation"] == "step":
            item["slices"] += 1
        if not launched:
            # Start/step launch failure is terminal for this task and is never
            # replayed on a different route member.
            receipt = dict(receipt)
            receipt["state"] = "failed"
            state, token = "failed", None
        else:
            try:
                state, token = _cooperative_state(receipt, item["operation"])
            except InputError as error:
                receipt = {
                    "schema_version": 1, "state": "failed",
                    "classification": "invalid_receipt", "status": "invalid_receipt",
                    "error": str(error), "backend_receipt": receipt,
                }
                state, token, status = "failed", None, 65
        if state in ("ready", "yielded", "permission_required") and status != 0:
            receipt = {
                "schema_version": 1, "state": "failed",
                "classification": "adapter_error", "status": "adapter_error",
                "error": "cooperative adapter exited nonzero for a resumable state",
                "adapter_exit_code": status, "backend_receipt": receipt,
            }
            state, token = "failed", None
        if state == "yielded" and "retry_after_seconds" in receipt:
            retry_after = receipt["retry_after_seconds"]
            if (not isinstance(retry_after, (int, float))
                    or isinstance(retry_after, bool)
                    or not 0 <= retry_after <= 60):
                receipt = {
                    "schema_version": 1, "state": "failed",
                    "classification": "invalid_receipt", "status": "invalid_receipt",
                    "error": "cooperative retry_after_seconds must be between 0 and 60",
                    "backend_receipt": receipt,
                }
                state, token, status = "failed", None, 65
            elif retry_after:
                time.sleep(min(float(retry_after), max(0.0, deadline - time.monotonic())))
        if state in ("ready", "yielded"):
            item["operation"] = "step"
            item["token"] = token
            pending.append(item)
            continue
        if state == "permission_required":
            job = dict(receipt)
            job["adapter_exit_code"] = status
            job["queue_index"] = item["index"]
            job["slices"] = item["slices"]
            job["exit_code"] = 9
            jobs.append(job)
            continue
        job = dict(receipt)
        task_status = job.get("exit_code", status)
        if (not isinstance(task_status, int) or isinstance(task_status, bool)
                or task_status < 0):
            job = {
                "schema_version": 1, "state": "failed",
                "classification": "invalid_receipt", "status": "invalid_receipt",
                "error": "cooperative terminal exit_code must be a non-negative integer",
                "backend_receipt": receipt,
            }
            state, task_status = "failed", 65
        job["adapter_exit_code"] = status
        job["queue_index"] = item["index"]
        job["slices"] = item["slices"]
        job["exit_code"] = task_status
        jobs.append(job)
        if state == "failed" or task_status != 0:
            terminal_failure = True
            if stop_on_error:
                for waiting in pending:
                    cancelled: dict[str, Any] | None = None
                    if waiting["token"] is not None:
                        cancelled = _cancel_cooperative(
                            agent, waiting["token"], quantum,
                            waiting["deadline"] or (
                                time.monotonic() + agent["binding"]["timeout_seconds"]
                            ),
                            "stop_on_error",
                        )
                    jobs.append({
                        "schema_version": 1, "state": "cancelled"
                        if waiting["token"] is not None else "skipped",
                        "classification": "cancelled"
                        if waiting["token"] is not None else "skipped",
                        "status": "cancelled"
                        if waiting["token"] is not None else "skipped",
                        "queue_index": waiting["index"],
                        "slices": waiting["slices"],
                        **({"cancel_receipt": cancelled} if cancelled is not None else {}),
                    })
                pending.clear()
    jobs.sort(key=lambda job: job["queue_index"])
    completed = sum(job["state"] in ("complete", "failed") for job in jobs)
    succeeded = sum(job["state"] == "complete" and job.get("exit_code", 0) == 0
                    for job in jobs)
    failed = sum(job["state"] == "failed" or job.get("exit_code", 0) != 0
                 for job in jobs if job["state"] != "permission_required")
    permissions = sum(job["state"] == "permission_required" for job in jobs)
    classification = (
        "partial_failure" if terminal_failure else
        "permission_required" if permissions else
        "success"
    )
    counts = {"requested": requested, "completed": completed,
              "succeeded": succeeded, "failed": failed,
              "skipped": requested - completed - permissions}
    if permissions:
        counts["permission_required"] = permissions
    result = {
        "schema_version": 1, "classification": classification,
        "status": classification, "backend": agent["id"],
        "protocol": "cooperative-v1", "queue_policy": policy,
        "stop_on_error": stop_on_error,
        "counts": counts,
        "jobs": jobs,
    }
    return result, 1 if terminal_failure else 9 if permissions else 0


def parser() -> argparse.ArgumentParser:
    base = Path(__file__).resolve().parents[2]
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--catalog", type=Path, default=base / "agents" / "catalog")
    result.add_argument("--routes", type=Path, default=base / "agents" / "multiplexer.json")
    commands = result.add_subparsers(dest="command", required=True)
    commands.add_parser("validate")
    listing = commands.add_parser("list")
    listing.add_argument("--route")
    for name in ("select", "run", "queue", "resume"):
        command = commands.add_parser(name)
        command.add_argument("--route", required=True)
        command.add_argument("--runtime")
        command.add_argument("--platform", default=runtime_platform())
        command.add_argument("--mode")
        command.add_argument("--workspace")
        command.add_argument("--delivery")
        command.add_argument("--require", action="append", default=[])
        if name == "select":
            command.add_argument("--delegation-queue", action="store_true")
        if name in ("run", "queue"):
            command.add_argument("--task-file")
        if name == "resume":
            command.add_argument("--resolution-file")
    return result


def emit(value: dict[str, Any] | list[Any]) -> None:
    print(json.dumps(value, sort_keys=True, ensure_ascii=False))


def main() -> int:
    args = parser().parse_args()
    try:
        agents = load_catalog(args.catalog)
        routes = load_routes(args.routes, agents)
        if args.command == "validate":
            emit({
                "schema_version": 1, "classification": "success", "status": "success",
                "agents": len(agents), "routes": len(routes),
            })
            return 0
        if args.command == "list":
            if args.route is None:
                selected = [agents[key] for key in sorted(agents)]
            else:
                if args.route not in routes:
                    raise InputError(f"unknown route: {args.route}")
                selected = [agents[key] for key in routes[args.route]]
            emit(selected)
            return 0
        filters = {name: getattr(args, name) for name in (
            "runtime", "platform", "mode", "workspace", "delivery",
        )}
        required = list(args.require)
        queue_only = args.command in ("queue", "resume") or (
            args.command == "select" and args.delegation_queue
        )
        if queue_only and "batch" not in required:
            required.append("batch")
        selected = candidates(args.route, routes, agents, filters, required)
        if queue_only:
            selected = [agent for agent in selected if agent["delegation_queue"]]
        if not selected:
            emit({
                "schema_version": 1, "classification": "no_backend",
                "status": "no_backend", "route": args.route,
            })
            return 69
        if args.command == "select":
            emit(selected[0])
            return 0
        if args.command == "resume":
            request = read_resume_request(args.resolution_file, DEFAULT_MAX_INPUT)
            selected = [agent for agent in selected
                        if agent["id"] == request["backend"]
                        and agent["binding"].get("protocol") == "cooperative-v1"]
            if not selected:
                emit({
                    "schema_version": 1, "classification": "no_backend",
                    "status": "no_backend", "route": args.route,
                    "backend": request["backend"],
                })
                return 69
            receipt, status = run_cooperative(
                selected[0], [], False, resume=request
            )
            emit(receipt)
            return status
        if args.command == "queue":
            agent = selected[0]
            limit = min(agent["binding"].get("max_input_bytes", DEFAULT_MAX_INPUT),
                        DEFAULT_MAX_INPUT)
            task = read_queue_manifest(args.task_file, limit)
            if agent["binding"].get("protocol", "oneshot") == "cooperative-v1":
                manifest = json.loads(task)
                receipt, status = run_cooperative(
                    agent, manifest["tasks"], manifest["stop_on_error"]
                )
                emit(receipt)
                return status
            with concurrency_lock(agent):
                receipt, status, _ = invoke(agent, task)
            emit(receipt)
            return status
        task = read_task(args.task_file, DEFAULT_MAX_INPUT) if args.command == "run" else None
        last_launch_failure: dict[str, Any] | None = None
        for agent in selected:
            if agent["native"]:
                emit({
                    "schema_version": 1, "classification": "native_required",
                    "status": "native_required", "backend": agent["id"],
                    "native": agent["binding"],
                })
                return 69
            limit = min(agent["binding"].get("max_input_bytes", DEFAULT_MAX_INPUT),
                        DEFAULT_MAX_INPUT)
            if len(task) > limit:
                continue
            if agent["binding"].get("protocol", "oneshot") == "cooperative-v1":
                receipt, status = run_cooperative(agent, [json.loads(task)], False)
                emit(receipt)
                return status
            with concurrency_lock(agent):
                receipt, status, launched = invoke(agent, task)
            if launched:
                emit(receipt)
                return status
            last_launch_failure = receipt
        emit(last_launch_failure or {
            "schema_version": 1, "classification": "no_backend",
            "status": "no_backend", "route": args.route,
        })
        return 71 if last_launch_failure else 69
    except (ConfigurationError, InputError) as error:
        emit({
            "schema_version": 1, "classification": "configuration_error"
            if isinstance(error, ConfigurationError) else "invalid_request",
            "status": "configuration_error"
            if isinstance(error, ConfigurationError) else "invalid_request",
            "error": str(error),
        })
        return 64


if __name__ == "__main__":
    raise SystemExit(main())
