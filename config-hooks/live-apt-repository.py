#!/usr/bin/python3
"""Expose only complete, readable offline APT repositories from this live ISO.

This helper is baked into the squashfs; it never installs packages or downloads
indexes. In module-only toram mode the original ISO may be reopened read-only
using the exact findiso path and filesystem UUID supplied by managed GRUB.
"""
from __future__ import annotations

import argparse
import bz2
import gzip
import hashlib
import lzma
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import stat
import subprocess
import sys
import tempfile
from urllib.parse import quote

SAFE_TOKEN = re.compile(r"^[a-z0-9][a-z0-9+.-]{0,63}$")
SAFE_UUID = re.compile(r"^[A-Za-z0-9-]{1,128}$")
MAX_INDEX_BYTES = 256 * 1024 * 1024
SOURCE_NAME = "debian-usb-medium.sources"


def warn(message: str) -> None:
    print(f"debian-usb-live-apt: {message}", file=sys.stderr)


def beneath(root: Path, relative: str) -> Path:
    """Reject package/index symlinks escaping the selected medium."""
    value = PurePosixPath(relative)
    if not relative or value.is_absolute() or ".." in value.parts or "\x00" in relative:
        raise ValueError(f"unsafe repository path: {relative!r}")
    target = (root / value).resolve(strict=True)
    if not target.is_relative_to(root.resolve()):
        raise ValueError(f"repository path escapes medium: {relative!r}")
    return target


def fields(text: str) -> dict[str, str]:
    result: dict[str, str] = {}
    key = ""
    for line in text.splitlines():
        if line.startswith((" ", "\t")) and key:
            result[key] += "\n" + line.strip()
        elif ":" in line:
            key, value = line.split(":", 1)
            key = key.lower()
            result[key] = value.strip()
        else:
            key = ""
    return result


def package_index_complete(medium: Path, index: Path) -> bool:
    """Validate every referenced binary package, not just the index filename.

    APT verifies package hashes when it consumes them. This check verifies
    path containment, presence, non-empty packages, and declared sizes.
    """
    opener = {".xz": lzma.open, ".gz": gzip.open, ".bz2": bz2.open}.get(index.suffix, open)
    total = 0
    count = 0
    stanza: list[str] = []

    def check(lines: list[str]) -> bool:
        nonlocal count
        if not lines:
            return True
        values = fields("\n".join(lines))
        filename = values.get("filename", "")
        if not filename:
            return False
        package = beneath(medium, filename)
        size = package.stat().st_size
        if not package.is_file() or size == 0 or size != int(values.get("size", "-1")):
            return False
        count += 1
        return True

    try:
        with opener(index, "rt", encoding="utf-8", errors="strict") as stream:
            while True:
                line = stream.readline(1024 * 1024 + 1)
                if not line:
                    break
                total += len(line)
                if len(line) > 1024 * 1024 or total > MAX_INDEX_BYTES:
                    return False
                if not line.strip():
                    if not check(stanza):
                        return False
                    stanza.clear()
                else:
                    stanza.append(line.rstrip("\n"))
            if not check(stanza):
                return False
    except (OSError, EOFError, ValueError, UnicodeError, lzma.LZMAError):
        return False
    return count > 0


def repository_components(medium: Path, suite: str, architecture: str) -> list[str]:
    """Return components with Release metadata, indexes, and actual .deb files."""
    try:
        try:
            release_path = beneath(medium, f"dists/{suite}/Release")
        except FileNotFoundError:
            release_path = beneath(medium, f"dists/{suite}/InRelease")
        if release_path.stat().st_size > 2 * 1024 * 1024:
            return []
        release = fields(release_path.read_text(encoding="utf-8"))
        if suite not in {release.get("suite"), release.get("codename")}:
            return []
        if architecture not in release.get("architectures", "").split():
            return []
        hashes: dict[str, tuple[str, int]] = {}
        for row in release.get("sha256", "").splitlines():
            parts = row.split()
            if len(parts) == 3 and re.fullmatch(r"[0-9a-fA-F]{64}", parts[0]):
                hashes[parts[2]] = (parts[0].lower(), int(parts[1]))
        result = []
        for component in release.get("components", "").split():
            if not SAFE_TOKEN.fullmatch(component):
                continue
            for suffix in (".xz", ".gz", ".bz2", ""):
                relative = f"{component}/binary-{architecture}/Packages{suffix}"
                try:
                    index = beneath(medium, f"dists/{suite}/{relative}")
                    digest, expected_size = hashes[relative]
                except (OSError, KeyError, ValueError):
                    continue
                if not index.is_file() or index.stat().st_size > MAX_INDEX_BYTES:
                    continue
                if index.stat().st_size != expected_size:
                    continue
                with index.open("rb") as stream:
                    actual_hash = hashlib.file_digest(stream, "sha256").hexdigest() if hasattr(hashlib, "file_digest") else _file_sha256(stream)
                if actual_hash == digest and package_index_complete(medium, index):
                    result.append(component)
                    break
        return result
    except (OSError, ValueError, UnicodeError):
        return []


def _file_sha256(stream: object) -> str:
    digest = hashlib.sha256()
    while chunk := stream.read(1024 * 1024):
        digest.update(chunk)
    return digest.hexdigest()


