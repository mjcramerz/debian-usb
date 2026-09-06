import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from debian_usb import cli


class CLITests(unittest.TestCase):
    def test_main_dispatches_inspect_iso(self) -> None:
        payload = {"media_class": "hybrid", "firmware": ["uefi"], "managed_payload_layout": "iso-store"}
        stdout = io.StringIO()
        with patch("debian_usb.cli.inspect_media", return_value=payload) as inspect_iso_mock:
            with patch("sys.stdout", stdout):
                exit_code = cli.main(["inspect-iso", "--iso-path", "/tmp/example.iso", "--profile", "debian"])
        self.assertEqual(exit_code, 0)
        inspect_iso_mock.assert_called_once_with("/tmp/example.iso", "debian", config_path="", use_custom_menu=False, source_role="primary")
        self.assertEqual(json.loads(stdout.getvalue()), payload)

    def test_main_dispatches_render_managed_grub(self) -> None:
        payload = {"grub_cfg": "menuentry", "entry_count": 3, "media_class": "hybrid", "top_level_entries": ["Live"]}
        stdout = io.StringIO()
        with patch("debian_usb.cli.render_managed_grub", return_value=payload) as render_mock:
            with patch("sys.stdout", stdout):
                exit_code = cli.main(
                    [
                        "render-managed-grub",
                        "--iso-path",
                        "/tmp/example.iso",
                        "--profile",
                        "ubuntu-desktop",
                        "--live-uuid",
                        "ABCD-1234",
                        "--persistence",
                        "1",
                        "--persistence-mode",
                        "plain",
                        "--config",
                        "configs/debian-usb.conf",
                        "--menu-label",
                        "Ubuntu Desktop Live",
                        "--kernel-args",
                        "boot=casper quiet splash",
                        "--kernel-path",
                        "/casper/vmlinuz",
                        "--initrd-path",
                        "/casper/initrd",
                        "--live-toram",
                        "1",
                        "--boot-assets-uuid",
                        "ESP-UUID",
                        "--use-custom-menu",
                        "1",
                        "--preserve-upstream-grub-entries",
                        "1",
                        "--include-preseed",
                        "1",
                    ]
                )
        self.assertEqual(exit_code, 0)
        render_mock.assert_called_once_with(
            source_path="/tmp/example.iso",
            profile="ubuntu-desktop",
            live_uuid="ABCD-1234",
            persistence=True,
            persistence_mode="plain",
            config_path="configs/debian-usb.conf",
            menu_label_override="Ubuntu Desktop Live",
            kernel_args_override="boot=casper quiet splash",
            kernel_path_override="/casper/vmlinuz",
            initrd_path_override="/casper/initrd",
            live_toram=True,
            boot_assets_uuid="ESP-UUID",
            use_custom_menu=True,
            preserve_upstream_entries=True,
            include_preseed_entries=True,
            source_role="primary",
        )
        self.assertEqual(json.loads(stdout.getvalue()), payload)

    def test_main_dispatches_device_and_iso_list_commands(self) -> None:
        stdout = io.StringIO()
        with patch("debian_usb.cli.list_local_isos", return_value=[{"path": "/tmp/a.iso"}]) as list_isos_mock:
            with patch("debian_usb.cli.list_devices", return_value=[{"path": "/dev/sdb"}]) as list_devices_mock:
                with patch("sys.stdout", stdout):
                    exit_code = cli.main(["list-local-isos"])
                self.assertEqual(exit_code, 0)
                list_isos_mock.assert_called_once_with()
                self.assertEqual(json.loads(stdout.getvalue()), [{"path": "/tmp/a.iso"}])

                stdout = io.StringIO()
                with patch("sys.stdout", stdout):
                    exit_code = cli.main(["list-devices"])
                self.assertEqual(exit_code, 0)
                list_devices_mock.assert_called_once_with()
                self.assertEqual(json.loads(stdout.getvalue()), [{"path": "/dev/sdb"}])

    def test_main_dispatches_multios_commands(self) -> None:
        stdout = io.StringIO()
        with patch("debian_usb.cli.validate_multios_plan_file", return_value={"valid": True}) as validate_mock:
            with patch("sys.stdout", stdout):
                exit_code = cli.main(["validate-multios-plan", "--plan", "/tmp/plan.json", "--inspect-media"])
        self.assertEqual(exit_code, 0)
        validate_mock.assert_called_once_with("/tmp/plan.json", inspect_sources=True)
        self.assertEqual(json.loads(stdout.getvalue()), {"valid": True})

        stdout = io.StringIO()
        payload = {"grub_cfg": "menuentry", "entry_count": 2}
        with patch("debian_usb.cli.render_multios_grub", return_value=payload) as render_mock:
            with patch("sys.stdout", stdout):
                exit_code = cli.main(
                    [
                        "render-multios-grub",
                        "--plan",
                        "/tmp/plan.json",
                        "--config",
                        "configs/debian-usb.conf",
                        "--payload-uuid",
                        "os1=AAAA",
                        "--boot-assets-uuid",
                        "ESP-UUID",
                    ]
                )
        self.assertEqual(exit_code, 0)
        render_mock.assert_called_once_with(
            plan_path="/tmp/plan.json",
            config_path="configs/debian-usb.conf",
            payload_uuids={"os1": "AAAA"},
            boot_assets_uuid="ESP-UUID",
        )
        self.assertEqual(json.loads(stdout.getvalue()), payload)

    def test_main_dispatches_build_iso_commands(self) -> None:
        stdout = io.StringIO()
        with patch("debian_usb.cli.validate_build_iso_plan_file", return_value={"valid": True}) as validate_mock:
            with patch("sys.stdout", stdout):
                exit_code = cli.main(["validate-build-iso-plan", "--plan", "/tmp/build-plan.json"])
        self.assertEqual(exit_code, 0)
        validate_mock.assert_called_once_with("/tmp/build-plan.json")
        self.assertEqual(json.loads(stdout.getvalue()), {"valid": True})

        stdout = io.StringIO()
        with patch("debian_usb.cli.build_debian_iso", return_value={"iso_path": "/tmp/custom.iso"}) as build_mock:
            with patch("sys.stdout", stdout):
                exit_code = cli.main(["build-debian-iso", "--plan", "/tmp/build-plan.json"])
        self.assertEqual(exit_code, 0)
        build_mock.assert_called_once_with("/tmp/build-plan.json")
        self.assertEqual(json.loads(stdout.getvalue()), {"iso_path": "/tmp/custom.iso"})

        stdout = io.StringIO()
        with patch("debian_usb.cli.ensure_debian_build_deps", return_value={"changed": False}) as deps_mock:
            with patch("sys.stdout", stdout):
                exit_code = cli.main(["ensure-debian-build-deps"])
        self.assertEqual(exit_code, 0)
        deps_mock.assert_called_once_with()
        self.assertEqual(json.loads(stdout.getvalue()), {"changed": False})

    def test_main_dispatches_live_tool_remaster(self) -> None:
        payload = {"iso_path": "/tmp/output/debian-live-admin-tools.iso", "packages": ["nmap"]}
        stdout = io.StringIO()
        with patch("debian_usb.cli.remaster_live_tools_source", return_value=payload) as remaster_mock:
            with patch("sys.stdout", stdout):
                exit_code = cli.main(
                    [
                        "remaster-live-tools-source",
                        "--profile",
                        "debian",
                        "--source-iso",
                        "/tmp/debian-live.iso",
                        "--output-dir",
                        "/tmp/output",
                    ]
                )

        self.assertEqual(exit_code, 0)
        remaster_mock.assert_called_once_with(
            "/tmp/debian-live.iso",
            "debian",
            "/tmp/output",
            selected_groups=None,
            live_kernel_args="",
            overlay_dir="",
            ensure_encrypted_persistence=False,
        )
        self.assertEqual(json.loads(stdout.getvalue()), payload)

    def test_main_dispatches_explicit_empty_live_tool_selection(self) -> None:
        stdout = io.StringIO()
        with patch("debian_usb.cli.remaster_live_tools_source", return_value={"iso_path": "/tmp/output.iso"}) as remaster_mock:
            with patch("sys.stdout", stdout):
                exit_code = cli.main(
                    [
                        "remaster-live-tools-source",
                        "--profile",
                        "debian",
                        "--source-iso",
                        "/tmp/debian-live.iso",
                        "--no-tools",
                        "--live-kernel-args",
                        "live-config.hooks=medium debian_usb.profile=test",
                    ]
                )

        self.assertEqual(exit_code, 0)
        remaster_mock.assert_called_once_with(
            "/tmp/debian-live.iso",
            "debian",
            "",
            selected_groups=[],
            live_kernel_args="live-config.hooks=medium debian_usb.profile=test",
            overlay_dir="",
            ensure_encrypted_persistence=False,
        )

    def test_main_validates_live_wifi_config_without_echoing_values(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            live_env = Path(temp_dir) / "live.env"
            live_env.write_text(
                "\n".join(
                    (
                        "LIVE_WIFI_INTERFACE='wlan0'",
                        "LIVE_WIFI_ESSID='Fixture Network'",
                        "LIVE_WIFI_SECURITY='wpa'",
                        "LIVE_WIFI_CIDR='192.0.2.10/24'",
                        "LIVE_WIFI_GATEWAY='192.0.2.1'",
                        "LIVE_WIFI_NAMESERVERS='192.0.2.1,198.51.100.53'",
                        "LIVE_WIFI_PASSPHRASE='literal$Pass123'",
                    )
                )
                + "\n",
                encoding="utf-8",
            )
            live_env.chmod(0o644)
            stdout = io.StringIO()
            stderr = io.StringIO()
            with patch("sys.stdout", stdout), patch("sys.stderr", stderr):
                exit_code = cli.main(
                    ["validate-live-wifi-config", "--path", str(live_env)]
                )

            self.assertEqual(exit_code, 0)
            self.assertEqual(json.loads(stdout.getvalue()), {"valid": True})
            self.assertEqual(stderr.getvalue(), "")
            self.assertEqual(live_env.stat().st_mode & 0o777, 0o644)
            self.assertNotIn("Fixture Network", stdout.getvalue())
            self.assertNotIn("literal$Pass123", stdout.getvalue())

            live_env.write_text(
                "LIVE_WIFI_ESSID=\"Publisher's Network\"\n"
                "LIVE_WIFI_SECURITY='open'\n",
                encoding="utf-8",
            )
            live_env.chmod(0o644)
            stdout = io.StringIO()
            stderr = io.StringIO()
            with patch("sys.stdout", stdout), patch("sys.stderr", stderr):
                exit_code = cli.main(
                    ["validate-live-wifi-config", "--path", str(live_env)]
                )

            self.assertEqual(exit_code, 1)
            self.assertEqual(stdout.getvalue(), "")
            self.assertIn("must not contain single quote", stderr.getvalue())
            self.assertNotIn("Publisher's Network", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
