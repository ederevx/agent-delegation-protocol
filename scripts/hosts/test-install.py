#!/usr/bin/env python3
"""Disposable-home tests for the manifest ownership primitive."""
from __future__ import annotations

import hashlib
import json
import os
import runpy
import tempfile
from functools import wraps
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


class SymlinkFixtureUnavailable(Exception):
    """The current Windows account cannot construct a legacy-link fixture."""


def fixture_symlink_to(destination: Path, source: Path) -> None:
    """Create a test-only link, skipping only for Windows privilege denial."""
    try:
        destination.symlink_to(source)
    except OSError as error:
        if os.name == "nt" and getattr(error, "winerror", None) == 1314:
            raise SymlinkFixtureUnavailable from error
        raise


def requires_symlink_fixture(test):
    """Keep legacy-link assertions active whenever the fixture is available."""
    @wraps(test)
    def wrapped() -> None:
        try:
            test()
        except SymlinkFixtureUnavailable:
            print(f"{test.__name__}: SKIP (Windows symlink privilege unavailable)")
    return wrapped


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


def test_codex_open_thread_capacity() -> None:
    """The Codex host writes its independent native thread capacity.

    The installer owns exactly one `[agents]` assignment in `config.toml`,
    preserves every other line, and undoes only what it recorded.
    """
    cap = install.CODEX_OPEN_THREAD_CAPACITY
    key = install.CODEX_CONCURRENCY_KEY
    classifier = runpy.run_path(
        str(Path(__file__).resolve().parents[1] / "agents" / "delegation-classifier.py")
    )
    assert cap == 10
    assert classifier["MAX_ACTIVE_WORKERS"] == 10

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
    cap = install.CODEX_OPEN_THREAD_CAPACITY
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
    cap = install.CODEX_OPEN_THREAD_CAPACITY
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
    cap = install.CODEX_OPEN_THREAD_CAPACITY
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


def test_codex_thread_capacity_upgrade_preserves_original_restore() -> None:
    """A 1024-thread workaround install migrates to 10 without losing restore.

    Its manifest must retain the user's original value when this version
    writes the ordinary capacity, so reinstall and untouched uninstall remain
    exact.
    """
    workaround_cap = 1024
    capacity = install.CODEX_OPEN_THREAD_CAPACITY
    key = install.CODEX_CONCURRENCY_KEY
    assert capacity == 10
    with tempfile.TemporaryDirectory(prefix="adp-codex-capacity-upgrade-") as raw:
        root = Path(raw)
        repo = fixture(root)
        home = root / "codex-upgrade"
        home.mkdir()
        config = home / "config.toml"
        original = f"[agents]\n{key} = 4\nmax_threads = 3\n".encode("utf-8")
        config.write_bytes(original)
        install.install(repo, home, "codex")

        # Model the 1024-thread workaround state: its manifest and backup
        # still describe the user's original setting.
        workaround = (
            f"[agents]\n{key} = {workaround_cap}\nmax_threads = 3\n"
        ).encode("utf-8")
        config.write_bytes(workaround)
        manifest_path = home / ".delegation-protocol/manifest.json"
        manifest = json.loads(manifest_path.read_text())
        record = manifest["codex_config"]
        record["value"] = workaround_cap
        record["previous"] = 4
        record["previous_line"] = f"{key} = 4\n"
        record["installed_digest"] = hashlib.sha256(workaround).hexdigest()
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        install.install(repo, home, "codex")
        manifest = json.loads(manifest_path.read_text())
        record = manifest["codex_config"]
        assert codex_config_value(home) == capacity
        assert record["value"] == capacity
        assert record["previous"] == 4
        assert record["previous_line"] == f"{key} = 4\n"
        assert "max_threads = 3\n" in config.read_text(encoding="utf-8")

        # A subsequent install must retain first-install restoration metadata.
        install.install(repo, home, "codex")
        manifest = json.loads(manifest_path.read_text())
        assert manifest["codex_config"]["previous"] == 4
        install.uninstall(home, "codex")
        assert config.read_bytes() == original


def manifest_for(home: Path) -> dict:
    return json.loads((home / ".delegation-protocol/manifest.json").read_text())


