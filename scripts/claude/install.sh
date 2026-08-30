#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
claude_home="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
if command -v python3 >/dev/null 2>&1; then
  python_exe="$(command -v python3)"
elif command -v python >/dev/null 2>&1; then
  python_exe="$(command -v python)"
else
  echo "Python 3 is required for the Claude delegation hook." >&2
  exit 1
fi
exec "$python_exe" "$script_dir/manage-install.py" install \
  --claude-home "$claude_home" --python "$python_exe"
