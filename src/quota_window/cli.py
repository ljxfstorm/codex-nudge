"""Development CLI; normal observation never rotates credentials."""

import argparse
from dataclasses import asdict
import getpass
from importlib.util import find_spec
import json
import sys
import warnings

from .auth import AuthError, AuthSession
from .config import ConfigError, absolute_path, create as create_config, load as load_config
from .diagnostics import collect as collect_diagnostics
from .identity import IdentityError
from .quota import QuotaError
from .inference import InferenceError
from .schedule import ScheduleError
from .service import ServiceError, render as render_service
from .storage import StorageError
from .transport import TransportError
from .watch import run as run_watch


def _quota(snapshot):
    return {"account_matches": snapshot.account_matches,
            "ordinary_usage_allowed": snapshot.ordinary_usage_allowed,
            "windows": [dict(asdict(window), remaining_percent=window.remaining_percent)
                        for window in snapshot.windows]}


def _parser():
    parser = argparse.ArgumentParser(description="Observe one Codex quota account independently.")
    parser.add_argument("--config", help="private JSON configuration file")
    parser.add_argument("--state-dir", help="private state directory; overrides configuration")
    commands = parser.add_subparsers(dest="command", required=True)
    setup = commands.add_parser("setup", help="create configuration without overwriting; no login or service activation")
    setup.add_argument("--client-id", help="optional compatibility OAuth client ID")
    setup.add_argument("--enable-send", action="store_true", help="explicitly enable automatic requests in this configuration")
    setup.add_argument("--interval", type=float, default=300, help="normal polling seconds (2..3600)")
    login = commands.add_parser("login", help="independent browser login or explicit credential recovery")
    login.add_argument("--client-id", help="explicit compatibility OAuth client ID; required for a new login")
    actions = login.add_mutually_exclusive_group()
    actions.add_argument("--reauthorize", action="store_true", help="start a new grant without replaying an old exchange")
    actions.add_argument("--resume", action="store_true", help="paste the callback for a still-waiting login")
    actions.add_argument("--verify", action="store_true", help="verify saved received credentials using a quota GET")
    actions.add_argument("--refresh", action="store_true", help="explicitly renew credentials once, then verify the account")
    status = commands.add_parser("status", help="query quota without inference or token refresh")
    status.add_argument("--offline", action="store_true", help="inspect local state without network access")
    doctor = commands.add_parser("doctor", help="diagnose local readiness; offline by default")
    doctor.add_argument("--online", action="store_true", help="also verify quota with a GET; never refresh")
    commands.add_parser("diagnostics", help="print an offline, redacted JSON report; no credentials or raw logs")
    watch = commands.add_parser("watch", help="observe five-hour windows using configuration or explicit mode")
    _send_options(watch)
    watch.add_argument("--once", action="store_true", help="take one observation and send at most once; verification may need later runs")
    watch.add_argument("--interval", type=float, help="normal polling seconds (2..3600); defaults to configuration or 300")
    service = commands.add_parser("service", help="print a systemd user unit; does not install or start it")
    _send_options(service)
    return parser


def _send_options(parser):
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--send", action="store_true", default=None, help="allow minimal inference and necessary renewal")
    group.add_argument("--no-send", dest="send", action="store_false", help="force read-only observation, overriding configuration")


def main(argv=None):
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "setup":
            if not args.config or not args.state_dir:
                parser.error("setup requires --config and --state-dir")
            settings = create_config(args.config, state_dir=args.state_dir, client_id=args.client_id,
                                     send=args.enable_send, interval=args.interval)
            print(json.dumps({"config_saved": True, "watch_send": settings.send}))
            return 0
        settings = load_config(args.config) if args.config else None
        if args.command == "service":
            if not args.config or args.state_dir:
                parser.error("service requires --config and does not accept --state-dir overrides")
            print(render_service(args.config, send=args.send), end="")
            return 0
        state_dir = absolute_path(args.state_dir) if args.state_dir else settings.state_dir if settings else None
        if state_dir is None:
            parser.error("provide --state-dir or a configuration with --config")
        if args.command == "diagnostics":
            print(json.dumps(collect_diagnostics(state_dir, settings=settings), allow_nan=False, indent=2))
            return 0
        if args.command == "login":
            recovery = args.resume or args.verify or args.refresh
            if recovery and args.client_id:
                parser.error("--client-id applies only to a new login")
            if not recovery:
                args.client_id = args.client_id or (settings.client_id if settings else None)
                if not args.client_id:
                    parser.error("a new login requires --client-id in arguments or configuration")
        session = AuthSession(state_dir)
        if args.command == "watch":
            send = args.send if args.send is not None else settings.send if settings else False
            interval = args.interval if args.interval is not None else settings.interval if settings else 300
            run_watch(state_dir, send=send, once=args.once, interval=interval,
                      emit=lambda event: print(json.dumps(event, allow_nan=False), flush=True))
            return 0
        if args.command == "login":
            if args.verify:
                snapshot = session.verify()
            elif args.refresh:
                snapshot = session.refresh()
            else:
                if not args.resume:
                    challenge = session.prepare_login(args.client_id, reauthorize=args.reauthorize)
                    print("Open this URL in a browser on any machine:")
                    print(challenge.url, flush=True)
                else:
                    local = session.local_status()
                    # complete_login validates state again under the process lock.
                    if local.get("login_phase") != "waiting":
                        raise AuthError("No waiting login can be resumed; explicitly reauthorize if needed.")
                if not sys.stdin.isatty():
                    raise AuthError("Callback entry requires a terminal; rerun login --resume in a terminal.")
                print("The localhost callback page may be unreachable. Copy its full address here; do not share it.")
                with warnings.catch_warnings():
                    warnings.simplefilter("error", getpass.GetPassWarning)
                    callback = getpass.getpass("Full callback URL (hidden): ")
                snapshot = session.complete_login(callback)
            result = {"state": "ready", "quota": _quota(snapshot)}
        else:
            local = session.local_status()
            result = {"authentication": local}
            online = args.command == "status" and not args.offline or args.command == "doctor" and args.online
            if args.command == "doctor":
                dependencies = {name: find_spec(name) is not None for name in ("jwt", "cryptography")}
                result.update(dependencies=dependencies, codex_required=False,
                              configured_watch_send=settings.send if settings else False,
                              ready_for_query=local["state"] == "ready" and not local.get("expired", True))
            if online and local["state"] != "not_logged_in":
                result["quota"] = _quota(session.query_quota())
            if online and local["state"] == "not_logged_in":
                result["query"] = "not_logged_in"
        print(json.dumps(result, allow_nan=False, indent=2))
        if args.command == "doctor":
            return 0 if result["ready_for_query"] and all(result["dependencies"].values()) else 1
        return 0
    except TransportError as error:
        print(str(error), file=sys.stderr)
        temporary = error.status is None or error.status == 429 or error.status >= 500
        return 2 if args.command == "watch" and not temporary else 1
    except (AuthError, IdentityError, QuotaError, StorageError, InferenceError, ScheduleError, ConfigError, ServiceError) as error:
        print(str(error), file=sys.stderr)
        return 2 if args.command == "watch" else 1
    except EOFError:
        print("Callback entry ended; no exchange was sent. Resume the waiting login in a terminal.", file=sys.stderr)
        return 1
    except getpass.GetPassWarning:
        print("Hidden callback input is unavailable; use a terminal with echo control. No exchange was sent.", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Interrupted. Inspect local state before continuing; uncertain requests will not be replayed.", file=sys.stderr)
        return 130
