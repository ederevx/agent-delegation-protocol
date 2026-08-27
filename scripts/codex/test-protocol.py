#!/usr/bin/env python3
"""Self-test Codex delegation hooks and non-destructive hooks.json merging."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
HOOK = REPO_ROOT / "codex" / "hooks" / "delegation-enforcer.py"
MANAGER = REPO_ROOT / "scripts" / "codex" / "manage-hooks.py"
MUX_SCHEDULER = REPO_ROOT / "scripts" / "agents" / "mux-scheduler.py"
INSTALLER = REPO_ROOT / "scripts" / "codex" / (
    "install.ps1" if os.name == "nt" else "install.sh"
)
UNINSTALLER = REPO_ROOT / "scripts" / "codex" / (
    "uninstall.ps1" if os.name == "nt" else "uninstall.sh"
)
WORKER_RENDERER = REPO_ROOT / "scripts" / "agents" / "render-bulk-workers.py"


def install_queue_fixture(home: Path, runtime: str, condition: str) -> None:
    installed = home / ".delegation-protocol"
    catalog = installed / "catalog"
    catalog.mkdir(parents=True, exist_ok=True)
    shutil.copy2(MUX_SCHEDULER, installed / "mux-scheduler.py")
    executable = sys.executable if condition != "unavailable" else "missing-queue-adapter-for-test"
    metadata: dict[str, Any] = {
        "schema_version": 1,
        "id": "test-queue",
        "name": "Test queue",
        "description": "Isolated single-stream queue fixture.",
        "native": False,
        "delegation_queue": True,
        "priority": 100,
        "provider": "test",
        "model": "test",
        "binding": {
            "argv": [executable, "-c", "pass"],
            "max_input_bytes": 1048576,
            "max_output_bytes": 2097152,
            "timeout_seconds": 30,
        },
        "capabilities": {
            "functions": ["audit", "edit", "batch"],
            "runtimes": [runtime],
            "platforms": ["linux", "darwin", "windows"],
            "modes": ["read", "edit"],
            "workspaces": ["shared", "isolated"],
            "deliveries": ["json-receipt"],
        },
        "limits": {"max_concurrency": 1},
    }
    if condition == "invalid":
        metadata["limits"] = {"max_concurrency": 2}
    (catalog / "test-queue.json").write_text(json.dumps(metadata), encoding="utf-8")
    members = ["missing-backend"] if condition == "misconfigured" else ["test-queue"]
    (installed / "mux-scheduler.json").write_text(json.dumps({
        "schema_version": 1, "routes": {"bulk": members},
    }), encoding="utf-8")


def install_round_robin_queue_fixture(home: Path, runtime: str, slots: int = 4) -> None:
    """Install a hook-level fixture that isolates selected metadata handling."""
    install_queue_fixture(home, runtime, "valid")
    installed = home / ".delegation-protocol"
    metadata_path = installed / "catalog" / "test-queue.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["queue_policy"] = {
        "strategy": "round_robin",
        "virtual_slots": slots,
        "quantum": {"unit": "agent_turn", "value": 4},
    }
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    (installed / "mux-scheduler.py").write_text(
        "import json\n"
        "def select_queue_backend(catalog, routes, route, runtime, platform=None):\n"
        "    return json.loads((catalog / 'test-queue.json').read_text(encoding='utf-8'))\n",
        encoding="utf-8",
    )


def run(cmd: list[str], *, env: dict[str, str] | None = None, stdin: dict[str, Any] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, input=(json.dumps(stdin) if stdin is not None else None), text=True, capture_output=True, env=env, check=False)


def installer_command(script: Path) -> list[str]:
    if os.name != "nt":
        return ["bash", str(script)]
    powershell = shutil.which("pwsh") or shutil.which("powershell")
    require(powershell is not None, "PowerShell is required to test the Windows installer")
    return [
        powershell,
        "-NoLogo",
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(script),
    ]


def installer_environment(home: Path) -> dict[str, str]:
    env = dict(os.environ)
    env["CODEX_HOME"] = str(home)
    if os.name == "nt":
        # Keep PATH compatible with older installers and explicitly select the
        # interpreter for current PowerShell installers, including bundled runtimes.
        env["CODEX_PYTHON"] = sys.executable
        env["PATH"] = os.pathsep.join(
            (str(Path(sys.executable).resolve().parent), env.get("PATH", ""))
        )
    return env


def require(value: bool, message: str) -> None:
    if not value:
        raise AssertionError(message)


def test_installer_platform_contract(home: Path) -> None:
    install_command = installer_command(INSTALLER)
    uninstall_command = installer_command(UNINSTALLER)
    env = installer_environment(home)
    require(env["CODEX_HOME"] == str(home), "installer environment lost CODEX_HOME")

    if os.name == "nt":
        require(sys.version_info >= (3, 11),
                "Windows installer tests require Python 3.11 or newer")
        require(INSTALLER.name == "install.ps1", "Windows test selected the Unix installer")
        require(UNINSTALLER.name == "uninstall.ps1", "Windows test selected the Unix uninstaller")
        for command, script in (
            (install_command, INSTALLER),
            (uninstall_command, UNINSTALLER),
        ):
            require(Path(command[0]).name.lower() in {"pwsh.exe", "powershell.exe"},
                    "Windows installer did not select a PowerShell host")
            require(command[-2:] == ["-File", str(script)],
                    f"PowerShell command did not pass {script.name} as a native path")
            require("bash" not in (part.lower() for part in command),
                    "Windows installer command still invokes Bash")
        require(env.get("CODEX_PYTHON") == sys.executable,
                "Windows installer did not receive the running Python interpreter")
        require(env["PATH"].split(os.pathsep, 1)[0] == str(Path(sys.executable).resolve().parent),
                "Windows installer PATH does not start with the running Python directory")
    else:
        require(INSTALLER.name == "install.sh", "Unix test selected the Windows installer")
        require(UNINSTALLER.name == "uninstall.sh", "Unix test selected the Windows uninstaller")
        require(install_command == ["bash", str(INSTALLER)],
                "Unix installer command changed unexpectedly")
        require(uninstall_command == ["bash", str(UNINSTALLER)],
                "Unix uninstaller command changed unexpectedly")


def symlink_integration_available(root: Path) -> bool:
    """Return whether the current Windows token can exercise symlink installs."""
    if os.name != "nt":
        return True
    root.mkdir(parents=True, exist_ok=True)
    target = root / "symlink-capability-target"
    link = root / "symlink-capability-link"
    target.write_text("probe\n", encoding="utf-8")
    try:
        link.symlink_to(target)
        return True
    except OSError as error:
        if getattr(error, "winerror", None) == 1314:
            return False
        raise
    finally:
        link.unlink(missing_ok=True)
        target.unlink(missing_ok=True)


def test_generated_workers() -> None:
    result = run([sys.executable, str(WORKER_RENDERER), "--check"])
    require(result.returncode == 0, f"generated bulk workers are stale: {result.stderr}")
    worker = REPO_ROOT / "codex" / "agents" / "bulk_worker.toml"
    instructions = tomllib.loads(worker.read_text(encoding="utf-8"))[
        "developer_instructions"
    ]
    for contract in (
        "For `native_required`", "whose `runtime` is `codex`",
        "When an external backend launches", "Never redo that task natively",
        "`exec_command` cannot attach an stdin payload", "--task-file",
        "fresh worker-owned scratch directory under `~/tmp`", "`apply_patch`",
        "validate that it is non-empty JSON", "remove the exact empty scratch directory",
        "queue envelope with exactly `tasks`", "even one task remains wrapped in `tasks`",
        "task fields never appear at the envelope's top level",
    ):
        require(contract in instructions,
                f"Codex dispatcher lacks dual-mode contract: {contract}")
    common_contract = (REPO_ROOT / "agents" / "bulk-worker-common.md.tmpl").read_text(
        encoding="utf-8"
    )
    for provider_specific in ("DeepSeek", "CheapestInference", "deepseek-ci", "7200"):
        require(provider_specific not in common_contract,
                f"common dispatcher contract embeds provider-specific policy: {provider_specific}")
    require("Send the JSON on stdin" not in common_contract,
            "common dispatcher contract requires an unavailable stdin transport")


def test_windows_non_elevated_installer_preflight(root: Path) -> None:
    if os.name != "nt" or symlink_integration_available(root / "symlink-probe"):
        return

    home = root / "fresh-codex-home"
    legacy_composed = REPO_ROOT / ".runtime" / "codex" / "AGENTS.composed.md"
    legacy_before = legacy_composed.read_bytes() if legacy_composed.exists() else None
    require(not home.exists(), "non-elevated preflight fixture is not fresh")
    result = run(installer_command(INSTALLER), env=installer_environment(home))
    output = " ".join("\n".join((result.stdout, result.stderr)).split())
    require(result.returncode != 0,
            "non-elevated Windows installer unexpectedly passed symlink preflight")
    for diagnostic_part in (
        "Unable to create symbolic links for Codex hooks",
        "Enable Windows Developer Mode or rerun PowerShell as",
        "Administrator, then retry",
    ):
        require(diagnostic_part in output,
                "non-elevated Windows installer omitted its actionable privilege diagnostic")
    require(not home.exists(),
            "non-elevated Windows installer created Codex state before symlink preflight")
    legacy_after = legacy_composed.read_bytes() if legacy_composed.exists() else None
    require(legacy_after == legacy_before,
            "non-elevated Windows preflight changed shared legacy composition state")


def call_hook(home: Path, mode: str, payload: dict[str, Any]) -> dict[str, Any] | None:
    env = dict(os.environ)
    env["CODEX_HOME"] = str(home)
    result = run([sys.executable, str(HOOK), mode], env=env, stdin=payload)
    require(result.returncode == 0, f"hook {mode} failed: {result.stderr}")
    text = result.stdout.strip()
    return json.loads(text) if text else None


def test_hooks_merge(home: Path) -> None:
    home.mkdir(parents=True, exist_ok=True)
    installed_hook = home / "hook path with spaces" / HOOK.name
    installed_hook.parent.mkdir(parents=True)
    shutil.copy2(HOOK, installed_hook)
    hooks_path = home / "hooks.json"
    original = {
        "description": "existing hooks",
        "hooks": {
            "PreToolUse": [
                {
                    "matcher": "^Bash$",
                    "hooks": [{"type": "command", "command": "echo existing", "statusMessage": "Existing hook"}],
                }
            ]
        },
    }
    hooks_path.write_text(json.dumps(original, indent=2) + "\n", encoding="utf-8")

    result = run([sys.executable, str(MANAGER), "install", "--codex-home", str(home), "--hook-path", str(installed_hook), "--python", sys.executable])
    require(result.returncode == 0, f"hook install failed: {result.stderr}")
    installed = json.loads(hooks_path.read_text(encoding="utf-8"))
    require(installed["description"] == "existing hooks", "top-level metadata was changed")
    require(any(h.get("statusMessage") == "Existing hook" for g in installed["hooks"]["PreToolUse"] for h in g.get("hooks", [])), "existing hook was removed")
    require(any(str(h.get("statusMessage", "")).startswith("Delegation protocol:") for groups in installed["hooks"].values() for g in groups for h in g.get("hooks", [])), "protocol hooks were not installed")

    if os.name == "nt":
        prompt_handler = next(
            handler
            for group in installed["hooks"]["UserPromptSubmit"]
            for handler in group.get("hooks", [])
            if str(handler.get("statusMessage", "")).startswith("Delegation protocol:")
        )
        command_env = dict(os.environ)
        command_env["CODEX_HOME"] = str(home)
        command_result = subprocess.run(
            str(prompt_handler["command"]),
            input=json.dumps({
                "session_id": "path-with-spaces",
                "turn_id": "t1",
                "prompt": "Classify this small request.",
            }),
            text=True,
            capture_output=True,
            env=command_env,
            shell=True,
            check=False,
        )
        require(command_result.returncode == 0,
                f"generated Windows hook command failed: {command_result.stderr}")
        command_output = json.loads(command_result.stdout)
        require(command_output["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit",
                "generated Windows hook command did not reach the prompt handler")
        require((home / ".delegation-protocol" / "hook-state" /
                 "path-with-spaces.json").is_file(),
                "generated Windows hook command did not write state under spaced CODEX_HOME")

    result = run([sys.executable, str(MANAGER), "uninstall", "--codex-home", str(home), "--hook-path", str(installed_hook), "--python", sys.executable])
    require(result.returncode == 0, f"hook uninstall failed: {result.stderr}")
    after = json.loads(hooks_path.read_text(encoding="utf-8"))
    require(any(h.get("statusMessage") == "Existing hook" for g in after["hooks"]["PreToolUse"] for h in g.get("hooks", [])), "existing hook was not preserved")
    require(not any(str(h.get("statusMessage", "")).startswith("Delegation protocol:") for groups in after.get("hooks", {}).values() for g in groups for h in g.get("hooks", [])), "protocol hooks remained after uninstall")


def test_composed_home_isolation(root: Path) -> None:
    homes = [root / "composed one", root / "composed two"]
    instructions = ["first user instructions\n", "second user instructions\n"]
    composed_paths: list[Path] = []
    override_paths: list[Path] = []

    for home, original in zip(homes, instructions):
        home.mkdir(parents=True)
        (home / "AGENTS.md").write_text(original, encoding="utf-8")
        result = run(installer_command(INSTALLER), env=installer_environment(home))
        require(result.returncode == 0,
                f"composed-home installer failed for {home}: {result.stderr}")
        composed = home / ".delegation-protocol" / "AGENTS.composed.md"
        override = home / "AGENTS.override.md"
        require(composed.is_file(), f"installer did not compose instructions under {home}")
        require(override.is_symlink() and override.resolve() == composed.resolve(),
                f"override for {home} does not target its per-home composition")
        require(composed.read_text(encoding="utf-8").startswith(original),
                f"composition for {home} lost its original instructions")
        composed_paths.append(composed)
        override_paths.append(override)

    require(composed_paths[0].resolve() != composed_paths[1].resolve(),
            "two Codex homes shared one composed instruction file")
    second_before = composed_paths[1].read_bytes()
    result = run(installer_command(UNINSTALLER), env=installer_environment(homes[0]))
    require(result.returncode == 0, f"first composed-home uninstall failed: {result.stderr}")
    require(not override_paths[0].exists() and not override_paths[0].is_symlink(),
            "first composed-home uninstall left its managed override")
    require(not composed_paths[0].exists(),
            "first composed-home uninstall left its managed composition")
    require(override_paths[1].is_symlink() and composed_paths[1].read_bytes() == second_before,
            "first composed-home uninstall changed the second home's composition")

    result = run(installer_command(UNINSTALLER), env=installer_environment(homes[1]))
    require(result.returncode == 0, f"second composed-home uninstall failed: {result.stderr}")
    require(not override_paths[1].exists() and not override_paths[1].is_symlink(),
            "second composed-home uninstall left its managed override")
    require(not composed_paths[1].exists(),
            "second composed-home uninstall left its managed composition")


def test_windows_composed_recovery(root: Path) -> None:
    if os.name != "nt":
        return

    def install_override(home: Path, script: Path = INSTALLER) -> tuple[Path, Path, Path]:
        home.mkdir(parents=True)
        override = home / "AGENTS.override.md"
        override.write_text("original override instructions\n", encoding="utf-8")
        result = run(installer_command(script), env=installer_environment(home))
        require(result.returncode == 0, f"override fixture install failed: {result.stderr}")
        state_dir = home / ".delegation-protocol"
        return override, state_dir / "AGENTS.composed.md", state_dir

    dangling_home = root / "dangling per-home"
    override, composed, state_dir = install_override(dangling_home)
    composed.unlink()
    require(override.is_symlink() and not override.exists(),
            "per-home dangling override fixture was not dangling")
    result = run(installer_command(INSTALLER), env=installer_environment(dangling_home))
    require(result.returncode == 0, f"dangling per-home repair failed: {result.stderr}")
    require(composed.is_file() and override.is_symlink() and override.resolve() == composed.resolve(),
            "dangling per-home override was not repaired")
    require("source=override" in (state_dir / "state").read_text(encoding="utf-8"),
            "dangling per-home repair changed source=override ownership")

    missing_link_home = root / "proven interrupted recovery"
    override, composed, state_dir = install_override(missing_link_home)
    override.unlink()
    saved = state_dir / "original-AGENTS.override.md.path-backup"
    saved_before = saved.read_bytes()
    result = run(installer_command(INSTALLER), env=installer_environment(missing_link_home))
    require(result.returncode == 0, f"proven interrupted recovery failed: {result.stderr}")
    require(override.is_symlink() and override.resolve() == composed.resolve(),
            "proven interrupted recovery did not recreate the managed override")
    require(saved.read_bytes() == saved_before and
            "source=override" in (state_dir / "state").read_text(encoding="utf-8"),
            "proven interrupted recovery changed original recovery material")

    for planted_kind in ("file", "directory"):
        planted_home = root / f"planted {planted_kind}"
        planted = planted_home / ".delegation-protocol" / "original-AGENTS.override.md.path-backup"
        if planted_kind == "file":
            planted.parent.mkdir(parents=True)
            planted.write_text("untrusted recovery material\n", encoding="utf-8")
        else:
            planted.mkdir(parents=True)
            (planted / "sentinel").write_text("preserve me\n", encoding="utf-8")
        result = run(installer_command(INSTALLER), env=installer_environment(planted_home))
        require(result.returncode != 0 and "Refusing" in result.stderr,
                f"installer activated planted recovery {planted_kind}")
        require(planted.is_file() if planted_kind == "file" else planted.is_dir(),
                f"installer removed planted recovery {planted_kind}")
        if planted_kind == "file":
            require(planted.read_text(encoding="utf-8") == "untrusted recovery material\n",
                    "installer modified planted recovery file")
        else:
            require((planted / "sentinel").read_text(encoding="utf-8") == "preserve me\n",
                    "installer modified planted recovery directory")
        require(not (planted_home / "AGENTS.override.md").exists() and
                not (planted_home / "AGENTS.override.md").is_symlink(),
                f"installer activated planted recovery {planted_kind}")

    copied_repo = root / "legacy fixture repo"
    shutil.copytree(
        REPO_ROOT,
        copied_repo,
        ignore=shutil.ignore_patterns(".git", ".runtime", "__pycache__"),
    )
    copied_installer = copied_repo / "scripts" / "codex" / "install.ps1"
    legacy_home = root / "dangling legacy"
    override, composed, state_dir = install_override(legacy_home, copied_installer)
    override.unlink()
    composed.unlink()
    legacy = copied_repo / ".runtime" / "codex" / "AGENTS.composed.md"
    override.symlink_to(legacy)
    require(override.is_symlink() and not override.exists(),
            "legacy dangling override fixture was not dangling")
    result = run(installer_command(copied_installer), env=installer_environment(legacy_home))
    require(result.returncode == 0, f"dangling legacy migration failed: {result.stderr}")
    require(composed.is_file() and override.is_symlink() and override.resolve() == composed.resolve(),
            "dangling legacy override was not migrated to per-home composition")
    require("source=override" in (state_dir / "state").read_text(encoding="utf-8"),
            "dangling legacy migration changed source=override ownership")


def test_windows_uninstaller_path_preflight(root: Path) -> None:
    if os.name != "nt" or not symlink_integration_available(root / "symlink probe"):
        return

    live_home_target = root / "live Codex home target"
    live_home_target.mkdir(parents=True)
    live_home_link = root / "live Codex home link"
    live_home_link.symlink_to(live_home_target, target_is_directory=True)
    result = run(installer_command(UNINSTALLER), env=installer_environment(live_home_link))
    require(result.returncode == 0, f"live symlinked CODEX_HOME uninstall failed: {result.stderr}")
    require(live_home_link.is_symlink() and live_home_link.resolve() == live_home_target.resolve(),
            "uninstaller removed or replaced a live symlinked CODEX_HOME")
    result = run(installer_command(UNINSTALLER), env=installer_environment(live_home_link))
    require(result.returncode == 0,
            f"live symlinked CODEX_HOME idempotent uninstall failed: {result.stderr}")

    dangling_home_target = root / "missing Codex home target"
    dangling_home_link = root / "dangling Codex home link"
    dangling_home_link.symlink_to(dangling_home_target, target_is_directory=True)
    result = run(installer_command(UNINSTALLER), env=installer_environment(dangling_home_link))
    require(result.returncode != 0 and "unsafe Codex home" in result.stderr,
            "uninstaller accepted a dangling CODEX_HOME symlink")
    require(dangling_home_link.is_symlink() and not dangling_home_link.exists(),
            "uninstaller removed or activated a dangling CODEX_HOME symlink")

    file_home = root / "file Codex home"
    file_home.write_text("not a directory\n", encoding="utf-8")
    file_before = file_home.read_bytes()
    result = run(installer_command(UNINSTALLER), env=installer_environment(file_home))
    require(result.returncode != 0 and "unsafe Codex home" in result.stderr,
            "uninstaller accepted a file CODEX_HOME")
    require(file_home.read_bytes() == file_before,
            "uninstaller modified a file CODEX_HOME")

    state_home = root / "symlinked state home"
    state_home.mkdir(parents=True)
    state_target = root / "external state target"
    state_target.mkdir()
    state_sentinel = state_target / "sentinel"
    state_sentinel.write_text("state target must remain unchanged\n", encoding="utf-8")
    state_link = state_home / ".delegation-protocol"
    state_link.symlink_to(state_target, target_is_directory=True)
    state_before = state_sentinel.read_bytes()
    result = run(installer_command(UNINSTALLER), env=installer_environment(state_home))
    require(result.returncode != 0 and "unsafe protocol state directory" in result.stderr,
            "uninstaller traversed a symlinked protocol state directory")
    require(state_link.is_symlink() and state_link.resolve() == state_target.resolve(),
            "uninstaller replaced or removed the symlinked protocol state directory")
    require(state_sentinel.read_bytes() == state_before,
            "uninstaller modified the symlinked protocol state target")

    hooks_home = root / "symlinked hooks json home"
    hooks_home.mkdir(parents=True)
    hooks_target = root / "external hooks target.json"
    hooks_target.write_text('{"hooks":{"Stop":[]}}\n', encoding="utf-8")
    hooks_link = hooks_home / "hooks.json"
    hooks_link.symlink_to(hooks_target)
    local_state = hooks_home / ".delegation-protocol"
    local_state.mkdir()
    manifest = local_state / "hooks-manifest.json"
    manifest.write_text("preserve local state\n", encoding="utf-8")
    hooks_before = hooks_target.read_bytes()
    manifest_before = manifest.read_bytes()
    result = run(installer_command(UNINSTALLER), env=installer_environment(hooks_home))
    require(result.returncode != 0 and "unsafe hooks.json" in result.stderr,
            "uninstaller accepted a symlinked hooks.json")
    require(hooks_link.is_symlink() and hooks_link.resolve() == hooks_target.resolve(),
            "uninstaller replaced or removed the symlinked hooks.json")
    require(hooks_target.read_bytes() == hooks_before,
            "uninstaller modified the symlinked hooks.json target")
    require(manifest.read_bytes() == manifest_before,
            "uninstaller deleted protocol state before rejecting symlinked hooks.json")


def test_codex_worker_install(root: Path) -> None:
    if not symlink_integration_available(root):
        print(
            "Codex installer integration: SKIP "
            "(Windows symbolic-link privilege or Developer Mode is required)",
            file=sys.stderr,
        )
        return
    balanced_source = REPO_ROOT / "codex" / "agents" / "balanced-worker.toml"
    legacy_source = REPO_ROOT / "codex" / "agents" / "bulk-worker.toml"
    env = installer_environment(root / "owned-link")
    agents = Path(env["CODEX_HOME"]) / "agents"
    agents.mkdir(parents=True)
    protocol_state = Path(env["CODEX_HOME"]) / ".delegation-protocol"
    protocol_state.mkdir()
    legacy_mux = protocol_state / "multiplexer.py"
    legacy_routes = protocol_state / "multiplexer.json"
    legacy_mux.symlink_to(REPO_ROOT / "scripts" / "agents" / "multiplexer.py")
    legacy_routes.symlink_to(REPO_ROOT / "agents" / "multiplexer.json")
    balanced = agents / "balanced-worker.toml"
    balanced.symlink_to(balanced_source)
    legacy_bulk = agents / "bulk-worker.toml"
    legacy_bulk.symlink_to(legacy_source)

    result = run(installer_command(INSTALLER), env=env)
    require(result.returncode == 0, f"installer failed: {result.stderr}")
    require(balanced.is_symlink() and balanced.resolve() == balanced_source.resolve(),
            "balanced worker link was not preserved")
    require(not legacy_bulk.exists() and not legacy_bulk.is_symlink(),
            "misnamed bulk worker link remained")
    require(not legacy_mux.is_symlink() and not legacy_routes.is_symlink(),
            "legacy multiplexer links remained after mux-scheduler migration")
    require((protocol_state / "mux-scheduler.py").is_symlink(),
            "mux-scheduler executable link was not installed")
    require((protocol_state / "mux-scheduler.json").is_symlink(),
            "mux-scheduler route link was not installed")
    bulk = agents / "bulk_worker.toml"
    require(bulk.is_file() and not bulk.is_symlink(),
            "bulk worker was not installed as a regular file")
    require(bulk.read_bytes() == (REPO_ROOT / "codex" / "agents" / "bulk_worker.toml").read_bytes(),
            "installed bulk worker differs from its source")
    worker_config = tomllib.loads(bulk.read_text(encoding="utf-8"))
    require("sandbox_workspace_write" not in worker_config,
            "bulk dispatcher carries an unexpected sandbox widening")
    instructions = worker_config.get("developer_instructions", "")
    require("`login: true`" in instructions,
            "bulk dispatcher may lose an installed adapter from PATH")
    for contract in (
        "Every task must contain `mode`", "Every `allowed_paths` entry must be relative",
        "`validation` is a list", "valid only in edit mode",
        "`preapproved_commands` is a list", "not argv arrays",
        "`exec_command`", "`yield_time_ms: 30000`", "`session_id`",
        "`write_stdin`", "empty `chars`", "`yield_time_ms: 60000`",
        "`yield_time_ms` is only a yield interval", "until the tool returns an `exit_code`",
        "Do not redirect the receipt", "invoke a second mux-scheduler process",
    ):
        require(contract in instructions,
                f"Codex dispatcher lacks required contract: {contract}")
    require("capture file" not in instructions,
            "Codex dispatcher incorrectly relies on a shell capture file")
    require("run_in_background" not in instructions,
            "Codex dispatcher incorrectly uses Claude's background-call contract")
    for claude_only in ("`SendMessage`", "`TaskStop`", "`timeout: 600000`"):
        require(claude_only not in instructions,
                f"Codex dispatcher incorrectly uses Claude mechanic: {claude_only}")
    worker_hash = root / "owned-link" / ".delegation-protocol" / "bulk-worker.sha256"
    require(worker_hash.read_text(encoding="utf-8").strip() == hashlib.sha256(bulk.read_bytes()).hexdigest(),
            "managed worker hash does not match the installed file")
    if hasattr(os, "O_NOFOLLOW"):
        fd = os.open(bulk, os.O_RDONLY | os.O_NOFOLLOW)
        os.close(fd)
    require({path.name for path in agents.glob("*.toml")} == {
                "bulk_worker.toml", "balanced-worker.toml"},
            "installer installed an unexpected custom agent")

    result = run(installer_command(INSTALLER), env=env)
    require(result.returncode == 0, f"idempotent reinstall failed: {result.stderr}")
    result = run(installer_command(UNINSTALLER), env=env)
    require(result.returncode == 0, f"uninstaller failed: {result.stderr}")
    require(not bulk.exists(), "uninstaller left the unmodified managed worker copy")
    result = run(installer_command(UNINSTALLER), env=env)
    require(result.returncode == 0, f"idempotent uninstall failed: {result.stderr}")

    modified_home = root / "modified-copy"
    modified_env = installer_environment(modified_home)
    result = run(installer_command(INSTALLER), env=modified_env)
    require(result.returncode == 0, f"modified-copy install failed: {result.stderr}")
    modified_worker = modified_home / "agents" / "bulk_worker.toml"
    modified_worker.write_text("# user modification\n", encoding="utf-8")
    result = run(installer_command(UNINSTALLER), env=modified_env)
    require(result.returncode == 0, f"modified-copy uninstall failed: {result.stderr}")
    require(modified_worker.read_text(encoding="utf-8") == "# user modification\n",
            "uninstaller removed a modified managed worker copy")

    custom_home = root / "custom-file"
    custom_agents = custom_home / "agents"
    custom_agents.mkdir(parents=True)
    custom_legacy = custom_agents / "balanced-worker.toml"
    custom_legacy.write_text("model = \"user-owned\"\n", encoding="utf-8")
    custom_env = installer_environment(custom_home)
    result = run(installer_command(INSTALLER), env=custom_env)
    require(result.returncode != 0 and "Refusing to overwrite" in result.stderr,
            "installer did not refuse a user-owned balanced worker")
    require(custom_legacy.read_text(encoding="utf-8") == "model = \"user-owned\"\n",
            "custom balanced worker file was changed")
    result = run(installer_command(UNINSTALLER), env=custom_env)
    require(result.returncode == 0, f"custom-file uninstall failed: {result.stderr}")
    require(custom_legacy.read_text(encoding="utf-8") == "model = \"user-owned\"\n",
            "custom balanced worker file was removed")

    conflict_home = root / "conflict"
    conflict_agents = conflict_home / "agents"
    conflict_agents.mkdir(parents=True)
    conflict_bulk = conflict_agents / "bulk_worker.toml"
    conflict_bulk.write_text("model = \"user-owned\"\n", encoding="utf-8")
    conflict_env = installer_environment(conflict_home)
    result = run(installer_command(INSTALLER), env=conflict_env)
    require(result.returncode != 0 and "Refusing to overwrite" in result.stderr,
            "installer did not refuse a conflicting bulk worker")
    require(conflict_bulk.read_text(encoding="utf-8") == "model = \"user-owned\"\n",
            "conflicting bulk worker was changed")

    identical_home = root / "identical-user-file"
    identical_agents = identical_home / "agents"
    identical_agents.mkdir(parents=True)
    identical_bulk = identical_agents / "bulk_worker.toml"
    identical_bulk.write_bytes((REPO_ROOT / "codex" / "agents" / "bulk_worker.toml").read_bytes())
    identical_env = installer_environment(identical_home)
    result = run(installer_command(INSTALLER), env=identical_env)
    require(result.returncode != 0 and "user-owned worker" in result.stderr,
            "installer claimed an untracked user-owned worker")

    test_composed_home_isolation(root / "composed isolation")
    test_windows_composed_recovery(root / "windows composed recovery")
    test_windows_uninstaller_path_preflight(root / "windows uninstall preflight")

def test_single_gate(home: Path) -> None:
    session = "single"
    call_hook(home, "prompt", {"session_id": session, "turn_id": "t1", "prompt": "Update 12 files with this mechanical rename."})
    denied = call_hook(home, "pretool", {"session_id": session, "turn_id": "t1", "tool_name": "apply_patch", "tool_input": {"command": "*** Begin Patch"}})
    require(denied is not None and denied["hookSpecificOutput"]["permissionDecision"] == "deny", "parent patch was not blocked before delegation")
    call_hook(home, "subagent-start", {"session_id": session, "turn_id": "t1", "agent_id": "worker-a", "agent_type": "bulk_worker"})
    allowed = call_hook(home, "pretool", {"session_id": session, "turn_id": "t1", "tool_name": "apply_patch", "tool_input": {"command": "*** Begin Patch"}})
    require(allowed is None, "parent patch stayed blocked after required delegation")


def test_multi_overlap(home: Path) -> None:
    session = "multi"
    call_hook(home, "prompt", {"session_id": session, "turn_id": "t1", "prompt": "Implement independent frontend and backend subsystems plus separate tests."})
    call_hook(home, "subagent-start", {"session_id": session, "turn_id": "t1", "agent_id": "front", "agent_type": "bulk_worker"})
    denied = call_hook(home, "pretool", {"session_id": session, "turn_id": "t1", "tool_name": "Bash", "tool_input": {"command": "touch integration.txt"}})
    require(denied is not None, "multi-agent task was not blocked after only one worker")
    call_hook(home, "subagent-start", {"session_id": session, "turn_id": "t1", "agent_id": "back", "agent_type": "bulk_worker"})
    allowed = call_hook(home, "pretool", {"session_id": session, "turn_id": "t1", "tool_name": "Bash", "tool_input": {"command": "touch integration.txt"}})
    require(allowed is None, "multi-agent task did not unlock after overlapping workers")
    call_hook(home, "subagent-stop", {"session_id": session, "turn_id": "t1", "agent_id": "front", "agent_type": "bulk_worker"})
    call_hook(home, "subagent-stop", {"session_id": session, "turn_id": "t1", "agent_id": "back", "agent_type": "bulk_worker"})
    for worker in ("front", "back"):
        call_hook(home, "pretool", {"session_id": session, "turn_id": "t1", "tool_name": "stop_task", "tool_input": {"task_id": worker}})
    require(call_hook(home, "stop", {"session_id": session, "turn_id": "t1", "stop_hook_active": False}) is None, "completed and dismissed fan-out task was blocked")


def test_delegation_queue(home: Path) -> None:
    install_queue_fixture(home, "codex", "valid")
    session = "delegation-queue"
    prompt = call_hook(home, "prompt", {
        "session_id": session, "turn_id": "t1",
        "prompt": "Implement independent frontend and backend subsystems plus separate tests.",
    })
    context = prompt["hookSpecificOutput"]["additionalContext"]
    require("delegation queue selected backend `test-queue`" in context, "queue selection was not injected")
    state = json.loads((home / ".delegation-protocol" / "hook-state" / f"{session}.json").read_text())
    require(state["requires_multi"] is True, "queue selection cleared the multi-workstream classification")
    require(state["delegation_queue"] is True and state["delegation_queue_backend"] == "test-queue", "queue state was not recorded")
    require(state["min_agents"] == 1, "queue selection did not reduce lifecycle-visible workers to one")
    call_hook(home, "subagent-start", {"session_id": session, "turn_id": "t1", "agent_id": "queue-dispatcher"})
    allowed = call_hook(home, "pretool", {"session_id": session, "turn_id": "t1", "tool_name": "apply_patch", "tool_input": {}})
    require(allowed is None, "valid queue did not unlock after one dispatcher")


def test_round_robin_delegation_queue(home: Path) -> None:
    install_round_robin_queue_fixture(home, "codex", slots=4)
    session = "round-robin-delegation-queue"
    prompt = call_hook(home, "prompt", {
        "session_id": session, "turn_id": "t1",
        "prompt": "Implement independent frontend and backend subsystems plus separate tests.",
    })
    context = prompt["hookSpecificOutput"]["additionalContext"]
    require("round-robin delegation queue selected backend `test-queue`" in context,
            "round-robin queue selection was not injected")
    require("advertising 4 virtual slots" in context, "virtual slot count was not injected")
    require("mux-scheduler `queue`" in context, "singular queue contract was not injected")
    require("singular scheduler process" in context, "singular scheduler ownership was not injected")
    state = json.loads((home / ".delegation-protocol" / "hook-state" / f"{session}.json").read_text())
    require(state["delegation_queue_strategy"] == "round_robin", "round-robin strategy was not recorded")
    require(state["delegation_queue_virtual_slots"] == 4, "virtual slot count was not recorded")
    require(state["min_agents"] == 1, "round-robin queue did not select one lifecycle dispatcher")
    call_hook(home, "subagent-start", {"session_id": session, "turn_id": "t1", "agent_id": "virtual-a"})
    allowed = call_hook(home, "pretool", {"session_id": session, "turn_id": "t1", "tool_name": "apply_patch", "tool_input": {}})
    require(allowed is None, "round-robin queue did not unlock after one dispatcher")


def test_queue_fallbacks(root: Path) -> None:
    for condition in ("invalid", "unavailable", "misconfigured"):
        home = root / condition
        install_queue_fixture(home, "codex", condition)
        session = f"queue-{condition}"
        prompt = call_hook(home, "prompt", {
            "session_id": session, "turn_id": "t1",
            "prompt": "Implement independent frontend and backend subsystems plus separate tests.",
        })
        require("delegation queue selected" not in prompt["hookSpecificOutput"]["additionalContext"], f"{condition} queue was selected")
        call_hook(home, "subagent-start", {"session_id": session, "turn_id": "t1", "agent_id": "only-worker"})
        denied = call_hook(home, "pretool", {"session_id": session, "turn_id": "t1", "tool_name": "apply_patch", "tool_input": {}})
        require(denied is not None, f"{condition} queue bypassed overlap enforcement")


def test_stop_continuation(home: Path) -> None:
    session = "continuation"
    call_hook(home, "prompt", {"session_id": session, "turn_id": "t1", "prompt": "Apply this change across 20 files."})
    blocked = call_hook(home, "stop", {"session_id": session, "turn_id": "t1", "stop_hook_active": False})
    require(blocked is not None and blocked.get("decision") == "block", "stop did not continue an undelegated bulk turn")
    reason = blocked["reason"]
    call_hook(home, "prompt", {"session_id": session, "turn_id": "t2", "prompt": reason})
    denied = call_hook(home, "pretool", {"session_id": session, "turn_id": "t2", "tool_name": "apply_patch", "tool_input": {"command": "patch"}})
    require(denied is not None, "continuation lost delegation requirement")


def test_opt_out(home: Path) -> None:
    session = "optout"
    call_hook(home, "prompt", {"session_id": session, "turn_id": "t1", "prompt": "Update 20 files but do not delegate or spawn agents."})
    require(call_hook(home, "pretool", {"session_id": session, "turn_id": "t1", "tool_name": "apply_patch", "tool_input": {"command": "patch"}}) is None, "explicit no-delegation instruction was ignored")


def test_worker_dismissal(home: Path) -> None:
    """Test worker-dismissal enforcement: blocking and clearing finished workers."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("codex_delegation_enforcer", HOOK)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    is_dismissal_tool = module.is_dismissal_tool

    session = "dismissal"

    # Test is_dismissal_tool function directly
    require(is_dismissal_tool("TaskStop"), "TaskStop not recognized as dismissal tool")
    require(is_dismissal_tool("stop_agent"), "stop_agent not recognized as dismissal tool")
    require(is_dismissal_tool("kill_task"), "kill_task not recognized as dismissal tool")
    require(is_dismissal_tool("AgentStop"), "AgentStop not recognized as dismissal tool")
    require(is_dismissal_tool("terminate_worker"), "terminate_worker not recognized as dismissal tool")
    require(not is_dismissal_tool("collaboration.interrupt_agent"),
            "collaboration.interrupt_agent incorrectly treated as slot-releasing dismissal")
    require(not is_dismissal_tool("Bash"), "Bash incorrectly recognized as dismissal tool")
    require(not is_dismissal_tool("Agent"), "Agent incorrectly recognized as dismissal tool")
    require(not is_dismissal_tool("apply_patch"), "apply_patch incorrectly recognized as dismissal tool")
    lifecycle_context = module.policy_context({"requires_delegation": False})
    require("FINAL_ANSWER" in lifecycle_context and "before doing more work" in lifecycle_context,
            "parent policy does not prioritize dismissal immediately after result collection")

    # Test 1: finished worker with NO dismissal tool observed -> stop does NOT block
    call_hook(home, "prompt", {"session_id": session, "turn_id": "t1", "prompt": "Create a bulk task."})
    call_hook(home, "subagent-start", {"session_id": session, "turn_id": "t1", "agent_id": "w1@session", "agent_type": "bulk_worker"})
    call_hook(home, "subagent-stop", {"session_id": session, "turn_id": "t1", "agent_id": "w1@session"})
    # Worker w1 is now finished but not dismissed, and no dismissal tool has been observed yet
    result = call_hook(home, "stop", {"session_id": session, "turn_id": "t1", "stop_hook_active": False})
    require(result is not None and isinstance(result.get("systemMessage"), str),
            "stop did not emit a schema-valid warning without a dismissal-tool marker")
    require("hookSpecificOutput" not in result, "stop warning used unsupported hookSpecificOutput")
    require("w1" in result["systemMessage"], "stop warning did not identify the held worker")

    # Test 2: after dismissal-shaped tool call is observed, finished worker DOES cause stop to block
    call_hook(home, "pretool", {"session_id": session, "turn_id": "t1", "tool_name": "TaskStop", "tool_input": {"task_id": "dummy"}})
    result = call_hook(home, "stop", {"session_id": session, "turn_id": "t1", "stop_hook_active": False})
    require(result is not None and result.get("decision") == "block", "stop did not block after dismissal-tool marker with outstanding worker")

    # ...but only once, so an undismissable worker cannot loop the stop hook
    again = call_hook(home, "stop", {"session_id": session, "turn_id": "t1", "stop_hook_active": False})
    require(again is None or again.get("decision") != "block", "stop blocked a second time on the same debt")

    # Test 3: dismissing that specific worker clears it -> stop passes
    call_hook(home, "pretool", {"session_id": session, "turn_id": "t1", "tool_name": "TaskStop", "tool_input": {"agent_id": "w1@session"}})
    result = call_hook(home, "stop", {"session_id": session, "turn_id": "t1", "stop_hook_active": False})
    require(result is None, "stop still blocked after worker was dismissed")

    # Test 4: a runtime id (`a<name>-<hex>`) is cleared by a dismissal on the bare name
    call_hook(home, "subagent-start", {"session_id": session, "turn_id": "t1", "agent_id": "aworker-one-353c3b7231845f11", "agent_type": "bulk_worker"})
    call_hook(home, "subagent-stop", {"session_id": session, "turn_id": "t1", "agent_id": "aworker-one-353c3b7231845f11"})
    require(call_hook(home, "stop", {"session_id": session, "turn_id": "t1", "stop_hook_active": False}) is not None, "runtime-id worker was not held")
    call_hook(home, "pretool", {"session_id": session, "turn_id": "t1", "tool_name": "TaskStop", "tool_input": {"task_id": "worker-one"}})
    require(call_hook(home, "stop", {"session_id": session, "turn_id": "t1", "stop_hook_active": False}) is None, "bare-name dismissal did not clear a runtime-id worker")

    # Test 5: new prompt clears stale outstanding workers
    call_hook(home, "prompt", {"session_id": session, "turn_id": "t2", "prompt": "New turn."})
    result = call_hook(home, "stop", {"session_id": session, "turn_id": "t2", "stop_hook_active": False})
    require(result is None, "stop blocked after prompt reset")


def main() -> int:
    require(HOOK.exists(), f"missing {HOOK}")
    require(MANAGER.exists(), f"missing {MANAGER}")
    require(INSTALLER.exists(), f"missing {INSTALLER}")
    require(UNINSTALLER.exists(), f"missing {UNINSTALLER}")
    require(WORKER_RENDERER.exists(), f"missing {WORKER_RENDERER}")
    test_installer_platform_contract(Path(tempfile.gettempdir()) / "codex-installer-contract")
    test_generated_workers()
    with tempfile.TemporaryDirectory(prefix="codex-delegation-test-") as tmp:
        root = Path(tmp)
        test_windows_non_elevated_installer_preflight(root / "preflight")
        test_codex_worker_install(root / "installer")
        test_hooks_merge(root / "merge path with spaces")
        test_single_gate(root / "single")
        test_multi_overlap(root / "multi")
        test_delegation_queue(root / "queue")
        test_round_robin_delegation_queue(root / "round-robin-queue")
        test_queue_fallbacks(root / "queue-fallbacks")
        test_stop_continuation(root / "continuation")
        test_opt_out(root / "optout")
        test_worker_dismissal(root / "dismissal")
    print("Codex delegation protocol self-test: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
