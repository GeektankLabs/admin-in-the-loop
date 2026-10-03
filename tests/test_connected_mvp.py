"""Real HTTP/MCP/TLS/PTY integration tests. Test framework: Python unittest only.

cryptography is an application dependency; the bridge itself is stdlib-only.
No production files, SSH, sudo, live API credentials or hosted tunnel are used.
"""
import base64
import hashlib
import hmac
import http.client
import os
import importlib.util
import json
import os
import queue
import select
import socket
import ssl
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import admin_in_the_loop as a


def request(url, method="GET", path=None, body=None, headers=None):
    from urllib.parse import urlsplit
    parsed = urlsplit(url)
    conn = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=5)
    raw = None if body is None else body if isinstance(body, bytes) else a.net_json(body)
    fields = dict(headers or {})
    if raw is not None:
        fields["Content-Type"] = "application/json"
    try:
        conn.request(method, path or parsed.path or "/", raw, fields)
        response = conn.getresponse()
        content = response.read(a.FRAME_LIMIT + 1)
        return response.status, content
    finally:
        conn.close()


def mcp(process, name, args=None, modern=False):
    args = dict(args or {})
    args.setdefault("conversation", process.profile.conversation)
    value = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": name, "arguments": args}}
    headers = {"X-AITL-AI-Key": process.ai_token}
    if modern:
        headers["MCP-Protocol-Version"] = "2026-07-28"
        value["params"]["_meta"] = {"io.modelcontextprotocol/protocolVersion": "2026-07-28",
                                     "io.modelcontextprotocol/clientCapabilities": {}}
    status, raw = request(process.mcp_url, "POST", body=value, headers=headers)
    if status != 200:
        raise a.ProbeError("test_mcp_http_failure")
    result = json.loads(raw)["result"]
    if result.get("isError"):
        raise a.ProbeError("test_mcp_tool_failure")
    data = result.get("structuredContent") or json.loads(result["content"][0]["text"])
    for item in data.get("outputs", []):
        index = item.pop("text_content_index", None)
        if index is not None:
            item["text"] = result["content"][index]["text"]
    return data


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="aitl-store-test-")
        self.now = 1800000000.0
        self.store = a.BridgeStore(self.temp.name, uuid.uuid4().hex, clock=lambda: self.now)
        self.controller, self.cid = self.store.issue("CONTROLLER")
        self.ai, self.aid = self.store.issue("AI")

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def body(self, content="approved output", key=None, submission=None):
        return {"version": 1, "conversation": self.store.conversation,
                "idempotency_key": key or uuid.uuid4().hex, "text": content,
                "submission_id": submission, "approved_at": self.now}

    def test_role_separation_pending_confirm_revoke(self):
        with self.assertRaises(a.BridgeError):
            self.store.status(self.ai)
        token, identifier = self.store.issue("CONTROLLER", pending=True)
        with self.assertRaises(a.BridgeError):
            self.store.status(token)
        self.assertEqual(self.store.confirm(token)["state"], "PAIRED")
        self.assertEqual(self.store.confirm(token)["state"], "PAIRED")
        self.store.revoke(identifier)
        with self.assertRaises(a.BridgeError):
            self.store.status(token)

    def test_pair_code_single_use_attempt_limit_and_expiry(self):
        code = self.store.open_pair()
        for _ in range(5):
            with self.assertRaises(a.BridgeError):
                self.store.claim({"code": "ZZZZ-ZZZZ-ZZZZ" if code != "ZZZZ-ZZZZ-ZZZZ" else "0000-0000-0000",
                                  "request_id": uuid.uuid4().hex})
        with self.assertRaises(a.BridgeError):
            self.store.claim({"code": code, "request_id": uuid.uuid4().hex})
        code = self.store.open_pair()
        claim = self.store.claim({"code": code.lower(), "request_id": uuid.uuid4().hex})
        with self.assertRaises(a.BridgeError):
            self.store.claim({"code": code, "request_id": uuid.uuid4().hex})
        self.now += 121
        with self.assertRaises(a.BridgeError):
            self.store.confirm(claim["token"])
        code = self.store.open_pair()
        self.now += 121
        with self.assertRaises(a.BridgeError):
            self.store.claim({"code": code, "request_id": uuid.uuid4().hex})

    def test_output_idempotency_exact_body_and_retention(self):
        value = self.body()
        first = self.store.approved_output(value)
        self.assertEqual(first, self.store.approved_output(value))
        with self.assertRaises(a.BridgeError):
            self.store.approved_output(dict(value, text="different"))
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM outputs").fetchone()[0], 1)
        self.now += a.BRIDGE_RETENTION + 1
        self.assertEqual(self.store.ai_output(self.store.authenticate(self.ai, "AI"), {})["outputs"], [])
        self.store.cleanup()
        with self.assertRaises(a.BridgeError):
            self.store.approved_output(value)

    def test_restart_respects_expiry_and_token_hashes(self):
        value = self.body("TEMPORARY_DISCLOSURE")
        self.store.approved_output(value)
        old_instance, conversation = self.store.instance, self.store.conversation
        token_blob = " ".join(str(tuple(row)) for row in self.store.db.execute("SELECT * FROM tokens"))
        self.assertTrue(self.ai not in token_blob and self.controller not in token_blob)
        self.store.close()
        self.store = a.BridgeStore(self.temp.name, conversation, clock=lambda: self.now)
        self.assertNotEqual(self.store.instance, old_instance)
        self.assertEqual(self.store.status(self.controller)["state"], "PAIRED")
        self.assertEqual(self.store.approved_output(value)["state"], "ACKNOWLEDGED")
        self.now += a.BRIDGE_RETENTION + 1
        self.store.cleanup()
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM outputs").fetchone()[0], 0)

    def test_proposal_limits_and_idempotency(self):
        value = {"request_id": uuid.uuid4().hex, "text": "printf 'hello\\n'"}
        first = self.store.submit(value)
        self.assertEqual(first, self.store.submit(value))
        with self.assertRaises(a.BridgeError):
            self.store.submit(dict(value, text="printf 'changed\\n'"))
        for _ in range(19):
            self.store.submit({"request_id": uuid.uuid4().hex, "text": "true"})
        with self.assertRaises(a.BridgeError):
            self.store.submit({"request_id": uuid.uuid4().hex, "text": "true"})

    def test_bound_cursors_and_passive_schema(self):
        self.store.submit({"request_id": uuid.uuid4().hex, "text": "true"})
        batch = self.store.inbox(self.controller, "")
        second, _ = self.store.issue("CONTROLLER")
        with self.assertRaises(a.BridgeError):
            self.store.inbox(second, batch["cursor"])
        with self.assertRaises(a.BridgeError):
            self.store.ai_output(self.store.authenticate(self.ai, "AI"), {"cursor": batch["cursor"]})
        for bad in ({"request_id": uuid.uuid4().hex, "text": "true", "approved": True},
                    {"request_id": uuid.uuid4().hex, "text": "true\x1b[0m"},
                    {"request_id": uuid.uuid4().hex, "text": "x" * 4097}):
            with self.assertRaises(a.BridgeError):
                self.store.submit(bad)

    def test_json_limits_duplicates_nonfinite_and_no_reflection(self):
        for raw in (b'{"x":1,"x":2}', b'{"x":NaN}', b'{"x":Infinity}', b'{"x":"\xff"}',
                    b'[' + b'0]' * 20, b'{"x":"' + b'x' * a.BRIDGE_BODY_LIMIT + b'"}'):
            with self.assertRaises(a.BridgeError):
                a.parse(raw)

    def test_symlink_state_is_rejected(self):
        with tempfile.TemporaryDirectory() as other:
            victim = Path(other) / "victim"
            victim.write_text("untouched")
            Path(other, "state").mkdir(mode=0o700)
            Path(other, "state", "bridge.sqlite3").symlink_to(victim)
            with self.assertRaises(a.BridgeError):
                a.BridgeStore(Path(other, "state"), uuid.uuid4().hex)
            self.assertEqual(victim.read_text(), "untouched")

    def test_output_envelope_extreme_escaping_is_not_silently_truncated(self):
        with self.assertRaises(a.BridgeError):
            self.store.approved_output(self.body("\n" * a.OUTPUT_LIMIT))
        accepted = self.body("X" * a.OUTPUT_LIMIT)
        self.assertEqual(self.store.approved_output(accepted)["state"], "ACKNOWLEDGED")


class RuntimeModeTests(unittest.TestCase):
    def test_bridge_component_loads_without_controller_capabilities(self):
        bridge = a._load_bridge_component()
        self.assertIn("BridgeStore", bridge)
        self.assertIn("bridge_main", bridge)
        self.assertNotIn("AppCore", bridge)
        self.assertNotIn("run_terminal_helper", bridge)
        self.assertNotIn("PtySession", bridge)

    def test_no_argument_and_explicit_gui_start_gui(self):
        for argv in ([a.__file__], [a.__file__, "--gui"]):
            with self.subTest(argv=argv), mock.patch.object(a.sys, "argv", list(argv)), \
                    mock.patch.object(a, "run_gui") as run_gui:
                self.assertEqual(a.main(), 0)
                run_gui.assert_called_once_with()

    def test_bridge_flag_is_explicit_and_conflicting_role_rejected(self):
        with mock.patch.object(a.sys, "argv", [a.__file__, "--gui", "--bridge"]):
            with self.assertRaisesRegex(a.ProbeError, "choose_one_role"):
                a.main()

    def test_remote_command_is_shell_quoted_and_uses_explicit_bridge_role(self):
        command = a.remote_bridge_command("python3", "/opt/Admin in the Loop/admin_in_the_loop.py",
                                          "/home/admin/.local/state/aitl-bridge", "0.0.0.0",
                                          "8443", "bridge.example")
        argv = __import__("shlex").split(command)
        self.assertEqual(argv, ["python3", "/opt/Admin in the Loop/admin_in_the_loop.py", "--bridge",
                                "--state-dir", "/home/admin/.local/state/aitl-bridge", "--bind", "0.0.0.0",
                                "--port", "8443", "--tls-name", "bridge.example", "--pair"])
        for invalid in (
            ("python3", "relative.py", "/state", "0.0.0.0", 8443, "bridge.example"),
            ("python3", "/app.py", "/state", "not-an-ip", 8443, "bridge.example"),
            ("python3", "/app.py", "/state", "0.0.0.0", 0, "bridge.example"),
        ):
            with self.assertRaises(a.ProbeError):
                a.remote_bridge_command(*invalid)

    def test_local_bridge_self_spawns_canonical_file_and_cleans_up(self):
        process = a.LocalBridgeProcess()
        state_dir = process.directory
        try:
            self.assertNotEqual(process.process.pid, os.getpid())
            self.assertEqual(process.profile.mode, "LOCAL")
            if sys.platform.startswith("linux"):
                command = Path("/proc", str(process.process.pid), "cmdline").read_bytes().split(b"\0")
                self.assertIn(str(Path(a.__file__).resolve()).encode(), command)
                self.assertIn(b"--bridge", command)
                descriptors = [os.readlink(path) for path in Path("/proc", str(process.process.pid), "fd").iterdir()]
                self.assertFalse(any("/dev/pts" in target for target in descriptors))
                environment = Path("/proc", str(process.process.pid), "environ").read_bytes().split(b"\0")
                self.assertEqual({item.split(b"=", 1)[0] for item in environment if item},
                                 {b"PATH", b"LC_ALL", b"LANG"})
        finally:
            process.close()
        self.assertIsNotNone(process.process.poll())
        self.assertFalse(state_dir.exists())


class StreamingProjectionTests(unittest.TestCase):
    def test_fragmented_utf8_and_escapes_match_whole_stream(self):
        raw = "a€\x1b[31mred\x1b[0m\r\nplain\x1b]52;c;INVISIBLE\nPAYLOAD\x07end".encode()
        full, fragmented = a.TranscriptSanitizer(), a.TranscriptSanitizer()
        expected = full.feed(raw)
        actual = b"".join(fragmented.feed(bytes([byte])) for byte in raw)
        self.assertEqual(actual, expected)
        self.assertTrue(b"INVISIBLE" not in actual and b"PAYLOAD" not in actual)
        self.assertEqual(fragmented.pending_marker(), b"")

    def test_last_lines_never_enter_middle_of_osc_payload(self):
        ring = a.TranscriptRing()
        for raw in (b"visible\n\x1b]52;c;SECRET", b"\nHIDDEN_LINE\n", b"\x1b\\last visible\n"):
            ring.append(raw)
        projected, lost = ring.selected_text()
        self.assertFalse(lost)
        self.assertTrue(b"SECRET" not in projected and b"HIDDEN_LINE" not in projected)
        self.assertEqual(projected.splitlines()[-1], b"last visible")

    def test_unterminated_sequence_remains_bounded_and_visible(self):
        ring = a.TranscriptRing(1024)
        ring.append(b"\x1b]52;c;")
        for _ in range(100):
            ring.append(b"x" * 1000)
        self.assertLessEqual(ring.clean_size, 1024)
        self.assertLessEqual(ring.sanitizer.count, 4097)
        self.assertIn(b"unterbrochene Terminalsequenz", ring.selected_text()[0])
        ring.append(b"\x07SAFE\n")
        self.assertIn(b"SAFE", ring.selected_text()[0])

    def test_loss_and_arbitrary_partial_chunk_offset_are_explicit(self):
        ring = a.TranscriptRing(16)
        ring.append(b"old line\n")
        marker = ring.end
        ring.append(b"new line\n")
        self.assertTrue(ring.selected_text(0)[1])
        self.assertEqual(ring.selected_text(marker)[0], b"new line\n")
        self.assertTrue(ring.selected_text(marker + 1)[1])


