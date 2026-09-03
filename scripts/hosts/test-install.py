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


def main() -> None:
    test_same_link_paths()
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
