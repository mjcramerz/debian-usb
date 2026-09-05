from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess

from .workdir import temporary_work_dir


def _collapse_whitespace(value: str) -> str:
    return " ".join(value.strip().split())


def _split_kernel_args(value: str) -> list[str]:
    collapsed = _collapse_whitespace(value)
    if not collapsed:
        return []
    return collapsed.split()


def _normalize_member_path(value: str) -> str:
    cleaned = value.strip().strip('"').strip("'")
    if not cleaned:
        return ""
    if not cleaned.startswith("/"):
        cleaned = "/" + cleaned
    return cleaned


def _preprocess_lines(text: str) -> list[str]:
    lines = text.splitlines()
    processed: list[str] = []
    current = ""
    for raw_line in lines:
        line = raw_line.rstrip()
        if not line:
            if current:
                processed.append(current)
                current = ""
            continue
        if line.endswith("\\"):
            current += line[:-1] + " "
            continue
        current += line
        processed.append(current)
        current = ""
    if current:
        processed.append(current)
    return processed


def _read_iso_probe(iso_path: Path) -> None:
    try:
        with iso_path.open("rb") as handle:
            chunk = handle.read(65536)
    except OSError as exc:  # pragma: no cover - platform-dependent I/O failure
        raise ValueError(f"unable to read ISO image: {iso_path}: {exc.strerror or exc}") from exc
    if not chunk:
        raise ValueError(f"ISO image is empty or unreadable: {iso_path}")


class DirectorySource:
    def __init__(self, root: str) -> None:
        self.root = Path(root).expanduser().resolve()
        if not self.root.is_dir():
            raise ValueError(f"live root is not a directory: {self.root}")
        self._file_cache: set[str] | None = None

    @property
    def source_path(self) -> Path:
        return self.root

    @property
    def display_path(self) -> str:
        return str(self.root)

    @property
    def source_type(self) -> str:
        return "directory"

    def exists(self, member_path: str) -> bool:
        normalized = _normalize_member_path(member_path)
        return bool(normalized) and (self.root / normalized.lstrip("/")).exists()

    def read_text(self, member_path: str) -> str:
        normalized = _normalize_member_path(member_path)
        return (self.root / normalized.lstrip("/")).read_text(encoding="utf-8", errors="ignore")

    @property
    def volume_id(self) -> str:
        return ""

    def list_files(self) -> set[str]:
        if self._file_cache is not None:
            return self._file_cache
        files: set[str] = set()
        for candidate in self.root.rglob("*"):
            if candidate.is_file():
                relative = candidate.relative_to(self.root)
                files.add(_normalize_member_path("/" + str(relative).replace(os.sep, "/")))
        self._file_cache = files
        return files

    def has_member_prefix(self, member_path: str) -> bool:
        normalized = _normalize_member_path(member_path)
        if not normalized:
            return False
        if (self.root / normalized.lstrip("/")).exists():
            return True
        prefix = normalized.rstrip("/") + "/"
        return any(path.startswith(prefix) for path in self.list_files())

    def extract_member(self, member_path: str, destination: Path) -> None:
        normalized = _normalize_member_path(member_path)
        source_path = self.root / normalized.lstrip("/")
        if not source_path.exists():
            raise FileNotFoundError(f"missing extracted member: {normalized}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        if source_path.is_dir():
            shutil.copytree(source_path, destination, dirs_exist_ok=True)
            return
        shutil.copy2(source_path, destination)


class IsoSource:
    def __init__(self, iso_path: str) -> None:
        self.iso_path = Path(iso_path).expanduser().resolve()
        if not self.iso_path.exists():
            raise ValueError(f"ISO path does not exist: {self.iso_path}")
        if not self.iso_path.is_file():
            raise ValueError(f"ISO path is not a regular file: {self.iso_path}")
        _read_iso_probe(self.iso_path)
        self._file_cache: set[str] | None = None
        self._volume_id = ""

    @property
    def source_path(self) -> Path:
        return self.iso_path

    @property
    def display_path(self) -> str:
        return str(self.iso_path)

    @property
    def source_type(self) -> str:
        return "iso"

    @property
    def volume_id(self) -> str:
        if self._file_cache is None:
            self.list_files()
        return self._volume_id

    def _run_xorriso(self, *args: str) -> subprocess.CompletedProcess[str]:
        command = ["xorriso", *args]
        result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", check=False)
        if result.returncode != 0:
            message = result.stderr.strip() or result.stdout.strip() or "xorriso failed"
            raise ValueError(message)
        return result

    def list_files(self) -> set[str]:
        if self._file_cache is not None:
            return self._file_cache
        result = self._run_xorriso("-indev", str(self.iso_path), "-find", "/", "-maxdepth", "8", "-type", "f")
        files: set[str] = set()
        volume_id = ""
        for line in result.stderr.splitlines():
            stripped = line.strip()
            if stripped.startswith("Volume id"):
                _, _, payload = stripped.partition(":")
                volume_id = payload.strip().strip("'")
        for line in result.stdout.splitlines():
            stripped = line.strip().strip("'")
            if stripped.startswith("/"):
                files.add(_normalize_member_path(stripped))
        if not files:
            raise ValueError(f"unable to enumerate ISO members from {self.iso_path}")
        self._file_cache = files
        self._volume_id = volume_id
        return files

    def has_member_prefix(self, member_path: str) -> bool:
        normalized = _normalize_member_path(member_path)
        if not normalized:
            return False
        if normalized in self.list_files():
            return True
        prefix = normalized.rstrip("/") + "/"
        return any(path.startswith(prefix) for path in self.list_files())

    def exists(self, member_path: str) -> bool:
        normalized = _normalize_member_path(member_path)
        return normalized in self.list_files()

    def read_text(self, member_path: str) -> str:
        normalized = _normalize_member_path(member_path)
        if not self.exists(normalized):
            raise FileNotFoundError(f"missing ISO member: {normalized}")
        with temporary_work_dir("iso-read-") as temp_dir:
            destination = Path(temp_dir) / Path(normalized).name
            self.extract_member(normalized, destination)
            return destination.read_text(encoding="utf-8", errors="ignore")

    def extract_member(self, member_path: str, destination: Path) -> None:
        normalized = _normalize_member_path(member_path)
        if not self.exists(normalized):
            raise FileNotFoundError(f"missing ISO member: {normalized}")
        self._run_xorriso(
            "-osirrox",
            "on",
            "-indev",
            str(self.iso_path),
            "-extract",
            normalized,
            str(destination),
        )


MediaSource = DirectorySource | IsoSource


def open_source(path: str) -> MediaSource:
    candidate = Path(path).expanduser().resolve()
    if candidate.is_dir():
        return DirectorySource(str(candidate))
    return IsoSource(str(candidate))