class HTTPTests(unittest.TestCase):
    def test_other_chat_cannot_guess_or_discover_binding(self):
        headers = {"X-AITL-AI-Key": self.process.ai_token}
        for arguments in ({}, {"conversation": "0" * 64}, {"conversation": "ä" * 16}):
            value = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                     "params": {"name": "get_approved_output", "arguments": arguments}}
            status, raw = request(self.process.mcp_url, "POST", body=value, headers=headers)
            self.assertEqual(status, 200)
            self.assertTrue(json.loads(raw)["result"]["isError"])
            self.assertTrue(self.process.profile.conversation.encode() not in raw)
        value["params"]["name"] = "get_preferences"
        status, raw = request(self.process.mcp_url, "POST", body=value, headers=headers)
        self.assertTrue(json.loads(raw)["result"]["isError"])

    def setUp(self):
        self.process = a.LocalBridgeProcess()

    def tearDown(self):
        self.process.close()

    def test_four_tools_legacy_and_modern_discovery(self):
        value = {"jsonrpc": "2.0", "id": "list", "method": "tools/list", "params": {}}
        headers = {"X-AITL-AI-Key": self.process.ai_token}
        status, raw = request(self.process.mcp_url, "POST", body=value, headers=headers)
        self.assertEqual(status, 200)
        names = {item["name"] for item in json.loads(raw)["result"]["tools"]}
        self.assertEqual(names, {"submit_proposal", "get_submission_status", "get_approved_output", "get_preferences"})
        self.assertEqual(mcp(self.process, "get_preferences", modern=True)["block_size"], "SMALL")
        meta = {"io.modelcontextprotocol/protocolVersion": "2026-07-28", "io.modelcontextprotocol/clientCapabilities": {}}
        value = {"jsonrpc": "2.0", "id": 2, "method": "server/discover", "params": {"_meta": meta}}
        headers["MCP-Protocol-Version"] = "2026-07-28"
        status, raw = request(self.process.mcp_url, "POST", body=value, headers=headers)
        self.assertEqual(status, 200)
        self.assertIn("2026-07-28", json.loads(raw)["result"]["supportedVersions"])

    def test_roles_auth_and_missing_terminal_endpoints(self):
        base = self.process.profile.url
        self.assertEqual(request(base, path="/v1/status")[0], 401)
        self.assertEqual(request(base, path="/v1/status", headers={"Authorization": "Bearer " + self.process.ai_token})[0], 401)
        value = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        self.assertEqual(request(self.process.mcp_url, "POST", body=value,
                                 headers={"X-AITL-AI-Key": self.process.profile.token})[0], 401)
        for path in ("/execute", "/send-keys", "/read-terminal", "/ssh", "/read-file", "/write-file"):
            self.assertEqual(request(base, "POST", path, {})[0], 404)

    def test_host_origin_limits_and_error_bodies_do_not_echo(self):
        headers = {"Authorization": "Bearer " + self.process.profile.token}
        self.assertEqual(request(self.process.profile.url, path="/v1/status", headers=dict(headers, Host="attacker.example"))[0], 403)
        self.assertEqual(request(self.process.profile.url, path="/v1/status", headers=dict(headers, Origin="https://attacker.example"))[0], 403)
        headers = {"X-AITL-AI-Key": self.process.ai_token}
        bad = b'{"CANARY_SECRET_CONTENT":"unterminated'
        status, raw = request(self.process.mcp_url, "POST", body=bad, headers=headers)
        self.assertEqual(status, 400)
        self.assertTrue(b"CANARY_SECRET_CONTENT" not in raw)
        self.assertEqual(request(self.process.mcp_url, "POST", body=b'x' * (a.FRAME_LIMIT + 1), headers=headers)[0], 413)

    def test_long_poll_empty_timeout_and_new_proposal(self):
        client = a.BridgeHTTP(self.process.profile)
        initial = client.request("GET", "/v1/inbox?wait=0")
        self.assertEqual(initial["messages"], [])
        result = []
        thread = threading.Thread(target=lambda: result.append(client.request("GET", "/v1/inbox?wait=2&cursor=" + initial["cursor"])))
        thread.start()
        time.sleep(0.1)
        item = mcp(self.process, "submit_proposal", {"request_id": uuid.uuid4().hex, "text": "true"})
        thread.join(timeout=3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(result[0]["messages"][0]["id"], item["submission_id"])

    def test_remote_plaintext_rejected_and_process_no_terminal_fds(self):
        with self.assertRaises(a.ProbeError):
            a.BridgeHTTP(replace(self.process.profile, mode="REMOTE"))
        if sys.platform.startswith("linux"):
            descriptors = Path("/proc") / str(self.process.process.pid) / "fd"
            targets = [os.readlink(p) for p in descriptors.iterdir()]
            self.assertFalse(any("/dev/pts" in target for target in targets))

    def test_full_size_approved_output_is_retrievable_over_mcp(self):
        profile = self.process.profile
        value = {"version": 1, "conversation": profile.conversation, "idempotency_key": uuid.uuid4().hex,
                 "text": "X" * a.OUTPUT_LIMIT, "submission_id": None, "approved_at": time.time()}
        self.assertEqual(a.BridgeHTTP(profile).request("POST", "/v1/approved-output", value)["state"], "ACKNOWLEDGED")
        self.assertEqual(len(mcp(self.process, "get_approved_output")["outputs"][0]["text"]), a.OUTPUT_LIMIT)


class ConnectedRoundTripTests(unittest.TestCase):
    def test_stopped_network_worker_blocks_existing_approval(self):
        self.submit(": > stopped-worker-marker")
        review = self.core.review_pending_proposal()
        self.core.bridge_worker.stop.set()
        self.core.pump()
        self.assertEqual(self.core.bridge_state, "BLOCKED")
        with self.assertRaises(a.ProbeError):
            self.core.approve(review.id)
        self.assertFalse(Path(self.temp.name, "stopped-worker-marker").exists())

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="aitl-roundtrip-")
        self.fixture = a.IntegrationFixture(self.temp.name)
        self.core = self.fixture.core
        self.core.connect_local_bridge()
        self.fixture.wait(lambda: self.core.bridge_state == "CONNECTED", timeout=6)
        self.process = self.core.bridge_process

    def tearDown(self):
        self.fixture.close()
        self.temp.cleanup()

    def submit(self, content):
        item = mcp(self.process, "submit_proposal", {"request_id": uuid.uuid4().hex, "text": content})
        self.fixture.wait(lambda: bool(self.core.inbox), timeout=6)
        return item["submission_id"]

    def test_complete_mcp_review_pty_output_review_mcp_round_trip(self):
        identifier = self.submit("printf 'CONNECTED_ROUNDTRIP\\n'; : > human-approved-marker")
        marker = Path(self.temp.name, "human-approved-marker")
        self.assertFalse(marker.exists())
        self.assertIsNone(self.core.review)
        self.assertEqual(mcp(self.process, "get_approved_output", {"submission_id": identifier})["outputs"], [])
        review = self.core.review_pending_proposal()
        self.assertFalse(marker.exists())
        self.core.approve(review.id)
        self.fixture.wait(lambda: self.core.review_state in {"TRANSFERRED", "OVERRIDE_REQUIRED"})
        if self.core.review_state == "OVERRIDE_REQUIRED":
            self.core.approve(review.id, override=True)
            self.fixture.wait(lambda: self.core.review_state == "TRANSFERRED")
        self.fixture.wait(lambda: marker.exists() and b"CONNECTED_ROUNDTRIP\r\n" in self.core.output.data)
        self.assertEqual(mcp(self.process, "get_submission_status", {"submission_id": identifier})["state"], "ACCEPTED")
        output = self.core.select_current_output()
        self.assertEqual(mcp(self.process, "get_approved_output", {"submission_id": identifier})["outputs"], [])
        output = self.core.edit_review(output.id, "CONNECTED_ROUNDTRIP\nOnly this chosen text is shared.", False)
        exact = output.payload
        self.core.approve(output.id)
        self.fixture.wait(lambda: self.core.review_state == "ACKNOWLEDGED", timeout=6)
        returned = mcp(self.process, "get_approved_output", {"submission_id": identifier})["outputs"]
        self.assertEqual(len(returned), 1)
        self.assertEqual(returned[0]["text"].encode(), exact)
        with self.assertRaises(a.ProbeError):
            self.core.approve(output.id)

    def test_output_binding_is_exact_per_submission_not_last_heuristic(self):
        first = self.submit("printf 'MULTI_FLIGHT_ONE\\n'")
        second = self.submit("printf 'MULTI_FLIGHT_TWO\\n'")
        for proposal in (first, second):
            self.fixture.wait(lambda: bool(self.core.inbox), timeout=6)
            review = self.core.review_pending_proposal()
            self.assertEqual(review.proposal_id, proposal)
            self.core.approve(review.id)
            self.fixture.wait(lambda: self.core.review_state in {"TRANSFERRED", "OVERRIDE_REQUIRED"})
            if self.core.review_state == "OVERRIDE_REQUIRED":
                self.core.approve(review.id, override=True)
            self.fixture.wait(lambda: self.core.review_state == "TRANSFERRED")
        candidates = self.core.output_candidates()
        self.assertEqual({item["submission_id"] for item in candidates}, {first, second})
        output = self.core.select_current_output()
        with self.assertRaises(a.ProbeError):
            self.core.approve(output.id, submission_id="f" * 32)
        self.core.approve(output.id, submission_id=second)
        self.fixture.wait(lambda: self.core.review_state == "ACKNOWLEDGED", timeout=6)
        named = mcp(self.process, "get_approved_output", {"submission_id": second})["outputs"]
        self.assertEqual(len(named), 1)
        self.assertEqual(named[0]["submission_id"], second)
        self.assertIn(b"MULTI_FLIGHT", named[0]["text"].encode())
        other = mcp(self.process, "get_approved_output", {"submission_id": first})["outputs"]
        self.assertEqual(other, [])
        general = mcp(self.process, "get_approved_output")["outputs"]
        self.assertEqual([item["submission_id"] for item in general], [second])

    def test_explicit_unbound_binding_ignores_last_submission(self):
        first = self.submit("printf 'EXPBIND_A\\n'")
        review = self.core.review_pending_proposal()
        self.core.approve(review.id)
        self.fixture.wait(lambda: self.core.review_state in {"TRANSFERRED", "OVERRIDE_REQUIRED"})
        if self.core.review_state == "OVERRIDE_REQUIRED":
            self.core.approve(review.id, override=True)
        self.fixture.wait(lambda: self.core.review_state == "TRANSFERRED")
        self.assertEqual(self.core.last_proposal_id, first)
        output = self.core._output_review("EXPLICIT_UNBOUND_CHOICE\n")
        self.core.approve(output.id, explicit_binding=True)
        self.fixture.wait(lambda: self.core.review_state == "ACKNOWLEDGED", timeout=6)
        with_named = mcp(self.process, "get_approved_output", {"submission_id": first})["outputs"]
        self.assertEqual(with_named, [])
        general = mcp(self.process, "get_approved_output")["outputs"]
        self.assertEqual(len(general), 1)
        self.assertIsNone(general[0]["submission_id"])
        self.assertEqual(general[0]["text"].encode(), output.payload)

    def test_pending_second_proposal_never_changes_review(self):
        first = self.submit("printf 'FIRST\\n'")
        review = self.core.review_pending_proposal()
        mcp(self.process, "submit_proposal", {"request_id": uuid.uuid4().hex, "text": "printf 'SECOND\\n'"})
        self.fixture.wait(lambda: bool(self.core.inbox), timeout=6)
        self.assertEqual(self.core.review, review)
        self.assertEqual(review.proposal_id, first)
        self.assertEqual(self.core.review_state, "REVIEW")
        self.assertEqual(len(self.core.inbox), 1)

    def test_bridge_disconnect_stales_review_manual_shell_survives(self):
        self.submit(": > forbidden-after-disconnect")
        review = self.core.review_pending_proposal()
        self.core.disconnect_bridge()
        with self.assertRaises(a.ProbeError):
            self.core.approve(review.id)
        self.assertEqual(self.core.review_state, "STALE")
        self.fixture.manual("printf 'MANUAL_STILL_AVAILABLE\\n'\n")
        self.fixture.wait(lambda: b"MANUAL_STILL_AVAILABLE\r\n" in self.core.output.data)
        self.assertFalse(Path(self.temp.name, "forbidden-after-disconnect").exists())

    def test_ack_loss_retry_identical_snapshot_no_duplicate_upload(self):
        self.submit("true")
        output = self.core._output_review("EXACT_APPROVED_SNAPSHOT\n")
        original = a.BridgeHTTP.request
        posted, lost = [], [False]
        def unreliable(client, method, path, value=None, **kwargs):
            result = original(client, method, path, value, **kwargs)
            if path == "/v1/approved-output":
                posted.append(value)
                if not lost[0]:
                    lost[0] = True
                    raise a.TransportError("network_unavailable", True)
            return result
        with mock.patch.object(a.BridgeHTTP, "request", unreliable):
            self.core.approve(output.id)
            self.fixture.wait(lambda: self.core.review_state == "DELIVERY_UNCERTAIN", timeout=6)
            self.fixture.wait(lambda: self.core.bridge_state == "CONNECTED", timeout=6)
            self.core.output.append(b"UNAPPROVED_LATER_OUTPUT\n")
            with self.assertRaises(a.ProbeError):
                self.core.select_current_output()
            self.core.retry_output(output.id)
            self.fixture.wait(lambda: self.core.review_state == "ACKNOWLEDGED", timeout=6)
        self.assertEqual(len(posted), 2)
        self.assertEqual(posted[0], posted[1])
        response = mcp(self.process, "get_approved_output")
        self.assertEqual(len(response["outputs"]), 1)
        self.assertEqual(response["outputs"][0]["text"].encode(), output.payload)
        self.assertEqual(mcp(self.process, "get_approved_output", {"cursor": response["cursor"]})["outputs"], [])

    def test_reconnect_generation_and_expired_proposal_block_approval(self):
        self.submit(": > generation-marker")
        review = self.core.review_pending_proposal()
        self.core.bridge_worker.emit("disconnected", ("network_unavailable", True))
        self.core.pump()
        self.assertEqual(self.core.review_state, "STALE")
        self.core.bridge_worker.emit("connected", self.process.profile.controller_id)
        self.core.pump()
        with self.assertRaises(a.ProbeError):
            self.core.approve(review.id)
        self.assertFalse(Path(self.temp.name, "generation-marker").exists())

    def test_disclosed_preference_single_and_no_approval_restore(self):
        self.core.set_preferences("SINGLE")
        last_poll = [0]
        def published():
            if time.monotonic() - last_poll[0] < 0.2:
                return False
            last_poll[0] = time.monotonic()
            return mcp(self.process, "get_preferences")["block_size"] == "SINGLE"
        self.fixture.wait(published, timeout=6)
        self.assertEqual(self.core.bridge_snapshot().mode, "LOCAL")
        self.assertFalse(hasattr(self.core.bridge_snapshot(), "token"))
        self.assertIsNone(a.AppCore().snapshot().review)

    def test_reconnect_does_not_represent_consumed_proposal(self):
        self.submit("true")
        review = self.core.review_pending_proposal()
        self.core.reject(review.id)
        self.core.reconnect_bridge()
        self.fixture.wait(lambda: self.core.bridge_state == "CONNECTED", timeout=6)
        deadline = time.monotonic() + 1.3
        while time.monotonic() < deadline:
            self.fixture.pump()
            time.sleep(0.01)
        self.assertFalse(self.core.inbox)
        self.assertEqual(self.core.review_state, "REJECTED")


