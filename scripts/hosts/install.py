#!/usr/bin/env python3
"""Transactional, manifest-owned installation for protocol v2 hosts.

The shell and PowerShell entry points are deliberately only runtime-discovery
wrappers.  All ownership, locking, and rollback decisions live here so the
two platforms cannot drift.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import tempfile
import subprocess
import signal
from pathlib import Path
from typing import Any

VERSION = 2


def digest(path: Path) -> str:
    if path.is_dir():
        entries = []
        for child in sorted(path.rglob("*")):
            if child.is_file():
                entries.append(str(child.relative_to(path)) + "\0" + digest(child))
        return hashlib.sha256("\n".join(entries).encode()).hexdigest()
    return hashlib.sha256(path.read_bytes()).hexdigest()


def same_link(path: Path, source: Path) -> bool:
    return path.is_symlink() and Path(os.readlink(path)) == source


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def acquire_lock(state: Path) -> Path:
    lock = state / "install.lock"
    try:
        lock.mkdir()
    except FileExistsError:
        try:
            pid = int((lock / "pid").read_text().strip())
            os.kill(pid, 0)
        except (FileNotFoundError, ValueError, ProcessLookupError):
            shutil.rmtree(lock, ignore_errors=True)
            lock.mkdir()
        except PermissionError:
            raise SystemExit(f"protocol installation is already active: {lock}")
        else:
            raise SystemExit(f"protocol installation is already active: {lock}")
    (lock / "pid").write_text(str(os.getpid()) + "\n", encoding="utf-8")
    return lock


def resources(repo: Path, home: Path, host: str) -> list[tuple[Path, Path, str]]:
    state = home / ".delegation-protocol"
    common = [
        # delegationctl and protocol-v2 are the only supported control plane.
        # The old mux-scheduler/catalog links are intentionally not migrated.
        (repo / "scripts/agents/delegationctl.py", state / "delegationctl.py", "link"),
        (repo / "agents/protocol-v2.json", state / "protocol-v2.json", "link"),
        (repo / "scripts/agents/delegation-classifier.py", state / "delegation-classifier.py", "link"),
        (repo / "scripts/hosts/hook_adapter.py", state / "hook_adapter.py", "link"),
        (repo / "scripts/hosts/lifecycle.py", state / "lifecycle.py", "link"),
    ]
    if host == "claude":
        return [
            (repo / "claude/rules/delegation-protocol.md", home / "rules/delegation-protocol.md", "link"),
            (repo / "claude/agents/bulk-worker.md", home / "agents/bulk-worker.md", "link"),
            (repo / "claude/hooks/delegation-enforcer.py", home / "hooks/delegation-enforcer.py", "link"),
            *common,
        ]
    return [
        (repo / "codex/AGENTS.md", home / "AGENTS.md", "link"),
        (repo / "codex/agents/bulk_worker.toml", home / "agents/bulk_worker.toml", "copy"),
        (repo / "codex/agents/balanced-worker.toml", home / "agents/balanced-worker.toml", "link"),
        (repo / "codex/hooks/delegation-enforcer.py", home / "hooks/delegation-enforcer.py", "link"),
        *common,
    ]


def validate_destination(source: Path, destination: Path, kind: str, owned: bool) -> None:
    if not destination.exists() and not destination.is_symlink():
        return
    if kind == "link" and same_link(destination, source):
        return
    if kind == "copy" and destination.is_file() and owned:
        return
    raise SystemExit(f"refusing to overwrite unowned destination: {destination}")


def prepare(repo: Path, home: Path, host: str, manifest: dict[str, Any] | None) -> list[tuple[Path, Path, str]]:
    state = home / ".delegation-protocol"
    directories = ((home / "rules" if host == "claude" else home), home / "agents", home / "hooks", state)
    for directory in directories:
        if directory.exists() and (not directory.is_dir() or directory.is_symlink()):
            raise SystemExit(f"unsafe protocol directory: {directory}")
    owned = set((manifest or {}).get("owned", []))
    result = resources(repo, home, host)
    for source, destination, kind in result:
        if not source.exists():
            raise SystemExit(f"missing protocol source: {source}")
        validate_destination(source, destination, kind, str(destination) in owned)
    return result


def install(repo: Path, home: Path, host: str) -> None:
    state = home / ".delegation-protocol"
    manifest_path = state / "manifest.json"
    previous = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else None
    if previous and previous.get("version") != VERSION:
        raise SystemExit("v1 installation detected; use the tagged v1 uninstaller before installing protocol v2")
    # Complete source/path preflight before creating any home or state entry.
    prepare(repo, home, host, previous)
    state.mkdir(parents=True, exist_ok=True)
    lock = acquire_lock(state)
    changed: list[tuple[Path, bytes | None, bool]] = []
    settings_path = home / ("settings.json" if host == "claude" else "hooks.json")
    prior_settings = settings_path.read_bytes() if settings_path.exists() else None
    try:
        for directory in ((home / "rules" if host == "claude" else home), home / "agents", home / "hooks", state):
            directory.mkdir(parents=True, exist_ok=True)
        items = prepare(repo, home, host, previous)
        for source, destination, kind in items:
            if kind == "link":
                if same_link(destination, source):
                    continue
                changed.append((destination, None, destination.exists() or destination.is_symlink()))
                destination.symlink_to(source, target_is_directory=source.is_dir())
            else:
                if destination.exists() and digest(destination) == digest(source):
                    continue
                prior = destination.read_bytes() if destination.exists() else None
                changed.append((destination, prior, prior is not None))
                shutil.copy2(source, destination)
        # Host settings are configured only after every filesystem destination
        # has passed ownership checks.  The host manager is itself idempotent;
        # a later failure removes newly-created links before returning.
        manager = (repo / "scripts/claude/manage-settings.py" if host == "claude"
                   else repo / "scripts/codex/manage-hooks.py")
        command = [sys.executable, os.fspath(manager), "install"]
        if host == "claude":
            command += ["--claude-home", os.fspath(home), "--hook-path",
                        os.fspath(home / "hooks/delegation-enforcer.py"), "--python", os.sys.executable]
        else:
            command += ["--codex-home", os.fspath(home), "--hook-path",
                        os.fspath(home / "hooks/delegation-enforcer.py"), "--python", os.sys.executable]
        if manager.exists():
            subprocess.run(command, check=True)
        manifest = {"version": VERSION, "host": host, "repo": str(repo),
                    "release": "automatic_release" if host == "claude" else "session_release",
                    "owned": [str(destination) for _, destination, _ in items],
                    "resources": [{"source": str(source), "destination": str(destination), "kind": kind}
                                  for source, destination, kind in items],
                    "hashes": {str(destination): digest(source) for source, destination, _ in items}}
        atomic_json(manifest_path, manifest)
    except Exception:
        for destination, prior, existed in reversed(changed):
            if destination.is_symlink() or destination.is_file():
                destination.unlink(missing_ok=True)
            if existed and prior is not None:
                destination.write_bytes(prior)
        if prior_settings is None:
            settings_path.unlink(missing_ok=True)
        else:
            settings_path.write_bytes(prior_settings)
        raise
    finally:
        shutil.rmtree(lock, ignore_errors=True)


def uninstall(home: Path, host: str) -> None:
    state = home / ".delegation-protocol"
    manifest_path = state / "manifest.json"
    if not manifest_path.exists():
        return
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("version") != VERSION or manifest.get("host") != host:
        raise SystemExit("unsupported or mismatched protocol manifest; refusing uninstall")
    lock = acquire_lock(state)
    try:
        repo = Path(manifest.get("repo", ""))
        manager = (repo / "scripts/claude/manage-settings.py" if host == "claude"
                   else repo / "scripts/codex/manage-hooks.py")
        if manager.exists():
            command = [sys.executable, os.fspath(manager), "uninstall"]
            if host == "claude":
                command += ["--claude-home", os.fspath(home), "--hook-path",
                            os.fspath(home / "hooks/delegation-enforcer.py"), "--python", sys.executable]
            else:
                command += ["--codex-home", os.fspath(home), "--hook-path",
                            os.fspath(home / "hooks/delegation-enforcer.py"), "--python", sys.executable]
            subprocess.run(command, check=True)
        for name in manifest.get("owned", []):
            path = Path(name)
            if path.is_symlink() or (path.is_file() and digest(path) == manifest.get("hashes", {}).get(name)):
                path.unlink(missing_ok=True)
        manifest_path.unlink(missing_ok=True)
    finally:
        shutil.rmtree(lock, ignore_errors=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("install", "uninstall"))
    parser.add_argument("--host", choices=("claude", "codex"), required=True)
    parser.add_argument("--home", required=True)
    parser.add_argument("--repo", required=True)
    args = parser.parse_args()
    home, repo = Path(args.home).expanduser().resolve(), Path(args.repo).resolve()
    if args.action == "install":
        install(repo, home, args.host)
    else:
        uninstall(home, args.host)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
