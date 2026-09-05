from __future__ import annotations

from dataclasses import dataclass, replace
import re

from .catalog import profile_for
from .iso_source import _normalize_member_path, _preprocess_lines, DirectorySource, IsoSource


@dataclass(frozen=True)
class BootEntry:
    title: str
    kernel_path: str
    initrd_path: str
    kernel_args: str
    source: str
    kind: str = "other"
    menu_path: tuple[str, ...] = ()
    order: int = 0
    platform: str = "all"
    commands: tuple[str, ...] = ()
    payload_uuid: str = ""
    asset_namespace: str = ""
    isofile_path: str = ""
    boot_kernel_path: str = ""
    boot_initrd_path: str = ""
    installer_media_path: str = ""
    boot_method: str = ""


def _current_platform(base_platform: str, if_stack: list[tuple[bool, str | None]]) -> str:
    if base_platform == "efi":
        return "efi"
    if any(platform == "efi" for _, platform in if_stack):
        return "efi"
    return "all"


def _is_false_if(line: str) -> bool:
    return bool(re.match(r"^if\s+false\s*;\s*then$", line))


def _is_efi_if(line: str) -> bool:
    return "grub_platform" in line and "efi" in line and line.startswith("if ")


def _extract_grub_title(line: str, keyword: str) -> str | None:
    pattern = rf"^{keyword}\b(.*)$"
    match = re.search(pattern, line)
    if match:
        remainder = match.group(1)
        title_match = re.search(r"['\"]([^'\"]+)['\"]", remainder)
        if title_match:
            return title_match.group(1)
    return None


def _grub_assignment_value(value: str) -> str:
    normalized = value.strip()
    if len(normalized) >= 2 and normalized[0] == normalized[-1] and normalized[0] in {"'", '"'}:
        return normalized[1:-1]
    return normalized


def _resolve_grub_variable(value: str, variables: dict[str, str]) -> str:
    normalized = value.strip()
    match = re.fullmatch(r"\$(?:\{([A-Za-z_][A-Za-z0-9_]*)\}|([A-Za-z_][A-Za-z0-9_]*))", normalized)
    if not match:
        return normalized
    return variables.get(match.group(1) or match.group(2), normalized)


def _expand_isofile_argument(value: str, variables: dict[str, str]) -> str:
    isofile = variables.get("isofile", "")
    if not isofile:
        return value
    return value.replace("${isofile}", isofile).replace("$isofile", isofile)


def _annotate_entry(entry: BootEntry) -> BootEntry:
    title = entry.title.lower()
    args = entry.kernel_args.lower()
    kernel_path = entry.kernel_path.lower()
    commands = " ".join(entry.commands).lower()
    kind = "utility"

    if "verify-checksums" in args or "integrity of the boot medium" in title:
        kind = "verify"
    elif "fwsetup" in commands or "firmware settings" in title:
        kind = "firmware"
    elif "boot=casper" in args or "boot=live" in args or "/casper/" in kernel_path or "/live/" in kernel_path:
        kind = "live"
        if any(token in args for token in ("persistence-encryption=luks", "persistent=cryptsetup")):
            kind = "live-encrypted-persistence"
        elif any(token in title or token in args for token in ("persistence", "persistent")):
            kind = "live-persistence"
        elif any(token in title or token in args for token in ("fail-safe", "failsafe", "safe graphics", "memtest")):
            kind = "live-failsafe"
        elif any(token in title or token in args for token in ("forensics", "forensic")):
            kind = "live-forensics"
    elif any(fragment in kernel_path for fragment in ("/install", "/d-i/", "/debian-installer/", "/hd-media/", "/netboot/")) or any(
        token in title for token in ("install", "installer", "rescue")
    ):
        kind = "installer"
        if "rescue" in title or "rescue/enable=true" in args:
            kind = "rescue"
        elif "expert" in title or "priority=low" in args:
            kind = "expert-installer"
        elif "auto=true" in args or "automated" in title:
            kind = "automated-installer"

    return replace(entry, kind=kind)


