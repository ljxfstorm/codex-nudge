import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlencode, urlsplit

from quota_window import auth
from quota_window.identity import Identity, IdentityError
from quota_window.storage import StateStore, StorageError
from quota_window.transport import TransportError


class AuthTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "state"
        self.now = 1800000000
        clock = patch.object(auth.time, "time", side_effect=lambda: self.now)
        clock.start()
        self.addCleanup(clock.stop)
        self.calls = []
        self.identities = []
        self.tokens = {"access_token": "SYNTHETIC_ACCESS", "id_token": "SYNTHETIC_ID",
                       "refresh_token": "SYNTHETIC_REFRESH", "expires_in": 3600}
        self.quota = {"account_id": "synthetic-account", "rate_limit": {"allowed": True,
            "primary_window": {"limit_window_seconds": 18000, "reset_at": self.now + 1000, "used_percent": 0}}}
        self.token_error = None
        self.quota_error = None
        self.identity_value = Identity("synthetic-subject", "synthetic-account")
        self.session = auth.AuthSession(self.path, request=self.request, identity=self.identity)

    def request(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if url == auth.TOKEN_URL:
            phase = json.loads((self.path / ("login.json" if kwargs["body"]["grant_type"] == "authorization_code"
                                            else "credentials.json")).read_text())["phase"]
            self.assertIn(phase, {"exchange_pending", "refresh_pending"})
            if self.token_error:
                raise self.token_error
            return self.tokens.copy()
        self.assertEqual(url, auth.USAGE_URL)
        if self.quota_error:
            raise self.quota_error
        return self.quota

    def identity(self, encoded, client_id, **kwargs):
        self.identities.append((encoded, client_id, kwargs))
        return self.identity_value

    def begin(self, **kwargs):
        challenge = self.session.prepare_login("synthetic-client", **kwargs)
        with StateStore(self.path) as store:
            pending = store.read("login.json")
        callback = auth.CALLBACK + "?" + urlencode({"state": pending["state"], "code": "SYNTHETIC_CODE"})
        return challenge, pending, callback

    def ready(self):
        _, _, callback = self.begin()
        return self.session.complete_login(callback)

    def credential(self):
        with StateStore(self.path) as store:
            return store.read("credentials.json")

    def test_login_binds_pkce_nonce_and_account_without_exposing_credentials(self):
        challenge, pending, callback = self.begin()
        self.assertEqual(self.calls, [])
        fields = parse_qs(urlsplit(challenge.url).query)
        self.assertNotIn(pending["verifier"], challenge.url)
        self.assertEqual(fields["nonce"], [pending["nonce"]])
        snapshot = self.session.complete_login(callback)
        self.assertIs(snapshot.account_matches, True)
        self.assertEqual(self.identities[0][2]["nonce"], pending["nonce"])
        self.assertEqual(self.calls[0][1]["body"]["code_verifier"], pending["verifier"])
        self.assertEqual(self.credential()["phase"], "ready")
        with StateStore(self.path) as store:
            self.assertEqual(store.read("login.json"), {"schema": 1, "phase": "complete"})
        status = json.dumps(self.session.local_status())
        for secret in ("SYNTHETIC_ACCESS", "SYNTHETIC_REFRESH", "synthetic-subject", "synthetic-account"):
            self.assertNotIn(secret, status)

    def test_callback_origin_state_duplicates_and_expiry_stop_before_exchange(self):
        _, pending, callback = self.begin()
        for candidate in (callback.replace("127.0.0.1", "evil.test"), callback + "&code=duplicate",
                          callback.replace(pending["state"], "wrong"), callback + "#fragment"):
            with self.subTest(candidate=candidate), self.assertRaises(auth.AuthError):
                self.session.complete_login(candidate)
        self.now += 901
        with self.assertRaises(auth.AuthError):
            self.session.complete_login(callback)
        self.assertEqual(self.calls, [])

    def test_denial_never_requests_tokens(self):
        _, pending, _ = self.begin()
        callback = auth.CALLBACK + "?" + urlencode({"state": pending["state"], "error": "access_denied"})
        with self.assertRaises(auth.AuthError):
            self.session.complete_login(callback)
        self.assertEqual(self.calls, [])

    def test_lost_code_exchange_and_interrupt_are_not_replayed(self):
        for error in (TransportError("unknown"), KeyboardInterrupt()):
            with self.subTest(error=type(error).__name__):
                _, _, callback = self.begin(reauthorize=True)
                self.token_error = error
                before = len(self.calls)
                with self.assertRaises(type(error)):
                    self.session.complete_login(callback)
                with self.assertRaises(auth.AuthError):
                    self.session.complete_login(callback)
                self.assertEqual(len(self.calls) - before, 1)

    def test_saved_candidate_is_recoverable_using_get_without_another_token_post(self):
        _, _, callback = self.begin()
        self.quota_error = TransportError("network error")
        with self.assertRaises(TransportError):
            self.session.complete_login(callback)
        self.assertEqual(self.session.local_status()["state"], "verification_pending")
        self.quota_error = None
        self.calls.clear()
        self.session.verify()
        self.assertEqual([url for url, _ in self.calls], [auth.USAGE_URL])
        self.assertEqual(self.credential()["phase"], "ready")

    def test_unknown_refresh_cannot_be_cleared_with_old_valid_access(self):
        self.ready()
        self.calls.clear()
        self.token_error = TransportError("unknown")
        with self.assertRaises(TransportError):
            self.session.refresh()
        for operation in (self.session.verify, self.session.query_quota, self.session.refresh):
            with self.assertRaises(auth.AuthError):
                operation()
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.credential()["phase"], "refresh_pending")

    def test_received_refresh_is_saved_before_quota_verification(self):
        self.ready()
        self.tokens = {"access_token": "NEW_SYNTHETIC_ACCESS", "refresh_token": "NEW_SYNTHETIC_REFRESH", "expires_in": 3600}
        self.quota_error = TransportError("lost quota response")
        with self.assertRaises(TransportError):
            self.session.refresh()
        value = self.credential()
        self.assertEqual(value["phase"], "verification_pending")
        self.assertEqual(value["refresh_token"], "NEW_SYNTHETIC_REFRESH")
        self.calls.clear()
        self.quota_error = None
        self.session.verify()
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0][1]["access_token"], "NEW_SYNTHETIC_ACCESS")

    def test_refresh_can_omit_replacement_id_and_refresh_tokens(self):
        self.ready()
        self.tokens = {"access_token": "NEW_SYNTHETIC_ACCESS", "expires_in": 3600}
        self.session.refresh()
        self.assertEqual(self.credential()["refresh_token"], "SYNTHETIC_REFRESH")
        self.assertEqual(self.credential()["phase"], "ready")

    def test_reauthorization_cannot_silently_switch_client_or_account(self):
        self.ready()
        with self.assertRaises(auth.AuthError):
            self.session.prepare_login("another-client", reauthorize=True)
        _, _, callback = self.begin(reauthorize=True)
        self.identity_value = Identity("other-subject", "other-account")
        with self.assertRaises(auth.AuthError):
            self.session.complete_login(callback)
        self.assertEqual(self.credential()["account_id"], "synthetic-account")

    def test_read_only_query_never_refreshes_expired_credentials(self):
        self.ready()
        self.now += 3601
        self.calls.clear()
        with self.assertRaises(auth.AuthError):
            self.session.query_quota()
        self.assertEqual(self.calls, [])
        self.assertIs(self.session.local_status()["expired"], True)

    def test_quota_mismatch_and_unknown_account_do_not_activate_credentials(self):
        _, _, callback = self.begin()
        self.quota["account_id"] = "other-account"
        with self.assertRaises(auth.AuthError):
            self.session.complete_login(callback)
        self.assertEqual(self.credential()["phase"], "verification_pending")

    def test_quota_exhaustion_does_not_make_a_valid_login_invalid(self):
        self.quota["rate_limit"]["allowed"] = False
        snapshot = self.ready()
        self.assertIs(snapshot.ordinary_usage_allowed, False)
        self.assertEqual(self.credential()["phase"], "ready")

    def test_intent_write_failure_prevents_refresh_request(self):
        self.ready()
        self.calls.clear()
        with patch.object(StateStore, "write", side_effect=StorageError("simulated write failure")):
            with self.assertRaises(StorageError):
                self.session.refresh()
        self.assertEqual(self.calls, [])

    def test_bad_record_and_invalid_signature_never_activate_new_credentials(self):
        _, _, callback = self.begin()
        with patch.object(self.session, "identity", side_effect=IdentityError("invalid signature")):
            with self.assertRaises(IdentityError):
                self.session.complete_login(callback)
        self.assertIsNone(self.session.local_status().get("expires_at"))
        with StateStore(self.path) as store:
            store.write("credentials.json", {"schema": 1, "phase": [], "expires_at": self.now})
        with self.assertRaises(auth.AuthError):
            self.session.local_status()

    def test_status_without_a_directory_does_not_create_one(self):
        self.assertEqual(self.session.local_status(), {"state": "not_logged_in"})
        self.assertFalse(self.path.exists())


if __name__ == "__main__":
    unittest.main()
