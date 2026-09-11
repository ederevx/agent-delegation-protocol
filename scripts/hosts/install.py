#!/usr/bin/env python3
"""Transactional, manifest-owned installation for protocol v2 hosts.

The shell and PowerShell entry points are deliberately only runtime-discovery
wrappers.  All ownership, locking, and rollback decisions live here so the
two platforms cannot drift.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
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
except ImportError:
    import settings

VERSION = 3

# Codex caps concurrently open spawned-agent threads per session through an
# `[agents]` table key (legacy alias `max_threads`, which we never write and
# never remove).  The protocol pins it to the same shared active-worker cap the
# classifier advertises so one number governs both halves.
CODEX_CONCURRENCY_TABLE = "agents"
CODEX_CONCURRENCY_KEY = "max_concurrent_threads_per_session"
CODEX_LEGACY_CONCURRENCY_KEY = "max_threads"
CODEX_CONFIG_BACKUP = "config.toml.before-first-install"
DEFAULT_ACTIVE_WORKERS = 10


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


def resource_digest(source: Path, kind: str) -> str:
    return digest(source)


def same_link(path: Path, source: Path) -> bool:
    if not path.is_symlink():
        return False
    target = os.fspath(os.readlink(path))
    expected = os.fspath(source)
    parent = os.fspath(path.parent)
    return _normalized_link_path(target, parent) == _normalized_link_path(
        expected, os.getcwd()
    )


def create_symlink(destination: Path, source: Path) -> None:
    """Create a managed link with actionable Windows privilege guidance."""
    try:
        destination.symlink_to(source, target_is_directory=source.is_dir())
    except OSError as error:
        if getattr(error, "winerror", None) == 1314:
            raise SystemExit(
                f"cannot create required symbolic link: {destination}. "
                "Windows denied symbolic-link creation (WinError 1314). "
                "Enable Developer Mode or rerun this installer from an "
                "elevated PowerShell."
            ) from None
        raise


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
        (repo / "scripts/agents/delegation-classifier.py", state / "delegation-classifier.py", "link"),
        (repo / "scripts/hosts/hook_adapter.py", state / "hook_adapter.py", "link"),
        (repo / "scripts/hosts/lifecycle.py", state / "lifecycle.py", "link"),
    ]
    if host == "claude":
        return [
            (repo / "claude/rules/delegation-protocol.md", home / "rules/delegation-protocol.md", "link"),
            (repo / "claude/agents/frontier-worker.md", home / "agents/frontier-worker.md", "link"),
            (repo / "claude/agents/balanced-worker.md", home / "agents/balanced-worker.md", "link"),
            (repo / "claude/agents/bulk-worker.md", home / "agents/bulk-worker.md", "link"),
            (repo / "claude/agents/quick-worker.md", home / "agents/quick-worker.md", "link"),
            (repo / "claude/hooks/delegation-enforcer.py", home / "hooks/delegation-enforcer.py", "link"),
            *common,
        ]
    return [
        (repo / "codex/agents/frontier_worker.toml", home / "agents/frontier_worker.toml", "link"),
        (repo / "codex/agents/balanced-worker.toml", home / "agents/balanced-worker.toml", "link"),
        (repo / "codex/agents/bulk_worker.toml", home / "agents/bulk_worker.toml", "copy"),
        (repo / "codex/agents/quick_worker.toml", home / "agents/quick_worker.toml", "link"),
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
            create_symlink(agents, protocol)

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
            create_symlink(override, composed)
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


def active_worker_cap() -> int:
    """Return the shared active-worker cap published by the classifier.

    The classifier is the single source of the number; this loader stays
    defensive so a host installation never fails merely because the constant
    moved or the module could not be executed here.
    """
    path = Path(__file__).resolve().parents[1] / "agents" / "delegation-classifier.py"
    try:
        specification = importlib.util.spec_from_file_location(
            "protocol_active_worker_cap", path
        )
        if specification and specification.loader:
            module = importlib.util.module_from_spec(specification)
            specification.loader.exec_module(module)
            value = getattr(module, "MAX_ACTIVE_WORKERS", None)
            if isinstance(value, int) and not isinstance(value, bool) and value > 0:
                return value
    except Exception:
        pass
    return DEFAULT_ACTIVE_WORKERS


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
    """Pin Codex's per-session subagent concurrency to the protocol cap."""
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
        raise SystemExit("legacy scheduler-based installation detected; use the tagged v2 uninstaller before installing native-only protocol v3")
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
        for directory in ((home / "rules" if host == "claude" else home), home / "agents", home / "hooks", state):
            directory.mkdir(parents=True, exist_ok=True)
        items = prepare(repo, home, host, previous)
        for source, destination, kind in items:
            if kind == "link":
                if same_link(destination, source):
                    continue
                changed.append((destination, None, destination.exists() or destination.is_symlink()))
                create_symlink(destination, source)
            else:
                expected_digest = resource_digest(source, kind)
                if destination.exists() and digest(destination) == expected_digest:
                    continue
                prior = destination.read_bytes() if destination.exists() else None
                changed.append((destination, prior, prior is not None))
                shutil.copy2(source, destination)
        policy = None
        codex_config = None
        if host == "codex":
            policy, rollback_policy = install_codex_policy(
                repo, home, prepare_codex_policy(repo, home, previous)
            )
            codex_config = install_codex_concurrency(
                home, active_worker_cap(), previous
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
        if host == "codex":
            # First, because a configuration this installer cannot undo safely
            # stops the uninstall before anything else has been removed.
            uninstall_codex_concurrency(home, manifest)
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
        shutil.rmtree(state / "__pycache__", ignore_errors=True)
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
