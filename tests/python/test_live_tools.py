from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from debian_usb import live_tools


class LiveToolProfileTests(unittest.TestCase):
    def test_repo_profile_maps_requested_commands_to_installed_packages(self) -> None:
        profile = live_tools.load_live_tool_profile()

        self.assertEqual(profile["supported_profiles"], ["debian", "kali-linux", "ubuntu-desktop"])
        self.assertEqual(
            {
                command: profile["command_packages"][command]
                for command in (
                    "blkdiscard",
                    "fdisk",
                    "sfdisk",
                    "nvme",
                    "mkfs.f2fs",
                    "fsck.f2fs",
                    "btrfs",
                    "mkfs.xfs",
                    "xfs_repair",
                    "nmap",
                    "flashrom",
                    "avrdude",
                    "dfu-util",
                    "openocd",
                    "i2cdetect",
                )
            },
            {
                "blkdiscard": "util-linux",
                "fdisk": "fdisk",
                "sfdisk": "fdisk",
                "nvme": "nvme-cli",
                "mkfs.f2fs": "f2fs-tools",
                "fsck.f2fs": "f2fs-tools",
                "btrfs": "btrfs-progs",
                "mkfs.xfs": "xfsprogs",
                "xfs_repair": "xfsprogs",
                "nmap": "nmap",
                "flashrom": "flashrom",
                "avrdude": "avrdude",
                "dfu-util": "dfu-util",
                "openocd": "openocd",
                "i2cdetect": "i2c-tools",
            },
        )
        self.assertEqual(len(profile["packages"]), len(set(profile["packages"])))

    def test_build_distro_and_profile_loaders_use_the_same_package_list(self) -> None:
        debian_packages, profile = live_tools.live_tool_packages_for_build_distro("debian")
        kali_packages, _ = live_tools.live_tool_packages_for_build_distro("kali-linux")
        ubuntu_build_packages, _ = live_tools.live_tool_packages_for_build_distro("ubuntu")
        ubuntu_packages, _ = live_tools.live_tool_packages_for_profile("ubuntu-desktop")

        self.assertEqual(debian_packages, profile["packages"])
        self.assertTrue(set(profile["packages"]) < set(kali_packages))
        self.assertIn("kali-tools-wireless", kali_packages)
        self.assertNotIn("kali-tools-wireless", debian_packages)
        self.assertEqual(ubuntu_build_packages, profile["packages"])
        self.assertEqual(ubuntu_packages, profile["packages"])

    def test_selected_groups_preserve_catalog_order_and_deduplicate_packages(self) -> None:
        packages, profile = live_tools.live_tool_packages_for_profile(
            "debian",
            selected_groups=["firmware_updates", "firmware_inspection", "nvme", "firmware_updates"],
        )

        self.assertEqual(
            profile["selected_groups"],
            ["nvme", "firmware_inspection", "firmware_updates"],
        )
        self.assertEqual(packages.count("fwupd"), 1)
        self.assertIn("nvme-cli", packages)
        self.assertIn("flashrom", packages)

    def test_firmware_programming_group_includes_ch341a_flashrom_stack(self) -> None:
        packages, profile = live_tools.live_tool_packages_for_profile(
            "debian",
            selected_groups=["firmware_programming"],
        )

        self.assertEqual(profile["selected_groups"], ["firmware_programming"])
        self.assertEqual(packages, ["flashrom", "avrdude", "dfu-util", "openocd", "i2c-tools"])

    def test_explicit_empty_selection_adds_no_optional_packages(self) -> None:
        packages, profile = live_tools.live_tool_packages_for_profile("debian", selected_groups=[])

        self.assertEqual(packages, [])
        self.assertEqual(profile["selected_groups"], [])

    def test_selected_groups_reject_unknown_group(self) -> None:
        with self.assertRaisesRegex(ValueError, "unsupported Live tool package groups"):
            live_tools.live_tool_packages_for_profile("debian", selected_groups=["not-a-group"])

    def test_profile_path_supports_module_relative_installed_layout(self) -> None:
        source_profile = live_tools.live_tool_profile_path()
        with tempfile.TemporaryDirectory() as temp_dir:
            libexec_root = Path(temp_dir) / "custom-libexec"
            module_path = libexec_root / "python" / "debian_usb" / "live_tools.py"
            installed_profile = libexec_root / "spec" / "live" / "admin-tools.json"
            module_path.parent.mkdir(parents=True)
            installed_profile.parent.mkdir(parents=True)
            module_path.write_text("# installed module fixture\n", encoding="utf-8")
            installed_profile.write_bytes(source_profile.read_bytes())

            with patch.object(live_tools, "__file__", str(module_path)):
                with patch.dict(os.environ, {live_tools.LIVE_TOOL_PROFILE_ENV: ""}):
                    resolved_profile = live_tools.live_tool_profile_path()

            self.assertEqual(resolved_profile, installed_profile.resolve())

    def test_profile_rejects_command_mapping_outside_package_groups(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            profile_path = Path(temp_dir) / "profile.json"
            profile_path.write_text(
                json.dumps(
                    {
                        "schema_version": live_tools.LIVE_TOOL_PROFILE_SCHEMA_VERSION,
                        "supported_profiles": ["debian"],
                        "build_distros": {"debian": "debian"},
                        "package_groups": [
                            {
                                "id": "storage",
                                "title": "Storage",
                                "description": "Storage fixture",
                                "packages": ["util-linux"],
                            }
                        ],
                        "command_packages": {"nmap": "nmap"},
                        "notes": [],
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "outside package_groups"):
                live_tools.load_live_tool_profile(profile_path)

    def test_profile_rejects_unsupported_remaster_profile(self) -> None:
        with self.assertRaisesRegex(ValueError, "not supported"):
            live_tools.live_tool_packages_for_profile("tails")


if __name__ == "__main__":
    unittest.main()
