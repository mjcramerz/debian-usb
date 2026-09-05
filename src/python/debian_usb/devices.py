from __future__ import annotations

import json
from pathlib import Path
import subprocess

from . import downloads as managed_downloads


def _command_output(*args: str) -> str:
    completed = subprocess.run(args, check=True, capture_output=True, text=True, encoding="utf-8", timeout=10)
    return completed.stdout.strip()


def _human_size(size_bytes: int) -> str:
    units = ["B", "KiB", "MiB", "GiB", "TiB"]
    size = float(size_bytes)
    for unit in units:
        if size < 1024.0 or unit == units[-1]:
            if unit == "B":
                return f"{int(size)} {unit}"
            return f"{size:.1f} {unit}"
        size /= 1024.0
    return f"{size_bytes} B"


def _root_disk_path() -> str | None:
    try:
        root_source = _command_output("findmnt", "-n", "-o", "SOURCE", "/")
    except subprocess.CalledProcessError:
        return None
    if not root_source.startswith("/dev/"):
        return None
    try:
        parent = _command_output("lsblk", "-no", "PKNAME", root_source).splitlines()[-1].strip()
    except (subprocess.CalledProcessError, IndexError):
        return None
    if not parent:
        return None
    return f"/dev/{parent}"


def _is_mounted(device: dict) -> bool:
    mountpoints = device.get("mountpoints") or []
    if any(mountpoints):
        return True
    for child in device.get("children", []) or []:
        if _is_mounted(child):
            return True
    return False


def list_devices() -> list[dict[str, object]]:
    payload = _command_output(
        "lsblk",
        "--json",
        "-b",
        "-o",
        "NAME,PATH,SIZE,MODEL,VENDOR,SERIAL,TRAN,RM,HOTPLUG,TYPE,MOUNTPOINTS,UUID,PTUUID",
    )
    data = json.loads(payload)
    root_disk = _root_disk_path()
    devices: list[dict[str, object]] = []
    for entry in data.get("blockdevices", []):
        if entry.get("type") != "disk":
            continue
        path = entry.get("path") or f"/dev/{entry['name']}"
        removable = bool(entry.get("rm")) or bool(entry.get("hotplug")) or entry.get("tran") == "usb"
        devices.append(
            {
                "name": entry["name"],
                "path": path,
                "model": (entry.get("model") or "").strip(),
                "vendor": (entry.get("vendor") or "").strip(),
                "serial": (entry.get("serial") or "").strip(),
                "transport": entry.get("tran") or "",
                "size_bytes": int(entry.get("size") or 0),
                "size_human": _human_size(int(entry.get("size") or 0)),
                "uuid": (entry.get("uuid") or "").strip(),
                "ptuuid": (entry.get("ptuuid") or "").strip(),
                "removable": removable,
                "mounted": _is_mounted(entry),
                "system_disk": path == root_disk,
            }
        )
    devices.sort(key=lambda item: item["path"])
    return devices


def list_local_isos() -> list[dict[str, object]]:
    candidates = [
        ("managed downloads", managed_downloads.ISO_DOWNLOAD_DIR),
        ("current directory", Path.cwd()),
    ]
    seen: set[str] = set()
    results: list[dict[str, object]] = []
    for source_name, directory in candidates:
        if not directory.is_dir():
            continue
        if source_name == "current directory":
            iso_paths = sorted(list(directory.glob("*.iso")) + list(directory.glob("*.ISO")))
        else:
            iso_paths = sorted(list(directory.rglob("*.iso")) + list(directory.rglob("*.ISO")))
        for path in iso_paths:
            resolved = str(path.resolve())
            if resolved in seen:
                continue
            seen.add(resolved)
            stat = path.stat()
            results.append(
                {
                    "name": path.name,
                    "path": resolved,
                    "source": source_name,
                    "source_type": "iso",
                    "size_human": _human_size(stat.st_size),
                    "modified_at": str(int(stat.st_mtime)),
                }
            )
    return results


def list_local_media() -> list[dict[str, object]]:
    return list_local_isos()
