#!/usr/bin/env python3
"""Agent-neutral authenticated provider gateway and managed service."""
from __future__ import annotations

import argparse
import atexit
import contextlib
import ctypes
import hashlib
import hmac
import http.client
import ipaddress
import json
import os
import secrets
import signal
import ssl
import stat
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PureWindowsPath
from typing import Any, Iterator
from urllib.parse import urlsplit

from lane_service import Lane

DEPLOYMENT_VERSION = 1
MAX_CONTROL_BYTES = 256 * 1024
MAX_HEADER_BYTES = 64 * 1024
CONTROL_PREFIX = "/_delegation/v1/"
HOP_BY_HOP = frozenset({
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailer", "transfer-encoding", "upgrade",
})
SECRET_FIELD_NAMES = frozenset({
    "secret", "password", "api_key", "apikey", "access_token",
    "bearer_token", "credential_value",
})
_DETACHED_CHILDREN: list[subprocess.Popen] = []


def _finalize_detached_children() -> None:
    for child in _DETACHED_CHILDREN:
        child.poll()
        if child.returncode is None:
            # The service deliberately outlives its starter. Mark the local
            # handle finalized without terminating or waiting for that daemon.
            child.returncode = 0


atexit.register(_finalize_detached_children)
WINDOWS_RESERVED_PARTS = {
    "aux", "clock$", "con", "conin$", "conout$", "nul", "prn",
    *(f"com{index}" for index in range(1, 10)),
    *(f"lpt{index}" for index in range(1, 10)),
    *(f"com{index}" for index in ("\N{SUPERSCRIPT ONE}",
                                   "\N{SUPERSCRIPT TWO}",
                                   "\N{SUPERSCRIPT THREE}")),
    *(f"lpt{index}" for index in ("\N{SUPERSCRIPT ONE}",
                                   "\N{SUPERSCRIPT TWO}",
                                   "\N{SUPERSCRIPT THREE}")),
}


class DeploymentError(ValueError):
    """A stable managed-deployment validation error."""


@dataclass(frozen=True)
class Upstream:
    scheme: str
    host: str
    port: int
    prefix: str


@dataclass
class Registration:
    registration_id: str
    client_id: str
    token: str
    pid: int | None
    process_identity: str | None
    dependency_until: float
    last_seen: float
    retained: bool = False
    retained_session_ids: set[str] | None = None


def _exact(value: Any, required: set[str], optional: set[str], where: str) -> None:
    if not isinstance(value, dict):
        raise DeploymentError(f"{where} must be an object")
    missing = required - value.keys()
    unknown = value.keys() - required - optional
    if missing:
        raise DeploymentError(f"{where} missing fields: {', '.join(sorted(missing))}")
    if unknown:
        raise DeploymentError(f"{where} unknown fields: {', '.join(sorted(unknown))}")


