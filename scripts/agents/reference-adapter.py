#!/usr/bin/env python3
"""Runnable, provider-neutral v2 adapter used by contract tests and examples."""
import json, os, sys, uuid
from pathlib import Path

STATE = Path(os.environ.get("DELEGATION_V2_STATE", ".delegation-v2-state"))
def receipt(status, **extra): return {"schema_version":2,"status":status,"classification":status,**extra}
def main():
    try: value=json.load(sys.stdin)
    except Exception as e: print(json.dumps(receipt("invalid_request",error=str(e)))); return 64
    if not isinstance(value,dict) or value.get("schema_version") != 2:
        print(json.dumps(receipt("unsupported_contract",error="schema_version 2 required"))); return 64
    op=value.get("operation","run")
    if op=="run":
        task=value.get("task")
        if not isinstance(task,dict) or not isinstance(task.get("prompt"),str) or not task["prompt"].strip():
            print(json.dumps(receipt("invalid_request",error="task.prompt is required"))); return 64
        print(json.dumps(receipt("success",task_id=task.get("id"),response={"echo":task["prompt"]}))); return 0
    if op=="start":
        token=uuid.uuid4().hex; STATE.mkdir(parents=True,exist_ok=True); (STATE/token).write_text("ready",encoding="utf-8")
        print(json.dumps(receipt("ready",operation=op,token=token))); return 0
    token=value.get("token")
    if not isinstance(token,str) or not token or not (STATE/token).is_file(): print(json.dumps(receipt("invalid_request",error="unknown token"))); return 64
    if op=="step":
        (STATE/token).unlink(missing_ok=True); print(json.dumps(receipt("complete",operation=op,exit_code=0,response={"reference":True}))); return 0
    if op=="cancel":
        (STATE/token).unlink(missing_ok=True); print(json.dumps(receipt("cancelled",operation=op,exit_code=0))); return 0
    print(json.dumps(receipt("invalid_request",error="operation must be run, start, step, or cancel"))); return 64
if __name__ == "__main__": sys.exit(main())
