#!/usr/bin/env python3
"""Render host bulk-worker definitions from the common semantic contract."""
from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sys
import tomllib
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
CLASSIFIER_PATH = Path(__file__).resolve().parent / "delegation-classifier.py"


def _load_classifier() -> Any:
    specification = importlib.util.spec_from_file_location(
        "protocol_v2_classifier", CLASSIFIER_PATH
    )
    if not specification or not specification.loader:
        raise RuntimeError(f"cannot load classifier module: {CLASSIFIER_PATH}")
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


_CLASSIFIER = _load_classifier()
AGENT_TOOL_NAME = _CLASSIFIER.AGENT_TOOL_NAME
WORKER_TIER_RANK = _CLASSIFIER.WORKER_TIER_RANK
WORKER_TURN_LIMITS = _CLASSIFIER.WORKER_TURN_LIMITS
ROUTING_POLICY = _CLASSIFIER.ROUTING_POLICY
TEMPLATE_PATH = REPO_ROOT / "agents" / "bulk-worker-common.md.tmpl"
PROFILES_PATH = REPO_ROOT / "agents" / "bulk-worker-profiles.json"
QUICK_PROFILES_PATH = REPO_ROOT / "agents" / "quick-worker-profiles.json"
BALANCED_TEMPLATE_PATH = REPO_ROOT / "agents" / "balanced-worker-common.md.tmpl"
BALANCED_PROFILES_PATH = REPO_ROOT / "agents" / "balanced-worker-profiles.json"
FRONTIER_PROFILES_PATH = REPO_ROOT / "agents" / "frontier-worker-profiles.json"
TOKEN = re.compile(r"{{([A-Z_]+)}}")


def paragraph_text(value: Any, name: str) -> str:
    if not isinstance(value, list) or not value or not all(isinstance(item, str) and item for item in value):
        raise ValueError(f"profile field {name!r} must be a non-empty string list")
    return "\n\n".join(value)


def render_body(template: str, profile: dict[str, Any]) -> str:
    replacements = {
        "HOST_NAME": profile["host_name"],
        "PARENT_CHANNEL": profile["parent_channel"],
        "ROUTING_POLICY": ROUTING_POLICY,
        "RUNTIME_CONTRACT": runtime_contract(profile["output"]),
        "SCOPE_CONTRACT": paragraph_text(profile["scope_contract"], "scope_contract"),
        "CONFLICT_CONTRACT": paragraph_text(profile["conflict_contract"], "conflict_contract"),
        "LIFECYCLE_CONTRACT": paragraph_text(profile["lifecycle_contract"], "lifecycle_contract"),
    }
    expected = set(TOKEN.findall(template))
    if expected != set(replacements):
        raise ValueError(f"template/profile token mismatch: expected={sorted(expected)} actual={sorted(replacements)}")
    body = TOKEN.sub(lambda match: replacements[match.group(1)], template).rstrip() + "\n"
    if TOKEN.search(body):
        raise ValueError("unresolved template token")
    return body


def render_balanced_body(template: str, profile: dict[str, Any]) -> str:
    replacements = {
        "SCOPE_CONTRACT": paragraph_text(profile["scope_contract"], "scope_contract"),
        "PARENT_CHANNEL": profile["parent_channel"],
        "ROUTING_POLICY": ROUTING_POLICY,
        "RUNTIME_CONTRACT": runtime_contract(profile["output"]),
        "LIFECYCLE_CONTRACT": paragraph_text(
            profile["lifecycle_contract"], "lifecycle_contract"
        ),
    }
    expected = set(TOKEN.findall(template))
    if expected != set(replacements):
        raise ValueError(
            f"balanced template/profile token mismatch: expected={sorted(expected)} "
            f"actual={sorted(replacements)}"
        )
    body = TOKEN.sub(lambda match: replacements[match.group(1)], template).rstrip() + "\n"
    if TOKEN.search(body):
        raise ValueError("unresolved balanced template token")
    return body


def worker_turn_limit(output: dict[str, Any]) -> int:
    tier = _CLASSIFIER.canonical_worker_name(output["name"])
    if tier is None or tier not in WORKER_TURN_LIMITS:
        raise ValueError(f"unknown worker profile: {output['name']!r}")
    limit = WORKER_TURN_LIMITS[tier]
    if type(limit) is not int or limit <= 0:
        raise ValueError(f"invalid worker turn limit: {limit!r}")
    return limit


def runtime_contract(output: dict[str, Any]) -> str:
    limit = worker_turn_limit(output)
    if output["format"] == "claude-markdown":
        return (
            f"Native agentic-turn limit: {limit} (maxTurns). Return useful results "
            "and remaining work before exhausting the budget."
        )
    return (
        f"Advisory agentic-turn budget: {limit}. Codex has no native per-worker "
        f"turn-limit field. ADP separately enforces a hard budget of {limit} "
        "PreToolUse tool-call attempts per identified worker lifetime, including attempts later denied; tool calls are "
        "not agentic turns. Return useful results and remaining work before "
        "exhausting either budget."
    )


