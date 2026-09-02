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
CLAUDE_ENV_DEFAULTS = {
    "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS": "1",
    "CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH": "3",
    "CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS": "20",
}


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
        "Stop": [{"hooks": [handler(base + " turn-stop", "verify delegation")]}],
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
    settings_path = home / ("settings.json" if host == "claude" else "hooks.json")
    state_dir = home / ".delegation-protocol"
    settings = load_json(settings_path)
    backup = state_dir / f"{settings_path.name}.before-first-install"
    if settings_path.exists() and not backup.exists():
        state_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(settings_path, backup)
    merge_groups(settings, groups(host, hook_path, python_executable))
    added_environment: dict[str, str] = {}
    if host == "claude":
        environment = settings.setdefault("env", {})
        if not isinstance(environment, dict):
            raise ValueError("refusing to replace non-object env setting")
        for key, value in CLAUDE_ENV_DEFAULTS.items():
            if key not in environment:
                environment[key] = value
                added_environment[key] = value
    atomic_json(settings_path, settings)
    atomic_json(state_dir / "host-settings.json", {
        "schema_version": 2,
        "host": host,
        "settings_path": str(settings_path),
        "added_environment": added_environment,
    })


def uninstall(host: str, home: Path) -> None:
    settings_path = home / ("settings.json" if host == "claude" else "hooks.json")
    state_dir = home / ".delegation-protocol"
    manifest_path = state_dir / "host-settings.json"
    manifest = load_json(manifest_path) if manifest_path.exists() else {}
    if settings_path.exists():
        settings = load_json(settings_path)
        strip_owned_hooks(settings)
        environment = settings.get("env")
        installed = manifest.get("added_environment", {})
        if isinstance(environment, dict) and isinstance(installed, dict):
            for key, value in installed.items():
                if environment.get(key) == value:
                    del environment[key]
            if not environment:
                settings.pop("env", None)
        atomic_json(settings_path, settings)
    manifest_path.unlink(missing_ok=True)
