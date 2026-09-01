#!/usr/bin/env python3
"""Authenticated, role-blind loopback lane service for protocol v2."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import socket
import socketserver
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PROTOCOL_VERSION = 2
MAX_MESSAGE_BYTES = 256 * 1024


class LaneError(ValueError):
    """A stable lane protocol error."""


@dataclass
class Lease:
    owner: str
    token: str
    expires_at: float


class Lane:
    """One fair FIFO resource lane with bounded concurrent leases."""

    def __init__(self, capacity: int, lease_seconds: int = 30) -> None:
        if capacity < 1 or lease_seconds < 1:
            raise LaneError("capacity and lease_seconds must be positive")
        self.capacity = capacity
        self.lease_seconds = lease_seconds
        self.condition = threading.Condition()
        self.leases: dict[str, Lease] = {}
        self.waiters: list[tuple[str, str]] = []
        self.last_activity = time.monotonic()

    def _expire(self) -> None:
        now = time.monotonic()
        expired = [token for token, lease in self.leases.items()
                   if lease.expires_at <= now]
        for token in expired:
            del self.leases[token]
        if expired:
            self.last_activity = now
            self.condition.notify_all()

    def acquire(
        self,
        owner: str,
        token: str | None = None,
        timeout_seconds: float = 30,
    ) -> str:
        if not owner:
            raise LaneError("owner is required")
        deadline = time.monotonic() + max(0, timeout_seconds)
        with self.condition:
            self._expire()
            if token:
                lease = self.leases.get(token)
                if lease and lease.owner == owner:
                    lease.expires_at = time.monotonic() + self.lease_seconds
                    self.last_activity = time.monotonic()
                    return token
                raise LaneError("invalid reentry lease")

            ticket = secrets.token_urlsafe(18)
            self.waiters.append((ticket, owner))
            try:
                while True:
                    self._expire()
                    position = next(
                        index for index, item in enumerate(self.waiters)
                        if item[0] == ticket
                    )
                    available = self.capacity - len(self.leases)
                    if available > 0 and position < available:
                        self.waiters.pop(position)
                        lease_token = secrets.token_urlsafe(32)
                        self.leases[lease_token] = Lease(
                            owner,
                            lease_token,
                            time.monotonic() + self.lease_seconds,
                        )
                        self.last_activity = time.monotonic()
                        self.condition.notify_all()
                        return lease_token
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError("lane acquire timed out")
                    self.condition.wait(min(remaining, 0.25))
            finally:
                self.waiters = [item for item in self.waiters
                                if item[0] != ticket]
                self.condition.notify_all()

    def heartbeat(self, owner: str, token: str) -> bool:
        with self.condition:
            self._expire()
            lease = self.leases.get(token)
            if not lease or lease.owner != owner:
                return False
            lease.expires_at = time.monotonic() + self.lease_seconds
            self.last_activity = time.monotonic()
            return True

    def release(self, owner: str, token: str) -> bool:
        with self.condition:
            self._expire()
            lease = self.leases.get(token)
            if not lease or lease.owner != owner:
                return False
            del self.leases[token]
            self.last_activity = time.monotonic()
            self.condition.notify_all()
            return True

    def status(self) -> dict[str, Any]:
        with self.condition:
            self._expire()
            return {
                "capacity": self.capacity,
                "leased": len(self.leases),
                "queued": len(self.waiters),
                "owners": sorted(lease.owner for lease in self.leases.values()),
                "idle": not self.leases and not self.waiters,
                "idle_seconds": max(0, time.monotonic() - self.last_activity),
            }


class LaneRegistry:
    """Owns named lanes; callers cannot redefine an existing lane."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.lanes: dict[str, Lane] = {}

    def lane(self, lane_id: str, capacity: int, lease_seconds: int) -> Lane:
        if not lane_id:
            raise LaneError("lane_id is required")
        with self.lock:
            lane = self.lanes.get(lane_id)
            if lane is None:
                lane = Lane(capacity, lease_seconds)
                self.lanes[lane_id] = lane
            elif (lane.capacity != capacity or
                  lane.lease_seconds != lease_seconds):
                raise LaneError("lane configuration mismatch")
            return lane

    def status(self) -> dict[str, Any]:
        with self.lock:
            lanes = dict(self.lanes)
        return {lane_id: lane.status() for lane_id, lane in sorted(lanes.items())}


def _auth(secret: bytes, request: dict[str, Any]) -> str:
    body = {key: value for key, value in request.items() if key != "auth"}
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    return hmac.new(secret, canonical, hashlib.sha256).hexdigest()


