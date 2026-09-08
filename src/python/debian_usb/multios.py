from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
import re

from .boot_inspect import _detect_firmware, _supports_encrypted_persistence, inspect_media
from .boot_parse import BootEntry, _find_boot_entries, _media_class, _select_entry
from .boot_render import (
    _adapt_entry_for_managed,
    _attach_iso_store_metadata,
    _boot_initrd_assets,
    _config_for_payload_layout,
    _entry_sort_key,
    _escape_grub_string,
    _filter_entries_for_secure_boot,
    _filter_entries_for_profile,
    _filter_entries_for_source_role,
    _installer_initrd_patch_manifest,
    _installer_boot_initrd_patch_assets,
    _installer_media_tree_manifest,
    _iso_payload_manifest,
    _LIVE_TORAM_MODULE_CONFIG_KEY,
    _live_toram_module_for_source,
    _load_effective_config,
    _load_custom_profile_spec,
    _managed_payload_module_lines,
    _payload_boot_assets_manifest,
    _payload_extra_assets_manifest,
    _profile_family_id,
    _preserved_family_menu_title,
    _secure_boot_policy_lines,
    _configured_static_entry_title,
    _configured_updatevars_menu_labels,
    _render_mok_menu_entry,
    _render_updatevars_menu_entry,
    _render_grub_menu,
    _signed_kernel_assets,
    _synthetic_live_entry,
    _updatevars_efi_available,
    build_custom_profile_preserved_entries,
    build_custom_profile_family_preseed_entries,
    build_custom_profile_entries,
    render_custom_main_menu,
)
from .catalog import profile_for
from .config import effective_managed_payload_layout
from .constants import (
    MANAGED_PAYLOAD_LAYOUT_EXTRACTED,
    MANAGED_PAYLOAD_LAYOUT_ISO_STORE,
    MANAGED_PAYLOAD_LAYOUT_RAW_ISO,
    MANAGED_PAYLOAD_LAYOUT_SHARED_DATA,
    PERSISTENCE_MODE_ENCRYPTED,
    PERSISTENCE_MODE_NONE,
    PERSISTENCE_MODE_PLAIN,
    PROFILE_DEBIAN,
    PROFILE_KEYS,
    PROFILE_KALI_LINUX,
    PROFILE_KALI_PURPLE,
    PROFILE_TAILS,
)
from .iso_source import open_source


MAX_PLAN_BYTES = 256 * 1024
_PLAN_ID_PATTERN = re.compile(r"^[A-Za-z0-9._+-]{1,128}$")
SOURCE_ROLE_PRIMARY = "primary"
SOURCE_ROLE_NETINST = "netinst"
SOURCE_ROLE_NETBOOT = "netboot"
SOURCE_ROLES = {SOURCE_ROLE_PRIMARY, SOURCE_ROLE_NETINST, SOURCE_ROLE_NETBOOT}
SECURE_BOOT_TRUST_MODES = {"mok", "firmware-db"}
INSTALLER_ENTRY_KINDS = {"installer", "automated-installer", "expert-installer", "rescue"}
MULTIOS_PROFILE_KEYS = {PROFILE_DEBIAN, PROFILE_KALI_LINUX, PROFILE_KALI_PURPLE, PROFILE_TAILS}


def _fixed_debian_kali_preseed_profile(profile: str) -> bool:
    return profile in {PROFILE_DEBIAN, PROFILE_KALI_LINUX, PROFILE_KALI_PURPLE}


def _family_preseed_owner_rank(owner_data: dict[str, object], *, online: bool) -> tuple[int, int]:
    source_role = str(owner_data.get("source_role") or SOURCE_ROLE_PRIMARY)
    media_class = str(owner_data.get("media_class") or "")
    item_index = int(owner_data.get("item_index") or 0)
    is_live_media = media_class in {"live", "hybrid"}
    if online:
        if source_role in {SOURCE_ROLE_NETINST, SOURCE_ROLE_NETBOOT}:
            mode_rank = 0
        elif not is_live_media:
            mode_rank = 1
        else:
            mode_rank = 2
    else:
        if source_role in {SOURCE_ROLE_NETINST, SOURCE_ROLE_NETBOOT} or media_class == "installer":
            mode_rank = 0
        elif not is_live_media:
            mode_rank = 1
        else:
            mode_rank = 2
    return (mode_rank, item_index)


def _select_family_preseed_owner(owner_candidates: list[dict[str, object]], *, online: bool) -> dict[str, object] | None:
    if not owner_candidates:
        return None
    return min(owner_candidates, key=lambda owner_data: _family_preseed_owner_rank(owner_data, online=online))


def _family_preseed_title(owner_data: dict[str, object]) -> str:
    profile = str(owner_data.get("profile") or "")
    source_role = str(owner_data.get("source_role") or SOURCE_ROLE_PRIMARY)
    media_class = str(owner_data.get("media_class") or "")
    profile_spec = _load_custom_profile_spec(profile, source_role, media_class)
    labels = profile_spec.get("labels", {})
    if not isinstance(labels, dict):
        return ""
    profile_title = str(profile_spec.get("title") or profile_for(profile).title).strip()
    return str(labels.get("preseed_default") or f"{profile_title} Preseed")


def _push_family_legacy_entries_to_end(entries: list[BootEntry], legacy_title: str) -> list[BootEntry]:
    legacy_title = legacy_title.strip()
    if not legacy_title:
        return entries
    legacy_entries = [
        entry
        for entry in entries
        if entry.menu_path and entry.menu_path[0] == legacy_title
    ]
    if not legacy_entries:
        return entries
    nonlegacy_entries = [
        entry
        for entry in entries
        if not entry.menu_path or entry.menu_path[0] != legacy_title
    ]
    if not nonlegacy_entries:
        return entries
    next_order = max(entry.order for entry in nonlegacy_entries) + 10
    reordered_legacy = []
    for entry in sorted(legacy_entries, key=_entry_sort_key):
        reordered_legacy.append(replace(entry, order=next_order))
        next_order += 1
    return [*nonlegacy_entries, *reordered_legacy]


