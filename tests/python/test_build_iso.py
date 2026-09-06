from __future__ import annotations

import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from debian_usb import build_iso


def minimal_plan(output_dir: str) -> dict[str, object]:
    return {
        "schema_version": 1,
        "distro": "debian",
        "output_dir": output_dir,
        "image_name": "custom.iso",
        "suite": "trixie",
        "architecture": "amd64",
        "archive_areas": ["main", "contrib", "non-free", "non-free-firmware"],
        "installer_mode": "live",
        "include_installer_launcher": False,
        "include_non_free_firmware": True,
        "base_packages": ["live-boot", "live-boot-initramfs-tools", "live-config", "live-config-systemd", "live-tools"],
        "extra_chroot_packages": ["vim"],
        "extra_binary_packages": [],
        "live_module_spec_path": "",
        "live_deb_spec_path": "",
        "live_udeb_spec_path": "",
        "di_module_spec_path": "",
        "di_deb_spec_path": "",
        "di_udeb_spec_path": "",
        "local_deb_dir": "",
        "local_udeb_dir": "",
        "udeb_rebuild_spec_path": "",
        "udeb_rebuild_source_overlay_dir": "",
        "preseed_path": "",
        "installer_include_dir": "",
        "live_include_dir": "",
        "binary_include_dir": "",
        "bootloader_override_dir": "",
        "installer_distribution": "",
        "installer_boot_append": "",
        "erofs_installer_component_policy": "warn",
        "erofs_installer_components": [],
        "kernel_mode": "stock-debian",
        "kernel_package_stub": "",
        "kernel_flavours": [],
        "kernel_deb_dir": "",
        "custom_apt_repo": "",
        "custom_binary_apt_repo": "",
        "custom_apt_repo_key_path": "",
        "custom_apt_repo_pin": "",
        "rootfs_format": "erofs",
        "erofs_compressor": "zstd",
        "erofs_extra_args": "",
        "filesystem_module_entries": [],
        "initramfs_modules": ["erofs", "xxhash", "xxhash_generic"],
        "kernel_inspection_modules": ["erofs", "xxhash", "xxhash_generic"],
        "kernel_target_version": "",
        "kernel_module_tree_dir": "",
        "kernel_download_if_missing": False,
        "kernel_config_symbols": ["CONFIG_EROFS_FS", "CONFIG_XXHASH"],
        "module_alias_candidates": ["xxhash64", "crypto_xxhash64"],
        "storage_tool_packages": [],
        "cleanup_mode": "keep-workspace",
    }


def minimal_netinst_plan(output_dir: str) -> dict[str, object]:
    plan = minimal_plan(output_dir)
    plan.update(
        {
            "installer_mode": "netinst",
            "rootfs_format": "none",
            "base_packages": [],
            "extra_chroot_packages": [],
            "live_module_spec_path": "",
            "live_deb_spec_path": "",
            "live_udeb_spec_path": "",
            "local_deb_dir": "",
            "live_include_dir": "",
            "bootappend_live": "",
            "filesystem_module_entries": [],
            "initramfs_modules": [],
            "kernel_inspection_modules": [],
            "kernel_config_symbols": [],
            "module_alias_candidates": [],
            "storage_tool_packages": [],
        }
    )
    return plan


