#!/usr/bin/env bash
set -euo pipefail
repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
home="${CODEX_HOME:-$HOME/.codex}"
python_exe="${CODEX_PYTHON:-$(command -v python3 || command -v python)}"
exec "$python_exe" "$repo_root/scripts/hosts/install.py" uninstall --host codex --home "$home" --repo "$repo_root"
