#!/usr/bin/env python3
"""Validate, select, and run priority-ordered delegation workers."""
from __future__ import annotations

import argparse
import atexit
import contextlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Iterator

SCHEMA_VERSION = 1
DEFAULT_MAX_INPUT = 1024 * 1024
DEFAULT_MAX_OUTPUT = 2 * 1024 * 1024
ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
INFERENCE_ENV = "AGENT_INFERENCE_CONFIG"
MAX_PRIORITY = 100
DEFAULT_COMMAND_CONCURRENCY = 4
DEFAULT_COMMAND_TIMEOUT = 900
MAX_COMMAND_OUTPUT = 24 * 1024
MAX_HANDLED_RESULT = 60 * 1024


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
        "delegation_queue", "priority", "provider", "model", "binding",
        "capabilities", "limits", "queue_policy", "inference",
    }, source)
    required = {
        "schema_version", "id", "name", "description", "native",
        "delegation_queue", "priority", "provider", "model", "binding",
        "capabilities", "limits",
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
    priority = agent["priority"]
    if (not isinstance(priority, int) or isinstance(priority, bool)
            or not 0 <= priority <= MAX_PRIORITY):
        raise ConfigurationError(
            f"{source}: priority must be an integer from 0 to {MAX_PRIORITY}"
        )
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
        _reject_unknown(policy, {
            "strategy", "virtual_slots", "quantum", "command_concurrency",
            "command_timeout_seconds",
        },
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
        command_concurrency = policy.get(
            "command_concurrency", DEFAULT_COMMAND_CONCURRENCY
        )
        _positive_int(
            command_concurrency, f"{source}: queue_policy.command_concurrency"
        )
        if command_concurrency > 32:
            raise ConfigurationError(
                f"{source}: queue_policy.command_concurrency must not exceed 32"
            )
        command_timeout = policy.get(
            "command_timeout_seconds",
            min(DEFAULT_COMMAND_TIMEOUT, agent["binding"]["timeout_seconds"]),
        )
        _positive_int(
            command_timeout, f"{source}: queue_policy.command_timeout_seconds"
        )
        if command_timeout > agent["binding"]["timeout_seconds"]:
            raise ConfigurationError(
                f"{source}: queue_policy.command_timeout_seconds must not exceed "
                "binding.timeout_seconds"
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
        if quantum["value"] > 100:
            raise ConfigurationError(
                f"{source}: queue_policy.quantum.value must not exceed 100"
            )
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
    selected = [agents[agent_id] for agent_id in routes[route]
                if matches(agents[agent_id], filters, required)
                and is_available(agents[agent_id])]
    return sorted(selected, key=lambda agent: (-agent["priority"], agent["id"]))


def runtime_platform() -> str:
    return {"win32": "windows", "darwin": "darwin"}.get(
        sys.platform, sys.platform
    )


def select_queue_backend(catalog_dir: Path, routes_path: Path, route: str,
                         runtime: str, platform: str | None = None
                         ) -> dict[str, Any] | None:
    """Return the highest-priority available queue backend on a validated route."""
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
    configured = (
        os.environ.get("AGENT_MUX_SCHEDULER_STATE_DIR")
        or os.environ.get("AGENT_MULTIPLEXER_STATE_DIR")
    )
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
    """Serialize whole oneshot invocations of a single-slot backend.

    This and fair_step_lock() below both guard the same one provider slot, and
    that duplication is deliberate rather than a merge candidate. A oneshot
    invocation holds the slot for its entire run, so a plain mutual-exclusion
    lock is the right shape and order between waiters does not matter. A
    cooperative round-robin run instead releases the slot between steps, so it
    needs ticket ordering to keep one virtual agent from starving the rest.
    Collapsing them would either serialize round-robin batches at invocation
    granularity or make every oneshot pay for ticket bookkeeping it cannot use.
    """
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
    """Acquire the single provider lane in cross-process FIFO ticket order.

    Step-granular counterpart to concurrency_lock(); see the note there for why
    both exist.
    """
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
                _terminate_process_group(process)
                stderr_file.seek(0)
                return ({
                    "schema_version": 1, "classification": "timeout",
                    "status": "timeout", "backend": agent["id"],
                    "stderr": stderr_file.read(65536).decode("utf-8", "replace"),
                }, 124, True)
            except BaseException:
                _terminate_process_group(process)
                raise
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


def _command_environment(argv: list[str]) -> dict[str, str]:
    """Build a small execution environment, never a credential denylist."""
    allowed = {
        "PATH", "LANG", "LANGUAGE", "TERM", "COLORTERM", "TZ",
        "TMPDIR", "TMP", "TEMP", "SYSTEMROOT", "WINDIR", "COMSPEC",
        "PATHEXT",
    }
    child_env = {
        key: value for key, value in os.environ.items()
        if key in allowed or key.startswith("LC_")
    }
    if Path(argv[0]).name.lower() in ("git", "git.exe"):
        child_env.update({
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_PAGER": "cat",
        })
    return child_env


def _harden_command_argv(argv: list[str]) -> list[str]:
    """Neutralize repository-configured helpers for an authorized Git argv."""
    if Path(argv[0]).name.lower() not in ("git", "git.exe"):
        return argv
    arguments = list(argv[1:])
    index = 0
    while index < len(arguments):
        if arguments[index] == "-C":
            index += 2
            continue
        if arguments[index].startswith("-"):
            index += 1
            continue
        if arguments[index] in ("diff", "log", "show"):
            arguments.insert(index + 1, "--no-ext-diff")
        break
    return [
        argv[0],
        "-c", "core.fsmonitor=false",
        "-c", f"core.hooksPath={os.devnull}",
        "-c", "credential.helper=",
        "-c", "gpg.program=false",
        "-c", "protocol.file.allow=never",
        *arguments,
    ]


def _try_command_slot(agent_id: str, limit: int) -> Any | None:
    """Try to reserve one crash-safe backend command slot across processes."""
    root = state_root() / "command-slots" / agent_id
    root.mkdir(parents=True, exist_ok=True)
    for index in range(limit):
        handle = (root / f"{index}.lock").open("a+b")
        try:
            if os.name == "nt":
                import msvcrt
                handle.seek(0)
                handle.write(b"0")
                handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError):
            handle.close()
            continue
        return handle
    return None


def _release_command_slot(handle: Any) -> None:
    try:
        if os.name == "nt":
            import msvcrt
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


def _validated_mux_execution(receipt: dict[str, Any]) -> tuple[str, list[str], Path] | None:
    request = receipt.get("request")
    if not isinstance(request, dict) or request.get("tool_name") != "Bash":
        return None
    request_id = request.get("request_id")
    execution = request.get("mux_execution")
    if execution is None:
        return None
    if (not isinstance(request_id, str) or not request_id
            or len(request_id.encode("utf-8")) > 4096 or "\0" in request_id):
        raise InputError("mux command request_id must be bounded")
    if not isinstance(execution, dict) or set(execution) != {"argv", "cwd"}:
        raise InputError("mux_execution must contain only argv and cwd")
    argv = execution.get("argv")
    if (not isinstance(argv, list) or not 1 <= len(argv) <= 64
            or any(not isinstance(arg, str) or not arg or "\0" in arg
                   or len(arg.encode("utf-8")) > 4096 for arg in argv)):
        raise InputError("mux_execution argv must contain 1 to 64 bounded strings")
    cwd_value = execution.get("cwd")
    if not isinstance(cwd_value, str) or not Path(cwd_value).is_absolute():
        raise InputError("mux_execution cwd must be absolute")
    try:
        cwd = Path(cwd_value).resolve(strict=True)
    except OSError as error:
        raise InputError(f"mux_execution cwd is invalid: {error}") from error
    if not cwd.is_dir():
        raise InputError("mux_execution cwd must be a directory")
    return request_id, list(argv), cwd


def _terminate_process_group(process: subprocess.Popen[Any]) -> None:
    if os.name == "nt":
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=0.2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        if process.poll() is None:
            process.wait()
        return
    deadline = time.monotonic() + 0.2
    while time.monotonic() < deadline:
        try:
            os.killpg(process.pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.01)
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    if process.poll() is None:
        process.wait()


def _start_mux_command(item: dict[str, Any], receipt: dict[str, Any],
                       timeout_seconds: int, command_slot: Any
                       ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Start one authorized argv command, returning (job, immediate result)."""
    validated = _validated_mux_execution(receipt)
    if validated is None:
        return None, None
    request_id, argv, cwd = validated
    started = time.monotonic()
    process: subprocess.Popen[Any] | None = None
    try:
        process = subprocess.Popen(
            _harden_command_argv(argv), cwd=cwd, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=_command_environment(argv), start_new_session=(os.name != "nt"),
        )
    except OSError as error:
        _release_command_slot(command_slot)
        return None, _bounded_handled_resolution(request_id, {
            "argv": argv, "returncode": None, "stdout": "", "stderr": "",
            "duration_seconds": 0.0, "timed_out": False,
            "spawn_error": str(error), "stdout_truncated": False,
            "stderr_truncated": False,
        })
    except BaseException:
        if process is not None:
            _terminate_process_group(process)
        _release_command_slot(command_slot)
        raise
    stdout_capture = _start_bounded_capture(process.stdout)
    stderr_capture = _start_bounded_capture(process.stderr)
    return {
        "item": item, "request_id": request_id, "argv": argv,
        "process": process, "stdout_capture": stdout_capture,
        "stderr_capture": stderr_capture,
        "command_slot": command_slot,
        "started": started,
        "deadline": started + timeout_seconds,
    }, None


def _start_bounded_capture(stream: Any) -> dict[str, Any]:
    capture: dict[str, Any] = {
        "stream": stream, "data": bytearray(), "truncated": False,
    }

    def drain() -> None:
        try:
            while True:
                chunk = stream.read(65536)
                if not chunk:
                    return
                remaining = MAX_COMMAND_OUTPUT - len(capture["data"])
                if remaining > 0:
                    capture["data"].extend(chunk[:remaining])
                if len(chunk) > max(0, remaining):
                    capture["truncated"] = True
        finally:
            stream.close()

    thread = threading.Thread(target=drain, daemon=True)
    capture["thread"] = thread
    thread.start()
    return capture


def _finish_capture(capture: dict[str, Any]) -> tuple[str, bool]:
    thread = capture["thread"]
    thread.join(timeout=2)
    if thread.is_alive():
        capture["truncated"] = True
        try:
            capture["stream"].close()
        except OSError:
            pass
        thread.join(timeout=1)
    return bytes(capture["data"]).decode("utf-8", "replace"), capture["truncated"]


def _bounded_handled_resolution(request_id: str,
                                result: dict[str, Any]) -> dict[str, Any]:
    resolution = {
        "request_id": request_id, "decision": "handled", "result": dict(result),
    }
    value = resolution["result"]
    while len(json.dumps(resolution, ensure_ascii=False).encode("utf-8")) >= MAX_HANDLED_RESULT:
        candidates = [name for name in ("stdout", "stderr")
                      if isinstance(value.get(name), str) and value[name]]
        if candidates:
            name = max(candidates, key=lambda field: len(value[field]))
            value[name] = value[name][:max(0, len(value[name]) // 2)]
            value[f"{name}_truncated"] = True
            continue
        argv = value.get("argv")
        if (isinstance(argv, list) and argv
                and not value.get("argv_truncated")):
            value["argv"] = [str(argv[0])[:1024]]
            value["argv_truncated"] = True
            continue
        error = value.get("spawn_error")
        if (isinstance(error, str) and len(error) > 1024
                and not value.get("spawn_error_truncated")):
            value["spawn_error"] = error[:1024]
            value["spawn_error_truncated"] = True
            continue
        raise InputError("mux handled result cannot fit the bounded wire format")
    return resolution


def _discard_mux_command(job: dict[str, Any]) -> None:
    if job.get("discarded"):
        return
    job["discarded"] = True
    _terminate_process_group(job["process"])
    _finish_capture(job["stdout_capture"])
    _finish_capture(job["stderr_capture"])
    command_slot = job.pop("command_slot", None)
    if command_slot is not None:
        _release_command_slot(command_slot)


def _finish_mux_command(job: dict[str, Any], *, timed_out: bool = False) -> dict[str, Any]:
    process = job["process"]
    if timed_out:
        _terminate_process_group(process)
    else:
        process.wait()
        # A completed foreground command may have left descendants behind.
        _terminate_process_group(process)
    streams = [
        _finish_capture(job["stdout_capture"]),
        _finish_capture(job["stderr_capture"]),
    ]
    command_slot = job.pop("command_slot", None)
    if command_slot is not None:
        _release_command_slot(command_slot)
    result = {
        "argv": job["argv"], "returncode": process.returncode,
        "stdout": streams[0][0], "stderr": streams[1][0],
        "duration_seconds": round(time.monotonic() - job["started"], 3),
        "timed_out": timed_out, "spawn_error": None,
        "stdout_truncated": streams[0][1], "stderr_truncated": streams[1][1],
    }
    return _bounded_handled_resolution(job["request_id"], result)


def _cooperative_envelope(operation: str, quantum: dict[str, Any], *,
                          task: dict[str, Any] | None = None,
                          token: str | None = None,
                          reason: str | None = None,
                          permission_resolution: dict[str, Any] | None = None) -> bytes:
    value: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION, "adapter_protocol": "cooperative-v1",
        "scheduler": {
            "protocol_version": 1,
            "capabilities": ["mux-command-execution-v1"],
        },
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


def _cooperative_receipt(operation: str, **fields: Any) -> dict[str, Any]:
    """Build a scheduler-owned receipt that obeys the adapter identity contract."""
    return {
        "schema_version": SCHEMA_VERSION,
        "adapter_protocol": "cooperative-v1",
        "operation": operation,
        **fields,
    }


def _cooperative_state(receipt: dict[str, Any], operation: str) -> tuple[str, str | None]:
    if receipt.get("schema_version") != SCHEMA_VERSION:
        raise InputError("cooperative receipt has unsupported schema_version")
    if receipt.get("adapter_protocol") != "cooperative-v1":
        raise InputError("cooperative receipt has invalid adapter_protocol")
    if receipt.get("operation") != operation:
        raise InputError("cooperative receipt did not echo the requested operation")
    classification = receipt.get("classification")
    if (not isinstance(classification, str) or not classification
            or len(classification.encode("utf-8")) > 128 or "\0" in classification):
        raise InputError("cooperative receipt requires a classification")
    state = receipt.get("state")
    allowed = {
        "start": ("ready", "failed"),
        "step": ("yielded", "permission_required", "complete", "failed"),
        "cancel": ("cancelled", "failed"),
    }[operation]
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
    if state in ("complete", "cancelled", "failed") and token is not None:
        raise InputError("cooperative terminal receipt must not retain a token")
    exit_code = receipt.get("exit_code")
    if state in ("ready", "yielded", "permission_required") and exit_code is not None:
        raise InputError("cooperative resumable receipt must not include exit_code")
    if state in ("complete", "cancelled", "failed") and (
        not isinstance(exit_code, int) or isinstance(exit_code, bool) or exit_code < 0
    ):
        raise InputError("cooperative terminal receipt requires a non-negative exit_code")
    return state, token


def _cancel_cooperative(agent: dict[str, Any], token: str,
                        quantum: dict[str, Any], deadline: float,
                        reason: str) -> dict[str, Any]:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return _cooperative_receipt(
            "cancel", state="failed", classification="cancel_timeout",
            exit_code=124,
        )
    try:
        with fair_step_lock(agent, deadline):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("timed out before cooperative cancel")
            receipt, status, launched = invoke(
                agent, _cooperative_envelope(
                    "cancel", quantum, token=token, reason=reason
                ), remaining,
            )
    except TimeoutError:
        return _cooperative_receipt(
            "cancel", state="failed", classification="cancel_timeout",
            exit_code=124,
        )
    if not launched:
        return _cooperative_receipt(
            "cancel", state="failed",
            classification=receipt.get("classification", "launch_failed"),
            status=receipt.get("status", "launch_failed"),
            exit_code=status if status >= 0 else 71,
            backend_receipt=receipt,
        )
    try:
        state, _ = _cooperative_state(receipt, "cancel")
    except InputError as error:
        return _cooperative_receipt(
            "cancel", state="failed", classification="invalid_receipt",
            status="invalid_receipt", exit_code=65, error=str(error),
            backend_receipt=receipt,
        )
    if state == "cancelled" and status != 0:
        return _cooperative_receipt(
            "cancel", state="failed", classification="adapter_error",
            status="adapter_error", exit_code=status if status >= 0 else 65,
            adapter_exit_code=status,
            backend_receipt=receipt,
        )
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
    all_items = list(pending)
    virtual_slots = 1 if resume is not None else policy["virtual_slots"]
    queued_items = pending[virtual_slots:]
    pending = pending[:virtual_slots]
    jobs: list[dict[str, Any]] = []
    terminal_failure = False
    paused_permissions: list[tuple[dict[str, Any], dict[str, Any], int]] = []
    waiting_commands: list[tuple[dict[str, Any], dict[str, Any]]] = []
    running_commands: list[dict[str, Any]] = []
    command_requests = 0
    command_peak = 0
    command_limit = policy.get(
        "command_concurrency", DEFAULT_COMMAND_CONCURRENCY
    )
    command_timeout = policy.get(
        "command_timeout_seconds",
        min(DEFAULT_COMMAND_TIMEOUT, agent["binding"]["timeout_seconds"]),
    )
    def cleanup_commands() -> None:
        for command_job in list(running_commands):
            _discard_mux_command(command_job)
        for interrupted in all_items:
            token = interrupted.get("token")
            if not token:
                continue
            _cancel_cooperative(
                agent, token, quantum,
                time.monotonic() + agent["binding"]["timeout_seconds"],
                "scheduler_interrupted",
            )

    atexit.register(cleanup_commands)
    while pending or waiting_commands or running_commands or queued_items:
        while (queued_items and
               len(pending) + len(waiting_commands) + len(running_commands)
               < virtual_slots):
            pending.append(queued_items.pop(0))
        now = time.monotonic()
        for command_job in list(running_commands):
            process = command_job["process"]
            timed_out = now >= command_job["deadline"] and process.poll() is None
            if process.poll() is None and not timed_out:
                continue
            item = command_job["item"]
            item["permission_resolution"] = _finish_mux_command(
                command_job, timed_out=timed_out
            )
            running_commands.remove(command_job)
            item["deadline"] += time.monotonic() - item.pop(
                "command_wait_started"
            )
            item["operation"] = "step"
            pending.append(item)
        while waiting_commands and len(running_commands) < command_limit:
            command_slot = _try_command_slot(agent["id"], command_limit)
            if command_slot is None:
                break
            item, command_receipt = waiting_commands.pop(0)
            command_job, immediate = _start_mux_command(
                item, command_receipt, command_timeout, command_slot
            )
            if immediate is not None:
                item["deadline"] += time.monotonic() - item.pop(
                    "command_wait_started"
                )
                item["permission_resolution"] = immediate
                item["operation"] = "step"
                pending.append(item)
            elif command_job is not None:
                running_commands.append(command_job)
                command_peak = max(command_peak, len(running_commands))
        if not pending:
            time.sleep(0.01)
            continue
        item = pending.pop(0)
        if item["deadline"] is None:
            item["deadline"] = (
                time.monotonic() + agent["binding"]["timeout_seconds"]
            )
        deadline = item["deadline"]
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            receipt = _cooperative_receipt(
                item["operation"], state="failed",
                classification="timeout", status="timeout", exit_code=124,
                error="cooperative task exceeded the mux-scheduler timeout",
            )
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
                receipt = _cooperative_receipt(
                    item["operation"], state="failed",
                    classification="invalid_request", status="invalid_request",
                    exit_code=64,
                    error="cooperative envelope exceeds backend input limit",
                )
                status, launched = 64, True
            else:
                try:
                    with fair_step_lock(agent, deadline):
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise TimeoutError("timed out before cooperative step")
                        receipt, status, launched = invoke(agent, envelope, remaining)
                except TimeoutError as error:
                    receipt = _cooperative_receipt(
                        item["operation"], state="failed",
                        classification="timeout", status="timeout", exit_code=124,
                        error=str(error),
                    )
                    status, launched = 124, True
        if item["operation"] == "step":
            item["slices"] += 1
        if not launched:
            # Start/step launch failure is terminal for this task and is never
            # replayed on a different route member.
            receipt = _cooperative_receipt(
                item["operation"], state="failed",
                classification=receipt.get("classification", "launch_failed"),
                status=receipt.get("status", "launch_failed"),
                exit_code=status if status >= 0 else 71,
                backend_receipt=receipt,
            )
            state, token = "failed", None
        else:
            try:
                state, token = _cooperative_state(receipt, item["operation"])
            except InputError as error:
                receipt = _cooperative_receipt(
                    item["operation"], state="failed",
                    classification="invalid_receipt", status="invalid_receipt",
                    exit_code=65, error=str(error), backend_receipt=receipt,
                )
                state, token, status = "failed", None, 65
        if state in ("ready", "yielded", "permission_required") and status != 0:
            receipt = _cooperative_receipt(
                item["operation"], state="failed",
                classification="adapter_error", status="adapter_error",
                exit_code=status if status >= 0 else 65,
                error="cooperative adapter exited nonzero for a resumable state",
                adapter_exit_code=status, backend_receipt=receipt,
            )
            state, token = "failed", None
        if state == "yielded" and "retry_after_seconds" in receipt:
            retry_after = receipt["retry_after_seconds"]
            if (not isinstance(retry_after, (int, float))
                    or isinstance(retry_after, bool)
                    or not 0 <= retry_after <= 60):
                receipt = _cooperative_receipt(
                    item["operation"], state="failed",
                    classification="invalid_receipt", status="invalid_receipt",
                    exit_code=65,
                    error="cooperative retry_after_seconds must be between 0 and 60",
                    backend_receipt=receipt,
                )
                state, token, status = "failed", None, 65
            elif retry_after:
                time.sleep(min(float(retry_after), max(0.0, deadline - time.monotonic())))
        if state in ("ready", "yielded"):
            item["operation"] = "step"
            item["token"] = token
            pending.append(item)
            continue
        if state == "permission_required":
            try:
                mux_execution = _validated_mux_execution(receipt)
            except InputError as error:
                receipt = _cooperative_receipt(
                    item["operation"], state="failed",
                    classification="invalid_receipt", status="invalid_receipt",
                    exit_code=65, error=str(error), backend_receipt=receipt,
                )
                state, status = "failed", 65
            else:
                if mux_execution is not None:
                    item["operation"] = "step"
                    item["token"] = token
                    item["command_wait_started"] = time.monotonic()
                    waiting_commands.append((item, receipt))
                    command_requests += 1
                    continue
        if state == "permission_required":
            item["token"] = token
            paused_permissions.append((item, receipt, status))
            continue
        job = dict(receipt)
        task_status = job.get("exit_code", status)
        if (not isinstance(task_status, int) or isinstance(task_status, bool)
                or task_status < 0):
            job = _cooperative_receipt(
                item["operation"], state="failed",
                classification="invalid_receipt", status="invalid_receipt",
                exit_code=65,
                error="cooperative terminal exit_code must be a non-negative integer",
                backend_receipt=receipt,
            )
            state, task_status = "failed", 65
        job["adapter_exit_code"] = status
        job["queue_index"] = item["index"]
        job["slices"] = item["slices"]
        job["exit_code"] = task_status
        jobs.append(job)
        item["token"] = None
        if state == "failed" or task_status != 0:
            terminal_failure = True
            if stop_on_error:
                stopping = (
                    list(pending)
                    + list(queued_items)
                    + [waiting for waiting, _ in waiting_commands]
                    + [command_job["item"] for command_job in running_commands]
                    + [paused for paused, _, _ in paused_permissions]
                )
                for command_job in list(running_commands):
                    _discard_mux_command(command_job)
                seen_indexes: set[int] = set()
                for waiting in stopping:
                    if waiting["index"] in seen_indexes:
                        continue
                    seen_indexes.add(waiting["index"])
                    cancelled: dict[str, Any] | None = None
                    if waiting["token"] is not None:
                        cancelled = _cancel_cooperative(
                            agent, waiting["token"], quantum,
                            time.monotonic() + agent["binding"]["timeout_seconds"],
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
                queued_items.clear()
                waiting_commands.clear()
                running_commands.clear()
                paused_permissions.clear()
    for item, receipt, status in paused_permissions:
        job = dict(receipt)
        job["adapter_exit_code"] = status
        job["queue_index"] = item["index"]
        job["slices"] = item["slices"]
        job["exit_code"] = 9
        jobs.append(job)
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
        "command_execution": {
            "requested": command_requests,
            "peak_concurrency": command_peak,
            "limit": command_limit,
        },
        "counts": counts,
        "jobs": jobs,
    }
    atexit.unregister(cleanup_commands)
    return result, 1 if terminal_failure else 9 if permissions else 0


def parser() -> argparse.ArgumentParser:
    base = Path(__file__).resolve().parents[2]
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--catalog", type=Path, default=base / "agents" / "catalog")
    result.add_argument("--routes", type=Path, default=base / "agents" / "mux-scheduler.json")
    commands = result.add_subparsers(dest="command", required=True)
    commands.add_parser("validate")
    listing = commands.add_parser("list")
    listing.add_argument("--route")
    listing.add_argument("--runtime")
    listing.add_argument("--platform")
    listing.add_argument("--mode")
    listing.add_argument("--workspace")
    listing.add_argument("--delivery")
    listing.add_argument("--require", action="append", default=[])
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
            filters = {name: getattr(args, name) for name in (
                "runtime", "platform", "mode", "workspace", "delivery",
            )}
            required = list(args.require)
            if args.route is None:
                selected = [agents[key] for key in sorted(agents)]
            else:
                if args.route not in routes:
                    raise InputError(f"unknown route: {args.route}")
                selected = [agents[key] for key in routes[args.route]]
            if any(filters.values()) or required:
                selected = [agent for agent in selected
                            if matches(agent, filters, required)
                            and is_available(agent)]
                selected.sort(key=lambda agent: (-agent["priority"], agent["id"]))
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
    except KeyboardInterrupt:
        emit({
            "schema_version": 1, "classification": "cancelled",
            "status": "cancelled",
        })
        return 130


def _interrupt_scheduler(_signum: int, _frame: Any) -> None:
    raise KeyboardInterrupt


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, _interrupt_scheduler)
    raise SystemExit(main())