def _profile_supports_source_role(profile: str, source_role: str) -> bool:
    if source_role == SOURCE_ROLE_PRIMARY:
        return True
    if source_role == SOURCE_ROLE_NETINST:
        return profile in {PROFILE_DEBIAN, PROFILE_KALI_LINUX}
    if source_role == SOURCE_ROLE_NETBOOT:
        return profile in {PROFILE_DEBIAN, PROFILE_KALI_LINUX}
    return False


def _source_role_requires_live_primary(profile: str) -> bool:
    return profile in {PROFILE_DEBIAN, PROFILE_KALI_LINUX, PROFILE_TAILS}


def _preseed_eligible(profile: str, source_role: str, media_class: str) -> bool:
    if profile in {PROFILE_DEBIAN, PROFILE_KALI_LINUX}:
        return source_role in {SOURCE_ROLE_NETINST, SOURCE_ROLE_NETBOOT}
    if profile == PROFILE_KALI_PURPLE:
        return source_role == SOURCE_ROLE_PRIMARY and media_class == "installer"
    return False


def _validate_source_role_media(profile: str, source_role: str, media_class: str, iso_path: Path) -> None:
    if source_role in {SOURCE_ROLE_NETINST, SOURCE_ROLE_NETBOOT}:
        if media_class not in {"installer", "hybrid"}:
            raise ValueError(f"{source_role} source must expose installer boot entries: {iso_path}")
        return
    if _source_role_requires_live_primary(profile) and media_class not in {"live", "hybrid"}:
        raise ValueError(
            f"{profile_for(profile).title} primary source must be live or hybrid media; "
            f"use a netinst or netboot source for installer-only media: {iso_path}"
        )
    if profile == PROFILE_KALI_PURPLE and media_class and media_class != "installer":
        raise ValueError(f"{profile_for(profile).title} primary source must expose installer boot entries: {iso_path}")


def _validate_prepared_installer_source(source_path: Path, source_role: str) -> None:
    asset_root = "hd-media" if source_role == SOURCE_ROLE_NETINST else "netboot"
    for name in ("vmlinuz", "initrd.gz"):
        asset_path = source_path / asset_root / name
        if not asset_path.is_file():
            raise ValueError(f"{source_role} source is missing separate {asset_root}/{name}: {source_path}")
        if asset_path.stat().st_size <= 0:
            raise ValueError(f"{source_role} source contains an empty separate {asset_root}/{name}: {source_path}")
    if source_role != SOURCE_ROLE_NETINST:
        return
    payload_dir = source_path / "payload"
    payload_isos = sorted(payload_dir.glob("*.iso")) if payload_dir.is_dir() else []
    if len(payload_isos) != 1 or not payload_isos[0].is_file():
        raise ValueError(f"netinst source must contain exactly one separate payload/*.iso: {source_path}")
    if payload_isos[0].stat().st_size <= 0:
        raise ValueError(f"netinst source contains an empty separate payload ISO: {payload_isos[0]}")


def load_multios_plan(plan_path: str, *, inspect_sources: bool = False) -> dict[str, object]:
    path = Path(plan_path).expanduser().resolve()
    if not path.is_file():
        raise ValueError(f"Multi-OS plan is not a regular file: {path}")
    if path.stat().st_size > MAX_PLAN_BYTES:
        raise ValueError(f"Multi-OS plan is larger than {MAX_PLAN_BYTES} bytes: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid Multi-OS plan JSON: {exc}") from exc
    return validate_multios_plan(payload, inspect_sources=inspect_sources)


