import errno
import os
import subprocess
import sys
import tempfile
import types
from pathlib import Path

from hook_adapter import (
    LegacyLockLayoutError,
    _classifier,
    _locked,
    _paths,
    _worker_tool_budget,
    run,
)


def test_advisory_lock_recovery() -> None:
    """A killed hook releases its kernel lock without touching its ledger."""
    source = (
        "import os, sys\n"
        "from pathlib import Path\n"
        "from hook_adapter import _locked\n"
        "with _locked(Path(sys.argv[1])):\n"
        "    os._exit(0)\n"
    )
    with tempfile.TemporaryDirectory(prefix="protocol-lock-") as raw:
        lock = Path(raw) / "state.lock"
        env = dict(os.environ, PYTHONPATH=str(Path(__file__).parent))
        crashed = subprocess.run(
            [sys.executable, "-c", source, str(lock)], env=env,
            capture_output=True, text=True,
        )
        assert crashed.returncode == 0, crashed.stderr
        advisory = lock.parent / "advisory-locks" / lock.name
        assert advisory.is_file()
        inode = advisory.stat().st_ino
        with _locked(lock):
            pass
        assert advisory.is_file()
        if os.name != "nt":
            assert advisory.stat().st_ino == inode


def test_advisory_lock_exclusion_and_legacy_refusal() -> None:
    source = (
        "import sys\n"
        "from pathlib import Path\n"
        "from hook_adapter import _locked\n"
        "with _locked(Path(sys.argv[1])):\n"
        "    print('locked', flush=True)\n"
        "    sys.stdin.read(1)\n"
    )
    with tempfile.TemporaryDirectory(prefix="protocol-lock-") as raw:
        lock = Path(raw) / "state.lock"
        env = dict(os.environ, PYTHONPATH=str(Path(__file__).parent))
        holder = subprocess.Popen(
            [sys.executable, "-c", source, str(lock)], env=env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True,
        )
        assert holder.stdout.readline() == "locked\n"
        try:
            try:
                with _locked(lock):
                    raise AssertionError("advisory lock did not exclude a live holder")
            except TimeoutError:
                pass
        finally:
            holder.stdin.write("x")
            holder.stdin.close()
            assert holder.wait(timeout=5) == 0, holder.stderr.read()
        lock.mkdir()
        message = ""
        try:
            with _locked(lock):
                raise AssertionError("legacy lock layout was accepted")
        except LegacyLockLayoutError as error:
            message = str(error)
        assert "quiescently restart" in message
        assert lock.is_dir()


def test_advisory_lock_prevents_lost_updates() -> None:
    source = (
        "import json, sys, time\n"
        "from pathlib import Path\n"
        "from hook_adapter import _locked\n"
        "state = Path(sys.argv[1])\n"
        "lock = Path(sys.argv[2])\n"
        "with _locked(lock):\n"
        "    value = json.loads(state.read_text())\n"
        "    time.sleep(.1)\n"
        "    value['used'] += 1\n"
        "    state.write_text(json.dumps(value))\n"
    )
    with tempfile.TemporaryDirectory(prefix="protocol-lock-") as raw:
        state = Path(raw) / "ledger.json"
        lock = Path(raw) / "ledger.lock"
        state.write_text('{"used": 0}')
        env = dict(os.environ, PYTHONPATH=str(Path(__file__).parent))
        children = [
            subprocess.Popen([sys.executable, "-c", source, str(state), str(lock)],
                env=env, stderr=subprocess.PIPE, text=True)
            for _ in range(2)
        ]
        for child in children:
            assert child.wait(timeout=5) == 0, child.stderr.read()
        assert state.read_text() == '{"used": 2}'


