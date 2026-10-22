from contextlib import redirect_stdout
from io import StringIO
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from quota_window.cli import main
from quota_window.config import create, load
from quota_window.diagnostics import collect
from quota_window.storage import StateStore, StorageError, read_snapshot


class DiagnosticTests(unittest.TestCase):
    def test_runtime_health_reports_actual_mode_and_overdue_without_network(self):
        with tempfile.TemporaryDirectory() as root:
            directory = Path(root) / 'state'
            with StateStore(directory / 'watch') as store:
                store.write('health.json', {'schema': 1, 'version': '0.1.0', 'phase': 'retrying',
                    'automatic_requests_enabled': True, 'started_at': 90, 'updated_at': 100,
                    'last_successful_query_at': 95, 'next_action': 'quota', 'next_action_at': 300,
                    'read_retries': 2, 'last_error': {'kind': 'http', 'http_status': 503}})
                before = (directory / 'watch' / 'health.json').read_bytes()
                with patch('quota_window.diagnostics.time.time', return_value=340):
                    report = collect(directory)
                runtime = report['watcher']['runtime']
                self.assertFalse(report['configuration']['watch_send'])
                self.assertTrue(runtime['automatic_requests_enabled'])
                self.assertEqual(runtime['next_action_at'], 300)
                self.assertEqual(runtime['last_error']['http_status'], 503)
                self.assertFalse(runtime['overdue'])
                with patch('quota_window.diagnostics.time.time', return_value=361):
                    self.assertTrue(collect(directory)['watcher']['runtime']['overdue'])
                self.assertEqual(before, (directory / 'watch' / 'health.json').read_bytes())

    def test_runtime_health_filters_untrusted_fields_and_marks_stopped(self):
        with tempfile.TemporaryDirectory() as root:
            directory = Path(root) / 'state'
            with StateStore(directory / 'watch') as store:
                store.write('health.json', {'schema': 1, 'version': 'PRIVATE_VERSION', 'phase': 'stopped',
                    'automatic_requests_enabled': 'PRIVATE_MODE', 'updated_at': 100,
                    'last_successful_query_at': 10 ** 400, 'next_action': {'PRIVATE_ACTION': True},
                    'read_retries': True, 'stop_reason': 'PRIVATE_REASON',
                    'last_error': {'kind': 'PRIVATE_KIND', 'http_status': 'PRIVATE_STATUS',
                                   'message': 'PRIVATE_MESSAGE'}, 'token': 'PRIVATE_TOKEN'})
            report = collect(directory)
            self.assertNotIn('PRIVATE_', json.dumps(report))
            runtime = report['watcher']['runtime']
            self.assertFalse(runtime['overdue'])
            self.assertEqual(runtime['stop_reason'], 'invalid')
            self.assertEqual(runtime['last_error'], {'kind': 'invalid', 'http_status': None})
            for key in ('version', 'automatic_requests_enabled', 'read_retries', 'last_successful_query_at'):
                self.assertNotIn(key, runtime)

    def test_absent_state_is_offline_and_creates_nothing(self):
        with tempfile.TemporaryDirectory() as root, patch('quota_window.auth.request_json') as request:
            directory = Path(root) / 'absent'
            output = StringIO()
            with redirect_stdout(output):
                self.assertEqual(main(['--state-dir', str(directory), 'diagnostics']), 0)
            report = json.loads(output.getvalue())
            self.assertTrue(report['offline'])
            self.assertEqual(report['authentication']['record'], 'missing')
            self.assertEqual(report['watcher']['record'], 'missing')
            request.assert_not_called()
            self.assertFalse(directory.exists())

    def test_report_works_during_watch_lease_and_excludes_secrets_and_identifiers(self):
        with tempfile.TemporaryDirectory() as root:
            directory = Path(root) / 'private-account-path'
            config = Path(root) / 'settings.json'
            create(config, state_dir=directory, client_id='PRIVATE_CLIENT', send=True)
            with StateStore(directory) as auth:
                auth.write('credentials.json', {'schema': 1, 'phase': 'ready', 'expires_at': 1900000000,
                    'access_token': 'PRIVATE_ACCESS', 'refresh_token': 'PRIVATE_REFRESH', 'id_token': 'PRIVATE_ID',
                    'subject': 'PRIVATE_SUBJECT', 'account_id': 'PRIVATE_ACCOUNT', 'client_id': 'PRIVATE_CLIENT'})
                auth.write('login.json', {'schema': 1, 'phase': 'complete', 'state': 'PRIVATE_STATE',
                    'nonce': 'PRIVATE_NONCE', 'verifier': 'PRIVATE_VERIFIER'})
                with StateStore(directory / 'watch') as watch:
                    watch.write('schedule.json', {'schema': 1, 'binding': 'PRIVATE_BINDING',
                        'window': {'phase': 'armed', 'deadline': 1900000000},
                        'attempt': {'id': 'PRIVATE_ATTEMPT', 'phase': 'verified', 'started_at': 100,
                            'finished_at': 102, 'samples': [{'at': 107, 'reset': 18102}, {'at': 137, 'reset': 18102}],
                            'usage': {'input_tokens': 13, 'output_tokens': 6, 'total_tokens': 19},
                            'unknown': 'PRIVATE_REPLY'}})
                    paths = list(directory.rglob('*')) + [config]
                    before = {p: p.read_bytes() for p in paths if p.is_file()}
                    report = collect(directory, settings=load(config))
                    self.assertEqual({p: p.read_bytes() for p in before}, before)
            encoded = json.dumps(report)
            self.assertNotIn('PRIVATE_', encoded)
            self.assertNotIn(str(directory), encoded)
            self.assertNotIn('private-account-path', encoded)
            self.assertTrue(report['configuration']['watch_send'])
            self.assertEqual(report['watcher']['attempt_phase'], 'verified')
            self.assertEqual(report['watcher']['usage']['total_tokens'], 19)
            self.assertEqual(report['watcher']['verification_samples'], 2)

    def test_malformed_and_unexpected_fields_cannot_enter_the_report(self):
        with tempfile.TemporaryDirectory() as root:
            directory = Path(root) / 'state'
            with StateStore(directory) as auth:
                auth.write('credentials.json', {'schema': 1, 'phase': 'PRIVATE_PHASE', 'expires_at': True})
                auth.write('login.json', {'schema': 999, 'phase': 'PRIVATE_LOGIN'})
            with StateStore(directory / 'watch') as watch:
                watch.write('schedule.json', {'schema': 1, 'window': {'phase': ['PRIVATE_WINDOW'], 'deadline': False},
                    'attempt': {'phase': 'blocked', 'started_at': 'PRIVATE_TIME', 'samples': ['PRIVATE_SAMPLE'] * 3,
                        'usage': {'input_tokens': 13, 'output_tokens': 5, 'total_tokens': 999, 'text': 'PRIVATE_REPLY'},
                        'failure': {'kind': 'PRIVATE_KIND', 'http_status': True,
                                    'content_type': 'text/html; secret=PRIVATE_HEADER', 'message': 'PRIVATE_ERROR'}}})
            report = collect(directory)
            self.assertNotIn('PRIVATE_', json.dumps(report))
            self.assertEqual(report['authentication']['phase'], 'invalid')
            self.assertEqual(report['authentication']['login_record'], 'unsupported_or_invalid')
            self.assertTrue(report['watcher']['usage_invalid'])
            self.assertEqual(report['watcher']['failure'],
                             {'kind': 'invalid', 'http_status': None, 'content_type': 'text/html'})

    def test_unsafe_record_reports_fixed_error_without_opening_target(self):
        with tempfile.TemporaryDirectory() as root:
            directory = Path(root) / 'state'
            with StateStore(directory):
                pass
            outside = Path(root) / 'secret.json'
            outside.write_text('{"schema":1,"phase":"PRIVATE_SECRET"}')
            (directory / 'credentials.json').symlink_to(outside)
            report = collect(directory)
            self.assertEqual(report['authentication']['record'], 'unreadable_or_invalid')
            self.assertNotIn('PRIVATE_SECRET', json.dumps(report))
            self.assertNotIn(str(outside), json.dumps(report))

    def test_snapshot_retains_private_file_size_and_record_name_checks(self):
        with tempfile.TemporaryDirectory() as root:
            directory = Path(root) / 'state'
            with StateStore(directory) as store:
                store.write('sample.json', {'schema': 1})
                self.assertEqual(read_snapshot(directory, 'sample.json'), {'schema': 1})
                for name in ('../sample.json', '.lock', ''):
                    with self.assertRaises(StorageError):
                        read_snapshot(directory, name)
                path = directory / 'sample.json'
                path.chmod(0o644)
                with self.assertRaises(StorageError):
                    read_snapshot(directory, 'sample.json')
                path.chmod(0o600)
                path.write_text('x' * (256 * 1024 + 1))
                with self.assertRaises(StorageError):
                    read_snapshot(directory, 'sample.json')
                path.unlink()
                os.mkfifo(path, 0o600)
                with self.assertRaises(StorageError):
                    read_snapshot(directory, 'sample.json')
