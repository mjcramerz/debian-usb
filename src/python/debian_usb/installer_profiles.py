"""Isolated Desktop/Server installer assets and explicitly requested HD-MEDIA trees.

This module is deliberately not used by any live-ISO preparation path.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import stat
import tempfile
from typing import Any

FAMILIES = {"debian": "debian", "kali-linux": "kali"}
FLAVORS = {"desktop": "de", "server": "srv"}
PROFILE_ASSET_DIR = ".debian-usb/installer-profiles"


def supports_installer_profiles(profile: str, source_role: str) -> bool:
    return profile in FAMILIES and source_role in {"netinst", "netboot"}


def normalize_hd_media_dirs(profile: str, source_role: str, values: object) -> dict[str, str]:
    """Normalize explicit consent. An absent/empty map means no copying, ever."""
    if values is None:
        return {}
    if not isinstance(values, dict):
        raise ValueError("hd_media_preseed_dirs must be an object")
    if values and not supports_installer_profiles(profile, source_role):
        raise ValueError("HD-MEDIA Desktop/Server preseeding requires Debian/Kali netinst or netboot")
    unknown = set(values) - set(FLAVORS)
    if unknown:
        raise ValueError("unknown HD-MEDIA profile(s): " + ", ".join(sorted(unknown)))
    normalized: dict[str, str] = {}
    for flavor, value in values.items():
        if not isinstance(value, str):
            raise ValueError(f"{flavor} preseed source must be a path")
        if not value.strip():
            continue
        path = Path(value).expanduser().resolve(strict=True)
        if path.is_file():
            if path.name != "preseed.cfg":
                raise ValueError(f"{flavor}: select preseed.cfg, not {path.name}")
            path = path.parent
        if not path.is_dir() or path == Path(path.anchor):
            raise ValueError(f"{flavor}: unsafe or missing preseed directory: {path}")
        seed = path / "preseed.cfg"
        if seed.is_symlink() or not seed.is_file() or seed.stat().st_size == 0:
            raise ValueError(f"{flavor}: expected a nonempty regular preseed.cfg in {path}")
        # Never follow source links; preserve them verbatim. Special files cannot
        # be safely copied from an arbitrary codebase by the privileged writer.
        for node in path.rglob("*"):
            mode = node.lstat().st_mode
            if not (stat.S_ISREG(mode) or stat.S_ISDIR(mode) or stat.S_ISLNK(mode)):
                raise ValueError(f"unsupported special file in preseed codebase: {node}")
        normalized[flavor] = str(path)
    return normalized


def hd_media_target(profile: str, flavor: str, config: dict[str, str]) -> str:
    family = FAMILIES[profile]
    suffix = FLAVORS[flavor]
    key = f"PRESEED_USB_{family.upper()}_{suffix.upper()}_FILE"
    path = config.get(key, f"/hd-media/{family}-preseed-{suffix}/preseed.cfg")
    match = re.fullmatch(r"/hd-media/([A-Za-z0-9._+-]+)/preseed\.cfg", path)
    if match is None:
        raise ValueError(f"{key} must be /hd-media/<separate-folder>/preseed.cfg")
    folder = match.group(1)
    reserved = {".", "..", ".debian-usb", "boot", "EFI", "lost+found", "payload"}
    reserved.update(f"{name}-{role}{suffix}" for name in FAMILIES.values()
                    for role in ("live", "netinst", "netboot") for suffix in ("", "-de", "-srv"))
    if folder in reserved:
        raise ValueError(f"{key} must not use a boot/payload directory: {folder}")
    # /hd-media is the INSTALLER mountpoint, not a directory on the data volume.
    return folder


def hd_media_requests(items: list[dict[str, Any]], config: dict[str, str]) -> list[tuple[str, Path]]:
    requests: dict[str, Path] = {}
    for item in items:
        profile, role = str(item.get("profile", "")), str(item.get("source_role", "primary"))
        values = normalize_hd_media_dirs(profile, role, item.get("hd_media_preseed_dirs"))
        for flavor, source in values.items():
            target = hd_media_target(profile, flavor, config)
            resolved = Path(source)
            if target in requests and requests[target] != resolved:
                raise ValueError(f"conflicting HD-MEDIA codebases for {target}; netinst and netboot share this folder")
            requests[target] = resolved
    return sorted(requests.items())


def stage_hd_media_preseeds(items: list[dict[str, Any]], config: dict[str, str], target_root: str = "") -> dict[str, Any]:
    requests = hd_media_requests(items, config)
    size = sum(node.lstat().st_size for _, source in requests for node in source.rglob("*")
               if node.is_file() and not node.is_symlink())
    if not target_root:
        return {"bytes": size, "folders": [name for name, _ in requests], "copied": False}
    root = Path(target_root).resolve(strict=True)
    if not root.is_dir() or root == Path(root.anchor):
        raise ValueError("HD-MEDIA target must be a mounted data-volume directory, not /")
    for name, source in requests:
        target = root / name
        if target.is_symlink():
            raise ValueError(f"refusing a symlinked preseed destination: {target}")
        if source == target or source.is_relative_to(target) or target.is_relative_to(source):
            raise ValueError("preseed source and target directories must not overlap")
        # Copy the complete parent directory, including dotfiles, before replacing
        # the previous profile tree. Never merge through old destination symlinks.
        with tempfile.TemporaryDirectory(prefix=".preseed-stage-", dir=root) as temp:
            staged = Path(temp) / name
            shutil.copytree(source, staged, symlinks=True)
            if target.exists():
                if not target.is_dir():
                    raise ValueError(f"preseed destination is not a directory: {target}")
                backup = Path(temp) / "previous"
                target.rename(backup)
                try:
                    staged.rename(target)
                except BaseException:
                    backup.rename(target)
                    raise
            else:
                staged.rename(target)
    return {"bytes": size, "folders": [name for name, _ in requests], "copied": bool(requests)}


# The native netboot initrd lacks the hd-media/file-preseed boot path. Its existing
# initrd-preseed loader is reused after mounting the exact USB data UUID. This
# retains Debian's own include/checksum/early_command implementation.
NETBOOT_HD_MEDIA_HELPER = r'''#!/bin/sh
# debian-usb: mount the selected USB source, never an arbitrary /dev/sdX disk.
set -eu
set -f
seed_file=
usb_uuid=
for argument in $(cat /proc/cmdline); do
    case "$argument" in
        file=*|preseed/file=*) seed_file=${argument#*=} ;;
        DUSB_HD_MEDIA_UUID=*) usb_uuid=${argument#*=} ;;
    esac
done
case "$seed_file" in /hd-media/*/preseed.cfg) ;; *) echo 'debian-usb: invalid HD-MEDIA preseed path' >&2; exit 1 ;; esac
case "$seed_file" in *..*|*[!A-Za-z0-9._+/-]*) echo 'debian-usb: unsafe HD-MEDIA preseed path' >&2; exit 1 ;; esac
case "$usb_uuid" in ''|*[!A-Za-z0-9-]*) echo 'debian-usb: missing/invalid USB UUID' >&2; exit 1 ;; esac
for module in xhci_pci xhci_hcd ehci_pci ehci_hcd uhci_hcd usb_storage uas sd_mod ext4; do
    modprobe "$module" 2>/dev/null || :
done
if command -v udevadm >/dev/null 2>&1; then udevadm settle --timeout=15 || :; fi
mkdir -p /hd-media
found=
attempt=0
while [ "$attempt" -lt 30 ]; do
    device=/dev/disk/by-uuid/$usb_uuid
    if [ ! -b "$device" ] && command -v blkid >/dev/null 2>&1; then
        device=$(blkid -U "$usb_uuid" 2>/dev/null || :)
    fi
    if [ -n "$device" ] && [ -b "$device" ]; then
        if awk '$2 == "/hd-media" { found=1 } END { exit !found }' /proc/mounts; then
            mounted=$(awk '$2 == "/hd-media" { print $1; exit }' /proc/mounts)
            [ "$(readlink -f "$mounted")" = "$(readlink -f "$device")" ] || {
                echo 'debian-usb: /hd-media is mounted from a different device' >&2; exit 1;
            }
            found=1
        elif mount -t ext4 -o ro "$device" /hd-media; then
            found=1
        fi
        [ -z "$found" ] || break
    fi
    attempt=$((attempt + 1))
    sleep 1
done
if [ -z "$found" ] || [ ! -s "$seed_file" ]; then
    echo "debian-usb: cannot load $seed_file from USB UUID $usb_uuid" >&2
    echo 'Populate the matching preseed folder on USB partition 2; verify USB/ext4 drivers in the netboot initrd.' >&2
    exit 1
fi
# The startup wrapper reads this file without eval or sourcing user content.
mkdir -p /var/run
umask 077
printf '%s\n' "$seed_file" > /var/run/debian-usb-hd-media-preseed
'''


def _install_transport_dispatch(root: Path, source_role: str) -> dict[str, Any]:
    from .installer_preseed import install_transport_dispatch
    return install_transport_dispatch(root, source_role, NETBOOT_HD_MEDIA_HELPER)


def prepare_installer_profiles(bundle_root: Path, profile: str, source_role: str, *,
                               overlay_root: Path | None = None,
                               desktop_preseed: Path | None = None) -> dict[str, Any]:
    """Derive isolated initrds from a clean, immutable role-specific archive.

    Copy the *archive*, never the extracted tree: real installer initrds have
    character/block devices, FIFOs and hard links, which shutil.copytree cannot
    reproduce. Repacking an existing archive also retains its compression and
    any leading early-cpio/microcode segments. Each flavor gets a fresh cpio
    extraction, so changes to one flavor cannot contaminate the other.
    """
    if not supports_installer_profiles(profile, source_role):
        raise ValueError("unsupported split installer profile")
    from .initrd_overlay import merge_initrd_overlay
    from .rebuild_iso import _extract_initrd_archive, _repack_initrd_archive
    asset_dir = bundle_root / ("hd-media" if source_role == "netinst" else "netboot")
    output = bundle_root / PROFILE_ASSET_DIR
    if overlay_root is not None:
        unexpected = [path.name for path in overlay_root.iterdir() if path.name not in {*FLAVORS, ".gitkeep"}]
        if unexpected:
            raise ValueError("installer overlays must be inside desktop/ or server/, not the stage root: " + ", ".join(unexpected))
    manifests: dict[str, Any] = {}
    with tempfile.TemporaryDirectory(prefix=".profile-build-", dir=bundle_root) as temporary:
        working = Path(temporary)
        staged_output = working / "profiles"
        staged_output.mkdir()
        for flavor, suffix in FLAVORS.items():
            destination = staged_output / flavor
            destination.mkdir(parents=True, exist_ok=True)
            archive = destination / "initrd.gz"
            shutil.copy2(asset_dir / "initrd.gz", archive)
            expanded = working / flavor
            try:
                _extract_initrd_archive(archive, expanded)
            except (OSError, RuntimeError) as exc:
                detail = str(exc)
                hint = ""
                if "Permission denied" in detail or "Operation not permitted" in detail:
                    hint = (
                        " Installer extraction must preserve device nodes; use the "
                        "application's sudo-managed preparation on a filesystem that "
                        "permits device-node creation (root/CAP_MKNOD). Do not delete "
                        "the nodes or change their permissions to bypass this check."
                    )
                raise RuntimeError(
                    f"{profile} {source_role} {flavor}: failed to extract installer "
                    f"initrd {asset_dir / 'initrd.gz'}: {detail}{hint}"
                ) from exc
            if (expanded / "preseed.cfg").exists() or (expanded / "preseed.cfg").is_symlink():
                raise ValueError("the shared source initrd already contains preseed.cfg; use a clean source and put preseeding in desktop/ or server/ to prevent cross-profile leakage")
            selected = overlay_root / flavor if overlay_root is not None else None
            if selected is not None and selected.is_dir():
                merge_initrd_overlay(selected, expanded)
                (expanded / ".gitkeep").unlink(missing_ok=True)
            if flavor == "desktop" and desktop_preseed is not None:
                shutil.copy2(desktop_preseed, expanded / "preseed.cfg")
            try:
                transport = _install_transport_dispatch(expanded, source_role)
            except (OSError, ValueError) as exc:
                raise ValueError(f"{profile} {source_role} {flavor}: {exc}") from exc
            embedded_preseed = (expanded / "preseed.cfg").is_file()
            shutil.copy2(asset_dir / "vmlinuz", destination / "vmlinuz")
            _repack_initrd_archive(expanded, archive)
            # Do not retain two expanded trees while preparing large installers.
            shutil.rmtree(expanded)
            manifests[flavor] = {
                "kernel_path": f"/{PROFILE_ASSET_DIR}/{flavor}/vmlinuz",
                "initrd_path": f"/{PROFILE_ASSET_DIR}/{flavor}/initrd.gz",
                "usb_directory": f"/{FAMILIES[profile]}-{source_role}-{suffix}",
                "overlay_dir": str(selected) if selected is not None and selected.is_dir() else "",
                "embedded_preseed": embedded_preseed,
                "transport_dispatch": True,
                "preseed_transport": transport,
            }
        (staged_output / "manifest.json").write_text(json.dumps(manifests, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        output.parent.mkdir(parents=True, exist_ok=True)
        if output.is_symlink():
            raise ValueError("refusing symlinked installer profile output")
        backup = working / "previous-profiles"
        if output.exists():
            output.rename(backup)
        try:
            staged_output.rename(output)
        except BaseException:
            if backup.exists():
                backup.rename(output)
            raise
    return manifests


def validate_installer_profile_assets(source_path: str, profile: str, source_role: str) -> dict[str, Any]:
    if not supports_installer_profiles(profile, source_role):
        return {}
    root = Path(source_path)
    source_manifest_path = root / "managed-installer-source.json"
    if not source_manifest_path.is_file() or source_manifest_path.is_symlink():
        raise ValueError("missing regular managed installer source manifest; prepare the source again")
    source_manifest = json.loads(source_manifest_path.read_text(encoding="utf-8"))
    if not isinstance(source_manifest, dict) or source_manifest.get("profile") != profile or source_manifest.get("source_role") != source_role:
        raise ValueError("prepared source identity does not match the selected installer role")
    if (root / PROFILE_ASSET_DIR).is_symlink():
        raise ValueError("refusing symlinked installer profile directory")
    manifest_path = root / PROFILE_ASSET_DIR / "manifest.json"
    if not root.is_dir() or not manifest_path.is_file():
        raise ValueError("installer source has no isolated Desktop/Server assets; prepare the source again (or run prepare-installer-profiles)")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict):
        raise ValueError("installer profile manifest must be an object")
    for flavor, suffix in FLAVORS.items():
        if (root / PROFILE_ASSET_DIR / flavor).is_symlink():
            raise ValueError(f"refusing symlinked installer flavor directory: {flavor}")
        if not isinstance(manifest.get(flavor), dict) or not manifest[flavor].get("transport_dispatch"):
            raise ValueError(f"missing installer transport dispatcher: {flavor}")
        from .installer_preseed import DISPATCH_VERSION
        transport = manifest[flavor].get("preseed_transport")
        if not isinstance(transport, dict) or transport.get("version") != DISPATCH_VERSION:
            raise ValueError(
                f"outdated installer preseed transport for {flavor}; prepare a new managed "
                "source from the original downloads with the selected Desktop/Server overlays "
                f"(transport v{DISPATCH_VERSION} required); the existing bundle is unchanged"
            )
        if transport.get("source_role") != source_role:
            raise ValueError(f"installer transport has the wrong source role: {flavor}")
        if manifest[flavor].get("usb_directory") != f"/{FAMILIES[profile]}-{source_role}-{suffix}":
            raise ValueError(f"installer profile manifest has the wrong USB directory: {flavor}")
        for filename in ("vmlinuz", "initrd.gz"):
            path = root / PROFILE_ASSET_DIR / flavor / filename
            if not path.is_file() or path.is_symlink() or path.stat().st_size == 0:
                raise ValueError(f"missing isolated {flavor} installer asset: {path}")
    return manifest
