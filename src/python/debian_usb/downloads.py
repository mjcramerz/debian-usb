from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from typing import Any
from urllib.parse import urlparse, urlunparse
import re

from .config import load_config
DOWNLOAD_ROOT = Path(os.environ.get("DEBIAN_USB_DOWNLOAD_ROOT", "/data/downloads/debian-usb")).expanduser()
ISO_DOWNLOAD_DIR = DOWNLOAD_ROOT / "iso"
BOOT_ASSET_DOWNLOAD_DIR = DOWNLOAD_ROOT / "boot"
MANIFEST_MAX_BYTES = 1024 * 1024
MUTABLE_SOURCE_URL_PATH_FRAGMENTS = (
    "/arch-latest/",
    "/current/",
    "/current-live/",
    "/lastsuccessful/",
)
DATED_DAILY_BUILD_SEGMENT_RE = re.compile(r"/\d{8}-\d+(?=/|$)")
CURRENT_RELEASE_ISO_PATTERNS = (
    (
        "/debian-cd/current-live/amd64/iso-hybrid/",
        re.compile(r"debian-live-\d+\.\d+\.\d+-amd64-standard\.iso"),
    ),
    (
        "/debian-cd/current/amd64/iso-cd/",
        re.compile(r"debian-\d+\.\d+\.\d+-amd64-netinst\.iso"),
    ),
    (
        "/current/",
        re.compile(r"kali-linux-\d+\.\d+-live-amd64\.iso"),
    ),
    (
        "/current/",
        re.compile(r"kali-linux-\d+\.\d+-installer-netinst-amd64\.iso"),
    ),
    (
        "/current/",
        re.compile(r"kali-linux-\d+\.\d+-installer-purple-amd64\.iso"),
    ),
    (
        "/kali-weekly/",
        re.compile(r"kali-linux-\d{4}-W\d{2}-live-amd64\.iso"),
    ),
    (
        "/kali-weekly/",
        re.compile(r"kali-linux-\d{4}-W\d{2}-installer-netinst-amd64\.iso"),
    ),
    (
        "/kali-weekly/",
        re.compile(r"kali-linux-\d{4}-W\d{2}-installer-purple-amd64\.iso"),
    ),
)


def _managed_source_url(config_path: str, key: str) -> str:
    config_data = load_config(config_path) if config_path else load_config("/etc/debian-usb/debian-usb.conf")
    value = str(config_data.get(key) or "").strip()
    if not value:
        raise ValueError(f"managed source URL is empty for key: {key}")
    return value


def _download_directory_for_key(key: str) -> Path:
    if key.endswith(("_ISO_URL", "_ISO_STABLE_URL", "_ISO_TESTING_URL")):
        return ISO_DOWNLOAD_DIR
    return BOOT_ASSET_DOWNLOAD_DIR


def _cache_slug_for_key(key: str) -> str:
    slug = key.strip().lower()
    if not slug:
        raise ValueError("managed source key must not be empty")
    if slug.endswith("_url"):
        slug = slug[:-4]
    return slug


def _filename_from_url(url: str) -> str:
    parsed = urlparse(url)
    name = Path(parsed.path).name.strip()
    if not name:
        raise ValueError(f"cannot derive filename from URL: {url}")
    return name


def _download_destination(key: str, url: str) -> Path:
    target_dir = _download_directory_for_key(key) / _cache_slug_for_key(key)
    return target_dir / _filename_from_url(url).removesuffix(".torrent")


def _cached_managed_destination(key: str) -> Path | None:
    target_dir = _download_directory_for_key(key) / _cache_slug_for_key(key)
    if not target_dir.is_dir():
        return None

    candidates: list[tuple[int, str, Path]] = []
    iso_source = _download_directory_for_key(key) == ISO_DOWNLOAD_DIR
    for path in target_dir.iterdir():
        if path.is_symlink() or not path.is_file():
            continue
        if path.name.startswith(".") or path.name.endswith((".source-url", ".part", ".aria2", ".torrent")):
            continue
        if iso_source and path.suffix.lower() != ".iso":
            continue
        try:
            stat_result = path.stat()
        except OSError:
            continue
        if stat_result.st_size <= 0:
            continue
        candidates.append((stat_result.st_mtime_ns, path.name, path))

    if not candidates:
        return None
    return max(candidates)[2]


def _current_release_iso_pattern(url: str) -> tuple[str, re.Pattern[str]] | None:
    parsed = urlparse(url)
    filename = Path(parsed.path).name.removesuffix(".torrent")
    for directory, pattern in CURRENT_RELEASE_ISO_PATTERNS:
        if parsed.path.startswith(directory) and pattern.fullmatch(filename):
            return directory, pattern
    return None


def _download_small_text(url: str) -> str:
    with tempfile.NamedTemporaryFile(prefix="debian-usb-manifest-", suffix=".txt", delete=False) as handle:
        temp_path = Path(handle.name)
    try:
        _run_subprocess(
            [
                "curl",
                "-4",
                "--fail",
                "--location",
                "--silent",
                "--show-error",
                "--max-time",
                "60",
                "--max-filesize",
                str(MANIFEST_MAX_BYTES),
                "--output",
                str(temp_path),
                url,
            ],
            timeout=90,
        )
        payload = temp_path.read_bytes()
        if len(payload) > MANIFEST_MAX_BYTES:
            raise RuntimeError(f"downloaded manifest exceeds {MANIFEST_MAX_BYTES} bytes: {url}")
        return payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RuntimeError(f"downloaded manifest is not valid UTF-8: {url}") from exc
    finally:
        temp_path.unlink(missing_ok=True)


