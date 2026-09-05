from __future__ import annotations

from dataclasses import replace
import json
import os
from pathlib import Path
import re

from .boot_parse import (
    BootEntry,
    _find_boot_entries,
    _media_class,
    _select_base_installer_entry,
    _select_entry,
    _select_installer_entry,
)
from .boot_inspect import _detect_firmware, _supports_encrypted_persistence
from .bootpolicy import installer_policy_args, live_policy_kernel_args
from .catalog import profile_for
from .config import (
    effective_managed_payload_layout,
    PROFILE_PREFIXES,
    LEGACY_SECRET_KERNEL_ARG_NAMES,
    LIVE_WIFI_SECRET_KERNEL_ARG_NAMES,
    load_config,
    load_template_config,
    profile_usb_preseed_file,
    profile_fallback_live_kernel_args,
    profile_forensics_kernel_extras,
    profile_installer_kernel_extras,
    profile_persistence_labels,
    profile_live_kernel_extras,
    profile_preseed_url,
)
from .constants import (
    MANAGED_PAYLOAD_LAYOUT_ISO_STORE,
    MANAGED_PAYLOAD_LAYOUT_RAW_ISO,
    MANAGED_PAYLOAD_LAYOUT_SHARED_DATA,
    PERSISTENCE_MODE_ENCRYPTED,
    PERSISTENCE_MODE_NONE,
    PERSISTENCE_MODE_PLAIN,
    PROFILE_DEBIAN,
    PROFILE_KALI_LINUX,
    PROFILE_KALI_PURPLE,
    PROFILE_TAILS,
    PROFILE_UBUNTU_DESKTOP,
    PROFILE_UBUNTU_SERVER,
)
from .iso_source import DirectorySource, MediaSource, _collapse_whitespace, _normalize_member_path, _split_kernel_args, open_source
from .live_hooks import DEBIAN_LIVE_HOOK_KERNEL_ARGS


_INSTALLER_ENTRY_KINDS = {"installer", "automated-installer", "expert-installer", "rescue"}
_DEBIAN_INSTALLER_PROFILES = {PROFILE_DEBIAN, PROFILE_KALI_LINUX, PROFILE_KALI_PURPLE}
_BOOT_METHOD_ISO_LOOPBACK = "iso-loopback"
_BOOT_METHOD_HD_MEDIA = "hd-media"
_BOOT_METHOD_NETBOOT = "netboot"
_BOOT_METHOD_INSTALLER_ISO = "installer-iso"
_LIVE_TORAM_MODULE_CONFIG_KEY = "__DEBIAN_USB_LIVE_TORAM_MODULE"
_LIVE_TORAM_MODULE_CANDIDATES = (
    "/live/filesystem.squashfs",
    "/live/filesystem.erofs",
    "/live/filesystem.ext4",
    "/live/filesystem.ext3",
    "/live/filesystem.ext2",
)


def _load_effective_config(config_path: str) -> dict[str, str]:
    if config_path:
        return dict(load_config(config_path))
    return dict(load_template_config())


_CUSTOM_GRUB_SPEC_ENV = "DEBIAN_USB_SPEC_DIR"
_GRUB_MODULE_PATTERN = re.compile(r"^[A-Za-z0-9_+-]{1,64}$")
_GRUB_PATH_PATTERN = re.compile(r"^/[A-Za-z0-9._+@/-]{1,255}$")
_GRUB_TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9._:+/-]{1,160}$")
_INTERNAL_DRIVE_DEFAULT_MODULES = (
    "part_gpt",
    "part_msdos",
    "fat",
    "ext2",
    "btrfs",
    "xfs",
    "regexp",
    "configfile",
    "chain",
    "search",
    "search_fs_file",
    "search_fs_uuid",
    "search_label",
    "probe",
    "test",
)
_INTERNAL_DRIVE_DEFAULT_CONFIG_PATHS = (
    "/boot/grub/custom.cfg",
    "/boot/grub/grub.cfg",
)
_INTERNAL_DRIVE_DEFAULT_EFI_LOADERS = (
    ("Debian Secure Boot shim", "/EFI/debian/shimx64.efi"),
    ("Debian GRUB", "/EFI/debian/grubx64.efi"),
    ("Ubuntu Secure Boot shim", "/EFI/ubuntu/shimx64.efi"),
    ("Ubuntu GRUB", "/EFI/ubuntu/grubx64.efi"),
    ("Kali Secure Boot shim", "/EFI/kali/shimx64.efi"),
    ("Kali GRUB", "/EFI/kali/grubx64.efi"),
    ("Windows Boot Manager", "/EFI/Microsoft/Boot/bootmgfw.efi"),
    ("Fallback UEFI Boot Loader", "/EFI/BOOT/BOOTX64.EFI"),
    ("Fallback UEFI Boot Loader", "/EFI/Boot/bootx64.efi"),
    ("Fedora Secure Boot shim", "/EFI/fedora/shimx64.efi"),
    ("Fedora GRUB", "/EFI/fedora/grubx64.efi"),
    ("Linux Mint Secure Boot shim", "/EFI/linuxmint/shimx64.efi"),
    ("Linux Mint GRUB", "/EFI/linuxmint/grubx64.efi"),
    ("openSUSE Secure Boot shim", "/EFI/opensuse/shimx64.efi"),
    ("openSUSE GRUB", "/EFI/opensuse/grubx64.efi"),
    ("Arch Linux GRUB", "/EFI/arch/grubx64.efi"),
)
_GRUB_CRYPTO_MODULES = (
    "cryptodisk",
    "crypto",
    "luks2",
    "argon2",
    "gcry_rijndael",
    "gcry_sha512",
    "gcry_sha256",
    "gcry_rsa",
    "pgp",
)


def _custom_grub_spec_dir() -> Path:
    env_path = os.environ.get(_CUSTOM_GRUB_SPEC_ENV, "").strip()
    candidates: list[Path] = []
    if env_path:
        candidates.append(Path(env_path).expanduser())
    module_path = Path(__file__).resolve()
    if len(module_path.parents) > 3:
        candidates.append(module_path.parents[3] / "configs" / "spec" / "grub")
    candidates.append(Path("/usr/lib/debian-usb/spec/grub"))
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    searched = ", ".join(str(candidate) for candidate in candidates)
    raise FileNotFoundError(f"unable to locate the custom GRUB spec directory; checked: {searched}")


def _load_custom_grub_json(name: str) -> dict[str, object]:
    path = _custom_grub_spec_dir() / name
    return json.loads(path.read_text(encoding="utf-8"))


def _static_entry_spec(main_spec: dict[str, object], entry_id: str) -> dict[str, object]:
    static_entries = main_spec.get("static_entries", [])
    if not isinstance(static_entries, list):
        return {}
    for item in static_entries:
        if not isinstance(item, dict):
            continue
        item_id = str(item.get("id") or "").strip()
        if item_id == entry_id:
            return item
    return {}


def _entry_child_title(entry_spec: dict[str, object], child_id: str, default_title: str) -> str:
    entries = entry_spec.get("entries", [])
    if not isinstance(entries, list):
        return default_title
    for item in entries:
        if not isinstance(item, dict):
            continue
        item_id = str(item.get("id") or "").strip()
        if item_id != child_id:
            continue
        title = str(item.get("title") or "").strip()
        return title or default_title
    return default_title


def _static_entry_title(main_spec: dict[str, object], entry_id: str, default_title: str) -> str:
    entry_spec = _static_entry_spec(main_spec, entry_id)
    title = str(entry_spec.get("title") or "").strip()
    return title or default_title


def _configured_static_entry_title(entry_id: str, default_title: str) -> str:
    return _static_entry_title(_load_custom_grub_json("main.json"), entry_id, default_title)


def _updatevars_menu_labels(main_spec: dict[str, object]) -> tuple[str, str, str]:
    entry_spec = _static_entry_spec(main_spec, "updatevars")
    title = str(entry_spec.get("title") or "").strip() or "UEFI UpdateVars"
    user_title = _entry_child_title(entry_spec, "user-mode", f"{title} User Mode (PK Installed) [.auth]")
    setup_title = _entry_child_title(entry_spec, "setup-mode", f"{title} Setup Mode (PK Not Installed) [.esl]")
    return title, user_title, setup_title


def _configured_updatevars_menu_labels() -> tuple[str, str, str]:
    return _updatevars_menu_labels(_load_custom_grub_json("main.json"))


def _updatevars_efi_available() -> bool:
    override = os.environ.get("DEBIAN_USB_UPDATEVARS_EFI_PATH")
    if override is not None:
        path = override.strip()
        return bool(path) and Path(path).is_file()
    return bool(_static_entry_spec(_load_custom_grub_json("main.json"), "updatevars"))


def _validate_grub_module_name(value: str, field_name: str) -> str:
    module = value.strip()
    if not _GRUB_MODULE_PATTERN.fullmatch(module):
        raise ValueError(f"invalid GRUB module name in {field_name}: {value}")
    return module


def _validate_grub_path(value: str, field_name: str) -> str:
    path = value.strip()
    if not _GRUB_PATH_PATTERN.fullmatch(path) or "//" in path:
        raise ValueError(f"invalid GRUB path in {field_name}: {value}")
    return path


def _validate_grub_token(value: str, field_name: str) -> str:
    token = value.strip()
    if not _GRUB_TOKEN_PATTERN.fullmatch(token) or any(character.isspace() for character in token):
        raise ValueError(f"invalid GRUB token in {field_name}: {value}")
    return token


