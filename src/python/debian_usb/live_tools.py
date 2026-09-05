from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
from typing import Any


LIVE_TOOL_PROFILE_SCHEMA_VERSION = 2
LIVE_TOOL_PROFILE_ENV = "DEBIAN_USB_LIVE_TOOL_PROFILE"
PACKAGE_RE = re.compile(r"^[a-z0-9][a-z0-9+.-]*$")
PROFILE_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
GROUP_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
COMMAND_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9+._-]*$")


def live_tool_profile_path() -> Path:
    override = os.environ.get(LIVE_TOOL_PROFILE_ENV, "").strip()
    if override:
        candidate = Path(override).expanduser()
        if not candidate.is_absolute():
            raise ValueError(f"{LIVE_TOOL_PROFILE_ENV} must be an absolute path: {override}")
        if not candidate.is_file():
            raise ValueError(f"{LIVE_TOOL_PROFILE_ENV} is not a regular file: {candidate}")
        return candidate.resolve()

    module_path = Path(__file__).resolve()
    candidates = [
        module_path.parents[2] / "spec" / "live" / "admin-tools.json",
        module_path.parents[3] / "configs" / "spec" / "live" / "admin-tools.json",
        Path("/usr/lib/debian-usb/spec/live/admin-tools.json"),
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    raise RuntimeError("could not locate the Debian-family Live administration tool profile")


def load_live_tool_profile(profile_path: str | Path | None = None) -> dict[str, Any]:
    path = Path(profile_path).expanduser() if profile_path is not None else live_tool_profile_path()
    if not path.is_absolute():
        raise ValueError(f"Live tool profile path must be absolute: {path}")
    if not path.is_file():
        raise ValueError(f"Live tool profile path is not a regular file: {path}")
    payload_bytes = path.read_bytes()
    payload = json.loads(payload_bytes.decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("Live tool profile must be a JSON object")
    schema_version = int(payload.get("schema_version") or 0)
    if schema_version != LIVE_TOOL_PROFILE_SCHEMA_VERSION:
        raise ValueError(f"Live tool profile schema_version must be {LIVE_TOOL_PROFILE_SCHEMA_VERSION}")

    supported_profiles = _validate_token_list(payload.get("supported_profiles"), "supported_profiles", PROFILE_RE)
    if not supported_profiles:
        raise ValueError("Live tool profile supported_profiles must not be empty")

    raw_build_distros = payload.get("build_distros")
    if not isinstance(raw_build_distros, dict):
        raise ValueError("Live tool profile build_distros must be a JSON object")
    build_distros: dict[str, str] = {}
    for raw_distro, raw_profile in raw_build_distros.items():
        distro = str(raw_distro).strip()
        profile = str(raw_profile).strip()
        if not PROFILE_RE.fullmatch(distro):
            raise ValueError(f"invalid Live tool build distro: {distro}")
        if profile not in supported_profiles:
            raise ValueError(f"Live tool build distro {distro} references unsupported profile: {profile}")
        build_distros[distro] = profile

    raw_groups = payload.get("package_groups")
    if not isinstance(raw_groups, list) or not raw_groups:
        raise ValueError("Live tool profile package_groups must be a non-empty JSON array")
    package_groups: dict[str, list[str]] = {}
    package_group_options: list[dict[str, Any]] = []
    packages: list[str] = []
    seen_packages: set[str] = set()
    for index, raw_group in enumerate(raw_groups, start=1):
        if not isinstance(raw_group, dict):
            raise ValueError(f"Live tool profile package_groups entry {index} must be a JSON object")
        group_id = str(raw_group.get("id") or "").strip()
        if not GROUP_RE.fullmatch(group_id):
            raise ValueError(f"invalid Live tool package group id: {group_id}")
        if group_id in package_groups:
            raise ValueError(f"duplicate Live tool package group id: {group_id}")
        title = str(raw_group.get("title") or "").strip()
        description = str(raw_group.get("description") or "").strip()
        if not title:
            raise ValueError(f"Live tool package group title must not be empty: {group_id}")
        if not description:
            raise ValueError(f"Live tool package group description must not be empty: {group_id}")
        group_packages = _validate_token_list(
            raw_group.get("packages"),
            f"package_groups.{group_id}.packages",
            PACKAGE_RE,
        )
        if not group_packages:
            raise ValueError(f"Live tool package group must not be empty: {group_id}")
        package_groups[group_id] = group_packages
        package_group_options.append(
            {
                "id": group_id,
                "title": title,
                "description": description,
                "packages": group_packages,
            }
        )
        for package in group_packages:
            if package in seen_packages:
                continue
            seen_packages.add(package)
            packages.append(package)

    raw_command_packages = payload.get("command_packages")
    if not isinstance(raw_command_packages, dict) or not raw_command_packages:
        raise ValueError("Live tool profile command_packages must be a non-empty JSON object")
    command_packages: dict[str, str] = {}
    for raw_command, raw_package in raw_command_packages.items():
        command = str(raw_command).strip()
        package = str(raw_package).strip()
        if not COMMAND_RE.fullmatch(command):
            raise ValueError(f"invalid command name in Live tool profile: {command}")
        if package not in seen_packages:
            raise ValueError(f"Live tool command {command} references a package outside package_groups: {package}")
        command_packages[command] = package

    notes = _validate_string_list(payload.get("notes"), "notes", optional=True)
    return {
        "schema_version": schema_version,
        "path": str(path.resolve()),
        "sha256": hashlib.sha256(payload_bytes).hexdigest(),
        "name": str(payload.get("name") or "").strip() or path.stem,
        "description": str(payload.get("description") or "").strip(),
        "supported_profiles": supported_profiles,
        "build_distros": build_distros,
        "package_groups": package_groups,
        "package_group_options": package_group_options,
        "packages": packages,
        "command_packages": command_packages,
        "notes": notes,
    }


def live_tool_packages_for_profile(
    profile: str,
    profile_path: str | Path | None = None,
    selected_groups: list[str] | None = None,
) -> tuple[list[str], dict[str, Any]]:
    normalized_profile = str(profile or "").strip()
    live_tool_profile = load_live_tool_profile(profile_path)
    if normalized_profile not in live_tool_profile["supported_profiles"]:
        raise ValueError(f"Live administration tool remaster is not supported for profile: {normalized_profile}")
    return _packages_for_selected_groups(live_tool_profile, selected_groups)


def live_tool_packages_for_build_distro(
    distro: str,
    profile_path: str | Path | None = None,
    selected_groups: list[str] | None = None,
) -> tuple[list[str], dict[str, Any]]:
    normalized_distro = str(distro or "").strip()
    live_tool_profile = load_live_tool_profile(profile_path)
    profile = live_tool_profile["build_distros"].get(normalized_distro)
    if not profile:
        selected_profile = dict(live_tool_profile)
        selected_profile["selected_groups"] = []
        return [], selected_profile
    return _packages_for_selected_groups(live_tool_profile, selected_groups)


def _packages_for_selected_groups(
    live_tool_profile: dict[str, Any],
    selected_groups: list[str] | None,
) -> tuple[list[str], dict[str, Any]]:
    group_options = live_tool_profile["package_group_options"]
    available_group_ids = [str(option["id"]) for option in group_options]
    if selected_groups is None:
        normalized_groups = available_group_ids
    else:
        requested_groups = _validate_token_list(selected_groups, "selected_groups", GROUP_RE)
        unknown_groups = [group_id for group_id in requested_groups if group_id not in live_tool_profile["package_groups"]]
        if unknown_groups:
            raise ValueError("unsupported Live tool package groups: " + ", ".join(unknown_groups))
        requested_set = set(requested_groups)
        normalized_groups = [group_id for group_id in available_group_ids if group_id in requested_set]

    packages: list[str] = []
    seen_packages: set[str] = set()
    for group_id in normalized_groups:
        for package in live_tool_profile["package_groups"][group_id]:
            if package in seen_packages:
                continue
            seen_packages.add(package)
            packages.append(package)

    selected_profile = dict(live_tool_profile)
    selected_profile["selected_groups"] = normalized_groups
    return packages, selected_profile


def _validate_token_list(value: Any, label: str, pattern: re.Pattern[str]) -> list[str]:
    values = _validate_string_list(value, label)
    normalized: list[str] = []
    seen: set[str] = set()
    for token in values:
        if not pattern.fullmatch(token):
            raise ValueError(f"invalid {label} entry: {token}")
        if token in seen:
            continue
        seen.add(token)
        normalized.append(token)
    return normalized


def _validate_string_list(value: Any, label: str, *, optional: bool = False) -> list[str]:
    if value in (None, "") and optional:
        return []
    if not isinstance(value, list):
        raise ValueError(f"Live tool profile {label} must be a JSON array")
    normalized: list[str] = []
    for raw_value in value:
        token = str(raw_value).strip()
        if not token:
            raise ValueError(f"Live tool profile {label} must not contain empty values")
        normalized.append(token)
    return normalized
