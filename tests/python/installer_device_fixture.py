"""Subprocess fixture: cpio device-node metadata, safe under one fakeroot session.

This fixture never opens a device, mounts a filesystem, or writes a host disk.
The archive headers are checked independently of fakeroot's stat emulation.
"""
from __future__ import annotations

import errno
import gzip
import json
import os
from pathlib import Path
import shutil
import stat
import tempfile
from unittest.mock import patch

from installer_fixture import write_installer_initrd
from debian_usb.installer_profiles import PROFILE_ASSET_DIR, prepare_installer_profiles
from debian_usb.rebuild_iso import _extract_initrd_archive


DEVICES = {
    'dev/console': (stat.S_IFCHR, 5, 1, 0o600),
    'dev/null': (stat.S_IFCHR, 1, 3, 0o666),
    'dev/zero': (stat.S_IFCHR, 1, 5, 0o666),
    'dev/fixture-block': (stat.S_IFBLK, 8, 0, 0o600),
}


def archive_records(archive: Path) -> dict[str, tuple[int, ...]]:
    """Read newc metadata independently; do not extract or read device contents."""
    data = gzip.decompress(archive.read_bytes())
    result: dict[str, tuple[int, ...]] = {}
    position = 0
    while position + 110 <= len(data):
        header = data[position:position + 110]
        assert header[:6] == b'070701', header[:6]
        fields = tuple(int(header[start:start + 8], 16) for start in range(6, 110, 8))
        name_end = position + 110 + fields[11]
        name = data[position + 110:name_end - 1].decode('utf-8')
        assert data[name_end - 1] == 0
        if name == 'TRAILER!!!':
            return result
        result[name.removeprefix('./')] = fields
        position = ((name_end + 3) & ~3) + fields[6]
        position = (position + 3) & ~3
    raise AssertionError('missing cpio trailer')


def run_fixture() -> dict[str, object]:
    results = []
    original_copyfile = shutil.copyfile

    def copy_regular_file_only(source: object, target: object, **kwargs: object) -> str:
        metadata = os.lstat(source)
        if stat.S_ISCHR(metadata.st_mode) or stat.S_ISBLK(metadata.st_mode) or stat.S_ISFIFO(metadata.st_mode):
            # Emulate nodev/EACCES for any attempt to READ a device. This is
            # exactly what the old copytree/copy2 path attempted. Real cpio
            # preserves node metadata without opening the device for copying.
            raise PermissionError(errno.EACCES, os.strerror(errno.EACCES), os.fspath(source))
        return original_copyfile(source, target, **kwargs)

    with tempfile.TemporaryDirectory(prefix='installer-device-regression-') as temporary:
        root = Path(temporary)
        for family in ('debian', 'kali-linux'):
            for role in ('netinst', 'netboot'):
                bundle = root / (family + '-' + role)
                assets = bundle / ('hd-media' if role == 'netinst' else 'netboot')
                assets.mkdir(parents=True)
                (assets / 'vmlinuz').write_bytes(b'fixture-kernel\n')
                tree = root / (family + '-' + role + '-tree')
                (tree / 'dev').mkdir(parents=True)
                for name, (kind, major, minor, mode) in DEVICES.items():
                    node = tree / name
                    os.mknod(node, kind | mode, os.makedev(major, minor))
                    node.chmod(mode)
                    assert stat.S_IFMT(node.lstat().st_mode) == kind, 'fakeroot did not intercept device metadata'
                os.mkfifo(tree / 'dev/fixture-pipe', 0o600)
                (tree / 'dev/null-alias').symlink_to('null')
                write_installer_initrd(assets / 'initrd.gz', tree)
                original = (assets / 'initrd.gz').read_bytes()
                with patch('shutil.copyfile', side_effect=copy_regular_file_only):
                    manifests = prepare_installer_profiles(bundle, family, role)
                for flavor in ('desktop', 'server'):
                    archive = bundle / PROFILE_ASSET_DIR / flavor / 'initrd.gz'
                    records = archive_records(archive)
                    for name, (kind, major, minor, mode) in DEVICES.items():
                        fields = records[name]
                        assert stat.S_IFMT(fields[1]) == kind, (name, fields)
                        assert stat.S_IMODE(fields[1]) == mode, (name, fields)
                        assert fields[2:4] == (0, 0), (name, fields)
                        assert fields[9:11] == (major, minor), (name, fields)
                        assert fields[6] == 0, (name, fields)
                    assert stat.S_ISFIFO(records['dev/fixture-pipe'][1])
                    assert stat.S_ISLNK(records['dev/null-alias'][1])
                    extracted = root / (family + '-' + role + '-' + flavor)
                    _extract_initrd_archive(archive, extracted)
                    for name, (kind, major, minor, mode) in DEVICES.items():
                        metadata = (extracted / name).lstat()
                        assert stat.S_IFMT(metadata.st_mode) == kind
                        assert stat.S_IMODE(metadata.st_mode) == mode
                        assert (os.major(metadata.st_rdev), os.minor(metadata.st_rdev)) == (major, minor)
                    assert manifests[flavor]['transport_dispatch'] is True
                    results.append(f'{family}/{role}/{flavor}')
                assert (assets / 'initrd.gz').read_bytes() == original
    return {'passed': results, 'device_headers_per_archive': len(DEVICES),
            'device_content_reads': 0, 'metadata_backend': 'fakeroot' if os.getenv('FAKEROOTKEY') else 'native'}


if __name__ == '__main__':
    print(json.dumps(run_fixture(), sort_keys=True))