def parse_grub_entries(
    text: str,
    source: str,
    *,
    include_loader: callable | None = None,
    menu_path: tuple[str, ...] = (),
    visited: set[str] | None = None,
    counter: list[int] | None = None,
    platform: str = "all",
) -> list[BootEntry]:
    entries: list[BootEntry] = []
    normalized_source = _normalize_member_path(source)
    visited = set() if visited is None else visited
    counter = [0] if counter is None else counter
    visited.add(normalized_source)

    block_stack: list[str] = []
    submenu_stack = list(menu_path)
    if_stack: list[tuple[bool, str | None]] = []
    current: dict[str, object] | None = None

    for line in _preprocess_lines(text):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue

        if re.match(r"^if\s+.*;\s*then$", stripped):
            if_stack.append((_is_false_if(stripped), "efi" if _is_efi_if(stripped) else None))
            continue

        if stripped == "fi":
            if if_stack:
                if_stack.pop()
            continue

        if any(skip for skip, _ in if_stack):
            continue

        if current is None and stripped.startswith("source "):
            include_path = _normalize_member_path(stripped.split(None, 1)[1])
            if include_loader is not None and include_path and include_path not in visited:
                include_text = include_loader(include_path)
                if include_text:
                    entries.extend(
                        parse_grub_entries(
                            include_text,
                            include_path,
                            include_loader=include_loader,
                            menu_path=tuple(submenu_stack),
                            visited=visited,
                            counter=counter,
                            platform=_current_platform(platform, if_stack),
                        )
                    )
            continue

        if stripped.startswith("submenu "):
            title = _extract_grub_title(stripped, "submenu")
            if title is None:
                continue
            submenu_stack.append(title)
            block_stack.append("submenu")
            continue

        if stripped.startswith("menuentry "):
            title = _extract_grub_title(stripped, "menuentry")
            if title is None:
                continue
            current = {
                "title": title,
                "kernel_path": "",
                "initrd_path": "",
                "kernel_args": "",
                "commands": [],
                "menu_path": tuple(submenu_stack),
                "platform": _current_platform(platform, if_stack),
                "source": normalized_source.lstrip("/"),
                "order": counter[0],
                "variables": {},
                "boot_method": "",
            }
            counter[0] += 1
            block_stack.append("menuentry")
            continue

        if stripped == "}":
            if not block_stack:
                continue
            block_kind = block_stack.pop()
            if block_kind == "submenu":
                if submenu_stack:
                    submenu_stack.pop()
                continue
            if block_kind == "menuentry" and current is not None:
                entry = BootEntry(
                    title=str(current["title"]),
                    kernel_path=_normalize_member_path(str(current["kernel_path"])),
                    initrd_path=_normalize_member_path(str(current["initrd_path"])),
                    kernel_args=" ".join(str(current["kernel_args"]).strip().split()),
                    source=str(current["source"]),
                    menu_path=tuple(current["menu_path"]),
                    order=int(current["order"]),
                    platform=str(current["platform"]),
                    commands=tuple(str(command) for command in current["commands"]),
                    isofile_path=str(current["variables"].get("isofile", "")),
                    boot_method=str(current["boot_method"]),
                )
                if entry.kernel_path or entry.commands:
                    entries.append(_annotate_entry(entry))
                current = None
            continue

        if current is None:
            continue

        assignment_match = re.match(r"^set\s+([A-Za-z_][A-Za-z0-9_]*)=(.*)$", stripped)
        if assignment_match:
            variables = dict(current["variables"])
            variables[assignment_match.group(1)] = _grub_assignment_value(assignment_match.group(2))
            current["variables"] = variables
            continue

        if re.match(r"^loopback\s+loop\s+\$\{?isofile\}?$", stripped):
            current["boot_method"] = "iso-loopback"
            continue

        if re.match(r"^linux(?:efi)?\s+", stripped):
            parts = stripped.split(None, 2)
            if len(parts) >= 2:
                kernel_path = _resolve_grub_variable(parts[1], dict(current["variables"]))
                if kernel_path.startswith("(loop)"):
                    kernel_path = kernel_path[len("(loop)") :]
                    current["boot_method"] = "iso-loopback"
                current["kernel_path"] = kernel_path
            current["kernel_args"] = (
                _expand_isofile_argument(parts[2], dict(current["variables"])) if len(parts) == 3 else ""
            )
            continue

        if re.match(r"^initrd(?:efi)?\s+", stripped):
            parts = stripped.split(None, 1)
            if len(parts) == 2:
                initrd_path = _resolve_grub_variable(parts[1], dict(current["variables"]))
                if initrd_path.startswith("(loop)"):
                    initrd_path = initrd_path[len("(loop)") :]
                    current["boot_method"] = "iso-loopback"
                current["initrd_path"] = initrd_path
            continue

        commands = list(current["commands"])
        commands.append(stripped)
        current["commands"] = commands

    if current is not None:
        entry = BootEntry(
            title=str(current["title"]),
            kernel_path=_normalize_member_path(str(current["kernel_path"])),
            initrd_path=_normalize_member_path(str(current["initrd_path"])),
            kernel_args=" ".join(str(current["kernel_args"]).strip().split()),
            source=str(current["source"]),
            menu_path=tuple(current["menu_path"]),
            order=int(current["order"]),
            platform=str(current["platform"]),
            commands=tuple(str(command) for command in current["commands"]),
            isofile_path=str(current["variables"].get("isofile", "")),
            boot_method=str(current["boot_method"]),
        )
        if entry.kernel_path or entry.commands:
            entries.append(_annotate_entry(entry))

    return entries


