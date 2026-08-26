#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
codex_home="${CODEX_HOME:-$HOME/.codex}"
claude_home="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
state_dir="$codex_home/.delegation-protocol"
runtime_dir="$repo_root/.runtime/codex"

mkdir -p "$codex_home" "$claude_home/rules" "$claude_home/agents" "$state_dir" "$runtime_dir"

abs_link() {
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

install_claude() {
  abs_link "$repo_root/claude/rules/delegation-protocol.md" "$claude_home/rules/delegation-protocol.md"
  abs_link "$repo_root/claude/agents/bulk-worker.md" "$claude_home/agents/bulk-worker.md"
}

install_codex() {
  local protocol="$repo_root/codex/AGENTS.md"
  local agents="$codex_home/AGENTS.md"
  local override="$codex_home/AGENTS.override.md"
  local state="$state_dir/state"
  local backup="$state_dir/original-active-global.md"
  local composed="$runtime_dir/AGENTS.composed.md"

  # Idempotent direct-link mode.
  if [[ -L "$agents" && "$(readlink "$agents")" == "$protocol" && ! -e "$override" ]]; then
    printf 'mode=direct\n' > "$state"
    return 0
  fi

  # Idempotent composed mode: rebuild from the saved original so policy updates apply.
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

  # Clean install with no global instructions: direct symlink.
  if [[ ! -e "$override" && ! -e "$agents" ]]; then
    ln -s "$protocol" "$agents"
    printf 'mode=direct\n' > "$state"
    return 0
  fi

  # Existing active global instructions require a composed file because Codex loads
  # only one global source: override first, otherwise AGENTS.md.
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
install_claude

echo "Installed delegation protocol. Restart Codex/Claude Code sessions to load the new instructions."
