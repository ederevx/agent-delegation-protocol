#!/usr/bin/env python3
"""Provider-neutral reference adapter used by v2 conformance tests."""
from __future__ import annotations

import json
import os
import sys
import uuid
from pathlib import Path
from typing import Any

STATE = Path(os.environ.get("DELEGATION_V2_STATE", ".delegation-v2-state"))


def receipt(status: str, classification: str, **fields: Any) -> dict[str, Any]:
    return {"schema_version": 2, "status": status,
            "classification": classification, **fields}


def fail(message: str) -> int:
    print(json.dumps(receipt("failed", "invalid_request", error=message)))
    return 64


def task_valid(task: Any) -> bool:
    return (isinstance(task, dict) and task.get("schema_version") == 2 and
            isinstance(task.get("id"), str) and
            isinstance(task.get("prompt"), str) and bool(task["prompt"].strip()))


def save(token: str, value: dict[str, Any]) -> None:
    STATE.mkdir(parents=True, exist_ok=True)
    (STATE / token).write_text(json.dumps(value), encoding="utf-8")


def load(token: Any) -> dict[str, Any] | None:
    if not isinstance(token, str) or not token:
        return None
    try:
        value = json.loads((STATE / token).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def main() -> int:
    try:
        value = json.load(sys.stdin)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        return fail(str(error))
    if not isinstance(value, dict) or value.get("schema_version") != 2:
        return fail("schema_version 2 required")
    operation = value.get("operation")
    if operation == "run":
        task = value.get("task")
        if not task_valid(task):
            return fail("valid task required")
        print(json.dumps(receipt("completed", "task_complete",
                                 task_id=task["id"],
                                 response={"echo": task["prompt"]})))
        return 0
    if operation == "batch":
        tasks = value.get("tasks")
        if not isinstance(tasks, list) or not tasks or not all(map(task_valid, tasks)):
            return fail("valid tasks required")
        results = [receipt("completed", "task_complete", task_id=task["id"])
                   for task in tasks]
        print(json.dumps(receipt("completed", "batch_complete", results=results)))
        return 0
    if operation == "start":
        task = value.get("task")
        if not task_valid(task):
            return fail("valid task required")
        token = uuid.uuid4().hex
        save(token, {"task": task, "permission": "permission" in task["prompt"],
                     "paused": False})
        print(json.dumps(receipt("ready", "session_ready", token=token)))
        return 0
    token = value.get("token")
    state = load(token)
    if state is None:
        return fail("unknown session token")
    if operation == "step":
        if state["permission"] and not state["paused"]:
            state["paused"] = True
            save(token, state)
            print(json.dumps(receipt(
                "permission_required", "parent_decision_required", token=token,
                request={"kind": "reference", "message": "allow completion"},
            )))
            return 9
        (STATE / token).unlink(missing_ok=True)
        print(json.dumps(receipt("completed", "task_complete", token=token,
                                 task_id=state["task"]["id"],
                                 response={"reference": True})))
        return 0
    if operation == "resume":
        resolution = value.get("resolution")
        if not isinstance(resolution, dict) or resolution.get("decision") not in {
                "allow", "deny", "handled"}:
            return fail("valid resolution required")
        if resolution["decision"] == "deny":
            (STATE / token).unlink(missing_ok=True)
            print(json.dumps(receipt("cancelled", "permission_denied", token=token)))
            return 0
        (STATE / token).unlink(missing_ok=True)
        print(json.dumps(receipt("completed", "task_complete", token=token,
                                 task_id=state["task"]["id"])))
        return 0
    if operation == "cancel":
        (STATE / token).unlink(missing_ok=True)
        print(json.dumps(receipt("cancelled", "cancelled", token=token)))
        return 0
    return fail("operation must be run, batch, start, step, resume, or cancel")


if __name__ == "__main__":
    raise SystemExit(main())
