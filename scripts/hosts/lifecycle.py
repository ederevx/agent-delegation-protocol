"""Small host-neutral lifecycle state machine used by v2 hook adapters.

Automatic hosts release on completion; session hosts retain completed workers
in their persisted session state.  There is deliberately no inferred
dismissal debt.
"""
from __future__ import annotations

from dataclasses import dataclass, field

MODES = {"automatic_release", "session_release"}


@dataclass
class LifecycleState:
    mode: str = "session_release"
    active: set[str] = field(default_factory=set)
    finished: set[str] = field(default_factory=set)
    # Separate from `active`: workers genuinely in flight right now, evicted on
    # completion regardless of release mode. `active` intentionally keeps a
    # completed worker under session_release (no inferred dismissal debt), but
    # not for measuring real concurrent overlap -- without this split, a
    # strictly sequential start/complete/start/complete pair would still read
    # as two workers "active" at once.
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
