#!/usr/bin/env python3
"""Disposable-home tests for the manifest ownership primitive."""
from __future__ import annotations

import json
import tempfile
from pathlib import Path, PureWindowsPath
from unittest.mock import patch

import install


class FakeWindowsLink:
    """Minimal path stand-in for testing Windows readlink spellings anywhere."""

    def __init__(self, path: str) -> None:
        self._path = PureWindowsPath(path)

    @property
    def parent(self) -> PureWindowsPath:
        return self._path.parent

    def is_symlink(self) -> bool:
        return True

    def __fspath__(self) -> str:
        return str(self._path)


def fixture(root: Path) -> Path:
    repo = root / "repo"
    for path in (
        "claude/rules/delegation-protocol.md", "claude/agents/frontier-worker.md",
        "claude/agents/balanced-worker.md", "claude/agents/bulk-worker.md",
        "claude/agents/quick-worker.md",
        "claude/hooks/delegation-enforcer.py", "codex/AGENTS.md",
        "codex/agents/frontier_worker.toml", "codex/agents/balanced-worker.toml",
        "codex/agents/bulk_worker.toml", "codex/agents/quick_worker.toml",
        "codex/hooks/delegation-enforcer.py",
        "scripts/hosts/hook_adapter.py", "scripts/hosts/lifecycle.py",
        "scripts/agents/delegation-classifier.py",
    ):
        target = repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(path + "\n", encoding="utf-8")
    return repo


def test_same_link_paths() -> None:
    windows_link = FakeWindowsLink(r"C:\Users\tester\.codex\hooks\hook.py")
    with patch.object(
        install.os, "readlink",
        return_value=r"\\?\C:\Users\tester\repo\hook.py",
    ):
        assert install.same_link(
            windows_link, PureWindowsPath(r"C:\Users\tester\repo\hook.py")
        )
        assert not install.same_link(
            windows_link, PureWindowsPath(r"C:\Users\tester\other\hook.py")
        )
    with patch.object(
        install.os, "readlink", return_value=r"..\..\repo\hook.py"
    ):
        assert install.same_link(
            windows_link, PureWindowsPath(r"C:\Users\tester\repo\hook.py")
        )
    with patch.object(
        install.os, "readlink",
        return_value=r"\\?\UNC\server\share\repo\hook.py",
    ):
        assert install.same_link(
            windows_link, PureWindowsPath(r"\\server\share\repo\hook.py")
        )


def test_windows_symlink_privilege_error() -> None:
    with tempfile.TemporaryDirectory(prefix="adp-symlink-error-") as raw:
        root = Path(raw)
        repo = fixture(root)
        home = root / "codex-home"
        error = OSError("symbolic-link privilege is unavailable")
        error.winerror = 1314
        with patch.object(Path, "symlink_to", side_effect=error):
            try:
                install.install(repo, home, "codex")
            except SystemExit as failure:
                message = str(failure)
            else:
                raise AssertionError("WinError 1314 should stop installation")
        assert "WinError 1314" in message
        assert "Developer Mode" in message
        assert "elevated PowerShell" in message
        assert str(home / "agents/frontier_worker.toml") in message
        state = home / ".delegation-protocol"
        assert not (state / "manifest.json").exists()
        assert not (state / "install.lock").exists()
        assert not (home / "hooks.json").exists()
        assert not (home / "agents/frontier_worker.toml").exists()


def test_other_symlink_errors_are_not_relabelled() -> None:
    source = Path("source")
    destination = Path("destination")
    error = OSError("different filesystem error")
    with patch.object(Path, "symlink_to", side_effect=error):
        try:
            install.create_symlink(destination, source)
        except OSError as failure:
            assert failure is error
        else:
            raise AssertionError("non-1314 OSError should propagate")


