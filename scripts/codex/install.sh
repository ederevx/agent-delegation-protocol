#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
codex_home="${CODEX_HOME:-$HOME/.codex}"
state_dir="$codex_home/.delegation-protocol"
legacy_bulk_sha256="1a53df02818dafb46b90fa0fea2bc840e50fcc4758c4172c40b8f48db23222f4"

resolve_python311() {
  local candidate resolved
  local -a candidates=()
  if [[ -n "${CODEX_PYTHON:-}" ]]; then
    candidates+=("$CODEX_PYTHON")
  fi
  if command -v python3 >/dev/null 2>&1; then
    candidates+=("$(command -v python3)")
  fi
  if command -v python >/dev/null 2>&1; then
    candidates+=("$(command -v python)")
  fi
  for candidate in "${candidates[@]}"; do
    if resolved="$("$candidate" -c \
      'import os, sys; sys.exit(1) if sys.version_info < (3, 11) else print(os.path.abspath(sys.executable))' \
      2>/dev/null)" && [[ -n "$resolved" && -x "$resolved" ]]; then
      printf '%s\n' "$resolved"
      return 0
    fi
  done
  echo "Python 3.11 or newer is required for Codex hook enforcement. Set CODEX_PYTHON when it is not discoverable." >&2
  return 1
}

python_exe="$(resolve_python311)"

"$python_exe" "$repo_root/scripts/agents/render-bulk-workers.py" --check

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

assert_directory_if_present() {
  local path="$1" label="$2"
  if [[ -e "$path" || -L "$path" ]]; then
    if [[ ! -d "$path" || -L "$path" ]]; then
      echo "Refusing unsafe $label path (expected a regular directory): $path" >&2
      return 1
    fi
  fi
}

assert_regular_file_if_present() {
  local path="$1" label="$2"
  if [[ -e "$path" || -L "$path" ]]; then
    if [[ ! -f "$path" || -L "$path" ]]; then
      echo "Refusing unsafe $label path (expected a regular file): $path" >&2
      return 1
    fi
  fi
}

assert_safe_link_destination() {
  local source="$1" destination="$2"
  if [[ -L "$destination" ]]; then
    if [[ "$(readlink "$destination")" == "$source" ]]; then
      return 0
    fi
    echo "Refusing to replace existing symlink: $destination -> $(readlink "$destination")" >&2
    return 1
  fi
  if [[ -e "$destination" ]]; then
    echo "Refusing to overwrite existing path: $destination" >&2
    return 1
  fi
}

assert_managed_copy_destination() {
  local source="$1" destination="$2" hash_file="$3"
  local source_sha256 current_sha256 recorded_sha256=""
  source_sha256="$(sha256_file "$source")"
  if [[ -L "$destination" ]]; then
    if [[ "$(readlink "$destination")" != "$source" ]]; then
      echo "Refusing to replace existing symlink: $destination -> $(readlink "$destination")" >&2
      return 1
    fi
    return 0
  fi
  if [[ ! -e "$destination" ]]; then
    return 0
  fi
  if [[ ! -f "$destination" ]]; then
    echo "Refusing to overwrite existing path: $destination" >&2
    return 1
  fi
  current_sha256="$(sha256_file "$destination")"
  if [[ -f "$hash_file" && ! -L "$hash_file" ]]; then
    recorded_sha256="$(tr -d '[:space:]' < "$hash_file")"
  fi
  if [[ -z "$recorded_sha256" ]]; then
    echo "Refusing to overwrite user-owned worker: $destination" >&2
    return 1
  fi
  if [[ "$current_sha256" != "$source_sha256" && "$current_sha256" != "$recorded_sha256" ]]; then
    echo "Refusing to overwrite modified or user-owned worker: $destination" >&2
    return 1
  fi
}

