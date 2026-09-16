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


def worker(tier: str, host: str = "claude") -> dict:
    return profile_document(tier)["profiles"][host]


def output(tier: str, host: str = "claude") -> dict:
    source = json.loads((ROOT / "agents/worker-profiles.json").read_text())
    return renderer.profile_output(source["hosts"][host], worker(tier, host))


def profile_document(tier: str) -> dict:
    return json.loads((ROOT / "agents/worker-profiles.json").read_text())["tiers"][tier]


class WorkerRenderingTests(unittest.TestCase):
    def test_native_and_advisory_budgets_use_common_policy(self):
        expected = {"quick": 128, "bulk": 64, "balanced": 32, "frontier": 16}
        outputs = renderer.rendered_outputs()
        for tier, limit in expected.items():
            for host in ("claude", "codex", "pi"):
                with self.subTest(tier=tier, host=host):
                    rendered = output(tier, host)
                    text = outputs[ROOT / rendered["path"]]
                    self.assertEqual(text.count(renderer.ROUTING_POLICY), 1)
                    self.assertIn("remaining work before exhausting the budget", text)
                    if host == "claude":
                        self.assertIn(f"maxTurns: {limit}\n", text)
                        self.assertIn(f"native agentic-turn limit is {limit} through maxTurns", text)
                    elif host == "pi":
                        self.assertIn(f"maxTurns: {limit}\n", text)
                        self.assertIn(f"native budget of {limit} agentic turns", text)
                        self.assertIn("stops the worker at the limit", text)
                        self.assertNotIn("no native per-worker turn-limit field", text)
                        self.assertIn(f"enforces {limit} tool-call attempts", text)
                    else:
                        parsed = tomllib.loads(text)
                        self.assertNotIn("max_turns", parsed)
                        self.assertNotIn("maxTurns", parsed)
                        self.assertIn(f"advisory budget of {limit} agentic turns", text)
                        self.assertIn(f"enforces {limit} PreToolUse tool-call attempts", text)

    def test_all_tiers_use_one_common_worker_contract(self):
        source = json.loads((ROOT / "agents/worker-profiles.json").read_text())
        self.assertEqual(source["template"], "agents/worker-common.md.tmpl")
        self.assertEqual(set(source["hosts"]), {"claude", "codex", "pi"})
        self.assertEqual(
            source["hosts"]["claude"]["output"],
            {"format": "claude-markdown"},
        )
        self.assertEqual(
            source["hosts"]["codex"]["output"],
            {"format": "codex-toml"},
        )
        for tier in source["tiers"].values():
            self.assertNotIn("template", tier)
            self.assertNotIn("body_renderer", tier)
            for worker_profile in tier["profiles"].values():
                self.assertNotIn("parent_channel", worker_profile)
                self.assertNotIn("lifecycle_contract", worker_profile)
                self.assertNotIn("escalation_contract", worker_profile)
                self.assertNotIn("output", worker_profile)

    def test_all_tiers_inherit_host_tool_access_without_scope_contracts(self):
        outputs = renderer.rendered_outputs()
        for tier in ("frontier", "balanced", "bulk", "quick"):
            source = profile_document(tier)
            self.assertNotIn("scope_contract", source)
            for host in ("claude", "codex", "pi"):
                with self.subTest(tier=tier, host=host):
                    rendered = output(tier, host)
                    self.assertNotIn("tools", rendered)
                    self.assertNotIn("disallowedTools", rendered)
                    text = outputs[ROOT / rendered["path"]]
                    self.assertNotIn("## Scope", text)
                    if host == "claude":
                        self.assertNotIn("tools:", text)
                        self.assertNotIn("disallowedTools:", text)
                    elif host == "pi":
                        self.assertNotIn("tools:", text)
                        self.assertNotIn("disallowedTools:", text)
                    else:
                        self.assertNotIn("tools", tomllib.loads(text))

    def test_tier_models_and_efforts_remain_configured(self):
        outputs = renderer.rendered_outputs()
        expected = {
            "quick": {"claude": ("haiku", "low"), "codex": ("gpt-5.6-luna", "low"),
                      "pi": ("low",)},
            "bulk": {"claude": ("sonnet", "medium"), "codex": ("gpt-5.6-terra", "medium"),
                     "pi": ("medium",)},
            "balanced": {"claude": ("opus", "high"), "codex": ("gpt-5.6-sol", "high"),
                         "pi": ("high",)},
            "frontier": {"claude": ("fable", "xhigh"), "codex": ("gpt-6-astra", "xhigh"),
                         "pi": ("xhigh",)},
        }
        for tier, hosts in expected.items():
            for host, values in hosts.items():
                with self.subTest(tier=tier, host=host):
                    rendered = output(tier, host)
                    if host == "pi":
                        # Pi pins no model and inherits the parent's session
                        # model; the profile's thinking directive is the tier
                        # effort on this host.
                        (thinking,) = values
                        self.assertNotIn("model", rendered)
                        self.assertEqual(rendered["thinking"], thinking)
                        self.assertNotIn("effort", rendered)
                        self.assertNotIn("reasoning_effort", rendered)
                        text = outputs[ROOT / rendered["path"]]
                        frontmatter = text.split("---\n\n", 1)[0]
                        self.assertNotIn("\nmodel:", frontmatter)
                        self.assertIn(
                            "Pi worker profiles pin no model: this tier inherits "
                            "the parent's session model. Reasoning effort "
                            f"`{thinking}` is this tier's MINIMUM: apply at least "
                            "this level, and if your model's effort ladder differs, "
                            "choose the closest supported level at or above the "
                            "minimum (see the ADP rules' effort-ladder guideline). "
                            "Never apply less.",
                            text,
                        )
                    else:
                        model, effort = values
                        self.assertEqual(rendered["model"], model)
                        self.assertEqual(
                            rendered["effort"] if host == "claude" else rendered["reasoning_effort"],
                            effort,
                        )

    def test_unknown_roles_fail_for_all_hosts(self):
        for host, render in (("claude", renderer.render_claude),
                             ("codex", renderer.render_codex),
                             ("pi", renderer.render_pi)):
            rendered = copy.deepcopy(output("frontier", host))
            rendered["name"] = "frontier-workre"
            with self.subTest(host=host), self.assertRaisesRegex(ValueError, "unknown worker"):
                render("body", "description", rendered)

    def test_generated_outputs_are_current_and_have_no_execution_prohibitions(self):
        outputs = renderer.rendered_outputs()
        self.assertEqual(len(outputs), 12)
        self.assertEqual(
            {path.relative_to(ROOT) for path in outputs},
            {
                Path("claude/agents/quick-worker.md"),
                Path("claude/agents/bulk-worker.md"),
                Path("claude/agents/balanced-worker.md"),
                Path("claude/agents/frontier-worker.md"),
                Path("codex/agents/quick_worker.toml"),
                Path("codex/agents/bulk_worker.toml"),
                Path("codex/agents/balanced-worker.toml"),
                Path("codex/agents/frontier_worker.toml"),
                Path("pi/agents/quick-worker.md"),
                Path("pi/agents/bulk-worker.md"),
                Path("pi/agents/balanced-worker.md"),
                Path("pi/agents/frontier-worker.md"),
            },
        )
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
