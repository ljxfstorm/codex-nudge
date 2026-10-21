import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from quota_window.storage import MAX_RECORD_BYTES, StateStore, StorageError


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.path = self.root / "state"

    def test_round_trip_and_private_permissions(self):
        with StateStore(self.path) as store:
            self.assertIsNone(store.read("credentials.json"))
            store.write("credentials.json", {"token": "synthetic-secret", "schema": 1})
            self.assertEqual(store.read("credentials.json")["schema"], 1)
        for path, mode in ((self.path, 0o700), (self.path / ".lock", 0o600),
                           (self.path / "credentials.json", 0o600)):
            self.assertEqual(path.stat().st_mode & 0o777, mode)

    def test_second_process_is_excluded_then_lock_is_released(self):
        code = ("import sys; from quota_window.storage import StateStore; "
                "s=StateStore(sys.argv[1]); s.__enter__(); s.__exit__()")
        with StateStore(self.path):
            result = subprocess.run([sys.executable, "-c", code, str(self.path)], capture_output=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn(b"Another operation", result.stderr)
        result = subprocess.run([sys.executable, "-c", code, str(self.path)], capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_rejected_directory_does_not_hold_a_lock(self):
        self.path.mkdir(mode=0o755)
        self.path.chmod(0o755)
        store = StateStore(self.path)
        with self.assertRaises(StorageError):
            store.__enter__()
        self.path.chmod(0o700)
        with store:
            store.write("state.json", {})

    def test_symlink_and_fifo_records_are_rejected(self):
        outside = self.root / "outside"
        outside.write_text("preserve")
        with StateStore(self.path) as store:
            record = self.path / "credentials.json"
            record.symlink_to(outside)
            with self.assertRaises(StorageError):
                store.read("credentials.json")
            with self.assertRaises(StorageError):
                store.write("credentials.json", {})
            record.unlink()
            os.mkfifo(record, 0o600)
            with self.assertRaises(StorageError):
                store.read("credentials.json")
        self.assertEqual(outside.read_text(), "preserve")

    def test_directory_and_lock_symlinks_are_rejected(self):
        target = self.root / "target"
        target.mkdir(mode=0o700)
        self.path.symlink_to(target, target_is_directory=True)
        with self.assertRaises(StorageError):
            with StateStore(self.path):
                pass
        self.path.unlink()
        self.path.mkdir(mode=0o700)
        (self.path / ".lock").symlink_to(target / "lock")
        with self.assertRaises(StorageError):
            with StateStore(self.path):
                pass
        self.assertFalse((target / "lock").exists())

    def test_failure_before_replace_preserves_old_credentials(self):
        with StateStore(self.path) as store:
            store.write("credentials.json", {"token": "old-synthetic"})
            with patch("quota_window.storage.os.replace", side_effect=OSError("secret-value")):
                with self.assertRaises(StorageError) as caught:
                    store.write("credentials.json", {"token": "new-synthetic"})
            self.assertNotIn("secret-value", str(caught.exception))
            self.assertEqual(store.read("credentials.json"), {"token": "old-synthetic"})
            self.assertFalse(list(self.path.glob(".pending-*")))

    def test_failure_after_replace_keeps_new_record_and_reports_uncertainty(self):
        with StateStore(self.path) as store:
            store.write("credentials.json", {"token": "old-synthetic"})
            original = os.fsync

            def fail_directory(descriptor):
                if descriptor == store._directory:
                    raise OSError("simulated directory sync failure")
                original(descriptor)

            with patch("quota_window.storage.os.fsync", side_effect=fail_directory):
                with self.assertRaisesRegex(StorageError, "uncertain"):
                    store.write("credentials.json", {"token": "new-synthetic"})
            self.assertEqual(store.read("credentials.json"), {"token": "new-synthetic"})

    def test_invalid_or_oversized_records_never_look_like_missing_state(self):
        with StateStore(self.path) as store:
            record = self.path / "state.json"
            for data in (b'{"secret":"DO_NOT_LOG",', b'[]', b'{"x":1,"x":2}', b'{"x":NaN}',
                         b"x" * (MAX_RECORD_BYTES + 1)):
                record.write_bytes(data)
                record.chmod(0o600)
                with self.assertRaises(StorageError) as caught:
                    store.read("state.json")
                self.assertNotIn("DO_NOT_LOG", str(caught.exception))

    def test_reject_public_record_and_path_escape(self):
        with StateStore(self.path) as store:
            store.write("state.json", {})
            (self.path / "state.json").chmod(0o644)
            with self.assertRaises(StorageError):
                store.read("state.json")
            for name in ("../outside.json", ".lock", "/state.json"):
                with self.assertRaises(StorageError):
                    store.write(name, {})


if __name__ == "__main__":
    unittest.main()
