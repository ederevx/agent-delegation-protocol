#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
CTL = ROOT / "scripts" / "agents" / "delegationctl.py"
ADAPTER = ROOT / "scripts" / "agents" / "reference-adapter.py"
sys.path.insert(0, str(ROOT / "scripts" / "agents"))
from delegationctl import ProtocolError, load_catalog  # noqa: E402
from lane_service import Lane, LaneClient, _atomic_json, _auth  # noqa: E402


def task(task_id: str, prompt: str = "hello") -> dict:
    return {
        "schema_version": 2,
        "id": task_id,
        "mode": "read",
        "repo": str(ROOT),
        "prompt": prompt,
        "allowed_paths": [],
        "workspace": "shared",
        "validation": [],
        "budgets": {
            "timeout_seconds": 10,
            "max_output_bytes": 65536,
            "max_steps": 5,
        },
    }


def request(operation: str, tasks: list[dict]) -> dict:
    value = {
        "schema_version": 2,
        "route": "bulk",
        "runtime": "test",
        "platform": "linux",
        "function": "batch" if operation == "batch" else "audit",
        "mode": "read",
        "workspace": "shared",
    }
    value["tasks" if operation == "batch" else "task"] = (
        tasks if operation == "batch" else tasks[0]
    )
    return value


class AtomicJsonTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_without_fchmod(self) -> None:
        path = self.root / "atomic.json"
        with mock.patch("lane_service.os.fchmod", None, create=True):
            _atomic_json(path, {"status": "completed"}, 0o600)
        self.assertEqual(json.loads(path.read_text(encoding="utf-8")),
                         {"status": "completed"})

    @unittest.skipIf(os.name == "nt", "POSIX mode bits are not portable")
    def test_applies_posix_mode(self) -> None:
        path = self.root / "atomic.json"
        _atomic_json(path, {"status": "completed"}, 0o640)
        self.assertEqual(path.stat().st_mode & 0o777, 0o640)

    def test_closes_fd_when_fchmod_fails(self) -> None:
        descriptor: list[int] = []

        def fail_fchmod(fd: int, _mode: int) -> None:
            descriptor.append(fd)
            raise PermissionError("fchmod failed")

        path = self.root / "atomic.json"
        with mock.patch("lane_service.os.fchmod", side_effect=fail_fchmod,
                        create=True):
            with self.assertRaisesRegex(PermissionError, "fchmod failed"):
                _atomic_json(path, {"status": "completed"}, 0o600)
        self.assertEqual(len(descriptor), 1)
        with self.assertRaises(OSError):
            os.fstat(descriptor[0])
        self.assertFalse(path.exists())


