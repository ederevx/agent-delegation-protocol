#!/usr/bin/env python3
"""Install/uninstall only the Codex hooks owned by this protocol."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any

STATUS_PREFIX = "Delegation protocol:"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("action", choices=("install", "uninstall"))
    p.add_argument("--codex-home", required=True)
    p.add_argument("--hook-path", required=True)
    p.add_argument("--python", dest="python_exe", default=sys.executable)
    return p.parse_args()


def quote(value: str) -> str:
    return '"' + value.replace('"', '\\"') + '"'


def load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Refusing to modify invalid JSON at {path}: {exc}")
    if not isinstance(data, dict):
        raise SystemExit(f"Refusing to modify non-object JSON at {path}")
    return data


def atomic_write(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".delegation-protocol.tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def handler(command: str, status: str) -> dict[str, Any]:
    return {
        "type": "command",
        "command": command,
        "timeout": 5,
        "statusMessage": f"{STATUS_PREFIX} {status}",
    }


def owned(value: Any) -> bool:
    return isinstance(value, dict) and str(value.get("statusMessage", "")).startswith(STATUS_PREFIX)


def groups(hook_path: Path, python_exe: str) -> dict[str, list[dict[str, Any]]]:
    base = f"{quote(str(python_exe))} {quote(str(hook_path))}"
    return {
        "UserPromptSubmit": [{"hooks": [handler(base + " prompt", "classify prompt")]}],
        "SubagentStart": [{"hooks": [handler(base + " subagent-start", "track worker start")]}],
        "SubagentStop": [{"hooks": [handler(base + " subagent-stop", "track worker stop")]}],
        "PreToolUse": [{"matcher": "*", "hooks": [handler(base + " pretool", "enforce delegation before mutation")]}],
        "PostToolUse": [{"matcher": "^Agent$", "hooks": [handler(base + " agent-result", "observe Agent result")]}],
        "Stop": [{"hooks": [handler(base + " stop", "verify delegation before stop")]}],
    }


def strip_owned(settings: dict[str, Any]) -> None:
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        return
    for event in list(hooks):
        event_groups = hooks.get(event)
        if not isinstance(event_groups, list):
            continue
        kept_groups: list[Any] = []
        for group in event_groups:
            if not isinstance(group, dict):
                kept_groups.append(group)
                continue
            handlers = group.get("hooks")
            if not isinstance(handlers, list):
                kept_groups.append(group)
                continue
            kept_handlers = [h for h in handlers if not owned(h)]
            if kept_handlers:
                new_group = dict(group)
                new_group["hooks"] = kept_handlers
                kept_groups.append(new_group)
        if kept_groups:
            hooks[event] = kept_groups
        else:
            hooks.pop(event, None)
    if not hooks:
        settings.pop("hooks", None)


def detect_disabled_hooks(codex_home: Path) -> bool:
    config = codex_home / "config.toml"
    if not config.exists():
        return False
    try:
        import tomllib
        data = tomllib.loads(config.read_text(encoding="utf-8"))
        features = data.get("features", {}) if isinstance(data, dict) else {}
        if isinstance(features, dict):
            if features.get("hooks") is False:
                return True
            if "hooks" not in features and features.get("codex_hooks") is False:
                return True
    except Exception:
        pass
    return False


def install(codex_home: Path, hooks_path: Path, state_dir: Path, hook_path: Path, python_exe: str) -> None:
    data = load_json(hooks_path)
    state_dir.mkdir(parents=True, exist_ok=True)
    backup = state_dir / "hooks.before-first-install.json"
    if hooks_path.exists() and not backup.exists():
        shutil.copy2(hooks_path, backup)

    strip_owned(data)
    hooks = data.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise SystemExit("Refusing to replace existing non-object `hooks` value")
    for event, new_groups in groups(hook_path, python_exe).items():
        current = hooks.setdefault(event, [])
        if not isinstance(current, list):
            raise SystemExit(f"Refusing to replace existing non-array hooks.{event}")
        current.extend(new_groups)

    atomic_write(hooks_path, data)
    atomic_write(state_dir / "hooks-manifest.json", {
        "version": 1,
        "hooks_path": str(hooks_path),
        "hook_path": str(hook_path),
        "python": python_exe,
    })

    if detect_disabled_hooks(codex_home):
        print("WARNING: Codex hooks appear explicitly disabled in config.toml; protocol hooks will not run until hooks are enabled.", file=sys.stderr)
    print("IMPORTANT: Codex requires non-managed hooks to be reviewed/trusted. Start Codex and use /hooks to trust the Agent Delegation Protocol hook definition.", file=sys.stderr)


def uninstall(hooks_path: Path, state_dir: Path) -> None:
    if hooks_path.exists():
        data = load_json(hooks_path)
        strip_owned(data)
        atomic_write(hooks_path, data)
    manifest = state_dir / "hooks-manifest.json"
    if manifest.exists():
        manifest.unlink()


def main() -> int:
    args = parse_args()
    codex_home = Path(args.codex_home).expanduser().resolve()
    hook_path = Path(args.hook_path).expanduser().resolve()
    hooks_path = codex_home / "hooks.json"
    state_dir = codex_home / ".delegation-protocol"
    if args.action == "install":
        install(codex_home, hooks_path, state_dir, hook_path, args.python_exe)
    else:
        uninstall(hooks_path, state_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