class BuildISOTests(unittest.TestCase):
    def test_validate_build_iso_plan_file_accepts_minimal_plan(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            plan_path = Path(temp_dir) / "plan.json"
            payload = minimal_plan(str(Path(temp_dir) / "output"))
            plan_path.write_text(json.dumps(payload), encoding="utf-8")

            result = build_iso.validate_build_iso_plan_file(str(plan_path))

        self.assertTrue(result["valid"])
        self.assertEqual(result["plan"]["rootfs_format"], "erofs")
        self.assertEqual(result["plan"]["image_name"], "custom.iso")
        self.assertEqual(result["plan"]["filesystem_module_entries"], ["filesystem.squashfs"])
        self.assertIn("nvme-cli", result["plan"]["storage_tool_packages"])
        self.assertIn("f2fs-tools", result["plan"]["storage_tool_packages"])
        self.assertIn("btrfs-progs", result["plan"]["storage_tool_packages"])
        self.assertIn("xfsprogs", result["plan"]["storage_tool_packages"])
        self.assertIn("nmap", result["plan"]["storage_tool_packages"])
        for package in build_iso.DEBIAN_LIVE_HOOK_PACKAGES:
            self.assertIn(package, result["plan"]["base_packages"])
        for module in build_iso.DEBIAN_LIVE_INITRAMFS_MODULES:
            self.assertIn(module, result["plan"]["initramfs_modules"])
            self.assertIn(module, result["plan"]["kernel_inspection_modules"])
        for symbol in build_iso.DEBIAN_LIVE_KERNEL_CONFIG_SYMBOLS:
            self.assertIn(symbol, result["plan"]["kernel_config_symbols"])
        for alias in build_iso.DEBIAN_LIVE_MODULE_ALIAS_CANDIDATES:
            self.assertIn(alias, result["plan"]["module_alias_candidates"])
        self.assertIn("live-config.hooks=medium", result["plan"]["bootappend_live"].split())
        self.assertEqual(
            result["plan"]["live_tool_profile"]["package_count"],
            len(result["plan"]["storage_tool_packages"]),
        )

    def test_live_build_without_debian_installer_still_adds_live_administration_tools(self) -> None:
        payload = minimal_plan("/tmp/out")
        payload["installer_mode"] = "none"

        result = build_iso._validate_build_iso_plan(payload)

        self.assertIn("nvme-cli", result["storage_tool_packages"])
        self.assertIn("nmap", result["storage_tool_packages"])
        self.assertEqual(
            result["live_tool_profile"]["package_count"],
            len(result["storage_tool_packages"]),
        )

    def test_live_build_accepts_selected_tool_groups(self) -> None:
        payload = minimal_plan("/tmp/out")
        payload["live_tool_groups"] = ["nmap", "nvme"]

        result = build_iso._validate_build_iso_plan(payload)

        self.assertEqual(result["live_tool_groups"], ["nvme", "nmap"])
        self.assertEqual(result["storage_tool_packages"], ["nvme-cli", "nmap"])

    def test_live_build_accepts_explicit_empty_tool_selection(self) -> None:
        payload = minimal_plan("/tmp/out")
        payload["live_tool_groups"] = []

        result = build_iso._validate_build_iso_plan(payload)

        self.assertEqual(result["live_tool_groups"], [])
        self.assertEqual(result["storage_tool_packages"], [])
        for package in build_iso.DEBIAN_LIVE_HOOK_PACKAGES:
            self.assertIn(package, result["base_packages"])

    def test_validate_build_iso_plan_rejects_relative_output_path(self) -> None:
        payload = minimal_plan("/tmp/out")
        payload["output_dir"] = "relative/out"
        with self.assertRaisesRegex(ValueError, "output_dir must be an absolute path"):
            build_iso._validate_build_iso_plan(payload)

    def test_validate_build_iso_plan_adds_non_free_firmware_area_when_needed(self) -> None:
        payload = minimal_plan("/tmp/out")
        payload["archive_areas"] = ["main", "contrib", "non-free"]
        result = build_iso._validate_build_iso_plan(payload)
        self.assertIn("non-free-firmware", result["archive_areas"])

    def test_lb_config_command_keeps_live_medium_apt_indices(self) -> None:
        plan = build_iso._validate_build_iso_plan(minimal_plan("/tmp/out"))
        command = build_iso._lb_config_command(plan)

        option_index = command.index("--apt-indices")
        self.assertEqual(command[option_index + 1], "true")

    def test_debian_live_bootappend_forces_medium_hooks_before_separator(self) -> None:
        payload = minimal_plan("/tmp/out")
        payload["bootappend_live"] = "quiet live-config.hooks=filesystem --- ignored=tail"

        plan = build_iso._validate_build_iso_plan(payload)
        tokens = plan["bootappend_live"].split()
        separator_index = tokens.index("---")

        self.assertIn("live-config.hooks=medium", tokens[:separator_index])
        self.assertNotIn("live-config.hooks=filesystem", tokens)
        self.assertEqual(tokens[separator_index + 1 :], ["ignored=tail"])
        command = build_iso._lb_config_command(plan)
        self.assertEqual(command[command.index("--bootappend-live") + 1], plan["bootappend_live"])

    def test_lb_config_command_live_tracks_trixie_and_forky_suite(self) -> None:
        for suite in ("trixie", "forky"):
            with self.subTest(suite=suite):
                payload = minimal_plan("/tmp/out")
                payload["suite"] = suite
                payload["installer_distribution"] = ""

                plan = build_iso._validate_build_iso_plan(payload)
                command = build_iso._lb_config_command(plan)

                self.assertEqual(plan["installer_distribution"], suite)
                self.assertEqual(command[command.index("--distribution") + 1], suite)
                self.assertEqual(command[command.index("--debian-installer-distribution") + 1], suite)
                self.assertEqual(command[command.index("--debian-installer") + 1], "live")
                self.assertEqual(command[command.index("--system") + 1], "live")

    def test_lb_config_command_netinst_uses_debian_installer_without_system(self) -> None:
        plan = build_iso._validate_build_iso_plan(minimal_netinst_plan("/tmp/out"))

        command = build_iso._lb_config_command(plan)

        self.assertNotIn("--system", command)
        installer_index = command.index("--debian-installer")
        self.assertEqual(command[installer_index + 1], "netinst")
        self.assertEqual(plan["rootfs_format"], "none")
        self.assertEqual(plan["live_tool_profile"], {})
        self.assertEqual(plan["storage_tool_packages"], [])
        self.assertEqual(plan["bootappend_live"], "")
        self.assertEqual(plan["initramfs_modules"], [])
        self.assertEqual(plan["kernel_inspection_modules"], [])
        self.assertEqual(plan["kernel_config_symbols"], [])
        self.assertEqual(plan["module_alias_candidates"], [])

    def test_lb_config_command_netinst_tracks_trixie_and_forky_suite(self) -> None:
        for suite in ("trixie", "forky"):
            with self.subTest(suite=suite):
                payload = minimal_netinst_plan("/tmp/out")
                payload["suite"] = suite
                payload["installer_distribution"] = ""

                plan = build_iso._validate_build_iso_plan(payload)
                command = build_iso._lb_config_command(plan)

                self.assertEqual(plan["installer_distribution"], suite)
                self.assertEqual(command[command.index("--distribution") + 1], suite)
                self.assertEqual(command[command.index("--debian-installer-distribution") + 1], suite)
                self.assertEqual(command[command.index("--debian-installer") + 1], "netinst")
                self.assertNotIn("--system", command)

    def test_bundled_netinst_profiles_forbid_live_rootfs_inputs(self) -> None:
        for relative_path in (
            "configs/spec/d-i/debian/netinst-build.json",
            "configs/spec/d-i/kali-linux/netinst-build.json",
        ):
            with self.subTest(relative_path=relative_path):
                payload = json.loads(Path(relative_path).read_text(encoding="utf-8"))
                plan = build_iso._validate_build_iso_plan(payload)
                command = build_iso._lb_config_command(plan)
                self.assertEqual(plan["rootfs_format"], "none")
                self.assertEqual(plan["base_packages"], [])
                self.assertEqual(plan["storage_tool_packages"], [])
                self.assertNotIn("--system", command)
                self.assertEqual(command[command.index("--debian-installer") + 1], "netinst")

    def test_validate_netinst_plan_rejects_live_packages(self) -> None:
        payload = minimal_netinst_plan("/tmp/out")
        payload["base_packages"] = ["live-boot"]

        with self.assertRaisesRegex(ValueError, "base_packages is forbidden"):
            build_iso._validate_build_iso_plan(payload)

    def test_validate_netinst_plan_rejects_live_tool_groups(self) -> None:
        payload = minimal_netinst_plan("/tmp/out")
        payload["live_tool_groups"] = ["nvme"]

        with self.assertRaisesRegex(ValueError, "live_tool_groups is forbidden"):
            build_iso._validate_build_iso_plan(payload)

    def test_materialize_netinst_workspace_omits_all_live_rootfs_inputs(self) -> None:
        plan = build_iso._validate_build_iso_plan(minimal_netinst_plan("/tmp/out"))

        with tempfile.TemporaryDirectory() as temp_dir:
            build_root = Path(temp_dir) / "build"
            build_iso._materialize_workspace(build_root, plan, None, [])

            self.assertFalse((build_root / "config/package-lists/base.list.chroot").exists())
            self.assertFalse((build_root / "config/package-lists/zz-user.list.chroot").exists())
            self.assertFalse((build_root / "config/hooks/normal/7000-initramfs-modules.hook.chroot").exists())
            self.assertFalse((build_root / "config/hooks/normal/9990-erofs-rootfs.hook.binary").exists())
            self.assertFalse((build_root / "config/includes.binary/live").exists())
            self.assertFalse(
                (build_root / "config/includes.chroot_after_packages/etc/debian-usb/live.env").exists()
            )

    def test_validate_live_apt_archive_accepts_release_packages_and_pool(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            binary_root = Path(temp_dir) / "binary"
            index_path = binary_root / "dists" / "trixie" / "main" / "binary-amd64" / "Packages.gz"
            release_path = binary_root / "dists" / "trixie" / "Release"
            package_path = binary_root / "pool" / "main" / "e" / "example" / "example_1_amd64.deb"
            for path in (index_path, release_path, package_path):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("fixture\n", encoding="utf-8")

            result = build_iso._validate_live_apt_archive(binary_root, "trixie", "amd64")

        self.assertEqual(result["release_path"], "/dists/trixie/Release")
        self.assertEqual(
            result["package_index_paths"],
            ["/dists/trixie/main/binary-amd64/Packages.gz"],
        )
        self.assertEqual(result["pool_components"], ["main"])
        self.assertTrue(result["package_pool_present"])

    def test_validate_live_apt_archive_rejects_missing_package_indices(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            binary_root = Path(temp_dir) / "binary"
            release_path = binary_root / "dists" / "trixie" / "Release"
            package_path = binary_root / "pool" / "main" / "e" / "example" / "example_1_amd64.deb"
            for path in (release_path, package_path):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("fixture\n", encoding="utf-8")

            with self.assertRaisesRegex(RuntimeError, "binary-amd64/Packages"):
                build_iso._validate_live_apt_archive(binary_root, "trixie", "amd64")

    def test_validate_live_apt_archive_rejects_missing_release_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            binary_root = Path(temp_dir) / "binary"
            index_path = binary_root / "dists" / "trixie" / "main" / "binary-amd64" / "Packages.gz"
            package_path = binary_root / "pool" / "main" / "e" / "example" / "example_1_amd64.deb"
            for path in (index_path, package_path):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("fixture\n", encoding="utf-8")

            with self.assertRaisesRegex(RuntimeError, "dists/trixie/InRelease"):
                build_iso._validate_live_apt_archive(binary_root, "trixie", "amd64")

    def test_validate_live_apt_archive_rejects_missing_package_pool(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            binary_root = Path(temp_dir) / "binary"
            release_path = binary_root / "dists" / "trixie" / "Release"
            index_path = binary_root / "dists" / "trixie" / "main" / "binary-amd64" / "Packages.gz"
            for path in (release_path, index_path):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("fixture\n", encoding="utf-8")

            with self.assertRaisesRegex(RuntimeError, "packages were staged under pool"):
                build_iso._validate_live_apt_archive(binary_root, "trixie", "amd64")

    def test_validate_live_apt_archive_rejects_pool_component_without_index(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            binary_root = Path(temp_dir) / "binary"
            fixture_paths = (
                binary_root / "dists" / "trixie" / "Release",
                binary_root / "dists" / "trixie" / "main" / "binary-amd64" / "Packages.gz",
                binary_root / "pool" / "main" / "e" / "example" / "example_1_amd64.deb",
                binary_root / "pool" / "non-free" / "f" / "firmware" / "firmware_1_all.deb",
            )
            for path in fixture_paths:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("fixture\n", encoding="utf-8")

            with self.assertRaisesRegex(RuntimeError, "pool components: non-free"):
                build_iso._validate_live_apt_archive(binary_root, "trixie", "amd64")

    def test_materialize_workspace_stages_live_tools_in_chroot_and_medium_archive_lists(self) -> None:
        payload = minimal_plan("/tmp/out")
        payload["extra_binary_packages"] = ["hello"]
        plan = build_iso._validate_build_iso_plan(payload)

        with tempfile.TemporaryDirectory() as temp_dir:
            build_root = Path(temp_dir) / "live-build"
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
            with patch.dict(build_iso.os.environ, {"DEBIAN_USB_LIVE_ENV_PATH": str(live_env)}):
                build_iso._materialize_workspace(build_root, plan, None, [])
            chroot_packages = (build_root / "config" / "package-lists" / "base.list.chroot").read_text(
                encoding="utf-8"
            ).splitlines()
            binary_packages = (build_root / "config" / "package-lists" / "zz-user.list.binary").read_text(
                encoding="utf-8"
            ).splitlines()
            live_hooks_dir = build_root / "config" / "includes.binary" / "live" / "config-hooks"
            hook_modes = {
                hook_name: (live_hooks_dir / hook_name).stat().st_mode & 0o777
                for hook_name in ("0500-apt-live-medium.sh", "1000-network-wifi.sh")
            }
            expected_wifi_keys = {
                "LIVE_WIFI_INTERFACE",
                "LIVE_WIFI_ESSID",
                "LIVE_WIFI_SECURITY",
                "LIVE_WIFI_CIDR",
                "LIVE_WIFI_GATEWAY",
                "LIVE_WIFI_NAMESERVERS",
                "LIVE_WIFI_PASSPHRASE",
            }
            for wifi_path in (
                build_root / "config/includes.chroot_after_packages/etc/debian-usb/live.env",
                build_root / "config/includes.binary/live/debian-usb-live.env",
            ):
                self.assertTrue(wifi_path.is_file())
                self.assertEqual(wifi_path.stat().st_mode & 0o777, 0o600)
                wifi_text = wifi_path.read_text(encoding="utf-8")
                wifi_keys = {
                    line.split("=", 1)[0]
                    for line in wifi_text.splitlines()
                    if line and not line.startswith("#")
                }
                self.assertEqual(wifi_keys, expected_wifi_keys)
                self.assertNotIn("PRESEED_WIFI_PASSPHRASE", wifi_text)
            for include_name in ("includes.chroot", "includes.chroot_after_packages"):
                policy_root = build_root / "config" / include_name
                for unit in ("fwupd-refresh.service", "fwupd-refresh.timer"):
                    mask_path = policy_root / "etc" / "systemd" / "system" / unit
                    if include_name == "includes.chroot":
                        self.assertFalse(mask_path.is_symlink())
                        self.assertFalse(mask_path.exists())
                    else:
                        self.assertTrue(mask_path.is_symlink())
                        self.assertEqual(mask_path.readlink(), Path("/dev/null"))
                self.assertIn(
                    "en_US.UTF-8 UTF-8",
                    (policy_root / "etc" / "locale.gen").read_text(encoding="utf-8").splitlines(),
                )
                self.assertIn(
                    "LANG=en_US.UTF-8",
                    (policy_root / "etc" / "default" / "locale").read_text(encoding="utf-8").splitlines(),
                )
            module_policy_root = build_root / "config" / "includes.chroot_after_packages"
            module_policy_paths = [
                module_policy_root / "usr/share/initramfs-tools/modules.d/debian-usb-live",
                module_policy_root / "usr/share/initramfs-tools/conf.d/debian-usb-live",
                module_policy_root / "etc/modules-load.d/debian-usb-live.conf",
            ]
            expected_modules = "\n".join(plan["initramfs_modules"]) + "\n"
            self.assertEqual(module_policy_paths[0].read_text(encoding="utf-8"), expected_modules)
            self.assertEqual(module_policy_paths[1].read_text(encoding="utf-8"), "MODULES=most\n")
            self.assertEqual(module_policy_paths[2].read_text(encoding="utf-8"), expected_modules)
            self.assertEqual(
                [module_path.stat().st_mode & 0o777 for module_path in module_policy_paths],
                [0o644, 0o644, 0o644],
            )
            policy_hook = build_root / "config" / "hooks" / "normal" / "6000-live-runtime-policy.hook.chroot"
            subprocess.run(["sh", "-n", str(policy_hook)], check=True)
            policy_hook_text = policy_hook.read_text(encoding="utf-8")
            self.assertEqual(policy_hook.stat().st_mode & 0o777, 0o755)
            self.assertIn("rm -f -- /etc/systemd/system/timers.target.wants/fwupd-refresh.timer", policy_hook_text)
            self.assertIn("ln -sfn -- /dev/null /etc/systemd/system/fwupd-refresh.service", policy_hook_text)
            self.assertIn("ln -sfn -- /dev/null /etc/systemd/system/fwupd-refresh.timer", policy_hook_text)
            self.assertIn("locale-gen 'en_US.UTF-8'", policy_hook_text)
            self.assertIn("update-locale LANG='en_US.UTF-8' LANGUAGE='en_US:en'", policy_hook_text)

        for package in ("nvme-cli", "f2fs-tools", "btrfs-progs", "xfsprogs", "nmap"):
            self.assertIn(package, chroot_packages)
            self.assertIn(package, binary_packages)
        for package in build_iso.DEBIAN_LIVE_HOOK_PACKAGES:
            self.assertIn(package, chroot_packages)
        self.assertIn("locales", chroot_packages)
        self.assertIn("hello", binary_packages)
        self.assertEqual(
            hook_modes,
            {"0500-apt-live-medium.sh": 0o755, "1000-network-wifi.sh": 0o755},
        )

    def test_validate_build_iso_plan_rejects_installer_only_inputs_when_installer_disabled(self) -> None:
        payload = minimal_plan("/tmp/out")
        payload["installer_mode"] = "none"
        payload["include_installer_launcher"] = True
        with self.assertRaisesRegex(ValueError, "include_installer_launcher requires installer_mode"):
            build_iso._validate_build_iso_plan(payload)

    def test_validate_build_iso_plan_rejects_invalid_apt_source_lines(self) -> None:
        payload = minimal_plan("/tmp/out")
        payload["custom_apt_repo"] = "https://example.test/repo"
        payload["kernel_mode"] = "custom-apt-repo"
        payload["kernel_package_stub"] = "linux-image-custom"
        payload["kernel_flavours"] = ["amd64"]
        with self.assertRaisesRegex(ValueError, "custom_apt_repo must start with a deb source entry"):
            build_iso._validate_build_iso_plan(payload)

    def test_validate_build_iso_plan_requires_installer_inputs_for_erofs_require_policy(self) -> None:
        payload = minimal_plan("/tmp/out")
        payload["erofs_installer_component_policy"] = "require"
        with self.assertRaisesRegex(ValueError, "installer_auto_udeb_packages is required when erofs_installer_component_policy is require"):
            build_iso._validate_build_iso_plan(payload)

    def test_validate_build_iso_plan_accepts_udeb_rebuild_spec_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            spec_path = Path(temp_dir) / "udeb-spec.json"
            spec_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "rebuilds": [
                            {
                                "source_package": "foo",
                                "binary_package": "foo",
                                "udeb_package": "foo-udeb",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            payload = minimal_plan("/tmp/out")
            payload["erofs_installer_component_policy"] = "require"
            payload["installer_include_dir"] = temp_dir
            payload["udeb_rebuild_spec_path"] = str(spec_path)
            result = build_iso._validate_build_iso_plan(payload)
        self.assertEqual(result["udeb_rebuilds"][0]["udeb_package"], "foo-udeb")

    def test_validate_build_iso_plan_accepts_kernel_installer_rebuild_spec(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            spec_path = Path(temp_dir) / "kernel-udeb-spec.json"
            spec_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "rebuilds": [
                            {
                                "rebuild_kind": "linux-installer-kernel",
                                "source_package": "linux",
                                "module_targets": [
                                    {
                                        "path": "debian/installer/modules/kernel-image",
                                        "modules": ["erofs", "xxhash", "xxhash_generic"],
                                    }
                                ],
                                "pkg_list_local_entries": ["erofs-modules"],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            payload = minimal_plan("/tmp/out")
            payload["installer_include_dir"] = temp_dir
            payload["udeb_rebuild_spec_path"] = str(spec_path)
            result = build_iso._validate_build_iso_plan(payload)
        self.assertEqual(result["udeb_rebuilds"][0]["rebuild_kind"], "linux-installer-kernel")

    def test_validate_build_iso_plan_accepts_auto_installer_kernel_rebuild_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            source_iso = Path(temp_dir) / "source.iso"
            source_iso.write_text("iso", encoding="utf-8")
            payload = minimal_plan("/tmp/out")
            payload["installer_kernel_rebuild_enabled"] = True
            payload["installer_kernel_source_iso_path"] = str(source_iso)
            payload["installer_kernel_modules"] = ["erofs", "xxhash_generic"]
            payload["installer_kernel_config_entries"] = ["CONFIG_EROFS_FS=y", "CONFIG_XXHASH=m"]
            result = build_iso._validate_build_iso_plan(payload)
        self.assertTrue(result["installer_kernel_rebuild_enabled"])
        self.assertEqual(result["installer_kernel_modules"], ["erofs", "xxhash_generic"])
        self.assertEqual(result["installer_kernel_config_entries"], ["CONFIG_EROFS_FS=y", "CONFIG_XXHASH=m"])

    def test_validate_build_iso_plan_accepts_auto_udeb_package_inputs(self) -> None:
        payload = minimal_plan("/tmp/out")
        payload["installer_auto_udeb_packages"] = ["nano", "vim"]
        result = build_iso._validate_build_iso_plan(payload)
        self.assertEqual(result["installer_auto_udeb_packages"], ["nano", "vim"])

    def test_validate_build_iso_plan_merges_repo_managed_spec_and_resolves_relative_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            overlay_dir = temp_root / "overlay"
            overlay_dir.mkdir(parents=True, exist_ok=True)
            rebuild_spec_path = temp_root / "kernel-udeb-spec.json"
            rebuild_spec_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "rebuilds": [
                            {
                                "rebuild_kind": "linux-installer-kernel",
                                "source_package": "linux",
                                "source_overlay_dir": "overlay",
                                "module_targets": [
                                    {
                                        "path": "debian/installer/modules/kernel-image",
                                        "modules": ["erofs", "xxhash", "xxhash_generic"],
                                    }
                                ],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            spec_path = temp_root / "profile.json"
            spec_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "name": "test profile",
                        "distro": "debian",
                        "stage": "d-i",
                        "kind": "modules",
                        "module_names": ["erofs", "xxhash_generic"],
                        "module_alias_candidates": ["crypto_xxhash64"],
                        "kernel_config_symbols": ["CONFIG_EROFS_FS"],
                        "udeb_rebuild_spec_path": "kernel-udeb-spec.json",
                        "source_overlay_dir": "overlay",
                    }
                ),
                encoding="utf-8",
            )
            payload = minimal_plan("/tmp/out")
            payload["di_module_spec_path"] = str(spec_path)
            payload["kernel_inspection_modules"] = []
            payload["kernel_config_symbols"] = []
            payload["module_alias_candidates"] = []
            payload["udeb_rebuild_spec_path"] = ""
            payload["udeb_rebuild_source_overlay_dir"] = ""
            result = build_iso._validate_build_iso_plan(payload)
        self.assertEqual(result["udeb_rebuild_spec_path"], str(rebuild_spec_path))
        self.assertEqual(result["udeb_rebuild_source_overlay_dir"], str(overlay_dir))
        self.assertIn("erofs", result["kernel_inspection_modules"])
        self.assertIn("crypto_xxhash64", result["module_alias_candidates"])
        self.assertEqual(result["udeb_rebuilds"][0]["source_overlay_dir"], str(overlay_dir))

    def test_populate_kernel_support_cache_retries_after_apt_update(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            cache_root = Path(temp_dir) / "kernel-support"
            notes: list[str] = []
            download_attempt_count = {"count": 0}
            commands: list[list[str]] = []

            def fake_download_kernel_package_candidate(package_name: str, packages_dir: Path, *, log_file: object | None) -> Path | None:
                download_attempt_count["count"] += 1
                if download_attempt_count["count"] <= 5:
                    return None
                if package_name != "linux-image-7.0.14+deb14-amd64":
                    return None
                package_path = packages_dir / "linux-image-7.0.14+deb14-amd64_amd64.deb"
                package_path.write_text("deb", encoding="utf-8")
                return package_path

            def fake_run_optional_command(
                command: list[str],
                *,
                cwd: Path,
                log_file: object | None,
                allow_failure: bool = False,
            ) -> bool:
                commands.append(command)
                return True

            with patch("debian_usb.build_iso.os.geteuid", return_value=0), patch(
                "debian_usb.build_iso._download_kernel_package_candidate",
                side_effect=fake_download_kernel_package_candidate,
            ), patch(
                "debian_usb.build_iso._run_optional_command",
                side_effect=fake_run_optional_command,
            ):
                downloaded = build_iso._populate_kernel_support_cache(
                    "7.0.14+deb14-amd64",
                    cache_root,
                    log_file=None,
                    notes=notes,
                )

        self.assertEqual(
            downloaded,
            [str(cache_root / "packages" / "linux-image-7.0.14+deb14-amd64_amd64.deb")],
        )
        self.assertIn(["apt-get", "update"], commands)
        self.assertIn("refreshing package lists and retrying", " ".join(notes))

    def test_download_kernel_package_candidate_falls_back_to_explicit_version_when_no_candidate_exists(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            packages_dir = Path(temp_dir)
            commands: list[list[str]] = []

            def fake_run_optional_command(
                command: list[str],
                *,
                cwd: Path,
                log_file: object | None,
                allow_failure: bool = False,
            ) -> bool:
                commands.append(command)
                package_spec = command[-1]
                if package_spec == "linux-image-6.12.94+deb13-amd64":
                    return False
                if package_spec == "linux-image-6.12.94+deb13-amd64=6.12.94-1":
                    (cwd / "linux-image-6.12.94+deb13-amd64_6.12.94-1_amd64.deb").write_text("deb", encoding="utf-8")
                    return True
                return False

            with patch(
                "debian_usb.build_iso._package_versions_from_apt_cache",
                return_value=["6.12.94-1"],
            ), patch(
                "debian_usb.build_iso._run_optional_command",
                side_effect=fake_run_optional_command,
            ):
                downloaded = build_iso._download_kernel_package_candidate(
                    "linux-image-6.12.94+deb13-amd64",
                    packages_dir,
                    log_file=None,
                )

        self.assertIsNotNone(downloaded)
        self.assertEqual(downloaded.name, "linux-image-6.12.94+deb13-amd64_6.12.94-1_amd64.deb")
        self.assertIn(["apt-get", "download", "linux-image-6.12.94+deb13-amd64"], commands)
        self.assertIn(["apt-get", "download", "linux-image-6.12.94+deb13-amd64=6.12.94-1"], commands)

    def test_validate_build_iso_plan_accepts_direct_di_build_local_tree(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            payload = minimal_plan("/tmp/out")
            payload["direct_di_build_enabled"] = True
            payload["direct_di_build_source_mode"] = "local-tree"
            payload["direct_di_build_source_tree"] = temp_dir
            payload["direct_di_build_targets"] = ["build_netboot", "build_cdrom"]
            result = build_iso._validate_build_iso_plan(payload)
        self.assertEqual(result["direct_di_build_targets"], ["build_netboot", "build_cdrom"])

    def test_scan_module_tree_recognizes_builtin_and_hyphenated_module_names(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            chroot_root = Path(temp_dir)
            version_root = chroot_root / "lib/modules/6.12-test"
            version_root.mkdir(parents=True)
            (version_root / "modules.builtin").write_text(
                "kernel/drivers/usb/storage/usb-storage.ko\n"
                "kernel/drivers/md/dm-mod.ko\n"
                "kernel/crypto/xxhash64-generic.ko\n",
                encoding="utf-8",
            )

            result = build_iso._scan_module_tree(
                chroot_root,
                {"usb_storage", "dm_mod", "xxhash_generic"},
                {"xxhash64_generic"},
            )

        self.assertEqual(result["requested_builtin"], ["dm_mod", "usb_storage"])
        self.assertEqual(result["alias_builtin"], ["xxhash64_generic"])
        self.assertEqual(result["requested_missing"], ["xxhash_generic"])
        build_iso._enforce_debian_live_module_contract(
            {
                "module_root": result["module_root"],
                "requested_matches": list(build_iso.DEBIAN_LIVE_INITRAMFS_MODULES),
                "requested_builtin": [],
                "requested_missing": [],
                "alias_matches": [],
                "alias_builtin": ["xxhash64_generic"],
            }
        )

    def test_debian_live_module_contract_rejects_missing_modules(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "missing: ch341"):
            build_iso._enforce_debian_live_module_contract(
                {
                    "module_root": "/fixture/lib/modules",
                    "requested_matches": [
                        module
                        for module in build_iso.DEBIAN_LIVE_INITRAMFS_MODULES
                        if module != "ch341"
                    ],
                    "requested_builtin": [],
                    "requested_missing": ["ch341"],
                    "alias_matches": [],
                    "alias_builtin": [],
                }
            )

    def test_inspect_build_kernel_support_reads_explicit_module_tree_and_config(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            version_root = temp_root / "lib" / "modules" / "6.12.63+deb13-amd64"
            module_dir = version_root / "kernel" / "crypto"
            module_dir.mkdir(parents=True, exist_ok=True)
            (module_dir / "xxhash_generic.ko").write_text("", encoding="utf-8")
            build_dir = version_root / "build"
            build_dir.mkdir(parents=True, exist_ok=True)
            (build_dir / ".config").write_text(
                "CONFIG_EROFS_FS=m\nCONFIG_CRYPTO_XXHASH=y\n",
                encoding="utf-8",
            )
            result = build_iso.inspect_build_kernel_support(
                kernel_version="6.12.63+deb13-amd64",
                module_names=["xxhash_generic", "erofs"],
                module_alias_candidates=["xxhash_generic"],
                config_symbols=["CONFIG_EROFS_FS", "CONFIG_CRYPTO_XXHASH"],
                module_tree_dir=str(version_root),
                download_if_missing=False,
            )
        self.assertEqual(result["kernel_version"], "6.12.63+deb13-amd64")
        self.assertTrue(result["modules"][0]["found"])
        self.assertFalse(result["modules"][1]["found"])
        self.assertTrue(result["config_path"].endswith(".config"))
        self.assertEqual(result["config_symbols"][0]["value"], "m")
        self.assertEqual(result["config_symbols"][1]["value"], "y")

    def test_inspect_build_kernel_support_uses_downloaded_usr_lib_modules_cache(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            cache_root = temp_root / "kernel-support" / "6.12.94+deb13-amd64"
            version_root = cache_root / "root" / "usr" / "lib" / "modules" / "6.12.94+deb13-amd64"
            module_dir = version_root / "kernel" / "crypto"
            module_dir.mkdir(parents=True, exist_ok=True)
            (module_dir / "xxhash_generic.ko.xz").write_text("", encoding="utf-8")
            (module_dir / "lz4.ko.xz").write_text("", encoding="utf-8")
            (cache_root / "root" / "boot").mkdir(parents=True, exist_ok=True)
            (cache_root / "root" / "boot" / "config-6.12.94+deb13-amd64").write_text(
                "CONFIG_XXHASH=m\nCONFIG_CRYPTO_XXHASH=m\nCONFIG_CRYPTO_LZ4=m\nCONFIG_LZ4_COMPRESS=m\n",
                encoding="utf-8",
            )

            def fake_populate(kernel_version: str, observed_cache_root: Path, *, log_file: object | None, notes: list[str]) -> list[str]:
                self.assertEqual(kernel_version, "6.12.94+deb13-amd64")
                self.assertEqual(observed_cache_root, cache_root)
                return [str(cache_root / "packages" / "linux-image-6.12.94+deb13-amd64_amd64.deb")]

            with patch.object(build_iso, "DEFAULT_CACHE_DIR", temp_root), patch(
                "debian_usb.build_iso._populate_kernel_support_cache",
                side_effect=fake_populate,
            ):
                result = build_iso.inspect_build_kernel_support(
                    kernel_version="6.12.94+deb13-amd64",
                    module_names=["xxhash_generic", "lz4"],
                    module_alias_candidates=["xxhash_generic"],
                    config_symbols=["CONFIG_XXHASH", "CONFIG_CRYPTO_LZ4"],
                    module_tree_dir="",
                    download_if_missing=True,
                )

        self.assertTrue(result["download_attempted"])
        self.assertTrue(result["download_used"])
        self.assertTrue(result["modules"][0]["found"])
        self.assertTrue(result["modules"][1]["found"])
        self.assertIn("/usr/lib/modules/6.12.94+deb13-amd64", result["module_tree_dir"])
        self.assertEqual(result["config_symbols"][0]["value"], "m")
        self.assertEqual(result["config_symbols"][1]["value"], "m")

    def test_build_debian_iso_rejects_missing_live_modules_before_replacing_output(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            output_root = temp_root / "output"
            output_root.mkdir()
            final_iso = output_root / "custom.iso"
            final_iso.write_text("existing-output", encoding="utf-8")
            plan_path = temp_root / "plan.json"
            payload = minimal_plan(str(output_root))
            payload["installer_mode"] = "none"
            payload["rootfs_format"] = "squashfs"
            payload["filesystem_module_entries"] = []
            payload["live_tool_groups"] = []
            plan_path.write_text(json.dumps(payload), encoding="utf-8")

            def fake_run_logged(command: list[str], *, cwd: Path, log_file: object) -> None:
                if command[:2] != ["lb", "build"]:
                    return
                (cwd / "live-image-amd64.hybrid.iso").write_text("invalid-new-iso", encoding="utf-8")
                fixture_paths = (
                    cwd / "binary/dists/trixie/Release",
                    cwd / "binary/dists/trixie/main/binary-amd64/Packages.gz",
                    cwd / "binary/pool/main/e/example/example_1_amd64.deb",
                )
                for fixture_path in fixture_paths:
                    fixture_path.parent.mkdir(parents=True, exist_ok=True)
                    fixture_path.write_text("fixture\n", encoding="utf-8")

            with patch.object(build_iso, "DEFAULT_WORK_DIR", temp_root / "work"), patch.object(
                build_iso,
                "DEFAULT_STATE_DIR",
                temp_root / "state",
            ), patch.object(build_iso, "DEFAULT_LOG_DIR", temp_root / "log"), patch(
                "debian_usb.build_iso._run_logged",
                side_effect=fake_run_logged,
            ), patch(
                "debian_usb.build_iso._inspect_kernel_support",
                return_value={},
            ), patch(
                "debian_usb.build_iso.ensure_debian_build_deps",
                return_value={"changed": False},
            ):
                with self.assertRaisesRegex(RuntimeError, "Debian Live kernel module contract is incomplete"):
                    build_iso.build_debian_iso(str(plan_path))

            self.assertEqual(final_iso.read_text(encoding="utf-8"), "existing-output")

    def test_build_debian_iso_materializes_workspace_and_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            work_root = temp_root / "work"
            state_root = temp_root / "state"
            log_root = temp_root / "log"
            output_root = temp_root / "output"
            live_overlay_dir = temp_root / "live-overlay"
            binary_overlay_dir = temp_root / "binary-overlay"
            bootloader_override_dir = temp_root / "bootloaders"
            installer_overlay_dir = temp_root / "installer-overlay"
            local_udeb_dir = temp_root / "local-udebs"
            plan_path = temp_root / "plan.json"
            (live_overlay_dir / "etc").mkdir(parents=True, exist_ok=True)
            (binary_overlay_dir / "docs").mkdir(parents=True, exist_ok=True)
            (bootloader_override_dir / "grub-pc").mkdir(parents=True, exist_ok=True)
            (installer_overlay_dir / "preseed").mkdir(parents=True, exist_ok=True)
            local_udeb_dir.mkdir(parents=True, exist_ok=True)
            (live_overlay_dir / "etc" / "motd").write_text("live overlay\n", encoding="utf-8")
            (binary_overlay_dir / "docs" / "release-notes.txt").write_text("binary overlay\n", encoding="utf-8")
            (bootloader_override_dir / "grub-pc" / "config.cfg").write_text("set timeout=5\n", encoding="utf-8")
            (installer_overlay_dir / "preseed" / "iso-scan-late.sh").write_text("#!/bin/sh\n", encoding="utf-8")
            (installer_overlay_dir / "hooks" / "partconf.sh").parent.mkdir(parents=True, exist_ok=True)
            (installer_overlay_dir / "hooks" / "partconf.sh").write_text("#!/bin/sh\n", encoding="utf-8")
            (local_udeb_dir / "iso-scan-erofs.udeb").write_text("", encoding="utf-8")
            payload = minimal_plan(str(output_root))
            payload["local_udeb_dir"] = str(local_udeb_dir)
            payload["live_include_dir"] = str(live_overlay_dir)
            payload["binary_include_dir"] = str(binary_overlay_dir)
            payload["bootloader_override_dir"] = str(bootloader_override_dir)
            payload["installer_include_dir"] = str(installer_overlay_dir)
            payload["installer_distribution"] = "trixie"
            payload["installer_boot_append"] = "auto=true priority=critical"
            payload["erofs_installer_component_policy"] = "require"
            payload["erofs_installer_components"] = ["iso-scan", "partconf"]
            payload["kernel_inspection_modules"] = []
            payload["kernel_config_symbols"] = []
            plan_path.write_text(json.dumps(payload), encoding="utf-8")

            def fake_run_logged(command: list[str], *, cwd: Path, log_file: object) -> None:
                if command[:2] == ["lb", "build"]:
                    (cwd / "live-image-amd64.hybrid.iso").write_text("iso", encoding="utf-8")
                    apt_release = cwd / "binary" / "dists" / "trixie" / "Release"
                    apt_index = cwd / "binary" / "dists" / "trixie" / "main" / "binary-amd64" / "Packages.gz"
                    apt_package = cwd / "binary" / "pool" / "main" / "e" / "example" / "example_1_amd64.deb"
                    for path in (apt_release, apt_index, apt_package):
                        path.parent.mkdir(parents=True, exist_ok=True)
                        path.write_text("fixture\n", encoding="utf-8")
                    module_dir = cwd / "chroot" / "lib" / "modules" / "6.1.0-test" / "kernel"
                    (module_dir / "fs" / "erofs").mkdir(parents=True, exist_ok=True)
                    (module_dir / "fs" / "erofs" / "erofs.ko").write_text("", encoding="utf-8")
                    (module_dir / "lib").mkdir(parents=True, exist_ok=True)
                    (module_dir / "lib" / "xxhash.ko").write_text("", encoding="utf-8")
                    (module_dir / "crypto").mkdir(parents=True, exist_ok=True)
                    (module_dir / "crypto" / "xxhash_generic.ko").write_text("", encoding="utf-8")
                    builtin_modules = [
                        module
                        for module in build_iso.DEBIAN_LIVE_INITRAMFS_MODULES
                        if module not in {"xxhash", "xxhash_generic"}
                    ]
                    (module_dir.parent / "modules.builtin").write_text(
                        "".join(
                            f"kernel/builtin/{module.replace('_', '-')}.ko\n"
                            for module in builtin_modules
                        ),
                        encoding="utf-8",
                    )

            with patch.object(build_iso, "DEFAULT_WORK_DIR", work_root):
                with patch.object(build_iso, "DEFAULT_STATE_DIR", state_root):
                    with patch.object(build_iso, "DEFAULT_LOG_DIR", log_root):
                        with patch("debian_usb.build_iso._run_logged", side_effect=fake_run_logged):
                            with patch("debian_usb.build_iso.ensure_debian_build_deps", return_value={"changed": False}):
                                with patch("debian_usb.build_iso._dpkg_deb_field", return_value="iso-scan-erofs"):
                                    with patch("debian_usb.build_iso._create_local_udeb_repo", return_value=None):
                                        result = build_iso.build_debian_iso(str(plan_path))

            manifest = json.loads(Path(result["manifest_path"]).read_text(encoding="utf-8"))
            hook_path = Path(result["workspace_dir"]) / "config" / "hooks" / "normal" / "9990-erofs-rootfs.hook.binary"
            filesystem_module_path = Path(result["workspace_dir"]) / "config" / "includes.binary" / "live" / "filesystem.module"
            live_overlay_target = Path(result["workspace_dir"]) / "config" / "includes.chroot_after_packages" / "etc" / "motd"
            binary_overlay_target = Path(result["workspace_dir"]) / "config" / "includes.binary" / "docs" / "release-notes.txt"
            bootloader_override_target = Path(result["workspace_dir"]) / "config" / "bootloaders" / "grub-pc" / "config.cfg"
            installer_audit_path = Path(result["installer_audit_path"])
            installer_audit = json.loads(installer_audit_path.read_text(encoding="utf-8"))
            self.assertTrue(Path(result["iso_path"]).is_file())
            self.assertTrue(hook_path.is_file())
            self.assertTrue(filesystem_module_path.is_file())
            self.assertTrue(live_overlay_target.is_file())
            self.assertTrue(binary_overlay_target.is_file())
            self.assertTrue(bootloader_override_target.is_file())
            self.assertTrue(installer_audit_path.is_file())
            self.assertEqual(manifest["rootfs_format_actual"], "erofs")
            self.assertEqual(manifest["live_apt_archive"]["release_path"], "/dists/trixie/Release")
            self.assertIn("erofs", manifest["resolved_modules"]["requested_matches"])
            self.assertIn("usb_storage", manifest["resolved_modules"]["requested_builtin"])
            self.assertEqual(manifest["resolved_modules"]["requested_missing"], [])
            self.assertEqual(filesystem_module_path.read_text(encoding="utf-8").strip().splitlines(), ["filesystem.squashfs"])
            self.assertEqual(installer_audit["status"], "ok")
            self.assertEqual(installer_audit["local_udeb_count"], 1)

    def test_prepare_udeb_packaging_adds_udeb_stanza_and_library_rules(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            source_root = Path(temp_dir)
            debian_dir = source_root / "debian"
            debian_dir.mkdir(parents=True, exist_ok=True)
            (debian_dir / "control").write_text(
                "\n".join(
                    [
                        "Source: foo",
                        "Section: misc",
                        "Priority: optional",
                        "Maintainer: Test <test@example.invalid>",
                        "",
                        "Package: libfoo1",
                        "Architecture: any",
                        "Depends: ${shlibs:Depends}, ${misc:Depends}",
                        "Description: foo runtime library",
                        " original long description.",
                        "",
                    ]
                ),
                encoding="utf-8",
            )
            (debian_dir / "rules").write_text("#!/usr/bin/make -f\n%:\n\tdh $@\n", encoding="utf-8")
            (debian_dir / "libfoo1.install").write_text("usr/lib/*\n", encoding="utf-8")

            build_iso._prepare_udeb_packaging(
                source_root,
                {
                    "source_package": "foo",
                    "binary_package": "libfoo1",
                    "udeb_package": "libfoo1-udeb",
                    "package_role": "library",
                    "installer_menu_item": "",
                    "description": "",
                    "long_description": [],
                    "build_dep_packages": [],
                    "depends": [],
                    "provides": [],
                    "conflicts": [],
                    "replaces": [],
                    "source_overlay_dir": "",
                    "copy_install_manifest_from": "libfoo1",
                    "library_shlibs_udeb": "libfoo1-udeb",
                    "architecture": "",
                },
            )

            control_text = (debian_dir / "control").read_text(encoding="utf-8")
            rules_text = (debian_dir / "rules").read_text(encoding="utf-8")
            self.assertIn("Package: libfoo1-udeb", control_text)
            self.assertIn("Package-Type: udeb", control_text)
            self.assertIn("Section: debian-installer", control_text)
            self.assertIn("override_dh_makeshlibs:", rules_text)
            self.assertIn("--add-udeb=libfoo1-udeb", rules_text)
            self.assertTrue((debian_dir / "libfoo1-udeb.install").is_file())

    def test_prepare_linux_installer_kernel_source_updates_module_targets(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            source_root = Path(temp_dir)
            module_target = source_root / "debian" / "installer" / "modules" / "kernel-image"
            module_target.parent.mkdir(parents=True, exist_ok=True)
            module_target.write_text("squashfs\n", encoding="utf-8")
            package_list_path = source_root / "debian" / "installer" / "package-list"
            package_list_path.write_text("Package: kernel-image\n", encoding="utf-8")

            build_iso._prepare_linux_installer_kernel_source(
                source_root,
                {
                    "module_targets": [
                        {
                            "path": "debian/installer/modules/kernel-image",
                            "modules": ["erofs", "xxhash", "xxhash_generic"],
                            "merge_strategy": "append-unique",
                        }
                    ],
                    "package_list_append_text": "Package: erofs-modules\nKernel-Version: yes",
                },
            )

            self.assertEqual(
                module_target.read_text(encoding="utf-8").strip().splitlines(),
                ["squashfs", "erofs", "xxhash", "xxhash_generic"],
            )
            self.assertIn("Package: erofs-modules", package_list_path.read_text(encoding="utf-8"))

    def test_prepare_linux_installer_kernel_source_applies_kernel_config_entries(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            source_root = Path(temp_dir)
            config_path = source_root / "debian" / "config" / "amd64" / "none" / "config"
            config_path.parent.mkdir(parents=True, exist_ok=True)
            config_path.write_text(
                "\n".join(
                    [
                        "CONFIG_OLD_FEATURE=y",
                        "# CONFIG_EROFS_FS is not set",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            build_iso._prepare_linux_installer_kernel_source(
                source_root,
                {
                    "module_targets": [],
                    "package_list_append_text": "",
                    "kernel_config_entries": ["CONFIG_EROFS_FS=y", "CONFIG_XXHASH=m"],
                    "target_architecture": "amd64",
                },
            )

            config_text = config_path.read_text(encoding="utf-8")
            self.assertIn("CONFIG_EROFS_FS=y", config_text)
            self.assertIn("CONFIG_XXHASH=m", config_text)
            self.assertNotIn("# CONFIG_EROFS_FS is not set", config_text)

    def test_apply_auto_installer_kernel_rebuild_generates_linux_rebuild_entry(self) -> None:
        plan = minimal_plan("/tmp/out")
        plan["installer_kernel_rebuild_enabled"] = True
        plan["installer_kernel_source_iso_path"] = "/tmp/source.iso"
        plan["installer_kernel_modules"] = ["erofs", "xxhash_generic"]
        plan["installer_kernel_config_entries"] = ["CONFIG_EROFS_FS=y", "CONFIG_XXHASH=m"]
        plan["udeb_rebuilds"] = []

        with patch(
            "debian_usb.build_iso._resolve_auto_installer_kernel_rebuild_metadata",
            return_value={
                "source_iso_path": "/tmp/source.iso",
                "installer_initrd_path": "/install.amd/initrd.gz",
                "kernel_version": "6.12.8-amd64",
                "installer_kernel_package": "kernel-image-6.12.8-amd64-di",
                "source_package": "linux",
                "source_version": "6.12.8-1",
                "architecture": "amd64",
            },
        ):
            build_iso._apply_auto_installer_kernel_rebuild(plan)

        self.assertEqual(plan["kernel_target_version"], "6.12.8-amd64")
        self.assertTrue(plan["kernel_download_if_missing"])
        self.assertEqual(plan["udeb_rebuilds"][0]["source_version"], "6.12.8-1")
        self.assertEqual(plan["udeb_rebuilds"][0]["module_targets"][0]["path"], "debian/installer/modules/kernel-image")
        self.assertEqual(plan["udeb_rebuilds"][0]["kernel_config_entries"], ["CONFIG_EROFS_FS=y", "CONFIG_XXHASH=m"])
        self.assertIn("CONFIG_XXHASH", plan["kernel_config_symbols"])

    def test_apply_auto_source_package_udeb_rebuilds_generates_source_package_entries(self) -> None:
        plan = minimal_plan("/tmp/out")
        plan["installer_auto_udeb_packages"] = ["nano", "vim"]
        plan["udeb_rebuilds"] = []

        with patch(
            "debian_usb.build_iso._resolve_binary_package_udeb_rebuild_metadata",
            side_effect=[
                {
                    "binary_package": "nano",
                    "udeb_package": "nano-udeb",
                    "source_package": "nano",
                    "source_version": "8.4-1",
                },
                {
                    "binary_package": "vim",
                    "udeb_package": "vim-udeb",
                    "source_package": "vim",
                    "source_version": "2:9.1.1234-1",
                },
            ],
        ):
            build_iso._apply_auto_source_package_udeb_rebuilds(plan)

        self.assertEqual(len(plan["udeb_rebuilds"]), 2)
        self.assertEqual(plan["udeb_rebuilds"][0]["binary_package"], "nano")
        self.assertEqual(plan["udeb_rebuilds"][0]["udeb_package"], "nano-udeb")
        self.assertEqual(plan["udeb_rebuilds"][0]["source_version"], "8.4-1")
        self.assertEqual(plan["udeb_rebuilds"][1]["binary_package"], "vim")
        self.assertEqual(plan["udeb_rebuilds"][1]["source_version"], "2:9.1.1234-1")

    def test_run_direct_di_build_driver_uses_exported_workspace_and_snapshots_dest(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            source_tree = temp_root / "d-i-src"
            build_dir = source_tree / "build"
            build_dir.mkdir(parents=True, exist_ok=True)
            export_dir = temp_root / "export"
            (export_dir / "localudebs").mkdir(parents=True, exist_ok=True)
            (export_dir / "pkg-lists").mkdir(parents=True, exist_ok=True)
            (export_dir / "repo").mkdir(parents=True, exist_ok=True)
            (export_dir / "localudebs" / "foo.udeb").write_text("", encoding="utf-8")
            (export_dir / "pkg-lists" / "local").write_text("foo-udeb\n", encoding="utf-8")
            (export_dir / "sources.list.udeb.local").write_text("deb [trusted=yes] file:/tmp ./\n", encoding="utf-8")
            manifest_path = export_dir / "manifest.json"
            manifest_path.write_text("{}", encoding="utf-8")

            plan = {
                "direct_di_build_source_mode": "local-tree",
                "direct_di_build_source_tree": str(source_tree),
                "direct_di_build_targets": ["build_netboot"],
                "direct_di_build_dep_packages": [],
                "direct_di_build_reallyclean_before": True,
                "direct_di_build_reallyclean_after": True,
            }
            installer_export = {
                "localudebs_dir": str(export_dir / "localudebs"),
                "pkg_list_local_path": str(export_dir / "pkg-lists" / "local"),
                "sources_list_udeb_local_path": str(export_dir / "sources.list.udeb.local"),
                "manifest_path": str(manifest_path),
            }

            def fake_run_logged(command: list[str], *, cwd: Path, log_file: object) -> None:
                if command == ["fakeroot", "make", "build_netboot"]:
                    (cwd / "dest").mkdir(parents=True, exist_ok=True)
                    (cwd / "dest" / "mini.iso").write_text("iso", encoding="utf-8")
                    (cwd / "tmp" / "amd64" / "tree" / "lib" / "modules").mkdir(parents=True, exist_ok=True)
                    (cwd / "tmp" / "amd64" / "tree" / "lib" / "modules" / "erofs.ko").write_text("", encoding="utf-8")

            with (temp_root / "driver.log").open("w", encoding="utf-8") as log_file:
                with patch("debian_usb.build_iso._run_logged", side_effect=fake_run_logged):
                    result = build_iso._run_direct_di_build_driver(
                        plan=plan,
                        installer_export=installer_export,
                        workspace_dir=temp_root / "workspace",
                        state_dir=temp_root / "state",
                        log_file=log_file,
                    )

            self.assertTrue((Path(result["dest_snapshot_dir"]) / "mini.iso").is_file())
            self.assertIn("tmp/amd64/tree/lib/modules/erofs.ko", result["inspection"]["tmp_matches"])


if __name__ == "__main__":
    unittest.main()
