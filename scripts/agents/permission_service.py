#!/usr/bin/env python3
"""Agent-neutral permission policy and replay-safe permission state."""
from __future__ import annotations

import json
import os
import re
import secrets
import tempfile
import time
from pathlib import Path, PureWindowsPath
from typing import Any

SCHEMA_VERSION = 1
MAX_RESULT_BYTES = 64 * 1024
DECISIONS = {"allow", "deny", "handled"}
OPERATIONS = {"read", "write", "shell", "network", "process", "external"}
PROTECTED_PARTS = {".git", ".claude", ".codex", ".ssh", ".gnupg", ".aws"}
SECRET_PARTS = {"credential", "credentials", "secret", "secrets", "id_rsa", "id_ed25519"}
SHELL_META = re.compile(r"(?:[;&|<>`\n$*?]|\[[^]]*\]|\{[^}]*,)")
HARD_DENY_COMMANDS = {
    "bash", "cmd", "curl", "doas", "gh", "mount", "nc", "net", "netsh",
    "powershell", "pwsh", "reboot", "rm", "scp", "sh", "shutdown", "ssh",
    "su", "sudo", "umount", "wget", "wsl", "zsh",
}
READ_COMMANDS = {
    "basename", "cat", "cut", "dirname", "grep", "head", "ls", "pwd",
    "realpath", "rg", "stat", "tail", "test", "wc",
}
READ_GIT = {
    "blame", "describe", "diff", "for-each-ref", "grep", "log", "ls-files",
    "merge-base", "name-rev", "rev-list", "rev-parse", "show", "status",
}


class PermissionError(ValueError):
    """A malformed or invalid permission transition."""


def _single_line(value: Any, name: str, limit: int = 4096) -> str:
    if (not isinstance(value, str) or not value or "\0" in value or "\n" in value
            or len(value.encode("utf-8")) > limit):
        raise PermissionError(f"{name} must be a bounded non-empty single-line string")
    return value


def validate_request(value: Any) -> dict[str, Any]:
    required = {
        "schema_version", "request_id", "session_id", "operation", "arguments",
        "reason", "created_at",
    }
    if not isinstance(value, dict) or set(value) != required:
        raise PermissionError("permission request fields are invalid")
    if value.get("schema_version") != SCHEMA_VERSION:
        raise PermissionError("permission request schema_version is invalid")
    for field in ("request_id", "session_id", "reason"):
        _single_line(value.get(field), f"permission request {field}")
    if value.get("operation") not in OPERATIONS:
        raise PermissionError("permission request operation is invalid")
    if not isinstance(value.get("arguments"), dict):
        raise PermissionError("permission request arguments must be an object")
    created = value.get("created_at")
    if not isinstance(created, (int, float)) or isinstance(created, bool) or created < 0:
        raise PermissionError("permission request created_at is invalid")
    return dict(value)


def validate_resolution(value: Any, request_id: str | None = None) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise PermissionError("permission resolution must be an object")
    allowed = {"schema_version", "request_id", "decision", "result"}
    if not {"schema_version", "request_id", "decision"} <= set(value) <= allowed:
        raise PermissionError("permission resolution fields are invalid")
    if value.get("schema_version") != SCHEMA_VERSION:
        raise PermissionError("permission resolution schema_version is invalid")
    _single_line(value.get("request_id"), "permission resolution request_id")
    if request_id is not None and value["request_id"] != request_id:
        raise PermissionError("permission resolution request_id does not match")
    decision = value.get("decision")
    if decision not in DECISIONS:
        raise PermissionError("permission resolution decision is invalid")
    if decision == "handled":
        if not isinstance(value.get("result"), dict):
            raise PermissionError("handled permission resolution requires a result")
        encoded = json.dumps(value["result"], sort_keys=True, ensure_ascii=False)
        if len(encoded.encode("utf-8")) > MAX_RESULT_BYTES:
            raise PermissionError("handled permission result is too large")
    elif "result" in value:
        raise PermissionError("result is valid only for a handled resolution")
    return dict(value)


def permission_request(session_id: str, operation: str, arguments: dict[str, Any],
                       reason: str, *, now: float | None = None) -> dict[str, Any]:
    return validate_request({
        "schema_version": SCHEMA_VERSION,
        "request_id": secrets.token_urlsafe(24),
        "session_id": session_id,
        "operation": operation,
        "arguments": arguments,
        "reason": reason,
        "created_at": time.time() if now is None else now,
    })


def _parts(value: str | Path) -> tuple[str, ...]:
    return (*Path(str(value)).parts, *PureWindowsPath(str(value)).parts)


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _path_decision(value: Any, root: Path, *, writing: bool,
                   mode: str, allowed_paths: list[str]) -> tuple[str, str]:
    if not isinstance(value, str) or not value or "\0" in value:
        return "deny", "invalid path"
    windows = PureWindowsPath(value)
    if windows.drive and not windows.is_absolute():
        return "deny", "drive-relative paths are forbidden"
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = root / candidate
    resolved = candidate.resolve(strict=False)
    if not _inside(resolved, root):
        return "deny", "path escapes the workspace"
    relative = resolved.relative_to(root)
    normalized = {item.casefold().rstrip(" .") for item in _parts(relative)}
    if normalized & (PROTECTED_PARTS | SECRET_PARTS) or any(
            item.startswith(".env") for item in normalized):
        return "deny", "protected path"
    if writing:
        if mode != "edit":
            return "deny", "write requested for a read-only task"
        if any(item.endswith((".pem", ".key")) for item in normalized):
            return "deny", "key material cannot be written"
        posix = relative.as_posix()
        if allowed_paths and not any(
                base == "." or posix == base or posix.startswith(base + "/")
                for base in allowed_paths):
            return "deny", "path is outside the task scope"
    return "allow", "workspace path is within deterministic policy"


