#!/usr/bin/env python3
"""Authenticated local FIFO lease lane; no provider or shell knowledge."""
from __future__ import annotations
import hmac, json, os, secrets, socket, threading, time
from dataclasses import dataclass
from pathlib import Path

@dataclass
class Lease:
    owner: str; token: str; expires: float

class Lane:
    def __init__(self, lease_seconds=30, idle_seconds=300):
        self.lease_seconds=max(1,int(lease_seconds)); self.idle_seconds=max(1,int(idle_seconds)); self.lock=threading.Condition(); self.current=None; self.waiters=[]; self.last_activity=time.monotonic()
    def _expire(self):
        if self.current and self.current.expires <= time.monotonic(): self.current=None
    def acquire(self, owner, token=None, timeout=30):
        deadline=time.monotonic()+timeout
        with self.lock:
            self._expire()
            if self.current and self.current.owner==owner and token==self.current.token:
                self.current.expires=time.monotonic()+self.lease_seconds; return self.current.token
            ticket=object(); self.waiters.append((ticket,owner));
            try:
                while True:
                    self._expire()
                    if self.current is None and self.waiters and self.waiters[0][0] is ticket:
                        self.waiters.pop(0); lease=Lease(owner,token or secrets.token_urlsafe(24),time.monotonic()+self.lease_seconds); self.current=lease; self.last_activity=time.monotonic(); return lease.token
                    remaining=deadline-time.monotonic()
                    if remaining<=0: self.waiters=[x for x in self.waiters if x[0] is not ticket]; raise TimeoutError("lane acquire timed out")
                    self.lock.wait(min(remaining, .25))
            finally: self.lock.notify_all()
    def release(self, owner, token):
        with self.lock:
            self._expire()
            if not self.current or self.current.owner!=owner or not hmac.compare_digest(self.current.token,token): return False
            self.current=None; self.last_activity=time.monotonic(); self.lock.notify_all(); return True
    def heartbeat(self, owner, token):
        with self.lock:
            self._expire()
            if not self.current or self.current.owner!=owner or not hmac.compare_digest(self.current.token,token): return False
            self.current.expires=time.monotonic()+self.lease_seconds; self.last_activity=time.monotonic(); return True
    def status(self):
        with self.lock:
            self._expire(); return {"owner":self.current.owner if self.current else None,"queued":len(self.waiters),"idle":not self.current and time.monotonic()-self.last_activity>=self.idle_seconds}

class LaneServer:
    def __init__(self, socket_path: Path, secret_path: Path, lane=None): self.socket_path=Path(socket_path); self.secret_path=Path(secret_path); self.lane=lane or Lane(); self.secret=self._secret()
    def _secret(self):
        self.secret_path.parent.mkdir(parents=True,exist_ok=True)
        if self.secret_path.exists(): return self.secret_path.read_bytes()
        value=secrets.token_bytes(32); self.secret_path.write_bytes(value); os.chmod(self.secret_path,0o600); return value
    def dispatch(self, request):
        if not isinstance(request,dict) or not hmac.compare_digest(str(request.get("auth","")), hmac.new(self.secret,b"lane", "sha256").hexdigest()): return {"status":"unauthorized"}
        op=request.get("op"); owner=request.get("owner"); token=request.get("token")
        try:
            if op=="acquire": return {"status":"acquired","token":self.lane.acquire(owner,token,request.get("timeout",30))}
            if op=="release": return {"status":"released" if self.lane.release(owner,token) else "invalid_lease"}
            if op=="heartbeat": return {"status":"renewed" if self.lane.heartbeat(owner,token) else "invalid_lease"}
            if op=="status": return {"status":"success",**self.lane.status()}
        except TimeoutError: return {"status":"timeout"}
        return {"status":"invalid_request"}

    def serve_forever(self):
        """Serve newline-delimited requests on a filesystem UNIX socket."""
        if os.name == "nt": raise OSError("v2 lane service requires a loopback UNIX socket")
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        try: self.socket_path.unlink()
        except FileNotFoundError: pass
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
            listener.bind(str(self.socket_path)); os.chmod(self.socket_path,0o600); listener.listen(16)
            while True:
                conn,_=listener.accept()
                with conn:
                    for line in conn.makefile("rb"):
                        try: request=json.loads(line); answer=self.dispatch(request)
                        except Exception as error: answer={"status":"invalid_request","error":str(error)}
                        conn.sendall((json.dumps(answer,separators=(",",":"))+"\n").encode())
