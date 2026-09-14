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
    return profile_document(tier)["profiles"][host]


def profile_document(tier: str) -> dict:
    return json.loads((ROOT / "agents/worker-profiles.json").read_text())["tiers"][tier]


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

    def test_all_tiers_inherit_host_tool_access_without_scope_contracts(self):
        outputs = renderer.rendered_outputs()
        for tier in ("frontier", "balanced", "bulk", "quick"):
            source = profile_document(tier)
            self.assertNotIn("scope_contract", source)
            for host in ("claude", "codex"):
                with self.subTest(tier=tier, host=host):
                    output = source["profiles"][host]["output"]
                    self.assertNotIn("tools", output)
                    self.assertNotIn("disallowedTools", output)
                    text = outputs[ROOT / output["path"]]
                    self.assertNotIn("## Scope", text)
                    if host == "claude":
                        self.assertNotIn("tools:", text)
                        self.assertNotIn("disallowedTools:", text)
                    else:
                        self.assertNotIn("tools", tomllib.loads(text))

    def test_tier_models_and_efforts_remain_configured(self):
        expected = {
            "quick": {"claude": ("haiku", "low"), "codex": ("gpt-5.6-luna", "low")},
            "bulk": {"claude": ("sonnet", "medium"), "codex": ("gpt-5.6-terra", "medium")},
            "balanced": {"claude": ("opus", "high"), "codex": ("gpt-5.6-sol", "high")},
            "frontier": {"claude": ("fable", "xhigh"), "codex": ("gpt-6-astra", "xhigh")},
        }
        for tier, hosts in expected.items():
            for host, (model, effort) in hosts.items():
                with self.subTest(tier=tier, host=host):
                    output = profile(tier, host)["output"]
                    self.assertEqual(output["model"], model)
                    self.assertEqual(
                        output["effort"] if host == "claude" else output["reasoning_effort"],
                        effort,
                    )

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


if __name__ == "__main__":
    unittest.main()
