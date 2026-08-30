#!/usr/bin/env python3
"""Install/uninstall only the Codex hooks owned by this protocol."""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agents"))
from hook_settings import atomic_write, handler, load_json, merge_hook_groups, quote, strip_owned_hooks


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("action", choices=("install", "uninstall"))
    p.add_argument("--codex-home", required=True)
    p.add_argument("--hook-path", required=True)
    p.add_argument("--python", dest="python_exe", default=sys.executable)
    return p.parse_args()


def groups(hook_path: Path, python_exe: str) -> dict[str, list[dict[str, Any]]]:
    base = f"{quote(str(python_exe))} {quote(str(hook_path))}"
    return {
        "UserPromptSubmit": [{"hooks": [handler(base + " prompt", "classify prompt")]}],
        "SubagentStart": [{"hooks": [handler(base + " subagent-start", "track worker start")]}],
        "SubagentStop": [{"hooks": [handler(base + " subagent-stop", "track worker stop")]}],
        "PreToolUse": [{"matcher": "*", "hooks": [handler(base + " pretool", "enforce delegation before mutation")]}],
        "PostToolUse": [{"matcher": "^Agent$", "hooks": [handler(base + " agent-result", "complete Agent attempt")]}],
        "Stop": [{"hooks": [handler(base + " stop", "verify delegation before stop")]}],
    }


def inspect_config(codex_home: Path) -> tuple[bool, bool]:
    """Return (hooks_disabled, agents_disabled) without modifying config.toml."""
    config = codex_home / "config.toml"
    if not config.exists():
        return False, False
    try:
        import tomllib
        data = tomllib.loads(config.read_text(encoding="utf-8"))
    except Exception:
        return False, False
    if not isinstance(data, dict):
        return False, False
    features = data.get("features", {})
    hooks_disabled = False
    if isinstance(features, dict):
        hooks_disabled = features.get("hooks") is False or (
            "hooks" not in features and features.get("codex_hooks") is False
        )
    agents = data.get("agents", {})
    agents_disabled = isinstance(agents, dict) and agents.get("enabled") is False
    return hooks_disabled, agents_disabled


def install(codex_home: Path, hooks_path: Path, state_dir: Path, hook_path: Path, python_exe: str) -> None:
    data = load_json(hooks_path)
    state_dir.mkdir(parents=True, exist_ok=True)
    backup = state_dir / "hooks.before-first-install.json"
    if hooks_path.exists() and not backup.exists():
        shutil.copy2(hooks_path, backup)

    merge_hook_groups(data, groups(hook_path, python_exe))

    atomic_write(hooks_path, data)
    atomic_write(state_dir / "hooks-manifest.json", {
        "version": 1,
        "hooks_path": str(hooks_path),
        "hook_path": str(hook_path),
        "python": python_exe,
    })

    hooks_disabled, agents_disabled = inspect_config(codex_home)
    if hooks_disabled:
        print("WARNING: Codex hooks are explicitly disabled in config.toml; mechanical protocol enforcement will not run until hooks are enabled.", file=sys.stderr)
    if agents_disabled:
        print("WARNING: Codex multi-agent tools are explicitly disabled in config.toml; delegation cannot run until agents.enabled is restored/enabled.", file=sys.stderr)
    print("IMPORTANT: Codex requires non-managed hooks to be reviewed/trusted. Start Codex and use /hooks to trust the Agent Delegation Protocol hook definition.", file=sys.stderr)


def uninstall(hooks_path: Path, state_dir: Path) -> None:
    if hooks_path.exists():
        data = load_json(hooks_path)
        strip_owned_hooks(data)
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