class LaneServer:
    """Threaded TCP service bound only to the local loopback interface."""

    def __init__(self, state_dir: Path, registry: LaneRegistry | None = None) -> None:
        self.state_dir = Path(state_dir)
        self.secret_path = self.state_dir / "lane.secret"
        self.endpoint_path = self.state_dir / "lane.json"
        self.registry = registry or LaneRegistry()
        self.secret = self._load_secret()
        self.auth_lock = threading.Lock()
        self.seen_nonces: dict[str, float] = {}

    def _load_secret(self) -> bytes:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        if self.secret_path.exists():
            return self.secret_path.read_bytes()
        secret = secrets.token_bytes(32)
        fd = os.open(self.secret_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(secret)
            handle.flush()
            os.fsync(handle.fileno())
        return secret

    def dispatch(self, request: Any) -> dict[str, Any]:
        if not isinstance(request, dict):
            return {"status": "invalid_request", "error": "object required"}
        supplied = str(request.get("auth", ""))
        nonce = request.get("nonce")
        timestamp = request.get("timestamp")
        if (not hmac.compare_digest(supplied, _auth(self.secret, request)) or
                not isinstance(nonce, str) or not nonce or
                not isinstance(timestamp, (int, float)) or
                abs(time.time() - timestamp) > 60):
            return {"status": "unauthorized"}
        with self.auth_lock:
            cutoff = time.time() - 60
            self.seen_nonces = {
                value: seen for value, seen in self.seen_nonces.items()
                if seen >= cutoff
            }
            if nonce in self.seen_nonces:
                return {"status": "unauthorized"}
            self.seen_nonces[nonce] = time.time()
        try:
            operation = request.get("operation")
            if operation == "status":
                return {"status": "completed", "lanes": self.registry.status()}
            lane = self.registry.lane(
                str(request.get("lane_id", "")),
                int(request.get("capacity", 1)),
                int(request.get("lease_seconds", 30)),
            )
            owner = str(request.get("owner", ""))
            token = str(request.get("token", ""))
            if operation == "acquire":
                lease = lane.acquire(
                    owner,
                    token or None,
                    float(request.get("timeout_seconds", 30)),
                )
                return {"status": "ready", "token": lease}
            if operation == "heartbeat":
                ok = lane.heartbeat(owner, token)
                return {"status": "ready" if ok else "invalid_lease"}
            if operation == "release":
                ok = lane.release(owner, token)
                return {"status": "completed" if ok else "invalid_lease"}
            raise LaneError("unknown operation")
        except TimeoutError as error:
            return {"status": "failed", "classification": "lane_timeout",
                    "error": str(error)}
        except (LaneError, TypeError, ValueError) as error:
            return {"status": "invalid_request", "error": str(error)}

    def serve_forever(
        self,
        host: str = "127.0.0.1",
        port: int = 0,
        idle_seconds: int = 300,
    ) -> None:
        server_ref = self

        class Handler(socketserver.StreamRequestHandler):
            def handle(self) -> None:
                line = self.rfile.readline(MAX_MESSAGE_BYTES + 1)
                if len(line) > MAX_MESSAGE_BYTES:
                    answer = {"status": "invalid_request",
                              "error": "message too large"}
                else:
                    try:
                        answer = server_ref.dispatch(json.loads(line))
                    except (UnicodeDecodeError, json.JSONDecodeError) as error:
                        answer = {"status": "invalid_request", "error": str(error)}
                self.wfile.write((json.dumps(answer, separators=(",", ":")) +
                                  "\n").encode())

        class Server(socketserver.ThreadingTCPServer):
            allow_reuse_address = True
            daemon_threads = True

        with Server((host, port), Handler) as server:
            started = time.monotonic()
            address, bound_port = server.server_address
            descriptor = {
                "schema_version": PROTOCOL_VERSION,
                "transport": "tcp-loopback",
                "host": address,
                "port": bound_port,
                "pid": os.getpid(),
                "secret_file": str(self.secret_path),
            }
            _atomic_json(self.endpoint_path, descriptor, 0o600)
            stopped = threading.Event()

            def stop_when_idle() -> None:
                while not stopped.wait(1):
                    statuses = self.registry.status()
                    if statuses:
                        idle = all(status["idle"] and
                                   status["idle_seconds"] >= idle_seconds
                                   for status in statuses.values())
                    else:
                        idle = time.monotonic() - started >= idle_seconds
                    if idle_seconds > 0 and idle:
                        server.shutdown()
                        return

            monitor = threading.Thread(target=stop_when_idle, daemon=True)
            monitor.start()
            try:
                server.serve_forever(poll_interval=0.2)
            finally:
                stopped.set()
                monitor.join(timeout=2)
                self.endpoint_path.unlink(missing_ok=True)


class LaneClient:
    """Client shared by the scheduler and provider integrations."""

    def __init__(self, endpoint_path: Path, timeout_seconds: float = 30) -> None:
        self.endpoint_path = Path(endpoint_path)
        self.timeout_seconds = timeout_seconds
        descriptor = json.loads(self.endpoint_path.read_text(encoding="utf-8"))
        if (descriptor.get("schema_version") != PROTOCOL_VERSION or
                descriptor.get("transport") != "tcp-loopback"):
            raise LaneError("unsupported lane endpoint")
        host = str(descriptor.get("host", ""))
        if host not in {"127.0.0.1", "::1", "localhost"}:
            raise LaneError("lane endpoint must use loopback")
        self.host = host
        self.port = int(descriptor["port"])
        self.secret_path = Path(descriptor["secret_file"])
        self.secret = self.secret_path.read_bytes()

    def request(self, operation: str, **fields: Any) -> dict[str, Any]:
        request = {"schema_version": PROTOCOL_VERSION,
                   "operation": operation, "timestamp": time.time(),
                   "nonce": secrets.token_urlsafe(18), **fields}
        request["auth"] = _auth(self.secret, request)
        payload = (json.dumps(request, separators=(",", ":")) + "\n").encode()
        with socket.create_connection((self.host, self.port), self.timeout_seconds) as client:
            client.sendall(payload)
            with client.makefile("rb") as handle:
                line = handle.readline(MAX_MESSAGE_BYTES + 1)
        if not line or len(line) > MAX_MESSAGE_BYTES:
            raise LaneError("invalid lane response")
        answer = json.loads(line)
        if not isinstance(answer, dict):
            raise LaneError("invalid lane response")
        return answer


def _atomic_json(path: Path, value: dict[str, Any], mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
