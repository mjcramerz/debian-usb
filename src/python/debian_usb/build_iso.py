from __future__ import annotations

import datetime as dt
import gzip
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from typing import Any

from .boot_inspect import validate_netinst_payload_iso
from .boot_parse import _find_boot_entries, _select_text_installer_entry
from .constants import PROFILE_DEBIAN
from .iso_source import open_source
from .live_hooks import (
    DEBIAN_LIVE_HOOK_KERNEL_ARGS,
    DEBIAN_LIVE_HOOK_PACKAGES,
    DEBIAN_LIVE_INITRAMFS_MODULES,
    DEBIAN_LIVE_KERNEL_CONFIG_SYMBOLS,
    DEBIAN_LIVE_LANGUAGE,
    DEBIAN_LIVE_LOCALE,
    DEBIAN_LIVE_MODULE_ALIAS_CANDIDATES,
    LIVE_SYSTEMD_DISABLE_LINKS,
    LIVE_SYSTEMD_MASK_UNITS,
    stage_debian_live_config_hooks,
    stage_debian_live_apt_policy,
    stage_debian_live_locale,
    stage_debian_live_medium_wifi_config,
    stage_debian_live_wifi_config,
    stage_live_kernel_module_policy,
    stage_live_systemd_masks,
)
from .live_tools import live_tool_packages_for_build_distro

SCHEMA_VERSION = 1
FEATURE_SPEC_SCHEMA_VERSION = 1

DISTRO_DEBIAN = "debian"
FEATURE_SPEC_DISTROS = {DISTRO_DEBIAN, "ubuntu", "kali-linux"}
SUPPORTED_BUILD_DISTROS = FEATURE_SPEC_DISTROS
FEATURE_SPEC_STAGES = {"live", "d-i"}
FEATURE_SPEC_KINDS = {"modules", "deb", "udeb"}

INSTALLER_MODES = {"none", "netinst", "live"}
KERNEL_MODES = {"stock-debian", "repo-package-stub", "local-kernel-deb-dir", "custom-apt-repo"}
ROOTFS_FORMATS = {"none", "squashfs", "erofs"}
CLEANUP_MODES = {"purge-workspace", "keep-workspace"}
EROFS_INSTALLER_COMPONENT_POLICIES = {"warn", "require"}
DIRECT_DI_BUILD_SOURCE_MODES = {"apt-source", "local-tree"}
DEFAULT_EROFS_FILESYSTEM_MODULE_ENTRIES = ["filesystem.squashfs"]
DEFAULT_EROFS_INSTALLER_COMPONENTS = ["build-config", "kernel-wedge", "iso-scan", "partman-auto", "partconf", "os-prober", "rescue"]
UDEB_REBUILD_SPEC_SCHEMA_VERSION = 1
UDEB_REBUILD_PACKAGE_ROLES = {"generic", "library", "main-menu"}
UDEB_REBUILD_KINDS = {"source-package", "linux-installer-kernel"}

DEFAULT_STATE_DIR = Path(os.environ.get("DEBIAN_USB_STATE_DIR", "/var/lib/debian-usb"))
DEFAULT_CACHE_DIR = Path(os.environ.get("DEBIAN_USB_CACHE_DIR", "/data/downloads/debian-usb"))
DEFAULT_LOG_DIR = Path(os.environ.get("DEBIAN_USB_LOG_DIR", "/var/log/debian-usb"))
DEFAULT_WORK_DIR = Path(os.environ.get("DEBIAN_USB_WORK_DIR", "/data/tmp/debian-usb"))

DEFAULT_DEBIAN_BUILD_APT_DEPS = [
    "apt-utils",
    "build-essential",
    "bzip2",
    "ca-certificates",
    "cpio",
    "cryptsetup",
    "debootstrap",
    "devscripts",
    "dpkg-dev",
    "e2fsprogs",
    "erofs-utils",
    "fakeroot",
    "file",
    "gpg",
    "initramfs-tools-core",
    "live-build",
    "parted",
    "rsync",
    "squashfs-tools",
    "systemd-container",
    "wget",
    "xorriso",
    "xz-utils",
]

REQUIRED_BUILD_COMMANDS = [
    "apt",
    "dpkg-name",
    "dpkg-buildpackage",
    "dpkg-deb",
    "dpkg-scanpackages",
    "lb",
    "mkfs.erofs",
]

PACKAGE_RE = re.compile(r"^[a-z0-9][a-z0-9+.-]*$")
MODULE_RE = re.compile(r"^[A-Za-z0-9_.+-]+$")
LIVE_IMAGE_ENTRY_RE = re.compile(r"^[A-Za-z0-9_.+-]+(?:/[A-Za-z0-9_.+-]+)*$")
KERNEL_CONFIG_SYMBOL_RE = re.compile(r"^CONFIG_[A-Z0-9_]+$")
KERNEL_CONFIG_ENTRY_ASSIGNMENT_RE = re.compile(r"^(CONFIG_[A-Z0-9_]+)\s*=\s*(.+)$")
KERNEL_CONFIG_ENTRY_DISABLED_RE = re.compile(r"^#\s*(CONFIG_[A-Z0-9_]+)\s+is\s+not\s+set$")
INITRD_KERNEL_VERSION_RE = re.compile(r"(?:^|/)(?:usr/)?lib/modules/([^/]+)/")
APT_SOURCE_FIELD_RE = re.compile(r"^([a-z0-9][a-z0-9+.-]*)(?: \(([^)]+)\))?$")

EROFS_INSTALLER_COMPONENT_PATH_HINTS = {
    "build-config": ("build/config", "build-config"),
    "kernel-wedge": ("kernel-wedge", "linux-image", "linux-modules", "erofs"),
    "iso-scan": ("iso-scan",),
    "partman-auto": ("partman-auto",),
    "partconf": ("partconf",),
    "os-prober": ("os-prober",),
    "rescue": ("rescue",),
}


def validate_build_iso_plan_file(plan_path: str) -> dict[str, Any]:
    plan = _load_json_file(Path(plan_path))
    normalized = _validate_build_iso_plan(plan)
    return {"valid": True, "plan": normalized}


def ensure_debian_build_deps() -> dict[str, Any]:
    build_deps = _configured_build_apt_deps()
    missing = [command for command in REQUIRED_BUILD_COMMANDS if shutil.which(command) is None]
    if not missing:
        return {"changed": False, "missing_commands": [], "packages": build_deps}

    _run_simple(["apt-get", "update"])
    _run_simple(["apt-get", "install", "-y", "--no-install-recommends", *build_deps])
    still_missing = [command for command in REQUIRED_BUILD_COMMANDS if shutil.which(command) is None]
    if still_missing:
        raise RuntimeError(f"Debian Build ISO dependency installation finished but commands are still missing: {', '.join(still_missing)}")
    return {"changed": True, "missing_commands": missing, "packages": build_deps}


def build_debian_iso(plan_path: str) -> dict[str, Any]:
    ensure_debian_build_deps()
    plan = _validate_build_iso_plan(_load_json_file(Path(plan_path)))
    _apply_auto_installer_kernel_rebuild(plan)
    _apply_auto_source_package_udeb_rebuilds(plan)
    run_id = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    workspace_dir = DEFAULT_WORK_DIR / "build-iso" / run_id
    state_dir = DEFAULT_STATE_DIR / "build-iso" / run_id
    log_dir = DEFAULT_LOG_DIR / "build-iso"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{run_id}.log"
    output_dir = Path(plan["output_dir"])
    output_dir.mkdir(parents=True, exist_ok=True)
    state_dir.mkdir(parents=True, exist_ok=True)
    workspace_dir.mkdir(parents=True, exist_ok=True)

    udeb_rebuild_result: dict[str, Any] = {}
    udeb_rebuild_manifest_path = ""
    installer_localudeb_repo_path = ""
    direct_di_build_manifest_path = ""
    generated_udeb_dir: Path | None = None
    if plan["udeb_rebuilds"]:
        udeb_rebuild_result = _rebuild_udebs_from_source(
            rebuilds=plan["udeb_rebuilds"],
            workspace_dir=workspace_dir / "udeb-rebuilds",
            state_dir=state_dir / "udeb-rebuilds",
        )
        udeb_rebuild_manifest_path = str((state_dir / "udeb-rebuilds" / "manifest.json"))
        installer_localudeb_repo_path = udeb_rebuild_result.get("installer_localudeb_repo_path", "")
        generated_udeb_dir_value = udeb_rebuild_result.get("staged_udeb_dir", "")
        generated_udeb_dir = Path(generated_udeb_dir_value) if generated_udeb_dir_value else None

    effective_udeb_dirs: list[Path] = []
    if plan["local_udeb_dir"]:
        effective_udeb_dirs.append(Path(plan["local_udeb_dir"]))
    if generated_udeb_dir is not None:
        effective_udeb_dirs.append(generated_udeb_dir)

    installer_audit: dict[str, Any] | None = None
    installer_audit_path = ""
    if plan["rootfs_format"] == "erofs" and plan["installer_mode"] != "none":
        installer_audit = _audit_erofs_installer_support(plan, effective_udeb_dirs)
        installer_audit_path = str(state_dir / "erofs-installer-audit.json")
        Path(installer_audit_path).write_text(json.dumps(installer_audit, indent=2) + "\n", encoding="utf-8")
        if installer_audit["status"] == "error":
            raise RuntimeError(f"EROFS installer integration requirements were not met. Review {installer_audit_path}")

    warnings: list[str] = []
    direct_di_build_result: dict[str, Any] = {}
    kernel_evidence: dict[str, Any] = {}
    live_apt_archive: dict[str, Any] = {}
    with log_path.open("w", encoding="utf-8") as log_file:
        _log(log_file, f"Run ID: {run_id}")
        _log(log_file, f"Workspace: {workspace_dir}")
        _log(log_file, f"Plan: {json.dumps(plan, sort_keys=True)}")
        if installer_audit is not None:
            _log(log_file, f"Installer audit: {json.dumps(installer_audit, sort_keys=True)}")
        if plan["kernel_inspection_modules"] or plan["kernel_config_symbols"] or plan["kernel_target_version"]:
            kernel_evidence = _inspect_kernel_support(
                kernel_version=plan["kernel_target_version"],
                module_names=plan["kernel_inspection_modules"],
                module_alias_candidates=plan["module_alias_candidates"],
                config_symbols=plan["kernel_config_symbols"],
                module_tree_dir=plan["kernel_module_tree_dir"],
                download_if_missing=plan["kernel_download_if_missing"],
                log_file=log_file,
            )
            _log(log_file, f"Kernel evidence: {json.dumps(kernel_evidence, sort_keys=True)}")

        build_root = workspace_dir / "live-build"
        build_root.mkdir(parents=True, exist_ok=True)

        installer_export_result: dict[str, Any] = {}
        if plan["installer_mode"] != "none" and (effective_udeb_dirs or plan["direct_di_build_enabled"]):
            installer_export_result = _export_installer_udeb_workspace(
                local_udeb_dirs=effective_udeb_dirs,
                pkg_list_entries=_collect_installer_pkg_list_entries(udeb_rebuild_result),
                export_dir=state_dir / "installer-export",
                log_file=log_file,
            )
            installer_localudeb_repo_path = installer_export_result.get("installer_localudeb_repo_path", installer_localudeb_repo_path)
            if plan["direct_di_build_enabled"]:
                direct_di_build_result = _run_direct_di_build_driver(
                    plan=plan,
                    installer_export=installer_export_result,
                    workspace_dir=workspace_dir / "direct-di-build",
                    state_dir=state_dir / "direct-di-build",
                    log_file=log_file,
                )
                direct_di_build_manifest_path = direct_di_build_result.get("manifest_path", "")

        _run_logged(
            _lb_config_command(plan),
            cwd=build_root,
            log_file=log_file,
        )
        _materialize_workspace(build_root, plan, log_file, effective_udeb_dirs)
        _run_logged(["lb", "build"], cwd=build_root, log_file=log_file)
        iso_candidate = _locate_built_iso(build_root)

        resolved_modules: dict[str, Any] = {}
        if plan["installer_mode"] != "netinst":
            live_apt_archive = _validate_live_apt_archive(
                build_root / "binary",
                plan["suite"],
                plan["architecture"],
            )
            _log(log_file, f"Live APT archive: {json.dumps(live_apt_archive, sort_keys=True)}")
            resolved_modules = _scan_module_tree(
                build_root / "chroot",
                set(plan["initramfs_modules"]),
                set(plan["module_alias_candidates"]),
            )
            if plan["distro"] == DISTRO_DEBIAN:
                _enforce_debian_live_module_contract(resolved_modules)

        final_iso_path = output_dir / plan["image_name"]
        shutil.copy2(iso_candidate, final_iso_path)

        netinst_payload: dict[str, object] = {}
        if plan["installer_mode"] == "netinst":
            netinst_payload = validate_netinst_payload_iso(str(final_iso_path), plan["distro"])
            _log(log_file, f"Netinst payload: {json.dumps(netinst_payload, sort_keys=True)}")
        if plan["installer_mode"] != "none" and plan["storage_tool_packages"] and not effective_udeb_dirs:
            warnings.append("Storage tool packages were added to the live environment only; no prebuilt .udeb directory was provided for Debian Installer staging.")
        if installer_audit is not None:
            warnings.extend(installer_audit.get("warnings", []))
        warnings.extend(udeb_rebuild_result.get("warnings", []))
        warnings.extend(kernel_evidence.get("notes", []))
        missing_kernel_modules = [entry["name"] for entry in kernel_evidence.get("modules", []) if not entry.get("found")]
        if missing_kernel_modules:
            warnings.append(
                "Could not confirm requested kernel modules for the selected target kernel version: "
                + ", ".join(missing_kernel_modules)
            )
        if plan["kernel_config_symbols"] and not kernel_evidence.get("config_path"):
            warnings.append("No kernel config file could be resolved for the selected target kernel version.")
        if plan["rootfs_format"] == "erofs" and "erofs" not in _resolved_module_names(resolved_modules):
            warnings.append("The selected kernel tree did not expose erofs as a loadable or built-in module. Validate live and installer boot paths carefully.")
        if plan["rootfs_format"] == "erofs" and not _has_xxhash_module(resolved_modules):
            warnings.append("The selected kernel tree did not expose an xxhash module under the requested or alias module names. Validate the exact kernel module names carried by the live and installer boot paths.")

        manifest_path = state_dir / "manifest.json"
        manifest = {
            "schema_version": 1,
            "run_id": run_id,
            "built_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "iso_path": str(final_iso_path),
            "workspace_dir": str(build_root),
            "log_path": str(log_path),
            "warnings": warnings,
            "plan": plan,
            "resolved_modules": resolved_modules,
            "kernel_evidence": kernel_evidence,
            "installer_support_audit": installer_audit or {},
            "live_apt_archive": live_apt_archive,
            "netinst_payload": netinst_payload,
            "udeb_rebuilds": udeb_rebuild_result,
            "direct_di_build": direct_di_build_result if plan["direct_di_build_enabled"] else {},
            "output_size_bytes": final_iso_path.stat().st_size,
            "rootfs_format_actual": plan["rootfs_format"],
        }
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

        if plan["cleanup_mode"] == "purge-workspace":
            _run_logged(["lb", "clean", "--purge"], cwd=build_root, log_file=log_file)

    return {
        "run_id": run_id,
        "iso_path": str(final_iso_path),
        "workspace_dir": str(build_root),
        "log_path": str(log_path),
        "manifest_path": str(manifest_path),
        "installer_audit_path": installer_audit_path,
        "udeb_rebuild_manifest_path": udeb_rebuild_manifest_path,
        "installer_localudeb_repo_path": installer_localudeb_repo_path,
        "direct_di_build_manifest_path": direct_di_build_manifest_path,
        "warnings": warnings,
    }


def inspect_build_kernel_support(
    kernel_version: str = "",
    module_names: list[str] | None = None,
    module_alias_candidates: list[str] | None = None,
    config_symbols: list[str] | None = None,
    module_tree_dir: str = "",
    download_if_missing: bool = False,
) -> dict[str, Any]:
    normalized_kernel_version = str(kernel_version or "").strip()
    normalized_modules = _validate_optional_module_list(module_names, "module_names")
    normalized_aliases = _validate_optional_module_list(module_alias_candidates, "module_alias_candidates")
    normalized_config_symbols = _validate_kernel_config_symbol_list(config_symbols, "config_symbols")
    normalized_module_tree_dir = _validate_absolute_path(
        module_tree_dir,
        allow_missing=False,
        expect_directory=True,
        label="module_tree_dir",
        optional=True,
    )
    return _inspect_kernel_support(
        kernel_version=normalized_kernel_version,
        module_names=normalized_modules,
        module_alias_candidates=normalized_aliases,
        config_symbols=normalized_config_symbols,
        module_tree_dir=normalized_module_tree_dir,
        download_if_missing=download_if_missing,
    )