def _positive_int(value: Any, where: str, maximum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise DeploymentError(f"{where} must be a positive integer")
    if maximum is not None and value > maximum:
        raise DeploymentError(f"{where} exceeds {maximum}")
    return value


def _scan_for_embedded_secrets(value: Any, path: str = "deployment") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            folded = key.casefold().replace("-", "_")
            if folded in SECRET_FIELD_NAMES:
                raise DeploymentError(f"{path}.{key} may not contain a secret")
            _scan_for_embedded_secrets(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _scan_for_embedded_secrets(child, f"{path}[{index}]")


def validate_deployment(value: Any) -> dict[str, Any]:
    """Validate and return a deployment without accepting legacy fields."""
    required = {"schema_version", "id", "selector", "provider", "credential",
                "gateway", "resources", "service", "runtime", "inference",
                "execution"}
    _exact(value, required, set(), "deployment")
    if value["schema_version"] != DEPLOYMENT_VERSION:
        raise DeploymentError("unsupported deployment schema_version")
    deployment_id = value["id"]
    if (not isinstance(deployment_id, str) or not deployment_id or
            len(deployment_id) > 64 or not deployment_id[0].isalnum() or
            any(character not in "abcdefghijklmnopqrstuvwxyz0123456789._-"
                for character in deployment_id)):
        raise DeploymentError("deployment.id is invalid")

    selector = value["selector"]
    _exact(selector, {"runtimes", "platforms", "modes", "workspaces",
                      "functions", "priority"}, set(), "selector")
    for key in ("runtimes", "platforms", "modes", "workspaces", "functions"):
        items = selector[key]
        if (not isinstance(items, list) or not items or len(items) != len(set(items))
                or any(not isinstance(item, str) or not item for item in items)):
            raise DeploymentError(f"selector.{key} must be a non-empty string set")
    if any(item not in {"read", "edit"} for item in selector["modes"]):
        raise DeploymentError("selector.modes is invalid")
    if any(item not in {"shared", "isolated"} for item in selector["workspaces"]):
        raise DeploymentError("selector.workspaces is invalid")
    priority = selector["priority"]
    if (isinstance(priority, bool) or not isinstance(priority, int) or
            not 0 <= priority <= 100):
        raise DeploymentError("selector.priority must be between 0 and 100")

    _exact(value["provider"], {"id"}, set(), "provider")
    if not isinstance(value["provider"]["id"], str) or not value["provider"]["id"]:
        raise DeploymentError("provider.id must be a non-empty string")

    credential = value["credential"]
    _exact(credential, {"kind", "reference"}, set(), "credential")
    if credential["kind"] != "protocol_store":
        raise DeploymentError("credential.kind must be protocol_store")
    reference = credential["reference"]
    if (not isinstance(reference, str) or not reference or len(reference) > 64 or
            not reference[0].isalnum() or
            any(character not in "abcdefghijklmnopqrstuvwxyz0123456789._-"
                for character in reference)):
        raise DeploymentError("credential.reference is invalid")

    gateway = value["gateway"]
    _exact(gateway, {"upstream", "allowed_methods", "allowed_paths",
                     "credential_header", "credential_scheme", "resource",
                     "timeout_seconds"}, set(), "gateway")
    parse_upstream(gateway["upstream"])
    methods = gateway["allowed_methods"]
    if (not isinstance(methods, list) or not methods or
            len(methods) != len(set(methods)) or
            any(method not in {"GET", "POST", "PUT", "PATCH", "DELETE"}
                for method in methods)):
        raise DeploymentError("gateway.allowed_methods is invalid")
    paths = gateway["allowed_paths"]
    if (not isinstance(paths, list) or not paths or len(paths) != len(set(paths)) or
            any(not isinstance(path, str) or not path.startswith("/") or
                path.startswith(CONTROL_PREFIX) for path in paths)):
        raise DeploymentError("gateway.allowed_paths is invalid")
    header = gateway["credential_header"]
    if (not isinstance(header, str) or not header or
            any(not (character.isalnum() or character == "-") for character in header)):
        raise DeploymentError("gateway.credential_header is invalid")
    if not isinstance(gateway["credential_scheme"], str):
        raise DeploymentError("gateway.credential_scheme must be a string")
    if not isinstance(gateway["resource"], str) or not gateway["resource"]:
        raise DeploymentError("gateway.resource is required")
    _positive_int(gateway["timeout_seconds"], "gateway.timeout_seconds", 3600)

    resources = value["resources"]
    if not isinstance(resources, list) or not resources:
        raise DeploymentError("resources must be a non-empty array")
    resource_ids: set[str] = set()
    for index, resource in enumerate(resources):
        where = f"resources[{index}]"
        _exact(resource, {"id", "capacity", "lease_seconds", "wait_seconds"},
               set(), where)
        resource_id = resource["id"]
        if not isinstance(resource_id, str) or not resource_id:
            raise DeploymentError(f"{where}.id is required")
        if resource_id in resource_ids:
            raise DeploymentError("resource ids must be unique")
        resource_ids.add(resource_id)
        for key in ("capacity", "lease_seconds", "wait_seconds"):
            _positive_int(resource[key], f"{where}.{key}")
    if gateway["resource"] not in resource_ids:
        raise DeploymentError("gateway.resource does not name a resource")

    service = value["service"]
    _exact(service, {"idle_seconds", "max_clients", "max_dependency_seconds",
                     "retention_probe_seconds"},
           set(), "service")
    for key in ("idle_seconds", "max_clients", "max_dependency_seconds",
                "retention_probe_seconds"):
        _positive_int(service[key], f"service.{key}")

    runtime = value["runtime"]
    _exact(runtime, {"profile", "executable", "session", "environment",
                     "arguments"}, set(), "runtime")
    if not isinstance(runtime["profile"], str) or not runtime["profile"]:
        raise DeploymentError("runtime.profile is required")
    executable = runtime["executable"]
    _exact(executable, {"command", "environment"}, set(), "runtime.executable")
    if any(not isinstance(executable[key], str) or not executable[key]
           for key in executable):
        raise DeploymentError("runtime.executable fields must be non-empty strings")
    session = runtime["session"]
    _exact(session, {"config_dir", "environment", "max_agents",
                     "permission_mode"}, set(), "runtime.session")
    config_dir = session["config_dir"]
    if isinstance(config_dir, dict):
        _exact(config_dir, {"posix", "windows"}, set(),
               "runtime.session.config_dir")
        if any(not isinstance(config_dir[key], str) or not config_dir[key]
               for key in config_dir):
            raise DeploymentError(
                "runtime.session.config_dir paths must be non-empty strings")
    elif not isinstance(config_dir, str) or not config_dir:
        raise DeploymentError(
            "runtime.session.config_dir must be a path or platform map")
    for key in ("environment", "permission_mode"):
        if not isinstance(session[key], str) or not session[key]:
            raise DeploymentError(f"runtime.session.{key} must be a non-empty string")
    _positive_int(session["max_agents"], "runtime.session.max_agents")
    if (not isinstance(runtime["environment"], dict) or
            any(not isinstance(key, str) or not isinstance(item, str)
                for key, item in runtime["environment"].items())):
        raise DeploymentError("runtime.environment must map strings to strings")
    if (not isinstance(runtime["arguments"], list) or
            any(not isinstance(item, str) for item in runtime["arguments"])):
        raise DeploymentError("runtime.arguments must contain strings")
    _scan_for_embedded_secrets(runtime)

    inference = value["inference"]
    _exact(inference, {"model", "thinking", "worker_effort",
                       "interactive_effort", "context_tokens",
                       "max_output_tokens"}, set(), "inference")
    if not isinstance(inference["model"], str) or not inference["model"]:
        raise DeploymentError("inference.model must be a non-empty string")
    if not isinstance(inference["thinking"], dict):
        raise DeploymentError("inference.thinking must be an object")
    thinking = inference["thinking"]
    _exact(thinking, {"type"}, {"budget_tokens"}, "inference.thinking")
    if thinking["type"] not in {"adaptive", "enabled", "disabled"}:
        raise DeploymentError("inference.thinking.type is invalid")
    if thinking["type"] == "enabled" and "budget_tokens" not in thinking:
        raise DeploymentError(
            "inference.thinking.budget_tokens is required when enabled")
    if thinking["type"] != "enabled" and "budget_tokens" in thinking:
        raise DeploymentError(
            "inference.thinking.budget_tokens is valid only when enabled")
    if "budget_tokens" in thinking:
        _positive_int(thinking["budget_tokens"],
                      "inference.thinking.budget_tokens")
    if inference["worker_effort"] not in {
            "low", "medium", "high", "xhigh", "max"}:
        raise DeploymentError("inference.worker_effort is invalid")
    if inference["interactive_effort"] not in {
            "unset", "auto", "low", "medium", "high", "xhigh", "max"}:
        raise DeploymentError("inference.interactive_effort is invalid")
    _positive_int(inference["context_tokens"], "inference.context_tokens")
    _positive_int(inference["max_output_tokens"],
                  "inference.max_output_tokens", 131072)
    _scan_for_embedded_secrets(inference)

    execution = value["execution"]
    _exact(execution, {"workspace_engine", "evidence", "permission_policy"},
           set(), "execution")
    if any(not isinstance(execution[key], str) or not execution[key]
           for key in execution):
        raise DeploymentError("execution fields must be non-empty strings")
    return value


def load_deployment(path: str | Path) -> dict[str, Any]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise DeploymentError(f"cannot read deployment: {error}") from error
    return validate_deployment(value)


def parse_upstream(url: Any) -> Upstream:
    if not isinstance(url, str):
        raise DeploymentError("gateway.upstream must be a URL")
    target = urlsplit(url)
    if (target.scheme.casefold() not in {"http", "https"} or not target.netloc or
            target.username is not None or target.password is not None or
            target.query or target.fragment):
        raise DeploymentError("gateway.upstream must be an http(s) URL")
    host = target.hostname
    if host is None:
        raise DeploymentError("gateway.upstream host is invalid")
    try:
        port = target.port or (443 if target.scheme.casefold() == "https" else 80)
    except ValueError as error:
        raise DeploymentError("gateway.upstream port is invalid") from error
    if target.scheme.casefold() == "http":
        try:
            loopback = ipaddress.ip_address(host).is_loopback
        except ValueError:
            loopback = False
        if not loopback:
            raise DeploymentError("plaintext upstream must be a loopback IP")
    return Upstream(target.scheme.casefold(), host, port, target.path.rstrip("/"))


def _validate_windows_path(path: str) -> None:
    candidate = PureWindowsPath(path)
    anchor = candidate.anchor.casefold()
    if (not path or "\0" in path or anchor.startswith(("\\\\.\\", "\\\\?\\", "\\\\"))
            or not candidate.drive or not candidate.is_absolute()):
        raise DeploymentError("credential must use a local absolute drive path")
    for part in candidate.parts:
        if part in (candidate.anchor, candidate.drive, candidate.root):
            continue
        normalized = part.rstrip(" .")
        if (normalized != part or ":" in part or
                normalized.split(".", 1)[0].casefold() in WINDOWS_RESERVED_PARTS):
            raise DeploymentError("credential path contains a Windows alias")


def _windows_credential_identity(path: str) -> tuple[int, int]:
    _validate_windows_path(path)
    candidate = os.path.abspath(path)
    _validate_windows_path(candidate)
    current = os.path.splitdrive(candidate)[0] + os.sep
    relative = candidate[len(current):]
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    info = None
    for part in (part for part in relative.split(os.sep) if part):
        current = os.path.join(current, part)
        info = os.lstat(current)
        if getattr(info, "st_file_attributes", 0) & reparse:
            raise DeploymentError("credential path contains a reparse point")
    if info is None or not info.st_dev or not info.st_ino:
        raise DeploymentError("credential has no stable Windows file identity")
    return info.st_dev, info.st_ino


def _windows_current_sids() -> set[str]:
    from ctypes import wintypes

    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi.OpenProcessToken.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    advapi.OpenProcessToken.restype = wintypes.BOOL
    advapi.GetTokenInformation.argtypes = [
        wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD)]
    advapi.GetTokenInformation.restype = wintypes.BOOL
    advapi.ConvertSidToStringSidW.argtypes = [
        ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR)]
    advapi.ConvertSidToStringSidW.restype = wintypes.BOOL
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [wintypes.HLOCAL]
    kernel.LocalFree.restype = wintypes.HLOCAL
    token = wintypes.HANDLE()
    if not advapi.OpenProcessToken(kernel.GetCurrentProcess(), 0x0008,
                                   ctypes.byref(token)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        values: set[str] = set()
        for information_class in (1, 4):
            needed = wintypes.DWORD()
            advapi.GetTokenInformation(token, information_class, None, 0,
                                       ctypes.byref(needed))
            if not needed.value:
                raise ctypes.WinError(ctypes.get_last_error())
            buffer = ctypes.create_string_buffer(needed.value)
            if not advapi.GetTokenInformation(
                    token, information_class, buffer, needed,
                    ctypes.byref(needed)):
                raise ctypes.WinError(ctypes.get_last_error())
            sid = ctypes.c_void_p.from_buffer(buffer).value
            rendered = wintypes.LPWSTR()
            if not sid or not advapi.ConvertSidToStringSidW(
                    sid, ctypes.byref(rendered)):
                raise ctypes.WinError(ctypes.get_last_error())
            try:
                values.add(rendered.value.casefold())
            finally:
                kernel.LocalFree(rendered)
        return values
    finally:
        kernel.CloseHandle(token)


def _private_windows_acl(descriptor: int) -> None:
    """Require the established owner-only Windows credential ACL."""
    import msvcrt
    from ctypes import wintypes

    class ACL(ctypes.Structure):
        _fields_ = [("AclRevision", ctypes.c_ubyte), ("Sbz1", ctypes.c_ubyte),
                    ("AclSize", ctypes.c_ushort), ("AceCount", ctypes.c_ushort),
                    ("Sbz2", ctypes.c_ushort)]

    class ACE_HEADER(ctypes.Structure):
        _fields_ = [("AceType", ctypes.c_ubyte), ("AceFlags", ctypes.c_ubyte),
                    ("AceSize", ctypes.c_ushort)]

    class ACCESS_ALLOWED_ACE(ctypes.Structure):
        _fields_ = [("Header", ACE_HEADER), ("Mask", wintypes.DWORD),
                    ("SidStart", wintypes.DWORD)]

    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi.GetSecurityInfo.argtypes = [
        wintypes.HANDLE, ctypes.c_int, wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p)]
    advapi.GetSecurityInfo.restype = wintypes.DWORD
    advapi.GetAce.argtypes = [
        ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p)]
    advapi.GetAce.restype = wintypes.BOOL
    advapi.ConvertSidToStringSidW.argtypes = [
        ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR)]
    advapi.ConvertSidToStringSidW.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [wintypes.HLOCAL]
    kernel.LocalFree.restype = wintypes.HLOCAL
    owner = ctypes.c_void_p()
    dacl = ctypes.c_void_p()
    security = ctypes.c_void_p()
    handle = wintypes.HANDLE(msvcrt.get_osfhandle(descriptor))
    result = advapi.GetSecurityInfo(
        handle, 1, 0x00000001 | 0x00000004, ctypes.byref(owner), None,
        ctypes.byref(dacl), None, ctypes.byref(security))
    if result:
        raise OSError(result, "GetSecurityInfo failed")
    try:
        if not owner.value or not dacl.value:
            raise DeploymentError("credential has no private Windows ACL")

        def sid_string(sid: int) -> str:
            rendered = wintypes.LPWSTR()
            if not advapi.ConvertSidToStringSidW(sid, ctypes.byref(rendered)):
                raise ctypes.WinError(ctypes.get_last_error())
            try:
                return rendered.value
            finally:
                kernel.LocalFree(rendered)

        allowed = _windows_current_sids() | {
            "s-1-5-18", "s-1-5-32-544",
        }
        if sid_string(owner.value).casefold() not in allowed:
            raise DeploymentError("credential owner is not the current Windows user")
        acl = ctypes.cast(dacl, ctypes.POINTER(ACL)).contents
        for index in range(acl.AceCount):
            ace_pointer = ctypes.c_void_p()
            if not advapi.GetAce(dacl, index, ctypes.byref(ace_pointer)):
                raise ctypes.WinError(ctypes.get_last_error())
            header = ctypes.cast(ace_pointer, ctypes.POINTER(ACE_HEADER)).contents
            if header.AceType not in {0, 5, 9, 11}:
                continue
            if header.AceType != 0:
                raise DeploymentError(
                    "credential ACL contains an unsupported allow entry")
            sid = ace_pointer.value + ACCESS_ALLOWED_ACE.SidStart.offset
            if sid_string(sid).casefold() not in allowed:
                raise DeploymentError(
                    "credential ACL grants access to another principal")
    finally:
        kernel.LocalFree(security)