class RemoteFixture:
    def __init__(self, directory):
        self.process = subprocess.Popen([sys.executable, "-I", str(Path(a.__file__).resolve()), "--bridge",
                                         "--state-dir", str(directory), "--tls-name", "127.0.0.1", "--pair"],
                                        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                        env=a.isolated_service_environment(), close_fds=True)
        try:
            def line():
                deadline = time.monotonic() + 6
                raw = bytearray()
                while not raw.endswith(b"\n"):
                    a.require(time.monotonic() < deadline and self.process.poll() is None, "remote_start_failed")
                    if select.select([self.process.stdout], [], [], 0.05)[0]:
                        chunk = os.read(self.process.stdout.fileno(), 1)
                        a.require(bool(chunk), "remote_start_failed")
                        raw.extend(chunk)
                return raw.decode()
            value = json.loads(line())
            self.url = "https://127.0.0.1:" + str(value["port"])
            self.pin = value["pin"]
            self.code = line().split(": ", 1)[1].strip()
        except BaseException:
            self.close()
            raise

    def close(self):
        if self.process.poll() is None:
            self.process.terminate()
            self.process.wait(timeout=5)
        self.process.stdout.close()


class TLSPairingTests(unittest.TestCase):
    def test_saved_connection_recovers_lost_confirm_without_new_claim(self):
        with tempfile.TemporaryDirectory(prefix="aitl-confirm-loss-") as directory:
            bridge = RemoteFixture(Path(directory, "bridge"))
            self.addCleanup(bridge.close)
            store = a.ConnectionStore(Path(directory, "controller"))
            core = a.AppCore(connection_store=store)
            self.addCleanup(core.close)
            original = a.BridgeHTTP.request
            calls = []
            lost = False
            def request(client, method, path, *args, **kwargs):
                nonlocal lost
                calls.append(path)
                result = original(client, method, path, *args, **kwargs)
                if path == "/v1/pair/confirm" and not lost:
                    lost = True
                    raise a.TransportError("network_unavailable", True)
                return result
            with mock.patch.object(a.BridgeHTTP, "request", request):
                with self.assertRaises(a.TransportError):
                    core.pair_remote(bridge.url, bridge.pin, bridge.code)
                self.assertIsNone(core.saved_connection)
                saved = store.load_connection()["profile"]
                core.connect_saved_bridge()
                deadline = time.monotonic() + 6
                while core.bridge_state != "CONNECTED" and time.monotonic() < deadline:
                    core.pump()
                    time.sleep(0.01)
                self.assertEqual(core.bridge_state, "CONNECTED")
                self.assertEqual(core.bridge_profile.token, saved["token"])
                self.assertEqual(calls.count("/v1/pair/claim"), 1)
                self.assertGreaterEqual(calls.count("/v1/pair/confirm"), 2)
                self.assertIsNone(core.bridge_process)

    def test_certificate_renewal_keeps_spki_identity(self):
        with tempfile.TemporaryDirectory(prefix="aitl-renew-cert-") as directory:
            _, pin = a.bridge_certificate(directory, ["127.0.0.1"])
            old = Path(directory, "tls-cert.pem").read_bytes()
            _, renewed_pin = a.bridge_certificate(directory, ["127.0.0.1"], renew=True)
            renewed = Path(directory, "tls-cert.pem").read_bytes()
            self.assertEqual(pin, renewed_pin)
            self.assertNotEqual(old, renewed)
            der = ssl.PEM_cert_to_DER_cert(renewed.decode())
            self.assertIsNotNone(a.check_certificate(der, "127.0.0.1", pin))

    def test_real_remote_pair_persist_confirm_revoke(self):
        with tempfile.TemporaryDirectory(prefix="aitl-remote-") as directory:
            bridge_dir = Path(directory, "bridge")
            bridge = RemoteFixture(bridge_dir)
            store = a.ConnectionStore(Path(directory, "controller"))
            core = a.AppCore(connection_store=store)
            try:
                profile = core.pair_remote(bridge.url, bridge.pin, bridge.code)
                self.assertEqual(a.validate_status(a.BridgeHTTP(profile).request("GET", "/v1/status"), profile).__len__(), 32)
                self.assertEqual(store.load_connection()["profile"]["pin"], bridge.pin)
                self.assertEqual(os.stat(store.directory / "connection.json").st_mode & 0o777, 0o600)
                self.assertTrue(profile.token not in repr(profile))
                self.assertIsNone(core.bridge_process)
                with self.assertRaises(a.ProbeError):
                    core.pair_remote(bridge.url, bridge.pin, bridge.code)
                result = subprocess.run([sys.executable, "-I", str(Path(a.__file__).resolve()), "--bridge", "--state-dir", str(bridge_dir),
                                         "--revoke", profile.controller_id], capture_output=True, timeout=5)
                self.assertEqual(result.returncode, 0)
                with self.assertRaises(a.TransportError):
                    a.BridgeHTTP(profile).request("GET", "/v1/status")
                restored = a.AppCore(connection_store=store)
                self.assertIsNone(restored.review)
                self.assertEqual(restored.bridge_state, "DISCONNECTED")
                restored.close()
            finally:
                core.close()
                bridge.close()

    def test_pinning_and_name_checked_before_any_http_bytes(self):
        with tempfile.TemporaryDirectory(prefix="aitl-tls-trap-") as directory:
            context, pin = a.bridge_certificate(directory, ["127.0.0.1"])
            listener = socket.socket()
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            listener.settimeout(3)
            observed = []
            def serve_once():
                try:
                    sock, _ = listener.accept()
                    with context.wrap_socket(sock, server_side=True) as channel:
                        channel.settimeout(2)
                        observed.append(channel.recv(2048))
                except (OSError, ssl.SSLError):
                    observed.append(b"")
            thread = threading.Thread(target=serve_once)
            thread.start()
            profile = a.ConnectionProfile("https://127.0.0.1:" + str(listener.getsockname()[1]), "0" * 64,
                                           "1" * 32, "2" * 32, "3" * 64)
            try:
                with self.assertRaises(a.TransportError):
                    a.BridgeHTTP(profile).request("POST", "/v1/pair/claim", {"SECRET_CODE_CANARY": "do not send"}, authenticate=False)
                thread.join(timeout=4)
                self.assertFalse(thread.is_alive())
                self.assertEqual(observed, [b""])
                der = ssl.PEM_cert_to_DER_cert(Path(directory, "tls-cert.pem").read_text())
                with self.assertRaises(a.ProbeError):
                    a.check_certificate(der, "wrong.example", pin)
                self.assertIsNotNone(a.check_certificate(der, "127.0.0.1", pin))
            finally:
                listener.close()
                thread.join(timeout=4)

    def test_unsafe_saved_config_and_plaintext_remote_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            store = a.ConnectionStore(Path(directory, "private"))
            store.load_connection()
            path = store.directory / "connection.json"
            path.write_text('{}')
            path.chmod(0o644)
            with self.assertRaises(a.ProbeError):
                store.load_connection()
            path.unlink()
            victim = Path(directory, "victim")
            victim.write_text("untouched")
            path.symlink_to(victim)
            with self.assertRaises(a.ProbeError):
                store.load_connection()
            self.assertEqual(victim.read_text(), "untouched")
        for url in ("http://bridge.example:8443", "https://user:pass@bridge.example:8443", "https://bridge.example:8443/path"):
            with self.assertRaises(a.ProbeError):
                a.check_profile(a.ConnectionProfile(url, "0" * 64, "1" * 32, "2" * 32, "3" * 64))


