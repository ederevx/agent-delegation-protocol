#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))
import permission_service  # noqa: E402
from permission_service import (  # noqa: E402
    PermissionError, PermissionStore, deterministic_decision,
    permission_request, validate_resolution,
)


class PermissionServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        scratch = Path.home() / "tmp"
        scratch.mkdir(exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(
            prefix="delegation-permission-test-", dir=scratch,
        )
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_atomic_state_write_is_windows_portable_and_posix_private(self) -> None:
        windows_path = self.root / "windows-state.json"
        with mock.patch.object(permission_service.os, "name", "nt"), \
                mock.patch.object(permission_service.os, "fchmod", create=True,
                                  side_effect=AssertionError("fchmod on Windows")):
            permission_service._atomic_json(windows_path, {"platform": "windows"})
        self.assertEqual(json.loads(windows_path.read_text(encoding="utf-8")),
                         {"platform": "windows"})

        posix_path = self.root / "posix-state.json"
        with mock.patch.object(permission_service.os, "name", "posix"), \
                mock.patch.object(permission_service.os, "fchmod", create=True) as chmod:
            permission_service._atomic_json(posix_path, {"platform": "posix"})
        chmod.assert_called_once_with(mock.ANY, 0o600)

    def test_atomic_state_write_closes_descriptor_and_preserves_primary_error(self) -> None:
        descriptor, temporary = tempfile.mkstemp(dir=self.root)
        real_close = os.close
        closed: list[int] = []

        def close(value: int) -> None:
            closed.append(value)
            real_close(value)

        try:
            with mock.patch.object(permission_service.tempfile, "mkstemp",
                                   return_value=(descriptor, temporary)), \
                    mock.patch.object(permission_service.os, "name", "posix"), \
                    mock.patch.object(permission_service.os, "fchmod", create=True,
                                      side_effect=OSError("permission failure")), \
                    mock.patch.object(permission_service.os, "close", side_effect=close), \
                    mock.patch.object(permission_service.os, "unlink",
                                      side_effect=OSError("cleanup failure")):
                with self.assertRaisesRegex(OSError, "permission failure"):
                    permission_service._atomic_json(
                        self.root / "failed-state.json", {"value": 1})
            self.assertEqual(closed, [descriptor])
        finally:
            Path(temporary).unlink(missing_ok=True)

    def test_policy_enforces_workspace_mode_scope_and_hard_denials(self) -> None:
        read = permission_request("session", "read", {"path": "src/main.py"},
                                  "read source", now=1)
        self.assertEqual(deterministic_decision(
            read, self.root, mode="read", allowed_paths=[],
        )[0], "allow")
        write = permission_request("session", "write", {"path": "docs/a.md"},
                                   "write docs", now=1)
        self.assertEqual(deterministic_decision(
            write, self.root, mode="read", allowed_paths=[],
        )[0], "deny")
        self.assertEqual(deterministic_decision(
            write, self.root, mode="edit", allowed_paths=["src"],
        )[0], "deny")
        secret = permission_request("session", "read", {"path": ".ssh/key"},
                                    "read key", now=1)
        self.assertEqual(deterministic_decision(
            secret, self.root, mode="read", allowed_paths=[],
        )[0], "deny")
        shell = permission_request("session", "shell", {"command": "curl example.test"},
                                   "network", now=1)
        self.assertEqual(deterministic_decision(
            shell, self.root, mode="edit", allowed_paths=[],
        )[0], "deny")
        for command in ("cat /etc/passwd", "git -C /etc status",
                        "/usr/bin/cat file", "cat ../outside"):
            shell = permission_request(
                "session", "shell", {"command": command}, "unsafe read", now=1,
            )
            self.assertEqual(deterministic_decision(
                shell, self.root, mode="read", allowed_paths=[],
            )[0], "deny", command)

    def test_exact_grant_is_one_use_and_resolution_cannot_replay(self) -> None:
        store = PermissionStore(self.root / "permissions.json", "session")
        request = permission_request("session", "shell", {"command": "make test"},
                                     "run tests", now=10)
        store.issue(request)
        answer = store.resolve({
            "schema_version": 1, "request_id": request["request_id"],
            "decision": "allow",
        }, now=15)
        self.assertEqual(answer["paused_seconds"], 5)
        self.assertFalse(store.consume_grant("shell", {"command": "make tests"}))
        self.assertTrue(store.consume_grant("shell", {"command": "make test"}))
        self.assertFalse(store.consume_grant("shell", {"command": "make test"}))
        with self.assertRaises(PermissionError):
            store.resolve({
                "schema_version": 1, "request_id": request["request_id"],
                "decision": "allow",
            })

    def test_handled_results_are_bounded_and_exact(self) -> None:
        with self.assertRaises(PermissionError):
            validate_resolution({
                "schema_version": 1, "request_id": "r", "decision": "allow",
                "result": {},
            })
        with self.assertRaises(PermissionError):
            validate_resolution({
                "schema_version": 1, "request_id": "r", "decision": "handled",
                "result": {"value": "x" * (64 * 1024)},
            })
        with self.assertRaises(PermissionError):
            permission_request("session", "shell", {"command": "x" * (64 * 1024)},
                               "oversized", now=1)

    def test_state_file_contains_no_implicit_grants(self) -> None:
        path = self.root / "permissions.json"
        PermissionStore(path, "session")
        state = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(state["grants"], [])
        self.assertIsNone(state["pending"])

    def test_concurrent_consumers_cannot_reuse_a_grant(self) -> None:
        store = PermissionStore(self.root / "permissions.json", "session")
        request = permission_request("session", "shell", {"command": "make test"},
                                     "run tests", now=1)
        store.issue(request)
        store.resolve({"schema_version": 1, "request_id": request["request_id"],
                       "decision": "allow"}, now=2)
        barrier = threading.Barrier(3)
        results = []

        def consume() -> None:
            barrier.wait()
            results.append(store.consume_grant("shell", {"command": "make test"}))

        threads = [threading.Thread(target=consume) for _ in range(2)]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join()
        self.assertEqual(sorted(results), [False, True])


if __name__ == "__main__":
    unittest.main()
