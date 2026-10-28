"""The installer validates first and only installs a manual, managed unit."""
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from ops import install_update_service as install


class UpdateServiceInstallTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.repo = self.directory / "checkout with spaces"
        (self.repo / "ops" / "systemd").mkdir(parents=True)
        (self.repo / "ops" / "update_bot.py").write_text("# test updater\n", encoding="utf-8")
        self.template = Path(__file__).resolve().parents[1] / "ops" / "systemd" / install.SERVICE_NAME
        (self.repo / "ops" / "systemd" / install.SERVICE_NAME).write_text(
            self.template.read_text(encoding="utf-8"), encoding="utf-8")
        self.config = self.directory / "update config.json"
        self.config.write_text(json.dumps({"repo": str(self.repo), "enabled": False}), encoding="utf-8")
        self.python = self.directory / "python3"
        self.python.write_text("", encoding="utf-8")
        self.units = self.directory / "units"
        self.units.mkdir()
        self.unit = self.units / install.SERVICE_NAME

    def test_install_checks_disabled_config_before_writing_and_never_starts_service(self):
        calls = []

        def run(args, *, check):
            self.assertTrue(check)
            if not calls:
                self.assertFalse(self.unit.exists())
            calls.append(args)

        with patch.object(install.subprocess, "run", side_effect=run):
            install.install_service(self.config, self.python, unit_dir=self.units)
        self.assertEqual(calls, [
            [str(self.python.resolve()), str(self.repo.resolve() / "ops" / "update_bot.py"),
             "--config", str(self.config.resolve()), "--check"],
            ["systemctl", "daemon-reload"],
        ])
        content = self.unit.read_text(encoding="utf-8")
        self.assertIn(install.MANAGED_MARKER, content)
        self.assertIn("Type=oneshot", content)
        self.assertIn("UMask=0077", content)
        self.assertIn("TimeoutStartSec=infinity", content)
        self.assertNotIn("[Install]", content)
        # Updater uses absolute paths and git -C; no single-path directive with
        # different quoting rules is needed alongside quoted ExecStart argv.
        self.assertNotIn("WorkingDirectory=", content)
        self.assertNotIn("@UPDATER@", content)
        self.assertNotIn(".timer", content)

    def test_failed_preflight_preserves_existing_unit_and_does_not_reload(self):
        original = install.MANAGED_MARKER + "\n# original version\n"
        self.unit.write_text(original, encoding="utf-8")
        with patch.object(install.subprocess, "run", side_effect=subprocess.CalledProcessError(1, ["check"])) as run:
            with self.assertRaises(subprocess.CalledProcessError):
                install.install_service(self.config, self.python, unit_dir=self.units)
        self.assertEqual(run.call_count, 1)
        self.assertEqual(self.unit.read_text(encoding="utf-8"), original)

    def test_unmanaged_unit_is_preserved_without_running_commands(self):
        self.unit.write_text("[Service]\nExecStart=/custom/updater\n", encoding="utf-8")
        with patch.object(install.subprocess, "run") as run:
            with self.assertRaisesRegex(install.InstallError, "unmanaged"):
                install.install_service(self.config, self.python, unit_dir=self.units)
        run.assert_not_called()
        self.assertIn("/custom/updater", self.unit.read_text(encoding="utf-8"))

    def test_symlink_unit_is_refused(self):
        with patch.object(Path, "is_symlink", return_value=True):
            with self.assertRaisesRegex(install.InstallError, "symlink"):
                install.write_unit(self.unit, "new content")
        self.assertFalse(self.unit.exists())

    def test_interrupted_replace_preserves_previous_unit_and_cleans_temp_file(self):
        original = install.MANAGED_MARKER + "\nold\n"
        self.unit.write_text(original, encoding="utf-8")
        with patch.object(install.os, "replace", side_effect=OSError("disk problem")):
            with self.assertRaises(OSError):
                install.write_unit(self.unit, install.MANAGED_MARKER + "\nnew\n")
        self.assertEqual(self.unit.read_text(encoding="utf-8"), original)
        self.assertEqual(list(self.units.iterdir()), [self.unit])

    def test_render_escapes_paths_as_arguments_without_shell_expansion(self):
        value = '/srv/my "book" %club $name'
        self.assertEqual(install.systemd_value(value, executable=True),
                         '"/srv/my \\"book\\" %%club $$name"')
        self.assertEqual(install.systemd_value(value),
                         '"/srv/my \\"book\\" %%club $name"')

    def test_render_rejects_directive_injection(self):
        for value in ("", "/srv/a\nExecStart=/bad", "/srv/a\r", "/srv/a\x00", "/srv/a\t"):
            with self.subTest(value=repr(value)):
                with self.assertRaises(install.InstallError):
                    install.systemd_value(value)

    def test_missing_updater_fails_before_install(self):
        (self.repo / "ops" / "update_bot.py").unlink()
        with patch.object(install.subprocess, "run") as run:
            with self.assertRaisesRegex(install.InstallError, "does not contain"):
                install.install_service(self.config, self.python, unit_dir=self.units)
        run.assert_not_called()
        self.assertFalse(self.unit.exists())

    def test_repository_path_must_be_absolute_and_existing(self):
        for repo in ("relative-checkout", str(self.directory / "absent")):
            self.config.write_text(json.dumps({"repo": repo}), encoding="utf-8")
            with self.subTest(repo=repo), self.assertRaisesRegex(install.InstallError, "absolute directory"):
                install.config_repo(self.config)

    def test_invalid_config_is_not_echoed(self):
        self.config.write_text('{"token":"private-value",', encoding="utf-8")
        with self.assertRaises(install.InstallError) as raised:
            install.config_repo(self.config)
        self.assertNotIn("private-value", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