class TunnelTests(unittest.TestCase):
    def test_bad_cli_arguments_never_echo_credentials(self):
        result = subprocess.run([sys.executable, "-I", a.__file__, "--bridge", "--api-key", "CLI_SECRET_CANARY_123456"],
                                capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 2)
        self.assertTrue(b"CLI_SECRET_CANARY" not in result.stdout + result.stderr)

    @unittest.skipUnless(os.environ.get("AITL_TEST_TUNNEL_BINARY"), "official client binary not supplied")
    def test_official_tunnel_forwards_full_human_controlled_round_trip(self):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        jobs, responses, changed = queue.Queue(), {}, threading.Condition()
        tid = "tunnel_" + "c" * 32
        class ControlPlane(BaseHTTPRequestHandler):
            def send_json(self, value, status=200):
                raw = a.net_json(value)
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
            def do_GET(self):
                if "/poll" in self.path:
                    try:
                        self.send_json({"commands": [jobs.get(timeout=.2)]})
                    except queue.Empty:
                        self.send_response(204)
                        self.end_headers()
                else:
                    self.send_json({"id": tid, "name": "local-only-test"})
            def do_POST(self):
                count = int(self.headers.get("Content-Length", "0"))
                if not 0 < count <= a.FRAME_LIMIT:
                    self.send_response(400)
                    self.end_headers()
                    return
                value = json.loads(self.rfile.read(count))
                with changed:
                    responses[value["request_id"]] = value
                    changed.notify_all()
                self.send_json({"ok": True})
            def log_message(self, *args):
                pass
        server = ThreadingHTTPServer(("127.0.0.1", 0), ControlPlane)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        with tempfile.TemporaryDirectory(prefix="aitl-real-tunnel-chain-") as directory:
            fixture = a.IntegrationFixture(directory)
            core = fixture.core
            tunnel = None
            try:
                core.connect_local_bridge()
                fixture.wait(lambda: core.bridge_state == "CONNECTED", timeout=6)
                config = {"binary": os.environ["AITL_TEST_TUNNEL_BINARY"], "tunnel_id": tid,
                          "api_key": "SYNTHETIC_LOCAL_TUNNEL_ONLY_KEY"}
                original = subprocess.Popen
                def local_only(argv, **kwargs):
                    return original(argv + ["--control-plane.base-url", "http://127.0.0.1:" + str(server.server_port)], **kwargs)
                with mock.patch.object(a.subprocess, "Popen", local_only):
                    tunnel = a.TunnelProcess(config, core.bridge_process.mcp_url, core.bridge_process.ai_token)
                def call(name, args=None):
                    identifier = uuid.uuid4().hex
                    args = dict(args or {}) | {"conversation": core.bridge_profile.conversation}
                    payload = {"jsonrpc": "2.0", "id": identifier, "method": "tools/call",
                               "params": {"name": name, "arguments": args, "_meta": {
                                   "io.modelcontextprotocol/protocolVersion": "2026-07-28",
                                   "io.modelcontextprotocol/clientCapabilities": {}}}}
                    jobs.put({"request_id": identifier, "shard_token": "local-shard", "command_type": "jsonrpc",
                              "channel": "main", "created_at": a.datetime.now(a.timezone.utc).isoformat(),
                              "response_timeout": "30s", "headers": {"MCP-Protocol-Version": ["2026-07-28"]}, "jsonrpc": payload})
                    deadline = time.monotonic() + 8
                    while time.monotonic() < deadline:
                        fixture.pump()
                        with changed:
                            if identifier in responses:
                                response = responses.pop(identifier)
                                a.require(response["resp_code"] == 200, "tunnel_forward_http_failure")
                                result = response["resp_json"]["result"]
                                a.require(not result.get("isError"), "tunnel_forward_tool_failure")
                                return result
                            changed.wait(.02)
                    raise a.ProbeError("tunnel_forward_timeout")
                response = call("submit_proposal", {"request_id": uuid.uuid4().hex,
                                                     "text": "printf 'REAL_TUNNEL_CHAIN\\n'; : > tunnel-approved-marker"})
                submission = response["structuredContent"]["submission_id"]
                fixture.wait(lambda: bool(core.inbox), timeout=6)
                self.assertFalse(Path(directory, "tunnel-approved-marker").exists())
                self.assertIsNone(core.review)
                self.assertEqual(call("get_approved_output", {"submission_id": submission})["structuredContent"]["outputs"], [])
                review = core.review_pending_proposal()
                core.approve(review.id)
                fixture.wait(lambda: core.review_state in {"TRANSFERRED", "OVERRIDE_REQUIRED"})
                if core.review_state == "OVERRIDE_REQUIRED":
                    core.approve(review.id, True)
                fixture.wait(lambda: Path(directory, "tunnel-approved-marker").exists())
                output = core._output_review("REAL_TUNNEL_CHAIN\nChosen human disclosure.")
                core.approve(output.id)
                fixture.wait(lambda: core.review_state == "ACKNOWLEDGED", timeout=6)
                response = call("get_approved_output", {"submission_id": submission})
                entry = response["structuredContent"]["outputs"][0]
                self.assertEqual(response["content"][entry["text_content_index"]]["text"].encode(), output.payload)
            finally:
                if tunnel:
                    tunnel.close()
                fixture.close()
                server.shutdown()
                server.server_close()

    def test_private_key_files_and_no_credential_argv_environment(self):
        process = a.LocalBridgeProcess()
        with tempfile.TemporaryDirectory(prefix="aitl-fake-tunnel-") as directory:
            executable = Path(directory, "client")
            executable.write_text("#!/usr/bin/python3\nimport time\ntime.sleep(30)\n")
            executable.chmod(0o700)
            config = {"binary": str(executable), "tunnel_id": "tunnel_" + "a" * 32, "api_key": "SYNTHETIC_TUNNEL_KEY_CANARY"}
            with mock.patch.dict(os.environ, {"SSH_AUTH_SOCK": "must-not-inherit", "CONTROL_PLANE_API_KEY": "must-not-inherit"}):
                tunnel = a.TunnelProcess(config, process.mcp_url, process.ai_token)
            try:
                self.assertTrue(config["api_key"] not in " ".join(tunnel.argv) and process.ai_token not in " ".join(tunnel.argv))
                self.assertEqual(os.stat(tunnel.directory / "api-key").st_mode & 0o777, 0o600)
                if sys.platform.startswith("linux"):
                    environ = Path("/proc", str(tunnel.process.pid), "environ").read_bytes()
                    self.assertTrue(b"CONTROL_PLANE_API_KEY" not in environ and b"SSH_AUTH_SOCK" not in environ)
                tunnel.refresh()
                self.assertEqual(tunnel.state, "RUNNING_NOT_READY")
                saved_dir = tunnel.directory
            finally:
                tunnel.close()
                process.close()
            self.assertFalse(saved_dir.exists())

    @unittest.skipUnless(os.environ.get("AITL_TEST_TUNNEL_BINARY"), "official client binary not supplied")
    def test_official_runtime_cli_start_health_and_stop_without_cloud(self):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        class ControlPlane(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(401)
                self.end_headers()
            def do_POST(self):
                self.do_GET()
            def log_message(self, *args):
                pass
        server = ThreadingHTTPServer(("127.0.0.1", 0), ControlPlane)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        bridge = a.LocalBridgeProcess()
        config = {"binary": os.environ["AITL_TEST_TUNNEL_BINARY"], "tunnel_id": "tunnel_" + "b" * 32,
                  "api_key": "SYNTHETIC_LOCAL_CONTROL_PLANE_KEY"}
        real_popen = subprocess.Popen
        def local_only(argv, **kwargs):
            return real_popen(argv + ["--control-plane.base-url", "http://127.0.0.1:" + str(server.server_port)], **kwargs)
        tunnel = None
        try:
            with mock.patch.object(a.subprocess, "Popen", local_only):
                tunnel = a.TunnelProcess(config, bridge.mcp_url, bridge.ai_token)
            deadline = time.monotonic() + 6
            while time.monotonic() < deadline and not (tunnel.directory / "health-url").exists() and tunnel.process.poll() is None:
                time.sleep(0.05)
            self.assertIsNone(tunnel.process.poll())
            self.assertTrue((tunnel.directory / "health-url").exists())
            tunnel.refresh()
            # v0.0.15 reports /readyz even when control-plane authentication fails.
            # Readiness is client-local lifecycle evidence, not a cloud round trip.
            self.assertIn(tunnel.state, {"RUNNING_NOT_READY", "READY"})
        finally:
            if tunnel:
                tunnel.close()
            bridge.close()
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


class ServiceHarness:
    """In-process bridge servers; webhook transport is a stub, no real network."""
    def __init__(self, store, controller, controller_id, ai, ai_id):
        self.temp = tempfile.TemporaryDirectory(prefix="aitl-events-")
        self.store, self.controller, self.ai = store, controller, ai
        self.ai_id, self.controller_id = ai_id, controller_id
        self.service = a.BridgeService(store)
        self.verifications, self.deliveries, self.failures = {}, {}, {}

        def webhook_call(inner, subscription, body, webhook_id=None):
            parsed = json.loads(body)
            if parsed.get("type") == "verification":
                self.verifications.setdefault(subscription["id"], []).append((subscription, parsed))
                if self.failures.get("verify"):
                    raise a.WebhookFailure(self.failures["verify"], retryable=False)
                return 200, json.dumps({"challenge": parsed["challenge"]}).encode()
            self.deliveries.setdefault(subscription["id"], []).append((subscription, parsed))
            if self.failures.get("deliver"):
                raise a.WebhookFailure(self.failures["deliver"], retryable=True)
            return 200, b""

        self.patcher = mock.patch.object(a.BridgeService, "webhook_call", webhook_call)
        self.patcher.start()

        def handler(kind):
            return type("_Harness" + kind, (a.BridgeRequestHandler,),
                        {"service": self.service, "kind": kind})
        self.controller_server = a.BridgeHTTPServer(("127.0.0.1", 0), handler("CONTROLLER"))
        self.mcp_server = a.BridgeHTTPServer(("127.0.0.1", 0), handler("MCP"))
        self.service.controller_hosts.add("127.0.0.1:%d" % self.controller_server.socket.getsockname()[1])
        self.service.mcp_hosts.add("127.0.0.1:%d" % self.mcp_server.socket.getsockname()[1])
        self.profile = a.ConnectionProfile(
            "http://127.0.0.1:%d" % self.controller_server.socket.getsockname()[1], None,
            store.conversation, controller_id, controller, "LOCAL")
        self.mcp_url = "http://127.0.0.1:%d/mcp" % self.mcp_server.socket.getsockname()[1]
        self.subscriptions = []
        self.service.start()
        self.threads = [threading.Thread(target=server.serve_forever, daemon=True)
                        for server in (self.controller_server, self.mcp_server)]
        for thread in self.threads:
            thread.start()

    def close(self):
        self.service.stop()
        for server in (self.controller_server, self.mcp_server):
            server.shutdown()
            server.server_close()
        self.patcher.stop()
        self.store.close()
        self.temp.cleanup()

    def wait(self, predicate, timeout=6):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.02)
        raise a.ProbeError("events_test_timeout")


class EventSubscriptionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="aitl-events-suite-")
        self.addCleanup(self.temp.cleanup)
        self.store = a.BridgeStore(self.temp.name, uuid.uuid4().hex, clock=lambda: time.time())
        self.addCleanup(self.store.close)
        controller, controller_id = self.store.issue("CONTROLLER")
        ai, ai_id = self.store.issue("AI")
        self.ai_id = ai_id
        self.harness = ServiceHarness(self.store, controller, controller_id, ai, ai_id)
        self.addCleanup(self.harness.close)
        self.secret = "whsec_" + base64.b64encode(b"x" * 24).decode()
        self.callback_url = "https://callback.example/hook"
        self.harness.service.WEBHOOK_RETRY_DELAYS = (0.01, 0.02)

    def rpc(self, method, params=None, modern=True):
        value = {"jsonrpc": "2.0", "id": uuid.uuid4().hex, "method": method,
                 "params": dict(params) if params is not None else {}}
        if modern:
            value["params"].setdefault("_meta", {"io.modelcontextprotocol/protocolVersion": "2026-07-28",
                                                 "io.modelcontextprotocol/clientCapabilities": {}})
        header = "2026-07-28" if modern else "2025-11-25"
        status, raw = request(self.harness.mcp_url, "POST", body=value,
                              headers={"X-AITL-AI-Key": self.harness.ai, "MCP-Protocol-Version": header})
        self.assertEqual(status, 200)
        return json.loads(raw)

    def subscribe(self, name=a.BRIDGE_EVENT_NAME, arguments=None,
                  url="https://callback.example/hook", secret=None, mode="webhook", ttl=None):
        delivery = {"mode": mode, "url": url, "secret": secret or self.secret}
        params = {"name": name, "delivery": delivery,
                  "arguments": dict(arguments or {}, conversation=self.store.conversation)}
        if ttl is not None:
            params["ttlMs"] = ttl
        return self.rpc("events/subscribe", params)

    def canonical(self, arguments):
        return json.dumps(dict(sorted(arguments.items())), separators=(",", ":"))

    def test_bridge_component_is_stdlib_only(self):
        text = Path(a.__file__).read_text(encoding="utf-8")
        block = text.split("## BEGIN AITL EMBEDDED BRIDGE SOURCE", 1)[1].split("## END AITL", 1)[0]
        self.assertNotIn("aiohttp", block)

    def test_discovery_lists_events_only_for_modern_protocol(self):
        modern = self.rpc("server/discover", {})
        self.assertIn("events", modern["result"]["capabilities"])
        definition = self.rpc("events/list")["result"]["events"][0]
        self.assertEqual(definition["name"], a.BRIDGE_EVENT_NAME)
        self.assertEqual(definition["delivery"], ["webhook"])
        self.assertIn("output_id", definition["payloadSchema"]["required"])
        self.assertIn("conversation", definition["inputSchema"]["required"])
        self.assertNotIn("conversation", definition["payloadSchema"]["properties"])
        self.assertNotIn(self.store.conversation, json.dumps(definition))
        self.assertEqual(self.rpc("events/list", {"cursor": "ignored"}, modern=False)["error"]["code"], -32601)

    def test_webhook_signature_helper_matches_standard_webhooks(self):
        body = b'{"eventId":"evt_1"}'
        secret = "whsec_" + base64.b64encode(b"0123456789abcdef01234567").decode()
        headers = a.standard_webhook_headers("sub_x", "evt_1", secret, body)
        key = base64.b64decode(secret[6:])
        signed = ("evt_1." + headers["webhook-timestamp"] + ".").encode() + body
        expected = "v1," + base64.b64encode(hmac.new(key, signed, hashlib.sha256).digest()).decode()
        self.assertEqual(headers["webhook-signature"], expected)
        self.assertEqual(headers["webhook-id"], "evt_1")
        self.assertTrue(a.webhook_address_allowed("8.8.8.8"))
        for address in ("127.0.0.1", "::1", "10.0.0.5", "169.254.169.254", "0.0.0.0"):
            self.assertFalse(a.webhook_address_allowed(address))

    def test_subscribe_refresh_unsubscribe_and_persistence(self):
        result = self.subscribe()
        self.assertTrue({"id", "refreshBefore", "cursor", "truncated"}
                        <= set(result["result"]))
        identifier = result["result"]["id"]
        self.assertTrue(identifier.startswith("sub_"))
        self.assertIsNone(result["result"]["cursor"])
        self.assertEqual(len(self.harness.verifications[identifier]), 1)
        row = self.harness.store.find_subscription(self.ai_id, "https://callback.example/hook",
                                                   a.BRIDGE_EVENT_NAME, self.canonical({}))
        self.assertEqual(row["id"], identifier)
        self.assertEqual(self.subscribe()["result"]["id"], identifier)
        self.assertEqual(len(self.harness.verifications[identifier]), 1)
        filtered = self.subscribe(arguments={"submission_id": "s" * 32},
                                  url="https://callback.example/hook-s")
        self.assertNotEqual(filtered["result"]["id"], identifier)
        first = self.rpc("events/unsubscribe", {
            "name": a.BRIDGE_EVENT_NAME, "arguments": {"submission_id": "s" * 32,
                                                       "conversation": self.store.conversation},
            "delivery": {"mode": "webhook", "url": "https://callback.example/hook-s"}})
        self.assertEqual({key: value for key, value in first["result"].items()
                          if key not in {"resultType", "_meta"}}, {})
        self.assertIsNone(self.harness.store.find_subscription(
            self.ai_id, "https://callback.example/hook-s", a.BRIDGE_EVENT_NAME,
            self.canonical({"submission_id": "s" * 32})))
        again = self.rpc("events/unsubscribe", {
            "name": a.BRIDGE_EVENT_NAME,
            "arguments": {"conversation": self.store.conversation},
            "delivery": {"mode": "webhook", "url": "https://callback.example/hook-s"}})
        self.assertEqual({key: value for key, value in again["result"].items()
                          if key not in {"resultType", "_meta"}}, {})

    def test_subscription_limit_caps_across_urls(self):
        for index in range(a.BRIDGE_EVENT_SUBSCRIPTION_LIMIT):
            self.store.store_subscription(self.ai_id, self.callback_url + str(index),
                                          a.BRIDGE_EVENT_NAME, "{}", self.secret,
                                          self.store.clock() + 600, bound=True)
        blocked = self.subscribe(url=self.callback_url)
        self.assertEqual(blocked["error"]["code"], -32013)
        self.assertEqual(self.store.count_subscriptions(self.ai_id), a.BRIDGE_EVENT_SUBSCRIPTION_LIMIT)
        # An active refresh consumes no new allocation at the limit.
        refreshed = self.subscribe(url=self.callback_url + "0")
        self.assertIn("result", refreshed)
        self.assertEqual(self.store.count_subscriptions(self.ai_id), a.BRIDGE_EVENT_SUBSCRIPTION_LIMIT)

    def test_missing_wrong_binding_cannot_subscribe_receive_or_ack(self):
        params = {"name": a.BRIDGE_EVENT_NAME,
                  "delivery": {"mode": "webhook", "url": self.callback_url, "secret": self.secret}}
        for arguments in (None, {}, {"conversation": "wrong_binding_0001"}, {"conversation": None}):
            with self.subTest(arguments=arguments):
                attempt = dict(params)
                if arguments is not None:
                    attempt["arguments"] = arguments
                blocked = self.rpc("events/subscribe", attempt)
                self.assertEqual(blocked["error"]["data"]["reason"], "conversation_binding_required")
        self.assertEqual(self.store.count_subscriptions(self.ai_id), 0)
        self.assertEqual(self.harness.verifications, {})
        submission = self.store.submit({"request_id": uuid.uuid4().hex, "text": "true"})["submission_id"]
        output = self.store.approved_output({"version": 1, "conversation": self.store.conversation,
            "idempotency_key": uuid.uuid4().hex, "text": "BOUND_OUTPUT",
            "submission_id": submission, "approved_at": self.store.clock()})
        self.assertEqual(self.harness.service.jobs, [])
        self.assertEqual(self.harness.deliveries, {})
        self.assertTrue(self.store.submission_status(submission)["approved_output_available"])
        self.assertEqual(self.store.execute("SELECT delivered FROM outputs WHERE id=?",
                                           (output["output_id"],)).fetchone()[0], 0)
        principal = self.store.authenticate(self.harness.ai, "AI")
        self.assertEqual(self.store.ai_output(principal, {})["outputs"][0]["output_id"], output["output_id"])
        self.assertIn("result", self.subscribe(url=self.callback_url))

    def test_refresh_secret_update_and_unsubscribe_require_binding(self):
        identifier = self.subscribe(url=self.callback_url)["result"]["id"]
        before = self.store.find_subscription(self.ai_id, self.callback_url, a.BRIDGE_EVENT_NAME, "{}")
        for arguments in ({}, {"conversation": "wrong_binding_0001"}):
            for method in ("events/subscribe", "events/unsubscribe"):
                with self.subTest(method=method, arguments=arguments):
                    delivery = {"mode": "webhook", "url": self.callback_url}
                    if method == "events/subscribe":
                        delivery["secret"] = "whsec_" + base64.b64encode(b"z" * 24).decode()
                    blocked = self.rpc(method, {"name": a.BRIDGE_EVENT_NAME,
                                               "delivery": delivery, "arguments": arguments})
                    self.assertEqual(blocked["error"]["data"]["reason"], "conversation_binding_required")
                    self.assertEqual(self.store.find_subscription(self.ai_id, self.callback_url,
                                                                  a.BRIDGE_EVENT_NAME, "{}"), before)
        removed = self.rpc("events/unsubscribe", {"name": a.BRIDGE_EVENT_NAME,
            "arguments": {"conversation": self.store.conversation},
            "delivery": {"mode": "webhook", "url": self.callback_url}})
        self.assertIn("result", removed)
        self.assertIsNone(self.store.active_subscription(identifier, self.store.clock()))

    def test_subscribe_validation_failures(self):
        insecure = self.subscribe(url="http://callback.example/insecure")
        self.assertEqual(insecure["error"]["code"], -32602)
        self.assertEqual(insecure["error"]["data"]["reason"], "invalid_webhook_url")
        embedded = self.subscribe(url="https://user:secret@callback.example/hook")
        self.assertEqual(embedded["error"]["data"]["reason"], "invalid_webhook_url")
        bad_secret = self.subscribe(secret="whsec_short")
        self.assertEqual(bad_secret["error"]["data"]["reason"], "invalid_webhook_secret")
        unknown = self.subscribe(name="another.event")
        self.assertEqual(unknown["error"]["code"], -32011)
        wrong_mode = self.subscribe(mode="poll")
        self.assertEqual(wrong_mode["error"]["data"]["reason"], "delivery_mode_unsupported")
        self.harness.failures["verify"] = "timeout"
        unreachable = self.subscribe(url="https://callback.example/unreachable")
        self.assertEqual(unreachable["error"]["code"], -32015)
        self.assertEqual(unreachable["error"]["data"]["reason"], "timeout")
        self.harness.failures.clear()
        recovered = self.subscribe(url="https://callback.example/recovered")
        self.assertTrue(recovered["result"]["id"].startswith("sub_"))

    def test_verification_challenge_mismatch_fails_closed(self):
        response = {"challenge": "WRONG"}

        def webhook_call(inner, subscription, body, webhook_id=None):
            return 200, json.dumps(response).encode()

        with mock.patch.object(a.BridgeService, "webhook_call", webhook_call):
            blocked = self.subscribe(url="https://callback.example/mismatch")
        self.assertEqual(blocked["error"]["code"], -32015)
        self.assertEqual(blocked["error"]["data"]["reason"], "challenge_failed")
        self.assertIsNone(self.harness.store.find_subscription(
            self.ai_id, "https://callback.example/mismatch", a.BRIDGE_EVENT_NAME, self.canonical({})))


class EventDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="aitl-delivery-")
        self.addCleanup(self.temp.cleanup)
        self.store = a.BridgeStore(self.temp.name, uuid.uuid4().hex, clock=lambda: time.time())
        controller, controller_id = self.store.issue("CONTROLLER")
        ai, ai_id = self.store.issue("AI")
        self.harness = ServiceHarness(self.store, controller, controller_id, ai, ai_id)
        self.addCleanup(self.harness.close)
        self.secret = "whsec_" + base64.b64encode(b"y" * 24).decode()
        self.harness.service.WEBHOOK_RETRY_DELAYS = (0.01, 0.02)
        self.subscription_id = self.subscribe()["result"]["id"]

    def rpc(self, method, params=None, modern=True):
        value = {"jsonrpc": "2.0", "id": uuid.uuid4().hex, "method": method,
                 "params": dict(params) if params is not None else {}}
        if modern:
            value["params"].setdefault("_meta", {"io.modelcontextprotocol/protocolVersion": "2026-07-28",
                                                 "io.modelcontextprotocol/clientCapabilities": {}})
        header = "2026-07-28" if modern else "2025-11-25"
        status, raw = request(self.harness.mcp_url, "POST", body=value,
                              headers={"X-AITL-AI-Key": self.harness.ai, "MCP-Protocol-Version": header})
        self.assertEqual(status, 200)
        return json.loads(raw)

    def subscribe(self, url="https://delivery.example/hook", arguments=None):
        params = {"name": a.BRIDGE_EVENT_NAME,
                  "delivery": {"mode": "webhook", "url": url, "secret": self.secret},
                  "arguments": dict(arguments or {}, conversation=self.store.conversation)}
        result = self.rpc("events/subscribe", params)
        if result.get("result", {}).get("id"):
            self.harness.subscriptions.append(result["result"]["id"])
        return result

    def post_output(self, submission, key=None):
        body = {"version": 1, "conversation": self.store.conversation,
                "idempotency_key": key or uuid.uuid4().hex, "text": "APPROVED_PAYLOAD",
                "submission_id": submission, "approved_at": time.time()}
        self.last_body = body
        return a.BridgeHTTP(self.harness.profile).request("POST", "/v1/approved-output", body)

    def make_submission(self):
        return self.store.submit({"request_id": uuid.uuid4().hex, "text": "true"})["submission_id"]

    def approved_output_tool(self, arguments):
        return self.rpc("tools/call", {"name": "get_approved_output",
                                       "arguments": dict(arguments, conversation=self.store.conversation)})

    def test_event_only_after_human_approved_output(self):
        created = self.store.submit({"request_id": uuid.uuid4().hex, "text": "true"})
        submission = created["submission_id"]
        self.assertEqual(self.harness.deliveries.get(self.subscription_id, []), [])
        output_id = self.post_output(submission)["output_id"]
        replay = self.last_body
        self.harness.wait(lambda: self.harness.deliveries.get(self.subscription_id))
        entry = self.harness.deliveries[self.subscription_id][-1][1]
        self.assertEqual(entry["eventId"], output_id)
        self.assertEqual(entry["name"], a.BRIDGE_EVENT_NAME)
        self.assertEqual(set(entry["data"]), {"submission_id", "output_id", "expires_at"})
        self.assertEqual(entry["data"]["output_id"], output_id)
        self.assertEqual(entry["data"]["submission_id"], submission)
        self.assertNotIn(self.store.conversation, json.dumps(entry))
        self.harness.wait(lambda: self.store.submission_status(submission)["approved_output_available"] is False)
        named = self.approved_output_tool({"submission_id": submission})
        self.assertEqual(len(named["result"]["structuredContent"]["outputs"]), 1)
        general = self.approved_output_tool({})
        self.assertEqual(general["result"]["structuredContent"]["outputs"], [])
        replayed = a.BridgeHTTP(self.harness.profile).request("POST", "/v1/approved-output", replay)
        self.assertEqual(replayed["output_id"], output_id)

    def test_filter_limits_event_per_submission(self):
        first, second = self.make_submission(), self.make_submission()
        filtered = self.subscribe(url="https://delivery.example/filtered",
                                  arguments={"submission_id": first})
        response = self.post_output(second)
        self.harness.wait(lambda: len(self.harness.deliveries.get(self.subscription_id, [])) == 1)
        self.assertEqual(len(self.harness.deliveries.get(filtered["result"]["id"], [])), 0)
        self.assertEqual(self.harness.deliveries[self.subscription_id][-1][1]["data"]["submission_id"], second)
        self.post_output(first)
        self.harness.wait(lambda: self.harness.deliveries.get(filtered["result"]["id"]))
        self.assertEqual(self.harness.deliveries[filtered["result"]["id"]][-1][1]["data"]["submission_id"], first)

    def test_unsubscribe_stops_delivery_and_pull_remains_available(self):
        created = self.store.submit({"request_id": uuid.uuid4().hex, "text": "true"})
        submission = created["submission_id"]
        gone = self.rpc("events/unsubscribe", {
            "name": a.BRIDGE_EVENT_NAME,
            "arguments": {"conversation": self.store.conversation},
            "delivery": {"mode": "webhook", "url": "https://delivery.example/hook"}})
        self.assertEqual({key: value for key, value in gone["result"].items()
                          if key not in {"resultType", "_meta"}}, {})
        self.post_output(submission)
        time.sleep(0.1)
        self.assertEqual(self.harness.deliveries, {})
        pulled = self.approved_output_tool({"submission_id": submission})
        self.assertEqual(len(pulled["result"]["structuredContent"]["outputs"]), 1)

    def test_failed_delivery_keeps_output_available(self):
        created = self.store.submit({"request_id": uuid.uuid4().hex, "text": "true"})
        submission = created["submission_id"]
        self.harness.failures["deliver"] = "http_5xx"
        self.post_output(submission)
        self.harness.wait(lambda: len(self.harness.deliveries.get(self.subscription_id, [])) > 1, timeout=3)
        attempts = len(self.harness.deliveries[self.subscription_id])
        self.assertEqual(self.store.submission_status(submission)["approved_output_available"], True)
        pulled = self.approved_output_tool({"submission_id": submission})
        self.assertEqual(len(pulled["result"]["structuredContent"]["outputs"]), 1)
        self.harness.failures.clear()
        later = self.make_submission()
        self.post_output(later)
        self.harness.wait(lambda: self.store.submission_status(later)["approved_output_available"] is False)
        self.assertGreaterEqual(len(self.harness.deliveries[self.subscription_id]), attempts + 1)

    def test_delivery_state_and_subscriptions_survive_restart(self):
        created = self.store.submit({"request_id": uuid.uuid4().hex, "text": "true"})
        submission = created["submission_id"]
        self.post_output(submission)
        self.harness.wait(lambda: self.store.submission_status(submission)["approved_output_available"] is False)
        with self.store.lock:
            self.store.close()
        reopened = a.BridgeStore(self.temp.name, self.store.conversation, clock=lambda: time.time())
        try:
            canonical = json.dumps({}, separators=(",", ":"))
            row = reopened.find_subscription(self.harness.ai_id, "https://delivery.example/hook",
                                             a.BRIDGE_EVENT_NAME, canonical)
            self.assertEqual(row["id"], self.subscription_id)
            self.assertEqual(reopened.submission_status(submission)["approved_output_available"], False)
            named = reopened.ai_output(reopened.authenticate(self.harness.ai, "AI"),
                                       {"submission_id": submission})
            self.assertEqual(len(named["outputs"]), 1)
        finally:
            reopened.close()

