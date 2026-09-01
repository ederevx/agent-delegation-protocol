#!/usr/bin/env python3
"""Claude Code runtime profile for protocol-managed deployments.

The deployment controller owns credentials, gateway admission, and client
registrations.  This module owns only Claude-specific argv, environment,
isolated settings, process supervision, and background-session handoff.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any


CONTROL_COMMANDS = frozenset({"agents", "logs", "stop", "kill", "rm", "respawn"})
VALID_EFFORTS = frozenset({"low", "medium", "high", "xhigh", "max", "unset", "auto"})
VALIDATION_ERROR = 64
RUNTIME_ERROR = 78
BACKEND_MISSING = 127


class RuntimeProfileError(ValueError):
    """The deployment or invocation cannot be represented safely."""

    def __init__(self, message: str, status: int = VALIDATION_ERROR):
        super().__init__(message)
        self.status = status


def _object(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise RuntimeProfileError(f"{label} must be an object")
    return value


def _positive_int(value: object, label: str, default: int,
                  maximum: int | None = None) -> int:
    if value is None:
        return default
    if isinstance(value, bool):
        raise RuntimeProfileError(f"{label} must be a positive integer")
    try:
        result = int(str(value), 10)
    except (TypeError, ValueError) as error:
        raise RuntimeProfileError(f"{label} must be a positive integer") from error
    if result < 1 or (maximum is not None and result > maximum):
        suffix = f" from 1 to {maximum}" if maximum is not None else ""
        raise RuntimeProfileError(f"{label} must be a positive integer{suffix}")
    return result


def validate_arguments(arguments: Sequence[str]) -> None:
    """Reserve Claude's single effective settings source for this profile."""
    for argument in arguments:
        if argument == "--":
            return
        if argument == "--settings" or argument.startswith("--settings="):
            raise RuntimeProfileError(
                "--settings is managed by the Claude runtime profile; put "
                "custom settings in the isolated session or project settings")


def _runtime(deployment: Mapping[str, Any]) -> Mapping[str, Any]:
    runtime = _object(deployment.get("runtime"), "runtime")
    if runtime.get("profile") != "claude-code":
        raise RuntimeProfileError("runtime.profile must be 'claude-code'")
    return runtime


def _inference(deployment: Mapping[str, Any]) -> Mapping[str, Any]:
    return _object(deployment.get("inference"), "inference")


def _session_dir(runtime: Mapping[str, Any], environ: Mapping[str, str]) -> Path:
    session = _object(runtime.get("session", {}), "runtime.session")
    override_name = session.get("environment", "DELEGATION_CLAUDE_CONFIG_DIR")
    if not isinstance(override_name, str) or not override_name:
        raise RuntimeProfileError("runtime.session.environment must be a string")
    configured = session.get("config_dir")
    raw = environ.get(override_name) or configured
    if raw is None:
        deployment_id = str(runtime.get("deployment_id", "default"))
        raw = str(Path.home() / ".config" / "delegation" / "sessions" / deployment_id)
    if not isinstance(raw, str) or not raw:
        raise RuntimeProfileError("runtime.session.config_dir must be a string")
    return Path(os.path.expandvars(os.path.expanduser(raw))).resolve()


def _resolve_executable(runtime: Mapping[str, Any],
                        environ: Mapping[str, str]) -> str:
    descriptor = runtime.get("executable", {})
    if isinstance(descriptor, str):
        command, override_name = descriptor, "DELEGATION_CLAUDE_BIN"
    else:
        executable = _object(descriptor, "runtime.executable")
        command = executable.get("command", "claude")
        override_name = executable.get("environment", "DELEGATION_CLAUDE_BIN")
    if not isinstance(command, str) or not command:
        raise RuntimeProfileError("runtime.executable.command must be a string")
    if not isinstance(override_name, str) or not override_name:
        raise RuntimeProfileError("runtime.executable.environment must be a string")
    requested = environ.get(override_name) or command
    resolved = shutil.which(requested) or (requested if Path(requested).is_file() else None)
    if not resolved:
        raise RuntimeProfileError(
            f"Claude executable {requested!r} was not found", BACKEND_MISSING)
    if Path(resolved).name.casefold() in {
            "ci-claude", "ci-claude.cmd", "ci-claude.exe",
            "ci-claude-v3.sh", "ci-claude-v3.py", "ci-claude-v3.cmd"}:
        raise RuntimeProfileError(
            "refusing to launch the ci-claude wrapper recursively",
            BACKEND_MISSING)
    return resolved


def _has_effort(arguments: Sequence[str]) -> bool:
    for argument in arguments:
        if argument == "--":
            break
        if argument == "--effort" or argument.startswith("--effort="):
            return True
    return False


