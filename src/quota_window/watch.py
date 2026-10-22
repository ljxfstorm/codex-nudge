"""Foreground watcher with one lease and durable inference intent."""

from contextlib import contextmanager
import math
import signal
import threading
import time

from . import __version__
from .auth import AccessTokenExpired, AuthError, AuthSession
from .identity import IdentityError
from .inference import MODEL, RECIPE, InferenceError
from .quota import QuotaError
from .schedule import Schedule, ScheduleError
from .storage import StateStore, StorageError
from .transport import TransportError


READ_RETRY_DELAYS = (5, 15, 45)


class _Terminated(BaseException):
    """Unwind a SIGTERM without treating it as a retryable operation failure."""


@contextmanager
def _termination():
    # Python signal handlers are process-wide and can only be set on the main
    # thread. Library callers on other threads retain their caller's policy.
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    stopping = False

    def terminate(signum, frame):
        nonlocal stopping
        if not stopping:
            stopping = True
            raise _Terminated()

    previous = signal.signal(signal.SIGTERM, terminate)
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, previous)


def _failure(error):
    if isinstance(error, TransportError):
        kind = "http" if error.status is not None else "network" if error.retryable else "protocol"
        return {"kind": kind, "http_status": error.status}
    for cls, kind in ((AuthError, "authentication"), (IdentityError, "identity"),
                      (QuotaError, "quota"), (ScheduleError, "schedule"),
                      (InferenceError, "inference"), (StorageError, "storage"),
                      (KeyboardInterrupt, "interrupted")):
        if isinstance(error, cls):
            return {"kind": kind}
    return {"kind": "unexpected"}


class _Health:
    """One bounded record of the last runtime state, independent of inference intent."""

    def __init__(self, store, send):
        self.store = store
        try:
            previous = store.read("health.json") or {}
        except StorageError:
            # This snapshot can be rebuilt. write() still rejects symlinks,
            # public permissions and files owned by another user.
            previous = {}
        last_query = previous.get("last_successful_query_at")
        try:
            valid = (isinstance(last_query, (int, float)) and not isinstance(last_query, bool)
                     and math.isfinite(last_query) and last_query >= 0)
        except OverflowError:
            valid = False
        if not valid:
            last_query = None
        self.data = {"schema": 1, "version": __version__, "phase": "starting",
                     "automatic_requests_enabled": send, "started_at": time.time(),
                     "last_successful_query_at": last_query, "read_retries": 0,
                     "next_action": None, "next_action_at": None}
        self.update()

    def update(self, **fields):
        self.data.update(fields, updated_at=time.time())
        self.store.write("health.json", self.data)

    def stop(self, error=None):
        reason = _failure(error)["kind"] if error is not None else "completed"
        if isinstance(error, _Terminated):
            reason = "terminated"
        fields = {"phase": "stopped", "next_action": None, "next_action_at": None,
                  "stopped_at": time.time(), "stop_reason": reason}
        if error is not None and not isinstance(error, _Terminated):
            fields.update(last_error=_failure(error), last_error_at=time.time())
        self.update(**fields)


def _read(action, *, operation, once, emit, health, initial_error=None):
    """Retry only an explicit GET; timing describes the successful attempt."""
    error = initial_error
    failures = 0
    while True:
        if error is None:
            health.update(phase="querying", next_action=operation, next_action_at=None)
            started = time.time()
            try:
                result = action()
            except TransportError as caught:
                error = caught
            else:
                finished = time.time()
                health.update(phase="observing", last_successful_query_at=finished,
                              next_action=None, next_action_at=None, read_retries=0)
                return result, started, finished
        if once or not error.retryable:
            raise error
        exhausted = failures == len(READ_RETRY_DELAYS)
        delay = 0 if exhausted else READ_RETRY_DELAYS[failures]
        if not exhausted:
            if error.status == 429:
                delay = max(delay, 300)
            failures += 1
        delay = max(delay, error.retry_after or 0)
        if delay:
            health.update(phase="retrying", read_retries=failures, next_action=operation,
                          next_action_at=time.time() + delay, last_error=_failure(error), last_error_at=time.time())
            emit({"event": "read_cooldown" if exhausted else "read_retry", "operation": operation,
                  "retry": failures, "delay_seconds": delay, "http_status": error.status})
            sleep_for(delay)
        if exhausted:
            # Honor the final response's delay before the supervisor can restart
            # us; the request budget remains four attempts.
            raise TransportError("GET retry budget exhausted after four attempts.", status=error.status,
                                 error_code=error.error_code, error_type=error.error_type) from None
        error = None


def _ready(session, *, send, once, emit, health):
    local = session.local_status()
    if local["state"] == "verification_pending" and send:
        _read(session.verify, operation="credential_verification", once=once, emit=emit, health=health)
        emit({"event": "credentials_verified"})
        local = session.local_status()
    if local["state"] != "ready":
        raise AuthError("Authentication recovery is required before watching.")
    if send and local["expires_at"] <= time.time() + 120:
        health.update(phase="renewing", next_action="credential_renewal", next_action_at=None)
        try:
            session.refresh()
        except TransportError as error:
            # A saved replacement token needs only GET verification. An unknown
            # rotation remains refresh_pending and must never enter this retry.
            if session.local_status()["state"] != "verification_pending":
                raise
            _read(session.verify, operation="credential_verification", once=once,
                  emit=emit, health=health, initial_error=error)
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
        health = _Health(store, send)
        with _termination():
            try:
                engine = Schedule(store.read("schedule.json"), binding)
                emit({"event": "watcher_started", "version": __version__, "automatic_requests_enabled": send})
                _loop(session, store, engine, health, send=send, once=once, interval=interval, emit=emit)
            except BaseException as error:
                try:
                    health.stop(error)
                except StorageError:
                    pass  # Keep the original error and durable inference intent.
                if isinstance(error, _Terminated):
                    return
                raise
            health.stop()


def _loop(session, store, engine, health, *, send, once, interval, emit):
    while True:
        _ready(session, send=send, once=once, emit=emit, health=health)
        try:
            snapshot, started, now = _read(session.query_quota, operation="quota",
                                          once=once, emit=emit, health=health)
        except AccessTokenExpired:
            if not send:
                raise
            # Read backoff can outlast the access token. Return to the explicit
            # renewal path rather than hiding a POST inside the GET retry loop.
            continue
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

            health.update(phase="requesting", next_action="wake_request", next_action_at=None)
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
        health.update(phase="waiting", next_action="observation", next_action_at=time.time() + delay)
        sleep_for(delay)