def deterministic_decision(request: dict[str, Any], root: Path, *, mode: str,
                           allowed_paths: list[str]) -> tuple[str, str]:
    """Return allow, deny, or ask for a normalized operation."""
    request = validate_request(request)
    operation = request["operation"]
    arguments = request["arguments"]
    if operation in {"read", "write"}:
        return _path_decision(arguments.get("path"), root.resolve(),
                              writing=operation == "write", mode=mode,
                              allowed_paths=allowed_paths)
    if operation in {"network", "external"}:
        return "deny", "external operations require parent authority"
    if operation == "process":
        return "ask", "process operation requires a parent decision"
    command = arguments.get("command")
    if not isinstance(command, str) or not command.strip() or "\0" in command:
        return "deny", "invalid shell command"
    if SHELL_META.search(command):
        return "deny", "shell composition is forbidden"
    try:
        import shlex
        words = shlex.split(command, posix=os.name != "nt")
    except ValueError:
        return "deny", "shell command cannot be parsed"
    if not words:
        return "deny", "empty shell command"
    executable = Path(words[0]).name.casefold()
    if executable.endswith(".exe"):
        executable = executable[:-4]
    if executable in HARD_DENY_COMMANDS or re.fullmatch(r"pythonw?(?:\d+(?:\.\d+)*)?", executable):
        return "deny", "executable is outside deterministic policy"
    if executable == "git":
        subcommand = next((word for word in words[1:] if not word.startswith("-")), "")
        return (("allow", "read-only git operation") if subcommand in READ_GIT
                else ("ask", "mutating or unknown git operation"))
    if executable in READ_COMMANDS:
        return "allow", "read-only command"
    return "ask", "command requires a parent decision"


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


class PermissionStore:
    """Persistent pending requests and exact, one-use grants for one session."""

    def __init__(self, path: Path, session_id: str) -> None:
        self.path = path
        self.session_id = _single_line(session_id, "session_id")
        if not path.exists():
            _atomic_json(path, {"schema_version": SCHEMA_VERSION,
                                "session_id": session_id, "pending": None,
                                "grants": [], "resolved": []})

    def _read(self) -> dict[str, Any]:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise PermissionError(f"permission state is unavailable: {error}") from error
        if (not isinstance(value, dict) or value.get("schema_version") != SCHEMA_VERSION
                or value.get("session_id") != self.session_id
                or not isinstance(value.get("grants"), list)
                or not isinstance(value.get("resolved"), list)):
            raise PermissionError("permission state is corrupt")
        return value

    def issue(self, request: dict[str, Any]) -> None:
        request = validate_request(request)
        if request["session_id"] != self.session_id:
            raise PermissionError("permission request session_id does not match")
        state = self._read()
        if state.get("pending") is not None:
            raise PermissionError("a permission request is already pending")
        if request["request_id"] in state["resolved"]:
            raise PermissionError("permission request was already resolved")
        state["pending"] = request
        _atomic_json(self.path, state)

    def pending(self) -> dict[str, Any] | None:
        pending = self._read().get("pending")
        return validate_request(pending) if pending is not None else None

    def resolve(self, resolution: dict[str, Any], *, now: float | None = None) -> dict[str, Any]:
        state = self._read()
        pending = state.get("pending")
        if pending is None:
            raise PermissionError("there is no pending permission request")
        pending = validate_request(pending)
        resolution = validate_resolution(resolution, pending["request_id"])
        decision = resolution["decision"]
        if decision == "allow":
            state["grants"].append({
                "request_id": pending["request_id"],
                "operation": pending["operation"],
                "arguments": pending["arguments"],
            })
            continuation = "The parent approved the exact operation for one use."
        elif decision == "deny":
            continuation = "The parent denied the operation; do not retry it or a variant."
        else:
            encoded = json.dumps(resolution["result"], sort_keys=True, ensure_ascii=False)
            continuation = "The parent handled the operation. Result: " + encoded
        state["resolved"].append(pending["request_id"])
        state["pending"] = None
        _atomic_json(self.path, state)
        current = time.time() if now is None else now
        return {"continuation": continuation,
                "paused_seconds": max(0.0, current - pending["created_at"])}

    def consume_grant(self, operation: str, arguments: dict[str, Any]) -> bool:
        state = self._read()
        for index, grant in enumerate(state["grants"]):
            if grant.get("operation") == operation and grant.get("arguments") == arguments:
                state["grants"].pop(index)
                _atomic_json(self.path, state)
                return True
        return False
