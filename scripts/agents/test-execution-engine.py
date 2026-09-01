#!/usr/bin/env python3
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))
import execution_engine  # noqa: E402
from execution_engine import (  # noqa: E402
    ExecutionEngine, ExecutionError, run_owned_process, spawn_owned_process,
)
from permission_service import permission_request  # noqa: E402


class ExecutionEngineTests(unittest.TestCase):
    def setUp(self) -> None:
        scratch = Path.home() / "tmp"
        scratch.mkdir(exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(
            prefix="delegation-execution-test-", dir=scratch,
        )
        self.base = Path(self.temporary.name)
        self.repo = self.base / "repo"
        self.repo.mkdir()
        self.git("init", "-q")
        self.git("config", "user.name", "Test")
        self.git("config", "user.email", "test@example.invalid")
        (self.repo / "src").mkdir()
        (self.repo / "src" / "main.txt").write_text("before\n", encoding="utf-8")
        self.git("add", ".")
        self.git("commit", "-qm", "initial")
        self.state = self.base / "state"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def git(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["git", "-C", str(self.repo), *arguments], check=True,
                              text=True, capture_output=True)

    def task(self, *, mode: str = "edit", workspace: str = "isolated",
             allowed: list[str] | None = None,
             validation: list[list[str]] | None = None,
             max_steps: int = 3, max_output: int = 65536) -> dict:
        return {
            "schema_version": 2, "id": "task", "mode": mode,
            "repo": str(self.repo), "prompt": "perform bounded work",
            "allowed_paths": ["src"] if allowed is None else allowed,
            "workspace": workspace, "validation": validation or [],
            "budgets": {"timeout_seconds": 30, "max_output_bytes": max_output,
                        "max_steps": max_steps},
        }

    def test_isolated_edit_returns_evidence_and_leaves_source_untouched(self) -> None:
        def runner(_task, cwd, _context):
            (cwd / "src" / "main.txt").write_text("after\n", encoding="utf-8")
            return {"classification": "success", "completed": True, "report": "done"}

        engine = ExecutionEngine(self.state, runner)
        started = engine.start(self.task(validation=[["git", "diff", "--check"]]))
        answer = engine.step(started["token"])
        self.assertEqual(answer["status"], "completed")
        self.assertEqual(answer["classification"], "success")
        self.assertEqual(answer["evidence"]["changed_paths"], ["src/main.txt"])
        self.assertEqual(answer["validation"][0]["returncode"], 0)
        self.assertEqual((self.repo / "src" / "main.txt").read_text(), "before\n")
        self.assertFalse((self.state / started["token"]).exists())

    def test_shared_read_mutation_and_edit_scope_escape_fail_closed(self) -> None:
        def runner(_task, cwd, _context):
            (cwd / "src" / "main.txt").write_text("changed\n", encoding="utf-8")
            return {"classification": "success"}

        engine = ExecutionEngine(self.state, runner)
        read = engine.start(self.task(mode="read", workspace="shared", validation=[]))
        answer = engine.step(read["token"])
        self.assertEqual(answer["classification"], "read_only_violation")
        self.git("checkout", "--", "src/main.txt")

        def outside(_task, cwd, _context):
            (cwd / "outside.txt").write_text("changed\n", encoding="utf-8")
            return {"classification": "success"}

        engine = ExecutionEngine(self.state, outside)
        edit = engine.start(self.task())
        answer = engine.step(edit["token"])
        self.assertEqual(answer["classification"], "scope_violation")
        self.assertEqual(answer["scope_violations"], ["outside.txt"])

    def test_permission_pause_requires_exact_resolution_and_resumes(self) -> None:
        calls = []

        def runner(_task, _cwd, context):
            calls.append(context)
            if len(calls) == 1:
                return {"completed": False, "permission_request": permission_request(
                    context["token"], "shell", {"command": "make test"},
                    "run validation",
                )}
            self.assertIn("approved", context["continuation"])
            self.assertTrue(context["permissions"].consume_grant(
                "shell", {"command": "make test"},
            ))
            self.assertFalse(context["permissions"].consume_grant(
                "shell", {"command": "make test"},
            ))
            return {"classification": "success"}

        engine = ExecutionEngine(self.state, runner)
        started = engine.start(self.task())
        paused = engine.step(started["token"])
        self.assertEqual(paused["status"], "permission_required")
        self.assertEqual(engine.step(started["token"])["status"], "permission_required")
        with self.assertRaises(ExecutionError):
            engine.resume(started["token"], {
                "schema_version": 1, "request_id": "wrong", "decision": "allow",
            })
        completed = engine.resume(started["token"], {
            "schema_version": 1,
            "request_id": paused["request"]["request_id"], "decision": "allow",
        })
        self.assertEqual(completed["status"], "completed")

    def test_step_output_and_step_budgets_are_enforced(self) -> None:
        yielding = ExecutionEngine(self.state, lambda *_: {
            "classification": "success", "completed": False,
        })
        started = yielding.start(self.task(max_steps=1))
        self.assertEqual(yielding.step(started["token"])["status"], "yielded")
        exhausted = yielding.step(started["token"])
        self.assertEqual(exhausted["classification"], "step_budget_exhausted")

        noisy = ExecutionEngine(self.state, lambda *_: {"report": "x" * 200})
        started = noisy.start(self.task(max_output=32))
        self.assertEqual(noisy.step(started["token"])["classification"],
                         "output_budget_exhausted")

    def test_expired_state_is_collected_and_process_timeout_is_owned(self) -> None:
        engine = ExecutionEngine(self.state, lambda *_: {"classification": "success"},
                                 state_ttl_seconds=1)
        started = engine.start(self.task())
        removed = engine.gc(now=10**12)
        self.assertEqual(removed, [started["token"]])

        cancelled = threading.Event()
        cancelled.set()
        result = run_owned_process(
            [sys.executable, "-c", "import time; time.sleep(30)"], self.repo, 30,
            cancel=cancelled,
        )
        self.assertTrue(result["cancelled"])
        self.assertIsNotNone(result["returncode"])

    def test_owned_process_supports_stdin_and_bounded_output(self) -> None:
        echoed = run_owned_process(
            [sys.executable, "-c", "import sys; print(sys.stdin.read())"],
            self.repo, 5, input_text="worker prompt",
        )
        self.assertEqual(echoed["stdout"], "worker prompt\n")
        self.assertFalse(echoed["output_budget_exhausted"])

        noisy = run_owned_process(
            [sys.executable, "-c",
             "import os; os.write(1, b'x' * 1048576)"],
            self.repo, 5, max_output_bytes=1024,
        )
        self.assertTrue(noisy["output_budget_exhausted"])
        self.assertTrue(noisy["stdout_truncated"])
        self.assertLessEqual(
            len(noisy["stdout"].encode()) + len(noisy["stderr"].encode()), 1024,
        )

    def test_windows_spawn_assigns_job_before_resuming(self) -> None:
        events = []

        class Process:
            _handle = 123

            def poll(self):
                return None

            def kill(self):
                events.append("kill")

            def wait(self):
                events.append("wait")

        class Owner:
            def __init__(self, process):
                self.process = process
                events.append("assign")

            def close(self):
                events.append("close")

        def popen(_argv, **kwargs):
            events.append(("spawn", kwargs["creationflags"]))
            return Process()

        with mock.patch.object(execution_engine.os, "name", "nt"), \
                mock.patch.object(execution_engine.subprocess, "Popen", popen), \
                mock.patch.object(execution_engine, "_WindowsJobOwner", Owner), \
                mock.patch.object(execution_engine, "_resume_windows_process",
                                  side_effect=lambda _process: events.append("resume")):
            _process, owner = spawn_owned_process(["worker"])
            owner.close()

        self.assertEqual(events[1:3], ["assign", "resume"])
        flags = events[0][1]
        self.assertTrue(flags & 0x00000004)
        self.assertTrue(flags & 0x00000200)
        self.assertEqual(events[-1], "close")

    def test_concurrent_cancel_interrupts_owned_process_and_cleans_state(self) -> None:
        entered = threading.Event()

        def runner(_task, cwd, context):
            entered.set()
            return context["run_process"](
                [sys.executable, "-c", "import time; time.sleep(30)"],
                cwd, 30,
            )

        engine = ExecutionEngine(self.state, runner)
        started = engine.start(self.task())
        step_result = []
        thread = threading.Thread(
            target=lambda: step_result.append(engine.step(started["token"])),
        )
        thread.start()
        self.assertTrue(entered.wait(2))
        cancelled = engine.cancel(started["token"])
        thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertEqual(step_result[0]["status"], "cancelled")
        self.assertFalse((self.state / started["token"]).exists())

    def test_gc_reclaims_a_provably_dead_owner_lock(self) -> None:
        engine = ExecutionEngine(self.state, lambda *_: {}, state_ttl_seconds=1)
        started = engine.start(self.task())
        session = self.state / started["token"]
        (session / "active.lock").write_text(
            '{"pid": 2147483647, "start_token": "gone"}', encoding="utf-8",
        )
        self.assertEqual(engine.gc(now=10**12), [started["token"]])

    def test_validation_rejects_legacy_or_unsafe_shapes(self) -> None:
        engine = ExecutionEngine(self.state, lambda *_: {})
        malformed = self.task()
        malformed["legacy"] = True
        with self.assertRaises(ExecutionError):
            engine.start(malformed)
        unsafe = self.task()
        unsafe["allowed_paths"] = ["../escape"]
        with self.assertRaises(ExecutionError):
            engine.start(unsafe)


if __name__ == "__main__":
    unittest.main()
