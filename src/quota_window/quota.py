"""Map WHAM quota fields without inventing missing windows or permissions."""

from dataclasses import dataclass
import math


class QuotaError(Exception):
    pass


def _number(value):
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


@dataclass(frozen=True)
class Window:
    bucket: str | None
    slot: str
    duration_seconds: float | None
    resets_at: float | None
    used_percent: float | None

    @property
    def remaining_percent(self):
        return None if self.used_percent is None else 100 - self.used_percent


@dataclass(frozen=True)
class Snapshot:
    account_matches: bool | None
    ordinary_usage_allowed: bool | None
    windows: tuple[Window, ...]

    def five_hour_window(self, bucket="codex"):
        matches = [w for w in self.windows if w.bucket == bucket and w.duration_seconds == 18000]
        if len(matches) > 1:
            raise QuotaError("Multiple five-hour windows match this bucket; automatic selection is unsafe.")
        return matches[0] if matches else None


def parse_snapshot(value, expected_account):
    """Account routing is checked against the server response, never guessed.

    Missing/invalid fields stay unknown. Additional buckets keep their own
    identity and cannot substitute for a missing ordinary Codex window.
    """
    if not isinstance(value, dict) or not ({"rate_limit", "additional_rate_limits"} & value.keys()):
        raise QuotaError("Response does not contain a recognized quota structure.")
    if not isinstance(expected_account, str) or not expected_account:
        raise QuotaError("An expected account is required to interpret quota.")
    account = value.get("account_id")
    matches = account == expected_account if isinstance(account, str) and account else None
    ordinary = value.get("rate_limit")
    allowed = ordinary.get("allowed") if isinstance(ordinary, dict) else None
    windows = []

    def add(bucket, limits):
        if not isinstance(limits, dict):
            return
        for slot in ("primary", "secondary"):
            raw = limits.get(slot + "_window")
            if not isinstance(raw, dict):
                continue
            duration, reset, used = (raw.get(key) for key in (
                "limit_window_seconds", "reset_at", "used_percent"))
            windows.append(Window(bucket, slot,
                duration if _number(duration) and duration > 0 else None,
                reset if _number(reset) and reset > 0 else None,
                used if _number(used) and 0 <= used <= 100 else None))

    add("codex", ordinary)
    additional = value.get("additional_rate_limits")
    for item in additional if isinstance(additional, list) else []:
        if isinstance(item, dict):
            bucket = item.get("metered_feature")
            add(bucket if isinstance(bucket, str) and bucket else None, item.get("rate_limit"))
    return Snapshot(matches, allowed if isinstance(allowed, bool) else None, tuple(windows))