class WebhookReceiver:
    """Real TLS webhook endpoint: verifies the Standard Webhooks signature."""
    def __init__(self, secret_bytes, directory, verify_status=200):
        self.secret = secret_bytes
        self.record = []
        self.verify_status = verify_status
        self.before_ack = None
        tls, pin = a.bridge_certificate(directory, ["127.0.0.1", "callback.example"])
        receiver = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                count = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(count)
                lower = {key.lower(): value for key, value in self.headers.items()}
                signed = (lower["webhook-id"] + "." + lower["webhook-timestamp"] + ".").encode() + raw
                expected = "v1," + base64.b64encode(
                    hmac.new(receiver.secret, signed, hashlib.sha256).digest()).decode()
                ok = hmac.compare_digest(lower.get("webhook-signature", ""), expected)
                receiver.record.append({"headers": lower, "body": raw, "signature_ok": ok})
                if not ok or lower.get("host") != "callback.example:%d" % receiver.port:
                    self.send_response(421)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if lower["webhook-id"].startswith("msg_verification_"):
                    response = json.dumps(json.loads(raw)).encode()
                    if receiver.verify_status != 200:
                        self.send_response(receiver.verify_status)
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                        return
                else:
                    response = b""
                    if receiver.before_ack is not None:
                        receiver.before_ack()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(response)))
                self.end_headers()
                self.wfile.write(response)

        class Server(http.server.ThreadingHTTPServer):
            daemon_threads = True

            def handle_error(self, request, client_address):
                pass  # Expected negative TLS fixtures contain no useful traceback.

            def get_request(self):
                accepted = self.socket.accept()
                accepted = (tls.wrap_socket(accepted[0], server_side=True), accepted[1])
                accepted[0].settimeout(8)
                return accepted

        self.server = Server(("127.0.0.1", 0), Handler)
        self.port = self.server.socket.getsockname()[1]
        self.pin = pin
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)


class WebhookTransportTests(EventSubscriptionTests):
    """The real webhook transport path: signature, identifiers, delivery state."""
    def receiver_url(self, path="hook"):
        return "https://callback.example:%d/%s" % (self.receiver.port, path)

    def setUp(self):
        EventSubscriptionTests.setUp(self)
        # The receiver verifies signatures with the SAME secret the bridge uses.
        self.receiver = WebhookReceiver(base64.b64decode(self.secret[6:]), str(self.temp.name))
        self.addCleanup(self.receiver.close)
        self.callback_url = self.receiver_url()
        # Only local destination admission/resolution are substituted. Trust this
        # fixture certificate explicitly; hostname and certificate checks stay on.
        def synthesizing():
            return ssl.create_default_context(cafile=str(Path(self.temp.name, "tls-cert.pem")))

        self.resolver = mock.patch.dict(a._bridge_component, {
            "webhook_addresses": lambda hostname, port: ["127.0.0.1"],
            # The SSRF guard itself is covered separately with private addresses;
            # this loopback receiver needs an explicit local-target allowance.
            "webhook_address_allowed": lambda address: address == "127.0.0.1",
            "webhook_tls_context": synthesizing})
        self.resolver.start()
        self.addCleanup(self.resolver.stop)
        # Drop the harness transport stub; the actual BridgeService.webhook_call runs.
        self.harness.patcher.stop()
        self.addCleanup(self.harness.patcher.start)

    def test_subscribe_validation_failures(self):
        insecure = self.subscribe(url="http://callback.example/insecure")
        self.assertEqual(insecure["error"]["code"], -32602)
        self.assertEqual(insecure["error"]["data"]["reason"], "invalid_webhook_url")
        embedded = self.subscribe(url="https://user:secret@callback.example/hook")
        self.assertEqual(embedded["error"]["data"]["reason"], "invalid_webhook_url")
        bad_secret = self.subscribe(secret="whsec_short")
        self.assertEqual(bad_secret["error"]["data"]["reason"], "invalid_webhook_secret")
        unknown = self.subscribe(name="another.event")
        self.assertEqual(unknown["error"]["code"], -32011)
        wrong_mode = self.subscribe(mode="poll")
        self.assertEqual(wrong_mode["error"]["data"]["reason"], "delivery_mode_unsupported")
        # Deliberately failing callback verification: point one subscribe attempt at
        # a closed loopback port so the real transport returns connection_refused.
        dead_port = self.receiver.port + 1
        while True:
            probe = socket.socket()
            try:
                probe.bind(("127.0.0.1", dead_port))
            except OSError:
                dead_port += 1
                continue
            finally:
                probe.close()
            break
        self.receiver.verify_status = 503
        blocked = self.subscribe(url=self.receiver_url("failing"))
        self.assertEqual(blocked["error"]["code"], -32015)
        self.assertEqual(blocked["error"]["data"]["reason"], "http_5xx")

    def test_subscribe_refresh_unsubscribe_and_persistence(self):
        result = self.subscribe(url=self.receiver_url())
        self.assertTrue({"id", "refreshBefore", "cursor", "truncated"}
                        <= set(result["result"]))
        identifier = result["result"]["id"]
        identifier = result["result"]["id"]
        self.assertTrue(identifier.startswith("sub_"))
        self.assertIsNone(result["result"]["cursor"])
        verification_count = len([entry for entry in self.receiver.record
                                  if entry["headers"]["webhook-id"].startswith("msg_verification_")])
        self.assertEqual(verification_count, 1)
        row = self.harness.store.find_subscription(self.ai_id, self.receiver_url(),
                                                   a.BRIDGE_EVENT_NAME, self.canonical({}))
        self.assertEqual(row["id"], identifier)
        # Idempotent refresh delivers no second challenge after the cached verify.
        self.assertEqual(self.subscribe(url=self.receiver_url())["result"]["id"], identifier)
        verification_count = len([entry for entry in self.receiver.record
                                  if entry["headers"]["webhook-id"].startswith("msg_verification_")])
        self.assertEqual(verification_count, 1)
        filtered = self.subscribe(arguments={"submission_id": "s" * 32},
                                  url=self.receiver_url("hook-s"))
        self.assertNotEqual(filtered["result"]["id"], identifier)
        first = self.rpc("events/unsubscribe", {
            "name": a.BRIDGE_EVENT_NAME, "arguments": {"submission_id": "s" * 32,
                                                       "conversation": self.store.conversation},
            "delivery": {"mode": "webhook", "url": self.receiver_url("hook-s")}})
        self.assertEqual({key: value for key, value in first["result"].items()
                          if key not in {"resultType", "_meta"}}, {})
        self.assertIsNone(self.harness.store.find_subscription(
            self.ai_id, self.receiver_url("hook-s"), a.BRIDGE_EVENT_NAME,
            self.canonical({"submission_id": "s" * 32})))
        again = self.rpc("events/unsubscribe", {
            "name": a.BRIDGE_EVENT_NAME,
            "arguments": {"conversation": self.store.conversation},
            "delivery": {"mode": "webhook", "url": self.receiver_url("hook-s")}})
        self.assertEqual({key: value for key, value in again["result"].items()
                          if key not in {"resultType", "_meta"}}, {})


    def test_delivery_signature_identifiers_and_delivery_state(self):
        receiver_url = self.receiver_url()
        created = self.store.submit({"request_id": uuid.uuid4().hex, "text": "true"})
        submission = created["submission_id"]
        result = self.subscribe(url=receiver_url, arguments={"submission_id": submission})
        sub_id = result["result"]["id"]
        self.assertTrue(self.receiver.record and self.receiver.record[0]["signature_ok"])
        self.assertTrue(self.receiver.record[0]["headers"]["webhook-id"].startswith("msg_verification_"))
        output = a.BridgeHTTP(self.harness.profile).request(
            "POST", "/v1/approved-output",
            {"version": 1, "conversation": self.store.conversation,
             "idempotency_key": uuid.uuid4().hex, "text": "APPROVED_PAYLOAD",
             "submission_id": submission, "approved_at": time.time()})
        output_id = output["output_id"]
        deadline = time.monotonic() + 6
        while time.monotonic() < deadline and len(self.receiver.record) < 2:
            time.sleep(0.02)
        entry = self.receiver.record[1]
        self.assertTrue(entry["signature_ok"])
        self.assertEqual(entry["headers"]["webhook-id"], output_id)
        self.assertEqual(entry["headers"].get("x-mcp-subscription-id"), sub_id)
        self.assertEqual(entry["headers"]["host"], "callback.example:%d" % self.receiver.port)
        self.assertEqual(self.receiver.record[0]["headers"]["host"], entry["headers"]["host"])
        value = json.loads(entry["body"])
        self.assertEqual(value["eventId"], output_id)
        self.assertEqual(set(value["data"]), {"submission_id", "output_id", "expires_at"})
        self.assertEqual(value["data"]["submission_id"], submission)
        self.assertNotIn(b"APPROVED_PAYLOAD", entry["body"])
        self.assertNotIn(self.store.conversation.encode(), entry["body"])
        self.harness.wait(lambda: self.store.submission_status(submission)["approved_output_available"] is False)
        named = self.rpc("tools/call", {"name": "get_approved_output",
                                        "arguments": {"conversation": self.store.conversation,
                                                      "submission_id": submission}})
        self.assertEqual(len(named["result"]["structuredContent"]["outputs"]), 1)
        general = self.rpc("tools/call", {"name": "get_approved_output",
                                          "arguments": {"conversation": self.store.conversation}})
        self.assertEqual(general["result"]["structuredContent"]["outputs"], [])

    def test_mixed_private_dns_is_rejected_before_any_connection(self):
        class NoConnection:
            def __init__(self, *args, **kwargs):
                raise AssertionError("a blocked target must never be contacted")
        with mock.patch.dict(a._bridge_component, {
                "webhook_addresses": lambda hostname, port: ["10.0.0.5", "8.8.8.8"],
                "WebhookConnection": NoConnection}):
            with self.assertRaises(a.WebhookFailure) as trap:
                self.harness.service.webhook_call(
                    {"id": "sub_x", "url": "https://callback.example/hook",
                     "secret": self.secret}, b"payload")
        self.assertEqual(trap.exception.reason, "connection_refused")
        self.assertFalse(trap.exception.retryable)

    def test_subscription_limit_caps_across_urls(self):
        EventSubscriptionTests.test_subscription_limit_caps_across_urls(self)

    def test_callback_certificate_hostname_mismatch_is_rejected(self):
        blocked = self.subscribe(url="https://wrong-host.example:%d/hook" % self.receiver.port)
        self.assertEqual(blocked["error"]["code"], -32015)
        self.assertEqual(blocked["error"]["data"]["reason"], "tls_error")
        self.assertEqual(self.receiver.record, [])

    def test_late_transport_ack_after_revocation_keeps_pull_fallback(self):
        identifier = self.subscribe(url=self.callback_url)["result"]["id"]
        subscription = self.store.active_subscription(identifier, self.store.clock())
        submission = self.store.submit({"request_id": uuid.uuid4().hex, "text": "true"})["submission_id"]
        self.store.on_new_output = None  # Invoke this one delivery explicitly for race control.
        output = self.store.approved_output({"version": 1, "conversation": self.store.conversation,
            "idempotency_key": uuid.uuid4().hex, "text": "LATE_ACK_OUTPUT",
            "submission_id": submission, "approved_at": self.store.clock()})
        received, release = threading.Event(), threading.Event()
        failures = []

        def before_ack():
            received.set()
            if not release.wait(5):
                failures.append("ack_fixture_timeout")

        def deliver():
            try:
                self.harness.service.deliver({"subscription": subscription, "attempts": 0,
                    "event": {"eventId": output["output_id"], "name": a.BRIDGE_EVENT_NAME,
                              "data": {"submission_id": submission, "output_id": output["output_id"]}}})
            except Exception as exc:
                failures.append(type(exc).__name__)

        self.receiver.before_ack = before_ack
        thread = threading.Thread(target=deliver)
        thread.start()
        try:
            self.assertTrue(received.wait(5))
            self.store.revoke(self.ai_id)  # Request was received; HTTP 200 has not been sent.
        finally:
            release.set()
            thread.join(timeout=6)
        self.assertFalse(thread.is_alive())
        self.assertEqual(failures, [])
        self.assertTrue(self.receiver.record[-1]["signature_ok"])
        row = self.store.execute("SELECT delivered,delivered_at FROM outputs WHERE id=?",
                                 (output["output_id"],)).fetchone()
        self.assertEqual(tuple(row), (0, None))
        self.assertTrue(self.store.submission_status(submission)["approved_output_available"])
        fresh_ai, _ = self.store.issue("AI")
        status, raw = request(self.harness.mcp_url, "POST", body={"jsonrpc": "2.0", "id": 1,
            "method": "tools/call", "params": {"name": "get_approved_output",
                "arguments": {"conversation": self.store.conversation}}},
            headers={"X-AITL-AI-Key": fresh_ai})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw)["result"]["structuredContent"]["outputs"][0]["output_id"],
                         output["output_id"])

    def test_host_authority_default_explicit_port_and_ipv6(self):
        for url, expected in (("https://callback.example/hook", "callback.example"),
                              ("https://callback.example:443/hook", "callback.example:443"),
                              ("https://[2606:4700:4700::1111]/hook", "[2606:4700:4700::1111]"),
                              ("https://[2606:4700:4700::1111]:8443/hook", "[2606:4700:4700::1111]:8443")):
            with self.subTest(url=url):
                connection = mock.Mock()
                connection.sock.getpeername.return_value = ("127.0.0.1", 443)
                connection.getresponse.return_value.status = 200
                connection.getresponse.return_value.read.return_value = b""
                with mock.patch.dict(a._bridge_component, {"WebhookConnection": mock.Mock(return_value=connection)}):
                    self.harness.service.webhook_call({"id": "sub_test", "url": url,
                                                       "secret": self.secret}, b"{}", "event_test")
                self.assertEqual(connection.request.call_args.kwargs["headers"]["Host"], expected)

    def test_all_private_dns_is_rejected(self):
        class NoConnection:
            def __init__(self, *args, **kwargs):
                raise AssertionError("a blocked target must never be contacted")
        with mock.patch.dict(a._bridge_component, {
                "webhook_addresses": lambda hostname, port: ["192.168.1.2"],
                "WebhookConnection": NoConnection}):
            with self.assertRaises(a.WebhookFailure) as trap:
                self.harness.service.webhook_call(
                    {"id": "sub_x", "url": "https://callback.example/hook",
                     "secret": self.secret}, b"payload")
        self.assertEqual(trap.exception.reason, "connection_refused")


