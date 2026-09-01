#!/usr/bin/env python3
"""Provider-neutral, resumable execution engine for protocol-v2 tasks."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import signal
import stat
import subprocess
import tempfile
import threading
import time
import uuid
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Callable

from permission_service import PermissionError, PermissionStore, validate_request

MAX_CAPTURE_BYTES = 1024 * 1024
MAX_STREAM_BYTES = 64 * 1024
DEFAULT_STATE_TTL_SECONDS = 24 * 60 * 60
Runner = Callable[[dict[str, Any], Path, dict[str, Any]], dict[str, Any]]


class ExecutionError(ValueError):
    """A malformed task, session transition, or unsafe workspace."""


def receipt(status: str, classification: str, **fields: Any) -> dict[str, Any]:
    return {"schema_version": 2, "status": status,
            "classification": classification, **fields}


def _clip(value: str, limit: int) -> tuple[str, bool]:
    raw = value.encode("utf-8", "replace")
    if len(raw) <= limit:
        return value, False
    return raw[:limit].decode("utf-8", "replace"), True


def _relative_path(value: Any) -> str:
    if not isinstance(value, str) or not value or "\0" in value:
        raise ExecutionError("allowed_paths entries must be non-empty strings")
    posix = PurePosixPath(value)
    windows = PureWindowsPath(value)
    parts = (*posix.parts, *windows.parts)
    if (posix.anchor or windows.anchor or windows.drive or ":" in value
            or ".." in parts
            or any(part.rstrip(" .").casefold() == ".git" for part in parts)):
        raise ExecutionError(f"unsafe allowed path: {value!r}")
    return PurePosixPath(value.replace("\\", "/")).as_posix().rstrip("/") or "."


def validate_task(value: Any) -> dict[str, Any]:
    required = {
        "schema_version", "id", "mode", "repo", "prompt", "allowed_paths",
        "workspace", "validation", "budgets",
    }
    if not isinstance(value, dict) or set(value) != required or value.get("schema_version") != 2:
        raise ExecutionError("task must contain the exact protocol-v2 fields")
    task = dict(value)
    if task.get("mode") not in {"read", "edit"}:
        raise ExecutionError("task.mode must be read or edit")
    if task.get("workspace") not in {"shared", "isolated"}:
        raise ExecutionError("task.workspace must be shared or isolated")
    for field in ("id", "repo", "prompt"):
        item = task.get(field)
        if (not isinstance(item, str) or not item.strip() or "\0" in item
                or (field == "id" and ("\n" in item or len(item) > 128))):
            raise ExecutionError(f"task.{field} is invalid")
    if len(task["prompt"].encode("utf-8")) > 48 * 1024:
        raise ExecutionError("task.prompt exceeds 49152 bytes")
    repo = Path(task["repo"])
    if not repo.is_absolute():
        raise ExecutionError("task.repo must be absolute")
    task["repo"] = str(repo.resolve())
    allowed = task.get("allowed_paths")
    if not isinstance(allowed, list) or len(allowed) > 128:
        raise ExecutionError("task.allowed_paths is invalid")
    task["allowed_paths"] = [_relative_path(item) for item in allowed]
    validation = task.get("validation")
    if not isinstance(validation, list) or len(validation) > 16:
        raise ExecutionError("task.validation is invalid")
    for command in validation:
        if (not isinstance(command, list) or not command or len(command) > 32
                or any(not isinstance(part, str) or not part or "\0" in part
                       or len(part) > 4096 for part in command)):
            raise ExecutionError("task.validation must contain bounded argv arrays")
        if Path(command[0]).name.casefold() == "git" and "push" in command[1:]:
            raise ExecutionError("git push validation is forbidden")
    if task["mode"] == "read" and validation:
        raise ExecutionError("read tasks cannot declare validation commands")
    budgets = task.get("budgets")
    maxima = {"timeout_seconds": 7200, "max_output_bytes": 8 * 1024 * 1024,
              "max_steps": 1000}
    if not isinstance(budgets, dict) or set(budgets) != set(maxima):
        raise ExecutionError("task.budgets fields are invalid")
    for name, maximum in maxima.items():
        amount = budgets.get(name)
        if (not isinstance(amount, int) or isinstance(amount, bool)
                or not 1 <= amount <= maximum):
            raise ExecutionError(f"task.budgets.{name} is out of range")
    return task


def _process_options() -> dict[str, Any]:
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def _terminate_process_tree(process: subprocess.Popen[str], force: bool = False) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        if not force:
            try:
                process.send_signal(signal.CTRL_BREAK_EVENT)
                return
            except (OSError, ValueError):
                pass
        process.kill()
        return
    try:
        os.killpg(process.pid, signal.SIGKILL if force else signal.SIGTERM)
    except ProcessLookupError:
        pass


def run_owned_process(argv: list[str], cwd: Path, timeout_seconds: float,
                      *, cancel: threading.Event | None = None,
                      env: dict[str, str] | None = None) -> dict[str, Any]:
    """Run an argv without a shell and terminate its owned process group."""
    started = time.monotonic()
    process = subprocess.Popen(argv, cwd=cwd, stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True, env=env, **_process_options())
    timed_out = cancelled = False
    try:
        while True:
            remaining = timeout_seconds - (time.monotonic() - started)
            if remaining <= 0 or (cancel is not None and cancel.is_set()):
                timed_out = remaining <= 0
                cancelled = not timed_out
                _terminate_process_tree(process)
                try:
                    stdout, stderr = process.communicate(timeout=0.5)
                except subprocess.TimeoutExpired:
                    _terminate_process_tree(process, force=True)
                    stdout, stderr = process.communicate()
                break
            try:
                stdout, stderr = process.communicate(timeout=min(remaining, 0.1))
                break
            except subprocess.TimeoutExpired:
                continue
    except BaseException:
        _terminate_process_tree(process)
        try:
            process.communicate(timeout=0.5)
        except subprocess.TimeoutExpired:
            _terminate_process_tree(process, force=True)
            process.communicate()
        raise
    stdout, stdout_cut = _clip(stdout, MAX_STREAM_BYTES)
    stderr, stderr_cut = _clip(stderr, MAX_STREAM_BYTES)
    return {
        "argv": argv, "returncode": 124 if timed_out else process.returncode,
        "timed_out": timed_out, "cancelled": cancelled,
        "duration_seconds": round(time.monotonic() - started, 3),
        "stdout": stdout, "stdout_truncated": stdout_cut,
        "stderr": stderr, "stderr_truncated": stderr_cut,
    }


def _git(repo: Path, *arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(["git", "-C", str(repo), *arguments], text=True,
                            capture_output=True, check=False)
    if check and result.returncode:
        raise ExecutionError(f"git {' '.join(arguments)} failed: {result.stderr.strip()}")
    return result


def filesystem_snapshot(root: Path) -> dict[str, tuple[Any, ...]]:
    """Hash workspace contents without following links or entering .git."""
    snapshot: dict[str, tuple[Any, ...]] = {}

    def visit(directory: Path, prefix: str = "") -> None:
        try:
            entries = sorted(os.scandir(directory), key=lambda item: item.name)
        except OSError as error:
            raise ExecutionError(f"could not inspect {directory}: {error}") from error
        for entry in entries:
            relative = f"{prefix}/{entry.name}" if prefix else entry.name
            if relative == ".git":
                continue
            item_stat = entry.stat(follow_symlinks=False)
            mode = stat.S_IMODE(item_stat.st_mode)
            if stat.S_ISLNK(item_stat.st_mode):
                snapshot[relative] = ("symlink", mode, os.readlink(entry.path))
            elif stat.S_ISDIR(item_stat.st_mode):
                snapshot[relative] = ("directory", mode)
                visit(Path(entry.path), relative)
            elif stat.S_ISREG(item_stat.st_mode):
                digest = hashlib.sha256()
                descriptor = os.open(entry.path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
                try:
                    with os.fdopen(descriptor, "rb", closefd=False) as handle:
                        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                            digest.update(chunk)
                finally:
                    os.close(descriptor)
                snapshot[relative] = ("file", mode, item_stat.st_size, digest.hexdigest())
            else:
                snapshot[relative] = ("special", stat.S_IFMT(item_stat.st_mode), mode)

    visit(root)
    return snapshot


def snapshot_changed_paths(before: dict[str, tuple[Any, ...]],
                           after: dict[str, tuple[Any, ...]]) -> list[str]:
    return sorted(path for path in before.keys() | after.keys()
                  if before.get(path) != after.get(path))


def unsafe_symlink_paths(root: Path, snapshot: dict[str, tuple[Any, ...]]) -> list[str]:
    unsafe = []
    for relative, description in snapshot.items():
        if description[0] != "symlink":
            continue
        try:
            (root / relative).resolve(strict=False).relative_to(root)
        except (OSError, RuntimeError, ValueError):
            unsafe.append(relative)
    return sorted(unsafe)


def _path_allowed(path: str, allowed: list[str]) -> bool:
    return not allowed or any(root == "." or path == root or path.startswith(root + "/")
                              for root in allowed)


def _evidence(repo: Path, task_changes: list[str]) -> dict[str, Any]:
    status, status_cut = _clip(
        _git(repo, "status", "--short", "--untracked-files=all").stdout,
        MAX_STREAM_BYTES,
    )
    patch_parts = [_git(repo, "diff", "--no-ext-diff", "--binary", "HEAD").stdout]
    untracked = _git(repo, "ls-files", "--others", "--exclude-standard", "-z")
    for relative in untracked.stdout.split("\0"):
        if not relative:
            continue
        item = _git(repo, "diff", "--no-index", "--binary", "--", os.devnull,
                    relative, check=False)
        if item.returncode not in {0, 1}:
            raise ExecutionError(f"could not capture evidence for {relative!r}")
        patch_parts.append(item.stdout)
    patch, patch_cut = _clip("".join(patch_parts), MAX_CAPTURE_BYTES)
    return {"changed_paths": task_changes, "status": status,
            "status_truncated": status_cut, "patch": patch,
            "diff_truncated": patch_cut}


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


class ExecutionEngine:
    """Own workspaces and lifecycle while delegating model work to ``runner``."""

    def __init__(self, state_root: Path, runner: Runner, *,
                 state_ttl_seconds: int = DEFAULT_STATE_TTL_SECONDS) -> None:
        if state_ttl_seconds < 1:
            raise ExecutionError("state_ttl_seconds must be positive")
        self.root = state_root
        self.runner = runner
        self.ttl = state_ttl_seconds
        self.root.mkdir(parents=True, mode=0o700, exist_ok=True)

    def _path(self, token: str) -> Path:
        try:
            parsed = uuid.UUID(token)
        except (ValueError, AttributeError) as error:
            raise ExecutionError("invalid continuation token") from error
        if str(parsed) != token:
            raise ExecutionError("invalid continuation token")
        return self.root / token

    def _read(self, token: str) -> tuple[Path, dict[str, Any]]:
        path = self._path(token)
        try:
            state = json.loads((path / "state.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ExecutionError("unknown or corrupt continuation token") from error
        if not isinstance(state, dict) or state.get("token") != token:
            raise ExecutionError("session state is corrupt")
        return path, state

    def _save(self, path: Path, state: dict[str, Any]) -> None:
        state["last_activity"] = time.time()
        state["expires_at"] = state["last_activity"] + self.ttl
        _atomic_json(path / "state.json", state)

    @staticmethod
    def _process_start_token(pid: int) -> str | None:
        if os.name == "nt":
            return None
        try:
            fields = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").split()
        except OSError:
            return None
        return fields[21] if len(fields) > 21 else None

    @classmethod
    def _owner_is_dead(cls, owner: Any) -> bool:
        if not isinstance(owner, dict) or not isinstance(owner.get("pid"), int):
            return False
        pid = owner["pid"]
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        except (OSError, PermissionError):
            return False
        expected = owner.get("start_token")
        actual = cls._process_start_token(pid)
        return expected is not None and actual is not None and expected != actual

    def _lock(self, path: Path) -> int | None:
        lock = path / "active.lock"
        for _attempt in range(2):
            try:
                descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                try:
                    owner = json.loads(lock.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    return None
                if not self._owner_is_dead(owner):
                    return None
                try:
                    lock.unlink()
                except OSError:
                    return None
                continue
            owner = {"pid": os.getpid(),
                     "start_token": self._process_start_token(os.getpid())}
            os.write(descriptor, json.dumps(owner, sort_keys=True).encode("utf-8"))
            os.fsync(descriptor)
            return descriptor
        return None

    def _unlock(self, path: Path, descriptor: int) -> None:
        os.close(descriptor)
        (path / "active.lock").unlink(missing_ok=True)

    def start(self, task_value: Any) -> dict[str, Any]:
        self.gc()
        task = validate_task(task_value)
        repo = Path(task["repo"])
        if (_git(repo, "rev-parse", "--is-inside-work-tree", check=False).stdout.strip()
                != "true"):
            raise ExecutionError("task.repo is not a git working tree")
        top = Path(_git(repo, "rev-parse", "--show-toplevel").stdout.strip()).resolve()
        revision = _git(top, "rev-parse", "HEAD").stdout.strip()
        token = str(uuid.uuid4())
        path = self._path(token)
        path.mkdir(mode=0o700)
        workspace = top
        isolated = task["workspace"] == "isolated"
        try:
            if isolated:
                workspace = path / "worktree"
                _git(top, "worktree", "add", "--detach", str(workspace), revision)
            before = filesystem_snapshot(workspace)
            unsafe = unsafe_symlink_paths(workspace, before)
            if unsafe:
                self._cleanup(path, {
                    "top": str(top),
                    "worktree": str(workspace) if isolated else None,
                })
                return receipt("failed", "unsafe_symlink", task_id=task["id"],
                               unsafe_symlinks=unsafe)
            state = {
                "schema_version": 2, "token": token, "task": task,
                "top": str(top), "revision": revision, "cwd": str(workspace),
                "worktree": str(workspace) if isolated else None,
                "before": {key: list(item) for key, item in before.items()},
                "deadline": time.time() + task["budgets"]["timeout_seconds"],
                "steps_used": 0, "slices": [], "continuation": None,
            }
            PermissionStore(path / "permissions.json", token)
            self._save(path, state)
            return receipt("ready", "session_ready", token=token, task_id=task["id"])
        except BaseException:
            if path.exists():
                self._cleanup(path, {"top": str(top),
                                     "worktree": str(workspace) if isolated else None})
            raise

    def step(self, token: str) -> dict[str, Any]:
        path, state = self._read(token)
        descriptor = self._lock(path)
        if descriptor is None:
            return receipt("yielded", "task_busy", token=token,
                           retry_after_seconds=0.1)
        try:
            permissions = PermissionStore(path / "permissions.json", token)
            pending = permissions.pending()
            if pending is not None:
                return receipt("permission_required", "parent_decision_required",
                               token=token, request=pending)
            task = state["task"]
            if time.time() >= state["deadline"]:
                return self._finish(path, state, {"classification": "timeout"}, "timeout")
            if state["steps_used"] >= task["budgets"]["max_steps"]:
                return self._finish(path, state, {"classification": "step_budget_exhausted"},
                                    "step_budget_exhausted")
            context = {
                "token": token, "step": state["steps_used"],
                "remaining_seconds": max(0.0, state["deadline"] - time.time()),
                "remaining_steps": task["budgets"]["max_steps"] - state["steps_used"],
                "continuation": state.pop("continuation", None),
                "run_process": run_owned_process,
                "permissions": permissions,
            }
            outcome = self.runner(task, Path(state["cwd"]), context)
            if not isinstance(outcome, dict):
                raise ExecutionError("runner must return an object")
            encoded = json.dumps(outcome, sort_keys=True, ensure_ascii=False)
            if len(encoded.encode("utf-8")) > task["budgets"]["max_output_bytes"]:
                return self._finish(path, state, {"classification": "output_budget_exhausted"},
                                    "output_budget_exhausted")
            used = outcome.get("steps_used", 1)
            if not isinstance(used, int) or isinstance(used, bool) or used < 1:
                raise ExecutionError("runner steps_used must be a positive integer")
            state["steps_used"] += min(used, context["remaining_steps"])
            state["slices"].append({"index": len(state["slices"]),
                                    "steps": min(used, context["remaining_steps"]),
                                    "classification": outcome.get("classification", "success")})
            request = outcome.get("permission_request")
            if request is not None:
                request = validate_request(request)
                if request["session_id"] != token:
                    raise ExecutionError("runner permission request session_id does not match")
                permissions.issue(request)
                self._save(path, state)
                return receipt("permission_required", "parent_decision_required",
                               token=token, request=request,
                               steps_used=state["steps_used"])
            if outcome.get("completed", True) is False:
                self._save(path, state)
                return receipt("yielded", outcome.get("classification", "session_yielded"),
                               token=token, steps_used=state["steps_used"])
            return self._finish(path, state, outcome,
                                outcome.get("classification", "success"))
        finally:
            if path.exists():
                self._unlock(path, descriptor)
            else:
                os.close(descriptor)

    def resume(self, token: str, resolution: dict[str, Any]) -> dict[str, Any]:
        path, state = self._read(token)
        descriptor = self._lock(path)
        if descriptor is None:
            return receipt("yielded", "task_busy", token=token,
                           retry_after_seconds=0.1)
        try:
            answer = PermissionStore(path / "permissions.json", token).resolve(resolution)
            state["deadline"] += answer["paused_seconds"]
            state["continuation"] = answer["continuation"]
            self._save(path, state)
        except PermissionError as error:
            raise ExecutionError(str(error)) from error
        finally:
            self._unlock(path, descriptor)
        return self.step(token)

    def cancel(self, token: str) -> dict[str, Any]:
        path, state = self._read(token)
        descriptor = self._lock(path)
        if descriptor is None:
            return receipt("yielded", "task_busy", token=token,
                           retry_after_seconds=0.1)
        error = self._cleanup(path, state)
        os.close(descriptor)
        if error:
            (path / "active.lock").unlink(missing_ok=True)
            return receipt("failed", "cleanup_failed", token=token, error=error)
        return receipt("cancelled", "cancelled", token=token)

    def _validations(self, state: dict[str, Any]) -> list[dict[str, Any]]:
        task = state["task"]
        remaining = max(0.01, state["deadline"] - time.time())
        records = []
        for command in task["validation"]:
            record = run_owned_process(command, Path(state["cwd"]), remaining)
            records.append(record)
            remaining = max(0.01, state["deadline"] - time.time())
        return records

    def _finish(self, path: Path, state: dict[str, Any], backend: dict[str, Any],
                forced: str) -> dict[str, Any]:
        task = state["task"]
        cwd = Path(state["cwd"])
        before = {key: tuple(value) for key, value in state["before"].items()}
        after = filesystem_snapshot(cwd)
        changes = snapshot_changed_paths(before, after)
        unsafe = unsafe_symlink_paths(cwd, after)
        outside = [item for item in changes if not _path_allowed(item, task["allowed_paths"])]
        read_violations = changes if task["mode"] == "read" else []
        validations = self._validations(state) if forced == "success" and not unsafe else []
        evidence = _evidence(cwd, changes)
        if unsafe:
            classification = "unsafe_symlink"
        elif read_violations:
            classification = "read_only_violation"
        elif outside:
            classification = "scope_violation"
        elif task["mode"] == "edit" and (evidence["diff_truncated"]
                                           or evidence["status_truncated"]):
            classification = "evidence_truncated"
        elif any(item["returncode"] for item in validations):
            classification = "validation_failed"
        else:
            classification = forced
        result = receipt("completed" if classification == "success" else "failed",
                         classification, token=state["token"], task_id=task["id"],
                         repo=state["top"], revision=state["revision"],
                         worktree_isolated=bool(state.get("worktree")), backend=backend,
                         steps_used=state["steps_used"], validation=validations,
                         evidence=evidence, unsafe_symlinks=unsafe,
                         read_only_violations=read_violations, scope_violations=outside)
        error = self._cleanup(path, state)
        if error:
            result.update(status="failed", classification="cleanup_failed",
                          cleanup_error=error, retained_path=str(path))
        return result

    def _cleanup(self, path: Path, state: dict[str, Any]) -> str | None:
        worktree = state.get("worktree")
        if worktree:
            result = _git(Path(state["top"]), "worktree", "remove", "--force",
                          worktree, check=False)
            if result.returncode:
                return result.stderr.strip() or "git worktree remove failed"
        try:
            shutil.rmtree(path)
        except OSError as error:
            return str(error)
        return None

    def gc(self, *, now: float | None = None) -> list[str]:
        """Remove expired unlocked sessions and return the removed tokens."""
        current = time.time() if now is None else now
        removed = []
        for path in list(self.root.iterdir()):
            if path.is_symlink() or not path.is_dir():
                continue
            try:
                state = json.loads((path / "state.json").read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(state, dict) or state.get("expires_at", current + 1) > current:
                continue
            descriptor = self._lock(path)
            if descriptor is None:
                continue
            error = self._cleanup(path, state)
            os.close(descriptor)
            if error:
                (path / "active.lock").unlink(missing_ok=True)
            else:
                removed.append(path.name)
        return removed
