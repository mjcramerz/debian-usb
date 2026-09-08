from __future__ import annotations

import datetime as dt
import json
import os
import shutil
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
from typing import Any

from .build_iso import (
    DEFAULT_LOG_DIR,
    DEFAULT_STATE_DIR,
    DEFAULT_WORK_DIR,
    INITRD_KERNEL_VERSION_RE,
    _run_simple,
    _inspect_kernel_support,
    _log,
    _resolve_kernel_module_path,
    _run_logged,
    _validate_optional_module_list,
)
from .boot_inspect import ISO_SCAN_EXACT_SELECTION_MARKER, validate_netinst_payload_iso
from .downloads import DOWNLOAD_ROOT
from .initrd_overlay import merge_initrd_overlay
from .iso_source import open_source
from .rebuild_iso import (
    _build_installer_kernel_udebs,
    _extract_initrd_archive,
    _extract_udeb_payloads,
    _repack_initrd_archive,
    ensure_debian_rebuild_deps,
)

SOURCE_BUNDLE_ROOT = DOWNLOAD_ROOT / "sources"
INSTALLER_INITRD_PREPARE_REQUIRED_COMMANDS = ["lsinitramfs"]
INSTALLER_INITRD_PREPARE_APT_DEPS = ["initramfs-tools-core"]
INSTALLER_INITRD_MODULE_COPY_REQUIRED_COMMANDS = ["cpio", "depmod", "lsinitramfs"]
INSTALLER_INITRD_MODULE_COPY_APT_DEPS = ["cpio", "kmod", "initramfs-tools-core"]
INSTALLER_INITRD_PRESEED_COPY_REQUIRED_COMMANDS = ["cpio", "lsinitramfs"]
INSTALLER_INITRD_PRESEED_COPY_APT_DEPS = ["cpio", "initramfs-tools-core"]
INSTALLER_INITRD_OVERLAY_REQUIRED_COMMANDS = ["cpio", "find", "gzip"]
INSTALLER_INITRD_OVERLAY_APT_DEPS = ["cpio", "findutils", "gzip"]
INSTALLER_INITRD_EXACT_ISO_REQUIRED_COMMANDS = ["cpio", "find", "gzip", "sh"]
INSTALLER_INITRD_EXACT_ISO_APT_DEPS = ["cpio", "findutils", "gzip", "dash"]
DEBIAN_NETINST_EXTRA_MODULE_OPTIONS = {
    "xxhash_generic": {
        "module_names": ["xxhash_generic"],
        "fallback_module_names": ["xxhash", "xxhash_generic"],
        "kernel_config_entries": ["CONFIG_XXHASH=m", "CONFIG_CRYPTO_XXHASH=m"],
        "module_alias_candidates": [
            "xxhash64",
            "xxhash64-generic",
            "xxhash_generic",
            "crypto-xxhash64",
            "crypto-xxhash64-generic",
            "crypto_xxhash64",
            "crypto_xxhash64_generic",
        ],
        "config_symbols": ["CONFIG_XXHASH", "CONFIG_CRYPTO_XXHASH"],
        "builtin_satisfies_symbols": ["CONFIG_XXHASH", "CONFIG_CRYPTO_XXHASH"],
    },
    "lz4": {
        "module_names": ["lz4"],
        "fallback_module_names": ["lz4", "lz4_compress"],
        "kernel_config_entries": ["CONFIG_CRYPTO_LZ4=m", "CONFIG_LZ4_COMPRESS=m"],
        "module_alias_candidates": [],
        "config_symbols": ["CONFIG_CRYPTO_LZ4", "CONFIG_LZ4_COMPRESS"],
        "builtin_satisfies_symbols": ["CONFIG_CRYPTO_LZ4"],
    },
}
MODULE_SOURCE_STRATEGY_HOST_KERNEL = "host-kernel"
MODULE_SOURCE_STRATEGY_SOURCE_UDEB = "source-udeb"
KERNEL_FILENAME_VERSION_RE = re.compile(r"^(?:vmlinuz|linux|bzImage)[-_](.+)$")
KERNEL_VERSION_TEXT_RE = re.compile(r"Linux version ([^ ]+)")
ISO_SCAN_STATE_19_HEADER_RE = re.compile(
    r"^(?P<indent>[ \t]*)19\)[^\r\n]*(?:\r?\n|$)",
    re.MULTILINE,
)
ISO_SCAN_FIRST_PASS_CALL_RE = re.compile(
    r'^[ \t]*scan_device_for_isos[ \t]+0[ \t]+(?:"\$(?:selected_devices|\{selected_devices\})"|\$(?:selected_devices|\{selected_devices\}))[ \t]*(?:\r?\n|$)',
    re.MULTILINE,
)
ISO_SCAN_EXACT_SELECTION_BLOCK = r'''

		# BEGIN debian-usb exact iso-scan/filename selection
		db_get iso-scan/filename
		requested_iso=$RET
		if [ -n "$requested_iso" ]; then
			case "$requested_iso" in
				/*.[iI][sS][oO]) ;;
				*)
					log "Refusing invalid requested ISO path: $requested_iso"
					exit 1
					;;
			esac
			case "$requested_iso" in
				*[!A-Za-z0-9._+/-]*|*"//"*|*"/../"*|"/.."|*"/..")
					log "Refusing unsafe requested ISO path: $requested_iso"
					exit 1
					;;
			esac
			case "$selected_devices" in
				/dev/*) ;;
				*)
					log "Exact ISO selection requires one explicit /dev source: $selected_devices"
					exit 1
					;;
			esac
			case "$selected_devices" in
				*[!A-Za-z0-9._/+:-]*|*' '*)
					log "Exact ISO selection refuses multiple or unsafe source devices: $selected_devices"
					exit 1
					;;
			esac
			if ! mount_device "$selected_devices"; then
				log "Failed to mount exact ISO source device: $selected_devices"
				exit 1
			fi
			if [ ! -f "/hd-media/${requested_iso#/}" ]; then
				log "Requested ISO does not exist on $selected_devices: $requested_iso"
				umount -d -l /hd-media 2>/dev/null || true
				exit 1
			fi
			umount -d -l /hd-media 2>/dev/null || true
			log "Using exact requested ISO without scanning sibling images: $requested_iso on $selected_devices"
			use_this_iso "$requested_iso" "$selected_devices"
			# use_this_iso exits on success. Never scan if it unexpectedly returns.
			exit 1
		fi
		# END debian-usb exact iso-scan/filename selection
'''


def _require_nonempty_regular_file(source_path: str, label: str) -> Path:
    source = Path(source_path).expanduser().resolve()
    if not source.is_file():
        raise ValueError(f"{label} is not a regular file: {source}")
    if source.stat().st_size <= 0:
        raise ValueError(f"{label} is empty: {source}")
    return source


def _copy_regular_file(source_path: str, destination: Path, label: str) -> str:
    source = _require_nonempty_regular_file(source_path, label)
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)
    return str(destination)


