#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
codex_home="${CODEX_HOME:-$HOME/.codex}"
state_dir="$codex_home/.delegation-protocol"
state="$state_dir/state"
legacy_bulk_sha256="1a53df02818dafb46b90fa0fea2bc840e50fcc4758c4172c40b8f48db23222f4"

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

sha256_file() {
  "$python_exe" -c \
    'import hashlib, sys; print(hashlib.sha256(open(sys.argv[1], "rb").read()).hexdigest())' \
    "$1"
}

remove_managed_copy_if_ours() {
  local dst="$1" source="$2" hash_file="$3"
  if [[ -L "$dst" && "$(readlink "$dst")" == "$source" ]]; then
    rm "$dst"
  elif [[ -f "$dst" && ! -L "$dst" && -f "$hash_file" ]]; then
    local actual_sha256 recorded_sha256
    actual_sha256="$(sha256_file "$dst")"
    recorded_sha256="$(tr -d '[:space:]' < "$hash_file")"
    if [[ "$actual_sha256" == "$recorded_sha256" ]]; then
      rm "$dst"
    fi
  fi
  rm -f "$hash_file"
}

remove_legacy_bulk_if_ours() {
  local dst="$1" expected="$2"
  if [[ -L "$dst" && "$(readlink "$dst")" == "$expected" ]]; then
    rm "$dst"
  elif [[ -f "$dst" && ! -L "$dst" ]]; then
    local actual_sha256
    actual_sha256="$("$python_exe" -c \
      'import hashlib, sys; print(hashlib.sha256(open(sys.argv[1], "rb").read()).hexdigest())' \
      "$dst")"
    if [[ "$actual_sha256" == "$legacy_bulk_sha256" ]]; then
      rm "$dst"
    fi
  fi
}

"$python_exe" "$repo_root/scripts/codex/manage-hooks.py" uninstall \
  --codex-home "$codex_home" \
  --hook-path "$codex_home/hooks/delegation-enforcer.py" \
  --python "$python_exe"

remove_managed_copy_if_ours \
  "$codex_home/agents/bulk_worker.toml" \
  "$repo_root/codex/agents/bulk_worker.toml" \
  "$state_dir/bulk-worker.sha256"
remove_legacy_bulk_if_ours "$codex_home/agents/bulk-worker.toml" "$repo_root/codex/agents/bulk-worker.toml"
remove_if_ours "$codex_home/agents/balanced-worker.toml" "$repo_root/codex/agents/balanced-worker.toml"
remove_if_ours "$codex_home/hooks/delegation-enforcer.py" "$repo_root/codex/hooks/delegation-enforcer.py"
remove_if_ours "$state_dir/mux-scheduler.py" "$repo_root/scripts/agents/mux-scheduler.py"
remove_if_ours "$state_dir/multiplexer.py" "$repo_root/scripts/agents/multiplexer.py"
remove_if_ours "$state_dir/catalog" "$repo_root/agents/catalog"
remove_if_ours "$state_dir/mux-scheduler.json" "$repo_root/agents/mux-scheduler.json"
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