def credential_path(reference: str,
                    credential_root: str | Path | None = None) -> Path:
    if (not reference or len(reference) > 64 or not reference[0].isalnum() or
            any(character not in "abcdefghijklmnopqrstuvwxyz0123456789._-"
                for character in reference)):
        raise DeploymentError("credential reference is invalid")
    if credential_root is None:
        if os.name == "nt":
            base = Path(os.environ.get("LOCALAPPDATA", str(Path.home())))
        else:
            base = Path(os.environ.get("XDG_CONFIG_HOME",
                                       str(Path.home() / ".config")))
        credential_root = base / "agent-delegation-protocol" / "credentials"
    return Path(credential_root) / reference


def _windows_user_sid() -> str:
    from ctypes import wintypes

    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi.OpenProcessToken.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    advapi.OpenProcessToken.restype = wintypes.BOOL
    advapi.GetTokenInformation.argtypes = [
        wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD)]
    advapi.GetTokenInformation.restype = wintypes.BOOL
    advapi.ConvertSidToStringSidW.argtypes = [
        ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR)]
    advapi.ConvertSidToStringSidW.restype = wintypes.BOOL
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [wintypes.HLOCAL]
    kernel.LocalFree.restype = wintypes.HLOCAL
    token = wintypes.HANDLE()
    if not advapi.OpenProcessToken(kernel.GetCurrentProcess(), 0x0008,
                                   ctypes.byref(token)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        needed = wintypes.DWORD()
        advapi.GetTokenInformation(token, 1, None, 0, ctypes.byref(needed))
        buffer = ctypes.create_string_buffer(needed.value)
        if not advapi.GetTokenInformation(token, 1, buffer, needed,
                                          ctypes.byref(needed)):
            raise ctypes.WinError(ctypes.get_last_error())
        sid = ctypes.c_void_p.from_buffer(buffer).value
        rendered = wintypes.LPWSTR()
        if not sid or not advapi.ConvertSidToStringSidW(
                sid, ctypes.byref(rendered)):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            return rendered.value
        finally:
            kernel.LocalFree(rendered)
    finally:
        kernel.CloseHandle(token)


def _protect_windows_path(path: Path) -> None:
    """Install a protected DACL for user, LocalSystem, and Administrators."""
    from ctypes import wintypes

    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wintypes.DWORD)]
    advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL
    advapi.SetFileSecurityW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_void_p]
    advapi.SetFileSecurityW.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [wintypes.HLOCAL]
    kernel.LocalFree.restype = wintypes.HLOCAL
    descriptor = ctypes.c_void_p()
    sddl = f"O:{_windows_user_sid()}D:P(A;;FA;;;{_windows_user_sid()})(A;;FA;;;SY)(A;;FA;;;BA)"
    if not advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            sddl, 1, ctypes.byref(descriptor), None):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        if not advapi.SetFileSecurityW(str(path), 0x00000001 | 0x00000004,
                                      descriptor):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        kernel.LocalFree(descriptor)