class ProtocolV2(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.lane_state = self.root / "lane"
        self.adapter_state = self.root / "adapter"
        self.environment = {
            **os.environ,
            "DELEGATION_LANE_STATE_DIR": str(self.lane_state),
            "DELEGATION_V2_STATE": str(self.adapter_state),
        }
        self.server = subprocess.Popen(
            [sys.executable, str(CTL), "lane", "serve", "--state-dir",
             str(self.lane_state)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if (self.lane_state / "lane.json").exists():
                break
            if self.server.poll() is not None:
                self.fail(f"lane server failed: {self.server.stderr.read()}")
            time.sleep(0.02)
        else:
            self.fail("lane server did not publish its endpoint")

    def tearDown(self) -> None:
        self.server.terminate()
        try:
            self.server.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self.server.kill()
            self.server.wait(timeout=3)
        if self.server.stderr:
            self.server.stderr.close()
        self.temporary.cleanup()

    def runctl(self, *arguments: str, catalog: Path | None = None) -> subprocess.CompletedProcess:
        command = [sys.executable, str(CTL)]
        if catalog:
            command += ["--catalog", str(catalog)]
        command += list(arguments)
        return subprocess.run(command, text=True, capture_output=True,
                              env=self.environment, timeout=20)

    def write_request(self, value: dict, name: str = "request.json") -> Path:
        path = self.root / name
        path.write_text(json.dumps(value), encoding="utf-8")
        return path

    def external_catalog(self, kind: str = "session") -> Path:
        value = {
            "schema_version": 2,
            "backends": [{
                "id": "reference",
                "name": "Reference",
                "kind": kind,
                "priority": 50,
                "selector": {
                    "runtimes": ["test"],
                    "platforms": ["linux"],
                    "modes": ["read"],
                    "workspaces": ["shared"],
                    "functions": ["audit", "batch"],
                },
                "availability": {"commands": [sys.executable], "environment": []},
                "execution": {
                    "delivery": "json",
                    "argv": [sys.executable, str(ADAPTER)],
                    "timeout_seconds": 10,
                    "max_steps": 10,
                },
                "lane": {"id": "reference", "max_concurrency": 1,
                         "lease_seconds": 3},
            }],
            "routes": {"bulk": ["reference"]},
            "includes": [],
        }
        path = self.root / f"{kind}.json"
        path.write_text(json.dumps(value), encoding="utf-8")
        return path

    def native_catalog(self) -> Path:
        value = json.loads(
            (ROOT / "agents" / "protocol-v2.json").read_text(encoding="utf-8")
        )
        value["includes"] = []
        value["routes"]["bulk"] = [
            "native-codex-bulk", "native-claude-bulk",
        ]
        path = self.root / "native.json"
        path.write_text(json.dumps(value), encoding="utf-8")
        return path

    def test_catalog_validate_list_and_select(self) -> None:
        self.assertEqual(self.runctl("validate").returncode, 0)
        listed = json.loads(self.runctl("list").stdout)
        self.assertEqual(listed["status"], "completed")
        selected = self.runctl(
            "select", "--route", "bulk", "--runtime", "codex",
            "--platform", "linux", "--mode", "read", "--workspace",
            "shared", "--function", "audit", catalog=self.native_catalog(),
        )
        self.assertEqual(json.loads(selected.stdout)["id"], "native-codex-bulk")

    def test_catalog_rejects_backend_lane_ownership(self) -> None:
        value = json.loads((ROOT / "agents" / "protocol-v2.json").read_text())
        value["includes"] = []
        value["routes"]["bulk"] = [
            "native-codex-bulk", "native-claude-bulk",
        ]
        value["backends"][0]["lane"]["owner"] = "backend"
        path = self.root / "invalid.json"
        path.write_text(json.dumps(value), encoding="utf-8")
        with self.assertRaisesRegex(ProtocolError, "scheduler lane"):
            load_catalog(path)

    def test_native_run_returns_handoff_without_starting_lane(self) -> None:
        value = request("run", [task("native")])
        value["runtime"] = "codex"
        path = self.write_request(value)
        result = self.runctl(
            "run", "--request-file", str(path), catalog=self.native_catalog()
        )
        answer = json.loads(result.stdout)
        self.assertEqual(result.returncode, 69)
        self.assertEqual(answer["status"], "native_required")
        self.assertEqual(answer["task_ids"], ["native"])

    def test_oneshot_run_and_batch(self) -> None:
        catalog = self.external_catalog("oneshot")
        run_path = self.write_request(request("run", [task("one")]), "run.json")
        run = self.runctl("run", "--request-file", str(run_path), catalog=catalog)
        self.assertEqual(json.loads(run.stdout)["status"], "completed")
        batch_path = self.write_request(
            request("batch", [task("a"), task("b")]), "batch.json"
        )
        batch = self.runctl("batch", "--request-file", str(batch_path),
                            catalog=catalog)
        self.assertEqual([item["task_id"] for item in json.loads(batch.stdout)["results"]],
                         ["a", "b"])

    def test_session_run_and_round_robin_batch(self) -> None:
        catalog = self.external_catalog()
        run_path = self.write_request(request("run", [task("one")]), "run.json")
        answer = json.loads(self.runctl(
            "run", "--request-file", str(run_path), catalog=catalog
        ).stdout)
        self.assertEqual((answer["status"], answer["task_id"]),
                         ("completed", "one"))
        batch_path = self.write_request(
            request("batch", [task("a"), task("b")]), "batch.json"
        )
        answer = json.loads(self.runctl(
            "batch", "--request-file", str(batch_path), catalog=catalog
        ).stdout)
        self.assertEqual(answer["status"], "completed")
        self.assertEqual([item["task_id"] for item in answer["results"]],
                         ["a", "b"])

    def test_permission_pause_and_resume(self) -> None:
        catalog = self.external_catalog()
        run_path = self.write_request(
            request("run", [task("pause", "permission please")]), "run.json"
        )
        paused = json.loads(self.runctl(
            "run", "--request-file", str(run_path), catalog=catalog
        ).stdout)
        self.assertEqual(paused["status"], "permission_required")
        resume = {
            "schema_version": 2,
            "backend": "reference",
            "token": paused["token"],
            "resolution": {"decision": "allow"},
        }
        resume_path = self.write_request(resume, "resume.json")
        completed = json.loads(self.runctl(
            "resume", "--request-file", str(resume_path), catalog=catalog
        ).stdout)
        self.assertEqual(completed["status"], "completed")

    def test_lane_reentry_expiry_and_fifo(self) -> None:
        lane = Lane(1, lease_seconds=1)
        first = lane.acquire("first")
        self.assertEqual(lane.acquire("first", first), first)
        result: list[str] = []

        def waiter() -> None:
            result.append(lane.acquire("second", timeout_seconds=3))

        thread = threading.Thread(target=waiter)
        thread.start()
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline and lane.status()["queued"] != 1:
            time.sleep(0.01)
        self.assertEqual(lane.status()["queued"], 1)
        self.assertTrue(lane.release("first", first))
        thread.join(timeout=2)
        self.assertEqual(len(result), 1)
        time.sleep(1.05)
        self.assertFalse(lane.release("second", result[0]))

    def test_loopback_authentication_and_status(self) -> None:
        client = LaneClient(self.lane_state / "lane.json")
        self.assertEqual(client.request("status")["status"], "completed")
        descriptor = json.loads((self.lane_state / "lane.json").read_text())
        with socket.create_connection((descriptor["host"], descriptor["port"])) as raw:
            raw.sendall(b'{"operation":"status","auth":"wrong"}\n')
            response = raw.makefile("rb").readline()
        self.assertEqual(json.loads(response)["status"], "unauthorized")
        replay = {
            "schema_version": 2,
            "operation": "status",
            "timestamp": time.time(),
            "nonce": "one-use-nonce",
        }
        secret = Path(descriptor["secret_file"]).read_bytes()
        replay["auth"] = _auth(secret, replay)

        def raw_request(value: dict) -> dict:
            with socket.create_connection((descriptor["host"], descriptor["port"])) as raw:
                raw.sendall((json.dumps(value) + "\n").encode())
                return json.loads(raw.makefile("rb").readline())

        self.assertEqual(raw_request(replay)["status"], "completed")
        self.assertEqual(raw_request(replay)["status"], "unauthorized")

    def test_unavailable_backend_is_not_selected(self) -> None:
        catalog = json.loads(self.external_catalog().read_text())
        catalog["backends"][0]["availability"]["commands"] = [
            "delegation-v2-definitely-missing-command"
        ]
        path = self.root / "unavailable.json"
        path.write_text(json.dumps(catalog), encoding="utf-8")
        selected = self.runctl(
            "select", "--route", "bulk", "--runtime", "test",
            "--platform", "linux", "--mode", "read", "--workspace",
            "shared", "--function", "audit", catalog=path,
        )
        self.assertEqual(json.loads(selected.stdout)["classification"], "no_backend")


if __name__ == "__main__":
    unittest.main()
