"""Small host-neutral lifecycle state machine used by v2 hook adapters.

Release is declarative: automatic hosts release on completion, explicit hosts
release only on an observed release event, and session hosts retain workers
until session end.  There is deliberately no inferred dismissal debt.
"""
from __future__ import annotations

from dataclasses import dataclass, field

MODES = {"automatic_release", "explicit_release", "session_release"}


@dataclass
class LifecycleState:
    mode: str = "session_release"
    active: set[str] = field(default_factory=set)
    finished: set[str] = field(default_factory=set)
    # Separate from `active`: workers genuinely in flight right now, evicted on
    # completion regardless of release mode. `active` intentionally keeps a
    # completed-but-unreleased worker under explicit/session release (no
    # inferred dismissal debt), which is right for `held()`/dismissal warnings
    # but wrong for measuring real concurrent overlap -- without this split, a
    # strictly sequential start/complete/start/complete pair would still read
    # as two workers "active" at once under session_release.
    concurrent: set[str] = field(default_factory=set)

    def __post_init__(self) -> None:
        if self.mode not in MODES:
            raise ValueError(f"unsupported lifecycle mode: {self.mode}")

    def start(self, worker: str) -> None:
        if worker:
            self.active.add(worker)
            self.finished.discard(worker)
            self.concurrent.add(worker)

    def complete(self, worker: str) -> None:
        self.concurrent.discard(worker)
        if worker not in self.active:
            return
        if self.mode == "automatic_release":
            self.active.remove(worker)
            self.finished.discard(worker)
        else:
            self.finished.add(worker)

    def release(self, worker: str) -> None:
        self.active.discard(worker)
        self.finished.discard(worker)
        self.concurrent.discard(worker)

    def end_session(self) -> None:
        self.active.clear()
        self.finished.clear()
        self.concurrent.clear()

    def held(self) -> tuple[str, ...]:
        return tuple(sorted(self.active))