class SubscriptionContractTests(EventSubscriptionTests):
    """events/subscribe contract: null cursor tolerated, ttlMs in milliseconds."""
    def subscribe(self, name=a.BRIDGE_EVENT_NAME, arguments=None,
                  url="https://callback.example/hook", secret=None, mode="webhook", ttl=None,
                  cursor=None):
        delivery = {"mode": mode, "url": url, "secret": secret or self.secret}
        params = {"name": name, "delivery": delivery,
                  "arguments": dict(arguments or {}, conversation=self.store.conversation)}
        if ttl is not None:
            params["ttlMs"] = ttl
        params["cursor"] = cursor  # documented (subscribe carries cursor: null)
        return self.rpc("events/subscribe", params)

    def test_subscribe_accepts_documented_cursor_null(self):
        result = self.subscribe(cursor=None)
        self.assertTrue(result["result"]["id"].startswith("sub_"))
        refresh = self.rpc("events/subscribe", {
            "name": a.BRIDGE_EVENT_NAME, "cursor": None,
            "arguments": {"conversation": self.store.conversation},
            "delivery": {"mode": "webhook", "url": "https://callback.example/hook",
                         "secret": self.secret}})
        self.assertEqual(refresh["result"]["id"], result["result"]["id"])

    def test_ttl_milliseconds_are_respected_not_read_as_seconds(self):
        granted = self.subscribe(ttl=60000, url="https://callback.example/hook-ttl")
        stamp = datetime.fromisoformat(granted["result"]["refreshBefore"].replace("Z", "+00:00"))
        self.assertAlmostEqual(stamp.timestamp(), self.store.clock() + 60, delta=3)
        peak = self.subscribe(ttl=7 * 24 * 3600 * 1000, url="https://callback.example/hook-ttl2")
        stamp_peak = datetime.fromisoformat(peak["result"]["refreshBefore"].replace("Z", "+00:00"))
        self.assertAlmostEqual(stamp_peak.timestamp(), self.store.clock() + 7 * 24 * 3600, delta=3)


