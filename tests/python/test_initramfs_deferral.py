from __future__ import annotations

import io
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from debian_usb.rebuild_iso import _deferred_initramfs_updates


@unittest.skipUnless(shutil.which("dpkg-divert"), "dpkg-divert integration requires dpkg")
class InitramfsDeferralTests(unittest.TestCase):
    def _exercise(self, vendor: bool, failure: bool, *, merged_usr: bool = False, owned: bool = False, unpack: bool = False, relative_wrapper: bool = False, upgrade_wrapper: bool = False, bin_alias: bool = False, legacy_payload: bool = False) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "usr/sbin").mkdir(parents=True)
            (root / "var/lib/dpkg").mkdir(parents=True)
            source = "/sbin/update-initramfs" if merged_usr else "/usr/sbin/update-initramfs"
            if merged_usr:
                (root / "sbin").symlink_to("usr/sbin")
            if bin_alias:
                (root / "bin").symlink_to("usr/bin")
            binary = root / source.lstrip("/")
            binary.write_text("original engine\n", encoding="utf-8")

            def run(_root: Path, command: list[str], _log: object) -> None:
                self.assertEqual(command[0], "dpkg-divert")
                subprocess.run([command[0], "--root=" + str(root), *command[1:]],
                               check=True, capture_output=True, encoding="utf-8")

            wrapper_target = "../bin/live-update-initramfs" if relative_wrapper else "/usr/bin/live-update-initramfs"
            if bin_alias:
                wrapper_target = "/bin/live-update-initramfs"
            if vendor:
                run(root, ["dpkg-divert", "--package", "live-tools", "--add", "--rename",
                           "--divert", source + ".orig.initramfs-tools", source], None)
                binary.symlink_to(wrapper_target)
                live_wrapper = root / "usr/bin/live-update-initramfs"
                live_wrapper.parent.mkdir(parents=True)
                live_wrapper.write_text("original live wrapper\n", encoding="utf-8")
            if owned:
                # Real Live images have a package database. Without the .list
                # ownership record dpkg-divert --rename takes a different path.
                info = root / "var/lib/dpkg/info"
                info.mkdir()
                (root / "var/lib/dpkg/status").write_text(
                    "Package: live-tools\nStatus: install ok installed\n"
                    "Architecture: all\nVersion: 1:20240525\n"
                    "Maintainer: Test <test@example.invalid>\nDescription: fixture\n\n"
                    "Package: initramfs-tools\nStatus: install ok installed\n"
                    "Architecture: all\nVersion: 1\n"
                    "Maintainer: Test <test@example.invalid>\nDescription: fixture\n\n",
                    encoding="utf-8",
                )
                listed_wrapper = "/bin/live-update-initramfs" if legacy_payload else "/usr/bin/live-update-initramfs"
                (info / "live-tools.list").write_text(source + "\n" + listed_wrapper + "\n", encoding="utf-8")
                (info / "initramfs-tools.list").write_text(source + "\n", encoding="utf-8")
            before = (root / "var/lib/dpkg/diversions").read_text(encoding="utf-8") if vendor else ""
            with patch("debian_usb.rebuild_iso._run_in_chroot", side_effect=run):
                try:
                    with _deferred_initramfs_updates(root, io.StringIO()):
                        shim = live_wrapper if vendor else binary
                        self.assertFalse(shim.is_symlink())
                        self.assertIn("exit 0", shim.read_text(encoding="utf-8"))
                        if vendor:
                            self.assertTrue(binary.is_symlink())
                            self.assertEqual(str(binary.readlink()), wrapper_target)
                            self.assertIn(before, (root / "var/lib/dpkg/diversions").read_text(encoding="utf-8"))
                        if unpack:
                            # Exercise dpkg's actual payload routing, not just a
                            # manual overwrite of the temporary engine backup.
                            payload = root / "package-fixture"
                            (payload / "DEBIAN").mkdir(parents=True)
                            (payload / "DEBIAN/control").write_text(
                                "Package: initramfs-tools\nVersion: 2\nArchitecture: all\n"
                                "Maintainer: Test <test@example.invalid>\nDescription: fixture\n",
                                encoding="utf-8",
                            )
                            script = payload / source.lstrip("/")
                            script.parent.mkdir(parents=True)
                            script.write_text("upgraded engine\n", encoding="utf-8")
                            script.chmod(0o755)
                            archive = root / "initramfs-fixture.deb"
                            subprocess.run(["dpkg-deb", "--build", str(payload), str(archive)],
                                           check=True, capture_output=True, encoding="utf-8")
                            result = subprocess.run(["dpkg", "--force-not-root", "--root=" + str(root),
                                                     "--unpack", str(archive)],
                                                    check=False, capture_output=True, encoding="utf-8")
                            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                            if upgrade_wrapper:
                                (payload / "DEBIAN/control").write_text(
                                    "Package: live-tools\nVersion: 1:20240526\nArchitecture: all\n"
                                    "Maintainer: Test <test@example.invalid>\nDescription: fixture\n",
                                    encoding="utf-8",
                                )
                                script.unlink()
                                script.symlink_to(wrapper_target)
                                wrapper_payload = payload / ("bin/live-update-initramfs" if legacy_payload else "usr/bin/live-update-initramfs")
                                wrapper_payload.parent.mkdir(parents=True)
                                wrapper_payload.write_text("upgraded live wrapper\n", encoding="utf-8")
                                wrapper_payload.chmod(0o755)
                                archive = root / "live-tools-fixture.deb"
                                subprocess.run(["dpkg-deb", "--build", str(payload), str(archive)],
                                               check=True, capture_output=True, encoding="utf-8")
                                result = subprocess.run(["dpkg", "--force-not-root", "--root=" + str(root),
                                                         "--unpack", str(archive)],
                                                        check=False, capture_output=True, encoding="utf-8")
                                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                                # The package can reassert its existing diversion.
                                run(root, ["dpkg-divert", "--package", "live-tools", "--add", "--rename",
                                           "--divert", source + ".orig.initramfs-tools", source], None)
                        else:
                            engine = (binary.parent / "update-initramfs.orig.initramfs-tools" if vendor
                                      else binary.parent / "update-initramfs.debian-usb-real")
                            engine.write_text("upgraded engine\n", encoding="utf-8")
                        # Maintainer scripts can call either mode, including
                        # after a package replaces the engine. Neither may run.
                        for mode in ("-c", "-u"):
                            subprocess.run([str(shim), mode, "-k", "all"], check=True,
                                           capture_output=True, encoding="utf-8")
                        if failure:
                            raise ValueError("apt fixture failure")
                except ValueError:
                    self.assertTrue(failure)
            self.assertEqual((root / "var/lib/dpkg/diversions").read_text(encoding="utf-8"), before)
            self.assertFalse((binary.parent / "update-initramfs.debian-usb-real").exists())
            self.assertFalse((binary.parent / "update-initramfs.debian-usb-wrapper").is_symlink())
            engine = binary
            if vendor:
                self.assertTrue(binary.is_symlink())
                self.assertEqual(str(binary.readlink()), wrapper_target)
                engine = binary.parent / "update-initramfs.orig.initramfs-tools"
                self.assertEqual(live_wrapper.read_text(encoding="utf-8"),
                                 "upgraded live wrapper\n" if upgrade_wrapper else "original live wrapper\n")
                self.assertFalse(live_wrapper.with_name("live-update-initramfs.debian-usb-real").exists())
            self.assertEqual(engine.read_text(encoding="utf-8"), "upgraded engine\n")

    def test_ordinary_engine_success(self) -> None:
        self._exercise(False, False)

    def test_ordinary_engine_failure(self) -> None:
        self._exercise(False, True)

    def test_live_tools_wrapper_success(self) -> None:
        self._exercise(True, False)

    def test_live_tools_wrapper_failure(self) -> None:
        self._exercise(True, True)

    def test_live_tools_legacy_merged_usr_path(self) -> None:
        self._exercise(True, False, merged_usr=True)

    def test_package_owned_live_tools_wrapper_success(self) -> None:
        self._exercise(True, False, owned=True)

    def test_package_owned_live_tools_wrapper_failure(self) -> None:
        self._exercise(True, True, owned=True)

    def test_package_owned_live_tools_merged_usr(self) -> None:
        self._exercise(True, False, merged_usr=True, owned=True)

    def test_package_owned_relative_wrapper(self) -> None:
        self._exercise(True, False, owned=True, relative_wrapper=True)

    @unittest.skipUnless(shutil.which("dpkg") and shutil.which("dpkg-deb"), "requires dpkg tools")
    def test_real_dpkg_engine_upgrade_with_live_tools_owner(self) -> None:
        self._exercise(True, False, owned=True, unpack=True)

    @unittest.skipUnless(shutil.which("dpkg") and shutil.which("dpkg-deb"), "requires dpkg tools")
    def test_real_dpkg_engine_upgrade_followed_by_failure(self) -> None:
        self._exercise(True, True, owned=True, unpack=True)

    def test_package_owned_wrapper_through_merged_bin_alias(self) -> None:
        self._exercise(True, False, owned=True, bin_alias=True)

    @unittest.skipUnless(shutil.which("dpkg") and shutil.which("dpkg-deb"), "requires dpkg tools")
    def test_real_upgrade_of_both_engine_and_live_wrapper(self) -> None:
        self._exercise(True, False, owned=True, unpack=True, upgrade_wrapper=True)

    @unittest.skipUnless(shutil.which("dpkg") and shutil.which("dpkg-deb"), "requires dpkg tools")
    def test_real_upgrade_of_both_packages_then_failure(self) -> None:
        self._exercise(True, True, owned=True, unpack=True, upgrade_wrapper=True)

    @unittest.skipUnless(shutil.which("dpkg") and shutil.which("dpkg-deb"), "requires dpkg tools")
    def test_merged_usr_upgrade_uses_legacy_bin_payload(self) -> None:
        self._exercise(True, False, owned=True, unpack=True, upgrade_wrapper=True,
                       bin_alias=True, legacy_payload=True)

    @unittest.skipUnless(shutil.which("dpkg") and shutil.which("dpkg-deb"), "requires dpkg tools")
    def test_merged_usr_upgrade_uses_canonical_bin_payload(self) -> None:
        self._exercise(True, False, owned=True, unpack=True, upgrade_wrapper=True, bin_alias=True)