def parse_syslinux_entries(text: str, source: str, *, counter: list[int] | None = None) -> list[BootEntry]:
    entries: list[BootEntry] = []
    current_title = ""
    current_kernel = ""
    current_args = ""
    current_initrd = ""
    menu_stack: list[str] = []
    counter = [0] if counter is None else counter

    def flush_entry() -> None:
        nonlocal current_title, current_kernel, current_args, current_initrd
        if not current_kernel:
            return
        entry = BootEntry(
            title=current_title,
            kernel_path=_normalize_member_path(current_kernel),
            initrd_path=_normalize_member_path(current_initrd),
            kernel_args=" ".join(current_args.strip().split()),
            source=_normalize_member_path(source).lstrip("/"),
            menu_path=tuple(menu_stack),
            order=counter[0],
        )
        counter[0] += 1
        if entry.kernel_path:
            entries.append(_annotate_entry(entry))
        current_title = ""
        current_kernel = ""
        current_args = ""
        current_initrd = ""

    for raw_line in text.splitlines():
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue

        lower = stripped.lower()
        if lower.startswith("menu begin "):
            menu_stack.append(stripped.split(None, 2)[2].strip())
            continue
        if lower == "menu end":
            if menu_stack:
                menu_stack.pop()
            continue
        if lower.startswith("label "):
            flush_entry()
            current_title = stripped.split(None, 1)[1].strip()
            continue
        if lower.startswith("menu label "):
            current_title = stripped.split(None, 2)[2].strip("^")
            continue
        if lower.startswith(("linux ", "kernel ")):
            current_kernel = stripped.split(None, 1)[1]
            continue
        if lower.startswith("initrd "):
            current_initrd = stripped.split(None, 1)[1]
            continue
        if lower.startswith("append "):
            append_payload = stripped.split(None, 1)[1]
            parts = append_payload.split()
            initrd_arg = next((part for part in parts if part.startswith("initrd=")), "")
            if initrd_arg:
                current_initrd = initrd_arg.split("=", 1)[1]
                parts = [part for part in parts if not part.startswith("initrd=")]
            current_args = " ".join(" ".join(parts).strip().split())

    flush_entry()
    return entries


def _candidate_config_paths() -> list[str]:
    return [
        "/boot/grub/grub.cfg",
        "/boot/grub/loopback.cfg",
        "/boot/grub/install_start.cfg",
        "/boot/grub/install.cfg",
        "/EFI/BOOT/grub.cfg",
        "/isolinux/txt.cfg",
        "/isolinux/live.cfg",
        "/isolinux/install.cfg",
        "/syslinux/live.cfg",
        "/syslinux.cfg",
    ]


