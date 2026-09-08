#!/usr/bin/env python3
"""Non-destructive split/transport verification of a selected installer initrd.

Run against trusted, checksum-verified installer downloads. Uses fakeroot to
preserve device metadata without sudo. Copies inputs into a temporary directory,
builds both flavor archives, reports discovery evidence, and deletes the copies.
Does not validate ISO/kernel ABI alignment, install packages, boot an OS, mount
a filesystem, or write a USB. The regular application validates source alignment.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src/python'))
from debian_usb.installer_profiles import prepare_installer_profiles


def digest(path: Path) -> str:
    result = hashlib.sha256()
    with path.open('rb') as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b''):
            result.update(block)
    return result.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--kernel', required=True, type=Path)
    parser.add_argument('--initrd', required=True, type=Path)
    parser.add_argument('--profile', choices=('debian', 'kali-linux'), default='debian')
    parser.add_argument('--role', choices=('netinst', 'netboot'), default='netinst')
    parser.add_argument('--overlay-dir', type=Path)
    args = parser.parse_args()
    try:
        kernel, initrd = args.kernel.resolve(strict=True), args.initrd.resolve(strict=True)
        for path in (kernel, initrd):
            if not path.is_file() or not path.stat().st_size:
                raise ValueError(f'expected a nonempty regular input: {path}')
        overlay = args.overlay_dir.resolve(strict=True) if args.overlay_dir else None
        if overlay is not None and not overlay.is_dir():
            raise ValueError(f'expected an overlay directory: {overlay}')
        if not os.environ.get('FAKEROOTKEY'):
            fakeroot = shutil.which('fakeroot')
            if not fakeroot:
                raise ValueError('fakeroot is required for this non-destructive check; install the project prerequisites, then retry without sudo')
            os.execv(fakeroot, [fakeroot, sys.executable, str(Path(__file__).resolve()), *sys.argv[1:]])
        before = {str(path): digest(path) for path in (kernel, initrd)}
        with tempfile.TemporaryDirectory(prefix='verify-installer-initrd-') as temporary:
            root = Path(temporary)
            assets = root / ('hd-media' if args.role == 'netinst' else 'netboot')
            assets.mkdir()
            shutil.copy2(kernel, assets / 'vmlinuz')
            shutil.copy2(initrd, assets / 'initrd.gz')
            profiles = prepare_installer_profiles(root, args.profile, args.role, overlay_root=overlay)
        after = {str(path): digest(path) for path in (kernel, initrd)}
        if before != after:
            raise ValueError('input files changed while verification was running; do not use these results')
        print(json.dumps({'scope': 'archive split and preseed transport only', 'profile': args.profile,
                          'source_role': args.role, 'source_sha256': before,
                          'inputs_unchanged': True, 'profiles': profiles,
                          'boot_tested': False, 'iso_kernel_alignment_tested': False}, indent=2))
        return 0
    except (OSError, ValueError, RuntimeError) as exc:
        print(f'verify-installer-initrd: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
