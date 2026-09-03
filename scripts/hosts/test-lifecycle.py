from lifecycle import LifecycleState


def main() -> None:
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
