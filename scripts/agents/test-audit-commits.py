#!/usr/bin/env python3
"""Tests for the commit-message audit command."""
from __future__ import annotations

import importlib.util
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("audit_commits", HERE / "audit-commits.py")
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def git(root: Path, *args: str, input: str | None = None) -> str:
    return subprocess.check_output(["git", "-C", str(root), *args], text=True,
                                   input=input, stderr=subprocess.DEVNULL)


class AuditCommitsTests(unittest.TestCase):
    def commit(self, root: Path, message: str) -> None:
        tree = git(root, "write-tree").strip()
        parents = []
        try:
            parents = [git(root, "rev-parse", "refs/heads/main").strip()]
        except subprocess.CalledProcessError:
            pass
        oid = git(root, "commit-tree", tree, *sum((["-p", parent] for parent in parents), []),
                   input=message).strip()
        git(root, "update-ref", "refs/heads/main", oid)

    def test_valid_commit_has_no_issues(self) -> None:
        with tempfile.TemporaryDirectory(prefix="audit-commits-test-") as name:
            root = Path(name)
            git(root, "init", "-q")
            self.commit(root, "Good change\n\nExplain the reason.\n\n"
                        "Assisted-by: Codex:gpt-5\n"
                        "Signed-off-by: Edrick Sinsuan <evcsinsuan@gmail.com>\n")
            result = MODULE.audit(root, ["main"])
            self.assertEqual(result["commit_count"], 1)
            self.assertEqual(result["issue_counts"], {})

    def test_reports_each_required_violation_deterministically(self) -> None:
        with tempfile.TemporaryDirectory(prefix="audit-commits-test-") as name:
            root = Path(name)
            git(root, "init", "-q")
            self.commit(root, "First\n\nReason.\n\n"
                        "Assisted-by: Codex:gpt 5\n\n"
                        "Signed-off-by: Edrick Sinsuan <evcsinsuan@gmail.com>\n")
            self.commit(root, "Second\n\n"
                        "Assisted-by: Codex:gpt-5\n"
                        "Signed-off-by: Edrick Sinsuan <evcsinsuan@gmail.com>\n")
            result = MODULE.audit(root, ["main"])
            self.assertEqual(result["commit_count"], 2)
            self.assertEqual(result["issue_counts"], {
                "assistant-identity": 1,
                "missing-body": 1,
                "split-trailer-block": 1,
            })
            self.assertEqual([c["subject"] for c in result["commits"]],
                             ["First", "Second"])
            encoded = json.dumps(result, sort_keys=True)
            self.assertEqual(encoded, json.dumps(MODULE.audit(root, ["main"]),
                                                  sort_keys=True))

    def test_accepts_actual_claude_model_id(self) -> None:
        with tempfile.TemporaryDirectory(prefix="audit-commits-test-") as name:
            root = Path(name)
            git(root, "init", "-q")
            self.commit(root, "Claude change\n\nExplain the reason.\n\n"
                        "Assisted-by: Claude-Code:claude-sonnet-4-20250514\n"
                        "Signed-off-by: Edrick Sinsuan <evcsinsuan@gmail.com>\n")
            self.assertEqual(MODULE.audit(root, ["main"])["issue_counts"], {})

    def test_rejects_missing_and_duplicate_assistant_trailers(self) -> None:
        with tempfile.TemporaryDirectory(prefix="audit-commits-test-") as name:
            root = Path(name)
            git(root, "init", "-q")
            self.commit(root, "Missing assistant\n\nExplain the reason.\n\n"
                        "Signed-off-by: Edrick Sinsuan <evcsinsuan@gmail.com>\n")
            self.commit(root, "Duplicate assistant\n\nExplain the reason.\n\n"
                        "Assisted-by: Claude-Code:claude-opus-5\n"
                        "Assisted-by: Claude-Code:claude-opus-5\n"
                        "Signed-off-by: Edrick Sinsuan <evcsinsuan@gmail.com>\n")
            self.assertEqual(MODULE.audit(root, ["main"])["issue_counts"], {
                "duplicate-assistant": 1,
                "missing-assistant": 1,
            })


if __name__ == "__main__":
    unittest.main()