def _patch_iso_scan_postinst_text(text: str) -> str:
    if ISO_SCAN_EXACT_SELECTION_MARKER in text:
        if text.count(ISO_SCAN_EXACT_SELECTION_MARKER) == 1 and ISO_SCAN_EXACT_SELECTION_BLOCK.strip("\n") in text:
            return text
        raise RuntimeError("cannot enforce exact iso-scan/filename selection: existing patch marker is incomplete")

    state_headers = list(ISO_SCAN_STATE_19_HEADER_RE.finditer(text))
    if len(state_headers) != 1:
        raise RuntimeError(
            "cannot enforce exact iso-scan/filename selection: expected exactly one "
            f"Debian iso-scan state-19 anchor, found {len(state_headers)}"
        )

    state_header = state_headers[0]
    state_indent = state_header.group("indent")
    next_state_header_re = re.compile(
        rf"^{re.escape(state_indent)}(?:[0-9]+|\*)\)[^\r\n]*(?:\r?\n|$)",
        re.MULTILINE,
    )
    next_state_header = next_state_header_re.search(text, state_header.end())
    state_end = next_state_header.start() if next_state_header is not None else len(text)
    first_pass_calls = list(ISO_SCAN_FIRST_PASS_CALL_RE.finditer(text, state_header.end(), state_end))
    if len(first_pass_calls) != 1:
        raise RuntimeError(
            "cannot enforce exact iso-scan/filename selection: expected exactly one first-pass scan "
            f"in the Debian iso-scan state-19 anchor, found {len(first_pass_calls)}"
        )

    insertion_offset = first_pass_calls[0].start()
    return text[:insertion_offset] + ISO_SCAN_EXACT_SELECTION_BLOCK + text[insertion_offset:]


def _managed_bundle_parent_directories(bundle_root: Path) -> tuple[Path, ...]:
    managed_root = Path(os.path.abspath(os.fspath(SOURCE_BUNDLE_ROOT.expanduser())))
    bundle_path = Path(os.path.abspath(os.fspath(bundle_root.expanduser())))
    try:
        relative_bundle = bundle_path.relative_to(managed_root)
    except ValueError:
        return ()
    if not relative_bundle.parts:
        return ()

    directories = [managed_root]
    current = managed_root
    for part in relative_bundle.parts[:-1]:
        current /= part
        directories.append(current)
    return tuple(directories)


def _finalize_bundle_access(bundle_root: Path) -> None:
    invoking_uid = str(os.environ.get("SUDO_UID") or "").strip()
    invoking_gid = str(os.environ.get("SUDO_GID") or "").strip()
    ownership: tuple[int, int] | None = None
    if os.geteuid() == 0 and invoking_uid.isdecimal() and invoking_gid.isdecimal():
        ownership = (int(invoking_uid), int(invoking_gid))

    managed_parents = _managed_bundle_parent_directories(bundle_root) if ownership is not None else ()
    for directory in managed_parents:
        metadata = directory.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise RuntimeError(f"managed installer source parent is not a real directory: {directory}")

    for current_root, directory_names, file_names in os.walk(bundle_root, followlinks=False):
        current_path = Path(current_root)
        if not current_path.is_symlink():
            current_path.chmod(0o755)
            if ownership is not None:
                os.chown(current_path, ownership[0], ownership[1], follow_symlinks=False)
        for directory_name in directory_names:
            directory_path = current_path / directory_name
            if directory_path.is_symlink():
                continue
            directory_path.chmod(0o755)
            if ownership is not None:
                os.chown(directory_path, ownership[0], ownership[1], follow_symlinks=False)
        for file_name in file_names:
            file_path = current_path / file_name
            if file_path.is_symlink():
                continue
            file_path.chmod(0o644)
            if ownership is not None:
                os.chown(file_path, ownership[0], ownership[1], follow_symlinks=False)

    if ownership is not None:
        for directory in managed_parents:
            os.chown(directory, -1, ownership[1], follow_symlinks=False)
            directory.chmod(0o2770)


def _bundle_slug(profile: str, source_role: str, iso_path: str, kernel_path: str) -> str:
    if iso_path:
        stem = Path(iso_path).stem.strip()
    else:
        stem = Path(kernel_path).stem.strip()
    if not stem:
        stem = "source"
    return f"{profile}-{source_role}-{stem}"


def _bundle_grub_cfg(asset_root: str) -> str:
    return (
        "menuentry 'Graphical install' {\n"
        f"  linux /{asset_root}/vmlinuz vga=788 --- quiet\n"
        f"  initrd /{asset_root}/initrd.gz\n"
        "}\n"
        "menuentry 'Install' {\n"
        f"  linux /{asset_root}/vmlinuz ---\n"
        f"  initrd /{asset_root}/initrd.gz\n"
        "}\n"
        "menuentry 'Expert install' {\n"
        f"  linux /{asset_root}/vmlinuz ---\n"
        f"  initrd /{asset_root}/initrd.gz\n"
        "}\n"
        "menuentry 'Rescue mode' {\n"
        f"  linux /{asset_root}/vmlinuz rescue/enable=true ---\n"
        f"  initrd /{asset_root}/initrd.gz\n"
        "}\n"
    )


def _bundle_syslinux_cfg(asset_root: str) -> str:
    return (
        "label install\n"
        "  menu label Install\n"
        f"  linux /{asset_root}/vmlinuz\n"
        f"  append initrd=/{asset_root}/initrd.gz ---\n"
        "label installgui\n"
        "  menu label Graphical install\n"
        f"  linux /{asset_root}/vmlinuz\n"
        f"  append initrd=/{asset_root}/initrd.gz vga=788 quiet ---\n"
        "label expert\n"
        "  menu label Expert install\n"
        f"  linux /{asset_root}/vmlinuz\n"
        f"  append initrd=/{asset_root}/initrd.gz priority=low ---\n"
        "label rescue\n"
        "  menu label Rescue mode\n"
        f"  linux /{asset_root}/vmlinuz\n"
        f"  append initrd=/{asset_root}/initrd.gz rescue/enable=true ---\n"
    )



class _InstallerInitrdSession:
    """One extraction/repack across modules, overlay, preseed and exact ISO policy."""
    def __init__(self, initrd_path: Path) -> None:
        self.initrd_path = initrd_path
        self._temporary: Any = None
        self.root: Path | None = None
        self.changed = False

    def __enter__(self) -> "_InstallerInitrdSession":
        return self

    def tree(self) -> Path:
        if self.root is None:
            self._temporary = tempfile.TemporaryDirectory(prefix=".initrd-work-", dir=self.initrd_path.parent)
            self.root = Path(self._temporary.name)
            _extract_initrd_archive(self.initrd_path, self.root)
        return self.root

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        try:
            if exc_type is None and self.changed and self.root is not None:
                _repack_initrd_archive(self.root, self.initrd_path)
        finally:
            if self._temporary is not None:
                self._temporary.cleanup()

