import errno
import os
import subprocess
import sys
import tempfile
import types
from pathlib import Path

import hook_adapter
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
    test_exhausted_budget_denies_with_wind_down_order()
    test_codex_pre_mutation_still_charges_each_attempt()
    print("Host lock tests: PASS")


def test_exhausted_budget_denies_with_wind_down_order() -> None:
    """A spent ledger returns a terminal wind-down order for the worker."""
    import json as _json

    with tempfile.TemporaryDirectory(prefix="protocol-budget-") as raw:
        home = Path(raw)
        previous_home = os.environ.get("CODEX_HOME")
        os.environ["CODEX_HOME"] = str(home)
        try:
            classifier = _classifier(home)
            ledger_path, _ = _paths(home, "worker-tool-budget:wind-down-worker")
            ledger_path.parent.mkdir(parents=True)
            ledger_path.write_text(_json.dumps(
                {"tier": "bulk-worker", "limit": 64, "used": 64, "seen": []}))
            denial = _worker_tool_budget(home, {
                "agent_id": "wind-down-worker",
                "agent_type": "bulk-worker",
                "tool_use_id": "call-1",
            }, classifier)
            output = denial["hookSpecificOutput"]
            assert output["permissionDecision"] == "deny"
            assert output["terminal"] is True
            reason = output["permissionDecisionReason"]
            assert "exhausted (64/64; 0 remaining)" in reason
            assert "Do not call any further tools" in reason
            assert "final evidence report" in reason
        finally:
            if previous_home is None:
                os.environ.pop("CODEX_HOME", None)
            else:
                os.environ["CODEX_HOME"] = previous_home


def test_codex_pre_mutation_still_charges_each_attempt() -> None:
    """Codex keeps its documented behavior: every attempt charges on entry."""
    import json as _json

    with tempfile.TemporaryDirectory(prefix="protocol-budget-") as raw:
        home = Path(raw)
        previous_home = os.environ.get("CODEX_HOME")
        os.environ["CODEX_HOME"] = str(home)
        try:
            # Pin a small limit so the boundary is reachable: the real
            # bulk-worker limit is 64, far past a three-call test.
            ledger_path, _ = _paths(home, "worker-tool-budget:codex-attempt")
            ledger_path.parent.mkdir(parents=True)
            ledger_path.write_text(_json.dumps(
                {"tier": "bulk-worker", "limit": 2, "used": 0, "seen": []}))
            worker = {
                "agent_id": "codex-attempt", "agent_type": "bulk-worker",
                "tool_name": "shell", "tool_input": {"command": ["echo", "hi"]},
            }
            assert run("codex", "pre-mutation", dict(worker, tool_use_id="call-1")) is None
            assert run("codex", "pre-mutation", dict(worker, tool_use_id="call-2")) is None
            ledger_path, _ = _paths(home, "worker-tool-budget:codex-attempt")
            ledger = _json.loads(ledger_path.read_text())
            assert ledger["used"] == 2, ledger
            assert ledger["seen"] == ["call-1", "call-2"], ledger
            denial = run("codex", "pre-mutation", dict(worker, tool_use_id="call-3"))
            output = denial["hookSpecificOutput"]
            assert output["permissionDecision"] == "deny", denial
            assert output["terminal"] is True
            # The budget denial itself does not charge: the atomic check
            # denies before the charge step runs.
            ledger = _json.loads(ledger_path.read_text())
            assert ledger["used"] == 2, ledger
            assert ledger["seen"] == ["call-1", "call-2"], ledger
        finally:
            if previous_home is None:
                os.environ.pop("CODEX_HOME", None)
            else:
                os.environ["CODEX_HOME"] = previous_home


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
            assert ("Active worker cap would be exceeded"
                    in body["permissionDecisionReason"])
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
            # A non-delegating event normalizes the migrated entry with the
            # default fan-out of one slot.
            assert state["concurrent"] == [
                {"id": "crashed-worker", "started_at": 0.0, "fan_out": 1}]
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


