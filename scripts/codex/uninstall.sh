#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
codex_home="${CODEX_HOME:-$HOME/.codex}"
state_dir="$codex_home/.delegation-protocol"
state="$state_dir/state"
composed="$state_dir/AGENTS.composed.md"
legacy_composed="$repo_root/.runtime/codex/AGENTS.composed.md"
legacy_bulk_sha256="1a53df02818dafb46b90fa0fea2bc840e50fcc4758c4172c40b8f48db23222f4"

if command -v python3 >/dev/null 2>&1; then
  python_exe="$(command -v python3)"
elif command -v python >/dev/null 2>&1; then
  python_exe="$(command -v python)"
else
  echo "Python 3 is required to remove protocol-owned Codex hooks safely." >&2
  exit 1
fi

assert_real_directory_if_present() {
  local path="$1" label="$2"
  if [[ -L "$path" ]]; then
    echo "Refusing unsafe $label path (expected a regular directory): $path" >&2
    return 1
  fi
  if [[ -e "$path" && ! -d "$path" ]]; then
    echo "Refusing unsafe $label path (expected a regular directory): $path" >&2
    return 1
  fi
}

assert_regular_file_if_present() {
  local path="$1" label="$2"
  if [[ -L "$path" ]]; then
    echo "Refusing unsafe $label path (expected a regular file): $path" >&2
    return 1
  fi
  if [[ -e "$path" && ! -f "$path" ]]; then
    echo "Refusing unsafe $label path (expected a regular file): $path" >&2
    return 1
  fi
}

preflight_uninstall() {
  if [[ -L "$codex_home" && ! -e "$codex_home" ]]; then
    echo "Refusing dangling Codex home symlink: $codex_home -> $(readlink "$codex_home")" >&2
    return 1
  fi
  if [[ -e "$codex_home" && ! -d "$codex_home" ]]; then
    echo "Codex home exists but is not a directory: $codex_home" >&2
    return 1
  fi
  assert_real_directory_if_present "$state_dir" "protocol state directory"
  assert_real_directory_if_present "$codex_home/agents" "agents directory"
  assert_real_directory_if_present "$codex_home/hooks" "hooks directory"
  assert_regular_file_if_present "$codex_home/hooks.json" "hooks.json"
}

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

preflight_uninstall

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
remove_if_ours "$state_dir/delegation-classifier.py" "$repo_root/scripts/agents/delegation-classifier.py"
remove_if_ours "$state_dir/delegation_queue.py" "$repo_root/scripts/agents/delegation_queue.py"
remove_if_ours "$state_dir/multiplexer.py" "$repo_root/scripts/agents/multiplexer.py"
remove_if_ours "$state_dir/catalog" "$repo_root/agents/catalog"
remove_if_ours "$state_dir/mux-scheduler.json" "$repo_root/agents/mux-scheduler.json"
remove_if_ours "$state_dir/multiplexer.json" "$repo_root/agents/multiplexer.json"

instructions_cleaned=false
if [[ -f "$state" && ! -L "$state" ]]; then
  mode="$(awk -F= '$1=="mode"{print $2}' "$state")"
  source_kind="$(awk -F= '$1=="source"{print $2}' "$state" || true)"
  if [[ "$mode" == "direct" ]]; then
    agents="$codex_home/AGENTS.md"
    if [[ -L "$agents" && "$(readlink "$agents")" == "$repo_root/codex/AGENTS.md" ]]; then
      rm "$agents"
      instructions_cleaned=true
    fi
  elif [[ "$mode" == "composed" ]]; then
    override="$codex_home/AGENTS.override.md"
    removed_owned_override=false
    if [[ -L "$override" ]]; then
      override_target="$(readlink "$override")"
      if [[ "$override_target" == "$composed" || "$override_target" == "$legacy_composed" ]]; then
        if [[ "$source_kind" == "agents" ]]; then
          rm "$override"
          removed_owned_override=true
        elif [[ "$source_kind" == "override" &&
          ( -e "$state_dir/original-AGENTS.override.md.path-backup" ||
          -L "$state_dir/original-AGENTS.override.md.path-backup" ) ]]; then
          rm "$override"
          removed_owned_override=true
        fi
      fi
    fi
    if [[ "$removed_owned_override" == true ]]; then
      if [[ "$source_kind" == "override" &&
        ( -e "$state_dir/original-AGENTS.override.md.path-backup" ||
        -L "$state_dir/original-AGENTS.override.md.path-backup" ) ]]; then
        mv "$state_dir/original-AGENTS.override.md.path-backup" "$override"
      fi
      rm -f "$composed"
      instructions_cleaned=true
    fi
  fi
fi

if [[ "$instructions_cleaned" == true ]]; then
  rm -f "$state_dir/state" \
    "$state_dir/original-active-global.md"
fi
rm -f "$state_dir/hooks.before-first-install.json" \
  "$state_dir/hooks-manifest.json"
# Per-session turn state was never reaped before this hook gained a sweep, so a
# long-lived home can carry state for every session it ever ran; it is entirely
# protocol-owned, so uninstall removes it outright rather than leaving it.
rm -f "$repo_root/.runtime/codex/AGENTS.composed.md"
rm -rf "$state_dir/hook-state"
rm -rf "$state_dir/__pycache__"
rmdir "$state_dir" 2>/dev/null || true
echo "Uninstalled Codex delegation protocol only; unrelated hooks/configuration were preserved and the prior Codex override was restored when applicable."
