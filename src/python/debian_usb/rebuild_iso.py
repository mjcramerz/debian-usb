from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
import hashlib
import json
from contextlib import contextmanager
import os
from pathlib import Path
import posixpath
import re
import shutil
import stat
import subprocess
import sys
from typing import Any, Iterator

from .boot_parse import _find_boot_entries, _media_class, _select_entry, _select_text_installer_entry
from .build_iso import (
    DEFAULT_CACHE_DIR,
    DEFAULT_LOG_DIR,
    DEFAULT_STATE_DIR,
    DEFAULT_WORK_DIR,
    _configured_build_apt_deps,
    _detect_kernel_version_from_initrd,
    _module_name_from_path,
    _populate_kernel_support_cache,
    _rebuild_udebs_from_source,
    _resolve_binary_package_udeb_rebuild_metadata,
    _resolve_installer_kernel_source_version,
    _run_logged,
    _run_simple,
    _validate_absolute_path,
    _validate_choice,
    _validate_image_name,
    _validate_non_empty_token,
    _validate_optional_module_list,
    _validate_package_list,
)
from .constants import (
    PROFILE_DEBIAN,
    PROFILE_KALI_LINUX,
    PROFILE_TAILS,
    PROFILE_UBUNTU_DESKTOP,
    PROFILE_UBUNTU_SERVER,
)
from .initrd_overlay import merge_initrd_overlay
from .iso_source import _normalize_member_path, open_source
from .live_hooks import (
    live_hook_packages,
    live_optional_firmware,
    debian_live_env_path,
    load_debian_live_wifi_config,
    stage_live_wifi_runtime,
    DEBIAN_LIVE_HOOK_KERNEL_ARGS,
    DEBIAN_LIVE_HOOK_PACKAGES,
    DEBIAN_LIVE_INITRAMFS_MODULES,
    DEBIAN_LIVE_LANGUAGE,
    DEBIAN_LIVE_LOCALE,
    LIVE_SYSTEMD_MASK_UNITS,
    stage_debian_live_config_hooks,
    stage_debian_live_apt_policy,
    _prepare_live_root_directory,
    stage_debian_live_locale,
    stage_debian_live_medium_wifi_config,
    stage_debian_live_wifi_config,
    stage_live_kernel_module_policy,
    stage_live_systemd_masks,
)
from .live_tools import live_tool_packages_for_profile

REBUILD_SCHEMA_VERSION = 1
DISTRO_DEBIAN = "debian"
REBUILD_SCOPES = {"d-i", "live-host"}
REBUILD_ACTIONS = {
    "add-kernel-modules",
    "add-udeb-packages",
    "update-kernel",
    "replace-kernel",
    "add-deb-packages",
}
REBUILD_REQUIRED_COMMANDS = [
    "apt-get",
    "chroot",
    "cpio",
    "depmod",
    "dpkg-deb",
    "lsinitramfs",
    "mksquashfs",
    "mount",
    "umount",
    "unsquashfs",
    "xorriso",
]
REBUILD_EXTRA_APT_DEPS = [
    "kernel-wedge",
    "kmod",
    "squashfs-tools",
    "zstd",
]
LIVE_INITRD_OVERLAY_REQUIRED_COMMANDS = ["bzip2", "cpio", "find", "gzip", "lz4", "xorriso", "xz", "zstd"]
LIVE_INITRD_OVERLAY_APT_DEPS = ["bzip2", "cpio", "findutils", "gzip", "lz4", "xorriso", "xz-utils", "zstd"]
LIVE_INITRD_OVERLAY_PROFILES = {
    PROFILE_DEBIAN,
    PROFILE_KALI_LINUX,
    PROFILE_UBUNTU_DESKTOP,
    PROFILE_UBUNTU_SERVER,
}
DEFAULT_REBUILD_OUTPUT_DIR = Path("/data/downloads/debian-usb/iso/rebuild")
LIVE_ROOTFS_CANDIDATES = (
    "/live/filesystem.squashfs",
    "/live/filesystem.erofs",
    "/casper/filesystem.squashfs",
)
LIVE_PERSISTENCE_SUPPORT_PACKAGES = [
    "cryptsetup",
    "cryptsetup-initramfs",
]
CHROOT_ISO_SOURCE_MOUNT = "/mnt/debian-usb-iso"
CHROOT_TEMP_APT_ROOT = "/tmp/debian-usb-remaster-apt"
MIB = 1024 * 1024
GIB = 1024 * MIB
INITIAL_REMATERIALIZATION_MIN_BYTES = 512 * MIB
OUTPUT_GROWTH_MIN_BYTES = GIB
EXPANDED_ROOTFS_HEADROOM_MIN_BYTES = 2 * GIB
EXPANDED_ROOTFS_HEADROOM_MIN_INODES = 16_384
INITIAL_REMATERIALIZATION_INODES = 8_192
LIVE_MEDIA_APT_URI = "file:/run/live/medium"
LOCAL_MEDIA_APT_URIS = {
    LIVE_MEDIA_APT_URI,
    "file:///run/live/medium",
    "file:/cdrom",
    "file:///cdrom",
}
APT_CDROM_SOURCE_RE = re.compile(
    r"^deb\s+(?P<options>\[[^]]+\]\s+)?cdrom:\[[^]]+\]/?\s+(?P<suite>\S+)(?P<components>(?:\s+\S+)*)$",
    re.IGNORECASE,
)
ARCH_HINTS = {
    "install.amd": "amd64",
    "install.a64": "arm64",
    "/amd64/": "amd64",
    "/arm64/": "arm64",
}
LIVE_BOOT_CONFIG_ROOTS = {"boot", "efi", "isolinux", "syslinux"}
LIVE_KERNEL_ARG_TOKEN_RE = re.compile(r"^[A-Za-z0-9._:+/@${},=-]+$")
LIVE_WIFI_SECRET_KERNEL_ARG_NAMES = frozenset(
    {
        "DEFAULT_LIVE_WIFI_INTERFACE",
        "DEFAULT_LIVE_WIFI_ESSID",
        "DEFAULT_LIVE_WIFI_SECURITY",
        "DEFAULT_LIVE_WIFI_CIDR",
        "DEFAULT_LIVE_WIFI_GATEWAY",
        "DEFAULT_LIVE_WIFI_NAMESERVERS",
        "DEFAULT_LIVE_WIFI_PSK",
        "LIVE_WIFI_INTERFACE",
        "LIVE_WIFI_ESSID",
        "LIVE_WIFI_SECURITY",
        "LIVE_WIFI_CIDR",
        "LIVE_WIFI_GATEWAY",
        "LIVE_WIFI_NAMESERVERS",
        "LIVE_WIFI_PASSPHRASE",
        "PRESEED_WIFI_PASSPHRASE",
        "live_wifi",
        "live_wifi_enabled",
        "live_wifi_interface",
        "live_wifi_iface",
        "live_wifi_ssid",
        "live_wifi_essid",
        "live_wifi_essid_b64",
        "live_wifi_security",
        "live_wifi_cidr",
        "live_wifi_gateway",
        "live_wifi_nameservers",
        "live_wifi_psk",
        "live_wifi_psk_b64",
        "live_wifi_wpa",
        "netcfg/choose_interface",
        "netcfg/wireless_essid",
        "netcfg/wireless_security_type",
        "netcfg/wireless_wpa",
    }
)
MOUNTINFO_ESCAPE_RE = re.compile(r"\\([0-7]{3})")


