#!/usr/bin/env python3
"""Isolated integration tests for the shared Git convention gate."""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
MANAGER = ROOT / "scripts" / "git" / "manage-conventions.py"
VALID = """Explain the convention gate

Install a local check so malformed assistant commits cannot be pushed.

Assisted-by: Codex:gpt-5
Signed-off-by: Edrick Sinsuan <evcsinsuan@gmail.com>
"""
INVALID = """Break the trailer block

Exercise the rejection path.

Assisted-by: Codex:gpt-5

Signed-off-by: Edrick Sinsuan <evcsinsuan@gmail.com>
"""


def run(args: list[str], *, cwd: Path | None = None,
        env: dict[str, str] | None = None, input_data: str | None = None
        ) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args, cwd=cwd, env=env, input=input_data, text=True,
        capture_output=True, check=False,
    )


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def load_manager() -> Any:
    spec = importlib.util.spec_from_file_location("git_conventions", MANAGER)
    if spec is None or spec.loader is None:
        raise AssertionError("cannot import Git convention manager")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def git(env: dict[str, str], repo: Path, *args: str,
        input_data: str | None = None) -> subprocess.CompletedProcess[str]:
    return run(["git", *args], cwd=repo, env=env, input_data=input_data)


def init_repo(env: dict[str, str], repo: Path) -> None:
    repo.mkdir(parents=True)
    require(git(env, repo, "init", "-q").returncode == 0, "git init failed")
    for key, value in (
        ("user.name", "Edrick Sinsuan"),
        ("user.email", "evcsinsuan@gmail.com"),
    ):
        require(git(env, repo, "config", key, value).returncode == 0,
                f"cannot set {key}")


