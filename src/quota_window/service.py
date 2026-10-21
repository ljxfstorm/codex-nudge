"""Render a user unit without installing, enabling or starting services."""

import os
from pathlib import Path
import sys

from .config import absolute_path, load


class ServiceError(Exception):
    pass


def _quote(value):
    value = os.fspath(value)
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ServiceError("Service paths cannot contain control characters.")
    return '"' + value.replace("%", "%%").replace("\\", "\\\\").replace('"', '\\"') + '"'


def render(config, *, send=None, python=None, source=None):
    config = absolute_path(config)
    load(config)
    if send is not None and type(send) is not bool:
        raise ServiceError("Service send override must be a boolean.")
    # Keep the venv interpreter symlink: resolving it would lose venv packages.
    python = absolute_path(sys.executable if python is None else python)
    source = absolute_path(Path(__file__).parent.parent if source is None else source)
    if any(char in str(python) for char in ("'", '"', "\\")):
        raise ServiceError("Systemd interpreter paths cannot contain quotes or backslashes; use another venv location.")
    if ":" in str(source):
        raise ServiceError("The development source path cannot contain a PYTHONPATH separator.")
    if not python.is_file() or not os.access(python, os.X_OK) or not source.is_dir():
        raise ServiceError("Service interpreter or module directory is unavailable.")
    command = [python, "-u", "-m", "quota_window", "--config", config, "watch"]
    if send is not None:
        command.append("--send" if send else "--no-send")
    return "\n".join([
        "[Unit]", "Description=Codex quota window watcher", "",
        "[Service]", "Type=simple", "UMask=0077", "NoNewPrivileges=true",
        "Environment=" + _quote("PYTHONPATH=" + str(source)),
        # ':' disables systemd's $ expansion; '%%' still escapes specifiers.
        "ExecStart=:" + " ".join(_quote(item) for item in command),
        "Restart=on-failure", "RestartSec=300", "RestartPreventExitStatus=2",
        "TimeoutStopSec=15", "", "[Install]", "WantedBy=default.target", "",
    ])
