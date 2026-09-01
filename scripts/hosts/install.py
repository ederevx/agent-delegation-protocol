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
from pathlib import Path
from typing import Any, Callable

try:
    from . import settings
except ImportError:
    import settings

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


def atomic_bytes(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(value)
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
        (repo / "scripts/agents/delegationctl.py", state / "delegationctl.py", "link"),
        (repo / "scripts/agents/lane_service.py", state / "lane_service.py", "link"),
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
        (repo / "codex/agents/bulk_worker.toml", home / "agents/bulk_worker.toml", "copy"),
        (repo / "codex/agents/balanced-worker.toml", home / "agents/balanced-worker.toml", "link"),
        (repo / "codex/hooks/delegation-enforcer.py", home / "hooks/delegation-enforcer.py", "link"),
        *common,
    ]


def _present(path: Path) -> bool:
    return path.exists() or path.is_symlink()


def prepare_codex_policy(
    repo: Path, home: Path, manifest: dict[str, Any] | None,
) -> dict[str, Any]:
    protocol = repo / "codex/AGENTS.md"
    agents = home / "AGENTS.md"
    override = home / "AGENTS.override.md"
    state = home / ".delegation-protocol"
    composed = state / "AGENTS.composed.md"
    backup = state / "original-active-global.md"
    saved_override = state / "original-AGENTS.override.md"
    if not protocol.is_file():
        raise SystemExit(f"missing protocol source: {protocol}")
    for path in (agents, override):
        if _present(path) and (not path.is_file() or (path.is_symlink() and not path.exists())):
            raise SystemExit(f"unsafe Codex instruction path: {path}")

    previous = (manifest or {}).get("policy")
    if previous is not None:
        if not isinstance(previous, dict) or previous.get("mode") not in {
                "direct", "composed"}:
            raise SystemExit("invalid Codex policy ownership metadata")
        if previous["mode"] == "direct":
            if not same_link(agents, protocol):
                raise SystemExit(f"refusing modified Codex policy: {agents}")
        else:
            source_kind = previous.get("source")
            if (source_kind not in {"agents", "override"} or
                    not same_link(override, composed) or
                    not composed.is_file() or not backup.is_file() or
                    (source_kind == "override") != _present(saved_override)):
                raise SystemExit("refusing incomplete composed Codex policy state")
        return previous

    if not _present(agents) and not _present(override):
        return {"mode": "direct"}
    for path in (composed, backup, saved_override):
        if _present(path):
            raise SystemExit(f"refusing unowned Codex policy state: {path}")
    return {"mode": "composed", "source": (
        "override" if _present(override) else "agents"
    )}


def install_codex_policy(
    repo: Path, home: Path, policy: dict[str, Any],
) -> tuple[dict[str, Any], Callable[[], None]]:
    protocol = repo / "codex/AGENTS.md"
    agents = home / "AGENTS.md"
    override = home / "AGENTS.override.md"
    state = home / ".delegation-protocol"
    composed = state / "AGENTS.composed.md"
    backup = state / "original-active-global.md"
    saved_override = state / "original-AGENTS.override.md"

    if policy["mode"] == "direct":
        created = not same_link(agents, protocol)
        if created:
            agents.symlink_to(protocol)

        def rollback_direct() -> None:
            if created and same_link(agents, protocol):
                agents.unlink(missing_ok=True)

        return policy, rollback_direct

    source_kind = policy["source"]
    reinstall = same_link(override, composed)
    prior_composed = composed.read_bytes() if reinstall else None
    moved_override = False
    try:
        if not reinstall:
            active = override if source_kind == "override" else agents
            atomic_bytes(backup, active.read_bytes())
        content = backup.read_bytes().rstrip(b"\n") + b"\n\n" + protocol.read_bytes()
        atomic_bytes(composed, content)
        if not reinstall and source_kind == "override":
            os.replace(override, saved_override)
            moved_override = True
        if not reinstall:
            override.symlink_to(composed)
    except BaseException:
        if same_link(override, composed):
            override.unlink(missing_ok=True)
        if moved_override and _present(saved_override):
            os.replace(saved_override, override)
        if prior_composed is None:
            composed.unlink(missing_ok=True)
            backup.unlink(missing_ok=True)
        else:
            atomic_bytes(composed, prior_composed)
        raise

    def rollback_composed() -> None:
        if reinstall:
            if prior_composed is not None:
                atomic_bytes(composed, prior_composed)
            return
        if same_link(override, composed):
            override.unlink(missing_ok=True)
        if moved_override and _present(saved_override):
            os.replace(saved_override, override)
        composed.unlink(missing_ok=True)
        backup.unlink(missing_ok=True)

    return policy, rollback_composed


def uninstall_codex_policy(home: Path, manifest: dict[str, Any]) -> None:
    policy = manifest.get("policy")
    if not isinstance(policy, dict):
        return
    state = home / ".delegation-protocol"
    agents = home / "AGENTS.md"
    override = home / "AGENTS.override.md"
    composed = state / "AGENTS.composed.md"
    backup = state / "original-active-global.md"
    saved_override = state / "original-AGENTS.override.md"
    if policy.get("mode") == "direct":
        source = Path(manifest["repo"]) / "codex/AGENTS.md"
        if same_link(agents, source):
            agents.unlink(missing_ok=True)
        return
    if same_link(override, composed):
        override.unlink(missing_ok=True)
    if policy.get("source") == "override" and _present(saved_override):
        os.replace(saved_override, override)
    composed.unlink(missing_ok=True)
    backup.unlink(missing_ok=True)


