import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from quota_window.config import ConfigError, create, load


class ConfigTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path = self.root / "settings.json"

    def write(self, value):
        self.path.write_text(json.dumps(value))
        self.path.chmod(0o600)

    def test_private_creation_defaults_and_round_trip(self):
        settings = create(self.path, state_dir=self.root / "state")
        self.assertEqual(load(self.path), settings)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertFalse(settings.send)
        self.assertEqual(settings.interval, 300)
        self.assertIsNone(settings.client_id)
        self.assertFalse(settings.state_dir.exists())
        self.assertEqual(list(self.root.iterdir()), [self.path])

    def test_explicit_opt_in_and_relative_paths_use_config_directory(self):
        self.write({"schema": 1, "state_dir": "state $HOME % spaces", "client_id": "synthetic-client",
                    "send": True, "interval": 60})
        settings = load(self.path)
        self.assertEqual(settings.state_dir, self.root / "state $HOME % spaces")
        self.assertTrue(settings.send)
        self.assertEqual(settings.client_id, "synthetic-client")

    def test_existing_files_and_symlinks_are_never_overwritten(self):
        create(self.path, state_dir=self.root / "state")
        old = self.path.read_bytes()
        with self.assertRaises(ConfigError):
            create(self.path, state_dir=self.root / "different", send=True)
        self.assertEqual(self.path.read_bytes(), old)
        link = self.root / "link.json"
        link.symlink_to(self.path)
        with self.assertRaises(ConfigError):
            create(link, state_dir=self.root / "different")
        with self.assertRaises(ConfigError):
            load(link)
        self.assertEqual(self.path.read_bytes(), old)

    def test_malformed_unknown_and_unsafe_settings_fail_closed(self):
        valid = {"schema": 1, "state_dir": "state"}
        for overrides in ({"schema": True}, {"schema": 2}, {"send": "false"}, {"send": 1},
                          {"interval": True}, {"interval": 1}, {"interval": 3601}, {"interval": float("nan")},
                          {"state_dir": ""}, {"state_dir": "path\ncommand"}, {"state_dir": []},
                          {"client_id": "bad\nclient"}, {"access_token": "PRIVATE_SECRET"}):
            self.write(dict(valid, **overrides))
            with self.subTest(overrides=overrides), self.assertRaises(ConfigError) as caught:
                load(self.path)
            self.assertNotIn("PRIVATE_SECRET", str(caught.exception))
        self.path.write_text('{"schema":1,"state_dir":"state","send":false,"send":true}')
        with self.assertRaises(ConfigError):
            load(self.path)

    def test_public_directory_fifo_and_oversized_files_are_rejected(self):
        self.write({"schema": 1, "state_dir": "state"})
        self.path.chmod(0o644)
        with self.assertRaises(ConfigError):
            load(self.path)
        with self.assertRaises(ConfigError):
            load(self.root)
        fifo = self.root / "pipe"
        os.mkfifo(fifo, 0o600)
        with self.assertRaises(ConfigError):
            load(fifo)
        self.path.chmod(0o600)
        self.path.write_bytes(b" " * 65537)
        with self.assertRaises(ConfigError):
            load(self.path)

    def test_failed_publication_never_leaves_partial_config(self):
        with patch("quota_window.config.os.link", side_effect=OSError("PRIVATE_DETAIL")), \
                self.assertRaises(ConfigError) as caught:
            create(self.path, state_dir=self.root / "state", send=True)
        self.assertNotIn("PRIVATE_DETAIL", str(caught.exception))
        self.assertFalse(self.path.exists())
        self.assertEqual(list(self.root.iterdir()), [])

    def test_unknown_home_and_oversized_configuration_fail_before_publication(self):
        with patch("quota_window.config.Path.expanduser", side_effect=RuntimeError("private detail")), \
                self.assertRaises(ConfigError) as caught:
            load(self.path)
        self.assertNotIn("private detail", str(caught.exception))
        with self.assertRaises(ConfigError):
            create(self.path, state_dir="state" * 14000)
        self.assertFalse(self.path.exists())

    def test_failure_after_publication_preserves_complete_result(self):
        original = os.fsync
        count = 0

        def fsync(fd):
            nonlocal count
            count += 1
            if count == 2:
                raise OSError("directory sync failed")
            return original(fd)

        with patch("quota_window.config.os.fsync", side_effect=fsync), self.assertRaises(ConfigError):
            create(self.path, state_dir=self.root / "state", send=True)
        self.assertTrue(load(self.path).send)
        with self.assertRaises(ConfigError):
            create(self.path, state_dir=self.root / "state", send=False)