def prepare_managed_installer_source(
    profile: str,
    source_role: str,
    kernel_path: str,
    initrd_path: str,
    *,
    iso_path: str = "",
    output_dir: str = "",
    extra_initrd_modules: list[str] | None = None,
    module_source_strategy: str = "",
    initrd_preseed_path: str = "",
    initrd_overlay_dir: str = "",
    preflight_only: bool = False,
) -> dict[str, Any]:
    if source_role not in {"netinst", "netboot"}:
        raise ValueError(f"unsupported managed installer source role: {source_role}")
    if source_role == "netinst" and not str(iso_path or "").strip():
        raise ValueError("netinst managed source requires a separate installer ISO")
    _require_nonempty_regular_file(kernel_path, "kernel_path")
    _require_nonempty_regular_file(initrd_path, "initrd_path")
    if iso_path:
        _require_nonempty_regular_file(iso_path, "iso_path")
    payload_inspection: dict[str, object] = {}
    if source_role == "netinst":
        payload_inspection = validate_netinst_payload_iso(iso_path, profile)
    selected_extra_modules = _normalize_debian_netinst_extra_modules(profile, source_role, extra_initrd_modules)
    normalized_module_source_strategy = _normalize_module_source_strategy(
        profile, source_role, selected_extra_modules, module_source_strategy
    )
    resolved_preseed_path = Path(initrd_preseed_path).expanduser().resolve() if str(initrd_preseed_path or "").strip() else None
    if resolved_preseed_path is not None and not resolved_preseed_path.is_file():
        raise ValueError(f"initrd_preseed_path is not a regular file: {resolved_preseed_path}")
    resolved_overlay_dir = Path(initrd_overlay_dir).expanduser().resolve() if str(initrd_overlay_dir or "").strip() else None
    if resolved_overlay_dir is not None and not resolved_overlay_dir.is_dir():
        raise ValueError(f"initrd_overlay_dir is not a directory: {resolved_overlay_dir}")
    if source_role == "netboot" and iso_path:
        raise ValueError("netboot sources must not include an ISO payload")
    if preflight_only:
        if iso_path:
            _ensure_installer_assets_align_with_iso(
                iso_path=Path(iso_path).expanduser().resolve(),
                kernel_path=Path(kernel_path).expanduser().resolve(),
                initrd_path=Path(initrd_path).expanduser().resolve(),
            )
        return {
            "preflight_only": True,
            "aligned": True,
            "profile": profile,
            "source_role": source_role,
            "payload_media_class": str(payload_inspection.get("media_class") or ""),
            "iso_path": str(Path(iso_path).expanduser().resolve()) if iso_path else "",
            "kernel_path": str(Path(kernel_path).expanduser().resolve()),
            "initrd_path": str(Path(initrd_path).expanduser().resolve()),
            "initrd_preseed_path": str(resolved_preseed_path) if resolved_preseed_path is not None else "",
            "initrd_overlay_dir": str(resolved_overlay_dir) if resolved_overlay_dir is not None else "",
            "initrd_overlay": (
                {"overlay_dir": str(resolved_overlay_dir), "embedded_root": "/"}
                if resolved_overlay_dir is not None
                else {}
            ),
        }
    _status(f"Starting {profile} {source_role} source preparation.")
    bundle_root = Path(output_dir).expanduser().resolve() if output_dir else SOURCE_BUNDLE_ROOT / profile / source_role / (
        _bundle_slug(profile, source_role, iso_path, kernel_path) + "-" + dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    )
    if bundle_root == Path(bundle_root.anchor):
        raise ValueError(f"refusing unsafe managed installer bundle root: {bundle_root}")
    for selected in (kernel_path, initrd_path, iso_path, initrd_preseed_path, initrd_overlay_dir):
        if selected and Path(selected).expanduser().resolve().is_relative_to(bundle_root):
            raise ValueError("installer output must not contain any selected input")
    # This directory is a generated, role-scoped bundle. Remove only paths
    # owned by this helper so a bundle created by an older release cannot leave
    # /install*, /live, .disk, or a second payload ISO behind.
    for stale_path in (
        bundle_root / "install",
        bundle_root / "install.amd",
        bundle_root / "live",
        bundle_root / "casper",
        bundle_root / ".disk",
        bundle_root / "hd-media",
        bundle_root / "netboot",
        bundle_root / "payload",
        bundle_root / "boot",
        bundle_root / "isolinux",
        bundle_root / "EFI",
        bundle_root / "managed-installer-source.json",
        bundle_root / ".debian-usb" / "installer-profiles",
    ):
        if stale_path.is_dir() and not stale_path.is_symlink():
            shutil.rmtree(stale_path)
        elif stale_path.exists() or stale_path.is_symlink():
            stale_path.unlink()
    asset_root = "hd-media" if source_role == "netinst" else "netboot"
    asset_dir = bundle_root / asset_root
    boot_grub_dir = bundle_root / "boot" / "grub"
    isolinux_dir = bundle_root / "isolinux"
    efi_boot_dir = bundle_root / "EFI" / "boot"
    asset_dir.mkdir(parents=True, exist_ok=True)
    boot_grub_dir.mkdir(parents=True, exist_ok=True)
    isolinux_dir.mkdir(parents=True, exist_ok=True)
    efi_boot_dir.mkdir(parents=True, exist_ok=True)
    if iso_path:
        _ensure_installer_assets_align_with_iso(
            iso_path=Path(iso_path).expanduser().resolve(),
            kernel_path=Path(kernel_path).expanduser().resolve(),
            initrd_path=Path(initrd_path).expanduser().resolve(),
        )

    bundled_kernel = _copy_regular_file(kernel_path, asset_dir / "vmlinuz", "kernel_path")
    bundled_initrd = _copy_regular_file(initrd_path, asset_dir / "initrd.gz", "initrd_path")
    rebuild_manifest: dict[str, Any] | None = None
    overlay_manifest: dict[str, Any] | None = None
    preseed_manifest: dict[str, Any] | None = None
    iso_scan_selection_manifest: dict[str, Any] | None = None
    with _InstallerInitrdSession(Path(bundled_initrd)) as session:
        if profile in {"debian", "kali-linux"}:
            from .installer_preseed import inspect_preseed_loader
            _status("Checking installer preseed startup compatibility before module preparation.")
            try:
                native_loader = inspect_preseed_loader(session.tree())
            except (OSError, ValueError) as exc:
                raise ValueError(f"{profile} {source_role}: {exc}") from exc
            _status(f"Installer preseed loader: {native_loader.hook_guest} ({native_loader.origin}).")
        if selected_extra_modules:
            _status("Selected modules: " + ", ".join(selected_extra_modules))
            _status("Selected strategy: " + _module_source_strategy_label(normalized_module_source_strategy))
            rebuild_manifest = _rebuild_debian_netinst_initrd_modules(
                kernel_path=Path(bundled_kernel),
                initrd_path=Path(bundled_initrd),
                selected_options=selected_extra_modules,
                bundle_root=bundle_root,
                session=session,
                module_source_strategy=normalized_module_source_strategy,
            )
        else:
            _clear_existing_initrd_rebuild_manifest(bundle_root)
        if resolved_overlay_dir is not None and profile not in {"debian", "kali-linux"}:
            overlay_manifest = _embed_initrd_overlay_into_initrd(
                initrd_path=Path(bundled_initrd),
                overlay_dir=resolved_overlay_dir,
                bundle_root=bundle_root,
                session=session,
            )
        else:
            _clear_existing_initrd_overlay_manifest(bundle_root)
        if resolved_preseed_path is not None and profile not in {"debian", "kali-linux"}:
            preseed_manifest = _embed_repo_preseed_into_initrd(
                initrd_path=Path(bundled_initrd),
                preseed_path=resolved_preseed_path,
                bundle_root=bundle_root,
                session=session,
            )
        else:
            _clear_existing_initrd_preseed_manifest(bundle_root)
        _clear_existing_iso_scan_selection_manifest(bundle_root)
        if source_role == "netinst":
            iso_scan_selection_manifest = _enforce_exact_iso_scan_filename(
                initrd_path=Path(bundled_initrd),
                bundle_root=bundle_root,
                session=session,
            )
    installer_profiles: dict[str, Any] = {}
    if profile in {"debian", "kali-linux"}:
        from .installer_profiles import prepare_installer_profiles
        _status("Preparing isolated Desktop and Server installer archives.")
        installer_profiles = prepare_installer_profiles(
            bundle_root, profile, source_role,
            overlay_root=resolved_overlay_dir, desktop_preseed=resolved_preseed_path,
        )
        for flavor, prepared in installer_profiles.items():
            transport = prepared["preseed_transport"]
            _status(f"{flavor.title()} installer ready: {transport['hook']} "
                    f"({transport['origin']}, transport v{transport['version']}).")
        if resolved_overlay_dir is not None:
            overlay_manifest = {"profiles": installer_profiles, "overlay_dir": str(resolved_overlay_dir)}
    bundled_iso = ""
    if iso_path:
        payload_dir = bundle_root / "payload"
        bundled_iso = _copy_regular_file(iso_path, payload_dir / Path(iso_path).name, "iso_path")

    (boot_grub_dir / "grub.cfg").write_text(_bundle_grub_cfg(asset_root), encoding="utf-8")
    (isolinux_dir / "install.cfg").write_text(_bundle_syslinux_cfg(asset_root), encoding="utf-8")
    (efi_boot_dir / "bootx64.efi").write_bytes(b"")
    (efi_boot_dir / "grubx64.efi").write_bytes(b"")
    source_manifest = {
        "schema_version": 2,
        "installer_profiles": installer_profiles,
        "profile": profile,
        "source_role": source_role,
        "boot_method": asset_root,
        "kernel_path": f"/{asset_root}/vmlinuz",
        "initrd_path": f"/{asset_root}/initrd.gz",
        "iso_path": f"/payload/{Path(bundled_iso).name}" if bundled_iso else "",
        "payload_media_class": str(payload_inspection.get("media_class") or ""),
        "iso_scan_filename_enforced": bool(
            iso_scan_selection_manifest and iso_scan_selection_manifest.get("enforced")
        ),
        "iso_scan_selection_manifest": (
            "/.debian-usb/installer/iso-scan-selection-manifest.json"
            if iso_scan_selection_manifest
            else ""
        ),
        "initrd_overlay_included": bool(overlay_manifest),
        "initrd_overlay_manifest": (
            "/.debian-usb/installer-profiles/manifest.json" if installer_profiles else
            "/.debian-usb/installer/initrd-overlay-manifest.json" if overlay_manifest else ""
        ),
    }
    (bundle_root / "managed-installer-source.json").write_text(
        json.dumps(source_manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    _finalize_bundle_access(bundle_root)

    return {
        "profile": profile,
        "source_role": source_role,
        "source_path": str(bundle_root),
        "kernel_path": bundled_kernel,
        "initrd_path": bundled_initrd,
        "iso_path": bundled_iso,
        "manifest_path": str(bundle_root / "managed-installer-source.json"),
        "payload_media_class": str(payload_inspection.get("media_class") or ""),
        "selected_extra_modules": selected_extra_modules,
        "module_source_strategy": normalized_module_source_strategy,
        "initrd_rebuild": rebuild_manifest or {},
        "initrd_overlay": overlay_manifest or {},
        "initrd_preseed": preseed_manifest or {},
        "iso_scan_selection": iso_scan_selection_manifest or {},
    }


def _ensure_installer_assets_align_with_iso(*, iso_path: Path, kernel_path: Path, initrd_path: Path) -> None:
    try:
        iso_kernel_version, iso_initrd_version = _detect_installer_member_versions_from_iso(iso_path)
    except (FileNotFoundError, ValueError, RuntimeError):
        return
    selected_kernel_version = _detect_kernel_version_from_kernel_file(kernel_path)
    selected_initrd_version = _detect_kernel_version_from_file(initrd_path)
    mismatches: list[str] = []
    if iso_kernel_version and selected_kernel_version and iso_kernel_version != selected_kernel_version:
        mismatches.append(
            f"ISO /install.amd/vmlinuz={iso_kernel_version} != selected kernel={selected_kernel_version}"
        )
    if iso_initrd_version and selected_initrd_version and iso_initrd_version != selected_initrd_version:
        mismatches.append(
            f"ISO /install.amd/initrd.gz={iso_initrd_version} != selected initrd={selected_initrd_version}"
        )
    if mismatches:
        raise RuntimeError(
            "installer ISO and selected installer assets do not match; use an aligned ISO/kernel/initrd set: "
            + "; ".join(mismatches)
        )


def _detect_installer_member_versions_from_iso(iso_path: Path) -> tuple[str, str]:
    source = open_source(str(iso_path))
    with tempfile.TemporaryDirectory(prefix="debian-usb-installer-iso-") as temp_dir:
        temp_root = Path(temp_dir)
        extracted_kernel = temp_root / "vmlinuz"
        extracted_initrd = temp_root / "initrd.gz"
        source.extract_member("/install.amd/vmlinuz", extracted_kernel)
        source.extract_member("/install.amd/initrd.gz", extracted_initrd)
        return (
            _detect_kernel_version_from_kernel_file(extracted_kernel),
            _detect_kernel_version_from_file(extracted_initrd),
        )


def _normalize_debian_netinst_extra_modules(
    profile: str,
    source_role: str,
    selected_modules: list[str] | None,
) -> list[str]:
    normalized = _validate_optional_module_list(selected_modules, "extra_initrd_modules")
    if not normalized:
        return []
    if profile != "debian" or source_role != "netinst":
        raise ValueError("extra initrd modules are supported only for Debian netinst managed sources")
    invalid = [module for module in normalized if module not in DEBIAN_NETINST_EXTRA_MODULE_OPTIONS]
    if invalid:
        raise ValueError("unsupported Debian netinst initrd module selection: " + ", ".join(invalid))
    return normalized


def _normalize_module_source_strategy(
    profile: str,
    source_role: str,
    selected_modules: list[str],
    module_source_strategy: str,
) -> str:
    if not selected_modules:
        return ""
    if profile != "debian" or source_role != "netinst":
        return ""
    normalized = str(module_source_strategy or "").strip()
    if not normalized:
        return MODULE_SOURCE_STRATEGY_HOST_KERNEL
    if normalized not in {MODULE_SOURCE_STRATEGY_HOST_KERNEL, MODULE_SOURCE_STRATEGY_SOURCE_UDEB}:
        raise ValueError(f"unsupported Debian netinst module source strategy: {normalized}")
    return normalized


def _module_source_strategy_label(module_source_strategy: str) -> str:
    if module_source_strategy == MODULE_SOURCE_STRATEGY_SOURCE_UDEB:
        return "Build UDEBs From Source"
    return "Use Host Kernel"




def _rebuild_debian_netinst_initrd_modules(
    *,
    kernel_path: Path,
    initrd_path: Path,
    selected_options: list[str],
    bundle_root: Path,
    module_source_strategy: str,
    session: _InstallerInitrdSession | None = None,
) -> dict[str, Any]:
    _status_checklist(1, 6, "Tools", "Verifying the helper tools needed to inspect the installer initrd.")
    _ensure_installer_initrd_prepare_deps()
    metadata = _selected_debian_netinst_module_metadata(selected_options)
    run_id = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    workspace_dir = DEFAULT_WORK_DIR / "prepare-managed-installer-source" / run_id
    state_dir = DEFAULT_STATE_DIR / "prepare-managed-installer-source" / run_id
    log_dir = DEFAULT_LOG_DIR / "prepare-managed-installer-source"
    for path in (workspace_dir, state_dir, log_dir):
        path.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{run_id}.log"
    _status_checklist(2, 6, "ABI", f"Detecting the installer kernel ABI from {initrd_path}.")
    kernel_version = _detect_kernel_version_from_file(initrd_path)
    _status_checklist(3, 6, "Kernel Match", f"Inspecting the kernel image version from {kernel_path}.")
    kernel_image_version = _detect_kernel_version_from_kernel_file(kernel_path)
    result_manifest: dict[str, Any]

    with log_path.open("w", encoding="utf-8") as log_file:
        _log(log_file, f"Selected Debian netinst initrd module options: {json.dumps(selected_options)}")
        _log(log_file, f"Installer initrd path: {initrd_path}")
        _log(log_file, f"Installer kernel path: {kernel_path}")
        _log(log_file, f"Detected installer kernel version: {kernel_version}")
        _log(log_file, f"Detected installer kernel image version: {kernel_image_version or '<undetected>'}")
        if kernel_image_version and kernel_image_version != kernel_version:
            raise RuntimeError(
                "installer kernel and initrd versions do not match: "
                f"kernel={kernel_image_version}, initrd={kernel_version}"
            )
        _status_checklist(4, 6, "Support Check", f"Inspecting selected module support for installer ABI {kernel_version}.")
        inspection = _inspect_kernel_support(
            kernel_version=kernel_version,
            module_names=metadata["fallback_module_names"],
            module_alias_candidates=metadata["module_alias_candidates"],
            config_symbols=metadata["config_symbols"],
            module_tree_dir="",
            download_if_missing=module_source_strategy == MODULE_SOURCE_STRATEGY_HOST_KERNEL,
            log_file=log_file,
        )
        option_resolutions = _resolve_selected_option_support(selected_options, inspection)
        _status_checklist(4, 6, "Support Check", "Resolution: " + ", ".join(_render_option_resolution_lines(option_resolutions)))
        unresolved_options = [resolution["option"] for resolution in option_resolutions if resolution["mode"] == "missing"]
        copy_module_names = [module_name for resolution in option_resolutions if resolution["mode"] == "module" for module_name in resolution["module_names"]]
        builtin_options = [resolution["option"] for resolution in option_resolutions if resolution["mode"] == "builtin"]
        fallback_options = [resolution["option"] for resolution in option_resolutions if resolution["mode"] != "builtin"]
        copied_modules: list[str] = []
        udeb_manifest: dict[str, Any] = {}
        module_tree_dir = str(inspection.get("module_tree_dir") or "")
        changed = False
        rebuild_mode = "builtin-support"
        if module_source_strategy == MODULE_SOURCE_STRATEGY_SOURCE_UDEB and fallback_options:
            changed = True
            _status_checklist(5, 6, "Apply Modules", "Source rebuild selected; rebuilding matching Debian installer udebs for the selected non-builtin support.")
            ensure_debian_rebuild_deps()
            initrd_tree = session.tree() if session is not None else workspace_dir / "installer-initrd"
            initrd_tree.mkdir(parents=True, exist_ok=True)
            _status_checklist(5, 6, "Apply Modules", "Extracting installer initrd so rebuilt udebs can be injected.")
            if session is None:
                _extract_initrd_archive(initrd_path, initrd_tree)
            unresolved_metadata = _selected_debian_netinst_module_metadata(fallback_options)
            udeb_manifest = _build_installer_kernel_udebs(
                architecture=_infer_architecture_from_kernel_version(kernel_version),
                kernel_version=kernel_version,
                module_names=unresolved_metadata["fallback_module_names"],
                kernel_config_entries=unresolved_metadata["kernel_config_entries"],
                state_dir=state_dir / "installer-kernel-udebs",
                workspace_dir=workspace_dir / "installer-kernel-udebs",
                log_file=log_file,
            )
            _status_checklist(5, 6, "Apply Modules", "Injecting rebuilt installer udeb payloads into initrd.")
            _extract_udeb_payloads(Path(udeb_manifest["staged_udeb_dir"]), initrd_tree)
            rebuild_mode = "linux-source-udeb-rebuild"
            module_tree_dir = ""
            _status_checklist(5, 6, "Apply Modules", "Refreshing initrd module dependency metadata with depmod.")
            _run_logged(["depmod", "-b", str(initrd_tree), kernel_version], cwd=workspace_dir, log_file=log_file)
            _status_checklist(5, 6, "Apply Modules", "Repacking installer initrd.gz.")
            if session is None:
                _repack_initrd_archive(initrd_tree, initrd_path)
            else:
                session.changed = True
        elif module_source_strategy == MODULE_SOURCE_STRATEGY_HOST_KERNEL and unresolved_options:
            unresolved = ", ".join(unresolved_options)
            raise RuntimeError(
                "selected support is not available in the local or downloaded matching kernel packages "
                f"for installer ABI {kernel_version}: {unresolved}. Choose 'Build UDEBs From Source' to continue."
            )
        elif module_source_strategy == MODULE_SOURCE_STRATEGY_HOST_KERNEL and copy_module_names:
            changed = True
            _status_checklist(5, 6, "Apply Modules", "Copying required kernel modules and their dependency closure into the installer initrd.")
            _ensure_installer_initrd_module_copy_deps()
            initrd_tree = session.tree() if session is not None else workspace_dir / "installer-initrd"
            initrd_tree.mkdir(parents=True, exist_ok=True)
            _status_checklist(5, 6, "Apply Modules", "Extracting installer initrd for module copy.")
            if session is None:
                _extract_initrd_archive(initrd_path, initrd_tree)
            module_tree_root = _normalize_module_tree_root(Path(inspection["module_tree_dir"]), kernel_version)
            destination_root = _resolve_initrd_module_destination_root(initrd_tree, kernel_version)
            module_paths = []
            for module_name in copy_module_names:
                module_path = _resolve_kernel_module_path(module_tree_root, module_name, metadata["module_alias_candidates"])
                if module_path is None:
                    raise RuntimeError(f"could not resolve module path for {module_name} in {module_tree_root}")
                module_paths.append(module_path)
            copied_modules = _copy_module_dependency_closure(
                module_tree_root=module_tree_root,
                module_paths=module_paths,
                destination_root=destination_root,
            )
            copied_metadata = _copy_module_tree_metadata(module_tree_root, destination_root)
            rebuild_mode = "module-tree-copy"
            module_tree_dir = str(module_tree_root)
            _log(log_file, f"Copied module dependency closure: {json.dumps(copied_modules)}")
            _log(log_file, f"Copied module tree metadata: {json.dumps(copied_metadata)}")
            _status_checklist(5, 6, "Apply Modules", "Refreshing initrd module dependency metadata with depmod.")
            _run_logged(
                ["depmod", "-b", str(initrd_tree), "-m", _depmod_module_dir_argument(initrd_tree, destination_root), kernel_version],
                cwd=workspace_dir,
                log_file=log_file,
            )
            _status_checklist(5, 6, "Apply Modules", "Repacking installer initrd.gz.")
            if session is None:
                _repack_initrd_archive(initrd_tree, initrd_path)
            else:
                session.changed = True
        else:
            _status_checklist(5, 6, "Apply Modules", "Selected support is already built into the matched kernel; no initrd mutation is required.")
        result_manifest = {
            "run_id": run_id,
            "selected_options": selected_options,
            "module_source_strategy": module_source_strategy,
            "kernel_version": kernel_version,
            "kernel_image_version": kernel_image_version,
            "kernel_path": str(kernel_path),
            "initrd_path": str(initrd_path),
            "module_names": metadata["module_names"],
            "fallback_module_names": metadata["fallback_module_names"],
            "kernel_config_entries": metadata["kernel_config_entries"],
            "config_symbols": metadata["config_symbols"],
            "option_resolutions": option_resolutions,
            "builtin_options": builtin_options,
            "copied_modules": copied_modules,
            "changed": changed,
            "module_tree_dir": module_tree_dir,
            "inspection": inspection,
            "rebuild_mode": rebuild_mode,
            "udeb_rebuild_manifest_path": udeb_manifest.get("manifest_path", "") if udeb_manifest else "",
            "workspace_dir": str(workspace_dir),
            "state_dir": str(state_dir),
            "log_path": str(log_path),
        }

    manifest_dir = bundle_root / ".debian-usb" / "installer"
    manifest_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = manifest_dir / "initrd-module-manifest.json"
    manifest_path.write_text(json.dumps(result_manifest, indent=2) + "\n", encoding="utf-8")
    result_manifest["manifest_path"] = str(manifest_path)
    _status_checklist(6, 6, "Complete", f"Prepared source bundle manifest written to {manifest_path}.")
    return result_manifest


def _selected_debian_netinst_module_metadata(selected_options: list[str]) -> dict[str, list[str]]:
    requested_modules: list[str] = []
    fallback_modules: list[str] = []
    kernel_config_entries: list[str] = []
    alias_candidates: list[str] = []
    config_symbols: list[str] = []
    builtin_satisfies_symbols: list[str] = []
    for option_name in selected_options:
        metadata = DEBIAN_NETINST_EXTRA_MODULE_OPTIONS[option_name]
        for key, values in (
            ("module_names", requested_modules),
            ("fallback_module_names", fallback_modules),
            ("kernel_config_entries", kernel_config_entries),
            ("module_alias_candidates", alias_candidates),
            ("config_symbols", config_symbols),
            ("builtin_satisfies_symbols", builtin_satisfies_symbols),
        ):
            for value in metadata[key]:
                if value not in values:
                    values.append(value)
    return {
        "module_names": requested_modules,
        "fallback_module_names": fallback_modules,
        "kernel_config_entries": kernel_config_entries,
        "module_alias_candidates": alias_candidates,
        "config_symbols": config_symbols,
        "builtin_satisfies_symbols": builtin_satisfies_symbols,
    }


def _resolve_selected_option_support(selected_options: list[str], inspection: dict[str, Any]) -> list[dict[str, Any]]:
    module_rows = {row["name"]: row for row in inspection.get("modules", [])}
    config_rows = {row["symbol"]: row for row in inspection.get("config_symbols", [])}
    resolutions: list[dict[str, Any]] = []
    for option_name in selected_options:
        metadata = DEBIAN_NETINST_EXTRA_MODULE_OPTIONS[option_name]
        found_modules = [
            module_name
            for module_name in metadata["fallback_module_names"]
            if module_rows.get(module_name, {}).get("found")
        ]
        builtin_symbols = [
            symbol
            for symbol in metadata["builtin_satisfies_symbols"]
            if str(config_rows.get(symbol, {}).get("value", "")).strip().lower() == "y"
        ]
        mode = "missing"
        if found_modules:
            mode = "module"
        elif builtin_symbols:
            mode = "builtin"
        resolutions.append(
            {
                "option": option_name,
                "mode": mode,
                "module_names": found_modules if found_modules else list(metadata["fallback_module_names"]),
                "found_modules": found_modules,
                "builtin_symbols": builtin_symbols,
            }
        )
    return resolutions


def _detect_kernel_version_from_file(initrd_path: Path) -> str:
    result = subprocess.run(["lsinitramfs", str(initrd_path)], capture_output=True, text=True, encoding="utf-8", check=False)
    if result.returncode != 0:
        message = result.stderr.strip() or result.stdout.strip() or "lsinitramfs failed"
        raise RuntimeError(f"failed to inspect installer initrd {initrd_path}: {message}")
    matches: list[str] = []
    seen: set[str] = set()
    for line in result.stdout.splitlines():
        match = INITRD_KERNEL_VERSION_RE.search(line.strip())
        if not match:
            continue
        kernel_version = match.group(1)
        if kernel_version in seen:
            continue
        seen.add(kernel_version)
        matches.append(kernel_version)
    if not matches:
        raise RuntimeError(f"could not infer installer kernel version from initrd: {initrd_path}")
    return matches[0]


def _detect_kernel_version_from_kernel_file(kernel_path: Path) -> str:
    name_match = KERNEL_FILENAME_VERSION_RE.match(kernel_path.name)
    if name_match:
        return name_match.group(1).strip()
    if shutil.which("strings") is None:
        return ""
    result = subprocess.run(["strings", "-a", str(kernel_path)], capture_output=True, text=True, encoding="utf-8", check=False)
    if result.returncode != 0:
        return ""
    for line in result.stdout.splitlines():
        match = KERNEL_VERSION_TEXT_RE.search(line)
        if match:
            return match.group(1).strip()
    return ""


def _infer_architecture_from_kernel_version(kernel_version: str) -> str:
    lowered = kernel_version.lower()
    if lowered.endswith("-amd64"):
        return "amd64"
    if lowered.endswith("-arm64"):
        return "arm64"
    raise RuntimeError(f"could not infer installer architecture from kernel version: {kernel_version}")


def _normalize_module_tree_root(module_tree_dir: Path, kernel_version: str) -> Path:
    if module_tree_dir.name == "kernel" and module_tree_dir.parent.name == kernel_version:
        return module_tree_dir.parent
    if (module_tree_dir / "kernel").is_dir():
        return module_tree_dir
    raise RuntimeError(f"module tree does not contain a kernel directory for {kernel_version}: {module_tree_dir}")


def _resolve_initrd_module_destination_root(initrd_tree: Path, kernel_version: str) -> Path:
    usr_root = initrd_tree / "usr" / "lib" / "modules" / kernel_version
    if usr_root.exists() or (initrd_tree / "lib").is_symlink():
        usr_root.mkdir(parents=True, exist_ok=True)
        return usr_root
    lib_root = initrd_tree / "lib" / "modules" / kernel_version
    lib_root.mkdir(parents=True, exist_ok=True)
    return lib_root


def _copy_module_dependency_closure(
    *,
    module_tree_root: Path,
    module_paths: list[Path],
    destination_root: Path,
) -> list[str]:
    dependency_index = _load_modules_dep_index(module_tree_root)
    relative_paths = [path.relative_to(module_tree_root).as_posix() for path in module_paths]
    closure: list[str] = []
    seen: set[str] = set()
    pending = list(relative_paths)
    while pending:
        relative = pending.pop(0)
        if relative in seen:
            continue
        seen.add(relative)
        closure.append(relative)
        pending.extend(dependency_index.get(relative, []))
    copied: list[str] = []
    for relative in closure:
        source_path = module_tree_root / relative
        if not source_path.is_file():
            continue
        destination_path = destination_root / relative
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_path, destination_path)
        copied.append(relative)
    return copied


def _copy_module_tree_metadata(module_tree_root: Path, destination_root: Path) -> list[str]:
    metadata_names = [
        "modules.order",
        "modules.builtin",
        "modules.builtin.modinfo",
        "modules.builtin.bin",
        "modules.builtin.alias.bin",
    ]
    copied: list[str] = []
    for name in metadata_names:
        source_path = module_tree_root / name
        if not source_path.is_file():
            continue
        destination_path = destination_root / name
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_path, destination_path)
        copied.append(name)
    return copied


