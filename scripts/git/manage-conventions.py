#!/usr/bin/env python3
"""Install and enforce the shared Git commit-message conventions."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Iterable

EDRICK_EMAIL = "evcsinsuan@gmail.com"
EDRICK_NAME = "Edrick Sinsuan"
EDRICK_SIGNOFF = "Signed-off-by: Edrick Sinsuan <evcsinsuan@gmail.com>"
CODEX_ASSISTED = "Assisted-by: Codex:gpt-5"
CLAUDE_ASSISTED = re.compile(
    r"Assisted-by: Claude-Code:[A-Za-z0-9][A-Za-z0-9._-]*$"
)
TRAILER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]*: .+$")
FORBIDDEN_ATTRIBUTION = re.compile(
    r"^(?:Co-authored-by|Generated-by|Reviewed-by):.*"
    r"(?:Codex|Claude|OpenAI|Anthropic)", re.IGNORECASE
)
ZERO_OID = re.compile(r"^0+$")


class GateError(RuntimeError):
    """A convention or installation invariant was violated."""


def run_git(args: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["git", *args], text=True, capture_output=True, check=False,
    )
    if check and result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "unknown Git error"
        raise GateError(f"git {' '.join(args)} failed: {detail}")
    return result


def state_dir(value: str | None = None) -> Path:
    override = value or os.environ.get("DELEGATION_PROTOCOL_GIT_STATE")
    if override:
        return Path(override).expanduser().resolve()
    return (Path.home() / ".agent-delegation-protocol" / "git").resolve()


def atomic_json(path: Path, value: object) -> None:
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


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def wrapper(checker: Path, python_exe: str, root: Path, hook: str) -> bytes:
    command = " ".join(shlex.quote(part) for part in (
        python_exe, str(checker), "hook", "--state-dir", str(root), hook,
    ))
    return f"#!/bin/sh\nexec {command} \"$@\"\n".encode("utf-8")


def global_hooks_path() -> str | None:
    result = run_git(["config", "--global", "--get", "core.hooksPath"], check=False)
    if result.returncode == 1:
        return None
    if result.returncode != 0:
        raise GateError(result.stderr.strip() or "cannot read global core.hooksPath")
    return result.stdout.rstrip("\n")


def write_owned(path: Path, content: bytes, prior_hash: str | None) -> str:
    if path.exists():
        current = digest(path.read_bytes())
        if prior_hash is None or current != prior_hash:
            raise GateError(f"refusing to overwrite modified or unowned hook: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    path.chmod(0o755)
    return digest(content)


def install(owner: str, root: Path, checker: Path, python_exe: str) -> None:
    manifest_path = root / "manifest.json"
    if manifest_path.exists():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise GateError(f"cannot read existing Git gate manifest: {error}") from error
        if manifest.get("version") != 1:
            raise GateError("refusing to replace an unknown Git gate manifest")
        if global_hooks_path() != str((root / "hooks").resolve()):
            raise GateError("global core.hooksPath changed after Git gate installation")
    else:
        manifest = {
            "version": 1, "owners": [],
            "previous_hooks_path": global_hooks_path(), "hook_hashes": {},
        }
    hashes = manifest.get("hook_hashes", {})
    if not isinstance(hashes, dict):
        raise GateError("Git gate manifest has invalid hook ownership data")
    new_hashes: dict[str, str] = {}
    for hook in ("commit-msg", "pre-push"):
        new_hashes[hook] = write_owned(
            root / "hooks" / hook, wrapper(checker, python_exe, root, hook),
            hashes.get(hook),
        )
    owners = manifest.get("owners", [])
    if not isinstance(owners, list) or any(not isinstance(item, str) for item in owners):
        raise GateError("Git gate manifest has invalid owners")
    manifest.update({
        "owners": sorted(set(owners) | {owner}), "checker": str(checker),
        "python": python_exe, "hook_hashes": new_hashes,
    })
    atomic_json(manifest_path, manifest)
    run_git(["config", "--global", "core.hooksPath", str((root / "hooks").resolve())])


def uninstall(owner: str, root: Path) -> None:
    manifest_path = root / "manifest.json"
    if not manifest_path.exists():
        return
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise GateError(f"cannot read Git gate manifest: {error}") from error
    owners = manifest.get("owners", [])
    if not isinstance(owners, list):
        raise GateError("Git gate manifest has invalid owners")
    remaining = sorted(set(owners) - {owner})
    if remaining:
        manifest["owners"] = remaining
        atomic_json(manifest_path, manifest)
        return
    ours = str((root / "hooks").resolve())
    if global_hooks_path() == ours:
        previous = manifest.get("previous_hooks_path")
        if previous is None:
            result = run_git(
                ["config", "--global", "--unset-all", "core.hooksPath"],
                check=False,
            )
            if result.returncode not in (0, 5):
                raise GateError(result.stderr.strip() or "cannot restore core.hooksPath")
        elif isinstance(previous, str):
            run_git(["config", "--global", "core.hooksPath", previous])
        else:
            raise GateError("Git gate manifest has invalid previous_hooks_path")
    hashes = manifest.get("hook_hashes", {})
    for hook in ("commit-msg", "pre-push"):
        path = root / "hooks" / hook
        if path.is_file() and isinstance(hashes, dict):
            expected = hashes.get(hook)
            if isinstance(expected, str) and digest(path.read_bytes()) == expected:
                path.unlink()
    manifest_path.unlink()
    for directory in (root / "hooks", root):
        try:
            directory.rmdir()
        except OSError:
            pass


def long_line_exempt(line: str) -> bool:
    value = line.strip()
    return bool(value) and not any(character.isspace() for character in value)


def managed_message(author_email: str, lines: list[str]) -> bool:
    return (
        author_email.lower() == EDRICK_EMAIL
        or any(line.startswith("Assisted-by:") for line in lines)
        or EDRICK_SIGNOFF in lines
        or any(FORBIDDEN_ATTRIBUTION.search(line) for line in lines)
    )


def lint_message(message: str, author_name: str, author_email: str) -> list[str]:
    lines = message.rstrip("\n").splitlines()
    if not managed_message(author_email, lines):
        return []
    errors: list[str] = []
    if author_email.lower() == EDRICK_EMAIL and author_name != EDRICK_NAME:
        errors.append(
            f"author with {EDRICK_EMAIL} must be exactly {EDRICK_NAME}"
        )
    if not lines or not lines[0].strip():
        return ["subject is empty"]
    for number, line in enumerate(lines, 1):
        if len(line) > 80 and not long_line_exempt(line):
            errors.append(f"line {number} exceeds 80 columns ({len(line)})")
    trailer_start = len(lines)
    while trailer_start and TRAILER.fullmatch(lines[trailer_start - 1]):
        trailer_start -= 1
    trailers = lines[trailer_start:]
    for number, line in enumerate(lines[:trailer_start], 1):
        if TRAILER.fullmatch(line):
            errors.append(f"trailer on line {number} is outside the final contiguous block")
    if not trailers:
        errors.append("message has no final trailer block")
    if (len(lines) < 3 or lines[1] != ""
            or not any(line.strip() for line in lines[2:trailer_start])):
        errors.append("message needs a blank line and a short explanatory body")
    assisted = [line for line in trailers if line.startswith("Assisted-by:")]
    signoffs = [line for line in trailers if line.startswith("Signed-off-by:")]
    if len(assisted) != len(set(assisted)):
        errors.append("Assisted-by trailers must be unique")
    if len(signoffs) != len(set(signoffs)):
        errors.append("Signed-off-by trailers must be unique per signer")
    edrick_assisted = [
        line for line in assisted
        if line == CODEX_ASSISTED or CLAUDE_ASSISTED.fullmatch(line)
    ]
    invalid_assisted = [
        line for line in assisted
        if line not in edrick_assisted
        and re.match(r"Assisted-by: (?:Codex|Claude|Claude-Code|OpenAI|Anthropic)",
                     line, re.IGNORECASE)
    ]
    if len(edrick_assisted) != 1:
        errors.append("exactly one Codex or Claude-Code Assisted-by trailer is required")
    if invalid_assisted:
        errors.append("unsupported Assisted-by identity: " + ", ".join(invalid_assisted))
    if signoffs.count(EDRICK_SIGNOFF) != 1:
        errors.append(f"exactly one '{EDRICK_SIGNOFF}' trailer is required")
    if edrick_assisted and EDRICK_SIGNOFF in trailers:
        if trailers.index(EDRICK_SIGNOFF) != trailers.index(edrick_assisted[0]) + 1:
            errors.append("Edrick's Assisted-by must immediately precede Signed-off-by")
    if any(FORBIDDEN_ATTRIBUTION.search(line) for line in trailers):
        errors.append("assistant attribution must not use Co-authored/Generated/Reviewed-by")
    return errors


def commit_data(commit: str) -> tuple[str, str, str]:
    result = run_git(["show", "-s", "--format=%an%x00%ae%x00%B", commit])
    parts = result.stdout.split("\0", 2)
    if len(parts) != 3:
        raise GateError(f"cannot parse commit metadata for {commit}")
    return parts[0], parts[1], parts[2]


def audit_commits(commits: Iterable[str]) -> None:
    failures: list[str] = []
    seen: set[str] = set()
    for commit in commits:
        if commit in seen:
            continue
        seen.add(commit)
        author_name, author_email, message = commit_data(commit)
        errors = lint_message(message, author_name, author_email)
        if errors:
            failures.append(f"{commit[:12]}: " + "; ".join(errors))
    if failures:
        raise GateError("commit convention audit failed:\n  " + "\n  ".join(failures))


def peel_commit(oid: str) -> str | None:
    result = run_git(["rev-parse", "--verify", f"{oid}^{{commit}}"], check=False)
    if result.returncode in (1, 128):
        return None
    if result.returncode != 0:
        raise GateError(result.stderr.strip() or f"cannot peel {oid}")
    return result.stdout.strip()


def outgoing_commits(stdin_data: bytes, remote_name: str) -> list[str]:
    commits: list[str] = []
    try:
        lines = stdin_data.decode("utf-8").splitlines()
    except UnicodeDecodeError as error:
        raise GateError(f"pre-push input is not UTF-8: {error}") from error
    for number, line in enumerate(lines, 1):
        fields = line.split()
        if len(fields) != 4:
            raise GateError(f"invalid pre-push input on line {number}")
        _local_ref, local_oid, _remote_ref, remote_oid = fields
        if ZERO_OID.fullmatch(local_oid):
            continue
        local_commit = peel_commit(local_oid)
        if local_commit is None:
            continue
        args = ["rev-list", local_commit]
        if ZERO_OID.fullmatch(remote_oid):
            selector = f"--remotes={remote_name}" if remote_name else "--remotes"
            args.extend(["--not", selector])
        else:
            remote_commit = peel_commit(remote_oid)
            if remote_commit is not None:
                args.append(f"^{remote_commit}")
        commits.extend(line for line in run_git(args).stdout.splitlines() if line)
    return commits


def previous_hook(root: Path, hook: str) -> Path | None:
    try:
        manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise GateError(f"cannot load Git gate manifest: {error}") from error
    previous = manifest.get("previous_hooks_path")
    if previous is None:
        base = Path(run_git(["rev-parse", "--git-common-dir"]).stdout.strip())
        if not base.is_absolute():
            base = Path.cwd() / base
        candidate = base / "hooks" / hook
    elif isinstance(previous, str):
        base = Path(os.path.expanduser(previous))
        if not base.is_absolute():
            base = Path.cwd() / base
        candidate = base / hook
    else:
        raise GateError("Git gate manifest has invalid previous_hooks_path")
    candidate = candidate.resolve()
    if (candidate == (root / "hooks" / hook).resolve()
            or not candidate.is_file() or not os.access(candidate, os.X_OK)):
        return None
    return candidate


def invoke_previous(path: Path | None, args: list[str], stdin_data: bytes | None) -> int:
    if path is None:
        return 0
    return subprocess.run([str(path), *args], input=stdin_data, check=False).returncode


def hook_commit_msg(root: Path, argv: list[str]) -> int:
    if len(argv) != 1:
        raise GateError("commit-msg hook requires exactly one message path")
    previous_result = invoke_previous(
        previous_hook(root, "commit-msg"), argv, None,
    )
    if previous_result != 0:
        return previous_result
    try:
        message = Path(argv[0]).read_text(encoding="utf-8")
    except OSError as error:
        raise GateError(f"cannot read commit message: {error}") from error
    ident = run_git(["var", "GIT_AUTHOR_IDENT"]).stdout
    match = re.match(r"^(.*?) <([^>]+)>", ident)
    if match is None:
        raise GateError("cannot parse GIT_AUTHOR_IDENT")
    errors = lint_message(message, match.group(1), match.group(2))
    if errors:
        raise GateError("commit convention audit failed: " + "; ".join(errors))
    return 0


def hook_pre_push(root: Path, argv: list[str]) -> int:
    stdin_data = sys.stdin.buffer.read()
    audit_commits(outgoing_commits(stdin_data, argv[0] if argv else ""))
    return invoke_previous(previous_hook(root, "pre-push"), argv, stdin_data)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="action", required=True)
    for action in ("install", "uninstall"):
        item = sub.add_parser(action)
        item.add_argument("--owner", required=True, choices=("codex", "claude"))
        item.add_argument("--state-dir")
        if action == "install":
            item.add_argument("--python", dest="python_exe", default=sys.executable)
    hook = sub.add_parser("hook")
    hook.add_argument("hook", choices=("commit-msg", "pre-push"))
    hook.add_argument("--state-dir", required=True)
    hook.add_argument("hook_args", nargs=argparse.REMAINDER)
    audit = sub.add_parser("audit-commit")
    audit.add_argument("commits", nargs="+")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        if args.action == "install":
            install(args.owner, state_dir(args.state_dir), Path(__file__).resolve(),
                    args.python_exe)
        elif args.action == "uninstall":
            uninstall(args.owner, state_dir(args.state_dir))
        elif args.action == "audit-commit":
            audit_commits(args.commits)
        elif args.hook == "commit-msg":
            return hook_commit_msg(state_dir(args.state_dir), args.hook_args)
        else:
            return hook_pre_push(state_dir(args.state_dir), args.hook_args)
    except GateError as error:
        print(f"Git convention gate: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
