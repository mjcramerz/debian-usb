from __future__ import annotations

from contextlib import nullcontext
from pathlib import Path
import os
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from debian_usb.boot_parse import BootEntry
from debian_usb import rebuild_iso


def minimal_plan(source_iso_path: str) -> dict[str, object]:
    return {
        "schema_version": 1,
        "distro": "debian",
        "source_iso_path": source_iso_path,
        "output_dir": "/tmp/out",
        "image_name": "source-di-kmods.iso",
        "scope": "d-i",
        "action": "add-kernel-modules",
        "architecture": "amd64",
        "installer_kernel_modules": ["erofs", "xxhash_generic"],
        "installer_udeb_packages": [],
        "live_deb_packages": [],
        "target_kernel_version": "",
    }


def write_apt_archive_fixture(root: Path, suite: str, components: list[str]) -> None:
    suite_root = root / "dists" / suite
    suite_root.mkdir(parents=True, exist_ok=True)
    (suite_root / "Release").write_text(f"Suite: {suite}\n", encoding="utf-8")
    architecture = rebuild_iso._host_architecture()
    for component in components:
        index_path = suite_root / component / f"binary-{architecture}" / "Packages.gz"
        index_path.parent.mkdir(parents=True, exist_ok=True)
        index_path.write_text("Package: fixture\n", encoding="utf-8")


class FakeSource:
    display_path = "/tmp/source.iso"
    source_type = "iso"
    volume_id = "DEBIAN_TEST"

    def exists(self, member_path: str) -> bool:
        return member_path in {"/live/filesystem.squashfs", "/install.amd/initrd.gz"}

    def has_member_prefix(self, member_path: str) -> bool:
        return member_path == "/EFI"


class FakeEntry:
    def __init__(self, *, title: str, kernel_path: str, initrd_path: str, kind: str = "live") -> None:
        self.title = title
        self.kernel_path = kernel_path
        self.initrd_path = initrd_path
        self.kind = kind