class InitramfsDeferralGuardTests(unittest.TestCase):
    def _reject(self, records: str, expected: str, *, wrapper: str = "/usr/bin/live-update-initramfs") -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = root / "var/lib/dpkg/diversions"
            database.parent.mkdir(parents=True)
            database.write_text(records, encoding="utf-8")
            binary = root / "usr/sbin/update-initramfs"
            binary.parent.mkdir(parents=True)
            binary.symlink_to(wrapper)
            executable = root / "usr/bin/live-update-initramfs"
            executable.parent.mkdir(parents=True)
            executable.write_text("unchanged wrapper\n", encoding="utf-8")
            with patch("debian_usb.rebuild_iso._run_in_chroot") as run:
                with self.assertRaisesRegex(RuntimeError, expected):
                    with _deferred_initramfs_updates(root, io.StringIO()):
                        self.fail("invalid source must not enter package installation")
                run.assert_not_called()
            self.assertEqual(database.read_text(encoding="utf-8"), records)
            self.assertEqual(str(binary.readlink()), wrapper)
            self.assertEqual(executable.read_text(encoding="utf-8"), "unchanged wrapper\n")

    def test_unknown_package_diversion_is_not_overwritten(self) -> None:
        self._reject("/usr/sbin/update-initramfs\n/usr/sbin/custom\nother-package\n", "unsupported update-initramfs diversion")

    def test_malformed_diversion_database_is_not_overwritten(self) -> None:
        self._reject("/usr/sbin/update-initramfs\n/incomplete\n", "invalid dpkg diversions database")

    def test_foreign_wrapper_symlink_is_not_followed(self) -> None:
        self._reject("/usr/sbin/update-initramfs\n/usr/sbin/update-initramfs.orig.initramfs-tools\nlive-tools\n",
                     "unsupported live-tools update-initramfs wrapper target", wrapper="/outside/host-command")

    def test_existing_wrapper_diversion_is_not_overwritten(self) -> None:
        self._reject("/usr/sbin/update-initramfs\n/usr/sbin/update-initramfs.orig.initramfs-tools\nlive-tools\n"
                     "/usr/bin/live-update-initramfs\n/usr/bin/custom-wrapper\n:\n", "unsupported live-update-initramfs diversion")