def render_claude(body: str, description: str, output: dict[str, Any]) -> str:
    limit = worker_turn_limit(output)
    title = output["name"].replace("-", " ").title()
    tools = output.get("tools")
    if tools is not None and (not isinstance(tools, list) or not tools or
                               not all(isinstance(item, str) and item for item in tools)):
        raise ValueError("output field 'tools', when present, must be a non-empty string list")
    # The lowest worker tier must never be able to spawn further subagents at
    # all (see WORKER_TIER_RANK / WORKER_TIERS in delegation-classifier.py),
    # so the delegation tool itself is rejected here for that tier
    # regardless of what its profile lists. A higher tier (e.g.
    # balanced-worker) may legitimately list it, since the hook-level tier
    # check enforces the strictly-lower-tier rule at call time instead.
    is_lowest_tier = WORKER_TIER_RANK[_CLASSIFIER.canonical_worker_name(output["name"])] == 1
    if is_lowest_tier and tools is not None and any(
        AGENT_TOOL_NAME.match(name.strip()) for name in tools
    ):
        raise ValueError(
            f"output field 'tools' for lowest-tier profile {output['name']!r} "
            "must not include the delegation tool (Agent/Task)"
        )
    tools_line = f"tools: {', '.join(tools)}\n" if tools else ""
    return (
        "---\n"
        f"name: {output['name']}\n"
        f"description: {description}\n"
        f"model: {output['model']}\n"
        f"effort: {output['effort']}\n"
        f"maxTurns: {limit}\n"
        f"{tools_line}"
        "---\n\n"
        "<!-- Generated by scripts/agents/render-bulk-workers.py; do not edit directly. -->\n\n"
        f"# {title}\n\n"
        + body
    )


def toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def render_codex(body: str, description: str, output: dict[str, Any]) -> str:
    worker_turn_limit(output)
    if '"""' in body or "\\" in body:
        raise ValueError("Codex body contains a sequence unsafe for a TOML multiline basic string")
    rendered = (
        "# Generated by scripts/agents/render-bulk-workers.py; do not edit directly.\n"
        f"name = {toml_string(output['name'])}\n"
        f"description = {toml_string(description)}\n"
        f"model = {toml_string(output['model'])}\n"
        f"model_reasoning_effort = {toml_string(output['reasoning_effort'])}\n\n"
        "developer_instructions = \"\"\"\n"
        + body
        + "\"\"\"\n"
    )
    tomllib.loads(rendered)
    return rendered


def rendered_outputs() -> dict[Path, str]:
    outputs: dict[Path, str] = {}
    sources = (
        (TEMPLATE_PATH, PROFILES_PATH, 2, render_body),
        (TEMPLATE_PATH, QUICK_PROFILES_PATH, 2, render_body),
        (BALANCED_TEMPLATE_PATH, BALANCED_PROFILES_PATH, 2, render_balanced_body),
        (BALANCED_TEMPLATE_PATH, FRONTIER_PROFILES_PATH, 2, render_balanced_body),
    )
    for template_path, profiles_path, schema_version, body_renderer in sources:
        template = template_path.read_text(encoding="utf-8")
        source = json.loads(profiles_path.read_text(encoding="utf-8"))
        if source.get("schema_version") != schema_version:
            raise ValueError(f"unsupported worker profile schema: {profiles_path}")
        description = source["description"]
        for profile in source["profiles"].values():
            output = profile["output"]
            body = body_renderer(template, profile)
            if output["format"] == "claude-markdown":
                text = render_claude(body, description, output)
            elif output["format"] == "codex-toml":
                text = render_codex(body, description, output)
            else:
                raise ValueError(f"unsupported output format: {output['format']}")
            relative_path = Path(output["path"])
            if relative_path.is_absolute() or ".." in relative_path.parts:
                raise ValueError(f"output path must stay inside the repository: {relative_path}")
            path = REPO_ROOT / relative_path
            if path in outputs:
                raise ValueError(f"duplicate output path: {path}")
            outputs[path] = text
    return outputs


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="fail if generated workers are stale")
    args = parser.parse_args()
    stale: list[Path] = []
    for path, expected in rendered_outputs().items():
        if args.check:
            if not path.is_file() or path.read_text(encoding="utf-8") != expected:
                stale.append(path)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(expected, encoding="utf-8", newline="\n")
            print(path.relative_to(REPO_ROOT))
    if stale:
        for path in stale:
            print(f"stale generated worker: {path.relative_to(REPO_ROOT)}", file=sys.stderr)
        print("run scripts/agents/render-bulk-workers.py", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
