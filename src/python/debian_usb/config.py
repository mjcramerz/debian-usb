from __future__ import annotations

from collections import OrderedDict
import json
import os
import re
from pathlib import Path

from .bootpolicy import valid_installer_policy, valid_live_boot_policy
from .constants import (
    MANAGED_PAYLOAD_LAYOUT_EXTRACTED,
    MANAGED_PAYLOAD_LAYOUT_ISO_STORE,
    MANAGED_PAYLOAD_LAYOUT_RAW_ISO,
    MANAGED_PAYLOAD_LAYOUT_SHARED_DATA,
    PROFILE_DEBIAN,
    PROFILE_KALI_LINUX,
    PROFILE_KALI_PURPLE,
    PROFILE_TAILS,
    PROFILE_UBUNTU_DESKTOP,
    PROFILE_UBUNTU_SERVER,
    PROFILE_KEYS,
)

LIVE_OVERRIDE_PROFILES = (
    PROFILE_DEBIAN,
    PROFILE_UBUNTU_DESKTOP,
    PROFILE_UBUNTU_SERVER,
    PROFILE_KALI_LINUX,
    PROFILE_TAILS,
)

INSTALLER_OVERRIDE_PROFILES = (
    PROFILE_DEBIAN,
    PROFILE_UBUNTU_DESKTOP,
    PROFILE_UBUNTU_SERVER,
    PROFILE_KALI_LINUX,
    PROFILE_KALI_PURPLE,
)

PRESEED_URL_PROFILES = (
    PROFILE_DEBIAN,
    PROFILE_KALI_LINUX,
    PROFILE_KALI_PURPLE,
)
FORENSICS_OVERRIDE_PROFILES = LIVE_OVERRIDE_PROFILES
FALLBACK_LIVE_KERNEL_ARG_PROFILES = LIVE_OVERRIDE_PROFILES
PROFILE_PREFIXES = {profile: profile.upper().replace("-", "_") for profile in PROFILE_KEYS}
TEMPLATE_CONFIG_ENV = "DEBIAN_USB_TEMPLATE_CONFIG"
CUSTOM_GRUB_SPEC_ENV = "DEBIAN_USB_SPEC_DIR"
LEGACY_KEY_ALIASES = {
    "KALI_LIVE_KERNEL_EXTRAS": "KALI_LINUX_LIVE_KERNEL_EXTRAS",
    "KALI_INSTALLER_KERNEL_EXTRAS": "KALI_LINUX_INSTALLER_KERNEL_EXTRAS",
    "DEBIAN_PRESEED_URL": "DEBIAN_DE_PRESEED_INTERNAL_URL",
    "KALI_PRESEED_URL": "KALI_LINUX_DE_PRESEED_INTERNAL_URL",
    "KALI_LINUX_PRESEED_URL": "KALI_LINUX_DE_PRESEED_INTERNAL_URL",
    "KALI_PURPLE_PRESEED_URL": "KALI_PURPLE_PRESEED_INTERNAL_URL",
}
LEGACY_KEY_ALIASES.update({
    'PRESEED_USB_DEBIAN_FILE': 'PRESEED_USB_DEBIAN_DE_FILE',
    'PRESEED_USB_KALI_FILE': 'PRESEED_USB_KALI_DE_FILE',
    'PRESEED_HOST_DEBIAN_PATH': 'PRESEED_HOST_DEBIAN_DE_PATH',
    'PRESEED_HOST_KALI_PATH': 'PRESEED_HOST_KALI_DE_PATH',
    'DEBIAN_PRESEED_INTERNAL_URL': 'DEBIAN_DE_PRESEED_INTERNAL_URL',
    'DEBIAN_PRESEED_PUBLIC_URL': 'DEBIAN_DE_PRESEED_PUBLIC_URL',
    'DEBIAN_PRESEED_INTERNAL_ARGS': 'DEBIAN_DE_PRESEED_INTERNAL_ARGS',
    'DEBIAN_PRESEED_PUBLIC_ARGS': 'DEBIAN_DE_PRESEED_PUBLIC_ARGS',
    'KALI_LINUX_PRESEED_INTERNAL_URL': 'KALI_LINUX_DE_PRESEED_INTERNAL_URL',
    'PRESEED_ONE_ARGS_DEBIAN': 'PRESEED_ONE_ARGS_DEBIAN_DE',
    'PRESEED_TWO_ARGS_DEBIAN': 'PRESEED_TWO_ARGS_DEBIAN_DE',
    'PRESEED_THREE_ARGS_DEBIAN': 'PRESEED_THREE_ARGS_DEBIAN_DE',
    'PRESEED_FOUR_ARGS_DEBIAN': 'PRESEED_FOUR_ARGS_DEBIAN_DE',
    'PRESEED_FIVE_ARGS_DEBIAN': 'PRESEED_FIVE_ARGS_DEBIAN_DE',
    'PRESEED_SIX_ARGS_DEBIAN': 'PRESEED_ONE_ARGS_DEBIAN_SRV',
    'PRESEED_SEVEN_ARGS_DEBIAN': 'PRESEED_TWO_ARGS_DEBIAN_SRV',
    'PRESEED_EIGHT_ARGS_DEBIAN': 'PRESEED_THREE_ARGS_DEBIAN_SRV',
    'PRESEED_NINE_ARGS_DEBIAN': 'PRESEED_FOUR_ARGS_DEBIAN_SRV',
    'PRESEED_ONE_ARGS_KALI': 'PRESEED_ONE_ARGS_KALI_DE',
    'PRESEED_TWO_ARGS_KALI': 'PRESEED_TWO_ARGS_KALI_DE',
    'PRESEED_THREE_ARGS_KALI': 'PRESEED_THREE_ARGS_KALI_DE',
    'PRESEED_FOUR_ARGS_KALI': 'PRESEED_FOUR_ARGS_KALI_DE',
    'PRESEED_FIVE_ARGS_KALI': 'PRESEED_FIVE_ARGS_KALI_DE',
    'PRESEED_SIX_ARGS_KALI': 'PRESEED_ONE_ARGS_KALI_SRV',
    'PRESEED_SEVEN_ARGS_KALI': 'PRESEED_TWO_ARGS_KALI_SRV',
    'PRESEED_EIGHT_ARGS_KALI': 'PRESEED_THREE_ARGS_KALI_SRV',
    'PRESEED_NINE_ARGS_KALI': 'PRESEED_FOUR_ARGS_KALI_SRV',
})


def profile_preseed_internal_key(profile: str) -> str:
    prefix = PROFILE_PREFIXES[profile]
    if profile in {"debian", "kali-linux"}:
        prefix += "_DE"
    return f"{prefix}_PRESEED_INTERNAL_URL"

