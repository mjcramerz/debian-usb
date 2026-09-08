#!/usr/bin/env python3
"""Optional real donor-module test on a synthetic installer archive.

Run under one fakeroot session. Uses an already-installed kernel/ABI; never
installs packages. Exercises actual ABI inspection, module dependency copying,
depmod, exact ISO policy patching, and Desktop/Server archive construction.
It is NOT a test of a downloaded Debian ISO or a boot test.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import sys
import tempfile
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'src/python'))
from installer_fixture import write_installer_initrd
from test_installer_sources import InstallerSourceTests
from debian_usb import installer_sources as source
from debian_usb.installer_preseed import inspect_preseed_loader
from debian_usb.installer_profiles import prepare_installer_profiles, PROFILE_ASSET_DIR
from debian_usb.rebuild_iso import _extract_initrd_archive


def run(kernel: Path) -> dict[str, object]:
    abi = source._detect_kernel_version_from_kernel_file(kernel)
    if not abi or not (Path('/lib/modules') / abi).is_dir():
        raise SystemExit('Install the matching donor kernel/modules before this optional test; it does not download dependencies.')
    commands = set(source.INSTALLER_INITRD_PREPARE_REQUIRED_COMMANDS + source.INSTALLER_INITRD_MODULE_COPY_REQUIRED_COMMANDS
                   + source.INSTALLER_INITRD_EXACT_ISO_REQUIRED_COMMANDS)
    missing = sorted(c for c in commands if not shutil.which(c))
    if missing:
        raise SystemExit('Missing test tools: ' + ', '.join(missing))
    with tempfile.TemporaryDirectory(prefix='installer-module-pipeline-') as temporary:
        root = Path(temporary)
        tree = root / 'input-tree'
        modules = tree / 'lib/modules' / abi
        modules.mkdir(parents=True)
        (modules / 'modules.dep').touch()
        (modules / 'modules.order').touch()
        for name in ('modules.builtin', 'modules.builtin.modinfo'):
            path = Path('/lib/modules') / abi / name
            if path.is_file():
                shutil.copy2(path, modules / name)
        postinst = tree / 'var/lib/dpkg/info/iso-scan.postinst'
        postinst.parent.mkdir(parents=True)
        postinst.write_text(InstallerSourceTests._upstream_iso_scan_postinst_text(), encoding='utf-8')
        postinst.chmod(0o755)
        (tree / 'dev').mkdir()
        os.mknod(tree / 'dev/console', stat.S_IFCHR | 0o600, os.makedev(5, 1))
        os.mknod(tree / 'dev/null', stat.S_IFCHR | 0o666, os.makedev(1, 3))
        original = root / 'original-initrd.gz'
        write_installer_initrd(original, tree)
        original_bytes = original.read_bytes()
        bundle = root / 'bundle'
        assets = bundle / 'hd-media'
        assets.mkdir(parents=True)
        shutil.copy2(kernel, assets / 'vmlinuz')
        shutil.copy2(original, assets / 'initrd.gz')
        # Redirect locations only. No module/archive/ISO/profile behavior is mocked.
        with patch.object(source, 'DEFAULT_WORK_DIR', root / 'work'), patch.object(source, 'DEFAULT_STATE_DIR', root / 'state'), patch.object(source, 'DEFAULT_LOG_DIR', root / 'logs'):
            with source._InstallerInitrdSession(assets / 'initrd.gz') as session:
                native = inspect_preseed_loader(session.tree())
                rebuilt = source._rebuild_debian_netinst_initrd_modules(
                    kernel_path=assets / 'vmlinuz', initrd_path=assets / 'initrd.gz',
                    selected_options=['xxhash_generic', 'lz4'], bundle_root=bundle,
                    module_source_strategy='host-kernel', session=session)
                policy = source._enforce_exact_iso_scan_filename(
                    initrd_path=assets / 'initrd.gz', bundle_root=bundle, session=session)
            profiles = prepare_installer_profiles(bundle, 'debian', 'netinst')
        checked = []
        for flavor in ('desktop', 'server'):
            expanded = root / flavor
            _extract_initrd_archive(bundle / PROFILE_ASSET_DIR / flavor / 'initrd.gz', expanded)
            assert source.ISO_SCAN_EXACT_SELECTION_MARKER in (expanded / 'var/lib/dpkg/info/iso-scan.postinst').read_text(encoding='utf-8')
            assert (expanded / 'lib/debian-usb/initrd-preseed.original').read_text(encoding='utf-8') == native.original
            assert stat.S_ISCHR((expanded / 'dev/console').stat().st_mode)
            assert os.major((expanded / 'dev/console').stat().st_rdev) == 5
            assert (expanded / 'lib/modules' / abi / 'modules.dep').stat().st_size > 0
            module_files = [str(p.relative_to(expanded)) for p in (expanded / 'lib/modules' / abi).rglob('*.ko*')]
            assert any('xxhash' in p for p in module_files), module_files
            assert any('lz4' in p for p in module_files), module_files
            checked.append({'flavor': flavor, 'modules': module_files, 'transport': profiles[flavor]['preseed_transport']})
        assert original.read_bytes() == original_bytes
        return {'kernel_abi': abi, 'real_kernel_sha256': hashlib.sha256(kernel.read_bytes()).hexdigest(),
                'rebuild_mode': rebuilt['rebuild_mode'], 'exact_iso_policy_enforced': policy['enforced'],
                'source_initrd_unchanged': True, 'profiles': checked,
                'fixture': 'synthetic installer userspace with real installed donor kernel/modules and cpio device nodes',
                'vm_or_hardware_boot': False}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--kernel', type=Path, required=True)
    print(json.dumps(run(parser.parse_args().kernel.resolve(strict=True)), indent=2))
