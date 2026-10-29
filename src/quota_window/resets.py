"""Explicit redemption of expiring reset credits, separate from inference."""

from dataclasses import dataclass
from datetime import datetime
import hashlib
import math
import time
import uuid

from .transport import TransportError


class ResetError(Exception):
    """Safe to display; never includes account data or response text."""


def _number(value):
    try:
        return type(value) in (int, float) and math.isfinite(value) and value >= 0
    except OverflowError:
        return False


def _id(value):
    return isinstance(value, str) and 0 < len(value) <= 1024 and all(32 < ord(c) < 127 for c in value)


def validate_lead(value):
    if value is not None and (not _number(value) or not 60 <= value <= 86400):
        raise ResetError("Reset lead time must be null or 60..86400 seconds.")
    return value


@dataclass(frozen=True)
class ResetCredit:
    id: str
    status: str
    expires_at: float | None
    supported: bool

    def preview(self, lead):
        return {"credit_hash": hashlib.sha256(self.id.encode()).hexdigest()[:12],
                "available": self.status == "available", "supported": self.supported,
                "expires_at": self.expires_at,
                "redeem_at": self.expires_at - lead if self.expires_at is not None and lead is not None else None}


def parse_credits(value):
    if (not isinstance(value, dict) or type(value.get("available_count")) is not int
            or value["available_count"] < 0 or not isinstance(value.get("credits"), list)
            or len(value["credits"]) > 1000):
        raise ResetError("Reset credit details are invalid or unavailable.")
    credits, seen = [], set()
    for item in value["credits"]:
        if not isinstance(item, dict) or not _id(item.get("id")) or item["id"] in seen:
            raise ResetError("Reset credit identity is invalid or ambiguous.")
        seen.add(item["id"])
        expiry = item.get("expires_at")
        if expiry is not None:
            try:
                date = datetime.fromisoformat(expiry.replace("Z", "+00:00"))
                if date.tzinfo is None:
                    raise ValueError()
                expiry = date.timestamp()
                if not _number(expiry):
                    raise ValueError()
            except (AttributeError, TypeError, ValueError, OverflowError):
                raise ResetError("Reset credit expiry is invalid; no redemption permitted.") from None
        status = item.get("status")
        if not isinstance(status, str):
            raise ResetError("Reset credit status is invalid.")
        credits.append(ResetCredit(item["id"], status, expiry,
            item.get("reset_type") == "codex_rate_limits" and item.get("is_supported_by_plan") is True))
    if sum(c.status == "available" for c in credits) > value["available_count"]:
        raise ResetError("Reset credit counts disagree.")
    return value["available_count"], tuple(credits)


def parse_outcome(value):
    if not isinstance(value, dict) or not isinstance(value.get("code"), str) or value["code"] not in {
            "reset", "already_redeemed", "nothing_to_reset", "no_credit"}:
        raise ResetError("Reset response is unknown; preserve the redemption intent.")
    windows = value.get("windows_reset", 0)
    if type(windows) is not int or windows < 0:
        raise ResetError("Reset response has an invalid window count.")
    return {"code": value["code"], "windows_reset": windows}


def _windows(snapshot):
    return [{"bucket": w.bucket, "slot": w.slot, "duration_seconds": w.duration_seconds,
             "used_percent": w.used_percent, "resets_at": w.resets_at} for w in snapshot.windows]


