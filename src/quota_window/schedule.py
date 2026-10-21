"""Persistent classification of one ordinary Codex five-hour window."""

import math
import uuid

from .inference import usage_counts


DURATION = 18000
GRACE = 5
CONFIRM = 2


class ScheduleError(Exception):
    pass


def _number(value):
    try:
        return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
    except OverflowError:
        return False


def _sample(value):
    return (isinstance(value, dict) and all(_number(value.get(key)) for key in ("at", "reset"))
            and value["reset"] > 0)


class Schedule:
    def __init__(self, saved, binding):
        self.data = saved if saved is not None else {
            "schema": 1, "binding": binding, "observed_at": None, "window": None, "attempt": None}
        value = self.data
        if (not {"schema", "binding", "observed_at", "window", "attempt"} <= value.keys()
                or type(value.get("schema")) is not int or value["schema"] != 1 or value.get("binding") != binding
                or not isinstance(binding, str) or len(binding) != 64
                or (value.get("observed_at") is not None and not _number(value["observed_at"]))):
            raise ScheduleError("Schedule version or account binding is invalid; preserve state.")
        window = value.get("window")
        if window is not None:
            if (not isinstance(window, dict) or window.get("phase") not in
                    ("discovering", "armed", "await_refresh", "ready") or not _number(window.get("deadline"))):
                raise ScheduleError("Saved window is invalid; preserve state.")
            candidate = window.get("candidate")
            if candidate is not None and (not _sample(candidate) or not _number(candidate.get("query_started"))
                    or candidate["query_started"] > candidate["at"]):
                raise ScheduleError("Saved window candidate is invalid; preserve state.")
            if window["phase"] in ("discovering", "ready") and candidate is None:
                raise ScheduleError("Saved window candidate is missing; preserve state.")
        attempt = value.get("attempt")
        if attempt is not None:
            if (not isinstance(attempt, dict) or attempt.get("phase") not in
                    ("sending", "completed", "verified", "blocked") or not isinstance(attempt.get("id"), str)
                    or not _number(attempt.get("started_at"))):
                raise ScheduleError("Saved attempt is invalid; preserve state.")
            if attempt["phase"] in ("sending", "blocked"):
                raise ScheduleError("A previous inference is uncertain or blocked; automatic sending is stopped.")
            samples = attempt.get("samples")
            if (not _number(attempt.get("finished_at")) or attempt["finished_at"] < attempt["started_at"]
                    or not isinstance(samples, list) or len(samples) > 2 or any(not _sample(s) for s in samples)):
                raise ScheduleError("Saved inference result is invalid; preserve state.")
            usage_counts(attempt.get("usage"))
            if ((attempt["phase"] == "verified") != (len(samples) == 2)
                    or any(not attempt["started_at"] - 10 <= s["reset"] - DURATION <= attempt["finished_at"] + 10
                           for s in samples)
                    or samples and samples[0]["at"] < attempt["finished_at"] + 5
                    or len(samples) == 2 and (samples[1]["reset"] != samples[0]["reset"]
                        or samples[1]["at"] < max(attempt["finished_at"] + 35, samples[0]["at"] + CONFIRM))):
                raise ScheduleError("Saved verification is incomplete; preserve state.")

    def _window(self, snapshot, started, now):
        if not _number(started) or not _number(now) or started > now:
            raise ScheduleError("Invalid query timing; no inference permitted.")
        previous = self.data["observed_at"]
        if previous is not None and now < previous - 2:
            raise ScheduleError("Wall clock moved backwards; automatic sending is stopped.")
        if snapshot.account_matches is not True:
            raise ScheduleError("Quota account has not been confirmed; no inference permitted.")
        window = snapshot.five_hour_window()
        if (window is None or not _number(window.resets_at) or window.resets_at <= 0 or not _number(window.used_percent)
                or not 0 <= window.used_percent <= 100 or window.resets_at > now + DURATION + 30):
            raise ScheduleError("An unambiguous ordinary five-hour window with valid data is required.")
        self.data["observed_at"] = now
        return window

    def observe(self, snapshot, started, now):
        current = self._window(snapshot, started, now)
        attempt = self.data["attempt"]
        if attempt and attempt["phase"] == "completed":
            return self._verify(current, now)
        reset = current.resets_at
        window = self.data["window"]
        if window is None:
            window = self.data["window"] = {"phase": "await_refresh", "deadline": reset}
        if window["phase"] == "armed" and now < window["deadline"] + GRACE:
            if reset != window["deadline"] and reset > now and current.used_percent > 0:
                window.update(deadline=reset)
                return "active_window"
            if reset == window["deadline"] or now >= window["deadline"]:
                return "await_boundary"
        anchor = reset - DURATION
        if reset > now and (current.used_percent > 0 or anchor < started - 5):
            window.update(phase="armed", deadline=reset)
            window.pop("candidate", None)
            return "active_window"
        if reset <= now:
            window["phase"] = "await_refresh"
            window.pop("candidate", None)
            return "await_refresh"
        previous = window.get("candidate")
        if previous and now - previous["at"] >= CONFIRM:
            elapsed, movement = now - previous["at"], reset - previous["reset"]
            if movement == 0:
                window.update(phase="armed", deadline=reset)
                window.pop("candidate", None)
                return "stable_window"
            uncertainty = 5 + now - started + previous["at"] - previous["query_started"]
            old_anchor = previous["reset"] - DURATION
            if (movement > 0 and abs(movement - elapsed) <= uncertainty
                    and started - 5 <= anchor <= now + 5
                    and previous["query_started"] - 5 <= old_anchor <= previous["at"] + 5):
                window["phase"] = "ready"
                # Keep the latest of this validated pair for a fresh preflight.
                window["candidate"] = {"at": now, "query_started": started, "reset": reset}
                return "idle_window"
        window["phase"] = "discovering"
        if previous is None or now - previous["at"] >= CONFIRM:
            window["candidate"] = {"at": now, "query_started": started, "reset": reset}
        return "confirming_reset"

    def prepare(self, snapshot, started, now):
        """A final GET must still look idle; another client's activity cancels."""
        current = self._window(snapshot, started, now)
        window = self.data["window"]
        attempt = self.data["attempt"]
        if attempt and attempt["phase"] != "verified":
            raise ScheduleError("An unresolved inference already exists; no retry permitted.")
        if not window or window["phase"] != "ready":
            raise ScheduleError("Two observations have not confirmed an idle window.")
        candidate = window["candidate"]
        anchor = current.resets_at - DURATION
        if (snapshot.ordinary_usage_allowed is not True or current.used_percent != 0
                or not started - 5 <= anchor <= now + 5
                or current.resets_at < candidate["reset"]
                or current.resets_at == candidate["reset"] and now - candidate["at"] >= CONFIRM
                or now - candidate["at"] > 30):
            window["phase"] = "await_refresh"
            window.pop("candidate", None)
            return False
        # Equal reset within the same second cannot distinguish a tiny external
        # request; the preflight narrows this race but cannot eliminate it.
        self.data["attempt"] = {"id": uuid.uuid4().hex, "phase": "sending", "started_at": now}
        return True

    def completed(self, usage, now):
        attempt = self.data["attempt"]
        if not attempt or attempt["phase"] != "sending" or not _number(now) or now < attempt["started_at"]:
            raise ScheduleError("Inference result cannot be attached to this attempt.")
        counts = usage_counts(usage)
        # Preserve normalized optional counts when loading the result again.
        attempt.update(phase="completed", finished_at=now, usage=counts, samples=[])

    def _verify(self, current, now):
        attempt = self.data["attempt"]
        samples = attempt["samples"]
        due = attempt["finished_at"] + (5 if not samples else 35)
        if samples:
            due = max(due, samples[-1]["at"] + CONFIRM)
        if now < due:
            return "await_verification"
        anchor = current.resets_at - DURATION
        if (not attempt["started_at"] - 10 <= anchor <= attempt["finished_at"] + 10
                or current.resets_at <= now or samples and current.resets_at != samples[0]["reset"]):
            attempt["phase"] = "blocked"
            raise ScheduleError("Post-request reset did not stay fixed near the request; no retry permitted.")
        samples.append({"at": now, "reset": current.resets_at})
        if len(samples) == 2:
            attempt["phase"] = "verified"
            self.data["window"] = {"phase": "armed", "deadline": current.resets_at}
            return "fixed_reset_observed"
        return "verification_sample"

    def delay(self, now, interval=300):
        attempt, window = self.data["attempt"], self.data["window"]
        delay = interval
        if attempt and attempt["phase"] == "completed":
            samples = attempt["samples"]
            due = attempt["finished_at"] + (5 if not samples else 35)
            if samples:
                due = max(due, samples[-1]["at"] + CONFIRM)
            delay = min(delay, due - now)
        elif window:
            if window["phase"] == "armed":
                delay = min(delay, window["deadline"] + GRACE - now)
            elif window["phase"] in ("discovering", "ready"):
                delay = min(delay, window["candidate"]["at"] + CONFIRM - now)
            else:
                delay = min(delay, 30)
        return max(0.1, delay)