def _lb_config_command(plan: dict[str, Any]) -> list[str]:
    command = [
        "lb",
        "config",
        "--mode",
        "debian",
        "--distribution",
        plan["suite"],
        "--architectures",
        plan["architecture"],
        "--binary-images",
        "iso-hybrid",
        "--archive-areas",
        " ".join(plan["archive_areas"]),
        "--apt-indices",
        "true",
    ]
    if plan["installer_mode"] != "netinst":
        command.extend(["--system", "live"])
    if plan["mirror_bootstrap"]:
        command.extend(["--mirror-bootstrap", plan["mirror_bootstrap"]])
    if plan["mirror_chroot"]:
        command.extend(["--mirror-chroot", plan["mirror_chroot"]])
    if plan["mirror_binary"]:
        command.extend(["--mirror-binary", plan["mirror_binary"]])
    if plan["mirror_binary_security"]:
        command.extend(["--mirror-binary-security", plan["mirror_binary_security"]])
    if plan["mirror_debian_installer"]:
        command.extend(["--mirror-debian-installer", plan["mirror_debian_installer"]])
    if plan["keyring_packages"]:
        command.extend(["--keyring-packages", " ".join(plan["keyring_packages"])])
    if plan["installer_mode"] != "none":
        command.extend(["--debian-installer", plan["installer_mode"]])
        if plan["preseed_path"]:
            command.extend(["--debian-installer-preseedfile", "/preseed.cfg"])
        if plan["installer_distribution"]:
            command.extend(["--debian-installer-distribution", plan["installer_distribution"]])
        if plan["installer_boot_append"]:
            command.extend(["--bootappend-install", plan["installer_boot_append"]])
    if plan["bootappend_live"]:
        command.extend(["--bootappend-live", plan["bootappend_live"]])
    if plan["iso_application"]:
        command.extend(["--iso-application", plan["iso_application"]])
    if plan["iso_preparer"]:
        command.extend(["--iso-preparer", plan["iso_preparer"]])
    if plan["iso_publisher"]:
        command.extend(["--iso-publisher", plan["iso_publisher"]])
    if plan["iso_volume"]:
        command.extend(["--iso-volume", plan["iso_volume"]])
    if plan["kernel_mode"] != "stock-debian":
        command.extend(["--linux-flavours", " ".join(plan["kernel_flavours"])])
        command.extend(["--linux-packages", plan["kernel_package_stub"]])
    return command


def _validate_live_apt_archive(binary_root: Path, suite: str, architecture: str) -> dict[str, Any]:
    suite_root = binary_root / "dists" / suite
    release_path = next(
        (path for path in (suite_root / "InRelease", suite_root / "Release") if path.is_file()),
        None,
    )
    if release_path is None:
        raise RuntimeError(
            "Live image APT archive is incomplete: "
            f"missing dists/{suite}/InRelease or dists/{suite}/Release. "
            "The live environment enables file:/run/live/medium, so publishing this image would make apt update fail."
        )

    pool_root = binary_root / "pool"
    pool_components = sorted(
        path.name
        for path in pool_root.iterdir()
        if path.is_dir() and any(package.is_file() for package in path.rglob("*.deb"))
    ) if pool_root.is_dir() else []
    if not pool_components:
        raise RuntimeError(
            "Live image APT archive is incomplete: no binary packages were staged under pool/. "
            "The generated media repository could not satisfy package installation requests."
        )

    package_indices: list[Path] = []
    missing_components: list[str] = []
    for component in pool_components:
        component_indices = sorted(
            path
            for path in (suite_root / component / f"binary-{architecture}").glob("Packages*")
            if path.is_file()
        )
        if not component_indices:
            missing_components.append(component)
            continue
        package_indices.extend(component_indices)
    if missing_components:
        raise RuntimeError(
            "Live image APT archive is incomplete: "
            f"missing dists/{suite}/<component>/binary-{architecture}/Packages indexes for pool components: "
            f"{', '.join(missing_components)}. "
            "The live environment enables file:/run/live/medium, so publishing this image would make apt update fail."
        )

    return {
        "suite": suite,
        "architecture": architecture,
        "release_path": "/" + release_path.relative_to(binary_root).as_posix(),
        "package_index_paths": ["/" + path.relative_to(binary_root).as_posix() for path in package_indices],
        "pool_components": pool_components,
        "package_pool_present": True,
    }


def _materialize_workspace(build_root: Path, plan: dict[str, Any], log_file: Any, local_udeb_dirs: list[Path]) -> None:
    package_lists_dir = build_root / "config" / "package-lists"
    packages_chroot_dir = build_root / "config" / "packages.chroot"
    packages_binary_dir = build_root / "config" / "packages.binary"
    archives_dir = build_root / "config" / "archives"
    includes_chroot_early_dir = build_root / "config" / "includes.chroot"
    includes_chroot_dir = build_root / "config" / "includes.chroot_after_packages"
    includes_binary_dir = build_root / "config" / "includes.binary"
    includes_installer_dir = build_root / "config" / "includes.installer"
    bootloaders_dir = build_root / "config" / "bootloaders"
    debian_installer_dir = build_root / "config" / "debian-installer"
    hooks_dir = build_root / "config" / "hooks" / "normal"

    for path in (
        package_lists_dir,
        packages_chroot_dir,
        packages_binary_dir,
        archives_dir,
        includes_chroot_early_dir,
        includes_chroot_dir,
        includes_binary_dir,
        includes_installer_dir,
        bootloaders_dir,
        debian_installer_dir,
        hooks_dir,
    ):
        path.mkdir(parents=True, exist_ok=True)

    netinst_only = plan["installer_mode"] == "netinst"
    if not netinst_only:
        base_packages = list(plan["base_packages"])
        if plan["include_non_free_firmware"]:
            base_packages.append("live-task-non-free-firmware-pc")
        if plan["include_installer_launcher"]:
            base_packages.append("debian-installer-launcher")
        base_packages.extend(plan["storage_tool_packages"])
        _write_package_list(package_lists_dir / "base.list.chroot", base_packages)
        _write_package_list(package_lists_dir / "zz-user.list.chroot", plan["extra_chroot_packages"])
    binary_packages = _append_unique(plan["extra_binary_packages"], plan["storage_tool_packages"])
    _write_package_list(package_lists_dir / "zz-user.list.binary", binary_packages)

    if plan["preseed_path"]:
        preseed_content = Path(plan["preseed_path"]).read_text(encoding="utf-8")
        (includes_installer_dir / "preseed.cfg").write_text(preseed_content, encoding="utf-8")
        (debian_installer_dir / "preseed.cfg").write_text(preseed_content, encoding="utf-8")

    if plan["installer_include_dir"]:
        _copy_tree(Path(plan["installer_include_dir"]), includes_installer_dir)
    if plan["live_include_dir"] and not netinst_only:
        _copy_tree(Path(plan["live_include_dir"]), includes_chroot_dir)
    if plan["binary_include_dir"]:
        _copy_tree(Path(plan["binary_include_dir"]), includes_binary_dir)
    if plan["bootloader_override_dir"]:
        _copy_tree(Path(plan["bootloader_override_dir"]), bootloaders_dir)

    if plan["custom_apt_repo"]:
        (archives_dir / "custom-kernel.list.chroot").write_text(plan["custom_apt_repo"].strip() + "\n", encoding="utf-8")
        binary_repo_line = plan["custom_binary_apt_repo"] or plan["custom_apt_repo"]
        (archives_dir / "custom-kernel.list.binary").write_text(binary_repo_line.strip() + "\n", encoding="utf-8")
        if plan["custom_apt_repo_key_path"]:
            key_data = Path(plan["custom_apt_repo_key_path"]).read_bytes()
            (archives_dir / "custom-kernel.key.chroot").write_bytes(key_data)
            (archives_dir / "custom-kernel.key.binary").write_bytes(key_data)
        if plan["custom_apt_repo_pin"]:
            (archives_dir / "custom-kernel.pref.chroot").write_text(plan["custom_apt_repo_pin"].strip() + "\n", encoding="utf-8")
            (archives_dir / "custom-kernel.pref.binary").write_text(plan["custom_apt_repo_pin"].strip() + "\n", encoding="utf-8")

    if plan["local_deb_dir"]:
        _stage_named_packages(Path(plan["local_deb_dir"]), packages_chroot_dir, ".deb", log_file)
    if plan["kernel_deb_dir"]:
        _stage_named_packages(Path(plan["kernel_deb_dir"]), packages_chroot_dir, ".deb", log_file)
    for local_udeb_dir in local_udeb_dirs:
        _stage_binary_packages(local_udeb_dir, packages_binary_dir, ".udeb")

    if not netinst_only:
        # Early masks break fwupd maintainer-script presets. live-build guards
        # service starts during package installation; mask only after packages.
        stage_live_systemd_masks(includes_chroot_dir)
        configure_locale = plan["distro"] == DISTRO_DEBIAN
        if configure_locale:
            for policy_root in (includes_chroot_early_dir, includes_chroot_dir):
                stage_debian_live_locale(policy_root)
        _write_live_runtime_policy_hook(
            hooks_dir / "6000-live-runtime-policy.hook.chroot",
            configure_locale=configure_locale,
        )

    if plan["initramfs_modules"] and not netinst_only:
        stage_live_kernel_module_policy(includes_chroot_dir, plan["initramfs_modules"])
        _write_initramfs_refresh_hook(hooks_dir / "7000-initramfs-modules.hook.chroot")

    if plan["filesystem_module_entries"] and not netinst_only:
        live_dir = includes_binary_dir / "live"
        live_dir.mkdir(parents=True, exist_ok=True)
        _write_value_list(live_dir / "filesystem.module", plan["filesystem_module_entries"])

    if plan["distro"] == DISTRO_DEBIAN and not netinst_only:
        stage_debian_live_wifi_config(includes_chroot_dir)
        stage_debian_live_apt_policy(includes_chroot_dir)
        stage_debian_live_config_hooks(includes_binary_dir / "live")
        stage_debian_live_medium_wifi_config(includes_binary_dir / "live")

    if plan["rootfs_format"] == "erofs" and not netinst_only:
        _write_erofs_binary_hook(
            hooks_dir / "9990-erofs-rootfs.hook.binary",
            compressor=plan["erofs_compressor"],
            extra_args=plan["erofs_extra_args"],
        )


def _write_package_list(path: Path, packages: list[str]) -> None:
    unique_packages = []
    seen: set[str] = set()
    for package in packages:
        package = package.strip()
        if not package or package in seen:
            continue
        seen.add(package)
        unique_packages.append(package)
    if not unique_packages:
        return
    path.write_text("\n".join(unique_packages) + "\n", encoding="utf-8")


def _write_value_list(path: Path, values: list[str]) -> None:
    unique_values: list[str] = []
    seen: set[str] = set()
    for value in values:
        token = str(value).strip()
        if not token or token in seen:
            continue
        seen.add(token)
        unique_values.append(token)
    if not unique_values:
        return
    path.write_text("\n".join(unique_values) + "\n", encoding="utf-8")


def _write_initramfs_refresh_hook(path: Path) -> None:
    content = """#!/bin/sh
set -eu
export DEBIAN_FRONTEND=noninteractive
update-initramfs -u
"""
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


