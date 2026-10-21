"""Independent browser PKCE and serialized credential recovery."""

from dataclasses import dataclass
import base64
import hashlib
import json
import math
from pathlib import Path
import re
import secrets
import time
from urllib.parse import parse_qs, urlencode, urlsplit

from .identity import ISSUER, verify_identity
from .quota import parse_snapshot
from .storage import StateStore
from .transport import request_json


TOKEN_URL = ISSUER + "/oauth/token"
USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
CALLBACK = "http://127.0.0.1:1455/auth/callback"
PHASES = {"ready", "verification_pending", "refresh_pending"}


class AuthError(Exception):
    pass


@dataclass(frozen=True)
class LoginChallenge:
    url: str
    expires_at: float


def _number(value):
    try:
        return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
    except OverflowError:
        return False


def _text(value, key):
    item = value.get(key)
    if not isinstance(item, str) or not 1 <= len(item) <= 65536 or any(ord(char) < 32 for char in item):
        raise AuthError("Authentication data has a missing or invalid field.")
    return item


def _client(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value):
        raise AuthError("A valid explicit client ID is required for this compatibility route.")
    return value


def _expiry(response, access, now):
    if "expires_in" in response:
        ttl = response["expires_in"]
        if not _number(ttl) or not 0 < ttl <= 366 * 86400:
            raise AuthError("Token response has an invalid expiry.")
        expiry = now + ttl
    else:
        # The unverified access-token claim is only a scheduling hint. Identity
        # comes from the verified ID token and the server's quota account.
        try:
            part = access.split(".")[1]
            expiry = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))).get("exp")
        except (ValueError, IndexError, AttributeError):
            expiry = None
    if not _number(expiry) or expiry <= now + 15:
        raise AuthError("Access token expiry is unknown or too close.")
    return expiry


