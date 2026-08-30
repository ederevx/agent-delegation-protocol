#!/usr/bin/env python3
"""Small fail-open adapter from delegation hooks to the mux scheduler."""
from __future__ import annotations

import importlib.util
import re
from pathlib import Path
from typing import Any


def select(installed: Path, runtime: str) -> dict[str, Any] | None:
    """Return normalized host-facing queue details or ordinary fan-out."""
    try:
        scheduler_path = installed / "mux-scheduler.py"
        spec = importlib.util.spec_from_file_location(
            "_installed_delegation_mux_scheduler", scheduler_path
        )
        if spec is None or spec.loader is None:
            return None
        scheduler = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(scheduler)
        selected = scheduler.select_queue_backend(
            installed / "catalog",
            installed / "mux-scheduler.json",
            "bulk",
            runtime,
            platform=None,
        )
        if not isinstance(selected, dict):
            return None
        backend_id = selected.get("id")
        if not isinstance(backend_id, str) or not re.fullmatch(
            r"[a-z0-9][a-z0-9._-]{0,63}", backend_id
        ):
            return None
        policy = selected.get("queue_policy")
        if policy is None:
            return {"backend": backend_id, "strategy": "fifo", "virtual_slots": 1}
        if not isinstance(policy, dict) or policy.get("strategy") != "round_robin":
            return None
        slots = policy.get("virtual_slots")
        if (
            not isinstance(slots, int)
            or isinstance(slots, bool)
            or not 1 <= slots <= 32
        ):
            return None
        return {
            "backend": backend_id,
            "strategy": "round_robin",
            "virtual_slots": slots,
        }
    except Exception:
        return None
