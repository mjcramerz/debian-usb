import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from debian_usb.config import (
    LEGACY_SECRET_KERNEL_ARG_NAMES,
    load_config,
    load_template_config,
    profile_payload_labels,
    runtime_config,
    save_config,
    update_default_boot_policy,
    update_default_forensics_kernel_extras,
    update_default_installer_kernel_extras,
    update_default_installer_policy,
    update_default_live_kernel_extras,
    update_default_live_mem_gib,
    update_default_live_toram,
    update_default_persistence_size,
    update_profile_forensics_kernel_extras,
    update_profile_live_kernel_extras,
    update_profile_preseed_url,
)
from debian_usb.devices import list_local_isos


class ConfigTests(unittest.TestCase):
    def test_rejects_legacy_secret_kernel_arguments(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "debian-usb.conf"
            for legacy_name in sorted(LEGACY_SECRET_KERNEL_ARG_NAMES):
                with self.subTest(legacy_name=legacy_name):
                    with self.assertRaisesRegex(
                        ValueError,
                        rf"PRESEED_ONE_ARGS_DEBIAN_DE contains forbidden legacy secret kernel argument.*{legacy_name}",
                    ):
                        save_config(
                            str(config_path),
                            {"PRESEED_ONE_ARGS_DEBIAN_DE": f"classes=test {legacy_name}=fixture-value"},
                        )

    def test_update_default_persistence_size_rewrites_config(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "debian-usb.conf"
            save_config(str(config_path), {})
            update_default_persistence_size(str(config_path), 16)
            loaded = load_config(str(config_path))
            self.assertEqual(loaded["DEFAULT_PERSISTENCE_SIZE_GIB"], "16")

    def test_runtime_config_contains_expected_keys(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "debian-usb.conf"
            save_config(str(config_path), {})
            loaded = load_config(str(config_path))
            payload = runtime_config(str(config_path))
            self.assertEqual(payload["app_name"], "debian-usb")
            self.assertEqual(payload["default_persistence_size_gib"], 8)
            self.assertEqual(payload["default_boot_policy"], "balanced")
            self.assertEqual(payload["default_installer_policy"], "installer-preseed")
            self.assertEqual(payload["profile_preseed_urls"]["debian"], loaded["DEBIAN_DE_PRESEED_INTERNAL_URL"])
            self.assertFalse(payload["default_live_toram"])
            self.assertEqual(payload["default_live_mem_gib"], 0)
            self.assertTrue(payload["default_live_hooks"])
            self.assertEqual(payload["default_live_args_hooks"], "live-config.hooks=medium")
            for key in (
                "default_live_wifi_interface",
                "default_live_wifi_essid",
                "default_live_wifi_security",
                "default_live_wifi_cidr",
                "default_live_wifi_gateway",
                "default_live_wifi_nameservers",
                "default_live_wifi_psk",
            ):
                self.assertNotIn(key, payload)
            for key in (
                "DEFAULT_LIVE_WIFI_INTERFACE",
                "DEFAULT_LIVE_WIFI_ESSID",
                "DEFAULT_LIVE_WIFI_SECURITY",
                "DEFAULT_LIVE_WIFI_CIDR",
                "DEFAULT_LIVE_WIFI_GATEWAY",
                "DEFAULT_LIVE_WIFI_NAMESERVERS",
                "DEFAULT_LIVE_WIFI_PSK",
            ):
                self.assertNotIn(key, loaded)
            self.assertEqual(payload["debian_preseed_public_url"], loaded["DEBIAN_DE_PRESEED_PUBLIC_URL"])
            self.assertEqual(payload["debian_preseed_public_args"], loaded["DEBIAN_DE_PRESEED_PUBLIC_ARGS"])
            self.assertEqual(payload["debian_preseed_internal_args"], "")
            self.assertEqual(payload["default_partition_labels"]["DEFAULT_ESP_LABEL"], "ESPBOOT")
            self.assertEqual(payload["default_partition_labels"]["DEFAULT_DEBIAN_NETINST_LABEL"], "DEBIAN-NETINST")
            self.assertEqual(payload["default_partition_labels"]["DEFAULT_DEBIAN_NETBOOT_LABEL"], "DEBIAN-NETBOOT")
            self.assertEqual(payload["default_partition_labels"]["DEFAULT_KALI_NETINST_LABEL"], "KALI-NETINST")
            self.assertEqual(payload["default_partition_labels"]["DEFAULT_KALI_NETBOOT_LABEL"], "KALI-NETBOOT")
            self.assertEqual(payload["installer_policy_preserve_kernel_args"], "")
            self.assertEqual(payload["default_live_kernel_extras"], loaded["DEFAULT_LIVE_KERNEL_EXTRAS"])
            self.assertEqual(payload["default_installer_kernel_extras"], "")
            self.assertEqual(payload["default_forensics_kernel_extras"], "")
            self.assertEqual(payload["preseed_common_kernel_args"], loaded["PRESEED_COMMON_KERNEL_ARGS"])
            self.assertEqual(payload["preseed_usb_files"]["debian"], loaded["PRESEED_USB_DEBIAN_DE_FILE"])
            self.assertEqual(payload["preseed_host_paths"]["kali-purple"], loaded["PRESEED_HOST_PURPLE_PATH"])
            self.assertEqual(
                payload["profile_live_kernel_extras"]["debian"],
                loaded["DEBIAN_LIVE_KERNEL_EXTRAS"],
            )
            self.assertEqual(
                payload["profile_live_kernel_extras"]["kali-linux"],
                loaded["KALI_LINUX_LIVE_KERNEL_EXTRAS"],
            )
            self.assertEqual(
                payload["profile_fallback_live_kernel_args"]["ubuntu-desktop"],
                loaded["UBUNTU_DESKTOP_FALLBACK_LIVE_KERNEL_ARGS"],
            )
            self.assertEqual(payload["profile_forensics_kernel_extras"]["kali-linux"], "")
            self.assertIn("DEBIAN_LIVE_ISO_STABLE_URL", loaded)
            self.assertIn("DEBIAN_LIVE_ISO_TESTING_URL", loaded)
            self.assertIn("KALI_LIVE_ISO_STABLE_URL", loaded)
            self.assertIn("KALI_LIVE_ISO_TESTING_URL", loaded)
            self.assertIn("TAILS_LIVE_ISO_STABLE_URL", loaded)
            self.assertIn("TAILS_LIVE_ISO_TESTING_URL", loaded)

    def test_repo_template_uses_kali_live_torrents(self) -> None:
        loaded = load_template_config()

        self.assertTrue(loaded["KALI_LIVE_ISO_STABLE_URL"].endswith(".iso.torrent"))
        self.assertTrue(loaded["KALI_LIVE_ISO_TESTING_URL"].endswith(".iso.torrent"))

    def test_preseed_variant_args_allow_empty_values_and_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "debian-usb.conf"
            save_config(
                str(config_path),
                {
                    "DEBIAN_DE_PRESEED_PUBLIC_ARGS": "",
                    "DEBIAN_DE_PRESEED_INTERNAL_ARGS": "",
                },
            )

            loaded = load_config(str(config_path))
            payload = runtime_config(str(config_path))

            self.assertEqual(loaded["DEBIAN_DE_PRESEED_PUBLIC_ARGS"], "")
            self.assertEqual(loaded["DEBIAN_DE_PRESEED_INTERNAL_ARGS"], "")
            self.assertEqual(payload["debian_preseed_public_args"], "")
            self.assertEqual(payload["debian_preseed_internal_args"], "")

            save_config(
                str(config_path),
                {
                    **loaded,
                    "DEBIAN_DE_PRESEED_PUBLIC_ARGS": (
                        "  debian-installer/allow_unauthenticated_ssl=true   public-only=1  "
                    ),
                    "DEBIAN_DE_PRESEED_INTERNAL_ARGS": "  internal-only=1  ",
                },
            )
            loaded = load_config(str(config_path))
            payload = runtime_config(str(config_path))

            self.assertEqual(
                loaded["DEBIAN_DE_PRESEED_PUBLIC_ARGS"],
                "debian-installer/allow_unauthenticated_ssl=true public-only=1",
            )
            self.assertEqual(loaded["DEBIAN_DE_PRESEED_INTERNAL_ARGS"], "internal-only=1")
            self.assertEqual(
                payload["debian_preseed_public_args"],
                "debian-installer/allow_unauthenticated_ssl=true public-only=1",
            )
            self.assertEqual(payload["debian_preseed_internal_args"], "internal-only=1")

    def test_load_config_rejects_invalid_managed_source_url(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "debian-usb.conf"
            config_text = Path("configs/debian-usb.conf").read_text(encoding="utf-8")
            config_text = config_text.replace(
                'TAILS_LIVE_ISO_TESTING_URL="https://nightly.tails.net/build_Tails_ISO_web-release-7.10/lastSuccessful/archive/latest.iso"',
                'TAILS_LIVE_ISO_TESTING_URL="file:///tmp/tails.iso"',
                1,
            )
            config_path.write_text(config_text, encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "installer URL values must start with http:// or https://"):
                load_config(str(config_path))

    def test_kernel_defaults_rewrite_config(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "debian-usb.conf"
            save_config(str(config_path), {})
            update_default_live_toram(str(config_path), True)
            update_default_live_mem_gib(str(config_path), 6)
            update_default_boot_policy(str(config_path), "performance")
            update_default_installer_policy(str(config_path), "preserve")
            update_profile_preseed_url(str(config_path), "debian", "https://example.test/preseed.cfg")
            update_default_live_kernel_extras(str(config_path), "  foo=bar   toram  ")
            update_default_installer_kernel_extras(str(config_path), " auto=true  priority=critical ")
            update_default_forensics_kernel_extras(str(config_path), " forensic=true  rd.shell=0 ")
            update_profile_forensics_kernel_extras(str(config_path), "kali-linux", "noprompt")
            loaded = load_config(str(config_path))
            self.assertEqual(loaded["DEFAULT_LIVE_TORAM"], "1")
            self.assertEqual(loaded["DEFAULT_LIVE_MEM_GIB"], "6")
            self.assertEqual(loaded["DEFAULT_BOOT_POLICY"], "performance")
            self.assertEqual(loaded["DEFAULT_INSTALLER_POLICY"], "preserve")
            self.assertEqual(loaded["DEBIAN_DE_PRESEED_INTERNAL_URL"], "https://example.test/preseed.cfg")
            self.assertEqual(loaded["DEFAULT_LIVE_KERNEL_EXTRAS"], "foo=bar toram")
            self.assertEqual(loaded["DEFAULT_INSTALLER_KERNEL_EXTRAS"], "auto=true priority=critical")
            self.assertEqual(loaded["DEFAULT_FORENSICS_KERNEL_EXTRAS"], "forensic=true rd.shell=0")
            self.assertEqual(loaded["KALI_LINUX_FORENSICS_KERNEL_EXTRAS"], "noprompt")

    def test_save_config_writes_streamlined_sections_and_omits_legacy_keys(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "debian-usb.conf"
            save_config(
                str(config_path),
                {
                    "INITRD_REQUIRED_MODULES": "btrfs nvme",
                    "DEFAULT_BOOT_POLICY": "hardened",
                },
            )
            rendered = config_path.read_text(encoding="utf-8")
            template = load_template_config()
            saved = load_config(str(config_path))
            self.assertIn("# Live boot policy kernel arguments", rendered)
            self.assertIn("# Live config hooks", rendered)
            self.assertIn("# Installer policy kernel arguments", rendered)
            self.assertIn("# Per-profile fallback live kernel arguments", rendered)
            self.assertIn('DEFAULT_BOOT_POLICY="hardened"', rendered)
            self.assertIn('BOOT_POLICY_HARDENED_KERNEL_ARGS="', rendered)
            self.assertIn('PRESEED_COMMON_KERNEL_ARGS="', rendered)
            self.assertIn('DEBIAN_DE_PRESEED_PUBLIC_ARGS="', rendered)
            self.assertEqual(saved["DEBIAN_DE_PRESEED_PUBLIC_ARGS"], template["DEBIAN_DE_PRESEED_PUBLIC_ARGS"])
            self.assertIn('DEBIAN_DE_PRESEED_INTERNAL_ARGS=""', rendered)
            self.assertIn('PRESEED_USB_DEBIAN_DE_FILE="', rendered)
            self.assertNotIn('PRESEED_HOST_KALI_DE_PATH=', rendered)
            self.assertIn('DEBIAN_FALLBACK_LIVE_KERNEL_ARGS="', rendered)
            self.assertIn('UBUNTU_SERVER_LIVE_KERNEL_EXTRAS=""', rendered)
            self.assertIn('DEFAULT_LIVE_HOOKS="1"', rendered)
            self.assertIn('DEFAULT_LIVE_ARGS_HOOKS="live-config.hooks=medium"', rendered)
            self.assertIn('DEFAULT_LIVE_KERNEL_EXTRAS="', rendered)
            self.assertEqual(saved["DEFAULT_LIVE_KERNEL_EXTRAS"], template["DEFAULT_LIVE_KERNEL_EXTRAS"])
            self.assertNotIn("DEFAULT_LIVE_WIFI_", rendered)
            self.assertFalse(any(key.startswith("DEFAULT_LIVE_WIFI_") for key in saved))
            self.assertFalse(any(key.startswith("DEFAULT_LIVE_WIFI_") for key in template))
            self.assertIn('DEFAULT_ESP_LABEL="ESPBOOT"', rendered)
            self.assertIn('DEFAULT_DEBIAN_NETINST_LABEL="DEBIAN-NETINST"', rendered)
            self.assertIn('DEFAULT_DEBIAN_NETBOOT_LABEL="DEBIAN-NETBOOT"', rendered)
            self.assertIn('DEFAULT_KALI_NETINST_LABEL="KALI-NETINST"', rendered)
            self.assertIn('DEFAULT_KALI_NETBOOT_LABEL="KALI-NETBOOT"', rendered)
            self.assertIn('DEFAULT_UBUNTU_PERSIST_PARTLABEL="writable"', rendered)
            self.assertNotIn("INITRD_REQUIRED_MODULES", rendered)
            self.assertIn('KALI_PURPLE_PRESEED_INTERNAL_URL="', rendered)
            self.assertEqual(
                saved["KALI_PURPLE_PRESEED_INTERNAL_URL"],
                template["KALI_PURPLE_PRESEED_INTERNAL_URL"],
            )

    def test_load_config_requires_spec_referenced_additional_preseed_key(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config_path = root / "debian-usb.conf"
            save_config(str(config_path), {})
            spec_dir = root / "spec"
            spec_dir.mkdir()
            debian_spec = json.loads(Path("configs/spec/grub/debian.json").read_text(encoding="utf-8"))
            debian_spec["preseed"]["preset_sets"]["debian-de"].append(
                {
                    "label": " (ROLE=TEST,GPU=Nvidia,NET=DHCP,BOOT=Dualboot)",
                    "args_key": "PRESEED_ELEVEN_ARGS_DEBIAN",
                }
            )
            (spec_dir / "debian.json").write_text(json.dumps(debian_spec), encoding="utf-8")

            with patch.dict(os.environ, {"DEBIAN_USB_SPEC_DIR": str(spec_dir)}):
                with self.assertRaisesRegex(ValueError, "missing required config key: PRESEED_ELEVEN_ARGS_DEBIAN"):
                    load_config(str(config_path))

    def test_save_config_preserves_spec_referenced_additional_preseed_key(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config_path = root / "debian-usb.conf"
            spec_dir = root / "spec"
            spec_dir.mkdir()
            debian_spec = json.loads(Path("configs/spec/grub/debian.json").read_text(encoding="utf-8"))
            debian_spec["preseed"]["preset_sets"]["debian-de"].append(
                {
                    "label": " (ROLE=TEST,GPU=Nvidia,NET=DHCP,BOOT=Dualboot)",
                    "args_key": "PRESEED_ELEVEN_ARGS_DEBIAN",
                }
            )
            (spec_dir / "debian.json").write_text(json.dumps(debian_spec), encoding="utf-8")

            with patch.dict(os.environ, {"DEBIAN_USB_SPEC_DIR": str(spec_dir)}):
                save_config(
                    str(config_path),
                    {
                        "PRESEED_ELEVEN_ARGS_DEBIAN": "classes=prod\\;test\\;dhcp\\;dualboot",
                    },
                )
                loaded = load_config(str(config_path))

            rendered = config_path.read_text(encoding="utf-8")
            self.assertEqual(loaded["PRESEED_ELEVEN_ARGS_DEBIAN"], "classes=prod\\;test\\;dhcp\\;dualboot")
            self.assertIn('PRESEED_ELEVEN_ARGS_DEBIAN="classes=prod\\;test\\;dhcp\\;dualboot"', rendered)

    def test_rejects_invalid_partition_labels(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "debian-usb.conf"
            with self.assertRaisesRegex(ValueError, "DEFAULT_ESP_LABEL"):
                save_config(str(config_path), {"DEFAULT_ESP_LABEL": "ESPBOOT-TOO-LONG"})
            with self.assertRaisesRegex(ValueError, "DEFAULT_DEBIAN_LIVE_LABEL"):
                save_config(str(config_path), {"DEFAULT_DEBIAN_LIVE_LABEL": "DEBIAN LIVE"})

    def test_drops_obsolete_global_live_wifi_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "debian-usb.conf"
            save_config(
                str(config_path),
                {
                    "DEFAULT_LIVE_WIFI_INTERFACE": "wlan0",
                    "DEFAULT_LIVE_WIFI_ESSID": "Install Net",
                    "DEFAULT_LIVE_WIFI_SECURITY": "wpa",
                    "DEFAULT_LIVE_WIFI_CIDR": "192.168.50.45/24",
                    "DEFAULT_LIVE_WIFI_GATEWAY": "192.168.50.1",
                    "DEFAULT_LIVE_WIFI_NAMESERVERS": "192.168.50.1",
                },
            )
            loaded = load_config(str(config_path))
            rendered = config_path.read_text(encoding="utf-8")
            self.assertFalse(any(key.startswith("DEFAULT_LIVE_WIFI_") for key in loaded))
            self.assertNotIn("DEFAULT_LIVE_WIFI_", rendered)

    def test_rejects_all_live_wifi_kernel_arguments(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "debian-usb.conf"
            for name in (
                "DEFAULT_LIVE_WIFI_PSK",
                "LIVE_WIFI_INTERFACE",
                "LIVE_WIFI_ESSID",
                "LIVE_WIFI_SECURITY",
                "LIVE_WIFI_CIDR",
                "LIVE_WIFI_GATEWAY",
                "LIVE_WIFI_NAMESERVERS",
                "LIVE_WIFI_PASSPHRASE",
                "PRESEED_WIFI_PASSPHRASE",
                "live_wifi_interface",
                "live_wifi_essid_b64",
                "live_wifi_security",
                "live_wifi_cidr",
                "live_wifi_gateway",
                "live_wifi_nameservers",
                "live_wifi_psk",
                "live_wifi_psk_b64",
                "live_wifi_wpa",
                "netcfg/wireless_essid",
                "netcfg/wireless_wpa",
            ):
                with self.subTest(name=name):
                    with self.assertRaisesRegex(ValueError, "forbidden Live Wi-Fi kernel argument"):
                        save_config(
                            str(config_path),
                            {"DEFAULT_LIVE_ARGS_HOOKS": f"live-config.hooks=medium {name}=fixture"},
                        )

    def test_accepts_live_kernel_overrides_for_ubuntu_server(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "debian-usb.conf"
            save_config(str(config_path), {})
            update_profile_live_kernel_extras(str(config_path), "ubuntu-server", "foo=bar")
            loaded = load_config(str(config_path))
            self.assertEqual(loaded["UBUNTU_SERVER_LIVE_KERNEL_EXTRAS"], "foo=bar")

    def test_rejects_forensics_kernel_overrides_for_non_live_profiles(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "debian-usb.conf"
            save_config(str(config_path), {})
            with self.assertRaisesRegex(ValueError, "forensics kernel extras are not supported for profile: kali-purple"):
                update_profile_forensics_kernel_extras(str(config_path), "kali-purple", "foo=bar")

    def test_load_config_remaps_legacy_kali_override_keys(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "debian-usb.conf"
            config_path.write_text(
                '\n'.join(
                    [
                        'APP_NAME="debian-usb"',
                        'APP_VERSION="0.1.0"',
                        'DEFAULT_PERSISTENCE_SIZE_GIB="8"',
                        'DEFAULT_BOOT_POLICY="balanced"',
                        'DEFAULT_INSTALLER_POLICY="installer-preseed"',
                        'DEFAULT_LIVE_TORAM="0"',
                        'DEFAULT_LIVE_MEM_GIB="0"',
                        'SHARED_LIVE_BASE_KERNEL_ARGS="base=1"',
                        'BOOT_POLICY_BALANCED_KERNEL_ARGS="balanced=1"',
                        'BOOT_POLICY_PERFORMANCE_KERNEL_ARGS="performance=1"',
                        'BOOT_POLICY_HARDENED_KERNEL_ARGS="hardened=1"',
                        'PRESEED_COMMON_KERNEL_ARGS="preseed-common=1"',
                        'PRESEED_USB_DEBIAN_DE_FILE="/hd-media/debian-preseed-de/preseed.cfg"',
                        'PRESEED_USB_KALI_DE_FILE="/hd-media/kali-preseed-de/preseed.cfg"',
                        'PRESEED_USB_PURPLE_FILE="/hd-media/preseed/purple/preseed.cfg"',
                        'PRESEED_HOST_DEBIAN_DE_PATH="/data/cfg/preseed/debian"',
                        'PRESEED_HOST_KALI_DE_PATH="/data/cfg/preseed/kali"',
                        'PRESEED_HOST_PURPLE_PATH="/data/cfg/preseed/purple"',
                        'DEBIAN_DE_PRESEED_PUBLIC_URL="https://example.test/public-preseed.cfg"',
                        'DEBIAN_DE_PRESEED_PUBLIC_ARGS="debian-installer/allow_unauthenticated_ssl=true"',
                        'DEBIAN_DE_PRESEED_INTERNAL_ARGS=""',
                        'DEBIAN_DE_PRESEED_INTERNAL_URL="https://example.test/debian-preseed.cfg"',
                        'PRESEED_ONE_ARGS_DEBIAN_DE="deb-one"',
                        'PRESEED_TWO_ARGS_DEBIAN_DE="deb-two"',
                        'PRESEED_THREE_ARGS_DEBIAN_DE="deb-three"',
                        'PRESEED_FOUR_ARGS_DEBIAN_DE="deb-four"',
                        'PRESEED_FIVE_ARGS_DEBIAN_DE="deb-five"',
                        'PRESEED_SIX_ARGS_DEBIAN_DE="deb-six"',
                        'PRESEED_SEVEN_ARGS_DEBIAN_DE="deb-seven"',
                        'PRESEED_EIGHT_ARGS_DEBIAN_DE="deb-eight"',
                        'PRESEED_NINE_ARGS_DEBIAN_DE="deb-nine"',
                        'PRESEED_ONE_ARGS_KALI_DE="kali-one"',
                        'PRESEED_TWO_ARGS_KALI_DE="kali-two"',
                        'PRESEED_THREE_ARGS_KALI_DE="kali-three"',
                        'PRESEED_FOUR_ARGS_KALI_DE="kali-four"',
                        'PRESEED_FIVE_ARGS_KALI_DE="kali-five"',
                        'PRESEED_SIX_ARGS_KALI_DE="kali-six"',
                        'PRESEED_SEVEN_ARGS_KALI_DE="kali-seven"',
                        'PRESEED_EIGHT_ARGS_KALI_DE="kali-eight"',
                        'PRESEED_NINE_ARGS_KALI_DE="kali-nine"',
                        'DEFAULT_LIVE_KERNEL_EXTRAS=""',
                        'DEFAULT_INSTALLER_KERNEL_EXTRAS=""',
                        'DEFAULT_FORENSICS_KERNEL_EXTRAS=""',
                        'KALI_LIVE_KERNEL_EXTRAS="components username=kali"',
                        'KALI_INSTALLER_KERNEL_EXTRAS="net.ifnames=0"',
                        'KALI_PRESEED_URL="https://example.test/preseed.cfg"',
                        'KALI_PURPLE_INSTALLER_KERNEL_EXTRAS=""',
                        'KALI_PURPLE_PRESEED_INTERNAL_URL=""',
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            payload = runtime_config(str(config_path))
            self.assertEqual(payload["profile_live_kernel_extras"]["kali-linux"], "components username=kali")
            self.assertEqual(payload["profile_installer_kernel_extras"]["kali-linux"], "net.ifnames=0")
            self.assertEqual(payload["profile_preseed_urls"]["kali-linux"], "https://example.test/preseed.cfg")

    def test_list_local_isos_detects_uppercase_extension(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            old_cwd = Path.cwd()
            try:
                Path(temp_dir, "sample.ISO").write_text("iso", encoding="utf-8")
                os.chdir(temp_dir)
                payload = list_local_isos()
            finally:
                os.chdir(old_cwd)
            self.assertTrue(any(item["name"] == "sample.ISO" for item in payload))

    def test_template_config_keeps_kali_preseed_preset_slots_defined(self) -> None:
        payload = load_template_config()
        self.assertEqual(payload["PRESEED_ONE_ARGS_KALI_DE"], "")
        for number_name in ("ONE", "TWO", "THREE", "FOUR", "FIVE", "SIX", "SEVEN", "EIGHT", "NINE"):
            for family in ("KALI", "DEBIAN"):
                for suffix in ("DE", "SRV"):
                    self.assertIn(f"PRESEED_{number_name}_ARGS_{family}_{suffix}", payload)

    def test_profile_payload_labels_use_explicit_netboot_labels(self) -> None:
        payload = load_template_config()
        self.assertEqual(
            profile_payload_labels(payload, "debian", source_role="netboot", media_class="installer"),
            ("DEBIAN-NETBOOT", "DEBIAN-NETBOOT"),
        )
        self.assertEqual(
            profile_payload_labels(payload, "kali-linux", source_role="netboot", media_class="installer"),
            ("KALI-NETBOOT", "KALI-NETBOOT"),
        )


if __name__ == "__main__":
    unittest.main()

class ManualHDMediaConfigTests(unittest.TestCase):
    def test_split_host_paths_are_absent_from_template_and_loaded_config(self):
        retired = ('PRESEED_HOST_DEBIAN_DE_PATH', 'PRESEED_HOST_DEBIAN_SRV_PATH',
                   'PRESEED_HOST_KALI_DE_PATH', 'PRESEED_HOST_KALI_SRV_PATH')
        template = load_template_config()
        for key in retired:
            self.assertNotIn(key, template)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'debian-usb.conf'
            save_config(str(path), {})
            values = load_config(str(path))
            for key in retired:
                self.assertNotIn(key, values)
            self.assertEqual(runtime_config(str(path))['preseed_host_paths']['debian'], '')
            self.assertEqual(runtime_config(str(path))['preseed_host_paths']['kali-linux'], '')

    def test_legacy_split_host_paths_are_dropped_not_reintroduced(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'debian-usb.conf'
            save_config(str(path), {'PRESEED_HOST_DEBIAN_DE_PATH': '/not/required/preseed.cfg',
                                    'PRESEED_HOST_DEBIAN_SRV_PATH': '/not/required/preseed.cfg',
                                    'PRESEED_HOST_KALI_DE_PATH': '/not/required/preseed.cfg',
                                    'PRESEED_HOST_KALI_SRV_PATH': '/not/required/preseed.cfg'})
            text = path.read_text(encoding='utf-8')
            self.assertNotIn('PRESEED_HOST_DEBIAN_', text)
            self.assertNotIn('PRESEED_HOST_KALI_', text)
            load_config(str(path))