def exact_iso_locator(cmdline: str) -> tuple[str, str] | None:
    """No wildcard device scans or mixing ISO repositories on Multi-OS media."""
    try:
        values = dict(token.split("=", 1) for token in shlex.split(cmdline) if "=" in token)
    except ValueError:
        return None
    device = values.get("live-media", "")
    iso = values.get("findiso", "")
    prefix = "/dev/disk/by-uuid/"
    if not device.startswith(prefix) or not SAFE_UUID.fullmatch(device[len(prefix):]):
        return None
    path = PurePosixPath(iso)
    if not iso.startswith("/") or ".." in path.parts or not iso.lower().endswith(".iso"):
        return None
    if any(ord(char) < 32 for char in iso) or len(iso) > 4096:
        return None
    return device, iso


def run(command: list[str]) -> str:
    result = subprocess.run(command, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, encoding="utf-8", timeout=20)
    return result.stdout.strip()


def reopen_exact_iso(cmdline: str, runtime: Path = Path("/run/debian-usb")) -> Path | None:
    locator = exact_iso_locator(cmdline)
    if not locator or os.geteuid() != 0:
        return None
    device, iso = locator
    store = runtime / "apt-store"
    medium = runtime / "apt-medium"
    mounted: list[Path] = []
    try:
        if not stat.S_ISBLK(os.stat(device).st_mode):
            return None
        runtime.mkdir(mode=0o755, parents=True, exist_ok=True)
        if runtime.is_symlink() or runtime.stat().st_uid != 0:
            return None
        for path in (store, medium):
            if path.is_symlink():
                raise ValueError(f"unsafe mount destination: {path}")
            path.mkdir(mode=0o755, exist_ok=True)
        # Reuse only our own exact locator from this boot. Never attach another
        # ISO over a mounted repository or trust an arbitrary existing mount.
        marker = runtime / "apt-locator"
        identity = device + "\n" + iso + "\n"
        if os.path.ismount(medium):
            return medium if marker.is_file() and marker.read_text(encoding="utf-8") == identity else None
        if os.path.ismount(store):
            if not marker.is_file() or marker.read_text(encoding="utf-8") != identity:
                return None
        else:
            fs_type = run(["blkid", "-s", "TYPE", "-o", "value", device])
            options = "ro,nosuid,nodev,noexec"
            if fs_type in {"ext3", "ext4"}:
                options += ",noload"
            run(["mount", "-t", fs_type, "-o", options, "--", device, str(store)])
            mounted.append(store)
        payload = beneath(store, iso.lstrip("/"))
        if not payload.is_file() or payload.stat().st_size == 0:
            raise ValueError("exact live ISO is missing")
        run(["mount", "-t", "iso9660", "-o", "loop,ro,nosuid,nodev,noexec", "--", str(payload), str(medium)])
        mounted.append(medium)
        marker.write_text(identity, encoding="utf-8")
        marker.chmod(0o600)
        return medium
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        warn(f"offline repository unavailable: {exc}")
        for path in reversed(mounted):
            try:
                run(["umount", "--", str(path)])
            except (OSError, subprocess.SubprocessError):
                pass
        return None


def write_sources(root: Path, suite: str, architecture: str, *, allow_mount: bool = True) -> bool:
    if not SAFE_TOKEN.fullmatch(suite) or not SAFE_TOKEN.fullmatch(architecture):
        raise ValueError("invalid APT suite or architecture")
    parts = root / "etc/apt/sources.list.d"
    parts.mkdir(parents=True, exist_ok=True)
    target = parts / SOURCE_NAME
    candidates = [root / item for item in ("run/live/medium", "lib/live/mount/medium", "cdrom")]
    selected: tuple[Path, list[str]] | None = None
    for medium in candidates:
        components = repository_components(medium, suite, architecture)
        if components:
            selected = medium, components
            break
    if selected is None and root == Path("/") and allow_mount:
        cmdline = Path("/proc/cmdline").read_text(encoding="utf-8")
        medium = reopen_exact_iso(cmdline)
        if medium:
            components = repository_components(medium, suite, architecture)
            if components:
                selected = medium, components
    if selected is None:
        target.unlink(missing_ok=True)
        return False
    medium, components = selected
    content = (
        "# Read-only packages from the selected live ISO; never the squashfs mount.\n"
        "Types: deb\n"
        f"URIs: file:{quote(str(medium), safe='/')}\n"
        f"Suites: {suite}\n"
        f"Components: {' '.join(components)}\n"
        f"Architectures: {architecture}\n"
        # Trust is deliberately scoped to local ISO data. Existing network
        # sources keep their own Signed-By policy; no global auth bypass.
        "Trusted: yes\n"
        "Check-Valid-Until: no\n"
    )
    if target.is_symlink():
        raise ValueError("refusing symlinked managed APT source")
    if target.is_file() and target.read_text(encoding="utf-8") == content:
        return True
    fd, name = tempfile.mkstemp(prefix=".debian-usb-medium-", dir=parts)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(name, 0o644)
        os.replace(name, target)
    finally:
        Path(name).unlink(missing_ok=True)
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("/"))
    parser.add_argument("--suite", required=True)
    parser.add_argument("--architecture", default="")
    args = parser.parse_args()
    try:
        architecture = args.architecture or run(["dpkg", "--print-architecture"])
        write_sources(args.root.resolve(), args.suite, architecture)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        warn(str(exc))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