def test_explicit_authorization_is_single_use() -> None:
    """The sole remaining override is per-action text, not a standing bypass.

    A prompt naming explicit authorization for the next action allows exactly
    one otherwise-blocked pre-mutation decision, then reverts to normal
    enforcement for every subsequent action, including an immediate repeat
    of the same tool call. No file or persistent marker is involved.
    """
    import os
    import hook_adapter

    with tempfile.TemporaryDirectory(prefix="adp-authorization-") as raw:
        home = Path(raw) / "claude"
        with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(home)}):
            authorized_prompt = (
                "Update 12 files across independent modules. "
                "I explicitly authorize this action."
            )
            routing = hook_adapter.run("claude", "prompt", {
                "session_id": "s", "prompt": authorized_prompt,
            })["hookSpecificOutput"]
            assert routing["hookEventName"] == "UserPromptSubmit", routing
            assert "lowest capable worker" in routing["additionalContext"], routing
            # First blocked action after explicit authorization is allowed.
            assert hook_adapter.run("claude", "pre-mutation", {
                "session_id": "s", "tool_name": "Edit",
            }) is None
            # The very next otherwise-blocked action, even the same tool
            # call, is denied again -- authorization was consumed, not a
            # standing bypass.
            denied = hook_adapter.run("claude", "pre-mutation", {
                "session_id": "s", "tool_name": "Edit",
            })
            assert denied["hookSpecificOutput"]["permissionDecision"] == "deny", denied
            # Without any authorization language, enforcement behaves as before.
            routing = hook_adapter.run("claude", "prompt", {
                "session_id": "t", "prompt": "Update 12 files across independent modules.",
            })["hookSpecificOutput"]
            assert routing["hookEventName"] == "UserPromptSubmit", routing
            assert "lowest capable worker" in routing["additionalContext"], routing
            unauthorized_denied = hook_adapter.run("claude", "pre-mutation", {
                "session_id": "t", "tool_name": "Edit",
            })
            assert unauthorized_denied["hookSpecificOutput"]["permissionDecision"] == "deny", \
                unauthorized_denied


def codex_config_value(home: Path) -> object:
    import tomllib

    data = tomllib.loads((home / "config.toml").read_text(encoding="utf-8"))
    return data.get("agents", {}).get(install.CODEX_CONCURRENCY_KEY)


def test_codex_concurrency_cap() -> None:
    """The Codex host pins native subagent concurrency to the protocol cap.

    The installer owns exactly one `[agents]` assignment in `config.toml`,
    preserves every other line, and undoes only what it recorded.
    """
    cap = install.active_worker_cap()
    key = install.CODEX_CONCURRENCY_KEY
    assert isinstance(cap, int) and cap > 0

    with tempfile.TemporaryDirectory(prefix="adp-codex-config-") as raw:
        root = Path(raw)
        repo = fixture(root)

        # Fresh home: no config.toml at all.
        home = root / "codex-fresh"
        install.install(repo, home, "codex")
        config = home / "config.toml"
        assert codex_config_value(home) == cap
        manifest = json.loads((
            home / ".delegation-protocol/manifest.json"
        ).read_text())
        record = manifest["codex_config"]
        assert record["path"] == str(config)
        assert record["key"] == key
        assert record["value"] == cap
        assert record["previous"] is None
        assert record["table_created"] is True
        assert record["file_created"] is True
        install.uninstall(home, "codex")
        assert not config.exists()

        # Existing configuration without an [agents] table.
        home = root / "codex-unrelated"
        home.mkdir()
        original = (
            'model = "gpt-5.6-sol"\n'
            "\n"
            "[tui]\n"
            "notifications = true\n"
        ).encode("utf-8")
        config = home / "config.toml"
        config.write_bytes(original)
        install.install(repo, home, "codex")
        text = config.read_text(encoding="utf-8")
        assert text.startswith(original.decode("utf-8"))
        assert "[tui]\nnotifications = true\n" in text
        assert codex_config_value(home) == cap
        manifest = json.loads((
            home / ".delegation-protocol/manifest.json"
        ).read_text())
        assert manifest["codex_config"]["previous"] is None
        assert manifest["codex_config"]["table_created"] is True
        assert manifest["codex_config"]["file_created"] is False
        install.uninstall(home, "codex")
        assert config.read_bytes() == original

        # Existing [agents] table carrying another value and the legacy alias.
        home = root / "codex-existing-agents"
        home.mkdir()
        original = (
            "[agents]\n"
            f"{key} = 4\n"
            "max_threads = 3\n"
            "\n"
            "[tui]\n"
            "notifications = false\n"
        ).encode("utf-8")
        config = home / "config.toml"
        config.write_bytes(original)
        install.install(repo, home, "codex")
        text = config.read_text(encoding="utf-8")
        assert codex_config_value(home) == cap
        assert "max_threads = 3\n" in text
        assert text.count(key) == 1
        manifest = json.loads((
            home / ".delegation-protocol/manifest.json"
        ).read_text())
        assert manifest["codex_config"]["previous"] == 4
        assert manifest["codex_config"]["table_created"] is False

        # Re-installing must not duplicate the assignment or take ownership
        # of the value this installer already wrote.
        install.install(repo, home, "codex")
        text = config.read_text(encoding="utf-8")
        assert text.count(key) == 1
        assert text.count("[agents]") == 1
        assert codex_config_value(home) == cap
        manifest = json.loads((
            home / ".delegation-protocol/manifest.json"
        ).read_text())
        assert manifest["codex_config"]["previous"] == 4
        install.uninstall(home, "codex")
        assert config.read_bytes() == original

        # A value the user changed after installation is left alone.
        home = root / "codex-user-changed"
        home.mkdir()
        config = home / "config.toml"
        install.install(repo, home, "codex")
        changed = f"[agents]\n{key} = 2\n".encode("utf-8")
        config.write_bytes(changed)
        install.uninstall(home, "codex")
        assert config.read_bytes() == changed

        # A configuration that does not parse after the edit rolls back.
        home = root / "codex-rollback"
        home.mkdir()
        original = f"[agents]\n{key} = 6\n".encode("utf-8")
        config = home / "config.toml"
        config.write_bytes(original)
        broken = ("[agents\nnot toml\n", None, False)
        with patch.object(install, "set_toml_table_key", return_value=broken):
            try:
                install.install(repo, home, "codex")
            except SystemExit as failure:
                message = str(failure)
            else:
                raise AssertionError("an unparsable edit must stop installation")
        assert key in message and str(config) in message
        assert config.read_bytes() == original
        assert not (home / ".delegation-protocol/manifest.json").exists()
        assert not (home / "hooks.json").exists()
        assert not (home / "AGENTS.md").exists()