assert_hooks_json() {
  local hooks_path="$codex_home/hooks.json"
  assert_regular_file_if_present "$hooks_path" "hooks.json"
  if [[ -f "$hooks_path" ]]; then
    "$python_exe" -c '
import json, pathlib, sys
path = pathlib.Path(sys.argv[1])
try:
    data = json.loads(path.read_text(encoding="utf-8"))
except Exception as exc:
    raise SystemExit(f"Refusing to modify invalid JSON at {path}: {exc}")
if not isinstance(data, dict):
    raise SystemExit(f"Refusing to modify non-object JSON at {path}")
hooks = data.get("hooks")
if hooks is not None and not isinstance(hooks, dict):
    raise SystemExit("Refusing to replace existing non-object `hooks` value")
if isinstance(hooks, dict):
    for event in ("UserPromptSubmit", "SubagentStart", "SubagentStop", "PreToolUse", "PostToolUse", "Stop"):
        if event in hooks and not isinstance(hooks[event], list):
            raise SystemExit(f"Refusing to replace existing non-array hooks.{event}")
' "$hooks_path"
  fi
}

preflight_install() {
  local agents="$codex_home/AGENTS.md"
  local override="$codex_home/AGENTS.override.md"
  local state="$state_dir/state"
  local backup="$state_dir/original-active-global.md"
  local composed="$state_dir/AGENTS.composed.md"
  local saved_override="$state_dir/original-AGENTS.override.md.path-backup"
  local legacy_composed="$repo_root/.runtime/codex/AGENTS.composed.md"
  local mode="" source_kind=""
  local owned_composed_override=false

  if [[ -e "$codex_home" || -L "$codex_home" ]]; then
    if [[ ! -d "$codex_home" ]]; then
      echo "Codex home exists but is not a directory: $codex_home" >&2
      return 1
    fi
  fi
  assert_directory_if_present "$codex_home/agents" "agents directory"
  assert_directory_if_present "$codex_home/hooks" "hooks directory"
  assert_directory_if_present "$state_dir" "protocol state directory"
  assert_regular_file_if_present "$state" "protocol state"
  assert_regular_file_if_present "$backup" "instruction backup"
  assert_regular_file_if_present "$composed" "composed instructions"
  assert_regular_file_if_present "$state_dir/bulk-worker.sha256" "worker ownership hash"
  assert_regular_file_if_present "$state_dir/hooks.before-first-install.json" "hook backup"
  assert_regular_file_if_present "$state_dir/hooks-manifest.json" "hook manifest"
  assert_hooks_json

  for path in "$agents" "$override"; do
    if [[ -L "$path" && ! -e "$path" ]]; then
      continue
    fi
    if [[ ( -e "$path" || -L "$path" ) && ! -f "$path" ]]; then
      echo "Refusing unsafe global instructions path (expected a file): $path" >&2
      return 1
    fi
  done
  if [[ -e "$saved_override" || -L "$saved_override" ]]; then
    if [[ -d "$saved_override" || ( ! -f "$saved_override" && ! -L "$saved_override" ) ]]; then
      echo "Refusing unsafe override recovery backup (expected a file or file symlink): $saved_override" >&2
      return 1
    fi
  fi

  if [[ -f "$state" ]]; then
    mode="$(awk -F= '$1=="mode"{print $2}' "$state")"
    source_kind="$(awk -F= '$1=="source"{print $2}' "$state" || true)"
  fi
  if [[ -L "$override" ]]; then
    local override_target
    override_target="$(readlink "$override")"
    if [[ "$override_target" == "$composed" || "$override_target" == "$legacy_composed" ]]; then
      owned_composed_override=true
      if [[ "$mode" != "composed" || ( "$source_kind" != "agents" && "$source_kind" != "override" ) ||
        ! -f "$backup" || -L "$backup" ]]; then
        echo "Refusing to migrate or replace composed instructions with incomplete ownership state: $override" >&2
        return 1
      fi
      if [[ "$override_target" == "$composed" && ( -e "$composed" || -L "$composed" ) &&
        ( ! -f "$composed" || -L "$composed" ) ]]; then
        echo "Refusing composed instructions with an unsafe per-home target: $composed" >&2
        return 1
      fi
      if [[ "$source_kind" == "override" && ! -e "$saved_override" && ! -L "$saved_override" ]]; then
        echo "Refusing composed override with a missing recovery backup: $saved_override" >&2
        return 1
      fi
      if [[ "$source_kind" == "agents" && ( -e "$saved_override" || -L "$saved_override" ) ]]; then
        echo "Refusing unexpected override recovery backup for AGENTS-sourced composition: $saved_override" >&2
        return 1
      fi
    fi
  fi
  if [[ -L "$agents" && ! -e "$agents" ]]; then
    echo "Refusing broken global instruction symlink: $agents -> $(readlink "$agents")" >&2
    return 1
  fi
  if [[ -L "$override" && ! -e "$override" && "$owned_composed_override" != true ]]; then
    echo "Refusing broken global instruction symlink: $override -> $(readlink "$override")" >&2
    return 1
  fi
  if [[ ( -e "$saved_override" || -L "$saved_override" ) && "$owned_composed_override" != true ]]; then
    echo "Refusing override recovery backup without a matching active composed override: $saved_override" >&2
    return 1
  fi

  assert_managed_copy_destination \
    "$repo_root/codex/agents/bulk_worker.toml" \
    "$codex_home/agents/bulk_worker.toml" \
    "$state_dir/bulk-worker.sha256"
  assert_safe_link_destination "$repo_root/codex/agents/balanced-worker.toml" "$codex_home/agents/balanced-worker.toml"
  assert_safe_link_destination "$repo_root/codex/hooks/delegation-enforcer.py" "$codex_home/hooks/delegation-enforcer.py"
  assert_safe_link_destination "$repo_root/scripts/agents/mux-scheduler.py" "$state_dir/mux-scheduler.py"
  assert_safe_link_destination "$repo_root/agents/catalog" "$state_dir/catalog"
  assert_safe_link_destination "$repo_root/agents/mux-scheduler.json" "$state_dir/mux-scheduler.json"
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
  local saved_override="$state_dir/original-AGENTS.override.md.path-backup"
  local composed="$state_dir/AGENTS.composed.md"
  local legacy_composed="$repo_root/.runtime/codex/AGENTS.composed.md"
  local mode=""
  local recorded_source=""

  if [[ -f "$state" && ! -L "$state" ]]; then
    mode="$(awk -F= '$1=="mode"{print $2}' "$state")"
    recorded_source="$(awk -F= '$1=="source"{print $2}' "$state" || true)"
  fi

  refresh_composed() {
    local destination="$1"
    local temporary="$state_dir/.AGENTS.composed.md.tmp.$$"
    if [[ -e "$temporary" || -L "$temporary" ]]; then
      echo "Refusing to overwrite temporary composition path: $temporary" >&2
      exit 1
    fi
    if [[ ! -f "$backup" || -L "$backup" ]]; then
      echo "Cannot refresh composed instructions without preserved input: $backup" >&2
      exit 1
    fi
    {
      cat "$backup"
      printf '\n\n'
      cat "$protocol"
    } > "$temporary"
    mv "$temporary" "$destination"
  }

  write_instruction_state() {
    local contents="$1"
    local temporary="$state_dir/.state.tmp.$$"
    if [[ -e "$temporary" || -L "$temporary" ]]; then
      echo "Refusing to overwrite temporary instruction state: $temporary" >&2
      return 1
    fi
    printf '%s' "$contents" > "$temporary" || {
      rm -f "$temporary"
      return 1
    }
    mv "$temporary" "$state" || {
      rm -f "$temporary"
      return 1
    }
  }

  if [[ -L "$agents" && "$(readlink "$agents")" == "$protocol" && ! -e "$override" && ! -L "$override" ]]; then
    write_instruction_state $'mode=direct\n'
    return 0
  fi

  if [[ -L "$override" && "$(readlink "$override")" == "$composed" ]]; then
    if [[ "$mode" != "composed" ||
      ( ( -e "$composed" || -L "$composed" ) && ( ! -f "$composed" || -L "$composed" ) ) ]]; then
      echo "Refusing to update composed instructions without matching owned state: $state" >&2
      exit 1
    fi
    refresh_composed "$composed"
    return 0
  fi

  # Older installers placed the composed file in the repository. Migrate only
  # when both the active link and recorded mode prove ownership. Do not delete
  # the legacy file: another CODEX_HOME may still be linked to that shared path.
  if [[ -L "$override" && "$(readlink "$override")" == "$legacy_composed" ]]; then
    if [[ "$mode" != "composed" || ( "$recorded_source" != "agents" && "$recorded_source" != "override" ) ||
      ! -f "$backup" || -L "$backup" ]]; then
      echo "Refusing to migrate legacy composed instructions without matching owned state and backup." >&2
      exit 1
    fi
    if [[ -e "$composed" || -L "$composed" ]]; then
      echo "Refusing to overwrite existing per-home composition: $composed" >&2
      exit 1
    fi
    refresh_composed "$composed"
    local replacement="$state_dir/.AGENTS.override.md.link.$$"
    if ! ln -s "$composed" "$replacement"; then
      rm -f "$composed"
      exit 1
    fi
    if [[ ! -L "$override" || "$(readlink "$override")" != "$legacy_composed" ]]; then
      rm -f "$replacement" "$composed"
      echo "Active override changed during legacy composition migration: $override" >&2
      exit 1
    fi
    if ! mv -f "$replacement" "$override"; then
      rm -f "$replacement" "$composed"
      exit 1
    fi
    return 0
  fi

  if [[ ! -e "$override" && ! -L "$override" && ! -e "$agents" && ! -L "$agents" ]]; then
    ln -s "$protocol" "$agents"
    if ! write_instruction_state $'mode=direct\n'; then
      rm -f "$agents"
      exit 1
    fi
    return 0
  fi

  local source_kind
  if [[ -e "$override" || -L "$override" ]]; then
    source_kind="override"
    if [[ -e "$saved_override" || -L "$saved_override" ]]; then
      echo "Refusing to overwrite existing override recovery backup: $saved_override" >&2
      exit 1
    fi
  else
    source_kind="agents"
  fi

  if [[ -e "$backup" || -L "$backup" ]]; then
    echo "Refusing to overwrite existing preserved instruction backup: $backup" >&2
    exit 1
  fi
  if [[ -e "$composed" || -L "$composed" ]]; then
    echo "Refusing to overwrite existing per-home composition: $composed" >&2
    exit 1
  fi
  if [[ "$source_kind" == "override" ]]; then
    cp -L "$override" "$backup"
  else
    cp -L "$agents" "$backup"
  fi
  refresh_composed "$composed"
  if [[ "$source_kind" == "override" ]]; then
    mv "$override" "$saved_override"
  fi
  if ! ln -s "$composed" "$override"; then
    rm -f "$composed"
    if [[ "$source_kind" == "override" && ! -e "$override" && ! -L "$override" &&
      ( -e "$saved_override" || -L "$saved_override" ) ]]; then
      mv "$saved_override" "$override"
    fi
    exit 1
  fi
  if ! write_instruction_state $'mode=composed\n'"source=$source_kind"$'\n'; then
    rm -f "$override"
    if [[ "$source_kind" == "override" && ( -e "$saved_override" || -L "$saved_override" ) ]]; then
      mv "$saved_override" "$override"
    fi
    rm -f "$composed" "$backup"
    exit 1
  fi
}

preflight_install
mkdir -p "$codex_home" "$codex_home/agents" "$codex_home/hooks" "$state_dir"

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
safe_link "$repo_root/agents/catalog" "$state_dir/catalog"
safe_link "$repo_root/agents/mux-scheduler.json" "$state_dir/mux-scheduler.json"

"$python_exe" "$repo_root/scripts/codex/manage-hooks.py" install \
  --codex-home "$codex_home" \
  --hook-path "$codex_home/hooks/delegation-enforcer.py" \
  --python "$python_exe"

echo "Installed Codex delegation protocol only: supplementary AGENTS instructions, worker tiers, agent mux-scheduler, and lifecycle hooks."
echo "Restart Codex, run /hooks, and review/trust the Agent Delegation Protocol hooks before relying on mechanical enforcement."
