#!/usr/bin/env python3
"""Disposable-home tests for the v2 manifest ownership primitive."""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

import install


def fixture(root: Path) -> Path:
    repo = root / "repo"
    for path in (
        "claude/rules/delegation-protocol.md", "claude/agents/bulk-worker.md",
        "claude/hooks/delegation-enforcer.py", "codex/AGENTS.md",
        "codex/agents/bulk_worker.toml", "codex/agents/balanced-worker.toml",
        "codex/hooks/delegation-enforcer.py", "scripts/agents/delegationctl.py",
        "scripts/agents/lane_service.py",
        "scripts/hosts/hook_adapter.py", "scripts/hosts/lifecycle.py",
        "scripts/agents/delegation-classifier.py",
        "agents/protocol-v2.json",
    ):
        target = repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(path + "\n", encoding="utf-8")
    return repo


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="protocol-hosts-") as raw:
        root, repo = Path(raw), None
        repo = fixture(root)
        home = root / "claude-home"
        install.install(repo, home, "claude")
        manifest = json.loads((home / ".delegation-protocol/manifest.json").read_text())
        assert manifest["version"] == 2
        assert (home / "hooks/delegation-enforcer.py").is_symlink()
        install.uninstall(home, "claude")
        assert not (home / "hooks/delegation-enforcer.py").exists()
        assert not (home / ".delegation-protocol/manifest.json").exists()
        print("Host v2 installation tests: PASS")


if __name__ == "__main__":
    main()
