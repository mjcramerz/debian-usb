from __future__ import annotations

import os
from pathlib import Path
import tempfile


def _candidate_work_roots() -> list[Path]:
    override = os.environ.get("DEBIAN_USB_WORK_DIR", "").strip()
    if override:
        return [Path(override).expanduser()]

    candidates = [Path("/data/tmp/debian-usb")]
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR", "").strip()
    if runtime_dir:
        candidates.append(Path(runtime_dir).expanduser() / "debian-usb")
    temp_root = Path(tempfile.gettempdir()) / f"debian-usb-{os.getuid()}"
    candidates.append(temp_root)
    return candidates


def _ensure_usable_work_root(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    probe = tempfile.TemporaryDirectory(prefix=".probe-", dir=str(path))
    probe_path = Path(probe.name)
    probe.cleanup()
    try:
        probe_path.rmdir()
    except FileNotFoundError:
        pass
    return path.resolve()


def work_root() -> Path:
    candidates = _candidate_work_roots()
    override_requested = bool(os.environ.get("DEBIAN_USB_WORK_DIR", "").strip())
    last_error: Exception | None = None
    for candidate in candidates:
        try:
            return _ensure_usable_work_root(candidate)
        except OSError as exc:
            last_error = exc
            if override_requested:
                raise
    raise OSError(f"unable to create a writable debian-usb work root from candidates {candidates}: {last_error}")


def temporary_work_dir(prefix: str) -> tempfile.TemporaryDirectory[str]:
    return tempfile.TemporaryDirectory(prefix=prefix, dir=str(work_root()))