def validate_multios_plan(plan: dict[str, object], *, inspect_sources: bool = False) -> dict[str, object]:
    if not isinstance(plan, dict):
        raise ValueError("Multi-OS plan must be a JSON object")
    if plan.get("schema_version") != 1:
        raise ValueError("Multi-OS plan schema_version must be 1")
    if plan.get("write_mode") != "multi-os":
        raise ValueError("Multi-OS plan write_mode must be multi-os")
    use_custom_grub_menu = plan.get("use_custom_grub_menu", False)
    if not isinstance(use_custom_grub_menu, bool):
        raise ValueError("Multi-OS plan use_custom_grub_menu must be a boolean")
    preserve_upstream_grub_entries = plan.get("preserve_upstream_grub_entries", False)
    if not isinstance(preserve_upstream_grub_entries, bool):
        raise ValueError("Multi-OS plan preserve_upstream_grub_entries must be a boolean")
    secure_boot_trust = str(plan.get("secure_boot_trust") or "mok").strip() or "mok"
    if secure_boot_trust not in SECURE_BOOT_TRUST_MODES:
        raise ValueError(f"Multi-OS plan secure_boot_trust must be one of: {', '.join(sorted(SECURE_BOOT_TRUST_MODES))}")
    plan["secure_boot_trust"] = secure_boot_trust
    esp_label = str(plan.get("esp_label") or "ESPBOOT").strip() or "ESPBOOT"
    _validate_label(esp_label, "esp_label", max_length=11)
    plan["esp_label"] = esp_label
    items = plan.get("items")
    if not isinstance(items, list) or len(items) == 0:
        raise ValueError("Multi-OS plan requires at least one item")
    seen_profile_roles: set[tuple[str, str]] = set()
    seen_ids: set[str] = set()
    seen_labels: set[str] = set()
    validated_items: list[dict[str, object]] = []
    for index, raw_item in enumerate(items, start=1):
        if not isinstance(raw_item, dict):
            raise ValueError(f"Multi-OS item {index} must be an object")
        item = dict(raw_item)
        item_id = _required_string(item, "id", index)
        if not _PLAN_ID_PATTERN.fullmatch(item_id):
            raise ValueError(f"Multi-OS item {index} id may only contain A-Z, a-z, 0-9, dot, underscore, plus, or hyphen")
        if item_id in seen_ids:
            raise ValueError(f"duplicate Multi-OS item id: {item_id}")
        seen_ids.add(item_id)
        profile = _required_string(item, "profile", index)
        if profile not in PROFILE_KEYS:
            raise ValueError(f"unsupported Multi-OS profile: {profile}")
        if profile not in MULTIOS_PROFILE_KEYS:
            raise ValueError(f"{profile_for(profile).title} is not supported in Multi-OS; use its separate single-OS workflow")
        source_role = _optional_string(item, "source_role", SOURCE_ROLE_PRIMARY)
        if source_role not in SOURCE_ROLES:
            raise ValueError(f"unsupported Multi-OS source role: {source_role}")
        item["source_role"] = source_role
        if not _profile_supports_source_role(profile, source_role):
            raise ValueError(f"profile does not support source role: {profile}/{source_role}")
        media_class = str(item.get("media_class") or "").strip()
        preseed = item.get("preseed", False)
        if not isinstance(preseed, bool):
            raise ValueError(f"preseed must be a boolean for {profile}")
        if preseed and not use_custom_grub_menu:
            raise ValueError(f"preseed entries require custom GRUB mode for {profile}")
        if preseed and not _preseed_eligible(profile, source_role, media_class):
            raise ValueError(f"preseed entries require an installer-capable managed source role for {profile}")
        item["preseed"] = bool(preseed)
        if use_custom_grub_menu and _preseed_eligible(profile, source_role, media_class):
            item["preseed"] = True
        else:
            item["preseed"] = False
        profile_role = (profile, source_role)
        if profile_role in seen_profile_roles:
            raise ValueError(f"duplicate Multi-OS profile/source role: {profile}/{source_role}")
        seen_profile_roles.add(profile_role)
        profile_meta = profile_for(profile)
        iso_path = Path(_required_string(item, "iso_path", index)).expanduser().resolve()
        if source_role == SOURCE_ROLE_PRIMARY:
            if not iso_path.is_file():
                raise ValueError(f"Multi-OS ISO path is not a regular file: {iso_path}")
            if iso_path.suffix.lower() != ".iso":
                raise ValueError(f"Multi-OS ISO path must end with .iso: {iso_path}")
        elif not iso_path.is_dir():
            source_label = "prepared hd-media" if source_role == SOURCE_ROLE_NETINST else "prepared netboot"
            raise ValueError(f"{source_role} source must be a {source_label} source directory: {iso_path}")
        else:
            _validate_prepared_installer_source(iso_path, source_role)
        item["iso_path"] = str(iso_path)
        if media_class:
            _validate_source_role_media(profile, source_role, media_class, iso_path)
        from .installer_profiles import normalize_hd_media_dirs
        item["hd_media_preseed_dirs"] = normalize_hd_media_dirs(profile, source_role, item.get("hd_media_preseed_dirs"))
        offline_preseed_source_dir = str(item.get("offline_preseed_source_dir") or "").strip()
        if offline_preseed_source_dir:
            offline_preseed_path = Path(offline_preseed_source_dir).expanduser().resolve()
            if not offline_preseed_path.is_dir():
                raise ValueError(f"offline_preseed_source_dir is not a directory for {profile}: {offline_preseed_path}")
            item["offline_preseed_source_dir"] = str(offline_preseed_path)
        else:
            item["offline_preseed_source_dir"] = ""
        layout = _required_string(item, "managed_payload_layout", index)
        if layout not in {MANAGED_PAYLOAD_LAYOUT_EXTRACTED, MANAGED_PAYLOAD_LAYOUT_RAW_ISO, MANAGED_PAYLOAD_LAYOUT_ISO_STORE, MANAGED_PAYLOAD_LAYOUT_SHARED_DATA}:
            raise ValueError(f"unsupported payload layout for {profile}: {layout}")
        if source_role in {SOURCE_ROLE_NETINST, SOURCE_ROLE_NETBOOT} and layout not in {
            MANAGED_PAYLOAD_LAYOUT_ISO_STORE,
            MANAGED_PAYLOAD_LAYOUT_SHARED_DATA,
        }:
            raise ValueError(f"{source_role} source requires an ISO-store payload layout for {profile}")
        if layout != MANAGED_PAYLOAD_LAYOUT_SHARED_DATA:
            _validate_label(_required_string(item, "payload_fs_label", index), "payload_fs_label")
            _validate_label(_required_string(item, "payload_partlabel", index), "payload_partlabel")
            if item["payload_fs_label"] in seen_labels:
                raise ValueError(f"duplicate payload label: {item['payload_fs_label']}")
            seen_labels.add(str(item["payload_fs_label"]))
        else:
            item["payload_fs_label"] = str(item.get("payload_fs_label") or "")
            item["payload_partlabel"] = str(item.get("payload_partlabel") or "")
        persistence = item.get("persistence", False)
        if not isinstance(persistence, bool):
            raise ValueError(f"persistence must be a boolean for {profile}")
        item["persistence"] = persistence
        live_toram = item.get("live_toram", False)
        if not isinstance(live_toram, bool):
            raise ValueError(f"live_toram must be a boolean for {profile}")
        persistence_mode = str(item.get("persistence_mode", PERSISTENCE_MODE_NONE) or PERSISTENCE_MODE_NONE)
        if persistence and persistence_mode == PERSISTENCE_MODE_NONE:
            persistence_mode = PERSISTENCE_MODE_PLAIN
            item["persistence_mode"] = persistence_mode
        if not persistence:
            persistence_mode = PERSISTENCE_MODE_NONE
            item["persistence_mode"] = persistence_mode
        if persistence_mode not in {PERSISTENCE_MODE_NONE, PERSISTENCE_MODE_PLAIN, PERSISTENCE_MODE_ENCRYPTED}:
            raise ValueError(f"invalid persistence mode for {profile}: {persistence_mode}")
        if profile == PROFILE_TAILS:
            if persistence:
                raise ValueError("Tails native Persistent Storage is not supported on managed/multi-OS USBs; use the official Tails USB image on a dedicated device")
            for field in ("kernel_args", "kernel_path", "initrd_path"):
                if str(item.get(field) or "").strip():
                    raise ValueError(f"Tails must use its stock ISO without {field} overrides")
        if persistence and not profile_meta.supports_persistence:
            raise ValueError(f"profile does not support persistence: {profile}")
        if source_role in {SOURCE_ROLE_NETINST, SOURCE_ROLE_NETBOOT}:
            if persistence:
                raise ValueError(f"{source_role} source cannot enable persistence for {profile}")
            if live_toram:
                raise ValueError(f"{source_role} source cannot enable live_toram for {profile}")
            for field in ("kernel_args", "kernel_path", "initrd_path", "menu_label"):
                if str(item.get(field) or "").strip():
                    raise ValueError(f"{source_role} source cannot use live override field {field} for {profile}")
        if persistence:
            size = item.get("persistence_size_gib", 0)
            if type(size) is not int or size <= 0:
                raise ValueError(f"persistence_size_gib must be a positive integer for {profile}")
            fs_label = _required_string(item, "persistence_fs_label", index)
            part_label = _required_string(item, "persistence_partlabel", index)
            _validate_label(fs_label, "persistence_fs_label")
            _validate_label(part_label, "persistence_partlabel")
            if fs_label in seen_labels:
                raise ValueError(f"duplicate persistence label: {fs_label}")
            seen_labels.add(fs_label)
        if inspect_sources:
            inspection = inspect_media(str(iso_path), profile, source_role=source_role)
            if not bool(inspection.get("managed_supported")):
                raise ValueError(f"Multi-OS ISO is not managed-capable for {profile}: {iso_path}")
            _validate_source_role_media(profile, source_role, str(inspection.get("media_class") or ""), iso_path)
            if persistence and not bool(inspection.get("supports_persistence")):
                raise ValueError(f"selected media does not support persistence for {profile}: {iso_path}")
            if persistence_mode == PERSISTENCE_MODE_ENCRYPTED and not bool(inspection.get("supports_encrypted_persistence")):
                raise ValueError(f"encrypted persistence is not supported for {profile}: {iso_path}")
        validated_items.append(item)
    persistence_labels: dict[str, str] = {}
    for item in validated_items:
        if not item["persistence"]:
            continue
        for field in ("persistence_fs_label", "persistence_partlabel"):
            label = str(item[field])
            owner = persistence_labels.setdefault(label, str(item["id"]))
            if owner != item["id"]:
                raise ValueError(f"persistence filesystem/GPT label collision: {label}")
        if item.get("media_class") == "installer":
            raise ValueError(f"installer media cannot enable Live persistence: {item['id']}")
    plan = dict(plan)
    plan["items"] = validated_items
    return plan


