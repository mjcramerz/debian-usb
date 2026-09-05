from __future__ import annotations

from dataclasses import dataclass

from .constants import PROFILE_DEBIAN, PROFILE_KALI_LINUX, PROFILE_KALI_PURPLE


@dataclass(frozen=True)
class LiveBootPolicy:
    name: str
    description: str
    config_key: str


@dataclass(frozen=True)
class InstallerPolicy:
    name: str
    description: str
    config_key: str


LIVE_BOOT_POLICIES: dict[str, LiveBootPolicy] = {
    "balanced": LiveBootPolicy(
        name="balanced",
        description="Balanced live defaults with boot-safe security and performance tuning.",
        config_key="BOOT_POLICY_BALANCED_KERNEL_ARGS",
    ),
    "performance": LiveBootPolicy(
        name="performance",
        description="Performance-oriented live defaults while keeping the same boot-safe baseline.",
        config_key="BOOT_POLICY_PERFORMANCE_KERNEL_ARGS",
    ),
    "hardened": LiveBootPolicy(
        name="hardened",
        description="Stricter live defaults that bias toward hardening over latency and throughput.",
        config_key="BOOT_POLICY_HARDENED_KERNEL_ARGS",
    ),
}


INSTALLER_POLICIES: dict[str, InstallerPolicy] = {
    "preserve": InstallerPolicy(
        name="preserve",
        description="Preserve upstream installer entries without adding automation parameters.",
        config_key="INSTALLER_POLICY_PRESERVE_KERNEL_ARGS",
    ),
    "installer-preseed": InstallerPolicy(
        name="installer-preseed",
        description="Add unattended installer URL handling where supported.",
        config_key="",
    ),
}


def valid_live_boot_policy(name: str) -> str:
    if name not in LIVE_BOOT_POLICIES:
        allowed = ", ".join(sorted(LIVE_BOOT_POLICIES))
        raise ValueError(f"DEFAULT_BOOT_POLICY must be one of: {allowed}")
    return name


def valid_installer_policy(name: str) -> str:
    if name not in INSTALLER_POLICIES:
        allowed = ", ".join(sorted(INSTALLER_POLICIES))
        raise ValueError(f"DEFAULT_INSTALLER_POLICY must be one of: {allowed}")
    return name


def live_policy_kernel_args(config_data: dict[str, str], name: str) -> str:
    policy_name = valid_live_boot_policy(name)
    base_args = " ".join(config_data.get("SHARED_LIVE_BASE_KERNEL_ARGS", "").split())
    policy_args = " ".join(config_data.get(LIVE_BOOT_POLICIES[policy_name].config_key, "").split())
    return " ".join(part for part in (base_args, policy_args) if part).strip()


def installer_policy_args(config_data: dict[str, str], name: str, preseed_url: str, profile: str) -> str:
    policy = INSTALLER_POLICIES[name]
    policy_args = ""
    if policy.config_key:
        policy_args = " ".join(config_data.get(policy.config_key, "").split())
    elif name == "installer-preseed" and profile in {PROFILE_DEBIAN, PROFILE_KALI_LINUX, PROFILE_KALI_PURPLE}:
        policy_args = " ".join(config_data.get("PRESEED_COMMON_KERNEL_ARGS", "").split())
    url_args = ""
    if preseed_url:
        url_args = f"url={preseed_url}"
    if name == "installer-preseed" and profile not in {PROFILE_DEBIAN, PROFILE_KALI_LINUX, PROFILE_KALI_PURPLE}:
        policy_args = ""
    return " ".join(part for part in (policy_args, url_args) if part).strip()