MANAGED_SOURCE_URL_BASE_KEYS = (
    "DEBIAN_LIVE_ISO_URL",
    "DEBIAN_NETINST_ISO_URL",
    "DEBIAN_NETINST_VMLINUZ_URL",
    "DEBIAN_NETINST_INITRD_URL",
    "DEBIAN_NETBOOT_VMLINUZ_URL",
    "DEBIAN_NETBOOT_INITRD_URL",
    "KALI_LIVE_ISO_URL",
    "KALI_NETINST_ISO_URL",
    "KALI_NETINST_VMLINUZ_URL",
    "KALI_NETINST_INITRD_URL",
    "KALI_PURPLE_ISO_URL",
    "KALI_NETBOOT_VMLINUZ_URL",
    "KALI_NETBOOT_INITRD_URL",
    "TAILS_LIVE_ISO_URL",
)
MANAGED_SOURCE_RELEASE_CHANNELS = ("STABLE", "TESTING")
MANAGED_SOURCE_URL_KEYS = tuple(
    f"{base_key.removesuffix('_URL')}_{channel}_URL"
    for base_key in MANAGED_SOURCE_URL_BASE_KEYS
    for channel in MANAGED_SOURCE_RELEASE_CHANNELS
)
PRESEED_NUMBER_NAMES = (
    "ONE",
    "TWO",
    "THREE",
    "FOUR",
    "FIVE",
    "SIX",
    "SEVEN",
    "EIGHT",
    "NINE",
)
LEGACY_SECRET_KERNEL_ARG_NAMES = frozenset(
    {
        "fruux_username",
        "fruux_password",
        "primary_user",
        "primary_password",
        "primary_gpg_passphrase",
        "root_password",
        "crowdsec_token",
        "tailscale_authkey",
        "telegram_chat_id",
        "telegram_api_key",
        "cf_r2_access_key",
        "cf_r2_secret_key",
        "obs_username",
        "obs_password",
    }
)
LEGACY_SECRET_KERNEL_ARG_DESTINATION = "initrd/debian/netinst/desktop/preseed.env"
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
LIVE_WIFI_SECRET_KERNEL_ARG_DESTINATION = "initrd/debian/live/live.env"

PARTITION_LABEL_CONFIG_KEYS = (
    "DEFAULT_ESP_LABEL",
    "DEFAULT_MULTI_DATA_LABEL",
    "DEFAULT_DEBIAN_PERSIST_LABEL",
    "DEFAULT_KALI_PERSIST_LABEL",
    "DEFAULT_TAILS_PERSIST_LABEL",
    "DEFAULT_DEBIAN_LIVE_LABEL",
    "DEFAULT_DEBIAN_NETINST_LABEL",
    "DEFAULT_DEBIAN_NETBOOT_LABEL",
    "DEFAULT_KALI_LIVE_LABEL",
    "DEFAULT_KALI_NETINST_LABEL",
    "DEFAULT_KALI_NETBOOT_LABEL",
    "DEFAULT_KALI_PURPLE_NETINST_LABEL",
    "DEFAULT_TAILS_LIVE_LABEL",
    "DEFAULT_UBUNTU_LIVE_LABEL",
    "DEFAULT_UBUNTU_NETINST_LABEL",
    "DEFAULT_UBUNTU_PERSIST_LABEL",
    "DEFAULT_UBUNTU_PERSIST_PARTLABEL",
)

MULTIOS_PARTITION_LABEL_CONFIG_KEYS = (
    "DEFAULT_ESP_LABEL",
    "DEFAULT_MULTI_DATA_LABEL",
    "DEFAULT_DEBIAN_PERSIST_LABEL",
    "DEFAULT_KALI_PERSIST_LABEL",
    "DEFAULT_TAILS_PERSIST_LABEL",
)

SINGLE_OS_PARTITION_LABEL_CONFIG_KEYS = (
    "DEFAULT_DEBIAN_LIVE_LABEL",
    "DEFAULT_DEBIAN_NETINST_LABEL",
    "DEFAULT_DEBIAN_NETBOOT_LABEL",
    "DEFAULT_KALI_LIVE_LABEL",
    "DEFAULT_KALI_NETINST_LABEL",
    "DEFAULT_KALI_NETBOOT_LABEL",
    "DEFAULT_KALI_PURPLE_NETINST_LABEL",
    "DEFAULT_TAILS_LIVE_LABEL",
    "DEFAULT_UBUNTU_LIVE_LABEL",
    "DEFAULT_UBUNTU_NETINST_LABEL",
    "DEFAULT_UBUNTU_PERSIST_LABEL",
    "DEFAULT_UBUNTU_PERSIST_PARTLABEL",
)

def _numbered_preseed_arg_keys(os_name: str) -> tuple[str, ...]:
    return tuple(f"PRESEED_{number_name}_ARGS_{os_name}_{suffix}" for suffix in ("DE", "SRV") for number_name in PRESEED_NUMBER_NAMES)


def _custom_grub_spec_candidates() -> tuple[Path, ...]:
    candidates: list[Path] = []
    env_path = os.environ.get(CUSTOM_GRUB_SPEC_ENV, "").strip()
    if env_path:
        candidates.append(Path(env_path).expanduser())
    module_path = Path(__file__).resolve()
    if len(module_path.parents) > 3:
        candidates.append(module_path.parents[3] / "configs" / "spec" / "grub")
    candidates.append(Path("/usr/lib/debian-usb/spec/grub"))

    unique: list[Path] = []
    seen: set[Path] = set()
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        unique.append(candidate)
    return tuple(unique)


def _custom_grub_spec_dir() -> Path | None:
    for candidate in _custom_grub_spec_candidates():
        if candidate.is_dir():
            return candidate
    return None


def _collect_spec_config_keys(value: object, collected: set[str]) -> None:
    if isinstance(value, dict):
        for key, nested in value.items():
            if key in {"args_key", "common_args_key", "preseed_common_args_key"}:
                normalized = str(nested or "").strip()
                if normalized:
                    collected.add(normalized)
            _collect_spec_config_keys(nested, collected)
        return
    if isinstance(value, list):
        for item in value:
            _collect_spec_config_keys(item, collected)


def _spec_referenced_config_keys() -> tuple[str, ...]:
    spec_dir = _custom_grub_spec_dir()
    if spec_dir is None:
        return _numbered_preseed_arg_keys("DEBIAN") + _numbered_preseed_arg_keys("KALI")
    collected: set[str] = set()
    for spec_path in sorted(spec_dir.glob("*.json")):
        if not spec_path.is_file():
            continue
        _collect_spec_config_keys(
            json.loads(spec_path.read_text(encoding="utf-8")),
            collected,
        )
    return tuple(sorted(collected))


def _spec_referenced_kernel_arg_keys() -> tuple[str, ...]:
    specialized_keys = {
        "PRESEED_COMMON_KERNEL_ARGS",
        "DEBIAN_DE_PRESEED_PUBLIC_ARGS",
        "DEBIAN_DE_PRESEED_INTERNAL_ARGS",
    }
    return tuple(
        key
        for key in _spec_referenced_config_keys()
        if key not in specialized_keys
    )


PRESEED_USB_FILE_KEYS = {
    PROFILE_DEBIAN: "PRESEED_USB_DEBIAN_DE_FILE",
    PROFILE_KALI_LINUX: "PRESEED_USB_KALI_DE_FILE",
    PROFILE_KALI_PURPLE: "PRESEED_USB_PURPLE_FILE",
}

PRESEED_HOST_PATH_KEYS = {
    PROFILE_KALI_PURPLE: "PRESEED_HOST_PURPLE_PATH",
}

