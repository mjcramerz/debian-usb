from __future__ import annotations

from dataclasses import dataclass

from .constants import (
    MANAGED_PAYLOAD_LAYOUT_EXTRACTED,
    MANAGED_PAYLOAD_LAYOUT_ISO_STORE,
    MANAGED_PAYLOAD_LAYOUT_RAW_ISO,
    PROFILE_DEBIAN,
    PROFILE_KALI_LINUX,
    PROFILE_KALI_PURPLE,
    PROFILE_TAILS,
    PROFILE_UBUNTU_DESKTOP,
    PROFILE_UBUNTU_SERVER,
)


@dataclass(frozen=True)
class Profile:
    title: str
    supports_managed: bool
    supports_persistence: bool
    managed_payload_layout: str
    preferred_media: str
    supports_live_overrides: bool
    live_boot_family: str
    default_menu_label: str
    persistence_fs_label: str
    persistence_partlabel: str


PROFILES: dict[str, Profile] = {
    PROFILE_DEBIAN: Profile(
        title="Debian",
        supports_managed=True,
        supports_persistence=True,
        managed_payload_layout=MANAGED_PAYLOAD_LAYOUT_RAW_ISO,
        preferred_media="live",
        supports_live_overrides=True,
        live_boot_family="live-boot",
        default_menu_label="Debian Live",
        persistence_fs_label="DEBIAN-PERSIST",
        persistence_partlabel="DEBIAN-PERSIST",
    ),
    PROFILE_UBUNTU_DESKTOP: Profile(
        title="Ubuntu Desktop",
        supports_managed=True,
        supports_persistence=True,
        managed_payload_layout=MANAGED_PAYLOAD_LAYOUT_EXTRACTED,
        preferred_media="live",
        supports_live_overrides=True,
        live_boot_family="casper",
        default_menu_label="Ubuntu Desktop Live",
        persistence_fs_label="casper-rw",
        persistence_partlabel="writable",
    ),
    PROFILE_UBUNTU_SERVER: Profile(
        title="Ubuntu Server",
        supports_managed=True,
        supports_persistence=False,
        managed_payload_layout=MANAGED_PAYLOAD_LAYOUT_EXTRACTED,
        preferred_media="installer",
        supports_live_overrides=True,
        live_boot_family="casper",
        default_menu_label="Ubuntu Server",
        persistence_fs_label="",
        persistence_partlabel="",
    ),
    PROFILE_KALI_LINUX: Profile(
        title="Kali Linux",
        supports_managed=True,
        supports_persistence=True,
        managed_payload_layout=MANAGED_PAYLOAD_LAYOUT_RAW_ISO,
        preferred_media="live",
        supports_live_overrides=True,
        live_boot_family="live-boot",
        default_menu_label="Kali Live",
        persistence_fs_label="KALI-PERSIST",
        persistence_partlabel="KALI-PERSIST",
    ),
    PROFILE_KALI_PURPLE: Profile(
        title="Kali Purple",
        supports_managed=True,
        supports_persistence=False,
        managed_payload_layout=MANAGED_PAYLOAD_LAYOUT_RAW_ISO,
        preferred_media="installer",
        supports_live_overrides=False,
        live_boot_family="",
        default_menu_label="Kali Purple Installer",
        persistence_fs_label="",
        persistence_partlabel="",
    ),
    PROFILE_TAILS: Profile(
        title="Tails",
        supports_managed=True,
        supports_persistence=True,
        managed_payload_layout=MANAGED_PAYLOAD_LAYOUT_RAW_ISO,
        preferred_media="live",
        supports_live_overrides=True,
        live_boot_family="live-boot",
        default_menu_label="Tails Live",
        persistence_fs_label="TailsData",
        persistence_partlabel="TailsData",
    ),
}


def profile_for(name: str) -> Profile:
    try:
        return PROFILES[name]
    except KeyError as exc:
        raise ValueError(f"unsupported profile: {name}") from exc