def test_parallel_fanout_reserves_and_releases_per_task_slots() -> None:
    """A 3-task fan-out holds 3 slots; over a cap of 3 a 4th spawn is denied."""
    import json as _json

    with tempfile.TemporaryDirectory(prefix="protocol-fanout-") as raw:
        home = Path(raw)
        previous_home = os.environ.get("CODEX_HOME")
        os.environ["CODEX_HOME"] = str(home)
        real_classifier_loader = hook_adapter._classifier
        # A cap of 3 keeps the fan-out arithmetic small; run() re-loads the
        # classifier for every event, so the loader itself is patched.
        classifier = _classifier(home)
        classifier.MAX_ACTIVE_WORKERS = 3
        hook_adapter._classifier = lambda _home: classifier
        try:
            path, _ = _paths(home, "fanout-session")
            path.parent.mkdir(parents=True, exist_ok=True)
            fan_input = {
                "agent": "bulk-worker", "subagent_type": "bulk-worker",
                "tasks": [{"agent": "bulk-worker", "task": f"t{n}"}
                          for n in range(3)],
            }
            admitted = run("codex", "pre-mutation", {
                "session_id": "fanout-session", "tool_name": "Agent",
                "tool_use_id": "fan-1", "tool_input": fan_input,
            })
            assert admitted is None, admitted
            state = _json.loads(path.read_text())
            # The admit-time reservation carries the whole fan-out footprint.
            assert state["pending_spawns"] == [{"id": "fan-1", "fan_out": 3}]
            # The start consumes the reservation and takes all three slots.
            run("codex", "worker-start", {
                "session_id": "fanout-session",
                "agent_id": "fan-1", "tool_use_id": "fan-1",
            })
            state = _json.loads(path.read_text())
            assert state["pending_spawns"] == []
            assert state["concurrent"] == [{
                "id": "fan-1",
                "started_at": state["concurrent"][0]["started_at"],
                "fan_out": 3,
            }]
            assert state["peak_active"] == 3
            # A further single spawn would exceed the cap of 3.
            denied = run("codex", "pre-mutation", {
                "session_id": "fanout-session", "tool_name": "Agent",
                "tool_use_id": "fan-2",
                "tool_input": {"agent": "bulk-worker",
                               "subagent_type": "bulk-worker"},
            })
            body = denied["hookSpecificOutput"]
            assert body["permissionDecision"] == "deny", body
            # The cap is not yet reached -- admitting this call would exceed
            # it -- and the denial names both the slots in use and the
            # requested footprint.
            assert body["permissionDecisionReason"] == (
                "Active worker cap would be exceeded (3/3 slots in use, 1 "
                "more requested); wait for running workers to finish or use "
                "a smaller fan-out.")
            # Completion releases the whole fan-out record at once.
            run("codex", "worker-complete", {
                "session_id": "fanout-session",
                "agent_id": "fan-1", "tool_use_id": "fan-1",
            })
            state = _json.loads(path.read_text())
            assert state["concurrent"] == []
            retried = run("codex", "pre-mutation", {
                "session_id": "fanout-session", "tool_name": "Agent",
                "tool_use_id": "fan-2",
                "tool_input": {"agent": "bulk-worker",
                               "subagent_type": "bulk-worker"},
            })
            assert retried is None, retried
        finally:
            hook_adapter._classifier = real_classifier_loader
            if previous_home is None:
                os.environ.pop("CODEX_HOME", None)
            else:
                os.environ["CODEX_HOME"] = previous_home


def test_fanout_replay_reuses_its_reservation() -> None:
    """A repeated 3-task fan-out delivery is the same spawn, not a new one."""
    import json as _json

    with tempfile.TemporaryDirectory(prefix="protocol-fanout-replay-") as raw:
        home = Path(raw)
        previous_home = os.environ.get("CODEX_HOME")
        os.environ["CODEX_HOME"] = str(home)
        real_classifier_loader = hook_adapter._classifier
        # A cap of exactly the fan-out size means a replay that fails to
        # match its existing reservation would be falsely denied for cap,
        # so any duplicate reservation or false denial fails the test.
        classifier = _classifier(home)
        classifier.MAX_ACTIVE_WORKERS = 3
        hook_adapter._classifier = lambda _home: classifier
        try:
            path, _ = _paths(home, "replay-session")
            path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "session_id": "replay-session", "tool_name": "Agent",
                "tool_use_id": "fan-replay-1",
                "tool_input": {
                    "agent": "bulk-worker", "subagent_type": "bulk-worker",
                    "tasks": [{"agent": "bulk-worker", "task": f"t{n}"}
                              for n in range(3)],
                },
            }
            assert run("codex", "pre-mutation", payload) is None
            # The replayed delivery is recognized as the reservation it
            # already holds: admitted again, one 3-slot record, not two.
            assert run("codex", "pre-mutation", payload) is None
            state = _json.loads(path.read_text())
            assert state["pending_spawns"] == [
                {"id": "fan-replay-1", "fan_out": 3}]
            assert hook_adapter._ledger_slots(
                state["pending_spawns"]) == 3
            # The start consumes the reservation once, taking all 3 slots.
            run("codex", "worker-start", {
                "session_id": "replay-session",
                "agent_id": "fan-replay-1", "tool_use_id": "fan-replay-1",
            })
            state = _json.loads(path.read_text())
            assert state["pending_spawns"] == []
            assert state["concurrent"] == [{
                "id": "fan-replay-1",
                "started_at": state["concurrent"][0]["started_at"],
                "fan_out": 3,
            }]
        finally:
            hook_adapter._classifier = real_classifier_loader
            if previous_home is None:
                os.environ.pop("CODEX_HOME", None)
            else:
                os.environ["CODEX_HOME"] = previous_home


