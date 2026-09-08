"""Archive fixtures: native loader syntax plus explicitly synthetic support files.

Loader source (20 shell words) verified from Kali's official preseed repository:
https://gitlab.com/kalilinux/packages/preseed/-/raw/kali/master/debian-installer-startup.d/S30initrd-preseed
This is a source-level fixture, NOT a downloaded/current Debian or Kali initrd.
The support libraries/observer below are test doubles, not vendor code.
"""
from pathlib import Path
from debian_usb.rebuild_iso import _repack_initrd_archive

NATIVE_PRESEED = """#!/bin/sh
set -e

. /usr/share/debconf/confmodule
. /lib/preseed/preseed.sh

if [ -e /preseed.cfg ]; then
\tpreseed_location file:///preseed.cfg
fi
preseed_command preseed/early_command
"""


def write_preseed_libraries(tree: Path) -> None:
    for name, text in {
        'usr/share/debconf/confmodule': '# Synthetic archive fixture; never a real debconf session.\n',
        'lib/preseed/preseed.sh': 'preseed_location () { :; }\npreseed_command () { :; }\n',
    }.items():
        path = tree / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8')


def write_installer_initrd(path: Path, tree: Path | None = None) -> None:
    import tempfile
    if tree is None:
        with tempfile.TemporaryDirectory() as temp:
            write_installer_initrd(path, Path(temp))
        return
    hook = tree / 'lib/debian-installer-startup.d/S30initrd-preseed'
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text(NATIVE_PRESEED, encoding='utf-8')
    hook.chmod(0o755)
    # The old detector falsely considered this unrelated observer a second
    # native loader. Include it in EVERY shared archive fixture going forward.
    observer = hook.with_name('S05fixture-seed-observer')
    observer.write_text('#!/bin/sh\n# Observing /preseed.cfg is not loading it.\n[ ! -e /preseed.cfg ] || :\n', encoding='utf-8')
    observer.chmod(0o755)
    write_preseed_libraries(tree)
    _repack_initrd_archive(tree, path)
