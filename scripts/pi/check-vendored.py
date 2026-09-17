#!/usr/bin/env python3
"""Check the vendored subagent extension against its upstream example.

Compares pi/extensions/adp-subagent/{index,agents}.ts against the upstream
examples/extensions/subagent/ files shipped in the installed
@earendil-works/pi-coding-agent package. The documented vendored header
block (the "Vendored from:" / "Upstream:" / "Maintained by:" /
"Local modifications:" comment lines and their continuations) is ignored;
every other difference is reported as a REVIEW line.

Expected non-header diffs (they are the documented local modifications,
so REVIEW lines for them are normal):
  - maxTurns turn budgets (profile section, budget handling)
  - child registry + /subagents command
  - spawn notifications
  - tool_result_end dead-branch fix (tool results are collected from
    message_end events with role "toolResult")

Behavior:
  - default: print every non-header diff line as REVIEW and exit 0
  - --strict: exit 1 when any non-header diff exists

Usage:
  scripts/pi/check-vendored.py [UPSTREAM_DIR] [--strict]

UPSTREAM_DIR defaults to the installed package's examples/extensions/subagent.
"""

import argparse
import difflib
import sys
from pathlib import Path

DEFAULT_UPSTREAM = Path(
    "/home/ederevx/.nvm/versions/node/v24.14.1/lib/node_modules/"
    "@earendil-works/pi-coding-agent/examples/extensions/subagent"
)
REPO_ROOT = Path(__file__).resolve().parents[2]
LOCAL_DIR = REPO_ROOT / "pi" / "extensions" / "adp-subagent"
FILES = ("index.ts", "agents.ts")


def strip_header(lines):
    """Drop the documented header block; return [(lineno, text)] pairs.

    The block starts at the "Vendored from:" comment line and ends at the
    "*/" that closes the enclosing doc comment. Comment continuation lines
    in between are skipped too.
    """
    kept = []
    skipping = False
    for lineno, line in enumerate(lines, 1):
        stripped = line.strip()
        if not skipping and stripped.startswith("*") and "Vendored from:" in line:
            skipping = True
            continue
        if skipping:
            if stripped == "*/":
                skipping = False
            elif stripped.startswith("*"):
                continue
            else:
                # Not part of the comment after all; keep the line.
                skipping = False
                kept.append((lineno, line))
            continue
        kept.append((lineno, line))
    return kept


def read_numbered(path):
    return strip_header(path.read_text(encoding="utf-8").splitlines())


def compare(name, local_path, upstream_path):
    """Yield REVIEW lines for non-header diffs between the two files."""
    local = read_numbered(local_path)
    upstream = read_numbered(upstream_path)

    local_texts = [text for _, text in local]
    upstream_texts = [text for _, text in upstream]

    matcher = difflib.SequenceMatcher(None, upstream_texts, local_texts)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        if tag in ("delete", "replace"):
            for idx in range(i1, i2):
                lineno, text = upstream[idx]
                yield f"REVIEW {name} upstream:{lineno} - {text}"
        if tag in ("insert", "replace"):
            for idx in range(j1, j2):
                lineno, text = local[idx]
                yield f"REVIEW {name} local:{lineno} + {text}"


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "upstream_dir",
        nargs="?",
        default=str(DEFAULT_UPSTREAM),
        help="Upstream examples/extensions/subagent directory",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Exit 1 when any non-header diff exists (default: exit 0)",
    )
    args = parser.parse_args()

    upstream_dir = Path(args.upstream_dir).expanduser().resolve()
    if not upstream_dir.is_dir():
        print(f"ERROR upstream directory not found: {upstream_dir}", file=sys.stderr)
        return 2

    review_lines = []
    for name in FILES:
        local_path = LOCAL_DIR / name
        upstream_path = upstream_dir / name
        if not local_path.is_file() or not upstream_path.is_file():
            print(f"ERROR missing file: {name} (local: {local_path}, upstream: {upstream_path})")
            review_lines.append(f"REVIEW {name} MISSING")
            continue
        review_lines.extend(compare(name, local_path, upstream_path))

    for line in review_lines:
        print(line)
    print(f"{len(review_lines)} non-header diff line(s) across {len(FILES)} file(s); upstream: {upstream_dir}")

    if args.strict and review_lines:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
