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
    print("Host lifecycle tests: PASS")


if __name__ == "__main__":
    main()