def assert_regular_resources(repo: Path, home: Path, host: str) -> dict:
    """Assert every installed runtime asset is self-contained and copied."""
    manifest = manifest_for(home)
    assert manifest["host"] == host
    assert manifest["resources"]
    for item in manifest["resources"]:
        source, destination = Path(item["source"]), Path(item["destination"])
        assert item["kind"] == "copy", item
        assert destination.is_file() and not destination.is_symlink(), destination
        assert destination.read_bytes() == source.read_bytes(), destination
        assert manifest["hashes"][str(destination)] == hashlib.sha256(
            destination.read_bytes()
        ).hexdigest()
    return manifest


def test_fresh_copy_install_and_managed_refresh() -> None:
    """Both hosts install only regular files, then refresh only owned copies."""
    with tempfile.TemporaryDirectory(prefix="adp-copy-fresh-") as raw:
        root = Path(raw)
        for host in ("claude", "codex"):
            repo, home = fixture(root / host), root / f"{host}-home"
            install.install(repo, home, host)
            first = assert_regular_resources(repo, home, host)
            tracked = next(item for item in first["resources"] if item["destination"].endswith(
                "delegation-enforcer.py"
            ))
            source, destination = Path(tracked["source"]), Path(tracked["destination"])
            source.write_bytes(b"managed refresh\n")
            install.install(repo, home, host)
            assert destination.read_bytes() == b"managed refresh\n"
            second = assert_regular_resources(repo, home, host)
            assert second["hashes"][str(destination)] == hashlib.sha256(
                b"managed refresh\n"
            ).hexdigest()


@requires_symlink_fixture
def test_legacy_v116_links_migrate_from_a_different_checkout() -> None:
    """A v1.16 link manifest may migrate only when each old target is exact."""
    with tempfile.TemporaryDirectory(prefix="adp-copy-legacy-") as raw:
        root = Path(raw)
        old_repo, repo = fixture(root / "v1.16-checkout"), fixture(root / "new-checkout")
        home = root / "codex-home"
        home.mkdir()
        original_agents = b"user global instructions\n"
        original_config = b'[agents]\nmax_threads = 9\n'
        (home / "AGENTS.md").write_bytes(original_agents)
        (home / "config.toml").write_bytes(original_config)

        # Build an authentic v1.16-style resource manifest: all resources were
        # links, and its source checkout is intentionally different from the
        # checkout now doing the migration.
        install.install(old_repo, home, "codex")
        legacy = manifest_for(home)
        old_resources = install.resources(old_repo, home, "codex")
        for source, destination, _ in old_resources:
            destination.unlink()
            fixture_symlink_to(destination, source)
        legacy["resources"] = [
            {"source": str(source), "destination": str(destination), "kind": "link"}
            for source, destination, _ in old_resources
        ]
        legacy["owned"] = [str(destination) for _, destination, _ in old_resources]
        legacy["hashes"] = {}
        legacy["repo"] = str(old_repo)
        legacy["version"] = 3  # v1.16's native-host manifest schema.
        manifest_path = home / ".delegation-protocol/manifest.json"
        preserved_backups = {
            path.name: path.read_bytes()
            for path in (home / ".delegation-protocol").glob("*.before-first-install")
        }
        policy = legacy["policy"].copy()
        codex_config = legacy["codex_config"].copy()
        manifest_path.write_text(json.dumps(legacy), encoding="utf-8")

        install.install(repo, home, "codex")
        migrated = assert_regular_resources(repo, home, "codex")
        assert migrated["policy"] == policy
        assert migrated["codex_config"] == codex_config
        retained_backups = {
            path.name: path.read_bytes()
            for path in (home / ".delegation-protocol").glob("*.before-first-install")
        }
        for name, payload in preserved_backups.items():
            assert retained_backups[name] == payload
        old_source, migrated_destination, _ = old_resources[0]
        migrated_destination.unlink()
        # The old checkout has identical bytes, but its link is no longer an
        # owned v3 resource after migration and must not be followed.
        fixture_symlink_to(migrated_destination, old_source)
        try:
            install.install(repo, home, "codex")
        except SystemExit as error:
            assert "unowned destination" in str(error)
        else:
            raise AssertionError("old source link was accepted after migration")
        migrated_destination.unlink()
        migrated_destination.write_bytes((repo / old_source.relative_to(old_repo)).read_bytes())
        install.uninstall(home, "codex")
        assert (home / "AGENTS.md").read_bytes() == original_agents
        assert (home / "config.toml").read_bytes() == original_config


