#!/usr/bin/env python3
"""Non-destructive, protocol-v2-owned host hook configuration."""
from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

STATUS_PREFIX = "Delegation protocol v2:"

# Single source of truth for which settings file each host's hook entries
# live in. install.py imports this table; every host-specific read of the
# filename goes through it. A None value means the host has no hook
# configuration JSON: Pi's enforcement is a discovered extension, so its
# integration is deployed as plain resources and no settings are edited.
HOST_SETTINGS_FILE = {"claude": "settings.json", "codex": "hooks.json", "pi": None}


def quote(value: str) -> str:
    return '"' + value.replace('"', '\\"') + '"'


def load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ValueError(f"invalid JSON at {path}: {error}") from error
    if not isinstance(value, dict):
        raise ValueError(f"JSON root at {path} must be an object")
    return value


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.",
                                             dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def handler(command: str, status: str) -> dict[str, Any]:
    return {
        "type": "command",
        "command": command,
        "timeout": 5,
        "statusMessage": f"{STATUS_PREFIX} {status}",
    }


def groups(host: str, hook_path: Path, python_executable: str) -> dict[str, list[dict[str, Any]]]:
    base = f"{quote(python_executable)} {quote(str(hook_path))}"
    common = {
        "UserPromptSubmit": [{"hooks": [handler(base + " prompt", "classify prompt")]}],
        "SubagentStart": [{"hooks": [handler(base + " worker-start", "track worker start")]}],
        "SubagentStop": [{"hooks": [handler(base + " worker-complete", "track worker completion")]}],
        "PreToolUse": [{"matcher": "*", "hooks": [handler(base + " pre-mutation", "enforce delegation")]}],
        "Stop": [{"hooks": [handler(base + " turn-stop", "finish turn bookkeeping")]}],
    }
    if host == "claude":
        common["PostToolUseFailure"] = [{
            "matcher": "Agent",
            "hooks": [handler(base + " worker-complete", "track Agent failure")],
        }]
    return common


def strip_owned_hooks(settings: dict[str, Any]) -> None:
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        return
    for event in list(hooks):
        existing = hooks[event]
        if not isinstance(existing, list):
            continue
        retained = []
        for group in existing:
            if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
                retained.append(group)
                continue
            handlers = [item for item in group["hooks"] if not (
                isinstance(item, dict) and
                str(item.get("statusMessage", "")).startswith(STATUS_PREFIX)
            )]
            if handlers:
                retained.append({**group, "hooks": handlers})
        if retained:
            hooks[event] = retained
        else:
            del hooks[event]
    if not hooks:
        settings.pop("hooks", None)


def merge_groups(settings: dict[str, Any], additions: dict[str, list[dict[str, Any]]]) -> None:
    strip_owned_hooks(settings)
    hooks = settings.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError("refusing to replace non-object hooks setting")
    for event, groups_to_add in additions.items():
        current = hooks.setdefault(event, [])
        if not isinstance(current, list):
            raise ValueError(f"refusing to replace non-array hooks.{event}")
        current.extend(groups_to_add)


def install(host: str, home: Path, hook_path: Path, python_executable: str) -> None:
    settings_path = home / HOST_SETTINGS_FILE[host]
    state_dir = home / ".delegation-protocol"
    settings = load_json(settings_path)
    backup = state_dir / f"{settings_path.name}.before-first-install"
    if settings_path.exists() and not backup.exists():
        state_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(settings_path, backup)
    merge_groups(settings, groups(host, hook_path, python_executable))
    atomic_json(settings_path, settings)


def uninstall(host: str, home: Path) -> None:
    settings_file = HOST_SETTINGS_FILE.get(host)
    if settings_file is None:
        # A host with no settings file only ever owns the host-settings
        # manifest record, never settings content.
        manifest_path = home / ".delegation-protocol" / "host-settings.json"
        manifest_path.unlink(missing_ok=True)
        return
    settings_path = home / settings_file
    state_dir = home / ".delegation-protocol"
    manifest_path = state_dir / "host-settings.json"
    # Older installs recorded only the environment entries they inserted.
    # Keep that record intact during later installs, then remove precisely
    # those unchanged values on uninstall. New installs do not create it.
    legacy_manifest = load_json(manifest_path) if manifest_path.exists() else {}
    if settings_path.exists():
        settings = load_json(settings_path)
        strip_owned_hooks(settings)
        environment = settings.get("env")
        installed = legacy_manifest.get("added_environment", {})
        if isinstance(environment, dict) and isinstance(installed, dict):
            for key, value in installed.items():
                if environment.get(key) == value:
                    del environment[key]
            if not environment:
                settings.pop("env", None)
        atomic_json(settings_path, settings)
    manifest_path.unlink(missing_ok=True)
