"""Local two-value orchestration, installation and credential evidence; no real keys."""
import hashlib
import io
import json
import os
import queue
import stat
import sys
import tempfile
import threading
import time
import unittest
import zipfile
from contextlib import redirect_stderr
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import admin_in_the_loop as a


CLIENT_RUN_HELP = """Usage of run:
      --control-plane.api-key string
      --control-plane.tunnel-id string
      --mcp.server-url stringArray
      --mcp.extra-headers stringArray
      --mcp.discovery-extra-headers stringArray
      --health.listen-addr string
      --health.url-file string
"""
FULL_VERSION = "0.0.15+a390c168ff1b2d14e73a95991c186c6aba3ff5a0 (git sha: a390c168ff1b2d14e73a95991c186c6aba3ff5a0)"
RUNTIME_VERSION = "0.0.15 git sha: a390c168ff1b2d14e73a95991c186c6aba3ff5a0 go: go1.27.0 build flags: -trimpath -buildvcs=false flavor=runtime"


def fake_client(path, version="0.0.15", help_output=CLIENT_RUN_HELP, help_exit=0):
    path.write_text("#!/bin/sh\ncase \"$*\" in\n"
                    "  --version) printf '%s\\n' " + a.shlex.quote(version) + ";;\n"
                    "  'run --help') printf '%s\\n' " + a.shlex.quote(help_output) +
                    "; exit " + str(help_exit) + ";;\n  *) exit 2;;\nesac\n")
    path.chmod(0o700)
    return path


def fake_brew(directory, exit_code=0, output="", version="0.0.15"):
    prefix = Path(directory)
    (prefix / "bin").mkdir(exist_ok=True)
    client = prefix / "bin" / "tunnel-client"
    fake_client(client, version)
    brew = prefix / "bin" / "brew"
    brew.write_text("#!/bin/sh\nif [ \"$1\" = '--prefix' ]; then\n printf '%s\\n' " + a.shlex.quote(str(prefix)) +
                    "\nelse\n printf '%s\\n' " + a.shlex.quote(output) + " >&2\n exit " + str(exit_code) + "\nfi\n")
    brew.chmod(0o700)
    return brew


def wait(core, predicate, timeout=8):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        core.pump()
        if predicate():
            return
        time.sleep(.01)
    raise AssertionError("local_setup_timeout")


class ClientIdentificationTests(unittest.TestCase):
    def test_real_full_runtime_formats_and_newer_version_use_run_capabilities(self):
        with tempfile.TemporaryDirectory() as directory:
            # Identity must not depend on either filename or name in version output.
            path = Path(directory, "renamed-client")
            for version in (FULL_VERSION, RUNTIME_VERSION, "0.0.16", "1.0.0"):
                with self.subTest(version=version):
                    fake_client(path, version)
                    diagnostics = []
                    self.assertEqual(a.compatible_tunnel_binary(path, diagnostics), str(path))
                    self.assertEqual(diagnostics, [])

    def test_old_unknown_and_nonclient_binaries_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "tunnel-client")
            cases = [("0.0.14", CLIENT_RUN_HELP, 0, "client_version_unsupported"),
                     ("unknown", CLIENT_RUN_HELP, 0, "client_version_unsupported"),
                     ("unrelated-tool 0.0.15", "Usage: unrelated-tool", 0, "client_probe_failed"),
                     (FULL_VERSION, "Usage of run:\n  --unrelated-option string", 0, "client_probe_failed"),
                     (RUNTIME_VERSION, CLIENT_RUN_HELP, 2, "client_probe_failed"),
                     (RUNTIME_VERSION, CLIENT_RUN_HELP.replace("--health.url-file", "--health.url-file-fake"),
                      0, "client_probe_failed")]
            for version, help_text, exit_code, reason in cases:
                with self.subTest(version=version, help_text=help_text, exit_code=exit_code):
                    fake_client(path, version, help_text, exit_code)
                    diagnostics = []
                    self.assertIsNone(a.compatible_tunnel_binary(path, diagnostics))
                    self.assertEqual(diagnostics[-1].code, reason)

    def test_probe_failures_and_isolated_no_credentials_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            path = fake_client(Path(directory, "client"), FULL_VERSION)
            version = a.subprocess.CompletedProcess([], 0, FULL_VERSION.encode(), b"")
            with mock.patch.object(a.subprocess, "run", side_effect=[version, a.subprocess.TimeoutExpired("probe", 5)]) as run:
                diagnostics = []
                self.assertIsNone(a.compatible_tunnel_binary(path, diagnostics))
                self.assertEqual(diagnostics[-1].component, "tunnel-client run --help")
                self.assertEqual(run.call_args.args[0], [str(path), "run", "--help"])
                self.assertNotIn("CONTROL_PLANE_API_KEY", run.call_args.kwargs["env"])
                self.assertNotIn("SSH_AUTH_SOCK", run.call_args.kwargs["env"])
                self.assertEqual(run.call_args.kwargs["timeout"], 5)
            help_result = a.subprocess.CompletedProcess([], 0, b"", CLIENT_RUN_HELP.encode())
            with mock.patch.object(a.subprocess, "run", side_effect=[version, help_result]):
                self.assertEqual(a.compatible_tunnel_binary(path), str(path))


