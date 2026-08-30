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

worker_source="$repo_root/claude/agents/bulk-worker.md"
if [[ "${CI_CLAUDE_FOREGROUND_ONLY:-0}" == "1" ]]; then
  worker_source="$repo_root/claude/agents/ci-foreground-bulk-worker.md"
fi

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

remove_legacy_link_if_ours() {
  local dst="$1" expected="$2"
  if [[ -L "$dst" && "$(readlink "$dst")" == "$expected" ]]; then
    rm "$dst"
  fi
}

safe_link "$repo_root/claude/rules/delegation-protocol.md" "$claude_home/rules/delegation-protocol.md"
safe_link "$worker_source" "$claude_home/agents/bulk-worker.md"
safe_link "$repo_root/claude/hooks/delegation-enforcer.py" "$claude_home/hooks/delegation-enforcer.py"
remove_legacy_link_if_ours "$claude_home/.delegation-protocol/multiplexer.py" "$repo_root/scripts/agents/multiplexer.py"
remove_legacy_link_if_ours "$claude_home/.delegation-protocol/multiplexer.json" "$repo_root/agents/multiplexer.json"
safe_link "$repo_root/scripts/agents/mux-scheduler.py" "$claude_home/.delegation-protocol/mux-scheduler.py"
safe_link "$repo_root/scripts/agents/delegation-classifier.py" "$claude_home/.delegation-protocol/delegation-classifier.py"
safe_link "$repo_root/scripts/agents/delegation_queue.py" "$claude_home/.delegation-protocol/delegation_queue.py"
safe_link "$repo_root/agents/catalog" "$claude_home/.delegation-protocol/catalog"
safe_link "$repo_root/agents/mux-scheduler.json" "$claude_home/.delegation-protocol/mux-scheduler.json"

"$python_exe" "$repo_root/scripts/claude/manage-settings.py" install \
  --claude-home "$claude_home" \
  --hook-path "$claude_home/hooks/delegation-enforcer.py" \
  --python "$python_exe"

echo "Installed Claude delegation protocol only: hooks, settings, rule, bulk-worker, and agent mux-scheduler. Restart Claude Code sessions."
