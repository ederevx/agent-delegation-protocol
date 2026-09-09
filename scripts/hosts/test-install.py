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
        "claude/rules/delegation-protocol.md", "claude/agents/bulk-worker.md",
        "claude/agents/balanced-worker.md",
        "claude/hooks/delegation-enforcer.py", "codex/AGENTS.md",
        "codex/agents/bulk_worker.toml", "codex/agents/balanced-worker.toml",
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


def test_bypass_not_creatable_by_product_code() -> None:
    """ADP product code must not activate its own bypass automatically.

    The user or an explicitly authorized assistant controls the marker.
    Only the adapter reads it; installers and helpers must not create it.
    """
    root = Path(__file__).resolve().parents[2]
    allowed_readers = {root / "scripts/hosts/hook_adapter.py"}
    product_dirs = (
        root / "scripts/hosts", root / "scripts/agents",
        root / "claude/hooks", root / "codex/hooks",
    )
    offenders = []
    for directory in product_dirs:
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.py")):
            if path.name.startswith("test-") or path.name.startswith("test_"):
                continue
            if "bypass" in path.read_text(encoding="utf-8").lower() and path not in allowed_readers:
                offenders.append(str(path))
    assert not offenders, f"unexpected bypass reference outside hook_adapter.py: {offenders}"


def test_total_bypass() -> None:
    import os
    import hook_adapter

    with tempfile.TemporaryDirectory(prefix="adp-bypass-") as raw:
        root = Path(raw)
        homes = {host: root / host for host in ("claude", "codex")}
        with patch.dict(os.environ, {
            "CLAUDE_CONFIG_DIR": str(homes["claude"]),
            "CODEX_HOME": str(homes["codex"]),
        }):
            for host, home in homes.items():
                marker = home / ".delegation-protocol/bypass"
                marker.parent.mkdir(parents=True)
                # Preserve even corrupt state and a busy lock under bypass.
                state, lock = hook_adapter._paths(home, "s")
                lock.mkdir(parents=True)
                state.write_text("invalid state")
                marker.write_text("explicit owner request")
                with patch.object(hook_adapter, "_classifier",
                                  side_effect=RuntimeError("broken classifier")):
                    for event in ("prompt", "pre-mutation", "turn-stop",
                                  "worker-start", "worker-complete",
                                  "worker-release", "session-end"):
                        for tool in ("Read", "Edit", "Bash", "Agent", "Task"):
                            for worker in (None, "leaf-a"):
                                assert hook_adapter.run(host, event, {
                                    "session_id": "s", "agent_id": worker,
                                    "tool_name": tool,
                                    "prompt": "Review and update 12 files",
                                }) is None
                    assert state.read_text() == "invalid state"
                    assert lock.is_dir()
                    other = "codex" if host == "claude" else "claude"
                    assert not hook_adapter._bypass(homes[other])
                    marker.unlink()
                    try:
                        hook_adapter.run(host, "pre-mutation", {
                            "session_id": "s", "tool_name": "Read"})
                    except RuntimeError as error:
                        assert str(error) == "broken classifier"
                    else:
                        raise AssertionError("Removing bypass did not restore hooks")


def main() -> None:
    test_total_bypass()
    test_same_link_paths()
    test_bypass_not_creatable_by_product_code()
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