@requires_symlink_fixture
def test_refuses_foreign_copies_and_foreign_symlinks() -> None:
    """Ownership never follows altered bytes or an attacker-replaced link."""
    with tempfile.TemporaryDirectory(prefix="adp-copy-foreign-") as raw:
        root = Path(raw)
        for host in ("claude", "codex"):
            repo, home = fixture(root / host), root / f"{host}-home"
            install.install(repo, home, host)
            item = manifest_for(home)["resources"][0]
            destination = Path(item["destination"])
            destination.write_bytes(b"user changed this copy\n")
            try:
                install.install(repo, home, host)
            except SystemExit as error:
                assert "unowned destination" in str(error)
            else:
                raise AssertionError("modified managed copy was overwritten")
            destination.unlink()
            foreign = root / f"foreign-{host}"
            # A link to identical data is still a foreign path.  Byte equality
            # must never turn a replacement symlink into an owned copy.
            foreign.write_bytes(Path(item["source"]).read_bytes())
            fixture_symlink_to(destination, foreign)
            try:
                install.install(repo, home, host)
            except SystemExit as error:
                assert "unowned destination" in str(error)
            else:
                raise AssertionError("foreign symlink was overwritten")


@requires_symlink_fixture
def test_reinstall_refuses_former_policy_links() -> None:
    """Managed policy copies cannot be replaced by legacy-style links."""
    with tempfile.TemporaryDirectory(prefix="adp-copy-policy-links-") as raw:
        root, repo = Path(raw), fixture(Path(raw) / "repo")

        # Direct policy: its former source link must not be accepted merely
        # because it points at the same bytes.
        direct = root / "direct"
        install.install(repo, direct, "codex")
        agents = direct / "AGENTS.md"
        agents.unlink()
        fixture_symlink_to(agents, repo / "codex/AGENTS.md")
        try:
            install.install(repo, direct, "codex")
        except SystemExit as error:
            assert "modified Codex policy" in str(error)
        else:
            raise AssertionError("direct policy source link was accepted")

        # Composed policy had a link to its composed state in earlier releases.
        # A current managed copy must reject that link on reinstall.
        composed = root / "composed"
        composed.mkdir()
        (composed / "AGENTS.md").write_bytes(b"user policy\n")
        install.install(repo, composed, "codex")
        override = composed / "AGENTS.override.md"
        override.unlink()
        fixture_symlink_to(
            override, composed / ".delegation-protocol/AGENTS.composed.md"
        )
        try:
            install.install(repo, composed, "codex")
        except SystemExit as error:
            assert "incomplete composed Codex policy" in str(error)
        else:
            raise AssertionError("composed policy link was accepted")


def test_direct_policy_copy_failure_restores_bytes_and_mode() -> None:
    """A late chmod failure leaves the pre-existing direct policy untouched."""
    with tempfile.TemporaryDirectory(prefix="adp-copy-policy-rollback-") as raw:
        root, repo, home = Path(raw), fixture(Path(raw) / "repo"), Path(raw) / "home"
        install.install(repo, home, "codex")
        agents = home / "AGENTS.md"
        previous_bytes, previous_mode = agents.read_bytes(), agents.stat().st_mode & 0o7777
        (repo / "codex/AGENTS.md").write_bytes(b"new policy bytes\n")
        real_chmod = install.os.chmod

        def fail_policy_chmod(path: Path | str, mode: int) -> None:
            real_chmod(path, mode)
            if Path(path) == agents:
                raise OSError("late policy chmod failure")

        with patch.object(install.os, "chmod", side_effect=fail_policy_chmod):
            try:
                install.install(repo, home, "codex")
            except OSError as error:
                assert str(error) == "late policy chmod failure"
            else:
                raise AssertionError("late policy chmod failure was swallowed")
        assert agents.is_file() and not agents.is_symlink()
        assert agents.read_bytes() == previous_bytes
        assert agents.stat().st_mode & 0o7777 == previous_mode


