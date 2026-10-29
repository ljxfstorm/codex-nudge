"""Explicit JSON configuration without environment or shell expansion."""

from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
import re
import secrets
import stat

from .storage import _constant, _object
from .resets import ResetError, validate_lead


MAX_BYTES = 65536


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Settings:
    state_dir: Path
    client_id: str | None = None
    send: bool = False
    interval: float = 300
    reset_before_expiry_seconds: float | None = None


def absolute_path(value, base=None):
    raw = os.fspath(value)
    if not raw or any(ord(char) < 32 or ord(char) == 127 for char in raw):
        raise ConfigError("Paths must be nonempty and contain no control characters.")
    try:
        path = Path(raw).expanduser()
    except (RuntimeError, OSError):
        raise ConfigError("The path's home directory could not be determined.") from None
    if not path.is_absolute() and base is not None:
        path = base / path
    return path.absolute()


def _settings(value, base):
    if (not isinstance(value, dict) or set(value) - {"schema", "state_dir", "client_id", "send", "interval", "reset_before_expiry_seconds"}
            or type(value.get("schema")) is not int or value["schema"] != 1):
        raise ConfigError("Configuration has an unsupported schema or unknown fields.")
    directory = value.get("state_dir")
    client = value.get("client_id")
    send, interval = value.get("send", False), value.get("interval", 300)
    if not isinstance(directory, str) or not directory:
        raise ConfigError("Configuration requires a state_dir path.")
    if client is not None and (not isinstance(client, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", client)):
        raise ConfigError("Configuration client_id is invalid.")
    if type(send) is not bool:
        raise ConfigError("Configuration send must be a boolean.")
    try:
        valid_interval = (isinstance(interval, (int, float)) and not isinstance(interval, bool)
                          and math.isfinite(interval) and 2 <= interval <= 3600)
    except OverflowError:
        valid_interval = False
    if not valid_interval:
        raise ConfigError("Configuration interval must be between 2 and 3600 seconds.")
    try:
        reset_lead = validate_lead(value.get("reset_before_expiry_seconds"))
    except ResetError as error:
        raise ConfigError(str(error)) from None
    return Settings(absolute_path(directory, base), client, send, interval, reset_lead)


def load(path):
    path = absolute_path(path)
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or info.st_mode & 0o077 or info.st_size > MAX_BYTES):
                raise ConfigError("Configuration must be a private regular file owned by the current user.")
            data = stream.read(MAX_BYTES + 1)
        if len(data) > MAX_BYTES:
            raise ConfigError("Configuration exceeds its size limit.")
        value = json.loads(data, object_pairs_hook=_object, parse_constant=_constant)
    except (OSError, ValueError, RecursionError):
        raise ConfigError("Cannot read a valid private configuration file.") from None
    return _settings(value, path.parent)


def create(path, *, state_dir, client_id=None, send=False, interval=300, reset_before_expiry_seconds=None):
    path = absolute_path(path)
    settings = _settings({"schema": 1, "state_dir": str(absolute_path(state_dir)),
                          "client_id": client_id, "send": send, "interval": interval,
                          "reset_before_expiry_seconds": reset_before_expiry_seconds}, path.parent)
    data = json.dumps({"schema": 1, "state_dir": str(settings.state_dir), "client_id": settings.client_id,
                       "send": settings.send, "interval": settings.interval,
                       "reset_before_expiry_seconds": settings.reset_before_expiry_seconds}, indent=2).encode() + b"\n"
    if len(data) > MAX_BYTES:
        raise ConfigError("Configuration exceeds its size limit.")
    directory = None
    temporary = ".config-" + secrets.token_hex(16)
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                             0o600, dir_fd=directory)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        # Publish a complete file atomically, never replacing an existing file
        # or symlink. Config contains no tokens, but its send permission matters.
        os.link(temporary, path.name, src_dir_fd=directory, dst_dir_fd=directory, follow_symlinks=False)
        os.fsync(directory)
    except FileExistsError:
        raise ConfigError("Configuration already exists; it was not overwritten.") from None
    except OSError:
        raise ConfigError("Configuration could not be saved; inspect the destination before retrying.") from None
    finally:
        if directory is not None:
            try:
                os.unlink(temporary, dir_fd=directory)
            except OSError:
                pass
            finally:
                os.close(directory)
    return settings