def write_executable(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    path.chmod(0o755)


def test_linter() -> None:
    module = load_manager()
    require(not module.lint_message(
        VALID, "Edrick Sinsuan", "evcsinsuan@gmail.com"), "valid message failed")
    errors = module.lint_message(
        INVALID, "Edrick Sinsuan", "evcsinsuan@gmail.com")
    require(any("outside the final contiguous block" in error for error in errors),
            "blank-separated trailer was accepted")
    long_message = VALID.replace(
        "Install a local check so malformed assistant commits cannot be pushed.",
        "This explanatory prose is deliberately made longer than eighty columns "
        "so the convention gate must reject it.",
    )
    require(any("exceeds 80 columns" in error for error in module.lint_message(
        long_message, "Edrick Sinsuan", "evcsinsuan@gmail.com")),
        "long prose was accepted")
    require(not module.lint_message(
        "Imported subject only\n", "Source Author", "source@example.com"),
        "unattributed non-Edrick import was rejected")
    no_body = """Missing explanation

Assisted-by: Codex:gpt-5
Signed-off-by: Edrick Sinsuan <evcsinsuan@gmail.com>
"""
    require(any("explanatory body" in error for error in module.lint_message(
        no_body, "Edrick Sinsuan", "evcsinsuan@gmail.com")),
        "trailer-only body was accepted")
    require(any("must be exactly Edrick Sinsuan" in error for error in
                module.lint_message(VALID, "Wrong Name", "evcsinsuan@gmail.com")),
            "incorrect Edrick author identity was accepted")
    forbidden = """Imported change

Preserve the source while testing forbidden assistant attribution.

Co-authored-by: Codex <codex@example.com>
"""
    require(module.lint_message(
        forbidden, "Source Author", "source@example.com"),
        "forbidden assistant attribution bypassed the import exception")


def test_installation_and_hooks(root: Path) -> None:
    global_config = root / "global.gitconfig"
    state = root / "state"
    prior = root / "prior-hooks"
    repo = root / "repo"
    marker = root / "previous-commit-msg"
    push_capture = root / "previous-pre-push"
    env = dict(os.environ)
    env.update({
        "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": str(global_config),
        "DELEGATION_PROTOCOL_GIT_STATE": str(state),
    })
    write_executable(
        prior / "commit-msg",
        f"touch {marker}\nprintf 'Signed-off-by: Edrick Sinsuan "
        "<evcsinsuan@gmail.com>\\n' >> \"$1\"\n",
    )
    write_executable(prior / "pre-push", f"cat > {push_capture}\n")
    require(run(["git", "config", "--global", "core.hooksPath", str(prior)],
                env=env).returncode == 0, "cannot seed prior hooks path")
    for owner in ("codex", "claude"):
        result = run([sys.executable, str(MANAGER), "install", "--owner", owner,
                      "--python", sys.executable], env=env)
        require(result.returncode == 0, f"{owner} install failed: {result.stderr}")
    manifest = json.loads((state / "manifest.json").read_text(encoding="utf-8"))
    require(manifest["owners"] == ["claude", "codex"], "owners were not shared")
    require(manifest["previous_hooks_path"] == str(prior),
            "prior global hooks path was not preserved")

    init_repo(env, repo)
    (repo / "file").write_text("one\n", encoding="utf-8")
    require(git(env, repo, "add", "file").returncode == 0, "git add failed")
    valid_path = root / "valid-message"
    valid_path.write_text(VALID, encoding="utf-8")
    result = git(env, repo, "commit", "-q", "-F", str(valid_path))
    require(result.returncode != 0 and "must be unique" in result.stderr,
            "gate validated before the previous hook modified the message")
    write_executable(prior / "commit-msg", f"touch {marker}\n")
    result = git(env, repo, "commit", "-q", "-F", str(valid_path))
    require(result.returncode == 0, f"valid commit failed: {result.stderr}")
    require(marker.exists(), "previous global commit-msg hook was not chained")
    good = git(env, repo, "rev-parse", "HEAD").stdout.strip()

    (repo / "file").write_text("two\n", encoding="utf-8")
    require(git(env, repo, "add", "file").returncode == 0, "second add failed")
    invalid_path = root / "invalid-message"
    invalid_path.write_text(INVALID, encoding="utf-8")
    result = git(env, repo, "commit", "-q", "-F", str(invalid_path))
    require(result.returncode != 0 and "outside the final contiguous block" in result.stderr,
            "invalid commit was not rejected")

    result = git(env, repo, "-c", "core.hooksPath=/dev/null", "commit", "-q",
                 "-F", str(invalid_path))
    require(result.returncode == 0, f"fixture invalid commit failed: {result.stderr}")
    bad = git(env, repo, "rev-parse", "HEAD").stdout.strip()
    pre_push = state / "hooks" / "pre-push"
    update = f"refs/heads/main {bad} refs/heads/main {good}\n"
    result = run([str(pre_push), "origin", "unused"], cwd=repo, env=env,
                 input_data=update)
    require(result.returncode != 0 and bad[:12] in result.stderr,
            "existing-ref outgoing invalid commit was accepted")
    require(not push_capture.exists(), "prior pre-push ran after gate rejection")

    require(git(env, repo, "update-ref", "refs/remotes/origin/main", good).returncode == 0,
            "cannot create remote-tracking fixture")
    tree = git(env, repo, "show", "-s", "--format=%T", good).stdout.strip()
    child_result = git(env, repo, "commit-tree", tree, "-p", good,
                       input_data=VALID)
    require(child_result.returncode == 0, f"cannot create valid child: {child_result.stderr}")
    child = child_result.stdout.strip()
    new_ref = f"refs/heads/new {child} refs/heads/new {'0' * 40}\n"
    result = run([str(pre_push), "origin", "unused"], cwd=repo, env=env,
                 input_data=new_ref)
    require(result.returncode == 0, f"valid new branch was rejected: {result.stderr}")
    require(push_capture.read_text(encoding="utf-8") == new_ref,
            "new-branch pre-push input was not replayed")

    require(git(env, repo, "tag", "-a", "outgoing-tag", child,
                "-m", "fixture tag").returncode == 0,
            "cannot create annotated tag fixture")
    tag_oid = git(env, repo, "rev-parse", "refs/tags/outgoing-tag").stdout.strip()
    tag_update = f"refs/tags/outgoing-tag {tag_oid} refs/tags/outgoing-tag {'0' * 40}\n"
    result = run([str(pre_push), "origin", "unused"], cwd=repo, env=env,
                 input_data=tag_update)
    require(result.returncode == 0, f"annotated commit tag was rejected: {result.stderr}")

    force_update = f"refs/heads/main {child} refs/heads/main {bad}\n"
    result = run([str(pre_push), "origin", "unused"], cwd=repo, env=env,
                 input_data=force_update)
    require(result.returncode == 0, f"valid force-push range was rejected: {result.stderr}")

    imported_env = dict(env)
    imported_env.update({
        "GIT_AUTHOR_NAME": "Source Author", "GIT_AUTHOR_EMAIL": "source@example.com",
        "GIT_COMMITTER_NAME": "Source Author",
        "GIT_COMMITTER_EMAIL": "source@example.com",
    })
    imported = run(["git", "commit-tree", tree, "-p", child], cwd=repo,
                   env=imported_env, input_data="Imported subject only\n")
    require(imported.returncode == 0, f"cannot create import fixture: {imported.stderr}")
    import_oid = imported.stdout.strip()
    import_update = (
        f"refs/heads/import {import_oid} refs/heads/import {'0' * 40}\n"
    )
    result = run([str(pre_push), "origin", "unused"], cwd=repo, env=env,
                 input_data=import_update)
    require(result.returncode == 0,
            f"unattributed exact-import history was rejected: {result.stderr}")

    blob = git(env, repo, "hash-object", "-w", "file").stdout.strip()
    blob_update = f"refs/tags/blob {blob} refs/tags/blob {'0' * 40}\n"
    result = run([str(pre_push), "origin", "unused"], cwd=repo, env=env,
                 input_data=blob_update)
    require(result.returncode == 0, f"non-commit tag was rejected: {result.stderr}")

    deletion = f"(delete) {'0' * 40} refs/heads/main {bad}\n"
    result = run([str(pre_push), "origin", "unused"], cwd=repo, env=env,
                 input_data=deletion)
    require(result.returncode == 0, f"deletion was rejected: {result.stderr}")
    require(push_capture.read_text(encoding="utf-8") == deletion,
            "pre-push stdin was not buffered and replayed exactly")

    result = run([sys.executable, str(MANAGER), "uninstall", "--owner", "codex"],
                 env=env)
    require(result.returncode == 0, f"first uninstall failed: {result.stderr}")
    require((state / "hooks" / "pre-push").exists(),
            "first owner removed the shared hook")
    result = run([sys.executable, str(MANAGER), "uninstall", "--owner", "claude"],
                 env=env)
    require(result.returncode == 0, f"last uninstall failed: {result.stderr}")
    restored = run(["git", "config", "--global", "--get", "core.hooksPath"],
                   env=env)
    require(restored.stdout.strip() == str(prior), "prior hooks path was not restored")


def test_default_repo_hook_chain(root: Path) -> None:
    env = dict(os.environ)
    state = root / "state"
    config = root / "global.gitconfig"
    env.update({
        "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": str(config),
        "DELEGATION_PROTOCOL_GIT_STATE": str(state),
    })
    repo = root / "repo"
    init_repo(env, repo)
    marker = root / "repo-hook-ran"
    write_executable(repo / ".git" / "hooks" / "commit-msg", f"touch {marker}\n")
    result = run([sys.executable, str(MANAGER), "install", "--owner", "codex",
                  "--python", sys.executable], env=env)
    require(result.returncode == 0, f"gate install failed: {result.stderr}")
    (repo / "file").write_text("one\n", encoding="utf-8")
    git(env, repo, "add", "file")
    message = root / "message"
    message.write_text(VALID, encoding="utf-8")
    result = git(env, repo, "commit", "-q", "-F", str(message))
    require(result.returncode == 0, f"commit failed: {result.stderr}")
    require(marker.exists(), "default repository commit-msg hook was not chained")


def main() -> int:
    require(MANAGER.exists(), "missing Git convention manager")
    test_linter()
    with tempfile.TemporaryDirectory(prefix="git-convention-test-") as temporary:
        root = Path(temporary)
        test_installation_and_hooks(root / "global-chain")
        test_default_repo_hook_chain(root / "repo-chain")
    print("Git convention gate self-test: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
