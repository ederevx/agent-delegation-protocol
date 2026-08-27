#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
codex_home="${CODEX_HOME:-$HOME/.codex}"
state_dir="$codex_home/.delegation-protocol"
runtime_dir="$repo_root/.runtime/codex"

if command -v python3 >/dev/null 2>&1; then
  python_exe="$(command -v python3)"
elif command -v python >/dev/null 2>&1; then
  python_exe="$(command -v python)"
else
  echo "Python 3 is required for Codex hook enforcement." >&2
  exit 1
fi

mkdir -p "$codex_home" "$codex_home/agents" "$codex_home/hooks" "$state_dir" "$runtime_dir"

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

install_global_instructions() {
  local protocol="$repo_root/codex/AGENTS.md"
  local agents="$codex_home/AGENTS.md"
  local override="$codex_home/AGENTS.override.md"
  local state="$state_dir/state"
  local backup="$state_dir/original-active-global.md"
  local composed="$runtime_dir/AGENTS.composed.md"

  if [[ -L "$agents" && "$(readlink "$agents")" == "$protocol" && ! -e "$override" ]]; then
    printf 'mode=direct\n' > "$state"
    return 0
  fi

  if [[ -L "$override" && "$(readlink "$override")" == "$composed" && -f "$state" ]]; then
    if [[ -f "$backup" ]]; then
      cat "$backup" > "$composed"
      printf '\n\n' >> "$composed"
    else
      : > "$composed"
    fi
    cat "$protocol" >> "$composed"
    return 0
  fi

  if [[ ! -e "$override" && ! -e "$agents" ]]; then
    ln -s "$protocol" "$agents"
    printf 'mode=direct\n' > "$state"
    return 0
  fi

  local source_kind
  if [[ -e "$override" || -L "$override" ]]; then
    source_kind="override"
    cp -L "$override" "$backup"
    mv "$override" "$state_dir/original-AGENTS.override.md.path-backup"
  else
    source_kind="agents"
    cp -L "$agents" "$backup"
  fi

  cat "$backup" > "$composed"
  printf '\n\n' >> "$composed"
  cat "$protocol" >> "$composed"
  ln -s "$composed" "$override"
  printf 'mode=composed\nsource=%s\n' "$source_kind" > "$state"
}

install_global_instructions
safe_link "$repo_root/codex/agents/bulk-worker.toml" "$codex_home/agents/bulk-worker.toml"
safe_link "$repo_root/codex/agents/balanced-worker.toml" "$codex_home/agents/balanced-worker.toml"
safe_link "$repo_root/codex/hooks/delegation-enforcer.py" "$codex_home/hooks/delegation-enforcer.py"
safe_link "$repo_root/scripts/agents/multiplexer.py" "$state_dir/multiplexer.py"
safe_link "$repo_root/agents/catalog" "$state_dir/catalog"
safe_link "$repo_root/agents/multiplexer.json" "$state_dir/multiplexer.json"

"$python_exe" "$repo_root/scripts/codex/manage-hooks.py" install \
  --codex-home "$codex_home" \
  --hook-path "$codex_home/hooks/delegation-enforcer.py" \
  --python "$python_exe"

echo "Installed Codex delegation protocol only: supplementary AGENTS instructions, worker tiers, agent multiplexer, and lifecycle hooks."
echo "Restart Codex, run /hooks, and review/trust the Agent Delegation Protocol hooks before relying on mechanical enforcement."
