from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import ANY, patch

from debian_usb import installer_sources
from debian_usb.boot_inspect import validate_prepared_netinst_initrd
from debian_usb.iso_source import DirectorySource


class InstallerSourceTests(unittest.TestCase):
    @staticmethod
    @contextmanager
    def _accept_installer_only_payload():
        with patch(
            "debian_usb.installer_sources.validate_netinst_payload_iso",
            return_value={"media_class": "installer", "installer_entry_count": 1, "live_entry_count": 0},
        ), patch(
            "debian_usb.installer_sources._enforce_exact_iso_scan_filename",
            return_value={"enforced": True, "changed": True},
        ):
            yield

    @staticmethod
    def _upstream_iso_scan_postinst_text() -> str:
        return (
            "#!/bin/sh\n"
            "set -e\n"
            "db_get () { RET=${REQUESTED_ISO-}; }\n"
            "log () { :; }\n"
            "mount_device () { return 1; }\n"
            "use_this_iso () { exit 0; }\n"
            "scan_device_for_isos () { : >\"$SCAN_MARKER\"; }\n"
            "selected_devices=/dev/test1\n"
            "STATE=19\n"
            "case $STATE in\n"
            "    19)\n"
            "\t\tlog \"selected_device(s)='$selected_devices'\"\n"
            "\n"
            "\t\tscan_device_for_isos 0 \"$selected_devices\"\n"
            "\t\t;;\n"
            "esac\n"
        )

    @classmethod
    def _write_test_iso_scan_initrd(cls, archive_path: Path, tree_root: Path) -> None:
        postinst = tree_root / "var/lib/dpkg/info/iso-scan.postinst"
        postinst.parent.mkdir(parents=True, exist_ok=True)
        postinst.write_text(cls._upstream_iso_scan_postinst_text(), encoding="utf-8")
        postinst.chmod(0o755)
        installer_sources._repack_initrd_archive(tree_root, archive_path)

    @unittest.skipUnless(all(shutil.which(command) for command in ("cpio", "find", "gzip")), "requires cpio/find/gzip")
    def test_overlay_preseed_and_exact_iso_policy_share_one_archive_pass(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            original = root / "original.gz"
            self._write_test_iso_scan_initrd(original, root / "original-tree")
            original_bytes = original.read_bytes()
            bundled = root / "bundled.gz"
            shutil.copy2(original, bundled)
            overlay = root / "overlay"
            overlay.mkdir()
            (overlay / "marker").write_text("overlay fixture", encoding="utf-8")
            preseed = root / "preseed.cfg"
            preseed.write_text("d-i debian-installer/locale string en_US.UTF-8\n", encoding="utf-8")
            with patch.object(installer_sources, "DEFAULT_WORK_DIR", root / "work"), patch.object(
                installer_sources, "DEFAULT_STATE_DIR", root / "state"
            ), patch.object(installer_sources, "DEFAULT_LOG_DIR", root / "logs"), patch.object(
                installer_sources, "_extract_initrd_archive", wraps=installer_sources._extract_initrd_archive
            ) as extract, patch.object(
                installer_sources, "_repack_initrd_archive", wraps=installer_sources._repack_initrd_archive
            ) as repack:
                with installer_sources._InstallerInitrdSession(bundled) as session:
                    installer_sources._embed_initrd_overlay_into_initrd(
                        initrd_path=bundled, overlay_dir=overlay, bundle_root=root / "bundle", session=session)
                    installer_sources._embed_repo_preseed_into_initrd(
                        initrd_path=bundled, preseed_path=preseed, bundle_root=root / "bundle", session=session)
                    installer_sources._enforce_exact_iso_scan_filename(
                        initrd_path=bundled, bundle_root=root / "bundle", session=session)
                self.assertEqual(extract.call_count, 1)
                self.assertEqual(repack.call_count, 1)
            self.assertEqual(original.read_bytes(), original_bytes)
            expanded = root / "expanded"
            installer_sources._extract_initrd_archive(bundled, expanded)
            self.assertEqual((expanded / "marker").read_text(encoding="utf-8"), "overlay fixture")
            self.assertEqual((expanded / "preseed.cfg").read_bytes(), preseed.read_bytes())
            self.assertIn(installer_sources.ISO_SCAN_EXACT_SELECTION_MARKER,
                          (expanded / "var/lib/dpkg/info/iso-scan.postinst").read_text(encoding="utf-8"))

    def test_detect_kernel_version_from_kernel_file_prefers_filename(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            kernel = Path(temp_dir) / "vmlinuz-7.0.13+deb14-amd64"
            kernel.write_text("kernel", encoding="utf-8")
            self.assertEqual(
                installer_sources._detect_kernel_version_from_kernel_file(kernel),
                "7.0.13+deb14-amd64",
            )

    @unittest.skipUnless(shutil.which("sh"), "requires sh")
    def test_patch_iso_scan_postinst_enforces_requested_iso_before_any_scan(self) -> None:
        upstream = self._upstream_iso_scan_postinst_text()

        patched = installer_sources._patch_iso_scan_postinst_text(upstream)

        self.assertEqual(patched.count(installer_sources.ISO_SCAN_EXACT_SELECTION_MARKER), 1)
        self.assertEqual(installer_sources._patch_iso_scan_postinst_text(patched), patched)
        exact_selection_offset = patched.index(installer_sources.ISO_SCAN_EXACT_SELECTION_MARKER)
        exact_mount_offset = patched.index('use_this_iso "$requested_iso" "$selected_devices"')
        fallback_scan_offset = patched.index('scan_device_for_isos 0 "$selected_devices"')
        self.assertLess(exact_selection_offset, exact_mount_offset)
        self.assertLess(exact_mount_offset, fallback_scan_offset)
        self.assertIn('db_get iso-scan/filename', patched)
        self.assertIn('Refusing invalid requested ISO path', patched)
        self.assertIn('Exact ISO selection refuses multiple or unsafe source devices', patched)
        self.assertIn('Requested ISO does not exist', patched)
        syntax_check = subprocess.run(
            ["sh", "-n"],
            input=patched,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        self.assertEqual(syntax_check.returncode, 0, syntax_check.stderr)

    @unittest.skipUnless(shutil.which("sh"), "requires sh")
    def test_patched_iso_scan_mount_failure_never_falls_back_to_sibling_scan(self) -> None:
        patched = installer_sources._patch_iso_scan_postinst_text(self._upstream_iso_scan_postinst_text())
        with tempfile.TemporaryDirectory() as temp_dir:
            scan_marker = Path(temp_dir) / "scan-called"
            sh_path = shutil.which("sh")
            self.assertIsNotNone(sh_path)
            result = subprocess.run(
                [str(sh_path)],
                input=patched,
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
                env={
                    "PATH": os.environ.get("PATH", ""),
                    "REQUESTED_ISO": "/debian-netinst/debian-netinst.iso",
                    "SCAN_MARKER": str(scan_marker),
                },
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(scan_marker.exists())

    def test_patch_iso_scan_postinst_rejects_unknown_upstream_layout(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "state-19 anchor"):
            installer_sources._patch_iso_scan_postinst_text("#!/bin/sh\nexit 0\n")

    @unittest.skipUnless(shutil.which("sh"), "requires sh")
    def test_patch_iso_scan_postinst_is_release_independent_within_state_19(self) -> None:
        upstream = self._upstream_iso_scan_postinst_text().replace(
            '\t\tscan_device_for_isos 0 "$selected_devices"',
            '            scan_device_for_isos   0   "${selected_devices}"',
        )

        patched = installer_sources._patch_iso_scan_postinst_text(upstream)

        exact_mount_offset = patched.index('use_this_iso "$requested_iso" "$selected_devices"')
        fallback_scan_offset = patched.index('scan_device_for_isos   0   "${selected_devices}"')
        self.assertLess(exact_mount_offset, fallback_scan_offset)
        syntax_check = subprocess.run(
            ["sh", "-n"],
            input=patched,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        self.assertEqual(syntax_check.returncode, 0, syntax_check.stderr)

    @unittest.skipUnless(
        all(shutil.which(command) for command in installer_sources.INSTALLER_INITRD_EXACT_ISO_REQUIRED_COMMANDS),
        "requires cpio, find, gzip, and sh",
    )
    def test_enforce_exact_iso_scan_filename_rewrites_real_initrd_archive(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            initrd = root / "initrd.gz"
            self._write_test_iso_scan_initrd(initrd, root / "source-tree")
            bundle_root = root / "bundle"
            with patch.object(installer_sources, "DEFAULT_WORK_DIR", root / "work"), patch.object(
                installer_sources, "DEFAULT_STATE_DIR", root / "state"
            ), patch.object(installer_sources, "DEFAULT_LOG_DIR", root / "log"):
                manifest = installer_sources._enforce_exact_iso_scan_filename(
                    initrd_path=initrd,
                    bundle_root=bundle_root,
                )

            extracted = root / "extracted"
            installer_sources._extract_initrd_archive(initrd, extracted)
            postinst = extracted / "var/lib/dpkg/info/iso-scan.postinst"
            patched = postinst.read_text(encoding="utf-8")
            self.assertTrue(manifest["enforced"])
            self.assertTrue(manifest["changed"])
            self.assertEqual(manifest["policy"], "exact-request-or-fail-without-scan")
            self.assertEqual(postinst.stat().st_mode & 0o777, 0o755)
            self.assertEqual(patched.count(installer_sources.ISO_SCAN_EXACT_SELECTION_MARKER), 1)
            self.assertLess(
                patched.index('use_this_iso "$requested_iso" "$selected_devices"'),
                patched.index('scan_device_for_isos 0 "$selected_devices"'),
            )
            manifest_payload = json.loads(
                (bundle_root / ".debian-usb/installer/iso-scan-selection-manifest.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertTrue(manifest_payload["enforced"])
            self.assertTrue(validate_prepared_netinst_initrd(initrd)["enforced"])

    @unittest.skipUnless(
        all(shutil.which(command) for command in installer_sources.INSTALLER_INITRD_EXACT_ISO_REQUIRED_COMMANDS),
        "requires cpio, find, gzip, and sh",
    )
    def test_validate_prepared_netinst_initrd_rejects_unpatched_iso_scan(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            initrd = root / "initrd.gz"
            self._write_test_iso_scan_initrd(initrd, root / "source-tree")

            with self.assertRaisesRegex(ValueError, "regenerate the managed Netinst source bundle"):
                validate_prepared_netinst_initrd(initrd)

    def test_validate_netinst_payload_iso_rejects_live_media_and_live_installer_udeb(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            iso = root / "debian-netinst.iso"
            iso.write_bytes(b"opaque-iso")
            media_root = root / "media"
            (media_root / "boot/grub").mkdir(parents=True)
            (media_root / "boot/grub/grub.cfg").write_text(
                "menuentry 'Install' {\n"
                "  linux /install.amd/vmlinuz ---\n"
                "  initrd /install.amd/initrd.gz\n"
                "}\n",
                encoding="utf-8",
            )
            (media_root / "install.amd").mkdir()
            (media_root / "install.amd/vmlinuz").write_bytes(b"kernel")
            (media_root / "install.amd/initrd.gz").write_bytes(b"initrd")

            with patch(
                "debian_usb.boot_inspect.open_source",
                return_value=DirectorySource(str(media_root)),
            ):
                result = installer_sources.validate_netinst_payload_iso(str(iso), "debian")
            self.assertEqual(result["media_class"], "installer")

            (media_root / "live").mkdir()
            (media_root / "live/filesystem.squashfs").write_bytes(b"live-rootfs")
            with patch(
                "debian_usb.boot_inspect.open_source",
                return_value=DirectorySource(str(media_root)),
            ):
                with self.assertRaisesRegex(ValueError, "Live/Casper media"):
                    installer_sources.validate_netinst_payload_iso(str(iso), "debian")

            (media_root / "live/filesystem.squashfs").unlink()
            (media_root / "live").rmdir()
            live_installer = media_root / "pool/main/l/live-installer/live-installer_60_amd64.udeb"
            live_installer.parent.mkdir(parents=True)
            live_installer.write_bytes(b"udeb")
            with patch(
                "debian_usb.boot_inspect.open_source",
                return_value=DirectorySource(str(media_root)),
            ):
                with self.assertRaisesRegex(ValueError, "live-installer udebs"):
                    installer_sources.validate_netinst_payload_iso(str(iso), "debian")

    def test_prepare_managed_installer_source_rebuilds_selected_debian_netinst_modules(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            kernel = root / "linux"
            initrd = root / "initrd.gz"
            iso = root / "debian-netinst.iso"
            kernel.write_text("kernel", encoding="utf-8")
            initrd.write_text("initrd", encoding="utf-8")
            iso.write_text("iso", encoding="utf-8")

            def fake_rebuild(**kwargs: object) -> dict[str, object]:
                Path(kwargs["initrd_path"]).write_text("rebuilt-initrd", encoding="utf-8")
                return {"rebuild_mode": "module-tree-copy", "kernel_version": "7.0.13+deb14-amd64"}

            with self._accept_installer_only_payload(), patch(
                "debian_usb.installer_sources._ensure_installer_assets_align_with_iso",
                return_value=None,
            ), patch(
                "debian_usb.installer_sources._rebuild_debian_netinst_initrd_modules",
                side_effect=fake_rebuild,
            ) as rebuild:
                bundle = installer_sources.prepare_managed_installer_source(
                    "debian",
                    "netinst",
                    str(kernel),
                    str(initrd),
                    iso_path=str(iso),
                    output_dir=str(root / "bundle"),
                    extra_initrd_modules=["xxhash_generic", "lz4"],
                    module_source_strategy="host-kernel",
                )

            rebuild.assert_called_once()
            self.assertEqual(bundle["selected_extra_modules"], ["xxhash_generic", "lz4"])
            self.assertEqual((root / "bundle" / "hd-media" / "initrd.gz").read_text(encoding="utf-8"), "rebuilt-initrd")
            self.assertFalse((root / "bundle" / "install.amd").exists())
            self.assertEqual(bundle["initrd_rebuild"]["kernel_version"], "7.0.13+deb14-amd64")

    @unittest.skipUnless(
        all(shutil.which(command) for command in installer_sources.INSTALLER_INITRD_EXACT_ISO_REQUIRED_COMMANDS),
        "requires cpio, find, gzip, and sh",
    )
    def test_prepare_managed_installer_source_patches_module_rebuilt_initrd_last(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            kernel = root / "linux"
            initrd = root / "downloaded-initrd.gz"
            iso = root / "debian-netinst.iso"
            kernel.write_text("kernel", encoding="utf-8")
            initrd.write_text("downloaded-initrd", encoding="utf-8")
            iso.write_text("opaque-netinst-iso", encoding="utf-8")

            def fake_rebuild(**kwargs: object) -> dict[str, object]:
                self._write_test_iso_scan_initrd(Path(kwargs["initrd_path"]), root / "rebuilt-initrd-tree")
                return {"rebuild_mode": "module-tree-copy", "kernel_version": "7.0.13+deb14-amd64"}

            with patch(
                "debian_usb.installer_sources.validate_netinst_payload_iso",
                return_value={"media_class": "installer", "installer_entry_count": 1, "live_entry_count": 0},
            ), patch(
                "debian_usb.installer_sources._ensure_installer_assets_align_with_iso",
                return_value=None,
            ), patch(
                "debian_usb.installer_sources._rebuild_debian_netinst_initrd_modules",
                side_effect=fake_rebuild,
            ) as rebuild, patch.object(
                installer_sources, "DEFAULT_WORK_DIR", root / "work"
            ), patch.object(
                installer_sources, "DEFAULT_STATE_DIR", root / "state"
            ), patch.object(
                installer_sources, "DEFAULT_LOG_DIR", root / "log"
            ):
                bundle = installer_sources.prepare_managed_installer_source(
                    "debian",
                    "netinst",
                    str(kernel),
                    str(initrd),
                    iso_path=str(iso),
                    output_dir=str(root / "bundle"),
                    extra_initrd_modules=["xxhash_generic", "lz4"],
                    module_source_strategy="host-kernel",
                )

            rebuild.assert_called_once()
            self.assertEqual(initrd.read_text(encoding="utf-8"), "downloaded-initrd")
            extracted = root / "prepared-initrd"
            installer_sources._extract_initrd_archive(Path(bundle["initrd_path"]), extracted)
            patched_postinst = (extracted / "var/lib/dpkg/info/iso-scan.postinst").read_text(encoding="utf-8")
            self.assertEqual(patched_postinst.count(installer_sources.ISO_SCAN_EXACT_SELECTION_MARKER), 1)
            self.assertTrue(bundle["iso_scan_selection"]["enforced"])
            self.assertTrue(bundle["iso_scan_selection"]["changed"])
            source_manifest = json.loads(Path(bundle["manifest_path"]).read_text(encoding="utf-8"))
            self.assertTrue(source_manifest["iso_scan_filename_enforced"])
            self.assertEqual(
                source_manifest["iso_scan_selection_manifest"],
                "/.debian-usb/installer/iso-scan-selection-manifest.json",
            )

    def test_prepare_managed_netboot_applies_selected_initrd_root_overlay(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            kernel = root / "downloaded-vmlinuz"
            initrd = root / "downloaded-initrd.gz"
            overlay = root / "initrd/debian/netboot"
            kernel.write_text("netboot-kernel", encoding="utf-8")
            initrd.write_text("netboot-initrd", encoding="utf-8")
            overlay.mkdir(parents=True)
            (overlay / "stage-marker").write_text("netboot\n", encoding="utf-8")
            overlay_manifest = {
                "overlay_dir": str(overlay.resolve()),
                "embedded_root": "/",
            }

            with patch(
                "debian_usb.installer_sources._embed_initrd_overlay_into_initrd",
                return_value=overlay_manifest,
            ) as embed_overlay:
                bundle = installer_sources.prepare_managed_installer_source(
                    "debian",
                    "netboot",
                    str(kernel),
                    str(initrd),
                    output_dir=str(root / "bundle"),
                    initrd_overlay_dir=str(overlay),
                )

            bundled_initrd = root / "bundle/netboot/initrd.gz"
            embed_overlay.assert_called_once_with(
                initrd_path=bundled_initrd,
                overlay_dir=overlay.resolve(),
                bundle_root=root / "bundle",
                session=ANY,
            )
            self.assertEqual(bundle["initrd_overlay"], overlay_manifest)
            source_manifest = json.loads(Path(bundle["manifest_path"]).read_text(encoding="utf-8"))
            self.assertTrue(source_manifest["initrd_overlay_included"])
            self.assertEqual(source_manifest["initrd_overlay_manifest"], "/.debian-usb/installer/initrd-overlay-manifest.json")
            self.assertFalse((root / "bundle/payload").exists())

    @unittest.skipUnless(
        all(shutil.which(command) for command in ("cpio", "find", "gzip")),
        "requires cpio, find, and gzip",
    )
    def test_embed_initrd_overlay_merges_files_at_archive_root(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            initrd = root / "initrd.gz"
            source_tree = root / "source-tree"
            source_tree.mkdir()
            (source_tree / "existing").write_text("preserved\n", encoding="utf-8")
            installer_sources._repack_initrd_archive(source_tree, initrd)
            overlay = root / "overlay"
            (overlay / "etc/debian-usb").mkdir(parents=True)
            (overlay / "preseed.env").write_text('PRESEED_ROOT_PASSWORD=""\n', encoding="utf-8")
            (overlay / "etc/debian-usb/stage").write_text("netinst\n", encoding="utf-8")
            bundle_root = root / "bundle"

            with patch.object(installer_sources, "DEFAULT_WORK_DIR", root / "work"), patch.object(
                installer_sources, "DEFAULT_STATE_DIR", root / "state"
            ), patch.object(installer_sources, "DEFAULT_LOG_DIR", root / "log"):
                manifest = installer_sources._embed_initrd_overlay_into_initrd(
                    initrd_path=initrd,
                    overlay_dir=overlay,
                    bundle_root=bundle_root,
                )

            extracted = root / "extracted"
            installer_sources._extract_initrd_archive(initrd, extracted)
            self.assertEqual((extracted / "existing").read_text(encoding="utf-8"), "preserved\n")
            self.assertEqual((extracted / "etc/debian-usb/stage").read_text(encoding="utf-8"), "netinst\n")
            self.assertEqual((extracted / "preseed.env").read_text(encoding="utf-8"), 'PRESEED_ROOT_PASSWORD=""\n')
            self.assertEqual(manifest["embedded_root"], "/")
            self.assertEqual(manifest["overlay_dir"], str(overlay.resolve()))
            self.assertTrue((bundle_root / ".debian-usb/installer/initrd-overlay-manifest.json").is_file())

    def test_finalize_bundle_access_hands_managed_source_tree_to_sudo_user(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            managed_root = root / "sources"
            bundle_root = managed_root / "debian/netinst/bundle"
            asset_dir = bundle_root / "hd-media"
            asset_dir.mkdir(parents=True)
            artifact = asset_dir / "initrd.gz"
            artifact.write_bytes(b"installer-initrd")
            managed_parents = (managed_root, managed_root / "debian", managed_root / "debian/netinst")
            for directory in (*managed_parents, bundle_root, asset_dir):
                directory.chmod(0o700)
            artifact.chmod(0o600)

            with patch.object(installer_sources, "SOURCE_BUNDLE_ROOT", managed_root), patch(
                "debian_usb.installer_sources.os.geteuid",
                return_value=0,
            ), patch.dict(
                os.environ,
                {"SUDO_UID": "1000", "SUDO_GID": "1000"},
                clear=False,
            ), patch("debian_usb.installer_sources.os.chown") as chown:
                installer_sources._finalize_bundle_access(bundle_root)

            for directory in managed_parents:
                self.assertEqual(directory.stat().st_mode & 0o7777, 0o2770)
                self.assertTrue(
                    any(
                        call.args == (directory, -1, 1000)
                        and call.kwargs == {"follow_symlinks": False}
                        for call in chown.call_args_list
                    )
                )
            for directory in (bundle_root, asset_dir):
                self.assertEqual(directory.stat().st_mode & 0o7777, 0o755)
                self.assertTrue(
                    any(
                        call.args == (directory, 1000, 1000)
                        and call.kwargs == {"follow_symlinks": False}
                        for call in chown.call_args_list
                    )
                )
            self.assertEqual(artifact.stat().st_mode & 0o7777, 0o644)
            self.assertTrue(
                any(
                    call.args == (artifact, 1000, 1000)
                    and call.kwargs == {"follow_symlinks": False}
                    for call in chown.call_args_list
                )
            )

    def test_finalize_bundle_access_preserves_custom_output_parent(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            custom_parent = root / "custom-output"
            bundle_root = custom_parent / "bundle"
            bundle_root.mkdir(parents=True)
            artifact = bundle_root / "initrd.gz"
            artifact.write_bytes(b"installer-initrd")
            custom_parent.chmod(0o750)

            with patch.object(installer_sources, "SOURCE_BUNDLE_ROOT", root / "managed-sources"), patch(
                "debian_usb.installer_sources.os.geteuid",
                return_value=0,
            ), patch.dict(
                os.environ,
                {"SUDO_UID": "1000", "SUDO_GID": "1000"},
                clear=False,
            ), patch("debian_usb.installer_sources.os.chown") as chown:
                installer_sources._finalize_bundle_access(bundle_root)

            self.assertEqual(custom_parent.stat().st_mode & 0o7777, 0o750)
            self.assertFalse(any(call.args and call.args[0] == custom_parent for call in chown.call_args_list))
            self.assertTrue(
                any(
                    call.args == (bundle_root, 1000, 1000)
                    and call.kwargs == {"follow_symlinks": False}
                    for call in chown.call_args_list
                )
            )

    def test_finalize_bundle_access_rejects_symlinked_managed_parent(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            managed_root = root / "sources"
            managed_root.mkdir()
            managed_root.chmod(0o700)
            redirected_profile = root / "redirected-debian"
            bundle_root = redirected_profile / "netinst/bundle"
            bundle_root.mkdir(parents=True)
            (managed_root / "debian").symlink_to(redirected_profile, target_is_directory=True)
            lexical_bundle_root = managed_root / "debian/netinst/bundle"

            with patch.object(installer_sources, "SOURCE_BUNDLE_ROOT", managed_root), patch(
                "debian_usb.installer_sources.os.geteuid",
                return_value=0,
            ), patch.dict(
                os.environ,
                {"SUDO_UID": "1000", "SUDO_GID": "1000"},
                clear=False,
            ), patch("debian_usb.installer_sources.os.chown") as chown:
                with self.assertRaisesRegex(RuntimeError, "managed installer source parent is not a real directory"):
                    installer_sources._finalize_bundle_access(lexical_bundle_root)

            self.assertEqual(managed_root.stat().st_mode & 0o7777, 0o700)
            chown.assert_not_called()

    def test_prepare_managed_installer_source_returns_traversable_managed_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            managed_root = root / "sources"
            managed_root.mkdir()
            managed_root.chmod(0o700)
            kernel = root / "downloaded-vmlinuz"
            initrd = root / "downloaded-initrd.gz"
            iso = root / "debian-netinst.iso"
            kernel.write_text("hd-media-kernel", encoding="utf-8")
            initrd.write_text("hd-media-initrd", encoding="utf-8")
            iso.write_text("opaque-netinst-iso", encoding="utf-8")

            with self._accept_installer_only_payload(), patch(
                "debian_usb.installer_sources._ensure_installer_assets_align_with_iso",
                return_value=None,
            ), patch.object(installer_sources, "SOURCE_BUNDLE_ROOT", managed_root), patch(
                "debian_usb.installer_sources.os.geteuid",
                return_value=0,
            ), patch.dict(
                os.environ,
                {"SUDO_UID": "1000", "SUDO_GID": "1000"},
                clear=False,
            ), patch("debian_usb.installer_sources.os.chown") as chown:
                bundle = installer_sources.prepare_managed_installer_source(
                    "debian",
                    "netinst",
                    str(kernel),
                    str(initrd),
                    iso_path=str(iso),
                )

            bundle_root = Path(bundle["source_path"])
            managed_parents = (managed_root, managed_root / "debian", managed_root / "debian/netinst")
            self.assertEqual(bundle_root.parent, managed_parents[-1])
            for directory in managed_parents:
                self.assertEqual(directory.stat().st_mode & 0o7777, 0o2770)
                self.assertTrue(
                    any(
                        call.args == (directory, -1, 1000)
                        and call.kwargs == {"follow_symlinks": False}
                        for call in chown.call_args_list
                    )
                )
            self.assertEqual(bundle_root.stat().st_mode & 0o7777, 0o755)
            self.assertEqual((bundle_root / "managed-installer-source.json").stat().st_mode & 0o7777, 0o644)

    def test_prepare_managed_installer_source_keeps_hd_media_assets_separate_from_iso(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            kernel = root / "downloaded-vmlinuz"
            initrd = root / "downloaded-initrd.gz"
            iso = root / "debian-netinst.iso"
            kernel.write_text("hd-media-kernel", encoding="utf-8")
            initrd.write_text("hd-media-initrd", encoding="utf-8")
            iso.write_text("opaque-netinst-iso", encoding="utf-8")
            initrd.chmod(0o600)
            iso.chmod(0o600)

            with self._accept_installer_only_payload(), patch(
                "debian_usb.installer_sources._ensure_installer_assets_align_with_iso",
                return_value=None,
            ):
                bundle = installer_sources.prepare_managed_installer_source(
                    "debian",
                    "netinst",
                    str(kernel),
                    str(initrd),
                    iso_path=str(iso),
                    output_dir=str(root / "bundle"),
                )

            bundle_root = Path(bundle["source_path"])
            self.assertEqual((bundle_root / "hd-media/vmlinuz").read_text(encoding="utf-8"), "hd-media-kernel")
            self.assertEqual((bundle_root / "hd-media/initrd.gz").read_text(encoding="utf-8"), "hd-media-initrd")
            self.assertEqual((bundle_root / "payload/debian-netinst.iso").read_text(encoding="utf-8"), "opaque-netinst-iso")
            self.assertEqual((bundle_root / "payload/debian-netinst.iso").stat().st_mode & 0o777, 0o644)
            self.assertEqual((bundle_root / "hd-media/initrd.gz").stat().st_mode & 0o777, 0o644)
            self.assertEqual(bundle_root.stat().st_mode & 0o777, 0o755)
            self.assertFalse((bundle_root / "install.amd").exists())
            grub_cfg = (bundle_root / "boot/grub/grub.cfg").read_text(encoding="utf-8")
            self.assertIn("linux /hd-media/vmlinuz", grub_cfg)
            self.assertIn("initrd /hd-media/initrd.gz", grub_cfg)
            self.assertNotIn("/live/", grub_cfg)
            self.assertNotIn("squashfs", grub_cfg)

    def test_prepare_managed_installer_source_removes_stale_live_and_installer_layouts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            kernel = root / "downloaded-vmlinuz"
            initrd = root / "downloaded-initrd.gz"
            iso = root / "debian-netinst.iso"
            kernel.write_text("hd-media-kernel", encoding="utf-8")
            initrd.write_text("hd-media-initrd", encoding="utf-8")
            iso.write_text("opaque-netinst-iso", encoding="utf-8")
            bundle_root = root / "bundle"
            for stale_file in (
                bundle_root / "install.amd/vmlinuz",
                bundle_root / "live/filesystem.squashfs",
                bundle_root / ".disk/info",
                bundle_root / "payload/old-netinst.iso",
            ):
                stale_file.parent.mkdir(parents=True, exist_ok=True)
                stale_file.write_text("stale", encoding="utf-8")

            with self._accept_installer_only_payload(), patch(
                "debian_usb.installer_sources._ensure_installer_assets_align_with_iso",
                return_value=None,
            ):
                installer_sources.prepare_managed_installer_source(
                    "debian",
                    "netinst",
                    str(kernel),
                    str(initrd),
                    iso_path=str(iso),
                    output_dir=str(bundle_root),
                )

            self.assertFalse((bundle_root / "install.amd").exists())
            self.assertFalse((bundle_root / "live").exists())
            self.assertFalse((bundle_root / ".disk").exists())
            self.assertEqual(
                [path.name for path in (bundle_root / "payload").glob("*.iso")],
                ["debian-netinst.iso"],
            )

    def test_prepare_managed_installer_source_rejects_empty_hd_media_asset(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            kernel = root / "vmlinuz"
            initrd = root / "initrd.gz"
            iso = root / "debian-netinst.iso"
            kernel.write_bytes(b"")
            initrd.write_text("initrd", encoding="utf-8")
            iso.write_text("iso", encoding="utf-8")

            with self._accept_installer_only_payload(), patch(
                "debian_usb.installer_sources._ensure_installer_assets_align_with_iso",
                return_value=None,
            ):
                with self.assertRaisesRegex(ValueError, "kernel_path is empty"):
                    installer_sources.prepare_managed_installer_source(
                        "debian",
                        "netinst",
                        str(kernel),
                        str(initrd),
                        iso_path=str(iso),
                        output_dir=str(root / "bundle"),
                    )

    def test_prepare_managed_installer_source_rejects_iso_asset_version_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            kernel = root / "linux"
            initrd = root / "initrd.gz"
            iso = root / "debian-netinst.iso"
            kernel.write_text("kernel", encoding="utf-8")
            initrd.write_text("initrd", encoding="utf-8")
            iso.write_text("iso", encoding="utf-8")

            with self._accept_installer_only_payload(), patch(
                "debian_usb.installer_sources._detect_installer_member_versions_from_iso",
                return_value=("7.0.13+deb14-amd64", "7.0.13+deb14-amd64"),
            ), patch(
                "debian_usb.installer_sources._detect_kernel_version_from_kernel_file",
                return_value="7.0.14+deb14-amd64",
            ), patch(
                "debian_usb.installer_sources._detect_kernel_version_from_file",
                return_value="7.0.14+deb14-amd64",
            ):
                with self.assertRaisesRegex(RuntimeError, "installer ISO and selected installer assets do not match"):
                    installer_sources.prepare_managed_installer_source(
                        "debian",
                        "netinst",
                        str(kernel),
                        str(initrd),
                        iso_path=str(iso),
                        output_dir=str(root / "bundle"),
                    )

    def test_prepare_managed_installer_source_preflight_only_checks_alignment_without_writing_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            kernel = root / "linux"
            initrd = root / "initrd.gz"
            iso = root / "debian-netinst.iso"
            kernel.write_text("kernel", encoding="utf-8")
            initrd.write_text("initrd", encoding="utf-8")
            iso.write_text("iso", encoding="utf-8")

            with self._accept_installer_only_payload(), patch(
                "debian_usb.installer_sources._ensure_installer_assets_align_with_iso",
                return_value=None,
            ) as align:
                payload = installer_sources.prepare_managed_installer_source(
                    "debian",
                    "netinst",
                    str(kernel),
                    str(initrd),
                    iso_path=str(iso),
                    output_dir=str(root / "bundle"),
                    extra_initrd_modules=["xxhash_generic"],
                    module_source_strategy="host-kernel",
                    preflight_only=True,
                )

            align.assert_called_once()
            self.assertTrue(payload["aligned"])
            self.assertTrue(payload["preflight_only"])
            self.assertFalse((root / "bundle").exists())


    def test_copy_module_dependency_closure_includes_modules_dep_dependencies(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            module_tree_root = root / "modules" / "7.0.13+deb14-amd64"
            destination_root = root / "initrd" / "usr" / "lib" / "modules" / "7.0.13+deb14-amd64"
            module_tree_root.mkdir(parents=True, exist_ok=True)
            destination_root.mkdir(parents=True, exist_ok=True)
            lz4_module = module_tree_root / "kernel" / "crypto" / "lz4.ko.xz"
            lz4_compress_module = module_tree_root / "kernel" / "lib" / "lz4" / "lz4_compress.ko.xz"
            lz4_module.parent.mkdir(parents=True, exist_ok=True)
            lz4_compress_module.parent.mkdir(parents=True, exist_ok=True)
            lz4_module.write_text("lz4", encoding="utf-8")
            lz4_compress_module.write_text("lz4-compress", encoding="utf-8")
            (module_tree_root / "modules.dep").write_text(
                "kernel/crypto/lz4.ko.xz: kernel/lib/lz4/lz4_compress.ko.xz\n"
                "kernel/lib/lz4/lz4_compress.ko.xz:\n",
                encoding="utf-8",
            )

            copied = installer_sources._copy_module_dependency_closure(
                module_tree_root=module_tree_root,
                module_paths=[lz4_module],
                destination_root=destination_root,
            )

            self.assertEqual(
                copied,
                [
                    "kernel/crypto/lz4.ko.xz",
                    "kernel/lib/lz4/lz4_compress.ko.xz",
                ],
            )
            self.assertTrue((destination_root / "kernel" / "crypto" / "lz4.ko.xz").is_file())
            self.assertTrue((destination_root / "kernel" / "lib" / "lz4" / "lz4_compress.ko.xz").is_file())

    def test_copy_module_tree_metadata_copies_depmod_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            module_tree_root = root / "modules" / "6.12.86+deb13-amd64"
            destination_root = root / "initrd" / "usr" / "lib" / "modules" / "6.12.86+deb13-amd64"
            module_tree_root.mkdir(parents=True, exist_ok=True)
            destination_root.mkdir(parents=True, exist_ok=True)
            for name in [
                "modules.order",
                "modules.builtin",
                "modules.builtin.modinfo",
                "modules.builtin.bin",
                "modules.builtin.alias.bin",
            ]:
                (module_tree_root / name).write_text(name, encoding="utf-8")

            copied = installer_sources._copy_module_tree_metadata(module_tree_root, destination_root)

            self.assertEqual(
                copied,
                [
                    "modules.order",
                    "modules.builtin",
                    "modules.builtin.modinfo",
                    "modules.builtin.bin",
                    "modules.builtin.alias.bin",
                ],
            )
            self.assertTrue((destination_root / "modules.builtin.modinfo").is_file())

    def test_depmod_module_dir_argument_matches_usr_lib_modules_layout(self) -> None:
        initrd_tree = Path("/tmp/work/initrd-tree")
        destination_root = initrd_tree / "usr" / "lib" / "modules" / "6.12.86+deb13-amd64"
        self.assertEqual(
            installer_sources._depmod_module_dir_argument(initrd_tree, destination_root),
            "/usr/lib/modules",
        )

    @patch("debian_usb.installer_sources._repack_initrd_archive")
    @patch("debian_usb.installer_sources._run_logged")
    @patch("debian_usb.installer_sources._extract_udeb_payloads")
    @patch("debian_usb.installer_sources._build_installer_kernel_udebs")
    @patch("debian_usb.installer_sources._extract_initrd_archive")
    @patch("debian_usb.installer_sources._inspect_kernel_support")
    @patch("debian_usb.installer_sources._detect_kernel_version_from_kernel_file", return_value="7.0.13+deb14-amd64")
    @patch("debian_usb.installer_sources._detect_kernel_version_from_file", return_value="7.0.13+deb14-amd64")
    @patch("debian_usb.installer_sources._ensure_installer_initrd_prepare_deps")
    @patch("debian_usb.installer_sources.ensure_debian_rebuild_deps")
    def test_rebuild_debian_netinst_initrd_modules_falls_back_to_source_rebuild_when_module_is_missing(
        self,
        _ensure_rebuild_deps,
        _ensure_prepare_deps,
        _detect_kernel_version,
        _detect_kernel_image_version,
        inspect_kernel_support,
        extract_initrd_archive,
        build_installer_kernel_udebs,
        extract_udeb_payloads,
        run_logged,
        repack_initrd_archive,
    ) -> None:
        inspect_kernel_support.return_value = {
            "module_tree_dir": "/lib/modules/7.0.13+deb14-amd64",
            "modules": [{"name": "xxhash_generic", "found": False, "path": ""}],
            "notes": [],
        }
        build_installer_kernel_udebs.return_value = {
            "staged_udeb_dir": "/tmp/staged-udebs",
            "manifest_path": "/tmp/manifest.json",
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            bundle_root = root / "bundle"
            initrd = root / "initrd.gz"
            kernel = root / "vmlinuz"
            initrd.write_text("initrd", encoding="utf-8")
            kernel.write_text("kernel", encoding="utf-8")
            with patch.object(installer_sources, "DEFAULT_WORK_DIR", root / "work"), patch.object(
                installer_sources, "DEFAULT_STATE_DIR", root / "state"
            ), patch.object(installer_sources, "DEFAULT_LOG_DIR", root / "log"):
                manifest = installer_sources._rebuild_debian_netinst_initrd_modules(
                    kernel_path=kernel,
                    initrd_path=initrd,
                    selected_options=["xxhash_generic"],
                    bundle_root=bundle_root,
                    module_source_strategy="source-udeb",
                )

        extract_initrd_archive.assert_called_once()
        build_installer_kernel_udebs.assert_called_once()
        call_kwargs = build_installer_kernel_udebs.call_args.kwargs
        self.assertEqual(call_kwargs["module_names"], ["xxhash", "xxhash_generic"])
        self.assertEqual(call_kwargs["kernel_config_entries"], ["CONFIG_XXHASH=m", "CONFIG_CRYPTO_XXHASH=m"])
        extract_udeb_payloads.assert_called_once()
        run_logged.assert_called_once()
        repack_initrd_archive.assert_called_once()
        self.assertEqual(manifest["rebuild_mode"], "linux-source-udeb-rebuild")

    @patch("debian_usb.installer_sources._repack_initrd_archive")
    @patch("debian_usb.installer_sources._run_logged")
    @patch("debian_usb.installer_sources._build_installer_kernel_udebs")
    @patch("debian_usb.installer_sources._extract_initrd_archive")
    @patch("debian_usb.installer_sources._inspect_kernel_support")
    @patch("debian_usb.installer_sources._detect_kernel_version_from_kernel_file", return_value="7.0.13+deb14-amd64")
    @patch("debian_usb.installer_sources._detect_kernel_version_from_file", return_value="7.0.13+deb14-amd64")
    @patch("debian_usb.installer_sources._ensure_installer_initrd_prepare_deps")
    @patch("debian_usb.installer_sources.ensure_debian_rebuild_deps")
    def test_rebuild_debian_netinst_initrd_modules_accepts_builtin_xxhash_support(
        self,
        _ensure_rebuild_deps,
        _ensure_prepare_deps,
        _detect_kernel_version,
        _detect_kernel_image_version,
        inspect_kernel_support,
        extract_initrd_archive,
        build_installer_kernel_udebs,
        run_logged,
        repack_initrd_archive,
    ) -> None:
        inspect_kernel_support.return_value = {
            "module_tree_dir": "/lib/modules/7.0.13+deb14-amd64",
            "modules": [{"name": "xxhash_generic", "found": False, "path": ""}],
            "config_symbols": [
                {"symbol": "CONFIG_XXHASH", "value": "y", "line": "CONFIG_XXHASH=y"},
                {"symbol": "CONFIG_CRYPTO_XXHASH", "value": "", "line": ""},
            ],
            "notes": [],
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            bundle_root = root / "bundle"
            initrd = root / "initrd.gz"
            kernel = root / "vmlinuz"
            initrd.write_text("initrd", encoding="utf-8")
            kernel.write_text("kernel", encoding="utf-8")
            with patch.object(installer_sources, "DEFAULT_WORK_DIR", root / "work"), patch.object(
                installer_sources, "DEFAULT_STATE_DIR", root / "state"
            ), patch.object(installer_sources, "DEFAULT_LOG_DIR", root / "log"):
                manifest = installer_sources._rebuild_debian_netinst_initrd_modules(
                    kernel_path=kernel,
                    initrd_path=initrd,
                    selected_options=["xxhash_generic"],
                    bundle_root=bundle_root,
                    module_source_strategy="host-kernel",
                )

        extract_initrd_archive.assert_not_called()
        build_installer_kernel_udebs.assert_not_called()
        run_logged.assert_not_called()
        repack_initrd_archive.assert_not_called()
        self.assertEqual(manifest["rebuild_mode"], "builtin-support")
        self.assertFalse(manifest["changed"])
        self.assertEqual(manifest["builtin_options"], ["xxhash_generic"])

    @patch("debian_usb.installer_sources._detect_kernel_version_from_kernel_file", return_value="6.12.86+deb13-amd64")
    @patch("debian_usb.installer_sources._detect_kernel_version_from_file", return_value="7.0.13+deb14-amd64")
    @patch("debian_usb.installer_sources._ensure_installer_initrd_prepare_deps")
    def test_rebuild_debian_netinst_initrd_modules_rejects_mismatched_kernel_and_initrd_versions(
        self,
        _ensure_prepare_deps,
        _detect_kernel_version,
        _detect_kernel_image_version,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            bundle_root = root / "bundle"
            initrd = root / "initrd.gz"
            kernel = root / "vmlinuz"
            initrd.write_text("initrd", encoding="utf-8")
            kernel.write_text("kernel", encoding="utf-8")
            with patch.object(installer_sources, "DEFAULT_WORK_DIR", root / "work"), patch.object(
                installer_sources, "DEFAULT_STATE_DIR", root / "state"
            ), patch.object(installer_sources, "DEFAULT_LOG_DIR", root / "log"):
                with self.assertRaisesRegex(RuntimeError, "installer kernel and initrd versions do not match"):
                    installer_sources._rebuild_debian_netinst_initrd_modules(
                        kernel_path=kernel,
                        initrd_path=initrd,
                        selected_options=["xxhash_generic"],
                        bundle_root=bundle_root,
                        module_source_strategy="host-kernel",
                    )

    @patch("debian_usb.installer_sources._detect_kernel_version_from_kernel_file", return_value="7.0.13+deb14-amd64")
    @patch("debian_usb.installer_sources._detect_kernel_version_from_file", return_value="7.0.13+deb14-amd64")
    @patch("debian_usb.installer_sources._inspect_kernel_support", return_value={"module_tree_dir": "", "modules": [], "config_symbols": [], "notes": []})
    @patch("debian_usb.installer_sources._ensure_installer_initrd_prepare_deps")
    def test_rebuild_debian_netinst_initrd_modules_bootstraps_inspection_dependencies_before_work(
        self,
        ensure_prepare_deps,
        _inspect_kernel_support,
        _detect_kernel_version,
        _detect_kernel_image_version,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            bundle_root = root / "bundle"
            initrd = root / "initrd.gz"
            kernel = root / "vmlinuz"
            initrd.write_text("initrd", encoding="utf-8")
            kernel.write_text("kernel", encoding="utf-8")
            with patch.object(installer_sources, "DEFAULT_WORK_DIR", root / "work"), patch.object(
                installer_sources, "DEFAULT_STATE_DIR", root / "state"
            ), patch.object(installer_sources, "DEFAULT_LOG_DIR", root / "log"), patch(
                "debian_usb.installer_sources.ensure_debian_rebuild_deps"
            ), patch("debian_usb.installer_sources._extract_initrd_archive"), patch(
                "debian_usb.installer_sources._build_installer_kernel_udebs", return_value={"staged_udeb_dir": "/tmp", "manifest_path": "/tmp/manifest.json"}
            ), patch("debian_usb.installer_sources._extract_udeb_payloads"), patch(
                "debian_usb.installer_sources._run_logged"
            ), patch("debian_usb.installer_sources._repack_initrd_archive"):
                installer_sources._rebuild_debian_netinst_initrd_modules(
                    kernel_path=kernel,
                    initrd_path=initrd,
                    selected_options=["xxhash_generic"],
                    bundle_root=bundle_root,
                    module_source_strategy="source-udeb",
                )
        ensure_prepare_deps.assert_called_once()

    @patch("debian_usb.installer_sources._repack_initrd_archive")
    @patch("debian_usb.installer_sources._run_logged")
    @patch("debian_usb.installer_sources._copy_module_tree_metadata", return_value=["modules.order", "modules.builtin.modinfo"])
    @patch("debian_usb.installer_sources._copy_module_dependency_closure", return_value=["kernel/crypto/lz4.ko.xz", "kernel/lib/lz4/lz4_compress.ko.xz"])
    @patch("debian_usb.installer_sources._ensure_installer_initrd_module_copy_deps")
    @patch("debian_usb.installer_sources._extract_initrd_archive")
    @patch("debian_usb.installer_sources._inspect_kernel_support")
    @patch("debian_usb.installer_sources._detect_kernel_version_from_kernel_file", return_value="6.12.86+deb13-amd64")
    @patch("debian_usb.installer_sources._detect_kernel_version_from_file", return_value="6.12.86+deb13-amd64")
    @patch("debian_usb.installer_sources._ensure_installer_initrd_prepare_deps")
    def test_rebuild_debian_netinst_initrd_modules_host_kernel_strategy_uses_usr_lib_modules_depmod_dir(
        self,
        _ensure_prepare_deps,
        _detect_kernel_version,
        _detect_kernel_image_version,
        inspect_kernel_support,
        extract_initrd_archive,
        _ensure_module_copy_deps,
        _copy_module_dependency_closure,
        _copy_module_tree_metadata,
        run_logged,
        repack_initrd_archive,
    ) -> None:
        inspect_kernel_support.return_value = {
            "module_tree_dir": "/lib/modules/6.12.86+deb13-amd64",
            "modules": [
                {"name": "xxhash_generic", "found": True, "path": "/lib/modules/6.12.86+deb13-amd64/kernel/crypto/xxhash_generic.ko.xz"},
                {"name": "lz4", "found": True, "path": "/lib/modules/6.12.86+deb13-amd64/kernel/crypto/lz4.ko.xz"},
            ],
            "config_symbols": [
                {"symbol": "CONFIG_XXHASH", "value": "m", "line": "CONFIG_XXHASH=m"},
                {"symbol": "CONFIG_CRYPTO_XXHASH", "value": "m", "line": "CONFIG_CRYPTO_XXHASH=m"},
                {"symbol": "CONFIG_CRYPTO_LZ4", "value": "m", "line": "CONFIG_CRYPTO_LZ4=m"},
                {"symbol": "CONFIG_LZ4_COMPRESS", "value": "m", "line": "CONFIG_LZ4_COMPRESS=m"},
            ],
            "notes": [],
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            bundle_root = root / "bundle"
            initrd = root / "initrd.gz"
            kernel = root / "vmlinuz-6.12.86+deb13-amd64"
            module_tree_root = root / "kernel-support" / "lib" / "modules" / "6.12.86+deb13-amd64"
            (module_tree_root / "kernel" / "crypto").mkdir(parents=True, exist_ok=True)
            (module_tree_root / "kernel" / "crypto" / "xxhash_generic.ko.xz").write_text("", encoding="utf-8")
            (module_tree_root / "kernel" / "crypto" / "lz4.ko.xz").write_text("", encoding="utf-8")
            inspect_kernel_support.return_value["module_tree_dir"] = str(module_tree_root)
            initrd.write_text("initrd", encoding="utf-8")
            kernel.write_text("kernel", encoding="utf-8")

            def fake_extract_initrd(_archive_path: Path, destination_dir: Path) -> None:
                (destination_dir / "usr" / "lib" / "modules" / "6.12.86+deb13-amd64").mkdir(parents=True, exist_ok=True)
            extract_initrd_archive.side_effect = fake_extract_initrd
            with patch.object(installer_sources, "DEFAULT_WORK_DIR", root / "work"), patch.object(
                installer_sources, "DEFAULT_STATE_DIR", root / "state"
            ), patch.object(installer_sources, "DEFAULT_LOG_DIR", root / "log"):
                manifest = installer_sources._rebuild_debian_netinst_initrd_modules(
                    kernel_path=kernel,
                    initrd_path=initrd,
                    selected_options=["xxhash_generic", "lz4"],
                    bundle_root=bundle_root,
                    module_source_strategy="host-kernel",
                )

        self.assertEqual(manifest["rebuild_mode"], "module-tree-copy")
        extract_initrd_archive.assert_called_once()
        repack_initrd_archive.assert_called_once()
        run_logged.assert_called_once()
        depmod_cmd = run_logged.call_args.args[0]
        initrd_tree = Path(manifest["workspace_dir"]) / "installer-initrd"
        self.assertEqual(
            depmod_cmd,
            [
                "depmod",
                "-b",
                str(initrd_tree),
                "-m",
                "/usr/lib/modules",
                "6.12.86+deb13-amd64",
            ],
        )

    @patch("debian_usb.installer_sources._repack_initrd_archive")
    @patch("debian_usb.installer_sources._run_logged")
    @patch("debian_usb.installer_sources._copy_module_tree_metadata", return_value=["modules.order", "modules.builtin.modinfo"])
    @patch("debian_usb.installer_sources._copy_module_dependency_closure", return_value=["kernel/crypto/xxhash.ko.xz", "kernel/lib/lz4/lz4_compress.ko.xz"])
    @patch("debian_usb.installer_sources._ensure_installer_initrd_module_copy_deps")
    @patch("debian_usb.installer_sources._extract_initrd_archive")
    @patch("debian_usb.installer_sources._inspect_kernel_support")
    @patch("debian_usb.installer_sources._detect_kernel_version_from_kernel_file", return_value="6.12.94+deb13-amd64")
    @patch("debian_usb.installer_sources._detect_kernel_version_from_file", return_value="6.12.94+deb13-amd64")
    @patch("debian_usb.installer_sources._ensure_installer_initrd_prepare_deps")
    def test_rebuild_debian_netinst_initrd_modules_host_kernel_strategy_accepts_fallback_module_names(
        self,
        _ensure_prepare_deps,
        _detect_kernel_version,
        _detect_kernel_image_version,
        inspect_kernel_support,
        extract_initrd_archive,
        _ensure_module_copy_deps,
        copy_module_dependency_closure,
        _copy_module_tree_metadata,
        run_logged,
        repack_initrd_archive,
    ) -> None:
        inspect_kernel_support.return_value = {
            "module_tree_dir": "/tmp/kernel-support/root/usr/lib/modules/6.12.94+deb13-amd64",
            "modules": [
                {"name": "xxhash", "found": True, "path": "/tmp/kernel-support/root/usr/lib/modules/6.12.94+deb13-amd64/kernel/crypto/xxhash.ko.xz"},
                {"name": "xxhash_generic", "found": False, "path": ""},
                {"name": "lz4", "found": False, "path": ""},
                {"name": "lz4_compress", "found": True, "path": "/tmp/kernel-support/root/usr/lib/modules/6.12.94+deb13-amd64/kernel/lib/lz4/lz4_compress.ko.xz"},
            ],
            "config_symbols": [
                {"symbol": "CONFIG_XXHASH", "value": "m", "line": "CONFIG_XXHASH=m"},
                {"symbol": "CONFIG_CRYPTO_XXHASH", "value": "m", "line": "CONFIG_CRYPTO_XXHASH=m"},
                {"symbol": "CONFIG_CRYPTO_LZ4", "value": "m", "line": "CONFIG_CRYPTO_LZ4=m"},
                {"symbol": "CONFIG_LZ4_COMPRESS", "value": "m", "line": "CONFIG_LZ4_COMPRESS=m"},
            ],
            "notes": [],
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            bundle_root = root / "bundle"
            initrd = root / "initrd.gz"
            kernel = root / "vmlinuz-6.12.94+deb13-amd64"
            module_tree_root = root / "kernel-support" / "root" / "usr" / "lib" / "modules" / "6.12.94+deb13-amd64"
            (module_tree_root / "kernel" / "crypto").mkdir(parents=True, exist_ok=True)
            (module_tree_root / "kernel" / "lib" / "lz4").mkdir(parents=True, exist_ok=True)
            (module_tree_root / "kernel" / "crypto" / "xxhash.ko.xz").write_text("", encoding="utf-8")
            (module_tree_root / "kernel" / "lib" / "lz4" / "lz4_compress.ko.xz").write_text("", encoding="utf-8")
            initrd.write_text("initrd", encoding="utf-8")
            kernel.write_text("kernel", encoding="utf-8")
            inspect_kernel_support.return_value["module_tree_dir"] = str(module_tree_root)

            def fake_extract_initrd(_archive_path: Path, destination_dir: Path) -> None:
                (destination_dir / "usr" / "lib" / "modules" / "6.12.94+deb13-amd64").mkdir(parents=True, exist_ok=True)

            extract_initrd_archive.side_effect = fake_extract_initrd
            with patch.object(installer_sources, "DEFAULT_WORK_DIR", root / "work"), patch.object(
                installer_sources, "DEFAULT_STATE_DIR", root / "state"
            ), patch.object(installer_sources, "DEFAULT_LOG_DIR", root / "log"):
                manifest = installer_sources._rebuild_debian_netinst_initrd_modules(
                    kernel_path=kernel,
                    initrd_path=initrd,
                    selected_options=["xxhash_generic", "lz4"],
                    bundle_root=bundle_root,
                    module_source_strategy="host-kernel",
                )

        self.assertEqual(manifest["rebuild_mode"], "module-tree-copy")
        self.assertEqual(
            [resolution["found_modules"] for resolution in manifest["option_resolutions"]],
            [["xxhash"], ["lz4_compress"]],
        )
        copied_names = copy_module_dependency_closure.call_args.kwargs["module_paths"]
        self.assertEqual([path.name for path in copied_names], ["xxhash.ko.xz", "lz4_compress.ko.xz"])
        run_logged.assert_called_once()
        repack_initrd_archive.assert_called_once()

    @patch("debian_usb.installer_sources._detect_kernel_version_from_kernel_file", return_value="6.12.86+deb13-amd64")
    @patch("debian_usb.installer_sources._detect_kernel_version_from_file", return_value="6.12.86+deb13-amd64")
    @patch("debian_usb.installer_sources._inspect_kernel_support")
    @patch("debian_usb.installer_sources._ensure_installer_initrd_prepare_deps")
    def test_rebuild_debian_netinst_initrd_modules_host_kernel_strategy_rejects_missing_support(
        self,
        _ensure_prepare_deps,
        inspect_kernel_support,
        _detect_kernel_version,
        _detect_kernel_image_version,
    ) -> None:
        inspect_kernel_support.return_value = {
            "module_tree_dir": "/lib/modules/6.12.86+deb13-amd64",
            "modules": [
                {"name": "xxhash_generic", "found": False, "path": ""},
                {"name": "lz4", "found": False, "path": ""},
            ],
            "config_symbols": [
                {"symbol": "CONFIG_XXHASH", "value": "n", "line": "# CONFIG_XXHASH is not set"},
                {"symbol": "CONFIG_CRYPTO_XXHASH", "value": "n", "line": "# CONFIG_CRYPTO_XXHASH is not set"},
                {"symbol": "CONFIG_CRYPTO_LZ4", "value": "n", "line": "# CONFIG_CRYPTO_LZ4 is not set"},
                {"symbol": "CONFIG_LZ4_COMPRESS", "value": "n", "line": "# CONFIG_LZ4_COMPRESS is not set"},
            ],
            "notes": [],
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            bundle_root = root / "bundle"
            initrd = root / "initrd.gz"
            kernel = root / "vmlinuz"
            initrd.write_text("initrd", encoding="utf-8")
            kernel.write_text("kernel", encoding="utf-8")
            with patch.object(installer_sources, "DEFAULT_WORK_DIR", root / "work"), patch.object(
                installer_sources, "DEFAULT_STATE_DIR", root / "state"
            ), patch.object(installer_sources, "DEFAULT_LOG_DIR", root / "log"):
                with self.assertRaisesRegex(RuntimeError, "Choose 'Build UDEBs From Source' to continue"):
                    installer_sources._rebuild_debian_netinst_initrd_modules(
                        kernel_path=kernel,
                        initrd_path=initrd,
                        selected_options=["xxhash_generic", "lz4"],
                        bundle_root=bundle_root,
                        module_source_strategy="host-kernel",
                    )


if __name__ == "__main__":
    unittest.main()
