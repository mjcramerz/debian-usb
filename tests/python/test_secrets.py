from __future__ import annotations

from pathlib import Path
import re
import runpy
import shutil
import stat
import subprocess
import tempfile
import unittest
from unittest import mock
import warnings


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "secrets.sh"
HELPER = ROOT / "scripts/manage-secrets.py"
MANAGE_SECRETS = runpy.run_path(str(HELPER))
RETIRED_CONFIG_SECRET_KEYS = ("DEFAULT_LIVE_WIFI_PSK",)
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
LIVE_SECRET_KEYS = ("LIVE_WIFI_PASSPHRASE",)
LEGACY_GRUB_SECRET_KEYS = (
    "fruux_username",
    "fruux_password",
    "primary_user",
    "primary_password",
    "primary_gpg_passphrase",
    "root_password",
    "crowdsec_token",
    "tailscale_authkey",
    "telegram_chat_id",
    "telegram_api_key",
    "cf_r2_access_key",
    "cf_r2_secret_key",
    "obs_username",
    "obs_password",
)
CANONICAL_SECRET_KEYS = PRESEED_SECRET_KEYS + LIVE_SECRET_KEYS


class SecretsScriptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.repo = Path(self.temp_dir.name) / "repo"
        config_dir = self.repo / "configs"
        example_dir = self.repo / "examples"
        preseed_dir = self.repo / "initrd/debian/netinst/desktop"
        live_dir = self.repo / "initrd/debian/live"
        config_dir.mkdir(parents=True)
        example_dir.mkdir(parents=True)
        preseed_dir.mkdir(parents=True)
        live_dir.mkdir(parents=True)
        self.script = self.repo / "secrets.sh"
        shutil.copy2(SCRIPT, self.script)
        self.script.chmod(0o755)
        helper_dir = self.repo / "scripts"
        helper_dir.mkdir()
        helper = helper_dir / "manage-secrets.py"
        shutil.copy2(HELPER, helper)
        helper.chmod(0o755)

        self.config = config_dir / "debian-usb.conf"
        self.original_config = (
            "# preserved comment\n"
            'DEFAULT_LIVE_WIFI_PSK="old-wifi"\n'
            'PRESEED_ONE_ARGS_DEBIAN_DE="auto=true  fruux_username=old-fruux-user fruux_password=old-one\t'
            "primary_user=old-primary-user primary_password=old-two primary_gpg_passphrase=old-gpg-pass "
            "root_password=old-three crowdsec_token=old-four "
            "tailscale_authkey=old-five telegram_chat_id=old-six telegram_api_key=old-seven "
            "cf_r2_access_key=old-eight cf_r2_secret_key=old-nine cf_r2_gpg_key=preserved-gpg-key "
            'obs_username=old-eleven obs_password=old-twelve"\n'
            "UNRELATED=value\n"
        )
        self.config.write_text(self.original_config, encoding="utf-8")
        self.config.chmod(0o640)

        self.preseed = preseed_dir / "preseed.env"
        preseed_lines = ["# preserved preseed comment", ""]
        for key in PRESEED_SECRET_KEYS:
            preseed_lines.extend((f"# Former field for {key}", f"{key}='old-{key.lower()}'", ""))
        self.original_preseed = "\n".join(preseed_lines).rstrip() + "\n"
        self.preseed.write_text(self.original_preseed, encoding="utf-8")
        self.preseed.chmod(0o644)

        self.live = live_dir / "live.env"
        self.original_live = (
            "# preserved live comment\n"
            "LIVE_WIFI_PASSPHRASE='old-live-wifi'\n"
        )
        self.live.write_text(self.original_live, encoding="utf-8")
        self.live.chmod(0o644)

        self.auxiliary_config_original = (
            "# auxiliary config\n"
            'DEFAULT_LIVE_WIFI_PSK="copy-wifi"\n'
            'PRESEED_ARGS="root_password=copy-root telegram_api_key=copy-telegram"\n'
            "UNRELATED_COPY=value\n"
        )
        self.auxiliary_configs = (
            example_dir / "debian-usb.conf",
            config_dir / "debian-usb.conf.bak",
            config_dir / "debian-usb.conf.example",
            config_dir / "debian-usb.conf.snapshot",
        )
        for path in self.auxiliary_configs:
            path.write_text(self.auxiliary_config_original, encoding="utf-8")
            path.chmod(0o640)

        self.auxiliary_env_originals = {
            example_dir / "preseed.env": (
                "PRESEED_ROOT_PASSWORD='copy-root'\n"
                "PRESEED_WIFI_PASSPHRASE='copy-netinst-wifi'\n"
            ),
            preseed_dir / "preseed.env.bak": (
                "PRESEED_ROOT_PASSWORD='copy-root'\n"
                "PRESEED_WIFI_PASSPHRASE='copy-netinst-wifi'\n"
            ),
            live_dir / "live.env.snapshot": "LIVE_WIFI_PASSPHRASE='copy-live-wifi'\n",
        }
        self.auxiliary_envs = tuple(self.auxiliary_env_originals)
        for path, original in self.auxiliary_env_originals.items():
            path.write_text(original, encoding="utf-8")
            path.chmod(0o644)

        self.untargeted_example = example_dir / "other.conf"
        self.untargeted_original = "root_password=leave-this-file-alone\n"
        self.untargeted_example.write_text(self.untargeted_original, encoding="utf-8")

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def run_script(self, *args: str, input_text: str | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(self.script), *args],
            cwd=self.temp_dir.name,
            input=input_text,
            text=True,
            encoding="utf-8",
            capture_output=True,
            check=False,
        )

    @staticmethod
    def assert_no_legacy_grub_tokens(text: str) -> None:
        for key in LEGACY_GRUB_SECRET_KEYS:
            if re.search(rf"(?<![A-Za-z0-9_]){re.escape(key)}=", text):
                raise AssertionError(f"legacy GRUB key remains: {key}")

    def assert_canonical_cleared(self) -> None:
        config_text = self.config.read_text(encoding="utf-8")
        self.assertNotIn("DEFAULT_LIVE_WIFI_PSK", config_text)
        self.assert_no_legacy_grub_tokens(config_text)
        preseed_text = self.preseed.read_text(encoding="utf-8")
        for key in PRESEED_SECRET_KEYS:
            self.assertEqual(len(re.findall(rf"(?m)^{re.escape(key)}=''$", preseed_text)), 1)
        live_text = self.live.read_text(encoding="utf-8")
        for key in LIVE_SECRET_KEYS:
            self.assertEqual(len(re.findall(rf"(?m)^{re.escape(key)}=''$", live_text)), 1)
        self.assertNotIn("PRESEED_WIFI_PASSPHRASE", live_text)
        self.assertEqual(stat.S_IMODE(self.config.stat().st_mode), 0o640)
        self.assertEqual(stat.S_IMODE(self.preseed.stat().st_mode), 0o644)
        self.assertEqual(stat.S_IMODE(self.live.stat().st_mode), 0o644)

    def assert_auxiliary_files_cleared(self) -> None:
        for path in self.auxiliary_configs:
            with self.subTest(path=path.name):
                text = path.read_text(encoding="utf-8")
                self.assertNotIn("DEFAULT_LIVE_WIFI_PSK", text)
                self.assert_no_legacy_grub_tokens(text)
                self.assertIn("UNRELATED_COPY=value", text)
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o640)
        for path in self.auxiliary_envs:
            with self.subTest(path=path.name):
                expected = (
                    "LIVE_WIFI_PASSPHRASE=''\n"
                    if path.name == "live.env.snapshot"
                    else "PRESEED_ROOT_PASSWORD=''\nPRESEED_WIFI_PASSPHRASE=''\n"
                )
                self.assertEqual(path.read_text(encoding="utf-8"), expected)
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o644)
        self.assertEqual(self.untargeted_example.read_text(encoding="utf-8"), self.untargeted_original)

    def test_clear_empties_active_fields_removes_legacy_grub_tokens_and_preserves_modes(self) -> None:
        result = self.run_script("--clear")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_canonical_cleared()
        self.assertIn("cf_r2_gpg_key=preserved-gpg-key", self.config.read_text(encoding="utf-8"))
        self.assert_auxiliary_files_cleared()
        self.assertNotIn("old-one", result.stdout + result.stderr)

    def test_hidden_tty_input_suppresses_only_getpass_encoding_warning(self) -> None:
        class TTYInput:
            @staticmethod
            def isatty() -> bool:
                return True

            @staticmethod
            def readline() -> str:
                raise AssertionError("hidden terminal input must use getpass")

        getpass_module = MANAGE_SECRETS["getpass"]

        def fake_getpass(prompt: str) -> str:
            self.assertEqual(prompt, "PRESEED_WIFI_PASSPHRASE: ")
            warnings.warn("stdlib terminal encoding", EncodingWarning)
            warnings.warn("terminal echo protection unavailable", getpass_module.GetPassWarning)
            return "synthetic-hidden-value"

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            with mock.patch.object(getpass_module, "getpass", side_effect=fake_getpass):
                value = MANAGE_SECRETS["_read_value"](
                    "PRESEED_WIFI_PASSPHRASE", TTYInput()
                )
            warnings.warn("outside hidden-input scope", EncodingWarning)

        self.assertEqual(value, "synthetic-hidden-value")
        self.assertEqual(
            [warning.category for warning in caught],
            [getpass_module.GetPassWarning, EncodingWarning],
        )

    def test_set_keeps_netinst_and_live_wifi_passphrases_separate(self) -> None:
        values = {
            "PRESEED_WIFI_PASSPHRASE": "Netinst WiFi Value",
            "PRESEED_FRUUX_USERNAME": "fruux-user",
            "PRESEED_FRUUX_PASSWORD": "fruux=value",
            "PRESEED_PRIMARY_USERNAME": "primary-user",
            "PRESEED_PRIMARY_PASSWORD": "primary&value",
            "PRESEED_PRIMARY_GPG_PASSPHRASE": "gpg-passphrase",
            "PRESEED_ROOT_PASSWORD": r"root\value",
            "PRESEED_CROWDSEC_TOKEN": "crowd$token",
            "PRESEED_TAILSCALE_TOKEN": "ts/auth:key",
            "PRESEED_TELEGRAM_CHAT_ID": "-123456789",
            "PRESEED_TELEGRAM_API_KEY": "bot:key",
            "PRESEED_CF_APTLY_ACCESS_KEY": "R2ACCESS",
            "PRESEED_CF_APTLY_SECRET_KEY": "R2SECRET==",
            "PRESEED_OBS_USERNAME": "observer@example",
            "PRESEED_OBS_PASSWORD": "obs%pass",
            "LIVE_WIFI_PASSPHRASE": "Live WiFi Value",
        }
        input_text = "".join(f"{values[key]}\n" for key in CANONICAL_SECRET_KEYS)

        result = self.run_script("--set", input_text=input_text)

        self.assertEqual(result.returncode, 0, result.stderr)
        config_text = self.config.read_text(encoding="utf-8")
        preseed_text = self.preseed.read_text(encoding="utf-8")
        live_text = self.live.read_text(encoding="utf-8")
        self.assertNotIn("DEFAULT_LIVE_WIFI_PSK", config_text)
        self.assert_no_legacy_grub_tokens(config_text)
        for key in PRESEED_SECRET_KEYS:
            self.assertIn(f"{key}='{values[key]}'", preseed_text)
            self.assertNotIn(values[key], config_text)
        self.assertNotIn("LIVE_WIFI_PASSPHRASE", preseed_text)
        self.assertNotIn(values["LIVE_WIFI_PASSPHRASE"], preseed_text)
        self.assertIn(f"LIVE_WIFI_PASSPHRASE='{values['LIVE_WIFI_PASSPHRASE']}'", live_text)
        self.assertNotIn("PRESEED_WIFI_PASSPHRASE", live_text)
        self.assertNotIn(values["PRESEED_WIFI_PASSPHRASE"], live_text)
        self.assertNotIn(values["PRESEED_ROOT_PASSWORD"], live_text)
        self.assertEqual(stat.S_IMODE(self.preseed.stat().st_mode), 0o644)
        self.assertEqual(stat.S_IMODE(self.live.stat().st_mode), 0o644)
        self.assertEqual(result.stderr.count("PRESEED_WIFI_PASSPHRASE: "), 1)
        self.assertEqual(result.stderr.count("LIVE_WIFI_PASSPHRASE: "), 1)
        for value in values.values():
            self.assertNotIn(value, result.stdout + result.stderr)
        self.assert_auxiliary_files_cleared()

    def test_missing_active_envs_and_optional_copies_do_not_fail(self) -> None:
        self.preseed.unlink()
        self.live.unlink()
        for path in (*self.auxiliary_configs, *self.auxiliary_envs, self.untargeted_example):
            path.unlink()
        (self.repo / "examples").rmdir()

        result = self.run_script("--clear")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("DEFAULT_LIVE_WIFI_PSK", self.config.read_text(encoding="utf-8"))
        self.assertFalse(self.preseed.exists())
        self.assertFalse(self.live.exists())

    def test_missing_assignment_is_upserted_without_fixed_entry_validation(self) -> None:
        text = self.preseed.read_text(encoding="utf-8")
        text = re.sub(r"(?m)^PRESEED_ROOT_PASSWORD=.*\n", "", text)
        self.preseed.write_text(text, encoding="utf-8")

        result = self.run_script("--clear")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.preseed.read_text(encoding="utf-8").count("PRESEED_ROOT_PASSWORD=''"), 1)

    def test_set_rejects_whitespace_without_changing_active_or_optional_files(self) -> None:
        paths = (self.config, self.preseed, self.live, *self.auxiliary_configs, *self.auxiliary_envs)
        before = [path.read_bytes() for path in paths]

        result = self.run_script(
            "--set",
            input_text="valid shared wifi passphrase\ncontains whitespace\n",
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("must not contain whitespace", result.stderr)
        self.assertEqual([path.read_bytes() for path in paths], before)

    def test_set_rejects_quotes_without_changing_active_files(self) -> None:
        for value in ('contains"quote', "contains'quote"):
            with self.subTest(value=value):
                result = self.run_script("--set", input_text=f"{value}\n")
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("must not contain quote characters", result.stderr)
                self.assertEqual(self.config.read_text(encoding="utf-8"), self.original_config)
                self.assertEqual(self.preseed.read_text(encoding="utf-8"), self.original_preseed)
                self.assertEqual(self.live.read_text(encoding="utf-8"), self.original_live)

    def test_clear_dry_run_leaves_all_files_unchanged(self) -> None:
        paths = (self.config, self.preseed, self.live, *self.auxiliary_configs, *self.auxiliary_envs)
        before = [path.read_bytes() for path in paths]

        result = self.run_script("--clear", "--dry-run")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("no changes written", result.stdout)
        self.assertEqual([path.read_bytes() for path in paths], before)

    def test_clear_copies_leaves_active_files_unchanged(self) -> None:
        result = self.run_script("--clear-copies")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.config.read_text(encoding="utf-8"), self.original_config)
        self.assertEqual(self.preseed.read_text(encoding="utf-8"), self.original_preseed)
        self.assertEqual(self.live.read_text(encoding="utf-8"), self.original_live)
        self.assert_auxiliary_files_cleared()

    def test_clear_initrd_clears_arbitrary_env_and_conf_secret_assignments(self) -> None:
        custom_dir = self.repo / "initrd/custom/netboot"
        custom_dir.mkdir(parents=True)
        access_value = "custom-access-fixture"
        token_value = "custom-token-fixture"
        env_path = custom_dir / "credentials.env.local"
        env_path.write_text(
            f"PRESEED_CF_ACCESS_KEY='{access_value}'\n"
            "OPAQUE_KEY='opaque-key-fixture'\n"
            "DEPLOYMENT_REGION='eu-north-1'\n",
            encoding="utf-8",
        )
        conf_path = custom_dir / "service.conf.backup"
        conf_path.write_text(
            f"api_token = {token_value} # retained comment\n"
            "PUBLIC_KEY_PASSWORD = 'public-key-password-fixture'\n"
            "PUBLIC_GPG_KEY = 'public-fixture'\n",
            encoding="utf-8",
        )
        ignored_path = custom_dir / "service.ini"
        ignored_original = "api_token=ignored-token-fixture\n"
        ignored_path.write_text(ignored_original, encoding="utf-8")
        config_before = self.config.read_bytes()

        result = self.run_script("--clear-initrd")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            env_path.read_text(encoding="utf-8"),
            "PRESEED_CF_ACCESS_KEY=''\nOPAQUE_KEY=''\nDEPLOYMENT_REGION='eu-north-1'\n",
        )
        self.assertEqual(
            conf_path.read_text(encoding="utf-8"),
            "api_token = '' # retained comment\n"
            "PUBLIC_KEY_PASSWORD = ''\n"
            "PUBLIC_GPG_KEY = 'public-fixture'\n",
        )
        self.assertEqual(ignored_path.read_text(encoding="utf-8"), ignored_original)
        self.assertEqual(self.config.read_bytes(), config_before)
        self.assertIn("PRESEED_CF_ACCESS_KEY", result.stdout)
        self.assertIn("api_token", result.stdout)
        self.assertIn("OPAQUE_KEY", result.stdout)
        self.assertIn("initrd/custom/netboot/credentials.env.local", result.stdout)
        self.assertIn("initrd/custom/netboot/service.conf.backup", result.stdout)
        self.assertNotIn(access_value, result.stdout + result.stderr)
        self.assertNotIn(token_value, result.stdout + result.stderr)
        self.assertNotIn("opaque-key-fixture", result.stdout + result.stderr)
        self.assertNotIn("public-key-password-fixture", result.stdout + result.stderr)

    def test_optional_backup_symlink_is_ignored_without_following_or_failure(self) -> None:
        outside = Path(self.temp_dir.name) / "outside.conf"
        outside.write_text("root_password=outside-secret\n", encoding="utf-8")
        symlink = self.repo / "configs/debian-usb.conf.link"
        symlink.symlink_to(outside)

        result = self.run_script("--clear-copies")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(outside.read_text(encoding="utf-8"), "root_password=outside-secret\n")
        self.assert_auxiliary_files_cleared()


if __name__ == "__main__":
    unittest.main()

class KaliLiveSecretTargetsTests(unittest.TestCase):
    def test_active_targets_include_both_live_families_without_installer_secret_aliases(self):
        root = Path('/fixture')
        targets = dict(MANAGE_SECRETS['_active_secret_targets'](root))
        for family in ('debian', 'kali'):
            self.assertEqual(targets[root / f'initrd/{family}/live/live.env'], ('LIVE_WIFI_PASSPHRASE',))