CONFIG_SECTIONS: tuple[tuple[str, tuple[str, ...], tuple[str, ...]], ...] = (
    (
        "Application identity",
        (
            "Install-time runtime identity written into the managed host config.",
        ),
        (
            "APP_NAME",
            "APP_VERSION",
        ),
    ),
    (
        "General defaults",
        (
            "Shared defaults used by the Settings menu and the managed USB planner.",
        ),
        (
            "DEFAULT_PERSISTENCE_SIZE_GIB",
            "DEFAULT_BOOT_POLICY",
            "DEFAULT_INSTALLER_POLICY",
            "DEFAULT_LIVE_TORAM",
            "DEFAULT_LIVE_MEM_GIB",
        ),
    ),
    (
        "Multi-OS and shared labels",
        (
            "These labels are used by both single-OS and Multi-OS flows where applicable.",
            "In the shared ISO-store Multi-OS flow, only DEFAULT_ESP_LABEL, DEFAULT_MULTI_DATA_LABEL, and the Debian/Kali persistence labels (the Tails label is legacy and unused) are used.",
            "DEFAULT_ESP_LABEL must fit the FAT volume-label limit; other labels must fit ext4 and GPT label use.",
        ),
        MULTIOS_PARTITION_LABEL_CONFIG_KEYS,
    ),
    (
        "Single-OS payload labels",
        (
            "These labels are only used by single-OS managed payload flows.",
            "Debian and Kali now keep separate netinst and netboot payload labels so those installer source roles do not reuse the same filesystem label.",
            "Ubuntu persistence keeps separate filesystem and GPT partition-name defaults for casper compatibility.",
        ),
        SINGLE_OS_PARTITION_LABEL_CONFIG_KEYS,
    ),
    (
        "Live config hooks",
        (
            "Debian and Kali Live receive Wi-Fi hooks plus live-config.hooks=medium; APT repair is Debian-only.",
            "DEFAULT_LIVE_HOOKS controls only optional additional hook arguments.",
            "Wi-Fi values come from initrd/debian/live/live.env or initrd/kali/live/live.env, never kernel arguments.",
        ),
        (
            "DEFAULT_LIVE_HOOKS",
            "DEFAULT_LIVE_ARGS_HOOKS",
        ),
    ),
    (
        "Live boot policy kernel arguments",
        (
            "The selected live boot policy appends its kernel arguments after the shared live base.",
            "Keep these values boot-safe because the managed live renderer consumes them directly.",
        ),
        (
            "SHARED_LIVE_BASE_KERNEL_ARGS",
            "BOOT_POLICY_BALANCED_KERNEL_ARGS",
            "BOOT_POLICY_PERFORMANCE_KERNEL_ARGS",
            "BOOT_POLICY_HARDENED_KERNEL_ARGS",
        ),
    ),
    (
        "Installer policy kernel arguments",
        (
            "The selected installer policy appends these arguments before any configured installer URL and installer extras.",
        ),
        (
            "INSTALLER_POLICY_PRESERVE_KERNEL_ARGS",
        ),
    ),
    (
        "Per-profile fallback live kernel arguments",
        (
            "Used only when a live-family entry cannot be detected from the ISO boot config.",
        ),
        tuple(f"{PROFILE_PREFIXES[profile]}_FALLBACK_LIVE_KERNEL_ARGS" for profile in FALLBACK_LIVE_KERNEL_ARG_PROFILES),
    ),
    (
        "Shared kernel extras",
        (
            "Optional extras appended after the selected live or installer policy.",
        ),
        (
            "DEFAULT_LIVE_KERNEL_EXTRAS",
            "DEFAULT_INSTALLER_KERNEL_EXTRAS",
            "DEFAULT_FORENSICS_KERNEL_EXTRAS",
        ),
    ),
    (
        "Per-profile live kernel extras",
        (
            "Applied only when the selected profile builds a managed live-family entry.",
        ),
        tuple(f"{PROFILE_PREFIXES[profile]}_LIVE_KERNEL_EXTRAS" for profile in LIVE_OVERRIDE_PROFILES),
    ),
    (
        "Per-profile forensic live kernel extras",
        (
            "Applied only to preserved forensic live entries for the selected managed profile.",
        ),
        tuple(f"{PROFILE_PREFIXES[profile]}_FORENSICS_KERNEL_EXTRAS" for profile in FORENSICS_OVERRIDE_PROFILES),
    ),
    (
        "Per-profile installer kernel extras",
        (
            "Applied only when installer-capable entries are present for the selected managed profile.",
        ),
        tuple(f"{PROFILE_PREFIXES[profile]}_INSTALLER_KERNEL_EXTRAS" for profile in INSTALLER_OVERRIDE_PROFILES),
    ),
    (
        "Preseed URL and variant argument defaults",
        (
            "Debian and Kali installer-capable entries use the configured internal profile URL by default.",
            "The shared public URL is applied by the custom GRUB Preseed Public submenus as the single url= transport.",
            "DEBIAN_DE_PRESEED_INTERNAL_ARGS and DEBIAN_DE_PRESEED_PUBLIC_ARGS are optional GRUB overlays applied only to their matching submenu variants.",
            "The shipped public overlay disables d-i HTTPS certificate validation; clear it to require normal CA validation.",
        ),
        ("DEBIAN_DE_PRESEED_PUBLIC_URL",)
        + tuple(profile_preseed_internal_key(profile) for profile in PRESEED_URL_PROFILES)
        + (
            "DEBIAN_DE_PRESEED_PUBLIC_ARGS",
            "DEBIAN_DE_PRESEED_INTERNAL_ARGS",
        ),
    ),
    (
        "USB preseed staging",
        (
            "These paths control where the writer copies host preseed trees onto the USB for Debian, Kali Linux, and Kali Purple.",
        ),
        (
            "PRESEED_USB_DEBIAN_DE_FILE",
            "PRESEED_USB_KALI_DE_FILE",
            "PRESEED_USB_PURPLE_FILE",
            "PRESEED_HOST_PURPLE_PATH",
        ),
    ),
    (
        "Custom Debian and Kali preseed preset kernel arguments",
        (
            "These args blocks back the JSON-driven Debian, Kali Linux, and Kali Purple custom GRUB preseed entries.",
            "Any custom GRUB spec args_key you add under configs/spec/grub/*.json becomes a required, preserved config key.",
        ),
        ("PRESEED_COMMON_KERNEL_ARGS",) + _numbered_preseed_arg_keys("DEBIAN") + _numbered_preseed_arg_keys("KALI"),
    ),
    (
        "Managed source URLs",
        (
            "These URLs back the Download from Internet flows for Debian, Kali Linux, Kali Purple, and Tails.",
            "Each source has separate stable and testing URLs; the download flow asks which release channel to use.",
            "Kali Live uses its torrent URL and is downloaded through aria2c with seeding disabled.",
        ),
        MANAGED_SOURCE_URL_KEYS,
    ),
)


INSTALLER_FLAVOR_CONFIG_KEYS = tuple(
    key
    for family, prefix in (("DEBIAN", "DEBIAN"), ("KALI", "KALI_LINUX"))
    for suffix in ("DE", "SRV")
    for key in (
        f"PRESEED_USB_{family}_{suffix}_FILE",
        *(f"{prefix}_{suffix}_PRESEED_{scope}_{kind}" for scope in ("PUBLIC", "INTERNAL") for kind in ("URL", "ARGS")),
        *(f"PRESEED_{number}_ARGS_{family}_{suffix}" for number in ("ONE", "TWO", "THREE", "FOUR", "FIVE", "SIX", "SEVEN", "EIGHT", "NINE")),
    )
)
# Move split-profile fields into one dedicated section; never emit duplicates.
CONFIG_SECTIONS = tuple(
    (title, comments, tuple(key for key in keys if key not in INSTALLER_FLAVOR_CONFIG_KEYS))
    for title, comments, keys in CONFIG_SECTIONS
) + ((
    "Desktop and Server installer profiles (netinst and netboot)",
    (
        "DE = Desktop; SRV = Server. These settings do not change live boot entries.",
        "PUBLIC URLs back HTTPS WEB; INTERNAL URLs back HTTP LAN. Empty URLs disable only that transport.",
        "Host paths are entered only after explicit HD-MEDIA consent. USB entries exist even when copying is declined.",
        "Preset visibility is controlled by the GRUB JSON; INITRD PRESEED never receives a URL or file argument.",
    ),
    INSTALLER_FLAVOR_CONFIG_KEYS,
),)