def validate_multios_plan_file(plan_path: str, *, inspect_sources: bool = False) -> dict[str, object]:
    return {
        "valid": True,
        "item_count": len(load_multios_plan(plan_path, inspect_sources=inspect_sources)["items"]),
    }


def render_multios_grub(
    plan_path: str,
    config_path: str,
    payload_uuids: dict[str, str],
    boot_assets_uuid: str = "",
) -> dict[str, object]:
    plan = load_multios_plan(plan_path, inspect_sources=True)
    items = plan["items"]
    assert isinstance(items, list)
    use_custom_menu = bool(plan.get("use_custom_grub_menu", False))
    preserve_upstream_grub_entries = bool(plan.get("preserve_upstream_grub_entries", False))
    config_data = _load_effective_config(config_path)
    uuid_map = _validate_payload_uuids(items, payload_uuids)
    module_lines = [
        "insmod part_gpt",
        "insmod part_msdos",
        "insmod search_fs_file",
        "insmod search_fs_uuid",
        "insmod fat",
        "insmod ext2",
    ]
    if any(str(item.get("managed_payload_layout") or "") == MANAGED_PAYLOAD_LAYOUT_RAW_ISO for item in items if isinstance(item, dict)):
        module_lines.append("insmod iso9660")
    lines = [
        "set timeout=-1",
        "set default=0",
        "set pager=1",
        "",
        *module_lines,
        "",
    ]
    lines.extend(_secure_boot_policy_lines(boot_assets_uuid))
    total_entries = 0
    media_classes: list[str] = []
    top_level_entries: list[str] = []
    signed_kernel_assets: list[dict[str, str]] = []
    boot_initrd_assets: list[dict[str, str]] = []
    iso_payloads: list[dict[str, str]] = []
    installer_media_trees: list[dict[str, str]] = []
    payload_boot_assets: list[dict[str, str]] = []
    payload_extra_assets: list[dict[str, str]] = []
    installer_initrd_patches: list[dict[str, str]] = []
    installer_boot_initrd_patches: list[dict[str, str]] = []
    if use_custom_menu:
        family_counts: dict[str, int] = {}
        family_owner_data: dict[str, list[dict[str, object]]] = {}
        family_preseed_owner_data: dict[str, dict[str, list[dict[str, object]]]] = {}
        fixed_netinst_profiles = {
            str(item.get("profile") or "")
            for item in items
            if isinstance(item, dict)
            and _fixed_debian_kali_preseed_profile(str(item.get("profile") or ""))
            and str(item.get("source_role") or SOURCE_ROLE_PRIMARY) == SOURCE_ROLE_NETINST
        }
        for item in items:
            if not isinstance(item, dict):
                continue
            family_id = _profile_family_id(str(item["profile"]))
            family_counts[family_id] = family_counts.get(family_id, 0) + 1
        family_entries: dict[str, list[BootEntry]] = {}
        family_offsets: dict[str, int] = {}
        for item_index, item in enumerate(items):
            assert isinstance(item, dict)
            profile = str(item["profile"])
            namespace = str(item.get("id") or profile)
            live_uuid = uuid_map[str(item["id"])]
            source_entries, rendered_entries, media_class, supports_encrypted_live = _prepare_multios_item_entries(item, config_data, live_uuid)
            item_layout = str(item.get("managed_payload_layout") or "")
            if boot_assets_uuid:
                rendered_entries = _filter_entries_for_secure_boot(rendered_entries)
            item_config_data = _config_for_payload_layout(
                config_data,
                profile,
                item_layout,
            )
            media_classes.append(media_class)
            family_id = _profile_family_id(profile)
            order_offset = family_offsets.get(family_id, 0)
            item_source_role = str(item.get("source_role") or SOURCE_ROLE_PRIMARY)
            skip_duplicate_fixed_netinst = (
                _fixed_debian_kali_preseed_profile(profile)
                and item_source_role == SOURCE_ROLE_PRIMARY
                and profile in fixed_netinst_profiles
                and media_class == "installer"
            )
            custom_entries = []
            if not skip_duplicate_fixed_netinst:
                custom_entries = build_custom_profile_entries(
                    profile=profile,
                    source_role=item_source_role,
                    source_path=str(item["iso_path"]),
                    media_class=media_class,
                    entries=source_entries,
                    rendered_entries=rendered_entries,
                    config_data=item_config_data,
                    payload_uuid=live_uuid,
                    asset_namespace=namespace,
                    include_profile_submenu=family_counts.get(family_id, 0) > 1,
                    order_offset=order_offset,
                    supports_encrypted_live=supports_encrypted_live,
                )
            family_offsets[family_id] = order_offset + max(len(custom_entries), 1) + 10
            total_entries += len(custom_entries)
            family_entries.setdefault(family_id, []).extend(custom_entries)
            iso_payloads.extend(
                _iso_payload_manifest(
                    profile,
                    str(item.get("source_role") or SOURCE_ROLE_PRIMARY),
                    media_class,
                    str(item["iso_path"]),
                    str(item.get("payload_iso_name") or ""),
                    str(item.get("managed_payload_layout") or ""),
                )
            )
            installer_media_trees.extend(_installer_media_tree_manifest(custom_entries, str(item["iso_path"])))
            payload_boot_assets.extend(_payload_boot_assets_manifest(custom_entries, str(item["iso_path"])))
            payload_extra_assets.extend(
                _payload_extra_assets_manifest(
                    profile,
                    str(item.get("source_role") or SOURCE_ROLE_PRIMARY),
                    media_class,
                    str(item["iso_path"]),
                    str(item.get("managed_payload_layout") or ""),
                )
            )
            installer_initrd_patches.extend(_installer_initrd_patch_manifest(custom_entries))
            installer_boot_initrd_patches.extend(_installer_boot_initrd_patch_assets(custom_entries, namespace, str(item["iso_path"])))
            owner_data = {
                "profile": profile,
                "source_role": str(item.get("source_role") or SOURCE_ROLE_PRIMARY),
                "source_path": str(item["iso_path"]),
                "media_class": media_class,
                "managed_payload_layout": str(item.get("managed_payload_layout") or ""),
                "entries": source_entries,
                "rendered_entries": rendered_entries,
                "config_data": item_config_data,
                "payload_uuid": live_uuid,
                "asset_namespace": namespace,
                "item_index": item_index,
            }
            family_owner_data.setdefault(family_id, []).append(owner_data)
            if (
                bool(item.get("preseed", False))
                and str(item.get("source_role") or SOURCE_ROLE_PRIMARY) == SOURCE_ROLE_NETINST
                and not _fixed_debian_kali_preseed_profile(profile)
            ):
                family_preseed_owner_data.setdefault(family_id, {}).setdefault(profile, []).append(
                    owner_data
                )
            if boot_assets_uuid:
                signed_kernel_assets.extend(_signed_kernel_assets(custom_entries, namespace, str(item["iso_path"])))
                boot_initrd_assets.extend(_boot_initrd_assets(custom_entries, namespace, str(item["iso_path"])))
        for family_id, owner_group_map in family_preseed_owner_data.items():
            shared_order_offset = family_offsets.get(family_id, 0)
            profile_owner_groups = sorted(
                owner_group_map.values(),
                key=lambda owner_candidates: min(int(candidate.get("item_index") or 0) for candidate in owner_candidates),
            )
            for owner_candidates in profile_owner_groups:
                shared_preseed_entries: list[BootEntry] = []
                title_owner = _select_family_preseed_owner(owner_candidates, online=False) or owner_candidates[0]
                preseed_title_override = _family_preseed_title(title_owner)
                for include_online, include_offline in ((True, False), (False, True)):
                    owner_data = _select_family_preseed_owner(owner_candidates, online=include_online)
                    if owner_data is None:
                        continue
                    mode_entries = build_custom_profile_family_preseed_entries(
                        profile=str(owner_data["profile"]),
                        source_role=str(owner_data["source_role"]),
                        source_path=str(owner_data["source_path"]),
                        media_class=str(owner_data["media_class"]),
                        entries=list(owner_data["entries"]),
                        rendered_entries=list(owner_data["rendered_entries"]),
                        config_data=dict(owner_data["config_data"]),
                        payload_uuid=str(owner_data["payload_uuid"]),
                        asset_namespace=str(owner_data["asset_namespace"]),
                        order_offset=shared_order_offset,
                        include_online=include_online,
                        include_offline=include_offline,
                        preseed_title_override=preseed_title_override,
                    )
                    if not mode_entries:
                        continue
                    shared_preseed_entries.extend(mode_entries)
                    shared_order_offset = max((entry.order for entry in shared_preseed_entries), default=shared_order_offset - 1) + 10
                    if boot_assets_uuid:
                        signed_kernel_assets.extend(
                            _signed_kernel_assets(
                                mode_entries,
                                str(owner_data["asset_namespace"]),
                                str(owner_data["source_path"]),
                            )
                        )
                        boot_initrd_assets.extend(
                            _boot_initrd_assets(
                                mode_entries,
                                str(owner_data["asset_namespace"]),
                                str(owner_data["source_path"]),
                            )
                        )
                    installer_media_trees.extend(_installer_media_tree_manifest(mode_entries, str(owner_data["source_path"])))
                    payload_boot_assets.extend(
                        _payload_boot_assets_manifest(mode_entries, str(owner_data["source_path"]))
                    )
                    payload_extra_assets.extend(
                        _payload_extra_assets_manifest(
                            str(owner_data["profile"]),
                            str(owner_data["source_role"]),
                            str(owner_data["media_class"]),
                            str(owner_data["source_path"]),
                            str(owner_data["managed_payload_layout"]),
                        )
                    )
                    installer_initrd_patches.extend(_installer_initrd_patch_manifest(mode_entries))
                    installer_boot_initrd_patches.extend(
                        _installer_boot_initrd_patch_assets(
                            mode_entries,
                            str(owner_data["asset_namespace"]),
                            str(owner_data["source_path"]),
                        )
                    )
                if shared_preseed_entries:
                    total_entries += len(shared_preseed_entries)
                    family_entries.setdefault(family_id, []).extend(shared_preseed_entries)
                    shared_order_offset = max((entry.order for entry in shared_preseed_entries), default=shared_order_offset - 1) + 10
                    family_offsets[family_id] = shared_order_offset
        if preserve_upstream_grub_entries:
            for family_id, owner_candidates in family_owner_data.items():
                preserved_order_offset = family_offsets.get(family_id, 0)
                for owner_data in sorted(owner_candidates, key=lambda candidate: int(candidate.get("item_index") or 0)):
                    preserved_entries = build_custom_profile_preserved_entries(
                        profile=str(owner_data["profile"]),
                        source_role=str(owner_data["source_role"]),
                        source_path=str(owner_data["source_path"]),
                        media_class=str(owner_data["media_class"]),
                        rendered_entries=list(owner_data["rendered_entries"]),
                        payload_uuid=str(owner_data["payload_uuid"]),
                        asset_namespace=str(owner_data["asset_namespace"]),
                        order_offset=preserved_order_offset,
                    )
                    if not preserved_entries:
                        continue
                    total_entries += len(preserved_entries)
                    family_entries.setdefault(family_id, []).extend(preserved_entries)
                    if boot_assets_uuid:
                        signed_kernel_assets.extend(
                            _signed_kernel_assets(
                                preserved_entries,
                                str(owner_data["asset_namespace"]),
                                str(owner_data["source_path"]),
                            )
                        )
                        boot_initrd_assets.extend(
                            _boot_initrd_assets(
                                preserved_entries,
                                str(owner_data["asset_namespace"]),
                                str(owner_data["source_path"]),
                            )
                        )
                    installer_media_trees.extend(_installer_media_tree_manifest(preserved_entries, str(owner_data["source_path"])))
                    payload_boot_assets.extend(
                        _payload_boot_assets_manifest(preserved_entries, str(owner_data["source_path"]))
                    )
                    payload_extra_assets.extend(
                        _payload_extra_assets_manifest(
                            str(owner_data["profile"]),
                            str(owner_data["source_role"]),
                            str(owner_data["media_class"]),
                            str(owner_data["source_path"]),
                            str(owner_data["managed_payload_layout"]),
                        )
                    )
                    installer_initrd_patches.extend(_installer_initrd_patch_manifest(preserved_entries))
                    installer_boot_initrd_patches.extend(
                        _installer_boot_initrd_patch_assets(
                            preserved_entries,
                            str(owner_data["asset_namespace"]),
                            str(owner_data["source_path"]),
                        )
                    )
                    preserved_order_offset = max((entry.order for entry in preserved_entries), default=preserved_order_offset - 1) + 10
                family_offsets[family_id] = preserved_order_offset
        for family_id, entries in list(family_entries.items()):
            owner_candidates = family_owner_data.get(family_id, [])
            if not owner_candidates:
                continue
            family_entries[family_id] = _push_family_legacy_entries_to_end(
                entries,
                _preserved_family_menu_title(str(owner_candidates[0]["profile"])),
            )
        grub_cfg = render_custom_main_menu(
            family_entries=family_entries,
            module_lines=module_lines,
            boot_assets_uuid=boot_assets_uuid,
        )
        for family_id in ("debian", "kali", "tails"):
            if family_entries.get(family_id):
                top_level_entries.append({"debian": "Debian", "kali": "Kali", "tails": "Tails"}[family_id])
        return {
            "grub_cfg": grub_cfg,
            "entry_count": total_entries,
            "media_class": "multi-os",
            "media_classes": media_classes,
            "top_level_entries": top_level_entries,
            "signed_kernel_assets": signed_kernel_assets,
            "boot_initrd_assets": boot_initrd_assets,
            "iso_payloads": iso_payloads,
            "installer_media_trees": installer_media_trees,
            "payload_boot_assets": payload_boot_assets,
            "payload_extra_assets": payload_extra_assets,
            "installer_initrd_patches": installer_initrd_patches,
            "installer_boot_initrd_patches": installer_boot_initrd_patches,
        }
    for item in items:
        assert isinstance(item, dict)
        profile = str(item["profile"])
        namespace = str(item.get("id") or profile)
        live_uuid = uuid_map[str(item["id"])]
        rendered_entries, media_class = _render_multios_item_entries(item, config_data, live_uuid)
        media_classes.append(media_class)
        title_text = str(item.get("title") or profile_for(profile).title)
        title = _escape_grub_string(title_text)
        if boot_assets_uuid:
            rendered_entries = _filter_entries_for_secure_boot(rendered_entries)
        total_entries += len(rendered_entries)
        if boot_assets_uuid:
            signed_kernel_assets.extend(_signed_kernel_assets(rendered_entries, namespace, str(item["iso_path"])))
            boot_initrd_assets.extend(_boot_initrd_assets(rendered_entries, namespace, str(item["iso_path"])))
        iso_payloads.extend(
            _iso_payload_manifest(
                profile,
                str(item.get("source_role") or SOURCE_ROLE_PRIMARY),
                media_class,
                str(item["iso_path"]),
                str(item.get("payload_iso_name") or ""),
                str(item.get("managed_payload_layout") or ""),
            )
        )
        installer_media_trees.extend(_installer_media_tree_manifest(rendered_entries, str(item["iso_path"])))
        payload_boot_assets.extend(_payload_boot_assets_manifest(rendered_entries, str(item["iso_path"])))
        payload_extra_assets.extend(
            _payload_extra_assets_manifest(
                profile,
                str(item.get("source_role") or SOURCE_ROLE_PRIMARY),
                media_class,
                str(item["iso_path"]),
                str(item.get("managed_payload_layout") or ""),
            )
        )
        installer_initrd_patches.extend(_installer_initrd_patch_manifest(rendered_entries))
        installer_boot_initrd_patches.extend(_installer_boot_initrd_patch_assets(rendered_entries, namespace, str(item["iso_path"])))
        top_level_entries.append(title)
        lines.append(f'submenu "{title}" {{')
        lines.extend(
            _render_grub_menu(
                rendered_entries,
                live_uuid,
                indent=4,
                boot_assets_uuid=boot_assets_uuid,
                asset_namespace=namespace,
            )
        )
        lines.append("}")
        lines.append("")
    if boot_assets_uuid:
        lines.extend(_render_mok_menu_entry(boot_assets_uuid, _configured_static_entry_title("mok", "MOK Enrollment")))
        if _updatevars_efi_available():
            lines.append("")
            updatevars_title, user_title, setup_title = _configured_updatevars_menu_labels()
            lines.extend(_render_updatevars_menu_entry(boot_assets_uuid, updatevars_title, user_title, setup_title))
            lines.append("")
    return {
        "grub_cfg": "\n".join(lines).rstrip() + "\n",
        "entry_count": total_entries,
        "media_class": "multi-os",
        "media_classes": media_classes,
        "top_level_entries": top_level_entries,
        "signed_kernel_assets": signed_kernel_assets,
        "boot_initrd_assets": boot_initrd_assets,
        "iso_payloads": iso_payloads,
        "installer_media_trees": installer_media_trees,
        "payload_boot_assets": payload_boot_assets,
        "payload_extra_assets": payload_extra_assets,
        "installer_initrd_patches": installer_initrd_patches,
        "installer_boot_initrd_patches": installer_boot_initrd_patches,
    }