def _dedupe_entries(entries: list[BootEntry]) -> list[BootEntry]:
    deduped: list[BootEntry] = []
    seen: set[tuple[object, ...]] = set()
    for entry in sorted(entries, key=lambda item: item.order):
        key = (
            entry.title,
            entry.kernel_path,
            entry.initrd_path,
            entry.kernel_args,
            entry.menu_path,
            entry.kind,
            entry.commands,
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(entry)
    return deduped


def _find_boot_entries(source: DirectorySource | IsoSource) -> list[BootEntry]:
    grub_entries: list[BootEntry] = []
    syslinux_entries: list[BootEntry] = []
    visited: set[str] = set()
    counter = [0]
    for candidate in _candidate_config_paths():
        normalized = _normalize_member_path(candidate)
        if normalized in visited or not source.exists(normalized):
            continue
        text = source.read_text(normalized)
        if "grub" in normalized:
            grub_entries.extend(
                parse_grub_entries(
                    text,
                    normalized,
                    include_loader=lambda member_path: source.read_text(member_path) if source.exists(member_path) else None,
                    visited=visited,
                    counter=counter,
                )
            )
        else:
            visited.add(normalized)
            syslinux_entries.extend(parse_syslinux_entries(text, normalized, counter=counter))
    if grub_entries:
        return _dedupe_entries(grub_entries)
    return _dedupe_entries(syslinux_entries)


def _entry_score(profile: str, entry: BootEntry) -> int:
    title = entry.title.lower()
    args = entry.kernel_args.lower()
    kernel_path = entry.kernel_path.lower()
    score = 0

    if entry.kind in {"live", "live-persistence", "live-encrypted-persistence", "live-failsafe", "live-forensics"}:
        score += 40
    if entry.kind == "live-persistence":
        score -= 30
    if entry.kind == "live-encrypted-persistence":
        score -= 40
    if entry.kind == "live-forensics":
        score -= 15
    if entry.kind == "live-failsafe":
        score -= 25
    profile_meta = profile_for(profile)
    if profile_meta.live_boot_family == "casper":
        if "boot=casper" in args:
            score += 50
        if "/casper/" in kernel_path:
            score += 25
        if "persistent" in args or "persistence" in title:
            score -= 40
        if any(word in title for word in ("safe graphics", "install", "oem", "check disc", "memtest")):
            score -= 30
        if any(word in title for word in ("try", "live")):
            score += 10
    elif profile_meta.live_boot_family == "live-boot":
        if "boot=live" in args:
            score += 50
        if "/live/" in kernel_path:
            score += 25
        if any(word in title for word in ("fail-safe", "failsafe")):
            score -= 20
        if any(word in title for word in ("install", "rescue")):
            score -= 30
    return score


def _select_entry(profile: str, entries: list[BootEntry]) -> BootEntry | None:
    candidates = [entry for entry in entries if entry.kind.startswith("live")]
    if not candidates:
        return None
    ranked = sorted(candidates, key=lambda entry: (_entry_score(profile, entry), -entry.order), reverse=True)
    if _entry_score(profile, ranked[0]) <= 0:
        return None
    return ranked[0]


def _installer_entry_score(profile: str, entry: BootEntry) -> int:
    del profile
    title = entry.title.lower()
    args = entry.kernel_args.lower()
    kernel_path = entry.kernel_path.lower()
    score = 0
    if entry.kind in {"installer", "automated-installer", "expert-installer", "rescue"}:
        score += 40
    if any(word in title for word in ("install", "installer")):
        score += 50
    if any(fragment in kernel_path for fragment in ("/install", "/d-i/", "/debian-installer/")):
        score += 50
    if "auto=true" in args or "automated" in title:
        score += 10
    if "rescue" in title or "rescue/enable=true" in args:
        score -= 25
    if "expert" in title or "priority=low" in args:
        score -= 10
    if any(word in title for word in ("live", "failsafe", "forensics", "persistence")):
        score -= 30
    return score


def _select_installer_entry(profile: str, entries: list[BootEntry]) -> BootEntry | None:
    candidates = [entry for entry in entries if entry.kind in {"installer", "automated-installer", "expert-installer"}]
    if not candidates:
        return None
    ranked = sorted(candidates, key=lambda entry: (_installer_entry_score(profile, entry), -entry.order), reverse=True)
    if _installer_entry_score(profile, ranked[0]) <= 0:
        return None
    return ranked[0]


def _select_base_installer_entry(profile: str, entries: list[BootEntry]) -> BootEntry | None:
    plain = [entry for entry in entries if entry.kind == "installer"]
    if plain:
        return _select_installer_entry(profile, plain) or plain[0]
    return _select_installer_entry(profile, entries)


def _select_text_installer_entry(profile: str, entries: list[BootEntry]) -> BootEntry | None:
    candidates = [
        entry
        for entry in entries
        if entry.kind in {"installer", "automated-installer", "expert-installer"}
        and "graphical" not in entry.title.lower()
        and "/gtk/" not in entry.kernel_path.lower()
        and "/gtk/" not in entry.initrd_path.lower()
    ]
    if candidates:
        return _select_installer_entry(profile, candidates) or candidates[0]
    return _select_base_installer_entry(profile, entries)


def _media_class(entries: list[BootEntry]) -> str:
    has_live = any(entry.kind.startswith("live") for entry in entries)
    has_installer = any(
        entry.kind in {"installer", "automated-installer", "expert-installer", "rescue"} for entry in entries
    )
    if has_live and has_installer:
        return "hybrid"
    if has_live:
        return "live"
    if has_installer:
        return "installer"
    return "utility"
