#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
codex_home="${CODEX_HOME:-$HOME/.codex}"
state_dir="$codex_home/.delegation-protocol"
runtime_dir="$repo_root/.runtime/codex"

mkdir -p "$codex_home" "$state_dir" "$runtime_dir"

install_codex() {
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

install_codex
echo "Installed Codex delegation protocol only. Restart Codex sessions to load the instructions."