def _write_live_runtime_policy_hook(path: Path, *, configure_locale: bool) -> None:
    lines = [
        "#!/bin/sh",
        "set -eu",
        "export DEBIAN_FRONTEND=noninteractive",
        "export LANG=C.UTF-8",
        "export LC_ALL=C.UTF-8",
        "install -d -m 0755 -- /etc/systemd/system",
    ]
    lines.extend(f"rm -f -- /{relative_path}" for relative_path in LIVE_SYSTEMD_DISABLE_LINKS)
    lines.extend(
        f"ln -sfn -- /dev/null /etc/systemd/system/{unit}"
        for unit in LIVE_SYSTEMD_MASK_UNITS
    )
    if configure_locale:
        lines.extend(
            [
                f"locale-gen {_shell_quote(DEBIAN_LIVE_LOCALE)}",
                (
                    f"update-locale LANG={_shell_quote(DEBIAN_LIVE_LOCALE)} "
                    f"LANGUAGE={_shell_quote(DEBIAN_LIVE_LANGUAGE)}"
                ),
            ]
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    path.chmod(0o755)


def _write_erofs_binary_hook(path: Path, compressor: str, extra_args: str) -> None:
    extra_tokens = extra_args.split() if extra_args else []
    positional_args = " ".join(_shell_quote(token) for token in extra_tokens)
    content = f"""#!/bin/sh
set -eu

compressor={_shell_quote(compressor)}
set -- {positional_args}
build_root="$(pwd)"
chroot_dir="${{build_root}}/chroot"
live_dir="${{build_root}}/binary/live"
if [ ! -d "${{live_dir}}" ]; then
  live_dir="$(find "${{build_root}}/binary" -type d -path '*/live' | head -n 1)"
fi

if [ ! -d "${{chroot_dir}}" ]; then
  printf 'missing chroot directory for EROFS conversion: %s\n' "${{chroot_dir}}" >&2
  exit 1
fi
if [ -z "${{live_dir}}" ]; then
  printf '%s\n' "could not locate binary/live directory for EROFS conversion" >&2
  exit 1
fi

target="${{live_dir}}/filesystem.squashfs"
tmp_target="${{live_dir}}/filesystem.erofs.tmp"
rm -f -- "${{tmp_target}}"
mkfs.erofs -z "${{compressor}}" "$@" "${{tmp_target}}" "${{chroot_dir}}"
mv -- "${{tmp_target}}" "${{target}}"
cat > "${{live_dir}}/filesystem.debian-usb.json" <<'EOF'
{json.dumps({"actual_format": "erofs", "preserved_filename": "filesystem.squashfs", "compressor": compressor, "extra_args": extra_tokens}, indent=2)}
EOF
"""
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


def _stage_named_packages(source_dir: Path, destination_dir: Path, suffix: str, log_file: Any) -> None:
    files = sorted(path for path in source_dir.iterdir() if path.is_file() and path.name.lower().endswith(suffix))
    if not files:
        raise RuntimeError(f"no {suffix} packages were found in {source_dir}")
    for package_path in files:
        copied_path = destination_dir / package_path.name
        shutil.copy2(package_path, copied_path)
        before = set(path.name for path in destination_dir.glob(f"*{suffix}"))
        _run_logged(["dpkg-name", "--overwrite", copied_path.name], cwd=destination_dir, log_file=log_file)
        after = set(path.name for path in destination_dir.glob(f"*{suffix}"))
        if copied_path.name in before and copied_path.name not in after:
            continue


def _stage_binary_packages(source_dir: Path, destination_dir: Path, suffix: str) -> None:
    files = sorted(path for path in source_dir.iterdir() if path.is_file() and path.name.lower().endswith(suffix))
    if not files:
        raise RuntimeError(f"no {suffix} packages were found in {source_dir}")
    for package_path in files:
        shutil.copy2(package_path, destination_dir / package_path.name)


def _copy_tree(source_dir: Path, destination_dir: Path) -> None:
    for child in source_dir.iterdir():
        destination_path = destination_dir / child.name
        if child.is_dir():
            shutil.copytree(child, destination_path, dirs_exist_ok=True)
        else:
            destination_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(child, destination_path)


def _collect_installer_pkg_list_entries(udeb_rebuild_result: dict[str, Any]) -> list[str]:
    entries: list[str] = []
    for rebuild in udeb_rebuild_result.get("rebuilds", []):
        entries.extend(rebuild.get("pkg_list_local_entries", []))
    return entries


def _export_installer_udeb_workspace(local_udeb_dirs: list[Path], pkg_list_entries: list[str], export_dir: Path, log_file: Any) -> dict[str, Any]:
    localudebs_dir = export_dir / "localudebs"
    pkg_lists_dir = export_dir / "pkg-lists"
    repo_dir = export_dir / "repo"
    for path in (localudebs_dir, pkg_lists_dir, repo_dir):
        path.mkdir(parents=True, exist_ok=True)

    copied_udebs: list[Path] = []
    for udeb_dir in local_udeb_dirs:
        for staged_udeb in sorted(udeb_dir.glob("*.udeb")):
            target_localudeb = localudebs_dir / staged_udeb.name
            target_repo = repo_dir / staged_udeb.name
            shutil.copy2(staged_udeb, target_localudeb)
            shutil.copy2(staged_udeb, target_repo)
            copied_udebs.append(target_repo)

    resolved_pkg_list_entries = list(pkg_list_entries)
    if not resolved_pkg_list_entries:
        for copied_udeb in copied_udebs:
            package_name = _dpkg_deb_field(copied_udeb, "Package")
            if package_name and package_name not in resolved_pkg_list_entries:
                resolved_pkg_list_entries.append(package_name)

    pkg_list_path = pkg_lists_dir / "local"
    if resolved_pkg_list_entries:
        _write_value_list(pkg_list_path, resolved_pkg_list_entries)
    else:
        pkg_list_path.write_text("", encoding="utf-8")

    _create_local_udeb_repo(repo_dir, log_file)
    sources_list_udeb_local_path = export_dir / "sources.list.udeb.local"
    sources_list_udeb_local_path.write_text(f"deb [trusted=yes] {repo_dir.as_uri()} ./\n", encoding="utf-8")
    manifest = {
        "local_udeb_dirs": [str(path) for path in local_udeb_dirs],
        "localudebs_dir": str(localudebs_dir),
        "installer_localudeb_repo_path": str(repo_dir),
        "pkg_list_local_path": str(pkg_list_path),
        "sources_list_udeb_local_path": str(sources_list_udeb_local_path),
        "pkg_list_entries": resolved_pkg_list_entries,
        "udeb_count": len(copied_udebs),
    }
    manifest_path = export_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    manifest["manifest_path"] = str(manifest_path)
    return manifest


def _run_direct_di_build_driver(plan: dict[str, Any], installer_export: dict[str, Any], workspace_dir: Path, state_dir: Path, log_file: Any) -> dict[str, Any]:
    workspace_dir.mkdir(parents=True, exist_ok=True)
    state_dir.mkdir(parents=True, exist_ok=True)
    source_checkout = _prepare_direct_di_source(plan, workspace_dir, log_file)
    build_dir = _resolve_direct_di_build_dir(source_checkout)
    if plan["direct_di_build_reallyclean_before"]:
        _run_logged(["make", "reallyclean"], cwd=build_dir, log_file=log_file)
    _sync_installer_export_to_di_build(installer_export, build_dir)

    built_targets: list[str] = []
    for target in plan["direct_di_build_targets"]:
        _run_logged(["fakeroot", "make", target], cwd=build_dir, log_file=log_file)
        built_targets.append(target)

    dest_dir = build_dir / "dest"
    dest_snapshot_dir = state_dir / "dest"
    if dest_dir.is_dir():
        shutil.copytree(dest_dir, dest_snapshot_dir, dirs_exist_ok=True)
    inspection = _inspect_direct_di_build_tree(build_dir)
    if plan["direct_di_build_reallyclean_after"]:
        _run_logged(["make", "reallyclean"], cwd=build_dir, log_file=log_file)

    manifest = {
        "source_checkout": str(source_checkout),
        "build_dir": str(build_dir),
        "targets": built_targets,
        "dest_snapshot_dir": str(dest_snapshot_dir) if dest_snapshot_dir.exists() else "",
        "installer_export_manifest_path": installer_export.get("manifest_path", ""),
        "inspection": inspection,
    }
    manifest_path = state_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    manifest["manifest_path"] = str(manifest_path)
    return manifest


def _prepare_direct_di_source(plan: dict[str, Any], workspace_dir: Path, log_file: Any) -> Path:
    source_mode = plan["direct_di_build_source_mode"]
    if source_mode == "local-tree":
        source_tree = Path(plan["direct_di_build_source_tree"])
        destination = workspace_dir / source_tree.name
        shutil.copytree(source_tree, destination, dirs_exist_ok=True)
        return destination

    source_root = workspace_dir / "source"
    source_root.mkdir(parents=True, exist_ok=True)
    source_package = plan["direct_di_build_source_package"] or "debian-installer"
    before_dirs = {path.name for path in source_root.iterdir() if path.is_dir()}
    _run_logged(["apt", "source", source_package], cwd=source_root, log_file=log_file)
    source_checkout = _discover_apt_source_checkout(source_root, before_dirs, source_package)
    if plan["direct_di_build_dep_packages"]:
        _run_logged(["apt-get", "install", "-y", "--no-install-recommends", *plan["direct_di_build_dep_packages"]], cwd=source_root, log_file=log_file)
    else:
        try:
            _run_logged(["apt-get", "build-dep", "-y", source_package], cwd=source_root, log_file=log_file)
        except RuntimeError as exc:
            raise RuntimeError(
                f"apt-get build-dep failed for Debian Installer source {source_package}. Provide direct_di_build_dep_packages or ensure deb-src entries are enabled."
            ) from exc
    return source_checkout


def _resolve_direct_di_build_dir(source_checkout: Path) -> Path:
    candidates = [
        source_checkout / "installer" / "build",
        source_checkout / "build",
    ]
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    raise RuntimeError(f"could not locate a Debian Installer build directory under {source_checkout}")


def _sync_installer_export_to_di_build(installer_export: dict[str, Any], build_dir: Path) -> None:
    localudebs_dir = build_dir / "localudebs"
    pkg_lists_dir = build_dir / "pkg-lists"
    for path in (localudebs_dir, pkg_lists_dir):
        path.mkdir(parents=True, exist_ok=True)
    shutil.copytree(Path(installer_export["localudebs_dir"]), localudebs_dir, dirs_exist_ok=True)
    shutil.copy2(Path(installer_export["pkg_list_local_path"]), pkg_lists_dir / "local")
    shutil.copy2(Path(installer_export["sources_list_udeb_local_path"]), build_dir / "sources.list.udeb.local")


def _inspect_direct_di_build_tree(build_dir: Path) -> dict[str, Any]:
    inspection_patterns = ("erofs", "xxhash", "preseed", "live-installer")
    tmp_dir = build_dir / "tmp"
    matches: list[str] = []
    if tmp_dir.is_dir():
        for path in tmp_dir.rglob("*"):
            lowered = str(path.relative_to(build_dir)).lower()
            if any(pattern in lowered for pattern in inspection_patterns):
                matches.append(str(path.relative_to(build_dir)))
    dest_dir = build_dir / "dest"
    dest_entries = sorted(str(path.relative_to(dest_dir)) for path in dest_dir.rglob("*") if path.is_file()) if dest_dir.is_dir() else []
    return {"tmp_matches": sorted(matches), "dest_entries": dest_entries}


def _rebuild_udebs_from_source(rebuilds: list[dict[str, Any]], workspace_dir: Path, state_dir: Path) -> dict[str, Any]:
    workspace_dir.mkdir(parents=True, exist_ok=True)
    state_dir.mkdir(parents=True, exist_ok=True)
    staged_udeb_dir = state_dir / "staged-udebs"
    staged_udeb_dir.mkdir(parents=True, exist_ok=True)
    build_log_path = state_dir / "build.log"
    results: list[dict[str, Any]] = []
    warnings: list[str] = []

    with build_log_path.open("w", encoding="utf-8") as log_file:
        for rebuild in rebuilds:
            _emit_progress(f"Rebuild workspace ready for {rebuild['source_package']} ({rebuild['rebuild_kind']}).")
            result = _rebuild_udeb_entry(rebuild, workspace_dir, staged_udeb_dir, log_file)
            results.append(result)
            warnings.extend(result.get("warnings", []))

        installer_workspace_dir = state_dir / "installer-build"
        localudebs_dir = installer_workspace_dir / "localudebs"
        pkg_lists_dir = installer_workspace_dir / "pkg-lists"
        repo_dir = installer_workspace_dir / "repo"
        for path in (localudebs_dir, pkg_lists_dir, repo_dir):
            path.mkdir(parents=True, exist_ok=True)

        for staged_udeb in sorted(staged_udeb_dir.glob("*.udeb")):
            shutil.copy2(staged_udeb, localudebs_dir / staged_udeb.name)
            shutil.copy2(staged_udeb, repo_dir / staged_udeb.name)

        pkg_list_entries: list[str] = []
        for result in results:
            pkg_list_entries.extend(result.get("pkg_list_local_entries", []))
        pkg_list_path = pkg_lists_dir / "local"
        if pkg_list_entries:
            _write_value_list(pkg_list_path, pkg_list_entries)
        else:
            pkg_list_path.write_text("", encoding="utf-8")
        _emit_progress("Creating the local Debian Installer udeb repository metadata.")
        _create_local_udeb_repo(repo_dir, log_file)
        sources_list_udeb_local_path = installer_workspace_dir / "sources.list.udeb.local"
        sources_list_udeb_local_path.write_text(f"deb [trusted=yes] {repo_dir.as_uri()} ./\n", encoding="utf-8")

    manifest = {
        "schema_version": UDEB_REBUILD_SPEC_SCHEMA_VERSION,
        "build_log_path": str(build_log_path),
        "staged_udeb_dir": str(staged_udeb_dir),
        "installer_localudebs_dir": str(state_dir / "installer-build" / "localudebs"),
        "installer_localudeb_repo_path": str(state_dir / "installer-build" / "repo"),
        "installer_pkg_list_local_path": str(state_dir / "installer-build" / "pkg-lists" / "local"),
        "installer_sources_list_udeb_local_path": str(state_dir / "installer-build" / "sources.list.udeb.local"),
        "warnings": warnings,
        "rebuilds": results,
    }
    manifest_path = state_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    manifest["manifest_path"] = str(manifest_path)
    return manifest


def _rebuild_udeb_entry(rebuild: dict[str, Any], workspace_dir: Path, staged_udeb_dir: Path, log_file: Any) -> dict[str, Any]:
    if rebuild["rebuild_kind"] == "linux-installer-kernel":
        return _rebuild_linux_installer_kernel(rebuild, workspace_dir, staged_udeb_dir, log_file)
    return _rebuild_single_udeb(rebuild, workspace_dir, staged_udeb_dir, log_file)


def _rebuild_single_udeb(rebuild: dict[str, Any], workspace_dir: Path, staged_udeb_dir: Path, log_file: Any) -> dict[str, Any]:
    spec_workspace_dir = workspace_dir / f"{rebuild['source_package']}-{rebuild['udeb_package']}"
    spec_workspace_dir.mkdir(parents=True, exist_ok=True)
    source_root = spec_workspace_dir / "source"
    source_root.mkdir(parents=True, exist_ok=True)

    before_dirs = {path.name for path in source_root.iterdir() if path.is_dir()}
    source_selector = rebuild["source_package"]
    if rebuild["source_version"]:
        source_selector = f"{source_selector}={rebuild['source_version']}"
    _emit_progress(f"Fetching Debian source package {source_selector}.")
    _run_logged(["apt", "source", source_selector], cwd=source_root, log_file=log_file)
    source_checkout = _discover_apt_source_checkout(source_root, before_dirs, rebuild["source_package"])

    warnings: list[str] = []
    try:
        _emit_progress(f"Installing build dependencies for {source_selector}.")
        _run_logged(["apt-get", "build-dep", "-y", source_selector], cwd=source_root, log_file=log_file)
    except RuntimeError as exc:
        if rebuild["build_dep_packages"]:
            warnings.append(
                f"apt-get build-dep failed for {source_selector}; continuing with explicit build_dep_packages: {exc}"
            )
        else:
            raise RuntimeError(
                f"apt-get build-dep failed for {source_selector}. Ensure deb-src entries are enabled or provide build_dep_packages in the UDEB rebuild spec."
            ) from exc
    if rebuild["build_dep_packages"]:
        _emit_progress(f"Installing explicit build dependency packages for {source_selector}.")
        _run_logged(["apt-get", "install", "-y", "--no-install-recommends", *rebuild["build_dep_packages"]], cwd=source_root, log_file=log_file)

    if rebuild["source_overlay_dir"]:
        _emit_progress(f"Applying source overlay for {source_selector}.")
        _copy_tree(Path(rebuild["source_overlay_dir"]), source_checkout)

    _emit_progress(f"Preparing udeb packaging metadata for {rebuild['udeb_package']}.")
    _prepare_udeb_packaging(source_checkout, rebuild)
    _emit_progress(f"Running dpkg-buildpackage for {rebuild['udeb_package']} (this can take several minutes).")
    _run_logged(["dpkg-buildpackage", "-b", "-uc", "-us", "-rfakeroot"], cwd=source_checkout, log_file=log_file)

    built_udeb_paths = _collect_built_udebs(source_checkout.parent, rebuild["udeb_package"])
    staged_paths: list[str] = []
    for built_udeb_path in built_udeb_paths:
        destination_path = staged_udeb_dir / built_udeb_path.name
        shutil.copy2(built_udeb_path, destination_path)
        staged_paths.append(str(destination_path))

    return {
        "rebuild_kind": rebuild["rebuild_kind"],
        "source_package": rebuild["source_package"],
        "source_version": rebuild["source_version"],
        "binary_package": rebuild["binary_package"],
        "udeb_package": rebuild["udeb_package"],
        "package_role": rebuild["package_role"],
        "source_checkout": str(source_checkout),
        "built_udeb_paths": [str(path) for path in built_udeb_paths],
        "staged_udeb_paths": staged_paths,
        "pkg_list_local_entries": [rebuild["udeb_package"]],
        "warnings": warnings,
    }


def _rebuild_linux_installer_kernel(rebuild: dict[str, Any], workspace_dir: Path, staged_udeb_dir: Path, log_file: Any) -> dict[str, Any]:
    spec_workspace_dir = workspace_dir / f"{rebuild['source_package']}-kernel-installer"
    spec_workspace_dir.mkdir(parents=True, exist_ok=True)
    source_root = spec_workspace_dir / "source"
    source_root.mkdir(parents=True, exist_ok=True)

    before_dirs = {path.name for path in source_root.iterdir() if path.is_dir()}
    source_selector = rebuild["source_package"]
    if rebuild["source_version"]:
        source_selector = f"{source_selector}={rebuild['source_version']}"
    _emit_progress(f"Fetching Debian kernel source package {source_selector}.")
    _run_logged(["apt", "source", source_selector], cwd=source_root, log_file=log_file)
    source_checkout = _discover_apt_source_checkout(source_root, before_dirs, rebuild["source_package"])

    warnings: list[str] = []
    try:
        _emit_progress(f"Installing kernel build dependencies for {source_selector}.")
        _run_logged(["apt-get", "build-dep", "-y", source_selector], cwd=source_root, log_file=log_file)
    except RuntimeError as exc:
        if rebuild["build_dep_packages"]:
            warnings.append(
                f"apt-get build-dep failed for kernel source {source_selector}; continuing with explicit build_dep_packages: {exc}"
            )
        else:
            raise RuntimeError(
                f"apt-get build-dep failed for kernel source {source_selector}. Ensure deb-src entries are enabled or provide build_dep_packages in the kernel installer rebuild spec."
            ) from exc
    if rebuild["build_dep_packages"]:
        _emit_progress(f"Installing explicit kernel build dependency packages for {source_selector}.")
        _run_logged(["apt-get", "install", "-y", "--no-install-recommends", *rebuild["build_dep_packages"]], cwd=source_root, log_file=log_file)

    if rebuild["source_overlay_dir"]:
        _emit_progress(f"Applying kernel source overlay for {source_selector}.")
        _copy_tree(Path(rebuild["source_overlay_dir"]), source_checkout)

    _emit_progress("Updating Debian installer kernel module lists and config fragments.")
    _prepare_linux_installer_kernel_source(source_checkout, rebuild)
    _emit_progress("Running dpkg-buildpackage for the Debian installer kernel udebs (this can take several minutes).")
    _run_logged(["dpkg-buildpackage", *rebuild["dpkg_buildpackage_args"]], cwd=source_checkout, log_file=log_file)

    built_udeb_paths = sorted(source_checkout.parent.glob("*.udeb"))
    if not built_udeb_paths:
        raise RuntimeError(f"dpkg-buildpackage did not produce any kernel installer .udeb files under {source_checkout.parent}")
    staged_paths: list[str] = []
    for built_udeb_path in built_udeb_paths:
        destination_path = staged_udeb_dir / built_udeb_path.name
        shutil.copy2(built_udeb_path, destination_path)
        staged_paths.append(str(destination_path))

    return {
        "rebuild_kind": rebuild["rebuild_kind"],
        "source_package": rebuild["source_package"],
        "source_version": rebuild["source_version"],
        "source_checkout": str(source_checkout),
        "built_udeb_paths": [str(path) for path in built_udeb_paths],
        "staged_udeb_paths": staged_paths,
        "module_targets": rebuild["module_targets"],
        "package_list_append_text": rebuild["package_list_append_text"],
        "kernel_config_entries": rebuild["kernel_config_entries"],
        "pkg_list_local_entries": rebuild["pkg_list_local_entries"],
        "warnings": warnings,
    }


def _discover_apt_source_checkout(source_root: Path, before_dirs: set[str], source_package: str) -> Path:
    candidates = [path for path in source_root.iterdir() if path.is_dir() and path.name not in before_dirs]
    if not candidates:
        candidates = [path for path in source_root.iterdir() if path.is_dir() and path.name.startswith(f"{source_package}-")]
    if not candidates:
        raise RuntimeError(f"could not determine extracted apt source checkout for {source_package} under {source_root}")
    candidates.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    return candidates[0]


def _prepare_udeb_packaging(source_checkout: Path, rebuild: dict[str, Any]) -> None:
    debian_dir = source_checkout / "debian"
    control_path = debian_dir / "control"
    if not control_path.is_file():
        raise RuntimeError(f"debian/control is missing in source checkout: {source_checkout}")

    paragraphs = _parse_debian_control(control_path.read_text(encoding="utf-8"))
    source_paragraph, binary_paragraphs = paragraphs[0], paragraphs[1:]
    if "Source" not in source_paragraph["fields"]:
        raise RuntimeError(f"debian/control did not start with a Source stanza: {control_path}")

    source_binary = _find_control_package_paragraph(binary_paragraphs, rebuild["binary_package"])
    if source_binary is None:
        raise RuntimeError(f"could not find binary package stanza {rebuild['binary_package']} in {control_path}")

    udeb_paragraph = _find_control_package_paragraph(binary_paragraphs, rebuild["udeb_package"])
    if udeb_paragraph is None:
        udeb_paragraph = {"fields": dict(source_binary["fields"]), "order": list(source_binary["order"])}
        paragraphs.append(udeb_paragraph)

    _set_control_field(udeb_paragraph, "Package", rebuild["udeb_package"])
    _set_control_field(udeb_paragraph, "Package-Type", "udeb")
    _set_control_field(udeb_paragraph, "Section", "debian-installer")
    if rebuild["architecture"]:
        _set_control_field(udeb_paragraph, "Architecture", rebuild["architecture"])
    else:
        _set_control_field(udeb_paragraph, "Architecture", source_binary["fields"].get("Architecture", "any"))
    _set_control_field(udeb_paragraph, "Priority", "optional")
    _set_control_field(udeb_paragraph, "Description", _compose_udeb_description(rebuild, source_binary["fields"].get("Description", "")))
    _remove_control_field(udeb_paragraph, "Multi-Arch")

    if rebuild["depends"]:
        _set_control_field(udeb_paragraph, "Depends", ", ".join(rebuild["depends"]))
    elif "Depends" not in udeb_paragraph["fields"]:
        _set_control_field(udeb_paragraph, "Depends", "${misc:Depends}, ${shlibs:Depends}")
    for field_name, values in (
        ("Provides", rebuild["provides"]),
        ("Conflicts", rebuild["conflicts"]),
        ("Replaces", rebuild["replaces"]),
    ):
        if values:
            _set_control_field(udeb_paragraph, field_name, ", ".join(values))
    if rebuild["package_role"] == "main-menu":
        _set_control_field(udeb_paragraph, "Installer-Menu-Item", rebuild["installer_menu_item"])
    else:
        _remove_control_field(udeb_paragraph, "Installer-Menu-Item")

    control_path.write_text(_render_debian_control(paragraphs), encoding="utf-8")
    _copy_binary_packaging_support_files(debian_dir, rebuild["copy_install_manifest_from"], rebuild["udeb_package"])
    if rebuild["package_role"] == "library":
        _ensure_udeb_library_rules(source_checkout / "debian" / "rules", rebuild["library_shlibs_udeb"])


def _prepare_linux_installer_kernel_source(source_checkout: Path, rebuild: dict[str, Any]) -> None:
    for module_target in rebuild.get("module_targets", []):
        target_path = source_checkout / module_target["path"]
        target_path.parent.mkdir(parents=True, exist_ok=True)
        if target_path.is_file():
            existing_modules = [line.strip() for line in target_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        else:
            existing_modules = []
        if module_target["merge_strategy"] == "replace":
            merged_modules = list(module_target["modules"])
        else:
            merged_modules = existing_modules[:]
            for module_name in module_target["modules"]:
                if module_name not in merged_modules:
                    merged_modules.append(module_name)
        target_path.write_text("\n".join(merged_modules) + "\n", encoding="utf-8")

    if rebuild.get("package_list_append_text"):
        package_list_path = source_checkout / "debian" / "installer" / "package-list"
        package_list_path.parent.mkdir(parents=True, exist_ok=True)
        existing_text = package_list_path.read_text(encoding="utf-8") if package_list_path.is_file() else ""
        append_text = rebuild["package_list_append_text"].rstrip() + "\n"
        if append_text not in existing_text:
            package_list_path.write_text(existing_text.rstrip() + "\n\n" + append_text if existing_text.strip() else append_text, encoding="utf-8")

    kernel_config_entries = rebuild.get("kernel_config_entries", [])
    if kernel_config_entries:
        _apply_kernel_config_entries(source_checkout, kernel_config_entries, rebuild.get("target_architecture", ""))


def _apply_kernel_config_entries(source_checkout: Path, config_entries: list[str], target_architecture: str) -> None:
    config_root = source_checkout / "debian" / "config"
    if not config_root.is_dir():
        raise RuntimeError(f"debian/config is missing in kernel source checkout: {source_checkout}")

    config_files = _select_kernel_config_fragment_paths(config_root, target_architecture)
    if not config_files:
        raise RuntimeError(f"could not find Debian kernel config fragments under {config_root}")

    parsed_entries = _normalize_kernel_config_entry_map(config_entries)
    for config_path in config_files:
        lines = config_path.read_text(encoding="utf-8").splitlines() if config_path.is_file() else []
        rendered = _rewrite_kernel_config_lines(lines, parsed_entries)
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text("\n".join(rendered) + "\n", encoding="utf-8")


def _select_kernel_config_fragment_paths(config_root: Path, target_architecture: str) -> list[Path]:
    generic_paths: list[Path] = []
    arch_paths: list[Path] = []
    fallback_paths: list[Path] = []
    for candidate in sorted(config_root.rglob("*")):
        if not candidate.is_file():
            continue
        if candidate.name != "config" and not candidate.name.startswith("config."):
            continue
        fallback_paths.append(candidate)
        relative_parts = candidate.relative_to(config_root).parts
        if target_architecture and target_architecture in relative_parts:
            arch_paths.append(candidate)
        elif relative_parts == ("config",):
            generic_paths.append(candidate)
    if arch_paths:
        return generic_paths + arch_paths
    return generic_paths or fallback_paths


def _normalize_kernel_config_entry_map(config_entries: list[str]) -> dict[str, str]:
    normalized: dict[str, str] = {}
    for entry in config_entries:
        symbol, rendered = _parse_kernel_config_entry(entry)
        normalized[symbol] = rendered
    return normalized


def _rewrite_kernel_config_lines(lines: list[str], config_entries: dict[str, str]) -> list[str]:
    rewritten = list(lines)
    for symbol, rendered in config_entries.items():
        matched = False
        next_lines: list[str] = []
        for line in rewritten:
            if _line_matches_kernel_config_symbol(line, symbol):
                if not matched:
                    next_lines.append(rendered)
                    matched = True
                continue
            next_lines.append(line)
        if not matched:
            if next_lines and next_lines[-1].strip():
                next_lines.append("")
            next_lines.append(rendered)
        rewritten = next_lines
    return rewritten


def _line_matches_kernel_config_symbol(line: str, symbol: str) -> bool:
    stripped = line.strip()
    if stripped.startswith(f"{symbol}="):
        return True
    return stripped == f"# {symbol} is not set"


def _parse_kernel_config_entry(entry: str) -> tuple[str, str]:
    stripped = str(entry).strip()
    if not stripped:
        raise ValueError("kernel config entry must not be empty")
    disabled_match = KERNEL_CONFIG_ENTRY_DISABLED_RE.match(stripped)
    if disabled_match:
        symbol = disabled_match.group(1)
        return symbol, f"# {symbol} is not set"
    assignment_match = KERNEL_CONFIG_ENTRY_ASSIGNMENT_RE.match(stripped)
    if not assignment_match:
        raise ValueError(f"invalid kernel config entry: {entry}")
    symbol = assignment_match.group(1)
    value = assignment_match.group(2).strip()
    if not value:
        raise ValueError(f"kernel config entry is missing a value: {entry}")
    if value == "n":
        return symbol, f"# {symbol} is not set"
    return symbol, f"{symbol}={value}"


def _apply_auto_installer_kernel_rebuild(plan: dict[str, Any]) -> None:
    if not plan["installer_kernel_rebuild_enabled"]:
        return

    metadata = _resolve_auto_installer_kernel_rebuild_metadata(
        source_iso_path=plan["installer_kernel_source_iso_path"],
        architecture=plan["architecture"],
    )
    plan["auto_installer_kernel_rebuild"] = metadata
    plan["udeb_rebuilds"].append(
        {
            "rebuild_kind": "linux-installer-kernel",
            "source_package": metadata["source_package"],
            "source_version": metadata["source_version"],
            "source_overlay_dir": "",
            "build_dep_packages": [],
            "module_targets": _default_installer_kernel_module_targets(plan["architecture"], plan["installer_kernel_modules"]),
            "package_list_append_text": "",
            "pkg_list_local_entries": [],
            "dpkg_buildpackage_args": ["-b", "-uc", "-us"],
            "kernel_config_entries": list(plan["installer_kernel_config_entries"]),
            "target_architecture": plan["architecture"],
        }
    )
    plan["kernel_inspection_modules"] = _append_unique(plan["kernel_inspection_modules"], plan["installer_kernel_modules"])
    plan["kernel_config_symbols"] = _append_unique(plan["kernel_config_symbols"], _kernel_config_symbols_from_entries(plan["installer_kernel_config_entries"]))
    if not plan["kernel_target_version"]:
        plan["kernel_target_version"] = metadata["kernel_version"]
    if not plan["kernel_download_if_missing"] and plan["kernel_target_version"]:
        plan["kernel_download_if_missing"] = True


def _apply_auto_source_package_udeb_rebuilds(plan: dict[str, Any]) -> None:
    for binary_package in plan["installer_auto_udeb_packages"]:
        metadata = _resolve_binary_package_udeb_rebuild_metadata(binary_package)
        plan["udeb_rebuilds"].append(
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


def _default_installer_kernel_module_targets(architecture: str, modules: list[str]) -> list[dict[str, Any]]:
    if not modules:
        return []
    return [
        {
            "path": "debian/installer/modules/kernel-image",
            "modules": list(modules),
            "merge_strategy": "append-unique",
        },
        {
            "path": f"debian/installer/modules/{architecture}/kernel-image",
            "modules": list(modules),
            "merge_strategy": "append-unique",
        },
    ]


def _kernel_config_symbols_from_entries(config_entries: list[str]) -> list[str]:
    symbols: list[str] = []
    for entry in config_entries:
        symbol, _ = _parse_kernel_config_entry(entry)
        if symbol not in symbols:
            symbols.append(symbol)
    return symbols


def _resolve_auto_installer_kernel_rebuild_metadata(*, source_iso_path: str, architecture: str) -> dict[str, str]:
    source = open_source(source_iso_path)
    entries = _find_boot_entries(source)
    installer_entry = _select_text_installer_entry(PROFILE_DEBIAN, entries)
    if installer_entry is None or not installer_entry.initrd_path:
        raise RuntimeError(f"could not resolve a Debian installer initrd from source ISO: {source_iso_path}")
    kernel_version = _detect_kernel_version_from_initrd(source, installer_entry.initrd_path)
    installer_kernel_package = f"kernel-image-{kernel_version}-di"
    source_package, source_version = _resolve_installer_kernel_source_version(installer_kernel_package)
    return {
        "source_iso_path": source_iso_path,
        "installer_initrd_path": installer_entry.initrd_path,
        "kernel_version": kernel_version,
        "installer_kernel_package": installer_kernel_package,
        "source_package": source_package,
        "source_version": source_version,
        "architecture": architecture,
    }


def _detect_kernel_version_from_initrd(source: Any, initrd_path: str) -> str:
    with tempfile.TemporaryDirectory(prefix="debian-usb-build-iso-initrd-") as temp_dir:
        initrd_file = Path(temp_dir) / Path(initrd_path).name
        source.extract_member(initrd_path, initrd_file)
        result = subprocess.run(["lsinitramfs", str(initrd_file)], capture_output=True, text=True, encoding="utf-8", check=False)
        if result.returncode != 0:
            message = result.stderr.strip() or result.stdout.strip() or "lsinitramfs failed"
            raise RuntimeError(f"failed to inspect installer initrd {initrd_path}: {message}")
    matches = []
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


def _resolve_installer_kernel_source_version(installer_kernel_package: str) -> tuple[str, str]:
    result = subprocess.run(["apt-cache", "showsrc", "linux"], capture_output=True, text=True, encoding="utf-8", check=False)
    if result.returncode != 0:
        message = result.stderr.strip() or result.stdout.strip() or "apt-cache showsrc linux failed"
        raise RuntimeError(f"failed to inspect Debian linux source metadata: {message}")
    for paragraph in _parse_debian_control(result.stdout):
        package_name = paragraph["fields"].get("Package", "").strip()
        version = paragraph["fields"].get("Version", "").strip()
        package_list = paragraph["fields"].get("Package-List", "")
        if installer_kernel_package not in package_list.split():
            continue
        if not package_name or not version:
            continue
        return package_name, version
    raise RuntimeError(f"could not resolve Debian source version for installer kernel package {installer_kernel_package} from APT metadata")


def _resolve_binary_package_udeb_rebuild_metadata(binary_package: str) -> dict[str, str]:
    result = subprocess.run(["apt-cache", "show", binary_package], capture_output=True, text=True, encoding="utf-8", check=False)
    if result.returncode != 0:
        message = result.stderr.strip() or result.stdout.strip() or "apt-cache show failed"
        raise RuntimeError(f"failed to inspect APT metadata for binary package {binary_package}: {message}")
    paragraphs = _parse_debian_control(result.stdout)
    selected: dict[str, Any] | None = None
    for paragraph in paragraphs:
        if paragraph["fields"].get("Package", "").strip() == binary_package:
            selected = paragraph
            break
    if selected is None and paragraphs:
        selected = paragraphs[0]
    if selected is None:
        raise RuntimeError(f"no APT metadata paragraphs were returned for binary package {binary_package}")

    package_name = selected["fields"].get("Package", "").strip()
    if package_name != binary_package:
        raise RuntimeError(f"APT metadata did not resolve the requested binary package {binary_package}")
    source_field = selected["fields"].get("Source", "").strip()
    binary_version = selected["fields"].get("Version", "").strip()
    if source_field:
        source_match = APT_SOURCE_FIELD_RE.match(source_field)
        if not source_match:
            raise RuntimeError(f"unexpected Source field format for binary package {binary_package}: {source_field}")
        source_package = source_match.group(1)
        source_version = source_match.group(2) or binary_version
    else:
        source_package = binary_package
        source_version = binary_version
    if not source_package or not source_version:
        raise RuntimeError(f"could not resolve source package/version for binary package {binary_package}")
    return {
        "binary_package": binary_package,
        "udeb_package": f"{binary_package}-udeb",
        "source_package": source_package,
        "source_version": source_version,
    }


def _compose_udeb_description(rebuild: dict[str, Any], original_description: str) -> str:
    original_lines = original_description.splitlines()
    original_short = original_lines[0].strip() if original_lines else rebuild["binary_package"]
    short_description = rebuild["description"] or f"{original_short} (udeb)"
    long_lines = rebuild["long_description"] or [
        f"stripped-down Debian Installer variant of {rebuild['binary_package']}.",
    ]
    return short_description + "\n" + "\n".join(long_lines)


def _parse_debian_control(text: str) -> list[dict[str, Any]]:
    paragraphs: list[dict[str, Any]] = []
    current_lines: list[str] = []
    for line in text.splitlines():
        if line.strip() == "":
            if current_lines:
                paragraphs.append(_parse_debian_control_paragraph(current_lines))
                current_lines = []
            continue
        current_lines.append(line.rstrip("\n"))
    if current_lines:
        paragraphs.append(_parse_debian_control_paragraph(current_lines))
    if not paragraphs:
        raise RuntimeError("debian/control did not contain any paragraphs")
    return paragraphs


def _parse_debian_control_paragraph(lines: list[str]) -> dict[str, Any]:
    fields: dict[str, str] = {}
    order: list[str] = []
    current_key = ""
    for line in lines:
        if line[:1].isspace():
            if not current_key:
                raise RuntimeError("invalid continuation line in debian/control")
            fields[current_key] += "\n" + line[1:]
            continue
        key, separator, value = line.partition(":")
        if separator != ":":
            raise RuntimeError(f"invalid debian/control line: {line}")
        current_key = key.strip()
        fields[current_key] = value.lstrip()
        order.append(current_key)
    return {"fields": fields, "order": order}


def _render_debian_control(paragraphs: list[dict[str, Any]]) -> str:
    rendered: list[str] = []
    for paragraph in paragraphs:
        lines: list[str] = []
        for field_name in paragraph["order"]:
            field_value = paragraph["fields"][field_name]
            value_lines = field_value.split("\n")
            lines.append(f"{field_name}: {value_lines[0]}".rstrip())
            for continuation in value_lines[1:]:
                lines.append(f" {continuation}".rstrip())
        rendered.append("\n".join(lines))
    return "\n\n".join(rendered) + "\n"


def _find_control_package_paragraph(paragraphs: list[dict[str, Any]], package_name: str) -> dict[str, Any] | None:
    for paragraph in paragraphs:
        if paragraph["fields"].get("Package") == package_name:
            return paragraph
    return None


def _set_control_field(paragraph: dict[str, Any], field_name: str, value: str) -> None:
    if field_name not in paragraph["fields"]:
        paragraph["order"].append(field_name)
    paragraph["fields"][field_name] = value


def _remove_control_field(paragraph: dict[str, Any], field_name: str) -> None:
    if field_name not in paragraph["fields"]:
        return
    del paragraph["fields"][field_name]
    paragraph["order"] = [name for name in paragraph["order"] if name != field_name]


def _copy_binary_packaging_support_files(debian_dir: Path, from_package: str, to_package: str) -> None:
    for suffix in ("install", "dirs", "links", "postinst", "preinst", "prerm", "postrm", "maintscript"):
        source_path = debian_dir / f"{from_package}.{suffix}"
        target_path = debian_dir / f"{to_package}.{suffix}"
        if source_path.is_file() and not target_path.exists():
            shutil.copy2(source_path, target_path)


def _ensure_udeb_library_rules(rules_path: Path, library_shlibs_udeb: str) -> None:
    if not rules_path.is_file():
        raise RuntimeError(f"debian/rules is missing for library udeb rebuild: {rules_path}")
    rules_text = rules_path.read_text(encoding="utf-8")
    if f"--add-udeb={library_shlibs_udeb}" in rules_text:
        return
    if re.search(r"^override_dh_makeshlibs:\s*$", rules_text, flags=re.MULTILINE):
        raise RuntimeError(
            f"debian/rules already defines override_dh_makeshlibs; provide a source_overlay_dir that adds --add-udeb={library_shlibs_udeb} explicitly"
        )
    rules_stat = rules_path.stat()
    rules_text = rules_text.rstrip() + f"\n\noverride_dh_makeshlibs:\n\tdh_makeshlibs --add-udeb={library_shlibs_udeb}\n"
    rules_path.write_text(rules_text, encoding="utf-8")
    rules_path.chmod(rules_stat.st_mode)


def _collect_built_udebs(build_parent: Path, udeb_package: str) -> list[Path]:
    matches: list[Path] = []
    for candidate in sorted(build_parent.glob("*.udeb")):
        if _dpkg_deb_field(candidate, "Package") == udeb_package:
            matches.append(candidate)
    if not matches:
        raise RuntimeError(f"dpkg-buildpackage did not produce a .udeb for package {udeb_package} under {build_parent}")
    return matches


def _dpkg_deb_field(package_path: Path, field_name: str) -> str:
    result = subprocess.run(
        ["dpkg-deb", "-f", str(package_path), field_name],
        check=True,
        text=True,
        encoding="utf-8",
        capture_output=True,
    )
    return result.stdout.strip()


def _create_local_udeb_repo(repo_dir: Path, log_file: Any) -> None:
    packages_text = _run_logged_capture(["dpkg-scanpackages", "-t", "udeb", ".", "/dev/null"], cwd=repo_dir, log_file=log_file)
    (repo_dir / "Packages").write_text(packages_text, encoding="utf-8")
    with gzip.open(repo_dir / "Packages.gz", "wt", encoding="utf-8") as handle:
        handle.write(packages_text)


def _locate_built_iso(build_root: Path) -> Path:
    candidates = sorted(build_root.glob("*.hybrid.iso")) + sorted(build_root.glob("*.iso"))
    if not candidates:
        raise RuntimeError(f"could not locate a built ISO under {build_root}")
    return candidates[0]


def _canonical_module_name(value: str) -> str:
    return str(value).strip().replace("-", "_")


def _module_name_lookup(values: set[str]) -> dict[str, str]:
    lookup: dict[str, str] = {}
    for value in sorted(values):
        lookup.setdefault(_canonical_module_name(value), value)
    return lookup


def _scan_module_tree(chroot_root: Path, requested_modules: set[str], alias_candidates: set[str]) -> dict[str, Any]:
    module_root = chroot_root / "lib" / "modules"
    module_files = sorted(module_root.glob("**/*.ko*")) if module_root.is_dir() else []
    requested_lookup = _module_name_lookup(requested_modules)
    alias_lookup = _module_name_lookup(alias_candidates)
    discovered: list[str] = []
    requested_matches: list[str] = []
    alias_matches: list[str] = []
    builtin_modules: set[str] = set()

    for builtin_path in sorted(module_root.glob("*/modules.builtin")) if module_root.is_dir() else []:
        for raw_line in builtin_path.read_text(encoding="utf-8", errors="replace").splitlines():
            module_path = raw_line.strip()
            if module_path:
                builtin_modules.add(_canonical_module_name(_module_name_from_path(Path(module_path))))

    for module_file in module_files:
        name = _module_name_from_path(module_file)
        canonical_name = _canonical_module_name(name)
        discovered.append(name)
        requested_name = requested_lookup.get(canonical_name)
        if requested_name is not None and requested_name not in requested_matches:
            requested_matches.append(requested_name)
        alias_name = alias_lookup.get(canonical_name)
        if alias_name is not None and alias_name not in alias_matches:
            alias_matches.append(alias_name)

    requested_builtin = sorted(
        requested_name
        for canonical_name, requested_name in requested_lookup.items()
        if canonical_name in builtin_modules
    )
    alias_builtin = sorted(
        alias_name
        for canonical_name, alias_name in alias_lookup.items()
        if canonical_name in builtin_modules
    )
    resolved_requested = set(requested_matches).union(requested_builtin)
    return {
        "module_root": str(module_root),
        "requested_matches": requested_matches,
        "requested_builtin": requested_builtin,
        "requested_missing": sorted(requested_modules.difference(resolved_requested)),
        "alias_matches": alias_matches,
        "alias_builtin": alias_builtin,
        "discovered_count": len(discovered),
    }


def _resolved_module_names(resolved_modules: dict[str, Any]) -> set[str]:
    resolved: set[str] = set()
    for key in ("requested_matches", "requested_builtin", "alias_matches", "alias_builtin"):
        resolved.update(_canonical_module_name(str(module)) for module in resolved_modules.get(key, []))
    return resolved


def _has_xxhash_module(resolved_modules: dict[str, Any]) -> bool:
    return any("xxhash" in module for module in _resolved_module_names(resolved_modules))


def _required_live_module_is_resolved(module: str, resolved_names: set[str]) -> bool:
    canonical = _canonical_module_name(module)
    if canonical in resolved_names:
        return True
    if canonical == "xxhash":
        return any("xxhash" in name for name in resolved_names)
    if canonical == "xxhash_generic":
        return any("xxhash" in name and "generic" in name for name in resolved_names)
    return False


def _enforce_debian_live_module_contract(resolved_modules: dict[str, Any]) -> None:
    resolved_names = _resolved_module_names(resolved_modules)
    missing = [
        module
        for module in DEBIAN_LIVE_INITRAMFS_MODULES
        if not _required_live_module_is_resolved(module, resolved_names)
    ]
    if missing:
        module_root = resolved_modules.get("module_root") or "<unknown>"
        raise RuntimeError(
            "Debian Live kernel module contract is incomplete under "
            f"{module_root}; missing: {', '.join(missing)}"
        )


def _audit_erofs_installer_support(plan: dict[str, Any], local_udeb_dirs: list[Path]) -> dict[str, Any]:
    local_udeb_dir = local_udeb_dirs[0] if local_udeb_dirs else None
    installer_include_dir = Path(plan["installer_include_dir"]) if plan["installer_include_dir"] else None

    udeb_files: list[str] = []
    for udeb_dir in local_udeb_dirs:
        udeb_files.extend(sorted(path.name for path in udeb_dir.glob("*.udeb")))
    installer_overlay_files = _relative_file_inventory(installer_include_dir) if installer_include_dir is not None else []

    component_hits: dict[str, list[str]] = {}
    named_component_misses: list[str] = []
    searchable_artifacts = [path.lower() for path in udeb_files] + [path.lower() for path in installer_overlay_files]
    for component in plan["erofs_installer_components"]:
        patterns = EROFS_INSTALLER_COMPONENT_PATH_HINTS.get(component, (component,))
        matches = [artifact for artifact in searchable_artifacts if any(pattern in artifact for pattern in patterns)]
        component_hits[component] = matches
        if not matches:
            named_component_misses.append(component)

    warnings: list[str] = []
    errors: list[str] = []
    if plan["erofs_installer_component_policy"] == "require":
        if local_udeb_dir is None:
            errors.append("Provide local_udeb_dir with installer module/component udebs for required EROFS installer integration.")
        elif not udeb_files:
            errors.append(f"No .udeb files were found under local_udeb_dir: {local_udeb_dir}")
        if installer_include_dir is None:
            errors.append("Provide installer_include_dir with explicit config/includes.installer content for required EROFS installer integration.")
        elif not installer_overlay_files:
            errors.append(f"No files were found under installer_include_dir: {installer_include_dir}")
    else:
        if local_udeb_dir is None:
            warnings.append("No local .udeb directory was provided for installer-side EROFS component integration.")
        elif not udeb_files:
            warnings.append(f"No .udeb files were found under local_udeb_dir: {local_udeb_dir}")
        if installer_include_dir is None:
            warnings.append("No config/includes.installer overlay directory was provided for installer-side EROFS integration.")
        elif not installer_overlay_files:
            warnings.append(f"No files were found under installer_include_dir: {installer_include_dir}")

    if plan["kernel_mode"] != "stock-debian" and not udeb_files:
        warnings.append("Custom live kernel packages do not automatically replace the separate Debian Installer kernel/initrd path; no local .udeb evidence was staged.")
    if named_component_misses:
        warnings.append(
            "Could not confirm component-specific installer evidence from filenames for: " + ", ".join(named_component_misses)
        )

    return {
        "applicable": True,
        "policy": plan["erofs_installer_component_policy"],
        "selected_components": list(plan["erofs_installer_components"]),
        "local_udeb_dir": str(local_udeb_dir) if local_udeb_dir is not None else "",
        "local_udeb_dirs": [str(path) for path in local_udeb_dirs],
        "local_udeb_count": len(udeb_files),
        "installer_include_dir": str(installer_include_dir) if installer_include_dir is not None else "",
        "installer_overlay_file_count": len(installer_overlay_files),
        "named_component_hits": component_hits,
        "named_component_misses": named_component_misses,
        "errors": errors,
        "warnings": warnings,
        "status": "error" if errors else "ok",
    }


def _relative_file_inventory(root: Path) -> list[str]:
    return sorted(str(path.relative_to(root)) for path in root.rglob("*") if path.is_file())


def _module_name_from_path(path: Path) -> str:
    name = path.name
    for suffix in (".zst", ".xz", ".gz"):
        if name.endswith(suffix):
            name = name[: -len(suffix)]
    if name.endswith(".ko"):
        name = name[:-3]
    return name


def _validate_build_iso_plan(plan: Any) -> dict[str, Any]:
    if not isinstance(plan, dict):
        raise ValueError("build ISO plan must be a JSON object")
    schema_version = int(plan.get("schema_version") or 0)
    if schema_version != SCHEMA_VERSION:
        raise ValueError(f"build ISO plan schema_version must be {SCHEMA_VERSION}")
    distro = str(plan.get("distro") or "").strip()
    if distro not in SUPPORTED_BUILD_DISTROS:
        raise ValueError(f"unsupported Build ISO distro: {distro}")

    normalized: dict[str, Any] = {
        "schema_version": schema_version,
        "distro": distro,
        "output_dir": _validate_absolute_path(plan.get("output_dir"), allow_missing=True, expect_directory=True, label="output_dir"),
        "image_name": _validate_image_name(plan.get("image_name")),
        "suite": _validate_non_empty_token(plan.get("suite"), "suite"),
        "architecture": _validate_non_empty_token(plan.get("architecture"), "architecture"),
        "archive_areas": _validate_string_list(plan.get("archive_areas"), "archive_areas"),
        "mirror_bootstrap": _validate_optional_apt_mirror_url(plan.get("mirror_bootstrap"), "mirror_bootstrap"),
        "mirror_chroot": _validate_optional_apt_mirror_url(plan.get("mirror_chroot"), "mirror_chroot"),
        "mirror_binary": _validate_optional_apt_mirror_url(plan.get("mirror_binary"), "mirror_binary"),
        "mirror_binary_security": _validate_optional_apt_mirror_url(plan.get("mirror_binary_security"), "mirror_binary_security"),
        "mirror_debian_installer": _validate_optional_apt_mirror_url(plan.get("mirror_debian_installer"), "mirror_debian_installer"),
        "keyring_packages": _validate_package_list(plan.get("keyring_packages"), "keyring_packages"),
        "installer_mode": _validate_choice(plan.get("installer_mode"), INSTALLER_MODES, "installer_mode"),
        "include_installer_launcher": bool(plan.get("include_installer_launcher")),
        "include_non_free_firmware": bool(plan.get("include_non_free_firmware")),
        "base_packages": _validate_package_list(plan.get("base_packages"), "base_packages"),
        "extra_chroot_packages": _validate_package_list(plan.get("extra_chroot_packages"), "extra_chroot_packages"),
        "extra_binary_packages": _validate_package_list(plan.get("extra_binary_packages"), "extra_binary_packages"),
        "live_module_spec_path": _validate_absolute_path(plan.get("live_module_spec_path"), allow_missing=False, expect_directory=False, label="live_module_spec_path", optional=True),
        "live_deb_spec_path": _validate_absolute_path(plan.get("live_deb_spec_path"), allow_missing=False, expect_directory=False, label="live_deb_spec_path", optional=True),
        "live_udeb_spec_path": _validate_absolute_path(plan.get("live_udeb_spec_path"), allow_missing=False, expect_directory=False, label="live_udeb_spec_path", optional=True),
        "di_module_spec_path": _validate_absolute_path(plan.get("di_module_spec_path"), allow_missing=False, expect_directory=False, label="di_module_spec_path", optional=True),
        "di_deb_spec_path": _validate_absolute_path(plan.get("di_deb_spec_path"), allow_missing=False, expect_directory=False, label="di_deb_spec_path", optional=True),
        "di_udeb_spec_path": _validate_absolute_path(plan.get("di_udeb_spec_path"), allow_missing=False, expect_directory=False, label="di_udeb_spec_path", optional=True),
        "local_deb_dir": _validate_absolute_path(plan.get("local_deb_dir"), allow_missing=False, expect_directory=True, label="local_deb_dir", optional=True),
        "local_udeb_dir": _validate_absolute_path(plan.get("local_udeb_dir"), allow_missing=False, expect_directory=True, label="local_udeb_dir", optional=True),
        "installer_kernel_rebuild_enabled": bool(plan.get("installer_kernel_rebuild_enabled")),
        "installer_kernel_source_iso_path": _validate_absolute_path(
            plan.get("installer_kernel_source_iso_path"),
            allow_missing=False,
            expect_directory=False,
            label="installer_kernel_source_iso_path",
            optional=True,
        ),
        "installer_kernel_modules": _validate_optional_module_list(plan.get("installer_kernel_modules"), "installer_kernel_modules"),
        "installer_kernel_config_entries": _validate_kernel_config_entry_list(plan.get("installer_kernel_config_entries"), "installer_kernel_config_entries"),
        "installer_auto_udeb_packages": _validate_package_list(plan.get("installer_auto_udeb_packages"), "installer_auto_udeb_packages"),
        "udeb_rebuild_spec_path": _validate_absolute_path(plan.get("udeb_rebuild_spec_path"), allow_missing=False, expect_directory=False, label="udeb_rebuild_spec_path", optional=True),
        "udeb_rebuild_source_overlay_dir": _validate_absolute_path(
            plan.get("udeb_rebuild_source_overlay_dir"),
            allow_missing=False,
            expect_directory=True,
            label="udeb_rebuild_source_overlay_dir",
            optional=True,
        ),
        "preseed_path": _validate_absolute_path(plan.get("preseed_path"), allow_missing=False, expect_directory=False, label="preseed_path", optional=True),
        "installer_include_dir": _validate_absolute_path(plan.get("installer_include_dir"), allow_missing=False, expect_directory=True, label="installer_include_dir", optional=True),
        "live_include_dir": _validate_absolute_path(plan.get("live_include_dir"), allow_missing=False, expect_directory=True, label="live_include_dir", optional=True),
        "binary_include_dir": _validate_absolute_path(plan.get("binary_include_dir"), allow_missing=False, expect_directory=True, label="binary_include_dir", optional=True),
        "bootloader_override_dir": _validate_absolute_path(plan.get("bootloader_override_dir"), allow_missing=False, expect_directory=True, label="bootloader_override_dir", optional=True),
        "installer_distribution": str(plan.get("installer_distribution") or "").strip(),
        "installer_boot_append": str(plan.get("installer_boot_append") or "").strip(),
        "bootappend_live": str(plan.get("bootappend_live") or "").strip(),
        "iso_application": str(plan.get("iso_application") or "").strip(),
        "iso_preparer": str(plan.get("iso_preparer") or "").strip(),
        "iso_publisher": str(plan.get("iso_publisher") or "").strip(),
        "iso_volume": str(plan.get("iso_volume") or "").strip(),
        "erofs_installer_component_policy": _validate_choice(
            plan.get("erofs_installer_component_policy") or "warn",
            EROFS_INSTALLER_COMPONENT_POLICIES,
            "erofs_installer_component_policy",
        ),
        "erofs_installer_components": _validate_optional_erofs_installer_components(
            plan.get("erofs_installer_components"), "erofs_installer_components"
        ),
        "direct_di_build_enabled": bool(plan.get("direct_di_build_enabled")),
        "direct_di_build_source_mode": str(plan.get("direct_di_build_source_mode") or "").strip(),
        "direct_di_build_source_tree": _validate_absolute_path(
            plan.get("direct_di_build_source_tree"), allow_missing=False, expect_directory=True, label="direct_di_build_source_tree", optional=True
        ),
        "direct_di_build_source_package": str(plan.get("direct_di_build_source_package") or "").strip(),
        "direct_di_build_targets": _validate_optional_string_list(plan.get("direct_di_build_targets"), "direct_di_build_targets"),
        "direct_di_build_dep_packages": _validate_package_list(plan.get("direct_di_build_dep_packages"), "direct_di_build_dep_packages"),
        "direct_di_build_reallyclean_before": bool(plan.get("direct_di_build_reallyclean_before")),
        "direct_di_build_reallyclean_after": bool(plan.get("direct_di_build_reallyclean_after")),
        "kernel_mode": _validate_choice(plan.get("kernel_mode"), KERNEL_MODES, "kernel_mode"),
        "kernel_package_stub": str(plan.get("kernel_package_stub") or "").strip(),
        "kernel_flavours": _validate_optional_string_list(plan.get("kernel_flavours"), "kernel_flavours"),
        "kernel_deb_dir": _validate_absolute_path(plan.get("kernel_deb_dir"), allow_missing=False, expect_directory=True, label="kernel_deb_dir", optional=True),
        "custom_apt_repo": _validate_optional_apt_source_line(plan.get("custom_apt_repo"), "custom_apt_repo"),
        "custom_binary_apt_repo": _validate_optional_apt_source_line(plan.get("custom_binary_apt_repo"), "custom_binary_apt_repo"),
        "custom_apt_repo_key_path": _validate_absolute_path(plan.get("custom_apt_repo_key_path"), allow_missing=False, expect_directory=False, label="custom_apt_repo_key_path", optional=True),
        "custom_apt_repo_pin": str(plan.get("custom_apt_repo_pin") or "").strip(),
        "rootfs_format": _validate_choice(plan.get("rootfs_format"), ROOTFS_FORMATS, "rootfs_format"),
        "erofs_compressor": str(plan.get("erofs_compressor") or "").strip() or "zstd",
        "erofs_extra_args": str(plan.get("erofs_extra_args") or "").strip(),
        "filesystem_module_entries": _validate_optional_live_image_entries(plan.get("filesystem_module_entries"), "filesystem_module_entries"),
        "initramfs_modules": _validate_module_list(plan.get("initramfs_modules"), "initramfs_modules"),
        "kernel_inspection_modules": _validate_optional_module_list(plan.get("kernel_inspection_modules"), "kernel_inspection_modules"),
        "kernel_target_version": str(plan.get("kernel_target_version") or "").strip(),
        "kernel_module_tree_dir": _validate_absolute_path(
            plan.get("kernel_module_tree_dir"),
            allow_missing=False,
            expect_directory=True,
            label="kernel_module_tree_dir",
            optional=True,
        ),
        "kernel_download_if_missing": bool(plan.get("kernel_download_if_missing")),
        "kernel_config_symbols": _validate_kernel_config_symbol_list(plan.get("kernel_config_symbols"), "kernel_config_symbols"),
        "module_alias_candidates": _validate_optional_module_list(plan.get("module_alias_candidates"), "module_alias_candidates"),
        "live_tool_groups": (
            None
            if plan.get("live_tool_groups") is None
            else _validate_string_list(plan.get("live_tool_groups"), "live_tool_groups")
        ),
        "storage_tool_packages": _validate_package_list(plan.get("storage_tool_packages"), "storage_tool_packages"),
        "cleanup_mode": _validate_choice(plan.get("cleanup_mode"), CLEANUP_MODES, "cleanup_mode"),
    }
    normalized["live_tool_profile"] = {}
    if normalized["installer_mode"] != "netinst":
        live_tool_packages, live_tool_profile = live_tool_packages_for_build_distro(
            normalized["distro"],
            selected_groups=normalized["live_tool_groups"],
        )
        normalized["live_tool_groups"] = list(live_tool_profile["selected_groups"])
        normalized["storage_tool_packages"] = _append_unique(
            normalized["storage_tool_packages"],
            live_tool_packages,
        )
        if normalized["distro"] == DISTRO_DEBIAN:
            normalized["base_packages"] = _append_unique(
                normalized["base_packages"],
                list(DEBIAN_LIVE_HOOK_PACKAGES),
            )
            normalized["initramfs_modules"] = _append_unique(
                normalized["initramfs_modules"],
                list(DEBIAN_LIVE_INITRAMFS_MODULES),
            )
            normalized["kernel_inspection_modules"] = _append_unique(
                normalized["kernel_inspection_modules"],
                list(DEBIAN_LIVE_INITRAMFS_MODULES),
            )
            normalized["kernel_config_symbols"] = _append_unique(
                normalized["kernel_config_symbols"],
                list(DEBIAN_LIVE_KERNEL_CONFIG_SYMBOLS),
            )
            normalized["module_alias_candidates"] = _append_unique(
                normalized["module_alias_candidates"],
                list(DEBIAN_LIVE_MODULE_ALIAS_CANDIDATES),
            )
            normalized["bootappend_live"] = _merge_required_kernel_args(
                normalized["bootappend_live"],
                DEBIAN_LIVE_HOOK_KERNEL_ARGS,
            )
        if live_tool_packages:
            normalized["live_tool_profile"] = {
                "path": live_tool_profile["path"],
                "sha256": live_tool_profile["sha256"],
                "name": live_tool_profile["name"],
                "selected_groups": list(live_tool_profile["selected_groups"]),
                "package_count": len(live_tool_packages),
            }
    else:
        if normalized["live_tool_groups"]:
            raise ValueError("live_tool_groups is forbidden when installer_mode is netinst")
        normalized["live_tool_groups"] = []
    normalized["applied_feature_specs"] = _load_feature_specs_for_plan(normalized)
    for spec in normalized["applied_feature_specs"]:
        _merge_feature_spec_into_plan(normalized, spec)
    normalized["udeb_rebuilds"] = _load_and_validate_udeb_rebuild_spec(normalized["udeb_rebuild_spec_path"])
    if normalized["udeb_rebuild_source_overlay_dir"]:
        if not normalized["udeb_rebuilds"]:
            raise ValueError("udeb_rebuild_source_overlay_dir requires udeb_rebuild_spec_path or a selected spec profile that resolves one")
        _apply_udeb_source_overlay_override(normalized["udeb_rebuilds"], normalized["udeb_rebuild_source_overlay_dir"])

    if not normalized["archive_areas"]:
        raise ValueError("archive_areas must not be empty")
    if normalized["distro"] != DISTRO_DEBIAN:
        for field_name in ("mirror_bootstrap", "mirror_chroot", "mirror_binary"):
            if not normalized[field_name]:
                raise ValueError(f"{field_name} is required for non-Debian Build ISO distros")
        if not normalized["keyring_packages"]:
            raise ValueError("keyring_packages is required for non-Debian Build ISO distros")
    if not normalized["installer_distribution"] and (
        normalized["installer_mode"] == "netinst"
        or (normalized["distro"] == DISTRO_DEBIAN and normalized["installer_mode"] == "live")
    ):
        normalized["installer_distribution"] = normalized["suite"]
    if normalized["installer_mode"] == "none":
        if normalized["include_installer_launcher"]:
            raise ValueError("include_installer_launcher requires installer_mode other than none")
        for field_name in (
            "di_module_spec_path",
            "di_deb_spec_path",
            "di_udeb_spec_path",
            "local_udeb_dir",
            "installer_kernel_source_iso_path",
            "installer_auto_udeb_packages",
            "udeb_rebuild_spec_path",
            "udeb_rebuild_source_overlay_dir",
            "preseed_path",
            "installer_include_dir",
            "installer_distribution",
            "installer_boot_append",
            "erofs_installer_components",
            "direct_di_build_source_tree",
            "direct_di_build_source_package",
            "direct_di_build_targets",
        ):
            if normalized[field_name]:
                raise ValueError(f"{field_name} requires installer_mode other than none")
        if normalized["installer_kernel_rebuild_enabled"]:
            raise ValueError("installer_kernel_rebuild_enabled requires installer_mode other than none")
        if normalized["direct_di_build_enabled"]:
            raise ValueError("direct_di_build_enabled requires installer_mode other than none")
    if normalized["installer_mode"] == "netinst":
        if normalized["rootfs_format"] != "none":
            raise ValueError("netinst installer_mode requires rootfs_format=none")
        forbidden_live_fields = (
            "base_packages",
            "extra_chroot_packages",
            "live_module_spec_path",
            "live_deb_spec_path",
            "live_udeb_spec_path",
            "local_deb_dir",
            "live_include_dir",
            "bootappend_live",
            "filesystem_module_entries",
            "initramfs_modules",
            "live_tool_groups",
            "storage_tool_packages",
        )
        for field_name in forbidden_live_fields:
            if normalized[field_name]:
                raise ValueError(f"{field_name} is forbidden when installer_mode is netinst")
        if normalized["include_installer_launcher"]:
            raise ValueError("include_installer_launcher is forbidden when installer_mode is netinst")
        if normalized["kernel_mode"] != "stock-debian":
            raise ValueError("kernel_mode must be stock-debian when installer_mode is netinst")
    elif normalized["rootfs_format"] == "none":
        raise ValueError("rootfs_format=none is valid only when installer_mode is netinst")
    if normalized["installer_kernel_rebuild_enabled"]:
        if not normalized["installer_kernel_source_iso_path"]:
            raise ValueError("installer_kernel_source_iso_path is required when installer_kernel_rebuild_enabled is true")
        if not normalized["installer_kernel_modules"] and not normalized["installer_kernel_config_entries"]:
            raise ValueError("installer_kernel_modules or installer_kernel_config_entries is required when installer_kernel_rebuild_enabled is true")
    if normalized["custom_binary_apt_repo"] and not normalized["custom_apt_repo"]:
        raise ValueError("custom_binary_apt_repo requires custom_apt_repo")
    if normalized["include_non_free_firmware"] and "non-free-firmware" not in normalized["archive_areas"]:
        normalized["archive_areas"].append("non-free-firmware")
    if normalized["kernel_mode"] != "stock-debian":
        if not normalized["kernel_package_stub"]:
            raise ValueError("kernel_package_stub is required when kernel_mode is not stock-debian")
        if not PACKAGE_RE.match(normalized["kernel_package_stub"]):
            raise ValueError(f"invalid kernel_package_stub: {normalized['kernel_package_stub']}")
        if not normalized["kernel_flavours"]:
            raise ValueError("kernel_flavours is required when kernel_mode is not stock-debian")
    if normalized["kernel_mode"] == "local-kernel-deb-dir" and not normalized["kernel_deb_dir"]:
        raise ValueError("kernel_deb_dir is required for local-kernel-deb-dir mode")
    if normalized["kernel_mode"] == "custom-apt-repo" and not normalized["custom_apt_repo"]:
        raise ValueError("custom_apt_repo is required for custom-apt-repo mode")
    if normalized["direct_di_build_enabled"]:
        if normalized["direct_di_build_source_mode"] not in DIRECT_DI_BUILD_SOURCE_MODES:
            raise ValueError("direct_di_build_source_mode must be apt-source or local-tree when direct_di_build_enabled is true")
        if not normalized["direct_di_build_targets"]:
            raise ValueError("direct_di_build_targets must not be empty when direct_di_build_enabled is true")
        if normalized["direct_di_build_source_mode"] == "local-tree":
            if not normalized["direct_di_build_source_tree"]:
                raise ValueError("direct_di_build_source_tree is required when direct_di_build_source_mode is local-tree")
            normalized["direct_di_build_source_package"] = ""
        else:
            normalized["direct_di_build_source_package"] = normalized["direct_di_build_source_package"] or "debian-installer"
            if not PACKAGE_RE.match(normalized["direct_di_build_source_package"]):
                raise ValueError(f"invalid direct_di_build_source_package: {normalized['direct_di_build_source_package']}")
            normalized["direct_di_build_source_tree"] = ""
    else:
        normalized["direct_di_build_source_mode"] = ""
        normalized["direct_di_build_source_tree"] = ""
        normalized["direct_di_build_source_package"] = ""
        normalized["direct_di_build_targets"] = []
        normalized["direct_di_build_dep_packages"] = []
        normalized["direct_di_build_reallyclean_before"] = False
        normalized["direct_di_build_reallyclean_after"] = False
    if normalized["rootfs_format"] == "erofs":
        if not normalized["initramfs_modules"]:
            raise ValueError("initramfs_modules must include erofs and xxhash entries when rootfs_format is erofs")
        if "erofs" not in normalized["initramfs_modules"]:
            raise ValueError("initramfs_modules must include erofs when rootfs_format is erofs")
        if not any("xxhash" in module for module in normalized["initramfs_modules"]):
            raise ValueError("initramfs_modules must include at least one xxhash module when rootfs_format is erofs")
        if not normalized["filesystem_module_entries"]:
            normalized["filesystem_module_entries"] = list(DEFAULT_EROFS_FILESYSTEM_MODULE_ENTRIES)
        if normalized["installer_mode"] != "none" and not normalized["erofs_installer_components"]:
            normalized["erofs_installer_components"] = list(DEFAULT_EROFS_INSTALLER_COMPONENTS)
        if normalized["installer_mode"] != "none" and normalized["erofs_installer_component_policy"] == "require":
            if not normalized["local_udeb_dir"] and not normalized["udeb_rebuilds"]:
                if not normalized["installer_kernel_rebuild_enabled"] and not normalized["installer_auto_udeb_packages"]:
                    raise ValueError(
                        "local_udeb_dir, udeb_rebuild_spec_path, installer_kernel_rebuild_enabled, or installer_auto_udeb_packages is required when erofs_installer_component_policy is require"
                    )
            if not normalized["installer_include_dir"]:
                raise ValueError("installer_include_dir is required when erofs_installer_component_policy is require")
    else:
        normalized["erofs_installer_components"] = []
    return normalized


def _load_json_file(path: Path) -> Any:
    if not path.is_file():
        raise ValueError(f"build ISO plan path is not a regular file: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def _validate_image_name(value: Any) -> str:
    image_name = str(value or "").strip()
    if not image_name:
        raise ValueError("image_name is required")
    if "/" in image_name or image_name in {".", ".."}:
        raise ValueError(f"invalid image_name: {image_name}")
    if not image_name.lower().endswith(".iso"):
        raise ValueError(f"image_name must end with .iso: {image_name}")
    return image_name


def _validate_non_empty_token(value: Any, label: str) -> str:
    token = str(value or "").strip()
    if not token:
        raise ValueError(f"{label} is required")
    return token


def _validate_choice(value: Any, choices: set[str], label: str) -> str:
    token = str(value or "").strip()
    if token not in choices:
        raise ValueError(f"invalid {label}: {token}")
    return token


def _validate_string_list(value: Any, label: str) -> list[str]:
    if not isinstance(value, list):
        raise ValueError(f"{label} must be a JSON array")
    result = []
    for item in value:
        token = str(item or "").strip()
        if not token:
            continue
        result.append(token)
    return result


def _validate_optional_string_list(value: Any, label: str) -> list[str]:
    if value in (None, ""):
        return []
    return _validate_string_list(value, label)


def _validate_package_list(value: Any, label: str) -> list[str]:
    packages = _validate_optional_string_list(value, label)
    for package in packages:
        if not PACKAGE_RE.match(package):
            raise ValueError(f"invalid package name in {label}: {package}")
    return packages


def _validate_module_list(value: Any, label: str) -> list[str]:
    modules = _validate_optional_string_list(value, label)
    for module in modules:
        if not MODULE_RE.match(module):
            raise ValueError(f"invalid module name in {label}: {module}")
    return modules


def _validate_optional_module_list(value: Any, label: str) -> list[str]:
    if value in (None, ""):
        return []
    return _validate_module_list(value, label)


def _validate_kernel_config_symbol_list(value: Any, label: str) -> list[str]:
    symbols = _validate_optional_string_list(value, label)
    normalized: list[str] = []
    seen: set[str] = set()
    for symbol in symbols:
        if not KERNEL_CONFIG_SYMBOL_RE.match(symbol):
            raise ValueError(f"invalid kernel config symbol in {label}: {symbol}")
        if symbol in seen:
            continue
        seen.add(symbol)
        normalized.append(symbol)
    return normalized


def _validate_kernel_config_entry_list(value: Any, label: str) -> list[str]:
    entries = _validate_optional_string_list(value, label)
    normalized: list[str] = []
    seen: set[str] = set()
    for entry in entries:
        symbol, rendered = _parse_kernel_config_entry(entry)
        if symbol in seen:
            normalized = [item for item in normalized if not _line_matches_kernel_config_symbol(item, symbol)]
        seen.add(symbol)
        normalized.append(rendered)
    return normalized


def _validate_optional_apt_source_line(value: Any, label: str) -> str:
    token = str(value or "").strip()
    if not token:
        return ""
    if "\n" in token:
        raise ValueError(f"{label} must be a single APT source line")
    if not (token.startswith("deb ") or token.startswith("deb [")):
        raise ValueError(f"{label} must start with a deb source entry")
    return token


def _validate_optional_apt_mirror_url(value: Any, label: str) -> str:
    token = str(value or "").strip()
    if not token:
        return ""
    if not (token.startswith("http://") or token.startswith("https://")):
        raise ValueError(f"{label} must be an http:// or https:// URL")
    if any(character.isspace() for character in token):
        raise ValueError(f"{label} must not contain whitespace")
    return token


def _validate_optional_live_image_entries(value: Any, label: str) -> list[str]:
    entries = _validate_optional_string_list(value, label)
    normalized: list[str] = []
    seen: set[str] = set()
    for entry in entries:
        if entry.startswith("/") or not LIVE_IMAGE_ENTRY_RE.match(entry):
            raise ValueError(f"invalid {label} entry: {entry}")
        if ".." in entry.split("/"):
            raise ValueError(f"{label} must not contain parent-directory segments: {entry}")
        if entry in seen:
            continue
        seen.add(entry)
        normalized.append(entry)
    return normalized


def _validate_optional_erofs_installer_components(value: Any, label: str) -> list[str]:
    components = _validate_optional_string_list(value, label)
    normalized: list[str] = []
    seen: set[str] = set()
    for component in components:
        if component not in DEFAULT_EROFS_INSTALLER_COMPONENTS:
            raise ValueError(f"invalid {label} entry: {component}")
        if component in seen:
            continue
        seen.add(component)
        normalized.append(component)
    return normalized


def _load_feature_specs_for_plan(plan: dict[str, Any]) -> list[dict[str, Any]]:
    spec_definitions = [
        ("live_module_spec_path", "live", "modules"),
        ("live_deb_spec_path", "live", "deb"),
        ("live_udeb_spec_path", "live", "udeb"),
        ("di_module_spec_path", "d-i", "modules"),
        ("di_deb_spec_path", "d-i", "deb"),
        ("di_udeb_spec_path", "d-i", "udeb"),
    ]
    loaded: list[dict[str, Any]] = []
    for field_name, expected_stage, expected_kind in spec_definitions:
        spec_path = plan.get(field_name) or ""
        if not spec_path:
            continue
        loaded.append(
            _load_and_validate_feature_spec(
                spec_path=spec_path,
                label=field_name,
                expected_distro=plan["distro"],
                expected_stage=expected_stage,
                expected_kind=expected_kind,
            )
        )
    return loaded


def _load_and_validate_feature_spec(
    *,
    spec_path: str,
    label: str,
    expected_distro: str,
    expected_stage: str,
    expected_kind: str,
) -> dict[str, Any]:
    payload = json.loads(Path(spec_path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{label} must be a JSON object")
    schema_version = int(payload.get("schema_version") or 0)
    if schema_version != FEATURE_SPEC_SCHEMA_VERSION:
        raise ValueError(f"{label} schema_version must be {FEATURE_SPEC_SCHEMA_VERSION}")
    spec_file = Path(spec_path)
    distro = _validate_choice(payload.get("distro"), FEATURE_SPEC_DISTROS, f"{label} distro")
    stage = _validate_choice(payload.get("stage"), FEATURE_SPEC_STAGES, f"{label} stage")
    kind = _validate_choice(payload.get("kind"), FEATURE_SPEC_KINDS, f"{label} kind")
    if distro != expected_distro:
        raise ValueError(f"{label} distro must be {expected_distro}: {distro}")
    if stage != expected_stage:
        raise ValueError(f"{label} stage must be {expected_stage}: {stage}")
    if kind != expected_kind:
        raise ValueError(f"{label} kind must be {expected_kind}: {kind}")
    return {
        "schema_version": schema_version,
        "path": str(spec_file),
        "name": str(payload.get("name") or "").strip() or spec_file.stem,
        "description": str(payload.get("description") or "").strip(),
        "distro": distro,
        "stage": stage,
        "kind": kind,
        "enabled_by_default": bool(payload.get("enabled_by_default")),
        "module_names": _validate_optional_module_list(payload.get("module_names"), f"{label} module_names"),
        "live_initramfs_modules": _validate_optional_module_list(payload.get("live_initramfs_modules"), f"{label} live_initramfs_modules"),
        "module_alias_candidates": _validate_optional_module_list(payload.get("module_alias_candidates"), f"{label} module_alias_candidates"),
        "kernel_config_symbols": _validate_kernel_config_symbol_list(payload.get("kernel_config_symbols"), f"{label} kernel_config_symbols"),
        "extra_chroot_packages": _validate_package_list(payload.get("extra_chroot_packages"), f"{label} extra_chroot_packages"),
        "extra_binary_packages": _validate_package_list(payload.get("extra_binary_packages"), f"{label} extra_binary_packages"),
        "local_deb_dir": _validate_feature_spec_path(payload.get("local_deb_dir"), spec_file=spec_file, expect_directory=True, label=f"{label} local_deb_dir"),
        "local_udeb_dir": _validate_feature_spec_path(payload.get("local_udeb_dir"), spec_file=spec_file, expect_directory=True, label=f"{label} local_udeb_dir"),
        "udeb_rebuild_spec_path": _validate_feature_spec_path(
            payload.get("udeb_rebuild_spec_path"),
            spec_file=spec_file,
            expect_directory=False,
            label=f"{label} udeb_rebuild_spec_path",
        ),
        "source_overlay_dir": _validate_feature_spec_path(
            payload.get("source_overlay_dir"),
            spec_file=spec_file,
            expect_directory=True,
            label=f"{label} source_overlay_dir",
        ),
        "kernel_version": str(payload.get("kernel_version") or "").strip(),
        "notes": _validate_optional_string_list(payload.get("notes"), f"{label} notes"),
    }


def _validate_feature_spec_path(value: Any, *, spec_file: Path, expect_directory: bool, label: str) -> str:
    token = str(value or "").strip()
    if not token:
        return ""
    candidate = Path(token)
    if not candidate.is_absolute():
        candidate = (spec_file.parent / candidate).resolve()
    return _validate_absolute_path(candidate, allow_missing=False, expect_directory=expect_directory, label=label, optional=True)


def _merge_feature_spec_into_plan(plan: dict[str, Any], spec: dict[str, Any]) -> None:
    plan["extra_chroot_packages"] = _append_unique(plan["extra_chroot_packages"], spec["extra_chroot_packages"])
    plan["extra_binary_packages"] = _append_unique(plan["extra_binary_packages"], spec["extra_binary_packages"])
    plan["initramfs_modules"] = _append_unique(plan["initramfs_modules"], spec["live_initramfs_modules"])
    plan["kernel_inspection_modules"] = _append_unique(plan["kernel_inspection_modules"], spec["module_names"])
    plan["module_alias_candidates"] = _append_unique(plan["module_alias_candidates"], spec["module_alias_candidates"])
    plan["kernel_config_symbols"] = _append_unique(plan["kernel_config_symbols"], spec["kernel_config_symbols"])
    if not plan["local_deb_dir"] and spec["local_deb_dir"]:
        plan["local_deb_dir"] = spec["local_deb_dir"]
    if not plan["local_udeb_dir"] and spec["local_udeb_dir"]:
        plan["local_udeb_dir"] = spec["local_udeb_dir"]
    if not plan["udeb_rebuild_spec_path"] and spec["udeb_rebuild_spec_path"]:
        plan["udeb_rebuild_spec_path"] = spec["udeb_rebuild_spec_path"]
    if not plan["udeb_rebuild_source_overlay_dir"] and spec["source_overlay_dir"]:
        plan["udeb_rebuild_source_overlay_dir"] = spec["source_overlay_dir"]
    if not plan["kernel_target_version"] and spec["kernel_version"]:
        plan["kernel_target_version"] = spec["kernel_version"]


def _append_unique(existing: list[str], additional: list[str]) -> list[str]:
    merged = list(existing)
    seen = set(existing)
    for value in additional:
        if value in seen:
            continue
        seen.add(value)
        merged.append(value)
    return merged


def _merge_required_kernel_args(existing: str, required: tuple[str, ...]) -> str:
    tokens = str(existing or "").split()
    separator_index = tokens.index("---") if "---" in tokens else len(tokens)
    prefix = list(tokens[:separator_index])
    suffix = list(tokens[separator_index:])
    for required_arg in required:
        key = required_arg.split("=", 1)[0]
        prefix = [token for token in prefix if token != key and not token.startswith(key + "=")]
        prefix.append(required_arg)
    return " ".join([*prefix, *suffix])


def _load_and_validate_udeb_rebuild_spec(spec_path: str) -> list[dict[str, Any]]:
    if not spec_path:
        return []
    spec_file = Path(spec_path)
    payload = json.loads(spec_file.read_text(encoding="utf-8"))
    if isinstance(payload, list):
        rebuilds = payload
    elif isinstance(payload, dict):
        schema_version = int(payload.get("schema_version") or 0)
        if schema_version != UDEB_REBUILD_SPEC_SCHEMA_VERSION:
            raise ValueError(f"udeb rebuild spec schema_version must be {UDEB_REBUILD_SPEC_SCHEMA_VERSION}")
        rebuilds = payload.get("rebuilds")
    else:
        raise ValueError("udeb rebuild spec must be a JSON object or array")
    if not isinstance(rebuilds, list):
        raise ValueError("udeb rebuild spec rebuilds must be a JSON array")
    normalized: list[dict[str, Any]] = []
    for index, rebuild in enumerate(rebuilds):
        normalized.append(_validate_udeb_rebuild_entry(rebuild, index, spec_file))
    return normalized


def _apply_udeb_source_overlay_override(rebuilds: list[dict[str, Any]], overlay_dir: str) -> None:
    for rebuild in rebuilds:
        if rebuild.get("rebuild_kind") == "linux-installer-kernel":
            rebuild["source_overlay_dir"] = overlay_dir


def _validate_udeb_rebuild_entry(rebuild: Any, index: int, spec_file: Path) -> dict[str, Any]:
    label = f"udeb rebuild entry {index}"
    if not isinstance(rebuild, dict):
        raise ValueError(f"{label} must be a JSON object")

    rebuild_kind = _validate_choice(rebuild.get("rebuild_kind") or "source-package", UDEB_REBUILD_KINDS, f"{label} rebuild_kind")
    if rebuild_kind == "linux-installer-kernel":
        return _validate_kernel_installer_rebuild_entry(rebuild, index, spec_file)
    return _validate_source_package_rebuild_entry(rebuild, index, spec_file)


def _validate_source_package_rebuild_entry(rebuild: dict[str, Any], index: int, spec_file: Path) -> dict[str, Any]:
    label = f"udeb rebuild entry {index}"
    source_package = _validate_non_empty_token(rebuild.get("source_package"), f"{label} source_package")
    binary_package = _validate_non_empty_token(rebuild.get("binary_package"), f"{label} binary_package")
    udeb_package = _validate_non_empty_token(rebuild.get("udeb_package"), f"{label} udeb_package")
    for package_name, package_label in (
        (source_package, f"{label} source_package"),
        (binary_package, f"{label} binary_package"),
        (udeb_package, f"{label} udeb_package"),
    ):
        if not PACKAGE_RE.match(package_name):
            raise ValueError(f"invalid package name in {package_label}: {package_name}")

    package_role = _validate_choice(rebuild.get("package_role") or "generic", UDEB_REBUILD_PACKAGE_ROLES, f"{label} package_role")
    installer_menu_item = str(rebuild.get("installer_menu_item") or "").strip()
    if package_role == "main-menu" and not installer_menu_item:
        raise ValueError(f"{label} installer_menu_item is required when package_role is main-menu")
    source_version = str(rebuild.get("source_version") or "").strip()
    if source_version and any(character.isspace() for character in source_version):
        raise ValueError(f"invalid {label} source_version: {source_version}")

    normalized = {
        "rebuild_kind": "source-package",
        "source_package": source_package,
        "source_version": source_version,
        "binary_package": binary_package,
        "udeb_package": udeb_package,
        "package_role": package_role,
        "installer_menu_item": installer_menu_item,
        "description": str(rebuild.get("description") or "").strip(),
        "long_description": _validate_optional_long_description(rebuild.get("long_description"), f"{label} long_description"),
        "build_dep_packages": _validate_package_list(rebuild.get("build_dep_packages"), f"{label} build_dep_packages"),
        "depends": _validate_relation_list(rebuild.get("depends"), f"{label} depends"),
        "provides": _validate_relation_list(rebuild.get("provides"), f"{label} provides"),
        "conflicts": _validate_relation_list(rebuild.get("conflicts"), f"{label} conflicts"),
        "replaces": _validate_relation_list(rebuild.get("replaces"), f"{label} replaces"),
        "source_overlay_dir": _validate_feature_spec_path(
            rebuild.get("source_overlay_dir"),
            spec_file=spec_file,
            expect_directory=True,
            label=f"{label} source_overlay_dir",
        ),
        "copy_install_manifest_from": str(rebuild.get("copy_install_manifest_from") or "").strip() or binary_package,
        "library_shlibs_udeb": str(rebuild.get("library_shlibs_udeb") or "").strip() or udeb_package,
        "architecture": str(rebuild.get("architecture") or "").strip(),
    }

    for field_name in ("copy_install_manifest_from", "library_shlibs_udeb"):
        value = normalized[field_name]
        if value and not PACKAGE_RE.match(value):
            raise ValueError(f"invalid package name in {label} {field_name}: {value}")
    if normalized["architecture"] and not LIVE_IMAGE_ENTRY_RE.match(normalized["architecture"].replace(" ", "-")):
        raise ValueError(f"invalid architecture override in {label}: {normalized['architecture']}")
    return normalized


def _validate_kernel_installer_rebuild_entry(rebuild: dict[str, Any], index: int, spec_file: Path) -> dict[str, Any]:
    label = f"udeb rebuild entry {index}"
    source_package = str(rebuild.get("source_package") or "").strip() or "linux"
    if not PACKAGE_RE.match(source_package):
        raise ValueError(f"invalid package name in {label} source_package: {source_package}")

    module_targets_value = rebuild.get("module_targets") or []
    if not isinstance(module_targets_value, list):
        raise ValueError(f"{label} module_targets must be a JSON array")
    module_targets = [_validate_kernel_module_target(target, label, target_index) for target_index, target in enumerate(module_targets_value)]

    dpkg_buildpackage_args = _validate_string_list(rebuild.get("dpkg_buildpackage_args") or ["-b", "-uc", "-us"], f"{label} dpkg_buildpackage_args")
    pkg_list_local_entries = _validate_optional_string_list(rebuild.get("pkg_list_local_entries"), f"{label} pkg_list_local_entries")
    kernel_config_entries = _validate_kernel_config_entry_list(rebuild.get("kernel_config_entries"), f"{label} kernel_config_entries")

    package_list_append_text = str(rebuild.get("package_list_append_text") or "").strip()
    if "\x00" in package_list_append_text:
        raise ValueError(f"{label} package_list_append_text contains invalid NUL bytes")
    source_version = str(rebuild.get("source_version") or "").strip()
    if source_version and any(character.isspace() for character in source_version):
        raise ValueError(f"invalid {label} source_version: {source_version}")
    target_architecture = str(rebuild.get("target_architecture") or "").strip()
    if target_architecture and not LIVE_IMAGE_ENTRY_RE.match(target_architecture):
        raise ValueError(f"invalid {label} target_architecture: {target_architecture}")
    if not module_targets and not kernel_config_entries and not package_list_append_text:
        raise ValueError(f"{label} must provide module_targets, kernel_config_entries, or package_list_append_text")

    return {
        "rebuild_kind": "linux-installer-kernel",
        "source_package": source_package,
        "source_version": source_version,
        "source_overlay_dir": _validate_feature_spec_path(
            rebuild.get("source_overlay_dir"),
            spec_file=spec_file,
            expect_directory=True,
            label=f"{label} source_overlay_dir",
        ),
        "build_dep_packages": _validate_package_list(rebuild.get("build_dep_packages"), f"{label} build_dep_packages"),
        "module_targets": module_targets,
        "package_list_append_text": package_list_append_text,
        "pkg_list_local_entries": pkg_list_local_entries,
        "dpkg_buildpackage_args": dpkg_buildpackage_args,
        "kernel_config_entries": kernel_config_entries,
        "target_architecture": target_architecture,
    }


def _validate_kernel_module_target(target: Any, label: str, target_index: int) -> dict[str, Any]:
    if not isinstance(target, dict):
        raise ValueError(f"{label} module_targets[{target_index}] must be a JSON object")
    path = str(target.get("path") or "").strip()
    if not path or path.startswith("/") or ".." in path.split("/"):
        raise ValueError(f"{label} module_targets[{target_index}] path must be a relative path inside the linux source tree")
    modules = _validate_module_list(target.get("modules"), f"{label} module_targets[{target_index}] modules")
    if not modules:
        raise ValueError(f"{label} module_targets[{target_index}] modules must not be empty")
    merge_strategy = _validate_choice(target.get("merge_strategy") or "append-unique", {"append-unique", "replace"}, f"{label} module_targets[{target_index}] merge_strategy")
    return {"path": path, "modules": modules, "merge_strategy": merge_strategy}


def _validate_optional_long_description(value: Any, label: str) -> list[str]:
    if value in (None, ""):
        return []
    if isinstance(value, str):
        lines = [line.strip() for line in value.splitlines() if line.strip()]
    elif isinstance(value, list):
        lines = [str(item).strip() for item in value if str(item).strip()]
    else:
        raise ValueError(f"{label} must be a string or JSON array")
    return lines


def _validate_relation_list(value: Any, label: str) -> list[str]:
    if value in (None, ""):
        return []
    if not isinstance(value, list):
        raise ValueError(f"{label} must be a JSON array")
    normalized: list[str] = []
    for item in value:
        token = str(item or "").strip()
        if not token:
            continue
        if "\n" in token:
            raise ValueError(f"{label} entries must be single-line values")
        normalized.append(token)
    return normalized


def _validate_absolute_path(
    value: Any,
    *,
    allow_missing: bool,
    expect_directory: bool,
    label: str,
    optional: bool = False,
) -> str:
    token = str(value or "").strip()
    if not token:
        if optional:
            return ""
        raise ValueError(f"{label} is required")
    path = Path(token)
    if not path.is_absolute():
        raise ValueError(f"{label} must be an absolute path: {path}")
    if path.exists():
        if expect_directory and not path.is_dir():
            raise ValueError(f"{label} must be a directory: {path}")
        if not expect_directory and path.is_dir():
            raise ValueError(f"{label} must be a file: {path}")
    elif not allow_missing:
        raise ValueError(f"{label} does not exist: {path}")
    return str(path)


def _inspect_kernel_support(
    *,
    kernel_version: str,
    module_names: list[str],
    module_alias_candidates: list[str],
    config_symbols: list[str],
    module_tree_dir: str,
    download_if_missing: bool,
    log_file: Any | None = None,
) -> dict[str, Any]:
    notes: list[str] = []
    searched_module_dirs: list[str] = []
    searched_config_paths: list[str] = []
    downloaded_packages: list[str] = []
    download_attempted = False
    download_used = False
    cache_root = DEFAULT_CACHE_DIR / "kernel-support" / (kernel_version or "unspecified")
    module_search_root = _first_existing_directory(
        _candidate_module_dirs(kernel_version=kernel_version, module_tree_dir=module_tree_dir),
        searched_module_dirs,
    )
    config_path = _resolve_kernel_config_path(
        kernel_version=kernel_version,
        module_search_root=module_search_root,
        cache_root=cache_root,
        searched_paths=searched_config_paths,
    )
    modules: list[dict[str, Any]] = []
    for module_name in module_names:
        module_path = _resolve_kernel_module_path(module_search_root, module_name, module_alias_candidates)
        modules.append(
            {
                "name": module_name,
                "found": module_path is not None,
                "path": str(module_path) if module_path is not None else "",
            }
        )
    if download_if_missing and kernel_version and _kernel_support_download_needed(
        module_search_root=module_search_root,
        config_path=config_path,
        modules=modules,
        config_symbols=config_symbols,
    ):
        download_attempted = True
        downloaded_packages = _populate_kernel_support_cache(kernel_version, cache_root, log_file=log_file, notes=notes)
        cached_root = _first_existing_directory(
            _candidate_cached_module_dirs(kernel_version=kernel_version, cache_root=cache_root),
            searched_module_dirs,
        )
        if cached_root is not None:
            module_search_root = cached_root
            download_used = True
            config_path = _resolve_kernel_config_path(
                kernel_version=kernel_version,
                module_search_root=module_search_root,
                cache_root=cache_root,
                searched_paths=searched_config_paths,
            )
            modules = []
            for module_name in module_names:
                module_path = _resolve_kernel_module_path(module_search_root, module_name, module_alias_candidates)
                modules.append(
                    {
                        "name": module_name,
                        "found": module_path is not None,
                        "path": str(module_path) if module_path is not None else "",
                    }
                )
    module_count = 0
    if module_search_root is not None:
        module_count = sum(1 for _ in module_search_root.rglob("*.ko*"))
    config_symbol_rows = _collect_kernel_config_symbols(config_path, config_symbols)
    if not module_search_root:
        notes.append("No matching kernel module tree was found locally or in the managed cache.")
    return {
        "kernel_version": kernel_version,
        "module_tree_dir": str(module_search_root) if module_search_root is not None else "",
        "module_count": module_count,
        "module_tree_searched": searched_module_dirs,
        "config_path": str(config_path) if config_path is not None else "",
        "config_paths_searched": searched_config_paths,
        "download_attempted": download_attempted,
        "download_used": download_used,
        "download_cache_dir": str(cache_root),
        "downloaded_packages": downloaded_packages,
        "modules": modules,
        "config_symbols": config_symbol_rows,
        "notes": notes,
    }


def _kernel_support_download_needed(
    *,
    module_search_root: Path | None,
    config_path: Path | None,
    modules: list[dict[str, Any]],
    config_symbols: list[str],
) -> bool:
    if module_search_root is None:
        return True
    if any(not bool(row.get("found")) for row in modules):
        return True
    if config_symbols and config_path is None:
        return True
    return False


def _candidate_module_dirs(*, kernel_version: str, module_tree_dir: str) -> list[Path]:
    candidates: list[Path] = []
    if module_tree_dir:
        path = Path(module_tree_dir)
        candidates.extend([path, path / "kernel"])
    if kernel_version:
        version_root = Path("/lib/modules") / kernel_version
        candidates.extend([version_root, version_root / "kernel"])
    return candidates


def _candidate_cached_module_dirs(*, kernel_version: str, cache_root: Path) -> list[Path]:
    candidates: list[Path] = []
    root_dir = cache_root / "root"
    for base_dir in ("lib/modules", "usr/lib/modules"):
        version_root = root_dir / base_dir / kernel_version
        candidates.extend([version_root, version_root / "kernel"])
    return candidates


def _first_existing_directory(candidates: list[Path], searched_paths: list[str]) -> Path | None:
    seen: set[str] = set()
    for candidate in candidates:
        candidate_str = str(candidate)
        if candidate_str in seen:
            continue
        seen.add(candidate_str)
        searched_paths.append(candidate_str)
        if candidate.is_dir():
            return candidate
    return None


def _populate_kernel_support_cache(kernel_version: str, cache_root: Path, *, log_file: Any | None, notes: list[str]) -> list[str]:
    cache_root.mkdir(parents=True, exist_ok=True)
    packages_dir = cache_root / "packages"
    root_dir = cache_root / "root"
    packages_dir.mkdir(parents=True, exist_ok=True)
    root_dir.mkdir(parents=True, exist_ok=True)
    package_candidates = [
        f"linux-image-{kernel_version}",
        f"linux-modules-{kernel_version}",
        f"linux-headers-{kernel_version}",
        f"linux-config-{kernel_version}",
        f"linux-image-unsigned-{kernel_version}",
    ]
    downloaded = _download_kernel_support_package_candidates(package_candidates, packages_dir, root_dir, log_file=log_file, notes=notes)
    if not downloaded and os.geteuid() == 0:
        notes.append(
            f"No downloadable package candidates succeeded for kernel version {kernel_version} with the current APT indices; refreshing package lists and retrying."
        )
        updated = _run_optional_command(["apt-get", "update"], cwd=packages_dir, log_file=log_file, allow_failure=True)
        if updated:
            downloaded = _download_kernel_support_package_candidates(
                package_candidates,
                packages_dir,
                root_dir,
                log_file=log_file,
                notes=notes,
            )
        else:
            notes.append("apt-get update failed while refreshing kernel package indices for installer module support.")
    if not downloaded:
        notes.append(
            f"No downloadable package candidates succeeded for kernel version {kernel_version}."
        )
    return downloaded


def _download_kernel_support_package_candidates(
    package_candidates: list[str],
    packages_dir: Path,
    root_dir: Path,
    *,
    log_file: Any | None,
    notes: list[str],
) -> list[str]:
    downloaded: list[str] = []
    for package_name in package_candidates:
        package_path = _download_kernel_package_candidate(package_name, packages_dir, log_file=log_file)
        if package_path is None:
            continue
        downloaded.append(str(package_path))
        try:
            _run_optional_command(["dpkg-deb", "-x", str(package_path), str(root_dir)], cwd=packages_dir, log_file=log_file)
        except RuntimeError as exc:
            notes.append(str(exc))
    return downloaded


def _package_versions_from_apt_cache(package_name: str) -> list[str]:
    versions: list[str] = []
    seen: set[str] = set()
    for command in (["apt-cache", "madison", package_name], ["apt-cache", "show", package_name]):
        process = subprocess.run(command, text=True, encoding="utf-8", capture_output=True, check=False)
        if process.returncode != 0:
            continue
        for raw_line in process.stdout.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            version = ""
            if command[1] == "madison":
                parts = [part.strip() for part in line.split("|")]
                if len(parts) >= 2:
                    version = parts[1]
            elif line.startswith("Version:"):
                version = line.split(":", 1)[1].strip()
            if version and version not in seen:
                seen.add(version)
                versions.append(version)
    return versions


def _download_kernel_package_candidate(package_name: str, packages_dir: Path, *, log_file: Any | None) -> Path | None:
    before = {path.name for path in packages_dir.glob("*.deb")}
    package_specs = [package_name]
    package_specs.extend(f"{package_name}={version}" for version in _package_versions_from_apt_cache(package_name))
    seen_specs: set[str] = set()
    for package_spec in package_specs:
        if package_spec in seen_specs:
            continue
        seen_specs.add(package_spec)
        for command in (["apt-get", "download", package_spec], ["apt", "download", package_spec]):
            if _run_optional_command(command, cwd=packages_dir, log_file=log_file, allow_failure=True):
                after = sorted(path for path in packages_dir.glob("*.deb") if path.name not in before)
                if after:
                    return after[-1]
                before = {path.name for path in packages_dir.glob("*.deb")}
                continue
        before = {path.name for path in packages_dir.glob("*.deb")}
    return None


def _resolve_kernel_config_path(
    *,
    kernel_version: str,
    module_search_root: Path | None,
    cache_root: Path,
    searched_paths: list[str],
) -> Path | None:
    candidates: list[Path] = []
    if module_search_root is not None:
        version_root = module_search_root.parent if module_search_root.name == "kernel" else module_search_root
        candidates.append(version_root / "build" / ".config")
    if kernel_version:
        candidates.append(Path("/boot") / f"config-{kernel_version}")
        candidates.append(cache_root / "root" / "boot" / f"config-{kernel_version}")
    return _first_existing_file(candidates, searched_paths)


def _first_existing_file(candidates: list[Path], searched_paths: list[str]) -> Path | None:
    seen: set[str] = set()
    for candidate in candidates:
        candidate_str = str(candidate)
        if candidate_str in seen:
            continue
        seen.add(candidate_str)
        searched_paths.append(candidate_str)
        if candidate.is_file():
            return candidate
    return None


def _resolve_kernel_module_path(module_search_root: Path | None, module_name: str, module_alias_candidates: list[str]) -> Path | None:
    if module_search_root is None:
        return None
    search_names = [module_name]
    if "xxhash" in module_name.lower():
        search_names.extend(module_alias_candidates)
    seen: set[str] = set()
    for search_name in search_names:
        if search_name in seen:
            continue
        seen.add(search_name)
        matches = sorted(module_search_root.rglob(f"{search_name}.ko*"))
        if matches:
            return matches[0]
    return None


def _collect_kernel_config_symbols(config_path: Path | None, config_symbols: list[str]) -> list[dict[str, str]]:
    if not config_symbols:
        return []
    config_map: dict[str, str] = {}
    line_map: dict[str, str] = {}
    if config_path is not None and config_path.is_file():
        for raw_line in config_path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = raw_line.strip()
            if not line:
                continue
            if line.startswith("# CONFIG_") and line.endswith(" is not set"):
                symbol = line.split()[1]
                config_map[symbol] = "n"
                line_map[symbol] = line
                continue
            if "=" in line and line.startswith("CONFIG_"):
                symbol, value = line.split("=", 1)
                config_map[symbol] = value
                line_map[symbol] = line
    rows: list[dict[str, str]] = []
    for symbol in config_symbols:
        rows.append({"symbol": symbol, "value": config_map.get(symbol, ""), "line": line_map.get(symbol, "")})
    return rows


def _run_simple(command: list[str]) -> None:
    # CLI commands reserve stdout for their final JSON payload.
    subprocess.run(command, check=True, text=True, encoding="utf-8", stdout=sys.stderr)


def _run_optional_command(
    command: list[str],
    *,
    cwd: Path,
    log_file: Any | None,
    allow_failure: bool = False,
) -> bool:
    if log_file is not None:
        _log(log_file, f"$ {' '.join(command)}")
    try:
        process = subprocess.run(command, cwd=str(cwd), text=True, encoding="utf-8", capture_output=True, check=False)
    except FileNotFoundError:
        if allow_failure:
            return False
        raise RuntimeError(f"command is not available: {command[0]}")
    if log_file is not None:
        if process.stdout:
            log_file.write(process.stdout)
        if process.stderr:
            log_file.write(process.stderr)
        log_file.flush()
    if process.returncode == 0:
        return True
    if allow_failure:
        return False
    raise RuntimeError(f"command failed with exit status {process.returncode}: {' '.join(command)}")


def _configured_build_apt_deps() -> list[str]:
    configured = os.environ.get("DEBIAN_USB_DEBIAN_ISO_BUILD_APT_DEPS", "").strip()
    if not configured:
        return list(DEFAULT_DEBIAN_BUILD_APT_DEPS)
    return [token for token in configured.split() if token]


def _run_logged(command: list[str], *, cwd: Path, log_file: Any) -> None:
    _log(log_file, f"$ {' '.join(command)}")
    process = subprocess.Popen(
        command,
        cwd=str(cwd),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
    )
    assert process.stdout is not None
    for line in process.stdout:
        log_file.write(line)
        sys.stderr.write(line)
        sys.stderr.flush()
    exit_code = process.wait()
    if exit_code != 0:
        raise RuntimeError(f"command failed with exit status {exit_code}: {' '.join(command)}")


def _run_logged_capture(command: list[str], *, cwd: Path, log_file: Any) -> str:
    _log(log_file, f"$ {' '.join(command)}")
    process = subprocess.run(command, cwd=str(cwd), check=True, text=True, encoding="utf-8", capture_output=True)
    if process.stdout:
        log_file.write(process.stdout)
    if process.stderr:
        log_file.write(process.stderr)
    log_file.flush()
    return process.stdout


def _log(log_file: Any, message: str) -> None:
    log_file.write(message.rstrip() + "\n")
    log_file.flush()


def _emit_progress(message: str) -> None:
    sys.stderr.write(f"[installer-udeb-rebuild] {message.rstrip()}\n")
    sys.stderr.flush()


def _shell_quote(value: str) -> str:
    if value == "":
        return "''"
    return "'" + value.replace("'", "'\"'\"'") + "'"
