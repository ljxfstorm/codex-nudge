"""Foreground watcher with one lease and durable inference intent."""

import math
import time

from .auth import AuthError, AuthSession
from .inference import MODEL, RECIPE, InferenceError
from .schedule import Schedule, ScheduleError
from .storage import StateStore
from .transport import TransportError


READ_RETRY_DELAYS = (5, 15, 45)


def _read(action, *, operation, once, emit, initial_error=None):
    """Retry only an explicit GET; timing describes the successful attempt."""
    error = initial_error
    failures = 0
    while True:
        if error is None:
            started = time.time()
            try:
                result = action()
            except TransportError as caught:
                error = caught
            else:
                return result, started, time.time()
        if once or not error.retryable or failures == len(READ_RETRY_DELAYS):
            raise error
        delay = READ_RETRY_DELAYS[failures]
        if error.status == 429:
            delay = max(delay, 300)
        failures += 1
        emit({"event": "read_retry", "operation": operation, "retry": failures,
              "delay_seconds": delay, "http_status": error.status})
        sleep_for(delay)
        error = None


def _ready(session, *, send, once, emit):
    local = session.local_status()
    if local["state"] == "verification_pending" and send:
        _read(session.verify, operation="credential_verification", once=once, emit=emit)
        emit({"event": "credentials_verified"})
        local = session.local_status()
    if local["state"] != "ready":
        raise AuthError("Authentication recovery is required before watching.")
    if send and local["expires_at"] <= time.time() + 120:
        try:
            session.refresh()
        except TransportError as error:
            # A saved replacement token needs only GET verification. An unknown
            # rotation remains refresh_pending and must never enter this retry.
            if session.local_status()["state"] != "verification_pending":
                raise
            _read(session.verify, operation="credential_verification", once=once,
                  emit=emit, initial_error=error)
        emit({"event": "credentials_refreshed"})


def sleep_for(seconds):
    wall, monotonic = time.time() + seconds, time.monotonic() + seconds
    while True:
        remaining = min(wall - time.time(), monotonic - time.monotonic())
        if remaining <= 0:
            return
        time.sleep(min(60, remaining))


def run(directory, *, send=False, once=False, interval=300, emit=None):
    if (not isinstance(interval, (int, float)) or isinstance(interval, bool)
            or not math.isfinite(interval) or not 2 <= interval <= 3600):
        raise ScheduleError("Watch interval must be between 2 and 3600 seconds.")
    emit = emit or (lambda event: None)
    session = AuthSession(directory)
    binding = session.account_binding()
    # A separate lease leaves the credential lock available to status/login
    # while sleeping. Every watcher for this account uses the same subdirectory.
    with StateStore(session.directory / "watch") as store:
        engine = Schedule(store.read("schedule.json"), binding)
        while True:
            _ready(session, send=send, once=once, emit=emit)
            snapshot, started, now = _read(session.query_quota, operation="quota",
                                          once=once, emit=emit)
            try:
                decision = engine.observe(snapshot, started, now)
            except ScheduleError:
                if engine.data["attempt"] and engine.data["attempt"]["phase"] == "blocked":
                    store.write("schedule.json", engine.data)
                raise
            store.write("schedule.json", engine.data)
            window = snapshot.five_hour_window()
            emit({"event": decision, "at": now, "automatic_requests_enabled": send,
                  "reset_at": window.resets_at, "remaining_percent": window.remaining_percent})
            if decision == "idle_window" and send and snapshot.ordinary_usage_allowed is True:
                def reserve(latest, query_start, query_finish):
                    prepared = engine.prepare(latest, query_start, query_finish)
                    store.write("schedule.json", engine.data)
                    return prepared

                try:
                    usage = session.wake_once(reserve)
                except InferenceError as error:
                    attempt = engine.data["attempt"]
                    if attempt and attempt["phase"] == "sending":
                        attempt.update(phase="blocked", failure={"kind": "inference_response",
                                       "http_status": error.http_status, "content_type": error.content_type})
                        store.write("schedule.json", engine.data)
                    raise
                if usage is not None:
                    engine.completed(usage, time.time())
                    store.write("schedule.json", engine.data)
                    attempt = engine.data["attempt"]
                    emit({"event": "inference_completed", "recipe": RECIPE, "model": MODEL, "usage": usage,
                          "attempt_id": attempt["id"], "started_at": attempt["started_at"],
                          "finished_at": attempt["finished_at"]})
                else:
                    emit({"event": "preflight_cancelled"})
            # --once means one observation and at most one send; later runs can
            # finish verification without resubmitting the successful request.
            if once:
                return
            delay = engine.delay(time.time(), interval)
            if decision == "idle_window" and (not send or snapshot.ordinary_usage_allowed is not True):
                delay = interval
            local = session.local_status()
            if send and local["state"] == "ready":
                delay = min(delay, max(0.1, local["expires_at"] - time.time() - 120))
            sleep_for(delay)