class AuthSession:
    """Own one private directory; never reads Codex or SIWC credential stores.

    Querying never refreshes. A refresh with an unknown result remains blocked;
    only received, saved tokens may be verified without a new authorization.
    """

    def __init__(self, directory, *, request=None, identity=None):
        self.directory = Path(directory)
        self.request = request or request_json
        self.identity = identity or verify_identity

    def _credentials(self, store):
        value = store.read("credentials.json")
        if value is None:
            raise AuthError("No credentials are saved; sign in first.")
        if (type(value.get("schema")) is not int or value["schema"] != 1
                or not isinstance(value.get("phase"), str) or value["phase"] not in PHASES
                or not _number(value.get("expires_at"))):
            raise AuthError("Credential state is invalid; preserve it for recovery.")
        _client(value.get("client_id"))
        for key in ("subject", "account_id", "access_token", "refresh_token", "id_token"):
            _text(value, key)
        return value

    def local_status(self):
        if not self.directory.exists() and not self.directory.is_symlink():
            return {"state": "not_logged_in"}
        with StateStore(self.directory) as store:
            pending = store.read("login.json")
            if pending is not None and (type(pending.get("schema")) is not int or pending["schema"] != 1
                    or pending.get("phase") not in ("waiting", "exchange_pending", "complete", "denied")):
                raise AuthError("Login state is invalid; preserve it for recovery.")
            if store.read("credentials.json") is None:
                return {"state": "login_pending" if pending else "not_logged_in",
                        "login_phase": pending.get("phase") if pending else None}
            value = self._credentials(store)
            return {"state": value["phase"], "expires_at": value["expires_at"],
                    "account_bound": True, "expired": value["expires_at"] <= time.time() + 15,
                    "login_phase": pending.get("phase") if pending else None}

    def prepare_login(self, client_id, *, reauthorize=False):
        client_id = _client(client_id)
        with StateStore(self.directory) as store:
            old = self._credentials(store) if store.read("credentials.json") is not None else None
            if (old or store.read("login.json") is not None) and not reauthorize:
                raise AuthError("A login already exists; resume it or explicitly reauthorize.")
            if old and old["client_id"] != client_id:
                raise AuthError("Reauthorization must retain this session's client ID.")
            pending = {"schema": 1, "phase": "waiting", "client_id": client_id,
                "state": secrets.token_urlsafe(32), "nonce": secrets.token_urlsafe(32),
                "verifier": secrets.token_urlsafe(48), "expires_at": time.time() + 900}
            challenge = base64.urlsafe_b64encode(hashlib.sha256(pending["verifier"].encode()).digest()).decode().rstrip("=")
            url = ISSUER + "/oauth/authorize?" + urlencode({"response_type": "code", "client_id": client_id,
                "redirect_uri": CALLBACK, "scope": "openid profile email offline_access",
                "state": pending["state"], "nonce": pending["nonce"], "code_challenge": challenge,
                "code_challenge_method": "S256", "id_token_add_organizations": "true",
                "codex_cli_simplified_flow": "true", "originator": "quota_window"})
            store.write("login.json", pending)
            return LoginChallenge(url, pending["expires_at"])

    def _callback(self, callback, pending):
        if not isinstance(callback, str) or len(callback) > 8192 or any(ord(c) < 32 for c in callback):
            raise AuthError("Invalid callback URL.")
        try:
            parsed = urlsplit(callback)
            if parsed._replace(query="", fragment="").geturl() != CALLBACK or parsed.fragment:
                raise ValueError()
            values = parse_qs(parsed.query, keep_blank_values=True, strict_parsing=True, max_num_fields=12)
            if any(len(items) != 1 for items in values.values()):
                raise ValueError()
            fields = {key: items[0] for key, items in values.items()}
        except ValueError:
            raise AuthError("Callback origin, path or query is invalid.") from None
        if not secrets.compare_digest(fields.get("state", "").encode(), _text(pending, "state").encode()):
            raise AuthError("Callback state does not match this login.")
        if fields.get("iss", ISSUER) != ISSUER:
            raise AuthError("Callback issuer does not match.")
        return fields

    def complete_login(self, callback):
        with StateStore(self.directory) as store:
            pending = store.read("login.json")
            if (not pending or type(pending.get("schema")) is not int or pending["schema"] != 1
                    or pending.get("phase") != "waiting" or not _number(pending.get("expires_at"))
                    or pending["expires_at"] <= time.time()):
                raise AuthError("No live login is waiting; an earlier code exchange will not be replayed.")
            fields = self._callback(callback, pending)
            if "error" in fields:
                store.write("login.json", {"schema": 1, "phase": "denied"})
                raise AuthError("Authorization was not granted; no token request sent.")
            code = _text(fields, "code")
            old = self._credentials(store) if store.read("credentials.json") is not None else None
            client_id = _client(pending.get("client_id"))
            if old and old["client_id"] != client_id:
                raise AuthError("The saved client changed during login.")
            pending["phase"] = "exchange_pending"
            store.write("login.json", pending)
            response = self.request(TOKEN_URL, body={"grant_type": "authorization_code", "client_id": client_id,
                "code": code, "code_verifier": _text(pending, "verifier"), "redirect_uri": CALLBACK}, form=True)
            record = self._token_record(response, client_id, previous=old, nonce=_text(pending, "nonce"))
            store.write("credentials.json", record)
            store.write("login.json", {"schema": 1, "phase": "complete"})
            return self._verify(store, record)

    def _token_record(self, response, client_id, *, previous=None, nonce=None):
        if not isinstance(response, dict) or response.get("token_type", "Bearer") not in ("Bearer", "bearer"):
            raise AuthError("Invalid token response.")
        access = _text(response, "access_token")
        previous = previous or {}
        if "id_token" in response:
            encoded = _text(response, "id_token")
            identity = self.identity(encoded, client_id, nonce=nonce, subject=previous.get("subject"), request=self.request)
            subject, account = identity.subject, identity.account_id
            if previous and (account != previous["account_id"] or subject != previous["subject"]):
                raise AuthError("The account changed; credentials were not activated.")
        elif previous and nonce is None:
            encoded, subject, account = previous["id_token"], previous["subject"], previous["account_id"]
        else:
            raise AuthError("Login requires a verifiable ID token.")
        refresh = _text(response if "refresh_token" in response else previous, "refresh_token")
        return {"schema": 1, "phase": "verification_pending", "client_id": client_id,
                "subject": subject, "account_id": account, "id_token": encoded,
                "access_token": access, "refresh_token": refresh,
                "expires_at": _expiry(response, access, time.time())}

    def _snapshot(self, value):
        if value["expires_at"] <= time.time() + 15:
            raise AuthError("Access token is expired or near expiry; this command will not refresh it.")
        raw = self.request(USAGE_URL, access_token=value["access_token"], account_id=value["account_id"])
        snapshot = parse_snapshot(raw, value["account_id"])
        if snapshot.account_matches is not True:
            raise AuthError("Quota did not match the verified login account.")
        return snapshot

    def _verify(self, store, value):
        if value["phase"] not in {"ready", "verification_pending"}:
            raise AuthError("Refresh result is unknown; reauthorize rather than replaying its refresh token.")
        snapshot = self._snapshot(value)
        value["phase"] = "ready"
        store.write("credentials.json", value)
        return snapshot

    def verify(self):
        with StateStore(self.directory) as store:
            return self._verify(store, self._credentials(store))

    def query_quota(self):
        with StateStore(self.directory) as store:
            value = self._credentials(store)
            if value["phase"] != "ready":
                raise AuthError("Credential recovery is pending; query will not clear it or refresh tokens.")
            return self._snapshot(value)

    def refresh(self):
        with StateStore(self.directory) as store:
            value = self._credentials(store)
            if value["phase"] != "ready":
                raise AuthError("Credential recovery is pending; refresh will not be replayed.")
            value["phase"] = "refresh_pending"
            store.write("credentials.json", value)
            response = self.request(TOKEN_URL, body={"grant_type": "refresh_token", "client_id": value["client_id"],
                "refresh_token": value["refresh_token"]}, form=True)
            updated = self._token_record(response, value["client_id"], previous=value)
            store.write("credentials.json", updated)
            return self._verify(store, updated)
