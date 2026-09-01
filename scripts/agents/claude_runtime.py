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
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from execution_engine import run_owned_process, spawn_owned_process


CONTROL_COMMANDS = frozenset({"agents", "logs", "stop", "kill", "rm", "respawn"})
RETIRED_HOOK_STATUS = frozenset({
    "ci-claude: fast permission decision",
    "ci-claude: enforce single API lane",
    "ci-claude: enforce bulk-worker tool policy",
    "ci-claude: open the lane turn",
    "ci-claude: close the lane turn",
})
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


def _session_arguments(arguments: list[str]) -> tuple[list[str], str]:
    """Give every launch an exact identity for background-session ownership."""
    session_id = None
    index = 0
    ambiguous = {"-c", "--continue", "--fork-session", "--from-pr",
                 "--teleport", "--cloud"}
    while index < len(arguments):
        argument = arguments[index]
        if argument == "--":
            break
        if argument in ambiguous or any(
                argument.startswith(name + "=") for name in ambiguous):
            raise RuntimeProfileError(
                f"{argument.split('=', 1)[0]} cannot provide an exact managed "
                "session identity; use --resume <uuid> instead")
        if argument in {"--session-id", "--resume", "-r"}:
            if index + 1 >= len(arguments):
                raise RuntimeProfileError(f"{argument} requires an explicit UUID")
            candidate = arguments[index + 1]
            index += 2
        elif (argument.startswith("--session-id=") or
              argument.startswith("--resume=")):
            candidate = argument.split("=", 1)[1]
            index += 1
        else:
            index += 1
            continue
        try:
            parsed = str(uuid.UUID(candidate))
        except (ValueError, AttributeError) as error:
            raise RuntimeProfileError(
                f"{argument.split('=', 1)[0]} must name an exact UUID") from error
        if parsed != candidate:
            raise RuntimeProfileError(
                f"{argument.split('=', 1)[0]} must use canonical UUID form")
        if session_id is not None and session_id != parsed:
            raise RuntimeProfileError("managed session identity is ambiguous")
        session_id = parsed
    if session_id is None:
        session_id = str(uuid.uuid4())
        arguments = ["--session-id", session_id, *arguments]
    return arguments, session_id


def _runtime(deployment: Mapping[str, Any]) -> Mapping[str, Any]:
    runtime = _object(deployment.get("runtime"), "runtime")
    if runtime.get("profile") != "claude-code":
        raise RuntimeProfileError("runtime.profile must be 'claude-code'")
    return runtime


def _inference(deployment: Mapping[str, Any]) -> Mapping[str, Any]:
    return _object(deployment.get("inference"), "inference")