def validate_codex_uninstall(home: Path, manifest: dict[str, Any]) -> None:
    policy = manifest.get("policy")
    if not isinstance(policy, dict) or policy.get("mode") != "composed":
        return
    state = home / ".delegation-protocol"
    override = home / "AGENTS.override.md"
    composed = state / "AGENTS.composed.md"
    saved_override = state / "original-AGENTS.override.md"
    if not same_link(override, composed):
        raise SystemExit("refusing to overwrite a modified Codex override")
    if policy.get("source") == "override" and not _present(saved_override):
        raise SystemExit("refusing uninstall without the preserved Codex override")


def validate_destination(source: Path, destination: Path, kind: str, owned: bool, recorded: str | None = None) -> None:
    if not destination.exists() and not destination.is_symlink():
        return
    if kind == "link" and same_link(destination, source):
        return
    if kind == "copy" and destination.is_file() and owned and recorded and digest(destination) == recorded:
        return
    raise SystemExit(f"refusing to overwrite unowned destination: {destination}")


def prepare(repo: Path, home: Path, host: str, manifest: dict[str, Any] | None) -> list[tuple[Path, Path, str]]:
    state = home / ".delegation-protocol"
    directories = ((home / "rules" if host == "claude" else home), home / "agents", home / "hooks", state)
    for directory in directories:
        if directory.exists() and (not directory.is_dir() or directory.is_symlink()):
            raise SystemExit(f"unsafe protocol directory: {directory}")
    owned = set((manifest or {}).get("owned", []))
    hashes = (manifest or {}).get("hashes", {})
    result = resources(repo, home, host)
    if host == "codex":
        prepare_codex_policy(repo, home, manifest)
    for source, destination, kind in result:
        if not source.exists():
            raise SystemExit(f"missing protocol source: {source}")
        validate_destination(source, destination, kind, str(destination) in owned, hashes.get(str(destination)))
    settings.load_json(home / ("settings.json" if host == "claude" else "hooks.json"))
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
    rollback_policy: Callable[[], None] = lambda: None
    settings_path = home / ("settings.json" if host == "claude" else "hooks.json")
    prior_settings = settings_path.read_bytes() if settings_path.exists() else None
    settings_manifest = state / "host-settings.json"
    prior_settings_manifest = (
        settings_manifest.read_bytes() if settings_manifest.exists() else None
    )
    settings_backup = state / f"{settings_path.name}.before-first-install"
    prior_settings_backup = (
        settings_backup.read_bytes() if settings_backup.exists() else None
    )
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
        policy = None
        if host == "codex":
            policy, rollback_policy = install_codex_policy(
                repo, home, prepare_codex_policy(repo, home, previous)
            )
        settings.install(
            host,
            home,
            home / "hooks/delegation-enforcer.py",
            sys.executable,
        )
        manifest = {"version": VERSION, "host": host, "repo": str(repo),
                    "release": "automatic_release" if host == "claude" else "session_release",
                    "owned": [str(destination) for _, destination, _ in items],
                    "resources": [{"source": str(source), "destination": str(destination), "kind": kind}
                                  for source, destination, kind in items],
                    "hashes": {str(destination): digest(source) for source, destination, _ in items}}
        if policy is not None:
            manifest["policy"] = policy
        atomic_json(manifest_path, manifest)
    except BaseException:
        rollback_policy()
        for destination, prior, existed in reversed(changed):
            if destination.is_symlink() or destination.is_file():
                destination.unlink(missing_ok=True)
            if existed and prior is not None:
                destination.write_bytes(prior)
        if prior_settings is None:
            settings_path.unlink(missing_ok=True)
        else:
            settings_path.write_bytes(prior_settings)
        if prior_settings_manifest is None:
            settings_manifest.unlink(missing_ok=True)
        else:
            settings_manifest.write_bytes(prior_settings_manifest)
        if prior_settings_backup is None:
            settings_backup.unlink(missing_ok=True)
        else:
            settings_backup.write_bytes(prior_settings_backup)
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
    if host == "codex":
        validate_codex_uninstall(home, manifest)
    lock = acquire_lock(state)
    try:
        settings.uninstall(host, home)
        if host == "codex":
            uninstall_codex_policy(home, manifest)
        resources_by_destination = {
            item.get("destination"): item
            for item in manifest.get("resources", [])
            if isinstance(item, dict)
        }
        for name in manifest.get("owned", []):
            path = Path(name)
            resource = resources_by_destination.get(name, {})
            source = Path(resource.get("source", ""))
            owned_link = resource.get("kind") == "link" and same_link(path, source)
            owned_copy = (
                resource.get("kind") == "copy" and path.is_file() and
                digest(path) == manifest.get("hashes", {}).get(name)
            )
            if owned_link or owned_copy:
                path.unlink(missing_ok=True)
        manifest_path.unlink(missing_ok=True)
        for backup in state.glob("*.before-first-install"):
            backup.unlink(missing_ok=True)
        shutil.rmtree(state / "hook-state", ignore_errors=True)
    finally:
        shutil.rmtree(lock, ignore_errors=True)
    try:
        state.rmdir()
    except OSError:
        pass


def main() -> int:
    if sys.version_info < (3, 11):
        raise SystemExit("protocol v2 requires Python 3.11 or newer")
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