def _required_config_keys() -> tuple[str, ...]:
    required = [
        "APP_NAME",
        "APP_VERSION",
        "DEFAULT_PERSISTENCE_SIZE_GIB",
        "DEFAULT_BOOT_POLICY",
        "DEFAULT_INSTALLER_POLICY",
        "DEFAULT_LIVE_TORAM",
        "DEFAULT_LIVE_MEM_GIB",
        "DEFAULT_LIVE_HOOKS",
        "DEFAULT_LIVE_ARGS_HOOKS",
        "SHARED_LIVE_BASE_KERNEL_ARGS",
        "BOOT_POLICY_BALANCED_KERNEL_ARGS",
        "BOOT_POLICY_PERFORMANCE_KERNEL_ARGS",
        "BOOT_POLICY_HARDENED_KERNEL_ARGS",
        "INSTALLER_POLICY_PRESERVE_KERNEL_ARGS",
        "DEFAULT_LIVE_KERNEL_EXTRAS",
        "DEFAULT_INSTALLER_KERNEL_EXTRAS",
        "DEFAULT_FORENSICS_KERNEL_EXTRAS",
        "PRESEED_COMMON_KERNEL_ARGS",
        "PRESEED_USB_DEBIAN_DE_FILE",
        "PRESEED_USB_KALI_DE_FILE",
        "PRESEED_USB_PURPLE_FILE",
        "PRESEED_HOST_PURPLE_PATH",
        "DEBIAN_DE_PRESEED_PUBLIC_URL",
        "DEBIAN_DE_PRESEED_PUBLIC_ARGS",
        "DEBIAN_DE_PRESEED_INTERNAL_ARGS",
    ]
    required.extend(INSTALLER_FLAVOR_CONFIG_KEYS)
    required.extend(MANAGED_SOURCE_URL_KEYS)
    required.extend(PARTITION_LABEL_CONFIG_KEYS)
    required.extend(_spec_referenced_config_keys())
    for profile in FALLBACK_LIVE_KERNEL_ARG_PROFILES:
        required.append(f"{PROFILE_PREFIXES[profile]}_FALLBACK_LIVE_KERNEL_ARGS")
    for profile in LIVE_OVERRIDE_PROFILES:
        required.append(f"{PROFILE_PREFIXES[profile]}_LIVE_KERNEL_EXTRAS")
    for profile in FORENSICS_OVERRIDE_PROFILES:
        required.append(f"{PROFILE_PREFIXES[profile]}_FORENSICS_KERNEL_EXTRAS")
    for profile in INSTALLER_OVERRIDE_PROFILES:
        required.append(f"{PROFILE_PREFIXES[profile]}_INSTALLER_KERNEL_EXTRAS")
    for profile in PRESEED_URL_PROFILES:
        required.append(profile_preseed_internal_key(profile))
    return tuple(required)


def _optional_empty_keys() -> set[str]:
    optional = {
        "SHARED_LIVE_BASE_KERNEL_ARGS",
        "BOOT_POLICY_BALANCED_KERNEL_ARGS",
        "BOOT_POLICY_PERFORMANCE_KERNEL_ARGS",
        "BOOT_POLICY_HARDENED_KERNEL_ARGS",
        "INSTALLER_POLICY_PRESERVE_KERNEL_ARGS",
        "DEFAULT_LIVE_KERNEL_EXTRAS",
        "DEFAULT_INSTALLER_KERNEL_EXTRAS",
        "DEFAULT_FORENSICS_KERNEL_EXTRAS",
        "DEFAULT_LIVE_ARGS_HOOKS",
        "DEBIAN_DE_PRESEED_PUBLIC_ARGS",
        "DEBIAN_DE_PRESEED_INTERNAL_ARGS",
    }
    optional.update({"PRESEED_ONE_ARGS_DEBIAN_DE", "PRESEED_ONE_ARGS_KALI_DE"})
    optional.update(f"{PROFILE_PREFIXES[profile]}_FALLBACK_LIVE_KERNEL_ARGS" for profile in FALLBACK_LIVE_KERNEL_ARG_PROFILES)
    optional.update(f"{PROFILE_PREFIXES[profile]}_LIVE_KERNEL_EXTRAS" for profile in LIVE_OVERRIDE_PROFILES)
    optional.update(f"{PROFILE_PREFIXES[profile]}_FORENSICS_KERNEL_EXTRAS" for profile in FORENSICS_OVERRIDE_PROFILES)
    optional.update(f"{PROFILE_PREFIXES[profile]}_INSTALLER_KERNEL_EXTRAS" for profile in INSTALLER_OVERRIDE_PROFILES)
    optional.update(profile_preseed_internal_key(profile) for profile in PRESEED_URL_PROFILES)
    optional.update(key for key in _known_config_keys() if ("_PRESEED_" in key and key.endswith(("_URL", "_ARGS"))) or (key.startswith("PRESEED_") and "_ARGS_" in key))
    return optional


def _known_config_keys() -> set[str]:
    keys: set[str] = set()
    for _, _, section_keys in CONFIG_SECTIONS:
        keys.update(section_keys)
    return keys


def _is_preserved_additional_key(key: str) -> bool:
    return key.startswith("PRESEED_") or key.endswith("_URL") or ("_PRESEED_" in key and key.endswith("_ARGS"))


