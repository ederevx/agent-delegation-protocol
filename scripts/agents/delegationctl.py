#!/usr/bin/env python3
"""Provider-neutral v2 catalog validator and deterministic selector."""
from __future__ import annotations
import argparse, json, os, shutil, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CATALOG = ROOT / "agents" / "protocol-v2.json"

class ProtocolError(ValueError): pass

def load(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("schema_version") != 2:
        raise ProtocolError("catalog must declare schema_version 2")
    backends = value.get("backends")
    if not isinstance(backends, list) or not backends:
        raise ProtocolError("backends must be a non-empty array")
    ids = set()
    for backend in backends:
        if not isinstance(backend, dict): raise ProtocolError("backend must be an object")
        required = {"id","name","kind","provider","model","selector","execution","lane","priority"}
        if set(backend) != required: raise ProtocolError(f"backend {backend.get('id','?')}: exact v2 fields required")
        ident = backend["id"]
        if not isinstance(ident, str) or ident in ids: raise ProtocolError("backend ids must be unique")
        ids.add(ident)
        if backend["kind"] not in ("native", "oneshot", "session"): raise ProtocolError(f"{ident}: invalid kind")
        if not isinstance(backend["priority"], int) or not 0 <= backend["priority"] <= 100: raise ProtocolError(f"{ident}: invalid priority")
        selector = backend["selector"]
        for key in ("runtimes","platforms","modes","workspaces","functions"):
            if not isinstance(selector.get(key), list) or not selector[key] or len(selector[key]) != len(set(selector[key])): raise ProtocolError(f"{ident}: invalid selector.{key}")
        execution = backend["execution"]
        if execution.get("delivery") not in ("native", "json"): raise ProtocolError(f"{ident}: invalid delivery")
        if backend["kind"] == "native" and execution["delivery"] != "native": raise ProtocolError(f"{ident}: native kind requires native delivery")
        if backend["kind"] != "native" and execution["delivery"] != "json": raise ProtocolError(f"{ident}: external kind requires json delivery")
        lane = backend["lane"]
        if lane.get("owner") not in ("scheduler", "backend") or not isinstance(lane.get("max_concurrency"), int) or lane["max_concurrency"] < 1: raise ProtocolError(f"{ident}: invalid lane")
    routes = value.get("routes")
    if not isinstance(routes, dict) or not routes: raise ProtocolError("routes must be non-empty")
    for route, members in routes.items():
        if not isinstance(members, list) or not members or len(members) != len(set(members)): raise ProtocolError(f"{route}: invalid route")
        if any(member not in ids for member in members): raise ProtocolError(f"{route}: unknown backend")
    return {**value, "by_id": {b["id"]: b for b in backends}}

def select(catalog: dict, route: str, args: argparse.Namespace) -> dict:
    members = catalog["routes"].get(route)
    if members is None: raise ProtocolError(f"unknown route: {route}")
    found = []
    for ident in members:
        b = catalog["by_id"][ident]; s = b["selector"]
        if args.runtime in s["runtimes"] and args.platform in s["platforms"] and args.mode in s["modes"] and args.workspace in s["workspaces"] and args.function in s["functions"]:
            if b["kind"] != "native" and b["execution"].get("argv") and shutil.which(b["execution"]["argv"][0]) is None: continue
            found.append(b)
    if not found: raise ProtocolError("no_backend")
    return sorted(found, key=lambda b: (-b["priority"], b["id"]))[0]

def main() -> int:
    p=argparse.ArgumentParser(); p.add_argument("--catalog",type=Path,default=CATALOG); sub=p.add_subparsers(dest="cmd",required=True)
    sub.add_parser("validate"); s=sub.add_parser("select"); s.add_argument("--route",required=True); s.add_argument("--runtime",required=True); s.add_argument("--platform",required=True); s.add_argument("--mode",required=True); s.add_argument("--workspace",required=True); s.add_argument("--function",required=True)
    a=p.parse_args()
    try:
        c=load(a.catalog)
        out={"schema_version":2,"status":"success","classification":"success"} if a.cmd=="validate" else select(c,a.route,a)
        print(json.dumps(out,sort_keys=True)); return 0
    except (OSError,ValueError,ProtocolError) as e:
        print(json.dumps({"schema_version":2,"status":"configuration_error" if not str(e)=="no_backend" else "no_backend","classification":"configuration_error" if not str(e)=="no_backend" else "no_backend","error":str(e)})); return 69 if str(e)=="no_backend" else 64
if __name__ == "__main__": sys.exit(main())
