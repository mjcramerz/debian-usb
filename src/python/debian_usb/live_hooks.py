from __future__ import annotations

import os
from pathlib import Path
import shutil


LIVE_HOOKS_DIR_ENV = "DEBIAN_USB_LIVE_HOOKS_DIR"
LIVE_CONFIG_HOOK_FILENAMES = (
    "0500-apt-live-medium.sh",
    "1000-network-wifi.sh",
)
LIVE_SYSTEMD_MASK_UNITS = (
    "fwupd-refresh.service",
    "fwupd-refresh.timer",
)
LIVE_SYSTEMD_DISABLE_LINKS = (
    "etc/systemd/system/timers.target.wants/fwupd-refresh.timer",
)
DEBIAN_LIVE_LOCALE = "en_US.UTF-8"
DEBIAN_LIVE_LANGUAGE = "en_US:en"
MAX_LIVE_ROOT_POLICY_SYMLINKS = 40

# These packages are not optional administration tools. They are the runtime
# contract for the Debian-only medium hooks and are installed even when the
# user explicitly selects no optional Live tool groups.
DEBIAN_LIVE_HOOK_PACKAGES = (
    "ca-certificates",
    "debian-archive-keyring",
    "live-config",
    "live-config-systemd",
    "locales",
    "iproute2",
    "iw",
    "wpasupplicant",
    "dhcpcd-base",
    "rfkill",
    "wireless-regdb",
    "firmware-iwlwifi",
    "firmware-atheros",
    "firmware-realtek",
    "firmware-brcm80211",
    "firmware-mediatek",
    "firmware-libertas",
)


def live_config_hooks_dir() -> Path:
    override = os.environ.get(LIVE_HOOKS_DIR_ENV, "").strip()
    if override:
        candidate = Path(override).expanduser()
        if not candidate.is_absolute():
            raise ValueError(f"{LIVE_HOOKS_DIR_ENV} must be an absolute path: {override}")
        return _validate_hooks_dir(candidate)

    module_path = Path(__file__).resolve()
    candidates = [
        module_path.parents[2] / "config-hooks",
        module_path.parents[3] / "config-hooks",
        Path("/usr/lib/debian-usb/config-hooks"),
    ]
    for candidate in candidates:
        if candidate.is_dir():
            return _validate_hooks_dir(candidate)
    raise RuntimeError("could not locate the Debian Live config hook directory")


def stage_debian_live_config_hooks(live_binary_dir: Path) -> list[Path]:
    source_dir = live_config_hooks_dir()
    destination_dir = live_binary_dir / "config-hooks"
    destination_dir.mkdir(parents=True, exist_ok=True)
    staged: list[Path] = []
    for filename in LIVE_CONFIG_HOOK_FILENAMES:
        source = source_dir / filename
        destination = destination_dir / filename
        shutil.copyfile(source, destination)
        destination.chmod(0o755)
        staged.append(destination)
    return staged


def stage_live_systemd_masks(live_root_dir: Path) -> list[Path]:
    root = _prepare_live_root(live_root_dir)
    for relative_path in LIVE_SYSTEMD_DISABLE_LINKS:
        link_path = _prepare_live_root_path(root, relative_path)
        if link_path.is_symlink() or link_path.is_file():
            link_path.unlink()
        elif link_path.exists():
            raise ValueError(f"Live systemd activation path is not a file or symlink: {link_path}")

    systemd_dir = _prepare_live_root_directory(root, "etc/systemd/system")
    staged: list[Path] = []
    for unit in LIVE_SYSTEMD_MASK_UNITS:
        mask_path = systemd_dir / unit
        if mask_path.is_symlink():
            if mask_path.readlink() == Path("/dev/null"):
                staged.append(mask_path)
                continue
            mask_path.unlink()
        elif mask_path.exists():
            if mask_path.is_dir():
                raise ValueError(f"Live systemd mask path is a directory: {mask_path}")
            mask_path.unlink()
        mask_path.symlink_to("/dev/null")
        staged.append(mask_path)
    return staged


def stage_debian_live_locale(live_root_dir: Path) -> list[Path]:
    root = _prepare_live_root(live_root_dir)
    etc_dir = _prepare_live_root_directory(root, "etc")
    default_dir = _prepare_live_root_directory(root, "etc/default")
    locale_gen_path = etc_dir / "locale.gen"
    default_locale_path = default_dir / "locale"
    _write_locale_gen(root, locale_gen_path)
    _write_default_locale(root, default_locale_path)
    return [locale_gen_path, default_locale_path]


def _prepare_live_root(live_root_dir: Path) -> Path:
    live_root_dir.mkdir(parents=True, exist_ok=True)
    root = live_root_dir.resolve()
    if not root.is_dir():
        raise ValueError(f"Live root policy path is not a directory: {root}")
    return root


def _prepare_live_root_directory(root: Path, relative_path: str) -> Path:
    current = root
    for part in Path(relative_path).parts:
        current /= part
        if current.is_symlink():
            raise ValueError(f"Live root policy directory must not be a symlink: {current}")
        if current.exists():
            if not current.is_dir():
                raise ValueError(f"Live root policy directory path is not a directory: {current}")
            continue
        current.mkdir()
    return current


