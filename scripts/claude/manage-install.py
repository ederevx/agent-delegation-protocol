#!/usr/bin/env python3
"""Cross-platform installer engine for the Claude delegation protocol."""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("install", "uninstall"))
    parser.add_argument("--claude-home", required=True)
    parser.add_argument("--python", dest="python_exe", default=sys.executable)
    return parser.parse_args()


def same_link(path: Path, source: Path) -> bool:
    if not path.is_symlink():
        return False
    try:
        return Path(os.readlink(path)) == source
    except OSError:
        return False


def safe_link(source: Path, destination: Path) -> None:
    if same_link(destination, source):
        return
    if destination.exists() or destination.is_symlink():
        raise SystemExit(f"Refusing to overwrite existing path: {destination}")
    destination.symlink_to(source, target_is_directory=source.is_dir())


def remove_if_ours(destination: Path, source: Path) -> None:
    if same_link(destination, source):
        destination.unlink()


def paths(repo: Path, home: Path) -> list[tuple[Path, Path]]:
    worker = repo / "claude" / "agents" / "bulk-worker.md"
    state = home / ".delegation-protocol"
    return [
        (repo / "claude/rules/delegation-protocol.md", home / "rules/delegation-protocol.md"),
        (worker, home / "agents/bulk-worker.md"),
        (repo / "claude/hooks/delegation-enforcer.py", home / "hooks/delegation-enforcer.py"),
        (repo / "scripts/agents/mux-scheduler.py", state / "mux-scheduler.py"),
        (repo / "scripts/agents/delegation-classifier.py", state / "delegation-classifier.py"),
        (repo / "scripts/agents/delegation_queue.py", state / "delegation_queue.py"),
        (repo / "agents/catalog", state / "catalog"),
        (repo / "agents/mux-scheduler.json", state / "mux-scheduler.json"),
    ]


def manage_settings(repo: Path, home: Path, python_exe: str, action: str) -> None:
    subprocess.run(
        [python_exe, str(repo / "scripts/claude/manage-settings.py"), action,
         "--claude-home", str(home), "--hook-path",
         str(home / "hooks/delegation-enforcer.py"), "--python", python_exe],
        check=True,
    )


def install(repo: Path, home: Path, python_exe: str) -> None:
    subprocess.run(
        [python_exe, str(repo / "scripts/agents/render-bulk-workers.py"), "--check"],
        check=True,
    )
    for directory in ("rules", "agents", "hooks", ".delegation-protocol"):
        (home / directory).mkdir(parents=True, exist_ok=True)
    shutil.rmtree(home / ".delegation-protocol/__pycache__", ignore_errors=True)
    for source, destination in paths(repo, home):
        safe_link(source, destination)
    remove_if_ours(home / ".delegation-protocol/multiplexer.py",
                   repo / "scripts/agents/multiplexer.py")
    remove_if_ours(home / ".delegation-protocol/multiplexer.json",
                   repo / "agents/multiplexer.json")
    manage_settings(repo, home, python_exe, "install")
    print("Installed Claude delegation protocol only: hooks, settings, rule, "
          "bulk-worker, and agent mux-scheduler. Restart Claude Code sessions.")


def uninstall(repo: Path, home: Path, python_exe: str) -> None:
    manage_settings(repo, home, python_exe, "uninstall")
    owned = paths(repo, home)
    owned.extend([
        (repo / "scripts/agents/multiplexer.py", home / ".delegation-protocol/multiplexer.py"),
        (repo / "agents/multiplexer.json", home / ".delegation-protocol/multiplexer.json"),
    ])
    for source, destination in owned:
        remove_if_ours(destination, source)
    state = home / ".delegation-protocol"
    for filename in ("settings.before-first-install.json", "settings-manifest.json"):
        (state / filename).unlink(missing_ok=True)
    shutil.rmtree(state / "sessions", ignore_errors=True)
    try:
        state.rmdir()
    except OSError:
        pass
    print("Uninstalled Claude delegation protocol only; unrelated Claude "
          "settings and hooks were preserved.")


def main() -> int:
    args = parse_args()
    repo = Path(__file__).resolve().parents[2]
    home = Path(args.claude_home).expanduser().resolve()
    if args.action == "install":
        install(repo, home, args.python_exe)
    else:
        uninstall(repo, home, args.python_exe)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
