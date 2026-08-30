#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
codex_home="${CODEX_HOME:-$HOME/.codex}"
state_dir="$codex_home/.delegation-protocol"
runtime_dir="$repo_root/.runtime/codex"
legacy_bulk_sha256="1a53df02818dafb46b90fa0fea2bc840e50fcc4758c4172c40b8f48db23222f4"

if command -v python3 >/dev/null 2>&1; then
  python_exe="$(command -v python3)"
elif command -v python >/dev/null 2>&1; then
  python_exe="$(command -v python)"
else
  echo "Python 3 is required for Codex hook enforcement." >&2
  exit 1
fi

"$python_exe" "$repo_root/scripts/agents/render-bulk-workers.py" --check

mkdir -p "$codex_home" "$codex_home/agents" "$codex_home/hooks" "$state_dir" "$runtime_dir"
rm -rf "$state_dir/__pycache__"

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

sha256_file() {
  "$python_exe" -c \
    'import hashlib, sys; print(hashlib.sha256(open(sys.argv[1], "rb").read()).hexdigest())' \
    "$1"
}

install_managed_copy() {
  local src="$1" dst="$2" hash_file="$3"
  local source_sha256 current_sha256 recorded_sha256=""
  source_sha256="$(sha256_file "$src")"

  if [[ -L "$dst" ]]; then
    if [[ "$(readlink "$dst")" != "$src" ]]; then
      echo "Refusing to replace existing symlink: $dst -> $(readlink "$dst")" >&2
      exit 1
    fi
    rm "$dst"
  elif [[ -e "$dst" ]]; then
    if [[ ! -f "$dst" ]]; then
      echo "Refusing to overwrite existing path: $dst" >&2
      exit 1
    fi
    current_sha256="$(sha256_file "$dst")"
    if [[ -f "$hash_file" ]]; then
      recorded_sha256="$(tr -d '[:space:]' < "$hash_file")"
    fi
    if [[ -z "$recorded_sha256" ]]; then
      echo "Refusing to overwrite user-owned worker: $dst" >&2
      exit 1
    fi
    if [[ "$current_sha256" != "$source_sha256" && "$current_sha256" != "$recorded_sha256" ]]; then
      echo "Refusing to overwrite modified or user-owned worker: $dst" >&2
      exit 1
    fi
  fi

  cp "$src" "$dst"
  printf '%s\n' "$source_sha256" > "$hash_file"
}

remove_legacy_worker_if_ours() {
  local dst="$1" expected="$2" legacy_sha256="$3"
  if [[ -L "$dst" && "$(readlink "$dst")" == "$expected" ]]; then
    rm "$dst"
  elif [[ -f "$dst" && ! -L "$dst" ]]; then
    local actual_sha256
    actual_sha256="$("$python_exe" -c \
      'import hashlib, sys; print(hashlib.sha256(open(sys.argv[1], "rb").read()).hexdigest())' \
      "$dst")"
    if [[ "$actual_sha256" == "$legacy_sha256" ]]; then
      rm "$dst"
    fi
  fi
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
remove_legacy_worker_if_ours \
  "$codex_home/agents/bulk-worker.toml" \
  "$repo_root/codex/agents/bulk-worker.toml" \
  "$legacy_bulk_sha256"
install_managed_copy \
  "$repo_root/codex/agents/bulk_worker.toml" \
  "$codex_home/agents/bulk_worker.toml" \
  "$state_dir/bulk-worker.sha256"
safe_link "$repo_root/codex/agents/balanced-worker.toml" "$codex_home/agents/balanced-worker.toml"
safe_link "$repo_root/codex/hooks/delegation-enforcer.py" "$codex_home/hooks/delegation-enforcer.py"
remove_legacy_link_if_ours "$state_dir/multiplexer.py" "$repo_root/scripts/agents/multiplexer.py"
remove_legacy_link_if_ours "$state_dir/multiplexer.json" "$repo_root/agents/multiplexer.json"
safe_link "$repo_root/scripts/agents/mux-scheduler.py" "$state_dir/mux-scheduler.py"
safe_link "$repo_root/scripts/agents/delegation-classifier.py" "$state_dir/delegation-classifier.py"
safe_link "$repo_root/scripts/agents/delegation_queue.py" "$state_dir/delegation_queue.py"
safe_link "$repo_root/agents/catalog" "$state_dir/catalog"
safe_link "$repo_root/agents/mux-scheduler.json" "$state_dir/mux-scheduler.json"

"$python_exe" "$repo_root/scripts/codex/manage-hooks.py" install \
  --codex-home "$codex_home" \
  --hook-path "$codex_home/hooks/delegation-enforcer.py" \
  --python "$python_exe"

echo "Installed Codex delegation protocol only: supplementary AGENTS instructions, worker tiers, agent mux-scheduler, and lifecycle hooks."
echo "Restart Codex, run /hooks, and review/trust the Agent Delegation Protocol hooks before relying on mechanical enforcement."
