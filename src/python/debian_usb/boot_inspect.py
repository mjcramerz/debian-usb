from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
import shutil
import struct
import subprocess

from .boot_parse import BootEntry, _find_boot_entries, _media_class, _select_entry, _select_installer_entry
from .catalog import profile_for
from .config import effective_managed_payload_layout, load_config, load_template_config
from .constants import MANAGED_PAYLOAD_LAYOUT_ISO_STORE, PROFILE_DEBIAN, PROFILE_KALI_LINUX, PROFILE_TAILS
from .iso_source import MediaSource, _normalize_member_path, open_source
from .workdir import temporary_work_dir


@dataclass(frozen=True)
class MediaInspection:
    source_path: str
    source_type: str
    volume_id: str
    media_class: str
    firmware: tuple[str, ...]
    managed_supported: bool
    supports_persistence: bool
    supports_encrypted_persistence: bool
    managed_payload_layout: str
    top_level_entries: tuple[str, ...]
    best_live_title: str
    best_installer_title: str
    warnings: tuple[str, ...]
    entries: tuple[BootEntry, ...]


ISOInspection = MediaInspection


_NETINST_FORBIDDEN_MEDIA_PREFIXES = ("/live/", "/casper/")
_NETINST_INSTALLER_ENTRY_KINDS = {"installer", "automated-installer", "expert-installer", "rescue"}
ISO_SCAN_EXACT_SELECTION_MARKER = "# BEGIN debian-usb exact iso-scan/filename selection"
_ISO_SCAN_POSTINST_MEMBER = "var/lib/dpkg/info/iso-scan.postinst"
_MAX_ISO_SCAN_POSTINST_BYTES = 1024 * 1024
_ISO_SCAN_FIRST_PASS_CALL_RE = re.compile(
    r'^[ \t]*scan_device_for_isos[ \t]+0[ \t]+(?:"\$(?:selected_devices|\{selected_devices\})"|\$(?:selected_devices|\{selected_devices\}))[ \t]*$',
    re.MULTILINE,
)


