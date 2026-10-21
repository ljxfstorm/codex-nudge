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
from quota_window.quota import Snapshot, Window
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

    def test_module_entry_point_runs_from_outside_repository(self):
        with tempfile.TemporaryDirectory() as root:
            env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"))
            result = subprocess.run([sys.executable, "-m", "quota_window", "--state-dir",
                                     str(Path(root) / "absent"), "status", "--offline"],
                                    cwd=root, env=env, capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)["authentication"]["state"], "not_logged_in")