def test_codex_config_edit_is_verified_semantically() -> None:
    """An edit that lands anywhere else is rejected even if the cap reads back.

    A value check alone cannot catch a line-based edit applied to the wrong
    place, so the parsed configuration minus the managed key must be
    unchanged.
    """
    cap = install.active_worker_cap()
    key = install.CODEX_CONCURRENCY_KEY
    with tempfile.TemporaryDirectory(prefix="adp-codex-verify-") as raw:
        root = Path(raw)
        repo = fixture(root)
        home = root / "codex-verify"
        home.mkdir()
        original = (
            "[tui]\nnotifications = true\n\n"
            f"[agents]\n{key} = {cap}\n"
        ).encode("utf-8")
        config = home / "config.toml"
        config.write_bytes(original)
        # The cap still reads back correctly; an unrelated table does not.
        damaged = (f"[agents]\n{key} = {cap}\n", None, False)
        with patch.object(install, "set_toml_table_key", return_value=damaged):
            try:
                install.install(repo, home, "codex")
            except SystemExit as failure:
                message = str(failure)
            else:
                raise AssertionError("a misplaced edit must stop installation")
        assert "outside" in message and key in message, message
        assert config.read_bytes() == original
        assert not (home / ".delegation-protocol/manifest.json").exists()


def test_codex_config_reinstall_keeps_user_edits() -> None:
    """Edits made between installs survive the next uninstall.

    Re-installing preserves the first install's backup, so the recorded
    digest must not be refreshed over a file the user has since changed --
    otherwise uninstall would reinstate pre-install bytes on top of edits.
    """
    cap = install.active_worker_cap()
    key = install.CODEX_CONCURRENCY_KEY
    with tempfile.TemporaryDirectory(prefix="adp-codex-reinstall-") as raw:
        root = Path(raw)
        repo = fixture(root)
        home = root / "codex-reinstall"
        home.mkdir()
        config = home / "config.toml"
        config.write_bytes(b'model = "gpt-5.6-sol"\n')
        install.install(repo, home, "codex")
        addition = '\n[history]\npersistence = "none"\n'
        config.write_text(
            config.read_text(encoding="utf-8") + addition, encoding="utf-8"
        )
        install.install(repo, home, "codex")
        manifest = json.loads((
            home / ".delegation-protocol/manifest.json"
        ).read_text())
        assert manifest["codex_config"]["installed_digest"] is None
        assert codex_config_value(home) == cap
        install.uninstall(home, "codex")
        text = config.read_text(encoding="utf-8")
        assert 'model = "gpt-5.6-sol"\n' in text
        assert '[history]\npersistence = "none"\n' in text
        assert key not in text and "[agents]" not in text