def _prepare_live_root_path(root: Path, relative_path: str) -> Path:
    relative = Path(relative_path)
    parent = _prepare_live_root_directory(root, relative.parent.as_posix())
    return parent / relative.name


def _normalize_live_root_path(root: Path, path: Path) -> Path:
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"Live root policy path escapes the Live root: {path}") from exc

    parts: list[str] = []
    for part in relative.parts:
        if part in ("", "."):
            continue
        if part == "..":
            if not parts:
                raise ValueError(f"Live root policy path escapes the Live root: {path}")
            parts.pop()
            continue
        parts.append(part)
    return root.joinpath(*parts)


def _resolve_live_root_symlink_target(root: Path, link_path: Path, target: Path) -> Path:
    if target.is_absolute():
        parts: list[str] = []
        target_parts = target.parts[1:]
    else:
        parts = list(link_path.parent.relative_to(root).parts)
        target_parts = target.parts

    for part in target_parts:
        if part in ("", "."):
            continue
        if part == "..":
            if not parts:
                raise ValueError(f"Live root policy symlink escapes the Live root: {link_path} -> {target}")
            parts.pop()
            continue
        parts.append(part)
    return root.joinpath(*parts)


def _validate_live_root_file_parent(root: Path, path: Path) -> None:
    relative = path.relative_to(root)
    current = root
    for part in relative.parts[:-1]:
        current /= part
        if current.is_symlink():
            raise ValueError(f"Live root policy directory must not be a symlink: {current}")
        if not current.exists():
            raise ValueError(f"Live root policy target directory does not exist: {current}")
        if not current.is_dir():
            raise ValueError(f"Live root policy directory path is not a directory: {current}")


def _resolve_live_root_policy_file(root: Path, path: Path) -> Path:
    candidate = _normalize_live_root_path(root, path)
    seen: set[Path] = set()
    followed = 0
    while True:
        _validate_live_root_file_parent(root, candidate)
        if not candidate.is_symlink():
            if candidate.exists() and not candidate.is_file():
                raise ValueError(f"Live root policy path is not a regular file: {candidate}")
            return candidate

        if candidate in seen:
            raise ValueError(f"Live root policy symlink loop detected: {candidate}")
        if followed >= MAX_LIVE_ROOT_POLICY_SYMLINKS:
            raise ValueError(f"Live root policy symlink chain is too deep: {path}")
        seen.add(candidate)
        followed += 1
        target_text = os.readlink(candidate)
        target_name = target_text.rsplit("/", 1)[-1]
        if not target_text or target_text.endswith("/") or target_name in (".", ".."):
            raise ValueError(
                f"Live root policy symlink target does not name a regular file: {candidate} -> {target_text}"
            )
        candidate = _resolve_live_root_symlink_target(root, candidate, Path(target_text))


def _read_live_root_text(path: Path) -> str:
    if path.is_symlink():
        raise ValueError(f"Live root policy file must not be a symlink: {path}")
    if not path.exists():
        return ""
    if not path.is_file():
        raise ValueError(f"Live root policy path is not a regular file: {path}")
    return path.read_text(encoding="utf-8")


def _write_locale_gen(root: Path, path: Path) -> None:
    target = _resolve_live_root_policy_file(root, path)
    lines = _read_live_root_text(target).splitlines()
    rendered: list[str] = []
    locale_written = False
    for line in lines:
        candidate = line.strip()
        if candidate.startswith("#"):
            candidate = candidate[1:].strip()
        if candidate.split() == [DEBIAN_LIVE_LOCALE, "UTF-8"]:
            if not locale_written:
                rendered.append(f"{DEBIAN_LIVE_LOCALE} UTF-8")
                locale_written = True
            continue
        rendered.append(line)
    if not locale_written:
        rendered.append(f"{DEBIAN_LIVE_LOCALE} UTF-8")
    target.write_text("\n".join(rendered) + "\n", encoding="utf-8")


def _write_default_locale(root: Path, path: Path) -> None:
    target = _resolve_live_root_policy_file(root, path)
    assignments = {
        "LANG": DEBIAN_LIVE_LOCALE,
        "LANGUAGE": DEBIAN_LIVE_LANGUAGE,
    }
    rendered: list[str] = []
    written: set[str] = set()
    for line in _read_live_root_text(target).splitlines():
        stripped = line.strip()
        key = stripped.split("=", 1)[0] if "=" in stripped and not stripped.startswith("#") else ""
        if key not in assignments:
            rendered.append(line)
            continue
        if key not in written:
            rendered.append(f"{key}={assignments[key]}")
            written.add(key)
    for key, value in assignments.items():
        if key not in written:
            rendered.append(f"{key}={value}")
    target.write_text("\n".join(rendered) + "\n", encoding="utf-8")


def _validate_hooks_dir(path: Path) -> Path:
    resolved = path.resolve()
    if not resolved.is_dir():
        raise ValueError(f"Live config hook path is not a directory: {resolved}")
    for filename in LIVE_CONFIG_HOOK_FILENAMES:
        hook_path = resolved / filename
        if not hook_path.is_file():
            raise ValueError(f"missing Debian Live config hook: {hook_path}")
    return resolved