class ExpiringResets:
    """One durable redemption under the same lease as the inference schedule."""

    def __init__(self, store, binding, lead):
        self.store, self.lead = store, validate_lead(lead)
        saved = store.read("resets.json")
        self.data = saved if saved is not None else {"schema": 1, "binding": binding, "attempt": None}
        if (type(self.data.get("schema")) is not int or self.data["schema"] != 1
                or self.data.get("binding") != binding or "attempt" not in self.data):
            raise ResetError("Reset recovery state is invalid; preserve it.")
        attempt = self.attempt
        if attempt is not None:
            if (not isinstance(attempt, dict) or not isinstance(attempt.get("phase"), str)
                    or attempt["phase"] not in {"sending", "received", "verified", "skipped"}
                    or not _id(attempt.get("credit_id")) or not _number(attempt.get("expires_at"))
                    or not _number(attempt.get("started_at")) or not _number(attempt.get("retry_at"))
                    or type(attempt.get("post_attempts")) is not int or not 1 <= attempt["post_attempts"] <= 4):
                raise ResetError("Saved reset attempt is invalid; preserve it.")
            try:
                uuid.UUID(attempt["redeem_request_id"])
            except (KeyError, ValueError, TypeError, AttributeError):
                raise ResetError("Saved reset idempotency key is invalid.") from None
            if attempt["phase"] != "sending":
                result = parse_outcome(attempt.get("result"))
                if ((attempt["phase"] in {"received", "verified"}) !=
                        (result["code"] in {"reset", "already_redeemed"})):
                    raise ResetError("Saved reset outcome disagrees with its phase.")
        self.next_at = None

    @property
    def attempt(self):
        return self.data["attempt"]

    @property
    def pending(self):
        return self.attempt is not None and self.attempt["phase"] in {"sending", "received"}

    def _save(self):
        self.store.write("resets.json", self.data)

    def tick(self, session, schedule, *, send, emit):
        """Return True while ordinary inference must wait for reset recovery."""
        if self.lead is None or not send:
            if self.pending:
                raise ResetError("Reset recovery is pending; explicitly enable expiring resets to resume.")
            return False
        if not schedule.can_reset():
            self.next_at = None
            return False  # Finish the existing inference's verification first.
        if self.pending and time.time() < self.attempt["retry_at"]:
            self.next_at = self.attempt["retry_at"]
            return True
        _, credits = session.query_reset_credits()
        now = time.time()
        if self.pending:
            credit = next((c for c in credits if c.id == self.attempt["credit_id"]), None)
            if credit is not None and credit.status == "redeemed":
                if self.attempt["phase"] == "sending":
                    self.attempt.update(phase="received", result={"code": "already_redeemed", "windows_reset": 0})
                    self._save()
                return self._verify(session, schedule, emit)
            if self.attempt["phase"] == "received":
                # A saved success is authoritative even when the history list
                # omits a redeemed row. Missing rows alone never resolve sending.
                if credit is None:
                    return self._verify(session, schedule, emit)
                if now > self.attempt["started_at"] + 600:
                    raise ResetError("Reset confirmation timed out; no new redemption will be sent.")
                self.attempt["retry_at"] = now + 5
                self._save()
                self.next_at = now + 5
                return True
            if now >= self.attempt["expires_at"] or self.attempt["post_attempts"] >= 4:
                raise ResetError("Reset result remains uncertain; no new redemption will be sent.")
            if credit is None or credit.status != "available" or not credit.supported:
                raise ResetError("Pending reset credit cannot be safely retried; preserve its intent.")
        else:
            available = [c for c in credits if c.supported and c.status == "available"
                         and c.expires_at is not None and c.expires_at > now]
            credit = min(available, key=lambda c: (c.expires_at, c.id), default=None)
            self.next_at = credit.expires_at - self.lead if credit else None
            if self.next_at is None or now < self.next_at:
                return False
            if self.attempt and self.attempt["credit_id"] == credit.id:
                if self.attempt["phase"] == "verified":
                    raise ResetError("A verified reset credit was offered again; no replay permitted.")
                if now < self.attempt["retry_at"]:
                    self.next_at = self.attempt["retry_at"]
                    return False

        chosen = credit.id

        def reserve(latest, snapshot, at):
            current = next((c for c in latest if c.id == chosen), None)
            if (current is None or not current.supported or current.status != "available"
                    or current.expires_at is None or not current.expires_at - self.lead <= at < current.expires_at):
                if self.pending:
                    raise ResetError("Pending reset credit changed during preflight; preserve its intent.")
                return None
            if self.pending and current.expires_at != self.attempt["expires_at"]:
                raise ResetError("Pending reset expiry changed; preserve the original intent.")
            if not self.pending:
                self.data["attempt"] = {"phase": "sending", "credit_id": chosen,
                    "expires_at": current.expires_at, "redeem_request_id": str(uuid.uuid4()),
                    "started_at": at, "retry_at": at, "post_attempts": 1, "before": _windows(snapshot)}
            else:
                self.attempt["post_attempts"] += 1
            self._save()  # A successful save is required before every POST.
            return {"credit_id": chosen, "redeem_request_id": self.attempt["redeem_request_id"]}

        try:
            result = session.redeem_reset_credit(reserve)
        except TransportError as error:
            if self.pending:
                self.attempt["retry_at"] = time.time() + max(30, error.retry_after or 0)
                self._save()
            raise
        if result is None:
            return False
        success = result["code"] in {"reset", "already_redeemed"}
        self.attempt.update(phase="received" if success else "skipped", result=result,
                            retry_at=time.time() + (5 if success else 300))
        self._save()
        emit({"event": "reset_redeemed" if success else "reset_not_redeemed", **result})
        # The next tick confirms the selected card and then reads quota.
        self.next_at = self.attempt["retry_at"]
        return success

    def _verify(self, session, schedule, emit):
        snapshot = session.query_quota()
        schedule.after_reset()
        self.attempt.update(phase="verified", after=_windows(snapshot), verified_at=time.time())
        self._save()
        self.next_at = None
        emit({"event": "reset_verified", **self.attempt["result"], "windows": _windows(snapshot)})
        return True
