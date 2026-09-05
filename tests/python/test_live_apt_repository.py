"""Offline APT regression tests. No host APT configuration or mounts are changed."""
from __future__ import annotations

import hashlib
import importlib.util
import lzma
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from debian_usb.live_hooks import stage_debian_live_apt_policy

HELPER = Path(__file__).resolve().parents[2] / "config-hooks/live-apt-repository.py"
spec = importlib.util.spec_from_file_location("live_apt_repository", HELPER)
assert spec is not None and spec.loader is not None
repo = importlib.util.module_from_spec(spec)
spec.loader.exec_module(repo)


class LiveAptRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.medium = self.root / "run/live/medium"
        self.medium.mkdir(parents=True)

    def repository(self, *, real_package: bool = False) -> Path:
        package = self.medium / "pool/main/d/dusb-fixture/dusb-fixture_1_all.deb"
        package.parent.mkdir(parents=True)
        if real_package:
            tree = self.root / "package-tree"
            (tree / "DEBIAN").mkdir(parents=True)
            (tree / "DEBIAN/control").write_text(
                "Package: dusb-fixture\nVersion: 1\nArchitecture: all\nMaintainer: Test <test@example.invalid>\n"
                "Description: Isolated Debian USB repository test\n", encoding="utf-8")
            subprocess.run(["dpkg-deb", "--build", str(tree), str(package)], check=True,
                           capture_output=True, encoding="utf-8", timeout=15)
        else:
            package.write_bytes(b"package fixture")
        index_dir = self.medium / "dists/trixie/main/binary-amd64"
        index_dir.mkdir(parents=True)
        text = ("Package: dusb-fixture\nVersion: 1\nArchitecture: all\n"
                "Maintainer: Test <test@example.invalid>\nDescription: Offline package test\n"
                f"Filename: {package.relative_to(self.medium).as_posix()}\n"
                f"Size: {package.stat().st_size}\nSHA256: {hashlib.sha256(package.read_bytes()).hexdigest()}\n\n")
        (index_dir / "Packages").write_text(text, encoding="utf-8")
        (index_dir / "Packages.xz").write_bytes(lzma.compress(text.encode("utf-8")))
        self.release()
        return package

    def release(self) -> None:
        release = self.medium / "dists/trixie/Release"
        text = "Origin: Debian USB Test\nLabel: Fixture\nSuite: trixie\nCodename: trixie\nArchitectures: amd64\nComponents: main\nSHA256:\n"
        for path in sorted((release.parent / "main/binary-amd64").glob("Packages*")):
            data = path.read_bytes()
            text += f" {hashlib.sha256(data).hexdigest()} {len(data)} {path.relative_to(release.parent).as_posix()}\n"
        release.write_text(text, encoding="utf-8")

    def test_complete_repository_keeps_real_packages_available(self) -> None:
        self.repository()
        self.assertEqual(repo.repository_components(self.medium, "trixie", "amd64"), ["main"])
        self.assertTrue(repo.write_sources(self.root, "trixie", "amd64", allow_mount=False))
        source = self.root / "etc/apt/sources.list.d" / repo.SOURCE_NAME
        before = source.read_bytes()
        self.assertIn(str(self.medium).encode(), before)
        self.assertNotIn(b"rootfs/filesystem.squashfs", before)
        self.assertIn(b"Trusted: yes", before)  # Scoped to this single file: URI.
        self.assertTrue(repo.write_sources(self.root, "trixie", "amd64", allow_mount=False))
        self.assertEqual(source.read_bytes(), before)
        self.assertEqual(source.stat().st_mode & 0o777, 0o644)

    def test_missing_pool_disables_source_even_with_valid_indexes(self) -> None:
        package = self.repository()
        repo.write_sources(self.root, "trixie", "amd64", allow_mount=False)
        package.unlink()
        self.assertFalse(repo.write_sources(self.root, "trixie", "amd64", allow_mount=False))
        self.assertFalse((self.root / "etc/apt/sources.list.d" / repo.SOURCE_NAME).exists())

    def test_squashfs_only_medium_is_not_an_apt_repository(self) -> None:
        (self.medium / "filesystem.squashfs").write_bytes(b"compressed rootfs")
        self.assertFalse(repo.write_sources(self.root, "trixie", "amd64", allow_mount=False))

    def test_missing_corrupt_wrong_suite_and_wrong_arch_indexes(self) -> None:
        self.repository()
        self.assertEqual(repo.repository_components(self.medium, "bookworm", "amd64"), [])
        self.assertEqual(repo.repository_components(self.medium, "trixie", "arm64"), [])
        for path in (self.medium / "dists/trixie/main/binary-amd64").glob("Packages*"):
            path.write_bytes(b"corrupt")
        self.assertEqual(repo.repository_components(self.medium, "trixie", "amd64"), [])

    def test_package_symlink_cannot_escape_medium(self) -> None:
        package = self.repository()
        outside = self.root / "outside.deb"
        outside.write_bytes(package.read_bytes())
        package.unlink()
        package.symlink_to(outside)
        self.assertEqual(repo.repository_components(self.medium, "trixie", "amd64"), [])

    def test_inrelease_only_metadata(self) -> None:
        self.repository()
        release = self.medium / "dists/trixie/Release"
        content = release.read_text(encoding="utf-8")
        release.unlink()
        release.with_name("InRelease").write_text(
            "-----BEGIN PGP SIGNED MESSAGE-----\nHash: SHA256\n\n" + content +
            "-----BEGIN PGP SIGNATURE-----\nfixture\n-----END PGP SIGNATURE-----\n", encoding="utf-8")
        self.assertEqual(repo.repository_components(self.medium, "trixie", "amd64"), ["main"])

    def test_exact_multios_locator_rejects_scanning_and_traversal(self) -> None:
        valid = "boot=live live-media=/dev/disk/by-uuid/abcd-1234 findiso=/isos/debian/live.iso toram=filesystem.squashfs"
        self.assertEqual(repo.exact_iso_locator(valid), ("/dev/disk/by-uuid/abcd-1234", "/isos/debian/live.iso"))
        for cmd in ("boot=live", valid.replace("/isos/debian/", "/../"), valid.replace("/dev/disk/by-uuid/abcd-1234", "/dev/sda2"), valid.replace("live.iso", "*")):
            with self.subTest(cmd=cmd):
                self.assertIsNone(repo.exact_iso_locator(cmd))

    def test_policy_is_baked_into_rootfs_and_live_only(self) -> None:
        staged = stage_debian_live_apt_policy(self.root)
        self.assertTrue(staged)
        for name in ("live-apt-repair", "live-apt-repository"):
            path = self.root / "usr/lib/debian-usb" / name
            self.assertTrue(path.is_file())
            self.assertEqual(path.stat().st_mode & 0o777, 0o755)
        service = (self.root / "etc/systemd/system/debian-usb-live-apt.service").read_text(encoding="utf-8")
        self.assertIn("ConditionKernelCommandLine=boot=live", service)
        self.assertIn("Before=apt-daily", service)
        apt_hook = self.root / "etc/apt/apt.conf.d/05debian-usb-live-medium"
        self.assertIn("Pre-Invoke", apt_hook.read_text(encoding="utf-8"))
        self.assertIn("boot=live", (self.root / "usr/lib/debian-usb/live-apt-repair").read_text(encoding="utf-8"))

    @unittest.skipUnless(shutil.which("apt-get") and shutil.which("dpkg-deb"), "requires native APT and dpkg-deb")
    def test_real_apt_update_and_package_download_from_generated_source(self) -> None:
        package = self.repository(real_package=True)
        self.assertTrue(repo.write_sources(self.root, "trixie", "amd64", allow_mount=False))
        source = self.root / "etc/apt/sources.list.d" / repo.SOURCE_NAME
        for name in ("state/lists/partial", "cache/archives/partial", "downloads"):
            (self.root / name).mkdir(parents=True)
        command = ["apt-get", "-o", f"Dir::Etc::sourcelist={source}", "-o", "Dir::Etc::sourceparts=-",
                   "-o", f"Dir::State={self.root / 'state'}", "-o", "Dir::State::status=/dev/null",
                   "-o", f"Dir::Cache={self.root / 'cache'}", "-o", "APT::Architecture=amd64",
                   "-o", "APT::Sandbox::User=root", "-o", "Acquire::Languages=none"]
        result = subprocess.run([*command, "update"], capture_output=True, encoding="utf-8", timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("Failed to fetch", result.stdout + result.stderr)
        result = subprocess.run([*command, "download", "dusb-fixture"], cwd=self.root / "downloads",
                                capture_output=True, encoding="utf-8", timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        downloaded = next((self.root / "downloads").glob("*.deb"))
        self.assertEqual(downloaded.read_bytes(), package.read_bytes())