class RebuildISOTests(unittest.TestCase):
    def test_squashfs_processor_count_uses_half_available_affinity(self) -> None:
        expectations = {
            1: 1,
            2: 1,
            3: 1,
            4: 2,
            12: 6,
        }
        for available, expected in expectations.items():
            with self.subTest(available=available):
                with patch("debian_usb.rebuild_iso.os.sched_getaffinity", return_value=set(range(available))):
                    self.assertEqual(rebuild_iso._squashfs_processor_count(), expected)

    def test_remaster_storage_capacity_aggregates_same_filesystem_requirements(self) -> None:
        snapshot = {
            "anchor": Path("/data"),
            "device": 99,
            "free_bytes": 100,
            "free_inodes": 10,
        }
        with patch("debian_usb.rebuild_iso._storage_snapshot", return_value=snapshot):
            with self.assertRaisesRegex(RuntimeError, "insufficient storage.*before ISO extraction"):
                rebuild_iso._require_remaster_storage_capacity(
                    [
                        ("workspace", Path("/data/work"), 80, 8),
                        ("output", Path("/data/output"), 40, 4),
                    ],
                    phase="before ISO extraction",
                )

    def test_remaster_storage_capacity_rejects_inode_exhaustion(self) -> None:
        snapshot = {
            "anchor": Path("/data"),
            "device": 99,
            "free_bytes": 10 * rebuild_iso.GIB,
            "free_inodes": 5,
        }
        with patch("debian_usb.rebuild_iso._storage_snapshot", return_value=snapshot):
            with self.assertRaisesRegex(RuntimeError, "6 free inodes.*available.*5 free inodes"):
                rebuild_iso._require_remaster_storage_capacity(
                    [("workspace", Path("/data/work"), rebuild_iso.MIB, 6)],
                    phase="before squashfs extraction",
                )

    def test_squashfs_expanded_usage_uses_selected_processors(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            rootfs_path = Path(temp_dir) / "filesystem.squashfs"
            rootfs_path.write_bytes(b"1234567")
            completed = subprocess.CompletedProcess(
                ["unsquashfs"],
                0,
                "\n".join(
                    [
                        "drwxr-xr-x 0/0 10 2026-09-03 12:00 squashfs-root",
                        "-rw-r--r-- 0/0 25 2026-09-03 12:00 squashfs-root/file",
                        "brw-r--r-- 0/0 1, 3 2026-09-03 12:00 squashfs-root/device",
                    ]
                ),
                "",
            )
            with patch("debian_usb.rebuild_iso.subprocess.run", return_value=completed) as run:
                expanded_bytes, entries = rebuild_iso._squashfs_expanded_usage(rootfs_path, 3)

        self.assertEqual(expanded_bytes, 35)
        self.assertEqual(entries, 3)
        self.assertEqual(
            run.call_args.args[0],
            ["unsquashfs", "-processors", "3", "-lln", str(rootfs_path)],
        )

    def test_live_tools_workspace_cleanup_refuses_tree_with_live_mount(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            work_root = Path(temp_dir) / "work"
            workspace_dir = work_root / "remaster-live-tools" / "run-id"
            workspace_dir.mkdir(parents=True)
            with patch.object(rebuild_iso, "DEFAULT_WORK_DIR", work_root), patch(
                "debian_usb.rebuild_iso._mount_targets_under_workspace",
                return_value=[workspace_dir / "proc"],
            ), patch("debian_usb.rebuild_iso.shutil.rmtree") as remove_tree:
                warning = rebuild_iso._cleanup_live_tools_workspace(workspace_dir)

            self.assertIn("mounts remain", warning)
            self.assertTrue(workspace_dir.is_dir())
            remove_tree.assert_not_called()

    def test_live_initrd_workspace_cleanup_removes_read_only_iso_tree(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            work_root = Path(temp_dir) / "work"
            workspace_dir = work_root / "remaster-live-initrd" / "run-id"
            restricted_dir = workspace_dir / "iso-root/pool-udeb/main/z/zlib"
            restricted_dir.mkdir(parents=True)
            (restricted_dir / "payload").write_bytes(b"fixture")
            for directory in (restricted_dir, restricted_dir.parent, workspace_dir / "iso-root"):
                directory.chmod(0o555)

            with patch.object(rebuild_iso, "DEFAULT_WORK_DIR", work_root), patch(
                "debian_usb.rebuild_iso._mount_targets_under_workspace",
                return_value=[],
            ):
                warning = rebuild_iso._cleanup_live_initrd_workspace(workspace_dir)

            self.assertEqual(warning, "")
            self.assertFalse(workspace_dir.exists())

    def test_extracted_iso_root_access_repairs_private_root_for_apt(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            iso_root = Path(temp_dir) / "iso-root"
            packages_dir = iso_root / "dists/trixie/main/binary-amd64"
            packages_dir.mkdir(parents=True)
            packages_path = packages_dir / "Packages"
            packages_path.write_text("Package: fixture\n", encoding="utf-8")
            packages_path.chmod(0o444)
            packages_dir.chmod(0o555)
            iso_root.chmod(0o700)

            rebuild_iso._ensure_extracted_iso_root_access(iso_root)

            self.assertEqual(iso_root.stat().st_mode & 0o777, 0o755)
            self.assertEqual(packages_dir.stat().st_mode & 0o777, 0o555)
            self.assertEqual(packages_path.stat().st_mode & 0o777, 0o444)

    def test_finalize_rebuild_output_access_hands_managed_output_to_sudo_user(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            managed_root = root / "iso/rebuild"
            output_root = managed_root / "live-initrd/run-id"
            managed_root.mkdir(parents=True)
            managed_root.chmod(0o700)
            created_directories = rebuild_iso._missing_output_directories(output_root)
            output_root.mkdir(parents=True)
            for directory in (output_root.parent, output_root):
                directory.chmod(0o700)
            final_iso = output_root / "debian-live-initrd-overlay.iso"
            final_iso.write_bytes(b"rebuilt-iso")
            final_iso.chmod(0o644)

            with patch.object(rebuild_iso, "DEFAULT_REBUILD_OUTPUT_DIR", managed_root), patch(
                "debian_usb.rebuild_iso.os.geteuid",
                return_value=0,
            ), patch.dict(
                os.environ,
                {"SUDO_UID": "1000", "SUDO_GID": "1000"},
                clear=False,
            ), patch("debian_usb.rebuild_iso.os.chown") as chown:
                rebuild_iso._finalize_rebuild_output_access(final_iso, created_directories)

            self.assertEqual(final_iso.stat().st_mode & 0o7777, 0o600)
            for directory in (managed_root, output_root.parent, output_root):
                self.assertEqual(directory.stat().st_mode & 0o7777, 0o2770)
            self.assertTrue(
                any(
                    call.args == (final_iso, 1000, 1000)
                    and call.kwargs == {"follow_symlinks": False}
                    for call in chown.call_args_list
                )
            )
            for directory in (managed_root, output_root.parent, output_root):
                self.assertTrue(
                    any(
                        call.args == (directory, -1, 1000)
                        and call.kwargs == {"follow_symlinks": False}
                        for call in chown.call_args_list
                    )
                )

    def test_finalize_rebuild_output_access_preserves_existing_custom_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            custom_output = root / "custom-output"
            custom_output.mkdir()
            custom_output.chmod(0o750)
            final_iso = custom_output / "rebuilt.iso"
            final_iso.write_bytes(b"rebuilt-iso")
            final_iso.chmod(0o644)

            with patch.object(
                rebuild_iso,
                "DEFAULT_REBUILD_OUTPUT_DIR",
                root / "managed-rebuild",
            ), patch(
                "debian_usb.rebuild_iso.os.geteuid",
                return_value=0,
            ), patch.dict(
                os.environ,
                {"SUDO_UID": "1000", "SUDO_GID": "1000"},
                clear=False,
            ), patch("debian_usb.rebuild_iso.os.chown") as chown:
                rebuild_iso._finalize_rebuild_output_access(final_iso)

            self.assertEqual(final_iso.stat().st_mode & 0o7777, 0o600)
            self.assertEqual(custom_output.stat().st_mode & 0o7777, 0o750)
            self.assertEqual(chown.call_count, 1)
            self.assertEqual(chown.call_args.args, (final_iso, 1000, 1000))
            self.assertEqual(chown.call_args.kwargs, {"follow_symlinks": False})

    def test_temporary_chroot_service_policy_blocks_starts_without_persisting(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            live_root = Path(temp_dir)
            policy_path = live_root / "usr" / "sbin" / "policy-rc.d"

            with rebuild_iso._temporary_chroot_service_policy(live_root):
                self.assertEqual(policy_path.read_text(encoding="utf-8"), "#!/bin/sh\nexit 101\n")
                self.assertEqual(policy_path.stat().st_mode & 0o777, 0o755)

            self.assertFalse(policy_path.exists())

    def test_temporary_chroot_service_policy_restores_existing_policy(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            live_root = Path(temp_dir)
            policy_path = live_root / "usr" / "sbin" / "policy-rc.d"
            policy_path.parent.mkdir(parents=True)
            policy_path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            policy_path.chmod(0o700)

            with rebuild_iso._temporary_chroot_service_policy(live_root):
                self.assertEqual(policy_path.read_text(encoding="utf-8"), "#!/bin/sh\nexit 101\n")

            self.assertEqual(policy_path.read_text(encoding="utf-8"), "#!/bin/sh\nexit 0\n")
            self.assertEqual(policy_path.stat().st_mode & 0o777, 0o700)

    def test_chroot_apt_commands_force_builtin_utf8_locale(self) -> None:
        command = rebuild_iso._apt_get_chroot_command(
            {"list_path": "/tmp/sources.list", "parts_dir": "/tmp/sources.list.d"},
            "update",
        )

        self.assertEqual(
            command[:5],
            [
                "env",
                "DEBIAN_FRONTEND=noninteractive",
                "LANG=C.UTF-8",
                "LC_ALL=C.UTF-8",
                "apt-get",
            ],
        )

    def test_validate_rebuild_plan_accepts_di_kernel_modules(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            source_iso = Path(temp_dir) / "source.iso"
            source_iso.write_text("iso", encoding="utf-8")
            result = rebuild_iso._validate_rebuild_plan(minimal_plan(str(source_iso)))
        self.assertEqual(result["scope"], "d-i")
        self.assertEqual(result["action"], "add-kernel-modules")
        self.assertEqual(result["installer_kernel_modules"], ["erofs", "xxhash_generic"])

    def test_validate_rebuild_plan_rejects_scope_action_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            source_iso = Path(temp_dir) / "source.iso"
            source_iso.write_text("iso", encoding="utf-8")
            payload = minimal_plan(str(source_iso))
            payload["scope"] = "live-host"
            with self.assertRaisesRegex(ValueError, "not valid for scope live-host"):
                rebuild_iso._validate_rebuild_plan(payload)

    def test_validate_rebuild_plan_requires_target_kernel_version_for_kernel_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            source_iso = Path(temp_dir) / "source.iso"
            source_iso.write_text("iso", encoding="utf-8")
            payload = minimal_plan(str(source_iso))
            payload["scope"] = "live-host"
            payload["action"] = "replace-kernel"
            payload["installer_kernel_modules"] = []
            with self.assertRaisesRegex(ValueError, "target_kernel_version is required"):
                rebuild_iso._validate_rebuild_plan(payload)

    @patch("debian_usb.rebuild_iso._detect_kernel_version_from_initrd", return_value="6.12.63+deb13-amd64")
    @patch("debian_usb.rebuild_iso._select_text_installer_entry")
    @patch("debian_usb.rebuild_iso._select_entry")
    @patch("debian_usb.rebuild_iso._media_class", return_value="hybrid")
    @patch("debian_usb.rebuild_iso._find_boot_entries")
    @patch("debian_usb.rebuild_iso.open_source", return_value=FakeSource())
    def test_inspect_debian_rebuild_source_reports_detected_paths(
        self,
        _open_source,
        find_boot_entries,
        _media_class,
        select_entry,
        select_installer_entry,
        _detect_kernel_version,
    ) -> None:
        find_boot_entries.return_value = [
            BootEntry(
                title="Live system",
                kernel_path="/live/vmlinuz",
                initrd_path="/live/initrd.img",
                kernel_args="boot=live quiet",
                source="/isolinux/live.cfg",
                kind="live",
            ),
            BootEntry(
                title="Live system (GRUB)",
                kernel_path="/live/vmlinuz-6.12.63+deb13-amd64",
                initrd_path="/live/initrd.img-6.12.63+deb13-amd64",
                kernel_args="boot=live quiet",
                source="/boot/grub/grub.cfg",
                kind="live",
            ),
            BootEntry(
                title="Install",
                kernel_path="/install.amd/vmlinuz",
                initrd_path="/install.amd/initrd.gz",
                kernel_args="auto=true",
                source="/boot/grub/install.cfg",
                kind="installer",
            ),
        ]
        select_entry.return_value = FakeEntry(
            title="Live system",
            kernel_path="/live/vmlinuz",
            initrd_path="/live/initrd.img",
        )
        select_installer_entry.return_value = FakeEntry(
            title="Install",
            kernel_path="/install.amd/vmlinuz",
            initrd_path="/install.amd/initrd.gz",
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            source_iso = Path(temp_dir) / "source.iso"
            source_iso.write_text("iso", encoding="utf-8")
            result = rebuild_iso.inspect_debian_rebuild_source(str(source_iso))

        self.assertEqual(result["architecture"], "amd64")
        self.assertEqual(result["installer_kernel_version"], "6.12.63+deb13-amd64")
        self.assertEqual(
            result["live_kernel_paths"],
            ["/live/vmlinuz", "/live/vmlinuz-6.12.63+deb13-amd64"],
        )
        self.assertEqual(
            result["live_initrd_paths"],
            ["/live/initrd.img", "/live/initrd.img-6.12.63+deb13-amd64"],
        )
        self.assertEqual(result["live_rootfs_path"], "/live/filesystem.squashfs")
        self.assertEqual(result["firmware"], ["uefi"])

    def test_apply_live_persistence_remaster_installs_packages_and_refreshes_initrd(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            iso_root = root / "iso-root"
            workspace_dir = root / "workspace"
            extracted_rootfs = iso_root / "live" / "filesystem.squashfs"
            extracted_initrd = iso_root / "live" / "initrd.img"
            versioned_initrd = iso_root / "live" / "initrd.img-6.1.0"
            live_root = workspace_dir / "live-root"
            rebuilt_initrd = live_root / "boot" / "initrd.img-6.1.0"
            boot_config = iso_root / "boot" / "grub" / "grub.cfg"
            extracted_rootfs.parent.mkdir(parents=True, exist_ok=True)
            extracted_initrd.parent.mkdir(parents=True, exist_ok=True)
            versioned_initrd.parent.mkdir(parents=True, exist_ok=True)
            rebuilt_initrd.parent.mkdir(parents=True, exist_ok=True)
            boot_config.parent.mkdir(parents=True, exist_ok=True)
            extracted_rootfs.write_text("rootfs", encoding="utf-8")
            extracted_initrd.write_text("old-initrd", encoding="utf-8")
            versioned_initrd.write_text("old-versioned-initrd", encoding="utf-8")
            rebuilt_initrd.write_text("new-initrd", encoding="utf-8")
            boot_config.write_text(
                "menuentry 'Live' {\n  linux /live/vmlinuz boot=live quiet ---\n}\n",
                encoding="utf-8",
            )
            expected_packages = rebuild_iso._dedupe(
                [
                    *rebuild_iso.DEBIAN_LIVE_HOOK_PACKAGES,
                    *rebuild_iso.LIVE_PERSISTENCE_SUPPORT_PACKAGES,
                ]
            )

            run_logged_calls: list[list[str]] = []
            run_in_chroot_calls: list[list[str]] = []
            install_calls: list[list[str]] = []
            install_roots: list[Path | None] = []

            def fake_run_logged(command: list[str], **_: object) -> None:
                run_logged_calls.append(command)

            def fake_run_in_chroot(_live_root: Path, command: list[str], _log_file: object) -> None:
                run_in_chroot_calls.append(command)

            def fake_install_packages(
                _live_root: Path,
                packages: list[str],
                _log_file: object,
                *,
                apt_source_root: Path | None = None,
            ) -> None:
                install_calls.append(packages)
                install_roots.append(apt_source_root)

            with patch("debian_usb.rebuild_iso._squashfs_processor_count", return_value=2), patch(
                "debian_usb.rebuild_iso._run_logged",
                side_effect=fake_run_logged,
            ):
                with patch("debian_usb.rebuild_iso._install_packages_in_chroot", side_effect=fake_install_packages):
                    with patch("debian_usb.rebuild_iso._run_in_chroot", side_effect=fake_run_in_chroot):
                        with patch("debian_usb.rebuild_iso._mounted_chroot", return_value=nullcontext()):
                            with patch("debian_usb.rebuild_iso._detect_live_root_kernel_version", return_value="6.1.0"):
                                with patch("debian_usb.rebuild_iso._resolve_live_root_initrd_file", return_value=rebuilt_initrd):
                                    with patch("debian_usb.rebuild_iso._refresh_live_metadata") as refresh_metadata:
                                        with patch("debian_usb.rebuild_iso._squashfs_compression", return_value="xz"):
                                            modified = rebuild_iso._apply_live_persistence_remaster(
                                                live_rootfs_path="/live/filesystem.squashfs",
                                                live_initrd_path="/live/initrd.img",
                                                live_initrd_paths=["/live/initrd.img", "/live/initrd.img-6.1.0"],
                                                live_kernel_path="/live/vmlinuz",
                                                packages=expected_packages,
                                                profile="debian",
                                                iso_root=iso_root,
                                                workspace_dir=workspace_dir,
                                                log_file=None,
                                            )

            self.assertEqual(
                modified,
                [
                    "/live/filesystem.squashfs",
                    "/live/initrd.img",
                    "/live/initrd.img-6.1.0",
                    "/live/config-hooks/0500-apt-live-medium.sh",
                    "/live/config-hooks/1000-network-wifi.sh",
                    "/live/debian-usb-live.env",
                    "/boot/grub/grub.cfg",
                ],
            )
            self.assertEqual(install_calls, [expected_packages])
            self.assertEqual(install_roots, [iso_root])
            self.assertEqual(
                (live_root / "usr/share/initramfs-tools/conf.d/debian-usb-live").read_text(encoding="utf-8"),
                "MODULES=most\n",
            )
            self.assertEqual(
                (live_root / "etc/modules-load.d/debian-usb-live.conf").read_text(encoding="utf-8").splitlines(),
                list(rebuild_iso.DEBIAN_LIVE_INITRAMFS_MODULES),
            )
            self.assertIn(
                [
                    "env",
                    "DEBIAN_FRONTEND=noninteractive",
                    "LANG=C.UTF-8",
                    "LC_ALL=C.UTF-8",
                    "update-initramfs",
                    "-u",
                    "-k",
                    "all",
                ],
                run_in_chroot_calls,
            )
            self.assertEqual(extracted_initrd.read_text(encoding="utf-8"), "new-initrd")
            self.assertEqual(versioned_initrd.read_text(encoding="utf-8"), "new-initrd")
            for hook_name in ("0500-apt-live-medium.sh", "1000-network-wifi.sh"):
                hook_path = iso_root / "live" / "config-hooks" / hook_name
                self.assertTrue(hook_path.is_file())
                self.assertEqual(hook_path.stat().st_mode & 0o777, 0o755)
            for wifi_path in (
                live_root / "etc" / "debian-usb" / "live.env",
                iso_root / "live" / "debian-usb-live.env",
            ):
                self.assertTrue(wifi_path.is_file())
                self.assertEqual(wifi_path.stat().st_mode & 0o777, 0o600)
                self.assertIn(
                    "LIVE_WIFI_PASSPHRASE",
                    {
                        line.split("=", 1)[0]
                        for line in wifi_path.read_text(encoding="utf-8").splitlines()
                        if line and not line.startswith("#")
                    },
                )
            boot_text = boot_config.read_text(encoding="utf-8")
            self.assertIn("live-config.hooks=medium", boot_text)
            self.assertNotIn("LIVE_WIFI_", boot_text)
            refresh_metadata.assert_called_once_with(live_root, iso_root, "/live/filesystem.squashfs")
            self.assertIn(
                ["unsquashfs", "-processors", "2", "-d", str(live_root), str(extracted_rootfs)],
                run_logged_calls,
            )
            self.assertIn(
                [
                    "mksquashfs",
                    str(live_root),
                    str(extracted_rootfs),
                    "-noappend",
                    "-comp",
                    "xz",
                    "-processors",
                    "2",
                ],
                run_logged_calls,
            )

    def test_apply_live_host_add_packages_enforces_debian_live_policy(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            iso_root = root / "iso-root"
            workspace_dir = root / "workspace"
            live_root = workspace_dir / "live-root"
            extracted_rootfs = iso_root / "live" / "filesystem.squashfs"
            extracted_initrd = iso_root / "live" / "initrd.img"
            versioned_initrd = iso_root / "live" / "initrd.img-6.1.0"
            rebuilt_initrd = live_root / "boot" / "initrd.img-6.1.0"
            boot_config = iso_root / "boot" / "grub" / "grub.cfg"
            extracted_rootfs.parent.mkdir(parents=True, exist_ok=True)
            rebuilt_initrd.parent.mkdir(parents=True, exist_ok=True)
            boot_config.parent.mkdir(parents=True, exist_ok=True)
            extracted_rootfs.write_text("rootfs", encoding="utf-8")
            extracted_initrd.write_text("old-initrd", encoding="utf-8")
            versioned_initrd.write_text("old-versioned-initrd", encoding="utf-8")
            rebuilt_initrd.write_text("new-initrd", encoding="utf-8")
            boot_config.write_text(
                "menuentry 'Live' {\n  linux /live/vmlinuz boot=live quiet ---\n}\n",
                encoding="utf-8",
            )
            plan = {
                "action": "add-deb-packages",
                "architecture": "",
                "live_deb_packages": ["nmap"],
            }
            inspection = {
                "architecture": "",
                "live_rootfs_path": "/live/filesystem.squashfs",
                "live_kernel_path": "/live/vmlinuz",
                "live_kernel_paths": ["/live/vmlinuz"],
                "live_initrd_path": "/live/initrd.img",
                "live_initrd_paths": ["/live/initrd.img", "/live/initrd.img-6.1.0"],
            }
            expected_packages = rebuild_iso._dedupe(
                [*rebuild_iso.DEBIAN_LIVE_HOOK_PACKAGES, "nmap"]
            )
            run_in_chroot_calls: list[list[str]] = []

            with patch("debian_usb.rebuild_iso._squashfs_processor_count", return_value=2), patch(
                "debian_usb.rebuild_iso._run_logged"
            ), patch(
                "debian_usb.rebuild_iso._install_packages_in_chroot"
            ) as install_packages, patch(
                "debian_usb.rebuild_iso._mounted_chroot",
                return_value=nullcontext(),
            ), patch(
                "debian_usb.rebuild_iso._run_in_chroot",
                side_effect=lambda _root, command, _log: run_in_chroot_calls.append(command),
            ), patch(
                "debian_usb.rebuild_iso._detect_live_root_kernel_version",
                return_value="6.1.0",
            ), patch(
                "debian_usb.rebuild_iso._resolve_live_root_initrd_file",
                return_value=rebuilt_initrd,
            ), patch(
                "debian_usb.rebuild_iso._refresh_live_metadata"
            ) as refresh_metadata, patch(
                "debian_usb.rebuild_iso._squashfs_compression",
                return_value="xz",
            ):
                result = rebuild_iso._apply_live_host_rebuild_action(
                    plan,
                    inspection,
                    iso_root,
                    workspace_dir,
                    root / "state",
                    None,
                )

            install_packages.assert_called_once_with(
                live_root,
                expected_packages,
                None,
                apt_source_root=iso_root,
            )
            self.assertEqual(result["packages"], expected_packages)
            self.assertEqual(
                result["modified_paths"],
                [
                    "/live/filesystem.squashfs",
                    "/live/initrd.img",
                    "/live/initrd.img-6.1.0",
                    "/live/config-hooks/0500-apt-live-medium.sh",
                    "/live/config-hooks/1000-network-wifi.sh",
                    "/live/debian-usb-live.env",
                    "/boot/grub/grub.cfg",
                ],
            )
            self.assertIn("flashrom", result["packages"])
            self.assertIn("firmware-iwlwifi", result["packages"])
            self.assertIn("wpasupplicant", result["packages"])
            self.assertIn(
                [
                    "env",
                    "DEBIAN_FRONTEND=noninteractive",
                    "LANG=C.UTF-8",
                    "LC_ALL=C.UTF-8",
                    "update-initramfs",
                    "-u",
                    "-k",
                    "all",
                ],
                run_in_chroot_calls,
            )
            self.assertEqual(extracted_initrd.read_text(encoding="utf-8"), "new-initrd")
            self.assertEqual(versioned_initrd.read_text(encoding="utf-8"), "new-initrd")
            expected_modules = "\n".join(rebuild_iso.DEBIAN_LIVE_INITRAMFS_MODULES) + "\n"
            self.assertEqual(
                (live_root / "usr/share/initramfs-tools/modules.d/debian-usb-live").read_text(
                    encoding="utf-8"
                ),
                expected_modules,
            )
            self.assertEqual(
                (live_root / "etc/modules-load.d/debian-usb-live.conf").read_text(
                    encoding="utf-8"
                ),
                expected_modules,
            )
            for hook_name in ("0500-apt-live-medium.sh", "1000-network-wifi.sh"):
                hook_path = iso_root / "live" / "config-hooks" / hook_name
                self.assertTrue(hook_path.is_file())
                self.assertEqual(hook_path.stat().st_mode & 0o777, 0o755)
            for wifi_path in (
                live_root / "etc" / "debian-usb" / "live.env",
                iso_root / "live" / "debian-usb-live.env",
            ):
                self.assertTrue(wifi_path.is_file())
                self.assertEqual(wifi_path.stat().st_mode & 0o777, 0o600)
            boot_text = boot_config.read_text(encoding="utf-8")
            self.assertIn("live-config.hooks=medium", boot_text)
            self.assertNotIn("LIVE_WIFI_", boot_text)
            refresh_metadata.assert_called_once_with(
                live_root,
                iso_root,
                "/live/filesystem.squashfs",
            )

    def test_apply_live_host_replace_kernel_refreshes_all_live_members(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            iso_root = root / "iso-root"
            workspace_dir = root / "workspace"
            live_root = workspace_dir / "live-root"
            extracted_rootfs = iso_root / "live" / "filesystem.squashfs"
            rebuilt_kernel = live_root / "boot" / "vmlinuz-6.12.1-amd64"
            rebuilt_initrd = live_root / "boot" / "initrd.img-6.12.1-amd64"
            extracted_rootfs.parent.mkdir(parents=True, exist_ok=True)
            rebuilt_kernel.parent.mkdir(parents=True, exist_ok=True)
            extracted_rootfs.write_text("rootfs", encoding="utf-8")
            rebuilt_kernel.write_text("new-kernel", encoding="utf-8")
            rebuilt_initrd.write_text("new-initrd", encoding="utf-8")
            plan = {
                "action": "replace-kernel",
                "architecture": "",
                "target_kernel_version": "6.12.1-amd64",
            }
            inspection = {
                "architecture": "",
                "live_rootfs_path": "/live/filesystem.squashfs",
                "live_kernel_path": "/live/vmlinuz",
                "live_kernel_paths": ["/live/vmlinuz", "/live/vmlinuz-6.12.1-amd64"],
                "live_initrd_path": "/live/initrd.img",
                "live_initrd_paths": ["/live/initrd.img", "/live/initrd.img-6.12.1-amd64"],
            }
            expected_packages = rebuild_iso._dedupe(
                [
                    *rebuild_iso.DEBIAN_LIVE_HOOK_PACKAGES,
                    "linux-image-6.12.1-amd64",
                    "linux-headers-6.12.1-amd64",
                ]
            )
            policy_paths = [
                "/live/config-hooks/0500-apt-live-medium.sh",
                "/live/config-hooks/1000-network-wifi.sh",
                "/live/debian-usb-live.env",
                "/boot/grub/grub.cfg",
            ]

            with patch("debian_usb.rebuild_iso._squashfs_processor_count", return_value=2), patch(
                "debian_usb.rebuild_iso._run_logged"
            ), patch(
                "debian_usb.rebuild_iso.stage_live_systemd_masks"
            ), patch(
                "debian_usb.rebuild_iso._stage_debian_live_root_policy"
            ) as stage_root_policy, patch(
                "debian_usb.rebuild_iso._install_packages_in_chroot"
            ) as install_packages, patch(
                "debian_usb.rebuild_iso._mounted_chroot",
                return_value=nullcontext(),
            ), patch(
                "debian_usb.rebuild_iso._run_in_chroot"
            ) as run_in_chroot, patch(
                "debian_usb.rebuild_iso._prune_live_kernel_versions"
            ) as prune_versions, patch(
                "debian_usb.rebuild_iso._resolve_live_root_kernel_file",
                return_value=rebuilt_kernel,
            ), patch(
                "debian_usb.rebuild_iso._resolve_live_root_initrd_file",
                return_value=rebuilt_initrd,
            ), patch(
                "debian_usb.rebuild_iso._stage_debian_live_iso_policy",
                return_value=policy_paths,
            ) as stage_iso_policy, patch(
                "debian_usb.rebuild_iso._refresh_live_metadata"
            ), patch(
                "debian_usb.rebuild_iso._squashfs_compression",
                return_value="xz",
            ):
                result = rebuild_iso._apply_live_host_rebuild_action(
                    plan,
                    inspection,
                    iso_root,
                    workspace_dir,
                    root / "state",
                    None,
                )

            stage_root_policy.assert_called_once_with(live_root)
            install_packages.assert_called_once_with(
                live_root,
                expected_packages,
                None,
                apt_source_root=iso_root,
            )
            run_in_chroot.assert_called_once_with(
                live_root,
                [
                    "env",
                    "DEBIAN_FRONTEND=noninteractive",
                    "LANG=C.UTF-8",
                    "LC_ALL=C.UTF-8",
                    "update-initramfs",
                    "-u",
                    "-k",
                    "all",
                ],
                None,
            )
            prune_versions.assert_called_once_with(live_root, "6.12.1-amd64")
            stage_iso_policy.assert_called_once_with(iso_root)
            self.assertEqual(result["packages"], expected_packages)
            self.assertEqual(
                result["modified_paths"],
                [
                    "/live/filesystem.squashfs",
                    "/live/vmlinuz",
                    "/live/vmlinuz-6.12.1-amd64",
                    "/live/initrd.img",
                    "/live/initrd.img-6.12.1-amd64",
                    *policy_paths,
                ],
            )
            for member_path in inspection["live_kernel_paths"]:
                self.assertEqual(
                    (iso_root / member_path.lstrip("/")).read_text(encoding="utf-8"),
                    "new-kernel",
                )
            for member_path in inspection["live_initrd_paths"]:
                self.assertEqual(
                    (iso_root / member_path.lstrip("/")).read_text(encoding="utf-8"),
                    "new-initrd",
                )

    @unittest.skipUnless(shutil.which("dpkg-divert"), "requires dpkg-divert")
    def test_apply_live_tools_remaster_installs_profile_packages_and_repacks_squashfs(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            iso_root = root / "iso-root"
            workspace_dir = root / "workspace"
            extracted_rootfs = iso_root / "live" / "filesystem.squashfs"
            extracted_initrd = iso_root / "live" / "initrd.img"
            live_root = workspace_dir / "live-tools-root"
            rebuilt_initrd = live_root / "boot" / "initrd.img-6.1.0"
            extracted_rootfs.parent.mkdir(parents=True, exist_ok=True)
            rebuilt_initrd.parent.mkdir(parents=True, exist_ok=True)
            extracted_rootfs.write_text("rootfs", encoding="utf-8")
            extracted_initrd.write_text("old-initrd", encoding="utf-8")
            rebuilt_initrd.write_text("new-initrd", encoding="utf-8")
            run_logged_calls: list[list[str]] = []

            def fake_run_logged(command: list[str], **_: object) -> None:
                run_logged_calls.append(command)
                if command and command[0] == "unsquashfs":
                    default_dir = live_root / "etc" / "default"
                    default_dir.mkdir(parents=True)
                    (live_root / "etc" / "locale.gen").write_text(
                        "# en_US.UTF-8 UTF-8\n",
                        encoding="utf-8",
                    )
                    (default_dir / "locale").symlink_to("../locale.conf")
                    # Real Live package ownership reproduces the reported
                    # cleanup failure; all mount/ISO operations remain fixtures.
                    wrapper = live_root / "usr/bin/live-update-initramfs"
                    wrapper.parent.mkdir(parents=True)
                    wrapper.write_text("original live wrapper\n", encoding="utf-8")
                    binary = live_root / "usr/sbin/update-initramfs"
                    binary.parent.mkdir(parents=True)
                    binary.symlink_to("/usr/bin/live-update-initramfs")
                    binary.with_name("update-initramfs.orig.initramfs-tools").write_text(
                        "original engine\n", encoding="utf-8")
                    info = live_root / "var/lib/dpkg/info"
                    info.mkdir(parents=True)
                    (info / "live-tools.list").write_text(
                        "/usr/sbin/update-initramfs\n/usr/bin/live-update-initramfs\n", encoding="utf-8")
                    (info.parent / "status").write_text(
                        "Package: live-tools\nStatus: install ok installed\nArchitecture: all\n"
                        "Version: 1:20240525\nMaintainer: Test <test@example.invalid>\nDescription: fixture\n\n",
                        encoding="utf-8")
                    subprocess.run(["dpkg-divert", "--root=" + str(live_root), "--package", "live-tools",
                                    "--add", "--no-rename", "--divert", "/usr/sbin/update-initramfs.orig.initramfs-tools",
                                    "/usr/sbin/update-initramfs"],
                                   check=True, capture_output=True, encoding="utf-8")
                elif command[:3] == ["chroot", str(live_root), "dpkg-divert"]:
                    subprocess.run(["dpkg-divert", "--root=" + str(live_root), *command[3:]],
                                   check=True, capture_output=True, encoding="utf-8")

            with patch("debian_usb.rebuild_iso._run_logged", side_effect=fake_run_logged):
                with patch("debian_usb.rebuild_iso._install_packages_in_chroot") as install_packages:
                    with patch("debian_usb.rebuild_iso._mounted_chroot", return_value=nullcontext()):
                        with patch("debian_usb.rebuild_iso._detect_live_root_kernel_version", return_value="6.1.0"):
                            with patch("debian_usb.rebuild_iso._resolve_live_root_initrd_file", return_value=rebuilt_initrd):
                                with patch("debian_usb.rebuild_iso._refresh_live_metadata") as refresh_metadata:
                                    with patch("debian_usb.rebuild_iso._squashfs_compression", return_value="xz"):
                                        modified = rebuild_iso._apply_live_tools_remaster(
                                            live_rootfs_path="/live/filesystem.squashfs",
                                            live_initrd_path="/live/initrd.img",
                                            live_initrd_paths=["/live/initrd.img"],
                                            live_kernel_path="/live/vmlinuz",
                                            packages=["nvme-cli", "nmap"],
                                            profile="debian",
                                            iso_root=iso_root,
                                            workspace_dir=workspace_dir,
                                            log_file=None,
                                            processors=2,
                                        )

            self.assertTrue((live_root / "usr/sbin/update-initramfs").is_symlink())
            self.assertEqual((live_root / "usr/bin/live-update-initramfs").read_text(encoding="utf-8"),
                             "original live wrapper\n")
            self.assertFalse((live_root / "usr/bin/live-update-initramfs.debian-usb-real").exists())
            self.assertEqual((live_root / "var/lib/dpkg/diversions").read_text(encoding="utf-8"),
                             "/usr/sbin/update-initramfs\n/usr/sbin/update-initramfs.orig.initramfs-tools\nlive-tools\n")
            generation = [i for i, command in enumerate(run_logged_calls) if "mkinitramfs" in command]
            repack = [i for i, command in enumerate(run_logged_calls) if command[0] == "mksquashfs"]
            self.assertEqual(len(generation), 1)
            self.assertEqual(len(repack), 1)
            self.assertLess(generation[0], repack[0])
            self.assertEqual(
                modified,
                [
                    "/live/filesystem.squashfs",
                    "/live/initrd.img",
                    "/live/config-hooks/0500-apt-live-medium.sh",
                    "/live/config-hooks/1000-network-wifi.sh",
                    "/live/debian-usb-live.env",
                ],
            )
            install_packages.assert_called_once_with(
                live_root,
                ["nvme-cli", "nmap"],
                None,
                apt_source_root=iso_root,
            )
            expected_modules = "\n".join(rebuild_iso.DEBIAN_LIVE_INITRAMFS_MODULES) + "\n"
            self.assertEqual(
                (live_root / "usr/share/initramfs-tools/modules.d/debian-usb-live").read_text(encoding="utf-8"),
                expected_modules,
            )
            self.assertEqual(
                (live_root / "usr/share/initramfs-tools/conf.d/debian-usb-live").read_text(encoding="utf-8"),
                "MODULES=most\n",
            )
            self.assertEqual(
                (live_root / "etc/modules-load.d/debian-usb-live.conf").read_text(encoding="utf-8"),
                expected_modules,
            )
            self.assertEqual(extracted_initrd.read_text(encoding="utf-8"), "new-initrd")
            for unit in ("fwupd-refresh.service", "fwupd-refresh.timer"):
                mask_path = live_root / "etc" / "systemd" / "system" / unit
                self.assertTrue(mask_path.is_symlink())
                self.assertEqual(mask_path.readlink(), Path("/dev/null"))
            self.assertIn(
                "en_US.UTF-8 UTF-8",
                (live_root / "etc" / "locale.gen").read_text(encoding="utf-8").splitlines(),
            )
            default_locale = live_root / "etc" / "default" / "locale"
            self.assertTrue(default_locale.is_symlink())
            self.assertEqual(default_locale.readlink(), Path("../locale.conf"))
            self.assertIn(
                "LANG=en_US.UTF-8",
                (live_root / "etc" / "locale.conf").read_text(encoding="utf-8").splitlines(),
            )
            self.assertIn(
                "LANGUAGE=en_US:en",
                (live_root / "etc" / "locale.conf").read_text(encoding="utf-8").splitlines(),
            )
            self.assertIn(
                [
                    "chroot",
                    str(live_root),
                    "env",
                    "DEBIAN_FRONTEND=noninteractive",
                    "LANG=C.UTF-8",
                    "LC_ALL=C.UTF-8",
                    "locale-gen",
                    "en_US.UTF-8",
                ],
                run_logged_calls,
            )
            self.assertIn(
                [
                    "chroot",
                    str(live_root),
                    "env",
                    "DEBIAN_FRONTEND=noninteractive",
                    "LANG=C.UTF-8",
                    "LC_ALL=C.UTF-8",
                    "update-locale",
                    "LANG=en_US.UTF-8",
                    "LANGUAGE=en_US:en",
                ],
                run_logged_calls,
            )
            refresh_metadata.assert_called_once_with(live_root, iso_root, "/live/filesystem.squashfs")
            for hook_name in ("0500-apt-live-medium.sh", "1000-network-wifi.sh"):
                hook_path = iso_root / "live" / "config-hooks" / hook_name
                self.assertTrue(hook_path.is_file())
                self.assertEqual(hook_path.stat().st_mode & 0o777, 0o755)
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
                live_root / "etc" / "debian-usb" / "live.env",
                iso_root / "live" / "debian-usb-live.env",
            ):
                self.assertTrue(wifi_path.is_file())
                self.assertEqual(wifi_path.stat().st_mode & 0o777, 0o600)
                wifi_keys = {
                    line.split("=", 1)[0]
                    for line in wifi_path.read_text(encoding="utf-8").splitlines()
                    if line and not line.startswith("#")
                }
                self.assertEqual(wifi_keys, expected_wifi_keys)
            self.assertIn(
                ["unsquashfs", "-processors", "2", "-d", str(live_root), str(extracted_rootfs)],
                run_logged_calls,
            )
            self.assertIn(
                [
                    "mksquashfs",
                    str(live_root),
                    str(extracted_rootfs),
                    "-noappend",
                    "-comp",
                    "xz",
                    "-processors",
                    "2",
                ],
                run_logged_calls,
            )

    @patch("debian_usb.rebuild_iso._rewrite_checksum_files")
    @patch("debian_usb.rebuild_iso._merge_initrd_overlay_archive")
    @patch("debian_usb.rebuild_iso._media_class", return_value="hybrid")
    @patch("debian_usb.rebuild_iso._find_boot_entries")
    @patch("debian_usb.rebuild_iso.open_source")
    @patch("debian_usb.rebuild_iso._ensure_live_initrd_overlay_deps")
    def test_remaster_live_initrd_source_changes_only_live_initrds(
        self,
        ensure_deps,
        open_source_mock,
        find_entries,
        _media_class,
        merge_archive,
        _rewrite_checksums,
    ) -> None:
        entries = [
            FakeEntry(title="Live", kernel_path="/live/vmlinuz", initrd_path="/live/initrd.img", kind="live"),
            FakeEntry(
                title="Installer",
                kernel_path="/install.amd/vmlinuz",
                initrd_path="/install.amd/initrd.gz",
                kind="installer",
            ),
        ]
        find_entries.return_value = entries
        source = FakeSource()
        source.display_path = "/tmp/source.iso"
        source.exists = lambda member_path: member_path in {
            "/live/filesystem.squashfs",
            "/live/initrd.img",
            "/install.amd/initrd.gz",
        }
        open_source_mock.return_value = source
        merge_archive.return_value = {
            "embedded_root": "/",
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_iso = root / "source.iso"
            source_iso.write_bytes(b"immutable-source")
            overlay = root / "initrd/debian/live"
            overlay.mkdir(parents=True)
            (overlay / "stage-marker").write_text("live\n", encoding="utf-8")
            output_dir = root / "output"
            work_root = root / "work"
            state_root = root / "state"
            log_root = root / "log"

            def fake_run_logged(command, **_kwargs) -> None:
                if "-extract" in command:
                    iso_root = Path(command[command.index("-extract") + 2])
                    (iso_root / "live").mkdir(parents=True, exist_ok=True)
                    (iso_root / "live/initrd.img").write_bytes(b"live-initrd")
                    (iso_root / "install.amd").mkdir(parents=True, exist_ok=True)
                    (iso_root / "install.amd/initrd.gz").write_bytes(b"installer-initrd")
                if "-outdev" in command:
                    Path(command[command.index("-outdev") + 1]).write_bytes(b"remastered-live-iso")

            with patch.object(rebuild_iso, "DEFAULT_WORK_DIR", work_root), patch.object(
                rebuild_iso, "DEFAULT_STATE_DIR", state_root
            ), patch.object(rebuild_iso, "DEFAULT_LOG_DIR", log_root), patch(
                "debian_usb.rebuild_iso._run_logged", side_effect=fake_run_logged
            ), patch(
                "debian_usb.rebuild_iso._finalize_rebuild_output_access",
                wraps=rebuild_iso._finalize_rebuild_output_access,
            ) as finalize_output:
                result = rebuild_iso.remaster_live_initrd_source(
                    str(source_iso),
                    "debian",
                    str(overlay),
                    str(output_dir),
                )

            self.assertEqual(source_iso.read_bytes(), b"immutable-source")
            self.assertEqual(result["modified_paths"], ["/live/initrd.img"])
            self.assertEqual(Path(result["iso_path"]).read_bytes(), b"remastered-live-iso")
            self.assertEqual(
                result["overlay"],
                {"overlay_dir": str(overlay.resolve()), "embedded_root": "/"},
            )
            ensure_deps.assert_called_once_with()
            merge_archive.assert_called_once()
            self.assertEqual(merge_archive.call_args.kwargs["archive_path"], work_root / "remaster-live-initrd" / result["run_id"] / "iso-root/live/initrd.img")
            self.assertFalse(any(call.kwargs.get("archive_path") == work_root / "remaster-live-initrd" / result["run_id"] / "iso-root/install.amd/initrd.gz" for call in merge_archive.call_args_list))
            finalize_output.assert_called_once()
            handoff_path, created_directories = finalize_output.call_args.args
            self.assertEqual(handoff_path.parent, output_dir)
            self.assertTrue(handoff_path.name.startswith(".source-initrd-overlay.iso."))
            self.assertTrue(handoff_path.name.endswith(".part"))
            self.assertFalse(handoff_path.exists())
            self.assertEqual(created_directories, (output_dir,))

    @unittest.skipUnless(
        all(shutil.which(command) for command in ("cpio", "find")),
        "requires cpio and find",
    )
    def test_repacked_initrd_uses_root_ownership_without_changing_source(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_tree = root / "source-tree"
            source_tree.mkdir()
            source_file = source_tree / "repo-overlay-marker"
            source_file.write_text("overlay\n", encoding="utf-8")
            if (source_file.stat().st_uid, source_file.stat().st_gid) == (0, 0):
                os.chown(source_file, 12345, 23456)
            source_owner = (source_file.stat().st_uid, source_file.stat().st_gid)
            self.assertNotEqual(source_owner, (0, 0))

            archive = root / "initrd.cpio"
            rebuild_iso._repack_initrd_archive(source_tree, archive)

            self.assertEqual((source_file.stat().st_uid, source_file.stat().st_gid), source_owner)
            with archive.open("rb") as archive_handle:
                listing = subprocess.run(
                    ["cpio", "-itv", "--numeric-uid-gid"],
                    stdin=archive_handle,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    check=False,
                )
            self.assertEqual(listing.returncode, 0, listing.stderr)
            member_fields = next(
                fields
                for line in listing.stdout.splitlines()
                if (fields := line.split()) and fields[-1] == "repo-overlay-marker"
            )
            self.assertEqual(member_fields[2:4], ["0", "0"])

    @unittest.skipUnless(
        all(shutil.which(command) for command in ("cpio", "find")),
        "requires cpio and find",
    )
    def test_live_initramfs_hook_drops_repository_ownership(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            overlay = root / "initrd/debian/live"
            overlay.mkdir(parents=True)
            source_file = overlay / "live-marker"
            source_file.write_text("selected-live-only\n", encoding="utf-8")
            source_owner = (source_file.stat().st_uid, source_file.stat().st_gid)
            live_root = root / "live-root"
            live_root.mkdir()

            rebuild_iso._stage_live_initrd_overlay(live_root, overlay)

            self.assertEqual((source_file.stat().st_uid, source_file.stat().st_gid), source_owner)
            hook = live_root / "etc/initramfs-tools/hooks/zz-debian-usb-overlay"
            self.assertIn(
                'cp -a --no-preserve=ownership -- '
                '/usr/share/debian-usb/initrd-overlay/. "${DESTDIR}/"',
                hook.read_text(encoding="utf-8"),
            )

    @unittest.skipUnless(
        all(shutil.which(command) for command in ("cpio", "find", "gzip")),
        "requires cpio, find, and gzip",
    )
    def test_live_initrd_overlay_preserves_gzip_compression_for_img_name(self) -> None:
        import gzip

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_tree = root / "source-tree"
            source_tree.mkdir()
            (source_tree / "existing").write_text("preserved\n", encoding="utf-8")
            uncompressed = root / "initrd.cpio"
            rebuild_iso._repack_initrd_archive(source_tree, uncompressed)
            archive = root / "initrd.img"
            archive.write_bytes(gzip.compress(uncompressed.read_bytes(), mtime=0))
            overlay = root / "overlay"
            overlay.mkdir()
            (overlay / "live-marker").write_text("selected-live-only\n", encoding="utf-8")

            manifest = rebuild_iso._merge_initrd_overlay_archive(
                archive_path=archive,
                overlay_dir=overlay,
                workspace_dir=root / "workspace",
            )

            self.assertTrue(archive.read_bytes().startswith(b"\x1f\x8b"))
            extracted = root / "extracted"
            rebuild_iso._extract_initrd_archive(archive, extracted)
            self.assertEqual((extracted / "existing").read_text(encoding="utf-8"), "preserved\n")
            self.assertEqual((extracted / "live-marker").read_text(encoding="utf-8"), "selected-live-only\n")
            self.assertEqual(manifest["embedded_root"], "/")

    @unittest.skipUnless(
        all(shutil.which(command) for command in ("cpio", "find", "zstd")),
        "requires cpio, find, and zstd",
    )
    def test_live_initrd_overlay_preserves_concatenated_early_cpio_segments(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)

            early_microcode_tree = root / "early-microcode-tree"
            microcode = early_microcode_tree / "kernel/x86/microcode/GenuineIntel.bin"
            microcode.parent.mkdir(parents=True)
            microcode.write_bytes(b"early-microcode")
            early_microcode_archive = root / "early-microcode.cpio"
            rebuild_iso._repack_initrd_archive(early_microcode_tree, early_microcode_archive)

            early_modules_tree = root / "early-modules-tree"
            early_module = early_modules_tree / "usr/lib/modules/test/early.ko"
            early_module.parent.mkdir(parents=True)
            early_module.write_bytes(b"early-module")
            early_modules_archive = root / "early-modules.cpio"
            rebuild_iso._repack_initrd_archive(early_modules_tree, early_modules_archive)

            main_tree = root / "main-tree"
            main_tree.mkdir()
            (main_tree / "init").write_text("#!/bin/sh\n", encoding="utf-8")
            main_archive = root / "main.zst"
            rebuild_iso._repack_initrd_archive(main_tree, main_archive)

            early_prefix = early_microcode_archive.read_bytes() + early_modules_archive.read_bytes()
            live_dir = root / "iso-root/live"
            live_dir.mkdir(parents=True)
            archive = live_dir / "initrd.img-6.12-test"
            archive.write_bytes(early_prefix + main_archive.read_bytes())
            archive.chmod(0o444)
            live_dir.chmod(0o555)
            layout = rebuild_iso._initrd_archive_layout(archive)
            self.assertEqual(layout.main_offset, len(early_prefix))
            self.assertEqual(layout.compression, "zstd")

            overlay = root / "overlay"
            overlay.mkdir()
            (overlay / "live.env").write_text("LIVE_WIFI_PASSPHRASE=''\n", encoding="utf-8")
            manifest = rebuild_iso._merge_initrd_overlay_archive(
                archive_path=archive,
                overlay_dir=overlay,
                workspace_dir=root / "workspace",
            )

            rebuilt = archive.read_bytes()
            self.assertEqual(archive.stat().st_mode & 0o777, 0o444)
            self.assertEqual(live_dir.stat().st_mode & 0o777, 0o555)
            self.assertEqual(rebuilt[: len(early_prefix)], early_prefix)
            self.assertEqual(rebuilt[len(early_prefix) : len(early_prefix) + 4], b"(\xb5/\xfd")
            extracted_main = root / "extracted-main"
            rebuild_iso._extract_initrd_archive(archive, extracted_main)
            self.assertEqual((extracted_main / "init").read_text(encoding="utf-8"), "#!/bin/sh\n")
            self.assertEqual(
                (extracted_main / "live.env").read_text(encoding="utf-8"),
                "LIVE_WIFI_PASSPHRASE=''\n",
            )
            self.assertEqual(manifest["embedded_root"], "/")

            if shutil.which("unmkinitramfs") is not None:
                extracted_all = root / "extracted-all"
                result = subprocess.run(
                    ["unmkinitramfs", str(archive), str(extracted_all)],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    check=False,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                # unmkinitramfs releases may use early/early2/main directories.
                self.assertEqual(
                    next(extracted_all.rglob("GenuineIntel.bin")).read_bytes(),
                    b"early-microcode",
                )
                self.assertEqual(
                    next(extracted_all.rglob("early.ko")).read_bytes(),
                    b"early-module",
                )
                self.assertEqual(
                    next(extracted_all.rglob("live.env")).read_text(encoding="utf-8"),
                    "LIVE_WIFI_PASSPHRASE=''\n",
                )

    @patch("debian_usb.rebuild_iso._rewrite_checksum_files")
    @patch("debian_usb.rebuild_iso._apply_live_tools_remaster", return_value=["/live/filesystem.squashfs"])
    @patch("debian_usb.rebuild_iso._select_entry")
    @patch("debian_usb.rebuild_iso._media_class", return_value="live")
    @patch("debian_usb.rebuild_iso._find_boot_entries", return_value=[])
    @patch("debian_usb.rebuild_iso.open_source", return_value=FakeSource())
    @patch("debian_usb.rebuild_iso.ensure_debian_rebuild_deps", return_value={"changed": False})
    def test_remaster_live_tools_source_keeps_source_immutable_and_records_profile(
        self,
        ensure_deps,
        _open_source,
        _find_entries,
        _media_class,
        select_entry,
        apply_tools,
        _rewrite_checksums,
    ) -> None:
        select_entry.return_value = FakeEntry(
            title="Live system",
            kernel_path="/live/vmlinuz",
            initrd_path="/live/initrd.img",
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_iso = root / "source.iso"
            output_dir = root / "output"
            work_root = root / "work"
            state_root = root / "state"
            log_root = root / "log"
            source_iso.write_text("source-bytes", encoding="utf-8")

            def fake_run_logged(command, **_kwargs) -> None:
                if "-outdev" in command:
                    Path(command[command.index("-outdev") + 1]).write_bytes(b"remastered-iso")

            with patch.object(rebuild_iso, "DEFAULT_WORK_DIR", work_root), patch.object(
                rebuild_iso,
                "DEFAULT_STATE_DIR",
                state_root,
            ), patch.object(rebuild_iso, "DEFAULT_LOG_DIR", log_root), patch(
                "debian_usb.rebuild_iso._preflight_live_tools_storage",
            ) as preflight_storage, patch(
                "debian_usb.rebuild_iso._squashfs_processor_count",
                return_value=2,
            ), patch(
                "debian_usb.rebuild_iso._run_logged",
                side_effect=fake_run_logged,
            ) as run_logged:
                result = rebuild_iso.remaster_live_tools_source(
                    str(source_iso),
                    "debian",
                    str(output_dir),
                    selected_groups=["nvme", "nmap"],
                    live_kernel_args="debian_usb.profile=test",
                )

            self.assertEqual(source_iso.read_text(encoding="utf-8"), "source-bytes")
            self.assertEqual(result["profile"], "debian")
            self.assertEqual(Path(result["iso_path"]).parent, output_dir)
            self.assertGreater(Path(result["iso_path"]).stat().st_size, 0)
            self.assertIn("nvme-cli", result["packages"])
            self.assertIn("nmap", result["packages"])
            self.assertIn("dhcpcd-base", result["packages"])
            self.assertIn("locales", result["packages"])
            self.assertNotIn("isc-dhcp-client", result["packages"])
            self.assertEqual(result["selected_groups"], ["nvme", "nmap"])
            self.assertTrue(result["workspace_removed"])
            self.assertFalse(Path(result["workspace_dir"]).exists())
            self.assertEqual(Path(result["workspace_dir"]).parent, work_root / "remaster-live-tools")
            self.assertEqual(Path(result["manifest_path"]).parent, state_root / "remaster-live-tools" / result["run_id"])
            self.assertEqual(Path(result["log_path"]).parent, log_root / "remaster-live-tools")
            self.assertFalse(any(output_dir.rglob(".debian-usb-live-tools")))
            self.assertEqual(preflight_storage.call_count, 2)
            self.assertEqual(
                set(result["live_kernel_arg_keys"]),
                {"debian_usb.profile", "live-config.hooks"},
            )
            for package in rebuild_iso.DEBIAN_LIVE_HOOK_PACKAGES:
                self.assertIn(package, result["packages"])
            self.assertEqual(result["package_profile"]["package_count"], len(result["packages"]))
            self.assertTrue(Path(result["manifest_path"]).is_file())
            manifest_text = Path(result["manifest_path"]).read_text(encoding="utf-8")
            self.assertNotIn("live_wifi_", manifest_text)
            ensure_deps.assert_called_once_with()
            apply_tools.assert_called_once()
            self.assertEqual(apply_tools.call_args.kwargs["profile"], "debian")
            self.assertEqual(apply_tools.call_args.kwargs["processors"], 2)
            xorriso_outputs = [
                call.args[0]
                for call in run_logged.call_args_list
                if "-outdev" in call.args[0]
            ]
            self.assertEqual(len(xorriso_outputs), 1)
            self.assertEqual(xorriso_outputs[0][xorriso_outputs[0].index("-indev") + 1], str(source_iso))
            xorriso_output_path = Path(xorriso_outputs[0][xorriso_outputs[0].index("-outdev") + 1])
            self.assertEqual(xorriso_output_path.parent, output_dir)
            self.assertTrue(xorriso_output_path.name.startswith(".source-admin-tools.iso."))
            self.assertTrue(xorriso_output_path.name.endswith(".part"))
            self.assertFalse(xorriso_output_path.exists())

    @patch("debian_usb.rebuild_iso._rewrite_checksum_files")
    @patch("debian_usb.rebuild_iso._apply_live_tools_remaster", return_value=["/live/filesystem.squashfs"])
    @patch("debian_usb.rebuild_iso._select_entry")
    @patch("debian_usb.rebuild_iso._media_class", return_value="live")
    @patch("debian_usb.rebuild_iso._find_boot_entries", return_value=[])
    @patch("debian_usb.rebuild_iso.open_source", return_value=FakeSource())
    @patch("debian_usb.rebuild_iso.ensure_debian_rebuild_deps", return_value={"changed": False})
    def test_remaster_live_tools_source_rejects_missing_output_iso(
        self,
        _ensure_deps,
        _open_source,
        _find_entries,
        _media_class,
        select_entry,
        _apply_tools,
        _rewrite_checksums,
    ) -> None:
        select_entry.return_value = FakeEntry(
            title="Live system",
            kernel_path="/live/vmlinuz",
            initrd_path="/live/initrd.img",
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_iso = root / "source.iso"
            output_dir = root / "output"
            work_root = root / "work"
            state_root = root / "state"
            log_root = root / "log"
            source_iso.write_text("source-bytes", encoding="utf-8")

            with patch.object(rebuild_iso, "DEFAULT_WORK_DIR", work_root), patch.object(
                rebuild_iso,
                "DEFAULT_STATE_DIR",
                state_root,
            ), patch.object(rebuild_iso, "DEFAULT_LOG_DIR", log_root), patch(
                "debian_usb.rebuild_iso._preflight_live_tools_storage",
            ) as preflight_storage, patch(
                "debian_usb.rebuild_iso._squashfs_processor_count",
                return_value=2,
            ), patch("debian_usb.rebuild_iso._run_logged"):
                with self.assertRaisesRegex(RuntimeError, "did not produce a non-empty regular ISO"):
                    rebuild_iso.remaster_live_tools_source(str(source_iso), "debian", str(output_dir))

            self.assertEqual(source_iso.read_text(encoding="utf-8"), "source-bytes")
            self.assertFalse(any(output_dir.rglob("manifest.json")))
            remaster_work_root = work_root / "remaster-live-tools"
            self.assertTrue(remaster_work_root.is_dir())
            self.assertEqual(list(remaster_work_root.iterdir()), [])
            self.assertEqual(preflight_storage.call_count, 2)

    @patch("debian_usb.rebuild_iso._rewrite_checksum_files")
    @patch("debian_usb.rebuild_iso._apply_live_tools_remaster", return_value=["/live/filesystem.squashfs"])
    @patch("debian_usb.rebuild_iso._select_entry")
    @patch("debian_usb.rebuild_iso._media_class", return_value="live")
    @patch("debian_usb.rebuild_iso._find_boot_entries", return_value=[])
    @patch("debian_usb.rebuild_iso.open_source", return_value=FakeSource())
    @patch("debian_usb.rebuild_iso.ensure_debian_rebuild_deps", return_value={"changed": False})
    def test_remaster_live_tools_source_preserves_existing_iso_when_output_fails(
        self,
        _ensure_deps,
        _open_source,
        _find_entries,
        _media_class,
        select_entry,
        _apply_tools,
        _rewrite_checksums,
    ) -> None:
        select_entry.return_value = FakeEntry(
            title="Live system",
            kernel_path="/live/vmlinuz",
            initrd_path="/live/initrd.img",
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_iso = root / "source.iso"
            output_dir = root / "output"
            work_root = root / "work"
            state_root = root / "state"
            log_root = root / "log"
            source_iso.write_text("source-bytes", encoding="utf-8")
            output_dir.mkdir()
            existing_iso = output_dir / "source-admin-tools.iso"
            existing_iso.write_text("existing-valid-iso", encoding="utf-8")

            def fake_run_logged(command: list[str], **_: object) -> None:
                if "-outdev" not in command:
                    return
                temporary_output = Path(command[command.index("-outdev") + 1])
                temporary_output.write_text("partial-output", encoding="utf-8")
                raise RuntimeError("simulated xorriso failure")

            with patch.object(rebuild_iso, "DEFAULT_WORK_DIR", work_root), patch.object(
                rebuild_iso,
                "DEFAULT_STATE_DIR",
                state_root,
            ), patch.object(rebuild_iso, "DEFAULT_LOG_DIR", log_root), patch(
                "debian_usb.rebuild_iso._preflight_live_tools_storage",
            ), patch(
                "debian_usb.rebuild_iso._squashfs_processor_count",
                return_value=2,
            ), patch("debian_usb.rebuild_iso._run_logged", side_effect=fake_run_logged):
                with self.assertRaisesRegex(RuntimeError, "simulated xorriso failure"):
                    rebuild_iso.remaster_live_tools_source(str(source_iso), "debian", str(output_dir))

            self.assertEqual(existing_iso.read_text(encoding="utf-8"), "existing-valid-iso")
            self.assertEqual(list(output_dir.glob(".*.part")), [])
            self.assertEqual(list((work_root / "remaster-live-tools").iterdir()), [])

    def test_patch_live_boot_configs_updates_live_lines_only(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            iso_root = Path(temp_dir)
            grub_path = iso_root / "boot" / "grub" / "grub.cfg"
            isolinux_path = iso_root / "isolinux" / "live.cfg"
            grub_path.parent.mkdir(parents=True)
            isolinux_path.parent.mkdir(parents=True)
            grub_path.write_text(
                "\n".join(
                    [
                        "linux /live/vmlinuz boot=live components quiet ---",
                        "linux /install.amd/vmlinuz auto=true priority=critical ---",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            isolinux_path.write_text(
                "append initrd=/live/initrd.img boot=live components\n",
                encoding="utf-8",
            )

            modified = rebuild_iso._patch_live_boot_configs(
                iso_root,
                "live-config.hooks=medium debian_usb.profile=test",
            )

            grub_text = grub_path.read_text(encoding="utf-8")
            isolinux_text = isolinux_path.read_text(encoding="utf-8")
            second_modified = rebuild_iso._patch_live_boot_configs(
                iso_root,
                "live-config.hooks=medium debian_usb.profile=test",
            )
            second_grub_text = grub_path.read_text(encoding="utf-8")
            second_isolinux_text = isolinux_path.read_text(encoding="utf-8")

        self.assertEqual(modified, ["/boot/grub/grub.cfg", "/isolinux/live.cfg"])
        self.assertEqual(second_modified, [])
        self.assertEqual(second_grub_text, grub_text)
        self.assertEqual(second_isolinux_text, isolinux_text)
        self.assertIn("live-config.hooks=medium debian_usb.profile=test ---", grub_text)
        self.assertIn("/install.amd/vmlinuz auto=true priority=critical ---", grub_text)
        self.assertNotIn("debian_usb.profile", grub_text.splitlines()[1])
        self.assertNotIn("live-config.hooks", grub_text.splitlines()[1])
        self.assertIn("debian_usb.profile=test", isolinux_text)

    def test_validate_live_kernel_args_rejects_grub_command_separators(self) -> None:
        with self.assertRaisesRegex(ValueError, "invalid Live kernel argument token"):
            rebuild_iso._validate_live_kernel_args("live-config.hooks=medium; halt")

    def test_validate_live_kernel_args_rejects_all_wifi_transports(self) -> None:
        forbidden = (
            "live_wifi_psk_b64=cmV0aXJlZA",
            "live_wifi_essid_b64=cmV0aXJlZA",
            "LIVE_WIFI_ESSID=retired",
            "DEFAULT_LIVE_WIFI_INTERFACE=wlan0",
            "netcfg/wireless_essid=retired",
            "netcfg/wireless_wpa=retired",
        )
        for argument in forbidden:
            with self.subTest(argument=argument):
                with self.assertRaisesRegex(ValueError, "Live Wi-Fi kernel arguments are forbidden"):
                    rebuild_iso._validate_live_kernel_args(
                        f"live-config.hooks=medium {argument}"
                    )

    def test_remaster_live_tools_source_rejects_non_debian_hook_arguments_before_dependencies(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            source_iso = Path(temp_dir) / "kali.iso"
            source_iso.write_text("source", encoding="utf-8")
            with patch("debian_usb.rebuild_iso.ensure_debian_rebuild_deps") as ensure_deps, patch(
                "debian_usb.rebuild_iso.open_source"
            ) as open_source:
                with self.assertRaisesRegex(ValueError, "supported only for Debian Live remasters"):
                    rebuild_iso.remaster_live_tools_source(
                        str(source_iso),
                        "kali-linux",
                        str(Path(temp_dir) / "out"),
                        selected_groups=["nmap"],
                        live_kernel_args="live-config.hooks=medium",
                    )
            ensure_deps.assert_not_called()
            open_source.assert_not_called()

    def test_remaster_live_tools_source_rejects_tails_before_dependency_installation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            source_iso = Path(temp_dir) / "tails.iso"
            source_iso.write_text("source", encoding="utf-8")
            with patch("debian_usb.rebuild_iso.ensure_debian_rebuild_deps") as ensure_deps:
                with self.assertRaisesRegex(ValueError, "not supported"):
                    rebuild_iso.remaster_live_tools_source(str(source_iso), "tails", str(Path(temp_dir) / "out"))
            ensure_deps.assert_not_called()

    def test_build_chroot_apt_source_lines_rewrites_live_media_source(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            live_root = root / "live-root"
            iso_root = root / "iso-root"
            apt_dir = live_root / "etc" / "apt"
            apt_dir.mkdir(parents=True, exist_ok=True)
            (apt_dir / "sources.list").write_text(
                "\n".join(
                    [
                        "deb [trusted=yes] file:/run/live/medium trixie main non-free-firmware",
                        "deb http://deb.debian.org/debian/ trixie main non-free-firmware",
                        "deb-src http://deb.debian.org/debian/ trixie main non-free-firmware",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            write_apt_archive_fixture(iso_root, "trixie", ["main", "non-free-firmware"])

            lines = rebuild_iso._build_chroot_apt_source_lines(live_root, iso_root)

        self.assertEqual(
            lines,
            [
                f"deb [trusted=yes] file:{rebuild_iso.CHROOT_ISO_SOURCE_MOUNT} trixie main non-free-firmware",
                "deb http://deb.debian.org/debian/ trixie main non-free-firmware",
            ],
        )

    def test_build_chroot_apt_source_lines_drops_partial_live_media_source(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            live_root = root / "live-root"
            iso_root = root / "iso-root"
            apt_dir = live_root / "etc" / "apt"
            apt_dir.mkdir(parents=True, exist_ok=True)
            (apt_dir / "sources.list").write_text(
                "\n".join(
                    [
                        "deb [trusted=yes] file:/run/live/medium trixie main non-free-firmware",
                        "deb http://deb.debian.org/debian/ trixie main non-free-firmware",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            write_apt_archive_fixture(iso_root, "trixie", ["main"])

            lines = rebuild_iso._build_chroot_apt_source_lines(live_root, iso_root)

        self.assertEqual(lines, ["deb http://deb.debian.org/debian/ trixie main non-free-firmware"])

    def test_build_chroot_apt_source_lines_drops_partial_deb822_live_media_source(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            live_root = root / "live-root"
            iso_root = root / "iso-root"
            apt_dir = live_root / "etc" / "apt" / "sources.list.d"
            apt_dir.mkdir(parents=True, exist_ok=True)
            (apt_dir / "debian.sources").write_text(
                "\n".join(
                    [
                        "Types: deb",
                        "URIs: file:/run/live/medium",
                        "Suites: trixie",
                        "Components: main non-free-firmware",
                        "Trusted: yes",
                        "",
                        "Types: deb",
                        "URIs: http://deb.debian.org/debian/",
                        "Suites: trixie",
                        "Components: main non-free-firmware",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            write_apt_archive_fixture(iso_root, "trixie", ["main"])

            lines = rebuild_iso._build_chroot_apt_source_lines(live_root, iso_root)

        self.assertEqual(lines, ["deb http://deb.debian.org/debian/ trixie main non-free-firmware"])

    def test_install_packages_in_chroot_updates_installs_and_cleans_archives(self) -> None:
        apt_config = {
            "list_path": "/tmp/debian-usb-remaster-apt/sources.list",
            "parts_dir": "/tmp/debian-usb-remaster-apt/sources.list.d",
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            live_root = Path(temp_dir) / "live-root"
            live_root.mkdir()
            with patch(
                "debian_usb.rebuild_iso._temporary_chroot_apt_config",
                return_value=nullcontext(apt_config),
            ):
                with patch(
                    "debian_usb.rebuild_iso._mounted_chroot",
                    return_value=nullcontext(),
                ):
                    with patch("debian_usb.rebuild_iso._run_in_chroot") as run_in_chroot:
                        rebuild_iso._install_packages_in_chroot(
                            live_root,
                            ["nvme-cli", "nmap"],
                            None,
                            apt_source_root=Path("/tmp/iso-root"),
                        )
            self.assertFalse((live_root / "usr" / "sbin" / "policy-rc.d").exists())

        commands = [call.args[1] for call in run_in_chroot.call_args_list]
        self.assertEqual(commands[0][-1], "update")
        self.assertEqual(commands[1][-3:], ["--no-install-recommends", "nvme-cli", "nmap"])
        self.assertEqual(commands[2][-1], "clean")

    def test_mounted_chroot_lazily_detaches_busy_bind_mounts_in_reverse_order(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            live_root = Path(temp_dir) / "live-root"
            live_root.mkdir()
            unmount_commands: list[list[str]] = []

            def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
                unmount_commands.append(command)
                returncode = 0 if "--lazy" in command else 1
                stderr = "" if returncode == 0 else "target is busy"
                return subprocess.CompletedProcess(command, returncode, "", stderr)

            with patch("debian_usb.rebuild_iso._run_logged") as run_logged:
                with patch("debian_usb.rebuild_iso.subprocess.run", side_effect=fake_run):
                    with rebuild_iso._mounted_chroot(live_root, None):
                        pass

        mount_commands = [call.args[0] for call in run_logged.call_args_list]
        self.assertEqual(mount_commands[0], ["mount", "--bind", str(live_root), str(live_root)])
        self.assertEqual(mount_commands[1], ["mount", "--make-rprivate", str(live_root)])
        normal_commands = [command for command in unmount_commands if "--lazy" not in command]
        lazy_commands = [command for command in unmount_commands if "--lazy" in command]
        expected_targets = [
            str(live_root / "run"),
            str(live_root / "sys"),
            str(live_root / "proc"),
            str(live_root / "dev/pts"),
            str(live_root / "dev"),
            str(live_root),
        ]
        self.assertEqual([command[-1] for command in normal_commands], expected_targets)
        self.assertEqual([command[-1] for command in lazy_commands], expected_targets)
        self.assertTrue(all("--recursive" in command for command in unmount_commands))

    def test_mounted_chroot_cleans_partial_mount_setup(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            live_root = Path(temp_dir) / "live-root"
            live_root.mkdir()
            unmount_commands: list[list[str]] = []

            def fake_run_logged(command: list[str], **_: object) -> None:
                if command[-1] == str(live_root / "proc"):
                    raise RuntimeError("mount failed")

            def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
                unmount_commands.append(command)
                return subprocess.CompletedProcess(command, 0, "", "")

            with patch("debian_usb.rebuild_iso._run_logged", side_effect=fake_run_logged):
                with patch("debian_usb.rebuild_iso.subprocess.run", side_effect=fake_run):
                    with self.assertRaisesRegex(RuntimeError, "mount failed"):
                        with rebuild_iso._mounted_chroot(live_root, None):
                            pass

        self.assertEqual(
            [command[-1] for command in unmount_commands],
            [str(live_root / "dev/pts"), str(live_root / "dev"), str(live_root)],
        )

    def test_squashfs_compression_inspection_uses_selected_processors(self) -> None:
        rootfs_path = Path("/tmp/filesystem.squashfs")
        completed = subprocess.CompletedProcess(
            ["unsquashfs"],
            0,
            "Compression: xz\n",
            "",
        )
        with patch("debian_usb.rebuild_iso._squashfs_processor_count", return_value=3), patch(
            "debian_usb.rebuild_iso.subprocess.run",
            return_value=completed,
        ) as run:
            compression = rebuild_iso._squashfs_compression(rootfs_path)

        self.assertEqual(compression, "xz")
        self.assertEqual(
            run.call_args.args[0],
            ["unsquashfs", "-processors", "3", "-s", str(rootfs_path)],
        )

    def test_build_chroot_apt_source_lines_rewrites_ubuntu_cdrom_source(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            live_root = root / "live-root"
            iso_root = root / "iso-root"
            apt_dir = live_root / "etc" / "apt"
            apt_dir.mkdir(parents=True, exist_ok=True)
            (apt_dir / "sources.list").write_text(
                "\n".join(
                    [
                        "deb cdrom:[Ubuntu 24.04 LTS _Noble Numbat_ - Release amd64]/ noble main restricted",
                        "deb http://archive.ubuntu.com/ubuntu noble main restricted",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            write_apt_archive_fixture(iso_root, "noble", ["main", "restricted"])

            lines = rebuild_iso._build_chroot_apt_source_lines(live_root, iso_root)

        self.assertEqual(
            lines,
            [
                f"deb file:{rebuild_iso.CHROOT_ISO_SOURCE_MOUNT} noble main restricted",
                "deb http://archive.ubuntu.com/ubuntu noble main restricted",
            ],
        )

    def test_rewrite_checksum_files_updates_md5_and_sha256(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            iso_root = Path(temp_dir)
            payload = iso_root / "live" / "initrd.img"
            payload.parent.mkdir(parents=True, exist_ok=True)
            payload.write_text("new-initrd", encoding="utf-8")
            md5_path = iso_root / "md5sum.txt"
            sha256_path = iso_root / "sha256sum.txt"
            md5_path.write_text("stale\n", encoding="utf-8")
            sha256_path.write_text("stale\n", encoding="utf-8")
            md5_path.chmod(0o444)
            sha256_path.chmod(0o444)

            rebuild_iso._rewrite_checksum_files(iso_root)

            md5_lines = md5_path.read_text(encoding="utf-8").splitlines()
            sha256_lines = sha256_path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(md5_path.stat().st_mode & 0o777, 0o444)
            self.assertEqual(sha256_path.stat().st_mode & 0o777, 0o444)

        self.assertEqual(len(md5_lines), 1)
        self.assertEqual(len(sha256_lines), 1)
        self.assertTrue(md5_lines[0].endswith("./live/initrd.img"))
        self.assertTrue(sha256_lines[0].endswith("./live/initrd.img"))


if __name__ == "__main__":
    unittest.main()