def test_windows_lock_contention_retries_eacces() -> None:
    fake = types.SimpleNamespace(LK_NBLCK=1, LK_UNLCK=2)
    attempts = []

    def locking(_fd, mode, _size):
        attempts.append(mode)
        if mode == fake.LK_NBLCK and attempts.count(mode) == 1:
            raise OSError(errno.EACCES, "locked")

    fake.locking = locking
    with tempfile.TemporaryDirectory(prefix="protocol-lock-") as raw:
        lock = Path(raw) / "state.lock"
        previous_name = os.name
        previous_module = sys.modules.get("msvcrt")
        os.name = "nt"
        sys.modules["msvcrt"] = fake
        try:
            with _locked(lock):
                pass
        finally:
            os.name = previous_name
            if previous_module is None:
                del sys.modules["msvcrt"]
            else:
                sys.modules["msvcrt"] = previous_module
        assert attempts == [fake.LK_NBLCK, fake.LK_NBLCK, fake.LK_UNLCK]


def test_legacy_locks_fail_closed_with_actionable_feedback() -> None:
    with tempfile.TemporaryDirectory(prefix="protocol-lock-") as raw:
        home = Path(raw)
        previous_home = os.environ.get("CODEX_HOME")
        os.environ["CODEX_HOME"] = str(home)
        try:
            classifier = _classifier(home)
            _, worker_lock = _paths(home, "worker-tool-budget:legacy-worker")
            worker_lock.parent.mkdir(parents=True)
            worker_lock.mkdir()
            denial = _worker_tool_budget(home, {
                "agent_id": "legacy-worker", "agent_type": "bulk-worker",
            }, classifier)
            assert denial["hookSpecificOutput"]["permissionDecision"] == "deny"
            assert "quiescently restart" in denial["hookSpecificOutput"][
                "permissionDecisionReason"
            ]
            _, session_lock = _paths(home, "legacy-session")
            session_lock.mkdir()
            denied = run("codex", "pre-mutation", {
                "session_id": "legacy-session", "tool_name": "Read",
            })
            assert denied["hookSpecificOutput"]["permissionDecision"] == "deny"
            stopped = run("codex", "turn-stop", {"session_id": "legacy-session"})
            assert stopped["decision"] == "block"
            prompt = run("codex", "prompt", {"session_id": "legacy-session"})
            assert "quiescently restart" in prompt["systemMessage"]
        finally:
            if previous_home is None:
                os.environ.pop("CODEX_HOME", None)
            else:
                os.environ["CODEX_HOME"] = previous_home


def main() -> None:
    test_advisory_lock_recovery()
    test_advisory_lock_exclusion_and_legacy_refusal()
    test_advisory_lock_prevents_lost_updates()
    test_windows_lock_contention_retries_eacces()
    test_legacy_locks_fail_closed_with_actionable_feedback()
    print("Host lock tests: PASS")


if __name__ == "__main__":
    main()


def test_stale_concurrent_entries_are_swept_at_cap_check() -> None:
    """A worker whose start predates the staleness ceiling frees its slot."""
    import json as _json
    import time as _time

    with tempfile.TemporaryDirectory(prefix="protocol-sweep-") as raw:
        home = Path(raw)
        previous_home = os.environ.get("CODEX_HOME")
        os.environ["CODEX_HOME"] = str(home)
        try:
            classifier = _classifier(home)
            cap = classifier.MAX_ACTIVE_WORKERS
            path, _ = _paths(home, "sweep-session")
            path.parent.mkdir(parents=True, exist_ok=True)
            stale = _time.time() - 7 * 24 * 60 * 60
            fresh = _time.time()
            entries = [{"id": f"stale-{n}", "started_at": stale} for n in range(cap - 1)]
            entries.append({"id": "fresh-worker", "started_at": fresh})
            path.write_text(_json.dumps({
                "schema_version": 3, "concurrent": entries,
                "pending_spawns": [], "denied_spawns": [], "observed": [],
                "peak_active": cap, "requires_delegation": False,
                "requires_multi": False, "min_agents": 0,
                "completed": False, "pending_authorization": False,
            }))
            denied = run("codex", "pre-mutation", {
                "session_id": "sweep-session", "tool_name": "Agent",
                "tool_use_id": "spawn-1",
                "tool_input": {"subagent_type": "bulk-worker"},
            })
            assert denied is None, denied
            state = _json.loads(path.read_text())
            assert not any(
                e["id"].startswith("stale-") for e in state["concurrent"]
            )
            assert any(entry["id"] == "fresh-worker" for entry in state["concurrent"])
            assert "spawn-1" in state["pending_spawns"]
        finally:
            if previous_home is None:
                os.environ.pop("CODEX_HOME", None)
            else:
                os.environ["CODEX_HOME"] = previous_home


