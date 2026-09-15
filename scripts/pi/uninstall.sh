#!/usr/bin/env bash
set -euo pipefail
repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
home="${PI_CODING_AGENT_DIR:-$HOME/.pi/agent}"
python_exe="${PYTHON:-$(command -v python3 || command -v python)}"
exec "$python_exe" "$repo_root/scripts/hosts/install.py" uninstall --host pi --home "$home" --repo "$repo_root"
