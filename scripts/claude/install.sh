#!/usr/bin/env bash
set -euo pipefail
repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
home="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
python_exe="${PYTHON:-$(command -v python3 || command -v python)}"
exec "$python_exe" "$repo_root/scripts/hosts/install.py" install --host claude --home "$home" --repo "$repo_root"