def test_fresh_workers_still_count_against_the_cap() -> None:
    """Only stale entries are swept; live workers keep consuming their slot."""
    import json as _json
    import time as _time

    with tempfile.TemporaryDirectory(prefix="protocol-sweep-fresh-") as raw:
        home = Path(raw)
        previous_home = os.environ.get("CODEX_HOME")
        os.environ["CODEX_HOME"] = str(home)
        try:
            classifier = _classifier(home)
            cap = classifier.MAX_ACTIVE_WORKERS
            path, _ = _paths(home, "fresh-session")
            path.parent.mkdir(parents=True, exist_ok=True)
            now = _time.time()
            path.write_text(_json.dumps({
                "schema_version": 3,
                "concurrent": [{"id": f"live-{n}", "started_at": now} for n in range(cap)],
                "pending_spawns": [], "denied_spawns": [], "observed": [],
                "peak_active": cap, "requires_delegation": False,
                "requires_multi": False, "min_agents": 0,
                "completed": False, "pending_authorization": False,
            }))
            denied = run("codex", "pre-mutation", {
                "session_id": "fresh-session", "tool_name": "Agent",
                "tool_use_id": "spawn-2",
                "tool_input": {"subagent_type": "bulk-worker"},
            })
            body = denied["hookSpecificOutput"]
            assert body["permissionDecision"] == "deny", body
            assert "Active worker cap reached" in body["permissionDecisionReason"]
            state = _json.loads(path.read_text())
            assert len(state["concurrent"]) == cap
            assert "spawn-2" not in state["pending_spawns"]
        finally:
            if previous_home is None:
                os.environ.pop("CODEX_HOME", None)
            else:
                os.environ["CODEX_HOME"] = previous_home


def test_schema2_state_migrates_and_stale_entries_expire() -> None:
    """Bare schema-2 ids adopt epoch-zero starts and sweep at the next check."""
    import json as _json

    with tempfile.TemporaryDirectory(prefix="protocol-migrate-") as raw:
        home = Path(raw)
        previous_home = os.environ.get("CODEX_HOME")
        os.environ["CODEX_HOME"] = str(home)
        try:
            path, _ = _paths(home, "migrate-session")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(_json.dumps({
                "schema_version": 2,
                "concurrent": ["crashed-worker"],
                "pending_spawns": [], "denied_spawns": [], "observed": [],
                "peak_active": 1, "requires_delegation": False,
                "requires_multi": False, "min_agents": 0,
                "completed": False, "pending_authorization": False,
            }))
            run("codex", "pre-mutation", {
                "session_id": "migrate-session", "tool_name": "Read",
            })
            state = _json.loads(path.read_text())
            assert state["schema_version"] == 3
            assert state["concurrent"] == [{"id": "crashed-worker", "started_at": 0.0}]
            # The migrated entry is older than the ceiling, so a spawn attempt
            # sweeps it instead of counting it against the cap.
            denied = run("codex", "pre-mutation", {
                "session_id": "migrate-session", "tool_name": "Agent",
                "tool_use_id": "spawn-3",
                "tool_input": {"subagent_type": "bulk-worker"},
            })
            assert denied is None, denied
            state = _json.loads(path.read_text())
            assert state["concurrent"] == []
        finally:
            if previous_home is None:
                os.environ.pop("CODEX_HOME", None)
            else:
                os.environ["CODEX_HOME"] = previous_home


def main() -> None:
    test_advisory_lock_recovery()
    test_advisory_lock_exclusion_and_legacy_refusal()
    test_advisory_lock_prevents_lost_updates()
    test_windows_lock_contention_retries_eacces()
    test_legacy_locks_fail_closed_with_actionable_feedback()
    test_stale_concurrent_entries_are_swept_at_cap_check()
    test_fresh_workers_still_count_against_the_cap()
    test_schema2_state_migrates_and_stale_entries_expire()
    print("Host lock tests: PASS")


if __name__ == "__main__":
    main()
