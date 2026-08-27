#!/usr/bin/env python3
"""Focused self-tests for the agent catalog, multiplexer, and adapter template."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
MUX = HERE / "multiplexer.py"
ADAPTER = HERE / "custom-adapter-template.py"


def metadata(agent_id: str, argv: list[str] | None = None, *, native: bool = False,
             runtime: str = "codex", concurrency: int = 1) -> dict[str, object]:
    binding: dict[str, object]
    if native:
        binding = {"runtime": runtime, "agent_type": "bulk_worker", "reasoning_effort": "medium"}
    else:
        binding = {
            "argv": argv, "max_input_bytes": 1048576,
            "max_output_bytes": 2097152, "timeout_seconds": 10,
        }
    return {
        "schema_version": 1, "id": agent_id, "name": agent_id,
        "description": "test agent", "native": native, "provider": "test",
        "model": "test-model", "binding": binding,
        "capabilities": {
            "functions": ["audit", "edit"],
            "runtimes": [runtime], "platforms": ["linux"],
            "modes": ["read", "edit"], "workspaces": ["shared", "isolated"],
            "deliveries": ["native-agent" if native else "json-receipt"],
        },
        "limits": {"max_concurrency": concurrency},
    }


class MultiplexerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="multiplexer-test-")
        self.root = Path(self.temp.name)
        self.catalog = self.root / "catalog"
        self.catalog.mkdir()
        self.routes = self.root / "routes.json"
        self.state = self.root / "state"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def write_agent(self, value: dict[str, object]) -> None:
        (self.catalog / f"{value['id']}.json").write_text(json.dumps(value), encoding="utf-8")

    def write_routes(self, members: list[str]) -> None:
        self.routes.write_text(json.dumps({
            "schema_version": 1, "routes": {"bulk": members},
        }), encoding="utf-8")

    def run_mux(self, *args: str, task: object | None = None) -> subprocess.CompletedProcess[str]:
        command = [sys.executable, str(MUX), "--catalog", str(self.catalog),
                   "--routes", str(self.routes), *args]
        env = os.environ.copy()
        env["AGENT_MULTIPLEXER_STATE_DIR"] = str(self.state)
        return subprocess.run(
            command, input=None if task is None else json.dumps(task), text=True,
            capture_output=True, env=env, check=False,
        )

    def make_stub(self, name: str, body: str) -> Path:
        path = self.root / name
        path.write_text(body, encoding="utf-8")
        return path

    def test_validation_and_missing_route_id_are_deterministic(self) -> None:
        self.write_agent(metadata("valid", [sys.executable, "unused.py"]))
        self.write_routes(["missing"])
        result = self.run_mux("validate")
        self.assertEqual(result.returncode, 64)
        receipt = json.loads(result.stdout)
        self.assertEqual(receipt["classification"], "configuration_error")
        self.assertIn("missing agent ids: missing", receipt["error"])

    def test_unknown_metadata_field_is_rejected(self) -> None:
        value = metadata("invalid", [sys.executable, "unused.py"])
        value["surprise"] = True
        self.write_agent(value)
        self.write_routes(["invalid"])
        result = self.run_mux("validate")
        self.assertEqual(result.returncode, 64)
        self.assertIn("unknown fields: surprise", json.loads(result.stdout)["error"])

    def test_priority_skips_unavailable_and_filters_capabilities(self) -> None:
        self.write_agent(metadata("missing", ["definitely-not-an-installed-worker"] ))
        self.write_agent(metadata("native", native=True))
        self.write_routes(["missing", "native"])
        result = self.run_mux("select", "--route", "bulk", "--runtime", "codex",
                              "--mode", "edit", "--workspace", "isolated",
                              "--delivery", "native-agent")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["id"], "native")

    def test_required_functions_filter_before_priority(self) -> None:
        first = metadata("first", [sys.executable, "unused.py"])
        first["capabilities"]["functions"] = ["audit"]
        second = metadata("second", native=True)
        self.write_agent(first)
        self.write_agent(second)
        self.write_routes(["first", "second"])
        result = self.run_mux("select", "--route", "bulk", "--runtime", "codex",
                              "--require", "edit")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["id"], "second")

    def test_command_execution_passes_json_and_parses_receipt(self) -> None:
        stub = self.make_stub("stub.py", """