class StoreSerializationTests(EventSubscriptionTests):
    def test_submit_is_idempotent_under_threads(self):
        value = {"request_id": uuid.uuid4().hex, "text": "true"}
        results = []
        lock = threading.Lock()

        def worker():
            outcome = self.store.submit(value)
            with lock:
                results.append(outcome["submission_id"])

        threads = [threading.Thread(target=worker) for _ in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
        self.assertEqual(len(set(results)), 1)
        self.assertEqual(self.store.execute("SELECT count(*) FROM proposals WHERE request=?",
                                            (value["request_id"],)).fetchone()[0], 1)

    def test_delivery_rechecks_unsubscribed_expired_and_revoked_principals(self):
        result = self.subscribe(url="https://callback.example/recheck")
        sub_id = result["result"]["id"]
        snapshot = self.store.active_subscription(sub_id, self.store.clock())
        self.assertIsNotNone(snapshot)
        self.store.delete_subscription(self.ai_id, sub_id)
        self.assertIsNone(self.store.active_subscription(sub_id, self.store.clock()))
        self.harness.service.deliver({"subscription": snapshot, "event":
                                      {"eventId": "e" * 32, "name": a.BRIDGE_EVENT_NAME,
                                       "timestamp": "", "data": {}, "cursor": None}})
        self.assertIsNone(self.harness.deliveries.get(sub_id))
        # Expired subscription: the stored deadline is in the past.
        result = self.subscribe(url="https://callback.example/recheck3")
        sub_id3 = result["result"]["id"]
        snapshot3 = self.store.active_subscription(sub_id3, self.store.clock())
        self.store.execute("UPDATE subscriptions SET expires=(?)/2.0 WHERE id=?",
                           (snapshot3["expires"], sub_id3))
        self.harness.service.deliver({"subscription": snapshot3, "event":
                                      {"eventId": "0" * 32, "name": a.BRIDGE_EVENT_NAME,
                                       "timestamp": "", "data": {}, "cursor": None}})
        self.assertIsNone(self.harness.deliveries.get(sub_id3))
        # Revoked AI principal: the token row disappears with the subscription row intact.
        second_ai, second_id = self.store.issue("AI")
        url = "https://callback.example/recheck2"
        args_json = "{}"
        revoked_id = self.store.subscription_identity(second_id, url, a.BRIDGE_EVENT_NAME, args_json)
        self.store.store_subscription(second_id, url, a.BRIDGE_EVENT_NAME, args_json,
                                      self.secret, self.store.clock() + 600, bound=True)
        snapshot2 = self.store.active_subscription(revoked_id, self.store.clock())
        self.assertIsNotNone(snapshot2)
        self.store.revoke(second_id)
        self.assertIsNone(self.store.active_subscription(revoked_id, self.store.clock()))
        self.harness.service.deliver({"subscription": snapshot2, "event":
                                      {"eventId": "f" * 32, "name": a.BRIDGE_EVENT_NAME,
                                       "timestamp": "", "data": {}, "cursor": None}})
        self.assertIsNone(self.harness.deliveries.get(revoked_id))


class FramingParityTests(EventSubscriptionTests):
    mcp_port = property(lambda self: urlsplit(self.harness.mcp_url).port)

    def test_duplicate_content_length_is_rejected(self):
        payload = b'{"jsonrpc":"2.0","id":1,"method":"ping"}'
        conn = http.client.HTTPConnection("127.0.0.1", self.mcp_port, timeout=5)
        try:
            conn.putrequest("POST", "/mcp", skip_host=True)
            conn.putheader("Host", "127.0.0.1:%d" % self.mcp_port)
            conn.putheader("X-AITL-AI-Key", self.harness.ai)
            conn.putheader("Content-Type", "application/json")
            conn.putheader("MCP-Protocol-Version", "2026-07-28")
            conn.putheader("Content-Length", str(len(payload)))
            conn.putheader("Content-Length", str(len(payload)))
            conn.endheaders()
            conn.send(payload)
            response = conn.getresponse()
            self.assertEqual(response.status, 400)
            self.assertIn(b"duplicate_header", response.read())
        finally:
            conn.close()

    def test_transfer_encoding_is_rejected(self):
        payload = b'{"jsonrpc":"2.0","id":1,"method":"ping"}'
        conn = http.client.HTTPConnection("127.0.0.1", self.mcp_port, timeout=5)
        try:
            conn.putrequest("POST", "/mcp", skip_host=True)
            conn.putheader("Host", "127.0.0.1:%d" % self.mcp_port)
            conn.putheader("X-AITL-AI-Key", self.harness.ai)
            conn.putheader("Content-Type", "application/json")
            conn.putheader("MCP-Protocol-Version", "2026-07-28")
            conn.putheader("Content-Length", str(len(payload)))
            conn.putheader("Transfer-Encoding", "chunked")
            conn.endheaders()
            conn.send(payload)
            response = conn.getresponse()
            self.assertEqual(response.status, 411)
            self.assertIn(b"transfer_encoding_rejected", response.read())
        finally:
            conn.close()


class UnboundOutputTests(EventDeliveryTests):
    """H5/M1: an ACKed un-bound output stays readable; explicit unbound binding."""
    def test_unbound_output_remains_readable_after_confirmed_push(self):
        response = self.post_output(None)
        output_id = response["output_id"]
        self.harness.wait(lambda: self.harness.deliveries.get(self.subscription_id))
        pulled = self.approved_output_tool({})
        self.assertEqual(len(pulled["result"]["structuredContent"]["outputs"]), 1)
        self.assertEqual(pulled["result"]["structuredContent"]["outputs"][0]["output_id"], output_id)
        self.assertIsNone(pulled["result"]["structuredContent"]["outputs"][0]["submission_id"])
        with_cursor = self.approved_output_tool(
            {"cursor": pulled["result"]["structuredContent"]["cursor"]})
        self.assertEqual(with_cursor["result"]["structuredContent"]["outputs"], [])


class TlsStallTests(EventSubscriptionTests):
    """H7: a stalled TLS handshake must never block the serving path."""
    def test_stalled_handshake_does_not_block_tls_serving(self):
        directory = str(self.temp.name)
        tls, pin = a.bridge_certificate(directory, ["127.0.0.1"])

        def handler_for(kind):
            return type("_HarnessTls" + kind, (a.BridgeRequestHandler,),
                        {"service": self.harness.service, "kind": kind})
        tls_server = a.BridgeHTTPServer(("127.0.0.1", 0), handler_for("CONTROLLER"), tls)
        self.harness.service.controller_hosts.add("127.0.0.1:%d" % tls_server.socket.getsockname()[1])
        profile = a.ConnectionProfile("https://127.0.0.1:%d" % tls_server.socket.getsockname()[1],
                                      pin, self.store.conversation, self.harness.controller_id,
                                      self.harness.controller, "REMOTE")
        server_thread = threading.Thread(target=tls_server.serve_forever, daemon=True)
        server_thread.start()
        self.addCleanup(tls_server.shutdown)
        self.addCleanup(tls_server.server_close)
        time.sleep(0.05)
        stall = socket.create_connection(("127.0.0.1", tls_server.socket.getsockname()[1]), timeout=5)
        try:
            client = a.BridgeHTTP(profile)
            status = client.request("GET", "/v1/status")["version"]
            self.assertEqual(status, 1)
        finally:
            stall.close()


class CorrectionRegressionTests(unittest.TestCase):
    """Focused F1/F3/F4/F5 cases, without inherited duplicate test executions."""
    setUp = EventSubscriptionTests.setUp
    rpc = EventSubscriptionTests.rpc
    subscribe = EventSubscriptionTests.subscribe

    def test_legacy_unbound_subscription_needs_authorized_refresh(self):
        # Exercise an actual pre-F1 schema upgrade in an isolated store.
        with tempfile.TemporaryDirectory(prefix="aitl-legacy-sub-") as directory:
            conversation = uuid.uuid4().hex
            store = a.BridgeStore(directory, conversation)
            ai, principal_id = store.issue("AI")
            identifier = store.store_subscription(principal_id, self.callback_url, a.BRIDGE_EVENT_NAME,
                                                  "{}", self.secret, store.clock() + 600)
            store.execute("ALTER TABLE subscriptions DROP COLUMN bound")
            store.close()
            store = a.BridgeStore(directory, conversation)
            try:
                service = a.BridgeService(store)
                self.assertIsNone(store.active_subscription(identifier, store.clock()))
                service.queue_output_events("a" * 32, None, store.clock() + 600)
                self.assertEqual(service.jobs, [])
                principal = store.authenticate(ai, "AI")
                with mock.patch.object(service, "verify_callback", return_value=True):
                    refreshed = service.subscribe(principal, {"name": a.BRIDGE_EVENT_NAME,
                        "arguments": {"conversation": conversation},
                        "delivery": {"mode": "webhook", "url": self.callback_url, "secret": self.secret}})
                self.assertEqual(refreshed["id"], identifier)
                self.assertIsNotNone(store.active_subscription(identifier, store.clock()))
                self.assertEqual(store.find_subscription(principal_id, self.callback_url,
                                                        a.BRIDGE_EVENT_NAME, "{}")["args"], "{}")
            finally:
                store.close()

    def test_parallel_subscribes_recheck_quota_after_verification(self):
        limit = a.BRIDGE_EVENT_SUBSCRIPTION_LIMIT
        for index in range(limit - 1):
            self.store.store_subscription(self.ai_id, self.callback_url + str(index), a.BRIDGE_EVENT_NAME,
                                          "{}", self.secret, self.store.clock() + 600, bound=True)
        # One contender is a refresh of an expired existing row: it is a NEW allocation.
        expired_url = self.callback_url + "expired"
        self.store.store_subscription(self.ai_id, expired_url, a.BRIDGE_EVENT_NAME,
                                      "{}", self.secret, self.store.clock() - 1, bound=True)
        barrier = threading.Barrier(2)
        outcomes, failures, observed_counts = [], [], []

        def verify(*_):
            barrier.wait(timeout=5)
            return True

        def worker(url):
            try:
                outcomes.append(self.subscribe(url=url))
                observed_counts.append(self.store.count_subscriptions(self.ai_id))
            except Exception as exc:
                failures.append(repr(exc))

        with mock.patch.object(self.harness.service, "verify_callback", verify):
            threads = [threading.Thread(target=worker, args=(url,))
                       for url in (expired_url, self.callback_url + "new")]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=6)
            self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(failures, [])
        self.assertEqual(sum("result" in outcome for outcome in outcomes), 1)
        errors = [outcome["error"] for outcome in outcomes if "error" in outcome]
        self.assertEqual([error["code"] for error in errors], [-32013])
        self.assertTrue(all(count <= limit for count in observed_counts))
        self.assertEqual(self.store.count_subscriptions(self.ai_id), limit)

    def test_expired_refresh_at_full_quota_is_rejected(self):
        expired_url = self.callback_url + "expired"
        self.store.store_subscription(self.ai_id, expired_url, a.BRIDGE_EVENT_NAME,
                                      "{}", self.secret, self.store.clock() - 1, bound=True)
        for index in range(a.BRIDGE_EVENT_SUBSCRIPTION_LIMIT):
            self.store.store_subscription(self.ai_id, self.callback_url + str(index), a.BRIDGE_EVENT_NAME,
                                          "{}", self.secret, self.store.clock() + 600, bound=True)
        blocked = self.subscribe(url=expired_url)
        self.assertEqual(blocked["error"]["code"], -32013)
        self.assertLess(self.store.find_subscription(self.ai_id, expired_url,
                                                   a.BRIDGE_EVENT_NAME, "{}")["expires"], self.store.clock())

    def test_revocation_during_callback_verification_rejects_subscription(self):
        def verify(*_):
            self.store.revoke(self.ai_id)
            return True
        with mock.patch.object(self.harness.service, "verify_callback", verify):
            blocked = self.subscribe()
        self.assertEqual(blocked["error"]["data"]["reason"], "unauthorized")
        self.assertEqual(self.store.count_subscriptions(self.ai_id), 0)

    def test_incomplete_request_lines_bound_threads_and_release_connections(self):
        worker_count = lambda: sum("process_request_thread" in thread.name for thread in threading.enumerate())
        baseline = worker_count()
        sockets = []
        port = self.harness.mcp_server.server_port
        try:
            for _ in range(64):
                sock = socket.create_connection(("127.0.0.1", port), timeout=2)
                sock.sendall(b"POST /mcp")
                sockets.append(sock)
            self.harness.wait(lambda: worker_count() - baseline == 64, timeout=2)
            for _ in range(8):
                sock = socket.create_connection(("127.0.0.1", port), timeout=2)
                try:
                    self.assertEqual(sock.recv(1), b"")
                except ConnectionResetError:
                    pass
                finally:
                    sock.close()
            # The controller listener uses the SAME pre-dispatch budget.
            other = socket.create_connection(("127.0.0.1", self.harness.controller_server.server_port), timeout=2)
            try:
                self.assertEqual(other.recv(1), b"")
            finally:
                other.close()
            self.assertLessEqual(worker_count() - baseline, 64)
            self.assertEqual(self.harness.service.inflight, 0)
        finally:
            for sock in sockets:
                sock.close()
        self.harness.wait(lambda: worker_count() == baseline, timeout=3)
        self.assertIn("result", self.rpc("ping"))

    def test_trickle_body_exceeds_monotonic_total_deadline(self):
        port = self.harness.mcp_server.server_port
        request_id = uuid.uuid4().hex
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "submit_proposal", "arguments": {"request_id": request_id,
                "text": "true", "conversation": self.store.conversation}}}).encode()
        headers = ("POST /mcp HTTP/1.1\r\nHost: 127.0.0.1:%d\r\nX-AITL-AI-Key: %s\r\n"
                   "Content-Type: application/json\r\nContent-Length: %d\r\n\r\n") % (port, self.harness.ai, len(body))
        sock = socket.create_connection(("127.0.0.1", port), timeout=8)
        stop = threading.Event()
        sent = []

        def trickle():
            for byte in body[-10:]:
                if stop.wait(0.9):
                    return
                try:
                    sock.sendall(bytes([byte]))
                    sent.append(byte)
                except OSError:
                    return

        began = time.monotonic()
        sock.sendall(headers.encode() + body[:-10])
        writer = threading.Thread(target=trickle)
        writer.start()
        try:
            response = http.client.HTTPResponse(sock)
            response.begin()
            self.assertEqual(response.status, 408)
            self.assertEqual(json.loads(response.read()), {"error": "body_timeout"})
            self.assertLess(time.monotonic() - began, 6.5)
            self.assertGreaterEqual(len(sent), 4)  # Continuous progress, no inactivity timeout.
        finally:
            stop.set()
            writer.join(timeout=2)
            sock.close()
        self.assertIsNone(self.store.execute("SELECT id FROM proposals WHERE request=?", (request_id,)).fetchone())
        self.harness.wait(lambda: self.harness.service.inflight == 0)
        self.assertIn("result", self.rpc("ping"))

    def test_trickle_headers_exceed_total_deadline_and_release_slot(self):
        sock = socket.create_connection(("127.0.0.1", self.harness.mcp_server.server_port), timeout=8)
        stop = threading.Event()
        sent = []

        def trickle():
            while not stop.wait(0.9):
                try:
                    sock.sendall(b"a")
                    sent.append(1)
                except OSError:
                    return

        began = time.monotonic()
        sock.sendall(b"POST /mcp HTTP/1.1\r\nHost: ")
        writer = threading.Thread(target=trickle)
        writer.start()
        try:
            self.assertEqual(sock.recv(1), b"")
            self.assertLess(time.monotonic() - began, 6.5)
            self.assertGreaterEqual(len(sent), 4)
        finally:
            stop.set()
            writer.join(timeout=2)
            sock.close()
        self.assertEqual(self.harness.service.inflight, 0)
        self.assertIn("result", self.rpc("ping"))

    def test_ack_update_authorization_is_atomic_with_external_sqlite_revocation(self):
        identifier = self.subscribe()["result"]["id"]
        self.store.on_new_output = None
        output = self.store.approved_output({"version": 1, "conversation": self.store.conversation,
            "idempotency_key": uuid.uuid4().hex, "text": "ACK_RACE", "submission_id": None,
            "approved_at": self.store.clock()})
        active = self.store.active_subscription
        external = sqlite3.connect(Path(self.temp.name, "bridge.sqlite3"), isolation_level=None)
        try:
            def revoke_after_check(*args):
                row = active(*args)
                self.assertIsNotNone(row)
                external.execute("DELETE FROM tokens WHERE id=?", (self.ai_id,))
                return row
            with mock.patch.object(self.store, "active_subscription", revoke_after_check):
                self.assertFalse(self.store.mark_delivered(output["output_id"], identifier))
        finally:
            external.close()
        self.assertEqual(self.store.execute("SELECT delivered,delivered_at FROM outputs WHERE id=?",
                                           (output["output_id"],)).fetchone()[:], (0, None))

    def test_late_ack_after_unsubscribe_or_expiry_cannot_suppress_pull(self):
        self.store.on_new_output = None
        for reason in ("unsubscribe", "expiry"):
            with self.subTest(reason=reason):
                identifier = self.subscribe(url=self.callback_url + reason)["result"]["id"]
                snapshot = self.store.active_subscription(identifier, self.store.clock())
                output = self.store.approved_output({"version": 1, "conversation": self.store.conversation,
                    "idempotency_key": uuid.uuid4().hex, "text": "LATE_ACK", "submission_id": None,
                    "approved_at": self.store.clock()})

                def late_ack(*_, **__):
                    if reason == "unsubscribe":
                        self.store.delete_subscription(self.ai_id, identifier)
                    else:
                        self.store.execute("UPDATE subscriptions SET expires=? WHERE id=?",
                                           (self.store.clock() - 1, identifier))
                    return 200, b""

                with mock.patch.object(self.harness.service, "webhook_call", late_ack):
                    self.harness.service.deliver({"subscription": snapshot, "event": {"eventId": output["output_id"]}})
                self.assertEqual(self.store.execute("SELECT delivered FROM outputs WHERE id=?",
                                                   (output["output_id"],)).fetchone()[0], 0)


class BindingDisplayRegressionTests(unittest.TestCase):
    """Execute the real ReviewUI logic headlessly; no GUI-smoke claim."""
    def setUp(self):
        self.first, self.second = "a" * 32, "b" * 32
        self.candidates = [{"submission_id": self.first, "text_view": "first"},
                           {"submission_id": self.second, "text_view": "second"}]
        value = [""]
        self.snapshot = SimpleNamespace(bridge=SimpleNamespace(state="CONNECTED"), review_state="REVIEW",
                                        review=SimpleNamespace(id="review-id", direction="TO_OUTPUT_SINK"))
        self.ui = SimpleNamespace(core=SimpleNamespace(output_candidates=lambda: self.candidates,
            last_proposal_id=self.second, snapshot=lambda: self.snapshot, approve=mock.Mock()),
            binding_row=mock.Mock(), binding_picker=mock.Mock(),
            binding_value=SimpleNamespace(get=lambda: value[0], set=lambda text: value.__setitem__(0, text)),
            binding_submission={}, binding_touched=False, last_binding_rows=None, last_binding_review=None,
            action=lambda run: run())

    def render(self):
        a.ReviewUI._render_binding_row(self.ui, self.snapshot, self.snapshot.review, True)

    def test_selection_only_and_unknown_label_is_rejected(self):
        self.render()
        self.assertEqual(self.ui.binding_picker.configure.call_args_list[0].kwargs["state"], "readonly")
        self.ui.binding_value.set("arbitrary input")
        with self.assertRaisesRegex(a.ProbeError, "unknown_output_binding"):
            a.ReviewUI.approve(self.ui)
        self.ui.core.approve.assert_not_called()
        self.ui.binding_value.set(next(label for label, value in self.ui.binding_submission.items() if value is None))
        a.ReviewUI.approve(self.ui)
        self.ui.core.approve.assert_called_once_with("review-id", override=False,
                                                    submission_id=None, explicit_binding=True)

    def test_approved_binding_frozen_across_candidate_expiry_removal_and_disconnect(self):
        self.render()
        label = next(label for label, value in self.ui.binding_submission.items() if value == self.first)
        self.ui.binding_value.set(label)
        self.ui.binding_touched = True
        a.ReviewUI.approve(self.ui)
        self.ui.core.approve.assert_called_once_with("review-id", override=False,
                                                    submission_id=self.first, explicit_binding=True)
        frozen = dict(self.ui.binding_submission)
        self.candidates.pop(0)  # Selected candidate expires / disappears from output_candidates.
        self.ui.binding_row.reset_mock()
        for state in ("SENDING", "DELIVERY_UNCERTAIN", "SENDING", "ACKNOWLEDGED"):
            with self.subTest(state=state):
                self.snapshot.review_state = state
                self.render()
                self.assertEqual(self.ui.binding_value.get(), label)
                self.assertEqual(self.ui.binding_submission, frozen)
                self.assertEqual(self.ui.binding_picker.configure.call_args.kwargs["state"], "disabled")
        self.candidates.clear()
        self.snapshot.bridge.state = "DISCONNECTED"
        self.render()
        self.assertEqual(self.ui.binding_value.get(), label)
        self.ui.binding_row.grid_remove.assert_not_called()
        # A different review gets a fresh mapping/default, never the consumed selection.
        self.snapshot.review = SimpleNamespace(id="next-review", direction="TO_OUTPUT_SINK")
        self.snapshot.review_state = "REVIEW"
        self.snapshot.bridge.state = "CONNECTED"
        self.candidates.append({"submission_id": self.second, "text_view": "second"})
        self.render()
        self.assertEqual(self.ui.binding_submission[self.ui.binding_value.get()], self.second)

if __name__ == "__main__":
    unittest.main(testRunner=unittest.TextTestRunner(verbosity=2, resultclass=a.SafeResult))
