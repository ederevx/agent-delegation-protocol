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

    def __post_init__(self) -> None:
        if self.mode not in MODES:
            raise ValueError(f"unsupported lifecycle mode: {self.mode}")

    def start(self, worker: str) -> None:
        if worker:
            self.active.add(worker)
            self.finished.discard(worker)

    def complete(self, worker: str) -> None:
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

    def end_session(self) -> None:
        self.active.clear()
        self.finished.clear()

    def held(self) -> tuple[str, ...]:
        return tuple(sorted(self.active))