def _depmod_module_dir_argument(initrd_tree: Path, destination_root: Path) -> str:
    module_dir = destination_root.parent
    relative = module_dir.relative_to(initrd_tree).as_posix()
    return "/" + relative.lstrip("/")


def _load_modules_dep_index(module_tree_root: Path) -> dict[str, list[str]]:
    modules_dep_path = module_tree_root / "modules.dep"
    if not modules_dep_path.is_file():
        return {}
    index: dict[str, list[str]] = {}
    for raw_line in modules_dep_path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw_line.strip()
        if not line or ":" not in line:
            continue
        module_path, _, dependency_list = line.partition(":")
        dependencies = [item for item in dependency_list.strip().split() if item]
        index[module_path.strip()] = dependencies
    return index


def _clear_existing_initrd_rebuild_manifest(bundle_root: Path) -> None:
    manifest_dir = bundle_root / ".debian-usb" / "installer"
    manifest_path = manifest_dir / "initrd-module-manifest.json"
    if manifest_path.exists():
        manifest_path.unlink()
    if manifest_dir.is_dir() and not any(manifest_dir.iterdir()):
        manifest_dir.rmdir()


def _clear_existing_initrd_overlay_manifest(bundle_root: Path) -> None:
    manifest_dir = bundle_root / ".debian-usb" / "installer"
    manifest_path = manifest_dir / "initrd-overlay-manifest.json"
    if manifest_path.exists():
        manifest_path.unlink()
    if manifest_dir.is_dir() and not any(manifest_dir.iterdir()):
        manifest_dir.rmdir()