class InstallationTests(unittest.TestCase):
    def archive(self, extra=None):
        data = io.BytesIO()
        stem = "tunnel-client-runtime-v0.0.15-linux-arm64"
        with zipfile.ZipFile(data, "w") as archive:
            for name in ("tunnel-client-runtime", "LICENSE", "NOTICE", stem + "-licenses.txt", stem + ".spdx.json"):
                archive.writestr(name, b"synthetic fixture")
            if extra:
                archive.writestr(extra, b"not allowed")
        return data.getvalue()

    def test_checksum_permissions_and_rejected_archive_do_not_publish_binary(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory, "install")
            archive = self.archive()
            with mock.patch.dict(a.TUNNEL_ARCHIVE_SHA256, {"arm64": hashlib.sha256(archive).hexdigest()}):
                binary = a.install_tunnel_archive(archive, target, "arm64", threading.Event())
                self.assertEqual(Path(binary).read_bytes(), b"synthetic fixture")
                self.assertEqual(target.stat().st_mode & 0o777, 0o700)
                self.assertEqual(Path(binary).stat().st_mode & 0o777, 0o700)
                self.assertEqual((target / "LICENSE").stat().st_mode & 0o777, 0o600)
                with self.assertRaisesRegex(a.ProbeError, "client_checksum_mismatch"):
                    a.install_tunnel_archive(archive + b"corrupt", target, "arm64", threading.Event())
                self.assertEqual(Path(binary).read_bytes(), b"synthetic fixture")
            unsafe = self.archive("../escape")
            with mock.patch.dict(a.TUNNEL_ARCHIVE_SHA256, {"arm64": hashlib.sha256(unsafe).hexdigest()}):
                with self.assertRaisesRegex(a.ProbeError, "unsafe_client_archive"):
                    a.install_tunnel_archive(unsafe, target, "arm64", threading.Event())
            self.assertFalse(Path(directory, "escape").exists())
            cancelled = threading.Event()
            cancelled.set()
            with mock.patch.dict(a.TUNNEL_ARCHIVE_SHA256, {"arm64": hashlib.sha256(archive).hexdigest()}):
                with self.assertRaisesRegex(a.ProbeError, "local_setup_cancelled"):
                    a.install_tunnel_archive(archive, target, "arm64", cancelled)

    def test_existing_client_is_discovered_without_download_and_unsafe_file_is_not_run(self):
        with tempfile.TemporaryDirectory() as directory:
            binary = Path(directory, "tunnel-client")
            fake_client(binary, FULL_VERSION)
            with mock.patch.object(a, "download_tunnel_archive") as download:
                found = a.ensure_tunnel_client(directory, str(binary), threading.Event(), lambda _: None)
                self.assertEqual(found, str(binary))
                download.assert_not_called()
            binary.chmod(0o777)
            with mock.patch.object(a.subprocess, "run") as run:
                self.assertIsNone(a.compatible_tunnel_binary(binary))
                run.assert_not_called()

    def test_macos_uses_official_brew_without_runtime_credentials_or_gatekeeper_bypass(self):
        with tempfile.TemporaryDirectory() as directory:
            brew = fake_brew(directory)
            original = a.subprocess.Popen
            with mock.patch.object(a.sys, "platform", "darwin"), \
                    mock.patch.object(a.shutil, "which", side_effect=lambda name: str(brew) if name == "brew" else None), \
                    mock.patch.object(a.subprocess, "Popen", wraps=original) as popen:
                found = a.ensure_tunnel_client("/unused", None, threading.Event(), lambda _: None)
                self.assertEqual(found, str(Path(directory, "bin", "tunnel-client")))
                call = next(call for call in popen.call_args_list if "install" in call.args[0])
                self.assertEqual(call.args[0], [str(brew), "install", "openai/tools/tunnel-client"])
                self.assertNotIn("CONTROL_PLANE_API_KEY", call.kwargs["env"])
                self.assertNotIn("SSH_AUTH_SOCK", call.kwargs["env"])
                self.assertEqual(call.kwargs["env"]["HOMEBREW_NO_SUDO"], "1")
                self.assertTrue(call.kwargs["start_new_session"])
        with mock.patch.object(a.sys, "platform", "darwin"), \
                mock.patch.object(a.shutil, "which", return_value=None), \
                mock.patch.object(a.Path, "is_file", return_value=False), \
                mock.patch.object(a, "compatible_tunnel_binary", return_value=None), \
                mock.patch.object(a, "download_tunnel_archive") as download:
            with self.assertRaisesRegex(a.ProbeError, "homebrew_required"):
                a.ensure_tunnel_client("/unused", None, threading.Event(), lambda _: None)
            download.assert_not_called()

    def test_successful_brew_install_of_old_tap_version_is_reported_as_client_check_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            brew = fake_brew(directory, version="0.0.14")
            with mock.patch.object(a.sys, "platform", "darwin"), \
                    mock.patch.object(a.shutil, "which", side_effect=lambda name: str(brew) if name == "brew" else None):
                with self.assertRaises(a.SetupFailure) as failure:
                    a.ensure_tunnel_client("/unused", None, threading.Event(), lambda _: None)
            diagnostic = failure.exception.diagnostic
            self.assertEqual(diagnostic.code, "client_version_unsupported")
            self.assertEqual(diagnostic.phase, "CLIENT_CHECK")
            self.assertEqual(diagnostic.exit_code, 0)
            self.assertIn("0.0.14", diagnostic.note)
            self.assertIn("0.0.15", diagnostic.note)
            self.assertIn(str(Path(directory, "Cellar", "tunnel-client")), diagnostic.target)

    def test_spawn_permission_error_preserves_errno_without_echoing_exception_secret(self):
        with mock.patch.object(a.subprocess, "Popen", side_effect=PermissionError(13, "SYNTHETIC_SECRET_KEY")):
            with self.assertRaises(a.SetupFailure) as failure:
                a.setup_command(["brew", "install"], {}, threading.Event(), "INSTALLING", "Homebrew", "/opt/homebrew/Cellar/tunnel-client")
        diagnostic = failure.exception.diagnostic.safe(("SYNTHETIC_SECRET_KEY",))
        self.assertIn("PermissionError", diagnostic.exception)
        self.assertIn("errno=13", diagnostic.exception)
        self.assertNotIn("SYNTHETIC_SECRET_KEY", diagnostic.text())
        self.assertIn("beschreiben", diagnostic.cause())

    def test_failing_installer_propagates_phase_target_exit_output_to_core_and_stderr(self):
        with tempfile.TemporaryDirectory() as directory:
            secret = "SYNTHETIC_INSTALL_API_SECRET"
            brew = fake_brew(directory, exit_code=23, output="Error: Permission denied\nAPI_KEY=" + secret + "\nAuthorization: Bearer abcdefghijklmnopqrstuvwxyz123456789")
            core = a.AppCore(connection_store=a.ConnectionStore(Path(directory, "config")))
            try:
                with mock.patch.object(a.sys, "platform", "darwin"), \
                        mock.patch.object(a.shutil, "which", side_effect=lambda name: str(brew) if name == "brew" else None), \
                        redirect_stderr(io.StringIO()) as console:
                    core.begin_local_setup("tunnel_" + "a" * 32, secret)
                    wait(core, lambda: core.local_setup_state == "FAILED")
                diagnostic = core.local_setup_diagnostic
                self.assertEqual(diagnostic.phase, "INSTALLING")
                self.assertEqual(diagnostic.exit_code, 23)
                self.assertIn("Permission denied", diagnostic.stderr)
                self.assertIn("Homebrew", console.getvalue())
                self.assertIn("Exit-Code: 23", console.getvalue())
                self.assertIn(diagnostic.target, console.getvalue())
                self.assertNotIn(secret, console.getvalue())
                self.assertNotIn("abcdefghijklmnopqrstuvwxyz123456789", console.getvalue())
                self.assertIsNone(core.connection_store.load_connection())
            finally:
                core.close()

    def test_cancelled_background_bootstrap_cannot_handoff_owned_process_late(self):
        entered, release = threading.Event(), threading.Event()
        process = mock.Mock()
        def bootstrap():
            entered.set()
            release.wait(3)
            return process
        with mock.patch.object(a, "ensure_tunnel_client", return_value="/fake/client"), \
                mock.patch.object(a.subprocess, "run", return_value=mock.Mock(returncode=0)), \
                mock.patch.object(a, "LocalBridgeProcess", side_effect=bootstrap):
            preparation = a.LocalSetupPreparation("/unused", None, "tunnel_" + "a" * 32, "SYNTHETIC_RUNTIME_KEY")
            self.assertTrue(entered.wait(2))
            preparation.close()
            release.set()
            preparation.thread.join(3)
            self.assertFalse(preparation.thread.is_alive())
            process.close.assert_called_once()
            self.assertFalse(any(kind == "prepared" for kind, _ in list(preparation.events.queue)))