def _normalize_absolute_preseed_file_string(value: str, key: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{key} is required")
    if not normalized.startswith("/"):
        raise ValueError(f"{key} must be an absolute path")
    if ".." in normalized.split("/"):
        raise ValueError(f"{key} must not contain parent-directory segments")
    if Path(normalized).name != "preseed.cfg":
        raise ValueError(f"{key} must point to a preseed.cfg file")
    return normalized


def _normalize_absolute_dir_string(value: str, key: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{key} is required")
    if not normalized.startswith("/"):
        raise ValueError(f"{key} must be an absolute path")
    if ".." in normalized.split("/"):
        raise ValueError(f"{key} must not contain parent-directory segments")
    return normalized.rstrip("/") or "/"


def profile_usb_preseed_file(config_data: dict[str, str], profile: str) -> str:
    key = PRESEED_USB_FILE_KEYS.get(profile, "")
    if not key:
        return ""
    return config_data.get(key, "").strip()


def profile_host_preseed_path(config_data: dict[str, str], profile: str) -> str:
    key = PRESEED_HOST_PATH_KEYS.get(profile, "")
    if not key:
        return ""
    return config_data.get(key, "").strip()


def effective_managed_payload_layout(
    profile: str,
    configured_layout: str,
    config_data: dict[str, str],
    use_custom_menu: bool,
) -> str:
    normalized = (configured_layout or "").strip()
    if normalized == MANAGED_PAYLOAD_LAYOUT_ISO_STORE:
        return MANAGED_PAYLOAD_LAYOUT_ISO_STORE
    if normalized == MANAGED_PAYLOAD_LAYOUT_SHARED_DATA:
        return MANAGED_PAYLOAD_LAYOUT_SHARED_DATA
    if normalized == "":
        if profile in {PROFILE_DEBIAN, PROFILE_KALI_LINUX, PROFILE_KALI_PURPLE, PROFILE_TAILS}:
            return MANAGED_PAYLOAD_LAYOUT_RAW_ISO
        return MANAGED_PAYLOAD_LAYOUT_EXTRACTED
    if normalized in {MANAGED_PAYLOAD_LAYOUT_EXTRACTED, MANAGED_PAYLOAD_LAYOUT_RAW_ISO, MANAGED_PAYLOAD_LAYOUT_SHARED_DATA}:
        return normalized
    return normalized


def _normalize_bool_string(value: str, key: str) -> str:
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return "1"
    if normalized in {"0", "false", "no", "off", ""}:
        return "0"
    raise ValueError(f"{key} must be a boolean value")


def _normalize_int_string(value: str, key: str, *, minimum: int = 0) -> str:
    try:
        number = int(value.strip())
    except ValueError as exc:
        raise ValueError(f"{key} must be an integer") from exc
    if number < minimum:
        if minimum > 0:
            raise ValueError(f"{key} must be at least {minimum}")
        raise ValueError(f"{key} must be zero or greater")
    return str(number)


def _normalize_kernel_args_string(value: str) -> str:
    return " ".join(value.split())


def _kernel_arg_config_keys() -> tuple[str, ...]:
    keys = [
        "SHARED_LIVE_BASE_KERNEL_ARGS",
        "BOOT_POLICY_BALANCED_KERNEL_ARGS",
        "BOOT_POLICY_PERFORMANCE_KERNEL_ARGS",
        "BOOT_POLICY_HARDENED_KERNEL_ARGS",
        "INSTALLER_POLICY_PRESERVE_KERNEL_ARGS",
        "DEFAULT_LIVE_ARGS_HOOKS",
        "DEFAULT_LIVE_KERNEL_EXTRAS",
        "DEFAULT_INSTALLER_KERNEL_EXTRAS",
        "DEFAULT_FORENSICS_KERNEL_EXTRAS",
        "PRESEED_COMMON_KERNEL_ARGS",
        "DEBIAN_DE_PRESEED_PUBLIC_ARGS",
        "DEBIAN_DE_PRESEED_INTERNAL_ARGS",
    ]
    keys.extend(_spec_referenced_kernel_arg_keys())
    keys.extend(f"{PROFILE_PREFIXES[profile]}_FALLBACK_LIVE_KERNEL_ARGS" for profile in FALLBACK_LIVE_KERNEL_ARG_PROFILES)
    keys.extend(f"{PROFILE_PREFIXES[profile]}_LIVE_KERNEL_EXTRAS" for profile in LIVE_OVERRIDE_PROFILES)
    keys.extend(f"{PROFILE_PREFIXES[profile]}_FORENSICS_KERNEL_EXTRAS" for profile in FORENSICS_OVERRIDE_PROFILES)
    keys.extend(f"{PROFILE_PREFIXES[profile]}_INSTALLER_KERNEL_EXTRAS" for profile in INSTALLER_OVERRIDE_PROFILES)
    return tuple(dict.fromkeys(keys))


def _reject_legacy_secret_kernel_args(value: str, key: str) -> None:
    found = sorted(
        {
            token.split("=", 1)[0]
            for token in value.split()
            if token.split("=", 1)[0] in LEGACY_SECRET_KERNEL_ARG_NAMES
        }
    )
    if found:
        names = ", ".join(found)
        raise ValueError(
            f"{key} contains forbidden legacy secret kernel argument(s): {names}; "
            f"store these values in {LEGACY_SECRET_KERNEL_ARG_DESTINATION}"
        )
    live_wifi_found = sorted(
        {
            token.split("=", 1)[0]
            for token in value.split()
            if token.split("=", 1)[0] in LIVE_WIFI_SECRET_KERNEL_ARG_NAMES
        }
    )
    if live_wifi_found:
        names = ", ".join(live_wifi_found)
        raise ValueError(
            f"{key} contains forbidden Live Wi-Fi kernel argument(s): {names}; "
            f"store every Wi-Fi value in {LIVE_WIFI_SECRET_KERNEL_ARG_DESTINATION}"
        )


def _normalize_partition_label(value: str, key: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{key} is required")
    max_length = 11 if key == "DEFAULT_ESP_LABEL" else 16
    if len(normalized) > max_length:
        raise ValueError(f"{key} {normalized!r} is longer than {max_length} characters")
    if any(ord(char) < 33 or ord(char) > 126 for char in normalized):
        raise ValueError(f"{key} {normalized!r} must contain printable ASCII without whitespace")
    return normalized


def _validate_preseed_network_kernel_args(value: str, key: str) -> str:
    normalized = _normalize_kernel_args_string(value)
    if "d-i" in normalized.split():
        raise ValueError(f"{key} must use kernel boot-parameter syntax; remove standalone d-i preseed owner token")
    return normalized


def _validate_preseed_wifi_kernel_args(value: str) -> str:
    normalized = _validate_preseed_network_kernel_args(value, "PRESEED_WIFI_KERNEL_ARGS")
    for item in normalized.split():
        if not item.startswith("netcfg/wireless_security_type="):
            continue
        security = item.split("=", 1)[1]
        if security not in {"open", "wep", "wpa"}:
            raise ValueError("PRESEED_WIFI_KERNEL_ARGS netcfg/wireless_security_type must be one of: open, wep, wpa")
    return normalized


def _normalize_optional_url_string(value: str) -> str:
    normalized = value.strip()
    if not normalized:
        return ""
    if not normalized.startswith(("http://", "https://")):
        raise ValueError("installer URL values must start with http:// or https://")
    return normalized


def _parse_config_file(path: str | Path) -> OrderedDict[str, str]:
    config_path = Path(path)
    data: OrderedDict[str, str] = OrderedDict()
    for line in config_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        data[key.strip()] = value.strip().strip('"').strip("'")
    return data


def _template_config_candidates() -> tuple[Path, ...]:
    candidates: list[Path] = []
    env_path = os.environ.get(TEMPLATE_CONFIG_ENV, "").strip()
    if env_path:
        candidates.append(Path(env_path).expanduser())
    module_path = Path(__file__).resolve()
    if len(module_path.parents) > 3:
        candidates.append(module_path.parents[3] / "configs" / "debian-usb.conf")
    candidates.append(Path("/etc/debian-usb/debian-usb.conf"))

    unique: list[Path] = []
    seen: set[Path] = set()
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        unique.append(candidate)
    return tuple(unique)


def template_config_path() -> Path:
    for candidate in _template_config_candidates():
        if candidate.is_file():
            return candidate
    searched = ", ".join(str(candidate) for candidate in _template_config_candidates())
    raise FileNotFoundError(f"unable to locate the debian-usb template config; checked: {searched}")


def _apply_legacy_key_aliases(data: dict[str, str]) -> OrderedDict[str, str]:
    remapped = OrderedDict(data)
    for legacy_key, canonical_key in LEGACY_KEY_ALIASES.items():
        legacy_value = remapped.get(legacy_key, "").strip()
        canonical_value = remapped.get(canonical_key, "").strip()
        if legacy_value and not canonical_value:
            for family in ("debian", "kali"):
                if legacy_key == f"PRESEED_USB_{family.upper()}_FILE" and legacy_value == f"/hd-media/preseed/{family}/preseed.cfg":
                    legacy_value = f"/hd-media/{family}-preseed-de/preseed.cfg"
            remapped[canonical_key] = legacy_value
        remapped.pop(legacy_key, None)
    # Discard obsolete ambient host paths on load and save.
    for family in ("DEBIAN", "KALI"):
        for suffix in ("DE", "SRV"):
            remapped.pop(f"PRESEED_HOST_{family}_{suffix}_PATH", None)
    return remapped


def load_template_config() -> OrderedDict[str, str]:
    return _apply_legacy_key_aliases(_parse_config_file(template_config_path()))


def normalize_config(data: dict[str, str]) -> OrderedDict[str, str]:
    normalized = load_template_config()
    normalized.update(_apply_legacy_key_aliases(data))
    normalized["APP_NAME"] = normalized["APP_NAME"].strip()
    normalized["APP_VERSION"] = normalized["APP_VERSION"].strip()
    normalized["DEFAULT_PERSISTENCE_SIZE_GIB"] = _normalize_int_string(
        normalized["DEFAULT_PERSISTENCE_SIZE_GIB"],
        "DEFAULT_PERSISTENCE_SIZE_GIB",
        minimum=1,
    )
    normalized["DEFAULT_BOOT_POLICY"] = valid_live_boot_policy(normalized["DEFAULT_BOOT_POLICY"].strip())
    normalized["DEFAULT_INSTALLER_POLICY"] = valid_installer_policy(normalized["DEFAULT_INSTALLER_POLICY"].strip())
    normalized["DEFAULT_LIVE_TORAM"] = _normalize_bool_string(
        normalized["DEFAULT_LIVE_TORAM"],
        "DEFAULT_LIVE_TORAM",
    )
    normalized["DEFAULT_LIVE_MEM_GIB"] = _normalize_int_string(
        normalized["DEFAULT_LIVE_MEM_GIB"],
        "DEFAULT_LIVE_MEM_GIB",
        minimum=0,
    )
    normalized["DEFAULT_LIVE_HOOKS"] = _normalize_bool_string(
        normalized["DEFAULT_LIVE_HOOKS"],
        "DEFAULT_LIVE_HOOKS",
    )
    normalized["DEFAULT_LIVE_ARGS_HOOKS"] = _normalize_kernel_args_string(
        normalized["DEFAULT_LIVE_ARGS_HOOKS"]
    )
    for key in PARTITION_LABEL_CONFIG_KEYS:
        normalized[key] = _normalize_partition_label(normalized[key], key)
    normalized["SHARED_LIVE_BASE_KERNEL_ARGS"] = _normalize_kernel_args_string(
        normalized["SHARED_LIVE_BASE_KERNEL_ARGS"]
    )
    normalized["BOOT_POLICY_BALANCED_KERNEL_ARGS"] = _normalize_kernel_args_string(
        normalized["BOOT_POLICY_BALANCED_KERNEL_ARGS"]
    )
    normalized["BOOT_POLICY_PERFORMANCE_KERNEL_ARGS"] = _normalize_kernel_args_string(
        normalized["BOOT_POLICY_PERFORMANCE_KERNEL_ARGS"]
    )
    normalized["BOOT_POLICY_HARDENED_KERNEL_ARGS"] = _normalize_kernel_args_string(
        normalized["BOOT_POLICY_HARDENED_KERNEL_ARGS"]
    )
    normalized["INSTALLER_POLICY_PRESERVE_KERNEL_ARGS"] = _normalize_kernel_args_string(
        normalized["INSTALLER_POLICY_PRESERVE_KERNEL_ARGS"]
    )
    normalized["DEFAULT_LIVE_KERNEL_EXTRAS"] = _normalize_kernel_args_string(
        normalized["DEFAULT_LIVE_KERNEL_EXTRAS"]
    )
    normalized["DEFAULT_INSTALLER_KERNEL_EXTRAS"] = _normalize_kernel_args_string(
        normalized["DEFAULT_INSTALLER_KERNEL_EXTRAS"]
    )
    normalized["DEFAULT_FORENSICS_KERNEL_EXTRAS"] = _normalize_kernel_args_string(
        normalized["DEFAULT_FORENSICS_KERNEL_EXTRAS"]
    )
    normalized["PRESEED_COMMON_KERNEL_ARGS"] = _normalize_kernel_args_string(
        normalized["PRESEED_COMMON_KERNEL_ARGS"]
    )
    normalized["DEBIAN_DE_PRESEED_PUBLIC_ARGS"] = _normalize_kernel_args_string(
        normalized["DEBIAN_DE_PRESEED_PUBLIC_ARGS"]
    )
    normalized["DEBIAN_DE_PRESEED_INTERNAL_ARGS"] = _normalize_kernel_args_string(
        normalized["DEBIAN_DE_PRESEED_INTERNAL_ARGS"]
    )
    normalized["PRESEED_USB_DEBIAN_DE_FILE"] = _normalize_absolute_preseed_file_string(
        normalized["PRESEED_USB_DEBIAN_DE_FILE"],
        "PRESEED_USB_DEBIAN_DE_FILE",
    )
    normalized["PRESEED_USB_KALI_DE_FILE"] = _normalize_absolute_preseed_file_string(
        normalized["PRESEED_USB_KALI_DE_FILE"],
        "PRESEED_USB_KALI_DE_FILE",
    )
    normalized["PRESEED_USB_PURPLE_FILE"] = _normalize_absolute_preseed_file_string(
        normalized["PRESEED_USB_PURPLE_FILE"],
        "PRESEED_USB_PURPLE_FILE",
    )
    normalized["PRESEED_HOST_PURPLE_PATH"] = _normalize_absolute_dir_string(
        normalized["PRESEED_HOST_PURPLE_PATH"],
        "PRESEED_HOST_PURPLE_PATH",
    )
    for key in _spec_referenced_kernel_arg_keys():
        normalized[key] = _normalize_kernel_args_string(normalized.get(key, ""))
    for profile in FALLBACK_LIVE_KERNEL_ARG_PROFILES:
        prefix = PROFILE_PREFIXES[profile]
        normalized[f"{prefix}_FALLBACK_LIVE_KERNEL_ARGS"] = _normalize_kernel_args_string(
            normalized[f"{prefix}_FALLBACK_LIVE_KERNEL_ARGS"]
        )
    for profile in LIVE_OVERRIDE_PROFILES:
        prefix = PROFILE_PREFIXES[profile]
        normalized[f"{prefix}_LIVE_KERNEL_EXTRAS"] = _normalize_kernel_args_string(
            normalized[f"{prefix}_LIVE_KERNEL_EXTRAS"]
        )
    for profile in FORENSICS_OVERRIDE_PROFILES:
        prefix = PROFILE_PREFIXES[profile]
        normalized[f"{prefix}_FORENSICS_KERNEL_EXTRAS"] = _normalize_kernel_args_string(
            normalized[f"{prefix}_FORENSICS_KERNEL_EXTRAS"]
        )
    for profile in INSTALLER_OVERRIDE_PROFILES:
        prefix = PROFILE_PREFIXES[profile]
        normalized[f"{prefix}_INSTALLER_KERNEL_EXTRAS"] = _normalize_kernel_args_string(
            normalized[f"{prefix}_INSTALLER_KERNEL_EXTRAS"]
        )
    for profile in PRESEED_URL_PROFILES:
        prefix = PROFILE_PREFIXES[profile]
        normalized[profile_preseed_internal_key(profile)] = _normalize_optional_url_string(
            normalized[profile_preseed_internal_key(profile)]
        )
    normalized["DEBIAN_DE_PRESEED_PUBLIC_URL"] = _normalize_optional_url_string(
        normalized["DEBIAN_DE_PRESEED_PUBLIC_URL"]
    )
    for key in INSTALLER_FLAVOR_CONFIG_KEYS:
        value = normalized.get(key, "")
        if key.endswith("_URL"):
            normalized[key] = _normalize_optional_url_string(value)
        elif key.startswith("PRESEED_USB_"):
            normalized[key] = _normalize_absolute_preseed_file_string(value, key)
            if not re.fullmatch(r"/hd-media/[A-Za-z0-9._+-]+/preseed\.cfg", normalized[key]):
                raise ValueError(f"{key} must be /hd-media/<separate-folder>/preseed.cfg")
        elif key.startswith("PRESEED_HOST_"):
            normalized[key] = _normalize_absolute_dir_string(value, key)
        else:
            normalized[key] = _normalize_kernel_args_string(value)
            _reject_legacy_secret_kernel_args(normalized[key], key)
    locations = [normalized[key] for key in INSTALLER_FLAVOR_CONFIG_KEYS if key.startswith("PRESEED_USB_")]
    if len(set(locations)) != len(locations):
        raise ValueError("Desktop/Server USB preseed folders must be distinct for Debian and Kali")
    for key in MANAGED_SOURCE_URL_KEYS:
        normalized[key] = _normalize_optional_url_string(normalized[key])
    for key in _kernel_arg_config_keys():
        _reject_legacy_secret_kernel_args(normalized.get(key, ""), key)
    return normalized


def load_config(path: str) -> OrderedDict[str, str]:
    config_path = Path(path)
    if not config_path.exists():
        raise FileNotFoundError(f"missing config file: {config_path}")
    normalized = normalize_config(_parse_config_file(config_path))
    validate_config(normalized)
    return normalized


def validate_config(data: dict[str, str]) -> None:
    required = _required_config_keys()
    optional = _optional_empty_keys()
    for key in required:
        if data.get(key):
            continue
        if key in optional:
            continue
        raise ValueError(f"missing required config key: {key}")
    if not data["APP_NAME"]:
        raise ValueError("APP_NAME is required")
    if not data["APP_VERSION"]:
        raise ValueError("APP_VERSION is required")
    normalize_config(data)


def save_config(path: str, data: dict[str, str]) -> None:
    config_path = Path(path)
    ordered = normalize_config(data)
    validate_config(ordered)
    lines = [
        "# Managed by debian-usb.",
        "# This file is the install-time source of truth for managed runtime defaults.",
        "# Update values through the Settings menu unless you are doing a controlled edit.",
        "",
    ]
    for index, (title, comments, keys) in enumerate(CONFIG_SECTIONS):
        lines.append(f"# {title}")
        for comment in comments:
            lines.append(f"# {comment}")
        for key in keys:
            lines.append(f'{key}="{ordered[key]}"')
        if index != len(CONFIG_SECTIONS) - 1:
            lines.append("")
    extra_keys = sorted(
        key for key in ordered if key not in _known_config_keys() and _is_preserved_additional_key(key)
    )
    if extra_keys:
        if lines and lines[-1] != "":
            lines.append("")
        lines.append("# Additional preserved config keys")
        lines.append("# These values are loaded and rewritten even when they are not exposed in the Settings UI.")
        for key in extra_keys:
            lines.append(f'{key}="{ordered[key]}"')
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def update_default_persistence_size(path: str, size_gib: int) -> OrderedDict[str, str]:
    if size_gib <= 0:
        raise ValueError("persistence size must be a positive integer")
    config = load_config(path)
    config["DEFAULT_PERSISTENCE_SIZE_GIB"] = str(size_gib)
    save_config(path, config)
    return config


def update_default_live_toram(path: str, enabled: bool) -> OrderedDict[str, str]:
    config = load_config(path)
    config["DEFAULT_LIVE_TORAM"] = "1" if enabled else "0"
    save_config(path, config)
    return config


def update_default_boot_policy(path: str, policy: str) -> OrderedDict[str, str]:
    config = load_config(path)
    config["DEFAULT_BOOT_POLICY"] = valid_live_boot_policy(policy.strip())
    save_config(path, config)
    return config


def update_default_installer_policy(path: str, policy: str) -> OrderedDict[str, str]:
    config = load_config(path)
    config["DEFAULT_INSTALLER_POLICY"] = valid_installer_policy(policy.strip())
    save_config(path, config)
    return config


def update_default_live_mem_gib(path: str, size_gib: int) -> OrderedDict[str, str]:
    if size_gib < 0:
        raise ValueError("live memory limit must be zero or greater")
    config = load_config(path)
    config["DEFAULT_LIVE_MEM_GIB"] = str(size_gib)
    save_config(path, config)
    return config


def update_default_live_kernel_extras(path: str, kernel_args: str) -> OrderedDict[str, str]:
    config = load_config(path)
    config["DEFAULT_LIVE_KERNEL_EXTRAS"] = _normalize_kernel_args_string(kernel_args)
    save_config(path, config)
    return config


def update_default_installer_kernel_extras(path: str, kernel_args: str) -> OrderedDict[str, str]:
    config = load_config(path)
    config["DEFAULT_INSTALLER_KERNEL_EXTRAS"] = _normalize_kernel_args_string(kernel_args)
    save_config(path, config)
    return config


def update_default_forensics_kernel_extras(path: str, kernel_args: str) -> OrderedDict[str, str]:
    config = load_config(path)
    config["DEFAULT_FORENSICS_KERNEL_EXTRAS"] = _normalize_kernel_args_string(kernel_args)
    save_config(path, config)
    return config


def update_profile_live_kernel_extras(path: str, profile: str, kernel_args: str) -> OrderedDict[str, str]:
    if profile not in LIVE_OVERRIDE_PROFILES:
        raise ValueError(f"live kernel extras are not supported for profile: {profile}")
    profile_key = f"{PROFILE_PREFIXES[profile]}_LIVE_KERNEL_EXTRAS"
    config = load_config(path)
    config[profile_key] = _normalize_kernel_args_string(kernel_args)
    save_config(path, config)
    return config


def update_profile_forensics_kernel_extras(path: str, profile: str, kernel_args: str) -> OrderedDict[str, str]:
    if profile not in FORENSICS_OVERRIDE_PROFILES:
        raise ValueError(f"forensics kernel extras are not supported for profile: {profile}")
    profile_key = f"{PROFILE_PREFIXES[profile]}_FORENSICS_KERNEL_EXTRAS"
    config = load_config(path)
    config[profile_key] = _normalize_kernel_args_string(kernel_args)
    save_config(path, config)
    return config


def update_profile_installer_kernel_extras(path: str, profile: str, kernel_args: str) -> OrderedDict[str, str]:
    if profile not in INSTALLER_OVERRIDE_PROFILES:
        raise ValueError(f"installer kernel extras are not supported for profile: {profile}")
    profile_key = f"{PROFILE_PREFIXES[profile]}_INSTALLER_KERNEL_EXTRAS"
    config = load_config(path)
    config[profile_key] = _normalize_kernel_args_string(kernel_args)
    save_config(path, config)
    return config


def update_profile_preseed_url(path: str, profile: str, url: str) -> OrderedDict[str, str]:
    if profile not in PRESEED_URL_PROFILES:
        raise ValueError(f"installer URLs are not supported for profile: {profile}")
    profile_key = profile_preseed_internal_key(profile)
    config = load_config(path)
    config[profile_key] = _normalize_optional_url_string(url)
    save_config(path, config)
    return config


def profile_fallback_live_kernel_args(data: dict[str, str], profile: str) -> str:
    return data.get(f"{PROFILE_PREFIXES[profile]}_FALLBACK_LIVE_KERNEL_ARGS", "").strip()


def profile_live_kernel_extras(data: dict[str, str], profile: str) -> str:
    return data.get(f"{PROFILE_PREFIXES[profile]}_LIVE_KERNEL_EXTRAS", "").strip()


def profile_forensics_kernel_extras(data: dict[str, str], profile: str) -> str:
    return data.get(f"{PROFILE_PREFIXES[profile]}_FORENSICS_KERNEL_EXTRAS", "").strip()


def profile_installer_kernel_extras(data: dict[str, str], profile: str) -> str:
    return data.get(f"{PROFILE_PREFIXES[profile]}_INSTALLER_KERNEL_EXTRAS", "").strip()


def profile_preseed_url(data: dict[str, str], profile: str) -> str:
    return data.get(profile_preseed_internal_key(profile), "").strip()


def partition_label(data: dict[str, str], key: str) -> str:
    return data.get(key, "").strip()


def esp_label(data: dict[str, str]) -> str:
    return partition_label(data, "DEFAULT_ESP_LABEL")


def profile_payload_labels(
    data: dict[str, str],
    profile: str,
    source_role: str = "primary",
    media_class: str = "",
) -> tuple[str, str]:
    if profile == PROFILE_DEBIAN:
        if source_role == "netboot":
            key = "DEFAULT_DEBIAN_NETBOOT_LABEL"
        else:
            installer_like = source_role == "netinst" or media_class == "installer"
            key = "DEFAULT_DEBIAN_NETINST_LABEL" if installer_like else "DEFAULT_DEBIAN_LIVE_LABEL"
    elif profile == PROFILE_UBUNTU_DESKTOP:
        installer_like = source_role == "netinst" or media_class == "installer"
        key = "DEFAULT_UBUNTU_NETINST_LABEL" if installer_like else "DEFAULT_UBUNTU_LIVE_LABEL"
    elif profile == PROFILE_UBUNTU_SERVER:
        key = "DEFAULT_UBUNTU_NETINST_LABEL"
    elif profile == PROFILE_KALI_LINUX:
        if source_role == "netboot":
            key = "DEFAULT_KALI_NETBOOT_LABEL"
        else:
            installer_like = source_role == "netinst" or media_class == "installer"
            key = "DEFAULT_KALI_NETINST_LABEL" if installer_like else "DEFAULT_KALI_LIVE_LABEL"
    elif profile == PROFILE_KALI_PURPLE:
        key = "DEFAULT_KALI_PURPLE_NETINST_LABEL"
    elif profile == PROFILE_TAILS:
        key = "DEFAULT_TAILS_LIVE_LABEL"
    else:
        return ("PAYLOAD", "PAYLOAD")
    label = partition_label(data, key)
    return (label, label)


def profile_persistence_labels(data: dict[str, str], profile: str) -> tuple[str, str]:
    if profile == PROFILE_DEBIAN:
        label = partition_label(data, "DEFAULT_DEBIAN_PERSIST_LABEL")
        return (label, label)
    if profile == PROFILE_KALI_LINUX:
        label = partition_label(data, "DEFAULT_KALI_PERSIST_LABEL")
        return (label, label)
    if profile == PROFILE_UBUNTU_DESKTOP:
        return (
            partition_label(data, "DEFAULT_UBUNTU_PERSIST_LABEL"),
            partition_label(data, "DEFAULT_UBUNTU_PERSIST_PARTLABEL"),
        )
    if profile == PROFILE_TAILS:
        label = partition_label(data, "DEFAULT_TAILS_PERSIST_LABEL")
        return (label, label)
    return ("", "")


def runtime_config(path: str) -> dict[str, object]:
    data = load_config(path)
    return {
        "app_name": data["APP_NAME"],
        "app_version": data["APP_VERSION"],
        "config_path": path,
        "default_persistence_size_gib": int(data["DEFAULT_PERSISTENCE_SIZE_GIB"]),
        "default_boot_policy": data["DEFAULT_BOOT_POLICY"],
        "default_installer_policy": data["DEFAULT_INSTALLER_POLICY"],
        "default_live_toram": data["DEFAULT_LIVE_TORAM"] == "1",
        "default_live_mem_gib": int(data["DEFAULT_LIVE_MEM_GIB"]),
        "default_live_hooks": data["DEFAULT_LIVE_HOOKS"] == "1",
        "default_live_args_hooks": data["DEFAULT_LIVE_ARGS_HOOKS"],
        "default_partition_labels": {
            key: partition_label(data, key)
            for key in PARTITION_LABEL_CONFIG_KEYS
        },
        "shared_live_base_kernel_args": data["SHARED_LIVE_BASE_KERNEL_ARGS"],
        "boot_policy_balanced_kernel_args": data["BOOT_POLICY_BALANCED_KERNEL_ARGS"],
        "boot_policy_performance_kernel_args": data["BOOT_POLICY_PERFORMANCE_KERNEL_ARGS"],
        "boot_policy_hardened_kernel_args": data["BOOT_POLICY_HARDENED_KERNEL_ARGS"],
        "installer_policy_preserve_kernel_args": data["INSTALLER_POLICY_PRESERVE_KERNEL_ARGS"],
        "default_live_kernel_extras": data["DEFAULT_LIVE_KERNEL_EXTRAS"],
        "default_installer_kernel_extras": data["DEFAULT_INSTALLER_KERNEL_EXTRAS"],
        "default_forensics_kernel_extras": data["DEFAULT_FORENSICS_KERNEL_EXTRAS"],
        "debian_preseed_public_url": data["DEBIAN_DE_PRESEED_PUBLIC_URL"],
        "debian_preseed_public_args": data["DEBIAN_DE_PRESEED_PUBLIC_ARGS"],
        "debian_preseed_internal_args": data["DEBIAN_DE_PRESEED_INTERNAL_ARGS"],
        "preseed_common_kernel_args": data["PRESEED_COMMON_KERNEL_ARGS"],
        "preseed_usb_files": {
            profile: profile_usb_preseed_file(data, profile)
            for profile in PROFILE_KEYS
        },
        "preseed_host_paths": {
            profile: profile_host_preseed_path(data, profile)
            for profile in PROFILE_KEYS
        },
        "profile_fallback_live_kernel_args": {
            profile: profile_fallback_live_kernel_args(data, profile)
            for profile in PROFILE_KEYS
        },
        "profile_live_kernel_extras": {
            profile: profile_live_kernel_extras(data, profile)
            for profile in PROFILE_KEYS
        },
        "profile_forensics_kernel_extras": {
            profile: profile_forensics_kernel_extras(data, profile)
            for profile in PROFILE_KEYS
        },
        "profile_installer_kernel_extras": {
            profile: profile_installer_kernel_extras(data, profile)
            for profile in PROFILE_KEYS
        },
        "profile_preseed_urls": {
            profile: profile_preseed_url(data, profile)
            for profile in PROFILE_KEYS
        },
    }