def test_codex_config_is_restored_byte_for_byte() -> None:
    """Uninstall returns an untouched config.toml to its exact prior bytes.

    Line endings, a missing final newline, and trailing blank lines are all
    user content: the installer records the pre-install bytes and restores
    them verbatim rather than re-serializing the file.
    """
    cap = install.active_worker_cap()
    key = install.CODEX_CONCURRENCY_KEY
    decoy = (
        "[profiles.review]\n"
        'developer_instructions = """\n'
        "[agents]\n"
        f"{key} = 99\n"
        '"""\n'
    )
    cases = {
        "crlf": (
            'model = "gpt-5.6-sol"\r\n\r\n[agents]\r\n'
            f"{key} = 4\r\n"
        ).encode("utf-8"),
        "no-final-newline": b'model = "gpt-5.6-sol"',
        "trailing-blank-lines": b'model = "gpt-5.6-sol"\n\n\n',
        # A table header spelled inside a multiline string is data, and the
        # cap already matching would hide a wrong edit from a value check.
        "multiline-decoy": (decoy + f"\n[agents]\n{key} = {cap}\n").encode("utf-8"),
        "multiline-decoy-only": decoy.encode("utf-8"),
    }
    with tempfile.TemporaryDirectory(prefix="adp-codex-bytes-") as raw:
        root = Path(raw)
        repo = fixture(root)
        for name, original in cases.items():
            home = root / f"codex-{name}"
            home.mkdir()
            config = home / "config.toml"
            config.write_bytes(original)
            install.install(repo, home, "codex")
            written = config.read_bytes()
            assert codex_config_value(home) == cap, name
            if "decoy" in name:
                text = written.decode("utf-8")
                assert decoy in text, name
                assert text.count(f"{key} = 99") == 1, name
                assert text.count("[agents]") == 2, name
            if name == "crlf":
                assert written.replace(b"\r\n", b"").count(b"\n") == 0, name
            backup = home / ".delegation-protocol" / install.CODEX_CONFIG_BACKUP
            assert backup.read_bytes() == original, name
            install.uninstall(home, "codex")
            assert config.read_bytes() == original, name
            assert not backup.exists(), name


def main() -> None:
    test_explicit_authorization_is_single_use()
    test_codex_concurrency_cap()
    test_codex_config_edit_is_verified_semantically()
    test_codex_config_reinstall_keeps_user_edits()
    test_codex_config_is_restored_byte_for_byte()
    test_same_link_paths()
    test_windows_symlink_privilege_error()
    test_other_symlink_errors_are_not_relabelled()
    with tempfile.TemporaryDirectory(prefix="protocol-hosts-") as raw:
        root, repo = Path(raw), None
        repo = fixture(root)
        home = root / "claude-home"
        install.install(repo, home, "claude")
        manifest = json.loads((home / ".delegation-protocol/manifest.json").read_text())
        assert manifest["version"] == 3
        assert (home / "hooks/delegation-enforcer.py").is_symlink()
        install.uninstall(home, "claude")
        assert not (home / "hooks/delegation-enforcer.py").exists()
        assert not (home / ".delegation-protocol/manifest.json").exists()

        codex = root / "codex-empty"
        install.install(repo, codex, "codex")
        assert (codex / "AGENTS.md").is_symlink()
        hook_adapter = codex / ".delegation-protocol" / "hook_adapter.py"
        manifest = json.loads((
            codex / ".delegation-protocol/manifest.json"
        ).read_text())
        hook_adapter_resource = next(item for item in manifest["resources"]
                                      if item["destination"] == str(hook_adapter))
        assert hook_adapter.is_symlink()
        assert hook_adapter_resource["kind"] == "link"
        cache = codex / ".delegation-protocol/__pycache__"
        cache.mkdir()
        (cache / "runtime.pyc").write_bytes(b"generated cache")
        install.uninstall(codex, "codex")
        assert not (codex / "AGENTS.md").exists()
        assert not hook_adapter.exists()
        assert not (codex / ".delegation-protocol").exists()

        codex = root / "codex-agents"
        codex.mkdir()
        original = b"user instructions\n"
        (codex / "AGENTS.md").write_bytes(original)
        install.install(repo, codex, "codex")
        override = codex / "AGENTS.override.md"
        assert override.is_symlink()
        assert override.read_bytes() == original.rstrip(b"\n") + b"\n\n" + (
            repo / "codex/AGENTS.md"
        ).read_bytes()
        (repo / "codex/AGENTS.md").write_text(
            "updated protocol\n", encoding="utf-8"
        )
        install.install(repo, codex, "codex")
        assert override.read_text(encoding="utf-8").endswith("updated protocol\n")
        install.uninstall(codex, "codex")
        assert not override.exists()
        assert (codex / "AGENTS.md").read_bytes() == original

        codex = root / "codex-override"
        codex.mkdir()
        (codex / "AGENTS.md").write_text("shadowed\n", encoding="utf-8")
        prior_override = codex / "AGENTS.override.md"
        prior_override.write_text("active override\n", encoding="utf-8")
        install.install(repo, codex, "codex")
        assert prior_override.is_symlink()
        install.uninstall(codex, "codex")
        assert not prior_override.is_symlink()
        assert prior_override.read_text(encoding="utf-8") == "active override\n"
        print("Host installation tests: PASS")


if __name__ == "__main__":
    main()