def test_uninstall_preserves_changed_composed_policy_backup() -> None:
    """A user-edited original policy backup survives uninstall unchanged."""
    with tempfile.TemporaryDirectory(prefix="adp-copy-policy-backup-") as raw:
        root, repo, home = Path(raw), fixture(Path(raw) / "repo"), Path(raw) / "home"
        home.mkdir()
        (home / "AGENTS.md").write_bytes(b"original user policy\n")
        install.install(repo, home, "codex")
        backup = home / ".delegation-protocol/original-active-global.md"
        backup.write_bytes(b"user edited preserved backup\n")
        install.uninstall(home, "codex")
        assert backup.read_bytes() == b"user edited preserved backup\n"


def test_uninstall_keeps_changed_assets_and_restores_user_state() -> None:
    with tempfile.TemporaryDirectory(prefix="adp-copy-uninstall-") as raw:
        root, repo, home = Path(raw), None, None
        repo = fixture(root)
        home = root / "codex-home"
        home.mkdir()
        original_agents = b"own instructions\n"
        original_config = b'model = "user-choice"\n'
        (home / "AGENTS.md").write_bytes(original_agents)
        (home / "config.toml").write_bytes(original_config)
        install.install(repo, home, "codex")
        item = manifest_for(home)["resources"][0]
        changed = Path(item["destination"])
        changed.write_bytes(b"leave this user edit alone\n")
        install.uninstall(home, "codex")
        assert changed.read_bytes() == b"leave this user edit alone\n"
        assert (home / "AGENTS.md").read_bytes() == original_agents
        assert (home / "config.toml").read_bytes() == original_config


@requires_symlink_fixture
def test_late_failure_restores_legacy_links_and_exact_bytes() -> None:
    """Migration failures restore the old link tree and all prior metadata."""
    with tempfile.TemporaryDirectory(prefix="adp-copy-rollback-") as raw:
        root = Path(raw)
        old_repo, repo, home = fixture(root / "old"), fixture(root / "new"), root / "claude"
        install.install(old_repo, home, "claude")
        legacy = manifest_for(home)
        old_resources = install.resources(old_repo, home, "claude")
        for source, destination, _ in old_resources:
            destination.unlink()
            fixture_symlink_to(destination, source)
        legacy["resources"] = [
            {"source": str(source), "destination": str(destination), "kind": "link"}
            for source, destination, _ in old_resources
        ]
        legacy["owned"] = [str(destination) for _, destination, _ in old_resources]
        legacy["hashes"] = {}
        legacy["repo"] = str(old_repo)
        manifest_path = home / ".delegation-protocol/manifest.json"
        before = {path: path.read_bytes() for path in (
            home / "settings.json", manifest_path,
            home / ".delegation-protocol/host-settings.json",
        )}
        manifest_path.write_text(json.dumps(legacy), encoding="utf-8")
        before[manifest_path] = manifest_path.read_bytes()
        with patch.object(install.settings, "install", side_effect=RuntimeError("late failure")):
            try:
                install.install(repo, home, "claude")
            except RuntimeError as error:
                assert str(error) == "late failure"
            else:
                raise AssertionError("late failure did not abort installation")
        for source, destination, _ in old_resources:
            assert destination.is_symlink() and install.same_link(destination, source)
        for path, payload in before.items():
            assert path.read_bytes() == payload


def main() -> None:
    test_explicit_authorization_is_single_use()
    test_codex_open_thread_capacity()
    test_codex_config_edit_is_verified_semantically()
    test_codex_config_reinstall_keeps_user_edits()
    test_codex_config_is_restored_byte_for_byte()
    test_codex_thread_capacity_upgrade_preserves_original_restore()
    test_fresh_copy_install_and_managed_refresh()
    test_legacy_v116_links_migrate_from_a_different_checkout()
    test_refuses_foreign_copies_and_foreign_symlinks()
    test_reinstall_refuses_former_policy_links()
    test_direct_policy_copy_failure_restores_bytes_and_mode()
    test_uninstall_preserves_changed_composed_policy_backup()
    test_uninstall_keeps_changed_assets_and_restores_user_state()
    test_late_failure_restores_legacy_links_and_exact_bytes()
    print("Host installation tests: PASS")


if __name__ == "__main__":
    main()