def _dedupe_preserving_order(values: list[str] | tuple[str, ...]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        ordered.append(value)
    return ordered


def _grub_module_lines(module_names: list[str] | tuple[str, ...]) -> list[str]:
    return [f"insmod {_validate_grub_module_name(module, 'modules')}" for module in _dedupe_preserving_order(module_names)]


def _merge_grub_module_lines(*line_groups: list[str]) -> list[str]:
    lines: list[str] = []
    seen: set[str] = set()
    for group in line_groups:
        for line in group:
            normalized = line.strip()
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            lines.append(normalized)
    return lines


def _internal_drive_modules(entry_spec: dict[str, object]) -> list[str]:
    raw_modules = entry_spec.get("modules")
    if raw_modules is None:
        return list(_INTERNAL_DRIVE_DEFAULT_MODULES)
    if not isinstance(raw_modules, list):
        raise ValueError("internal-drive modules must be a list")
    if len(raw_modules) > 32:
        raise ValueError("internal-drive modules must not contain more than 32 entries")
    modules: list[str] = []
    for index, module in enumerate(raw_modules, start=1):
        if not isinstance(module, str):
            raise ValueError(f"internal-drive module {index} must be a string")
        modules.append(_validate_grub_module_name(module, "internal-drive modules"))
    return _dedupe_preserving_order(modules)


def _internal_drive_config_paths(entry_spec: dict[str, object]) -> list[str]:
    raw_paths = entry_spec.get("config_paths")
    if raw_paths is None:
        return list(_INTERNAL_DRIVE_DEFAULT_CONFIG_PATHS)
    if not isinstance(raw_paths, list):
        raise ValueError("internal-drive config_paths must be a list")
    if len(raw_paths) > 16:
        raise ValueError("internal-drive config_paths must not contain more than 16 entries")
    paths: list[str] = []
    for index, path in enumerate(raw_paths, start=1):
        if not isinstance(path, str):
            raise ValueError(f"internal-drive config path {index} must be a string")
        paths.append(_validate_grub_path(path, "internal-drive config_paths"))
    return _dedupe_preserving_order(paths)


def _internal_drive_efi_loaders(entry_spec: dict[str, object]) -> list[tuple[str, str]]:
    raw_loaders = entry_spec.get("efi_loaders")
    if raw_loaders is None:
        return list(_INTERNAL_DRIVE_DEFAULT_EFI_LOADERS)
    if not isinstance(raw_loaders, list):
        raise ValueError("internal-drive efi_loaders must be a list")
    if len(raw_loaders) > 64:
        raise ValueError("internal-drive efi_loaders must not contain more than 64 entries")
    loaders: list[tuple[str, str]] = []
    seen_paths: set[str] = set()
    for index, item in enumerate(raw_loaders, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"internal-drive EFI loader {index} must be an object")
        title = str(item.get("title") or "").strip()
        path_value = item.get("path")
        if not title:
            raise ValueError(f"internal-drive EFI loader {index} requires a title")
        if len(title) > 96 or any(ord(character) < 32 or ord(character) == 127 for character in title):
            raise ValueError(f"internal-drive EFI loader {index} has an invalid title")
        if not isinstance(path_value, str):
            raise ValueError(f"internal-drive EFI loader {index} requires a path")
        path = _validate_grub_path(path_value, "internal-drive efi_loaders")
        path_key = path.lower()
        if path_key in seen_paths:
            continue
        seen_paths.add(path_key)
        loaders.append((title, path))
    return loaders


def _internal_drive_static_entry_spec(main_spec: dict[str, object]) -> dict[str, object]:
    return _static_entry_spec(main_spec, "internal-drive")


def _internal_drive_module_lines(main_spec: dict[str, object]) -> list[str]:
    entry_spec = _internal_drive_static_entry_spec(main_spec)
    if not entry_spec:
        return []
    return _grub_module_lines(_internal_drive_modules(entry_spec))


def _remove_kernel_args_matching(kernel_args: str, patterns: list[str]) -> str:
    args = _split_kernel_args(kernel_args)
    filtered: list[str] = []
    for item in args:
        if any(re.fullmatch(pattern, item) for pattern in patterns):
            continue
        filtered.append(item)
    return _collapse_whitespace(" ".join(filtered))


def _kernel_arg_key(item: str) -> str:
    return item.split("=", 1)[0] if "=" in item else ""


def _remove_secret_kernel_args(kernel_args: str) -> str:
    forbidden = LEGACY_SECRET_KERNEL_ARG_NAMES | LIVE_WIFI_SECRET_KERNEL_ARG_NAMES
    return _collapse_whitespace(
        " ".join(
            item
            for item in _split_kernel_args(kernel_args)
            if item.split("=", 1)[0] not in forbidden
        )
    )


def _merge_kernel_args(kernel_args: str, additions: str) -> str:
    args = _split_kernel_args(kernel_args)
    additions_tokens = _split_kernel_args(additions)
    if not additions_tokens:
        return _collapse_whitespace(kernel_args)
    separator_index = len(args)
    for index, item in enumerate(args):
        if item == "---":
            separator_index = index
            break
    merged = list(args[:separator_index])
    suffix = args[separator_index:]
    override_keys = {_kernel_arg_key(item) for item in additions_tokens if "=" in item}
    override_flags = {item for item in additions_tokens if "=" not in item}
    merged = [
        item
        for item in merged
        if not (
            ("=" in item and _kernel_arg_key(item) in override_keys)
            or ("=" not in item and item in override_flags)
        )
    ]
    merged.extend(additions_tokens)
    return _collapse_whitespace(" ".join(merged + suffix))


def _kernel_arg_assignments(kernel_args: str) -> dict[str, str]:
    assignments: dict[str, str] = {}
    for item in _split_kernel_args(kernel_args):
        if "=" not in item:
            continue
        key, value = item.split("=", 1)
        assignments[key] = value
    return assignments


def _live_hook_kernel_args(config_data: dict[str, str], profile: str) -> str:
    if profile != PROFILE_DEBIAN:
        return ""
    mandatory_args = " ".join(DEBIAN_LIVE_HOOK_KERNEL_ARGS)
    optional_args = ""
    if config_data.get("DEFAULT_LIVE_HOOKS", "0").strip() == "1":
        optional_args = _remove_secret_kernel_args(config_data.get("DEFAULT_LIVE_ARGS_HOOKS", ""))
    return _merge_kernel_args(optional_args, mandatory_args)


def _escape_unescaped_semicolons(value: str) -> str:
    escaped = False
    result: list[str] = []
    for char in value:
        if escaped:
            result.append(char)
            escaped = False
            continue
        if char == "\\":
            result.append(char)
            escaped = True
            continue
        if char == ";":
            result.append("\\;")
            continue
        result.append(char)
    return "".join(result)


def _normalize_classes_kernel_args(kernel_args: str) -> str:
    normalized: list[str] = []
    for item in _split_kernel_args(kernel_args):
        if item.startswith("classes="):
            key, value = item.split("=", 1)
            normalized.append(f"{key}={_escape_unescaped_semicolons(value)}")
        else:
            normalized.append(item)
    return _collapse_whitespace(" ".join(normalized))


def _merge_semicolon_values(existing_value: str, addition_value: str) -> str:
    existing_value = existing_value.strip()
    addition_value = addition_value.strip()
    if not addition_value:
        return existing_value
    if not existing_value:
        return addition_value.lstrip(";")
    if addition_value.startswith("\\;"):
        return f"{existing_value}{addition_value}"
    if addition_value.startswith(";"):
        addition_value = addition_value.lstrip(";")
    separator = "" if existing_value.endswith((";", "\\;")) else "\\;"
    return f"{existing_value}{separator}{addition_value}"


def _append_kernel_arg_value(kernel_args: str, addition: str) -> str:
    if "=" not in addition:
        return _merge_kernel_args(kernel_args, addition)
    key, addition_value = addition.split("=", 1)
    args = _split_kernel_args(kernel_args)
    separator_index = len(args)
    for index, item in enumerate(args):
        if item == "---":
            separator_index = index
            break
    merged = list(args[:separator_index])
    suffix = args[separator_index:]
    for index, item in enumerate(merged):
        if item == key or item.startswith(f"{key}="):
            existing_value = item.split("=", 1)[1] if "=" in item else ""
            merged[index] = f"{key}={_merge_semicolon_values(existing_value, addition_value)}"
            return _collapse_whitespace(" ".join(merged + suffix))
    merged.append(addition)
    return _collapse_whitespace(" ".join(merged + suffix))


def _classes_last_kernel_args(kernel_args: str) -> str:
    args = _split_kernel_args(kernel_args)
    separator_index = len(args)
    for index, item in enumerate(args):
        if item == "---":
            separator_index = index
            break

    merged: list[str] = []
    class_value = ""
    for item in args[:separator_index]:
        if item.startswith("classes="):
            class_value = _merge_semicolon_values(class_value, item.split("=", 1)[1])
            continue
        merged.append(item)
    if class_value:
        merged.append(f"classes={class_value}")
    return _collapse_whitespace(" ".join(merged + args[separator_index:]))


def _merge_preseed_append_args(kernel_args: str, append_args: str) -> str:
    merged = _normalize_classes_kernel_args(kernel_args)
    passthrough: list[str] = []
    class_additions: list[str] = []
    for addition in _split_kernel_args(append_args):
        addition = _normalize_classes_kernel_args(addition)
        if addition.startswith("classes="):
            class_additions.append(addition)
            continue
        passthrough.append(addition)
    if passthrough:
        merged = _merge_kernel_args(merged, " ".join(passthrough))
    for addition in class_additions:
        merged = _append_kernel_arg_value(merged, addition)
    return _classes_last_kernel_args(merged)


def _preseed_append_args_for_network(config_data: dict[str, str], network_args_key: str) -> str:
    if network_args_key == "PRESEED_WIFI_KERNEL_ARGS":
        return config_data.get("PRESEED_ARGS_WIFI_APPEND", "").strip()
    if network_args_key == "PRESEED_ETHERNET_KERNEL_ARGS":
        return config_data.get("PRESEED_ARGS_ETH_APPEND", "").strip()
    return ""


def _sanitize_live_kernel_args(kernel_args: str) -> str:
    args = _remove_kernel_args_matching(
        kernel_args,
        [
            r"findiso=.*",
            r"fromiso=.*",
            r"iso-scan/filename=.*",
            r"bootfrom=.*",
        ],
    )
    return _collapse_whitespace(args)


def _live_toram_module_for_source(source: DirectorySource | MediaSource, profile: str) -> str:
    if profile_for(profile).live_boot_family != "live-boot":
        return ""
    for candidate in _LIVE_TORAM_MODULE_CANDIDATES:
        if source.exists(candidate):
            return Path(candidate).name
    return "filesystem.squashfs"


def _remove_live_only_kernel_args(kernel_args: str) -> str:
    return _remove_kernel_args_matching(_collapse_whitespace(kernel_args), [r"toram(?:=.*)?"])


def _live_toram_kernel_arg(config_data: dict[str, str], profile: str) -> str:
    if profile_for(profile).live_boot_family == "live-boot":
        module = config_data.get(_LIVE_TORAM_MODULE_CONFIG_KEY, "").strip() or "filesystem.squashfs"
        return f"toram={module}"
    return "toram"


def _apply_live_settings(kernel_args: str, config_data: dict[str, str], profile: str, live_toram: bool | None = None) -> str:
    args = _remove_live_only_kernel_args(kernel_args)
    fallback_args = profile_fallback_live_kernel_args(config_data, profile)
    profile_meta = profile_for(profile)
    if profile_meta.live_boot_family == "casper" and "boot=casper" not in args:
        args = _merge_kernel_args(args, fallback_args)
    elif profile_meta.live_boot_family == "live-boot" and "boot=live" not in args:
        args = _merge_kernel_args(args, fallback_args)
    policy_args = live_policy_kernel_args(config_data, config_data["DEFAULT_BOOT_POLICY"])
    if policy_args:
        args = _merge_kernel_args(args, policy_args)
    extras = config_data["DEFAULT_LIVE_KERNEL_EXTRAS"]
    if extras:
        args = _merge_kernel_args(args, extras)
    profile_extras = profile_live_kernel_extras(config_data, profile)
    if profile_extras:
        args = _merge_kernel_args(args, profile_extras)
    live_hook_args = _live_hook_kernel_args(config_data, profile)
    if live_hook_args:
        args = _merge_kernel_args(args, live_hook_args)
    effective_live_toram = config_data["DEFAULT_LIVE_TORAM"] == "1" if live_toram is None else live_toram
    # An explicit OFF choice must also override inherited/profile extras.
    args = _remove_kernel_args_matching(args, [r"toram(?:=.*)?"])
    if effective_live_toram:
        args = _merge_kernel_args(args, _live_toram_kernel_arg(config_data, profile))
    mem_gib = int(config_data["DEFAULT_LIVE_MEM_GIB"])
    if mem_gib > 0:
        args = _merge_kernel_args(args, f"mem={mem_gib}G")
    return _remove_secret_kernel_args(args)


def _apply_installer_settings(kernel_args: str, config_data: dict[str, str], profile: str) -> str:
    args = _remove_live_only_kernel_args(kernel_args)
    profile_url = profile_preseed_url(config_data, profile)
    policy_args = installer_policy_args(
        config_data,
        config_data["DEFAULT_INSTALLER_POLICY"],
        profile_url,
        profile,
    )
    if policy_args:
        args = _merge_kernel_args(args, policy_args)
    extras = config_data["DEFAULT_INSTALLER_KERNEL_EXTRAS"]
    if extras:
        args = _merge_kernel_args(args, extras)
    profile_extras = profile_installer_kernel_extras(config_data, profile)
    if profile_extras:
        args = _merge_kernel_args(args, profile_extras)
    return _remove_secret_kernel_args(
        _remove_live_only_kernel_args(_set_installer_seed_transport(args, url=profile_url))
    )


def _installer_media_device_arg(payload_uuid: str) -> str:
    payload_locator = payload_uuid.strip()
    if not payload_locator:
        return ""
    return f"INSTALL_MEDIA_DEV={_payload_device_path(payload_locator)}"


def _bind_installer_media_device(kernel_args: str, payload_uuid: str) -> str:
    install_media_arg = _installer_media_device_arg(payload_uuid)
    if not install_media_arg:
        return _collapse_whitespace(kernel_args)
    args = _remove_kernel_args_matching(
        _collapse_whitespace(kernel_args),
        [
            r"INSTALL_MEDIA_DEV=.*",
        ],
    )
    return _merge_kernel_args(args, install_media_arg)


def _payload_device_path(payload_locator: str) -> str:
    normalized = payload_locator.strip()
    if normalized.startswith("/dev/"):
        return normalized
    return f"/dev/disk/by-uuid/{normalized}"


def _payload_uuid_value(payload_locator: str) -> str:
    normalized = payload_locator.strip()
    prefix = "/dev/disk/by-uuid/"
    if normalized.startswith(prefix):
        return normalized[len(prefix) :]
    if normalized.startswith("/dev/"):
        return ""
    return normalized


def _remove_managed_media_locator_args(kernel_args: str) -> str:
    return _remove_kernel_args_matching(
        _collapse_whitespace(kernel_args),
        [
            r"findiso=.*",
            r"fromiso=.*",
            r"iso-scan/filename=.*",
            r"bootfrom=.*",
            r"INSTALL_MEDIA_DEV=.*",
            r"live-media=.*",
            r"uuid=.*",
            r"ignore_uuid",
        ],
    )


def _bind_iso_store_payload(kernel_args: str, profile: str, entry: BootEntry, isofile_path: str) -> str:
    normalized_isofile = _normalize_member_path(isofile_path)
    if not normalized_isofile:
        return _collapse_whitespace(kernel_args)

    args = _remove_managed_media_locator_args(kernel_args)
    if entry.kind in _INSTALLER_ENTRY_KINDS:
        return _merge_kernel_args(args, f"iso-scan/filename={normalized_isofile}")
    if entry.kind.startswith("live"):
        profile_meta = profile_for(profile)
        if profile_meta.live_boot_family == "live-boot":
            return _merge_kernel_args(args, f"findiso={normalized_isofile}")
        if profile_meta.live_boot_family == "casper":
            return _merge_kernel_args(args, f"iso-scan/filename={normalized_isofile}")
    return args


def _shared_data_root(profile: str, source_role: str, media_class: str) -> str:
    installer_like = source_role == "netinst" or media_class == "installer"
    if profile == PROFILE_DEBIAN:
        if source_role == "netboot":
            return "/debian-netboot"
        if installer_like:
            return "/debian-netinst"
        return "/debian-live"
    if profile == PROFILE_KALI_LINUX:
        if source_role == "netboot":
            return "/kali-netboot"
        if installer_like:
            return "/kali-netinst"
        return "/kali-live"
    if profile == PROFILE_KALI_PURPLE:
        return "/kali-purple-installer"
    if profile == PROFILE_UBUNTU_DESKTOP:
        return "/ubuntu"
    if profile == PROFILE_UBUNTU_SERVER:
        return "/ubuntu-server"
    if profile == PROFILE_TAILS:
        return "/tails-live"
    return "/payload"


def _bind_shared_data_payload(kernel_args: str, profile: str, entry: BootEntry, root_path: str, isofile_path: str) -> str:
    args = _remove_managed_media_locator_args(kernel_args)
    if entry.kind in _INSTALLER_ENTRY_KINDS:
        if isofile_path:
            return _merge_kernel_args(args, f"iso-scan/filename={isofile_path}")
        return args
    if entry.kind.startswith("live"):
        if not isofile_path:
            raise ValueError(f"shared ISO-store Live entry requires an ISO payload under {root_path}")
        return _bind_iso_store_payload(args, profile, entry, isofile_path)
    return args


def _apply_forensics_settings(kernel_args: str, config_data: dict[str, str], profile: str) -> str:
    args = _collapse_whitespace(kernel_args)
    extras = config_data["DEFAULT_FORENSICS_KERNEL_EXTRAS"]
    if extras:
        args = _merge_kernel_args(args, extras)
    profile_extras = profile_forensics_kernel_extras(config_data, profile)
    if profile_extras:
        args = _merge_kernel_args(args, profile_extras)
    return args


def _apply_preserved_live_settings(entry: BootEntry, profile: str, config_data: dict[str, str], live_toram: bool | None = None) -> str:
    kernel_args = _apply_live_settings(entry.kernel_args, config_data, profile, live_toram)
    if entry.kind == "live-forensics":
        kernel_args = _apply_forensics_settings(kernel_args, config_data, profile)
    return kernel_args


def _finalize_kernel_args(
    profile: str,
    kernel_args: str,
    live_uuid: str,
    persistence_mode: str,
    persistence_label: str = "",
) -> str:
    args = _sanitize_live_kernel_args(kernel_args)
    profile_meta = profile_for(profile)
    if profile_meta.live_boot_family == "casper":
        uuid_value = _payload_uuid_value(live_uuid)
        args = _remove_kernel_args_matching(
            args,
            [
                r"uuid=.*",
                r"ignore_uuid",
                r"live-media=.*",
                r"persistent",
                r"nopersistent",
            ],
        )
        if persistence_mode == PERSISTENCE_MODE_PLAIN and profile_meta.supports_persistence:
            args = _merge_kernel_args(args, "persistent")
        if uuid_value:
            args = _merge_kernel_args(args, f"uuid={uuid_value}")
        else:
            args = _merge_kernel_args(args, f"live-media={_payload_device_path(live_uuid)}")
        return args
    args = _remove_kernel_args_matching(
        args,
        [
            r"uuid=.*",
            r"ignore_uuid",
            r"live-media=.*",
            r"bootfrom=.*",
            r"persistence",
            r"nopersistence",
            r"persistent=cryptsetup",
            r"persistence-label=.*",
            r"persistence-encryption=.*",
            r"persistence-media=.*",
            r"persistence-storage=.*",
            r"persistence-method=.*",
            r"union=.*",
        ],
    )
    args = _merge_kernel_args(args, "ignore_uuid")
    args = _merge_kernel_args(args, f"live-media={_payload_device_path(live_uuid)}")
    persistence_label = persistence_label.strip()
    if profile == PROFILE_TAILS:
        return args
    if persistence_mode == PERSISTENCE_MODE_ENCRYPTED:
        if profile in {PROFILE_DEBIAN, PROFILE_KALI_LINUX, PROFILE_TAILS}:
            args = _merge_kernel_args(
                args,
                "persistent=cryptsetup persistence-encryption=luks persistence "
                "persistence-media=removable-usb persistence-storage=filesystem union=overlay",
            )
        else:
            args = _merge_kernel_args(
                args,
                "persistence persistence-encryption=luks persistence-media=removable-usb "
                "persistence-storage=filesystem union=overlay",
            )
        if persistence_label:
            args = _merge_kernel_args(args, f"persistence-label={persistence_label}")
    elif persistence_mode == PERSISTENCE_MODE_PLAIN:
        args = _merge_kernel_args(
            args,
            f"persistence persistence-label={persistence_label or 'persistence'} "
            "persistence-media=removable-usb persistence-storage=filesystem union=overlay",
        )
    return args


def _ensure_member_exists(source: DirectorySource | MediaSource, member_path: str) -> str:
    normalized = _normalize_member_path(member_path)
    if not normalized or not source.exists(normalized):
        raise ValueError(f"media member does not exist: {normalized}")
    return normalized


def resolve_live_boot(
    *,
    root: str,
    profile: str,
    live_uuid: str,
    persistence: bool,
    kernel_args_override: str = "",
    kernel_path_override: str = "",
    initrd_path_override: str = "",
    menu_label_override: str = "",
    config_path: str = "",
) -> dict[str, str]:
    profile_meta = profile_for(profile)
    config_data = _load_effective_config(config_path)
    source = DirectorySource(root)
    entries = _find_boot_entries(source)
    selected_entry = _select_entry(profile, entries)

    kernel_path = kernel_path_override or (selected_entry.kernel_path if selected_entry else "")
    initrd_path = initrd_path_override or (selected_entry.initrd_path if selected_entry else "")
    kernel_args = (
        kernel_args_override
        or (selected_entry.kernel_args if selected_entry else "")
        or profile_fallback_live_kernel_args(config_data, profile)
    )
    menu_label = menu_label_override or (selected_entry.title if selected_entry else profile_meta.default_menu_label)

    if config_path and not kernel_args_override:
        config_data[_LIVE_TORAM_MODULE_CONFIG_KEY] = _live_toram_module_for_source(source, profile)
        kernel_args = _apply_live_settings(kernel_args, config_data, profile)

    if not kernel_path:
        raise ValueError(f"unable to detect kernel path for profile {profile}; supply an override")
    if not kernel_args:
        raise ValueError(f"unable to detect kernel arguments for profile {profile}; supply an override")

    kernel_path = _ensure_member_exists(source, kernel_path)
    initrd_path = _ensure_member_exists(source, initrd_path) if initrd_path else ""
    persistence_fs_label, persistence_partlabel = profile_persistence_labels(config_data, profile)
    kernel_args = _finalize_kernel_args(
        profile,
        kernel_args,
        live_uuid,
        PERSISTENCE_MODE_PLAIN if persistence else PERSISTENCE_MODE_NONE,
        persistence_fs_label,
    )

    return {
        "menu_label": menu_label,
        "kernel_path": kernel_path,
        "initrd_path": initrd_path,
        "kernel_args": kernel_args,
        "persistence_fs_label": persistence_fs_label,
        "persistence_partlabel": persistence_partlabel,
        "entry_source": selected_entry.source if selected_entry else "fallback",
    }


def resolve_installer_boot(
    *,
    root: str,
    profile: str,
    config_path: str = "",
) -> dict[str, str]:
    source = DirectorySource(root)
    config_data = _load_effective_config(config_path)
    entries = _find_boot_entries(source)
    selected_entry = _select_installer_entry(profile, entries)
    if selected_entry is None:
        return {"available": False}

    kernel_path = _ensure_member_exists(source, selected_entry.kernel_path)
    initrd_path = _ensure_member_exists(source, selected_entry.initrd_path) if selected_entry.initrd_path else ""
    kernel_args = _collapse_whitespace(selected_entry.kernel_args)
    if config_path:
        kernel_args = _apply_installer_settings(kernel_args, config_data, profile)

    return {
        "available": True,
        "menu_label": selected_entry.title or f"{profile_for(profile).title} Installer",
        "kernel_path": kernel_path,
        "initrd_path": initrd_path,
        "kernel_args": kernel_args,
        "entry_source": selected_entry.source,
    }


def _filter_entries_for_profile(profile: str, entries: list[BootEntry]) -> list[BootEntry]:
    return entries


def _filter_entries_for_source_role(profile: str, source_role: str, entries: list[BootEntry]) -> list[BootEntry]:
    if source_role in {"netinst", "netboot"}:
        return [entry for entry in entries if entry.kind in _INSTALLER_ENTRY_KINDS]
    if source_role == "primary" and profile_for(profile).preferred_media == "live":
        live_entries = [entry for entry in entries if entry.kind.startswith("live") or entry.kind == "verify"]
        if live_entries:
            return live_entries
    return entries


def _synthetic_live_entry(
    *,
    source: MediaSource,
    profile: str,
    config_data: dict[str, str],
    live_uuid: str,
    persistence_mode: str,
    template: BootEntry | None,
    kernel_args_override: str,
    kernel_path_override: str,
    initrd_path_override: str,
    menu_label_override: str,
    order: int,
    persistence_label: str = "",
    live_toram: bool | None = None,
) -> BootEntry:
    profile_meta = profile_for(profile)
    kernel_path = kernel_path_override or (template.kernel_path if template else "")
    initrd_path = initrd_path_override or (template.initrd_path if template else "")
    kernel_args = (
        kernel_args_override
        or (template.kernel_args if template else "")
        or profile_fallback_live_kernel_args(config_data, profile)
    )
    title = menu_label_override or (template.title if template else profile_meta.default_menu_label)
    menu_path = template.menu_path if template else ()

    if not kernel_path:
        raise ValueError(f"unable to render a managed live entry for profile {profile}; supply a kernel override")
    kernel_path = _ensure_member_exists(source, kernel_path)
    if initrd_path:
        initrd_path = _ensure_member_exists(source, initrd_path)
    if not kernel_args_override:
        kernel_args = _apply_live_settings(kernel_args, config_data, profile, live_toram)
    if not persistence_label:
        persistence_label, _ = profile_persistence_labels(config_data, profile)
    kernel_args = _finalize_kernel_args(profile, kernel_args, live_uuid, persistence_mode, persistence_label)
    if persistence_mode == PERSISTENCE_MODE_PLAIN:
        title = f"{title} (Persistence)"
    elif persistence_mode == PERSISTENCE_MODE_ENCRYPTED:
        title = f"{title} (Encrypted Persistence)"
    return BootEntry(
        title=title,
        kernel_path=kernel_path,
        initrd_path=initrd_path,
        kernel_args=kernel_args,
        source="synthetic/live",
        kind="live-encrypted-persistence" if persistence_mode == PERSISTENCE_MODE_ENCRYPTED else ("live-persistence" if persistence_mode == PERSISTENCE_MODE_PLAIN else "live"),
        menu_path=menu_path,
        order=order,
    )


def _entry_sort_key(entry: BootEntry) -> tuple[int, int]:
    return (entry.order, 0)


def _apply_preserved_installer_settings(
    entry: BootEntry,
    profile: str,
    config_data: dict[str, str],
    *,
    payload_uuid: str = "",
    payload_layout: str = "",
) -> BootEntry:
    kernel_args = _collapse_whitespace(entry.kernel_args)
    if entry.kind not in {"installer", "automated-installer", "expert-installer", "rescue"}:
        return entry
    if entry.kind != "rescue":
        kernel_args = _apply_installer_settings(kernel_args, config_data, profile)
    else:
        extras = config_data["DEFAULT_INSTALLER_KERNEL_EXTRAS"]
        if extras:
            kernel_args = _merge_kernel_args(kernel_args, extras)
        profile_extras = profile_installer_kernel_extras(config_data, profile)
        if profile_extras:
            kernel_args = _merge_kernel_args(kernel_args, profile_extras)
        kernel_args = _remove_installer_seed_transport_args(kernel_args)
    if payload_layout == MANAGED_PAYLOAD_LAYOUT_RAW_ISO:
        kernel_args = _bind_installer_media_device(kernel_args, payload_uuid)
    return replace(entry, kernel_args=kernel_args)


def _adapt_entry_for_managed(
    entry: BootEntry,
    profile: str,
    live_uuid: str,
    config_data: dict[str, str],
    persistence_mode: str,
    payload_layout: str = "",
    persistence_label: str = "",
    live_toram: bool | None = None,
) -> BootEntry:
    if not persistence_label:
        persistence_label, _ = profile_persistence_labels(config_data, profile)
    if entry.kind == "live-persistence":
        kernel_args = _apply_preserved_live_settings(entry, profile, config_data, live_toram)
        return replace(entry, kernel_args=_finalize_kernel_args(profile, kernel_args, live_uuid, PERSISTENCE_MODE_PLAIN, persistence_label))
    if entry.kind == "live-encrypted-persistence":
        kernel_args = _apply_preserved_live_settings(entry, profile, config_data, live_toram)
        return replace(entry, kernel_args=_finalize_kernel_args(profile, kernel_args, live_uuid, PERSISTENCE_MODE_ENCRYPTED, persistence_label))
    if entry.kind == "live-forensics":
        kernel_args = _apply_preserved_live_settings(entry, profile, config_data, live_toram)
        return replace(entry, kernel_args=_finalize_kernel_args(profile, kernel_args, live_uuid, PERSISTENCE_MODE_NONE))
    if entry.kind.startswith("live"):
        kernel_args = _apply_preserved_live_settings(entry, profile, config_data, live_toram)
        return replace(entry, kernel_args=_finalize_kernel_args(profile, kernel_args, live_uuid, PERSISTENCE_MODE_NONE))
    if entry.kind == "verify":
        return replace(entry, kernel_args=_finalize_kernel_args(profile, entry.kernel_args, live_uuid, PERSISTENCE_MODE_NONE))
    if entry.kind in {"installer", "automated-installer", "expert-installer", "rescue"}:
        return _apply_preserved_installer_settings(
            entry,
            profile,
            config_data,
            payload_uuid=live_uuid,
            payload_layout=payload_layout,
        )
    return entry


def _escape_grub_string(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


_GRUB_ARGUMENT_ESCAPE_CHARS = set(';&|<>(){}$"\'!#`')


def _escape_grub_argument_token(value: str) -> str:
    escaped = False
    result: list[str] = []
    for char in value:
        if escaped:
            result.append(char)
            escaped = False
            continue
        if char == "\\":
            result.append(char)
            escaped = True
            continue
        if char in _GRUB_ARGUMENT_ESCAPE_CHARS:
            result.append("\\")
        result.append(char)
    return "".join(result)


def _render_grub_kernel_args(kernel_args: str, isofile_path: str = "") -> str:
    normalized_isofile = _normalize_member_path(isofile_path)
    rendered: list[str] = []
    for token in _split_kernel_args(_remove_secret_kernel_args(kernel_args)):
        if normalized_isofile:
            for prefix in ("findiso=", "fromiso=", "iso-scan/filename=", "bootfrom="):
                if token == f"{prefix}{normalized_isofile}":
                    rendered.append(f"{prefix}$isofile")
                    break
            else:
                rendered.append(_escape_grub_argument_token(token))
            continue
        rendered.append(_escape_grub_argument_token(token))
    return " ".join(rendered)


def _entry_uses_iso_loopback(entry: BootEntry) -> bool:
    return entry.boot_method == _BOOT_METHOD_ISO_LOOPBACK and bool(entry.isofile_path)


def _secure_boot_asset_path(namespace: str, kernel_path: str) -> str:
    normalized = _normalize_member_path(kernel_path)
    namespace = namespace.strip().strip("/")
    if not namespace:
        raise ValueError("secure-boot asset namespace is required")
    return f"/EFI/debian-usb/assets/{namespace}{normalized}"


def _signed_kernel_assets(entries: list[BootEntry], namespace: str, source_path: str) -> list[dict[str, str]]:
    assets: list[dict[str, str]] = []
    seen: set[str] = set()
    for entry in entries:
        if not entry.kernel_path or _entry_uses_iso_loopback(entry):
            continue
        normalized = _normalize_member_path(entry.kernel_path)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        assets.append(
            {
                "iso_path": source_path,
                "source_path": normalized,
                "asset_path": _secure_boot_asset_path(namespace, normalized),
            }
        )
    return assets


def _boot_initrd_assets(entries: list[BootEntry], namespace: str, source_path: str) -> list[dict[str, str]]:
    assets: list[dict[str, str]] = []
    seen: set[str] = set()
    for entry in entries:
        if not entry.initrd_path or _entry_uses_iso_loopback(entry):
            continue
        normalized = _normalize_member_path(entry.initrd_path)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        assets.append(
            {
                "iso_path": source_path,
                "source_path": normalized,
                "asset_path": _secure_boot_asset_path(namespace, normalized),
            }
        )
    return assets


def _installer_boot_initrd_patch_assets(entries: list[BootEntry], namespace: str, source_path: str) -> list[dict[str, str]]:
    assets: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for entry in entries:
        if entry.kind not in _INSTALLER_ENTRY_KINDS or "INSTALL_MEDIA_DEV=" not in entry.kernel_args:
            continue
        normalized = _normalize_member_path(entry.initrd_path)
        if not normalized:
            continue
        asset_path = _secure_boot_asset_path(namespace, normalized)
        key = (source_path, normalized, asset_path)
        if key in seen:
            continue
        seen.add(key)
        assets.append(
            {
                "iso_path": source_path,
                "source_path": normalized,
                "asset_path": asset_path,
            }
        )
    return assets


def _filter_entries_for_secure_boot(entries: list[BootEntry]) -> list[BootEntry]:
    filtered: list[BootEntry] = []
    for entry in entries:
        kernel_path = entry.kernel_path.lower()
        commands = " ".join(entry.commands).lower()
        if "memtest" in kernel_path or "linux16 " in commands:
            continue
        filtered.append(entry)
    return filtered


_SAFE_ISO_BASENAME_RE = re.compile(r"[^A-Za-z0-9._+-]+")


def _safe_path_segment(value: str, fallback: str) -> str:
    segment = _SAFE_ISO_BASENAME_RE.sub("_", value.strip()).strip("._/")
    return segment or fallback


def _safe_iso_basename(source_path: str) -> str:
    name = Path(source_path).name.strip()
    if not name:
        name = "payload.iso"
    name = _SAFE_ISO_BASENAME_RE.sub("_", name).strip("._")
    if not name:
        name = "payload.iso"
    if not name.lower().endswith(".iso"):
        name = f"{name}.iso"
    return name


def _payload_iso_basename(source_path: str, payload_iso_name: str = "") -> str:
    normalized_name = payload_iso_name.strip()
    if normalized_name:
        return _safe_iso_basename(normalized_name)
    payload_iso_source = _prepared_source_payload_iso(source_path)
    if payload_iso_source:
        return _safe_iso_basename(payload_iso_source)
    return _safe_iso_basename(source_path)


def _profile_iso_family(profile: str) -> str:
    family_id = _profile_family_id(profile)
    if family_id == "kali":
        return "kali"
    return family_id


def _source_role_directory(profile: str, source_role: str, media_class: str) -> str:
    if source_role == "netboot":
        return "netboot"
    if profile == PROFILE_KALI_PURPLE and media_class == "installer":
        return "installer"
    if source_role == "netinst" or media_class == "installer":
        return "netinst"
    return "live"


def _entry_boot_role(profile: str, entry: BootEntry, source_role: str, media_class: str) -> str:
    if entry.kind.startswith("live") or entry.kind == "verify":
        return "live"
    source_directory = _source_role_directory(profile, source_role, media_class)
    if source_directory == "live":
        return "installer"
    return source_directory


def _profile_boot_asset_prefix(profile: str, asset_namespace: str = "") -> str:
    family = _profile_iso_family(profile)
    namespace_segment = _safe_path_segment(asset_namespace, "") if asset_namespace else ""
    if namespace_segment and namespace_segment != family:
        return f"/boot/{family}/{namespace_segment}"
    return f"/boot/{family}"


def _boot_asset_variant(source_path: str) -> str:
    normalized = _normalize_member_path(source_path).lower()
    if "/gtk/" in normalized:
        return "gtk"
    return ""


def _managed_isofile_path(profile: str, source_role: str, media_class: str, source_path: str) -> str:
    family = _profile_iso_family(profile)
    role = _source_role_directory(profile, source_role, media_class)
    return f"/boot/iso/{family}/{role}/{_safe_iso_basename(source_path)}"


def _managed_boot_asset_path(profile: str, role: str, source_path: str, *, initrd: bool, asset_namespace: str = "") -> str:
    normalized_source = _normalize_member_path(source_path)
    source_name = Path(normalized_source).name.strip()
    fallback = "initrd" if initrd else "vmlinuz"
    name = _safe_path_segment(source_name, fallback)
    path_parts = [_profile_boot_asset_prefix(profile, asset_namespace), role]
    variant = _boot_asset_variant(source_path)
    if variant:
        path_parts.append(variant)
    path_parts.append(name)
    return "/" + "/".join(part.strip("/") for part in path_parts if part)


def _safe_iso_stem(source_path: str) -> str:
    stem = Path(source_path).stem.strip()
    if not stem:
        stem = "payload"
    stem = _SAFE_ISO_BASENAME_RE.sub("_", stem).strip("._")
    return stem or "payload"


def _managed_installer_media_path(profile: str, source_role: str, media_class: str, source_path: str) -> str:
    family = _profile_iso_family(profile)
    role = _source_role_directory(profile, source_role, media_class)
    return f"/installer-media/{family}/{role}/{_safe_iso_stem(source_path)}"


def _prepared_source_payload_iso(source_path: str) -> str:
    source_candidate = Path(source_path).expanduser().resolve()
    if source_candidate.is_file():
        return str(source_candidate)
    if not source_candidate.is_dir():
        return ""
    payload_dir = source_candidate / "payload"
    if not payload_dir.is_dir():
        return ""
    payload_candidates = sorted(payload_dir.glob("*.iso"))
    if len(payload_candidates) > 1:
        raise ValueError(f"prepared source must contain exactly one payload ISO: {source_candidate}")
    if not payload_candidates:
        return ""
    return str(payload_candidates[0])


def _attach_iso_store_metadata(
    entries: list[BootEntry],
    *,
    profile: str,
    source_role: str,
    media_class: str,
    source_path: str,
    payload_layout: str,
    payload_iso_name: str = "",
    asset_namespace: str = "",
) -> list[BootEntry]:
    if payload_layout not in {MANAGED_PAYLOAD_LAYOUT_ISO_STORE, MANAGED_PAYLOAD_LAYOUT_SHARED_DATA}:
        return entries

    payload_iso_source = _prepared_source_payload_iso(source_path)
    if source_role == "netinst" and not payload_iso_source:
        raise ValueError(f"prepared netinst hd-media source must contain exactly one payload/*.iso: {source_path}")

    if payload_layout == MANAGED_PAYLOAD_LAYOUT_ISO_STORE:
        iso_basename = _payload_iso_basename(source_path, payload_iso_name)
        effective_iso_source = source_path
        if source_role == "netboot":
            effective_iso_source = ""
        isofile_path = (
            f"/boot/iso/{_profile_iso_family(profile)}/{_source_role_directory(profile, source_role, media_class)}/{iso_basename}"
            if effective_iso_source
            else ""
        )
    else:
        shared_root = _shared_data_root(profile, source_role, media_class)
        isofile_path = ""
        if source_role != "netboot":
            if payload_iso_source or payload_iso_name.strip():
                isofile_path = f"{shared_root}/{_payload_iso_basename(source_path, payload_iso_name)}"
    updated: list[BootEntry] = []
    for entry in entries:
        if not entry.kernel_path:
            updated.append(entry)
            continue
        loopback_live = (
            source_role == "primary"
            and media_class in {"live", "hybrid"}
            and (entry.kind.startswith("live") or entry.kind == "verify")
        )
        if loopback_live:
            kernel_args = (
                _bind_iso_store_payload(entry.kernel_args, profile, entry, isofile_path)
                if payload_layout == MANAGED_PAYLOAD_LAYOUT_ISO_STORE
                else _bind_shared_data_payload(entry.kernel_args, profile, entry, shared_root, isofile_path)
            )
            updated.append(
                replace(
                    entry,
                    isofile_path=isofile_path,
                    boot_kernel_path="",
                    boot_initrd_path="",
                    kernel_args=kernel_args,
                    installer_media_path="",
                    boot_method=_BOOT_METHOD_ISO_LOOPBACK,
                )
            )
            continue
        if source_role == "netinst":
            if entry.kernel_path != "/hd-media/vmlinuz" or entry.initrd_path != "/hd-media/initrd.gz":
                raise ValueError(
                    f"netinst entries must use separate hd-media/vmlinuz and hd-media/initrd.gz assets: {source_path}"
                )
            boot_method = _BOOT_METHOD_HD_MEDIA
        elif source_role == "netboot":
            if entry.kernel_path != "/netboot/vmlinuz" or entry.initrd_path != "/netboot/initrd.gz":
                raise ValueError(f"netboot entries must use separate netboot kernel/initrd assets: {source_path}")
            boot_method = _BOOT_METHOD_NETBOOT
        elif entry.kind in _INSTALLER_ENTRY_KINDS and isofile_path:
            boot_method = _BOOT_METHOD_INSTALLER_ISO
        else:
            boot_method = ""
        if payload_layout == MANAGED_PAYLOAD_LAYOUT_ISO_STORE:
            role = _entry_boot_role(profile, entry, source_role, media_class)
            boot_kernel_path = _managed_boot_asset_path(profile, role, entry.kernel_path, initrd=False, asset_namespace=asset_namespace)
            boot_initrd_path = (
                _managed_boot_asset_path(profile, role, entry.initrd_path, initrd=True, asset_namespace=asset_namespace)
                if entry.initrd_path
                else ""
            )
            kernel_args = _bind_iso_store_payload(entry.kernel_args, profile, entry, isofile_path)
        else:
            boot_kernel_path = f"{shared_root}/{Path(_normalize_member_path(entry.kernel_path)).name}"
            boot_initrd_path = f"{shared_root}/{Path(_normalize_member_path(entry.initrd_path)).name}" if entry.initrd_path else ""
            kernel_args = _bind_shared_data_payload(entry.kernel_args, profile, entry, shared_root, isofile_path)
        updated.append(
            replace(
                entry,
                isofile_path=isofile_path,
                boot_kernel_path=boot_kernel_path,
                boot_initrd_path=boot_initrd_path,
                kernel_args=kernel_args,
                installer_media_path="",
                boot_method=boot_method,
            )
        )
    return updated


def _iso_payload_manifest(
    profile: str,
    source_role: str,
    media_class: str,
    source_path: str,
    payload_iso_name: str = "",
    payload_layout: str = "",
) -> list[dict[str, str]]:
    if payload_layout == MANAGED_PAYLOAD_LAYOUT_SHARED_DATA:
        if source_role == "netboot":
            return []
        source_candidate = Path(source_path).expanduser().resolve()
        payload_source = (
            str(source_candidate)
            if source_role == "primary" and source_candidate.is_file() and source_candidate.suffix.lower() == ".iso"
            else _prepared_source_payload_iso(source_path)
        )
        if not payload_source:
            if source_role == "netinst":
                raise ValueError(f"prepared netinst hd-media source must contain exactly one payload/*.iso: {source_path}")
            return []
        return [
            {
                "source_path": payload_source,
                "target_path": f"{_shared_data_root(profile, source_role, media_class)}/{_payload_iso_basename(payload_source, payload_iso_name)}",
            }
        ]
    payload_source = _prepared_source_payload_iso(source_path)
    if not payload_source:
        if source_role == "netboot":
            return []
        if source_role == "netinst":
            raise ValueError(f"prepared netinst hd-media source must contain exactly one payload/*.iso: {source_path}")
        payload_source = source_path
    return [
        {
            "source_path": payload_source,
            "target_path": f"/boot/iso/{_profile_iso_family(profile)}/{_source_role_directory(profile, source_role, media_class)}/{_payload_iso_basename(payload_source, payload_iso_name)}",
        }
    ]


def _installer_media_tree_manifest(entries: list[BootEntry], source_path: str) -> list[dict[str, str]]:
    target_paths = sorted({entry.installer_media_path for entry in entries if entry.installer_media_path})
    return [{"source_path": source_path, "target_path": target_path} for target_path in target_paths]


def _payload_boot_assets_manifest(entries: list[BootEntry], source_path: str) -> list[dict[str, str]]:
    assets: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for entry in entries:
        if _entry_uses_iso_loopback(entry):
            continue
        for member_path, target_path in (
            (entry.kernel_path, entry.boot_kernel_path),
            (entry.initrd_path, entry.boot_initrd_path),
        ):
            normalized_member = _normalize_member_path(member_path)
            normalized_target = _normalize_member_path(target_path)
            if not normalized_member or not normalized_target:
                continue
            key = (normalized_member, normalized_target)
            if key in seen:
                continue
            seen.add(key)
            assets.append(
                {
                    "iso_path": source_path,
                    "source_path": normalized_member,
                    "target_path": normalized_target,
                }
            )
    return assets


def _payload_extra_assets_manifest(
    profile: str,
    source_role: str,
    media_class: str,
    source_path: str,
    payload_layout: str,
) -> list[dict[str, str]]:
    if payload_layout != MANAGED_PAYLOAD_LAYOUT_SHARED_DATA:
        return []
    # Shared Multi-OS partition 2 is an ISO store, not a merged Live medium.
    # Kernels/initrds are staged separately as signed ESP boot assets; the
    # selected Live root filesystem and package metadata stay inside its ISO.
    return []


def _installer_initrd_patch_manifest(entries: list[BootEntry]) -> list[dict[str, str]]:
    patches: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for entry in entries:
        target_path = _normalize_member_path(entry.boot_initrd_path)
        installer_media_path = entry.installer_media_path.strip()
        if entry.kind not in _INSTALLER_ENTRY_KINDS or not target_path or not installer_media_path:
            continue
        row = (target_path, installer_media_path)
        if row in seen:
            continue
        seen.add(row)
        patches.append(
            {
                "target_path": target_path,
                "installer_media_path": installer_media_path,
            }
        )
    return patches


def _render_mok_menu_entry(boot_assets_uuid: str, title: str = "MOK Enrollment") -> list[str]:
    escaped_title = _escape_grub_string(title)
    return [
        f'menuentry "{escaped_title}" {{',
        "    insmod chain",
        f"    search --no-floppy --fs-uuid --set=root {boot_assets_uuid}",
        "    echo Loading signed MokManager.",
        "    if chainloader /EFI/debian-usb/mok/mmx64.efi; then",
        "        boot",
        "    fi",
        "    echo Direct MokManager launch failed - trying shim fallback.",
        "    chainloader /EFI/debian-usb/mok/shimx64.efi",
        "    boot",
        "}",
    ]


def _render_updatevars_menu_entry(
    boot_assets_uuid: str,
    title: str = "UEFI UpdateVars",
    user_mode_title: str = "",
    setup_mode_title: str = "",
) -> list[str]:
    escaped_title = _escape_grub_string(title)
    escaped_user_mode_title = _escape_grub_string(user_mode_title or f"{title} User Mode (PK Installed) [.auth]")
    escaped_setup_mode_title = _escape_grub_string(setup_mode_title or f"{title} Setup Mode (PK Not Installed) [.esl]")
    return [
        f'submenu "{escaped_title}" {{',
        f'    menuentry "{escaped_user_mode_title}" {{',
        "        insmod chain",
        f"        search --no-floppy --fs-uuid --set=root {boot_assets_uuid}",
        "        echo Loading UpdateVars with authenticated db update.",
        "        chainloader /EFI/debian-usb/updatevars/UpdateVars.efi -a db /secureboot/db.auth",
        "        boot",
        "    }",
        "",
        f'    menuentry "{escaped_setup_mode_title}" {{',
        "        insmod chain",
        f"        search --no-floppy --fs-uuid --set=root {boot_assets_uuid}",
        "        echo Loading UpdateVars with db EFI Signature List.",
        "        chainloader /EFI/debian-usb/updatevars/UpdateVars.efi -a -e db /secureboot/db.esl",
        "        boot",
        "    }",
        "}",
    ]


def _managed_payload_skip_values(
    family_entries: dict[str, list[BootEntry]],
    boot_assets_uuid: str,
) -> tuple[list[str], list[str]]:
    fs_uuids: list[str] = []
    part_uuids: list[str] = []
    if boot_assets_uuid:
        fs_uuids.append(_validate_grub_token(boot_assets_uuid, "boot_assets_uuid"))
    for entries in family_entries.values():
        for entry in entries:
            locator = (entry.payload_uuid or "").strip()
            if not locator:
                continue
            partuuid_prefix = "/dev/disk/by-partuuid/"
            if locator.startswith(partuuid_prefix):
                part_uuids.append(_validate_grub_token(locator[len(partuuid_prefix) :], "payload partuuid"))
                continue
            if locator.startswith("/dev/"):
                continue
            fs_uuids.append(_validate_grub_token(locator, "payload uuid"))
    return _dedupe_preserving_order(fs_uuids), _dedupe_preserving_order(part_uuids)


def _render_check_signatures_restore_lines(indent: str, variable_name: str) -> list[str]:
    return [
        f'{indent}if [ -n "${{{variable_name}}}" ]; then',
        f'{" " * (len(indent) + 4)}set check_signatures="${{{variable_name}}}"',
        f"{indent}else",
        f'{" " * (len(indent) + 4)}set check_signatures=',
        f"{indent}fi",
    ]


def _render_internal_drive_submenu(
    *,
    title: str,
    entry_spec: dict[str, object],
    family_entries: dict[str, list[BootEntry]],
    boot_assets_uuid: str,
) -> list[str]:
    escaped_title = _escape_grub_string(title)
    config_paths = _internal_drive_config_paths(entry_spec)
    efi_loaders = _internal_drive_efi_loaders(entry_spec)
    skip_fs_uuids, skip_part_uuids = _managed_payload_skip_values(family_entries, boot_assets_uuid)
    lines = [
        f'submenu "{escaped_title}" {{',
        "    set debian_usb_internal_found=0",
        '    set debian_usb_scan_previous_check_signatures="${check_signatures}"',
        "    set check_signatures=no",
        "",
        "    for debian_usb_internal_dev in (hd*,gpt*) (hd*,msdos*) (ahci*,gpt*) (ahci*,msdos*) (ata*,gpt*) (ata*,msdos*) (nvme*,gpt*) (nvme*,msdos*) (virtio*,gpt*) (virtio*,msdos*); do",
        "        if regexp '^\\((hd|ahci|ata|nvme|virtio)[^,]*,(gpt|msdos)[0-9]+\\)$' \"${debian_usb_internal_dev}\"; then",
        '            set debian_usb_internal_name="${debian_usb_internal_dev}"',
        "            regexp --set=1:debian_usb_internal_name '^\\((.*)\\)$' \"${debian_usb_internal_dev}\"",
        "            set debian_usb_internal_skip=0",
        "",
        '            if [ "${debian_usb_internal_dev}" = "${root}" ]; then',
        "                set debian_usb_internal_skip=1",
        "            fi",
        '            if [ "${debian_usb_internal_name}" = "${root}" ]; then',
        "                set debian_usb_internal_skip=1",
        "            fi",
        '            if [ "${debian_usb_internal_dev}" = "${boot_root}" ]; then',
        "                set debian_usb_internal_skip=1",
        "            fi",
        '            if [ "${debian_usb_internal_name}" = "${boot_root}" ]; then',
        "                set debian_usb_internal_skip=1",
        "            fi",
        '            if [ "${debian_usb_internal_dev}" = "${trust_root}" ]; then',
        "                set debian_usb_internal_skip=1",
        "            fi",
        '            if [ "${debian_usb_internal_name}" = "${trust_root}" ]; then',
        "                set debian_usb_internal_skip=1",
        "            fi",
    ]
    if skip_fs_uuids:
        lines.extend(
            [
                "",
                '            if [ "${debian_usb_internal_skip}" = "0" ]; then',
                "                set debian_usb_internal_fsuuid=",
                '                if probe --fs-uuid --set=debian_usb_internal_fsuuid "${debian_usb_internal_dev}"; then',
            ]
        )
        for fs_uuid in skip_fs_uuids:
            escaped_uuid = _escape_grub_string(fs_uuid)
            lines.extend(
                [
                    f'                    if [ "${{debian_usb_internal_fsuuid}}" = "{escaped_uuid}" ]; then',
                    "                        set debian_usb_internal_skip=1",
                    "                    fi",
                ]
            )
        lines.extend(["                fi", "            fi"])
    if skip_part_uuids:
        lines.extend(
            [
                "",
                '            if [ "${debian_usb_internal_skip}" = "0" ]; then',
                "                set debian_usb_internal_partuuid=",
                '                if probe --part-uuid --set=debian_usb_internal_partuuid "${debian_usb_internal_dev}"; then',
            ]
        )
        for part_uuid in skip_part_uuids:
            escaped_uuid = _escape_grub_string(part_uuid)
            lines.extend(
                [
                    f'                    if [ "${{debian_usb_internal_partuuid}}" = "{escaped_uuid}" ]; then',
                    "                        set debian_usb_internal_skip=1",
                    "                    fi",
                ]
            )
        lines.extend(["                fi", "            fi"])
    lines.extend(
        [
            "",
            '            if [ "${debian_usb_internal_skip}" = "0" ]; then',
            "                set debian_usb_internal_label=",
            '                if probe --label --set=debian_usb_internal_label "${debian_usb_internal_dev}"; then',
            '                    set debian_usb_internal_label_text="${debian_usb_internal_label} (${debian_usb_internal_name})"',
            "                else",
            '                    set debian_usb_internal_label_text="${debian_usb_internal_name}"',
            "                fi",
            "",
            "                for debian_usb_internal_cfg in " + " ".join(config_paths) + "; do",
            '                    if [ -f "${debian_usb_internal_dev}${debian_usb_internal_cfg}" ]; then',
            '                        set debian_usb_internal_cfg_title="GRUB Menu"',
            '                        if [ "${debian_usb_internal_cfg}" = "/boot/grub/custom.cfg" ]; then',
            '                            set debian_usb_internal_cfg_title="Custom GRUB Menu"',
            "                        fi",
            "                        set debian_usb_internal_found=1",
            '                        menuentry "${debian_usb_internal_cfg_title} on ${debian_usb_internal_label_text}" "${debian_usb_internal_dev}" "${debian_usb_internal_cfg}" {',
            '                            set debian_usb_selected_device="${2}"',
            '                            set debian_usb_selected_path="${3}"',
            '                            set debian_usb_entry_previous_check_signatures="${check_signatures}"',
            "                            set check_signatures=no",
            '                            echo "Loading ${debian_usb_selected_path} from ${debian_usb_selected_device}."',
            '                            configfile "${debian_usb_selected_device}${debian_usb_selected_path}"',
            '                            echo "Internal GRUB config failed: ${debian_usb_selected_device}${debian_usb_selected_path}"',
            *_render_check_signatures_restore_lines("                            ", "debian_usb_entry_previous_check_signatures"),
            "                        }",
            "                    fi",
            "                done",
            "",
            "                if regexp '^\\([^,]+,(gpt|msdos)1\\)$' \"${debian_usb_internal_dev}\"; then",
        ]
    )
    for loader_title, loader_path in efi_loaders:
        escaped_loader_title = _escape_grub_string(loader_title)
        escaped_loader_path = _escape_grub_string(loader_path)
        lines.extend(
            [
                f'                    if [ -f "${{debian_usb_internal_dev}}{loader_path}" ]; then',
                "                        set debian_usb_internal_found=1",
                f'                        menuentry "{escaped_loader_title} on ${{debian_usb_internal_label_text}}" "${{debian_usb_internal_dev}}" "{escaped_loader_path}" {{',
                '                            set debian_usb_selected_device="${2}"',
                '                            set debian_usb_selected_path="${3}"',
                '                            set debian_usb_entry_previous_check_signatures="${check_signatures}"',
                "                            set check_signatures=no",
                '                            echo "Chainloading ${debian_usb_selected_path} from ${debian_usb_selected_device}."',
                '                            chainloader "${debian_usb_selected_device}${debian_usb_selected_path}"',
                "                            boot",
                '                            echo "Internal UEFI loader failed: ${debian_usb_selected_device}${debian_usb_selected_path}"',
                *_render_check_signatures_restore_lines("                            ", "debian_usb_entry_previous_check_signatures"),
                "                        }",
                "                    fi",
            ]
        )
    lines.extend(
        [
            "                fi",
            "            fi",
            "        fi",
            "    done",
            "",
            *_render_check_signatures_restore_lines("    ", "debian_usb_scan_previous_check_signatures"),
            "",
            '    if [ "${debian_usb_internal_found}" = "0" ]; then',
            '        menuentry "No internal drive boot entries detected" {',
            '            echo "No internal /boot/grub config files or first-partition UEFI loaders were detected."',
            "        }",
            "    fi",
            "}",
        ]
    )
    return lines


def _secure_boot_policy_lines(boot_assets_uuid: str) -> list[str]:
    if not boot_assets_uuid:
        return []
    return [
        *_grub_module_lines(_GRUB_CRYPTO_MODULES),
        f"search --no-floppy --fs-uuid --set=trust_root {boot_assets_uuid}",
        "trust --skip-sig (${trust_root})/secureboot/grub-signing.pub",
        "set check_signatures=enforce",
        "",
    ]


def _render_grub_entry(
    entry: BootEntry,
    live_uuid: str,
    indent: int = 0,
    *,
    boot_assets_uuid: str = "",
    asset_namespace: str = "",
) -> list[str]:
    prefix = " " * indent
    title = _escape_grub_string(entry.title)
    effective_live_uuid = entry.payload_uuid or live_uuid
    effective_asset_namespace = entry.asset_namespace or asset_namespace
    lines = [f'{prefix}menuentry "{title}" {{']
    if entry.kernel_path:
        if _entry_uses_iso_loopback(entry):
            lines.append(f"{prefix}    search --no-floppy --fs-uuid --set=root {effective_live_uuid}")
            lines.append(f'{prefix}    set isofile="{_escape_grub_string(entry.isofile_path)}"')
            command_prefix = f"{prefix}    "
            if boot_assets_uuid:
                # check_signatures=enforce authenticates the opaque ISO (and its
                # adjacent .sig) when loopback opens it. The kernel and initrd
                # are then read from that already-authenticated container; they
                # cannot carry adjacent detached signatures inside an unchanged
                # upstream ISO, so disable per-member checks only inside the
                # successful loopback branch and restore enforcement afterwards.
                lines.append(f"{prefix}    if loopback loop $isofile; then")
                lines.append(f"{prefix}        set check_signatures=no")
                command_prefix = f"{prefix}        "
            else:
                lines.append(f"{prefix}    loopback loop $isofile")
            linux_line = f"{command_prefix}linux (loop){entry.kernel_path}"
            if entry.kernel_args:
                linux_line += f" {_render_grub_kernel_args(entry.kernel_args, entry.isofile_path)}"
            lines.append(linux_line)
            if entry.initrd_path:
                lines.append(f"{command_prefix}initrd (loop){entry.initrd_path}")
            if boot_assets_uuid:
                lines.append(f"{prefix}        set check_signatures=enforce")
                lines.append(f"{prefix}    else")
                lines.append(f'{prefix}        echo "Live ISO signature verification or loopback setup failed: $isofile"')
                lines.append(f"{prefix}    fi")
            lines.append(f"{prefix}}}")
            return lines

        rendered_kernel_path = entry.boot_kernel_path or entry.kernel_path
        rendered_initrd_path = entry.boot_initrd_path or entry.initrd_path
        if boot_assets_uuid:
            if entry.isofile_path:
                lines.append(f"{prefix}    search --no-floppy --fs-uuid --set=root {effective_live_uuid}")
            lines.append(f"{prefix}    search --no-floppy --fs-uuid --set=boot_root {boot_assets_uuid}")
            lines.append(f"{prefix}    echo Loading signed kernel. Trust it via the selected Secure Boot mode before booting.")
            rendered_kernel_path = f"(${{boot_root}}){_secure_boot_asset_path(effective_asset_namespace, entry.kernel_path)}"
            rendered_initrd_path = (
                f"(${{boot_root}}){_secure_boot_asset_path(effective_asset_namespace, entry.initrd_path)}"
                if entry.initrd_path
                else ""
            )
        else:
            lines.append(f"{prefix}    search --no-floppy --fs-uuid --set=root {effective_live_uuid}")

        use_managed_variables = entry.boot_method in {
            _BOOT_METHOD_HD_MEDIA,
            _BOOT_METHOD_NETBOOT,
            _BOOT_METHOD_INSTALLER_ISO,
        }
        if use_managed_variables:
            lines.append(f'{prefix}    set kernel="{_escape_grub_string(rendered_kernel_path)}"')
            if rendered_initrd_path:
                lines.append(f'{prefix}    set initrd="{_escape_grub_string(rendered_initrd_path)}"')
            if entry.isofile_path:
                lines.append(f'{prefix}    set isofile="{_escape_grub_string(entry.isofile_path)}"')
            linux_line = f"{prefix}    linux $kernel"
        else:
            linux_line = f"{prefix}    linux {rendered_kernel_path}"
        if entry.kernel_args:
            linux_line += f" {_render_grub_kernel_args(entry.kernel_args, entry.isofile_path)}"
        lines.append(linux_line)
        if rendered_initrd_path:
            if use_managed_variables:
                lines.append(f"{prefix}    initrd $initrd")
            else:
                lines.append(f"{prefix}    initrd {rendered_initrd_path}")
    else:
        if entry.kind == "firmware" or any(command.strip() == "fwsetup" for command in entry.commands):
            lines.append(f"{prefix}    fwsetup")
        else:
            for command in entry.commands:
                lines.append(f"{prefix}    {command}")
    lines.append(f"{prefix}}}")
    return lines


def _render_grub_menu(
    entries: list[BootEntry],
    live_uuid: str,
    menu_path: tuple[str, ...] = (),
    indent: int = 0,
    *,
    boot_assets_uuid: str = "",
    asset_namespace: str = "",
) -> list[str]:
    prefix = " " * indent
    direct_entries = [entry for entry in entries if entry.menu_path == menu_path]
    child_titles = {
        entry.menu_path[len(menu_path)]
        for entry in entries
        if len(entry.menu_path) > len(menu_path) and entry.menu_path[: len(menu_path)] == menu_path
    }
    child_orders = {
        title: min(
            entry.order
            for entry in entries
            if len(entry.menu_path) > len(menu_path)
            and entry.menu_path[: len(menu_path)] == menu_path
            and entry.menu_path[len(menu_path)] == title
        )
        for title in child_titles
    }
    ordered_items: list[tuple[int, str, object]] = []
    for entry in direct_entries:
        ordered_items.append((entry.order, "entry", entry))
    for title in child_titles:
        ordered_items.append((child_orders[title], "submenu", title))
    ordered_items.sort(key=lambda item: item[0])

    rendered: list[str] = []
    for _, item_type, payload in ordered_items:
        if item_type == "entry":
            rendered.extend(
                _render_grub_entry(
                    payload,
                    live_uuid,
                    indent,
                    boot_assets_uuid=boot_assets_uuid,
                    asset_namespace=asset_namespace,
                )
            )
            continue
        title = str(payload)
        rendered.append(f'{prefix}submenu "{_escape_grub_string(title)}" {{')
        rendered.extend(
            _render_grub_menu(
                entries,
                live_uuid,
                menu_path + (title,),
                indent + 4,
                boot_assets_uuid=boot_assets_uuid,
                asset_namespace=asset_namespace,
            )
        )
        rendered.append(f"{prefix}}}")
    return rendered


def _managed_payload_module_lines(layout: str) -> list[str]:
    lines = [
        "insmod part_gpt",
        "insmod part_msdos",
        "insmod search_fs_uuid",
        "insmod fat",
        *_grub_module_lines(_GRUB_CRYPTO_MODULES),
    ]
    if layout == MANAGED_PAYLOAD_LAYOUT_RAW_ISO:
        lines.append("insmod iso9660")
    else:
        lines.append("insmod ext2")
    return lines


def _profile_family_id(profile: str) -> str:
    if profile == PROFILE_DEBIAN:
        return "debian"
    if profile == PROFILE_TAILS:
        return "tails"
    if profile in {PROFILE_UBUNTU_DESKTOP, PROFILE_UBUNTU_SERVER}:
        return "ubuntu"
    if profile in {PROFILE_KALI_LINUX, PROFILE_KALI_PURPLE}:
        return "kali"
    raise ValueError(f"unsupported profile family: {profile}")


def _load_custom_family_spec(family_id: str) -> dict[str, object]:
    filenames = {
        "debian": "debian.json",
        "tails": "tails.json",
        "ubuntu": "ubuntu.json",
        "kali": "kali-linux.json",
    }
    return _load_custom_grub_json(filenames[family_id])


def _load_custom_profile_spec(profile: str, source_role: str, media_class: str) -> dict[str, object]:
    family_spec = _load_custom_family_spec(_profile_family_id(profile))
    profiles = family_spec.get("profiles", [])
    if not isinstance(profiles, list):
        raise ValueError(f"invalid custom GRUB profile list for {profile}")
    fallback: dict[str, object] | None = None
    for item in profiles:
        if not isinstance(item, dict):
            continue
        if item.get("profile") != profile or item.get("source_role", "primary") != source_role:
            continue
        if _custom_spec_media_matches(item.get("media_class"), media_class):
            return item
        if fallback is None:
            fallback = item
    if fallback is not None:
        return fallback
    raise ValueError(f"missing custom GRUB profile spec for {profile}/{source_role}/{media_class}")


def _config_with_profile_preseed_url(config_data: dict[str, str], profile: str, url: str) -> dict[str, str]:
    updated = dict(config_data)
    updated[f"{PROFILE_PREFIXES[profile]}_PRESEED_INTERNAL_URL"] = url.strip()
    return updated


def _config_for_payload_layout(config_data: dict[str, str], profile: str, payload_layout: str) -> dict[str, str]:
    updated = dict(config_data)
    if payload_layout != MANAGED_PAYLOAD_LAYOUT_RAW_ISO:
        updated["DEFAULT_LIVE_HOOKS"] = "0"
    return updated


def _custom_spec_media_matches(spec_media: object, media_class: str) -> bool:
    if not spec_media:
        return True
    if isinstance(spec_media, str):
        return spec_media == media_class
    if isinstance(spec_media, list):
        return any(isinstance(item, str) and item == media_class for item in spec_media)
    return False


def _resolved_custom_source_role(profile: str, requested_source_role: str, media_class: str, source_path: str) -> str:
    requested_source_role = requested_source_role.strip()
    if requested_source_role:
        return requested_source_role
    source_name = Path(source_path).name.lower()
    if profile in {PROFILE_DEBIAN, PROFILE_KALI_LINUX} and media_class == "installer":
        return "netinst"
    if profile in {PROFILE_KALI_LINUX, PROFILE_KALI_PURPLE} and "netinst" in source_name:
        return "netinst"
    return "primary"


def _remove_installer_seed_transport_args(kernel_args: str) -> str:
    return _remove_kernel_args_matching(
        _collapse_whitespace(kernel_args),
        [
            r"url=.*",
            r"preseed/url=.*",
            r"url/preseed=.*",
            r"file=.*",
            r"preseed/file=.*",
            r"file/preseed=.*",
        ],
    )


def _set_installer_seed_transport(kernel_args: str, *, url: str = "", seed_file: str = "") -> str:
    url = url.strip()
    seed_file = seed_file.strip()
    if url and seed_file:
        raise ValueError("installer seed transport must use exactly one of url or file")
    args = _remove_installer_seed_transport_args(kernel_args)
    if url:
        return _merge_kernel_args(args, f"url={url}")
    if seed_file:
        return _merge_kernel_args(args, f"file={seed_file}")
    return args


def _apply_installer_seed_file_settings(kernel_args: str, config_data: dict[str, str], profile: str, seed_file: str) -> str:
    args = _remove_installer_seed_transport_args(kernel_args)
    policy_args = installer_policy_args(
        config_data,
        config_data["DEFAULT_INSTALLER_POLICY"],
        "",
        profile,
    )
    if policy_args:
        args = _merge_kernel_args(args, policy_args)
    extras = config_data["DEFAULT_INSTALLER_KERNEL_EXTRAS"]
    if extras:
        args = _merge_kernel_args(args, extras)
    profile_extras = profile_installer_kernel_extras(config_data, profile)
    if profile_extras:
        args = _merge_kernel_args(args, profile_extras)
    return _remove_live_only_kernel_args(_set_installer_seed_transport(args, seed_file=seed_file))


def _apply_manual_installer_settings(kernel_args: str, config_data: dict[str, str], profile: str) -> str:
    args = _remove_installer_seed_transport_args(kernel_args)
    args = _remove_kernel_args_matching(
        args,
        [
            r"classes=.*",
            r"dualboot=true",
            r"dualboot_efi=.*",
            r"dualboot_debian=.*",
            r"ssh_server=true",
        ],
    )
    extras = config_data["DEFAULT_INSTALLER_KERNEL_EXTRAS"]
    if extras:
        args = _merge_kernel_args(args, extras)
    profile_extras = profile_installer_kernel_extras(config_data, profile)
    if profile_extras:
        args = _merge_kernel_args(args, profile_extras)
    return _remove_live_only_kernel_args(_remove_installer_seed_transport_args(args))


def _next_custom_order(counter: list[int]) -> int:
    value = counter[0]
    counter[0] += 1
    return value


def _clone_custom_entry(
    entry: BootEntry,
    *,
    title: str,
    menu_path: tuple[str, ...],
    order: int,
    payload_uuid: str,
    asset_namespace: str,
    kernel_args: str | None = None,
) -> BootEntry:
    return replace(
        entry,
        title=title,
        menu_path=menu_path,
        order=order,
        kernel_args=kernel_args if kernel_args is not None else entry.kernel_args,
        payload_uuid=payload_uuid,
        asset_namespace=asset_namespace,
    )


def _select_preferred_entry(
    entries: list[BootEntry],
    kinds: set[str],
    preferred_titles: list[str],
    *,
    prefer_non_graphical: bool = True,
) -> BootEntry | None:
    candidates = [entry for entry in entries if entry.kind in kinds]
    if not candidates:
        return None

    def rank(entry: BootEntry) -> tuple[int, int, int, int, int, int]:
        title = entry.title.lower()
        menu_text = " / ".join(entry.menu_path).lower()
        text = f"{title} {menu_text}"
        preferred_rank = len(preferred_titles)
        for index, preferred in enumerate(preferred_titles):
            lowered = preferred.lower()
            if title == lowered or lowered in title:
                preferred_rank = index
                break
        return (
            preferred_rank,
            1 if prefer_non_graphical and "graphical" in text else 0,
            1 if "dark" in text else 0,
            1 if "speech" in text else 0,
            1 if "accessible" in text else 0,
            entry.order,
        )

    return min(candidates, key=rank)


def _select_live_entry(entries: list[BootEntry]) -> BootEntry | None:
    for entry in sorted(entries, key=_entry_sort_key):
        if entry.kind == "live":
            return entry
    return None


def _select_persistence_entry(entries: list[BootEntry]) -> BootEntry | None:
    for entry in sorted(entries, key=_entry_sort_key):
        if entry.kind in {"live-persistence", "live-encrypted-persistence"}:
            return entry
    return None


def _select_manual_normal_entry(entries: list[BootEntry], source_role: str) -> BootEntry | None:
    preferred = ["Install", "Start installer"] if source_role != "primary" else ["Start installer", "Install"]
    return _select_preferred_entry(entries, {"installer"}, preferred)


def _select_expert_entry(entries: list[BootEntry]) -> BootEntry | None:
    return _select_preferred_entry(entries, {"expert-installer"}, ["Expert install"])


def _select_rescue_entry(entries: list[BootEntry]) -> BootEntry | None:
    return _select_preferred_entry(entries, {"rescue"}, ["Rescue mode"])


def _select_automated_entry(entries: list[BootEntry]) -> BootEntry | None:
    return _select_preferred_entry(entries, {"automated-installer"}, ["Automated install"])


def _require_preseed_base_entry(
    *,
    profile: str,
    source_role: str,
    expert_entry: BootEntry | None,
    automated_entry: BootEntry | None,
    manual_normal: BootEntry | None,
) -> BootEntry:
    base_entry = expert_entry or automated_entry or manual_normal
    if base_entry is None:
        raise ValueError(f"preseed entries require a detectable installer boot entry for {profile}/{source_role}")
    return base_entry


def _custom_spec_string(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"invalid custom GRUB spec field: {field}")
    return value.strip()


def _custom_spec_bool(value: object, field: str, *, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    raise ValueError(f"invalid custom GRUB boolean field: {field}")


def _custom_preseed_section(family_spec: dict[str, object], profile_spec: dict[str, object]) -> dict[str, object]:
    value = profile_spec.get("preseed")
    if value is None:
        value = family_spec.get("preseed", {})
    if not value:
        return {}
    if not isinstance(value, dict):
        raise ValueError("invalid custom GRUB preseed section")
    return value


def _preseed_preset_specs(
    family_spec: dict[str, object],
    profile_spec: dict[str, object],
    preset_set: str = "",
) -> list[object]:
    preseed_spec = _custom_preseed_section(family_spec, profile_spec)
    presets = preseed_spec.get("preset_sets")
    if presets is None:
        presets = preseed_spec.get("presets")
    if presets is None:
        presets = profile_spec.get("preseed_presets")
    if presets is None:
        presets = family_spec.get("preseed_presets")
    if presets is None:
        presets = family_spec.get("debian_preseed_presets", [])
    if isinstance(presets, dict):
        preset_name = preset_set.strip()
        if not preset_name:
            raise ValueError("custom GRUB preseed preset_set is required when preset sets are named")
        selected = presets.get(preset_name)
        if selected is None:
            raise ValueError(f"missing custom GRUB preseed preset set: {preset_name}")
        presets = selected
    if not isinstance(presets, list):
        raise ValueError("invalid custom GRUB preseed preset list")
    return presets


def _preseed_common_args_key(family_spec: dict[str, object], profile_spec: dict[str, object]) -> str:
    value = profile_spec.get("preseed_common_args_key")
    if value is None:
        preseed_spec = _custom_preseed_section(family_spec, profile_spec)
        value = preseed_spec.get("common_args_key")
    if value is None:
        value = family_spec.get("preseed_common_args_key", "")
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError("invalid custom GRUB preseed common args key")
    return value.strip()


def _preseed_network_specs(family_spec: dict[str, object], group_spec: dict[str, object]) -> list[object]:
    variants = group_spec.get("variant_menus")
    if variants is None:
        variants = group_spec.get("variant_submenus")
    if variants is None:
        variants = group_spec.get("network_menus")
    if variants is None:
        variants = group_spec.get("network_submenus")
    if variants is None:
        variants = family_spec.get("preseed_variants", [])
    if not isinstance(variants, list) or not variants:
        raise ValueError("invalid custom GRUB preseed variant menu list")
    return variants


def _custom_preseed_transport_is_online(group_spec: dict[str, object]) -> bool:
    transport = _custom_spec_string(group_spec.get("transport"), "transport").lower()
    if transport == "http":
        return True
    if transport == "usb":
        return False
    raise ValueError(f"unsupported custom GRUB preseed transport: {transport}")


def _custom_preseed_base_entry(
    *,
    base_kind: str,
    manual_normal: BootEntry | None,
    automated_entry: BootEntry | None,
    expert_entry: BootEntry | None,
) -> BootEntry | None:
    installer_base = manual_normal or automated_entry or expert_entry
    if base_kind == "installer":
        return expert_entry or installer_base
    raise ValueError(f"unsupported custom GRUB preseed base kind: {base_kind}")


def _custom_installer_entry(
    *,
    base_kind: str,
    manual_normal: BootEntry | None,
    automated_entry: BootEntry | None,
    expert_entry: BootEntry | None,
    rescue_entry: BootEntry | None,
) -> BootEntry | None:
    if base_kind == "installer":
        return manual_normal
    if base_kind == "automated":
        return automated_entry
    if base_kind == "expert":
        return expert_entry
    if base_kind == "rescue":
        return rescue_entry
    raise ValueError(f"unsupported custom GRUB installer base kind: {base_kind}")


def _custom_spec_entries(value: object, field: str) -> list[object]:
    if not isinstance(value, list):
        raise ValueError(f"invalid custom GRUB entry list field: {field}")
    return value


def _generic_preseed_entries(
    *,
    base_entry: BootEntry | None,
    config_data: dict[str, str],
    profile: str,
    menu_path: tuple[str, ...],
    counter: list[int],
    payload_uuid: str,
    asset_namespace: str,
    title: str,
    online: bool,
) -> list[BootEntry]:
    if base_entry is None:
        return []
    seed_location = profile_preseed_url(config_data, profile) if online else profile_usb_preseed_file(config_data, profile)
    if online and not seed_location:
        return []
    if online:
        if base_entry.kind.startswith("live"):
            kernel_args = _remove_installer_seed_transport_args(base_entry.kernel_args)
            extras = config_data["DEFAULT_INSTALLER_KERNEL_EXTRAS"]
            if extras:
                kernel_args = _merge_kernel_args(kernel_args, extras)
            profile_extras = profile_installer_kernel_extras(config_data, profile)
            if profile_extras:
                kernel_args = _merge_kernel_args(kernel_args, profile_extras)
            kernel_args = _set_installer_seed_transport(kernel_args, url=seed_location)
        else:
            kernel_args = _apply_installer_settings(
                _remove_installer_seed_transport_args(base_entry.kernel_args),
                _config_with_profile_preseed_url(config_data, profile, seed_location),
                profile,
            )
    else:
        if base_entry.kind.startswith("live"):
            kernel_args = _remove_installer_seed_transport_args(base_entry.kernel_args)
            extras = config_data["DEFAULT_INSTALLER_KERNEL_EXTRAS"]
            if extras:
                kernel_args = _merge_kernel_args(kernel_args, extras)
            profile_extras = profile_installer_kernel_extras(config_data, profile)
            if profile_extras:
                kernel_args = _merge_kernel_args(kernel_args, profile_extras)
            kernel_args = _set_installer_seed_transport(kernel_args, seed_file=seed_location)
        else:
            kernel_args = _apply_installer_seed_file_settings(
                _collapse_whitespace(base_entry.kernel_args),
                config_data,
                profile,
                seed_location,
            )
    return [
        _clone_custom_entry(
            base_entry,
            title=title,
            menu_path=menu_path,
            order=_next_custom_order(counter),
            payload_uuid=payload_uuid,
            asset_namespace=asset_namespace,
            kernel_args=kernel_args,
        )
    ]


def _profile_preset_preseed_entries(
    *,
    base_entry: BootEntry | None,
    config_data: dict[str, str],
    profile: str,
    presets: list[object],
    common_args_key: str,
    preseed_url_override: str = "",
    menu_path: tuple[str, ...],
    counter: list[int],
    payload_uuid: str,
    asset_namespace: str,
    online: bool,
    variant_args: str = "",
    title_prefix: str = "",
) -> list[BootEntry]:
    if base_entry is None:
        return []
    seed_location = profile_preseed_url(config_data, profile) if online else profile_usb_preseed_file(config_data, profile)
    preseed_url_override = preseed_url_override.strip()
    if preseed_url_override:
        seed_location = preseed_url_override
    if not seed_location:
        return []
    if common_args_key and common_args_key not in config_data:
        raise ValueError(f"missing preseed common kernel args config key: {common_args_key}")
    common_args = config_data.get(common_args_key, "").strip() if common_args_key else ""
    variant_args = variant_args.strip()
    base_kernel_args = _remove_installer_seed_transport_args(base_entry.kernel_args)
    if common_args:
        base_kernel_args = _merge_kernel_args(base_kernel_args, common_args)
    extras = config_data["DEFAULT_INSTALLER_KERNEL_EXTRAS"]
    if extras:
        base_kernel_args = _merge_kernel_args(base_kernel_args, extras)
    profile_extras = profile_installer_kernel_extras(config_data, profile)
    if profile_extras:
        base_kernel_args = _merge_kernel_args(base_kernel_args, profile_extras)
    entries: list[BootEntry] = []
    for preset_index, preset in enumerate(presets):
        if not isinstance(preset, dict):
            raise ValueError("invalid custom GRUB preseed preset")
        raw_label = preset.get("label")
        if not isinstance(raw_label, str) or not raw_label.strip():
            raise ValueError(f"invalid custom GRUB spec field: preseed preset label[{preset_index}]")
        label = raw_label
        args_key = _custom_spec_string(
            preset.get("args_key"),
            f"preseed preset args_key[{preset_index}]",
        )
        if args_key not in config_data:
            raise ValueError(f"missing preseed preset kernel args config key: {args_key}")
        preset_args = config_data.get(args_key, "").strip()
        kernel_args = base_kernel_args if not preset_args else _merge_kernel_args(base_kernel_args, preset_args)
        if variant_args:
            kernel_args = _merge_kernel_args(kernel_args, variant_args)
        if online or preseed_url_override:
            kernel_args = _set_installer_seed_transport(kernel_args, url=seed_location)
        else:
            kernel_args = _set_installer_seed_transport(kernel_args, seed_file=seed_location)
        kernel_args = _remove_live_only_kernel_args(_classes_last_kernel_args(kernel_args))
        entries.append(
            _clone_custom_entry(
                base_entry,
                title=f"{title_prefix}{label}" if title_prefix else label,
                menu_path=menu_path,
                order=_next_custom_order(counter),
                payload_uuid=payload_uuid,
                asset_namespace=asset_namespace,
                kernel_args=kernel_args,
            )
        )
    return entries


def _debian_preset_preseed_entries(
    *,
    base_entry: BootEntry | None,
    config_data: dict[str, str],
    menu_path: tuple[str, ...],
    counter: list[int],
    payload_uuid: str,
    asset_namespace: str,
    online: bool,
    title_prefix: str = "",
) -> list[BootEntry]:
    family_spec = _load_custom_family_spec("debian")
    return _profile_preset_preseed_entries(
        base_entry=base_entry,
        config_data=config_data,
        profile=PROFILE_DEBIAN,
        presets=_preseed_preset_specs(family_spec, {}, "debian"),
        common_args_key=_preseed_common_args_key(family_spec, {}),
        menu_path=menu_path,
        counter=counter,
        payload_uuid=payload_uuid,
        asset_namespace=asset_namespace,
        online=online,
        title_prefix=title_prefix,
    )


def _custom_menu_prefix(include_profile_submenu: bool, profile_title: str) -> tuple[str, ...]:
    if include_profile_submenu:
        return (profile_title,)
    return ()


_FIXED_CUSTOM_GRUB_PROFILES = {PROFILE_DEBIAN, PROFILE_KALI_LINUX, PROFILE_KALI_PURPLE, PROFILE_TAILS}
_FAMILY_LEGACY_PROFILES = {PROFILE_DEBIAN, PROFILE_KALI_LINUX, PROFILE_KALI_PURPLE, PROFILE_TAILS}


def _fixed_os_title(profile: str) -> str:
    if profile == PROFILE_KALI_LINUX:
        return "Kali"
    return profile_for(profile).title


def _fixed_role_title(profile: str, source_role: str, media_class: str) -> str:
    os_title = _fixed_os_title(profile)
    resolved_source_role = _resolved_custom_source_role(profile, source_role, media_class, "")
    if resolved_source_role == "netboot":
        return f"{os_title} Netboot"
    if resolved_source_role == "netinst" or media_class == "installer":
        return f"{os_title} Netinst"
    return f"{os_title} Live"


def _fixed_live_variant(
    *,
    live_entry: BootEntry,
    profile: str,
    config_data: dict[str, str],
    title: str,
    menu_path: tuple[str, ...],
    counter: list[int],
    payload_uuid: str,
    asset_namespace: str,
    live_toram: bool,
    persistence_mode: str = PERSISTENCE_MODE_NONE,
) -> BootEntry:
    kernel_args = _apply_preserved_live_settings(live_entry, profile, config_data, live_toram)
    persistence_label, _ = profile_persistence_labels(config_data, profile)
    kernel_args = _finalize_kernel_args(profile, kernel_args, payload_uuid, persistence_mode, persistence_label)
    if live_entry.isofile_path:
        kernel_args = _bind_iso_store_payload(kernel_args, profile, live_entry, live_entry.isofile_path)
    return _clone_custom_entry(
        live_entry,
        title=title,
        menu_path=menu_path,
        order=_next_custom_order(counter),
        payload_uuid=payload_uuid,
        asset_namespace=asset_namespace,
        kernel_args=kernel_args,
    )


def _append_live_variants(
    *,
    custom_entries: list[BootEntry],
    live_entry: BootEntry | None,
    persistence_entry: BootEntry | None,
    labels: dict[str, object],
    menu_path: tuple[str, ...],
    counter: list[int],
    payload_uuid: str,
    asset_namespace: str,
    profile: str,
    config_data: dict[str, str],
) -> None:
    if live_entry is not None and "live" in labels:
        live_title = str(labels["live"])
        custom_entries.append(
            _clone_custom_entry(
                live_entry,
                title=live_title,
                menu_path=menu_path,
                order=_next_custom_order(counter),
                payload_uuid=payload_uuid,
                asset_namespace=asset_namespace,
            )
        )
        custom_entries.append(
            _fixed_live_variant(
                live_entry=live_entry,
                profile=profile,
                config_data=config_data,
                title=_label_text(labels, "live_ram") or f"{live_title} (RAM)",
                menu_path=menu_path,
                counter=counter,
                payload_uuid=payload_uuid,
                asset_namespace=asset_namespace,
                live_toram=True,
            )
        )
    if persistence_entry is not None and "persistence" in labels:
        custom_entries.append(
            _clone_custom_entry(
                persistence_entry,
                title=str(labels["persistence"]),
                menu_path=menu_path,
                order=_next_custom_order(counter),
                payload_uuid=payload_uuid,
                asset_namespace=asset_namespace,
            )
        )


def _build_fixed_debian_kali_entries(
    *,
    profile: str,
    source_role: str,
    source_path: str,
    media_class: str,
    profile_spec: dict[str, object],
    rendered_entries: list[BootEntry],
    config_data: dict[str, str],
    payload_uuid: str,
    asset_namespace: str,
    order_offset: int,
    supports_encrypted_live: bool,
) -> list[BootEntry]:
    if profile not in _FIXED_CUSTOM_GRUB_PROFILES:
        return []

    family_spec = _load_custom_family_spec(_profile_family_id(profile))
    menu_specs = profile_spec.get("menus", [])
    if not isinstance(menu_specs, list):
        raise ValueError(f"invalid custom GRUB menu list for {profile}/{source_role}")
    legacy_entry_specs = profile_spec.get("legacy_entries", [])
    if not isinstance(legacy_entry_specs, list):
        raise ValueError(f"invalid custom GRUB legacy entry list for {profile}/{source_role}")
    if not menu_specs and not legacy_entry_specs:
        return []

    resolved_source_role = _resolved_custom_source_role(profile, source_role, media_class, source_path)
    counter = [order_offset]
    custom_entries: list[BootEntry] = []
    live_entry = _select_live_entry(rendered_entries)
    manual_normal = _select_manual_normal_entry(rendered_entries, resolved_source_role)
    automated_entry = _select_automated_entry(rendered_entries)
    expert_entry = _select_expert_entry(rendered_entries)
    rescue_entry = _select_rescue_entry(rendered_entries)
    common_args_key = _preseed_common_args_key(family_spec, profile_spec)

    for menu_index, menu_spec in enumerate(menu_specs):
        if not isinstance(menu_spec, dict):
            raise ValueError(f"invalid custom GRUB menu spec for {profile}/{source_role}: index {menu_index}")
        menu_title = _custom_spec_string(menu_spec.get("title"), f"menus[{menu_index}].title")
        menu_path = (menu_title,)
        entry_specs = _custom_spec_entries(menu_spec.get("entries"), f"menus[{menu_index}].entries")
        for entry_index, custom_spec in enumerate(entry_specs):
            field_prefix = f"menus[{menu_index}].entries[{entry_index}]"
            if not isinstance(custom_spec, dict):
                raise ValueError(f"invalid custom GRUB entry spec for {profile}/{source_role}: {field_prefix}")
            entry_type = _custom_spec_string(custom_spec.get("type"), f"{field_prefix}.type")
            if entry_type == "live":
                if live_entry is None:
                    continue
                persistence_mode = str(custom_spec.get("persistence_mode") or PERSISTENCE_MODE_NONE).strip()
                if persistence_mode == "none":
                    persistence_mode = PERSISTENCE_MODE_NONE
                if persistence_mode not in {PERSISTENCE_MODE_NONE, PERSISTENCE_MODE_PLAIN, PERSISTENCE_MODE_ENCRYPTED}:
                    raise ValueError(f"invalid custom GRUB live persistence mode: {persistence_mode}")
                if persistence_mode == PERSISTENCE_MODE_ENCRYPTED and not supports_encrypted_live:
                    continue
                custom_entries.append(
                    _fixed_live_variant(
                        live_entry=live_entry,
                        profile=profile,
                        config_data=config_data,
                        title=_custom_spec_string(custom_spec.get("title"), f"{field_prefix}.title"),
                        menu_path=menu_path,
                        counter=counter,
                        payload_uuid=payload_uuid,
                        asset_namespace=asset_namespace,
                        live_toram=_custom_spec_bool(custom_spec.get("live_toram"), f"{field_prefix}.live_toram"),
                        persistence_mode=persistence_mode,
                    )
                )
                continue
            if entry_type == "installer":
                base_entry = _custom_installer_entry(
                    base_kind=_custom_spec_string(custom_spec.get("base"), f"{field_prefix}.base"),
                    manual_normal=manual_normal,
                    automated_entry=automated_entry,
                    expert_entry=expert_entry,
                    rescue_entry=rescue_entry,
                )
                if base_entry is None:
                    continue
                custom_entries.append(
                    _clone_custom_entry(
                        base_entry,
                        title=_custom_spec_string(custom_spec.get("title"), f"{field_prefix}.title"),
                        menu_path=menu_path,
                        order=_next_custom_order(counter),
                        payload_uuid=payload_uuid,
                        asset_namespace=asset_namespace,
                        kernel_args=_apply_manual_installer_settings(base_entry.kernel_args, config_data, profile),
                    )
                )
                continue
            if entry_type == "preseed-preset-menu":
                base_entry = _custom_preseed_base_entry(
                    base_kind=_custom_spec_string(custom_spec.get("base"), f"{field_prefix}.base"),
                    manual_normal=manual_normal,
                    automated_entry=automated_entry,
                    expert_entry=expert_entry,
                )
                if base_entry is None:
                    continue
                online = _custom_preseed_transport_is_online(custom_spec)
                title_prefix = _custom_spec_string(custom_spec.get("title_prefix"), f"{field_prefix}.title_prefix")
                submenu_title = _custom_spec_string(custom_spec.get("title"), f"{field_prefix}.title")
                preset_set = str(custom_spec.get("preset_set") or "").strip()
                presets = _preseed_preset_specs(family_spec, profile_spec, preset_set)
                for network_index, network_spec in enumerate(_preseed_network_specs(family_spec, custom_spec)):
                    if not isinstance(network_spec, dict):
                        raise ValueError(f"invalid custom GRUB preseed variant spec: {field_prefix}.variant_menus[{network_index}]")
                    network_title = _custom_spec_string(
                        network_spec.get("title"),
                        f"{field_prefix}.variant_menus[{network_index}].title",
                    )
                    preseed_url_key = str(network_spec.get("preseed_url_key") or "").strip()
                    preseed_url_override = ""
                    if preseed_url_key:
                        if preseed_url_key not in config_data:
                            raise ValueError(f"missing custom GRUB preseed variant URL config key: {preseed_url_key}")
                        preseed_url_override = config_data.get(preseed_url_key, "").strip()
                    variant_args_key = str(network_spec.get("args_key") or "").strip()
                    variant_args = ""
                    if variant_args_key:
                        if variant_args_key not in config_data:
                            raise ValueError(f"missing custom GRUB preseed variant args config key: {variant_args_key}")
                        variant_args = config_data.get(variant_args_key, "").strip()
                    custom_entries.extend(
                        _profile_preset_preseed_entries(
                            base_entry=base_entry,
                            config_data=config_data,
                            profile=profile,
                            presets=presets,
                            common_args_key=common_args_key,
                            preseed_url_override=preseed_url_override,
                            variant_args=variant_args,
                            menu_path=menu_path + (submenu_title, network_title),
                            counter=counter,
                            payload_uuid=payload_uuid,
                            asset_namespace=asset_namespace,
                            online=online,
                            title_prefix=title_prefix,
                        )
                    )
                continue
            raise ValueError(f"unsupported custom GRUB entry type: {entry_type}")

    legacy_menu_path = (_preserved_family_menu_title(profile),)
    for entry_index, custom_spec in enumerate(_custom_spec_entries(legacy_entry_specs, "legacy_entries")):
        field_prefix = f"legacy_entries[{entry_index}]"
        if not isinstance(custom_spec, dict):
            raise ValueError(f"invalid custom GRUB legacy entry spec for {profile}/{source_role}: {field_prefix}")
        entry_type = _custom_spec_string(custom_spec.get("type"), f"{field_prefix}.type")
        if entry_type != "installer":
            raise ValueError(f"unsupported custom GRUB legacy entry type: {entry_type}")
        base_entry = _custom_installer_entry(
            base_kind=_custom_spec_string(custom_spec.get("base"), f"{field_prefix}.base"),
            manual_normal=manual_normal,
            automated_entry=automated_entry,
            expert_entry=expert_entry,
            rescue_entry=rescue_entry,
        )
        if base_entry is None:
            continue
        custom_entries.append(
            _clone_custom_entry(
                base_entry,
                title=_custom_spec_string(custom_spec.get("title"), f"{field_prefix}.title"),
                menu_path=legacy_menu_path,
                order=_next_custom_order(counter),
                payload_uuid=payload_uuid,
                asset_namespace=asset_namespace,
                kernel_args=_apply_manual_installer_settings(base_entry.kernel_args, config_data, profile),
            )
        )

    return custom_entries


def _label_text(labels: dict[str, object], *keys: str) -> str:
    for key in keys:
        value = str(labels.get(key) or "").strip()
        if value:
            return value
    return ""


def _submenu_spec(profile_spec: dict[str, object], name: str) -> dict[str, object]:
    submenus = profile_spec.get("submenus")
    if not isinstance(submenus, dict):
        return {}
    submenu = submenus.get(name)
    if not isinstance(submenu, dict):
        return {}
    return submenu


def _append_manual_installer_entries(
    *,
    custom_entries: list[BootEntry],
    labels: dict[str, object],
    menu_path: tuple[str, ...],
    counter: list[int],
    payload_uuid: str,
    asset_namespace: str,
    config_data: dict[str, str],
    profile: str,
    manual_normal: BootEntry | None,
    expert_entry: BootEntry | None,
    rescue_entry: BootEntry | None,
) -> None:
    for label_key, base_entry in (
        ("normal", manual_normal),
        ("expert", expert_entry),
        ("rescue", rescue_entry),
    ):
        label = _label_text(labels, label_key)
        if not label or base_entry is None:
            continue
        custom_entries.append(
            _clone_custom_entry(
                base_entry,
                title=label,
                menu_path=menu_path,
                order=_next_custom_order(counter),
                payload_uuid=payload_uuid,
                asset_namespace=asset_namespace,
                kernel_args=_apply_manual_installer_settings(base_entry.kernel_args, config_data, profile),
            )
        )


def _append_preseed_submenu_entries(
    *,
    custom_entries: list[BootEntry],
    submenu: str,
    labels: dict[str, object],
    mode_prefix: str,
    manual_normal: BootEntry | None,
    expert_entry: BootEntry | None,
    rescue_entry: BootEntry | None,
    fallback_base_entry: BootEntry | None,
    config_data: dict[str, str],
    profile: str,
    menu_path: tuple[str, ...],
    counter: list[int],
    payload_uuid: str,
    asset_namespace: str,
    preseed_variant: str,
    preseed_title: str,
    online: bool,
) -> None:
    submenu = submenu.strip()
    if not submenu:
        return
    submenu_prefix = menu_path + (submenu,)
    added_labeled_entries = False
    for suffix, base_entry in (
        ("normal", manual_normal),
        ("expert", expert_entry),
        ("rescue", rescue_entry),
    ):
        label_key = f"{mode_prefix}_{suffix}" if mode_prefix else suffix
        label = _label_text(labels, label_key)
        if not label or base_entry is None:
            continue
        custom_entries.extend(
            _generic_preseed_entries(
                base_entry=base_entry,
                config_data=config_data,
                profile=profile,
                menu_path=submenu_prefix,
                counter=counter,
                payload_uuid=payload_uuid,
                asset_namespace=asset_namespace,
                title=label,
                online=online,
            )
        )
        added_labeled_entries = True
    single_entry_key = f"{mode_prefix}_entry" if mode_prefix else "entry"
    single_entry_label = _label_text(labels, single_entry_key)
    if single_entry_label:
        custom_entries.extend(
            _generic_preseed_entries(
                base_entry=fallback_base_entry,
                config_data=config_data,
                profile=profile,
                menu_path=submenu_prefix,
                counter=counter,
                payload_uuid=payload_uuid,
                asset_namespace=asset_namespace,
                title=single_entry_label,
                online=online,
            )
        )
        added_labeled_entries = True
    if preseed_variant == "debian-presets":
        custom_entries.extend(
            _debian_preset_preseed_entries(
                base_entry=fallback_base_entry,
                config_data=config_data,
                menu_path=submenu_prefix,
                counter=counter,
                payload_uuid=payload_uuid,
                asset_namespace=asset_namespace,
                online=online,
            )
        )
        return
    if added_labeled_entries:
        return
    custom_entries.extend(
        _generic_preseed_entries(
            base_entry=fallback_base_entry,
            config_data=config_data,
            profile=profile,
            menu_path=submenu_prefix,
            counter=counter,
            payload_uuid=payload_uuid,
            asset_namespace=asset_namespace,
            title=preseed_title,
            online=online,
        )
    )


def _append_shared_preseed_submenu_entries(
    *,
    custom_entries: list[BootEntry],
    submenu: str,
    fallback_base_entry: BootEntry | None,
    config_data: dict[str, str],
    profile: str,
    menu_path: tuple[str, ...],
    counter: list[int],
    payload_uuid: str,
    asset_namespace: str,
    preseed_variant: str,
    preseed_title: str,
    online: bool,
) -> None:
    submenu = submenu.strip()
    if not submenu:
        return
    submenu_prefix = menu_path + (submenu,)
    if preseed_variant == "debian-presets":
        custom_entries.extend(
            _debian_preset_preseed_entries(
                base_entry=fallback_base_entry,
                config_data=config_data,
                menu_path=submenu_prefix,
                counter=counter,
                payload_uuid=payload_uuid,
                asset_namespace=asset_namespace,
                online=online,
            )
        )
        return
    custom_entries.extend(
        _generic_preseed_entries(
            base_entry=fallback_base_entry,
            config_data=config_data,
            profile=profile,
            menu_path=submenu_prefix,
            counter=counter,
            payload_uuid=payload_uuid,
            asset_namespace=asset_namespace,
            title=preseed_title,
            online=online,
        )
    )


def _append_profile_preset_submenu_entries(
    *,
    custom_entries: list[BootEntry],
    submenu: str,
    fallback_base_entry: BootEntry | None,
    config_data: dict[str, str],
    menu_path: tuple[str, ...],
    counter: list[int],
    payload_uuid: str,
    asset_namespace: str,
    preseed_variant: str,
    online: bool,
) -> None:
    submenu = submenu.strip()
    if preseed_variant != "debian-presets" or not submenu:
        return
    custom_entries.extend(
        _debian_preset_preseed_entries(
            base_entry=fallback_base_entry,
            config_data=config_data,
            menu_path=menu_path + (submenu,),
            counter=counter,
            payload_uuid=payload_uuid,
            asset_namespace=asset_namespace,
            online=online,
        )
    )


def _preserved_menu_title(profile: str, source_role: str, media_class: str, source_path: str) -> str:
    resolved_source_role = _resolved_custom_source_role(profile, source_role, media_class, source_path)
    profile_spec = _load_custom_profile_spec(profile, resolved_source_role, media_class)
    preserved_spec = profile_spec.get("preserved")
    if isinstance(preserved_spec, dict):
        preserved_title = str(preserved_spec.get("title") or "").strip()
        if preserved_title:
            return preserved_title
    if profile in _FAMILY_LEGACY_PROFILES:
        return f"{_fixed_role_title(profile, source_role, media_class)} Legacy ..."
    profile_title = str(profile_spec.get("title") or profile_for(profile).title).strip()
    if not profile_title:
        return ""
    return f"Preserved {profile_title}"


def _preserved_entry_title(entry: BootEntry, preserved_spec: dict[str, object]) -> str:
    overrides = preserved_spec.get("entry_title_overrides", [])
    if isinstance(overrides, list):
        for item in overrides:
            if not isinstance(item, dict):
                continue
            match_title = str(item.get("match_title") or "").strip()
            title = str(item.get("title") or "").strip()
            if match_title == entry.title and title:
                return title
    template = str(preserved_spec.get("entry_title_template") or "{title}").strip()
    if not template:
        template = "{title}"
    return template.replace("{title}", entry.title)


def _preserved_family_menu_title(profile: str) -> str:
    family_spec = _load_custom_family_spec(_profile_family_id(profile))
    title = str(family_spec.get("preserved_title") or "").strip()
    if title:
        return title
    return f"{_fixed_os_title(profile)} Legacy ..."


def _preserved_include_kinds(preserved_spec: dict[str, object]) -> set[str]:
    include_kinds = preserved_spec.get("include_kinds")
    if not isinstance(include_kinds, list):
        return set()
    return {
        str(kind).strip()
        for kind in include_kinds
        if str(kind).strip()
    }


def _preserved_flatten_menu_path(preserved_spec: dict[str, object]) -> bool:
    return bool(preserved_spec.get("flatten_menu_path", False))


def build_custom_profile_preserved_entries(
    *,
    profile: str,
    source_role: str,
    source_path: str,
    media_class: str,
    rendered_entries: list[BootEntry],
    payload_uuid: str,
    asset_namespace: str,
    order_offset: int = 0,
) -> list[BootEntry]:
    resolved_source_role = _resolved_custom_source_role(profile, source_role, media_class, source_path)
    if profile in _FAMILY_LEGACY_PROFILES and resolved_source_role in {"netinst", "netboot"}:
        return []
    profile_spec = _load_custom_profile_spec(profile, resolved_source_role, media_class)
    preserved_spec = profile_spec.get("preserved")
    if not isinstance(preserved_spec, dict):
        preserved_spec = {}
    submenu_title = _preserved_menu_title(profile, source_role, media_class, source_path).strip()
    if not submenu_title:
        return []
    menu_prefix: tuple[str, ...] = (submenu_title,)
    if profile in _FAMILY_LEGACY_PROFILES:
        menu_prefix = (_preserved_family_menu_title(profile),)
    include_kinds = _preserved_include_kinds(preserved_spec)
    flatten_menu_path = _preserved_flatten_menu_path(preserved_spec)
    custom_entries: list[BootEntry] = []
    counter = order_offset
    for entry in sorted(rendered_entries, key=_entry_sort_key):
        if entry.kind == "firmware":
            continue
        if include_kinds and entry.kind not in include_kinds:
            continue
        target_menu_path = menu_prefix
        if not flatten_menu_path:
            target_menu_path = menu_prefix + tuple(entry.menu_path)
        custom_entries.append(
            replace(
                entry,
                title=_preserved_entry_title(entry, preserved_spec),
                menu_path=target_menu_path,
                order=counter,
                payload_uuid=payload_uuid,
                asset_namespace=asset_namespace,
            )
        )
        counter += 1
    return custom_entries


def build_custom_profile_entries(
    *,
    profile: str,
    source_role: str,
    source_path: str,
    media_class: str,
    entries: list[BootEntry],
    rendered_entries: list[BootEntry],
    config_data: dict[str, str],
    payload_uuid: str,
    asset_namespace: str,
    include_profile_submenu: bool,
    order_offset: int = 0,
    include_preseed_entries: bool = False,
    supports_encrypted_live: bool = False,
) -> list[BootEntry]:
    resolved_source_role = _resolved_custom_source_role(profile, source_role, media_class, source_path)
    profile_spec = _load_custom_profile_spec(profile, resolved_source_role, media_class)
    labels = profile_spec.get("labels", {})
    if not isinstance(labels, dict):
        raise ValueError(f"invalid labels in custom GRUB profile spec for {profile}/{resolved_source_role}")
    fixed_entries = _build_fixed_debian_kali_entries(
        profile=profile,
        source_role=resolved_source_role,
        source_path=source_path,
        media_class=media_class,
        profile_spec=profile_spec,
        rendered_entries=rendered_entries,
        config_data=config_data,
        payload_uuid=payload_uuid,
        asset_namespace=asset_namespace,
        order_offset=order_offset,
        supports_encrypted_live=supports_encrypted_live,
    )
    if fixed_entries:
        return fixed_entries
    manual_spec = _submenu_spec(profile_spec, "manual")
    online_spec = _submenu_spec(profile_spec, "online")
    offline_spec = _submenu_spec(profile_spec, "offline")
    menu_kind = str(profile_spec.get("menu_kind") or "").strip()
    profile_title = str(profile_spec.get("title") or profile_for(profile).title).strip()
    preseed_variant = str(profile_spec.get("preseed_variant") or "").strip()
    preseed_title = str(labels.get("preseed_default") or f"{profile_title} Preseed")
    if not include_preseed_entries:
        online_spec = {}
        offline_spec = {}
        preseed_variant = ""
    prefix = _custom_menu_prefix(include_profile_submenu, profile_title)
    counter = [order_offset]
    custom_entries: list[BootEntry] = []

    live_entry = _select_live_entry(rendered_entries)
    persistence_entry = _select_persistence_entry(rendered_entries)
    manual_normal = _select_manual_normal_entry(rendered_entries, resolved_source_role)
    expert_entry = _select_expert_entry(rendered_entries)
    rescue_entry = _select_rescue_entry(rendered_entries)
    automated_entry = _select_automated_entry(rendered_entries)
    preseed_fallback_entry = None
    if include_preseed_entries:
        preseed_fallback_entry = _require_preseed_base_entry(
            profile=profile,
            source_role=resolved_source_role,
            expert_entry=expert_entry,
            automated_entry=automated_entry,
            manual_normal=manual_normal,
        )
    if not include_preseed_entries:
        preseed_fallback_entry = None

    if menu_kind == "family-live-submenu":
        profile_prefix = (profile_title,) if profile_title else prefix
        _append_live_variants(
            custom_entries=custom_entries,
            live_entry=live_entry,
            persistence_entry=persistence_entry,
            labels=labels,
            menu_path=profile_prefix,
            counter=counter,
            payload_uuid=payload_uuid,
            asset_namespace=asset_namespace,
            profile=profile,
            config_data=config_data,
        )
        return custom_entries

    if menu_kind == "family-live-and-installer-submenus":
        profile_prefix = (profile_title,) if profile_title else prefix
        _append_live_variants(
            custom_entries=custom_entries,
            live_entry=live_entry,
            persistence_entry=persistence_entry,
            labels=labels,
            menu_path=profile_prefix,
            counter=counter,
            payload_uuid=payload_uuid,
            asset_namespace=asset_namespace,
            profile=profile,
            config_data=config_data,
        )
        manual_menu = _label_text(manual_spec, "title") or _label_text(labels, "manual_submenu")
        manual_prefix = (manual_menu,) if manual_menu else profile_prefix
        _append_manual_installer_entries(
            custom_entries=custom_entries,
            labels=manual_spec or labels,
            menu_path=manual_prefix,
            counter=counter,
            payload_uuid=payload_uuid,
            asset_namespace=asset_namespace,
            config_data=config_data,
            profile=profile,
            manual_normal=manual_normal,
            expert_entry=expert_entry,
            rescue_entry=rescue_entry,
        )
        return custom_entries

    if menu_kind == "family-installer-submenu":
        profile_prefix = (profile_title,) if profile_title else prefix
        _append_manual_installer_entries(
            custom_entries=custom_entries,
            labels=manual_spec or labels,
            menu_path=profile_prefix,
            counter=counter,
            payload_uuid=payload_uuid,
            asset_namespace=asset_namespace,
            config_data=config_data,
            profile=profile,
            manual_normal=manual_normal,
            expert_entry=expert_entry,
            rescue_entry=rescue_entry,
        )
        return custom_entries

    if menu_kind == "debian-live-with-installer":
        _append_profile_preset_submenu_entries(
            custom_entries=custom_entries,
            submenu=_label_text(online_spec, "preset_submenu"),
            fallback_base_entry=preseed_fallback_entry,
            config_data=config_data,
            menu_path=prefix,
            counter=counter,
            payload_uuid=payload_uuid,
            asset_namespace=asset_namespace,
            preseed_variant=preseed_variant,
            online=True,
        )
        _append_profile_preset_submenu_entries(
            custom_entries=custom_entries,
            submenu=_label_text(offline_spec, "preset_submenu"),
            fallback_base_entry=preseed_fallback_entry,
            config_data=config_data,
            menu_path=prefix,
            counter=counter,
            payload_uuid=payload_uuid,
            asset_namespace=asset_namespace,
            preseed_variant=preseed_variant,
            online=False,
        )
        if live_entry is not None and "live" in labels:
            custom_entries.append(
                _clone_custom_entry(
                    live_entry,
                    title=str(labels["live"]),
                    menu_path=prefix,
                    order=_next_custom_order(counter),
                    payload_uuid=payload_uuid,
                    asset_namespace=asset_namespace,
                )
            )
        if persistence_entry is not None and "persistence" in labels:
            custom_entries.append(
                _clone_custom_entry(
                    persistence_entry,
                    title=str(labels["persistence"]),
                    menu_path=prefix,
                    order=_next_custom_order(counter),
                    payload_uuid=payload_uuid,
                    asset_namespace=asset_namespace,
                )
            )
        manual_menu = _label_text(manual_spec, "title") or _label_text(labels, "manual_submenu")
        manual_prefix = prefix + (manual_menu,) if manual_menu else prefix
        _append_manual_installer_entries(
            custom_entries=custom_entries,
            labels=manual_spec or labels,
            menu_path=manual_prefix,
            counter=counter,
            payload_uuid=payload_uuid,
            asset_namespace=asset_namespace,
            config_data=config_data,
            profile=profile,
            manual_normal=manual_normal,
            expert_entry=expert_entry,
            rescue_entry=rescue_entry,
        )
        _append_preseed_submenu_entries(
            custom_entries=custom_entries,
            submenu=_label_text(online_spec, "title") or _label_text(labels, "online_submenu"),
            labels=online_spec or labels,
            mode_prefix="" if online_spec else "online",
            manual_normal=manual_normal,
            expert_entry=expert_entry,
            rescue_entry=rescue_entry,
            fallback_base_entry=preseed_fallback_entry,
            config_data=config_data,
            profile=profile,
            menu_path=prefix,
            counter=counter,
            payload_uuid=payload_uuid,
            asset_namespace=asset_namespace,
            preseed_variant=preseed_variant,
            preseed_title=preseed_title,
            online=True,
        )
        _append_preseed_submenu_entries(
            custom_entries=custom_entries,
            submenu=_label_text(offline_spec, "title") or _label_text(labels, "offline_submenu", "preseed_submenu"),
            labels=offline_spec or labels,
            mode_prefix="" if offline_spec else "offline",
            manual_normal=manual_normal,
            expert_entry=expert_entry,
            rescue_entry=rescue_entry,
            fallback_base_entry=preseed_fallback_entry,
            config_data=config_data,
            profile=profile,
            menu_path=prefix,
            counter=counter,
            payload_uuid=payload_uuid,
            asset_namespace=asset_namespace,
            preseed_variant=preseed_variant,
            preseed_title=preseed_title,
            online=False,
        )
        return custom_entries

    if menu_kind in {"live-with-installer", "live-with-offline-installer", "live-basic"}:
        _append_live_variants(
            custom_entries=custom_entries,
            live_entry=live_entry,
            persistence_entry=persistence_entry,
            labels=labels,
            menu_path=prefix,
            counter=counter,
            payload_uuid=payload_uuid,
            asset_namespace=asset_namespace,
            profile=profile,
            config_data=config_data,
        )

    if menu_kind in {"live-with-installer", "live-with-offline-installer"}:
        manual_menu = _label_text(manual_spec, "title") or _label_text(labels, "manual_submenu")
        if manual_menu:
            manual_prefix = prefix + (manual_menu,)
            _append_manual_installer_entries(
                custom_entries=custom_entries,
                labels=manual_spec or labels,
                menu_path=manual_prefix,
                counter=counter,
                payload_uuid=payload_uuid,
                asset_namespace=asset_namespace,
                config_data=config_data,
                profile=profile,
                manual_normal=manual_normal,
                expert_entry=expert_entry,
                rescue_entry=rescue_entry,
            )
        _append_preseed_submenu_entries(
            custom_entries=custom_entries,
            submenu=_label_text(online_spec, "title") or _label_text(labels, "online_submenu"),
            labels=online_spec or labels,
            mode_prefix="" if online_spec else "online",
            manual_normal=manual_normal,
            expert_entry=expert_entry,
            rescue_entry=rescue_entry,
            fallback_base_entry=preseed_fallback_entry,
            config_data=config_data,
            profile=profile,
            menu_path=prefix,
            counter=counter,
            payload_uuid=payload_uuid,
            asset_namespace=asset_namespace,
            preseed_variant=preseed_variant,
            preseed_title=preseed_title,
            online=True,
        )
        _append_preseed_submenu_entries(
            custom_entries=custom_entries,
            submenu=_label_text(offline_spec, "title") or _label_text(labels, "offline_submenu", "preseed_submenu"),
            labels=offline_spec or labels,
            mode_prefix="" if offline_spec else "offline",
            manual_normal=manual_normal,
            expert_entry=expert_entry,
            rescue_entry=rescue_entry,
            fallback_base_entry=preseed_fallback_entry,
            config_data=config_data,
            profile=profile,
            menu_path=prefix,
            counter=counter,
            payload_uuid=payload_uuid,
            asset_namespace=asset_namespace,
            preseed_variant=preseed_variant,
            preseed_title=preseed_title,
            online=False,
        )
        return custom_entries

    if menu_kind == "debian-netinst-online":
        _append_profile_preset_submenu_entries(
            custom_entries=custom_entries,
            submenu=_label_text(online_spec, "preset_submenu"),
            fallback_base_entry=automated_entry or manual_normal,
            config_data=config_data,
            menu_path=prefix,
            counter=counter,
            payload_uuid=payload_uuid,
            asset_namespace=asset_namespace,
            preseed_variant=preseed_variant,
            online=True,
        )
        _append_manual_installer_entries(
            custom_entries=custom_entries,
            labels=labels,
            menu_path=prefix,
            counter=counter,
            payload_uuid=payload_uuid,
            asset_namespace=asset_namespace,
            config_data=config_data,
            profile=profile,
            manual_normal=manual_normal,
            expert_entry=expert_entry,
            rescue_entry=rescue_entry,
        )
        _append_preseed_submenu_entries(
            custom_entries=custom_entries,
            submenu=_label_text(online_spec, "title") or _label_text(labels, "online_submenu", "online_preseed_submenu"),
            labels=online_spec or labels,
            mode_prefix="" if online_spec else "online",
            manual_normal=manual_normal,
            expert_entry=expert_entry,
            rescue_entry=rescue_entry,
            fallback_base_entry=preseed_fallback_entry,
            config_data=config_data,
            profile=profile,
            menu_path=prefix,
            counter=counter,
            payload_uuid=payload_uuid,
            asset_namespace=asset_namespace,
            preseed_variant=preseed_variant,
            preseed_title=preseed_title,
            online=True,
        )
        return custom_entries

    if menu_kind == "installer-online-offline":
        manual_menu = _label_text(manual_spec, "title") or _label_text(labels, "manual_submenu")
        manual_prefix = prefix + (manual_menu,) if manual_menu else prefix
        _append_manual_installer_entries(
            custom_entries=custom_entries,
            labels=manual_spec or labels,
            menu_path=manual_prefix,
            counter=counter,
            payload_uuid=payload_uuid,
            asset_namespace=asset_namespace,
            config_data=config_data,
            profile=profile,
            manual_normal=manual_normal,
            expert_entry=expert_entry,
            rescue_entry=rescue_entry,
        )
        _append_preseed_submenu_entries(
            custom_entries=custom_entries,
            submenu=_label_text(online_spec, "title") or _label_text(labels, "online_submenu", "online_preseed_submenu"),
            labels=online_spec or labels,
            mode_prefix="" if online_spec else "online",
            manual_normal=manual_normal,
            expert_entry=expert_entry,
            rescue_entry=rescue_entry,
            fallback_base_entry=preseed_fallback_entry,
            config_data=config_data,
            profile=profile,
            menu_path=prefix,
            counter=counter,
            payload_uuid=payload_uuid,
            asset_namespace=asset_namespace,
            preseed_variant=preseed_variant,
            preseed_title=preseed_title,
            online=True,
        )
        _append_preseed_submenu_entries(
            custom_entries=custom_entries,
            submenu=_label_text(offline_spec, "title") or _label_text(labels, "offline_submenu", "offline_preseed_submenu"),
            labels=offline_spec or labels,
            mode_prefix="" if offline_spec else "offline",
            manual_normal=manual_normal,
            expert_entry=expert_entry,
            rescue_entry=rescue_entry,
            fallback_base_entry=preseed_fallback_entry,
            config_data=config_data,
            profile=profile,
            menu_path=prefix,
            counter=counter,
            payload_uuid=payload_uuid,
            asset_namespace=asset_namespace,
            preseed_variant=preseed_variant,
            preseed_title=preseed_title,
            online=False,
        )
        return custom_entries

    if menu_kind == "live-basic":
        _append_preseed_submenu_entries(
            custom_entries=custom_entries,
            submenu=_label_text(online_spec, "title") or _label_text(labels, "online_submenu"),
            labels=online_spec or labels,
            mode_prefix="" if online_spec else "online",
            manual_normal=manual_normal,
            expert_entry=expert_entry,
            rescue_entry=rescue_entry,
            fallback_base_entry=live_entry,
            config_data=config_data,
            profile=profile,
            menu_path=prefix,
            counter=counter,
            payload_uuid=payload_uuid,
            asset_namespace=asset_namespace,
            preseed_variant=preseed_variant,
            preseed_title=preseed_title,
            online=True,
        )
        _append_preseed_submenu_entries(
            custom_entries=custom_entries,
            submenu=_label_text(offline_spec, "title") or _label_text(labels, "offline_submenu", "preseed_submenu"),
            labels=offline_spec or labels,
            mode_prefix="" if offline_spec else "offline",
            manual_normal=manual_normal,
            expert_entry=expert_entry,
            rescue_entry=rescue_entry,
            fallback_base_entry=live_entry,
            config_data=config_data,
            profile=profile,
            menu_path=prefix,
            counter=counter,
            payload_uuid=payload_uuid,
            asset_namespace=asset_namespace,
            preseed_variant=preseed_variant,
            preseed_title=preseed_title,
            online=False,
        )
        return custom_entries

    raise ValueError(f"unsupported custom GRUB menu kind: {menu_kind}")


def build_custom_profile_family_preseed_entries(
    *,
    profile: str,
    source_role: str,
    source_path: str,
    media_class: str,
    entries: list[BootEntry],
    rendered_entries: list[BootEntry],
    config_data: dict[str, str],
    payload_uuid: str,
    asset_namespace: str,
    order_offset: int = 0,
    include_online: bool = True,
    include_offline: bool = True,
    preseed_title_override: str = "",
) -> list[BootEntry]:
    resolved_source_role = _resolved_custom_source_role(profile, source_role, media_class, source_path)
    profile_spec = _load_custom_profile_spec(profile, resolved_source_role, media_class)
    labels = profile_spec.get("labels", {})
    if not isinstance(labels, dict):
        raise ValueError(f"invalid labels in custom GRUB profile spec for {profile}/{resolved_source_role}")
    online_spec = _submenu_spec(profile_spec, "online")
    offline_spec = _submenu_spec(profile_spec, "offline")
    menu_kind = str(profile_spec.get("menu_kind") or "").strip()
    if menu_kind not in {"family-live-submenu", "family-live-and-installer-submenus", "family-installer-submenu"}:
        return []
    preseed_variant = str(profile_spec.get("preseed_variant") or "").strip()
    profile_title = str(profile_spec.get("title") or profile_for(profile).title).strip()
    preseed_title = preseed_title_override.strip() or str(labels.get("preseed_default") or f"{profile_title} Preseed")
    expert_entry = _select_expert_entry(rendered_entries)
    manual_normal = _select_manual_normal_entry(rendered_entries, resolved_source_role)
    automated_entry = _select_automated_entry(rendered_entries)
    fallback_base_entry = _require_preseed_base_entry(
        profile=profile,
        source_role=resolved_source_role,
        expert_entry=expert_entry,
        automated_entry=automated_entry,
        manual_normal=manual_normal,
    )
    counter = [order_offset]
    custom_entries: list[BootEntry] = []
    if include_online:
        _append_shared_preseed_submenu_entries(
            custom_entries=custom_entries,
            submenu=_label_text(online_spec, "title") or _label_text(labels, "online_submenu", "online_preseed_submenu"),
            fallback_base_entry=fallback_base_entry,
            config_data=config_data,
            profile=profile,
            menu_path=(),
            counter=counter,
            payload_uuid=payload_uuid,
            asset_namespace=asset_namespace,
            preseed_variant=preseed_variant,
            preseed_title=preseed_title,
            online=True,
        )
    if include_offline:
        _append_shared_preseed_submenu_entries(
            custom_entries=custom_entries,
            submenu=_label_text(offline_spec, "title") or _label_text(labels, "offline_submenu", "offline_preseed_submenu", "preseed_submenu"),
            fallback_base_entry=fallback_base_entry,
            config_data=config_data,
            profile=profile,
            menu_path=(),
            counter=counter,
            payload_uuid=payload_uuid,
            asset_namespace=asset_namespace,
            preseed_variant=preseed_variant,
            preseed_title=preseed_title,
            online=False,
        )
    return custom_entries


def render_custom_main_menu(
    *,
    family_entries: dict[str, list[BootEntry]],
    module_lines: list[str],
    boot_assets_uuid: str = "",
) -> str:
    main_spec = _load_custom_grub_json("main.json")
    timeout = int(main_spec.get("timeout", -1))
    lines = [
        f"set timeout={timeout}",
        "set default=0",
        "set pager=1",
        "",
    ]
    lines.extend(_merge_grub_module_lines(module_lines, _internal_drive_module_lines(main_spec)))
    lines.append("")
    lines.extend(_secure_boot_policy_lines(boot_assets_uuid))

    top_level_families = main_spec.get("top_level_families", [])
    if isinstance(top_level_families, list):
        for family in top_level_families:
            if not isinstance(family, dict):
                continue
            family_id = str(family.get("id") or "").strip()
            title = str(family.get("title") or "").strip()
            entries = family_entries.get(family_id, [])
            if not family_id or not title or not entries:
                continue
            lines.append(f'submenu "{_escape_grub_string(title)}" {{')
            lines.extend(_render_grub_menu(entries, "", indent=4, boot_assets_uuid=boot_assets_uuid))
            lines.append("}")
            lines.append("")

    static_entries = main_spec.get("static_entries", [])
    if isinstance(static_entries, list):
        for item in static_entries:
            if not isinstance(item, dict):
                continue
            entry_id = str(item.get("id") or "").strip()
            title = str(item.get("title") or "").strip()
            if not entry_id or not title:
                continue
            if entry_id == "internal-drive":
                lines.extend(
                    _render_internal_drive_submenu(
                        title=title,
                        entry_spec=item,
                        family_entries=family_entries,
                        boot_assets_uuid=boot_assets_uuid,
                    )
                )
                lines.append("")
                continue
            if entry_id == "mok":
                if boot_assets_uuid:
                    lines.extend(_render_mok_menu_entry(boot_assets_uuid, title))
                    lines.append("")
                continue
            if entry_id == "updatevars":
                if boot_assets_uuid and _updatevars_efi_available():
                    updatevars_title, user_title, setup_title = _updatevars_menu_labels(main_spec)
                    lines.extend(_render_updatevars_menu_entry(boot_assets_uuid, updatevars_title, user_title, setup_title))
                    lines.append("")
                continue
            if entry_id == "uefi":
                lines.extend(
                    _render_grub_entry(
                        BootEntry(
                            title=title,
                            kernel_path="",
                            initrd_path="",
                            kernel_args="",
                            source="generated/uefi",
                            kind="firmware",
                            commands=("fwsetup",),
                        ),
                        "",
                    )
                )
                lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def render_managed_grub(
    *,
    source_path: str,
    profile: str,
    live_uuid: str,
    persistence: bool,
    persistence_mode: str = "",
    config_path: str = "",
    kernel_args_override: str = "",
    kernel_path_override: str = "",
    initrd_path_override: str = "",
    menu_label_override: str = "",
    live_toram: bool | None = None,
    boot_assets_uuid: str = "",
    boot_assets_namespace: str = "",
    use_custom_menu: bool = False,
    preserve_upstream_entries: bool = False,
    include_preseed_entries: bool = False,
    source_role: str = "",
) -> dict[str, object]:
    profile_meta = profile_for(profile)
    source = open_source(source_path)
    entries = _filter_entries_for_profile(profile, _find_boot_entries(source))
    if "uefi" not in _detect_firmware(source):
        raise ValueError(f"managed mode requires UEFI-capable media: {source.display_path}")
    if not entries:
        raise ValueError(f"no managed boot entries were detected in {source.display_path}")
    if include_preseed_entries and not use_custom_menu:
        raise ValueError("preseed entries require custom GRUB mode")
    detected_media_class = _media_class(entries)
    source_role = _resolved_custom_source_role(profile, source_role, detected_media_class, source.display_path)
    entries = _filter_entries_for_source_role(profile, source_role, entries)
    if not entries:
        raise ValueError(f"no {source_role} boot entries were detected in {source.display_path}")

    effective_persistence_mode = persistence_mode.strip() or (PERSISTENCE_MODE_PLAIN if persistence else PERSISTENCE_MODE_NONE)
    if effective_persistence_mode not in {PERSISTENCE_MODE_NONE, PERSISTENCE_MODE_PLAIN, PERSISTENCE_MODE_ENCRYPTED}:
        raise ValueError("persistence mode must be one of: plain, encrypted")
    if profile == PROFILE_TAILS and effective_persistence_mode == PERSISTENCE_MODE_PLAIN:
        raise ValueError("Tails persistence must be encrypted")
    source_supports_encrypted_live = _supports_encrypted_persistence(source, entries, profile, profile_meta)
    if effective_persistence_mode == PERSISTENCE_MODE_ENCRYPTED and not source_supports_encrypted_live:
        raise ValueError(f"encrypted persistence is not supported for profile {profile} with {source.display_path}")
    if effective_persistence_mode == PERSISTENCE_MODE_PLAIN:
        entries = [entry for entry in entries if entry.kind != "live-encrypted-persistence"]
    elif effective_persistence_mode == PERSISTENCE_MODE_ENCRYPTED:
        entries = [entry for entry in entries if entry.kind != "live-persistence"]
    else:
        entries = [entry for entry in entries if entry.kind not in {"live-persistence", "live-encrypted-persistence"}]

    config_data = _load_effective_config(config_path)
    payload_layout = effective_managed_payload_layout(
        profile,
        profile_meta.managed_payload_layout,
        config_data,
        use_custom_menu,
    )
    if source_role in {"netinst", "netboot"}:
        payload_layout = MANAGED_PAYLOAD_LAYOUT_ISO_STORE
    config_data = _config_for_payload_layout(config_data, profile, payload_layout)
    config_data[_LIVE_TORAM_MODULE_CONFIG_KEY] = _live_toram_module_for_source(source, profile)
    live_entries = [entry for entry in entries if entry.kind.startswith("live")]
    installer_entries = [
        entry for entry in entries if entry.kind in {"installer", "automated-installer", "expert-installer", "rescue"}
    ]
    media_class = _media_class(entries)
    live_capable_media = media_class in {"live", "hybrid"}

    best_live = _select_entry(profile, live_entries)
    best_installer = _select_base_installer_entry(profile, installer_entries)
    persistence_fs_label, _ = profile_persistence_labels(config_data, profile)
    rendered_entries = [
        _adapt_entry_for_managed(
            entry,
            profile,
            live_uuid,
            config_data,
            effective_persistence_mode,
            payload_layout,
            persistence_fs_label,
            live_toram=live_toram,
        )
        for entry in entries
    ]
    synthetic_entries: list[BootEntry] = []
    earliest_order = min(entry.order for entry in rendered_entries)

    if profile_meta.preferred_media == "live" and live_capable_media:
        if (kernel_args_override or kernel_path_override or initrd_path_override or menu_label_override) and (
            best_live is None and not kernel_path_override
        ):
            raise ValueError(f"unable to detect a live boot entry in {source.display_path}; supply a kernel override")
        if kernel_args_override or kernel_path_override or initrd_path_override or menu_label_override:
            synthetic_entries.append(
                _synthetic_live_entry(
                    source=source,
                    profile=profile,
                    config_data=config_data,
                    live_uuid=live_uuid,
                    persistence_mode=PERSISTENCE_MODE_NONE,
                    template=best_live,
                    kernel_args_override=kernel_args_override,
                        kernel_path_override=kernel_path_override,
                        initrd_path_override=initrd_path_override,
                        menu_label_override=menu_label_override,
                        order=earliest_order - 20,
                        live_toram=live_toram,
                    )
                )
            if effective_persistence_mode in {PERSISTENCE_MODE_PLAIN, PERSISTENCE_MODE_ENCRYPTED} and profile_meta.supports_persistence:
                synthetic_entries.append(
                    _synthetic_live_entry(
                        source=source,
                        profile=profile,
                        config_data=config_data,
                        live_uuid=live_uuid,
                        persistence_mode=effective_persistence_mode,
                        template=best_live,
                        kernel_args_override=kernel_args_override,
                        kernel_path_override=kernel_path_override,
                        initrd_path_override=initrd_path_override,
                        menu_label_override=menu_label_override,
                        order=earliest_order - 19,
                        persistence_label=persistence_fs_label,
                        live_toram=live_toram,
                    )
                )
        elif effective_persistence_mode in {PERSISTENCE_MODE_PLAIN, PERSISTENCE_MODE_ENCRYPTED} and profile_meta.supports_persistence and best_live is not None and not any(
            entry.kind == ("live-persistence" if effective_persistence_mode == PERSISTENCE_MODE_PLAIN else "live-encrypted-persistence")
            for entry in rendered_entries
        ):
            synthetic_entries.append(
                _synthetic_live_entry(
                    source=source,
                    profile=profile,
                    config_data=config_data,
                    live_uuid=live_uuid,
                    persistence_mode=effective_persistence_mode,
                    template=best_live,
                    kernel_args_override="",
                    kernel_path_override="",
                    initrd_path_override="",
                    menu_label_override="",
                    order=best_live.order + 1,
                    persistence_label=persistence_fs_label,
                    live_toram=live_toram,
                )
            )

    rendered_entries.extend(synthetic_entries)
    rendered_entries.sort(key=_entry_sort_key)
    rendered_entries = _attach_iso_store_metadata(
        rendered_entries,
        profile=profile,
        source_role=source_role,
        media_class=media_class,
        source_path=source.display_path,
        payload_layout=payload_layout,
    )
    asset_namespace = boot_assets_namespace or profile
    signed_kernel_assets: list[dict[str, str]] = []
    boot_initrd_assets: list[dict[str, str]] = []
    if boot_assets_uuid and not use_custom_menu:
        rendered_entries = _filter_entries_for_secure_boot(rendered_entries)
        signed_kernel_assets = _signed_kernel_assets(rendered_entries, asset_namespace, source.display_path)
        boot_initrd_assets = _boot_initrd_assets(rendered_entries, asset_namespace, source.display_path)

    if use_custom_menu:
        if include_preseed_entries and media_class != "installer" and profile not in _FIXED_CUSTOM_GRUB_PROFILES:
            raise ValueError("preseed entries require an installer or netinst media source")
        if boot_assets_uuid:
            rendered_entries = _filter_entries_for_secure_boot(rendered_entries)
        custom_entries = build_custom_profile_entries(
            profile=profile,
            source_role=source_role,
            source_path=source.display_path,
            media_class=media_class,
            entries=entries,
            rendered_entries=rendered_entries,
            config_data=config_data,
            payload_uuid=live_uuid,
            asset_namespace=asset_namespace,
            include_profile_submenu=False,
            include_preseed_entries=include_preseed_entries,
            supports_encrypted_live=source_supports_encrypted_live,
        )
        if include_preseed_entries and profile not in _FIXED_CUSTOM_GRUB_PROFILES:
            custom_entries.extend(
                build_custom_profile_family_preseed_entries(
                    profile=profile,
                    source_role="netinst",
                    source_path=source.display_path,
                    media_class=media_class,
                    entries=entries,
                    rendered_entries=rendered_entries,
                    config_data=config_data,
                    payload_uuid=live_uuid,
                    asset_namespace=asset_namespace,
                    order_offset=max((entry.order for entry in custom_entries), default=-1) + 10,
                )
            )
        if preserve_upstream_entries:
            custom_entries.extend(
                build_custom_profile_preserved_entries(
                    profile=profile,
                    source_role=source_role,
                    source_path=source.display_path,
                    media_class=media_class,
                    rendered_entries=rendered_entries,
                    payload_uuid=live_uuid,
                    asset_namespace=asset_namespace,
                    order_offset=max((entry.order for entry in custom_entries), default=-1) + 10,
                )
            )
        if boot_assets_uuid:
            signed_kernel_assets = _signed_kernel_assets(custom_entries, asset_namespace, source.display_path)
            boot_initrd_assets = _boot_initrd_assets(custom_entries, asset_namespace, source.display_path)
        grub_cfg = render_custom_main_menu(
            family_entries={_profile_family_id(profile): custom_entries},
            module_lines=_managed_payload_module_lines(payload_layout),
            boot_assets_uuid=boot_assets_uuid,
        )
        return {
            "grub_cfg": grub_cfg,
            "entry_count": len(custom_entries),
            "media_class": media_class,
            "source_path": source.display_path,
            "source_type": source.source_type,
            "top_level_entries": [profile_for(profile).title],
            "signed_kernel_assets": signed_kernel_assets,
            "boot_initrd_assets": boot_initrd_assets,
            "installer_boot_initrd_patches": _installer_boot_initrd_patch_assets(custom_entries, asset_namespace, source.display_path),
            "iso_payloads": _iso_payload_manifest(
                profile,
                source_role,
                media_class,
                source.display_path,
                payload_layout=payload_layout,
            ),
            "installer_media_trees": _installer_media_tree_manifest(custom_entries, source.display_path),
            "payload_boot_assets": _payload_boot_assets_manifest(custom_entries, source.display_path),
            "payload_extra_assets": _payload_extra_assets_manifest(profile, source_role, media_class, source.display_path, payload_layout),
            "installer_initrd_patches": _installer_initrd_patch_manifest(custom_entries),
        }

    lines = [
        "set timeout=-1",
        "set default=0",
        "set pager=1",
        "",
    ]
    lines.extend(_managed_payload_module_lines(payload_layout))
    lines.append("")
    lines.extend(_secure_boot_policy_lines(boot_assets_uuid))
    lines.extend(
        _render_grub_menu(
            rendered_entries,
            live_uuid,
            boot_assets_uuid=boot_assets_uuid,
            asset_namespace=asset_namespace,
        )
    )
    if boot_assets_uuid:
        lines.append("")
        lines.extend(_render_mok_menu_entry(boot_assets_uuid, _configured_static_entry_title("mok", "MOK Enrollment")))
        if _updatevars_efi_available():
            lines.append("")
            updatevars_title, user_title, setup_title = _configured_updatevars_menu_labels()
            lines.extend(_render_updatevars_menu_entry(boot_assets_uuid, updatevars_title, user_title, setup_title))
    grub_cfg = "\n".join(lines) + "\n"

    return {
        "grub_cfg": grub_cfg,
        "entry_count": len(rendered_entries),
        "media_class": _media_class(entries),
        "source_path": source.display_path,
        "source_type": source.source_type,
        "top_level_entries": list(dict.fromkeys(entry.menu_path[0] if entry.menu_path else entry.title for entry in rendered_entries)),
        "signed_kernel_assets": signed_kernel_assets,
        "boot_initrd_assets": boot_initrd_assets,
        "installer_boot_initrd_patches": _installer_boot_initrd_patch_assets(rendered_entries, asset_namespace, source.display_path),
        "iso_payloads": _iso_payload_manifest(
            profile,
            source_role,
            media_class,
            source.display_path,
            payload_layout=payload_layout,
        ),
        "installer_media_trees": _installer_media_tree_manifest(rendered_entries, source.display_path),
        "payload_boot_assets": _payload_boot_assets_manifest(rendered_entries, source.display_path),
        "payload_extra_assets": _payload_extra_assets_manifest(profile, source_role, media_class, source.display_path, payload_layout),
        "installer_initrd_patches": _installer_initrd_patch_manifest(rendered_entries),
    }