class PollEvidenceTests(unittest.TestCase):
    def test_ready_only_missing_success_unauthorized_and_unknown_schema_are_not_accepted(self):
        payload = {"schema_version": 1, "component": "control-plane", "status": "unknown",
                   "details": {"consecutive_failures": 0}}
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.server.paths.append(self.path)
                raw = json.dumps(payload).encode()
                self.send_response(200)
                self.end_headers()
                self.wfile.write(raw)
            def log_message(self, *_):
                pass
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.paths = []
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        with tempfile.TemporaryDirectory() as directory:
            tunnel = a.TunnelProcess.__new__(a.TunnelProcess)
            tunnel.directory = Path(directory)
            path = tunnel.directory / "health-url"
            path.write_text("http://127.0.0.1:" + str(server.server_port))
            path.chmod(0o600)
            tunnel.state = "READY"
            self.assertEqual(tunnel.polling_check(), (False, None))
            payload["status"] = "ok"
            payload["details"]["last_success"] = "2026-09-30T12:00:00Z"
            self.assertEqual(tunnel.polling_check(), (True, None))
            for status, reason in ((401, "runtime_key_rejected"), (403, "runtime_permission_denied"), (404, "runtime_tunnel_not_found")):
                payload["details"]["http_status"] = status
                with self.assertRaisesRegex(a.ProbeError, reason):
                    tunnel.polling_check()
            del payload["details"]["http_status"]
            payload["schema_version"] = True
            with self.assertRaisesRegex(a.ProbeError, "client_health_unsupported"):
                tunnel.polling_check()
            self.assertTrue(all(path == "/health/control-plane" for path in server.paths))


class LocalFlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="aitl-local-flow-")
        self.addCleanup(self.tmp.cleanup)
        self.store = a.ConnectionStore(Path(self.tmp.name, "config"))
        self.core = a.AppCore(connection_store=self.store)
        self.addCleanup(self.core.close)
        self.binary = Path(self.tmp.name, "client")
        self.binary.write_text("#!/bin/sh\nexit 0\n")
        self.binary.chmod(0o700)
        self.config = {"binary": str(self.binary), "tunnel_id": "tunnel_" + "a" * 32, "api_key": "SYNTHETIC_RUNTIME_KEY"}

    def prepared(self, client):
        preparation = mock.Mock(events=queue.Queue())
        patch = mock.patch.object(a, "LocalSetupPreparation", return_value=preparation)
        patch.start()
        self.addCleanup(patch.stop)
        patch = mock.patch.object(a, "TunnelProcess", return_value=client)
        patch.start()
        self.addCleanup(patch.stop)
        self.core.begin_local_setup(self.config["tunnel_id"], self.config["api_key"])
        process = a.LocalBridgeProcess()
        preparation.events.put(("prepared", (process, self.config)))
        wait(self.core, lambda: self.core.tunnel is client)
        return process

    def test_persist_only_after_poll_success_and_local_readiness_preserving_remote_profile(self):
        remote = a.ConnectionProfile("https://bridge.example:8443", "b" * 64, "c" * 32, "d" * 32, "e" * 64)
        self.store.save_connection(remote)
        before = (self.store.directory / "connection.json").read_bytes()
        client = mock.Mock(state="READY", error=None)
        client.polling_check.return_value = (False, None)
        self.prepared(client)
        with self.assertRaisesRegex(a.ProbeError, "setup_busy"):
            self.core.begin_local_setup(self.config["tunnel_id"], self.config["api_key"])
        self.core.local_check_at = 0
        self.core.pump()
        self.assertEqual((self.store.directory / "connection.json").read_bytes(), before)
        client.polling_check.return_value = (True, None)
        client.state = "RUNNING_NOT_READY"
        self.core.local_check_at = 0
        self.core.pump()
        self.assertEqual((self.store.directory / "connection.json").read_bytes(), before)
        client.state = "READY"
        self.core.local_check_at = 0
        wait(self.core, lambda: self.core.local_setup_state == "CONNECTED")
        saved = self.store.load_connection()
        self.assertEqual(saved["tunnel"], self.config)
        self.assertEqual(saved["profile"]["token"], remote.token)
        self.assertIsNone(self.core.review)

    def test_denied_credentials_and_timeout_preserve_previous_save_and_close_owned_children(self):
        self.store.save_connection(None, self.config)
        before = (self.store.directory / "connection.json").read_bytes()
        self.config = dict(self.config, api_key="SYNTHETIC_INVALID_NEW_KEY")
        client = mock.Mock(state="READY", error=None)
        client.polling_check.side_effect = a.ProbeError("runtime_permission_denied")
        process = self.prepared(client)
        wait(self.core, lambda: self.core.local_setup_state == "FAILED")
        self.assertEqual(self.core.local_setup_error, "runtime_permission_denied")
        self.assertEqual((self.store.directory / "connection.json").read_bytes(), before)
        client.close.assert_called_once()
        self.assertIsNone(self.core.bridge_process)
        self.assertFalse(process.directory.exists())
        client = mock.Mock(state="READY", error=None)
        client.polling_check.return_value = (False, None)
        process = self.prepared(client)
        self.core.local_setup_deadline = 0
        self.core.pump()
        self.assertEqual(self.core.local_setup_error, "local_setup_timeout")
        self.assertEqual((self.store.directory / "connection.json").read_bytes(), before)
        self.assertFalse(process.directory.exists())

    @unittest.skipUnless(os.environ.get("AITL_TEST_TUNNEL_BINARY"), "official client binary not supplied")
    def test_official_runtime_actual_poll_acceptance_and_auth_failures(self):
        for status in (204, 401, 403, 404):
            with self.subTest(status=status):
                class Handler(BaseHTTPRequestHandler):
                    def do_GET(handler):
                        authenticated = handler.headers.get("Authorization") == "Bearer " + self.config["api_key"]
                        handler.server.external_key_seen = handler.server.external_key_seen or authenticated
                        if "/poll" in handler.path:
                            time.sleep(.05)
                            handler.send_response(status if authenticated else 401)
                            handler.end_headers()
                        else:
                            raw = json.dumps({"id": self.config["tunnel_id"], "name": "local-simulator"}).encode()
                            handler.send_response(200)
                            handler.end_headers()
                            handler.wfile.write(raw)
                    def do_POST(handler):
                        handler.send_response(200)
                        handler.end_headers()
                    def log_message(handler, *_):
                        pass
                server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
                server.external_key_seen = False
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                binary = os.environ["AITL_TEST_TUNNEL_BINARY"]
                original = a.subprocess.Popen
                def local_only(argv, **kwargs):
                    if argv[0] == binary and "run" in argv:
                        argv = argv + ["--control-plane.base-url", "http://127.0.0.1:" + str(server.server_port)]
                    return original(argv, **kwargs)
                try:
                    with mock.patch.object(a, "ensure_tunnel_client", return_value=binary), \
                            mock.patch.object(a.subprocess, "Popen", side_effect=local_only):
                        self.core.begin_local_setup(self.config["tunnel_id"], self.config["api_key"])
                        wait(self.core, lambda: self.core.local_setup_state in {"CONNECTED", "FAILED"}, timeout=12)
                    self.assertEqual(self.core.local_setup_state, "CONNECTED" if status == 204 else "FAILED")
                    self.assertTrue(server.external_key_seen)
                    if status != 204:
                        self.assertEqual(self.core.local_setup_error, {401: "runtime_key_rejected", 403: "runtime_permission_denied", 404: "runtime_tunnel_not_found"}[status])
                        self.assertIsNone(self.core.tunnel)
                        self.assertIsNone(self.core.bridge_process)
                finally:
                    self.core.disconnect_bridge()
                    server.shutdown()
                    server.server_close()
                    thread.join(2)


