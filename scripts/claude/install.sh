#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
claude_home="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"

if command -v python3 >/dev/null 2>&1; then
  python_exe="$(command -v python3)"
elif command -v python >/dev/null 2>&1; then
  python_exe="$(command -v python)"
else
  echo "Python 3 is required for the Claude delegation hook." >&2
  exit 1
fi

"$python_exe" "$repo_root/scripts/agents/render-bulk-workers.py" --check

mkdir -p "$claude_home/rules" "$claude_home/agents" "$claude_home/hooks" "$claude_home/.delegation-protocol"

safe_link() {
  local src="$1" dst="$2"
  if [[ -L "$dst" ]]; then
    local current
    current="$(readlink "$dst")"
    if [[ "$current" == "$src" ]]; then
      return 0
    fi
    echo "Refusing to replace existing symlink: $dst -> $current" >&2
    exit 1
  fi
  if [[ -e "$dst" ]]; then
    echo "Refusing to overwrite existing path: $dst" >&2
    exit 1
  fi
  ln -s "$src" "$dst"
}

safe_link "$repo_root/claude/rules/delegation-protocol.md" "$claude_home/rules/delegation-protocol.md"
safe_link "$repo_root/claude/agents/bulk-worker.md" "$claude_home/agents/bulk-worker.md"
safe_link "$repo_root/claude/hooks/delegation-enforcer.py" "$claude_home/hooks/delegation-enforcer.py"
safe_link "$repo_root/scripts/agents/multiplexer.py" "$claude_home/.delegation-protocol/multiplexer.py"
safe_link "$repo_root/agents/catalog" "$claude_home/.delegation-protocol/catalog"
safe_link "$repo_root/agents/multiplexer.json" "$claude_home/.delegation-protocol/multiplexer.json"

"$python_exe" "$repo_root/scripts/claude/manage-settings.py" install \
  --claude-home "$claude_home" \
  --hook-path "$claude_home/hooks/delegation-enforcer.py" \
  --python "$python_exe"

echo "Installed Claude delegation protocol only: hooks, settings, rule, bulk-worker, and agent multiplexer. Restart Claude Code sessions."