def write_credential(reference: str, value: str,
                     credential_root: str | Path | None = None) -> Path:
    if not isinstance(value, str) or not value or "\n" in value or "\r" in value:
        raise DeploymentError("credential must contain exactly one non-empty line")
    path = credential_path(reference, credential_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        _protect_windows_path(path.parent)
    else:
        os.chmod(path.parent, 0o700)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.",
                                             dir=path.parent)
    temporary_path = Path(temporary)
    try:
        if os.name == "nt":
            os.close(descriptor)
            descriptor = -1
            temporary_path.write_text(value + "\n", encoding="utf-8")
            _protect_windows_path(temporary_path)
        else:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                descriptor = -1
                handle.write(value + "\n")
                handle.flush()
                os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        if os.name == "nt":
            check = os.open(path, os.O_RDONLY)
            try:
                _private_windows_acl(check)
            finally:
                os.close(check)
        return path
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary_path.unlink(missing_ok=True)


def remove_credential(reference: str,
                      credential_root: str | Path | None = None) -> bool:
    path = credential_path(reference, credential_root)
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return False
    if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise DeploymentError("credential is not a regular non-symlink file")
    # Validate protection before allowing the store operation to remove it.
    read_credential(path)
    try:
        os.unlink(path)
    except FileNotFoundError:
        return False
    return True


def read_credential(reference: str | Path) -> str:
    """Read one protected credential without following the final symlink."""
    reference = os.path.abspath(os.path.expanduser(str(reference)))
    before = _windows_credential_identity(reference) if os.name == "nt" else None
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(reference, flags)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise DeploymentError("credential is not a regular file")
        if os.name == "nt":
            if before != (info.st_dev, info.st_ino):
                raise DeploymentError("credential changed while it was opened")
            if before != _windows_credential_identity(reference):
                raise DeploymentError("credential changed while it was validated")
            _private_windows_acl(descriptor)
        elif stat.S_IMODE(info.st_mode) & 0o077:
            raise DeploymentError("credential permissions are broader than 0600")
        with os.fdopen(descriptor, encoding="utf-8") as handle:
            descriptor = -1
            value = handle.read().rstrip("\n")
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if not value or "\n" in value or "\r" in value:
        raise DeploymentError("credential must contain exactly one non-empty line")
    return value


