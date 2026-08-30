#!/usr/bin/env python3
"""Install/uninstall only the Claude Code settings owned by this protocol."""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agents"))
from hook_settings import atomic_write, handler, load_json, merge_hook_groups, quote, strip_owned_hooks

ENV_DEFAULTS = {
    "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS": "1",
    "CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH": "3",
    "CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS": "20",
}

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("action", choices=("install", "uninstall"))
    p.add_argument("--claude-home", required=True)
    p.add_argument("--hook-path", required=True)
    p.add_argument("--python", dest="python_exe", default=sys.executable)
    return p.parse_args()


def hook_groups(hook_path: Path, python_exe: str) -> dict[str, list[dict[str, Any]]]:
    base = f"{quote(str(python_exe))} {quote(str(hook_path))}"
    return {
        "UserPromptSubmit": [
            {"hooks": [handler(base + " prompt", "classify prompt")]}
        ],
        "SubagentStart": [
            {"hooks": [handler(base + " subagent-start", "track worker start")]}
        ],
        "SubagentStop": [
            {"hooks": [handler(base + " subagent-stop", "track worker stop")]}
        ],
        "PostToolUseFailure": [
            {"matcher": "Agent", "hooks": [handler(base + " agent-failure", "track Agent failure")]}
        ],
        "PreToolUse": [
            {
                "matcher": "Edit|Write|NotebookEdit|Bash|PowerShell",
                "hooks": [handler(base + " pretool", "enforce delegation")],
            }
        ],
        "Stop": [
            {"hooks": [handler(base + " stop", "verify delegation before stop")]}
        ],
    }


def install(settings_path: Path, state_dir: Path, hook_path: Path, python_exe: str) -> None:
    settings = load_json(settings_path)
    state_dir.mkdir(parents=True, exist_ok=True)

    backup = state_dir / "settings.before-first-install.json"
    if settings_path.exists() and not backup.exists():
        shutil.copy2(settings_path, backup)

    # Replace only prior protocol-owned handlers; preserve all unrelated hook groups.
    merge_hook_groups(settings, hook_groups(hook_path, python_exe))

    env = settings.setdefault("env", {})
    if not isinstance(env, dict):
        raise SystemExit("Refusing to replace existing non-object `env` setting")

    added_env: dict[str, str] = {}
    conflicts: dict[str, str] = {}
    for key, value in ENV_DEFAULTS.items():
        if key not in env:
            env[key] = value
            added_env[key] = value
        elif str(env[key]) != value:
            conflicts[key] = str(env[key])

    manifest = {
        "version": 1,
        "settings_path": str(settings_path),
        "hook_path": str(hook_path),
        "python": python_exe,
        "added_env": added_env,
        "conflicting_env_preserved": conflicts,
    }
    atomic_write(settings_path, settings)
    atomic_write(state_dir / "settings-manifest.json", manifest)

    if settings.get("disableAllHooks") is True:
        print("WARNING: disableAllHooks=true is already set; protocol hooks may not run.", file=sys.stderr)
    if conflicts:
        print("WARNING: preserved existing Claude env overrides instead of replacing them:", file=sys.stderr)
        for key, value in conflicts.items():
            print(f"  {key}={value}", file=sys.stderr)


def uninstall(settings_path: Path, state_dir: Path) -> None:
    if not settings_path.exists():
        return
    settings = load_json(settings_path)
    strip_owned_hooks(settings)

    manifest_path = state_dir / "settings-manifest.json"
    manifest = load_json(manifest_path) if manifest_path.exists() else {}
    added_env = manifest.get("added_env", {}) if isinstance(manifest, dict) else {}
    env = settings.get("env")
    if isinstance(env, dict) and isinstance(added_env, dict):
        for key, installed_value in added_env.items():
            if env.get(key) == installed_value:
                env.pop(key, None)
        if not env:
            settings.pop("env", None)

    atomic_write(settings_path, settings)
    if manifest_path.exists():
        manifest_path.unlink()


def main() -> int:
    args = parse_args()
    claude_home = Path(args.claude_home).expanduser().resolve()
    hook_path = Path(args.hook_path).expanduser().resolve()
    settings_path = claude_home / "settings.json"
    state_dir = claude_home / ".delegation-protocol"

    if args.action == "install":
        install(settings_path, state_dir, hook_path, args.python_exe)
    else:
        uninstall(settings_path, state_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