class CompactConnectionPresentationTests(unittest.TestCase):
    def test_local_ready_requires_actual_local_setup_and_tunnel_evidence(self):
        core = a.AppCore()
        self.addCleanup(core.close)
        bridge = a.replace(core.bridge_snapshot(), state="CONNECTED", mode="LOCAL", tunnel_state="READY")
        with mock.patch.object(core, "bridge_snapshot", return_value=bridge):
            self.assertNotIn("Tunnel-Zugriff geprüft", a.local_setup_summary(core))
            core.local_setup_state = "CONNECTED"
            self.assertEqual(a.local_setup_summary(core), "Verbindung bereit · Tunnel-Zugriff geprüft")
        for changed in (a.replace(bridge, tunnel_state="RUNNING_NOT_READY"),
                        a.replace(bridge, state="DISCONNECTED")):
            with mock.patch.object(core, "bridge_snapshot", return_value=changed):
                self.assertNotIn("Tunnel-Zugriff geprüft", a.local_setup_summary(core))

    def test_remote_summary_never_borrows_local_ready_evidence(self):
        core = a.AppCore()
        self.addCleanup(core.close)
        core.local_setup_state = "CONNECTED"  # Even if a stale local state remains.
        bridge = a.replace(core.bridge_snapshot(), state="CONNECTED", mode="REMOTE", tunnel_state="READY")
        with mock.patch.object(core, "bridge_snapshot", return_value=bridge):
            summary = a.local_setup_summary(core)
            self.assertIn("authentifiziert", summary)
            self.assertIn("Tunnelbetrieb am Gateway", summary)
            self.assertNotIn("Tunnel-Zugriff geprüft", summary)
            self.assertNotIn("Round", summary)
        with mock.patch.object(core, "bridge_snapshot", return_value=a.replace(bridge, state="BLOCKED")):
            self.assertIn("gesperrt", a.local_setup_summary(core))