def test_chain_call_reserves_a_single_slot() -> None:
    """A chain runs its steps sequentially, so it reserves exactly 1 slot."""
    import json as _json

    with tempfile.TemporaryDirectory(prefix="protocol-chain-") as raw:
        home = Path(raw)
        previous_home = os.environ.get("CODEX_HOME")
        os.environ["CODEX_HOME"] = str(home)
        real_classifier_loader = hook_adapter._classifier
        classifier = _classifier(home)
        classifier.MAX_ACTIVE_WORKERS = 3
        hook_adapter._classifier = lambda _home: classifier
        try:
            path, _ = _paths(home, "chain-session")
            path.parent.mkdir(parents=True, exist_ok=True)
            chain_input = {
                "agent": "bulk-worker", "subagent_type": "bulk-worker",
                "chain": [{"agent": "bulk-worker", "task": f"step{n}"}
                          for n in range(5)],
            }
            admitted = run("codex", "pre-mutation", {
                "session_id": "chain-session", "tool_name": "Agent",
                "tool_use_id": "chain-1", "tool_input": chain_input,
            })
            # Five sequential steps fit under a cap of 3: a chain charges
            # one slot, not one per step.
            assert admitted is None, admitted
            state = _json.loads(path.read_text())
            assert state["pending_spawns"] == ["chain-1"]
            run("codex", "worker-start", {
                "session_id": "chain-session",
                "agent_id": "chain-1", "tool_use_id": "chain-1",
            })
            state = _json.loads(path.read_text())
            assert state["pending_spawns"] == []
            assert state["concurrent"] == [{
                "id": "chain-1",
                "started_at": state["concurrent"][0]["started_at"],
                "fan_out": 1,
            }]
            run("codex", "worker-complete", {
                "session_id": "chain-session",
                "agent_id": "chain-1", "tool_use_id": "chain-1",
            })
            state = _json.loads(path.read_text())
            assert state["concurrent"] == []
        finally:
            hook_adapter._classifier = real_classifier_loader
            if previous_home is None:
                os.environ.pop("CODEX_HOME", None)
            else:
                os.environ["CODEX_HOME"] = previous_home


def test_worker_session_nested_spawn_releases_its_slot() -> None:
    """A nested spawn from a worker session frees its slot on completion."""
    import json as _json

    with tempfile.TemporaryDirectory(prefix="protocol-nested-") as raw:
        home = Path(raw)
        previous_home = os.environ.get("CLAUDE_CONFIG_DIR")
        os.environ["CLAUDE_CONFIG_DIR"] = str(home)
        try:
            path, _ = _paths(home, "nested-parent")
            path.parent.mkdir(parents=True, exist_ok=True)
            run("claude", "worker-start", {
                "session_id": "nested-parent", "agent_id": "outer-worker",
                "agent_type": "bulk-worker",
            })
            # A nested spawn arrives with the worker-session payload shape:
            # agent_id names the calling worker and the session_id names the
            # parent ledger both share.
            admitted = run("claude", "pre-mutation", {
                "session_id": "nested-parent", "agent_id": "outer-worker",
                "agent_type": "bulk-worker", "tool_name": "Agent",
                "tool_use_id": "nested-call",
                "tool_input": {"subagent_type": "quick-worker"},
            })
            assert admitted is None, admitted
            state = _json.loads(path.read_text())
            assert state["pending_spawns"] == ["nested-call"]
            run("claude", "worker-start", {
                "session_id": "nested-parent", "agent_id": "nested-worker",
                "agent_type": "quick-worker", "tool_use_id": "nested-call",
            })
            state = _json.loads(path.read_text())
            assert [entry["id"] for entry in state["concurrent"]] == [
                "nested-worker", "outer-worker"]
            # The completion of a nested worker must free its slot exactly
            # like a parent's own worker would.
            run("claude", "worker-complete", {
                "session_id": "nested-parent", "agent_id": "nested-worker",
            })
            state = _json.loads(path.read_text())
            assert [entry["id"] for entry in state["concurrent"]] == [
                "outer-worker"]
        finally:
            if previous_home is None:
                os.environ.pop("CLAUDE_CONFIG_DIR", None)
            else:
                os.environ["CLAUDE_CONFIG_DIR"] = previous_home


def main() -> None:
    test_advisory_lock_recovery()
    test_advisory_lock_exclusion_and_legacy_refusal()
    test_advisory_lock_prevents_lost_updates()
    test_windows_lock_contention_retries_eacces()
    test_legacy_locks_fail_closed_with_actionable_feedback()
    test_stale_concurrent_entries_are_swept_at_cap_check()
    test_fresh_workers_still_count_against_the_cap()
    test_schema2_state_migrates_and_stale_entries_expire()
    test_parallel_fanout_reserves_and_releases_per_task_slots()
    test_fanout_replay_reuses_its_reservation()
    test_chain_call_reserves_a_single_slot()
    test_worker_session_nested_spawn_releases_its_slot()
    print("Host lock tests: PASS")


if __name__ == "__main__":
    main()
