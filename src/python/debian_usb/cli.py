from __future__ import annotations

import argparse
import json
import sys

from .build_iso import build_debian_iso, ensure_debian_build_deps, inspect_build_kernel_support, validate_build_iso_plan_file
from .bootconfig import inspect_media, render_managed_grub
from .config import effective_managed_payload_layout, load_config, load_template_config
from .constants import (
    MANAGED_PAYLOAD_LAYOUT_EXTRACTED,
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
from .downloads import download_managed_source
from .installer_sources import prepare_managed_installer_source
from .devices import list_devices, list_local_isos
from .multios import payload_uuid_args_to_map, render_multios_grub, validate_multios_plan_file
from .rebuild_iso import (
    ensure_debian_rebuild_deps,
    inspect_debian_rebuild_source,
    rebuild_debian_installer_iso,
    remaster_live_initrd_source,
    remaster_live_persistence_source,
    remaster_live_tools_source,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="debian-usb-python")
    subparsers = parser.add_subparsers(dest="command", required=True)

    inspect_iso_cmd = subparsers.add_parser("inspect-iso")
    inspect_iso_cmd.add_argument("--iso-path", dest="source_path", required=True)
    inspect_iso_cmd.add_argument(
        "--profile",
        choices=[PERSISTENCE_MODE_NONE, PROFILE_DEBIAN, PROFILE_UBUNTU_DESKTOP, PROFILE_UBUNTU_SERVER, PROFILE_KALI_LINUX, PROFILE_KALI_PURPLE, PROFILE_TAILS],
        default=PERSISTENCE_MODE_NONE,
    )
    inspect_iso_cmd.add_argument("--config", default="")
    inspect_iso_cmd.add_argument("--use-custom-menu", type=int, choices=[0, 1], default=0)
    inspect_iso_cmd.add_argument("--source-role", choices=["primary", "netinst", "netboot"], default="primary")

    effective_payload_layout_cmd = subparsers.add_parser("effective-payload-layout")
    effective_payload_layout_cmd.add_argument(
        "--profile",
        choices=[PROFILE_DEBIAN, PROFILE_UBUNTU_DESKTOP, PROFILE_UBUNTU_SERVER, PROFILE_KALI_LINUX, PROFILE_KALI_PURPLE, PROFILE_TAILS],
        required=True,
    )
    effective_payload_layout_cmd.add_argument(
        "--configured-layout",
        choices=[MANAGED_PAYLOAD_LAYOUT_EXTRACTED, MANAGED_PAYLOAD_LAYOUT_RAW_ISO, MANAGED_PAYLOAD_LAYOUT_ISO_STORE, MANAGED_PAYLOAD_LAYOUT_SHARED_DATA],
        required=True,
    )
    effective_payload_layout_cmd.add_argument("--config", default="")
    effective_payload_layout_cmd.add_argument("--use-custom-menu", type=int, choices=[0, 1], default=0)

    render_managed_grub_cmd = subparsers.add_parser("render-managed-grub")
    render_managed_grub_cmd.add_argument("--iso-path", dest="source_path", required=True)
    render_managed_grub_cmd.add_argument(
        "--profile",
        choices=[PROFILE_DEBIAN, PROFILE_UBUNTU_DESKTOP, PROFILE_UBUNTU_SERVER, PROFILE_KALI_LINUX, PROFILE_KALI_PURPLE, PROFILE_TAILS],
        required=True,
    )
    render_managed_grub_cmd.add_argument("--live-uuid", required=True)
    render_managed_grub_cmd.add_argument("--persistence", type=int, choices=[0, 1], default=0)
    render_managed_grub_cmd.add_argument("--persistence-mode", choices=[PERSISTENCE_MODE_NONE, PERSISTENCE_MODE_PLAIN, PERSISTENCE_MODE_ENCRYPTED], default=PERSISTENCE_MODE_NONE)
    render_managed_grub_cmd.add_argument("--config", default="")
    render_managed_grub_cmd.add_argument("--menu-label", default="")
    render_managed_grub_cmd.add_argument("--kernel-args", default="")
    render_managed_grub_cmd.add_argument("--kernel-path", default="")
    render_managed_grub_cmd.add_argument("--initrd-path", default="")
    render_managed_grub_cmd.add_argument("--live-toram", type=int, choices=[0, 1], default=None)
    render_managed_grub_cmd.add_argument("--boot-assets-uuid", default="")
    render_managed_grub_cmd.add_argument("--use-custom-menu", type=int, choices=[0, 1], default=0)
    render_managed_grub_cmd.add_argument("--preserve-upstream-grub-entries", type=int, choices=[0, 1], default=0)
    render_managed_grub_cmd.add_argument("--include-preseed", type=int, choices=[0, 1], default=0)
    render_managed_grub_cmd.add_argument("--source-role", choices=["primary", "netinst", "netboot"], default="primary")

    validate_multios_plan_cmd = subparsers.add_parser("validate-multios-plan")
    validate_multios_plan_cmd.add_argument("--plan", required=True)
    validate_multios_plan_cmd.add_argument("--inspect-media", action="store_true")

    render_multios_grub_cmd = subparsers.add_parser("render-multios-grub")
    render_multios_grub_cmd.add_argument("--plan", required=True)
    render_multios_grub_cmd.add_argument("--config", default="")
    render_multios_grub_cmd.add_argument("--payload-uuid", action="append", default=[])
    render_multios_grub_cmd.add_argument("--boot-assets-uuid", default="")

    subparsers.add_parser("list-local-isos")

    subparsers.add_parser("list-devices")

    download_managed_source_cmd = subparsers.add_parser("download-managed-source")
    download_managed_source_cmd.add_argument("--key", required=True)
    download_managed_source_cmd.add_argument("--config", default="")

    prepare_installer_source_cmd = subparsers.add_parser("prepare-managed-installer-source")
    prepare_installer_source_cmd.add_argument("--profile", required=True)
    prepare_installer_source_cmd.add_argument("--source-role", required=True)
    prepare_installer_source_cmd.add_argument("--kernel-path", required=True)
    prepare_installer_source_cmd.add_argument("--initrd-path", required=True)
    prepare_installer_source_cmd.add_argument("--iso-path", default="")
    prepare_installer_source_cmd.add_argument("--output-dir", default="")
    prepare_installer_source_cmd.add_argument("--extra-module", action="append", default=[])
    prepare_installer_source_cmd.add_argument("--module-source-strategy", default="")
    prepare_installer_source_cmd.add_argument("--initrd-preseed-path", default="")
    prepare_installer_source_cmd.add_argument("--initrd-overlay-dir", default="")
    prepare_installer_source_cmd.add_argument("--preflight-only", type=int, choices=[0, 1], default=0)

    remaster_live_initrd_cmd = subparsers.add_parser("remaster-live-initrd-source")
    remaster_live_initrd_cmd.add_argument(
        "--profile",
        choices=[PROFILE_DEBIAN, PROFILE_KALI_LINUX, PROFILE_UBUNTU_DESKTOP, PROFILE_UBUNTU_SERVER, PROFILE_TAILS],
        required=True,
    )
    remaster_live_initrd_cmd.add_argument("--source-iso", required=True)
    remaster_live_initrd_cmd.add_argument("--overlay-dir", required=True)
    remaster_live_initrd_cmd.add_argument("--output-dir", default="")

    remaster_live_persistence_cmd = subparsers.add_parser("remaster-live-persistence-source")
    remaster_live_persistence_cmd.add_argument(
        "--profile",
        choices=[PROFILE_DEBIAN, PROFILE_KALI_LINUX, PROFILE_TAILS],
        required=True,
    )
    remaster_live_persistence_cmd.add_argument("--source-iso", required=True)

    remaster_live_tools_cmd = subparsers.add_parser("remaster-live-tools-source")
    remaster_live_tools_cmd.add_argument(
        "--profile",
        choices=[PROFILE_DEBIAN, PROFILE_KALI_LINUX, PROFILE_UBUNTU_DESKTOP],
        required=True,
    )
    remaster_live_tools_cmd.add_argument("--source-iso", required=True)
    remaster_live_tools_cmd.add_argument("--output-dir", default="")
    live_tool_selection = remaster_live_tools_cmd.add_mutually_exclusive_group()
    live_tool_selection.add_argument("--group", action="append", dest="selected_groups")
    live_tool_selection.add_argument("--no-tools", action="store_true")
    remaster_live_tools_cmd.add_argument("--live-kernel-args", default="")

    validate_build_iso_plan_cmd = subparsers.add_parser("validate-build-iso-plan")
    validate_build_iso_plan_cmd.add_argument("--plan", required=True)

    build_debian_iso_cmd = subparsers.add_parser("build-debian-iso")
    build_debian_iso_cmd.add_argument("--plan", required=True)

    inspect_debian_rebuild_cmd = subparsers.add_parser("inspect-debian-rebuild-source")
    inspect_debian_rebuild_cmd.add_argument("--source-iso", required=True)

    rebuild_debian_installer_iso_cmd = subparsers.add_parser("rebuild-debian-installer-iso")
    rebuild_debian_installer_iso_cmd.add_argument("--plan", required=True)

    inspect_build_kernel_support_cmd = subparsers.add_parser("inspect-build-kernel-support")
    inspect_build_kernel_support_cmd.add_argument("--kernel-version", default="")
    inspect_build_kernel_support_cmd.add_argument("--module-tree-dir", default="")
    inspect_build_kernel_support_cmd.add_argument("--module", action="append", default=[])
    inspect_build_kernel_support_cmd.add_argument("--module-alias", action="append", default=[])
    inspect_build_kernel_support_cmd.add_argument("--config-symbol", action="append", default=[])
    inspect_build_kernel_support_cmd.add_argument("--download-if-missing", type=int, choices=[0, 1], default=0)

    subparsers.add_parser("ensure-debian-build-deps")
    subparsers.add_parser("ensure-debian-rebuild-deps")

    args = parser.parse_args(argv)
    try:
        if args.command == "inspect-iso":
            payload = inspect_media(
                args.source_path,
                args.profile,
                config_path=args.config,
                use_custom_menu=bool(args.use_custom_menu),
                source_role=args.source_role,
            )
        elif args.command == "effective-payload-layout":
            config_data = load_config(args.config) if args.config else load_template_config()
            payload = {
                "managed_payload_layout": effective_managed_payload_layout(
                    args.profile,
                    args.configured_layout,
                    config_data,
                    bool(args.use_custom_menu),
                )
            }
        elif args.command == "render-managed-grub":
            payload = render_managed_grub(
                source_path=args.source_path,
                profile=args.profile,
                live_uuid=args.live_uuid,
                persistence=bool(args.persistence),
                persistence_mode=args.persistence_mode,
                config_path=args.config,
                menu_label_override=args.menu_label,
                kernel_args_override=args.kernel_args,
                kernel_path_override=args.kernel_path,
                initrd_path_override=args.initrd_path,
                live_toram=None if args.live_toram is None else bool(args.live_toram),
                boot_assets_uuid=args.boot_assets_uuid,
                use_custom_menu=bool(args.use_custom_menu),
                preserve_upstream_entries=bool(args.preserve_upstream_grub_entries),
                include_preseed_entries=bool(args.include_preseed),
                source_role=args.source_role,
            )
        elif args.command == "validate-multios-plan":
            payload = validate_multios_plan_file(args.plan, inspect_sources=args.inspect_media)
        elif args.command == "render-multios-grub":
            payload = render_multios_grub(
                plan_path=args.plan,
                config_path=args.config,
                payload_uuids=payload_uuid_args_to_map(args.payload_uuid),
                boot_assets_uuid=args.boot_assets_uuid,
            )
        elif args.command == "list-local-isos":
            payload = list_local_isos()
        elif args.command == "list-devices":
            payload = list_devices()
        elif args.command == "download-managed-source":
            payload = download_managed_source(args.config, args.key)
        elif args.command == "prepare-managed-installer-source":
            payload = prepare_managed_installer_source(
                args.profile,
                args.source_role,
                args.kernel_path,
                args.initrd_path,
                iso_path=args.iso_path,
                output_dir=args.output_dir,
                extra_initrd_modules=args.extra_module,
                module_source_strategy=args.module_source_strategy,
                initrd_preseed_path=args.initrd_preseed_path,
                initrd_overlay_dir=args.initrd_overlay_dir,
                preflight_only=bool(args.preflight_only),
            )
        elif args.command == "remaster-live-initrd-source":
            payload = remaster_live_initrd_source(args.source_iso, args.profile, args.overlay_dir, args.output_dir)
        elif args.command == "remaster-live-persistence-source":
            payload = remaster_live_persistence_source(args.source_iso, args.profile)
        elif args.command == "remaster-live-tools-source":
            payload = remaster_live_tools_source(
                args.source_iso,
                args.profile,
                args.output_dir,
                selected_groups=[] if args.no_tools else args.selected_groups,
                live_kernel_args=args.live_kernel_args,
            )
        elif args.command == "validate-build-iso-plan":
            payload = validate_build_iso_plan_file(args.plan)
        elif args.command == "build-debian-iso":
            payload = build_debian_iso(args.plan)
        elif args.command == "inspect-debian-rebuild-source":
            payload = inspect_debian_rebuild_source(args.source_iso)
        elif args.command == "rebuild-debian-installer-iso":
            payload = rebuild_debian_installer_iso(args.plan)
        elif args.command == "inspect-build-kernel-support":
            payload = inspect_build_kernel_support(
                kernel_version=args.kernel_version,
                module_names=args.module,
                module_alias_candidates=args.module_alias,
                config_symbols=args.config_symbol,
                module_tree_dir=args.module_tree_dir,
                download_if_missing=bool(args.download_if_missing),
            )
        elif args.command == "ensure-debian-build-deps":
            payload = ensure_debian_build_deps()
        elif args.command == "ensure-debian-rebuild-deps":
            payload = ensure_debian_rebuild_deps()
        else:
            raise ValueError(f"unsupported command: {args.command}")
    except Exception as exc:  # pragma: no cover - handled through process exit
        print(str(exc), file=sys.stderr)
        return 1
    json.dump(payload, sys.stdout)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
