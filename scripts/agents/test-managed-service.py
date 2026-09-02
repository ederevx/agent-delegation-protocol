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
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "agents"))
import managed_service  # noqa: E402
from managed_service import (  # noqa: E402
    CONTROL_PREFIX,
    ClientRegistry,
    DeploymentError,
    GatewayAuditLog,
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
            "functions": ["audit"], "tier": "low",
        },
        "provider": {"id": "fake"},
        "credential": {"kind": "protocol_store", "reference": "fake"},
        "gateway": {
            "upstream": upstream, "allowed_methods": ["POST"],
            "allowed_paths": ["/v1/messages"],
            "credential_header": "Authorization",
            "credential_scheme": "Bearer", "resource": "provider",
            "timeout_seconds": 10, "max_request_bytes": 1048576,
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
        # DELEGATION_*_HOME are the only overrides honoured on every platform;
        # XDG_CONFIG_HOME alone leaks into the live store on Windows.
        self.old_environment = {
            name: os.environ.get(name)
            for name in ("DELEGATION_CONFIG_HOME", "DELEGATION_STATE_HOME",
                         "XDG_CONFIG_HOME", "XDG_STATE_HOME", "LOCALAPPDATA")
        }
        os.environ["DELEGATION_CONFIG_HOME"] = str(self.root / "config")
        os.environ["DELEGATION_STATE_HOME"] = str(self.root / "state-home")
        os.environ["XDG_CONFIG_HOME"] = str(self.root / "config")
        os.environ["XDG_STATE_HOME"] = str(self.root / "state-home")
        os.environ["LOCALAPPDATA"] = str(self.root / "localappdata")
        self.assertTrue(
            credential_path("fake").is_relative_to(self.root),
            "credential store escaped the disposable test home")
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
            except (ProcessLookupError, PermissionError):
                pass
            try:
                os.waitpid(pid, 0)
            except (ChildProcessError, OSError):
                pass
        self.upstream.shutdown()
        self.upstream.server_close()
        self.upstream_thread.join(timeout=2)
        for name, value in self.old_environment.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        self.temporary.cleanup()

    def test_atomic_writes_skip_windows_fchmod_and_keep_posix_modes(self):
        windows_json = self.root / "windows.json"
        windows_text = self.root / "windows.txt"
        with mock.patch.object(managed_service.os, "name", "nt"), \
                mock.patch.object(managed_service.os, "fchmod", create=True,
                                  side_effect=AssertionError("fchmod on Windows")), \
                mock.patch.object(managed_service, "_protect_windows_path") as protect:
            managed_service._atomic_json(windows_json, {"value": 1})
            managed_service._atomic_text(windows_text, "value")
        self.assertEqual(protect.call_count, 2)

        with mock.patch.object(managed_service.os, "name", "posix"), \
                mock.patch.object(managed_service.os, "fchmod", create=True) as chmod:
            managed_service._atomic_json(self.root / "posix.json", {}, mode=0o640)
            managed_service._atomic_text(self.root / "posix.txt", "", mode=0o600)
        self.assertEqual(
            [call.args[1] for call in chmod.call_args_list], [0o640, 0o600])

    def test_atomic_write_closes_descriptor_and_preserves_primary_error(self):
        real_close = os.close
        cases = {
            "state": lambda: managed_service._atomic_text(
                self.root / "failed-state.txt", "value"),
            "credential": lambda: write_credential(
                "failed", "value", self.root / "credentials"),
        }
        for name, invoke in cases.items():
            with self.subTest(name=name):
                descriptor, temporary = tempfile.mkstemp(dir=self.root)
                closed = []

                def close(value):
                    closed.append(value)
                    real_close(value)

                try:
                    with mock.patch.object(managed_service.tempfile, "mkstemp",
                                           return_value=(descriptor, temporary)), \
                            mock.patch.object(managed_service, "credential_path",
                                              return_value=self.root /
                                              "credentials" / "failed"), \
                            mock.patch.object(managed_service, "Path",
                                              type(self.root)), \
                            mock.patch.object(managed_service.os, "name", "posix"), \
                            mock.patch.object(managed_service.os, "fchmod", create=True,
                                              side_effect=OSError(
                                                  "permission failure")), \
                            mock.patch.object(managed_service.os, "close",
                                              side_effect=close), \
                            mock.patch.object(managed_service.os, "unlink",
                                              side_effect=OSError(
                                                  "cleanup failure")):
                        with self.assertRaisesRegex(OSError, "permission failure"):
                            invoke()
                    self.assertEqual(closed, [descriptor])
                finally:
                    Path(temporary).unlink(missing_ok=True)

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

    def test_store_roots_honour_delegation_overrides(self):
        """delegationctl and managed_service must resolve the same roots.

        The DELEGATION_*_HOME branch short-circuits before any platform test,
        so it is the one override that isolates a store on every platform.
        """
        import delegationctl

        configured = self.root / "configured"
        with mock.patch.dict(os.environ, {
                "DELEGATION_CONFIG_HOME": str(configured / "config"),
                "DELEGATION_STATE_HOME": str(configured / "state"),
                # Deliberately point the per-platform fallbacks somewhere else:
                # the explicit override must win regardless of which branch a
                # given platform would otherwise have taken.
                "LOCALAPPDATA": str(self.root / "unused-windows"),
                "XDG_CONFIG_HOME": str(self.root / "unused-posix"),
                "XDG_STATE_HOME": str(self.root / "unused-posix"),
        }):
            self.assertEqual(managed_service._config_root(),
                             delegationctl._config_root())
            self.assertEqual(managed_service._state_root(),
                             delegationctl._state_root())
            self.assertEqual(credential_path("fake"),
                             configured / "config" / "credentials" / "fake")
            self.assertEqual(
                managed_service._default_state_dir("test-provider"),
                configured / "state" / "services" / "test-provider")

    def test_windows_store_ignores_posix_only_overrides(self):
        """XDG_CONFIG_HOME must not be mistaken for isolation on Windows."""
        environment = {
            "XDG_CONFIG_HOME": str(self.root / "posix-only"),
            "LOCALAPPDATA": str(self.root / "windows"),
        }
        with mock.patch.object(managed_service.os, "name", "nt"):
            with mock.patch.dict(os.environ, environment):
                os.environ.pop("DELEGATION_CONFIG_HOME", None)
                self.assertEqual(
                    credential_path("fake"),
                    self.root / "windows" / "agent-delegation-protocol" /
                    "credentials" / "fake")

    def test_protected_credential_store(self):
        self.assertEqual(read_credential(credential_path("fake")),
                         "provider-secret")
        with self.assertRaisesRegex(DeploymentError, "raw token"):
            write_credential("assignment", "CHEAPESTINFERENCE_API_KEY=secret")
        with self.assertRaisesRegex(DeploymentError, "raw token"):
            write_credential("generic", "MY_PROVIDER_TOKEN=secret")
        with self.assertRaisesRegex(DeploymentError, "raw token"):
            write_credential("bare-name", "TOKEN=secret")
        write_credential("padded", "opaque-token==")
        self.assertEqual(read_credential(credential_path("padded")),
                         "opaque-token==")
        assignment = credential_path("read-assignment")
        for value in ("CHEAPESTINFERENCE_API_KEY=secret",
                      "MY_PROVIDER_SECRET=secret"):
            assignment.write_text(value + "\n", encoding="utf-8")
            if os.name == "nt":
                managed_service._protect_windows_path(assignment)
            else:
                os.chmod(assignment, 0o600)
            with self.assertRaisesRegex(DeploymentError, "raw token"):
                read_credential(assignment)
        if os.name != "nt":
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

    def test_draining_registry_rejects_late_registration(self):
        registry = ClientRegistry(2, 60, self.root / "drain-registry.json")
        self.assertTrue(registry.drain())
        self.assertIsNone(registry.register("late", os.getpid(), 0))

    def test_gateway_audit_rotation_is_private_and_bounded(self):
        path = self.root / "audit" / "gateway-audit.jsonl"
        audit = GatewayAuditLog(path, maximum_bytes=300)
        for index in range(12):
            audit.append("test-provider", "/v1/messages",
                         upstream_status=200 + index,
                         elapsed_seconds=index / 1000)
        self.assertTrue(audit.rotated_path.exists())
        for candidate in (path, audit.rotated_path):
            self.assertLessEqual(candidate.stat().st_size, 300)
            if os.name != "nt":
                self.assertEqual(candidate.stat().st_mode & 0o077, 0)
            for line in candidate.read_text(encoding="utf-8").splitlines():
                record = json.loads(line)
                self.assertEqual(record["deployment_id"], "test-provider")
                self.assertIn("elapsed_ms", record)
                self.assertNotIn("error_class", record)

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
            client.request("POST", "/v1/messages?query-secret=hidden",
                           body=b"payload", headers={
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
        audit_text = (self.state / "gateway-audit.jsonl").read_text(
            encoding="utf-8")
        admin_token = (self.state / "service.secret").read_text(
            encoding="ascii").strip()
        for secret in ("provider-secret", binding.token, admin_token,
                       "payload", "query-secret", "hidden", "attacker"):
            self.assertNotIn(secret, audit_text)
        audit_records = [json.loads(line) for line in audit_text.splitlines()]
        self.assertEqual(len(audit_records), 2)
        for record in audit_records:
            self.assertEqual(record["deployment_id"], "test-provider")
            self.assertEqual(record["path"], "/v1/messages")
            self.assertEqual(record["upstream_status"], 200)
            self.assertIsInstance(record["elapsed_ms"], (int, float))

            self.assertGreaterEqual(record["elapsed_ms"], 0)
            self.assertTrue(record["timestamp"].endswith("Z"))
        connection = http.client.HTTPConnection(
            binding.client.host, binding.client.port, timeout=10)
        connection.request(
            "POST", "/v1/messages", body=b"x" * (1024 * 1024 + 1),
            headers={"Authorization": "Bearer " + binding.token})
        response = connection.getresponse()
        self.assertEqual(response.status, 413)
        try:
            response.read()
        except OSError:
            # Some Windows TCP stacks reset the connection after the server
            # rejects an oversized request without consuming its body.
            pass
        connection.close()
        binding.close()
        self.assertTrue(answers[0].stop())

    def test_gateway_audit_records_sanitized_transport_error(self):
        unused = socket.socket()
        unused.bind(("127.0.0.1", 0))
        port = unused.getsockname()[1]
        unused.close()
        value = deployment(f"http://127.0.0.1:{port}")
        path = self.root / "unavailable-deployment.json"
        path.write_text(json.dumps(value), encoding="utf-8")
        state = self.root / "unavailable-state"
        client = ensure_service(path, state)
        descriptor = json.loads((state / "service.json").read_text())
        self.service_pids.add(descriptor["pid"])
        binding = client.register("unavailable-client")
        connection = http.client.HTTPConnection(client.host, client.port, timeout=10)
        connection.request("POST", "/v1/messages", body=b"private-body",
                           headers={"Authorization": "Bearer " + binding.token})
        response = connection.getresponse()
        self.assertEqual((response.status, response.read()),
                         (502, b"upstream unavailable\n"))
        connection.close()
        audit_path = state / "gateway-audit.jsonl"
        deadline = time.monotonic() + 2
        while audit_path.stat().st_size == 0 and time.monotonic() < deadline:
            time.sleep(0.01)
        record = json.loads(
            audit_path.read_text(encoding="utf-8"))
        self.assertEqual(record["error_class"], "transport_error")
        self.assertNotIn("upstream_status", record)
        self.assertEqual(record["path"], "/v1/messages")
        self.assertIn("elapsed_ms", record)
        raw = audit_path.read_text(encoding="utf-8")
        self.assertNotIn("private-body", raw)
        self.assertNotIn(binding.token, raw)
        binding.close()
        self.assertTrue(client.stop())

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
        second = registry.register("second", os.getpid(), 0)
        waiting = registry.register("waiting", os.getpid(), 0)
        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        self.assertIsNotNone(waiting)
        assert first is not None and second is not None and waiting is not None
        self.assertTrue(registry.retain(first.registration_id, {"bg-1", "bg-2"}))
        self.assertTrue(registry.retain(second.registration_id, {"bg-3"}))
        self.assertFalse(registry.retain(waiting.registration_id, {"bg-3"}))
        self.assertTrue(registry.unregister(waiting.registration_id))

        restarted = ClientRegistry(8, 60, state_file)
        restarted.reconcile_retained({"bg-2", "bg-3"})
        by_client = {item["client_id"]: item for item in restarted.snapshot()}
        self.assertEqual(by_client["first"]["retained_session_ids"], ["bg-2"])
        self.assertEqual(by_client["second"]["retained_session_ids"],
                         ["bg-3"])

        restarted.reconcile_retained({"bg-3"})
        by_client = {item["client_id"]: item for item in restarted.snapshot()}
        self.assertNotIn("first", by_client)
        self.assertEqual(by_client["second"]["retained_session_ids"], ["bg-3"])
        restarted.reconcile_retained(set())
        self.assertEqual(restarted.snapshot(), [])


if __name__ == "__main__":
    unittest.main()
