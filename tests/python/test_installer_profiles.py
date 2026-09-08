"""Desktop/Server integration tests. No real devices, networking, or mounts."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from installer_fixture import write_installer_initrd, write_preseed_libraries
from debian_usb.bootconfig import parse_grub_entries, render_managed_grub
from debian_usb.config import load_config, load_template_config, save_config
from debian_usb.installer_profiles import (
    PROFILE_ASSET_DIR, _install_transport_dispatch, hd_media_requests,
    normalize_hd_media_dirs, prepare_installer_profiles, stage_hd_media_preseeds,
    validate_installer_profile_assets,
)
from debian_usb.installer_sources import prepare_managed_installer_source
from debian_usb.rebuild_iso import _extract_initrd_archive


class InstallerProfileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def seed(self, name: str) -> Path:
        root = self.root / name
        (root / 'scripts').mkdir(parents=True)
        (root / 'preseed.cfg').write_text('d-i debian-installer/locale string en_US.UTF-8\n', encoding='utf-8')
        (root / '.hidden').write_text(name, encoding='utf-8')
        (root / 'scripts/late.sh').write_text('#!/bin/sh\nexit 0\n', encoding='utf-8')
        (root / 'scripts/late.sh').chmod(0o750)
        (root / 'scripts/link').symlink_to('late.sh')
        return root

    def bundle(self, profile: str = 'debian', role: str = 'netboot', overlay: Path | None = None) -> Path:
        root = self.root / (profile + '-' + role)
        root.mkdir(exist_ok=True)
        kernel, initrd = root / 'downloaded-vmlinuz', root / 'downloaded-initrd.gz'
        kernel.write_bytes(b'one original kernel\n')
        write_installer_initrd(initrd)
        iso = root / 'installer.iso'
        iso.write_bytes(b'opaque installer ISO; never remastered')
        with patch('debian_usb.installer_sources.validate_netinst_payload_iso', return_value={
            'media_class': 'installer', 'installer_entry_count': 1, 'live_entry_count': 0,
        }), patch('debian_usb.installer_sources._ensure_installer_assets_align_with_iso'), patch(
            'debian_usb.installer_sources._enforce_exact_iso_scan_filename', return_value={'enforced': True}
        ):
            result = prepare_managed_installer_source(
                profile, role, str(kernel), str(initrd),
                iso_path=str(iso) if role == 'netinst' else '',
                output_dir=str(root / 'bundle'), initrd_overlay_dir=str(overlay) if overlay else '',
            )
        self.assertEqual(initrd.read_bytes(), (root / 'bundle' / ('hd-media' if role == 'netinst' else 'netboot') / 'initrd.gz').read_bytes())
        if role == 'netinst':
            self.assertEqual(iso.read_bytes(), Path(result['iso_path']).read_bytes())
        return Path(result['source_path'])

    def test_both_families_and_roles_have_isolated_archives(self) -> None:
        for profile in ('debian', 'kali-linux'):
            for role in ('netinst', 'netboot'):
                with self.subTest(profile=profile, role=role):
                    overlay = self.root / f'overlays-{profile}-{role}'
                    for flavor in ('desktop', 'server'):
                        tree = overlay / flavor
                        (tree / 'etc').mkdir(parents=True)
                        (tree / 'preseed.cfg').write_text(f'# {flavor}\n', encoding='utf-8')
                        (tree / f'{flavor}-only').write_text(flavor, encoding='utf-8')
                        (tree / '.hidden').write_text('included', encoding='utf-8')
                        (tree / 'etc/private').write_text('test fixture', encoding='utf-8')
                        (tree / 'etc/private').chmod(0o600)
                        (tree / 'alias').symlink_to(f'{flavor}-only')
                    bundle = self.bundle(profile, role, overlay)
                    manifests = validate_installer_profile_assets(str(bundle), profile, role)
                    for flavor, other, suffix in (('desktop', 'server', 'de'), ('server', 'desktop', 'srv')):
                        assets = bundle / PROFILE_ASSET_DIR / flavor
                        self.assertEqual((assets / 'vmlinuz').read_bytes(), b'one original kernel\n')
                        expanded = self.root / f'expanded-{profile}-{role}-{flavor}'
                        _extract_initrd_archive(assets / 'initrd.gz', expanded)
                        self.assertEqual((expanded / 'preseed.cfg').read_text(encoding='utf-8'), f'# {flavor}\n')
                        self.assertFalse((expanded / f'{other}-only').exists())
                        self.assertTrue((expanded / '.hidden').is_file())
                        self.assertTrue((expanded / 'alias').is_symlink())
                        self.assertEqual(stat.S_IMODE((expanded / 'etc/private').stat().st_mode), 0o600)
                        self.assertTrue(manifests[flavor]['embedded_preseed'])
                        self.assertEqual(manifests[flavor]['usb_directory'], f'/{profile.replace("-linux", "")}-{role}-{suffix}')

    def test_shipped_overlay_trees_prepare_for_all_installer_roles(self) -> None:
        repo = Path(__file__).resolve().parents[2]
        for profile, family in (("debian", "debian"), ("kali-linux", "kali")):
            for role in ("netinst", "netboot"):
                with self.subTest(profile=profile, role=role):
                    bundle = self.bundle(profile, role, repo / "initrd" / family / role)
                    manifest = validate_installer_profile_assets(str(bundle), profile, role)
                    self.assertEqual(set(manifest), {"desktop", "server"})

    def test_profile_validation_rejects_wrong_source_identity(self) -> None:
        bundle = self.bundle()
        for profile, role in (("kali-linux", "netboot"), ("debian", "netinst")):
            with self.subTest(profile=profile, role=role), self.assertRaisesRegex(ValueError, "identity"):
                validate_installer_profile_assets(str(bundle), profile, role)
        path = bundle / PROFILE_ASSET_DIR / "manifest.json"
        manifest = json.loads(path.read_text(encoding="utf-8"))
        manifest["server"]["usb_directory"] = "/debian-netboot-de"
        path.write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "wrong USB directory"):
            validate_installer_profile_assets(str(bundle), "debian", "netboot")

    def test_legacy_or_wrong_role_transport_is_not_silently_reused(self) -> None:
        bundle = self.bundle()
        path = bundle / PROFILE_ASSET_DIR / "manifest.json"
        original = path.read_text(encoding="utf-8")
        for transport, message in ((None, "outdated installer preseed transport"),
                                   ({"version": 1}, "outdated installer preseed transport"),
                                   ({"version": 2, "source_role": "netinst"}, "wrong source role")):
            with self.subTest(transport=transport):
                manifest = json.loads(original)
                manifest["desktop"]["preseed_transport"] = transport
                path.write_text(json.dumps(manifest), encoding="utf-8")
                before = path.read_bytes()
                with self.assertRaisesRegex(ValueError, message):
                    validate_installer_profile_assets(str(bundle), "debian", "netboot")
                self.assertEqual(path.read_bytes(), before)

    def test_failed_profile_rebuild_preserves_previous_pair(self) -> None:
        bundle = self.bundle()
        before = {str(path.relative_to(bundle)): path.read_bytes()
                  for path in (bundle / PROFILE_ASSET_DIR).rglob('*') if path.is_file()}
        overlay = self.root / 'bad-overlay'
        (overlay / 'desktop').mkdir(parents=True)
        # A second competing native startup loader makes only the server fail.
        hook = overlay / 'server/lib/debian-installer-startup.d/S99initrd-preseed'
        hook.parent.mkdir(parents=True)
        hook.write_text('#!/bin/sh\ncat /preseed.cfg\n', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'multiple active native loaders'):
            prepare_installer_profiles(bundle, 'debian', 'netboot', overlay_root=overlay)
        after = {str(path.relative_to(bundle)): path.read_bytes()
                 for path in (bundle / PROFILE_ASSET_DIR).rglob('*') if path.is_file()}
        self.assertEqual(before, after)

    def test_shared_embedded_seed_is_rejected_instead_of_leaking_to_server(self) -> None:
        bundle = self.bundle()
        tree = self.root / 'contaminated'
        tree.mkdir()
        (tree / 'preseed.cfg').write_text('# desktop only', encoding='utf-8')
        write_installer_initrd(bundle / 'netboot/initrd.gz', tree)
        with self.assertRaisesRegex(ValueError, 'cross-profile leakage'):
            prepare_installer_profiles(bundle, 'debian', 'netboot')

    def test_flat_installer_overlay_requires_migration(self) -> None:
        bundle = self.bundle()
        flat = self.root / 'flat'
        flat.mkdir()
        (flat / 'preseed.cfg').write_text('# old layout', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'desktop/ or server/'):
            prepare_installer_profiles(bundle, 'debian', 'netboot', overlay_root=flat)

    def test_all_transports_profiles_roles_and_secure_boot_assets(self) -> None:
        for profile, family, prefix, title in (
            ('debian', 'debian', 'DEBIAN', 'Debian'),
            ('kali-linux', 'kali', 'KALI_LINUX', 'Kali'),
        ):
            for role in ('netinst', 'netboot'):
                bundle = self.bundle(profile, role)
                config = self.root / f'{profile}-{role}.conf'
                values = {'DEFAULT_INSTALLER_KERNEL_EXTRAS': 'file=/wrong url=http://wrong/ preseed/file=/also-wrong'}
                for suffix, flavor in (('DE', 'desktop'), ('SRV', 'server')):
                    for scope, protocol in (('PUBLIC', 'https'), ('INTERNAL', 'http')):
                        values[f'{prefix}_{suffix}_PRESEED_{scope}_URL'] = f'{protocol}://example.test/{flavor}/preseed.cfg'
                        values[f'{prefix}_{suffix}_PRESEED_{scope}_ARGS'] = f'scope={scope.lower()} url=http://ignored/'
                    for number in ('ONE', 'TWO', 'THREE', 'FOUR', 'FIVE', 'SIX', 'SEVEN', 'EIGHT', 'NINE'):
                        values[f'PRESEED_{number}_ARGS_{family.upper()}_{suffix}'] = f'profile-test={flavor} file=/ignored classes={flavor}'
                save_config(str(config), values)
                for secure in (False, True):
                    with self.subTest(profile=profile, role=role, secure=secure):
                        result = render_managed_grub(source_path=str(bundle), profile=profile,
                            live_uuid='1234-DATA', persistence=False, config_path=str(config),
                            use_custom_menu=True, include_preseed_entries=False, source_role=role,
                            boot_assets_uuid='1234-ESP' if secure else '', boot_assets_namespace='test')
                        entries = parse_grub_entries(result['grub_cfg'], 'boot/grub/grub.cfg')
                        for flavor, suffix in (('desktop', 'de'), ('server', 'srv')):
                            profile_path = (f'{title} ...', f'{title} {role.title()} ...', f'{family.upper()} {flavor.upper()}')
                            for transport, mode in (('HTTPS WEB', 'https'), ('HTTP LAN', 'http'), ('INITRD PRESEED', 'initrd'), ('USB HD-MEDIA', 'usb')):
                                path = profile_path + (f'{title} {role.title()} Install ({transport}) ...',)
                                selected = [entry for entry in entries if entry.menu_path == path]
                                self.assertTrue(selected, path)
                                for entry in selected:
                                    tokens = entry.kernel_args.split()
                                    selectors = [v for v in tokens if v.startswith(('file=', 'url=', 'preseed/file=', 'preseed/url='))]
                                    expected = ([] if mode == 'initrd' else
                                        [f'file=/hd-media/{family}-preseed-{suffix}/preseed.cfg'] if mode == 'usb' else
                                        [f'url={mode}://example.test/{flavor}/preseed.cfg'])
                                    self.assertEqual(selectors, expected)
                                    self.assertIn(f'profile-test={flavor}', tokens)
                                    self.assertIn(f'DUSB_PRESEED_MODE={mode}', tokens)
                                    if mode in ('usb', 'initrd'):
                                        self.assertNotIn('scope=', entry.kernel_args)
                                    self.assertEqual('iso-scan/filename=' in entry.kernel_args, role == 'netinst')
                                    self.assertNotIn('boot=live', tokens)
                                    if mode == 'usb':
                                        self.assertIn('DUSB_HD_MEDIA_UUID=1234-DATA', tokens)
                                    if secure:
                                        self.assertIn(f'/installer-profiles/{flavor}/vmlinuz', entry.kernel_path)
                                    else:
                                        self.assertEqual(entry.kernel_path, f'/{family}-{role}-{suffix}/vmlinuz')
                        manifest = result['signed_kernel_assets'] if secure else result['payload_boot_assets']
                        for flavor in ('desktop', 'server'):
                            self.assertTrue(any(row['source_path'] == f'/{PROFILE_ASSET_DIR}/{flavor}/vmlinuz' for row in manifest))

    def test_unset_endpoint_does_not_disable_usb_or_initrd(self) -> None:
        bundle = self.bundle()
        config = self.root / 'empty.conf'
        save_config(str(config), {'DEBIAN_SRV_PRESEED_PUBLIC_URL': '', 'DEBIAN_SRV_PRESEED_INTERNAL_URL': ''})
        result = render_managed_grub(source_path=str(bundle), profile='debian', live_uuid='DATA',
            persistence=False, config_path=str(config), use_custom_menu=True, source_role='netboot')
        entries = parse_grub_entries(result['grub_cfg'], 'boot/grub/grub.cfg')
        server = [entry for entry in entries if 'DEBIAN SERVER' in entry.menu_path]
        self.assertEqual(sum(entry.title.startswith('Not configured:') for entry in server), 2)
        self.assertTrue(any('DUSB_PRESEED_MODE=usb' in entry.kernel_args for entry in server))
        self.assertTrue(any('DUSB_PRESEED_MODE=initrd' in entry.kernel_args for entry in server))

    def test_transport_scheme_must_match_the_menu(self) -> None:
        bundle = self.bundle()
        config = self.root / 'wrong.conf'
        save_config(str(config), {'DEBIAN_DE_PRESEED_INTERNAL_URL': 'https://example.test/preseed.cfg'})
        with self.assertRaisesRegex(ValueError, 'must use http://'):
            render_managed_grub(source_path=str(bundle), profile='debian', live_uuid='DATA',
                persistence=False, config_path=str(config), use_custom_menu=True, source_role='netboot')

    def test_declined_hd_media_never_uses_host_defaults_or_modifies_existing_folder(self) -> None:
        source = self.seed('host-default')
        target = self.root / 'usb'
        target.mkdir()
        old = target / 'debian-preseed-de'
        old.mkdir()
        (old / 'keep').write_text('already on USB', encoding='utf-8')
        config = dict(load_template_config(), PRESEED_HOST_DEBIAN_DE_PATH=str(source))
        result = stage_hd_media_preseeds([{'profile': 'debian', 'source_role': 'netinst'}], config, str(target))
        self.assertFalse(result['copied'])
        self.assertEqual(list(target.iterdir()), [old])
        self.assertEqual(list(old.iterdir()), [old / 'keep'])

    def test_copy_entire_parent_with_dotfiles_permissions_links_and_separate_folders(self) -> None:
        target = self.root / 'usb'
        target.mkdir()
        items = []
        for profile, family in (('debian', 'debian'), ('kali-linux', 'kali')):
            dirs = {flavor: str(self.seed(f'{family}-{flavor}') / 'preseed.cfg') for flavor in ('desktop', 'server')}
            items.append({'profile': profile, 'source_role': 'netinst', 'hd_media_preseed_dirs': dirs})
        result = stage_hd_media_preseeds(items, load_template_config(), str(target))
        self.assertEqual(result['folders'], ['debian-preseed-de', 'debian-preseed-srv', 'kali-preseed-de', 'kali-preseed-srv'])
        self.assertTrue(result['copied'])
        for folder in result['folders']:
            path = target / folder
            self.assertTrue((path / 'preseed.cfg').is_file())
            self.assertTrue((path / '.hidden').is_file())
            self.assertTrue((path / 'scripts/link').is_symlink())
            self.assertEqual(stat.S_IMODE((path / 'scripts/late.sh').stat().st_mode), 0o750)
        self.assertFalse((target / 'hd-media').exists())

    def test_shared_netinst_netboot_copy_is_deduplicated_and_conflicts_rejected(self) -> None:
        first, second = self.seed('first'), self.seed('second')
        items = [{'profile': 'debian', 'source_role': role, 'hd_media_preseed_dirs': {'desktop': str(first)}} for role in ('netinst', 'netboot')]
        result = stage_hd_media_preseeds(items, load_template_config())
        self.assertEqual(result['folders'], ['debian-preseed-de'])
        self.assertFalse(result['copied'])
        items[1]['hd_media_preseed_dirs'] = {'desktop': str(second)}
        with self.assertRaisesRegex(ValueError, 'conflicting'):
            hd_media_requests(items, load_template_config())

    def test_invalid_and_unsafe_sources_are_rejected(self) -> None:
        root = self.seed('source')
        for profile, role, dirs in (
            ('debian', 'primary', {'desktop': str(root)}),
            ('debian', 'netboot', {'unknown': str(root)}),
            ('debian', 'netboot', {'desktop': str(root / '.hidden')}),
            ('debian', 'netboot', {'desktop': '/'}),
        ):
            with self.subTest(profile=profile, role=role, dirs=dirs), self.assertRaises(ValueError):
                normalize_hd_media_dirs(profile, role, dirs)
        (root / 'preseed.cfg').unlink()
        (root / 'preseed.cfg').symlink_to('.hidden')
        with self.assertRaisesRegex(ValueError, 'regular preseed.cfg'):
            normalize_hd_media_dirs('debian', 'netboot', {'desktop': str(root)})

    def test_special_files_and_symlink_destinations_are_rejected(self) -> None:
        source = self.seed('source')
        os.mkfifo(source / 'fifo')
        with self.assertRaisesRegex(ValueError, 'special file'):
            normalize_hd_media_dirs('debian', 'netboot', {'desktop': str(source)})
        (source / 'fifo').unlink()
        target = self.root / 'usb'
        target.mkdir()
        (target / 'debian-preseed-de').symlink_to(source, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, 'symlinked'):
            stage_hd_media_preseeds([{'profile': 'debian', 'source_role': 'netboot', 'hd_media_preseed_dirs': {'desktop': str(source)}}], load_template_config(), str(target))

    def test_failed_copy_keeps_previous_tree_intact(self) -> None:
        source = self.seed('source')
        target = self.root / 'usb'
        old = target / 'debian-preseed-de'
        old.mkdir(parents=True)
        (old / 'sentinel').write_text('original', encoding='utf-8')
        items = [{'profile': 'debian', 'source_role': 'netboot', 'hd_media_preseed_dirs': {'desktop': str(source)}}]
        with patch('debian_usb.installer_profiles.shutil.copytree', side_effect=OSError('copy failed')):
            with self.assertRaisesRegex(OSError, 'copy failed'):
                stage_hd_media_preseeds(items, load_template_config(), str(target))
        self.assertEqual((old / 'sentinel').read_text(encoding='utf-8'), 'original')

    def test_dispatch_skips_embedded_seed_for_external_transports(self) -> None:
        # Run the generated shell dispatcher against a stub native loader. This
        # exercises selection, not a real d-i boot or block-device mount.
        for role in ('netinst', 'netboot'):
            tree = self.root / role
            hook = tree / 'lib/debian-installer-startup.d/S30initrd-preseed'
            hook.parent.mkdir(parents=True)
            hook.write_text('#!/bin/sh\nprintf "%s" /preseed.cfg\n', encoding='utf-8')
            write_preseed_libraries(tree)
            _install_transport_dispatch(tree, role)
            cmdline = tree / 'cmdline'
            selected = tree / 'var/run/debian-usb-hd-media-preseed'
            selected.parent.mkdir(parents=True)
            support = tree / 'lib/debian-usb'
            (support / 'initrd-preseed.original').write_text('#!/bin/sh\nprintf "%s" "${DUSB_PRESEED_FILE:-/preseed.cfg}"\n', encoding='utf-8')
            (support / 'mount-hd-media-preseed').write_text(f'#!/bin/sh\nprintf "%s" /hd-media/debian-preseed-de/preseed.cfg > "{selected}"\n', encoding='utf-8')
            (support / 'load-hd-media-preseed').write_text(f'#!/bin/sh\ncat "{selected}"\n', encoding='utf-8')
            (support / 'load-hd-media-preseed').chmod(0o755)
            script = hook.read_text(encoding='utf-8').replace('/proc/cmdline', str(cmdline)).replace('/lib/debian-usb/', str(support) + '/').replace('/var/run/debian-usb-hd-media-preseed', str(selected))
            hook.write_text(script, encoding='utf-8')
            for mode in ('initrd', 'http', 'https', 'usb'):
                cmdline.write_text(f'DUSB_PRESEED_MODE={mode}', encoding='utf-8')
                result = subprocess.run(['sh', str(hook)], capture_output=True, text=True, encoding='utf-8', check=True)
                expected = '/preseed.cfg' if mode == 'initrd' else '/hd-media/debian-preseed-de/preseed.cfg' if (mode, role) == ('usb', 'netboot') else ''
                self.assertEqual(result.stdout, expected, (role, mode))

    def test_legacy_server_values_migrate_to_server_not_desktop(self) -> None:
        config = self.root / 'legacy.conf'
        config.write_text('PRESEED_SIX_ARGS_DEBIAN="classes=legacy-server"\nPRESEED_USB_DEBIAN_FILE="/hd-media/preseed/debian/preseed.cfg"\n', encoding='utf-8')
        values = load_config(str(config))
        self.assertEqual(values['PRESEED_ONE_ARGS_DEBIAN_SRV'], 'classes=legacy-server')
        self.assertNotIn('legacy-server', values['PRESEED_ONE_ARGS_DEBIAN_DE'])
        self.assertEqual(values['PRESEED_USB_DEBIAN_DE_FILE'], '/hd-media/debian-preseed-de/preseed.cfg')


if __name__ == '__main__':
    unittest.main()
