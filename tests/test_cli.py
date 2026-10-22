from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import getpass
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from quota_window.auth import AuthSession, LoginChallenge
from quota_window.cli import main
from quota_window.config import create, load
from quota_window.quota import Snapshot, Window
from quota_window.schedule import ScheduleError
from quota_window.transport import TransportError


class CliTests(unittest.TestCase):
    def invoke(self, directory, *args):
        out, err = StringIO(), StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(["--state-dir", str(directory), *args])
        return code, out.getvalue(), err.getvalue()

    def test_missing_state_observation_is_offline_and_does_not_create_files(self):
        with tempfile.TemporaryDirectory() as root, patch("quota_window.auth.request_json") as request:
            directory = Path(root) / "absent"
            for command in (("status",), ("status", "--offline"), ("doctor",), ("doctor", "--online")):
                code, output, error = self.invoke(directory, *command)
                self.assertEqual(code, 1 if command[0] == "doctor" else 0)
                self.assertEqual(json.loads(output)["authentication"]["state"], "not_logged_in")
                self.assertEqual(error, "")
            request.assert_not_called()
            self.assertFalse(directory.exists())

    def test_readonly_commands_never_refresh_or_exchange(self):
        local = {"state": "ready", "expired": False}
        snapshot = Snapshot(True, False, (Window("codex", "primary", 18000, 1900000000, 100),))
        with patch.object(AuthSession, "local_status", return_value=local), \
                patch.object(AuthSession, "query_quota", return_value=snapshot) as query, \
                patch.object(AuthSession, "refresh") as refresh, \
                patch.object(AuthSession, "complete_login") as exchange:
            for command in (("status", "--offline"), ("doctor",)):
                self.assertEqual(self.invoke("unused", *command)[0], 0)
            query.assert_not_called()
            for command in (("status",), ("doctor", "--online")):
                code, output, _ = self.invoke("unused", *command)
                self.assertEqual(code, 0)
                self.assertEqual(json.loads(output)["quota"]["windows"][0]["remaining_percent"], 0)
            self.assertEqual(query.call_count, 2)
            refresh.assert_not_called()
            exchange.assert_not_called()

    def test_login_callback_uses_hidden_terminal_input(self):
        with patch.object(AuthSession, "prepare_login", return_value=LoginChallenge("https://example.test", 1)) as prepare, \
                patch.object(AuthSession, "complete_login", return_value=Snapshot(True, True, ())) as complete, \
                patch("quota_window.cli.sys.stdin.isatty", return_value=True), \
                patch("quota_window.cli.getpass.getpass", return_value="private-callback") as hidden:
            code, out, err = self.invoke("unused", "login", "--client-id", "app_test")
            self.assertEqual(code, 0)
            prepare.assert_called_once_with("app_test", reauthorize=False)
            complete.assert_called_once_with("private-callback")
            hidden.assert_called_once()
            self.assertNotIn("private-callback", out + err)

    def test_nonterminal_login_can_be_resumed_without_an_exchange(self):
        with patch.object(AuthSession, "prepare_login", return_value=LoginChallenge("url", 1)), \
                patch.object(AuthSession, "complete_login") as complete, \
                patch("quota_window.cli.sys.stdin.isatty", return_value=False):
            code, _, err = self.invoke("unused", "login", "--client-id", "app_test")
            self.assertEqual(code, 1)
            self.assertIn("--resume", err)
            complete.assert_not_called()

    def test_hidden_input_failure_does_not_fall_back_to_visible_callback(self):
        with patch.object(AuthSession, "prepare_login", return_value=LoginChallenge("url", 1)), \
                patch.object(AuthSession, "complete_login") as complete, \
                patch("quota_window.cli.sys.stdin.isatty", return_value=True), \
                patch("quota_window.cli.getpass.getpass", side_effect=getpass.GetPassWarning("no echo control")):
            code, _, err = self.invoke("unused", "login", "--client-id", "app_test")
            self.assertEqual(code, 1)
            self.assertIn("No exchange was sent", err)
            complete.assert_not_called()

    def test_resume_uses_waiting_login_without_starting_another(self):
        with patch.object(AuthSession, "local_status", return_value={"state": "ready", "login_phase": "waiting"}), \
                patch.object(AuthSession, "prepare_login") as prepare, \
                patch.object(AuthSession, "complete_login", return_value=Snapshot(True, True, ())), \
                patch("quota_window.cli.sys.stdin.isatty", return_value=True), \
                patch("quota_window.cli.getpass.getpass", return_value="callback"):
            self.assertEqual(self.invoke("unused", "login", "--resume")[0], 0)
            prepare.assert_not_called()

    def test_explicit_refresh_and_verification_are_separate(self):
        with patch.object(AuthSession, "refresh", return_value=Snapshot(True, True, ())) as refresh, \
                patch.object(AuthSession, "verify", return_value=Snapshot(True, True, ())) as verify, \
                patch.object(AuthSession, "prepare_login") as prepare:
            self.assertEqual(self.invoke("unused", "login", "--refresh")[0], 0)
            self.assertEqual(self.invoke("unused", "login", "--verify")[0], 0)
            refresh.assert_called_once()
            verify.assert_called_once()
            prepare.assert_not_called()

    def test_safe_transport_errors_do_not_emit_tracebacks(self):
        with patch.object(AuthSession, "local_status", return_value={"state": "ready"}), \
                patch.object(AuthSession, "query_quota", side_effect=TransportError("OpenAI HTTP 530; no retry sent.")):
            code, out, err = self.invoke("unused", "status")
            self.assertEqual(code, 1)
            self.assertEqual(out, "")
            self.assertEqual(err, "OpenAI HTTP 530; no retry sent.\n")

    def test_watch_requires_explicit_opt_in_and_forwards_one_shot_semantics(self):
        with patch("quota_window.cli.run_watch") as run:
            self.assertEqual(self.invoke("unused", "watch", "--once")[0], 0)
            self.assertFalse(run.call_args.kwargs["send"])
            self.assertTrue(run.call_args.kwargs["once"])
            self.assertEqual(self.invoke("unused", "watch", "--send", "--interval", "60")[0], 0)
            self.assertTrue(run.call_args.kwargs["send"])
            self.assertEqual(run.call_args.kwargs["interval"], 60)

    def configured(self, config, *args, state_dir=None):
        out, err = StringIO(), StringIO()
        argv = ["--config", str(config)]
        if state_dir is not None:
            argv += ["--state-dir", str(state_dir)]
        with redirect_stdout(out), redirect_stderr(err):
            code = main([*argv, *args])
        return code, out.getvalue(), err.getvalue()

    def test_setup_is_offline_private_and_never_overwrites(self):
        with tempfile.TemporaryDirectory() as root, patch("quota_window.cli.AuthSession") as auth:
            config, state = Path(root) / "config.json", Path(root) / "state"
            code, out, err = self.configured(config, "setup", state_dir=state)
            self.assertEqual((code, err), (0, ""))
            self.assertFalse(json.loads(out)["watch_send"])
            self.assertFalse(load(config).send)
            self.assertFalse(state.exists())
            self.assertEqual(self.configured(config, "setup", "--enable-send", state_dir=state)[0], 1)
            self.assertFalse(load(config).send)
            auth.assert_not_called()

    def test_configuration_precedence_and_readonly_override(self):
        with tempfile.TemporaryDirectory() as root, patch("quota_window.cli.run_watch") as run:
            config, state, override = Path(root) / "config.json", Path(root) / "state", Path(root) / "other"
            create(config, state_dir=state, send=True, interval=60)
            self.assertEqual(self.configured(config, "watch", "--once")[0], 0)
            self.assertEqual(run.call_args.args, (state,))
            self.assertTrue(run.call_args.kwargs["send"])
            self.assertEqual(run.call_args.kwargs["interval"], 60)
            self.assertEqual(self.configured(config, "watch", "--no-send", "--interval", "45", state_dir=override)[0], 0)
            self.assertEqual(run.call_args.args, (override,))
            self.assertFalse(run.call_args.kwargs["send"])
            self.assertEqual(run.call_args.kwargs["interval"], 45)
            self.assertTrue(load(config).send)

    def test_configured_client_id_is_used_only_for_a_new_login(self):
        with tempfile.TemporaryDirectory() as root, \
                patch.object(AuthSession, "prepare_login", return_value=LoginChallenge("url", 1)) as prepare, \
                patch.object(AuthSession, "complete_login", return_value=Snapshot(True, True, ())), \
                patch.object(AuthSession, "refresh", return_value=Snapshot(True, True, ())), \
                patch("quota_window.cli.sys.stdin.isatty", return_value=True), \
                patch("quota_window.cli.getpass.getpass", return_value="private-callback"):
            config = Path(root) / "config.json"
            create(config, state_dir=Path(root) / "state", client_id="configured-client")
            self.assertEqual(self.configured(config, "login")[0], 0)
            prepare.assert_called_once_with("configured-client", reauthorize=False)
            self.assertEqual(self.configured(config, "login", "--refresh")[0], 0)
            self.assertEqual(prepare.call_count, 1)

    def test_enabled_configuration_does_not_make_status_or_doctor_send(self):
        with tempfile.TemporaryDirectory() as root, \
                patch.object(AuthSession, "local_status", return_value={"state": "ready", "expired": False}), \
                patch.object(AuthSession, "query_quota", return_value=Snapshot(True, True, ())) as query, \
                patch.object(AuthSession, "refresh") as refresh, \
                patch.object(AuthSession, "wake_once") as wake:
            config = Path(root) / "config.json"
            create(config, state_dir=Path(root) / "state", send=True)
            self.assertEqual(self.configured(config, "status")[0], 0)
            self.assertEqual(self.configured(config, "doctor", "--online")[0], 0)
            self.assertEqual(query.call_count, 2)
            refresh.assert_not_called()
            wake.assert_not_called()

    def test_service_only_renders_without_opening_credentials(self):
        with tempfile.TemporaryDirectory() as root, patch("quota_window.cli.AuthSession") as auth:
            config = Path(root) / "config.json"
            create(config, state_dir=Path(root) / "state")
            code, out, err = self.configured(config, "service", "--no-send")
            self.assertEqual((code, err), (0, ""))
            self.assertIn("[Service]", out)
            self.assertIn('"--no-send"', out)
            auth.assert_not_called()

    def test_fatal_watch_errors_stop_service_and_temporary_reads_allow_restart(self):
        for error, expected in ((ScheduleError("uncertain attempt"), 2),
                                (TransportError("invalid JSON", retryable=False), 2),
                                (TransportError("unauthorized", status=401), 2),
                                (TransportError("unsupported", status=400), 2),
                                (TransportError("disconnected"), 1),
                                (TransportError("busy", status=429), 1),
                                (TransportError("route", status=530), 1)):
            with self.subTest(error=error), patch("quota_window.cli.run_watch", side_effect=error):
                code, out, err = self.invoke("unused", "watch", "--once")
                self.assertEqual(code, expected)
                self.assertEqual(out, "")
                self.assertNotIn("Traceback", err)

    def test_module_entry_point_runs_from_outside_repository(self):
        with tempfile.TemporaryDirectory() as root:
            env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"))
            result = subprocess.run([sys.executable, "-m", "quota_window", "--state-dir",
                                     str(Path(root) / "absent"), "status", "--offline"],
                                    cwd=root, env=env, capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)["authentication"]["state"], "not_logged_in")