class AppVersionConsistencyTests(unittest.TestCase):
    """Versionsregel (Journal 8.10.): jede Auslieferung erhoehrt mindestens den
    Patch-Stand; APP_VERSION (kanonisch) und BRIDGE_SERVER_INFO (Bridge-Kopie)
    muessen identisch sein; --version existiert im Haupt-Parser."""

    def test_version_sources_pinned(self):
        self.assertEqual(a.APP_VERSION, "0.9.1")
        self.assertEqual(a.BRIDGE_SERVER_INFO["name"], "admin-in-the-loop")
        self.assertEqual(a.BRIDGE_SERVER_INFO["version"], a.APP_VERSION)

class BridgeSetupDefaultsTests(unittest.TestCase):
    """M9: home-state defaults, path resolution, auto SAN, usable URLs, policy line."""

    def test_default_state_dir_is_private_home_path(self):
        with mock.patch.object(a.Path, "home", return_value=Path("/home/operator")):
            self.assertEqual(a.default_state_dir(), Path("/home/operator/.local/state/aitl-bridge"))

    def test_resolve_state_dir_expands_tilde_relative_and_spaces(self):
        with mock.patch.dict(os.environ, {"HOME": "/home/operator"}):
            self.assertEqual(a.resolve_state_dir("~/.aitl x"), "/home/operator/.aitl x")
        with mock.patch.object(a.Path, "cwd", return_value=Path("/tmp/base")):
            self.assertEqual(a.resolve_state_dir("relative dir"), "/tmp/base/relative dir")
        self.assertEqual(a.resolve_state_dir("/abs/path"), "/abs/path")
        with self.assertRaises(a.BridgeError):
            a.resolve_state_dir("   ")

    def test_resolve_bridge_runtime_defaults_and_bootstrap_contract(self):
        # Standalone: 0.0.0.0:8443 without prompting, automatic SAN names.
        bind, port, names = a.resolve_bridge_runtime(None, None, [], False)
        self.assertEqual((bind, port), ("0.0.0.0", 8443))
        self.assertEqual(names[:2], ["localhost", "127.0.0.1"])
        # Explicit overrides win; internal bootstrap stays loopback without TLS.
        self.assertEqual(a.resolve_bridge_runtime("127.0.0.1", 9000, ["x.example"], False)[0:2],
                         ("127.0.0.1", 9000))
        self.assertEqual(a.resolve_bridge_runtime(None, None, [], True), ("127.0.0.1", 0, []))

    def test_default_tls_names_and_usable_urls(self):
        with mock.patch.object(a, "local_interface_ips", return_value=["192.168.1.50", "192.168.1.50"]):
            names = a.default_tls_names(["bridge.example", "192.168.1.50"])
        self.assertEqual(names[:2], ["localhost", "127.0.0.1"])
        self.assertIn("192.168.1.50", names)
        self.assertIn("bridge.example", names)
        self.assertEqual(len(names), len(set(names)))
        # A wildcard bind is never shown as a usable destination URL.
        self.assertEqual(a.usable_urls("0.0.0.0", 8443, ["0.0.0.0", "localhost", "192.168.1.50"]),
                         ["https://localhost:8443", "https://192.168.1.50:8443"])

    def test_local_interface_ips_failure_is_non_fatal(self):
        with mock.patch.object(a.socket, "if_nameindex", side_effect=OSError):
            self.assertEqual(a.local_interface_ips(), [])

    def test_qrexec_policy_line_and_qube_names(self):
        with mock.patch.dict(os.environ, {"QUBES_VM_NAME": "work-qube"}):
            self.assertEqual(a.source_qube_name(), "work-qube")
            self.assertTrue(a.qubes_detected())
            self.assertEqual(a.qrexec_policy_line(a.source_qube_name(), "untrusted", 8443),
                             "qubes.ConnectTCP +8443 work-qube untrusted allow")
        environ = {key: value for key, value in os.environ.items() if key != "QUBES_VM_NAME"}
        with mock.patch.dict(os.environ, environ, clear=True):
            self.assertIsNone(a.source_qube_name())
            # Honest marker-only detection; never a guessed privileged identity.
            self.assertEqual(a.qubes_detected(), Path("/usr/share/qubes/marker-vm").exists())
        # Unknown source uses the documented board placeholder, never a silent guess.
        self.assertEqual(a.qrexec_policy_line(None, "untrusted"),
                         "qubes.ConnectTCP +8443 personal untrusted allow")
        # #88/#91: IPs und localhost sind Adressen, keine Qube-Namen.
        for bad in ("", "  ", "with space", "-start", "x" * 64, "a;b", "a\nb",
                    "127.0.0.1", "::1", "localhost", "LOCALHOST"):
            with self.assertRaises(a.ProbeError):
                a.validate_qube_name(bad)
        for good in ("personal", "untrusted", "work-2", "sys_net.ext"):
            self.assertEqual(a.validate_qube_name(good), good)

    def test_qrexec_first_start_command_and_probe_url_guard(self):
        # M9 Folgeanpassung: exact, secret-free target-qube command; the unauthenticated
        # reachability probe only accepts an explicit https URL with port.
        self.assertEqual(a.qrexec_first_start_command(), "python3 ./admin_in_the_loop.py --bridge")
        for bad in ("http://127.0.0.1:8443", "https://127.0.0.1", "", "https://u:p@127.0.0.1:8443/x"):
            with self.assertRaises(a.ProbeError):
                a.bridge_reachability(bad)


if __name__ == "__main__":
    unittest.main(testRunner=unittest.TextTestRunner(verbosity=2, resultclass=a.SafeResult))
