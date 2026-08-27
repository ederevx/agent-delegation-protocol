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
from pathlib import Path
from typing import Any, Iterator

SCHEMA_VERSION = 1
DEFAULT_MAX_INPUT = 1024 * 1024
DEFAULT_MAX_OUTPUT = 2 * 1024 * 1024
ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")


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


def validate_agent(agent: Any, source: str) -> dict[str, Any]:
    if not isinstance(agent, dict):
        raise ConfigurationError(f"{source}: metadata must be a JSON object")
    _reject_unknown(agent, {
        "schema_version", "id", "name", "description", "native",
        "delegation_queue", "provider", "model", "binding", "capabilities",
        "limits",
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

    binding = agent["binding"]
    if not isinstance(binding, dict):
        raise ConfigurationError(f"{source}: binding must be an object")
    if not agent["native"]:
        _reject_unknown(binding, {
            "argv", "max_input_bytes", "max_output_bytes", "timeout_seconds",
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
def concurrency_lock(agent: dict[str, Any]) -> Iterator[None]:
    if agent["limits"]["max_concurrency"] != 1:
        yield
        return
    root = state_root() / "locks"
    root.mkdir(parents=True, exist_ok=True)
    lock_path = root / f"{agent['id']}.lock"
    handle = lock_path.open("a+b")
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


def invoke(agent: dict[str, Any], raw_task: bytes) -> tuple[dict[str, Any], int, bool]:
    binding = agent["binding"]
    argv = binding["argv"]
    scratch = state_root() / "capture"
    scratch.mkdir(parents=True, exist_ok=True)
    try:
        with tempfile.TemporaryFile(dir=scratch) as stdout_file, \
                tempfile.TemporaryFile(dir=scratch) as stderr_file:
            process = subprocess.Popen(
                argv, stdin=subprocess.PIPE, stdout=stdout_file, stderr=stderr_file,
                env=os.environ.copy(), start_new_session=(os.name != "nt"),
            )
            try:
                process.communicate(raw_task, timeout=binding["timeout_seconds"])
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


def parser() -> argparse.ArgumentParser:
    base = Path(__file__).resolve().parents[2]
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--catalog", type=Path, default=base / "agents" / "catalog")
    result.add_argument("--routes", type=Path, default=base / "agents" / "multiplexer.json")
    commands = result.add_subparsers(dest="command", required=True)
    commands.add_parser("validate")
    listing = commands.add_parser("list")
    listing.add_argument("--route")
    for name in ("select", "run", "queue"):
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
        queue_only = args.command == "queue" or (
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
        if args.command == "queue":
            agent = selected[0]
            limit = min(agent["binding"].get("max_input_bytes", DEFAULT_MAX_INPUT),
                        DEFAULT_MAX_INPUT)
            task = read_queue_manifest(args.task_file, limit)
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
