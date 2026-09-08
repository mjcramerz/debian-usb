"""Native-loader discovery and executable transport regression tests.

Archive tests use real cpio. Shell tests rebase guest paths into a private tree
and stub debconf/preseed-common; they do not claim to boot Debian Installer.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from installer_fixture import NATIVE_PRESEED, write_installer_initrd, write_preseed_libraries
from debian_usb.installer_preseed import (
    DISPATCH_MARKER, HD_MEDIA_LOADER, initrd_path, inspect_preseed_loader,
)
from debian_usb.installer_profiles import _install_transport_dispatch, prepare_installer_profiles, PROFILE_ASSET_DIR
from debian_usb.rebuild_iso import _extract_initrd_archive, _repack_initrd_archive


class InstallerPreseedTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix='preseed-regression-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def tree(self, name: str = 'tree') -> tuple[Path, Path]:
        tree = self.root / name
        tree.mkdir()
        write_preseed_libraries(tree)
        hook = tree / 'lib/debian-installer-startup.d/S30initrd-preseed'
        hook.parent.mkdir(parents=True)
        hook.write_text(NATIVE_PRESEED, encoding='utf-8')
        hook.chmod(0o755)
        return tree, hook

    def test_old_detector_failure_reproduced_and_corrected(self) -> None:
        tree, hook = self.tree()
        observer = hook.with_name('S05seed-observer')
        observer.write_text('#!/bin/sh\n# /preseed.cfg is optional\n[ ! -f /preseed.cfg ] || :\n', encoding='utf-8')
        old_candidates = [p for p in hook.parent.glob('*')
                          if p.is_file() and not p.is_symlink() and '/preseed.cfg' in p.read_text(encoding='utf-8')]
        self.assertEqual(len(old_candidates), 2, 'fixture must reproduce the old exactly-one failure')
        before = observer.read_bytes()
        metadata = _install_transport_dispatch(tree, 'netinst')
        self.assertEqual(metadata['hook'], '/lib/debian-installer-startup.d/S30initrd-preseed')
        self.assertEqual(observer.read_bytes(), before)
        self.assertIn(DISPATCH_MARKER, hook.read_text(encoding='utf-8'))

    def test_loader_signature_in_diagnostic_text_is_not_another_loader(self) -> None:
        tree, hook = self.tree()
        observer = hook.with_name('S06diagnostics')
        observer.write_text(
            '#!/bin/sh\necho "preseed_location /preseed.cfg"\n'
            'printf "%s\\n" "preseed_location file:///preseed.cfg"\n'
            'echo preseed_location /preseed.cfg\n', encoding='utf-8')
        before = observer.read_bytes()
        self.assertEqual(_install_transport_dispatch(tree, 'netinst')['hook'],
                         '/lib/debian-installer-startup.d/S30initrd-preseed')
        self.assertEqual(observer.read_bytes(), before)

    def test_native_script_and_file_url_are_preserved_verbatim(self) -> None:
        tree, hook = self.tree()
        original = hook.read_bytes()
        metadata = _install_transport_dispatch(tree, 'netboot')
        self.assertEqual((tree / 'lib/debian-usb/initrd-preseed.original').read_bytes(), original)
        self.assertEqual(metadata['native_sha256'], hashlib.sha256(original).hexdigest())
        self.assertIn(b'file:///preseed.cfg', original)
        self.assertIn('preseed_location "file://$seed_file" "$checksum"',
                      (tree / 'lib/debian-usb/load-hd-media-preseed').read_text(encoding='utf-8'))

    def test_named_loader_can_delegate_without_a_literal_seed_path(self) -> None:
        tree, hook = self.tree()
        original = '#!/bin/sh\nexec /lib/preseed/native-loader "$@"\n'
        hook.write_text(original, encoding='utf-8')
        _install_transport_dispatch(tree, 'netboot')
        self.assertEqual((tree / 'lib/debian-usb/initrd-preseed.original').read_text(encoding='utf-8'), original)

    def test_numbered_and_shell_extension_loader_names(self) -> None:
        for name in ('S29initrd-preseed', 'S70initrd-preseed.sh', 'initrd-preseed'):
            with self.subTest(name=name):
                tree, hook = self.tree(name)
                hook.rename(hook.with_name(name))
                self.assertTrue(_install_transport_dispatch(tree, 'netinst')['hook'].endswith('/' + name))

    def test_renamed_package_owned_loader(self) -> None:
        tree, hook = self.tree()
        hook = hook.rename(hook.with_name('S29vendor-loader'))
        hook.write_text('#!/bin/sh\nexec /bin/native-preseed "$@"\n', encoding='utf-8')
        listing = tree / 'var/lib/dpkg/info/initrd-preseed.list'
        listing.parent.mkdir(parents=True)
        listing.write_text('./lib/debian-installer-startup.d/S29vendor-loader\n', encoding='utf-8')
        self.assertTrue(_install_transport_dispatch(tree, 'netinst')['hook'].endswith('S29vendor-loader'))

    def test_loader_call_signature_fallback(self) -> None:
        tree, hook = self.tree()
        hook.rename(hook.with_name('S28vendor-seeding'))
        self.assertTrue(_install_transport_dispatch(tree, 'netinst')['hook'].endswith('S28vendor-seeding'))

    def test_backups_are_not_startup_loaders(self) -> None:
        tree, hook = self.tree()
        for suffix in ('.bak', '.dpkg-old', '~', '.disabled'):
            shutil.copy2(hook, hook.with_name(hook.name + suffix))
        self.assertEqual(_install_transport_dispatch(tree, 'netinst')['origin'], 'native')

    def test_two_actual_loaders_fail_with_paths_before_modification(self) -> None:
        tree, hook = self.tree()
        other = hook.with_name('S99initrd-preseed')
        shutil.copy2(hook, other)
        original = hook.read_bytes()
        with self.assertRaisesRegex(ValueError, 'multiple active.*S30initrd-preseed.*S99initrd-preseed'):
            _install_transport_dispatch(tree, 'netboot')
        self.assertEqual(hook.read_bytes(), original)
        self.assertFalse((tree / 'lib/debian-usb').exists())

    def test_distinct_signature_loader_is_not_silently_ignored(self) -> None:
        tree, hook = self.tree()
        other = hook.with_name('S99vendor-loader')
        other.write_text('#!/bin/sh\npreseed_location file:///preseed.cfg\n', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'multiple active'):
            _install_transport_dispatch(tree, 'netinst')

    def test_missing_native_uses_complete_preseed_common_api(self) -> None:
        tree, hook = self.tree()
        hook.unlink()
        self.assertEqual(inspect_preseed_loader(tree).origin, 'preseed-common-adapter')
        self.assertFalse(hook.exists(), 'inspection must not create files')
        metadata = _install_transport_dispatch(tree, 'netinst')
        self.assertEqual(metadata['origin'], 'preseed-common-adapter')
        self.assertTrue(hook.is_file())
        self.assertIn('preseed_command preseed/early_command',
                      (tree / 'lib/debian-usb/initrd-preseed.original').read_text(encoding='utf-8'))

    def test_missing_adapter_api_gives_discovery_evidence(self) -> None:
        tree, hook = self.tree()
        hook.unlink()
        observer = hook.with_name('S05observer')
        observer.write_text('#!/bin/sh\n# /preseed.cfg is optional\n:\n', encoding='utf-8')
        (tree / 'lib/preseed/preseed.sh').write_text('# incomplete library\n', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'no native loader or complete.*S05observer'):
            _install_transport_dispatch(tree, 'netboot')

    def test_missing_library_is_a_real_error_not_a_false_native_count(self) -> None:
        tree, _ = self.tree()
        (tree / 'lib/preseed/preseed.sh').unlink()
        with self.assertRaisesRegex(ValueError, 'missing preseed-common.*startup entries:'):
            _install_transport_dispatch(tree, 'netboot')

    def test_unknown_initramfs_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, 'no Debian Installer startup directory'):
            _install_transport_dispatch(self.root, 'netinst')

    def test_invalid_native_shell_is_rejected(self) -> None:
        tree, hook = self.tree()
        hook.write_text('#!/bin/sh\nif broken\n', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'shell syntax'):
            _install_transport_dispatch(tree, 'netinst')

    def test_native_scripts_are_never_executed_on_build_host(self) -> None:
        tree, hook = self.tree()
        marker = self.root / 'must-not-exist'
        hook.write_text(f'#!/bin/sh\ntouch "{marker}"\n', encoding='utf-8')
        _install_transport_dispatch(tree, 'netinst')
        self.assertFalse(marker.exists())

    def test_symlinked_hook_keeps_shared_target_untouched(self) -> None:
        for absolute in (False, True):
            with self.subTest(absolute=absolute):
                tree, hook = self.tree(str(absolute))
                target = tree / 'lib/preseed/native-loader'
                hook.rename(target)
                before = target.read_bytes()
                hook.symlink_to('/lib/preseed/native-loader' if absolute else '../preseed/native-loader')
                _install_transport_dispatch(tree, 'netboot')
                self.assertFalse(hook.is_symlink())
                self.assertEqual(target.read_bytes(), before)
                self.assertEqual((tree / 'lib/debian-usb/initrd-preseed.original').read_bytes(), before)

    def test_hardlinked_hook_keeps_shared_target_untouched(self) -> None:
        tree, hook = self.tree()
        target = tree / 'lib/preseed/native-loader'
        os.link(hook, target)
        before = target.read_bytes()
        _install_transport_dispatch(tree, 'netinst')
        self.assertNotEqual(hook.stat().st_ino, target.stat().st_ino)
        self.assertEqual(target.read_bytes(), before)

    def test_usr_merged_layout_and_directory_alias_are_not_double_counted(self) -> None:
        for link in ('usr/lib', '/usr/lib'):
            with self.subTest(link=link):
                tree, _ = self.tree('relative' if link[0] != '/' else 'absolute')
                (tree / 'lib').rename(tree / 'usr/lib')
                (tree / 'lib').symlink_to(link)
                metadata = _install_transport_dispatch(tree, 'netboot')
                self.assertEqual(metadata['origin'], 'native')
                self.assertTrue((tree / 'usr/lib/debian-usb/initrd-preseed.original').is_file())

    def test_usr_only_layout_uses_guest_usr_paths(self) -> None:
        tree, hook = self.tree()
        hook.write_text(NATIVE_PRESEED.replace('/lib/preseed/', '/usr/lib/preseed/'), encoding='utf-8')
        (tree / 'lib').rename(tree / 'usr/lib')
        metadata = _install_transport_dispatch(tree, 'netinst')
        self.assertEqual(metadata['hook'], '/usr/lib/debian-installer-startup.d/S30initrd-preseed')
        wrapper = (tree / metadata['hook'].lstrip('/')).read_text(encoding='utf-8')
        self.assertIn('/usr/lib/debian-usb/initrd-preseed.original', wrapper)

    def test_guest_absolute_link_cannot_reach_host_file(self) -> None:
        tree, hook = self.tree()
        host = self.root / 'host-secret'
        host.write_text('#!/bin/sh\n# host sentinel\n', encoding='utf-8')
        before = host.read_bytes()
        hook.unlink()
        hook.symlink_to(host)
        with self.assertRaisesRegex(ValueError, 'missing/non-regular target'):
            _install_transport_dispatch(tree, 'netinst')
        self.assertEqual(host.read_bytes(), before)

    def test_guest_symlink_loops_and_root_traversal(self) -> None:
        tree, _ = self.tree()
        (tree / 'loop').symlink_to('/loop')
        with self.assertRaisesRegex(ValueError, 'symlink loop'):
            initrd_path(tree, 'loop')
        (tree / 'lib/parent').symlink_to('../../../../sentinel')
        self.assertEqual(initrd_path(tree, 'lib/parent'), tree / 'sentinel')

    def test_reapplication_is_idempotent_and_checks_native_backup(self) -> None:
        tree, hook = self.tree()
        first = _install_transport_dispatch(tree, 'netboot')
        original = (tree / 'lib/debian-usb/initrd-preseed.original').read_bytes()
        wrapper = hook.read_bytes()
        second = _install_transport_dispatch(tree, 'netboot')
        self.assertEqual(first['native_sha256'], second['native_sha256'])
        self.assertEqual(hook.read_bytes(), wrapper)
        saved = tree / 'lib/debian-usb/initrd-preseed.original'
        self.assertEqual(saved.read_bytes(), original)
        saved.write_bytes(original + b'# changed\n')
        with self.assertRaisesRegex(ValueError, 'checksum mismatch'):
            _install_transport_dispatch(tree, 'netboot')

    def test_old_dispatcher_backup_can_be_upgraded(self) -> None:
        tree, hook = self.tree()
        support = tree / 'lib/debian-usb'
        support.mkdir()
        (support / 'initrd-preseed.original').write_text(NATIVE_PRESEED, encoding='utf-8')
        hook.write_text('#!/bin/sh\n' + DISPATCH_MARKER + '\nexit 0\n', encoding='utf-8')
        self.assertEqual(_install_transport_dispatch(tree, 'netboot')['origin'], 'legacy-managed-backup')

    def test_nested_dispatcher_backup_is_rejected(self) -> None:
        tree, hook = self.tree()
        _install_transport_dispatch(tree, 'netinst')
        (tree / 'lib/debian-usb/initrd-preseed.original').write_bytes(hook.read_bytes())
        with self.assertRaisesRegex(ValueError, 'backup is itself a dispatcher'):
            _install_transport_dispatch(tree, 'netinst')

    def test_all_family_role_flavor_archives_handle_symlinked_native_loader(self) -> None:
        for family in ('debian', 'kali-linux'):
            for role in ('netinst', 'netboot'):
                with self.subTest(family=family, role=role):
                    name = family + '-' + role
                    tree, hook = self.tree(name + '-tree')
                    target = tree / 'lib/preseed/native-loader'
                    hook.rename(target)
                    hook.symlink_to('../preseed/native-loader')
                    observer = hook.with_name('S05observer')
                    observer.write_text('#!/bin/sh\n# /preseed.cfg is optional\n:\n', encoding='utf-8')
                    bundle = self.root / name
                    assets = bundle / ('hd-media' if role == 'netinst' else 'netboot')
                    assets.mkdir(parents=True)
                    (assets / 'vmlinuz').write_bytes(b'fixture-kernel\n')
                    _repack_initrd_archive(tree, assets / 'initrd.gz')
                    original = (assets / 'initrd.gz').read_bytes()
                    result = prepare_installer_profiles(bundle, family, role)
                    for flavor in ('desktop', 'server'):
                        out = self.root / (name + '-' + flavor)
                        _extract_initrd_archive(bundle / PROFILE_ASSET_DIR / flavor / 'initrd.gz', out)
                        self.assertEqual((out / 'lib/debian-usb/initrd-preseed.original').read_text(encoding='utf-8'), NATIVE_PRESEED)
                        self.assertEqual(result[flavor]['preseed_transport']['version'], 2)
                    self.assertEqual((assets / 'initrd.gz').read_bytes(), original)

    def test_shell_dispatch_modes_and_native_url_loader(self) -> None:
        # Rebase filesystem interfaces only. The generated shell itself chooses
        # the transport and constructs file://; preseed-common records calls.
        for role in ('netinst', 'netboot'):
            tree, hook = self.tree(role)
            _install_transport_dispatch(tree, role)
            support = tree / 'lib/debian-usb'
            for dirname in ('proc', 'var/run', 'hd-media/debian-preseed-de'):
                (tree / dirname).mkdir(parents=True, exist_ok=True)
            (tree / 'preseed.cfg').write_text('embedded desktop\n', encoding='utf-8')
            usb_seed = tree / 'hd-media/debian-preseed-de/preseed.cfg'
            usb_seed.write_text('external usb\n', encoding='utf-8')
            marker = tree / 'var/run/debian-usb-hd-media-preseed'
            trace = tree / 'trace'
            library = tree / 'lib/preseed/preseed.sh'
            library.write_text('preseed_location () { printf "load:%s:%s\\n" "$1" "${2-}" >> "$TRACE"; }\n'
                               'preseed_command () { printf "early:%s\\n" "$1" >> "$TRACE"; }\n', encoding='utf-8')
            # Stub ONLY mounting; the real URL loader is still executed.
            (support / 'mount-hd-media-preseed').write_text(f'#!/bin/sh\nprintf "%s" "{usb_seed}" > "{marker}"\n', encoding='utf-8')
            replacements = ('/lib/debian-usb', '/usr/share/debconf/confmodule', '/lib/preseed/preseed.sh',
                            '/var/run/debian-usb-hd-media-preseed', '/proc/cmdline', '/hd-media/', '/preseed.cfg')
            for script in (hook, support / 'initrd-preseed.original', support / 'load-hd-media-preseed'):
                text = script.read_text(encoding='utf-8')
                # All endpoints are rebased in a single pass (no double prefix).
                import re
                text = re.sub('|'.join(re.escape(p) for p in replacements),
                              lambda m: str(tree) + m.group(), text)
                script.write_text(text, encoding='utf-8')
            # Rebasing /preseed.cfg also rebases the glob suffix: undo ONLY that
            # glob component in the fixture so it still matches the USB path.
            hd_loader = support / 'load-hd-media-preseed'
            text = hd_loader.read_text(encoding='utf-8').replace('/*' + str(tree) + '/preseed.cfg', '/*/preseed.cfg')
            hd_loader.write_text(text, encoding='utf-8')
            for mode in ('initrd', 'http', 'https', 'usb', 'invalid'):
                with self.subTest(role=role, mode=mode):
                    trace.unlink(missing_ok=True)
                    (tree / 'proc/cmdline').write_text(f'DUSB_PRESEED_MODE={mode}', encoding='utf-8')
                    result = subprocess.run(['sh', str(hook)], env={**os.environ, 'TRACE': str(trace), 'DUSB_PRESEED_FILE': '/wrong'},
                                            text=True, encoding='utf-8', capture_output=True)
                    self.assertEqual(result.returncode == 0, mode != 'invalid', result.stderr)
                    output = trace.read_text(encoding='utf-8') if trace.exists() else ''
                    if mode == 'initrd':
                        self.assertEqual(output, f'load:file://{tree}/preseed.cfg:\nearly:preseed/early_command\n')
                    elif mode == 'usb' and role == 'netboot':
                        self.assertEqual(output, f'load:file://{usb_seed}:\nearly:preseed/early_command\n')
                    else:
                        self.assertEqual(output, '')

    def test_selected_initrd_checker_uses_copies_and_reports_both_flavors(self) -> None:
        import sys
        repo = Path(__file__).resolve().parents[2]
        archive = self.root / 'selected-initrd.gz'
        kernel = self.root / 'selected-vmlinuz'
        kernel.write_bytes(b'fixture-kernel; checker does not validate ABI\n')
        write_installer_initrd(archive)
        before = archive.read_bytes()
        result = subprocess.run([sys.executable, str(repo / 'scripts/verify-installer-initrd.py'),
                                 '--kernel', str(kernel), '--initrd', str(archive)],
                                capture_output=True, text=True, encoding='utf-8', timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertTrue(report['inputs_unchanged'])
        self.assertEqual(set(report['profiles']), {'desktop', 'server'})
        self.assertFalse(report['boot_tested'])
        self.assertEqual(archive.read_bytes(), before)

    def test_invalid_native_is_rejected_before_module_preparation(self) -> None:
        from debian_usb import installer_sources
        tree, hook = self.tree()
        hook.write_text('#!/bin/sh\nif broken\n', encoding='utf-8')
        archive, kernel, iso = self.root / 'initrd.gz', self.root / 'vmlinuz', self.root / 'payload.iso'
        _repack_initrd_archive(tree, archive)
        kernel.write_bytes(b'fixture-kernel\n')
        iso.write_bytes(b'fixture-iso\n')
        with patch.object(installer_sources, 'validate_netinst_payload_iso', return_value={}), patch.object(
            installer_sources, '_ensure_installer_assets_align_with_iso'
        ), patch.object(installer_sources, '_rebuild_debian_netinst_initrd_modules') as rebuild:
            with self.assertRaisesRegex(ValueError, 'debian netinst.*shell syntax'):
                installer_sources.prepare_managed_installer_source(
                    'debian', 'netinst', str(kernel), str(archive), iso_path=str(iso),
                    output_dir=str(self.root / 'bundle'), extra_initrd_modules=['xxhash_generic', 'lz4'],
                    module_source_strategy='host-kernel')
            rebuild.assert_not_called()

    def test_usb_loader_keeps_checksum_and_native_early_command(self) -> None:
        # Contract assertions supplement the execution/chroot tests; do not
        # rewrite native file:// operands or invent a second preseed engine.
        self.assertIn('preseed/file/checksum=*|preseed-md5=*', HD_MEDIA_LOADER)
        self.assertIn('"${#checksum}" -eq 32', HD_MEDIA_LOADER)
        self.assertEqual(HD_MEDIA_LOADER.count('preseed_command preseed/early_command'), 1)
        self.assertNotIn('set -u', HD_MEDIA_LOADER.split('set -e\n', 1)[1])


if __name__ == '__main__':
    unittest.main()
