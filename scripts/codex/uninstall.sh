#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
codex_home="${CODEX_HOME:-$HOME/.codex}"
state_dir="$codex_home/.delegation-protocol"
state="$state_dir/state"

if command -v python3 >/dev/null 2>&1; then
  python_exe="$(command -v python3)"
elif command -v python >/dev/null 2>&1; then
  python_exe="$(command -v python)"
else
  echo "Python 3 is required to remove protocol-owned Codex hooks safely." >&2
  exit 1
fi

remove_if_ours() {
  local dst="$1" expected="$2"
  if [[ -L "$dst" && "$(readlink "$dst")" == "$expected" ]]; then
    rm "$dst"
  fi
}

"$python_exe" "$repo_root/scripts/codex/manage-hooks.py" uninstall \
  --codex-home "$codex_home" \
  --hook-path "$codex_home/hooks/delegation-enforcer.py" \
  --python "$python_exe"

remove_if_ours "$codex_home/agents/bulk-worker.toml" "$repo_root/codex/agents/bulk-worker.toml"
remove_if_ours "$codex_home/agents/balanced-worker.toml" "$repo_root/codex/agents/balanced-worker.toml"
remove_if_ours "$codex_home/hooks/delegation-enforcer.py" "$repo_root/codex/hooks/delegation-enforcer.py"
remove_if_ours "$state_dir/multiplexer.py" "$repo_root/scripts/agents/multiplexer.py"
remove_if_ours "$state_dir/catalog" "$repo_root/agents/catalog"
remove_if_ours "$state_dir/multiplexer.json" "$repo_root/agents/multiplexer.json"

if [[ -f "$state" ]]; then
  mode="$(awk -F= '$1=="mode"{print $2}' "$state")"
  source_kind="$(awk -F= '$1=="source"{print $2}' "$state" || true)"
  if [[ "$mode" == "direct" ]]; then
    remove_if_ours "$codex_home/AGENTS.md" "$repo_root/codex/AGENTS.md"
  elif [[ "$mode" == "composed" ]]; then
    remove_if_ours "$codex_home/AGENTS.override.md" "$repo_root/.runtime/codex/AGENTS.composed.md"
    if [[ "$source_kind" == "override" && -e "$state_dir/original-AGENTS.override.md.path-backup" ]]; then
      mv "$state_dir/original-AGENTS.override.md.path-backup" "$codex_home/AGENTS.override.md"
    fi
  fi
fi

rm -f "$state_dir/state" \
  "$state_dir/original-active-global.md" \
  "$state_dir/hooks.before-first-install.json" \
  "$state_dir/hooks-manifest.json"
rm -f "$repo_root/.runtime/codex/AGENTS.composed.md"
rmdir "$state_dir" 2>/dev/null || true
rmdir "$repo_root/.runtime/codex" 2>/dev/null || true
rmdir "$repo_root/.runtime" 2>/dev/null || true
echo "Uninstalled Codex delegation protocol only; unrelated hooks/configuration were preserved and the prior Codex override was restored when applicable."