def _clear_existing_initrd_preseed_manifest(bundle_root: Path) -> None:
    manifest_dir = bundle_root / ".debian-usb" / "installer"
    manifest_path = manifest_dir / "initrd-preseed-manifest.json"
    if manifest_path.exists():
        manifest_path.unlink()
    if manifest_dir.is_dir() and not any(manifest_dir.iterdir()):
        manifest_dir.rmdir()


def _clear_existing_iso_scan_selection_manifest(bundle_root: Path) -> None:
    manifest_dir = bundle_root / ".debian-usb" / "installer"
    manifest_path = manifest_dir / "iso-scan-selection-manifest.json"
    if manifest_path.exists():
        manifest_path.unlink()
    if manifest_dir.is_dir() and not any(manifest_dir.iterdir()):
        manifest_dir.rmdir()


def _enforce_exact_iso_scan_filename(
    *,
    initrd_path: Path,
    bundle_root: Path,
    session: _InstallerInitrdSession | None = None,
) -> dict[str, Any]:
    if not initrd_path.is_file() or initrd_path.stat().st_size <= 0:
        raise ValueError(f"installer initrd is not a non-empty regular file: {initrd_path}")
    _status("Enforcing exact iso-scan/filename selection in the copied hd-media initrd.gz.")
    _ensure_installer_initrd_exact_iso_deps()
    run_id = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    workspace_dir = DEFAULT_WORK_DIR / "prepare-managed-installer-source" / f"{run_id}-iso-scan"
    state_dir = DEFAULT_STATE_DIR / "prepare-managed-installer-source" / f"{run_id}-iso-scan"
    log_dir = DEFAULT_LOG_DIR / "prepare-managed-installer-source"
    for path in (workspace_dir, state_dir, log_dir):
        path.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{run_id}-iso-scan.log"
    initrd_tree = session.tree() if session is not None else workspace_dir / "installer-initrd"
    initrd_tree.mkdir(parents=True, exist_ok=True)
    if session is None:
        _extract_initrd_archive(initrd_path, initrd_tree)
    postinst_path = initrd_tree / "var" / "lib" / "dpkg" / "info" / "iso-scan.postinst"
    resolved_tree = initrd_tree.resolve()
    resolved_postinst = postinst_path.resolve()
    if (
        not postinst_path.is_file()
        or postinst_path.is_symlink()
        or not resolved_postinst.is_relative_to(resolved_tree)
    ):
        raise RuntimeError(
            "cannot enforce exact iso-scan/filename selection: installer initrd does not contain "
            "a regular /var/lib/dpkg/info/iso-scan.postinst"
        )

    original_mode = postinst_path.stat().st_mode & 0o7777
    original_text = postinst_path.read_text(encoding="utf-8")
    patched_text = _patch_iso_scan_postinst_text(original_text)
    changed = patched_text != original_text
    if changed:
        postinst_path.write_text(patched_text, encoding="utf-8")
        postinst_path.chmod(original_mode)

    syntax_check = subprocess.run(
        ["sh", "-n", str(postinst_path)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    if syntax_check.returncode != 0:
        message = syntax_check.stderr.strip() or syntax_check.stdout.strip() or "sh -n failed"
        raise RuntimeError(f"patched iso-scan.postinst failed shell syntax validation: {message}")
    if ISO_SCAN_EXACT_SELECTION_BLOCK.strip("\n") not in patched_text:
        raise RuntimeError("exact iso-scan/filename selection block was not present after patching")
    if changed:
        if session is None:
            _repack_initrd_archive(initrd_tree, initrd_path)
        else:
            session.changed = True

    result_manifest = {
        "schema_version": 1,
        "run_id": run_id,
        "enforced": True,
        "changed": changed,
        "initrd_path": str(initrd_path),
        "postinst_path": "/var/lib/dpkg/info/iso-scan.postinst",
        "postinst_mode": f"{original_mode:04o}",
        "marker": ISO_SCAN_EXACT_SELECTION_MARKER,
        "policy": "exact-request-or-fail-without-scan",
        "workspace_dir": str(workspace_dir),
        "state_dir": str(state_dir),
        "log_path": str(log_path),
    }
    with log_path.open("w", encoding="utf-8") as log_file:
        _log(log_file, f"Installer initrd path: {initrd_path}")
        _log(log_file, f"Patched postinst path: {postinst_path}")
        _log(log_file, f"Exact ISO selection block changed: {changed}")
        _log(log_file, "Exact ISO selection policy: exact-request-or-fail-without-scan")
    manifest_dir = bundle_root / ".debian-usb" / "installer"
    manifest_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = manifest_dir / "iso-scan-selection-manifest.json"
    manifest_path.write_text(json.dumps(result_manifest, indent=2) + "\n", encoding="utf-8")
    result_manifest["manifest_path"] = str(manifest_path)
    return result_manifest


def _embed_initrd_overlay_into_initrd(
    *,
    initrd_path: Path,
    overlay_dir: Path,
    bundle_root: Path,
    session: _InstallerInitrdSession | None = None,
) -> dict[str, Any]:
    if not initrd_path.is_file() or initrd_path.stat().st_size <= 0:
        raise ValueError(f"installer initrd is not a non-empty regular file: {initrd_path}")
    _status(f"Merging {overlay_dir} into the root of {initrd_path.name}.")
    _ensure_installer_initrd_overlay_deps()
    run_id = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    workspace_dir = DEFAULT_WORK_DIR / "prepare-managed-installer-source" / f"{run_id}-overlay"
    state_dir = DEFAULT_STATE_DIR / "prepare-managed-installer-source" / f"{run_id}-overlay"
    log_dir = DEFAULT_LOG_DIR / "prepare-managed-installer-source"
    for managed_path in (workspace_dir, state_dir, log_dir):
        managed_path.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{run_id}-overlay.log"
    initrd_tree = session.tree() if session is not None else workspace_dir / "installer-initrd"
    initrd_tree.mkdir(parents=True, exist_ok=True)
    if session is None:
        _extract_initrd_archive(initrd_path, initrd_tree)
    merged = merge_initrd_overlay(overlay_dir, initrd_tree)
    if session is None:
        _repack_initrd_archive(initrd_tree, initrd_path)
    else:
        session.changed = True
    result_manifest = {
        "schema_version": 1,
        "run_id": run_id,
        "initrd_path": str(initrd_path),
        "overlay_dir": merged["overlay_dir"],
        "embedded_root": merged["embedded_root"],
        "workspace_dir": str(workspace_dir),
        "state_dir": str(state_dir),
        "log_path": str(log_path),
    }
    with log_path.open("w", encoding="utf-8") as log_file:
        _log(log_file, f"Installer initrd path: {initrd_path}")
        _log(log_file, f"Initrd overlay directory: {overlay_dir}")
        _log(log_file, "Merged all selected overlay content at the initrd root.")
    manifest_dir = bundle_root / ".debian-usb" / "installer"
    manifest_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = manifest_dir / "initrd-overlay-manifest.json"
    manifest_path.write_text(json.dumps(result_manifest, indent=2) + "\n", encoding="utf-8")
    result_manifest["manifest_path"] = str(manifest_path)
    return result_manifest


def _embed_repo_preseed_into_initrd(
    *,
    initrd_path: Path,
    preseed_path: Path,
    bundle_root: Path,
    session: _InstallerInitrdSession | None = None,
) -> dict[str, Any]:
    if not preseed_path.is_file():
        raise ValueError(f"initrd_preseed_path is not a regular file: {preseed_path}")
    _status_checklist(1, 4, "Preseed", f"Embedding repo preseed {preseed_path} into {initrd_path.name}.")
    _ensure_installer_initrd_preseed_copy_deps()
    run_id = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    workspace_dir = DEFAULT_WORK_DIR / "prepare-managed-installer-source" / f"{run_id}-preseed"
    state_dir = DEFAULT_STATE_DIR / "prepare-managed-installer-source" / f"{run_id}-preseed"
    log_dir = DEFAULT_LOG_DIR / "prepare-managed-installer-source"
    for path in (workspace_dir, state_dir, log_dir):
        path.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{run_id}-preseed.log"
    with log_path.open("w", encoding="utf-8") as log_file:
        _log(log_file, f"Embedded repo preseed path: {preseed_path}")
        _log(log_file, f"Installer initrd path: {initrd_path}")
        initrd_tree = session.tree() if session is not None else workspace_dir / "installer-initrd"
        initrd_tree.mkdir(parents=True, exist_ok=True)
        _status_checklist(2, 4, "Preseed", "Extracting installer initrd so /preseed.cfg can be updated.")
        if session is None:
            _extract_initrd_archive(initrd_path, initrd_tree)
        shutil.copy2(preseed_path, initrd_tree / "preseed.cfg")
        _status_checklist(3, 4, "Preseed", "Repacking installer initrd.gz with embedded /preseed.cfg.")
        if session is None:
            _repack_initrd_archive(initrd_tree, initrd_path)
        else:
            session.changed = True
        result_manifest = {
            "run_id": run_id,
            "initrd_path": str(initrd_path),
            "preseed_path": str(preseed_path),
            "embedded_path": "/preseed.cfg",
            "workspace_dir": str(workspace_dir),
            "state_dir": str(state_dir),
            "log_path": str(log_path),
        }
    manifest_dir = bundle_root / ".debian-usb" / "installer"
    manifest_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = manifest_dir / "initrd-preseed-manifest.json"
    manifest_path.write_text(json.dumps(result_manifest, indent=2) + "\n", encoding="utf-8")
    result_manifest["manifest_path"] = str(manifest_path)
    _status_checklist(4, 4, "Complete", f"Embedded initrd preseed manifest written to {manifest_path}.")
    return result_manifest


def _ensure_installer_initrd_module_copy_deps() -> None:
    missing = [command for command in INSTALLER_INITRD_MODULE_COPY_REQUIRED_COMMANDS if shutil.which(command) is None]
    if not missing:
        return
    _status_checklist(5, 6, "Module Copy", "Installing missing tools for module copy: " + ", ".join(missing))
    _run_simple(["apt-get", "update"])
    _run_simple(["apt-get", "install", "-y", "--no-install-recommends", *INSTALLER_INITRD_MODULE_COPY_APT_DEPS])
    still_missing = [command for command in INSTALLER_INITRD_MODULE_COPY_REQUIRED_COMMANDS if shutil.which(command) is None]
    if still_missing:
        raise RuntimeError(
            "installer initrd module preparation finished but commands are still missing: " + ", ".join(still_missing)
        )


def _ensure_installer_initrd_prepare_deps() -> None:
    missing = [command for command in INSTALLER_INITRD_PREPARE_REQUIRED_COMMANDS if shutil.which(command) is None]
    if not missing:
        return
    _status_checklist(1, 6, "Tools", "Installing missing initrd inspection tools: " + ", ".join(missing))
    _run_simple(["apt-get", "update"])
    _run_simple(["apt-get", "install", "-y", "--no-install-recommends", *INSTALLER_INITRD_PREPARE_APT_DEPS])
    still_missing = [command for command in INSTALLER_INITRD_PREPARE_REQUIRED_COMMANDS if shutil.which(command) is None]
    if still_missing:
        raise RuntimeError(
            "installer initrd inspection finished but commands are still missing: " + ", ".join(still_missing)
        )


def _ensure_installer_initrd_overlay_deps() -> None:
    missing = [command for command in INSTALLER_INITRD_OVERLAY_REQUIRED_COMMANDS if shutil.which(command) is None]
    if not missing:
        return
    _status("Installing missing initrd overlay tools: " + ", ".join(missing))
    _run_simple(["apt-get", "update"])
    _run_simple(["apt-get", "install", "-y", "--no-install-recommends", *INSTALLER_INITRD_OVERLAY_APT_DEPS])
    still_missing = [command for command in INSTALLER_INITRD_OVERLAY_REQUIRED_COMMANDS if shutil.which(command) is None]
    if still_missing:
        raise RuntimeError(
            "installer initrd overlay preparation finished but commands are still missing: "
            + ", ".join(still_missing)
        )


def _ensure_installer_initrd_preseed_copy_deps() -> None:
    missing = [command for command in INSTALLER_INITRD_PRESEED_COPY_REQUIRED_COMMANDS if shutil.which(command) is None]
    if not missing:
        return
    _status_checklist(1, 4, "Preseed", "Installing missing initrd embed tools: " + ", ".join(missing))
    _run_simple(["apt-get", "update"])
    _run_simple(["apt-get", "install", "-y", "--no-install-recommends", *INSTALLER_INITRD_PRESEED_COPY_APT_DEPS])
    still_missing = [command for command in INSTALLER_INITRD_PRESEED_COPY_REQUIRED_COMMANDS if shutil.which(command) is None]
    if still_missing:
        raise RuntimeError(
            "installer initrd preseed embedding finished but commands are still missing: " + ", ".join(still_missing)
        )


def _ensure_installer_initrd_exact_iso_deps() -> None:
    missing = [command for command in INSTALLER_INITRD_EXACT_ISO_REQUIRED_COMMANDS if shutil.which(command) is None]
    if not missing:
        return
    _status("Installing missing tools for exact Netinst ISO selection: " + ", ".join(missing))
    _run_simple(["apt-get", "update"])
    _run_simple(
        ["apt-get", "install", "-y", "--no-install-recommends", *INSTALLER_INITRD_EXACT_ISO_APT_DEPS]
    )
    still_missing = [
        command for command in INSTALLER_INITRD_EXACT_ISO_REQUIRED_COMMANDS if shutil.which(command) is None
    ]
    if still_missing:
        raise RuntimeError(
            "exact Netinst ISO selection preparation finished but commands are still missing: "
            + ", ".join(still_missing)
        )


def _render_option_resolution_lines(option_resolutions: list[dict[str, Any]]) -> list[str]:
    lines: list[str] = []
    for resolution in option_resolutions:
        option = resolution["option"]
        mode = resolution["mode"]
        if mode == "builtin":
            builtins = ", ".join(resolution.get("builtin_symbols", [])) or "kernel config"
            lines.append(f"{option}=builtin ({builtins})")
        elif mode == "module":
            modules = ", ".join(resolution.get("found_modules", [])) or "module"
            lines.append(f"{option}=module ({modules})")
        else:
            lines.append(f"{option}=rebuild-needed")
    return lines


def _status_checklist(step: int, total: int, title: str, message: str) -> None:
    _status(f"Checklist {step}/{total} | {title}: {message}")


def _status(message: str) -> None:
    sys.stderr.write(f"[prepare-managed-installer-source] {message.rstrip()}\n")
    sys.stderr.flush()