def _squashfs_processor_count() -> int:
    try:
        available = len(os.sched_getaffinity(0))
    except (AttributeError, OSError):
        available = os.cpu_count() or 1
    return max(1, available // 2)


def ensure_debian_rebuild_deps() -> dict[str, Any]:
    packages = _dedupe(_configured_build_apt_deps() + REBUILD_EXTRA_APT_DEPS)
    missing = [command for command in REBUILD_REQUIRED_COMMANDS if shutil.which(command) is None]
    if not missing:
        return {"changed": False, "missing_commands": [], "packages": packages}

    _run_simple(["apt-get", "update"])
    _run_simple(["apt-get", "install", "-y", "--no-install-recommends", *packages])
    still_missing = [command for command in REBUILD_REQUIRED_COMMANDS if shutil.which(command) is None]
    if still_missing:
        raise RuntimeError(
            "Debian rebuild dependency installation finished but commands are still missing: " + ", ".join(still_missing)
        )
    return {"changed": True, "missing_commands": missing, "packages": packages}


def inspect_debian_rebuild_source(source_iso_path: str) -> dict[str, Any]:
    source_path = _validate_absolute_path(
        source_iso_path,
        allow_missing=False,
        expect_directory=False,
        label="source_iso_path",
    )
    source = open_source(source_path)
    entries = _find_boot_entries(source)
    media_class = _media_class(entries)
    live_entry = _select_entry(PROFILE_DEBIAN, entries)
    installer_entry = _select_text_installer_entry(PROFILE_DEBIAN, entries)
    live_rootfs_path = _find_existing_member(source, LIVE_ROOTFS_CANDIDATES)
    live_kernel_paths = _collect_live_entry_member_paths(entries, "kernel_path")
    live_initrd_paths = _collect_live_entry_member_paths(entries, "initrd_path")
    architecture = _infer_architecture(entries, live_entry.kernel_path if live_entry else "", installer_entry.kernel_path if installer_entry else "")
    warnings: list[str] = []

    installer_kernel_version = ""
    if installer_entry and installer_entry.initrd_path:
        try:
            installer_kernel_version = _detect_kernel_version_from_initrd(source, installer_entry.initrd_path)
            if not architecture:
                architecture = _infer_architecture_from_kernel_version(installer_kernel_version)
        except RuntimeError as exc:
            warnings.append(str(exc))
    else:
        warnings.append("No Debian installer initrd was detected in the source ISO.")

    if live_rootfs_path and not live_rootfs_path.endswith(".squashfs"):
        warnings.append(f"Live root filesystem {live_rootfs_path} is not squashfs. Live Host actions support squashfs-based media only.")

    return {
        "source_path": source.display_path,
        "source_type": source.source_type,
        "volume_id": source.volume_id,
        "media_class": media_class,
        "architecture": architecture,
        "firmware": _detect_firmware(source),
        "best_live_title": live_entry.title if live_entry else "",
        "best_installer_title": installer_entry.title if installer_entry else "",
        "installer_kernel_path": installer_entry.kernel_path if installer_entry else "",
        "installer_initrd_path": installer_entry.initrd_path if installer_entry else "",
        "installer_kernel_version": installer_kernel_version,
        "live_kernel_path": live_entry.kernel_path if live_entry else "",
        "live_kernel_paths": live_kernel_paths,
        "live_initrd_path": live_entry.initrd_path if live_entry else "",
        "live_initrd_paths": live_initrd_paths,
        "live_rootfs_path": live_rootfs_path,
        "warnings": warnings,
    }


def remaster_live_persistence_source(source_iso_path: str, profile: str) -> dict[str, Any]:
    """Compatibility entry point using the same single-pass Live pipeline."""
    if profile == PROFILE_TAILS:
        raise ValueError("Tails native Persistent Storage requires the official USB image on a dedicated device")
    if profile not in {PROFILE_DEBIAN, PROFILE_KALI_LINUX}:
        raise ValueError(f"encrypted persistence remaster supports only Debian and Kali Linux: {profile}")
    return remaster_live_tools_source(source_iso_path, profile, selected_groups=[],
                                     ensure_encrypted_persistence=True)


def _existing_storage_anchor(path: Path) -> Path:
    candidate = path.expanduser().resolve(strict=False)
    while not candidate.exists():
        parent = candidate.parent
        if parent == candidate:
            raise RuntimeError(f"could not resolve an existing filesystem path for storage check: {path}")
        candidate = parent
    return candidate


def _storage_snapshot(path: Path) -> dict[str, Any]:
    anchor = _existing_storage_anchor(path)
    try:
        stat_result = anchor.stat()
        usage = shutil.disk_usage(anchor)
        filesystem = os.statvfs(anchor)
    except OSError as exc:
        raise RuntimeError(f"could not inspect storage capacity for {path}: {exc}") from exc
    free_inodes = filesystem.f_favail if filesystem.f_files > 0 else None
    return {
        "anchor": anchor,
        "device": stat_result.st_dev,
        "free_bytes": usage.free,
        "free_inodes": free_inodes,
    }


def _format_bytes(value: int) -> str:
    amount = float(max(0, value))
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if amount < 1024.0 or unit == "TiB":
            return f"{amount:.1f} {unit}"
        amount /= 1024.0
    return f"{amount:.1f} TiB"


def _require_remaster_storage_capacity(
    requirements: list[tuple[str, Path, int, int]],
    *,
    phase: str,
) -> None:
    filesystems: dict[int, dict[str, Any]] = {}
    for label, path, required_bytes, required_inodes in requirements:
        snapshot = _storage_snapshot(path)
        group = filesystems.setdefault(
            int(snapshot["device"]),
            {
                "free_bytes": int(snapshot["free_bytes"]),
                "free_inodes": snapshot["free_inodes"],
                "required_bytes": 0,
                "required_inodes": 0,
                "locations": [],
            },
        )
        group["free_bytes"] = min(group["free_bytes"], int(snapshot["free_bytes"]))
        if group["free_inodes"] is not None and snapshot["free_inodes"] is not None:
            group["free_inodes"] = min(int(group["free_inodes"]), int(snapshot["free_inodes"]))
        elif snapshot["free_inodes"] is None:
            group["free_inodes"] = None
        group["required_bytes"] += max(0, required_bytes)
        group["required_inodes"] += max(0, required_inodes)
        group["locations"].append(f"{label} ({path})")

    for group in filesystems.values():
        lacks_bytes = group["free_bytes"] < group["required_bytes"]
        lacks_inodes = (
            group["free_inodes"] is not None
            and group["free_inodes"] < group["required_inodes"]
        )
        if not lacks_bytes and not lacks_inodes:
            continue
        required_inode_detail = ""
        available_inode_detail = ""
        if group["free_inodes"] is not None:
            required_inode_detail = f" and {group['required_inodes']:,} free inodes"
            available_inode_detail = f" and {group['free_inodes']:,} free inodes"
        raise RuntimeError(
            "insufficient storage for Live administration tool remaster "
            f"{phase} on the filesystem serving {', '.join(group['locations'])}: "
            f"requires at least {_format_bytes(group['required_bytes'])} free{required_inode_detail}; "
            f"available {_format_bytes(group['free_bytes'])}{available_inode_detail}. "
            "Remove abandoned remaster scratch directories, or place DEBIAN_USB_WORK_DIR "
            "and the output directory on filesystems with sufficient free blocks and inodes."
        )


def _squashfs_expanded_usage(path: Path, processors: int) -> tuple[int, int]:
    result = subprocess.run(
        ["unsquashfs", "-processors", str(processors), "-lln", str(path)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit status {result.returncode}"
        raise RuntimeError(f"could not inspect expanded squashfs usage for {path}: {detail}")

    apparent_bytes = 0
    entries = 0
    for line in result.stdout.splitlines():
        fields = line.split()
        if len(fields) < 6 or not fields[0] or fields[0][0] not in "-dlcbsp":
            continue
        timestamp_index = next(
            (
                index
                for index in range(2, len(fields) - 1)
                if re.fullmatch(r"\d{4}-\d{2}-\d{2}", fields[index])
                and re.fullmatch(r"\d{2}:\d{2}", fields[index + 1])
            ),
            None,
        )
        if timestamp_index is None:
            continue
        entries += 1
        if timestamp_index >= 3 and fields[2].isdigit():
            apparent_bytes += int(fields[2])
    if entries == 0:
        raise RuntimeError(f"unsquashfs did not report any filesystem entries for {path}")
    return max(apparent_bytes, path.stat().st_size), entries


def _preflight_live_tools_storage(
    *,
    source_file: Path,
    workspace_dir: Path,
    final_iso_path: Path,
    extracted_rootfs: Path | None = None,
    processors: int | None = None,
) -> None:
    source_size = source_file.stat().st_size
    output_bytes = source_size + max(OUTPUT_GROWTH_MIN_BYTES, source_size // 4)
    if extracted_rootfs is None:
        workspace_bytes = max(INITIAL_REMATERIALIZATION_MIN_BYTES, source_size * 2)
        workspace_inodes = INITIAL_REMATERIALIZATION_INODES
        phase = "before ISO extraction"
    else:
        expanded_bytes, expanded_inodes = _squashfs_expanded_usage(
            extracted_rootfs,
            processors or _squashfs_processor_count(),
        )
        workspace_bytes = expanded_bytes + max(EXPANDED_ROOTFS_HEADROOM_MIN_BYTES, expanded_bytes // 2)
        workspace_inodes = expanded_inodes + max(EXPANDED_ROOTFS_HEADROOM_MIN_INODES, expanded_inodes // 2)
        phase = "before squashfs extraction"
    _require_remaster_storage_capacity(
        [
            ("workspace", workspace_dir, workspace_bytes, workspace_inodes),
            ("output", final_iso_path.parent, output_bytes, 16),
        ],
        phase=phase,
    )


def _decode_mountinfo_path(value: str) -> str:
    return MOUNTINFO_ESCAPE_RE.sub(lambda match: chr(int(match.group(1), 8)), value)


def _mount_targets_under_workspace(workspace_dir: Path) -> list[Path] | None:
    try:
        mountinfo = Path("/proc/self/mountinfo").read_text(encoding="utf-8")
    except OSError:
        return None
    root = workspace_dir.resolve(strict=False)
    targets: list[Path] = []
    for line in mountinfo.splitlines():
        fields = line.split()
        if len(fields) < 5:
            continue
        target = Path(_decode_mountinfo_path(fields[4]))
        if target == root or root in target.parents:
            targets.append(target)
    return sorted(set(targets))


@contextmanager
def _temporary_owner_write_access(path: Path) -> Iterator[None]:
    metadata = path.lstat()
    if stat.S_ISLNK(metadata.st_mode):
        raise RuntimeError(f"refusing temporary write access through symlink: {path}")
    original_mode = stat.S_IMODE(metadata.st_mode)
    required_mode = stat.S_IWUSR
    if stat.S_ISDIR(metadata.st_mode):
        required_mode |= stat.S_IRUSR | stat.S_IXUSR
    changed = original_mode & required_mode != required_mode
    if changed:
        path.chmod(original_mode | required_mode)
    try:
        yield
    finally:
        if changed:
            path.chmod(original_mode)


def _ensure_extracted_iso_root_access(iso_root: Path) -> None:
    """Keep a rebuilt ISO root traversable by unprivileged readers.

    write_usb.sh intentionally uses umask 077.  xorriso preserves the mode of
    an already-existing extraction target, so the extracted ISO root would
    otherwise remain 0700 and APT's _apt worker could not traverse the local
    file: repository mounted from that tree.
    """

    metadata = iso_root.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise RuntimeError(f"extracted ISO root is not a real directory: {iso_root}")
    required_mode = (
        stat.S_IRUSR
        | stat.S_IWUSR
        | stat.S_IXUSR
        | stat.S_IRGRP
        | stat.S_IXGRP
        | stat.S_IROTH
        | stat.S_IXOTH
    )
    current_mode = stat.S_IMODE(metadata.st_mode)
    if current_mode & required_mode != required_mode:
        iso_root.chmod(current_mode | required_mode)


def _sudo_invoking_identity() -> tuple[int, int] | None:
    if os.geteuid() != 0:
        return None
    uid_text = str(os.environ.get("SUDO_UID") or "").strip()
    gid_text = str(os.environ.get("SUDO_GID") or "").strip()
    if not uid_text and not gid_text:
        return None
    if not uid_text.isdecimal() or not gid_text.isdecimal():
        raise RuntimeError("SUDO_UID and SUDO_GID must be numeric before rebuild output can be handed back")
    uid = int(uid_text)
    gid = int(gid_text)
    if uid <= 0:
        return None
    return uid, gid


def _missing_output_directories(output_root: Path) -> tuple[Path, ...]:
    missing: list[Path] = []
    current = output_root
    while True:
        if current.is_symlink():
            raise RuntimeError(f"rebuild output directory must not be a symbolic link: {current}")
        if current.exists():
            break
        missing.append(current)
        parent = current.parent
        if parent == current:
            raise RuntimeError(f"could not resolve an existing parent for rebuild output directory: {output_root}")
        current = parent
    return tuple(reversed(missing))


def _managed_rebuild_output_directories(output_parent: Path) -> tuple[Path, ...]:
    managed_root = DEFAULT_REBUILD_OUTPUT_DIR.resolve(strict=False)
    resolved_parent = output_parent.resolve(strict=False)
    try:
        relative_parent = resolved_parent.relative_to(managed_root)
    except ValueError:
        return ()
    directories = [managed_root]
    current = managed_root
    for part in relative_parent.parts:
        current /= part
        directories.append(current)
    return tuple(directories)


def _finalize_rebuild_output_access(
    final_iso_path: Path,
    created_output_directories: tuple[Path, ...] = (),
) -> None:
    identity = _sudo_invoking_identity()
    if identity is None:
        return
    metadata = final_iso_path.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode) or metadata.st_size <= 0:
        raise RuntimeError(f"rebuilt ISO is not a non-empty regular file: {final_iso_path}")

    managed_directories = _managed_rebuild_output_directories(final_iso_path.parent)
    handoff_directories = managed_directories or created_output_directories
    for directory in handoff_directories:
        directory_metadata = directory.lstat()
        if stat.S_ISLNK(directory_metadata.st_mode) or not stat.S_ISDIR(directory_metadata.st_mode):
            raise RuntimeError(f"rebuild output parent is not a real directory: {directory}")

    uid, gid = identity
    os.chown(final_iso_path, uid, gid, follow_symlinks=False)
    final_iso_path.chmod(0o600)
    if managed_directories:
        for directory in managed_directories:
            os.chown(directory, -1, gid, follow_symlinks=False)
            directory.chmod(0o2770)
    else:
        for directory in created_output_directories:
            os.chown(directory, uid, gid, follow_symlinks=False)
            directory.chmod(0o700)


def _make_workspace_tree_owner_removable(workspace_dir: Path) -> None:
    pending = [workspace_dir]
    while pending:
        current = pending.pop()
        metadata = current.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            continue
        current.chmod(
            stat.S_IMODE(metadata.st_mode)
            | stat.S_IRUSR
            | stat.S_IWUSR
            | stat.S_IXUSR
        )
        with os.scandir(current) as entries:
            for entry in entries:
                if entry.is_dir(follow_symlinks=False):
                    pending.append(Path(entry.path))


def _cleanup_live_initrd_workspace(workspace_dir: Path) -> str:
    if workspace_dir.is_symlink():
        return f"refusing to remove symlinked Live-initrd workspace: {workspace_dir}"
    if not workspace_dir.exists():
        return ""
    managed_root = (DEFAULT_WORK_DIR / "remaster-live-initrd").resolve(strict=False)
    resolved_workspace = workspace_dir.resolve(strict=False)
    if resolved_workspace == managed_root or managed_root not in resolved_workspace.parents:
        return f"refusing to remove Live-initrd workspace outside {managed_root}: {resolved_workspace}"
    mount_targets = _mount_targets_under_workspace(resolved_workspace)
    if mount_targets is None:
        return f"leaving Live-initrd workspace in place because mount state could not be inspected: {resolved_workspace}"
    if mount_targets:
        return (
            "leaving Live-initrd workspace in place because mounts remain below it: "
            + ", ".join(str(path) for path in mount_targets)
        )
    try:
        _make_workspace_tree_owner_removable(resolved_workspace)
        shutil.rmtree(resolved_workspace)
    except OSError as exc:
        return f"failed to remove Live-initrd workspace {resolved_workspace}: {exc}"
    return ""


def _cleanup_live_tools_workspace(workspace_dir: Path) -> str:
    if workspace_dir.is_symlink():
        return f"refusing to remove symlinked Live-tools workspace: {workspace_dir}"
    if not workspace_dir.exists():
        return ""
    managed_root = (DEFAULT_WORK_DIR / "remaster-live-tools").resolve(strict=False)
    resolved_workspace = workspace_dir.resolve(strict=False)
    if resolved_workspace == managed_root or managed_root not in resolved_workspace.parents:
        return f"refusing to remove Live-tools workspace outside {managed_root}: {resolved_workspace}"
    mount_targets = _mount_targets_under_workspace(resolved_workspace)
    if mount_targets is None:
        return f"leaving Live-tools workspace in place because mount state could not be inspected: {resolved_workspace}"
    if mount_targets:
        return (
            f"leaving Live-tools workspace in place because mounts remain below it: "
            f"{', '.join(str(path) for path in mount_targets)}"
        )
    try:
        _make_workspace_tree_owner_removable(resolved_workspace)
        shutil.rmtree(resolved_workspace)
    except OSError as exc:
        return f"failed to remove Live-tools workspace {resolved_workspace}: {exc}"
    return ""


def _ensure_live_initrd_overlay_deps() -> None:
    missing = [command for command in LIVE_INITRD_OVERLAY_REQUIRED_COMMANDS if shutil.which(command) is None]
    if not missing:
        return
    _run_simple(["apt-get", "update"])
    _run_simple(["apt-get", "install", "-y", "--no-install-recommends", *LIVE_INITRD_OVERLAY_APT_DEPS])
    still_missing = [command for command in LIVE_INITRD_OVERLAY_REQUIRED_COMMANDS if shutil.which(command) is None]
    if still_missing:
        raise RuntimeError(
            "Live initrd overlay preparation finished but commands are still missing: "
            + ", ".join(still_missing)
        )


def remaster_live_initrd_source(
    source_iso_path: str,
    profile: str,
    overlay_dir: str,
    output_dir: str = "",
) -> dict[str, Any]:
    if profile not in LIVE_INITRD_OVERLAY_PROFILES:
        raise ValueError(f"Live initrd overlays are not supported for profile: {profile}")
    source_path = _validate_absolute_path(
        source_iso_path,
        allow_missing=False,
        expect_directory=False,
        label="source_iso_path",
    )
    resolved_overlay = Path(
        _validate_absolute_path(
            overlay_dir,
            allow_missing=False,
            expect_directory=True,
            label="overlay_dir",
        )
    )
    source = open_source(source_path)
    entries = _find_boot_entries(source)
    media_class = _media_class(entries)
    if media_class not in {"live", "hybrid"}:
        raise RuntimeError(
            f"Live initrd overlay remaster requires live or hybrid media, got {media_class or '<unknown>'}"
        )
    live_initrd_paths = _collect_live_entry_member_paths(entries, "initrd_path")
    if not live_initrd_paths:
        raise RuntimeError("The selected Live source ISO does not expose a live initrd.")
    missing_members = [member_path for member_path in live_initrd_paths if not source.exists(member_path)]
    if missing_members:
        raise RuntimeError("Live initrd members are missing from the selected ISO: " + ", ".join(missing_members))

    run_id = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    requested_output_dir = output_dir or str(DEFAULT_REBUILD_OUTPUT_DIR / "live-initrd" / run_id)
    output_root = Path(
        _validate_absolute_path(
            requested_output_dir,
            allow_missing=True,
            expect_directory=True,
            label="output_dir",
        )
    )
    source_file = Path(source_path)
    final_iso_path = output_root / _validate_image_name(f"{source_file.stem}-initrd-overlay.iso")
    if final_iso_path.resolve() == source_file.resolve():
        raise ValueError("Live initrd overlay output must not replace the source ISO")

    _ensure_live_initrd_overlay_deps()
    workspace_dir = DEFAULT_WORK_DIR / "remaster-live-initrd" / run_id
    state_dir = DEFAULT_STATE_DIR / "remaster-live-initrd" / run_id
    log_dir = DEFAULT_LOG_DIR / "remaster-live-initrd"
    log_path = log_dir / f"{run_id}.log"
    output_temp_path = output_root / f".{final_iso_path.name}.{run_id}.part"
    created_output_directories = _missing_output_directories(output_root)
    output_complete = False
    cleanup_warning = ""
    modified_paths: list[str] = []
    archive_manifests: list[dict[str, object]] = []
    try:
        for managed_path in (workspace_dir, state_dir, log_dir, output_root):
            managed_path.mkdir(parents=True, exist_ok=True)
        iso_root = workspace_dir / "iso-root"
        iso_root.mkdir(parents=True, exist_ok=True)
        with log_path.open("w", encoding="utf-8") as log_file:
            _run_logged(
                ["xorriso", "-osirrox", "on", "-indev", source_path, "-extract", "/", str(iso_root)],
                cwd=workspace_dir,
                log_file=log_file,
            )
            _ensure_extracted_iso_root_access(iso_root)
            for index, member_path in enumerate(live_initrd_paths, start=1):
                archive_path = iso_root / member_path.lstrip("/")
                if archive_path.is_symlink() or not archive_path.is_file() or archive_path.stat().st_size <= 0:
                    raise RuntimeError(f"extracted Live initrd is not a non-empty regular file: {member_path}")
                archive_manifest = _merge_initrd_overlay_archive(
                    archive_path=archive_path,
                    overlay_dir=resolved_overlay,
                    workspace_dir=workspace_dir / "initrd-trees" / f"{index:03d}",
                )
                archive_manifests.append({"member_path": member_path, **archive_manifest})
                modified_paths.append(member_path)
                _log(log_file, f"Merged initrd overlay into Live member: {member_path}")
            _rewrite_checksum_files(iso_root)
            if output_temp_path.is_symlink() or output_temp_path.is_file():
                output_temp_path.unlink()
            elif output_temp_path.exists():
                raise RuntimeError(f"temporary remaster output path is not a file: {output_temp_path}")
            _run_logged(
                [
                    "xorriso",
                    "-indev",
                    source_path,
                    "-outdev",
                    str(output_temp_path),
                    "-boot_image",
                    "any",
                    "replay",
                    "-map",
                    str(iso_root),
                    "/",
                    "-commit",
                    "-end",
                ],
                cwd=workspace_dir,
                log_file=log_file,
            )

        if output_temp_path.is_symlink() or not output_temp_path.is_file() or output_temp_path.stat().st_size == 0:
            raise RuntimeError(
                "Live initrd overlay remaster did not produce a non-empty regular ISO: "
                f"{output_temp_path}"
            )
        _finalize_rebuild_output_access(output_temp_path, created_output_directories)
        output_temp_path.replace(final_iso_path)
        output_complete = True
    finally:
        if not output_complete and (output_temp_path.is_symlink() or output_temp_path.is_file()):
            output_temp_path.unlink(missing_ok=True)
        cleanup_warning = _cleanup_live_initrd_workspace(workspace_dir)
        if cleanup_warning:
            print(f"WARNING: {cleanup_warning}", file=sys.stderr, flush=True)

    warnings = [cleanup_warning] if cleanup_warning else []
    manifest = {
        "schema_version": 1,
        "run_id": run_id,
        "profile": profile,
        "source_iso_path": source_path,
        "iso_path": str(final_iso_path),
        "overlay": {"overlay_dir": str(resolved_overlay), "embedded_root": "/"},
        "modified_paths": sorted(modified_paths),
        "archive_manifests": archive_manifests,
        "workspace_dir": str(workspace_dir),
        "workspace_removed": not workspace_dir.exists(),
        "log_path": str(log_path),
        "warnings": warnings,
    }
    manifest_path = state_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return {**manifest, "manifest_path": str(manifest_path)}


def _merge_initrd_overlay_archive(
    *,
    archive_path: Path,
    overlay_dir: Path,
    workspace_dir: Path,
) -> dict[str, object]:
    if archive_path.is_symlink() or not archive_path.is_file() or archive_path.stat().st_size <= 0:
        raise ValueError(f"initrd archive is not a non-empty regular file: {archive_path}")
    if workspace_dir.is_symlink():
        raise ValueError(f"initrd overlay workspace must not be a symbolic link: {workspace_dir}")
    workspace_dir.mkdir(parents=True, exist_ok=True)
    _extract_initrd_archive(archive_path, workspace_dir)
    manifest = merge_initrd_overlay(overlay_dir, workspace_dir)
    _repack_initrd_archive(workspace_dir, archive_path)
    return manifest


def remaster_live_tools_source(
    source_iso_path: str,
    profile: str,
    output_dir: str = "",
    selected_groups: list[str] | None = None,
    live_kernel_args: str = "",
    *,
    overlay_dir: str = "",
    ensure_encrypted_persistence: bool = False,
) -> dict[str, Any]:
    """Materialize all selected live customizations in one rootfs/ISO pass.

    The caller collects choices first. This function performs no USB writes.
    The source ISO is immutable; output is published atomically after success.
    """
    if profile == PROFILE_TAILS:
        raise ValueError("Tails remastering is not supported: keep its stock ISO, initrd and security configuration unchanged")
    if overlay_dir:
        overlay_dir = _validate_absolute_path(
            overlay_dir, allow_missing=False, expect_directory=True, label="overlay_dir"
        )
    if ensure_encrypted_persistence and profile not in {PROFILE_DEBIAN, PROFILE_KALI_LINUX}:
        raise ValueError(f"Encrypted persistence is not supported for profile {profile}")
    if profile == PROFILE_UBUNTU_SERVER and selected_groups == []:
        selected_packages = []
        live_tool_profile = {"path": "", "sha256": "", "name": "live-customization", "selected_groups": []}
    else:
        selected_packages, live_tool_profile = live_tool_packages_for_profile(
            profile, selected_groups=selected_groups,
        )
    packages = list(selected_packages)
    optional_packages = list(live_tool_profile.get("optional_packages", [])) + live_optional_firmware(profile)
    if profile == PROFILE_KALI_LINUX:
        optional_packages = _dedupe([*selected_packages, *optional_packages])
    wifi_source = ""
    if profile in {PROFILE_DEBIAN, PROFILE_KALI_LINUX}:
        source_env = Path(overlay_dir) / "live.env" if overlay_dir else None
        wifi_source = str(debian_live_env_path(source_env if source_env and source_env.is_file() else "", profile))
        load_debian_live_wifi_config(wifi_source, profile)  # Fail before package work.
        packages = _dedupe([*live_hook_packages(profile), *packages, *optional_packages])
    if ensure_encrypted_persistence:
        packages = _dedupe([*packages, *LIVE_PERSISTENCE_SUPPORT_PACKAGES, "initramfs-tools"])
    if overlay_dir:
        packages = _dedupe([*packages, "initramfs-tools"])
    if not packages and not overlay_dir:
        raise ValueError("Live administration tool remaster requires at least one selected package group")
    # All package triggers were deferred; each modified Live ISO needs a final initrd.
    packages = _dedupe([*packages, "initramfs-tools"])
    normalized_live_kernel_args = _validate_live_kernel_args(live_kernel_args)
    if profile not in {PROFILE_DEBIAN, PROFILE_KALI_LINUX} and normalized_live_kernel_args:
        raise ValueError("Live hook kernel arguments are supported only for Debian/Kali Live remasters")
    if profile in {PROFILE_DEBIAN, PROFILE_KALI_LINUX}:
        normalized_live_kernel_args = _merge_live_kernel_line(
            normalized_live_kernel_args,
            " ".join(DEBIAN_LIVE_HOOK_KERNEL_ARGS),
        )
    run_id = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    requested_output_dir = output_dir or str(DEFAULT_REBUILD_OUTPUT_DIR / "live-tools" / run_id)
    source_path = _validate_absolute_path(
        source_iso_path,
        allow_missing=False,
        expect_directory=False,
        label="source_iso_path",
    )
    output_root = Path(
        _validate_absolute_path(
            requested_output_dir,
            allow_missing=True,
            expect_directory=True,
            label="output_dir",
        )
    )
    source = open_source(source_path)
    entries = _find_boot_entries(source)
    media_class = _media_class(entries)
    if media_class not in {"live", "hybrid"}:
        raise RuntimeError(
            f"Live administration tool remaster requires live or hybrid media, got {media_class or '<unknown>'}"
        )
    live_entry = _select_entry(profile, entries)
    live_rootfs_path = _find_existing_member(source, LIVE_ROOTFS_CANDIDATES)
    if not live_entry or not live_rootfs_path:
        raise RuntimeError("The selected source ISO does not expose a supported live entry and root filesystem.")
    if profile == PROFILE_DEBIAN and (not live_entry.kernel_path or not live_entry.initrd_path):
        raise RuntimeError("Debian Live tool remastering requires live kernel and initrd paths.")
    live_initrd_paths = _collect_live_entry_member_paths(entries, "initrd_path")
    if not live_rootfs_path.endswith(".squashfs"):
        raise RuntimeError(f"Live administration tool remaster requires squashfs media, got {live_rootfs_path}")
    architecture = _infer_architecture(entries, live_entry.kernel_path, live_entry.initrd_path)
    if architecture and architecture != _host_architecture():
        raise RuntimeError(
            "Live administration tool remaster requires a matching host and ISO architecture. "
            f"Source ISO: {architecture}, host: {_host_architecture()}"
        )

    processors = _squashfs_processor_count()
    workspace_dir = DEFAULT_WORK_DIR / "remaster-live-tools" / run_id
    state_dir = DEFAULT_STATE_DIR / "remaster-live-tools" / run_id
    log_dir = DEFAULT_LOG_DIR / "remaster-live-tools"
    source_file = Path(source_path)
    final_iso_path = output_root / _validate_image_name(f"{source_file.stem}-admin-tools.iso")
    if final_iso_path.resolve() == source_file.resolve():
        raise ValueError("Live administration tool remaster output must not replace the source ISO")
    _preflight_live_tools_storage(
        source_file=source_file,
        workspace_dir=workspace_dir,
        final_iso_path=final_iso_path,
    )
    ensure_debian_rebuild_deps()
    log_path = log_dir / f"{run_id}.log"
    output_temp_path = output_root / f".{final_iso_path.name}.{run_id}.part"
    created_output_directories = _missing_output_directories(output_root)

    output_complete = False
    cleanup_warning = ""
    try:
        for path in (workspace_dir, state_dir, log_dir, output_root):
            path.mkdir(parents=True, exist_ok=True)
        iso_root = workspace_dir / "iso-root"
        iso_root.mkdir(parents=True, exist_ok=True)
        with log_path.open("w", encoding="utf-8") as log_file:
            _run_logged(
                ["xorriso", "-osirrox", "on", "-indev", source_path, "-extract", "/", str(iso_root)],
                cwd=workspace_dir,
                log_file=log_file,
            )
            _ensure_extracted_iso_root_access(iso_root)
            extracted_rootfs = iso_root / live_rootfs_path.lstrip("/")
            _preflight_live_tools_storage(
                source_file=source_file,
                workspace_dir=workspace_dir,
                final_iso_path=final_iso_path,
                extracted_rootfs=extracted_rootfs,
                processors=processors,
            )
            modified_paths = _apply_live_tools_remaster(
                live_rootfs_path=live_rootfs_path,
                live_initrd_path=live_entry.initrd_path,
                live_initrd_paths=live_initrd_paths,
                live_kernel_path=live_entry.kernel_path,
                packages=packages,
                profile=profile,
                iso_root=iso_root,
                workspace_dir=workspace_dir,
                log_file=log_file,
                processors=processors,
                overlay_dir=overlay_dir,
                ensure_encrypted_persistence=ensure_encrypted_persistence,
                live_entries=entries,
                live_env_source=wifi_source,
                optional_packages=optional_packages,
            )
            if normalized_live_kernel_args:
                modified_paths = _dedupe(
                    [
                        *modified_paths,
                        *_patch_live_boot_configs(iso_root, normalized_live_kernel_args),
                    ]
                )
            _rewrite_checksum_files(iso_root)
            if output_temp_path.is_symlink() or output_temp_path.is_file():
                output_temp_path.unlink()
            elif output_temp_path.exists():
                raise RuntimeError(f"temporary remaster output path is not a file: {output_temp_path}")
            _run_logged(
                [
                    "xorriso",
                    "-indev",
                    source_path,
                    "-outdev",
                    str(output_temp_path),
                    "-boot_image",
                    "any",
                    "replay",
                    "-map",
                    str(iso_root),
                    "/",
                    "-commit",
                    "-end",
                ],
                cwd=workspace_dir,
                log_file=log_file,
            )

        if output_temp_path.is_symlink() or not output_temp_path.is_file() or output_temp_path.stat().st_size == 0:
            raise RuntimeError(
                "Live administration tool remaster did not produce a non-empty regular ISO: "
                f"{output_temp_path}"
            )
        _finalize_rebuild_output_access(output_temp_path, created_output_directories)
        output_temp_path.replace(final_iso_path)
        output_complete = True
    finally:
        if not output_complete and (output_temp_path.is_symlink() or output_temp_path.is_file()):
            output_temp_path.unlink(missing_ok=True)
        cleanup_warning = _cleanup_live_tools_workspace(workspace_dir)
        if cleanup_warning:
            try:
                with log_path.open("a", encoding="utf-8") as cleanup_log:
                    cleanup_log.write(f"WARNING: {cleanup_warning}\n")
            except OSError:
                pass
            print(f"WARNING: {cleanup_warning}", file=sys.stderr, flush=True)

    manifest = {
        "run_id": run_id,
        "profile": profile,
        "source_iso_path": source_path,
        "iso_path": str(final_iso_path),
        "workspace_dir": str(workspace_dir),
        "workspace_removed": not workspace_dir.exists(),
        "log_path": str(log_path),
        "warnings": [cleanup_warning] if cleanup_warning else [],
        "modified_paths": modified_paths,
        "packages": packages,
        "required_live_packages": live_hook_packages(profile),
        "optional_live_packages": optional_packages,
        "packages_semantics": "requested packages; unavailable optional names are recorded inside the Live root at /var/log/debian-usb/optional-packages.json",
        "selected_groups": list(live_tool_profile["selected_groups"]),
        "initrd_overlay_dir": overlay_dir,
        "ensure_encrypted_persistence": ensure_encrypted_persistence,
        "preparation_passes": 1,
        "live_kernel_arg_keys": [token.split("=", 1)[0] for token in normalized_live_kernel_args.split()],
        "package_profile": {
            "path": live_tool_profile["path"],
            "sha256": live_tool_profile["sha256"],
            "name": live_tool_profile["name"],
            "selected_groups": list(live_tool_profile["selected_groups"]),
            "package_count": len(packages),
        },
    }
    manifest_path = state_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    manifest["manifest_path"] = str(manifest_path)
    return manifest


def _validate_live_kernel_args(value: str) -> str:
    normalized = " ".join(str(value or "").split())
    for token in normalized.split():
        if not LIVE_KERNEL_ARG_TOKEN_RE.fullmatch(token):
            raise ValueError(f"invalid Live kernel argument token: {token}")
        if token.split("=", 1)[0] in LIVE_WIFI_SECRET_KERNEL_ARG_NAMES:
            raise ValueError(
                "Live Wi-Fi kernel arguments are forbidden; "
                "use LIVE_WIFI_* assignments in initrd/debian/live/live.env"
            )
    return normalized


def _merge_live_kernel_line(payload: str, additions: str) -> str:
    tokens = payload.split()
    separator_index = len(tokens)
    for index, token in enumerate(tokens):
        if token == "---":
            separator_index = index
            break
    merged = list(tokens[:separator_index])
    suffix = list(tokens[separator_index:])
    for addition in additions.split():
        key = addition.split("=", 1)[0]
        merged = [
            token
            for token in merged
            if token != key and not token.startswith(key + "=")
        ]
        merged.append(addition)
    return " ".join([*merged, *suffix])


def _patch_live_boot_configs(iso_root: Path, live_kernel_args: str) -> list[str]:
    modified_paths: list[str] = []
    for config_path in sorted(iso_root.rglob("*.cfg")):
        if not config_path.is_file() or config_path.is_symlink():
            continue
        relative_path = config_path.relative_to(iso_root)
        if not relative_path.parts or relative_path.parts[0].lower() not in LIVE_BOOT_CONFIG_ROOTS:
            continue
        if config_path.stat().st_size > 2 * 1024 * 1024:
            raise ValueError(f"refusing oversized Live boot configuration: /{relative_path.as_posix()}")
        original = config_path.read_text(encoding="utf-8")
        updated_lines: list[str] = []
        changed = False
        for raw_line in original.splitlines(keepends=True):
            line = raw_line.rstrip("\r\n")
            newline = raw_line[len(line) :]
            match = re.match(r"^(?P<prefix>\s*(?:linux|linuxefi|linux16|append)\s+)(?P<payload>.*)$", line)
            if not match:
                updated_lines.append(raw_line)
                continue
            payload = match.group("payload")
            tokens = payload.split()
            live_line = "boot=live" in tokens or any(
                token.startswith(("/live/", "($root)/live/")) and "vmlinuz" in token
                for token in tokens
            )
            if not live_line:
                updated_lines.append(raw_line)
                continue
            merged_payload = _merge_live_kernel_line(payload, live_kernel_args)
            updated_line = match.group("prefix") + merged_payload + newline
            updated_lines.append(updated_line)
            changed = changed or updated_line != raw_line
        if not changed:
            continue
        config_path.write_text("".join(updated_lines), encoding="utf-8")
        modified_paths.append("/" + relative_path.as_posix())
    return modified_paths


def _stage_debian_live_iso_policy(iso_root: Path, profile: str = PROFILE_DEBIAN, live_env_source: str = "") -> list[str]:
    staged_hooks = stage_debian_live_config_hooks(iso_root / "live", profile)
    staged_wifi = stage_debian_live_medium_wifi_config(iso_root / "live", live_env_source, profile)
    staged_paths = [
        "/" + path.relative_to(iso_root).as_posix()
        for path in [*staged_hooks, staged_wifi]
    ]
    boot_paths = _patch_live_boot_configs(
        iso_root,
        " ".join(DEBIAN_LIVE_HOOK_KERNEL_ARGS),
    )
    return _dedupe([*staged_paths, *boot_paths])


def _stage_debian_live_root_policy(live_root: Path, profile: str = PROFILE_DEBIAN, live_env_source: str = "") -> None:
    stage_live_kernel_module_policy(live_root, DEBIAN_LIVE_INITRAMFS_MODULES)
    stage_live_wifi_runtime(live_root, live_env_source, profile)
    if profile == PROFILE_DEBIAN:
        stage_debian_live_apt_policy(live_root)


def rebuild_debian_installer_iso(plan_path: str) -> dict[str, Any]:
    ensure_debian_rebuild_deps()
    plan = _validate_rebuild_plan(_load_json_file(Path(plan_path)))
    inspection = inspect_debian_rebuild_source(plan["source_iso_path"])
    run_id = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    workspace_dir = DEFAULT_WORK_DIR / "rebuild-installer-iso" / run_id
    state_dir = DEFAULT_STATE_DIR / "rebuild-installer-iso" / run_id
    log_dir = DEFAULT_LOG_DIR / "rebuild-installer-iso"
    output_dir = Path(plan["output_dir"])
    final_iso_path = output_dir / plan["image_name"]
    created_output_directories = _missing_output_directories(output_dir)
    for path in (workspace_dir, state_dir, log_dir, output_dir):
        path.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{run_id}.log"
    iso_root = workspace_dir / "iso-root"
    iso_root.mkdir(parents=True, exist_ok=True)

    with log_path.open("w", encoding="utf-8") as log_file:
        _run_logged(
            ["xorriso", "-osirrox", "on", "-indev", plan["source_iso_path"], "-extract", "/", str(iso_root)],
            cwd=workspace_dir,
            log_file=log_file,
        )
        _ensure_extracted_iso_root_access(iso_root)
        modified_paths: list[str]
        staged_udeb_repo_path = ""
        warnings = list(inspection.get("warnings", []))
        if plan["scope"] == "d-i":
            action_result = _apply_installer_rebuild_action(plan, inspection, iso_root, workspace_dir, state_dir, log_file)
        else:
            action_result = _apply_live_host_rebuild_action(plan, inspection, iso_root, workspace_dir, state_dir, log_file)
        modified_paths = action_result["modified_paths"]
        live_host_packages = list(action_result.get("packages", []))
        if action_result.get("staged_udeb_repo_path"):
            staged_udeb_repo_path = action_result["staged_udeb_repo_path"]
        warnings.extend(action_result.get("warnings", []))
        _rewrite_checksum_files(iso_root)
        if final_iso_path.exists():
            final_iso_path.unlink()
        _run_logged(
            [
                "xorriso",
                "-indev",
                plan["source_iso_path"],
                "-outdev",
                str(final_iso_path),
                "-boot_image",
                "any",
                "replay",
                "-map",
                str(iso_root),
                "/",
                "-commit",
                "-end",
            ],
            cwd=workspace_dir,
            log_file=log_file,
        )

    _finalize_rebuild_output_access(final_iso_path, created_output_directories)
    manifest = {
        "schema_version": REBUILD_SCHEMA_VERSION,
        "run_id": run_id,
        "built_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "plan": plan,
        "inspection": inspection,
        "iso_path": str(final_iso_path),
        "workspace_dir": str(workspace_dir),
        "log_path": str(log_path),
        "staged_udeb_repo_path": staged_udeb_repo_path,
        "modified_paths": modified_paths,
        "warnings": warnings,
    }
    if plan["scope"] == "live-host":
        manifest["packages"] = live_host_packages
    manifest_path = state_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    result = {
        "run_id": run_id,
        "iso_path": str(final_iso_path),
        "workspace_dir": str(workspace_dir),
        "log_path": str(log_path),
        "manifest_path": str(manifest_path),
        "staged_udeb_repo_path": staged_udeb_repo_path,
        "modified_paths": modified_paths,
        "warnings": warnings,
    }
    if plan["scope"] == "live-host":
        result["packages"] = live_host_packages
    return result


def _apply_installer_rebuild_action(
    plan: dict[str, Any],
    inspection: dict[str, Any],
    iso_root: Path,
    workspace_dir: Path,
    state_dir: Path,
    log_file: Any,
) -> dict[str, Any]:
    installer_initrd_path = str(inspection.get("installer_initrd_path") or "").strip()
    installer_kernel_path = str(inspection.get("installer_kernel_path") or "").strip()
    installer_kernel_version = str(inspection.get("installer_kernel_version") or "").strip()
    architecture = str(plan["architecture"] or inspection.get("architecture") or "").strip()
    if not installer_initrd_path or not installer_kernel_version:
        raise RuntimeError("The selected source ISO does not expose a Debian installer initrd with a detectable kernel version.")
    if not architecture:
        raise RuntimeError("Could not infer the Debian installer architecture from the source ISO.")

    extracted_initrd = iso_root / installer_initrd_path.lstrip("/")
    initrd_tree = workspace_dir / "installer-initrd"
    initrd_tree.mkdir(parents=True, exist_ok=True)
    _extract_initrd_archive(extracted_initrd, initrd_tree)
    staged_udeb_repo_path = ""
    warnings: list[str] = []
    modified_paths = [installer_initrd_path]

    if plan["action"] == "add-kernel-modules":
        udeb_manifest = _build_installer_kernel_udebs(
            architecture=architecture,
            kernel_version=installer_kernel_version,
            module_names=plan["installer_kernel_modules"],
            kernel_config_entries=None,
            state_dir=state_dir / "installer-kernel-udebs",
            workspace_dir=workspace_dir / "installer-kernel-udebs",
            log_file=log_file,
        )
        _extract_udeb_payloads(Path(udeb_manifest["staged_udeb_dir"]), initrd_tree)
        _run_logged(["depmod", "-b", str(initrd_tree), installer_kernel_version], cwd=workspace_dir, log_file=log_file)
        staged_udeb_repo_path = _stage_installer_udeb_workspace(iso_root, udeb_manifest, state_dir / "iso-installer-udebs")
        modified_paths.append("/.debian-usb/installer/localudebs")
    elif plan["action"] == "add-udeb-packages":
        udeb_manifest = _build_source_package_udebs(
            package_names=plan["installer_udeb_packages"],
            state_dir=state_dir / "installer-package-udebs",
            workspace_dir=workspace_dir / "installer-package-udebs",
            log_file=log_file,
        )
        _extract_udeb_payloads(Path(udeb_manifest["staged_udeb_dir"]), initrd_tree)
        _run_logged(["depmod", "-b", str(initrd_tree), installer_kernel_version], cwd=workspace_dir, log_file=log_file)
        staged_udeb_repo_path = _stage_installer_udeb_workspace(iso_root, udeb_manifest, state_dir / "iso-installer-udebs")
        modified_paths.append("/.debian-usb/installer/localudebs")
    elif plan["action"] == "update-kernel":
        target_kernel_version = plan["target_kernel_version"]
        cache_root = DEFAULT_CACHE_DIR / "kernel-support" / target_kernel_version
        notes: list[str] = []
        downloaded_packages = _populate_kernel_support_cache(target_kernel_version, cache_root, log_file=log_file, notes=notes)
        warnings.extend(notes)
        if not downloaded_packages:
            raise RuntimeError(f"No Debian kernel package candidates could be downloaded for {target_kernel_version}")

        old_module_names = _scan_installer_module_names(initrd_tree, installer_kernel_version)
        if not old_module_names:
            warnings.append("No existing installer modules were detected in the source initrd. Rebuilding only the requested kernel image path.")
        udeb_manifest = _build_installer_kernel_udebs(
            architecture=architecture,
            kernel_version=target_kernel_version,
            module_names=old_module_names,
            kernel_config_entries=None,
            state_dir=state_dir / "installer-kernel-update",
            workspace_dir=workspace_dir / "installer-kernel-update",
            log_file=log_file,
        )
        _replace_installer_kernel_tree(initrd_tree, installer_kernel_version, target_kernel_version, cache_root)
        _extract_udeb_payloads(Path(udeb_manifest["staged_udeb_dir"]), initrd_tree)
        _run_logged(["depmod", "-b", str(initrd_tree), target_kernel_version], cwd=workspace_dir, log_file=log_file)

        if not installer_kernel_path:
            raise RuntimeError("The selected source ISO does not expose a Debian installer kernel path.")
        extracted_kernel = iso_root / installer_kernel_path.lstrip("/")
        extracted_kernel.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(_resolve_cached_kernel_image(cache_root, target_kernel_version), extracted_kernel)
        staged_udeb_repo_path = _stage_installer_udeb_workspace(iso_root, udeb_manifest, state_dir / "iso-installer-udebs")
        modified_paths.extend([installer_kernel_path, "/.debian-usb/installer/localudebs"])
    else:
        raise RuntimeError(f"unsupported installer rebuild action: {plan['action']}")

    _repack_initrd_archive(initrd_tree, extracted_initrd)
    return {
        "modified_paths": _dedupe(modified_paths),
        "staged_udeb_repo_path": staged_udeb_repo_path,
        "warnings": warnings,
    }


def _apply_live_host_rebuild_action(
    plan: dict[str, Any],
    inspection: dict[str, Any],
    iso_root: Path,
    workspace_dir: Path,
    state_dir: Path,
    log_file: Any,
) -> dict[str, Any]:
    del state_dir
    live_rootfs_path = str(inspection.get("live_rootfs_path") or "").strip()
    live_kernel_path = str(inspection.get("live_kernel_path") or "").strip()
    live_initrd_path = str(inspection.get("live_initrd_path") or "").strip()
    live_kernel_paths = _coerce_member_path_list(
        inspection.get("live_kernel_paths"),
        fallback=[live_kernel_path] if live_kernel_path else [],
    )
    live_initrd_paths = _coerce_member_path_list(
        inspection.get("live_initrd_paths"),
        fallback=[live_initrd_path] if live_initrd_path else [],
    )
    architecture = str(plan["architecture"] or inspection.get("architecture") or "").strip()
    if not live_rootfs_path:
        raise RuntimeError("The selected source ISO does not expose a Debian live root filesystem.")
    if not live_kernel_path or not live_initrd_path:
        raise RuntimeError("The selected source ISO does not expose Debian Live kernel and initrd paths.")
    if not live_rootfs_path.endswith(".squashfs"):
        raise RuntimeError(f"Live Host actions currently require squashfs media, got {live_rootfs_path}")
    if architecture and architecture != _host_architecture():
        raise RuntimeError(
            f"Live Host actions currently require a matching host and ISO architecture. Source ISO: {architecture}, host: {_host_architecture()}"
        )

    processors = _squashfs_processor_count()
    extracted_rootfs = iso_root / live_rootfs_path.lstrip("/")
    live_root = workspace_dir / "live-root"
    _run_logged(
        ["unsquashfs", "-processors", str(processors), "-d", str(live_root), str(extracted_rootfs)],
        cwd=workspace_dir,
        log_file=log_file,
    )
    _stage_debian_live_root_policy(live_root)
    warnings: list[str] = []
    modified_paths = [live_rootfs_path]

    if plan["action"] == "add-deb-packages":
        installed_packages = _dedupe([*DEBIAN_LIVE_HOOK_PACKAGES, *plan["live_deb_packages"]])
        target_kernel_version = ""
    elif plan["action"] == "replace-kernel":
        target_kernel_version = plan["target_kernel_version"]
        installed_packages = _dedupe(
            [
                *DEBIAN_LIVE_HOOK_PACKAGES,
                f"linux-image-{target_kernel_version}",
                f"linux-headers-{target_kernel_version}",
            ]
        )
    else:
        raise RuntimeError(f"unsupported live host rebuild action: {plan['action']}")

    _install_packages_in_chroot(
        live_root,
        installed_packages,
        log_file,
        apt_source_root=iso_root,
    )
    with _mounted_chroot(live_root, log_file):
        _run_in_chroot(
            live_root,
            _chroot_noninteractive_command("update-initramfs", "-u", "-k", "all"),
            log_file,
        )

    if target_kernel_version:
        kernel_version = target_kernel_version
        _prune_live_kernel_versions(live_root, kernel_version)
        rebuilt_kernel = _resolve_live_root_kernel_file(live_root, kernel_version)
        modified_paths.extend(_copy_file_to_member_paths(rebuilt_kernel, iso_root, live_kernel_paths))
    else:
        kernel_version = _detect_live_root_kernel_version(live_root, live_kernel_path)
    rebuilt_initrd = _resolve_live_root_initrd_file(live_root, kernel_version)
    modified_paths.extend(_copy_file_to_member_paths(rebuilt_initrd, iso_root, live_initrd_paths))
    modified_paths.extend(_stage_debian_live_iso_policy(iso_root))

    stage_live_systemd_masks(live_root)
    _refresh_live_metadata(live_root, iso_root, live_rootfs_path)
    compression = _squashfs_compression(extracted_rootfs, processors=processors)
    extracted_rootfs.unlink()
    _run_logged(
        [
            "mksquashfs",
            str(live_root),
            str(extracted_rootfs),
            "-noappend",
            "-comp",
            compression,
            "-processors",
            str(processors),
        ],
        cwd=workspace_dir,
        log_file=log_file,
    )
    return {
        "modified_paths": _dedupe(modified_paths),
        "packages": installed_packages,
        "staged_udeb_repo_path": "",
        "warnings": warnings,
    }


def _apply_live_tools_remaster(
    *,
    live_rootfs_path: str,
    live_initrd_path: str,
    live_initrd_paths: list[str],
    live_kernel_path: str,
    packages: list[str],
    profile: str,
    iso_root: Path,
    workspace_dir: Path,
    log_file: Any,
    processors: int | None = None,
    overlay_dir: str = "",
    ensure_encrypted_persistence: bool = False,
    live_entries: list[Any] | None = None,
    live_env_source: str = "",
    optional_packages: list[str] | None = None,
) -> list[str]:
    processor_count = processors or _squashfs_processor_count()
    extracted_rootfs = iso_root / live_rootfs_path.lstrip("/")
    live_root = workspace_dir / "live-tools-root"
    _run_logged(
        ["unsquashfs", "-processors", str(processor_count), "-d", str(live_root), str(extracted_rootfs)],
        cwd=workspace_dir,
        log_file=log_file,
    )
    if profile == PROFILE_DEBIAN:
        stage_debian_live_locale(live_root)
    if profile in {PROFILE_DEBIAN, PROFILE_KALI_LINUX}:
        _stage_debian_live_root_policy(live_root, profile, live_env_source)
    # Package maintainer scripts and triggers must not repeatedly generate an
    # initrd before the complete module/crypto/overlay policy is in place.
    with _deferred_initramfs_updates(live_root, log_file):
        _install_packages_in_chroot(live_root, packages, log_file, apt_source_root=iso_root, optional_packages=optional_packages)
    stage_live_systemd_masks(live_root)
    if ensure_encrypted_persistence:
        _stage_encrypted_persistence_policy(live_root)
    if overlay_dir:
        _stage_live_initrd_overlay(live_root, Path(overlay_dir))

    modified_initrd_paths: list[str] = []
    if profile == PROFILE_DEBIAN:
        stage_debian_live_locale(live_root)
        _configure_debian_live_locale(live_root, log_file)
    if packages or overlay_dir or ensure_encrypted_persistence:
        # Resolve each kernel/initrd pairing, never copy the first ABI's initrd
        # over a different kernel. Aliased entries share one generated image.
        members = _coerce_member_path_list(live_initrd_paths, fallback=[live_initrd_path])
        kernel_for_initrd: dict[str, str] = {}
        for entry in live_entries or []:
            if not entry.kind.startswith("live") or not entry.initrd_path:
                continue
            previous = kernel_for_initrd.get(entry.initrd_path)
            if previous and previous != entry.kernel_path:
                raise RuntimeError(f"Conflicting kernels for live initrd {entry.initrd_path}")
            kernel_for_initrd[entry.initrd_path] = entry.kernel_path
        for member in members:
            kernel_for_initrd.setdefault(member, live_kernel_path)
        generated: dict[str, Path] = {}
        versions = {
            member: _detect_live_root_kernel_version(live_root, kernel)
            for member, kernel in kernel_for_initrd.items()
        }
        with _mounted_chroot(live_root, log_file):
            for version in dict.fromkeys(versions.values()):
                target = f"/boot/initrd.img-{version}"
                _run_in_chroot(live_root, _chroot_noninteractive_command("depmod", "-a", version), log_file)
                # mkinitramfs does not depend on an existing /boot initrd or on
                # upstream update-initramfs.conf. One generation per ABI.
                _run_in_chroot(live_root, _chroot_noninteractive_command(
                    "mkinitramfs", "-o", target, version
                ), log_file)
                generated[version] = _resolve_live_root_initrd_file(live_root, version)
        for member, version in versions.items():
            modified_initrd_paths.extend(_copy_file_to_member_paths(generated[version], iso_root, [member]))

    hook_paths: list[str] = []
    if profile in {PROFILE_DEBIAN, PROFILE_KALI_LINUX}:
        hook_paths = _stage_debian_live_iso_policy(iso_root, profile, live_env_source)
    _refresh_live_metadata(live_root, iso_root, live_rootfs_path)
    compression = _squashfs_compression(extracted_rootfs, processors=processor_count)
    extracted_rootfs.unlink()
    _run_logged(
        [
            "mksquashfs",
            str(live_root),
            str(extracted_rootfs),
            "-noappend",
            "-comp",
            compression,
            "-processors",
            str(processor_count),
        ],
        cwd=workspace_dir,
        log_file=log_file,
    )
    return _dedupe([live_rootfs_path, *modified_initrd_paths, *hook_paths])


def _build_installer_kernel_udebs(
    *,
    architecture: str,
    kernel_version: str,
    module_names: list[str],
    kernel_config_entries: list[str] | None,
    state_dir: Path,
    workspace_dir: Path,
    log_file: Any,
) -> dict[str, Any]:
    source_package, source_version = _resolve_installer_kernel_source_version(f"kernel-image-{kernel_version}-di")
    manifest = _rebuild_udebs_from_source(
        rebuilds=[
            {
                "rebuild_kind": "linux-installer-kernel",
                "source_package": source_package,
                "source_version": source_version,
                "source_overlay_dir": "",
                "build_dep_packages": [],
                "module_targets": [
                    {
                        "path": "debian/installer/modules/kernel-image",
                        "modules": list(module_names),
                        "merge_strategy": "append-unique",
                    },
                    {
                        "path": f"debian/installer/modules/{architecture}/kernel-image",
                        "modules": list(module_names),
                        "merge_strategy": "append-unique",
                    },
                ],
                "package_list_append_text": "",
                "pkg_list_local_entries": [],
                "dpkg_buildpackage_args": ["-b", "-uc", "-us"],
                "kernel_config_entries": list(kernel_config_entries or []),
                "target_architecture": architecture,
            }
        ],
        workspace_dir=workspace_dir,
        state_dir=state_dir,
    )
    _log(log_file, f"Installer kernel UDEB manifest: {json.dumps(manifest, sort_keys=True)}")
    return manifest


def _build_source_package_udebs(
    *,
    package_names: list[str],
    state_dir: Path,
    workspace_dir: Path,
    log_file: Any,
) -> dict[str, Any]:
    rebuilds = []
    for package_name in package_names:
        metadata = _resolve_binary_package_udeb_rebuild_metadata(package_name)
        rebuilds.append(
            {
                "rebuild_kind": "source-package",
                "source_package": metadata["source_package"],
                "source_version": metadata["source_version"],
                "binary_package": metadata["binary_package"],
                "udeb_package": metadata["udeb_package"],
                "package_role": "generic",
                "installer_menu_item": "",
                "description": "",
                "long_description": [],
                "build_dep_packages": [],
                "depends": [],
                "provides": [],
                "conflicts": [],
                "replaces": [],
                "source_overlay_dir": "",
                "copy_install_manifest_from": metadata["binary_package"],
                "library_shlibs_udeb": metadata["udeb_package"],
                "architecture": "",
            }
        )
    manifest = _rebuild_udebs_from_source(rebuilds=rebuilds, workspace_dir=workspace_dir, state_dir=state_dir)
    _log(log_file, f"Installer package UDEB manifest: {json.dumps(manifest, sort_keys=True)}")
    return manifest


def _stage_installer_udeb_workspace(iso_root: Path, manifest: dict[str, Any], staging_dir: Path) -> str:
    staging_root = iso_root / ".debian-usb" / "installer"
    localudebs_dir = staging_root / "localudebs"
    repo_dir = staging_root / "repo"
    pkg_lists_dir = staging_root / "pkg-lists"
    for path in (localudebs_dir, repo_dir, pkg_lists_dir):
        path.mkdir(parents=True, exist_ok=True)
    shutil.copytree(Path(manifest["installer_localudebs_dir"]), localudebs_dir, dirs_exist_ok=True)
    shutil.copytree(Path(manifest["installer_localudeb_repo_path"]), repo_dir, dirs_exist_ok=True)
    shutil.copy2(Path(manifest["installer_pkg_list_local_path"]), pkg_lists_dir / "local")
    shutil.copy2(Path(manifest["installer_sources_list_udeb_local_path"]), staging_root / "sources.list.udeb.local")
    staging_dir.mkdir(parents=True, exist_ok=True)
    snapshot = {
        "installer_localudebs_dir": str(localudebs_dir),
        "installer_localudeb_repo_path": str(repo_dir),
        "installer_pkg_list_local_path": str(pkg_lists_dir / "local"),
        "installer_sources_list_udeb_local_path": str(staging_root / "sources.list.udeb.local"),
    }
    (staging_dir / "manifest.json").write_text(json.dumps(snapshot, indent=2) + "\n", encoding="utf-8")
    return str(repo_dir)


def _scan_installer_module_names(initrd_tree: Path, kernel_version: str) -> list[str]:
    module_root = initrd_tree / "lib" / "modules" / kernel_version
    if not module_root.is_dir():
        return []
    names = [_module_name_from_path(path) for path in sorted(module_root.rglob("*.ko*"))]
    return _dedupe(names)


def _replace_installer_kernel_tree(initrd_tree: Path, old_kernel_version: str, new_kernel_version: str, cache_root: Path) -> None:
    old_root = initrd_tree / "lib" / "modules" / old_kernel_version
    if old_root.exists():
        shutil.rmtree(old_root)
    cache_modules_root = cache_root / "root" / "lib" / "modules" / new_kernel_version
    if not cache_modules_root.is_dir():
        raise RuntimeError(f"downloaded kernel support cache did not provide /lib/modules/{new_kernel_version}")
    shutil.copytree(cache_modules_root, initrd_tree / "lib" / "modules" / new_kernel_version, dirs_exist_ok=True)


def _extract_udeb_payloads(staged_udeb_dir: Path, destination_root: Path) -> None:
    for package_path in sorted(staged_udeb_dir.glob("*.udeb")):
        subprocess.run(["dpkg-deb", "-x", str(package_path), str(destination_root)], check=True)


def _resolve_cached_kernel_image(cache_root: Path, kernel_version: str) -> Path:
    boot_dir = cache_root / "root" / "boot"
    candidates = [boot_dir / f"vmlinuz-{kernel_version}", boot_dir / f"vmlinuz-{kernel_version}.efi"]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    matches = sorted(boot_dir.glob(f"*{kernel_version}*"))
    for candidate in matches:
        if candidate.is_file():
            return candidate
    raise RuntimeError(f"downloaded kernel support cache did not provide a kernel image for {kernel_version}")


def _resolve_live_root_kernel_file(live_root: Path, kernel_version: str) -> Path:
    candidate = live_root / "boot" / f"vmlinuz-{kernel_version}"
    if candidate.is_file():
        return candidate
    matches = sorted((live_root / "boot").glob(f"*{kernel_version}*"))
    for match in matches:
        if match.is_file() and "vmlinuz" in match.name:
            return match
    raise RuntimeError(f"live root did not provide /boot/vmlinuz-{kernel_version}")


def _detect_live_root_kernel_version(live_root: Path, live_kernel_path: str) -> str:
    modules_root = live_root / "lib" / "modules"
    module_versions = sorted(path.name for path in modules_root.iterdir() if path.is_dir()) if modules_root.is_dir() else []
    kernel_name = Path(live_kernel_path).name
    for version in module_versions:
        if version and version in kernel_name:
            return version
    if len(module_versions) == 1:
        return module_versions[0]
    raise RuntimeError(
        f"Cannot unambiguously match live kernel {live_kernel_path} to installed module ABI(s): "
        + ", ".join(module_versions)
    )


def _resolve_live_root_initrd_file(live_root: Path, kernel_version: str) -> Path:
    candidate = live_root / "boot" / f"initrd.img-{kernel_version}"
    if candidate.is_file():
        return candidate
    matches = sorted((live_root / "boot").glob(f"*{kernel_version}*"))
    for match in matches:
        if match.is_file() and ("initrd" in match.name or "initramfs" in match.name):
            return match
    raise RuntimeError(f"live root did not provide /boot/initrd.img-{kernel_version}")


def _prune_live_kernel_versions(live_root: Path, kernel_version: str) -> None:
    modules_root = live_root / "lib" / "modules"
    for candidate in modules_root.iterdir() if modules_root.is_dir() else []:
        if candidate.name != kernel_version:
            shutil.rmtree(candidate)
    boot_dir = live_root / "boot"
    for candidate in boot_dir.iterdir() if boot_dir.is_dir() else []:
        if candidate.is_file() and kernel_version not in candidate.name and (
            candidate.name.startswith("vmlinuz-") or candidate.name.startswith("initrd") or candidate.name.startswith("config-")
        ):
            candidate.unlink()


def _refresh_live_metadata(live_root: Path, iso_root: Path, live_rootfs_path: str) -> None:
    manifest_path = iso_root / live_rootfs_path.lstrip("/")
    packages_output = _run_capture(["chroot", str(live_root), "dpkg-query", "-W", "--showformat=${Package} ${Version}\\n"])
    packages_rows = [line.rstrip() for line in packages_output.splitlines() if line.strip()]
    if (manifest_path.parent / "filesystem.manifest").exists():
        (manifest_path.parent / "filesystem.manifest").write_text("\n".join(packages_rows) + "\n", encoding="utf-8")
    if (manifest_path.parent / "filesystem.packages").exists():
        package_names = [row.split()[0] for row in packages_rows]
        (manifest_path.parent / "filesystem.packages").write_text("\n".join(package_names) + "\n", encoding="utf-8")
    filesystem_size_path = manifest_path.parent / "filesystem.size"
    if filesystem_size_path.exists():
        size_bytes = _directory_size_bytes(live_root)
        filesystem_size_path.write_text(f"{size_bytes}\n", encoding="utf-8")


@contextmanager
def _mounted_chroot(
    live_root: Path,
    log_file: Any,
    *,
    extra_binds: list[tuple[Path, Path]] | None = None,
) -> Iterator[None]:
    bind_pairs = [
        (Path("/dev"), live_root / "dev"),
        (Path("/dev/pts"), live_root / "dev/pts"),
        (Path("/proc"), live_root / "proc"),
        (Path("/sys"), live_root / "sys"),
        (Path("/run"), live_root / "run"),
    ]
    if extra_binds:
        bind_pairs.extend(extra_binds)
    mounted_targets: list[Path] = []
    resolv_conf = live_root / "etc" / "resolv.conf"
    resolv_backup: bytes | None = None
    resolv_replaced = False
    try:
        # Isolate all chroot binds from shared/slave parent propagation. Without
        # this boundary, one bind can be replicated as stacked mounts at the
        # same pathname and survive a single-path unmount during error cleanup.
        _run_logged(["mount", "--bind", str(live_root), str(live_root)], cwd=live_root, log_file=log_file)
        mounted_targets.append(live_root)
        _run_logged(["mount", "--make-rprivate", str(live_root)], cwd=live_root, log_file=log_file)

        for source, target in bind_pairs:
            target.mkdir(parents=True, exist_ok=True)
            _run_logged(["mount", "--bind", str(source), str(target)], cwd=live_root, log_file=log_file)
            mounted_targets.append(target)

        if resolv_conf.exists():
            resolv_backup = resolv_conf.read_bytes()
        host_resolv = Path("/etc/resolv.conf")
        if host_resolv.is_file():
            resolv_conf.parent.mkdir(parents=True, exist_ok=True)
            resolv_replaced = True
            shutil.copy2(host_resolv, resolv_conf)
        yield
    finally:
        cleanup_errors: list[str] = []
        if resolv_replaced:
            try:
                if resolv_backup is not None:
                    resolv_conf.write_bytes(resolv_backup)
                elif resolv_conf.exists():
                    resolv_conf.unlink()
            except OSError as exc:
                cleanup_errors.append(f"failed to restore {resolv_conf}: {exc}")
        try:
            _unmount_chroot_targets(mounted_targets)
        except RuntimeError as exc:
            cleanup_errors.append(str(exc))
        if cleanup_errors:
            raise RuntimeError("chroot cleanup failed: " + "; ".join(cleanup_errors))


def _unmount_chroot_targets(mounted_targets: list[Path]) -> None:
    failures: list[str] = []
    for target in reversed(mounted_targets):
        normal_command = ["umount", "--recursive", "--", str(target)]
        normal = subprocess.run(
            normal_command,
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )
        if normal.returncode == 0:
            continue

        lazy_command = ["umount", "--recursive", "--lazy", "--", str(target)]
        lazy = subprocess.run(
            lazy_command,
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )
        if lazy.returncode == 0:
            continue

        detail = lazy.stderr.strip() or normal.stderr.strip() or f"exit status {lazy.returncode}"
        failures.append(f"{target}: {detail}")
    if failures:
        raise RuntimeError("failed to unmount chroot bind mounts: " + "; ".join(failures))


def _install_packages_in_chroot(
    live_root: Path,
    packages: list[str],
    log_file: Any,
    *,
    apt_source_root: Path | None = None,
    optional_packages: list[str] | None = None,
) -> None:
    optional = list(dict.fromkeys(optional_packages or []))
    packages = [package for package in packages if package not in optional]
    if not packages and not optional:
        return
    extra_binds: list[tuple[Path, Path]] = []
    if apt_source_root is not None:
        extra_binds.append((apt_source_root, live_root / CHROOT_ISO_SOURCE_MOUNT.lstrip("/")))
    # policy-rc.d blocks service starts while maintainer scripts configure
    # units. Static masks must not obstruct deb-systemd-helper preset; restore
    # the runtime policy before removing the service-start guard, even on error.
    with _temporary_chroot_service_policy(live_root):
        with _temporary_chroot_systemd_unmask(live_root):
            with _temporary_chroot_apt_config(live_root, apt_source_root) as apt_config:
                with _mounted_chroot(live_root, log_file, extra_binds=extra_binds):
                    _run_in_chroot(live_root, _apt_get_chroot_command(apt_config, "update"), log_file)
                    if packages:
                        _run_in_chroot(live_root, _apt_get_chroot_command(apt_config, "-s", "install", "--no-install-recommends", *packages), log_file)
                        _run_in_chroot(live_root, _apt_get_chroot_command(apt_config, "install", "-y", "--no-install-recommends", *packages), log_file)
                    if optional:
                        helper = _prepare_live_root_directory(live_root, "usr/local/lib/debian-usb") / "live-packages.py"
                        if helper.is_symlink():
                            raise ValueError("refusing symlinked optional package helper")
                        shutil.copyfile(Path(__file__).with_name("live_packages.py"), helper)
                        command = _chroot_noninteractive_command("python3", "/usr/local/lib/debian-usb/live-packages.py",
                            "--apt-list", apt_config["list_path"], "--apt-parts", apt_config["parts_dir"])
                        for package in optional:
                            command += ["--package", package]
                        _run_in_chroot(live_root, command, log_file)
                    _run_in_chroot(live_root, _apt_get_chroot_command(apt_config, "clean"), log_file)


def _configure_debian_live_locale(live_root: Path, log_file: Any) -> None:
    _run_in_chroot(
        live_root,
        _chroot_noninteractive_command("locale-gen", DEBIAN_LIVE_LOCALE),
        log_file,
    )
    _run_in_chroot(
        live_root,
        _chroot_noninteractive_command(
            "update-locale",
            f"LANG={DEBIAN_LIVE_LOCALE}",
            f"LANGUAGE={DEBIAN_LIVE_LANGUAGE}",
        ),
        log_file,
    )


def _run_in_chroot(live_root: Path, command: list[str], log_file: Any) -> None:
    _run_logged(["chroot", str(live_root), *command], cwd=live_root, log_file=log_file)


@contextmanager
def _temporary_chroot_systemd_unmask(live_root: Path) -> Iterator[None]:
    """Allow presets for managed fwupd units only during guarded package work.

    This does not run systemctl on the host, remove unrelated masks, or start
    services. The caller must keep policy-rc.d active for this entire context.
    Both /dev/null links and empty unit files are systemd masks.
    """
    systemd_dir = _prepare_live_root_directory(live_root, "etc/systemd/system")
    try:
        for unit in LIVE_SYSTEMD_MASK_UNITS:
            path = systemd_dir / unit
            if path.is_symlink():
                if path.readlink() == Path("/dev/null"):
                    path.unlink()
            elif path.is_file() and path.stat().st_size == 0:
                path.unlink()
        yield
    finally:
        stage_live_systemd_masks(live_root)


@contextmanager
def _temporary_chroot_service_policy(live_root: Path) -> Iterator[None]:
    root = live_root.resolve()
    if not root.is_dir():
        raise RuntimeError(f"chroot service policy requires an existing Live root: {root}")
    policy_dir = root
    for part in ("usr", "sbin"):
        policy_dir /= part
        if policy_dir.is_symlink():
            raise RuntimeError(f"chroot service policy directory must not be a symlink: {policy_dir}")
        if policy_dir.exists() and not policy_dir.is_dir():
            raise RuntimeError(f"chroot service policy directory path is not a directory: {policy_dir}")
        policy_dir.mkdir(exist_ok=True)
    policy_path = policy_dir / "policy-rc.d"
    if policy_path.is_symlink():
        raise RuntimeError(f"chroot service policy must not be a symlink: {policy_path}")
    if policy_path.exists() and not policy_path.is_file():
        raise RuntimeError(f"chroot service policy path is not a regular file: {policy_path}")
    previous_content = policy_path.read_bytes() if policy_path.exists() else None
    previous_mode = policy_path.stat().st_mode & 0o7777 if policy_path.exists() else None
    try:
        policy_path.write_text("#!/bin/sh\nexit 101\n", encoding="utf-8")
        policy_path.chmod(0o755)
        yield
    finally:
        if previous_content is None:
            if policy_path.is_symlink() or policy_path.is_file():
                policy_path.unlink()
            elif policy_path.exists():
                raise RuntimeError(f"temporary chroot service policy became a non-file path: {policy_path}")
        else:
            if policy_path.is_symlink():
                policy_path.unlink()
            elif policy_path.exists() and not policy_path.is_file():
                raise RuntimeError(f"temporary chroot service policy became a non-file path: {policy_path}")
            policy_path.write_bytes(previous_content)
            if previous_mode is not None:
                policy_path.chmod(previous_mode)


@contextmanager
def _temporary_chroot_apt_config(live_root: Path, apt_source_root: Path | None) -> Iterator[dict[str, str]]:
    temp_root = live_root / CHROOT_TEMP_APT_ROOT.lstrip("/")
    list_path = temp_root / "sources.list"
    parts_dir = temp_root / "sources.list.d"
    temp_root.mkdir(parents=True, exist_ok=True)
    parts_dir.mkdir(parents=True, exist_ok=True)
    lines = _build_chroot_apt_source_lines(live_root, apt_source_root)
    if not lines:
        raise RuntimeError("Could not synthesize a usable APT source list for the remaster chroot.")
    list_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        yield {
            "list_path": "/" + str(list_path.relative_to(live_root)),
            "parts_dir": "/" + str(parts_dir.relative_to(live_root)),
        }
    finally:
        shutil.rmtree(temp_root, ignore_errors=True)


def _build_chroot_apt_source_lines(live_root: Path, apt_source_root: Path | None) -> list[str]:
    apt_root = live_root / "etc" / "apt"
    source_files = [apt_root / "sources.list"]
    source_parts_dir = apt_root / "sources.list.d"
    if source_parts_dir.is_dir():
        source_files.extend(sorted(source_parts_dir.glob("*.list")))
        source_files.extend(sorted(source_parts_dir.glob("*.sources")))
    lines: list[str] = []
    for source_file in source_files:
        if not source_file.is_file():
            continue
        content = source_file.read_text(encoding="utf-8")
        if source_file.suffix == ".sources":
            lines.extend(_deb822_source_lines_for_chroot(content, apt_source_root))
        else:
            lines.extend(_apt_list_source_lines_for_chroot(content, apt_source_root))
    return _dedupe(lines)


def _apt_list_source_lines_for_chroot(content: str, apt_source_root: Path | None) -> list[str]:
    lines: list[str] = []
    for raw_line in content.splitlines():
        stripped = raw_line.split("#", 1)[0].strip()
        if not stripped or stripped.startswith("#"):
            continue
        cdrom_match = APT_CDROM_SOURCE_RE.fullmatch(stripped)
        if cdrom_match:
            suite = cdrom_match.group("suite")
            options = (cdrom_match.group("options") or "").strip()
            components = (cdrom_match.group("components") or "").split()
            architectures = _apt_source_architectures_from_options(options)
            if apt_source_root is None or not _apt_source_root_supports_suite(
                apt_source_root,
                suite,
                components,
                architectures,
            ):
                continue
            option_prefix = f"{options} " if options else ""
            component_suffix = f" {' '.join(components)}" if components else ""
            lines.append(f"deb {option_prefix}file:{CHROOT_ISO_SOURCE_MOUNT} {suite}{component_suffix}")
            continue
        parts = stripped.split()
        if not parts or parts[0] != "deb":
            continue
        uri_index = 1
        if len(parts) > 1 and parts[1].startswith("["):
            while uri_index < len(parts) and not parts[uri_index].endswith("]"):
                uri_index += 1
            uri_index += 1
        if len(parts) <= uri_index + 1:
            continue
        suite = parts[uri_index + 1]
        components = parts[uri_index + 2 :]
        architectures = _apt_source_architectures_from_options(" ".join(parts[1:uri_index]))
        rewritten_uri = _rewrite_live_media_uri(
            parts[uri_index],
            suite,
            components,
            architectures,
            apt_source_root,
        )
        if not rewritten_uri:
            continue
        parts[uri_index] = rewritten_uri
        lines.append(" ".join(parts))
    return lines


def _deb822_source_lines_for_chroot(content: str, apt_source_root: Path | None) -> list[str]:
    stanzas: list[dict[str, str]] = []
    current: dict[str, str] = {}
    for raw_line in content.splitlines():
        stripped = raw_line.strip()
        if not stripped:
            if current:
                stanzas.append(current)
                current = {}
            continue
        if stripped.startswith("#"):
            continue
        key, separator, value = raw_line.partition(":")
        if not separator:
            continue
        normalized_key = key.strip().lower()
        payload = value.strip()
        if normalized_key in current:
            current[normalized_key] = f"{current[normalized_key]} {payload}".strip()
        else:
            current[normalized_key] = payload
    if current:
        stanzas.append(current)

    lines: list[str] = []
    for stanza in stanzas:
        if stanza.get("enabled", "yes").strip().lower() == "no":
            continue
        uris = stanza.get("uris", "").split()
        suites = stanza.get("suites", "").split()
        components = stanza.get("components", "").split()
        architectures = stanza.get("architectures", "").split()
        options: list[str] = []
        if stanza.get("trusted", "").strip().lower() == "yes":
            options.append("trusted=yes")
        signed_by = stanza.get("signed-by", "").strip()
        if signed_by:
            options.append(f"signed-by={signed_by}")
        option_prefix = f"[{' '.join(options)}] " if options else ""
        for package_type in stanza.get("types", "deb").split():
            if package_type != "deb":
                continue
            for uri in uris:
                for suite in suites:
                    rewritten_uri = _rewrite_live_media_uri(
                        uri,
                        suite,
                        components,
                        architectures,
                        apt_source_root,
                    )
                    if not rewritten_uri:
                        continue
                    suffix = f" {' '.join(components)}" if components else ""
                    lines.append(f"{package_type} {option_prefix}{rewritten_uri} {suite}{suffix}")
    return lines


def _apt_source_architectures_from_options(options: str) -> list[str]:
    match = re.search(r"(?:^|\s)arch=([^\s\]]+)", str(options or ""))
    if not match:
        return []
    return _dedupe([token for token in match.group(1).replace(",", " ").split() if token])


def _rewrite_live_media_uri(
    uri: str,
    suite: str,
    components: list[str],
    architectures: list[str],
    apt_source_root: Path | None,
) -> str:
    normalized_uri = uri.strip()
    if normalized_uri not in LOCAL_MEDIA_APT_URIS and not normalized_uri.lower().startswith("cdrom:"):
        return normalized_uri
    if apt_source_root is None:
        return ""
    if not _apt_source_root_supports_suite(apt_source_root, suite, components, architectures):
        return ""
    return f"file:{CHROOT_ISO_SOURCE_MOUNT}"


def _apt_source_root_supports_suite(
    apt_source_root: Path,
    suite: str,
    components: list[str],
    architectures: list[str],
) -> bool:
    token = str(suite or "").strip()
    if not token:
        return False
    if token.endswith("/"):
        flat_root = apt_source_root / token.lstrip("/")
        return any(path.is_file() for path in flat_root.glob("Packages*"))
    if not components:
        return False
    suite_root = apt_source_root / "dists" / token
    if not any(path.is_file() for path in (suite_root / "InRelease", suite_root / "Release")):
        return False
    effective_architectures = architectures or [_host_architecture()]
    for component in components:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+-]*", component):
            return False
        for architecture in effective_architectures:
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", architecture):
                return False
            index_root = suite_root / component / f"binary-{architecture}"
            if not any(path.is_file() for path in index_root.glob("Packages*")):
                return False
    return True


def _chroot_noninteractive_command(*args: str) -> list[str]:
    return [
        "env",
        "DEBIAN_FRONTEND=noninteractive",
        "LANG=C.UTF-8",
        "LC_ALL=C.UTF-8",
        *args,
    ]


def _apt_get_chroot_command(apt_config: dict[str, str], *args: str) -> list[str]:
    return [
        *_chroot_noninteractive_command("apt-get"),
        "-o",
        f"Dir::Etc::sourcelist={apt_config['list_path']}",
        "-o",
        f"Dir::Etc::sourceparts={apt_config['parts_dir']}",
        "-o",
        "Acquire::Retries=3",
        "-o", "APT::Update::Error-Mode=any",
        *args,
    ]


@dataclass(frozen=True)
class _InitrdArchiveLayout:
    main_offset: int
    compression: str


_CPIO_NEWC_MAGICS = {b"070701", b"070702"}
_CPIO_NEWC_HEADER_SIZE = 110
_INITRD_COPY_CHUNK_SIZE = 1024 * 1024


def _cpio_aligned_offset(offset: int) -> int:
    return (offset + 3) & ~3


def _newc_archive_end(archive_handle: Any, archive_path: Path, start: int, archive_size: int) -> int:
    position = start
    while True:
        if position + _CPIO_NEWC_HEADER_SIZE > archive_size:
            raise RuntimeError(f"truncated initrd cpio header at offset {position}: {archive_path}")
        archive_handle.seek(position)
        header = archive_handle.read(_CPIO_NEWC_HEADER_SIZE)
        if len(header) != _CPIO_NEWC_HEADER_SIZE or header[:6] not in _CPIO_NEWC_MAGICS:
            raise RuntimeError(f"invalid initrd cpio header at offset {position}: {archive_path}")
        try:
            file_size = int(header[54:62], 16)
            name_size = int(header[94:102], 16)
        except ValueError as exc:
            raise RuntimeError(f"invalid initrd cpio metadata at offset {position}: {archive_path}") from exc
        if name_size < 1:
            raise RuntimeError(f"invalid initrd cpio name size at offset {position}: {archive_path}")
        name_start = position + _CPIO_NEWC_HEADER_SIZE
        name_end = name_start + name_size
        data_start = _cpio_aligned_offset(name_end)
        data_end = data_start + file_size
        next_position = _cpio_aligned_offset(data_end)
        if name_end > archive_size or next_position > archive_size:
            raise RuntimeError(f"truncated initrd cpio member at offset {position}: {archive_path}")
        archive_handle.seek(name_start)
        member_name = archive_handle.read(name_size)
        if len(member_name) != name_size or member_name[-1:] != b"\0":
            raise RuntimeError(f"invalid initrd cpio member name at offset {position}: {archive_path}")
        if member_name[:-1] == b"TRAILER!!!":
            return next_position
        if next_position <= position:
            raise RuntimeError(f"non-advancing initrd cpio member at offset {position}: {archive_path}")
        position = next_position


def _skip_initrd_nul_padding(archive_handle: Any, position: int, archive_size: int) -> int:
    while position < archive_size:
        archive_handle.seek(position)
        chunk = archive_handle.read(min(_INITRD_COPY_CHUNK_SIZE, archive_size - position))
        if not chunk:
            return archive_size
        for index, value in enumerate(chunk):
            if value != 0:
                return position + index
        position += len(chunk)
    return position


def _compression_from_magic(magic: bytes) -> str | None:
    if magic.startswith(b"\x1f\x8b"):
        return "gzip"
    if magic.startswith(b"\xfd7zXZ\x00"):
        return "xz"
    if magic.startswith(b"(\xb5/\xfd"):
        return "zstd"
    if magic.startswith(b"BZh"):
        return "bzip2"
    if magic.startswith(b"\x04\x22M\x18"):
        return "lz4"
    if magic.startswith(b"\x02!L\x18"):
        return "lz4-legacy"
    return None


def _archive_compression(archive_path: Path) -> str:
    magic = b""
    if archive_path.is_file():
        with archive_path.open("rb") as handle:
            magic = handle.read(8)
    detected = _compression_from_magic(magic)
    if detected is not None:
        return detected
    if archive_path.suffix == ".gz":
        return "gzip"
    if archive_path.suffix == ".xz":
        return "xz"
    if archive_path.suffix == ".zst":
        return "zstd"
    return "none"


def _initrd_archive_layout(archive_path: Path) -> _InitrdArchiveLayout:
    if not archive_path.is_file():
        return _InitrdArchiveLayout(main_offset=0, compression=_archive_compression(archive_path))
    archive_size = archive_path.stat().st_size
    if archive_size <= 0:
        raise RuntimeError(f"initrd archive is empty: {archive_path}")

    cpio_starts: list[int] = []
    with archive_path.open("rb") as archive_handle:
        position = 0
        while position < archive_size:
            segment_start = _skip_initrd_nul_padding(archive_handle, position, archive_size)
            if segment_start >= archive_size:
                break
            archive_handle.seek(segment_start)
            magic = archive_handle.read(8)
            compression = _compression_from_magic(magic)
            if compression is not None:
                return _InitrdArchiveLayout(main_offset=segment_start, compression=compression)
            if magic[:6] in _CPIO_NEWC_MAGICS:
                cpio_starts.append(segment_start)
                position = _newc_archive_end(archive_handle, archive_path, segment_start, archive_size)
                continue
            if not cpio_starts and segment_start == 0:
                fallback_compression = _archive_compression(archive_path)
                if fallback_compression != "none":
                    return _InitrdArchiveLayout(main_offset=0, compression=fallback_compression)
            raise RuntimeError(
                f"unsupported initrd segment at offset {segment_start} in {archive_path}: "
                f"magic={magic.hex()}"
            )

    if cpio_starts:
        return _InitrdArchiveLayout(main_offset=cpio_starts[-1], compression="none")
    raise RuntimeError(f"initrd archive does not contain a supported archive segment: {archive_path}")


def _extract_initrd_archive(archive_path: Path, destination_dir: Path) -> None:
    destination_dir.mkdir(parents=True, exist_ok=True)
    layout = _initrd_archive_layout(archive_path)
    decompress_command = _archive_decompress_command(layout.compression)
    with archive_path.open("rb") as archive_handle:
        archive_handle.seek(layout.main_offset)
        first = subprocess.Popen(
            decompress_command,
            stdin=archive_handle,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        assert first.stdout is not None
        second = subprocess.Popen(
            ["cpio", "-idm", "--quiet", "--no-absolute-filenames"],
            cwd=str(destination_dir),
            stdin=first.stdout,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        first.stdout.close()
        _, second_stderr = second.communicate()
        first_stderr = first.stderr.read() if first.stderr is not None else b""
        if first.stderr is not None:
            first.stderr.close()
        first_rc = first.wait()
    if first_rc != 0:
        message = first_stderr.decode(errors="ignore").strip() or f"failed to decompress initrd {archive_path}"
        raise RuntimeError(message)
    if second.returncode != 0:
        message = second_stderr.decode(errors="ignore").strip() or f"failed to unpack initrd {archive_path}"
        raise RuntimeError(message)


def _copy_initrd_prefix(archive_path: Path, output_handle: Any, length: int) -> None:
    if length <= 0:
        return
    remaining = length
    with archive_path.open("rb") as input_handle:
        while remaining > 0:
            chunk = input_handle.read(min(_INITRD_COPY_CHUNK_SIZE, remaining))
            if not chunk:
                raise RuntimeError(f"could not preserve initrd prefix from {archive_path}")
            output_handle.write(chunk)
            remaining -= len(chunk)


def _repack_initrd_archive(source_dir: Path, archive_path: Path) -> None:
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    if archive_path.is_symlink():
        raise RuntimeError(f"refusing to replace symlinked initrd archive: {archive_path}")
    original_archive_mode = (
        stat.S_IMODE(archive_path.stat().st_mode) if archive_path.is_file() else None
    )
    layout = _initrd_archive_layout(archive_path)
    temp_path = archive_path.with_suffix(archive_path.suffix + ".tmp")
    compress_command = _archive_compress_command(layout.compression)
    with _temporary_owner_write_access(archive_path.parent):
        if temp_path.is_symlink() or temp_path.is_file():
            temp_path.unlink()
        elif temp_path.exists():
            raise RuntimeError(f"temporary initrd archive path is not a file: {temp_path}")
        try:
            with temp_path.open("wb") as output_handle:
                _copy_initrd_prefix(archive_path, output_handle, layout.main_offset)
                output_handle.flush()
                first = subprocess.Popen(
                    ["find", ".", "-mindepth", "1", "-print0"],
                    cwd=str(source_dir),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
                assert first.stdout is not None
                # Normalize cpio headers without changing checkout or workspace ownership.
                second = subprocess.Popen(
                    ["cpio", "--null", "-o", "-H", "newc", "--owner=0:0", "--quiet"],
                    cwd=str(source_dir),
                    stdin=first.stdout,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
                first.stdout.close()
                assert second.stdout is not None
                third = subprocess.Popen(
                    compress_command,
                    stdin=second.stdout,
                    stdout=output_handle,
                    stderr=subprocess.PIPE,
                )
                second.stdout.close()
                third_stderr = third.communicate()[1] or b""
            first_stderr = first.stderr.read() if first.stderr is not None else b""
            if first.stderr is not None:
                first.stderr.close()
            second_stderr = second.stderr.read() if second.stderr is not None else b""
            if second.stderr is not None:
                second.stderr.close()
            first_rc = first.wait()
            second_rc = second.wait()
            if first_rc != 0:
                raise RuntimeError(
                    first_stderr.decode(errors="ignore").strip()
                    or f"failed to enumerate initrd tree {source_dir}"
                )
            if second_rc != 0:
                raise RuntimeError(
                    second_stderr.decode(errors="ignore").strip()
                    or f"failed to create initrd cpio for {source_dir}"
                )
            if third.returncode != 0:
                raise RuntimeError(
                    third_stderr.decode(errors="ignore").strip()
                    or f"failed to compress initrd {archive_path}"
                )
            temp_path.replace(archive_path)
            if original_archive_mode is not None:
                archive_path.chmod(original_archive_mode)
        finally:
            if temp_path.is_symlink() or temp_path.is_file():
                temp_path.unlink()


def _archive_decompress_command(compression: str) -> list[str]:
    commands = {
        "gzip": ["gzip", "-dc"],
        "xz": ["xz", "-dc"],
        "zstd": ["zstd", "-dc"],
        "bzip2": ["bzip2", "-dc"],
        "lz4": ["lz4", "-dc"],
        "lz4-legacy": ["lz4", "-dc"],
        "none": ["cat"],
    }
    if compression not in commands:
        raise ValueError(f"unsupported initrd compression: {compression}")
    return commands[compression]


def _archive_compress_command(compression: str) -> list[str]:
    commands = {
        "gzip": ["gzip", "-9c"],
        "xz": ["xz", "-9c"],
        "zstd": ["zstd", "-19c"],
        "bzip2": ["bzip2", "-9c"],
        "lz4": ["lz4", "-9", "-c"],
        "lz4-legacy": ["lz4", "-l", "-9", "-c"],
        "none": ["cat"],
    }
    if compression not in commands:
        raise ValueError(f"unsupported initrd compression: {compression}")
    return commands[compression]


def _squashfs_compression(path: Path, *, processors: int | None = None) -> str:
    processor_count = processors or _squashfs_processor_count()
    result = subprocess.run(
        ["unsquashfs", "-processors", str(processor_count), "-s", str(path)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    if result.returncode != 0:
        return "xz"
    for line in result.stdout.splitlines():
        if "Compression" not in line:
            continue
        _, _, value = line.partition(":")
        token = value.strip().lower()
        if token:
            return token
    return "xz"


def _rewrite_checksum_files(iso_root: Path) -> None:
    checksum_specs = {
        "md5sum.txt": hashlib.md5,
        "sha256sum.txt": hashlib.sha256,
    }
    excluded_names = set(checksum_specs)
    for filename, digest_factory in checksum_specs.items():
        checksum_path = iso_root / filename
        if not checksum_path.is_file():
            continue
        rows = []
        for candidate in sorted(
            path for path in iso_root.rglob("*") if path.is_file() and path.name not in excluded_names
        ):
            digest = digest_factory(candidate.read_bytes()).hexdigest()
            relative = "./" + str(candidate.relative_to(iso_root))
            rows.append(f"{digest}  {relative}")
        with _temporary_owner_write_access(checksum_path):
            checksum_path.write_text("\n".join(rows) + "\n", encoding="utf-8")


def _load_json_file(path: Path) -> Any:
    if not path.is_file():
        raise ValueError(f"rebuild plan path is not a regular file: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def _validate_rebuild_plan(plan: Any) -> dict[str, Any]:
    if not isinstance(plan, dict):
        raise ValueError("rebuild installer ISO plan must be a JSON object")
    schema_version = int(plan.get("schema_version") or 0)
    if schema_version != REBUILD_SCHEMA_VERSION:
        raise ValueError(f"rebuild installer ISO plan schema_version must be {REBUILD_SCHEMA_VERSION}")

    normalized = {
        "schema_version": schema_version,
        "distro": _validate_choice(plan.get("distro"), {DISTRO_DEBIAN}, "distro"),
        "source_iso_path": _validate_absolute_path(
            plan.get("source_iso_path"),
            allow_missing=False,
            expect_directory=False,
            label="source_iso_path",
        ),
        "output_dir": _validate_absolute_path(
            plan.get("output_dir") or str(DEFAULT_REBUILD_OUTPUT_DIR),
            allow_missing=True,
            expect_directory=True,
            label="output_dir",
        ),
        "image_name": _validate_image_name(plan.get("image_name")),
        "scope": _validate_choice(plan.get("scope"), REBUILD_SCOPES, "scope"),
        "action": _validate_choice(plan.get("action"), REBUILD_ACTIONS, "action"),
        "architecture": str(plan.get("architecture") or "").strip(),
        "installer_kernel_modules": _validate_optional_module_list(plan.get("installer_kernel_modules"), "installer_kernel_modules"),
        "installer_udeb_packages": _validate_package_list(plan.get("installer_udeb_packages"), "installer_udeb_packages"),
        "live_deb_packages": _validate_package_list(plan.get("live_deb_packages"), "live_deb_packages"),
        "target_kernel_version": str(plan.get("target_kernel_version") or "").strip(),
    }
    if normalized["scope"] == "d-i":
        if normalized["action"] not in {
            "add-kernel-modules",
            "add-udeb-packages",
            "update-kernel",
        }:
            raise ValueError(f"action {normalized['action']} is not valid for scope d-i")
    else:
        if normalized["action"] not in {"replace-kernel", "add-deb-packages"}:
            raise ValueError(f"action {normalized['action']} is not valid for scope live-host")
    if normalized["action"] == "add-kernel-modules" and not normalized["installer_kernel_modules"]:
        raise ValueError("installer_kernel_modules is required for add-kernel-modules")
    if normalized["action"] == "add-udeb-packages" and not normalized["installer_udeb_packages"]:
        raise ValueError("installer_udeb_packages is required for add-udeb-packages")
    if normalized["action"] == "add-deb-packages" and not normalized["live_deb_packages"]:
        raise ValueError("live_deb_packages is required for add-deb-packages")
    if normalized["action"] in {"update-kernel", "replace-kernel"}:
        normalized["target_kernel_version"] = _validate_non_empty_token(
            normalized["target_kernel_version"],
            "target_kernel_version",
        )
    return normalized


def _find_existing_member(source: Any, candidates: tuple[str, ...]) -> str:
    for candidate in candidates:
        if source.exists(candidate):
            return candidate
    return ""


def _detect_firmware(source: Any) -> list[str]:
    firmware: list[str] = []
    if source.has_member_prefix("/EFI"):
        firmware.append("uefi")
    return firmware


def _infer_architecture(entries: list[Any], *paths: str) -> str:
    for path in paths:
        lowered = path.lower()
        for token, value in ARCH_HINTS.items():
            if token in lowered:
                return value
    for entry in entries:
        combined = " ".join(filter(None, [entry.kernel_path, entry.initrd_path])).lower()
        for token, value in ARCH_HINTS.items():
            if token in combined:
                return value
    return ""


def _infer_architecture_from_kernel_version(kernel_version: str) -> str:
    lowered = kernel_version.lower()
    if lowered.endswith("-amd64"):
        return "amd64"
    if lowered.endswith("-arm64"):
        return "arm64"
    return ""


def _host_architecture() -> str:
    result = subprocess.run(["dpkg", "--print-architecture"], capture_output=True, text=True, encoding="utf-8", check=False)
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def _run_capture(command: list[str]) -> str:
    result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", check=False)
    if result.returncode != 0:
        message = result.stderr.strip() or result.stdout.strip() or f"command failed: {' '.join(command)}"
        raise RuntimeError(message)
    return result.stdout


def _directory_size_bytes(path: Path) -> int:
    total = 0
    for candidate in path.rglob("*"):
        if candidate.is_file():
            total += candidate.stat().st_size
    return total


def _collect_live_entry_member_paths(entries: list[Any], attribute: str) -> list[str]:
    values: list[str] = []
    for entry in entries:
        if not str(getattr(entry, "kind", "")).startswith("live"):
            continue
        normalized = _normalize_member_path(str(getattr(entry, attribute, "") or ""))
        if normalized:
            values.append(normalized)
    return _dedupe(values)


def _coerce_member_path_list(value: Any, *, fallback: list[str]) -> list[str]:
    values = value if isinstance(value, list) else fallback
    normalized: list[str] = []
    for item in values:
        token = _normalize_member_path(str(item or ""))
        if token:
            normalized.append(token)
    return _dedupe(normalized or fallback)


def _copy_file_to_member_paths(source_path: Path, iso_root: Path, member_paths: list[str]) -> list[str]:
    modified_paths: list[str] = []
    for member_path in _dedupe(member_paths):
        target_path = iso_root / member_path.lstrip("/")
        target_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_path, target_path)
        modified_paths.append(member_path)
    return modified_paths


def _dedupe(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        token = str(value or "").strip()
        if not token or token in seen:
            continue
        seen.add(token)
        result.append(token)
    return result


def _log(handle: Any, line: str) -> None:
    handle.write(line.rstrip() + "\n")
    handle.flush()


def _initramfs_command_directory(live_root: Path, source: str) -> Path:
    relative = str(Path(source).parent).lstrip("/")
    # Resolve only the standard merged-/usr aliases, inside the extracted root.
    # Never let an absolute chroot symlink be followed against the host root.
    if relative in ("bin", "sbin") and (live_root / relative).is_symlink():
        target = "usr/" + relative
        if str((live_root / relative).readlink()) not in (target, "/" + target):
            raise RuntimeError(f"unsupported /{relative} symlink in source root")
        relative = target
    return _prepare_live_root_directory(live_root, relative)


def _initramfs_deferral_command(live_root: Path) -> str:
    """Find the command to defer without dismantling live-tools' diversion.

    A normal root calls update-initramfs directly. A Debian Live root instead
    has a package-owned symlink to live-update-initramfs, with the real engine
    diverted to update-initramfs.orig.initramfs-tools. Defer the live wrapper's
    executable, NOT that symlink or its engine: the vendor diversion must remain
    installed so dpkg can upgrade either package without ownership conflicts.
    """
    database_dir = _prepare_live_root_directory(live_root, "var/lib/dpkg")
    database = database_dir / "diversions"
    if database.is_symlink():
        raise RuntimeError("refusing symlinked dpkg diversions database")
    if database.exists() and not database.is_file():
        raise RuntimeError("dpkg diversions database is not a regular file")
    lines = database.read_text(encoding="utf-8").splitlines() if database.is_file() else []
    if len(lines) % 3:
        raise RuntimeError("invalid dpkg diversions database in source root")
    records = [tuple(lines[i:i + 3]) for i in range(0, len(lines), 3)]
    candidates = [record for record in records
                  if record[0] in ("/usr/sbin/update-initramfs", "/sbin/update-initramfs")]
    if len(candidates) > 1:
        raise RuntimeError("conflicting update-initramfs diversions in source root")
    if not candidates:
        source = "/usr/sbin/update-initramfs"
        legacy = live_root / "sbin/update-initramfs"
        if not (live_root / "usr/sbin/update-initramfs").exists() and legacy.exists():
            source = "/sbin/update-initramfs"
        directory = _initramfs_command_directory(live_root, source)
        binary = directory / "update-initramfs"
        if binary.is_symlink() or (binary.exists() and not binary.is_file()):
            raise RuntimeError("unsupported update-initramfs command in source root")
        return source

    source, destination, package = candidates[0]
    if package != "live-tools" or destination != source + ".orig.initramfs-tools":
        raise RuntimeError("unsupported update-initramfs diversion in source root: " + package)
    directory = _initramfs_command_directory(live_root, source)
    binary = directory / "update-initramfs"
    backup = directory / "update-initramfs.debian-usb-wrapper"
    if backup.exists() or backup.is_symlink():
        raise RuntimeError("unfinished live-tools wrapper backup in source root; use a fresh extraction")
    if not binary.is_symlink():
        raise RuntimeError("unsupported live-tools update-initramfs wrapper: expected a symlink")
    source_parent = "/" + directory.relative_to(live_root.resolve()).as_posix()
    target = posixpath.normpath(posixpath.join(source_parent, str(binary.readlink())))
    if target not in ("/bin/live-update-initramfs", "/usr/bin/live-update-initramfs"):
        raise RuntimeError("unsupported live-tools update-initramfs wrapper target: " + target)
    # Refuse a pre-existing diversion under either spelling of the live wrapper.
    if any(record[0] in ("/bin/live-update-initramfs", "/usr/bin/live-update-initramfs")
           for record in records):
        raise RuntimeError("unsupported live-update-initramfs diversion in source root")
    wrapper_dir = _initramfs_command_directory(live_root, target)
    wrapper = wrapper_dir / "live-update-initramfs"
    if wrapper.is_symlink() or not wrapper.is_file():
        raise RuntimeError("live-update-initramfs executable must be a regular file in the source root")
    # Use the on-disk spelling for a merged-/usr wrapper; preserve the vendor
    # update-initramfs diversion and its source spelling byte for byte.
    return "/" + wrapper.relative_to(live_root.resolve()).as_posix()


@contextmanager
def _deferred_initramfs_updates(live_root: Path, log_file: Any) -> Iterator[None]:
    """Defer -c/-u while keeping package-owned diversions and upgrades valid.

    The temporary local diversion protects a no-op shim from package unpacking.
    For Live images this is the executable behind the live-tools symlink; its
    original diversion and underlying initramfs-tools engine are never removed.
    The latest package-installed executable is restored before final generation.
    """
    source = _initramfs_deferral_command(live_root)
    directory = _initramfs_command_directory(live_root, source)
    binary = directory / Path(source).name
    destination = source + ".debian-usb-real"
    diverted = directory / Path(destination).name
    if diverted.exists() or diverted.is_symlink():
        raise RuntimeError("unfinished initramfs command diversion in source root: " + destination)
    shim = "#!/bin/sh\n# Deferred by debian-usb; generated once after customization.\nexit 0\n"
    # dpkg diversion keys are path spellings, not resolved inodes. On merged
    # /usr, protect the legacy spelling too: a package upgrade may unpack via
    # /bin or /sbin even when the symlink currently names /usr/bin or /usr/sbin.
    # The alias points to the SAME file, so register it without a second rename.
    alias = ""
    parent = str(Path(source).parent)
    if parent in ("/usr/bin", "/usr/sbin"):
        legacy_dir = live_root / Path(parent).name
        if legacy_dir.is_symlink():
            legacy_source = "/" + Path(parent).name + "/" + Path(source).name
            if _initramfs_command_directory(live_root, legacy_source) == directory:
                alias = legacy_source
    active: list[tuple[str, str]] = []
    restore_required = binary.is_file()
    created = False
    try:
        _run_in_chroot(live_root, ["dpkg-divert", "--local", "--add", "--rename", "--divert",
            destination, source], log_file)
        active.append((source, "--rename"))
        if alias:
            _run_in_chroot(live_root, ["dpkg-divert", "--local", "--add", "--no-rename", "--divert",
                alias + ".debian-usb-real", alias], log_file)
            active.append((alias, "--no-rename"))
        # Exclusive creation cannot accidentally follow a package/host symlink.
        with binary.open("x", encoding="utf-8") as handle:
            created = True
            handle.write(shim)
        binary.chmod(0o755)
        yield
    finally:
        if created:
            if binary.is_symlink() or (binary.exists() and not binary.is_file()):
                raise RuntimeError("temporary initramfs shim became a non-regular file: " + source)
            binary.unlink(missing_ok=True)
        for command_path, rename_option in reversed(active):
            _run_in_chroot(live_root, ["dpkg-divert", "--local", "--remove", rename_option, "--divert",
                command_path + ".debian-usb-real", command_path], log_file)
        if restore_required and (binary.is_symlink() or not binary.is_file()):
            raise RuntimeError("package installation did not preserve the initramfs command: " + source)


def _stage_encrypted_persistence_policy(live_root: Path) -> None:
    directory = _prepare_live_root_directory(live_root, "etc/cryptsetup-initramfs")
    target = directory / "conf-hook"
    if target.is_symlink():
        raise ValueError("refusing symlinked cryptsetup initramfs policy")
    content = target.read_text(encoding="utf-8") if target.is_file() else ""
    target.write_text(content.rstrip() + "\n# Include cryptsetup even without host crypttab entries.\nCRYPTSETUP=y\n", encoding="utf-8")
    directory = _prepare_live_root_directory(live_root, "etc/initramfs-tools")
    target = directory / "modules"
    if target.is_symlink():
        raise ValueError("refusing symlinked initramfs modules policy")
    content = target.read_text(encoding="utf-8") if target.is_file() else ""
    existing = {line.split()[0] for line in content.splitlines() if line.strip() and not line.lstrip().startswith("#")}
    additions = [name for name in ("dm_mod", "dm_crypt") if name not in existing]
    target.write_text(content.rstrip() + "\n" + "\n".join(additions) + "\n", encoding="utf-8")


def _stage_live_initrd_overlay(live_root: Path, overlay_dir: Path) -> None:
    destination = _prepare_live_root_directory(live_root, "usr/share/debian-usb/initrd-overlay")
    merge_initrd_overlay(overlay_dir, destination)
    hooks = _prepare_live_root_directory(live_root, "etc/initramfs-tools/hooks")
    target = hooks / "zz-debian-usb-overlay"
    if target.is_symlink():
        raise ValueError("refusing symlinked initramfs overlay hook")
    target.write_text(
        '#!/bin/sh\nset -eu\ncase "${1:-}" in prereqs) exit 0;; esac\n'
        ': "${DESTDIR:?initramfs destination is required}"\n'
        '# Keep repository ownership out of the generated initrd.\n'
        'cp -a --no-preserve=ownership -- /usr/share/debian-usb/initrd-overlay/. "${DESTDIR}/"\n',
        encoding="utf-8",
    )
    target.chmod(0o755)
