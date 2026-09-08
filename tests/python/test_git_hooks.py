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
        (self.repo / "initrd/debian/netinst/desktop").mkdir(parents=True)
        (self.repo / "initrd/debian/live").mkdir(parents=True)
        (self.repo / "initrd/custom").mkdir(parents=True)
        for relative in (
            "secrets.sh",
            ".githooks/pre-commit",
            ".githooks/pre-push",
            "scripts/check-secrets.py",
            "scripts/manage-secrets.py",
            "scripts/install-git-hooks.sh",
        ):
            destination = self.repo / relative
            shutil.copy2(ROOT / relative, destination)
            destination.chmod(0o755)

        self.config = self.repo / "configs/debian-usb.conf"
        self.preseed = self.repo / "initrd/debian/netinst/desktop/preseed.env"
        self.live = self.repo / "initrd/debian/live/live.env"
        self.service = self.repo / "initrd/custom/service.conf"
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
        access_key: str = "",
        root_password: str = "",
        service_secret: str = "",
        wifi: str = "",
        non_secret: str = "baseline",
        legacy: str = "",
    ) -> None:
        legacy_suffix = f" {legacy}=legacy-fixture" if legacy else ""
        self.config.write_text(
            'DEFAULT_LIVE_WIFI_PSK=""\n'
            f'PRESEED_ONE_ARGS_DEBIAN_DE="classes=test{legacy_suffix}"\n',
            encoding="utf-8",
        )
        lines = ["# Test preseed environment", ""]
        for key in PRESEED_SECRET_KEYS:
            if key == "PRESEED_ROOT_PASSWORD":
                value = root_password
            elif key == "PRESEED_WIFI_PASSPHRASE":
                value = wifi
            else:
                value = ""
            lines.append(f"{key}='{value}'")
        lines.extend(
            (
                f"PRESEED_CF_ACCESS_KEY='{access_key}'",
                f"INSTALL_LOCALE='{non_secret}'",
            )
        )
        self.preseed.write_text("\n".join(lines) + "\n", encoding="utf-8")
        self.preseed.chmod(0o644)
        self.live.write_text(f"LIVE_WIFI_PASSPHRASE='{wifi}'\n", encoding="utf-8")
        self.live.chmod(0o644)
        self.service.write_text(
            f'export SERVICE_CLIENT_SECRET = "{service_secret}"\n'
            "cache_size = 64\n",
            encoding="utf-8",
        )
        self.service.chmod(0o640)

    def _run_hook(self, hook_name: str) -> subprocess.CompletedProcess[str]:
        input_text = None
        args = [str(self.repo / ".githooks" / hook_name)]
        if hook_name == "pre-push":
            local_oid = self._git("rev-parse", "HEAD").stdout.strip()
            input_text = f"refs/heads/main {local_oid} refs/heads/main {self.base_oid}\n"
            args.extend(("origin", "test-remote"))
        return subprocess.run(
            args,
            cwd=self.repo,
            input=input_text,
            text=True,
            encoding="utf-8",
            capture_output=True,
            check=False,
        )

    def _run_scanner(
        self,
        *args: str,
        input_text: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(self.repo / "scripts/check-secrets.py"), *args],
            cwd=self.repo,
            input=input_text,
            text=True,
            encoding="utf-8",
            capture_output=True,
            check=False,
        )

    def _pre_push_update(self) -> str:
        local_oid = self._git("rev-parse", "HEAD").stdout.strip()
        return f"refs/heads/main {local_oid} refs/heads/main {self.base_oid}\n"

    def test_manual_scanner_accepts_safe_index_and_history(self) -> None:
        result = self._run_scanner("--pre-push", input_text=self._pre_push_update())

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, "")

    def test_manual_scanner_rejects_staged_netinst_and_live_secrets(self) -> None:
        root_value = "scanner-root-fixture"
        wifi_value = "scanner-wifi-fixture"
        self._write_secret_files(root_password=root_value, wifi=wifi_value)
        self._git(
            "add",
            "initrd/debian/netinst/desktop/preseed.env",
            "initrd/debian/live/live.env",
        )

        result = self._run_scanner("--index")

        self.assertEqual(result.returncode, 1)
        self.assertIn(
            "index initrd/debian/netinst/desktop/preseed.env: PRESEED_ROOT_PASSWORD",
            result.stderr,
        )
        self.assertIn(
            "index initrd/debian/live/live.env: LIVE_WIFI_PASSPHRASE",
            result.stderr,
        )
        self.assertNotIn(root_value, result.stdout + result.stderr)
        self.assertNotIn(wifi_value, result.stdout + result.stderr)

    def test_manual_scanner_rejects_secret_in_outgoing_history(self) -> None:
        secret_value = "scanner-history-fixture"
        self._write_secret_files(root_password=secret_value)
        self._git("add", "initrd/debian/netinst/desktop/preseed.env")
        self._git("commit", "--no-verify", "-m", "scanner secret snapshot")
        self._write_secret_files()
        self._git("add", "initrd/debian/netinst/desktop/preseed.env")
        self._git("commit", "--no-verify", "-m", "scanner clear snapshot")

        result = self._run_scanner("--pre-push", input_text=self._pre_push_update())

        self.assertEqual(result.returncode, 1)
        self.assertIn("outgoing ", result.stderr)
        self.assertIn(
            "initrd/debian/netinst/desktop/preseed.env: PRESEED_ROOT_PASSWORD",
            result.stderr,
        )
        self.assertNotIn(secret_value, result.stdout + result.stderr)

    def test_manual_scanner_rejects_empty_legacy_grub_key(self) -> None:
        self._write_secret_files(legacy="root_password")
        config_text = self.config.read_text(encoding="utf-8").replace(
            "root_password=legacy-fixture",
            "root_password=",
        )
        self.config.write_text(config_text, encoding="utf-8")
        self._git("add", "configs/debian-usb.conf")

        result = self._run_scanner("--index")

        self.assertEqual(result.returncode, 1)
        self.assertIn("index configs/debian-usb.conf: root_password", result.stderr)

    def test_safe_hooks_never_block(self) -> None:
        for hook_name in ("pre-commit", "pre-push"):
            with self.subTest(hook=hook_name):
                result = self._run_hook(hook_name)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("No non-empty managed secret values found", result.stdout)

    def test_pre_push_clears_arbitrary_env_and_conf_secrets_and_reports_keys(self) -> None:
        access_value = "access-fixture-123"
        password_value = "password-fixture-456"
        service_value = "service-fixture-789"
        wifi_value = "wifi fixture"
        self._write_secret_files(
            access_key=access_value,
            root_password=password_value,
            service_secret=service_value,
            wifi=wifi_value,
        )

        result = self._run_hook("pre-push")

        self.assertEqual(result.returncode, 0, result.stderr)
        preseed_text = self.preseed.read_text(encoding="utf-8")
        live_text = self.live.read_text(encoding="utf-8")
        self.assertIn("PRESEED_CF_ACCESS_KEY=''", preseed_text)
        self.assertIn("PRESEED_ROOT_PASSWORD=''", preseed_text)
        self.assertIn("PRESEED_WIFI_PASSPHRASE=''", preseed_text)
        self.assertNotIn("LIVE_WIFI_PASSPHRASE", preseed_text)
        self.assertIn("INSTALL_LOCALE='baseline'", preseed_text)
        self.assertIn("LIVE_WIFI_PASSPHRASE=''", live_text)
        self.assertNotIn("PRESEED_WIFI_PASSPHRASE", live_text)
        self.assertEqual(
            self.service.read_text(encoding="utf-8"),
            'export SERVICE_CLIENT_SECRET = ""\ncache_size = 64\n',
        )
        self.assertEqual(stat.S_IMODE(self.preseed.stat().st_mode), 0o644)
        self.assertEqual(stat.S_IMODE(self.live.stat().st_mode), 0o644)
        self.assertEqual(stat.S_IMODE(self.service.stat().st_mode), 0o640)
        for key in (
            "PRESEED_CF_ACCESS_KEY",
            "PRESEED_ROOT_PASSWORD",
            "PRESEED_WIFI_PASSPHRASE",
            "LIVE_WIFI_PASSPHRASE",
            "SERVICE_CLIENT_SECRET",
        ):
            self.assertIn(key, result.stdout)
        for value in (access_value, password_value, service_value, wifi_value):
            self.assertNotIn(value, result.stdout + result.stderr)

    def test_pre_commit_clears_index_without_staging_unrelated_worktree_edits(self) -> None:
        secret_value = "partial-stage-secret-fixture"
        self._write_secret_files(root_password=secret_value, non_secret="staged")
        self._git("add", "initrd/debian/netinst/desktop/preseed.env")
        worktree_text = self.preseed.read_text(encoding="utf-8").replace(
            "INSTALL_LOCALE='staged'",
            "INSTALL_LOCALE='unstaged'",
        )
        self.preseed.write_text(worktree_text, encoding="utf-8")

        result = self._run_hook("pre-commit")

        self.assertEqual(result.returncode, 0, result.stderr)
        index_text = self._git("show", ":initrd/debian/netinst/desktop/preseed.env").stdout
        worktree_text = self.preseed.read_text(encoding="utf-8")
        self.assertIn("PRESEED_ROOT_PASSWORD=''", index_text)
        self.assertIn("PRESEED_ROOT_PASSWORD=''", worktree_text)
        self.assertIn("INSTALL_LOCALE='staged'", index_text)
        self.assertIn("INSTALL_LOCALE='unstaged'", worktree_text)
        self.assertIn("index initrd/debian/netinst/desktop/preseed.env", result.stdout)
        self.assertNotIn(secret_value, result.stdout + result.stderr)

    def test_installed_pre_commit_allows_commit_and_commits_cleared_value(self) -> None:
        installer = self.repo / "scripts/install-git-hooks.sh"
        installed = subprocess.run(
            [str(installer)],
            cwd=self.repo,
            text=True,
            encoding="utf-8",
            capture_output=True,
            check=False,
        )
        self.assertEqual(installed.returncode, 0, installed.stderr)

        secret_value = "committed-service-fixture"
        self._write_secret_files(service_secret=secret_value)
        self._git("add", "initrd/custom/service.conf")
        committed = self._git("commit", "-m", "exercise installed pre-commit")

        snapshot = self._git("show", "HEAD:initrd/custom/service.conf").stdout
        self.assertIn('SERVICE_CLIENT_SECRET = ""', snapshot)
        self.assertNotIn(secret_value, snapshot)
        self.assertIn("SERVICE_CLIENT_SECRET", committed.stdout + committed.stderr)
        self.assertNotIn(secret_value, committed.stdout + committed.stderr)

    def test_pre_push_never_blocks_an_existing_secret_commit(self) -> None:
        secret_value = "outgoing-history-fixture"
        self._write_secret_files(access_key=secret_value)
        self._git("add", "initrd/debian/netinst/desktop/preseed.env")
        self._git("commit", "--no-verify", "-m", "secret fixture snapshot")

        result = self._run_hook("pre-push")

        self.assertEqual(result.returncode, 0, result.stderr)
        snapshot = self._git("show", "HEAD:initrd/debian/netinst/desktop/preseed.env").stdout
        self.assertIn(secret_value, snapshot)
        self.assertIn(
            "PRESEED_CF_ACCESS_KEY=''",
            self.preseed.read_text(encoding="utf-8"),
        )
        self.assertNotIn(secret_value, result.stdout + result.stderr)

    def test_hooks_continue_when_clearer_is_missing(self) -> None:
        (self.repo / "secrets.sh").unlink()

        for hook_name in ("pre-commit", "pre-push"):
            with self.subTest(hook=hook_name):
                result = self._run_hook(hook_name)
                self.assertEqual(result.returncode, 0)
                self.assertIn("continuing without blocking", result.stderr)

    def test_installer_copies_both_hooks_and_refuses_unrelated_hook(self) -> None:
        installer = self.repo / "scripts/install-git-hooks.sh"
        installed_paths = {
            hook_name: self.repo / ".git/hooks" / hook_name
            for hook_name in ("pre-commit", "pre-push")
        }

        first = subprocess.run(
            [str(installer)],
            cwd=self.repo,
            text=True,
            encoding="utf-8",
            capture_output=True,
            check=False,
        )
        self.assertEqual(first.returncode, 0, first.stderr)
        for hook_name, installed_hook in installed_paths.items():
            with self.subTest(hook=hook_name):
                self.assertEqual(
                    installed_hook.read_bytes(),
                    (self.repo / ".githooks" / hook_name).read_bytes(),
                )
                self.assertTrue(os.access(installed_hook, os.X_OK))

        installed_paths["pre-commit"].write_text(
            "#!/bin/sh\necho unrelated\n",
            encoding="utf-8",
        )
        before = {name: path.read_bytes() for name, path in installed_paths.items()}
        second = subprocess.run(
            [str(installer)],
            cwd=self.repo,
            text=True,
            encoding="utf-8",
            capture_output=True,
            check=False,
        )
        self.assertNotEqual(second.returncode, 0)
        self.assertIn("refusing to overwrite unrelated existing hook", second.stderr)
        self.assertEqual(
            {name: path.read_bytes() for name, path in installed_paths.items()},
            before,
        )


    def test_hook_removal_is_idempotent_and_preserves_unrelated_hooks(self) -> None:
        installer = str(self.repo / "scripts/install-git-hooks.sh")
        def run(action: str) -> subprocess.CompletedProcess[str]:
            return subprocess.run([installer, action], cwd=self.repo, text=True, encoding="utf-8", capture_output=True)
        self.assertEqual(run("--install").returncode, 0)
        unrelated = self.repo / ".git/hooks/post-commit"
        unrelated.write_text("#!/bin/sh\necho user-hook\n", encoding="utf-8")
        self.assertEqual(run("--remove").returncode, 0)
        for name in ("pre-commit", "pre-push"):
            self.assertFalse((self.repo / ".git/hooks" / name).exists())
            self.assertTrue((self.repo / ".githooks" / name).exists())
        self.assertEqual(run("--remove").returncode, 0)
        self.assertTrue(unrelated.exists())
        self.assertEqual(run("--install").returncode, 0)
        replaced = self.repo / ".git/hooks/pre-push"
        replaced.write_text("#!/bin/sh\necho user-replacement\n", encoding="utf-8")
        result = run("--remove")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("user-replacement", replaced.read_text(encoding="utf-8"))

    def test_make_install_and_nuke_invoke_hook_lifecycle(self) -> None:
        shutil.copy2(ROOT / "Makefile", self.repo / "Makefile")
        # Override only prerequisites in this disposable repository. The actual
        # Makefile recipes run against a no-op staged-host fixture, not the host.
        (self.repo / "fixture.mk").write_text("guard-non-root:\n\t@:\nbuild:\n\t@:\n", encoding="utf-8")
        (self.repo / "scripts/make-host.sh").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        for target, present in (("install", True), ("nuke", False)):
            result = subprocess.run(["make", "-f", "Makefile", "-f", "fixture.mk", target,
                "DESTDIR=" + str(self.repo / "stage")], cwd=self.repo, text=True, encoding="utf-8", capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            for name in ("pre-commit", "pre-push"):
                self.assertEqual((self.repo / ".git/hooks" / name).exists(), present)


if __name__ == "__main__":
    unittest.main()
