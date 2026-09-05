from __future__ import annotations

from pathlib import Path
import os
import shutil
import stat
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
PRESEED_SECRET_KEYS = (
    "PRESEED_WIFI_PASSPHRASE",
    "PRESEED_FRUUX_USERNAME",
    "PRESEED_FRUUX_PASSWORD",
    "PRESEED_PRIMARY_USERNAME",
    "PRESEED_PRIMARY_PASSWORD",
    "PRESEED_PRIMARY_GPG_PASSPHRASE",
    "PRESEED_ROOT_PASSWORD",
    "PRESEED_CROWDSEC_TOKEN",
    "PRESEED_TAILSCALE_TOKEN",
    "PRESEED_TELEGRAM_CHAT_ID",
    "PRESEED_TELEGRAM_API_KEY",
    "PRESEED_CF_APTLY_ACCESS_KEY",
    "PRESEED_CF_APTLY_SECRET_KEY",
    "PRESEED_OBS_USERNAME",
    "PRESEED_OBS_PASSWORD",
)


class GitSecretHookTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.repo = Path(self.temp_dir.name) / "repo"
        self.repo.mkdir()
        self._git("init", "-b", "main")
        self._git("config", "user.name", "Debian USB Tests")
        self._git("config", "user.email", "debian-usb-tests@example.invalid")

        (self.repo / ".githooks").mkdir()
        (self.repo / "scripts").mkdir()
        (self.repo / "configs").mkdir()
        (self.repo / "initrd/debian/netinst").mkdir(parents=True)
        (self.repo / "initrd/debian/live").mkdir(parents=True)
        for relative in (
            "secrets.sh",
            ".githooks/pre-push",
            "scripts/check-secrets.py",
            "scripts/manage-secrets.py",
            "scripts/install-git-hooks.sh",
        ):
            destination = self.repo / relative
            shutil.copy2(ROOT / relative, destination)
            destination.chmod(0o755)

        self.config = self.repo / "configs/debian-usb.conf"
        self.preseed = self.repo / "initrd/debian/netinst/preseed.env"
        self.live = self.repo / "initrd/debian/live/live.env"
        self._write_secret_files()
        self._git("add", ".")
        self._git("commit", "-m", "safe baseline")
        self.base_oid = self._git("rev-parse", "HEAD").stdout.strip()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _git(self, *args: str) -> subprocess.CompletedProcess[str]:
        completed = subprocess.run(
            ["git", *args],
            cwd=self.repo,
            text=True,
            encoding="utf-8",
            capture_output=True,
            check=False,
        )
        if completed.returncode != 0:
            self.fail(f"git command failed: {' '.join(args)}: {completed.stderr}")
        return completed

    def _write_secret_files(
        self,
        *,
        wifi: str = "",
        preseed_wifi: str = "",
        root_password: str = "",
        legacy: str = "",
    ) -> None:
        legacy_suffix = f" {legacy}=legacy-fixture" if legacy else ""
        self.config.write_text(
            f'DEFAULT_LIVE_WIFI_PSK="{wifi}"\n'
            f'PRESEED_ONE_ARGS_DEBIAN="classes=test{legacy_suffix}"\n',
            encoding="utf-8",
        )
        lines = ["# Test preseed environment", ""]
        for key in PRESEED_SECRET_KEYS:
            if key == "PRESEED_ROOT_PASSWORD":
                value = root_password
            elif key == "PRESEED_WIFI_PASSPHRASE":
                value = preseed_wifi
            else:
                value = ""
            lines.append(f"{key}='{value}'")
        self.preseed.write_text("\n".join(lines) + "\n", encoding="utf-8")
        self.preseed.chmod(0o600)
        self.live.write_text(f"PRESEED_WIFI_PASSPHRASE='{preseed_wifi}'\n", encoding="utf-8")
        self.live.chmod(0o600)

    def _run_hook(self, remote_oid: str, *, local_oid: str | None = None) -> subprocess.CompletedProcess[str]:
        if local_oid is None:
            local_oid = self._git("rev-parse", "HEAD").stdout.strip()
        update = f"refs/heads/main {local_oid} refs/heads/main {remote_oid}\n"
        return subprocess.run(
            [str(self.repo / ".githooks/pre-push"), "origin", "test-remote"],
            cwd=self.repo,
            input=update,
            text=True,
            encoding="utf-8",
            capture_output=True,
            check=False,
        )

    def test_safe_history_and_index_pass(self) -> None:
        result = self._run_hook("0" * 40)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")

    def test_hook_clears_unstaged_worktree_values_before_scan(self) -> None:
        self._write_secret_files(
            wifi="worktree-wifi",
            preseed_wifi="worktree-preseed-wifi",
            root_password="worktree-root",
        )

        result = self._run_hook(self.base_oid)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("DEFAULT_LIVE_WIFI_PSK", self.config.read_text(encoding="utf-8"))
        self.assertIn("PRESEED_ROOT_PASSWORD=''", self.preseed.read_text(encoding="utf-8"))
        self.assertIn("PRESEED_WIFI_PASSPHRASE=''", self.preseed.read_text(encoding="utf-8"))
        self.assertIn("PRESEED_WIFI_PASSPHRASE=''", self.live.read_text(encoding="utf-8"))
        self.assertEqual(stat.S_IMODE(self.preseed.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.live.stat().st_mode), 0o600)

    def test_hook_rejects_staged_secret_after_clearing_worktree(self) -> None:
        secret_value = "staged-root-fixture"
        self._write_secret_files(root_password=secret_value)
        self._git("add", "initrd/debian/netinst/preseed.env")

        result = self._run_hook(self.base_oid)

        self.assertEqual(result.returncode, 1)
        self.assertIn("index initrd/debian/netinst/preseed.env: PRESEED_ROOT_PASSWORD", result.stderr)
        self.assertNotIn(secret_value, result.stdout + result.stderr)
        self.assertIn("PRESEED_ROOT_PASSWORD=''", self.preseed.read_text(encoding="utf-8"))

    def test_hook_rejects_staged_live_wifi_secret_without_printing_value(self) -> None:
        secret_value = "staged-live-wifi-fixture"
        self._write_secret_files(preseed_wifi=secret_value)
        self._git("add", "initrd/debian/live/live.env")

        result = self._run_hook(self.base_oid)

        self.assertEqual(result.returncode, 1)
        self.assertIn("index initrd/debian/live/live.env: PRESEED_WIFI_PASSPHRASE", result.stderr)
        self.assertNotIn(secret_value, result.stdout + result.stderr)
        self.assertIn("PRESEED_WIFI_PASSPHRASE=''", self.live.read_text(encoding="utf-8"))

    def test_hook_rejects_secret_in_any_outgoing_commit_snapshot(self) -> None:
        secret_value = "history-root-fixture"
        self._write_secret_files(root_password=secret_value)
        self._git("add", "initrd/debian/netinst/preseed.env")
        self._git("commit", "-m", "secret snapshot")
        self._write_secret_files()
        self._git("add", "initrd/debian/netinst/preseed.env")
        self._git("commit", "-m", "clear snapshot")

        result = self._run_hook(self.base_oid)

        self.assertEqual(result.returncode, 1)
        self.assertIn("initrd/debian/netinst/preseed.env: PRESEED_ROOT_PASSWORD", result.stderr)
        self.assertIn("outgoing ", result.stderr)
        self.assertNotIn(secret_value, result.stdout + result.stderr)

    def test_hook_rejects_legacy_grub_key_even_when_value_is_empty(self) -> None:
        self._write_secret_files(legacy="root_password")
        # Make the legacy assignment empty but retain the forbidden argument name.
        config_text = self.config.read_text(encoding="utf-8").replace(
            "root_password=legacy-fixture",
            "root_password=",
        )
        self.config.write_text(config_text, encoding="utf-8")
        self._git("add", "configs/debian-usb.conf")

        result = self._run_hook(self.base_oid)

        self.assertEqual(result.returncode, 1)
        self.assertIn("index configs/debian-usb.conf: root_password", result.stderr)

    def test_installer_copies_managed_hook_and_refuses_unrelated_hook(self) -> None:
        installer = self.repo / "scripts/install-git-hooks.sh"
        installed_hook = self.repo / ".git/hooks/pre-push"

        first = subprocess.run([str(installer)], cwd=self.repo, text=True, encoding="utf-8", capture_output=True, check=False)
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(installed_hook.read_bytes(), (self.repo / ".githooks/pre-push").read_bytes())
        self.assertTrue(os.access(installed_hook, os.X_OK))

        installed_hook.write_text("#!/bin/sh\necho unrelated\n", encoding="utf-8")
        before = installed_hook.read_bytes()
        second = subprocess.run([str(installer)], cwd=self.repo, text=True, encoding="utf-8", capture_output=True, check=False)
        self.assertNotEqual(second.returncode, 0)
        self.assertIn("refusing to overwrite unrelated existing hook", second.stderr)
        self.assertEqual(installed_hook.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