def _process_identity(pid: int) -> str | None:
    if pid <= 1:
        return None
    if os.name != "nt":
        try:
            fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
            return f"{pid}:{fields[19]}"
        except (OSError, IndexError):
            ps = next((candidate for candidate in ("/bin/ps", "/usr/bin/ps")
                       if os.path.isfile(candidate)), None)
            if ps:
                try:
                    result = subprocess.run(
                        [ps, "-o", "lstart=", "-p", str(pid)],
                        stdin=subprocess.DEVNULL, capture_output=True, text=True,
                        timeout=2, check=False,
                        env={"PATH": "/usr/bin:/bin"})
                    started = result.stdout.strip()
                    if result.returncode == 0 and started:
                        return f"{pid}:{hashlib.sha256(started.encode()).hexdigest()}"
                except (OSError, subprocess.SubprocessError):
                    pass
            return None
    try:
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL,
                                       wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetProcessTimes.argtypes = [
            wintypes.HANDLE, ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME), ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME)]
        kernel.GetProcessTimes.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return None
        created, exited, kernel_time, user_time = (wintypes.FILETIME() for _ in range(4))
        try:
            if not kernel.GetProcessTimes(handle, ctypes.byref(created),
                                          ctypes.byref(exited),
                                          ctypes.byref(kernel_time),
                                          ctypes.byref(user_time)):
                return None
            stamp = (created.dwHighDateTime << 32) | created.dwLowDateTime
            return f"{pid}:{stamp}"
        finally:
            kernel.CloseHandle(handle)
    except (AttributeError, OSError):
        return None


class ClientRegistry:
    def __init__(self, maximum: int, max_dependency_seconds: int,
                 state_file: Path) -> None:
        self.maximum = maximum
        self.max_dependency_seconds = max_dependency_seconds
        self.state_file = state_file
        self.lock = threading.Lock()
        self.records: dict[str, Registration] = {}
        self._load()

    def _load(self) -> None:
        try:
            values = json.loads(self.state_file.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            return
        if not isinstance(values, list):
            return
        for value in values:
            try:
                if not isinstance(value, dict):
                    continue
                registration_id = value["registration_id"]
                client_id = value["client_id"]
                token = value["token"]
                pid = value.get("pid")
                identity = value.get("process_identity")
                if (not isinstance(registration_id, str) or not registration_id or
                        not isinstance(client_id, str) or not client_id or
                        not isinstance(token, str) or not token or
                        (pid is not None and
                         (isinstance(pid, bool) or not isinstance(pid, int) or
                          pid <= 1)) or
                        (identity is not None and not isinstance(identity, str))):
                    continue
                retained = value.get("retained") is True
                session_ids = value.get("retained_session_ids")
                if session_ids is not None and (
                        not isinstance(session_ids, list) or
                        any(not isinstance(item, str) or not item
                            for item in session_ids) or
                        len(session_ids) != len(set(session_ids))):
                    continue
                record = Registration(
                    registration_id, client_id, token, pid, identity, 0,
                    float(value.get("last_seen", 0)), retained,
                    set(session_ids) if retained and session_ids is not None else None)
            except (KeyError, TypeError, ValueError):
                continue
            if (not record.registration_id or not record.client_id or not record.token
                    or (not record.retained and
                        (record.pid is None or
                         _process_identity(record.pid) != record.process_identity))):
                continue
            self.records[record.registration_id] = record

    def _persist(self) -> None:
        values = [{"registration_id": record.registration_id,
                   "client_id": record.client_id, "token": record.token,
                   "pid": record.pid,
                   "process_identity": record.process_identity,
                   "last_seen": record.last_seen, "retained": record.retained,
                   "retained_session_ids": (
                       sorted(record.retained_session_ids)
                       if record.retained_session_ids is not None else None)}
                  for record in self.records.values()]
        _atomic_json(self.state_file, values)

    def _reap(self) -> None:
        now = time.time()
        changed = False
        for key, record in list(self.records.items()):
            if record.retained or record.dependency_until > now:
                continue
            if record.pid is None or _process_identity(record.pid) != record.process_identity:
                self.records.pop(key, None)
                changed = True
        if changed:
            self._persist()

    def register(self, client_id: str, pid: int | None,
                 dependency_seconds: int) -> Registration | None:
        with self.lock:
            self._reap()
            if len(self.records) >= self.maximum:
                return None
            registration_id = secrets.token_urlsafe(24)
            record = Registration(
                registration_id, client_id, secrets.token_urlsafe(32), pid,
                _process_identity(pid) if pid else None,
                time.time() + min(dependency_seconds, self.max_dependency_seconds),
                time.time(),
            )
            self.records[registration_id] = record
            self._persist()
            return record

    def resolve(self, authorization: str) -> Registration | None:
        found = None
        with self.lock:
            self._reap()
            for record in self.records.values():
                if hmac.compare_digest(authorization, "Bearer " + record.token):
                    found = record
            if found:
                found.last_seen = time.time()
                self._persist()
        return found

    def heartbeat(self, registration_id: str, seconds: int) -> bool:
        with self.lock:
            record = self.records.get(registration_id)
            if record is None:
                return False
            record.dependency_until = time.time() + min(
                max(0, seconds), self.max_dependency_seconds)
            record.last_seen = time.time()
            self._persist()
            return True

    def retain(self, registration_id: str,
               session_ids: set[str] | None = None) -> bool:
        with self.lock:
            record = self.records.get(registration_id)
            if record is None:
                return False
            record.retained = True
            record.retained_session_ids = (
                set(session_ids) if session_ids is not None else None)
            record.pid = None
            record.process_identity = None
            self._persist()
            return True

    def reconcile_retained(self, session_ids: set[str]) -> None:
        """Reconcile retained registrations against a successful roster probe."""
        with self.lock:
            changed = False
            for key, record in list(self.records.items()):
                if not record.retained:
                    continue
                owned = record.retained_session_ids
                if owned is None:
                    if session_ids:
                        record.retained_session_ids = set(session_ids)
                    else:
                        self.records.pop(key, None)
                    changed = True
                    continue
                remaining = owned & session_ids
                if not remaining:
                    self.records.pop(key, None)
                    changed = True
                elif remaining != owned:
                    record.retained_session_ids = remaining
                    changed = True
            if changed:
                self._persist()

    def unregister(self, registration_id: str) -> bool:
        with self.lock:
            changed = self.records.pop(registration_id, None) is not None
            if changed:
                self._persist()
            return changed

    def snapshot(self) -> list[dict[str, Any]]:
        with self.lock:
            self._reap()
            return [{"registration_id": record.registration_id,
                     "client_id": record.client_id, "pid": record.pid,
                     "dependency": record.dependency_until > time.time(),
                     "retained": record.retained,
                     "retained_session_ids": (
                         sorted(record.retained_session_ids)
                         if record.retained_session_ids is not None else None)}
                    for record in self.records.values()]

    def active(self) -> bool:
        return bool(self.snapshot())


class ManagedHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], deployment: dict[str, Any],
                 credential: str, admin_token: str, registry_file: Path):
        self.deployment = deployment
        self.upstream = parse_upstream(deployment["gateway"]["upstream"])
        self.credential = credential
        self.admin_token = admin_token
        service = deployment["service"]
        self.clients = ClientRegistry(service["max_clients"],
                                      service["max_dependency_seconds"],
                                      registry_file)
        self.resources = {
            item["id"]: (Lane(item["capacity"], item["lease_seconds"]), item)
            for item in deployment["resources"]
        }
        self.ssl_context = ssl.create_default_context()
        self.activity_lock = threading.Lock()
        self.active_requests = 0
        self.last_activity = time.monotonic()
        super().__init__(address, ManagedHandler)

    @contextlib.contextmanager
    def activity(self) -> Iterator[None]:
        with self.activity_lock:
            self.active_requests += 1
            self.last_activity = time.monotonic()
        try:
            yield
        finally:
            with self.activity_lock:
                self.active_requests -= 1
                self.last_activity = time.monotonic()

    def idle(self) -> bool:
        with self.activity_lock:
            quiet = (self.active_requests == 0 and
                     time.monotonic() - self.last_activity >=
                     self.deployment["service"]["idle_seconds"])
        return quiet and not self.clients.active()


class ManagedHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "delegation-managed-gateway/1"

    def log_message(self, _format: str, *_args: Any) -> None:
        return

    def _error(self, status: int, message: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(message)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(message)

    def _json(self, status: int, value: dict[str, Any]) -> None:
        body = json.dumps(value, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def _control(self, operation: str) -> None:
        if not hmac.compare_digest(self.headers.get("Authorization", ""),
                                   "Bearer " + self.server.admin_token):
            self._error(401, b"unauthorized\n")
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length < 0 or length > MAX_CONTROL_BYTES:
                raise ValueError
            value = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(value, dict):
                raise ValueError
        except (OSError, ValueError, json.JSONDecodeError):
            self._error(400, b"invalid request\n")
            return
        if operation == "status":
            self._json(200, {"status": "ready",
                             "deployment_id": self.server.deployment["id"],
                             "clients": self.server.clients.snapshot()})
            return
        if operation == "stop":
            if self.server.clients.active():
                self._error(409, b"service has active clients\n")
                return
            self._json(200, {"status": "completed"})
            threading.Thread(target=self.server.shutdown, daemon=True).start()
            return
        if operation == "register":
            if set(value) - {"client_id", "pid", "dependency_seconds"}:
                self._error(400, b"invalid request\n")
                return
            client_id = value.get("client_id")
            pid = value.get("pid")
            seconds = value.get("dependency_seconds", 0)
            if (not isinstance(client_id, str) or not client_id or
                    (pid is not None and (isinstance(pid, bool) or
                                          not isinstance(pid, int) or pid <= 1)) or
                    isinstance(seconds, bool) or not isinstance(seconds, int) or
                    seconds < 0):
                self._error(400, b"invalid registration\n")
                return
            record = self.server.clients.register(client_id, pid, seconds)
            if record is None:
                self._error(409, b"client limit reached\n")
                return
            self._json(200, {"registration_id": record.registration_id,
                             "token": record.token})
            return
        registration_id = value.get("registration_id")
        if not isinstance(registration_id, str):
            self._error(400, b"registration_id required\n")
            return
        if operation == "heartbeat":
            seconds = value.get("dependency_seconds", 0)
            if isinstance(seconds, bool) or not isinstance(seconds, int) or seconds < 0:
                self._error(400, b"invalid dependency_seconds\n")
                return
            ok = self.server.clients.heartbeat(registration_id, seconds)
        elif operation == "retain":
            session_ids = value.get("retained_session_ids")
            if session_ids is not None and (
                    not isinstance(session_ids, list) or not session_ids or
                    any(not isinstance(item, str) or not item
                        for item in session_ids) or
                    len(session_ids) != len(set(session_ids))):
                self._error(400, b"invalid retained_session_ids\n")
                return
            ok = self.server.clients.retain(
                registration_id,
                set(session_ids) if session_ids is not None else None)
        else:
            ok = self.server.clients.unregister(registration_id)
        self._json(200, {"status": "completed" if ok else "not_found"})

    def _dispatch(self) -> None:
        self.close_connection = True
        target = urlsplit(self.path)
        if target.scheme or target.netloc or target.fragment:
            self._error(404, b"not found\n")
            return
        if target.path == CONTROL_PREFIX + "binding":
            if self.command != "GET":
                self._error(405, b"method not allowed\n")
                return
            record = self.server.clients.resolve(
                self.headers.get("Authorization", ""))
            if record is None:
                self._error(401, b"unauthorized\n")
                return
            self._json(200, {
                "deployment_id": self.server.deployment["id"],
                "registration_id": record.registration_id,
            })
            return
        if target.path.startswith(CONTROL_PREFIX):
            operation = target.path[len(CONTROL_PREFIX):]
            if self.command != "POST" or operation not in {
                    "status", "stop", "register", "heartbeat", "retain",
                    "unregister"}:
                self._error(404, b"not found\n")
                return
            self._control(operation)
            return
        gateway = self.server.deployment["gateway"]
        if self.command not in gateway["allowed_methods"]:
            self._error(405, b"method not allowed\n")
            return
        if target.path not in gateway["allowed_paths"]:
            self._error(404, b"not found\n")
            return
        record = self.server.clients.resolve(self.headers.get("Authorization", ""))
        if record is None:
            self._error(401, b"unauthorized\n")
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length < 0:
                raise ValueError
            body = self.rfile.read(length)
        except (OSError, ValueError):
            self._error(400, b"invalid content length\n")
            return
        upstream_target = target.path + (("?" + target.query) if target.query else "")
        with self.server.activity():
            self._forward(body, upstream_target, record)

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = _dispatch

    def _forward(self, body: bytes, target: str, record: Registration) -> None:
        gateway = self.server.deployment["gateway"]
        lane, resource = self.server.resources[gateway["resource"]]
        owner = f"{self.server.deployment['id']}:{record.registration_id}"
        try:
            token = lane.acquire(owner, timeout_seconds=resource["wait_seconds"])
        except TimeoutError:
            self._error(429, b"provider resource remained busy\n")
            return
        stop = threading.Event()
        lost = threading.Event()

        def renew() -> None:
            while not stop.wait(max(0.25, resource["lease_seconds"] / 3)):
                if not lane.heartbeat(owner, token):
                    lost.set()
                    return

        heartbeat = threading.Thread(target=renew, daemon=True)
        heartbeat.start()
        try:
            self._forward_acquired(body, target, lost)
        finally:
            stop.set()
            heartbeat.join(timeout=1)
            lane.release(owner, token)

    def _forward_acquired(self, body: bytes, target: str,
                          lost: threading.Event) -> None:
        upstream = self.server.upstream
        timeout = self.server.deployment["gateway"]["timeout_seconds"]
        if upstream.scheme == "https":
            connection = http.client.HTTPSConnection(
                upstream.host, upstream.port, timeout=timeout,
                context=self.server.ssl_context)
        else:
            connection = http.client.HTTPConnection(upstream.host, upstream.port,
                                                    timeout=timeout)
        gateway = self.server.deployment["gateway"]
        denied = HOP_BY_HOP | {"host", "content-length", "authorization",
                               "x-api-key", gateway["credential_header"].casefold()}
        headers = {key: value for key, value in self.headers.items()
                   if key.casefold() not in denied and
                   len(key) + len(value) <= MAX_HEADER_BYTES}
        prefix = gateway["credential_scheme"]
        headers[gateway["credential_header"]] = (
            (prefix + " ") if prefix else "") + self.server.credential
        try:
            connection.request(self.command, upstream.prefix + target,
                               body=body, headers=headers)
            response = connection.getresponse()
        except (OSError, http.client.HTTPException):
            self._error(502, b"upstream unavailable\n")
            connection.close()
            return
        try:
            self.send_response(response.status, response.reason)
            for key, value in response.getheaders():
                if key.casefold() not in HOP_BY_HOP | {"server", "date"}:
                    self.send_header(key, value)
            self.send_header("Connection", "close")
            self.end_headers()
            while not lost.is_set():
                chunk = response.read1(65536)
                if not chunk:
                    break
                self.wfile.write(chunk)
                self.wfile.flush()
        except (OSError, http.client.HTTPException):
            pass
        finally:
            connection.close()


def _atomic_json(path: Path, value: Any, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        if os.name != "nt":
            os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        if os.name == "nt":
            _protect_windows_path(Path(temporary))
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _atomic_text(path: Path, value: str, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        if os.name != "nt":
            os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "w", encoding="ascii") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        if os.name == "nt":
            _protect_windows_path(Path(temporary))
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _default_state_dir(deployment_id: str) -> Path:
    if os.name == "nt":
        root = Path(os.environ.get("LOCALAPPDATA", str(Path.home())))
    else:
        root = Path(os.environ.get("XDG_STATE_HOME",
                                   str(Path.home() / ".local" / "state")))
    return root / "agent-delegation-protocol" / "services" / deployment_id


def _fingerprint(deployment: dict[str, Any]) -> str:
    canonical = json.dumps(deployment, sort_keys=True,
                           separators=(",", ":")).encode()
    return hashlib.sha256(canonical).hexdigest()


def _preferred_port(state: Path) -> int:
    try:
        return int((state / "service.port").read_text(encoding="ascii"))
    except (OSError, ValueError):
        try:
            descriptor = json.loads(
                (state / "service.json").read_text(encoding="utf-8"))
            return int(descriptor.get("port", 0))
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return 0


def _has_retained_registry(state: Path) -> bool:
    try:
        records = json.loads(
            (state / "registrations.json").read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return False
    return isinstance(records, list) and any(
        isinstance(record, dict) and record.get("retained") is True
        for record in records)


def _retained_sessions(deployment: dict[str, Any]) -> set[str] | None:
    if deployment["runtime"]["profile"] != "claude-code":
        return None
    try:
        from claude_runtime import background_session_ids
        return background_session_ids(deployment)
    except (ImportError, OSError, ValueError, subprocess.SubprocessError):
        return None


def serve(deployment_path: str | Path, state_dir: str | Path | None = None) -> int:
    deployment = load_deployment(deployment_path)
    state = Path(state_dir) if state_dir else _default_state_dir(deployment["id"])
    state.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        _protect_windows_path(state)
    else:
        os.chmod(state, 0o700)
    credential = read_credential(credential_path(
        deployment["credential"]["reference"]))
    admin_token = secrets.token_urlsafe(32)
    endpoint = state / "service.json"
    secret_file = state / "service.secret"
    preferred = _preferred_port(state)
    try:
        server = ManagedHTTPServer(("127.0.0.1", preferred), deployment,
                                   credential, admin_token,
                                   state / "registrations.json")
    except OSError:
        if preferred and _has_retained_registry(state):
            raise DeploymentError(
                "preferred gateway port is unavailable for retained clients")
        server = ManagedHTTPServer(("127.0.0.1", 0), deployment, credential,
                                   admin_token, state / "registrations.json")
    _atomic_text(secret_file, admin_token + "\n")
    port_file = state / "service.port"
    _atomic_text(port_file, f"{server.server_address[1]}\n")
    _atomic_json(endpoint, {
        "schema_version": DEPLOYMENT_VERSION,
        "deployment_id": deployment["id"], "transport": "http-loopback",
        "deployment_fingerprint": _fingerprint(deployment),
        "host": "127.0.0.1", "port": server.server_address[1],
        "pid": os.getpid(), "process_identity": _process_identity(os.getpid()),
        "secret_file": str(secret_file),
    })
    stopped = threading.Event()

    def monitor() -> None:
        next_probe = time.monotonic()
        while not stopped.wait(0.25):
            now = time.monotonic()
            if now >= next_probe:
                sessions = _retained_sessions(deployment)
                if sessions is not None:
                    server.clients.reconcile_retained(sessions)
                next_probe = now + deployment["service"]["retention_probe_seconds"]
            if server.idle():
                threading.Thread(target=server.shutdown, daemon=True).start()
                return

    thread = threading.Thread(target=monitor, daemon=True)
    thread.start()

    def stop(_signum: int, _frame: Any) -> None:
        threading.Thread(target=server.shutdown, daemon=True).start()

    if threading.current_thread() is threading.main_thread():
        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
    try:
        server.serve_forever(poll_interval=0.2)
    finally:
        stopped.set()
        thread.join(timeout=1)
        server.server_close()
        current = _descriptor(state, deployment["id"], _fingerprint(deployment),
                              require_live=False)
        if current and current.get("process_identity") == _process_identity(os.getpid()):
            endpoint.unlink(missing_ok=True)
            secret_file.unlink(missing_ok=True)
    return 0


def _descriptor(state: Path, deployment_id: str, fingerprint: str,
                require_live: bool = True) -> dict[str, Any] | None:
    try:
        value = json.loads((state / "service.json").read_text(encoding="utf-8"))
        if (value.get("schema_version") != DEPLOYMENT_VERSION or
                value.get("deployment_id") != deployment_id or
                value.get("transport") != "http-loopback" or
                value.get("host") != "127.0.0.1"):
            return None
        live = _process_identity(int(value["pid"])) == value.get("process_identity")
        if value.get("deployment_fingerprint") != fingerprint:
            if live:
                raise DeploymentError(
                    "a different deployment configuration is already running")
            return None
        if require_live and not live:
            return None
        return value
    except DeploymentError:
        raise
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None


class ServiceClient:
    def __init__(self, descriptor: dict[str, Any]):
        self.deployment_id = descriptor["deployment_id"]
        self.host = descriptor["host"]
        self.port = int(descriptor["port"])
        self.base_url = f"http://{self.host}:{self.port}"
        self.admin_token = Path(descriptor["secret_file"]).read_text(
            encoding="ascii").strip()

    def _control(self, operation: str, value: dict[str, Any]) -> dict[str, Any]:
        connection = http.client.HTTPConnection(self.host, self.port, timeout=10)
        body = json.dumps(value, separators=(",", ":"))
        try:
            connection.request("POST", CONTROL_PREFIX + operation, body=body,
                               headers={"Authorization": "Bearer " + self.admin_token,
                                        "Content-Type": "application/json"})
            response = connection.getresponse()
            answer = json.loads(response.read())
            if response.status >= 400:
                raise DeploymentError(f"service {operation} failed: HTTP {response.status}")
            return answer
        except (OSError, http.client.HTTPException, json.JSONDecodeError) as error:
            raise DeploymentError(f"service {operation} failed: {error}") from error
        finally:
            connection.close()

    def status(self) -> dict[str, Any]:
        return self._control("status", {})

    def stop(self) -> bool:
        return self._control("stop", {})["status"] == "completed"

    def register(self, client_id: str, pid: int | None = None,
                 dependency_seconds: int = 0) -> "GatewayBinding":
        if pid is None:
            pid = os.getpid()
        answer = self._control("register", {
            "client_id": client_id, "pid": pid,
            "dependency_seconds": dependency_seconds,
        })
        return GatewayBinding(self, answer["registration_id"], answer["token"])


class GatewayBinding:
    def __init__(self, client: ServiceClient, registration_id: str, token: str):
        self.client = client
        self.deployment_id = client.deployment_id
        self.base_url = client.base_url
        self.registration_id = registration_id
        self.token = token
        self.closed = False

    def heartbeat(self, dependency_seconds: int = 0) -> bool:
        answer = self.client._control("heartbeat", {
            "registration_id": self.registration_id,
            "dependency_seconds": dependency_seconds,
        })
        return answer["status"] == "completed"

    def retain(self, session_ids: set[str] | None = None,
               dependency_seconds: int | None = None) -> bool:
        del dependency_seconds
        payload: dict[str, Any] = {"registration_id": self.registration_id}
        if session_ids is not None:
            payload["retained_session_ids"] = sorted(session_ids)
        answer = self.client._control("retain", payload)
        return answer["status"] == "completed"

    def close(self) -> None:
        if not self.closed:
            self.client._control("unregister", {
                "registration_id": self.registration_id,
            })
            self.closed = True

    def __enter__(self) -> "GatewayBinding":
        return self

    def __exit__(self, *_args: Any) -> None:
        self.close()


def ensure_service(deployment_path: str | Path,
                   state_dir: str | Path | None = None,
                   timeout_seconds: float = 10) -> ServiceClient:
    """Atomically join a healthy singleton or start its replacement."""
    deployment_path = Path(deployment_path).resolve()
    deployment = load_deployment(deployment_path)
    fingerprint = _fingerprint(deployment)
    state = Path(state_dir) if state_dir else _default_state_dir(deployment["id"])
    state.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout_seconds
    lock_path = state / "starting.json"
    while time.monotonic() < deadline:
        descriptor = _descriptor(state, deployment["id"], fingerprint)
        if descriptor:
            try:
                client = ServiceClient(descriptor)
                client.status()
                return client
            except (OSError, DeploymentError):
                time.sleep(0.05)
                continue
        try:
            descriptor_fd = os.open(lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                                    0o600)
        except FileExistsError:
            try:
                lock = json.loads(lock_path.read_text(encoding="utf-8"))
                if _process_identity(int(lock["pid"])) != lock.get("process_identity"):
                    lock_path.unlink(missing_ok=True)
                    continue
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                # The elected starter creates the file before it can publish
                # its identity. Do not let another contender mistake that
                # short write window for a stale election.
                try:
                    old = time.time() - lock_path.stat().st_mtime > 1
                except OSError:
                    old = False
                if old:
                    lock_path.unlink(missing_ok=True)
                else:
                    time.sleep(0.05)
                continue
            time.sleep(0.05)
            continue
        try:
            with os.fdopen(descriptor_fd, "w", encoding="utf-8") as handle:
                json.dump({"pid": os.getpid(),
                           "process_identity": _process_identity(os.getpid())}, handle)
            command = [sys.executable, str(Path(__file__).resolve()), "serve",
                       "--deployment", str(deployment_path), "--state-dir", str(state)]
            child = subprocess.Popen(
                command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, start_new_session=(os.name != "nt"),
                creationflags=(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                               if os.name == "nt" else 0))
            _DETACHED_CHILDREN.append(child)
            while time.monotonic() < deadline:
                descriptor = _descriptor(state, deployment["id"], fingerprint)
                if descriptor:
                    try:
                        client = ServiceClient(descriptor)
                        client.status()
                        return client
                    except (OSError, DeploymentError):
                        pass
                time.sleep(0.05)
        finally:
            lock_path.unlink(missing_ok=True)
    raise DeploymentError("managed service did not become ready")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    serve_parser = subparsers.add_parser("serve")
    serve_parser.add_argument("--deployment", required=True)
    serve_parser.add_argument("--state-dir")
    arguments = parser.parse_args(argv)
    try:
        return serve(arguments.deployment, arguments.state_dir)
    except (OSError, DeploymentError) as error:
        print(f"managed service: {error}", file=sys.stderr)
        return 78


if __name__ == "__main__":
    raise SystemExit(main())
