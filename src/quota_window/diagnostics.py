"""Offline, allowlisted observations for shareable bug reports."""

from importlib.metadata import PackageNotFoundError, version
import math
import platform
from pathlib import Path
import time

from .inference import InferenceError, safe_content_type, usage_counts
from .storage import StorageError, read_snapshot


AUTH_PHASES = {"ready", "verification_pending", "refresh_pending"}
LOGIN_PHASES = {"waiting", "exchange_pending", "complete", "denied"}
WINDOW_PHASES = {"discovering", "armed", "await_refresh", "ready"}
ATTEMPT_PHASES = {"sending", "completed", "verified", "blocked"}


def _number(value):
    try:
        return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
    except OverflowError:
        return False


def _phase(value, allowed):
    return value if isinstance(value, str) and value in allowed else "invalid"


def _record(directory, name):
    try:
        value = read_snapshot(directory, name)
    except (StorageError, OSError):
        return "unreadable_or_invalid", None
    if value is None:
        return "missing", None
    if type(value.get("schema")) is not int or value["schema"] != 1:
        return "unsupported_or_invalid", None
    return "present", value


def collect(directory, *, settings=None):
    """Project known values only; never return raw records or error messages."""
    directory = Path(directory)
    dependencies = {}
    for name in ("PyJWT", "cryptography"):
        try:
            dependencies[name] = version(name)
        except PackageNotFoundError:
            dependencies[name] = None
    report = {"schema": 1, "offline": True,
              "runtime": {"python": platform.python_version(), "system": platform.system(),
                          "machine": platform.machine(), "dependencies": dependencies},
              "configuration": {"provided": settings is not None,
                                "watch_send": settings.send if settings else False,
                                "interval_seconds": settings.interval if settings else 300}}
    status, credentials = _record(directory, "credentials.json")
    authentication = report["authentication"] = {"record": status}
    if credentials:
        authentication["phase"] = _phase(credentials.get("phase"), AUTH_PHASES)
        expires = credentials.get("expires_at")
        if _number(expires):
            authentication.update(expires_at=expires, expired=expires <= time.time() + 15)
    status, login = _record(directory, "login.json")
    authentication["login_record"] = status
    if login:
        authentication["login_phase"] = _phase(login.get("phase"), LOGIN_PHASES)
    status, schedule = _record(directory / "watch", "schedule.json")
    watcher = report["watcher"] = {"record": status}
    if not schedule:
        return report
    window, attempt = schedule.get("window"), schedule.get("attempt")
    if isinstance(window, dict):
        watcher["window_phase"] = _phase(window.get("phase"), WINDOW_PHASES)
        if _number(window.get("deadline")):
            watcher["reset_at"] = window["deadline"]
    elif window is not None:
        watcher["window_phase"] = "invalid"
    if attempt is None:
        watcher["attempt_phase"] = None
        return report
    if not isinstance(attempt, dict):
        watcher["attempt_phase"] = "invalid"
        return report
    watcher["attempt_phase"] = _phase(attempt.get("phase"), ATTEMPT_PHASES)
    for key in ("started_at", "finished_at"):
        if _number(attempt.get(key)):
            watcher[key] = attempt[key]
    if "usage" in attempt:
        try:
            watcher["usage"] = usage_counts(attempt["usage"])
        except InferenceError:
            watcher["usage"] = None
            watcher["usage_invalid"] = True
    samples = attempt.get("samples")
    if isinstance(samples, list) and len(samples) <= 2:
        watcher["verification_samples"] = len(samples)
    failure = attempt.get("failure")
    if isinstance(failure, dict):
        status = failure.get("http_status")
        watcher["failure"] = {"kind": _phase(failure.get("kind"), {"inference_response"}),
                              "http_status": status if type(status) is int and 100 <= status <= 599 else None,
                              "content_type": safe_content_type(failure.get("content_type"))}
    return report
