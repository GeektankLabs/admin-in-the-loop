"""Durable F1–F4 regressions from the preserved independent 797cd8b probes.

Local HTTP/MCP fixtures only; no hosted tunnel or production state.
"""
import base64
import json
import sqlite3
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import admin_in_the_loop as a
from test_connected_mvp import ServiceHarness, request


class ChatBindingReviewFixTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="chatbind-fix-")
        self.addCleanup(self.temp.cleanup)
        self.now = 1800000000.0
        self.store = a.BridgeStore(self.temp.name, uuid.uuid4().hex, clock=lambda: self.now)
        self.controller, self.cid = self.store.issue("CONTROLLER")
        self.ai, self.aid = self.store.issue("AI")
        self.harness = ServiceHarness(self.store, self.controller, self.cid, self.ai, self.aid)
        self.addCleanup(self.harness.close)
        self.code = self.store.chat_binding["code"]
        self.url = "https://callback.example/same-subscription"
        self.secret = "whsec_" + base64.b64encode(b"r" * 24).decode()
        self.store.on_new_output = None

    def rpc(self, method, params):
        value = {"jsonrpc": "2.0", "id": uuid.uuid4().hex, "method": method, "params": dict(params)}
        value["params"]["_meta"] = {"io.modelcontextprotocol/protocolVersion": "2026-07-28",
                                   "io.modelcontextprotocol/clientCapabilities": {}}
        status, raw = request(self.harness.mcp_url, "POST", body=value, headers={
            "X-AITL-AI-Key": self.ai, "MCP-Protocol-Version": "2026-07-28"})
        self.assertEqual(status, 200)
        return json.loads(raw)

    def subscribe(self, code):
        return self.rpc("events/subscribe", {"name": a.BRIDGE_EVENT_NAME,
            "arguments": {"conversation": code},
            "delivery": {"mode": "webhook", "url": self.url, "secret": self.secret}})["result"]["id"]

    def rotate(self):
        return a.BridgeHTTP(self.harness.profile).request("POST", "/v1/chat-binding/rotate", {
            "version": 1, "request_id": uuid.uuid4().hex,
            "expected_generation": self.store.chat_binding["generation"]})

    def output_and_job(self, identifier):
        submission = self.store.submit({"request_id": uuid.uuid4().hex, "text": "true"})["submission_id"]
        output = a.BridgeHTTP(self.harness.profile).request("POST", "/v1/approved-output", {
            "version": 1, "conversation": self.store.conversation, "idempotency_key": uuid.uuid4().hex,
            "text": "HUMAN_APPROVED_FIXTURE", "submission_id": submission, "approved_at": self.now})
        job = {"subscription": self.store.active_subscription(identifier, self.now),
               "attempts": 0, "ready": 0,
               "event": {"eventId": output["output_id"], "name": a.BRIDGE_EVENT_NAME,
                         "data": {"submission_id": submission, "output_id": output["output_id"],
                                  "expires_at": self.now + a.BRIDGE_RETENTION}}}
        return output, submission, job

    def assert_pending(self, output, submission):
        row = self.store.execute("SELECT delivered,delivered_at FROM outputs WHERE id=?",
                                 (output["output_id"],)).fetchone()
        self.assertEqual(tuple(row), (0, None))
        self.assertTrue(self.store.submission_status(submission)["approved_output_available"])

    def test_old_ack_cannot_consume_after_same_id_resubscribe(self):
        identifier = self.subscribe(self.code)
        output, submission, job = self.output_and_job(identifier)

        def ack(*_, **__):
            rotated = self.rotate()
            self.assertEqual(self.subscribe(rotated["code"]), identifier)
            return 200, b""

        with mock.patch.object(self.harness.service, "webhook_call", side_effect=ack):
            self.harness.service.deliver(job)
        self.assert_pending(output, submission)

    def test_old_queued_job_does_not_rebind(self):
        identifier = self.subscribe(self.code)
        output, submission, job = self.output_and_job(identifier)
        self.assertEqual(self.subscribe(self.rotate()["code"]), identifier)
        with mock.patch.object(self.harness.service, "webhook_call", return_value=(200, b"")) as send:
            self.harness.service.deliver(job)
        send.assert_not_called()
        self.assert_pending(output, submission)

    def test_retry_preserves_original_generation(self):
        identifier = self.subscribe(self.code)
        output, submission, job = self.output_and_job(identifier)
        with mock.patch.object(self.harness.service, "webhook_call", side_effect=a.WebhookFailure(
                "http_5xx", retryable=True)):
            self.harness.service.deliver(job)
        with self.harness.service.jobs_cv:
            retry = self.harness.service.jobs.pop()
        self.assertEqual(retry["subscription"]["binding_generation"], 1)
        self.assertEqual(self.subscribe(self.rotate()["code"]), identifier)
        with mock.patch.object(self.harness.service, "webhook_call", return_value=(200, b"")) as send:
            self.harness.service.deliver(retry)
        send.assert_not_called()
        self.assert_pending(output, submission)

    def test_ack_sql_rechecks_persisted_binding_deadline(self):
        identifier = self.subscribe(self.code)
        output, submission, job = self.output_and_job(identifier)
        active = self.store.active_subscription
        external = sqlite3.connect(Path(self.temp.name, "bridge.sqlite3"), isolation_level=None)
        self.addCleanup(external.close)

        def expire_after_check(*args):
            row = active(*args)
            binding = dict(self.store.chat_binding, expires_at=self.now)
            external.execute("UPDATE meta SET value=? WHERE key='chat_binding'", (json.dumps(binding),))
            return row

        with mock.patch.object(self.store, "active_subscription", side_effect=expire_after_check):
            self.assertFalse(self.store.mark_delivered(output["output_id"], identifier,
                                                       job["subscription"]["binding_generation"]))
        self.assert_pending(output, submission)

    def test_healthy_ack_and_expiry_before_ack(self):
        identifier = self.subscribe(self.code)
        output, _, job = self.output_and_job(identifier)
        with mock.patch.object(self.harness.service, "webhook_call", return_value=(200, b"")):
            self.harness.service.deliver(job)
        self.assertEqual(self.store.execute("SELECT delivered FROM outputs WHERE id=?",
                                            (output["output_id"],)).fetchone()[0], 1)
        output, _, job = self.output_and_job(identifier)

        def expired_ack(*_, **__):
            self.now = self.store.chat_binding["expires_at"]
            return 200, b""

        with mock.patch.object(self.harness.service, "webhook_call", side_effect=expired_ack):
            self.harness.service.deliver(job)
        self.assertEqual(self.store.execute("SELECT delivered,delivered_at FROM outputs WHERE id=?",
                                            (output["output_id"],)).fetchone()[:], (0, None))

    def test_storage_failure_rolls_back_both_new_and_existing_counters(self):
        original = self.store._save_attempt

        def save(principal, state):
            if principal == "*":
                raise sqlite3.OperationalError("fixture storage failure")
            return original(principal, state)

        for existing in (False, True):
            with self.subTest(existing=existing):
                if existing:
                    with self.assertRaisesRegex(a.BridgeError, "conversation_binding_required"):
                        self.store.authorize_chat_binding(self.aid, "WRONG000")
                before = [tuple(row) for row in self.store.execute(
                    "SELECT * FROM chat_binding_attempts ORDER BY principal").fetchall()]
                with mock.patch.object(self.store, "_save_attempt", side_effect=save):
                    status, _ = request(self.harness.mcp_url, "POST", body={"jsonrpc": "2.0", "id": 1,
                        "method": "tools/call", "params": {"name": "get_preferences",
                        "arguments": {"conversation": "WRONG000"}}}, headers={"X-AITL-AI-Key": self.ai})
                self.assertEqual(status, 500)
                after = [tuple(row) for row in self.store.execute(
                    "SELECT * FROM chat_binding_attempts ORDER BY principal").fetchall()]
                self.assertEqual(after, before)
                self.assertFalse(self.store.db.in_transaction)

    def test_corrupt_lockout_is_not_reset_by_success(self):
        for index in range(12):
            with self.assertRaises(a.BridgeError):
                self.store.authorize_chat_binding(self.aid, "WRONG000")
            if 3 <= index < 11:
                self.now += a._bridge_component["BRIDGE_CHAT_BACKOFF"][index - 3]
        self.store.execute("UPDATE chat_binding_attempts SET locked_until=NULL WHERE principal=?", (self.aid,))
        with self.assertRaisesRegex(a.BridgeError, "chat_binding_state_invalid"):
            self.store.authorize_chat_binding(self.aid, self.code)
        self.assertEqual(self.store.execute("SELECT failures FROM chat_binding_attempts WHERE principal=?",
                                            (self.aid,)).fetchone()[0], 12)

    def test_corrupt_backoff_and_aggregate_fail_closed(self):
        for _ in range(4):
            with self.assertRaises(a.BridgeError):
                self.store.authorize_chat_binding(self.aid, "WRONG000")
        self.store.execute("UPDATE chat_binding_attempts SET next_allowed=NULL WHERE principal=?", (self.aid,))
        with self.assertRaisesRegex(a.BridgeError, "chat_binding_state_invalid"):
            self.store.authorize_chat_binding(self.aid, self.code)
        self.rotate()
        with self.assertRaises(a.BridgeError):
            self.store.authorize_chat_binding(self.aid, "WRONG000")
        self.store.execute("UPDATE chat_binding_attempts SET failures=2 WHERE principal='*'")
        with self.assertRaisesRegex(a.BridgeError, "chat_binding_state_invalid"):
            self.store.authorize_chat_binding(self.aid, self.store.chat_binding["code"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