def _write_json_atomic(path: Path, payload: object) -> None:
    encoded = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def configure_session(deployment: Mapping[str, Any], session_dir: Path) -> Path:
    """Merge protocol-managed Claude settings without erasing user settings."""
    runtime = _runtime(deployment)
    session = _object(runtime.get("session", {}), "runtime.session")
    path = session_dir / "settings.json"
    if path.exists():
        try:
            settings = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise RuntimeProfileError(f"{path} is invalid JSON: {error}") from error
        settings = dict(_object(settings, str(path)))
    else:
        settings = {}
    permissions = settings.setdefault("permissions", {})
    environment = settings.setdefault("env", {})
    if not isinstance(permissions, dict) or not isinstance(environment, dict):
        raise RuntimeProfileError(
            f"{path} permissions and env values must be objects")
    permissions["defaultMode"] = session.get("permission_mode", "auto")
    maximum = _positive_int(session.get("max_agents"),
                            "runtime.session.max_agents", 4)
    environment.update({
        "CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS": str(maximum),
        "DELEGATION_MAX_AGENTS": str(maximum),
    })
    managed_hooks = session.get("hooks")
    if managed_hooks is not None:
        settings["hooks"] = dict(_object(managed_hooks, "runtime.session.hooks"))
    _write_json_atomic(path, settings)
    return path


def provider_overlay(deployment: Mapping[str, Any], session_dir: Path) -> Path:
    runtime = _runtime(deployment)
    session = _object(runtime.get("session", {}), "runtime.session")
    inference = _inference(deployment)
    model = inference.get("model")
    if not isinstance(model, str) or not model:
        raise RuntimeProfileError("inference.model must be a non-empty string")
    maximum = _positive_int(session.get("max_agents"),
                            "runtime.session.max_agents", 4)
    payload = {
        "model": model,
        "env": {
            "CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS": str(maximum),
            "DELEGATION_MAX_AGENTS": str(maximum),
        },
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16]
    path = session_dir / f".delegation-provider-settings-{digest}.json"
    try:
        current = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        current = None
    if current != payload:
        _write_json_atomic(path, payload)
    return path


def _gateway_value(gateway: object, attribute: str,
                   environ: Mapping[str, str], env_name: str) -> str | None:
    if gateway is not None:
        if isinstance(gateway, Mapping):
            value = gateway.get(attribute)
        else:
            value = getattr(gateway, attribute, None)
        if isinstance(value, str) and value:
            return value
    return environ.get(env_name)


def build_environment(deployment: Mapping[str, Any], session_dir: Path,
                      *, gateway: object = None,
                      environ: Mapping[str, str] | None = None,
                      control: bool = False) -> dict[str, str]:
    source = os.environ if environ is None else environ
    result = dict(source)
    result.pop("ANTHROPIC_API_KEY", None)
    result.pop("CHEAPESTINFERENCE_API_KEY", None)
    result["CLAUDE_CONFIG_DIR"] = str(session_dir)
    if control:
        result.pop("ANTHROPIC_AUTH_TOKEN", None)
        result.pop("ANTHROPIC_BASE_URL", None)
        return result
    base_url = _gateway_value(
        gateway, "base_url", source, "DELEGATION_GATEWAY_BASE_URL")
    token = _gateway_value(
        gateway, "token", source, "DELEGATION_GATEWAY_TOKEN")
    if not base_url or not token:
        raise RuntimeProfileError(
            "the protocol gateway binding is unavailable", RUNTIME_ERROR)
    inference = _inference(deployment)
    model = inference.get("model")
    if not isinstance(model, str) or not model:
        raise RuntimeProfileError("inference.model must be a non-empty string")
    context = _positive_int(inference.get("context_tokens"),
                            "inference.context_tokens", 1_000_000)
    output = _positive_int(inference.get("max_output_tokens"),
                           "inference.max_output_tokens", 32_000, 131_072)
    runtime = _runtime(deployment)
    session = _object(runtime.get("session", {}), "runtime.session")
    maximum = _positive_int(session.get("max_agents"),
                            "runtime.session.max_agents", 4)
    result.update({
        "ANTHROPIC_AUTH_TOKEN": token,
        "ANTHROPIC_BASE_URL": base_url,
        "ANTHROPIC_MODEL": model,
        "ANTHROPIC_SMALL_FAST_MODEL": model,
        "ANTHROPIC_DEFAULT_OPUS_MODEL": model,
        "ANTHROPIC_DEFAULT_SONNET_MODEL": model,
        "ANTHROPIC_DEFAULT_HAIKU_MODEL": model,
        "ANTHROPIC_DEFAULT_FABLE_MODEL": model,
        "CLAUDE_CODE_AUTO_COMPACT_WINDOW": str(context),
        "CLAUDE_CODE_MAX_CONTEXT_TOKENS": str(context),
        "CLAUDE_CODE_MAX_OUTPUT_TOKENS": str(output),
        "CLAUDE_CODE_AUTO_MODE_EXTERNAL_PERMISSIONS": "1",
        "CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS": str(maximum),
        "DELEGATION_MAX_AGENTS": str(maximum),
    })
    return result