def _expand_session_path(raw: str, environ: Mapping[str, str], *,
                         windows: bool) -> Path:
    if "\0" in raw or "\n" in raw or "\r" in raw:
        raise RuntimeProfileError("runtime.session.config_dir is invalid")
    if windows:
        marker = "%LOCALAPPDATA%"
        if marker.casefold() in raw.casefold():
            local = environ.get("LOCALAPPDATA")
            if not local:
                raise RuntimeProfileError("LOCALAPPDATA is unavailable")
            start = raw.casefold().index(marker.casefold())
            raw = raw[:start] + local + raw[start + len(marker):]
        if "%" in raw:
            raise RuntimeProfileError(
                "runtime.session.config_dir has an unknown Windows placeholder")
        return Path(raw).resolve()
    if raw == "~" or raw.startswith("~/"):
        home = environ.get("HOME") or str(Path.home())
        raw = home + raw[1:]
    elif raw.startswith("~") or "$" in raw or "%" in raw:
        raise RuntimeProfileError(
            "runtime.session.config_dir has an unknown POSIX placeholder")
    return Path(raw).resolve()


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
    if isinstance(raw, Mapping):
        if set(raw) != {"posix", "windows"} or any(
                not isinstance(value, str) or not value for value in raw.values()):
            raise RuntimeProfileError(
                "runtime.session.config_dir platform map is invalid")
        raw = raw["windows" if os.name == "nt" else "posix"]
    if not isinstance(raw, str) or not raw:
        raise RuntimeProfileError(
            "runtime.session.config_dir must be a string or platform map")
    return _expand_session_path(raw, environ, windows=os.name == "nt")


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
    hooks = settings.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise RuntimeProfileError(f"{path} hooks must be an object")
    command = [sys.executable, str(Path(__file__).resolve()), "hook"]
    rendered = (subprocess.list2cmdline(command) if os.name == "nt" else
                " ".join(shlex.quote(value) for value in command))
    managed = {
        "PermissionRequest": [{"matcher": "*", "hooks": [{
            "type": "command", "command": rendered + " permission",
            "timeout": 30, "statusMessage": "delegation: permission policy",
        }]}],
        "PreToolUse": [
            {"matcher": "Agent", "hooks": [{
                "type": "command", "command": rendered + " agent-preflight",
                "timeout": 5, "statusMessage": "delegation: gateway preflight",
            }]},
            {"matcher": "*", "hooks": [{
                "type": "command", "command": rendered + " permission",
                "timeout": 5, "statusMessage": "delegation: tool policy",
            }]},
        ],
    }
    for event, groups in managed.items():
        existing = hooks.get(event, [])
        if not isinstance(existing, list):
            raise RuntimeProfileError(f"{path} hooks.{event} must be an array")
        statuses = RETIRED_HOOK_STATUS | {
            handler.get("statusMessage")
            for group in groups for handler in group["hooks"]
        }
        kept = []
        for group in existing:
            if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
                kept.append(group)
                continue
            handlers = [handler for handler in group["hooks"]
                        if not (isinstance(handler, dict)
                                and handler.get("statusMessage") in statuses)]
            if handlers:
                replacement = dict(group)
                replacement["hooks"] = handlers
                kept.append(replacement)
        hooks[event] = [*kept, *groups]
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
    runtime = _runtime(deployment)
    configured_environment = _object(
        runtime.get("environment", {}), "runtime.environment")
    if any(not isinstance(key, str) or not isinstance(value, str)
           for key, value in configured_environment.items()):
        raise RuntimeProfileError("runtime.environment must map strings to strings")
    result.update(configured_environment)
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
    deployment_id = _gateway_value(
        gateway, "deployment_id", source, "DELEGATION_GATEWAY_DEPLOYMENT_ID")
    registration_id = _gateway_value(
        gateway, "registration_id", source,
        "DELEGATION_GATEWAY_REGISTRATION_ID")
    if deployment_id:
        result["DELEGATION_GATEWAY_DEPLOYMENT_ID"] = deployment_id
    if registration_id:
        result["DELEGATION_GATEWAY_REGISTRATION_ID"] = registration_id
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


def _background_sessions(
        executable: str, environment: Mapping[str, str]
        ) -> dict[str, str] | None:
    try:
        result = run_owned_process(
            _command(executable, ["agents", "--json"]), Path.cwd(), 10,
            env=dict(environment), max_output_bytes=1024 * 1024)
        payload = (json.loads(result["stdout"])
                   if result["returncode"] == 0
                   and not result["output_budget_exhausted"] else None)
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(payload, list):
        return None
    return {
        str(entry.get("id")): str(entry.get("sessionId"))
        for entry in payload
        if isinstance(entry, dict) and entry.get("kind") == "background"
        and entry.get("id") is not None and entry.get("sessionId") is not None
    }


def _background_ids(executable: str, environment: Mapping[str, str]) -> set[str] | None:
    sessions = _background_sessions(executable, environment)
    return None if sessions is None else set(sessions)


def background_session_ids(
        deployment: Mapping[str, Any], *,
        environ: Mapping[str, str] | None = None) -> set[str] | None:
    """Return retained Claude session IDs, or None when the roster is unknown.

    The managed service calls this profile probe after a launcher hands off a
    registration.  Unknown is deliberately distinct from empty so a transient
    daemon or filesystem failure cannot strand a live background session.
    """
    source = os.environ if environ is None else environ
    runtime = _runtime(deployment)
    executable = _resolve_executable(runtime, source)
    session_dir = _session_dir(runtime, source)
    environment = build_environment(
        deployment, session_dir, environ=source, control=True)
    return _background_ids(executable, environment)