def _resolve_current_release_iso_url(url: str) -> tuple[str, bool]:
    match = _current_release_iso_pattern(url)
    if match is None:
        return url, False
    directory, filename_pattern = match
    parsed = urlparse(url)
    manifest_url = urlunparse((parsed.scheme, parsed.netloc, directory + "SHA256SUMS", "", "", ""))
    candidates = {
        line.split(maxsplit=1)[1].lstrip("*")
        for line in _download_small_text(manifest_url).splitlines()
        if len(line.split(maxsplit=1)) == 2 and filename_pattern.fullmatch(line.split(maxsplit=1)[1].lstrip("*"))
    }
    if len(candidates) != 1:
        raise RuntimeError(
            f"could not resolve exactly one current release ISO from {manifest_url}; found {sorted(candidates)}"
        )
    filename = candidates.pop()
    if Path(parsed.path).name.endswith(".torrent"):
        filename += ".torrent"
    return urlunparse((parsed.scheme, parsed.netloc, directory + filename, "", "", "")), True


def _metadata_destination(destination: Path) -> Path:
    return destination.with_name(destination.name + ".source-url")


def _write_cached_source_url(destination: Path, url: str) -> None:
    metadata_path = _metadata_destination(destination)
    metadata_path.parent.mkdir(parents=True, exist_ok=True)
    metadata_path.write_text(url.strip() + "\n", encoding="utf-8")


def _cached_source_url(destination: Path, fallback_url: str) -> str:
    metadata_path = _metadata_destination(destination)
    if metadata_path.is_symlink() or not metadata_path.is_file():
        return fallback_url
    try:
        cached_url = metadata_path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        return fallback_url
    return cached_url or fallback_url


def _source_url_uses_moving_alias(url: str) -> bool:
    parsed = urlparse(url)
    normalized_path = parsed.path.strip().lower()
    if any(fragment in normalized_path for fragment in MUTABLE_SOURCE_URL_PATH_FRAGMENTS):
        return True
    if "/daily-images/" in normalized_path:
        return "/daily/" in normalized_path and DATED_DAILY_BUILD_SEGMENT_RE.search(normalized_path) is None
    if "/daily-builds/" in normalized_path or "/weekly-live-builds/" in normalized_path:
        return DATED_DAILY_BUILD_SEGMENT_RE.search(normalized_path) is None
    return False


def _status(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def _run_subprocess(command: list[str], *, timeout: int, stream_output: bool = False) -> None:
    if stream_output:
        result = subprocess.run(
            command,
            check=False,
            text=True,
            encoding="utf-8",
            timeout=timeout,
            stdout=sys.stderr,
            stderr=sys.stderr,
        )
        if result.returncode != 0:
            raise RuntimeError(f"command failed with exit status {result.returncode}: {command[0]}")
        return
    result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", check=False, timeout=timeout)
    if result.returncode != 0:
        message = result.stderr.strip() or result.stdout.strip() or "command failed"
        raise RuntimeError(message)


def _download_http(url: str, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(prefix=destination.name + ".", suffix=".part", dir=destination.parent, delete=False) as handle:
        temp_path = Path(handle.name)
    try:
        _run_subprocess(
            [
                "curl",
                "-4",
                "--fail",
                "--location",
                "--progress-bar",
                "--show-error",
                "--max-time",
                "7200",
                "--output",
                str(temp_path),
                url,
            ],
            timeout=7300,
            stream_output=True,
        )
        temp_path.replace(destination)
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise


def _download_torrent(url: str, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="debian-usb-aria2-", dir=destination.parent) as temp_dir:
        temp_root = Path(temp_dir)
        _run_subprocess(
            [
                "aria2c",
                "--dir",
                str(temp_root),
                "--seed-time=0",
                "--seed-ratio=0",
                "--bt-save-metadata=false",
                "--follow-torrent=true",
                "--max-tries=3",
                "--retry-wait=5",
                url,
            ],
            timeout=14400,
            stream_output=True,
        )
        candidates = sorted(
            (path for path in temp_root.rglob("*") if path.is_file() and path.suffix.lower() == ".iso"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        if not candidates:
            raise RuntimeError(f"aria2c did not produce an ISO payload for {url}")
        shutil.move(str(candidates[0]), str(destination))


def download_managed_source(config_path: str, key: str) -> dict[str, Any]:
    configured_url = _managed_source_url(config_path, key)
    cached_destination = _cached_managed_destination(key)
    if cached_destination is not None:
        _status(f"[download] {key}: using cached file {cached_destination}")
        return {
            "key": key,
            "url": _cached_source_url(cached_destination, configured_url),
            "path": str(cached_destination),
            "cached": True,
        }

    url, resolved_current_release_iso = _resolve_current_release_iso_url(configured_url)
    if resolved_current_release_iso and url != configured_url:
        _status(f"[download] {key}: resolved current release ISO to {url}")
    destination = _download_destination(key, url)
    _status(f"[download] {key}: downloading {url}")
    _status(f"[download] {key}: destination {destination}")
    if url.endswith(".torrent"):
        _download_torrent(url, destination)
    else:
        _download_http(url, destination)
    if not destination.is_file() or destination.stat().st_size <= 0:
        raise RuntimeError(f"downloaded file is missing or empty: {destination}")
    _write_cached_source_url(destination, url)
    _status(f"[download] {key}: saved {destination} ({destination.stat().st_size} bytes)")
    return {
        "key": key,
        "url": url,
        "path": str(destination),
        "cached": False,
    }
