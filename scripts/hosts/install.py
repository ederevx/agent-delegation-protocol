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
import ntpath
import os
import re
import shutil
import sys
import tempfile
import tomllib
from pathlib import Path
from typing import Any, Callable

try:
    from . import settings
    from .settings import HOST_SETTINGS_FILE
except ImportError:
    import settings
    from settings import HOST_SETTINGS_FILE

VERSION = 3

# Per-host runtime facts. The settings file name is None for a host with no
# hook-configuration JSON; its integration is deployed as plain resources.
# First entry may be empty, meaning the home root itself (Codex's AGENTS.md
# policy lives at the root; Claude's rules live under rules/).
HOST_DIRECTORIES = {
    "claude": ("rules", "agents", "hooks", ".delegation-protocol"),
    "codex": ("", "agents", "hooks", ".delegation-protocol"),
    # Pi enforces through a discovered extension, and its bridge lives in the
    # shared state directory: no hooks/ directory exists, so pi never emits
    # pi's "hooks/ directory found" deprecation warning.
    "pi": ("agents", "extensions", ".delegation-protocol"),
}

# Codex caps concurrently open spawned-agent threads per session through an
# `[agents]` table key (legacy alias `max_threads`, which we never write and
# never remove).  It is configured to the ordinary protocol capacity; the
# hook independently enforces the same active-worker limit.
CODEX_CONCURRENCY_TABLE = "agents"
CODEX_CONCURRENCY_KEY = "max_concurrent_threads_per_session"
CODEX_LEGACY_CONCURRENCY_KEY = "max_threads"
CODEX_CONFIG_BACKUP = "config.toml.before-first-install"
CODEX_OPEN_THREAD_CAPACITY = 10


def _strip_windows_extended_prefix(value: str) -> str:
    """Return the ordinary spelling of a Windows extended-length path."""
    if value[:8].casefold() == "\\\\?\\unc\\".casefold():
        return "\\\\" + value[8:]
    if value.startswith("\\\\?\\"):
        return value[4:]
    return value


def _windows_path(value: str) -> bool:
    return value.startswith("\\\\?\\") or bool(ntpath.splitdrive(value)[0])


def _normalized_link_path(value: str, parent: str) -> str:
    """Normalize link ownership paths lexically, without following aliases."""
    if os.name == "nt" or _windows_path(value) or _windows_path(parent):
        value = _strip_windows_extended_prefix(value)
        parent = _strip_windows_extended_prefix(parent)
        if not ntpath.isabs(value):
            value = ntpath.join(parent, value)
        return ntpath.normcase(ntpath.normpath(value))
    if not os.path.isabs(value):
        value = os.path.join(parent, value)
    return os.path.normcase(os.path.normpath(value))


def digest(path: Path) -> str:
    if path.is_dir():
        entries = []
        for child in sorted(path.rglob("*")):
            if child.is_file():
                entries.append(str(child.relative_to(path)) + "\0" + digest(child))
        return hashlib.sha256("\n".join(entries).encode()).hexdigest()
    return hashlib.sha256(path.read_bytes()).hexdigest()


def leaf_digest(path: Path) -> str | None:
    """Hash a leaf's identity without dereferencing a user symlink."""
    if path.is_symlink():
        return hashlib.sha256(
            b"link\0" + os.fsencode(os.readlink(path))
        ).hexdigest()
    if path.is_file():
        return digest(path)
    return None


def resource_digest(source: Path, kind: str) -> str:
    return digest(source)


def _mode(path: Path) -> int:
    return path.lstat().st_mode & 0o7777


def capture_path(path: Path) -> tuple[str, bytes | str | None, int | None]:
    """Capture a leaf without dereferencing a symlink for transaction rollback."""
    if path.is_symlink():
        return "link", os.readlink(path), _mode(path)
    if path.is_file():
        return "file", path.read_bytes(), _mode(path)
    if path.exists():
        raise SystemExit(f"unsafe protocol destination: {path}")
    return "missing", None, None


def restore_path(path: Path, saved: tuple[str, bytes | str | None, int | None]) -> None:
    """Restore a captured leaf exactly, replacing links rather than following them."""
    kind, value, mode = saved
    if _present(path):
        if path.is_dir() and not path.is_symlink():
            raise SystemExit(f"cannot roll back unsafe directory: {path}")
        path.unlink()
    if kind == "link":
        assert isinstance(value, str)
        path.symlink_to(value)
    elif kind == "file":
        assert isinstance(value, bytes)
        atomic_bytes(path, value)
        assert mode is not None
        os.chmod(path, mode)


