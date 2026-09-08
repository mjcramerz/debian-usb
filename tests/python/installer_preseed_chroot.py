#!/usr/bin/env python3
"""Optional root-only BusyBox chroot smoke test, not a VM or installer boot.

Only host BusyBox/shared libraries are copied. No mounts, network, real device
nodes, or host disks are used. Debconf/preseed-common are explicit test doubles;
all generated transport scripts execute unchanged at their real guest paths.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'src/python'))
from installer_fixture import NATIVE_PRESEED, write_preseed_libraries
from debian_usb.installer_profiles import _install_transport_dispatch


def run() -> dict[str, object]:
    if os.geteuid() != 0:
        raise SystemExit('This optional isolated chroot smoke test requires root/CAP_SYS_CHROOT. Never use sudo make.')
    busybox = shutil.which('busybox')
    chroot = shutil.which('chroot')
    if not busybox or not chroot:
        raise SystemExit('busybox and chroot are required')
    # Inspect the trusted HOST BusyBox only; never run ldd on downloaded code.
    linked = subprocess.run(['ldd', busybox], capture_output=True, text=True, encoding='utf-8', check=True).stdout
    libraries = set(re.findall(r'(/[^\s()]+)', linked))
    results: list[str] = []
    with tempfile.TemporaryDirectory(prefix='preseed-busybox-chroot-') as temporary:
        base = Path(temporary)
        for role in ('netinst', 'netboot'):
            root = base / role
            root.mkdir()
            write_preseed_libraries(root)
            for directory in ('bin', 'proc', 'dev', 'var/run', 'hd-media/debian-preseed-de'):
                (root / directory).mkdir(parents=True, exist_ok=True)
            shutil.copy2(busybox, root / 'bin/busybox')
            for library in libraries:
                destination = root / library.lstrip('/')
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(library, destination)
            for applet in ('sh', 'cat', 'md5sum', 'cut'):
                (root / 'bin' / applet).symlink_to('busybox')
            # A regular /dev/null fixture is sufficient for these scripts.
            (root / 'dev/null').touch()
            hook = root / 'lib/debian-installer-startup.d/S30initrd-preseed'
            hook.parent.mkdir(parents=True)
            hook.write_text(NATIVE_PRESEED, encoding='utf-8')
            hook.chmod(0o755)
            _install_transport_dispatch(root, role)
            assert (root / 'lib/debian-usb/initrd-preseed.original').read_text(encoding='utf-8') == NATIVE_PRESEED
            library = root / 'lib/preseed/preseed.sh'
            library.write_text('''# Test double: records API calls, verifies URL and checksum, not a preseed parser.
preseed_location () {
    case "$1" in file:///*) ;; *) return 51 ;; esac
    path=${1#file://}
    [ -f "$path" ] || return 52
    if [ -n "${2-}" ]; then
        actual=$(md5sum "$path" | cut -d ' ' -f 1)
        [ "$actual" = "$2" ] || return 53
    fi
    printf 'load:%s:%s\\n' "$1" "${2-}" >> /trace
}
preseed_command () { printf 'early:%s\\n' "$1" >> /trace; }
''', encoding='utf-8')
            usb = root / 'hd-media/debian-preseed-de/preseed.cfg'
            usb.write_text('USB fixture\n', encoding='utf-8')
            (root / 'preseed.cfg').write_text('embedded fixture\n', encoding='utf-8')
            # Only device discovery/mounting is stubbed. The native loader,
            # dispatcher, and URL loader run unchanged at their guest paths.
            (root / 'lib/debian-usb/mount-hd-media-preseed').write_text(
                '#!/bin/sh\nprintf "%s\\n" /hd-media/debian-preseed-de/preseed.cfg > /var/run/debian-usb-hd-media-preseed\n', encoding='utf-8')
            digest = hashlib.md5(usb.read_bytes()).hexdigest()
            scenarios = [
                ('initrd', '', 0, 'load:file:///preseed.cfg:\nearly:preseed/early_command\n'),
                ('http', '', 0, ''), ('https', '', 0, ''),
                ('usb', '', 0, 'load:file:///hd-media/debian-preseed-de/preseed.cfg:\nearly:preseed/early_command\n' if role == 'netboot' else ''),
                ('invalid', '', 1, ''),
            ]
            if role == 'netboot':
                scenarios.extend([
                    ('usb', 'preseed-md5=' + digest, 0, f'load:file:///hd-media/debian-preseed-de/preseed.cfg:{digest}\nearly:preseed/early_command\n'),
                    ('usb', 'preseed/file/checksum=' + digest, 0, f'load:file:///hd-media/debian-preseed-de/preseed.cfg:{digest}\nearly:preseed/early_command\n'),
                    ('usb', 'preseed-md5=' + '0' * 32, 53, ''),
                    ('usb', 'preseed-md5=not-hex', 1, ''),
                    ('usb', 'preseed-md5=12', 1, ''),
                ])
            for mode, extra, expected_rc, expected in scenarios:
                (root / 'trace').unlink(missing_ok=True)
                (root / 'proc/cmdline').write_text(f'DUSB_PRESEED_MODE={mode} {extra}', encoding='utf-8')
                result = subprocess.run([chroot, str(root), '/bin/sh', '/lib/debian-installer-startup.d/S30initrd-preseed'],
                                        env={**os.environ, 'PATH': '/bin:/usr/bin', 'DUSB_PRESEED_FILE': '/wrong'},
                                        capture_output=True, text=True, encoding='utf-8', timeout=10)
                assert result.returncode == expected_rc, (role, mode, extra, result.returncode, result.stderr)
                actual = (root / 'trace').read_text(encoding='utf-8') if (root / 'trace').exists() else ''
                assert actual == expected, (role, mode, extra, actual, expected)
                results.append(f'{role}/{mode}/{extra or "no-checksum"}')
    return {'passed': results, 'count': len(results), 'shell': 'host BusyBox /bin/sh in isolated chroot',
            'native_loader_fixture': 'official Kali repository source, not a current binary initrd',
            'stubs': ['debconf', 'preseed-common API', 'USB mounting'],
            'disk_writes': 'temporary directory only', 'vm_or_hardware_boot': False}


if __name__ == '__main__':
    print(json.dumps(run(), indent=2))