def validate_prepared_netinst_initrd(initrd_path: str | Path) -> dict[str, object]:
    candidate = Path(initrd_path).expanduser().resolve()
    if not candidate.is_file() or candidate.stat().st_size <= 0:
        raise ValueError(f"prepared netinst source requires a non-empty hd-media/initrd.gz: {candidate}")
    missing_commands = [command for command in ("cpio", "gzip", "sh") if shutil.which(command) is None]
    if missing_commands:
        raise RuntimeError(
            "cannot validate prepared netinst hd-media initrd; missing commands: " + ", ".join(missing_commands)
        )

    decompressor = subprocess.Popen(
        ["gzip", "-dc", "--", str(candidate)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert decompressor.stdout is not None
    extractor = subprocess.Popen(
        ["cpio", "-i", "--to-stdout", "--quiet", _ISO_SCAN_POSTINST_MEMBER],
        stdin=decompressor.stdout,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    decompressor.stdout.close()
    postinst_bytes, extractor_stderr = extractor.communicate()
    decompressor_stderr = decompressor.stderr.read() if decompressor.stderr is not None else b""
    if decompressor.stderr is not None:
        decompressor.stderr.close()
    decompressor_returncode = decompressor.wait()
    if decompressor_returncode != 0:
        message = decompressor_stderr.decode(errors="replace").strip() or "gzip failed"
        raise ValueError(f"unable to decompress prepared netinst hd-media initrd: {candidate}: {message}")
    if extractor.returncode != 0:
        message = extractor_stderr.decode(errors="replace").strip() or "cpio failed"
        raise ValueError(f"unable to read iso-scan.postinst from prepared netinst hd-media initrd: {candidate}: {message}")
    if not postinst_bytes:
        raise ValueError(
            f"prepared netinst hd-media initrd does not contain /{_ISO_SCAN_POSTINST_MEMBER}: {candidate}"
        )
    if len(postinst_bytes) > _MAX_ISO_SCAN_POSTINST_BYTES:
        raise ValueError(
            f"prepared netinst iso-scan.postinst exceeds {_MAX_ISO_SCAN_POSTINST_BYTES} bytes: {candidate}"
        )
    try:
        postinst = postinst_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"prepared netinst iso-scan.postinst is not UTF-8 text: {candidate}") from exc

    required_fragments = (
        ISO_SCAN_EXACT_SELECTION_MARKER,
        'db_get iso-scan/filename',
        'if [ -n "$requested_iso" ]; then',
        'if [ ! -f "/hd-media/${requested_iso#/}" ]; then',
        'use_this_iso "$requested_iso" "$selected_devices"',
        'exit 1',
    )
    missing_fragments = [fragment for fragment in required_fragments if fragment not in postinst]
    if missing_fragments or postinst.count(ISO_SCAN_EXACT_SELECTION_MARKER) != 1:
        raise ValueError(
            "prepared netinst hd-media initrd does not enforce exact iso-scan/filename selection; "
            "regenerate the managed Netinst source bundle"
        )
    exact_use_offset = postinst.index('use_this_iso "$requested_iso" "$selected_devices"')
    fallback_scans = list(_ISO_SCAN_FIRST_PASS_CALL_RE.finditer(postinst))
    if len(fallback_scans) != 1 or exact_use_offset >= fallback_scans[0].start():
        raise ValueError(
            "prepared netinst hd-media initrd does not enforce exact ISO selection before the fallback scan; "
            "regenerate the managed Netinst source bundle"
        )
    syntax_check = subprocess.run(
        ["sh", "-n"],
        input=postinst,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    if syntax_check.returncode != 0:
        raise ValueError(
            "prepared netinst iso-scan.postinst fails shell syntax validation; "
            "regenerate the managed Netinst source bundle"
        )
    return {
        "initrd_path": str(candidate),
        "enforced": True,
        "marker": ISO_SCAN_EXACT_SELECTION_MARKER,
        "postinst_bytes": len(postinst_bytes),
    }


def validate_netinst_payload_iso(source_path: str, profile: str = "") -> dict[str, object]:
    candidate = Path(source_path).expanduser().resolve()
    if not candidate.is_file() or candidate.suffix.lower() != ".iso":
        raise ValueError(f"netinst payload must be a regular .iso file: {candidate}")
    if candidate.stat().st_size <= 0:
        raise ValueError(f"netinst payload ISO is empty: {candidate}")

    source = open_source(str(candidate))
    files = source.list_files()
    lowered_files = {path.lower() for path in files}
    forbidden_paths = sorted(
        path
        for path in lowered_files
        if path.startswith(_NETINST_FORBIDDEN_MEDIA_PREFIXES)
        or "/live-installer_" in path
        or path.endswith("/live-installer.udeb")
    )
    if forbidden_paths:
        raise ValueError(
            "netinst payload must be installer-only and must not contain Live/Casper media or live-installer udebs: "
            f"{candidate}: {forbidden_paths[0]}"
        )

    entries = _find_boot_entries(source)
    media_class = _media_class(entries)
    if media_class != "installer":
        raise ValueError(
            "netinst payload must classify as installer-only; Live and hybrid ISOs are forbidden: "
            f"{candidate} ({media_class or 'unknown'})"
        )
    if not any(entry.kind in _NETINST_INSTALLER_ENTRY_KINDS for entry in entries):
        raise ValueError(f"netinst payload does not expose Debian Installer boot entries: {candidate}")
    for entry in entries:
        lowered_args = entry.kernel_args.lower()
        lowered_kernel = entry.kernel_path.lower()
        if "boot=live" in lowered_args or "boot=casper" in lowered_args or lowered_kernel.startswith(_NETINST_FORBIDDEN_MEDIA_PREFIXES):
            raise ValueError(f"netinst payload contains a forbidden Live boot entry: {candidate}: {entry.title}")

    return {
        "iso_path": str(candidate),
        "media_class": media_class,
        "installer_entry_count": sum(entry.kind in _NETINST_INSTALLER_ENTRY_KINDS for entry in entries),
        "live_entry_count": 0,
    }


def _validate_prepared_netinst_payload(source: MediaSource, profile: str) -> None:
    if source.source_type != "directory":
        validate_netinst_payload_iso(str(source.source_path), profile)
        return
    validate_prepared_netinst_initrd(source.source_path / "hd-media" / "initrd.gz")
    payload_dir = source.source_path / "payload"
    payload_isos = sorted(payload_dir.glob("*.iso")) if payload_dir.is_dir() else []
    if len(payload_isos) != 1:
        raise ValueError(f"prepared netinst source must contain exactly one payload/*.iso: {source.display_path}")
    validate_netinst_payload_iso(str(payload_isos[0]), profile)


def _detect_firmware(source: MediaSource) -> tuple[str, ...]:
    firmware: list[str] = []
    if source.has_member_prefix("/EFI"):
        firmware.append("uefi")
    return tuple(firmware)


def _read_source_text_if_exists(source: MediaSource, member_path: str) -> str:
    normalized = _normalize_member_path(member_path)
    if not normalized or not source.exists(normalized):
        return ""
    return source.read_text(normalized)


def _find_live_initrd_path(entries: list[BootEntry]) -> str:
    for entry in entries:
        if entry.kind.startswith("live") and entry.initrd_path:
            return entry.initrd_path
    return ""


def _host_secure_boot_enabled() -> bool:
    efivars = Path("/sys/firmware/efi/efivars")
    if not efivars.is_dir():
        return False
    for candidate in efivars.glob("SecureBoot-*"):
        try:
            payload = candidate.read_bytes()
        except OSError:
            continue
        if len(payload) >= 5:
            return payload[4] == 1
    return False


def _find_live_kernel_path(entries: list[BootEntry]) -> str:
    for entry in entries:
        if entry.kind.startswith("live") and entry.kernel_path:
            return entry.kernel_path
    return ""


def _pe_security_directory_size(path: Path) -> int:
    try:
        header = path.read_bytes()[:16384]
    except OSError:
        return 0
    if len(header) < 0x40 or header[:2] != b"MZ":
        return 0
    pe_offset = struct.unpack_from("<I", header, 0x3C)[0]
    if pe_offset + 0x80 > len(header) or header[pe_offset : pe_offset + 4] != b"PE\x00\x00":
        return 0
    optional_offset = pe_offset + 24
    magic = struct.unpack_from("<H", header, optional_offset)[0]
    if magic == 0x20B:
        data_dir_offset = optional_offset + 112
    elif magic == 0x10B:
        data_dir_offset = optional_offset + 96
    else:
        return 0
    security_entry_offset = data_dir_offset + (8 * 4)
    if security_entry_offset + 8 > len(header):
        return 0
    _, security_size = struct.unpack_from("<II", header, security_entry_offset)
    return security_size


def _kernel_has_pe_signature_directory(source: MediaSource, kernel_path: str) -> bool:
    normalized = _normalize_member_path(kernel_path)
    if not normalized:
        return False
    with temporary_work_dir("inspect-kernel-") as temp_dir:
        kernel_file = Path(temp_dir) / Path(normalized).name
        source.extract_member(normalized, kernel_file)
        return _pe_security_directory_size(kernel_file) > 0


def _initrd_supports_cryptsetup(source: MediaSource, initrd_path: str) -> bool:
    normalized = _normalize_member_path(initrd_path)
    if not normalized:
        return False
    with temporary_work_dir("inspect-initrd-") as temp_dir:
        initrd_file = Path(temp_dir) / Path(normalized).name
        source.extract_member(normalized, initrd_file)
        result = subprocess.run(["lsinitramfs", str(initrd_file)], capture_output=True, text=True, encoding="utf-8", check=False)
        if result.returncode != 0:
            return False
        listing = result.stdout.lower()
    return all(token in listing for token in ("cryptroot", "dm-crypt")) and "cryptsetup" in listing


def _supports_encrypted_persistence(
    source: MediaSource,
    entries: list[BootEntry],
    profile: str,
    selected_profile: object | None,
) -> bool:
    if not selected_profile or not getattr(selected_profile, "supports_persistence", False):
        return False
    if profile not in {PROFILE_DEBIAN, PROFILE_KALI_LINUX, PROFILE_TAILS}:
        return False
    if profile == PROFILE_TAILS and source.exists("/live/Tails.module"):
        return True
    initrd_path = _find_live_initrd_path(entries)
    initrd_supported = _initrd_supports_cryptsetup(source, initrd_path)
    if any(entry.kind == "live-encrypted-persistence" for entry in entries):
        return initrd_supported
    package_lists = [
        _read_source_text_if_exists(source, "/live/filesystem.packages"),
        _read_source_text_if_exists(source, "/casper/filesystem.manifest"),
    ]
    combined = "\n".join(text for text in package_lists if text)
    if not combined:
        return False
    lowered = combined.lower()
    return "cryptsetup" in lowered and "cryptsetup-initramfs" in lowered and initrd_supported


def inspect_media(
    source_path: str,
    profile: str = "",
    config_path: str = "",
    use_custom_menu: bool = False,
    source_role: str = "primary",
) -> dict[str, object]:
    source = open_source(source_path)
    if source_role == "netinst":
        _validate_prepared_netinst_payload(source, profile)
    entries = _find_boot_entries(source)
    media_class = _media_class(entries)
    top_level_entries: list[str] = []
    seen_labels: set[str] = set()
    for entry in sorted(entries, key=lambda item: item.order):
        label = entry.menu_path[0] if entry.menu_path else entry.title
        if not label or label in seen_labels:
            continue
        seen_labels.add(label)
        top_level_entries.append(label)

    warnings: list[str] = []
    if media_class == "utility":
        warnings.append("No Linux boot entries were detected in the media bootloader configs.")
    firmware = _detect_firmware(source)
    if "uefi" not in firmware:
        warnings.append("UEFI boot markers were not detected in the media source.")

    selected_profile = profile_for(profile) if profile else None
    config_data = load_config(config_path) if config_path else load_template_config()
    managed_payload_layout = ""
    if selected_profile is not None:
        managed_payload_layout = effective_managed_payload_layout(
            profile,
            selected_profile.managed_payload_layout,
            config_data,
            use_custom_menu,
        )
        if source_role in {"netinst", "netboot"}:
            managed_payload_layout = MANAGED_PAYLOAD_LAYOUT_ISO_STORE
    supports_encrypted_persistence = _supports_encrypted_persistence(source, entries, profile, selected_profile)
    if (
        media_class in {"live", "hybrid"}
        and profile in {PROFILE_DEBIAN, PROFILE_KALI_LINUX, PROFILE_TAILS}
        and selected_profile
        and selected_profile.supports_persistence
        and not supports_encrypted_persistence
    ):
        warnings.append("Encrypted persistence is unavailable because the live initrd or live package set does not provide the required cryptsetup support.")
    if selected_profile and media_class in {"live", "hybrid"}:
        kernel_path = _find_live_kernel_path(entries)
        if kernel_path and not _kernel_has_pe_signature_directory(source, kernel_path):
            if _host_secure_boot_enabled():
                warnings.append("Current host has Secure Boot enabled, and the selected live kernel does not carry a PE signature directory. Managed GRUB boot is expected to fail until Secure Boot is disabled or trusted signed media is used.")
            else:
                warnings.append("The selected live kernel does not carry a PE signature directory. This media is expected to fail under Secure Boot.")
    best_live = _select_entry(profile if profile else PROFILE_DEBIAN, entries)
    best_installer = _select_installer_entry(profile if profile else PROFILE_DEBIAN, entries)

    inspection = MediaInspection(
        source_path=source.display_path,
        source_type=source.source_type,
        volume_id=source.volume_id,
        media_class=media_class,
        firmware=firmware,
        managed_supported=media_class in {"live", "installer", "hybrid"} and bool(entries) and "uefi" in firmware,
        supports_persistence=media_class in {"live", "hybrid"} and bool(selected_profile and selected_profile.supports_persistence),
        supports_encrypted_persistence=supports_encrypted_persistence,
        managed_payload_layout=managed_payload_layout,
        top_level_entries=tuple(top_level_entries),
        best_live_title=best_live.title if best_live else "",
        best_installer_title=best_installer.title if best_installer else "",
        warnings=tuple(warnings),
        entries=tuple(entries),
    )

    return {
        "source_path": inspection.source_path,
        "iso_path": inspection.source_path,
        "source_type": inspection.source_type,
        "volume_id": inspection.volume_id,
        "media_class": inspection.media_class,
        "firmware": list(inspection.firmware),
        "managed_supported": inspection.managed_supported,
        "supports_persistence": inspection.supports_persistence,
        "supports_encrypted_persistence": inspection.supports_encrypted_persistence,
        "managed_payload_layout": inspection.managed_payload_layout,
        "top_level_entries": list(inspection.top_level_entries),
        "best_live_title": inspection.best_live_title,
        "best_installer_title": inspection.best_installer_title,
        "warnings": list(inspection.warnings),
    }


def inspect_iso(iso_path: str, profile: str = "") -> dict[str, object]:
    return inspect_media(iso_path, profile)