def atomic_copy(source: Path, destination: Path) -> None:
    """Install a regular owned copy without ever opening a destination link."""
    atomic_bytes(destination, source.read_bytes())
    os.chmod(destination, _mode(source))


def same_link(path: Path, source: Path) -> bool:
    if not path.is_symlink():
        return False
    target = os.fspath(os.readlink(path))
    expected = os.fspath(source)
    parent = os.fspath(path.parent)
    return _normalized_link_path(target, parent) == _normalized_link_path(
        expected, os.getcwd()
    )


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
        (repo / "scripts/agents/delegation-classifier.py", state / "delegation-classifier.py", "copy"),
        (repo / "scripts/hosts/hook_adapter.py", state / "hook_adapter.py", "copy"),
    ]
    if host == "claude":
        return [
            (repo / "claude/rules/delegation-protocol.md", home / "rules/delegation-protocol.md", "copy"),
            (repo / "claude/agents/frontier-worker.md", home / "agents/frontier-worker.md", "copy"),
            (repo / "claude/agents/balanced-worker.md", home / "agents/balanced-worker.md", "copy"),
            (repo / "claude/agents/bulk-worker.md", home / "agents/bulk-worker.md", "copy"),
            (repo / "claude/agents/quick-worker.md", home / "agents/quick-worker.md", "copy"),
            (repo / "claude/hooks/delegation-enforcer.py", home / "hooks/delegation-enforcer.py", "copy"),
            *common,
        ]
    if host == "pi":
        return [
            (repo / "pi/rules/delegation-protocol.md", home / "rules/delegation-protocol.md", "copy"),
            (repo / "pi/agents/frontier-worker.md", home / "agents/frontier-worker.md", "copy"),
            (repo / "pi/agents/balanced-worker.md", home / "agents/balanced-worker.md", "copy"),
            (repo / "pi/agents/bulk-worker.md", home / "agents/bulk-worker.md", "copy"),
            (repo / "pi/agents/quick-worker.md", home / "agents/quick-worker.md", "copy"),
            (repo / "pi/.delegation-protocol/delegation-enforcer.py",
             state / "delegation-enforcer.py", "copy"),
            (repo / "pi/extensions/adp-subagent/index.ts",
             home / "extensions/adp-subagent/index.ts", "copy"),
            (repo / "pi/extensions/adp-subagent/agents.ts",
             home / "extensions/adp-subagent/agents.ts", "copy"),
                        (repo / "pi/extensions/adp-subagent/constants.ts",
             home / "extensions/adp-subagent/constants.ts", "copy"),
            (repo / "pi/extensions/adp-subagent/detail-view.ts",
             home / "extensions/adp-subagent/detail-view.ts", "copy"),
            (repo / "pi/extensions/adp-subagent/enforcer.ts",
             home / "extensions/adp-subagent/enforcer.ts", "copy"),
            (repo / "pi/extensions/adp-subagent/format.ts",
             home / "extensions/adp-subagent/format.ts", "copy"),
            (repo / "pi/extensions/adp-subagent/registry.ts",
             home / "extensions/adp-subagent/registry.ts", "copy"),
            (repo / "pi/extensions/adp-subagent/result-views.ts",
             home / "extensions/adp-subagent/result-views.ts", "copy"),
            (repo / "pi/extensions/adp-subagent/run.ts",
             home / "extensions/adp-subagent/run.ts", "copy"),
            (repo / "pi/extensions/adp-subagent/selector-view.ts",
             home / "extensions/adp-subagent/selector-view.ts", "copy"),
            (repo / "pi/extensions/adp-subagent/types.ts",
             home / "extensions/adp-subagent/types.ts", "copy"),
            (repo / "pi/extensions/adp-subagent/viewer-chrome.ts",
             home / "extensions/adp-subagent/viewer-chrome.ts", "copy"),
            (repo / "pi/extensions/adp-subagent/conservative-width.ts",
             home / "extensions/adp-subagent/conservative-width.ts", "copy"),
            (repo / "pi/extensions/adp-subagent/wheel-input.ts",
             home / "extensions/adp-subagent/wheel-input.ts", "copy"),
            *common,
        ]
    return [
        (repo / "codex/agents/frontier_worker.toml", home / "agents/frontier_worker.toml", "copy"),
        (repo / "codex/agents/balanced-worker.toml", home / "agents/balanced-worker.toml", "copy"),
        (repo / "codex/agents/bulk_worker.toml", home / "agents/bulk_worker.toml", "copy"),
        (repo / "codex/agents/quick_worker.toml", home / "agents/quick_worker.toml", "copy"),
        (repo / "codex/hooks/delegation-enforcer.py", home / "hooks/delegation-enforcer.py", "copy"),
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
            installed = previous.get("installed_digest")
            legacy_source = Path(previous.get("source", manifest.get("repo", ""))) / "codex/AGENTS.md"
            if not (agents.is_file() and not agents.is_symlink() and installed and
                    digest(agents) == installed) and not (
                        not installed and same_link(agents, legacy_source)):
                raise SystemExit(f"refusing modified Codex policy: {agents}")
        else:
            source_kind = previous.get("source")
            override_digest = previous.get("override_digest")
            owned_override = (override.is_file() and not override.is_symlink() and
                              override_digest and digest(override) == override_digest)
            if (source_kind not in {"agents", "override"} or
                    not (owned_override or (not override_digest and
                                            same_link(override, composed))) or
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
        prior = capture_path(agents)
        expected = digest(protocol)
        try:
            if not (agents.is_file() and not agents.is_symlink() and digest(agents) == expected):
                atomic_copy(protocol, agents)
        except BaseException:
            restore_path(agents, prior)
            raise

        def rollback_direct() -> None:
            restore_path(agents, prior)

        return {**policy, "installed_digest": expected, "source": str(repo)}, rollback_direct

    source_kind = policy["source"]
    prior = {path: capture_path(path) for path in (override, composed, backup, saved_override)}
    reinstall = composed.is_file() and backup.is_file()
    try:
        if not reinstall:
            active = override if source_kind == "override" else agents
            atomic_copy(active, backup)
        content = backup.read_bytes().rstrip(b"\n") + b"\n\n" + protocol.read_bytes()
        atomic_bytes(composed, content)
        if not reinstall and source_kind == "override":
            os.replace(override, saved_override)
        atomic_copy(composed, override)
    except BaseException:
        for path, saved in prior.items():
            restore_path(path, saved)
        raise

    def rollback_composed() -> None:
        for path, saved in prior.items():
            restore_path(path, saved)

    return {**policy, "composed_digest": digest(composed),
            "override_digest": digest(override), "backup_digest": digest(backup),
            "saved_override_digest": leaf_digest(saved_override)}, rollback_composed


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
        source = Path(policy.get("source", manifest["repo"])) / "codex/AGENTS.md"
        if (agents.is_file() and not agents.is_symlink() and
                digest(agents) == policy.get("installed_digest")) or (
                    not policy.get("installed_digest") and same_link(agents, source)):
            agents.unlink(missing_ok=True)
        return
    if (override.is_file() and not override.is_symlink() and
            digest(override) == policy.get("override_digest")) or (
                not policy.get("override_digest") and same_link(override, composed)):
        override.unlink(missing_ok=True)
    if policy.get("source") == "override" and _present(saved_override):
        os.replace(saved_override, override)
    if composed.is_file() and digest(composed) == policy.get("composed_digest"):
        composed.unlink(missing_ok=True)
    # A backup is installer-owned only after v3 recorded its regular-file hash.
    # Older records and any modified replacement remain user data.
    if (backup.is_file() and not backup.is_symlink() and
            digest(backup) == policy.get("backup_digest")):
        backup.unlink(missing_ok=True)


def _split_lines(text: str) -> list[str]:
    """Split on newlines only, keeping every byte (including CR) in place."""
    return re.findall(r"[^\n]*\n|[^\n]+", text)


def _newline(text: str) -> str:
    return "\r\n" if "\r\n" in text else "\n"


def _multiline_mask(lines: list[str]) -> list[bool]:
    """Mark each line that begins inside a multiline basic/literal string.

    A `[agents]`-looking line inside a triple-quoted basic or literal string is
    data, not a table header, so the scanner must not see it.
    """
    mask: list[bool] = []
    delimiter: str | None = None
    for line in lines:
        mask.append(delimiter is not None)
        index = 0
        while index < len(line):
            if delimiter is not None:
                if line.startswith(delimiter, index):
                    delimiter, index = None, index + 3
                elif delimiter == '"""' and line[index] == "\\":
                    index += 2
                else:
                    index += 1
                continue
            character = line[index]
            if character == "#":
                break
            if line.startswith('"""', index) or line.startswith("'''", index):
                delimiter, index = line[index:index + 3], index + 3
                continue
            if character in "\"'":
                index += 1
                while index < len(line):
                    if character == '"' and line[index] == "\\":
                        index += 2
                        continue
                    if line[index] == character:
                        index += 1
                        break
                    index += 1
                continue
            index += 1
    return mask


def _toml_table_header(line: str) -> bool:
    return line.lstrip().startswith("[")


def _toml_names_table(line: str, table: str) -> bool:
    text = line.split("#", 1)[0].strip()
    return text.replace(" ", "") == f"[{table}]"


def _toml_assignment(key: str) -> re.Pattern[str]:
    name = re.escape(key)
    return re.compile(rf"""^\s*(?:{name}|"{name}"|'{name}')\s*=""")


def _toml_scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    raise ValueError(f"unsupported scalar for {CODEX_CONCURRENCY_KEY}: {value!r}")


def _toml_table_span(
    lines: list[str], mask: list[bool], table: str,
) -> tuple[int, int] | None:
    """Return the header index and end index of `table`, or None if absent."""
    for index, line in enumerate(lines):
        if mask[index] or not _toml_table_header(line):
            continue
        if not _toml_names_table(line, table):
            continue
        end = len(lines)
        for following in range(index + 1, len(lines)):
            if not mask[following] and _toml_table_header(lines[following]):
                end = following
                break
        return index, end
    return None


def set_toml_table_key(
    text: str, table: str, key: str, assignment: str,
) -> tuple[str, str | None, bool]:
    """Set one key inside `table`, preserving every other byte verbatim.

    Returns the new text, the replaced assignment line (when one existed), and
    whether the table header had to be created.
    """
    newline = _newline(text)
    if not assignment.endswith("\n"):
        assignment += newline
    lines = _split_lines(text)
    mask = _multiline_mask(lines)
    span = _toml_table_span(lines, mask, table)
    if span is None:
        prefix = text
        if prefix and not prefix.endswith("\n"):
            prefix += newline
        if prefix.strip():
            prefix += newline
        return prefix + f"[{table}]{newline}" + assignment, None, True
    header, end = span
    if not lines[header].endswith("\n"):
        lines[header] += newline
    pattern = _toml_assignment(key)
    replaced: str | None = None
    result = lines[:header + 1]
    for index in range(header + 1, end):
        if replaced is None and not mask[index] and pattern.match(lines[index]):
            replaced = lines[index]
            result.append(assignment)
        else:
            result.append(lines[index])
    if replaced is None:
        result.insert(header + 1, assignment)
    result.extend(lines[end:])
    return "".join(result), replaced, False


def remove_toml_table_key(
    text: str, table: str, key: str, drop_created_table: bool,
) -> str:
    """Drop one key from `table`, undoing a table this installer appended.

    Only the bytes this installer could have added are removed: the assignment
    line, and -- when the table was created by this installer and is now empty
    -- its header plus the single blank separator line that preceded it.
    """
    lines = _split_lines(text)
    mask = _multiline_mask(lines)
    span = _toml_table_span(lines, mask, table)
    if span is None:
        return text
    header, end = span
    pattern = _toml_assignment(key)
    body = [
        line for index, line in enumerate(lines[header + 1:end], header + 1)
        if mask[index] or not pattern.match(line)
    ]
    empty = all(not line.strip() or line.lstrip().startswith("#") for line in body)
    if drop_created_table and empty:
        start = header
        if end >= len(lines) and start > 0 and not lines[start - 1].strip():
            start -= 1
        return "".join(lines[:start]) + "".join(body) + "".join(lines[end:])
    return "".join(lines[:header + 1]) + "".join(body) + "".join(lines[end:])


def _config_without_cap(text: str, key: str) -> dict[str, Any]:
    """Parse `text` and remove the managed key, so only the rest can differ."""
    data = tomllib.loads(text)
    table = data.get(CODEX_CONCURRENCY_TABLE)
    if isinstance(table, dict):
        table = {name: value for name, value in table.items() if name != key}
        if table:
            data[CODEX_CONCURRENCY_TABLE] = table
        else:
            data.pop(CODEX_CONCURRENCY_TABLE, None)
    return data


def verify_codex_config(config: Path, before: str, key: str, expected: Any) -> None:
    """Confirm the edited file differs from `before` only in the managed key.

    Line editing alone cannot prove it touched the right place -- a header
    spelled inside a multiline string looks identical to a real one -- so the
    parsed result is compared both ways.
    """
    text = config.read_bytes().decode("utf-8")
    data = tomllib.loads(text)
    table = data.get(CODEX_CONCURRENCY_TABLE)
    table = table if isinstance(table, dict) else {}
    actual = table.get(key)
    if actual != expected:
        raise ValueError(f"{key} read back as {actual!r}, expected {expected!r}")
    if _config_without_cap(text, key) != _config_without_cap(before, key):
        raise ValueError(
            f"the edit changed configuration outside {CODEX_CONCURRENCY_TABLE}.{key}"
        )


def _restore_codex_config(config: Path, prior: bytes | None) -> None:
    if prior is None:
        config.unlink(missing_ok=True)
    else:
        atomic_bytes(config, prior)


def _codex_config_path(home: Path) -> Path:
    config = home / "config.toml"
    if config.is_symlink():
        resolved = Path(os.path.realpath(config))
        if resolved.exists() and not resolved.is_file():
            raise SystemExit(f"unsafe Codex configuration path: {config}")
        return resolved
    if config.exists() and not config.is_file():
        raise SystemExit(f"unsafe Codex configuration path: {config}")
    return config


def install_codex_concurrency(
    home: Path, cap: int, manifest: dict[str, Any] | None,
) -> dict[str, Any]:
    """Set Codex's native open-thread capacity."""
    config = _codex_config_path(home)
    backup = home / ".delegation-protocol" / CODEX_CONFIG_BACKUP
    key = CODEX_CONCURRENCY_KEY
    prior = config.read_bytes() if config.is_file() else None
    if prior is None:
        text, data = "", {}
    else:
        try:
            text = prior.decode("utf-8")
            data = tomllib.loads(text)
        except (UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
            raise SystemExit(
                f"refusing to edit unparsable Codex configuration: {config}: {error}"
            ) from None
    table = data.get(CODEX_CONCURRENCY_TABLE)
    table = table if isinstance(table, dict) else {}
    previous = table.get(key)

    updated, replaced, table_created = set_toml_table_key(
        text, CODEX_CONCURRENCY_TABLE, key, f"{key} = {_toml_scalar(cap)}",
    )
    payload = updated.encode("utf-8")
    record = {"path": str(config), "key": key, "value": cap,
              "previous": previous, "previous_line": replaced,
              "table_created": table_created, "file_created": prior is None,
              "installed_digest": hashlib.sha256(payload).hexdigest()}
    owned = (manifest or {}).get("codex_config")
    inherited = (
        isinstance(owned, dict) and owned.get("path") == str(config) and
        owned.get("key") == key and previous is not None and
        previous == owned.get("value")
    )
    if inherited:
        # A re-install must keep the ownership metadata of the first install;
        # otherwise uninstall would "restore" the value this installer wrote.
        record["previous"] = owned.get("previous")
        record["previous_line"] = owned.get("previous_line")
        record["table_created"] = bool(owned.get("table_created"))
        record["file_created"] = bool(owned.get("file_created"))
        # The preserved backup is only a safe restore target while the file is
        # still exactly what the last install wrote.  Once the user has edited
        # it, drop the digest so uninstall removes just our key instead of
        # reinstating pre-install bytes over those edits.
        recorded = owned.get("installed_digest")
        if not (recorded and prior is not None and
                hashlib.sha256(prior).hexdigest() == recorded):
            record["installed_digest"] = None

    atomic_bytes(config, payload)
    try:
        verify_codex_config(config, text, key, cap)
    except (OSError, UnicodeDecodeError, ValueError,
            tomllib.TOMLDecodeError) as error:
        _restore_codex_config(config, prior)
        raise SystemExit(
            f"failed to set {CODEX_CONCURRENCY_TABLE}.{key} in {config}: {error}"
        ) from None
    if not inherited:
        # The pre-install bytes are the only exact record of what to restore.
        _restore_codex_config(backup, prior)
    return record


def uninstall_codex_concurrency(home: Path, manifest: dict[str, Any]) -> None:
    """Undo the concurrency pin, leaving a user-changed value untouched."""
    record = manifest.get("codex_config")
    if not isinstance(record, dict):
        return
    key, path = record.get("key"), record.get("path")
    if not key or not path:
        return
    config, backup = Path(path), home / ".delegation-protocol" / CODEX_CONFIG_BACKUP
    if not config.is_file():
        return
    prior = config.read_bytes()
    installed = record.get("installed_digest")
    if installed and hashlib.sha256(prior).hexdigest() == installed:
        # Untouched since installation: restore the exact pre-install bytes.
        if record.get("file_created"):
            config.unlink(missing_ok=True)
            return
        if backup.is_file():
            atomic_bytes(config, backup.read_bytes())
            return
    try:
        text = prior.decode("utf-8")
        data = tomllib.loads(text)
    except (UnicodeDecodeError, tomllib.TOMLDecodeError):
        return
    table = data.get(CODEX_CONCURRENCY_TABLE)
    table = table if isinstance(table, dict) else {}
    if table.get(key) != record.get("value"):
        return
    previous, previous_line = record.get("previous"), record.get("previous_line")
    if previous is None:
        updated = remove_toml_table_key(
            text, CODEX_CONCURRENCY_TABLE, key, bool(record.get("table_created")),
        )
    elif isinstance(previous_line, str):
        updated, _, _ = set_toml_table_key(
            text, CODEX_CONCURRENCY_TABLE, key, previous_line,
        )
    else:
        try:
            restored = f"{key} = {_toml_scalar(previous)}"
        except ValueError:
            return
        updated, _, _ = set_toml_table_key(
            text, CODEX_CONCURRENCY_TABLE, key, restored,
        )
    if record.get("file_created") and not updated.strip():
        config.unlink(missing_ok=True)
        return
    atomic_bytes(config, updated.encode("utf-8"))
    try:
        verify_codex_config(config, text, key, previous)
    except (OSError, UnicodeDecodeError, ValueError,
            tomllib.TOMLDecodeError) as error:
        _restore_codex_config(config, prior)
        raise SystemExit(
            f"failed to restore {CODEX_CONCURRENCY_TABLE}.{key} in {config}: {error}"
        ) from None


def validate_codex_uninstall(home: Path, manifest: dict[str, Any]) -> None:
    policy = manifest.get("policy")
    if not isinstance(policy, dict) or policy.get("mode") != "composed":
        return
    state = home / ".delegation-protocol"
    override = home / "AGENTS.override.md"
    composed = state / "AGENTS.composed.md"
    saved_override = state / "original-AGENTS.override.md"
    owned_override = (
        override.is_file() and not override.is_symlink() and
        digest(override) == policy.get("override_digest")
    )
    if not owned_override and not (
            not policy.get("override_digest") and same_link(override, composed)):
        raise SystemExit("refusing to overwrite a modified Codex override")
    if policy.get("source") == "override" and not _present(saved_override):
        raise SystemExit("refusing uninstall without the preserved Codex override")
    saved_digest = policy.get("saved_override_digest")
    if (policy.get("source") == "override" and saved_digest is not None and
            leaf_digest(saved_override) != saved_digest):
        raise SystemExit("refusing to overwrite a modified preserved Codex override")


def _legacy_resource_source(
    manifest: dict[str, Any] | None, destination: Path,
) -> tuple[Path, str] | None:
    """Find the recorded v3 source; never guess a foreign link is ours."""
    for item in (manifest or {}).get("resources", []):
        if isinstance(item, dict) and item.get("destination") == str(destination):
            source = item.get("source")
            kind = item.get("kind")
            if isinstance(source, str) and isinstance(kind, str):
                return Path(source), kind
    return None


def retire_owned_lifecycle(
    state: Path, manifest: dict[str, Any] | None,
    changed: list[tuple[Path, tuple[str, bytes | str | None, int | None]]],
) -> None:
    """Retire only the unchanged lifecycle copy owned by the prior manifest."""
    destination = state / "lifecycle.py"
    name = str(destination)
    if name not in set((manifest or {}).get("owned", [])):
        return
    resource = next((
        item for item in (manifest or {}).get("resources", [])
        if isinstance(item, dict) and item.get("destination") == name
    ), None)
    recorded = (manifest or {}).get("hashes", {}).get(name)
    if not (
        isinstance(resource, dict) and resource.get("kind") == "copy" and
        isinstance(recorded, str) and destination.is_file() and
        not destination.is_symlink() and digest(destination) == recorded
    ):
        return
    changed.append((destination, capture_path(destination)))
    destination.unlink()


def retire_orphaned_owned(
    home: Path, previous: dict[str, Any] | None,
    items: list[tuple[Path, Path, str]],
    changed: list[tuple[Path, tuple[str, bytes | str | None, int | None]]],
) -> None:
    """Unlink prior-manifest-owned copies the new resource set no longer has.

    When a resource disappears from `resources()`, a reinstall would otherwise
    strand its deployed copy forever: the new manifest no longer owns it, so
    no later uninstall can remove it. Remove only destinations the previous
    manifest owned as unchanged managed copies and that the new install does
    not write; modified or foreign files are left in place.
    """
    if not previous:
        return
    fresh = {str(destination) for _, destination, _ in items}
    recorded_hashes = previous.get("hashes", {})
    recorded_resources = {
        item.get("destination"): item
        for item in previous.get("resources", [])
        if isinstance(item, dict)
    }
    for name in previous.get("owned", []):
        if name in fresh:
            continue
        path = Path(name)
        resource = recorded_resources.get(name, {})
        if not (
            resource.get("kind") == "copy" and path.is_file() and
            not path.is_symlink() and
            digest(path) == recorded_hashes.get(name)
        ):
            continue
        changed.append((path, capture_path(path)))
        path.unlink()


def validate_destination(source: Path, destination: Path, kind: str, owned: bool,
                         recorded: str | None = None,
                         legacy: tuple[Path, str] | None = None) -> None:
    if not destination.exists() and not destination.is_symlink():
        return
    if (kind == "copy" and destination.is_file() and not destination.is_symlink()
            and owned and recorded and digest(destination) == recorded):
        return
    # v3 installed links.  They may be upgraded only when the manifest proves
    # the exact target; an arbitrary link is never an owned destination.
    if (kind == "copy" and owned and legacy is not None and legacy[1] == "link"
            and same_link(destination, legacy[0])):
        return
    raise SystemExit(f"refusing to overwrite unowned destination: {destination}")


SOURCE_MARKER_NAME = ".source-repo"


def write_source_markers(repo: Path, home: Path, host: str) -> list[Path]:
    """Record the absolute source-repository path next to every managed
    directory, so an agent inspecting an installed extension or hook can
    discover where its source lives without guessing. Idempotent: existing
    identical markers are left alone; newly written paths are returned so
    the manifest and rollback can manage them."""
    markers: list[Path] = []
    repo_line = str(repo) + "\n"
    for directory in _managed_directories(home, host):
        marker = directory / SOURCE_MARKER_NAME
        if not (marker.is_file() and marker.read_text(encoding="utf-8") == repo_line):
            marker.write_text(repo_line, encoding="utf-8")
            markers.append(marker)
    state = home / ".delegation-protocol"
    marker = state / SOURCE_MARKER_NAME
    if not (marker.is_file() and marker.read_text(encoding="utf-8") == repo_line):
        marker.write_text(repo_line, encoding="utf-8")
        markers.append(marker)
    return markers


def _managed_directories(home: Path, host: str) -> tuple[Path, ...]:
    try:
        parts = HOST_DIRECTORIES[host]
    except KeyError:
        raise SystemExit(f"no managed-directory layout for host: {host}") from None
    return tuple(home / part if part else home for part in parts)


def prepare(repo: Path, home: Path, host: str, manifest: dict[str, Any] | None) -> list[tuple[Path, Path, str]]:
    state = home / ".delegation-protocol"
    for directory in _managed_directories(home, host):
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
        validate_destination(
            source, destination, kind, str(destination) in owned,
            hashes.get(str(destination)),
            _legacy_resource_source(manifest, destination),
        )
    settings_file = HOST_SETTINGS_FILE.get(host)
    if settings_file is None:
        return result
    settings_path = home / settings_file
    settings.load_json(settings_path)
    return result


def install(repo: Path, home: Path, host: str) -> None:
    state = home / ".delegation-protocol"
    manifest_path = state / "manifest.json"
    previous = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else None
    if previous and previous.get("version") != VERSION:
        raise SystemExit("legacy scheduler-based installation detected; use the tagged v2 uninstaller before installing native-only protocol v3")
    # Complete source/path preflight before creating any home or state entry.
    prepare(repo, home, host, previous)
    state.mkdir(parents=True, exist_ok=True)
    lock = acquire_lock(state)
    changed: list[tuple[Path, tuple[str, bytes | str | None, int | None]]] = []
    rollback_policy: Callable[[], None] = lambda: None
    settings_file = HOST_SETTINGS_FILE.get(host)
    settings_path = home / settings_file if settings_file else None
    prior_settings = settings_path.read_bytes() if settings_path and settings_path.exists() else None
    settings_backup = state / f"{settings_file}.before-first-install" if settings_file else None
    prior_settings_backup = (
        settings_backup.read_bytes() if settings_backup and settings_backup.exists() else None
    )
    codex_config_path = _codex_config_path(home) if host == "codex" else None
    prior_codex_config = (
        codex_config_path.read_bytes()
        if codex_config_path is not None and codex_config_path.is_file()
        else None
    )
    codex_config_backup = state / CODEX_CONFIG_BACKUP
    prior_codex_config_backup = (
        codex_config_backup.read_bytes() if codex_config_backup.exists() else None
    )
    try:
        for directory in _managed_directories(home, host):
            directory.mkdir(parents=True, exist_ok=True)
        items = prepare(repo, home, host, previous)
        for source, destination, kind in items:
            expected_digest = resource_digest(source, kind)
            if (destination.is_file() and not destination.is_symlink() and
                    digest(destination) == expected_digest):
                continue
            changed.append((destination, capture_path(destination)))
            atomic_copy(source, destination)
        retire_owned_lifecycle(state, previous, changed)
        retire_orphaned_owned(home, previous, items, changed)
        for marker in write_source_markers(repo, home, host):
            changed.append((marker, capture_path(marker)))
        policy = None
        codex_config = None
        if host == "codex":
            policy, rollback_policy = install_codex_policy(
                repo, home, prepare_codex_policy(repo, home, previous)
            )
            codex_config = install_codex_concurrency(
                home, CODEX_OPEN_THREAD_CAPACITY, previous
            )
        if settings_path is not None:
            settings.install(
                host,
                home,
                home / "hooks/delegation-enforcer.py",
                sys.executable,
            )
        source_markers = write_source_markers(repo, home, host)
        manifest = {"version": VERSION, "host": host, "repo": str(repo),
                    "source_markers": [str(m) for m in source_markers],
                    "owned": [str(destination) for _, destination, _ in items],
                    "resources": [{"source": str(source), "destination": str(destination), "kind": kind}
                                  for source, destination, kind in items],
                    "hashes": {str(destination): resource_digest(source, kind)
                               for source, destination, kind in items}}
        if policy is not None:
            manifest["policy"] = policy
        if codex_config is not None:
            manifest["codex_config"] = codex_config
        atomic_json(manifest_path, manifest)
    except BaseException:
        rollback_policy()
        if codex_config_path is not None:
            _restore_codex_config(codex_config_path, prior_codex_config)
            _restore_codex_config(codex_config_backup, prior_codex_config_backup)
        for destination, prior in reversed(changed):
            restore_path(destination, prior)
        if settings_path is None:
            pass
        elif prior_settings is None:
            settings_path.unlink(missing_ok=True)
        else:
            settings_path.write_bytes(prior_settings)
        if settings_backup is None:
            pass
        elif prior_settings_backup is None:
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
        if host == "codex":
            # First, because a configuration this installer cannot undo safely
            # stops the uninstall before anything else has been removed.
            uninstall_codex_concurrency(home, manifest)
        if host not in HOST_SETTINGS_FILE:
            raise SystemExit(f"unsupported host in manifest: {host}")
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
            owned_copy = (
                path.is_file() and not path.is_symlink() and
                digest(path) == manifest.get("hashes", {}).get(name)
            )
            legacy_link = (resource.get("kind") == "link" and same_link(path, source))
            if legacy_link or owned_copy:
                path.unlink(missing_ok=True)
        for name in manifest.get("source_markers", []):
            Path(name).unlink(missing_ok=True)
        for directory in _managed_directories(home, host):
            (directory / SOURCE_MARKER_NAME).unlink(missing_ok=True)
        (state / SOURCE_MARKER_NAME).unlink(missing_ok=True)
        manifest_path.unlink(missing_ok=True)
        for backup in state.glob("*.before-first-install"):
            backup.unlink(missing_ok=True)
        shutil.rmtree(state / "hook-state", ignore_errors=True)
        shutil.rmtree(state / "__pycache__", ignore_errors=True)
    finally:
        shutil.rmtree(lock, ignore_errors=True)
    try:
        state.rmdir()
    except OSError:
        pass


def verify(home: Path, host: str, repo: Path) -> int:
    """Compare deployed managed copies against the current checkout.

    Manifest hashes record the deployed bytes, not the repo bytes, so they
    cannot detect staleness after the checkout advances. Drift here means the
    hook is enforcing an older protocol than the repo tests: rerun the
    installer to resync. Missing deployed resources are drift too.
    """
    state = home / ".delegation-protocol"
    manifest_path = state / "manifest.json"
    if not manifest_path.exists():
        print(f"drift: no protocol installation in {home}")
        return 1
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("version") != VERSION or manifest.get("host") != host:
        print("drift: unsupported or mismatched protocol manifest")
        return 1
    drifted = 0
    for item in manifest.get("resources", []):
        if not isinstance(item, dict) or item.get("kind") != "copy":
            continue
        destination = Path(item.get("destination", ""))
        source = Path(item.get("source", ""))
        if not destination.is_file() or destination.is_symlink():
            print(f"drift: missing deployed copy {destination}")
            drifted += 1
            continue
        if not source.is_file():
            print(f"drift: manifest source retired from the checkout {source}")
            drifted += 1
            continue
        if digest(destination) != digest(source):
            print(f"drift: deployed copy differs from checkout {destination}")
            drifted += 1
    if drifted:
        print(f"{drifted} drifted resource(s); run the installer to resync.")
        return 1
    print(f"verified: deployed {host} resources match the checkout.")
    return 0


def main() -> int:
    if sys.version_info < (3, 11):
        raise SystemExit("protocol v2 requires Python 3.11 or newer")
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("install", "uninstall", "verify"))
    parser.add_argument("--host", choices=tuple(HOST_SETTINGS_FILE), required=True)
    parser.add_argument("--home", required=True)
    parser.add_argument("--repo", required=True)
    args = parser.parse_args()
    home, repo = Path(args.home).expanduser().resolve(), Path(args.repo).resolve()
    if args.action == "install":
        install(repo, home, args.host)
    elif args.action == "verify":
        return verify(home, args.host, repo)
    else:
        uninstall(home, args.host)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