def _prepare_multios_item_entries(
    item: dict[str, object],
    config_data: dict[str, str],
    live_uuid: str,
) -> tuple[list[BootEntry], list[BootEntry], str, bool]:
    profile = str(item["profile"])
    source_role = str(item.get("source_role") or SOURCE_ROLE_PRIMARY)
    payload_layout = effective_managed_payload_layout(
        profile,
        str(item.get("managed_payload_layout") or ""),
        config_data,
        False,
    )
    if source_role in {SOURCE_ROLE_NETINST, SOURCE_ROLE_NETBOOT} and payload_layout not in {
        MANAGED_PAYLOAD_LAYOUT_ISO_STORE,
        MANAGED_PAYLOAD_LAYOUT_SHARED_DATA,
    }:
        raise ValueError(f"{source_role} source requires an ISO-store payload layout for {profile}")
    item_config_data = _config_for_payload_layout(config_data, profile, payload_layout)
    source = open_source(str(item["iso_path"]))
    item_config_data[_LIVE_TORAM_MODULE_CONFIG_KEY] = _live_toram_module_for_source(source, profile)
    entries = _filter_entries_for_profile(profile, _find_boot_entries(source))
    if "uefi" not in _detect_firmware(source):
        raise ValueError(f"managed mode requires UEFI-capable media: {source.display_path}")
    if not entries:
        raise ValueError(f"no managed boot entries were detected in {source.display_path}")
    entries = _filter_entries_for_source_role(profile, source_role, entries)
    if source_role in {SOURCE_ROLE_NETINST, SOURCE_ROLE_NETBOOT} and not entries:
        raise ValueError(f"{source_role} source has no installer boot entries in {source.display_path}")
    media_class = _media_class(entries)
    profile_meta = profile_for(profile)
    persistence_mode = str(item.get("persistence_mode") or PERSISTENCE_MODE_NONE)
    live_toram = bool(item.get("live_toram", False))
    supports_encrypted_live = _supports_encrypted_persistence(source, entries, profile, profile_meta)
    if persistence_mode == PERSISTENCE_MODE_ENCRYPTED and not supports_encrypted_live:
        raise ValueError(f"encrypted persistence is not supported for profile {profile} with {source.display_path}")
    if profile == PROFILE_TAILS:
        # Stock Tails entries were normalized to nonpersistent Live entries
        # by _filter_entries_for_profile; retain their upstream boot parameters.
        pass
    elif persistence_mode == PERSISTENCE_MODE_PLAIN:
        entries = [entry for entry in entries if entry.kind != "live-encrypted-persistence"]
    elif persistence_mode == PERSISTENCE_MODE_ENCRYPTED:
        entries = [entry for entry in entries if entry.kind != "live-persistence"]
    else:
        entries = [entry for entry in entries if entry.kind not in {"live-persistence", "live-encrypted-persistence"}]
    live_entries = [entry for entry in entries if entry.kind.startswith("live")]
    best_live = _select_entry(profile, live_entries)
    persistence_label = str(item.get("persistence_fs_label") or "")
    rendered_entries = [
        _adapt_entry_for_managed(
            entry,
            profile,
            live_uuid,
            item_config_data,
            persistence_mode,
            payload_layout,
            persistence_label,
            live_toram=live_toram,
        )
        for entry in entries
    ]
    synthetic_entries: list[BootEntry] = []
    earliest_order = min(entry.order for entry in rendered_entries)
    kernel_args_override = str(item.get("kernel_args") or "")
    kernel_path_override = str(item.get("kernel_path") or "")
    initrd_path_override = str(item.get("initrd_path") or "")
    menu_label_override = str(item.get("menu_label") or "")
    if profile_meta.preferred_media == "live" and media_class in {"live", "hybrid"}:
        has_override = any((kernel_args_override, kernel_path_override, initrd_path_override, menu_label_override))
        if has_override and best_live is None and not kernel_path_override:
            raise ValueError(f"unable to detect a live boot entry in {source.display_path}; supply a kernel override")
        if has_override:
            synthetic_entries.append(
                _synthetic_live_entry(
                    source=source,
                    profile=profile,
                    config_data=item_config_data,
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
            if persistence_mode in {PERSISTENCE_MODE_PLAIN, PERSISTENCE_MODE_ENCRYPTED} and profile_meta.supports_persistence:
                synthetic_entries.append(
                    _synthetic_live_entry(
                        source=source,
                        profile=profile,
                        config_data=item_config_data,
                        live_uuid=live_uuid,
                        persistence_mode=persistence_mode,
                        template=best_live,
                        kernel_args_override=kernel_args_override,
                        kernel_path_override=kernel_path_override,
                        initrd_path_override=initrd_path_override,
                        menu_label_override=menu_label_override,
                        order=earliest_order - 19,
                        persistence_label=persistence_label,
                        live_toram=live_toram,
                    )
                )
        elif persistence_mode in {PERSISTENCE_MODE_PLAIN, PERSISTENCE_MODE_ENCRYPTED} and profile_meta.supports_persistence and best_live is not None and not any(
            entry.kind == ("live-persistence" if persistence_mode == PERSISTENCE_MODE_PLAIN else "live-encrypted-persistence")
            for entry in rendered_entries
        ):
            synthetic_entries.append(
                _synthetic_live_entry(
                    source=source,
                    profile=profile,
                    config_data=item_config_data,
                    live_uuid=live_uuid,
                    persistence_mode=persistence_mode,
                    template=best_live,
                    kernel_args_override="",
                    kernel_path_override="",
                    initrd_path_override="",
                    menu_label_override="",
                    order=best_live.order + 1,
                    persistence_label=persistence_label,
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
        payload_iso_name=str(item.get("payload_iso_name") or ""),
        asset_namespace=profile,
    )
    return entries, rendered_entries, media_class, supports_encrypted_live


def _render_multios_item_entries(
    item: dict[str, object],
    config_data: dict[str, str],
    live_uuid: str,
) -> tuple[list[BootEntry], str]:
    _, rendered_entries, media_class, _ = _prepare_multios_item_entries(item, config_data, live_uuid)
    return rendered_entries, media_class


def payload_uuid_args_to_map(values: list[str]) -> dict[str, str]:
    uuid_map: dict[str, str] = {}
    for value in values:
        item_id, separator, uuid = value.partition("=")
        if not separator or not item_id.strip() or not uuid.strip():
            raise ValueError(f"payload UUID values must use <id>=<uuid>: {value}")
        uuid_map[item_id.strip()] = uuid.strip()
    return uuid_map


def _validate_payload_uuids(items: list[object], payload_uuids: dict[str, str]) -> dict[str, str]:
    expected = {str(item["id"]) for item in items if isinstance(item, dict)}
    missing = sorted(expected - set(payload_uuids))
    extra = sorted(set(payload_uuids) - expected)
    if missing:
        raise ValueError(f"missing payload UUIDs for: {', '.join(missing)}")
    if extra:
        raise ValueError(f"unexpected payload UUIDs for: {', '.join(extra)}")
    for item_id, uuid in payload_uuids.items():
        if not uuid or any(character.isspace() for character in uuid):
            raise ValueError(f"invalid payload UUID for {item_id}: {uuid}")
    return dict(payload_uuids)


def _required_string(item: dict[str, object], key: str, index: int) -> str:
    value = item.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Multi-OS item {index} requires non-empty {key}")
    return value.strip()


def _optional_string(item: dict[str, object], key: str, default: str) -> str:
    value = item.get(key, default)
    if value is None:
        return default
    if not isinstance(value, str):
        raise ValueError(f"Multi-OS {key} must be a string")
    return value.strip() or default


def _inspection_has_installer(inspection: dict[str, object]) -> bool:
    media_class = str(inspection.get("media_class") or "").strip()
    best_installer = str(inspection.get("best_installer_title") or "").strip()
    return media_class in {"installer", "hybrid"} or bool(best_installer)


def _validate_label(value: str, key: str, *, max_length: int = 16) -> None:
    if not re.fullmatch(rf"[!-~]{{1,{max_length}}}", value):
        raise ValueError(f"{key} must be 1-{max_length} printable ASCII characters without whitespace: {value}")
