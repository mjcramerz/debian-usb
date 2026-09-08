"""Discover and gate Debian Installer's native preseed startup loader.

A reference to /preseed.cfg does NOT identify a loader. Other startup scripts
can test for, log, or document that file. Discover named/package-owned loaders
first; use a loader-call signature only as a compatibility fallback. Resolve
initrd links relative to the initrd root, never the build host's /lib or /usr.

Embedded preseeding executes the native script verbatim. Netboot HD-MEDIA uses
preseed-common's API directly, with a file:// URL, rather than regexp-rewriting
a downloaded shell script (which used to miss file:///preseed.cfg operands).
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import tempfile
from typing import Any

DISPATCH_VERSION = 2
DISPATCH_MARKER = "# debian-usb managed installer preseed transport dispatch"
STARTUP_DIRS = (
    "lib/debian-installer-startup.d", "usr/lib/debian-installer-startup.d",
    "lib/debian-installer.d", "usr/lib/debian-installer.d",
)
_NATIVE_NAME = re.compile(r"(?:S[0-9]+)?initrd-preseed(?:\.sh)?$")
_ACTIVE_NAME = re.compile(r"(?:S[0-9]+[A-Za-z0-9_-]+|initrd-preseed)(?:\.sh)?$")
_LOADER_CALL = re.compile(
    # A command-position signature, not text in echo/printf diagnostics.
    r"(?:^|[;\n]|&&|\|\|)\s*(?:(?:then|do)\s+)?"
    r"(?:preseed_location|debconf-set-selections)\s+"
    r"[\"']?(?:file://)?/preseed\.cfg(?:[\"'\s;]|$)"
)


def initrd_path(root: Path, path: str) -> Path:
    """Resolve a guest path with chroot-style symlink semantics, without chroot.

    Absolute symlink targets are guest-absolute, not host-absolute. Missing
    terminal components are permitted for a new output. No traversal can escape
    root, and loops are rejected. Callers operate only on private build trees.
    """
    root = root.resolve(strict=True)
    pending = deque(PurePosixPath(path).parts)
    parts: list[str] = []
    links = 0
    while pending:
        part = pending.popleft()
        if part in ("", ".", "/"):
            continue
        if part == "..":
            if parts:
                parts.pop()
            continue
        candidate = root.joinpath(*parts, part)
        if candidate.is_symlink():
            links += 1
            if links > 40:
                raise ValueError(f"initrd symlink loop while resolving /{path.lstrip('/')}")
            target = PurePosixPath(os.readlink(candidate))
            if target.is_absolute():
                parts.clear()
            pending.extendleft(reversed(target.parts))
        else:
            parts.append(part)
    return root.joinpath(*parts)


def _regular_text(path: Path) -> str:
    if not path.is_file() or not stat.S_ISREG(path.stat().st_mode):
        raise ValueError(f"expected a regular installer script: {path}")
    if path.stat().st_size > 1024 * 1024:
        raise ValueError(f"installer startup script is unexpectedly large: {path}")
    return path.read_bytes().decode("utf-8")


def _check_shell(text: str, label: str) -> None:
    if not text.startswith("#!") or "\x00" in text:
        raise ValueError(f"{label}: expected an executable shell script with a shebang")
    result = subprocess.run(["sh", "-n"], input=text, text=True, encoding="utf-8",
                            capture_output=True, check=False)
    if result.returncode:
        raise ValueError(f"{label}: shell syntax validation failed: {result.stderr.strip()}")


@dataclass(frozen=True)
class PreseedLoader:
    # hook is the actual startup entry, NOT its symlink target. Replacing the
    # entry must not modify a shared target used by another installer component.
    hook: Path
    hook_guest: str
    resolved_guest: str
    original: str
    origin: str
    support_guest: str
    confmodule_guest: str
    library_guest: str
    inspected: tuple[str, ...]


def inspect_preseed_loader(root: Path) -> PreseedLoader:
    """Inspect only: no writes, no downloaded code execution, no host sourcing."""
    root = root.resolve(strict=True)
    directories: dict[Path, str] = {}
    inspected: list[str] = []
    for guest in STARTUP_DIRS:
        path = initrd_path(root, guest)
        if path.is_dir() and path not in directories:
            directories[path] = "/" + guest
    if not directories:
        raise ValueError("installer preseed discovery: no Debian Installer startup directory; "
                         "searched " + ", ".join("/" + p for p in STARTUP_DIRS))

    # Package ownership covers renamed/vendor loaders without accepting every
    # script mentioning the seed. The .list file is optional in compact udebs.
    owned: set[str] = set()
    listing = initrd_path(root, "var/lib/dpkg/info/initrd-preseed.list")
    if listing.is_file():
        owned = {"/" + p.strip().lstrip("./") for p in _regular_text(listing).splitlines()
                 if p.strip()}
    named: list[tuple[Path, str, Path, str]] = []
    compatible: list[tuple[Path, str, Path, str]] = []
    for directory, guest_dir in directories.items():
        for entry in sorted(directory.iterdir()):
            guest = guest_dir + "/" + entry.name
            inspected.append(guest)
            if not _ACTIVE_NAME.fullmatch(entry.name):
                continue
            resolved = initrd_path(root, guest)
            is_named = bool(_NATIVE_NAME.fullmatch(entry.name)) or guest in owned
            if not resolved.is_file():
                if is_named:
                    raise ValueError(f"installer preseed loader {guest} has a missing/non-regular target "
                                     f"/{resolved.relative_to(root)}")
                continue
            text = _regular_text(resolved)
            record = (entry, guest, resolved, text)
            if is_named:
                named.append(record)
            else:
                active = "\n".join(line for line in text.splitlines()
                                   if not line.lstrip().startswith("#"))
                if _LOADER_CALL.search(active):
                    compatible.append(record)
    candidates = named + compatible
    if len(candidates) > 1:
        raise ValueError("installer preseed discovery: multiple active native loaders: "
                         + ", ".join(record[1] for record in candidates)
                         + "; remove the competing loader from the selected overlay/source, "
                         "not ordinary scripts that merely mention preseed.cfg")

    def required_path(choices: tuple[str, ...], description: str) -> str:
        for guest in choices:
            if initrd_path(root, guest).is_file():
                return "/" + guest
        raise ValueError("installer preseed discovery: missing " + description + "; searched "
                         + ", ".join("/" + p for p in choices)
                         + "; startup entries: " + ", ".join(inspected))

    confmodule = required_path(("usr/share/debconf/confmodule",), "debconf shell library")
    library = required_path(("lib/preseed/preseed.sh", "usr/lib/preseed/preseed.sh"),
                            "preseed-common shell library")
    support_guest = "/lib/debian-usb" if initrd_path(root, "lib").is_dir() else "/usr/lib/debian-usb"
    if candidates:
        hook, guest, resolved, original = candidates[0]
        origin = "native"
        if DISPATCH_MARKER in original:
            saved = initrd_path(root, support_guest + "/initrd-preseed.original")
            original = _regular_text(saved)
            if DISPATCH_MARKER in original:
                raise ValueError("managed preseed backup is itself a dispatcher; use the original input initrd")
            metadata_path = initrd_path(root, support_guest + "/transport.json")
            if metadata_path.is_file():
                metadata = json.loads(_regular_text(metadata_path))
                digest = hashlib.sha256(original.encode("utf-8")).hexdigest()
                if not isinstance(metadata, dict) or metadata.get("native_sha256") != digest:
                    raise ValueError("managed preseed backup checksum mismatch; use the original input initrd")
                origin = str(metadata.get("origin", "native"))
            else:
                origin = "legacy-managed-backup"
        _check_shell(original, guest)
        return PreseedLoader(hook, guest, "/" + str(resolved.relative_to(root)), original,
                             origin, support_guest, confmodule, library, tuple(inspected))

    # Some compact/vendor installers omit initrd-preseed while shipping its
    # preseed-common API. Install the small equivalent adapter only when the
    # API and the early-command entry point are demonstrably present. Never
    # accept a random Linux/live initramfs or remove the native loader check.
    library_text = _regular_text(initrd_path(root, library))
    has_location = bool(re.search(r"\bpreseed_location\s*\(\s*\)", library_text))
    has_command = bool(re.search(r"\bpreseed_command\s*\(\s*\)", library_text)) or any(
        initrd_path(root, p).is_file() for p in ("bin/preseed_command", "usr/bin/preseed_command"))
    startup = next(((p, g) for p, g in directories.items() if g.endswith("debian-installer-startup.d")), None)
    if not (has_location and has_command and startup):
        raise ValueError("installer preseed discovery: no native loader or complete preseed-common adapter API; "
                         "startup entries: " + ", ".join(inspected))
    directory, guest_dir = startup
    original = ("#!/bin/sh\nset -e\n. " + confmodule + "\n. " + library + "\n"
                "if [ -e /preseed.cfg ]; then\n    preseed_location file:///preseed.cfg\nfi\n"
                "preseed_command preseed/early_command\n")
    hook = directory / "S30initrd-preseed"
    return PreseedLoader(hook, guest_dir + "/S30initrd-preseed", "", original,
                         "preseed-common-adapter", support_guest, confmodule, library, tuple(inspected))


HD_MEDIA_LOADER = r'''#!/bin/sh
# Use preseed-common's URL/include/checksum/early-command implementation.
# No set -u: native preseed-common/confmodule may access unset positional args.
set -e
. @CONFMODULE@
. @LIBRARY@
seed_file=$(cat /var/run/debian-usb-hd-media-preseed)
case "$seed_file" in /hd-media/*/preseed.cfg) ;; *) echo 'debian-usb: invalid HD-MEDIA seed path' >&2; exit 1 ;; esac
case "$seed_file" in *..*|*[!A-Za-z0-9._+/-]*) echo 'debian-usb: unsafe HD-MEDIA seed path' >&2; exit 1 ;; esac
[ -s "$seed_file" ] || { echo "debian-usb: missing HD-MEDIA seed: $seed_file" >&2; exit 1; }
checksum=
set -f
for argument in $(cat /proc/cmdline); do
    case "$argument" in preseed/file/checksum=*|preseed-md5=*) checksum=${argument#*=} ;; esac
done
set +f
if [ -n "$checksum" ]; then
    case "$checksum" in *[!a-fA-F0-9]*) echo 'debian-usb: invalid preseed checksum' >&2; exit 1 ;; esac
    [ "${#checksum}" -eq 32 ] || { echo 'debian-usb: invalid preseed checksum length' >&2; exit 1; }
fi
preseed_location "file://$seed_file" "$checksum"
preseed_command preseed/early_command
'''


def _write_private_script(path: Path, text: str) -> None:
    """Replace the directory entry, not any symlink/hardlink target."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=".preseed-script-", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
        temporary.chmod(0o755)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def install_transport_dispatch(root: Path, source_role: str, mount_helper: str) -> dict[str, Any]:
    if source_role not in {"netinst", "netboot"}:
        raise ValueError(f"unsupported installer transport role: {source_role}")
    loader = inspect_preseed_loader(root)
    support = initrd_path(root, loader.support_guest)
    support.mkdir(parents=True, exist_ok=True)
    usb_branch = '''
    @SUPPORT@/mount-hd-media-preseed || exit 1
    exec @SUPPORT@/load-hd-media-preseed "$@"
''' if source_role == "netboot" else '''
    # file-preseed runs after hd-media has mounted the exact selected source.
    exit 0
'''
    wrapper = '''#!/bin/sh
''' + DISPATCH_MARKER + '''
set -eu
set -f
mode=initrd
# Never let inherited host/kernel environment redirect an embedded seed.
unset DUSB_PRESEED_FILE
for argument in $(cat /proc/cmdline); do
    case "$argument" in DUSB_PRESEED_MODE=*) mode=${argument#*=} ;; esac
done
case "$mode" in
  initrd) exec @SUPPORT@/initrd-preseed.original "$@" ;;
  http|https) exit 0 ;;
  usb)
''' + usb_branch + '''    ;;
  *) echo "debian-usb: invalid preseed mode: $mode" >&2; exit 1 ;;
esac
'''
    wrapper = wrapper.replace("@SUPPORT@", loader.support_guest)
    hd_loader = HD_MEDIA_LOADER.replace("@CONFMODULE@", loader.confmodule_guest).replace("@LIBRARY@", loader.library_guest)
    for label, text in ((loader.hook_guest, wrapper), ("mount-hd-media-preseed", mount_helper),
                        ("load-hd-media-preseed", hd_loader)):
        _check_shell(text, label)
    _write_private_script(support / "initrd-preseed.original", loader.original)
    _write_private_script(support / "mount-hd-media-preseed", mount_helper)
    _write_private_script(support / "load-hd-media-preseed", hd_loader)
    metadata = {
        "version": DISPATCH_VERSION, "source_role": source_role,
        "hook": loader.hook_guest, "resolved_hook": loader.resolved_guest,
        "origin": loader.origin,
        "native_sha256": hashlib.sha256(loader.original.encode("utf-8")).hexdigest(),
    }
    metadata_path = support / "transport.json"
    # Replace rather than follow an overlay-provided symlink.
    _write_private_script(metadata_path, json.dumps(metadata, indent=2, sort_keys=True) + "\n")
    metadata_path.chmod(0o644)
    _write_private_script(loader.hook, wrapper)
    return metadata
