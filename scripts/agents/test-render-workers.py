#!/usr/bin/env python3
"""Regression checks for worker budgets and generated host contracts."""
from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path
import tomllib
import unittest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    "worker_renderer", ROOT / "scripts/agents/render-bulk-workers.py"
)
assert spec and spec.loader
renderer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(renderer)


def profile(tier: str, host: str = "claude") -> dict:
    return json.loads((ROOT / f"agents/{tier}-worker-profiles.json").read_text())["profiles"][host]


class WorkerRenderingTests(unittest.TestCase):
    def test_native_and_advisory_budgets_use_common_policy(self):
        expected = {"quick": 128, "bulk": 64, "balanced": 32, "frontier": 16}
        outputs = renderer.rendered_outputs()
        for tier, limit in expected.items():
            for host in ("claude", "codex"):
                with self.subTest(tier=tier, host=host):
                    output = profile(tier, host)["output"]
                    text = outputs[ROOT / output["path"]]
                    self.assertEqual(text.count(renderer.ROUTING_POLICY), 1)
                    if host == "claude":
                        self.assertIn(f"maxTurns: {limit}\n", text)
                        self.assertIn(f"Native agentic-turn limit: {limit}", text)
                    else:
                        parsed = tomllib.loads(text)
                        self.assertNotIn("max_turns", parsed)
                        self.assertNotIn("maxTurns", parsed)
                        self.assertIn(f"Advisory agentic-turn budget: {limit}", text)
                        self.assertIn(f"hard budget of {limit} PreToolUse tool-call attempts", text)

    def test_execution_tiers_keep_execution_and_quick_cannot_delegate(self):
        for tier in ("frontier", "balanced", "bulk", "quick"):
            output = profile(tier)["output"]
            text = renderer.render_claude("body", "description", output)
            self.assertIn("Write, Edit, Bash", text)
        for delegation_tool in ("Agent", "Task", "functions.spawn_agent"):
            output = copy.deepcopy(profile("quick")["output"])
            output["tools"].append(delegation_tool)
            with self.subTest(tool=delegation_tool), self.assertRaisesRegex(ValueError, "delegation tool"):
                renderer.render_claude("body", "description", output)

    def test_unknown_roles_fail_for_both_hosts(self):
        for host, render in (("claude", renderer.render_claude), ("codex", renderer.render_codex)):
            output = copy.deepcopy(profile("frontier", host)["output"])
            output["name"] = "frontier-workre"
            with self.subTest(host=host), self.assertRaisesRegex(ValueError, "unknown worker"):
                render("body", "description", output)

    def test_generated_outputs_are_current_and_have_no_execution_prohibitions(self):
        outputs = renderer.rendered_outputs()
        self.assertEqual(len(outputs), 8)
        for path, generated in outputs.items():
            with self.subTest(path=path):
                self.assertEqual(path.read_text(), generated)
                if path.suffix == ".toml":
                    parsed = tomllib.loads(generated)
                    self.assertIn(parsed["model_reasoning_effort"], ("low", "medium", "high", "xhigh"))
                self.assertNotIn("Do not edit files", generated)
                self.assertNotIn("analysis and lower-tier delegation only", generated)
                self.assertNotIn("matched native balanced-worker completion", generated)
                if "frontier" in path.name:
                    self.assertIn("Analyze, implement, run commands and tests", generated)
                if "balanced" in path.name:
                    self.assertIn("Prefer analysis", generated)


if __name__ == "__main__":
    unittest.main()