def _command(executable: str, arguments: Sequence[str]) -> str | list[str]:
    values = list(arguments)
    if os.name != "nt" or Path(executable).suffix.casefold() not in {".cmd", ".bat"}:
        return [executable, *values]
    unsafe = '%\0\r\n"&|<>^()!'
    if any(any(character in value for character in unsafe)
           for value in [executable, *values]):
        raise RuntimeProfileError(
            "a Windows batch launcher cannot represent quotes or cmd "
            "metacharacters safely; install a native claude.exe")
    rendered = " ".join(
        part if part.startswith('"') else f'"{part}"'
        for part in (subprocess.list2cmdline([value])
                     for value in [executable, *values]))
    comspec = os.environ.get("COMSPEC", "cmd.exe")
    prefix = subprocess.list2cmdline([comspec, "/d", "/v:off", "/s", "/c"])
    return f'{prefix} "{rendered}"'


def _background_ids(executable: str, environment: Mapping[str, str]) -> set[str] | None:
    try:
        result = subprocess.run(
            _command(executable, ["agents", "--json"]), env=dict(environment),
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, timeout=10, check=False)
        payload = json.loads(result.stdout) if result.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
        return None
    if not isinstance(payload, list):
        return None
    return {
        str(entry.get("id"))
        for entry in payload
        if isinstance(entry, dict) and entry.get("kind") == "background"
        and entry.get("id") is not None
    }


def _binding_action(binding: object, name: str) -> bool:
    method = getattr(binding, name, None)
    if not callable(method):
        return False
    method()
    return True


def _wait(process: subprocess.Popen[Any], binding: object) -> int:
    while True:
        try:
            return process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            _binding_action(binding, "heartbeat")
        except KeyboardInterrupt:
            if process.poll() is None:
                process.send_signal(signal.SIGINT)
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
            return 130


def launch(deployment: Mapping[str, Any], arguments: Sequence[str], *,
           gateway: object = None,
           environ: Mapping[str, str] | None = None) -> int:
    """Launch Claude with a protocol gateway binding and preserve its status."""
    source = os.environ if environ is None else environ
    runtime = _runtime(deployment)
    executable = _resolve_executable(runtime, source)
    values = list(arguments)
    control = bool(values and values[0] in CONTROL_COMMANDS)
    validate_arguments(values)
    session_dir = _session_dir(runtime, source)
    configure_session(deployment, session_dir)
    control_environment = build_environment(
        deployment, session_dir, gateway=gateway, environ=source, control=True)
    if control:
        return subprocess.run(
            _command(executable, values), env=control_environment,
            check=False).returncode

    environment = build_environment(
        deployment, session_dir, gateway=gateway, environ=source)
    inference = _inference(deployment)
    effort = inference.get("interactive_effort", "high")
    if not isinstance(effort, str) or effort not in VALID_EFFORTS:
        raise RuntimeProfileError(
            "inference.interactive_effort must be low, medium, high, xhigh, "
            "max, unset, or auto")
    if _has_effort(values):
        environment.pop("CLAUDE_CODE_EFFORT_LEVEL", None)
    elif effort in {"unset", "auto"}:
        environment["CLAUDE_CODE_EFFORT_LEVEL"] = effort
    else:
        environment.pop("CLAUDE_CODE_EFFORT_LEVEL", None)
        values = ["--effort", effort, *values]
    overlay = provider_overlay(deployment, session_dir)
    values = ["--settings", str(overlay), *values]

    before = _background_ids(executable, control_environment)
    process = subprocess.Popen(_command(executable, values), env=environment)
    retained = False
    try:
        status = _wait(process, gateway)
        after = _background_ids(executable, control_environment)
        if gateway is not None and (after is None or before is None or after != before):
            retained = _binding_action(gateway, "retain")
        return status
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait()
        if gateway is not None and not retained:
            _binding_action(gateway, "close")


def load_deployment(path: Path) -> Mapping[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeProfileError(f"could not read deployment {path}: {error}") from error
    return _object(payload, "deployment")


def _load_gateway(path: Path | None) -> Mapping[str, str] | None:
    if path is None:
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeProfileError(f"could not read gateway binding: {error}") from error
    value = _object(payload, "gateway binding")
    return {str(key): str(item) for key, item in value.items()}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    launch_parser = subparsers.add_parser("launch")
    launch_parser.add_argument("--deployment", type=Path, required=True)
    launch_parser.add_argument("--gateway-file", type=Path)
    launch_parser.add_argument("arguments", nargs=argparse.REMAINDER)
    options = parser.parse_args(argv)
    arguments = options.arguments
    if arguments[:1] == ["--"]:
        arguments = arguments[1:]
    try:
        return launch(load_deployment(options.deployment), arguments,
                      gateway=_load_gateway(options.gateway_file))
    except RuntimeProfileError as error:
        print(f"claude runtime: {error}", file=sys.stderr)
        return error.status


if __name__ == "__main__":
    raise SystemExit(main())