def _read_hook_event() -> dict[str, Any]:
    try:
        value = json.load(sys.stdin)
    except (OSError, ValueError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def _emit_hook(event: str, decision: str, reason: str) -> None:
    if event == "PreToolUse":
        specific = {
            "hookEventName": event,
            "permissionDecision": decision,
            "permissionDecisionReason": reason,
        }
    else:
        result: dict[str, str] = {"behavior": decision}
        if decision == "deny":
            result["message"] = reason
        specific = {"hookEventName": "PermissionRequest", "decision": result}
    print(json.dumps({"hookSpecificOutput": specific}, separators=(",", ":")))


def _normalize_tool(event: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
    tool = event.get("tool_name")
    tool_input = event.get("tool_input")
    if not isinstance(tool, str) or not isinstance(tool_input, dict):
        return "external", {"tool": str(tool), "input": {}}
    path_fields = {
        "Read": "file_path", "Edit": "file_path", "Write": "file_path",
        "Glob": "path", "Grep": "path", "LS": "path",
    }
    if tool in path_fields:
        operation = "write" if tool in {"Edit", "Write"} else "read"
        return operation, {"path": tool_input.get(path_fields[tool], ".")}
    if tool == "Bash":
        return "shell", {"command": tool_input.get("command")}
    if tool in {"WebFetch", "WebSearch"}:
        return "network", {"tool": tool, **tool_input}
    if tool == "Agent":
        return "process", {"tool": tool, **tool_input}
    return "external", {"tool": tool, "input": tool_input}


def permission_hook() -> int:
    """Translate Claude hooks into the normalized permission service."""
    event = _read_hook_event()
    hook_event = str(event.get("hook_event_name") or "PermissionRequest")
    state_path = os.environ.get("DELEGATION_PERMISSION_STATE")
    # Interactive PreToolUse keeps Claude's normal permission flow. The
    # PermissionRequest event below may still apply deterministic safe/deny
    # results without turning the runtime profile into an interactive judge.
    if hook_event == "PreToolUse" and not state_path:
        return 0
    try:
        from permission_service import (
            PermissionError as ProtocolPermissionError,
            PermissionStore, deterministic_decision, permission_request,
        )
        operation, arguments = _normalize_tool(event)
        session_id = (os.environ.get("DELEGATION_TASK_ID") or
                      str(event.get("session_id") or "interactive"))
        request = permission_request(
            session_id, operation, arguments,
            f"Claude requested {event.get('tool_name', 'an unknown tool')}")
        root = Path(os.environ.get("DELEGATION_WORKSPACE_ROOT") or
                    str(event.get("cwd") or os.getcwd()))
        mode = os.environ.get("DELEGATION_TASK_MODE", "edit")
        try:
            allowed = json.loads(os.environ.get("DELEGATION_ALLOWED_PATHS", "[]"))
        except json.JSONDecodeError:
            allowed = []
        if not isinstance(allowed, list) or any(not isinstance(item, str) for item in allowed):
            allowed = []
        store = PermissionStore(Path(state_path), session_id) if state_path else None
        if store is not None and store.consume_grant(operation, arguments):
            decision, reason = "allow", "the exact one-use parent grant was consumed"
        else:
            decision, reason = deterministic_decision(
                request, root, mode=mode, allowed_paths=allowed)
        if decision == "ask":
            if store is None:
                return 0
            try:
                store.issue(request)
            except ProtocolPermissionError:
                # A prior request is already the authoritative parent pause.
                pass
            decision = "deny"
            reason = "the operation is paused for a parent permission decision"
        _emit_hook(hook_event, decision, reason)
        return 0
    except (OSError, ValueError) as error:
        _emit_hook(hook_event, "deny", f"permission policy failed closed: {error}")
        return 0


def agent_preflight_hook() -> int:
    """Allow Agent only when the gateway confirms this exact binding."""
    event = _read_hook_event()
    if (event.get("hook_event_name") != "PreToolUse" or
            event.get("tool_name") != "Agent"):
        return 0
    base_url = os.environ.get("ANTHROPIC_BASE_URL", "")
    token = os.environ.get("ANTHROPIC_AUTH_TOKEN", "")
    deployment_id = os.environ.get("DELEGATION_GATEWAY_DEPLOYMENT_ID", "")
    registration_id = os.environ.get("DELEGATION_GATEWAY_REGISTRATION_ID", "")
    reason = "the protocol gateway binding could not be verified"
    try:
        request = urllib.request.Request(
            base_url.rstrip("/") + "/_delegation/v1/binding",
            headers={"Authorization": "Bearer " + token})
        with urllib.request.urlopen(request, timeout=3) as response:
            descriptor = json.loads(response.read())
        valid = (
            isinstance(descriptor, dict)
            and descriptor.get("deployment_id") == deployment_id
            and descriptor.get("registration_id") == registration_id
            and bool(deployment_id) and bool(registration_id)
        )
    except (OSError, ValueError, urllib.error.URLError):
        valid = False
    _emit_hook(
        "PreToolUse", "allow" if valid else "deny",
        "the Agent call uses a verified protocol gateway binding" if valid else reason)
    return 0


def _binding_action(binding: object, name: str, *arguments: object) -> bool:
    method = getattr(binding, name, None)
    if not callable(method):
        return False
    return method(*arguments) is not False


def _wait(process: subprocess.Popen[Any], owner: object,
          binding: object) -> int:
    while True:
        try:
            return process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            _binding_action(binding, "heartbeat")
        except KeyboardInterrupt:
            if process.poll() is None:
                request = getattr(owner, "request_termination")
                force = getattr(owner, "force_termination")
                request()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    force()
                    if process.poll() is None:
                        process.wait()
            return 130


def launch(deployment: Mapping[str, Any], arguments: Sequence[str], *,
           gateway: object = None,
           environ: Mapping[str, str] | None = None) -> int:
    """Launch Claude with a protocol gateway binding and preserve its status."""
    retained = False
    process: subprocess.Popen[Any] | None = None
    owner: object | None = None
    try:
        source = os.environ if environ is None else environ
        runtime = _runtime(deployment)
        executable = _resolve_executable(runtime, source)
        configured_arguments = runtime.get("arguments", [])
        if (not isinstance(configured_arguments, list) or
                any(not isinstance(value, str)
                    for value in configured_arguments)):
            raise RuntimeProfileError("runtime.arguments must contain strings")
        caller_arguments = list(arguments)
        control = bool(
            caller_arguments and caller_arguments[0] in CONTROL_COMMANDS)
        values = (caller_arguments if control else
                  [*configured_arguments, *caller_arguments])
        validate_arguments(values)
        session_dir = _session_dir(runtime, source)
        configure_session(deployment, session_dir)
        control_environment = build_environment(
            deployment, session_dir, gateway=gateway, environ=source,
            control=True)
        if control:
            process, owner = spawn_owned_process(
                _command(executable, values), env=control_environment)
            return _wait(process, owner, None)

        environment = build_environment(
            deployment, session_dir, gateway=gateway, environ=source)
        inference = _inference(deployment)
        effort = inference.get("interactive_effort", "high")
        if not isinstance(effort, str) or effort not in VALID_EFFORTS:
            raise RuntimeProfileError(
                "inference.interactive_effort must be low, medium, high, "
                "xhigh, max, unset, or auto")
        if _has_effort(values):
            environment.pop("CLAUDE_CODE_EFFORT_LEVEL", None)
        elif effort in {"unset", "auto"}:
            environment["CLAUDE_CODE_EFFORT_LEVEL"] = effort
        else:
            environment.pop("CLAUDE_CODE_EFFORT_LEVEL", None)
            values = ["--effort", effort, *values]
        values, session_id = _session_arguments(values)
        overlay = provider_overlay(deployment, session_dir)
        values = ["--settings", str(overlay), *values]

        process, owner = spawn_owned_process(
            _command(executable, values), env=environment)
        status = _wait(process, owner, gateway)
        after = _background_sessions(executable, control_environment)
        if gateway is not None:
            owned = ({identifier for identifier, exact in after.items()
                      if exact == session_id} if after is not None else set())
            if owned:
                retained = _binding_action(gateway, "retain", owned)
                if not retained:
                    getattr(owner, "force_termination")()
                    status = RUNTIME_ERROR
            elif after is None:
                getattr(owner, "force_termination")()
                status = RUNTIME_ERROR
        return status
    finally:
        if process is not None and process.poll() is None:
            assert owner is not None
            getattr(owner, "request_termination")()
            try:
                process.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                getattr(owner, "force_termination")()
                if process.poll() is None:
                    process.wait()
        if owner is not None:
            if retained:
                getattr(owner, "release_descendants")()
            else:
                getattr(owner, "close")()
        if gateway is not None and not retained:
            _binding_action(gateway, "close")


def _worker_prompt(task: Mapping[str, Any], context: Mapping[str, Any]) -> str:
    if task["mode"] == "read":
        scope = (
            "You are a read-only audit worker. Inspect only; do not create, "
            "edit, delete, rename, commit, or push files. Return a concise "
            "evidence-based report.")
    else:
        paths = task.get("allowed_paths") or ["any path in this isolated worktree"]
        scope = (
            "You are an isolated edit worker. Do not commit or push. Stay "
            "inside this allowed path scope: " + ", ".join(paths) + ".")
    continuation = context.get("continuation")
    resumed = (f"\n\nPARENT CONTINUATION:\n{continuation}" if continuation else "")
    return (
        scope + "\n\nUse one deterministic command per shell call. External "
        "operations require parent authority. Stop early enough to return a "
        "final report within this slice.\n\nTASK:\n" + str(task["prompt"]) + resumed)


def worker_runner(deployment: Mapping[str, Any], gateway_factory: object):
    """Build an ExecutionEngine runner for headless Claude slices.

    ``gateway_factory`` may be a ServiceClient or a callable accepting
    ``(task, context)`` and returning a GatewayBinding. Each slice owns and
    closes exactly one registration.
    """
    runtime = _runtime(deployment)
    inference = _inference(deployment)

    def runner(task: dict[str, Any], cwd: Path,
               context: dict[str, Any]) -> dict[str, Any]:
        if callable(gateway_factory):
            binding = gateway_factory(task, context)
        else:
            register = getattr(gateway_factory, "register", None)
            if not callable(register):
                raise RuntimeProfileError("gateway_factory cannot register a client")
            dependency = min(
                int(max(1, context["remaining_seconds"])),
                int(_object(deployment.get("service"), "service").get(
                    "max_dependency_seconds", 3600)))
            binding = register(
                f"worker:{context['token']}:{context['step']}",
                pid=os.getpid(), dependency_seconds=dependency)
        try:
            permission_store = context.get("permissions")
            permission_path = getattr(permission_store, "path", None)
            if not isinstance(permission_path, Path):
                permission_path = Path(str(permission_path))
            worker_session = permission_path.parent / "claude-session"
            configure_session(deployment, worker_session)
            environment = build_environment(
                deployment, worker_session, gateway=binding)
            environment.update({
                "DELEGATION_PERMISSION_STATE": str(permission_path),
                "DELEGATION_TASK_ID": str(context["token"]),
                "DELEGATION_WORKSPACE_ROOT": str(cwd),
                "DELEGATION_TASK_MODE": str(task["mode"]),
                "DELEGATION_ALLOWED_PATHS": json.dumps(
                    task.get("allowed_paths", []), separators=(",", ":")),
            })
            effort = inference.get("worker_effort", "low")
            environment["CLAUDE_CODE_EFFORT_LEVEL"] = str(effort)
            thinking = _object(
                inference.get("thinking", {}), "inference.thinking")
            if thinking.get("type") == "disabled":
                environment["MAX_THINKING_TOKENS"] = "0"
            elif thinking.get("type") == "adaptive":
                environment.pop("MAX_THINKING_TOKENS", None)
            elif thinking.get("type") == "enabled":
                environment["MAX_THINKING_TOKENS"] = str(_positive_int(
                    thinking.get("budget_tokens"),
                    "inference.thinking.budget_tokens", 1))
            else:
                raise RuntimeProfileError("inference.thinking.type is invalid")
            executable = _resolve_executable(runtime, environment)
            maximum = max(1, int(context["remaining_steps"]))
            arguments = [
                "-p", "--output-format", "json", "--max-turns", str(maximum),
            ]
            if int(context.get("step", 0)):
                arguments.extend(["--resume", str(context["token"])])
            else:
                arguments.extend(["--session-id", str(context["token"])])
            tools = ("Read,Grep,Glob,Bash" if task["mode"] == "read" else
                     "Read,Grep,Glob,Bash,Edit,Write")
            arguments.extend([
                "--effort", str(effort), "--disable-slash-commands",
                "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
                "--setting-sources", "user", "--permission-mode",
                "dontAsk" if task["mode"] == "read" else "acceptEdits",
                "--tools", tools, "--settings",
                str(provider_overlay(deployment, worker_session)),
            ])
            run_process = context.get("run_process")
            if not callable(run_process):
                raise RuntimeProfileError(
                    "the execution engine process supervisor is unavailable")
            process_result = run_process(
                _command(executable, arguments), cwd,
                float(context["remaining_seconds"]),
                input_text=_worker_prompt(task, context),
                max_output_bytes=int(task["budgets"]["max_output_bytes"]),
                env=environment)
            returncode = int(process_result["returncode"])
            stdout = str(process_result["stdout"])
            stderr = str(process_result["stderr"])
            try:
                response = json.loads(stdout)
            except json.JSONDecodeError:
                response = stdout
            pending = (permission_store.pending()
                       if permission_store is not None else None)
            turns = (response.get("num_turns", 1)
                     if isinstance(response, dict) else 1)
            if not isinstance(turns, int) or isinstance(turns, bool) or turns < 1:
                turns = 1
            max_turns = isinstance(response, dict) and (
                response.get("subtype") == "error_max_turns"
                or response.get("stop_reason") == "max_turns")
            if process_result.get("cancelled"):
                classification, completed = "cancelled", True
            elif process_result.get("output_budget_exhausted"):
                classification, completed = "output_budget_exhausted", True
            elif process_result.get("timed_out"):
                classification, completed = "timeout", True
            elif pending is not None:
                classification, completed = "permission_requested", False
            elif max_turns:
                classification, completed = "session_yielded", False
            elif returncode:
                classification, completed = (
                    "backend_missing" if returncode == 127 else
                    "backend_error"), True
            elif isinstance(response, dict) and response.get("is_error"):
                classification, completed = "backend_reported_error", True
            elif not isinstance(response, dict):
                classification, completed = "invalid_backend_output", True
            else:
                classification, completed = "success", True
            return {
                "completed": completed, "classification": classification,
                "steps_used": min(turns, maximum), "returncode": returncode,
                "response": response, "stderr": stderr,
                **({"permission_request": pending}
                   if pending is not None else {}),
            }
        finally:
            _binding_action(binding, "close")

    return runner


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
    probe_parser = subparsers.add_parser("probe-background")
    probe_parser.add_argument("--deployment", type=Path, required=True)
    hook_parser = subparsers.add_parser("hook")
    hook_parser.add_argument("codec", choices=("permission", "agent-preflight"))
    options = parser.parse_args(argv)
    try:
        if options.command == "hook":
            return (permission_hook() if options.codec == "permission" else
                    agent_preflight_hook())
        deployment = load_deployment(options.deployment)
        if options.command == "probe-background":
            identifiers = background_session_ids(deployment)
            if identifiers is None:
                return 2
            print(json.dumps(sorted(identifiers), separators=(",", ":")))
            return 0 if identifiers else 1
        arguments = options.arguments
        if arguments[:1] == ["--"]:
            arguments = arguments[1:]
        return launch(deployment, arguments,
                      gateway=_load_gateway(options.gateway_file))
    except RuntimeProfileError as error:
        print(f"claude runtime: {error}", file=sys.stderr)
        return error.status


if __name__ == "__main__":
    raise SystemExit(main())
