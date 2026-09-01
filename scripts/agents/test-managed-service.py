#!/usr/bin/env python3
from __future__ import annotations

import copy
import http.client
import json
import os
import signal
import socket
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "agents"))
from managed_service import (  # noqa: E402
    CONTROL_PREFIX,
    ClientRegistry,
    DeploymentError,
    ServiceClient,
    credential_path,
    ensure_service,
    load_deployment,
    read_credential,
    remove_credential,
    serve,
    validate_deployment,
    write_credential,
)


class UpstreamHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, _format, *_args):
        return

    def do_POST(self):
        with self.server.guard:
            self.server.active += 1
            self.server.maximum = max(self.server.maximum, self.server.active)
        try:
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length)
            self.server.received.append({
                "path": self.path, "authorization": self.headers.get("Authorization"),
                "api_key": self.headers.get("X-Api-Key"), "body": body,
            })
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            for chunk in (b"first", b"second"):
                self.wfile.write(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n")
                self.wfile.flush()
                time.sleep(0.08)
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        finally:
            with self.server.guard:
                self.server.active -= 1


class UpstreamServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self):
        self.guard = threading.Lock()
        self.active = 0
        self.maximum = 0
        self.received = []
        super().__init__(("127.0.0.1", 0), UpstreamHandler)


def deployment(upstream: str) -> dict:
    return {
        "schema_version": 1,
        "id": "test-provider",
        "selector": {
            "runtimes": ["test"], "platforms": ["linux"],
            "modes": ["read", "edit"],
            "workspaces": ["shared", "isolated"],
            "functions": ["audit"], "priority": 50,
        },
        "provider": {"id": "fake"},
        "credential": {"kind": "protocol_store", "reference": "fake"},
        "gateway": {
            "upstream": upstream, "allowed_methods": ["POST"],
            "allowed_paths": ["/v1/messages"],
            "credential_header": "Authorization",
            "credential_scheme": "Bearer", "resource": "provider",
            "timeout_seconds": 10,
        },
        "resources": [{"id": "provider", "capacity": 1,
                       "lease_seconds": 2, "wait_seconds": 5}],
        "service": {"idle_seconds": 30, "max_clients": 8,
                    "max_dependency_seconds": 60,
                    "retention_probe_seconds": 1},
        "runtime": {
            "profile": "test", "executable": {"command": "test-agent",
                                                  "environment": "TEST_BIN"},
            "session": {"config_dir": {"posix": "~/.test-agent",
                                        "windows": "%LOCALAPPDATA%/test-agent"},
                        "environment": "TEST_SESSION_DIR", "max_agents": 4,
                        "permission_mode": "auto"},
            "environment": {}, "arguments": [],
        },
        "inference": {"model": "fake-model", "thinking": {"type": "adaptive"},
                      "worker_effort": "low", "interactive_effort": "auto",
                      "context_tokens": 1000, "max_output_tokens": 100},
        "execution": {"workspace_engine": "git-isolated-v1",
                      "evidence": "git-patch-v1",
                      "permission_policy": "bounded-coding-v1"},
    }


class ManagedServiceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.old_config = os.environ.get("XDG_CONFIG_HOME")
        os.environ["XDG_CONFIG_HOME"] = str(self.root / "config")
        write_credential("fake", "provider-secret")
        self.upstream = UpstreamServer()
        self.upstream_thread = threading.Thread(
            target=self.upstream.serve_forever, daemon=True)
        self.upstream_thread.start()
        value = deployment(
            f"http://127.0.0.1:{self.upstream.server_address[1]}")
        self.deployment_path = self.root / "deployment.json"
        self.deployment_path.write_text(json.dumps(value), encoding="utf-8")
        self.state = self.root / "state"
        self.service_pids: set[int] = set()

    def tearDown(self):
        for pid in self.service_pids:
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                os.waitpid(pid, 0)
            except (ChildProcessError, ProcessLookupError):
                pass
        self.upstream.shutdown()
        self.upstream.server_close()
        self.upstream_thread.join(timeout=2)
        if self.old_config is None:
            os.environ.pop("XDG_CONFIG_HOME", None)
        else:
            os.environ["XDG_CONFIG_HOME"] = self.old_config
        self.temporary.cleanup()

    def start(self) -> ServiceClient:
        client = ensure_service(self.deployment_path, self.state)
        descriptor = json.loads((self.state / "service.json").read_text())
        self.service_pids.add(descriptor["pid"])
        return client

    def test_exact_validation_and_secret_free_configuration(self):
        value = load_deployment(self.deployment_path)
        self.assertEqual(value["inference"]["model"], "fake-model")
        changed = copy.deepcopy(value)
        changed["provider"]["model"] = "legacy"
        with self.assertRaisesRegex(DeploymentError, "unknown fields"):
            validate_deployment(changed)
        changed = copy.deepcopy(value)
        changed["runtime"]["environment"]["api_key"] = "embedded"
        with self.assertRaisesRegex(DeploymentError, "may not contain a secret"):
            validate_deployment(changed)
        changed = copy.deepcopy(value)
        changed["inference"]["thinking"] = {"type": "enabled"}
        with self.assertRaisesRegex(DeploymentError, "budget_tokens is required"):
            validate_deployment(changed)
        changed["inference"]["thinking"] = {
            "type": "adaptive", "budget_tokens": 1024}
        with self.assertRaisesRegex(DeploymentError, "valid only when enabled"):
            validate_deployment(changed)
        changed = copy.deepcopy(value)
        changed["credential"] = {"kind": "protected_file", "reference": "/tmp/key"}
        with self.assertRaisesRegex(DeploymentError, "protocol_store"):
            validate_deployment(changed)

    def test_protected_credential_store(self):
        self.assertEqual(read_credential(credential_path("fake")),
                         "provider-secret")
        os.chmod(credential_path("fake"), 0o644)
        with self.assertRaisesRegex(DeploymentError, "broader than 0600"):
            read_credential(credential_path("fake"))
        with self.assertRaisesRegex(DeploymentError, "reference is invalid"):
            credential_path("../escape")

    def test_retained_clients_fail_closed_when_preferred_port_is_occupied(self):
        state = self.root / "occupied-state"
        state.mkdir()
        occupied = socket.socket()
        occupied.bind(("127.0.0.1", 0))
        try:
            port = occupied.getsockname()[1]
            (state / "service.port").write_text(f"{port}\n", encoding="ascii")
            (state / "registrations.json").write_text(json.dumps([{
                "registration_id": "retained", "client_id": "background",
                "token": "dummy", "pid": None, "process_identity": None,
                "last_seen": time.time(), "retained": True,
            }]), encoding="utf-8")
            with self.assertRaisesRegex(
                    DeploymentError, "unavailable for retained clients"):
                serve(self.deployment_path, state)
        finally:
            occupied.close()
        write_credential("rotated", "second-secret")
        self.assertEqual(read_credential(credential_path("rotated")),
                         "second-secret")
        self.assertTrue(remove_credential("rotated"))
        self.assertFalse(remove_credential("rotated"))

    def test_singleton_binding_auth_header_filtering_and_fifo(self):
        answers: list[ServiceClient] = []
        threads = [threading.Thread(
            target=lambda: answers.append(
                ensure_service(self.deployment_path, self.state)))
                   for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=15)
        self.assertEqual(len(answers), 4)
        self.assertEqual(len({answer.port for answer in answers}), 1)
        descriptor = json.loads((self.state / "service.json").read_text())
        self.service_pids.add(descriptor["pid"])
        binding = answers[0].register("test-client")

        connection = http.client.HTTPConnection(answers[0].host, answers[0].port)
        connection.request("GET", CONTROL_PREFIX + "binding", headers={
            "Authorization": "Bearer " + binding.token})
        response = connection.getresponse()
        identity = json.loads(response.read())
        connection.close()
        self.assertEqual(identity, {"deployment_id": "test-provider",
                                    "registration_id": binding.registration_id})

        results = []
        def request():
            client = http.client.HTTPConnection(binding.client.host,
                                                binding.client.port, timeout=10)
            client.request("POST", "/v1/messages", body=b"payload", headers={
                "Authorization": "Bearer " + binding.token,
                "X-Api-Key": "attacker", "Content-Type": "application/json"})
            answer = client.getresponse()
            results.append((answer.status, answer.read()))
            client.close()
        request_threads = [threading.Thread(target=request) for _ in range(2)]
        for thread in request_threads:
            thread.start()
        for thread in request_threads:
            thread.join(timeout=10)
        self.assertEqual(results, [(200, b"firstsecond"), (200, b"firstsecond")])
        self.assertEqual(self.upstream.maximum, 1)
        self.assertTrue(all(item["authorization"] == "Bearer provider-secret"
                            and item["api_key"] is None
                            for item in self.upstream.received))
        binding.close()
        self.assertTrue(answers[0].stop())

    @unittest.skipIf(os.name == "nt", "SIGKILL restart exercised on POSIX")
    def test_retained_binding_survives_service_restart_on_preferred_port(self):
        client = self.start()
        binding = client.register("background-client")
        self.assertTrue(binding.retain({"bg-restart"}))
        old_port = client.port
        descriptor = json.loads((self.state / "service.json").read_text())
        old_pid = descriptor["pid"]
        os.kill(old_pid, signal.SIGKILL)
        os.waitpid(old_pid, 0)
        self.service_pids.discard(old_pid)
        restarted = self.start()
        self.assertEqual(restarted.port, old_port)
        connection = http.client.HTTPConnection(restarted.host, restarted.port)
        connection.request("POST", "/v1/messages", body=b"after-restart",
                           headers={"Authorization": "Bearer " + binding.token})
        response = connection.getresponse()
        self.assertEqual((response.status, response.read()),
                         (200, b"firstsecond"))
        connection.close()
        retained = restarted.status()["clients"]
        self.assertEqual(retained[0]["retained_session_ids"], ["bg-restart"])

    def test_retained_session_ownership_reconciles_across_restart(self):
        state_file = self.root / "registry.json"
        registry = ClientRegistry(8, 60, state_file)
        first = registry.register("first", os.getpid(), 0)
        wildcard = registry.register("wildcard", os.getpid(), 0)
        self.assertIsNotNone(first)
        self.assertIsNotNone(wildcard)
        assert first is not None and wildcard is not None
        self.assertTrue(registry.retain(first.registration_id, {"bg-1", "bg-2"}))
        self.assertTrue(registry.retain(wildcard.registration_id))

        restarted = ClientRegistry(8, 60, state_file)
        restarted.reconcile_retained({"bg-2", "bg-3"})
        by_client = {item["client_id"]: item for item in restarted.snapshot()}
        self.assertEqual(by_client["first"]["retained_session_ids"], ["bg-2"])
        self.assertEqual(by_client["wildcard"]["retained_session_ids"],
                         ["bg-2", "bg-3"])

        restarted.reconcile_retained({"bg-3"})
        by_client = {item["client_id"]: item for item in restarted.snapshot()}
        self.assertNotIn("first", by_client)
        self.assertEqual(by_client["wildcard"]["retained_session_ids"], ["bg-3"])
        restarted.reconcile_retained(set())
        self.assertEqual(restarted.snapshot(), [])


if __name__ == "__main__":
    unittest.main()
