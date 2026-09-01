#!/usr/bin/env python3
"""Audit commit-message conventions on one or more Git refs."""
from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path
from typing import Any

TRAILER = re.compile(r"^(?P<key>[A-Za-z][A-Za-z0-9-]*): (?P<value>.+)$")
FORBIDDEN = {"Co-authored-by", "Generated-by", "Reviewed-by"}
AUTHOR = "Edrick Sinsuan <evcsinsuan@gmail.com>"
SIGNOFF = "Edrick Sinsuan <evcsinsuan@gmail.com>"
ASSISTANT_IDENTITY = re.compile(
    r"^(?:Codex|Claude-Code):[A-Za-z0-9][A-Za-z0-9._-]*$"
)


def git(repo: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True)


def commit_ids(repo: Path, refs: list[str]) -> list[str]:
    # Git's topo ordering is deterministic for fixed refs and preserves the
    # ancestry order needed when comparing an audit ledger across rewrites.
    return git(repo, "rev-list", "--topo-order", "--reverse", *refs).split()


def parse_message(message: str) -> tuple[str, list[str], list[tuple[str, str]], list[str]]:
    lines = message.rstrip("\n").splitlines()
    subject = lines[0] if lines else ""
    end = len(lines)
    while end and not lines[end - 1].strip():
        end -= 1
    first = end
    cursor = end - 1
    saw_trailer = False
    while cursor >= 1:
        if TRAILER.match(lines[cursor]):
            first = cursor
            saw_trailer = True
        elif saw_trailer and not lines[cursor].strip():
            pass
        else:
            break
        cursor -= 1
    block = lines[first:end]
    trailers: list[tuple[str, str]] = []
    for line in block:
        match = TRAILER.match(line)
        if match:
            trailers.append((match.group("key"), match.group("value")))
    return subject, lines[1:first], trailers, block


def inspect(repo: Path, oid: str) -> dict[str, Any]:
    raw = git(repo, "cat-file", "-p", oid)
    header, message = raw.split("\n\n", 1)
    author_field = next((line[7:] for line in header.splitlines()
                        if line.startswith("author ")), "")
    author_match = re.match(r"^.+ <[^>]+>", author_field)
    author_line = author_match.group(0) if author_match else author_field
    subject, body, trailers, block = parse_message(message)
    issues: list[str] = []
    if author_line != AUTHOR:
        issues.append("author")
    keys = [key for key, _ in trailers]
    if SIGNOFF not in [value for key, value in trailers if key == "Signed-off-by"]:
        issues.append("signoff")
    if any(key in FORBIDDEN for key in keys):
        issues.append("forbidden-trailer")
    assistants = [value for key, value in trailers if key == "Assisted-by"]
    if not assistants:
        issues.append("missing-assistant")
    if any(not ASSISTANT_IDENTITY.fullmatch(value) for value in assistants):
        issues.append("assistant-identity")
    if len(assistants) != len(set(assistants)):
        issues.append("duplicate-assistant")
    signoffs = [value for key, value in trailers if key == "Signed-off-by"]
    if len(signoffs) != len(set(signoffs)):
        issues.append("duplicate-signoff")
    if "Assisted-by" in keys and keys[0:1] != ["Assisted-by"]:
        issues.append("trailer-order")
    if "Signed-off-by" in keys and keys[-1:] != ["Signed-off-by"]:
        issues.append("trailer-order")
    if not any(line.strip() for line in body):
        issues.append("missing-body")
    if any(not TRAILER.match(line) and line.strip() for line in block):
        issues.append("malformed-trailer")
    if any(not line.strip() for line in block):
        issues.append("split-trailer-block")
    if len(subject) > 80:
        issues.append("long-subject")
    long_body = [len(line) for line in body if len(line) > 80]
    if long_body:
        issues.append("long-body")
    return {
        "hash": oid,
        "subject": subject,
        "author": author_line,
        "assistant": assistants,
        "issues": sorted(set(issues)),
        "body_lines_over_80": long_body,
        "trailers": [{"key": key, "value": value} for key, value in trailers],
    }


def audit(repo: Path, refs: list[str]) -> dict[str, Any]:
    commits = [inspect(repo, oid) for oid in commit_ids(repo, refs)]
    counts: dict[str, int] = {}
    for commit in commits:
        for issue in commit["issues"]:
            counts[issue] = counts.get(issue, 0) + 1
    return {"refs": refs, "commit_count": len(commits),
            "issue_counts": dict(sorted(counts.items())), "commits": commits}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("refs", nargs="*", default=["origin/main", "origin/ci-agents"])
    args = parser.parse_args()
    print(json.dumps(audit(args.repo.resolve(), args.refs), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
