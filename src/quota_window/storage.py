"""Private JSON records, serialized across local processes on Linux."""

import fcntl
import json
import os
from pathlib import Path
import re
import secrets
import stat


MAX_RECORD_BYTES = 256 * 1024


class StorageError(Exception):
    """Safe to display; never includes record contents."""


def _private(info, *, directory=False):
    expected = stat.S_ISDIR if directory else stat.S_ISREG
    if not expected(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise StorageError("State must use private files and directories owned by the current user.")


def _object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate key")
        value[key] = item
    return value


def _constant(_):
    raise ValueError("non-finite value")


def _read_record(directory, name):
    try:
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                             dir_fd=directory)
    except FileNotFoundError:
        return None
    except OSError:
        raise StorageError("Cannot read the private state record.") from None
    try:
        with os.fdopen(descriptor, "rb") as stream:
            info = os.fstat(stream.fileno())
            _private(info)
            if info.st_size > MAX_RECORD_BYTES:
                raise StorageError("State record exceeds the size limit.")
            data = stream.read(MAX_RECORD_BYTES + 1)
            if len(data) > MAX_RECORD_BYTES:
                raise StorageError("State record exceeds the size limit.")
        value = json.loads(data, object_pairs_hook=_object, parse_constant=_constant)
        if not isinstance(value, dict):
            raise ValueError("not an object")
        return value
    except (OSError, ValueError, RecursionError):
        raise StorageError("State record is invalid; preserve it for recovery.") from None


class StateStore:
    """Hold one process lock while reading and replacing records.

    A caller keeps the context open across its read/intent/network/result
    transaction. A successful local write does not prove a remote action ran.
    """

    def __init__(self, path):
        self.path = Path(path)
        self._directory = None
        self._lock = None

    def __enter__(self):
        if self._directory is not None:
            raise StorageError("State store is already open.")
        try:
            self.path.mkdir(parents=True, exist_ok=True, mode=0o700)
            self._directory = os.open(self.path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            _private(os.fstat(self._directory), directory=True)
            self._lock = os.open(".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK,
                                 0o600, dir_fd=self._directory)
            _private(os.fstat(self._lock))
            fcntl.flock(self._lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return self
        except BlockingIOError:
            self.__exit__()
            raise StorageError("Another operation is using this state directory.") from None
        except (OSError, StorageError) as error:
            self.__exit__()
            if isinstance(error, StorageError):
                raise
            raise StorageError("Cannot open the private state directory or lock.") from None

    def __exit__(self, *_):
        for name in ("_lock", "_directory"):
            descriptor = getattr(self, name)
            if descriptor is not None:
                os.close(descriptor)
                setattr(self, name, None)

    def _check(self, name):
        if self._directory is None:
            raise StorageError("State store is not open.")
        if not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}\.json", name):
            raise StorageError("Invalid state record name.")

    def read(self, name):
        self._check(name)
        return _read_record(self._directory, name)

    def write(self, name, value):
        self._check(name)
        try:
            if not isinstance(value, dict):
                raise ValueError("not an object")
            data = json.dumps(value, allow_nan=False, separators=(",", ":")).encode()
            if len(data) > MAX_RECORD_BYTES:
                raise ValueError("too large")
        except (ValueError, TypeError, RecursionError):
            raise StorageError("State record cannot be encoded within the size limit.") from None
        temporary = ".pending-" + secrets.token_hex(16)
        try:
            try:
                _private(os.stat(name, dir_fd=self._directory, follow_symlinks=False))
            except FileNotFoundError:
                pass
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                 0o600, dir_fd=self._directory)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, name, src_dir_fd=self._directory, dst_dir_fd=self._directory)
            os.fsync(self._directory)
        except OSError:
            raise StorageError("State could not be saved; the operation outcome may be uncertain.") from None
        finally:
            try:
                os.unlink(temporary, dir_fd=self._directory)
            except FileNotFoundError:
                pass


def read_snapshot(path, name):
    """Read one atomically replaced record without creating files or taking a lease.

    For offline observation only: this does not serialize a read/modify/write
    transaction or establish consistency between multiple records.
    """
    if not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}\.json", name):
        raise StorageError("Invalid state record name.")
    try:
        directory = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return None
    except OSError:
        raise StorageError("Cannot open the private snapshot directory.") from None
    try:
        _private(os.fstat(directory), directory=True)
        return _read_record(directory, name)
    finally:
        os.close(directory)