import json, sys
task = json.load(sys.stdin)
print(json.dumps({"schema_version": 1, "classification": "success", "seen": task}))
""")
        self.write_agent(metadata("custom", [sys.executable, str(stub)]))
        self.write_routes(["custom"])
        task = {"prompt": "audit", "mode": "read"}
        result = self.run_mux("run", "--route", "bulk", "--runtime", "codex", task=task)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["seen"], task)

    def test_backend_is_not_replayed_after_launch(self) -> None:
        marker = self.root / "second-ran"
        first = self.make_stub("first.py", "import sys\nsys.stdin.read()\nprint('not-json')\n")
        second = self.make_stub("second.py", f"from pathlib import Path\nPath({str(marker)!r}).touch()\n")
        self.write_agent(metadata("first", [sys.executable, str(first)]))
        self.write_agent(metadata("second", [sys.executable, str(second)]))
        self.write_routes(["first", "second"])
        result = self.run_mux("run", "--route", "bulk", "--runtime", "codex",
                              task={"prompt": "audit", "mode": "read"})
        self.assertEqual(result.returncode, 65)
        self.assertEqual(json.loads(result.stdout)["classification"], "invalid_receipt")
        self.assertFalse(marker.exists())

    def test_backend_output_limit_is_enforced(self) -> None:
        stub = self.make_stub(
            "large.py", "import sys\nsys.stdin.read()\nprint('x' * 4096)\n",
        )
        value = metadata("large", [sys.executable, str(stub)])
        value["binding"]["max_output_bytes"] = 128
        self.write_agent(value)
        self.write_routes(["large"])
        result = self.run_mux("run", "--route", "bulk", "--runtime", "codex",
                              task={"prompt": "audit", "mode": "read"})
        self.assertEqual(result.returncode, 65)
        self.assertIn("receipt exceeds 128 bytes", result.stdout)

    def test_input_limit_skips_to_later_backend_before_launch(self) -> None:
        first = metadata("small", [sys.executable, "unused.py"])
        first["binding"]["max_input_bytes"] = 32
        stub = self.make_stub(
            "accept.py",
            "import json, sys\njson.load(sys.stdin)\nprint(json.dumps({'classification': 'success'}))\n",
        )
        self.write_agent(first)
        self.write_agent(metadata("large", [sys.executable, str(stub)]))
        self.write_routes(["small", "large"])
        result = self.run_mux("run", "--route", "bulk", "--runtime", "codex",
                              task={"prompt": "a" * 100, "mode": "read"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["classification"], "success")

    def test_launch_failure_can_fall_back_before_task_starts(self) -> None:
        broken = self.make_stub("not-executable-format", "this is not an executable format\n")
        broken.chmod(0o700)
        self.write_agent(metadata("broken", [str(broken)]))
        self.write_agent(metadata("native", native=True))
        self.write_routes(["broken", "native"])
        result = self.run_mux("run", "--route", "bulk", "--runtime", "codex",
                              task={"prompt": "audit", "mode": "read"})
        self.assertEqual(result.returncode, 69)
        self.assertEqual(json.loads(result.stdout)["classification"], "native_required")

    def test_native_run_returns_exact_handoff_status(self) -> None:
        self.write_agent(metadata("native", native=True))
        self.write_routes(["native"])
        result = self.run_mux("run", "--route", "bulk", "--runtime", "codex",
                              task={"prompt": "unused", "mode": "read"})
        self.assertEqual(result.returncode, 69)
        receipt = json.loads(result.stdout)
        self.assertEqual(receipt["classification"], "native_required")
        self.assertEqual(receipt["status"], "native_required")
        self.assertEqual(receipt["backend"], "native")

    def test_single_lane_lock_serializes_processes(self) -> None:
        events = self.root / "events"
        stub = self.make_stub("slow.py", f"""
import json, sys, time
from pathlib import Path
p = Path({str(events)!r})
with p.open("a") as f: f.write("start\\n")
time.sleep(0.25)
with p.open("a") as f: f.write("end\\n")
json.load(sys.stdin)
print(json.dumps({{"schema_version": 1, "classification": "success"}}))
""")
        self.write_agent(metadata("serial", [sys.executable, str(stub)]))
        self.write_routes(["serial"])
        command = [sys.executable, str(MUX), "--catalog", str(self.catalog),
                   "--routes", str(self.routes), "run", "--route", "bulk",
                   "--runtime", "codex"]
        env = os.environ.copy()
        env["AGENT_MULTIPLEXER_STATE_DIR"] = str(self.state)
        processes = [subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                      stderr=subprocess.PIPE, text=True, env=env) for _ in range(2)]
        for process in processes:
            process.communicate(json.dumps({"prompt": "audit", "mode": "read"}), timeout=5)
            self.assertEqual(process.returncode, 0)
        self.assertEqual(events.read_text(encoding="utf-8").splitlines(),
                         ["start", "end", "start", "end"])


class AdapterTemplateTests(unittest.TestCase):
    def invoke(self, task: object) -> subprocess.CompletedProcess[str]:
        return subprocess.run([sys.executable, str(ADAPTER)], input=json.dumps(task),
                              text=True, capture_output=True, check=False)

    def test_single_manifest_returns_bounded_not_configured_receipt(self) -> None:
        result = self.invoke({"id": "one", "prompt": "audit", "mode": "read"})
        self.assertEqual(result.returncode, 1)
        receipt = json.loads(result.stdout)
        self.assertEqual(receipt["classification"], "backend_error")
        self.assertEqual(receipt["task_id"], "one")

    def test_batch_is_sequential_and_honors_stop_on_error(self) -> None:
        result = self.invoke({
            "tasks": [
                {"id": "one", "prompt": "audit", "mode": "read"},
                {"id": "two", "prompt": "edit", "mode": "edit"},
            ],
            "stop_on_error": True,
        })
        self.assertEqual(result.returncode, 1)
        receipt = json.loads(result.stdout)
        self.assertTrue(receipt["sequential"])
        self.assertEqual(receipt["counts"], {
            "requested": 2, "completed": 1, "succeeded": 0, "failed": 1, "skipped": 1,
        })


if __name__ == "__main__":
    unittest.main()
