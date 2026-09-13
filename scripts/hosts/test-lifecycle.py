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
from lifecycle import LifecycleState


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
    automatic = LifecycleState("automatic_release")
    automatic.start("a"); automatic.complete("a")
    assert automatic.held() == ()
    explicit = LifecycleState("explicit_release")
    explicit.start("e"); explicit.complete("e")
    assert explicit.held() == ("e",)
    explicit.release("e")
    assert explicit.held() == ()
    session = LifecycleState()
    session.start("s"); session.complete("s")
    assert session.held() == ("s",)
    session.end_session()
    assert session.held() == ()
    # `held()` (active) intentionally keeps a completed-but-unreleased worker
    # under session_release -- no inferred dismissal debt. `concurrent` must
    # not: it is the signal real multi-agent overlap is verified against, and
    # has to evict on completion regardless of release mode or a strictly
    # sequential pair of workers would misread as having overlapped.
    overlap = LifecycleState("session_release")
    overlap.start("a")
    assert overlap.concurrent == {"a"}
    overlap.complete("a")
    assert overlap.held() == ("a",)
    assert overlap.concurrent == set()
    overlap.start("b")
    assert overlap.concurrent == {"b"}
    print("Host lifecycle tests: PASS")


if __name__ == "__main__":
    main()
