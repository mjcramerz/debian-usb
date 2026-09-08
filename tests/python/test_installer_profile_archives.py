"""Real cpio regression tests for isolated installer archives (no block devices)."""
from __future__ import annotations

import bz2
import gzip
import json
import lzma
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from installer_fixture import write_installer_initrd
from debian_usb.installer_profiles import PROFILE_ASSET_DIR, prepare_installer_profiles
from debian_usb.rebuild_iso import (
    _extract_initrd_archive, _initrd_archive_layout, _repack_initrd_archive,
)


class InstallerArchiveRegressionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def bundle(self, role: str = 'netinst', name: str = 'bundle') -> tuple[Path, Path]:
        bundle = self.root / name
        assets = bundle / ('hd-media' if role == 'netinst' else 'netboot')
        assets.mkdir(parents=True)
        (assets / 'vmlinuz').write_bytes(b'unchanged-kernel\n')
        return bundle, assets / 'initrd.gz'

    def profile_snapshot(self, bundle: Path) -> dict[str, bytes]:
        return {str(p.relative_to(bundle)): p.read_bytes()
                for p in (bundle / PROFILE_ASSET_DIR).rglob('*') if p.is_file()}

    def test_fifo_is_preserved_for_both_families_and_roles(self) -> None:
        # A real FIFO needs no root privileges and catches the same invalid
        # shutil.copytree path as the device nodes in the reported failure.
        for family in ('debian', 'kali-linux'):
            for role in ('netinst', 'netboot'):
                with self.subTest(family=family, role=role):
                    bundle, archive = self.bundle(role, family + '-' + role)
                    tree = self.root / (family + '-' + role + '-input')
                    (tree / 'dev').mkdir(parents=True)
                    os.mkfifo(tree / 'dev/fixture-pipe', 0o620)
                    (tree / 'dev/fixture-pipe').chmod(0o620)
                    write_installer_initrd(archive, tree)
                    before = archive.read_bytes()
                    prepare_installer_profiles(bundle, family, role)
                    for flavor in ('desktop', 'server'):
                        expanded = self.root / (family + '-' + role + '-' + flavor)
                        _extract_initrd_archive(bundle / PROFILE_ASSET_DIR / flavor / 'initrd.gz', expanded)
                        metadata = (expanded / 'dev/fixture-pipe').lstat()
                        self.assertTrue(stat.S_ISFIFO(metadata.st_mode))
                        self.assertEqual(stat.S_IMODE(metadata.st_mode), 0o620)
                    self.assertEqual(archive.read_bytes(), before)

    def test_profile_archives_preserve_internal_hardlinks_and_symlinks(self) -> None:
        bundle, archive = self.bundle()
        tree = self.root / 'links-input'
        (tree / 'bin').mkdir(parents=True)
        original = tree / 'bin/busybox-fixture'
        original.write_bytes(b'fixture executable\n')
        original.chmod(0o755)
        os.link(original, tree / 'bin/hardlink')
        (tree / 'bin/sh').symlink_to('busybox-fixture')
        (tree / '.hidden').write_text('preserved\n', encoding='utf-8')
        private = tree / 'private'
        private.write_text('private\n', encoding='utf-8')
        private.chmod(0o600)
        write_installer_initrd(archive, tree)
        prepare_installer_profiles(bundle, 'debian', 'netinst')
        expanded = {}
        for flavor in ('desktop', 'server'):
            expanded[flavor] = self.root / flavor
            _extract_initrd_archive(bundle / PROFILE_ASSET_DIR / flavor / 'initrd.gz', expanded[flavor])
            first = expanded[flavor] / 'bin/busybox-fixture'
            second = expanded[flavor] / 'bin/hardlink'
            self.assertEqual(first.stat().st_ino, second.stat().st_ino)
            self.assertEqual(stat.S_IMODE(first.stat().st_mode), 0o755)
            self.assertEqual(os.readlink(expanded[flavor] / 'bin/sh'), 'busybox-fixture')
            self.assertEqual((expanded[flavor] / '.hidden').read_text(encoding='utf-8'), 'preserved\n')
            self.assertEqual(stat.S_IMODE((expanded[flavor] / 'private').stat().st_mode), 0o600)
        (expanded['desktop'] / 'bin/hardlink').write_bytes(b'desktop-only\n')
        self.assertEqual((expanded['server'] / 'bin/busybox-fixture').read_bytes(), b'fixture executable\n')

    def test_original_compression_and_early_archives_survive_profile_split(self) -> None:
        main = self.root / 'main.cpio'
        write_installer_initrd(main)
        prefix = b''
        for index in range(2):
            tree = self.root / f'early-{index}'
            tree.mkdir()
            (tree / f'early-data-{index}').write_bytes(b'preserve verbatim\n')
            archive = self.root / f'early-{index}.cpio'
            _repack_initrd_archive(tree, archive)
            prefix += archive.read_bytes()
        raw = main.read_bytes()
        cases = {'gzip': gzip.compress(raw, mtime=0), 'xz': lzma.compress(raw),
                 'bzip2': bz2.compress(raw), 'none': raw}
        if shutil.which('zstd'):
            cases['zstd'] = subprocess.run(['zstd', '-qc'], input=raw, capture_output=True, check=True).stdout
        for compression, payload in cases.items():
            for with_prefix in (False, True):
                with self.subTest(compression=compression, prefix=with_prefix):
                    bundle, archive = self.bundle(name=f'{compression}-{with_prefix}')
                    expected_prefix = prefix if with_prefix else b''
                    archive.write_bytes(expected_prefix + payload)
                    archive.chmod(0o444)
                    before = archive.read_bytes()
                    prepare_installer_profiles(bundle, 'debian', 'netinst')
                    for flavor in ('desktop', 'server'):
                        result = bundle / PROFILE_ASSET_DIR / flavor / 'initrd.gz'
                        layout = _initrd_archive_layout(result)
                        self.assertEqual(layout.compression, compression)
                        self.assertEqual(layout.main_offset, len(expected_prefix))
                        self.assertEqual(result.read_bytes()[:len(expected_prefix)], expected_prefix)
                        self.assertEqual(stat.S_IMODE(result.stat().st_mode), 0o444)
                        out = self.root / f'out-{compression}-{with_prefix}-{flavor}'
                        _extract_initrd_archive(result, out)
                        self.assertTrue((out / 'lib/debian-usb/initrd-preseed.original').is_file())
                    self.assertEqual(archive.read_bytes(), before)

    def test_extracted_initrd_never_goes_through_shutil_copytree(self) -> None:
        bundle, archive = self.bundle()
        write_installer_initrd(archive)
        with patch('debian_usb.installer_profiles.shutil.copytree', side_effect=AssertionError('unsafe initrd tree copy')):
            result = prepare_installer_profiles(bundle, 'debian', 'netinst')
        self.assertEqual(set(result), {'desktop', 'server'})

    def test_second_extraction_failure_preserves_previous_pair(self) -> None:
        bundle, archive = self.bundle()
        write_installer_initrd(archive)
        prepare_installer_profiles(bundle, 'debian', 'netinst')
        before = self.profile_snapshot(bundle)
        original = archive.read_bytes()
        calls = []

        def extract(source: Path, target: Path) -> None:
            calls.append(target)
            if len(calls) == 2:
                raise RuntimeError('injected server extraction failure')
            _extract_initrd_archive(source, target)

        with patch('debian_usb.rebuild_iso._extract_initrd_archive', side_effect=extract):
            with self.assertRaisesRegex(RuntimeError, 'server extraction failure'):
                prepare_installer_profiles(bundle, 'debian', 'netinst')
        self.assertEqual(before, self.profile_snapshot(bundle))
        self.assertEqual(archive.read_bytes(), original)
        self.assertEqual(list(bundle.glob('.profile-build-*')), [])

    def test_second_repack_failure_preserves_previous_pair(self) -> None:
        bundle, archive = self.bundle()
        write_installer_initrd(archive)
        prepare_installer_profiles(bundle, 'debian', 'netinst')
        before = self.profile_snapshot(bundle)
        original = archive.read_bytes()
        calls = []

        def repack(source: Path, target: Path) -> None:
            calls.append(target)
            if len(calls) == 2:
                raise RuntimeError('injected server repack failure')
            _repack_initrd_archive(source, target)

        with patch('debian_usb.rebuild_iso._repack_initrd_archive', side_effect=repack):
            with self.assertRaisesRegex(RuntimeError, 'server repack failure'):
                prepare_installer_profiles(bundle, 'debian', 'netinst')
        self.assertEqual(before, self.profile_snapshot(bundle))
        self.assertEqual(archive.read_bytes(), original)
        self.assertEqual(list(bundle.glob('.profile-build-*')), [])


    def test_device_node_metadata_roundtrip_without_reading_devices(self) -> None:
        fakeroot = shutil.which('fakeroot')
        self.assertIsNotNone(fakeroot, 'device-node regression requires fakeroot; install the fakeroot test dependency')
        fixture = Path(__file__).with_name('installer_device_fixture.py')
        result = subprocess.run([fakeroot, sys.executable, str(fixture)],
                                capture_output=True, text=True, encoding='utf-8', timeout=45)
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(len(report['passed']), 8)
        self.assertEqual(report['device_headers_per_archive'], 4)
        self.assertEqual(report['device_content_reads'], 0)

    def test_permission_error_identifies_flavor_and_required_capability(self) -> None:
        bundle, archive = self.bundle()
        write_installer_initrd(archive)
        error = RuntimeError('cpio: dev/console: Cannot mknod: Operation not permitted')
        with patch('debian_usb.rebuild_iso._extract_initrd_archive', side_effect=error):
            with self.assertRaisesRegex(RuntimeError, 'debian netinst desktop.*CAP_MKNOD'):
                prepare_installer_profiles(bundle, 'debian', 'netinst')
        self.assertFalse((bundle / PROFILE_ASSET_DIR).exists())
        self.assertEqual(list(bundle.glob('.profile-build-*')), [])


if __name__ == '__main__':
    unittest.main()
