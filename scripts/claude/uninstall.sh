#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
claude_home="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"

if command -v python3 >/dev/null 2>&1; then
  python_exe="$(command -v python3)"
elif command -v python >/dev/null 2>&1; then
  python_exe="$(command -v python)"
else
  echo "Python 3 is required to remove the protocol-owned Claude settings safely." >&2
  exit 1
fi

remove_if_ours() {
  local dst="$1" expected="$2"
  if [[ -L "$dst" && "$(readlink "$dst")" == "$expected" ]]; then
    rm "$dst"
  fi
}

"$python_exe" "$repo_root/scripts/claude/manage-settings.py" uninstall \
  --claude-home "$claude_home" \
  --hook-path "$claude_home/hooks/delegation-enforcer.py" \
  --python "$python_exe"

remove_if_ours "$claude_home/rules/delegation-protocol.md" "$repo_root/claude/rules/delegation-protocol.md"
remove_if_ours "$claude_home/agents/bulk-worker.md" "$repo_root/claude/agents/bulk-worker.md"
remove_if_ours "$claude_home/agents/bulk-worker.md" "$repo_root/claude/agents/ci-foreground-bulk-worker.md"
remove_if_ours "$claude_home/hooks/delegation-enforcer.py" "$repo_root/claude/hooks/delegation-enforcer.py"
remove_if_ours "$claude_home/.delegation-protocol/mux-scheduler.py" "$repo_root/scripts/agents/mux-scheduler.py"
remove_if_ours "$claude_home/.delegation-protocol/delegation-classifier.py" "$repo_root/scripts/agents/delegation-classifier.py"
remove_if_ours "$claude_home/.delegation-protocol/multiplexer.py" "$repo_root/scripts/agents/multiplexer.py"
remove_if_ours "$claude_home/.delegation-protocol/catalog" "$repo_root/agents/catalog"
remove_if_ours "$claude_home/.delegation-protocol/mux-scheduler.json" "$repo_root/agents/mux-scheduler.json"
remove_if_ours "$claude_home/.delegation-protocol/multiplexer.json" "$repo_root/agents/multiplexer.json"

rm -f "$claude_home/.delegation-protocol/settings.before-first-install.json" \
  "$claude_home/.delegation-protocol/settings-manifest.json"
# Session state is protocol-owned turn evidence; nothing else ever wrote to it,
# and nothing else reads it back, so uninstall removes the whole tree rather
# than leaving accumulated per-session files behind.
rm -rf "$claude_home/.delegation-protocol/sessions"
rmdir "$claude_home/.delegation-protocol" 2>/dev/null || true
echo "Uninstalled Claude delegation protocol only; unrelated Claude settings and hooks were preserved."
