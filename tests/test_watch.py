import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from quota_window import auth, inference, watch
from quota_window.schedule import ScheduleError
from quota_window.storage import StateStore, StorageError
from quota_window.transport import TransportError


USAGE = {"input_tokens": 13, "output_tokens": 5, "total_tokens": 18, "cached_tokens": 3, "reasoning_tokens": 0}


class WatchTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name) / "account"
        self.now = 100
        self.sent = 0
        self.refreshes = 0
        self.gets = 0
        self.anchor = None
        self.allowed = True
        self.fail_send = False
        self.fail_refresh = False
        self.preflight_used = 0
        self.events = []
        with StateStore(self.directory) as store:
            store.write("credentials.json", {"schema": 1, "phase": "ready", "client_id": "synthetic-client",
                "subject": "synthetic-subject", "account_id": "synthetic-account", "id_token": "SYNTHETIC_ID",
                "access_token": "SYNTHETIC_ACCESS", "refresh_token": "SYNTHETIC_REFRESH", "expires_at": 10000})
        for patcher in (patch.object(auth.time, "time", side_effect=lambda: self.now),
                        patch.object(auth, "request_json", side_effect=self.request),
                        patch.object(inference, "post_once", side_effect=self.post)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def request(self, url, **kwargs):
        if url == auth.TOKEN_URL:
            self.refreshes += 1
            self.assertEqual(json.loads((self.directory / "credentials.json").read_text())["phase"], "refresh_pending")
            if self.fail_refresh:
                raise TransportError("uncertain refresh")
            return {"access_token": "NEW_SYNTHETIC_ACCESS", "refresh_token": "NEW_SYNTHETIC_REFRESH", "expires_in": 3600}
        self.assertEqual(url, auth.USAGE_URL)
        self.assertEqual(kwargs["account_id"], "synthetic-account")
        self.gets += 1
        return {"account_id": "synthetic-account", "rate_limit": {"allowed": self.allowed,
            "primary_window": {"limit_window_seconds": 18000, "used_percent": self.preflight_used,
                               "reset_at": (self.anchor if self.anchor is not None else self.now) + 18000}}}

    def post(self, access, account):
        self.assertEqual(account, "synthetic-account")
        self.assertIn(access, ("SYNTHETIC_ACCESS", "NEW_SYNTHETIC_ACCESS"))
        self.assertEqual(self.saved()["attempt"]["phase"], "sending")
        self.sent += 1
        self.anchor = self.now
        self.now += 1
        if self.fail_send:
            raise TransportError("uncertain inference")
        return USAGE.copy()

    def saved(self):
        return json.loads((self.directory / "watch/schedule.json").read_text())

    def credentials(self):
        with StateStore(self.directory) as store:
            return store.read("credentials.json")

    def run_once(self, send=True):
        watch.run(self.directory, send=send, once=True, emit=self.events.append)

    def test_send_once_then_restart_only_verifies_and_retains_usage(self):
        self.run_once()
        self.assertEqual(self.sent, 0)
        self.now = 102
        self.run_once()
        self.assertEqual(self.sent, 1)
        self.assertEqual(self.saved()["attempt"]["phase"], "completed")
        self.now = 108
        self.run_once()
        self.now = 138
        self.run_once()
        self.assertEqual(self.saved()["attempt"]["phase"], "verified")
        self.assertEqual(self.saved()["attempt"]["usage"]["cached_tokens"], 3)
        self.now = 400
        self.run_once()
        self.assertEqual(self.sent, 1)
        for secret in ("SYNTHETIC_ACCESS", "SYNTHETIC_REFRESH", "synthetic-account", "synthetic-subject"):
            self.assertNotIn(secret, json.dumps(self.events))

    def test_readonly_watch_never_infers_or_renews(self):
        self.run_once(send=False)
        self.now = 102
        self.run_once(send=False)
        self.assertEqual(self.saved()["window"]["phase"], "ready")
        self.assertEqual((self.sent, self.refreshes), (0, 0))
        self.now = 10001
        with self.assertRaises(auth.AuthError):
            self.run_once(send=False)
        self.assertEqual(self.refreshes, 0)

    def test_failed_send_is_not_replayed_on_restart(self):
        self.run_once()
        self.now = 102
        self.fail_send = True
        with self.assertRaises(TransportError):
            self.run_once()
        self.assertEqual(self.saved()["attempt"]["phase"], "sending")
        with self.assertRaises(ScheduleError):
            self.run_once()
        self.assertEqual(self.sent, 1)

    def test_unexpected_response_persists_safe_metadata_and_blocks_restart(self):
        self.run_once()
        self.now = 102
        error = inference.InferenceError("PRIVATE_RESPONSE_MESSAGE", http_status=200,
                                         content_type="application/json; private=PRIVATE_PARAMETER")
        with patch.object(inference, "post_once", side_effect=error) as post, \
                self.assertRaises(inference.InferenceError):
            self.run_once()
        post.assert_called_once()
        saved = self.saved()
        self.assertEqual(saved["attempt"]["phase"], "blocked")
        self.assertEqual(saved["attempt"]["failure"], {"kind": "inference_response", "http_status": 200,
                                                       "content_type": "application/json"})
        self.assertNotIn("usage", saved["attempt"])
        self.assertNotIn("PRIVATE_", json.dumps(saved))
        with patch.object(inference, "post_once") as post, self.assertRaises(ScheduleError):
            self.run_once()
        post.assert_not_called()

    def test_intent_write_failure_prevents_inference(self):
        self.run_once()
        self.now = 102
        original = StateStore.write

        def write(store, name, value):
            attempt = value.get("attempt")
            if name == "schedule.json" and attempt and attempt["phase"] == "sending":
                raise StorageError("intent not saved")
            return original(store, name, value)

        with patch.object(StateStore, "write", new=write), self.assertRaises(StorageError):
            self.run_once()
        self.assertEqual(self.sent, 0)

    def test_result_write_failure_preserves_uncertain_marker(self):
        self.run_once()
        self.now = 102
        original = StateStore.write

        def write(store, name, value):
            attempt = value.get("attempt")
            if name == "schedule.json" and attempt and attempt["phase"] == "completed":
                raise StorageError("result not saved")
            return original(store, name, value)

        with patch.object(StateStore, "write", new=write), self.assertRaises(StorageError):
            self.run_once()
        with self.assertRaises(ScheduleError):
            self.run_once()
        self.assertEqual(self.sent, 1)

    def test_final_preflight_cancels_after_another_client_uses_quota(self):
        self.run_once()
        self.now = 102
        original = auth.AuthSession.wake_once

        def final_check(session, reserve):
            self.preflight_used = 1
            return original(session, reserve)

        with patch.object(auth.AuthSession, "wake_once", new=final_check):
            self.run_once()
        self.assertEqual(self.sent, 0)
        self.assertIsNone(self.saved()["attempt"])
        self.assertEqual(self.events[-1]["event"], "preflight_cancelled")

    def test_permission_denied_waits_at_normal_interval_without_inference(self):
        class Finished(Exception):
            pass

        self.run_once()
        self.now = 102
        self.allowed = False

        def sleep(delay):
            self.assertEqual(delay, 300)
            # The watcher lease never prevents status from opening credentials.
            self.assertEqual(auth.AuthSession(self.directory).local_status()["state"], "ready")
            raise Finished()

        with patch.object(watch, "sleep_for", side_effect=sleep), self.assertRaises(Finished):
            watch.run(self.directory, send=True)
        self.assertEqual(self.sent, 0)

    def test_only_enabled_watch_renews_and_unknown_refresh_stays_blocked(self):
        with StateStore(self.directory) as store:
            value = store.read("credentials.json")
            value["expires_at"] = 110
            store.write("credentials.json", value)
        self.run_once()
        self.assertEqual(self.refreshes, 1)
        self.assertEqual(self.credentials()["access_token"], "NEW_SYNTHETIC_ACCESS")
        self.now = 3650
        self.fail_refresh = True
        with self.assertRaises(TransportError):
            self.run_once()
        with self.assertRaises(auth.AuthError):
            self.run_once()
        self.assertEqual(self.refreshes, 2)

    def test_two_watchers_cannot_share_an_account(self):
        with StateStore(self.directory / "watch"), self.assertRaises(StorageError):
            self.run_once()
        self.assertEqual(self.sent, 0)

    def test_continuous_watch_finishes_a_cycle_without_repeating_inference(self):
        class Finished(Exception):
            pass

        def sleep(delay):
            self.now += delay

        def emit(event):
            self.events.append(event)
            if event["event"] == "fixed_reset_observed":
                raise Finished()

        with patch.object(watch, "sleep_for", side_effect=sleep), self.assertRaises(Finished):
            watch.run(self.directory, send=True, emit=emit)
        self.assertEqual(self.sent, 1)
        self.assertEqual(self.now, 138)
        self.assertEqual(self.saved()["attempt"]["phase"], "verified")

    def test_interrupted_inference_preserves_marker_and_cannot_replay(self):
        self.run_once()
        self.now = 102
        with patch.object(inference, "post_once", side_effect=KeyboardInterrupt()), self.assertRaises(KeyboardInterrupt):
            self.run_once()
        self.assertEqual(self.saved()["attempt"]["phase"], "sending")
        with self.assertRaises(ScheduleError):
            self.run_once()

    def test_failed_anchor_verification_is_persisted_and_blocks_restart(self):
        self.run_once()
        self.now = 102
        self.run_once()
        self.now = 108
        self.run_once()
        self.anchor = None
        self.now = 138
        with self.assertRaises(ScheduleError):
            self.run_once()
        self.assertEqual(self.saved()["attempt"]["phase"], "blocked")
        with self.assertRaises(ScheduleError):
            self.run_once()
        self.assertEqual(self.sent, 1)

    def test_readonly_failure_before_intent_can_be_retried(self):
        with patch.object(auth.AuthSession, "query_quota", side_effect=TransportError("quota unavailable")), \
                self.assertRaises(TransportError):
            self.run_once()
        self.run_once()
        self.assertEqual(self.sent, 0)

    def test_interval_is_bounded_and_validated_before_io(self):
        for interval in (0, 1, True, float("nan"), 3601):
            with self.subTest(interval=interval), self.assertRaises(ScheduleError):
                watch.run(self.directory, interval=interval)
