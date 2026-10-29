from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from quota_window.auth import AuthSession, RESET_CREDITS_URL, USAGE_URL
from quota_window.config import ConfigError, create, load
from quota_window.diagnostics import collect
from quota_window.resets import ExpiringResets, ResetError, parse_credits, parse_outcome
from quota_window.schedule import PersistentSchedule
from quota_window.storage import StateStore, StorageError, read_snapshot
from quota_window.transport import TransportError
from quota_window import resets, watch


class ResetTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.directory = self.root / "state"
        self.now = 1800000000
        clock = patch.object(resets.time, "time", side_effect=lambda: self.now)
        clock.start()
        self.addCleanup(clock.stop)
        with StateStore(self.directory) as store:
            store.write("credentials.json", {"schema": 1, "phase": "ready", "client_id": "synthetic-client",
                "subject": "synthetic-subject", "account_id": "synthetic-account", "id_token": "SYNTHETIC_ID",
                "access_token": "SYNTHETIC_ACCESS", "refresh_token": "SYNTHETIC_REFRESH", "expires_at": self.now + 3600})
        self.cards = [self.card("first", 600), self.card("second", 86400)]
        self.posts = []
        self.events = []
        self.calls = []
        self.failure = None
        self.fail_after_redeem = False
        self.code = "reset"
        self.expire_at_preflight = False
        self.session = AuthSession(self.directory, request=self.request)
        self.binding = self.session.account_binding()
        self.store = StateStore(self.directory / "watch")
        self.store.__enter__()
        self.addCleanup(self.store.__exit__)
        self.engine = PersistentSchedule(self.store, self.binding)
        self.manager = ExpiringResets(self.store, self.binding, 900)

    def card(self, name, remaining):
        return {"id": name, "status": "available", "reset_type": "codex_rate_limits",
                "is_supported_by_plan": True,
                "expires_at": datetime.fromtimestamp(self.now + remaining, timezone.utc).isoformat(),
                "profile_user_id": "PRIVATE_USER", "title": "PRIVATE_TITLE"}

    def details(self):
        return {"available_count": sum(c["status"] == "available" for c in self.cards), "credits": self.cards}

    def request(self, url, **kwargs):
        self.calls.append(url)
        if url == USAGE_URL:
            return {"account_id": "synthetic-account", "rate_limit": {"allowed": True,
                "primary_window": {"limit_window_seconds": 18000, "used_percent": 24, "reset_at": self.now + 1000},
                "secondary_window": {"limit_window_seconds": 604800, "used_percent": 61, "reset_at": self.now + 100000}}}
        if url == RESET_CREDITS_URL:
            if self.expire_at_preflight and self.calls.count(url) == 2:
                self.now += 601
            return self.details()
        self.assertEqual(url, RESET_CREDITS_URL + "/consume")
        body = kwargs["body"].copy()
        saved = read_snapshot(self.directory / "watch", "resets.json")["attempt"]
        self.assertEqual(saved["phase"], "sending")
        self.assertEqual(saved["credit_id"], body["credit_id"])
        self.assertEqual(saved["redeem_request_id"], body["redeem_request_id"])
        # The same credential lease covers final reads, reservation and POST.
        with self.assertRaises(StorageError), StateStore(self.directory):
            pass
        self.posts.append(body)
        if self.failure and not self.fail_after_redeem:
            raise self.failure
        if self.code in {"reset", "already_redeemed"}:
            next(c for c in self.cards if c["id"] == body["credit_id"])["status"] = "redeemed"
        if self.failure:
            raise self.failure
        return {"code": self.code, "windows_reset": 2}

    def tick(self, send=True):
        return self.manager.tick(self.session, self.engine, send=send, emit=self.events.append)

    def restored(self):
        self.manager = ExpiringResets(self.store, self.binding, 900)

    def test_partial_usage_redeems_only_earliest_expiring_card_and_verifies_after_restart(self):
        self.assertTrue(self.tick())
        self.assertEqual([p["credit_id"] for p in self.posts], ["first"])
        self.assertEqual(self.manager.attempt["phase"], "received")
        self.restored()
        self.now += 5
        self.assertTrue(self.tick())
        self.assertEqual(self.manager.attempt["phase"], "verified")
        self.assertEqual(self.manager.attempt["result"], {"code": "reset", "windows_reset": 2})
        self.assertEqual(self.manager.attempt["after"][1]["used_percent"], 61)
        # Concurrent usage need not leave the quota at 100% to confirm redemption.
        self.assertFalse(self.tick())
        self.assertEqual(len(self.posts), 1)
        self.assertEqual(self.cards[1]["status"], "available")
        self.assertNotIn("PRIVATE", json.dumps(self.events))

    def test_future_expired_unsupported_and_unknown_expiry_never_redeem(self):
        for mutate in (lambda c: c.update(expires_at=self.card("x", 2000)["expires_at"]),
                       lambda c: c.update(expires_at=self.card("x", -1)["expires_at"]),
                       lambda c: c.update(is_supported_by_plan=False),
                       lambda c: c.update(reset_type="unknown"), lambda c: c.update(expires_at=None)):
            self.cards = [self.card("first", 600)]
            mutate(self.cards[0])
            self.assertFalse(self.tick())
        self.assertEqual(self.posts, [])

    def test_readonly_and_disabled_modes_never_query_or_redeem(self):
        self.assertFalse(self.tick(send=False))
        self.manager = ExpiringResets(self.store, self.binding, None)
        self.assertFalse(self.tick())
        self.assertEqual(self.calls, [])
        self.assertIsNone(self.store.read("resets.json"))

    def test_lost_response_is_confirmed_by_selected_credit_without_replaying_post(self):
        self.failure, self.fail_after_redeem = TransportError("PRIVATE_FAILURE"), True
        with self.assertRaises(TransportError):
            self.tick()
        self.assertEqual(self.manager.attempt["phase"], "sending")
        self.restored()
        self.now += 31
        self.assertTrue(self.tick())
        self.assertEqual(self.manager.attempt["phase"], "verified")
        self.assertEqual(len(self.posts), 1)

    def test_uncertain_unsent_request_reuses_idempotency_key_after_restart(self):
        self.failure = TransportError("synthetic disconnect")
        with self.assertRaises(TransportError):
            self.tick()
        self.restored()
        self.now += 31
        self.failure = None
        self.assertTrue(self.tick())
        self.assertEqual(self.posts[0], self.posts[1])
        self.assertEqual(self.manager.attempt["post_attempts"], 2)

    def test_intent_write_failure_prevents_post(self):
        with patch.object(self.store, "write", side_effect=StorageError("synthetic failure")), \
                self.assertRaises(StorageError):
            self.tick()
        self.assertEqual(self.posts, [])

    def test_received_result_is_not_reposted_when_verification_get_fails(self):
        self.tick()
        self.now += 5
        with patch.object(self.session, "query_quota", side_effect=TransportError("failed GET")), \
                self.assertRaises(TransportError):
            self.tick()
        self.restored()
        self.assertTrue(self.tick())
        self.assertEqual(len(self.posts), 1)

    def test_result_write_failure_recovers_from_saved_intent_without_another_post(self):
        original = self.store.write

        def write(name, value):
            if name == "resets.json" and value["attempt"]["phase"] == "received":
                raise StorageError("synthetic result save failure")
            return original(name, value)

        with patch.object(self.store, "write", side_effect=write), self.assertRaises(StorageError):
            self.tick()
        self.assertEqual(self.store.read("resets.json")["attempt"]["phase"], "sending")
        self.restored()
        self.assertTrue(self.tick())
        self.assertEqual(len(self.posts), 1)

    def test_pending_redemption_cannot_switch_credit_or_bypass_disabled_mode(self):
        self.failure = TransportError("synthetic disconnect")
        with self.assertRaises(TransportError):
            self.tick()
        with self.assertRaises(ResetError):
            self.tick(send=False)
        self.cards = [self.card("second", 600)]
        self.now += 31
        with self.assertRaises(ResetError):
            self.tick()
        self.assertEqual(len(self.posts), 1)

    def test_preflight_expiry_prevents_a_late_post(self):
        self.expire_at_preflight = True
        self.assertFalse(self.tick())
        self.assertEqual(self.posts, [])

    def test_retry_after_and_post_budget_are_respected(self):
        self.failure = TransportError("busy", status=503, retry_after=120)
        for number in range(4):
            with self.assertRaises(TransportError):
                self.tick()
            self.assertEqual(len(self.posts), number + 1)
            self.now += 1
            self.assertTrue(self.tick())
            self.assertEqual(len(self.posts), number + 1)
            self.now += 120
        with self.assertRaises(ResetError):
            self.tick()
        self.assertTrue(all(post == self.posts[0] for post in self.posts))

    def test_nothing_to_reset_is_not_reported_as_redeemed(self):
        self.code = "nothing_to_reset"
        self.assertFalse(self.tick())
        self.assertEqual(self.manager.attempt["phase"], "skipped")
        self.assertEqual(self.events[-1]["event"], "reset_not_redeemed")
        self.now += 1
        self.assertFalse(self.tick())
        self.assertEqual(len(self.posts), 1)

    def test_unknown_outcome_retains_intent_and_diagnostics_redact_identity(self):
        self.code = "PRIVATE_CODE"
        with self.assertRaises(ResetError):
            self.tick()
        self.assertEqual(self.store.read("resets.json")["attempt"]["phase"], "sending")
        report = json.dumps(collect(self.directory))
        for value in ("PRIVATE", "credit_id", "redeem_request_id", "SYNTHETIC_ACCESS"):
            self.assertNotIn(value, report)

    def test_reset_waits_for_inference_verification(self):
        self.manager.next_at = self.now - 1
        with patch.object(self.engine, "can_reset", return_value=False):
            self.assertFalse(self.tick())
        self.assertEqual(self.calls, [])
        self.assertIsNone(self.manager.next_at)

    def test_config_lead_is_explicit_and_strict(self):
        path = self.root / "settings.json"
        self.assertIsNone(create(path, state_dir=self.directory).reset_before_expiry_seconds)
        value = json.loads(path.read_text())
        for lead in (True, 0, 59, 86401, float("nan"), "900"):
            value["reset_before_expiry_seconds"] = lead
            path.write_text(json.dumps(value))
            with self.assertRaises(ConfigError):
                load(path)
        value["reset_before_expiry_seconds"] = 900
        path.write_text(json.dumps(value))
        self.assertEqual(load(path).reset_before_expiry_seconds, 900)

    def test_malformed_credit_details_and_saved_state_fail_closed(self):
        for mutate in (lambda c: c.update(expires_at="2026-10-30T01:00:00"),
                       lambda c: c.update(expires_at=True), lambda c: c.update(id="")):
            card = self.card("first", 600)
            mutate(card)
            with self.assertRaises(ResetError):
                parse_credits({"available_count": 1, "credits": [card]})
        with self.assertRaises(ResetError):
            parse_credits({"available_count": 1, "credits": [self.cards[0]] * 2})
        with self.assertRaises(ResetError):
            parse_outcome({"code": "reset", "windows_reset": True})
        with self.assertRaises(ResetError):
            parse_outcome({"code": []})
        self.store.write("resets.json", {})
        with self.assertRaises(ResetError):
            self.restored()

    def test_watch_readonly_override_and_due_reset_priority(self):
        self.store.__exit__()
        with patch.object(watch, "AuthSession", return_value=self.session):
            watch.run(self.directory, send=False, once=True, reset_before_expiry_seconds=900)
            self.assertEqual(self.posts, [])
            watch.run(self.directory, send=True, once=True, reset_before_expiry_seconds=900)
        self.assertEqual(len(self.posts), 1)
        with StateStore(self.directory / "watch") as store:
            self.assertIsNone(store.read("schedule.json")["attempt"])
