from __future__ import annotations

import os
from pathlib import Path
import re
import shutil
import stat


LIVE_HOOKS_DIR_ENV = "DEBIAN_USB_LIVE_HOOKS_DIR"
LIVE_ENV_PATH_ENV = "DEBIAN_USB_LIVE_ENV_PATH"
LIVE_CONFIG_HOOK_FILENAMES = (
    "0500-apt-live-medium.sh",
    "1000-network-wifi.sh",
)
DEBIAN_LIVE_WIFI_KEYS = (
    "LIVE_WIFI_INTERFACE",
    "LIVE_WIFI_ESSID",
    "LIVE_WIFI_SECURITY",
    "LIVE_WIFI_CIDR",
    "LIVE_WIFI_GATEWAY",
    "LIVE_WIFI_NAMESERVERS",
    "LIVE_WIFI_PASSPHRASE",
)
DEBIAN_LIVE_WIFI_ROOT_PATH = "etc/debian-usb/live.env"
DEBIAN_LIVE_WIFI_MEDIUM_FILENAME = "debian-usb-live.env"
MAX_DEBIAN_LIVE_ENV_BYTES = 16 * 1024
DEBIAN_LIVE_HOOK_KERNEL_ARGS = (
    "live-config.hooks=medium",
)
DEBIAN_LIVE_INITRAMFS_MODULES = (
    "xxhash",
    "xxhash_generic",
    "lz4",
    "lz4_compress",
    "lz4_decompress",
    "loop",
    "squashfs",
    "overlay",
    "ext4",
    "dm_mod",
    "dm_crypt",
    "usb_storage",
    "uas",
    "nvme",
    "usbserial",
    "ch341",
)
DEBIAN_LIVE_KERNEL_CONFIG_SYMBOLS = (
    "CONFIG_XXHASH",
    "CONFIG_CRYPTO_XXHASH",
    "CONFIG_CRYPTO_LZ4",
    "CONFIG_LZ4_COMPRESS",
    "CONFIG_LZ4_DECOMPRESS",
    "CONFIG_BLK_DEV_LOOP",
    "CONFIG_SQUASHFS",
    "CONFIG_OVERLAY_FS",
    "CONFIG_EXT4_FS",
    "CONFIG_BLK_DEV_DM",
    "CONFIG_DM_CRYPT",
    "CONFIG_USB_STORAGE",
    "CONFIG_USB_UAS",
    "CONFIG_BLK_DEV_NVME",
    "CONFIG_USB_SERIAL",
    "CONFIG_USB_SERIAL_CH341",
)
DEBIAN_LIVE_MODULE_ALIAS_CANDIDATES = (
    "xxhash64",
    "xxhash64-generic",
    "xxhash64_generic",
    "xxhash_generic",
    "crypto-xxhash64",
    "crypto-xxhash64-generic",
    "crypto_xxhash64",
    "crypto_xxhash64_generic",
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
# contract for Debian Live boot, APT repair, firmware coverage, module
# inspection/loading, and CH341A userspace access. They are installed even
# when the user explicitly selects no optional Live tool groups.
DEBIAN_LIVE_HOOK_PACKAGES = (
    "python3",  # Full stdlib: APT repository validation needs bz2/lzma/hashlib.
    "util-linux",
    "ca-certificates",
    "debian-archive-keyring",
    "initramfs-tools",
    "live-config",
    "live-config-systemd",
    "locales",
    "kmod",
    "xxhash",
    "lz4",
    "zstd",
    "usbutils",
    "pciutils",
    "flashrom",
    "i2c-tools",
    "iproute2",
    "iw",
    "wpasupplicant",
    "network-manager",
    "sudo",
    "live-boot",
    "live-boot-initramfs-tools",
    "dhcpcd-base",
    "rfkill",
    "wireless-regdb",
    "bluez-firmware",
    "firmware-linux",
    "firmware-iwlwifi",
    "firmware-ipw2x00",
    "firmware-intel-graphics",
    "firmware-intel-misc",
    "firmware-intel-sound",
    "firmware-sof-signed",
    "intel-microcode",
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


def stage_debian_live_config_hooks(live_binary_dir: Path, profile: str = "debian") -> list[Path]:
    source_dir = live_config_hooks_dir()
    destination_dir = live_binary_dir / "config-hooks"
    destination_dir.mkdir(parents=True, exist_ok=True)
    staged: list[Path] = []
    for filename in LIVE_CONFIG_HOOK_FILENAMES:
        if profile != "debian" and filename == "0500-apt-live-medium.sh":
            continue  # Never replace Kali repositories with Debian APT policy.
        source = source_dir / filename
        destination = destination_dir / filename
        shutil.copyfile(source, destination)
        destination.chmod(0o755)
        staged.append(destination)
    return staged


def debian_live_env_path(path: str | Path = "", profile: str = "debian") -> Path:
    if profile not in {"debian", "kali-linux"}:
        raise ValueError("Live Wi-Fi policy supports only Debian and Kali Live")
    family = "kali" if profile == "kali-linux" else "debian"
    override = str(path or os.environ.get(f"DEBIAN_USB_{family.upper()}_LIVE_ENV_PATH", "")
                   or os.environ.get(LIVE_ENV_PATH_ENV, "")).strip()
    if override:
        candidate = Path(override).expanduser()
        if not candidate.is_absolute():
            raise ValueError(f"{LIVE_ENV_PATH_ENV} must be an absolute path: {override}")
        return _validate_live_env_source(candidate)

    module_path = Path(__file__).resolve()
    candidates = (
        module_path.parents[2] / f"initrd/{family}/live/live.env",
        module_path.parents[3] / f"initrd/{family}/live/live.env",
        Path(f"/usr/lib/debian-usb/initrd/{family}/live/live.env"),
    )
    for candidate in candidates:
        if candidate.exists():
            return _validate_live_env_source(candidate)
    raise RuntimeError(f"could not locate initrd/{family}/live/live.env")


def load_debian_live_wifi_config(path: str | Path = "", profile: str = "debian") -> dict[str, str]:
    from .live_wifi import load_config
    return load_config(debian_live_env_path(path, profile))


def render_debian_live_wifi_config(path: str | Path = "", profile: str = "debian") -> str:
    from .live_wifi import render_config
    return render_config(load_debian_live_wifi_config(path, profile))


def stage_debian_live_wifi_config(live_root_dir: Path, source_path: str | Path = "", profile: str = "debian") -> Path:
    root = _prepare_live_root(live_root_dir)
    destination = _prepare_live_root_path(root, DEBIAN_LIVE_WIFI_ROOT_PATH)
    _write_private_live_config(destination, render_debian_live_wifi_config(source_path, profile))
    return destination


def stage_debian_live_medium_wifi_config(live_binary_dir: Path, source_path: str | Path = "", profile: str = "debian") -> Path:
    live_dir = _prepare_live_root(live_binary_dir)
    destination = live_dir / DEBIAN_LIVE_WIFI_MEDIUM_FILENAME
    _write_private_live_config(destination, render_debian_live_wifi_config(source_path, profile))
    return destination


def _validate_live_env_source(path: Path) -> Path:
    if path.is_symlink():
        raise ValueError(f"Debian Live environment file must not be a symlink: {path}")
    try:
        metadata = path.stat()
    except FileNotFoundError as exc:
        raise ValueError(f"Debian Live environment file does not exist: {path}") from exc
    if not stat.S_ISREG(metadata.st_mode):
        raise ValueError(f"Debian Live environment path is not a regular file: {path}")
    if metadata.st_size > MAX_DEBIAN_LIVE_ENV_BYTES:
        raise ValueError(f"Debian Live environment file exceeds {MAX_DEBIAN_LIVE_ENV_BYTES} bytes: {path}")
    return path.resolve()


def _write_private_live_config(path: Path, content: str) -> None:
    if path.is_symlink():
        raise ValueError(f"Debian Live Wi-Fi configuration must not be a symlink: {path}")
    if path.exists() and not path.is_file():
        raise ValueError(f"Debian Live Wi-Fi configuration path is not a regular file: {path}")
    temporary = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    if temporary.exists() or temporary.is_symlink():
        raise ValueError(f"temporary Debian Live Wi-Fi configuration path already exists: {temporary}")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    path.chmod(0o600)


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


def stage_live_kernel_module_policy(live_root_dir: Path, modules: list[str] | tuple[str, ...]) -> list[Path]:
    root = _prepare_live_root(live_root_dir)
    normalized_modules: list[str] = []
    seen: set[str] = set()
    for value in modules:
        module = str(value).strip()
        if not module or any(not (char.isalnum() or char in "_.+-") for char in module):
            raise ValueError(f"invalid Live kernel module name: {value}")
        if module in seen:
            continue
        seen.add(module)
        normalized_modules.append(module)
    if not normalized_modules:
        raise ValueError("Live kernel module policy requires at least one module")

    initramfs_modules_dir = _prepare_live_root_directory(root, "usr/share/initramfs-tools/modules.d")
    initramfs_conf_dir = _prepare_live_root_directory(root, "usr/share/initramfs-tools/conf.d")
    modules_load_dir = _prepare_live_root_directory(root, "etc/modules-load.d")
    initramfs_modules_path = initramfs_modules_dir / "debian-usb-live"
    initramfs_conf_path = initramfs_conf_dir / "debian-usb-live"
    modules_load_path = modules_load_dir / "debian-usb-live.conf"
    policy_paths = (initramfs_modules_path, initramfs_conf_path, modules_load_path)
    for policy_path in policy_paths:
        if policy_path.is_symlink():
            raise ValueError(f"Live kernel module policy file must not be a symlink: {policy_path}")
        if policy_path.exists() and not policy_path.is_file():
            raise ValueError(f"Live kernel module policy path is not a regular file: {policy_path}")

    rendered_modules = "\n".join(normalized_modules) + "\n"
    initramfs_modules_path.write_text(rendered_modules, encoding="utf-8")
    initramfs_conf_path.write_text("MODULES=most\n", encoding="utf-8")
    modules_load_path.write_text(rendered_modules, encoding="utf-8")
    for policy_path in policy_paths:
        policy_path.chmod(0o644)
    return [initramfs_modules_path, initramfs_conf_path, modules_load_path]


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


def stage_debian_live_apt_policy(live_root_dir: Path) -> list[Path]:
    """Make APT repair independent of medium hooks and persistent /etc changes."""
    root = _prepare_live_root(live_root_dir)
    source = live_config_hooks_dir()
    destinations: list[Path] = []
    libexec = _prepare_live_root_directory(root, "usr/lib/debian-usb")
    for original, filename in (("0500-apt-live-medium.sh", "live-apt-repair"),
                               ("live-apt-repository.py", "live-apt-repository")):
        target = libexec / filename
        if target.is_symlink():
            raise ValueError(f"refusing symlinked live APT helper: {target}")
        shutil.copyfile(source / original, target)
        target.chmod(0o755)
        destinations.append(target)
    units = _prepare_live_root_directory(root, "etc/systemd/system")
    unit = units / "debian-usb-live-apt.service"
    if unit.is_symlink():
        raise ValueError("refusing symlinked live APT service")
    unit.write_text(
        "[Unit]\nDescription=Repair Debian Live APT sources and attach offline ISO repository\n"
        "After=local-fs.target live-config.service\nBefore=apt-daily.service apt-daily-upgrade.service\n"
        "ConditionKernelCommandLine=boot=live\n\n[Service]\nType=oneshot\n"
        "ExecStart=/usr/lib/debian-usb/live-apt-repair\nRemainAfterExit=yes\n"
        "TimeoutStartSec=90\n\n[Install]\nWantedBy=multi-user.target\n", encoding="utf-8")
    unit.chmod(0o644)
    wants = _prepare_live_root_directory(root, "etc/systemd/system/multi-user.target.wants")
    link = wants / unit.name
    if link.is_symlink() or link.exists():
        link.unlink()
    link.symlink_to("../" + unit.name)
    apt = _prepare_live_root_directory(root, "etc/apt/apt.conf.d")
    hook = apt / "05debian-usb-live-medium"
    if hook.is_symlink():
        raise ValueError("refusing symlinked APT pre-update configuration")
    hook.write_text(
        '// Revalidate on every update, including a persisted /etc or removed USB.\n'
        'APT::Update::Pre-Invoke { "/usr/lib/debian-usb/live-apt-repair"; };\n', encoding="utf-8")
    hook.chmod(0o644)
    return [*destinations, unit, link, hook]


def live_hook_packages(profile: str) -> list[str]:
    """Required runtime packages. Distro-specific firmware is resolved separately."""
    if profile == "debian":
        return list(DEBIAN_LIVE_HOOK_PACKAGES)
    if profile != "kali-linux":
        return []
    return [package for package in DEBIAN_LIVE_HOOK_PACKAGES
            if not (package.startswith("firmware-") or package in {
                "debian-archive-keyring", "bluez-firmware", "intel-microcode"})] + ["kali-archive-keyring"]


def live_optional_firmware(profile: str) -> list[str]:
    if profile != "kali-linux":
        return []
    return [package for package in DEBIAN_LIVE_HOOK_PACKAGES
            if package.startswith("firmware-") or package in {"bluez-firmware", "intel-microcode"}]


def stage_live_wifi_runtime(live_root_dir: Path, source_path: str | Path = "", profile: str = "debian") -> list[Path]:
    """Install a shared runtime, first-boot service, home launcher and initrd hook."""
    from .live_wifi import LAUNCHER
    root = _prepare_live_root(live_root_dir)
    staged = [stage_debian_live_wifi_config(root, source_path, profile)]
    runtime = _prepare_live_root_path(root, "usr/local/lib/debian-usb/live-wifi.py")
    if runtime.is_symlink():
        raise ValueError("refusing symlinked Wi-Fi runtime")
    shutil.copyfile(Path(__file__).with_name("live_wifi.py"), runtime)
    runtime.chmod(0o755)
    staged.append(runtime)
    scripts = {
        "etc/skel/wifi-connect.sh": LAUNCHER,
        "usr/local/bin/wifi-connect.sh": LAUNCHER,
        "etc/NetworkManager/dispatcher.d/90-debian-usb-wifi-priority":
            '#!/bin/sh\ncase "${2:-}" in up|dhcp4-change|dhcp6-change) '
            '/usr/local/lib/debian-usb/live-wifi.py --priority ;; esac\n',
        "etc/initramfs-tools/hooks/zzzz-debian-usb-wifi":
            '#!/bin/sh\nset -eu\ncase "${1:-}" in prereqs) exit 0 ;; esac\n'
            ': "${DESTDIR:?initramfs destination is required}"\n'
            'umask 077\ncp /etc/debian-usb/live.env "${DESTDIR}/live.env"\n'
            'chmod 0600 "${DESTDIR}/live.env"\n'
            'mkdir -p "${DESTDIR}/scripts/init-bottom"\n'
            'cp /usr/share/debian-usb/live-env-init-bottom "${DESTDIR}/scripts/init-bottom/debian-usb-live-env"\n'
            'chmod 0755 "${DESTDIR}/scripts/init-bottom/debian-usb-live-env"\n',
        "usr/share/debian-usb/live-env-init-bottom":
            '#!/bin/sh\nset -eu\ncase "${1:-}" in prereqs) exit 0 ;; esac\n'
            '[ -s /live.env ] || exit 0\numask 077\n'
            'mkdir -p /run/initramfs/debian-usb\n'
            'cp /live.env /run/initramfs/debian-usb/live.env\n'
            'chmod 0600 /run/initramfs/debian-usb/live.env\n',
    }
    for relative, text in scripts.items():
        path = _prepare_live_root_path(root, relative)
        if path.is_symlink():
            raise ValueError(f"refusing symlinked Wi-Fi helper: {path}")
        path.write_text(text, encoding="utf-8")
        path.chmod(0o755)
        staged.append(path)
    unit = _prepare_live_root_path(root, "etc/systemd/system/debian-usb-live-wifi.service")
    if unit.is_symlink():
        raise ValueError("refusing symlinked Wi-Fi service")
    unit.write_text(
        "[Unit]\nDescription=Debian/Kali Live Wi-Fi preference and Ethernet fallback\n"
        "Wants=NetworkManager.service\nAfter=NetworkManager.service live-config.service\n"
        "ConditionPathExists=/etc/debian-usb/live.env\n\n[Service]\nType=oneshot\n"
        "ExecStart=/usr/local/lib/debian-usb/live-wifi.py --boot\nTimeoutStartSec=120\n"
        "RemainAfterExit=yes\n\n[Install]\nWantedBy=multi-user.target\n", encoding="utf-8")
    unit.chmod(0o644)
    wants = _prepare_live_root_path(root, "etc/systemd/system/multi-user.target.wants/debian-usb-live-wifi.service")
    if wants.exists() or wants.is_symlink():
        wants.unlink()
    wants.symlink_to("../debian-usb-live-wifi.service")
    staged += [unit, wants]
    # Images with a pre-created Live account do not copy /etc/skel again.
    home_root = _prepare_live_root_directory(root, "home")
    for home in home_root.iterdir():
        if home.is_dir() and not home.is_symlink():
            target = home / "wifi-connect.sh"
            if target.exists() or target.is_symlink():
                continue
            target.write_text(LAUNCHER, encoding="utf-8")
            target.chmod(0o755)
            if os.geteuid() == 0:
                metadata = home.stat()
                os.chown(target, metadata.st_uid, metadata.st_gid)
            staged.append(target)
    return staged
