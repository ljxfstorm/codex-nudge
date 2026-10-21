import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
import uuid

from quota_window.config import ConfigError, create
from quota_window.service import ServiceError, render


NATIVE = os.environ.get("QUOTA_WINDOW_SYSTEMD_TESTS") == "1"


class ServiceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="quota-window-service-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config = self.root / 'settings $HOME % " \\.json'
        create(self.config, state_dir=self.root / "state", send=True)

    def test_unit_uses_absolute_paths_without_shell_or_working_directory(self):
        unit = render(self.config)
        self.assertIn('ExecStart=:"' + sys.executable + '" "-u" "-m" "quota_window"', unit)
        self.assertNotIn("WorkingDirectory=", unit)
        self.assertNotIn("/bin/sh", unit)
        self.assertIn('settings $HOME %% \\" \\\\.json', unit)
        self.assertIn("UMask=0077", unit)
        self.assertIn("RestartPreventExitStatus=2", unit)
        self.assertIn("RestartSec=300", unit)
        self.assertNotIn('"--send"', unit)
        self.assertFalse((self.root / "state").exists())

    def test_overrides_are_explicit_and_do_not_modify_config(self):
        original = self.config.read_bytes()
        self.assertIn('"--send"', render(self.config, send=True))
        self.assertIn('"--no-send"', render(self.config, send=False))
        self.assertEqual(self.config.read_bytes(), original)

    def test_invalid_runtime_paths_are_rejected(self):
        for source in (self.root / "missing", self.root / "bad:path", self.root / "bad\npath"):
            with self.subTest(source=source), self.assertRaises((ServiceError, ConfigError)):
                render(self.config, source=source)
        with self.assertRaises(ServiceError):
            render(self.config, python=self.root / "missing")
        for char in ("'", '"', "\\"):
            interpreter = self.root / ("python" + char + "probe")
            interpreter.symlink_to(sys.executable)
            with self.subTest(character=char), self.assertRaises(ServiceError):
                render(self.config, python=interpreter)

    @unittest.skipUnless(NATIVE, "set QUOTA_WINDOW_SYSTEMD_TESTS=1 for native systemd acceptance")
    def test_native_parser_accepts_rendered_unit(self):
        self.assertIsNotNone(shutil.which("systemd-analyze"))
        unit = self.root / "quota-window-parser-fixture.service"
        unit.write_text(render(self.config))
        result = subprocess.run(["systemd-analyze", "--user", "--generators=no", "--man=no", "verify", str(unit)],
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)

    @unittest.skipUnless(NATIVE, "requires a user manager; starts only a synthetic one-shot argument fixture")
    def test_native_manager_preserves_actual_arguments_and_source_environment(self):
        self.assertIsNotNone(shutil.which("systemctl"))
        source = self.root / 'source $HOME % " \\'
        package = source / "quota_window"
        package.mkdir(parents=True)
        (package / "__init__.py").write_text("")
        result_path = self.root / "actual.json"
        (package / "__main__.py").write_text(
            "import json, os, pathlib, sys\n"
            f"pathlib.Path({str(result_path)!r}).write_text(json.dumps("
            "{'argv': sys.argv[1:], 'python': sys.executable, 'source': os.environ.get('PYTHONPATH')}))\n")
        interpreter = self.root / 'python $HOME %'
        interpreter.symlink_to(sys.executable)
        name = "quota-window-argv-test-" + uuid.uuid4().hex + ".service"
        unit = self.root / name
        # Execute only the synthetic module above; there are no credentials,
        # HTTP calls, inference, or changes to any existing unit's configuration.
        text = render(self.config, source=source, python=interpreter, send=False)
        text = text.replace("Type=simple", "Type=oneshot").replace("Restart=on-failure", "Restart=no")
        unit.write_text(text + "\n")
        linked = False
        try:
            self.systemctl("--runtime", "link", str(unit))
            linked = True
            self.systemctl("start", name)
            actual = json.loads(result_path.read_text())
            self.assertEqual(actual["argv"], ["--config", str(self.config), "watch", "--no-send"])
            self.assertEqual(actual["source"], str(source))
            self.assertEqual(actual["python"], str(interpreter))
        finally:
            if linked:
                self.systemctl("stop", name, check=False)
                self.systemctl("--runtime", "disable", name)
                self.systemctl("reset-failed", name, check=False)
                remaining = self.systemctl("list-unit-files", name, "--no-legend", "--no-pager", check=False)
                # No matching unit files is exit 1 on the tested systemd.
                self.assertIn(remaining.returncode, (0, 1))
                self.assertEqual(remaining.stdout.strip(), "")
                self.assertEqual(remaining.stderr.strip(), "")

    def systemctl(self, *args, check=True):
        result = subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True, timeout=30)
        if check:
            self.assertEqual(result.returncode, 0, result.stderr)
        return result
