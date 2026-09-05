from __future__ import annotations

from pathlib import Path
import subprocess
import tempfile


def merge_initrd_overlay(overlay_dir: str | Path, initrd_root: str | Path) -> dict[str, object]:
    """Merge every entry below *overlay_dir* at the extracted initrd root.

    GNU cpio pass-through mode is used deliberately: initrd overlays are not
    constrained to a repository-defined list of paths or filesystem object
    kinds. find does not dereference symbolic links, and it emits only
    paths rooted below the selected overlay directory.
    """

    overlay_root = Path(overlay_dir).expanduser().resolve()
    destination = Path(initrd_root).expanduser()
    if destination.is_symlink():
        raise ValueError(f"initrd root must not be a symbolic link: {destination}")
    destination.mkdir(parents=True, exist_ok=True)
    destination_root = destination.resolve()
    if destination_root == Path(destination_root.anchor):
        raise ValueError(f"refusing unsafe initrd root: {destination_root}")

    with tempfile.TemporaryFile() as entries:
        listed = subprocess.run(
            ["find", ".", "-mindepth", "1", "-print0"],
            cwd=overlay_root,
            stdout=entries,
            stderr=subprocess.PIPE,
            check=False,
        )
        if listed.returncode != 0:
            detail = listed.stderr.decode(errors="replace").strip()
            raise RuntimeError(detail or f"failed to enumerate initrd overlay {overlay_root}")

        entries.seek(0)
        copied = subprocess.run(
            [
                "cpio",
                "--null",
                "--pass-through",
                "--make-directories",
                "--preserve-modification-time",
                "--unconditional",
                "--quiet",
                "--",
                str(destination_root),
            ],
            cwd=overlay_root,
            stdin=entries,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            check=False,
        )
    if copied.returncode != 0:
        detail = copied.stderr.decode(errors="replace").strip()
        raise RuntimeError(detail or f"failed to merge initrd overlay {overlay_root}")

    return {
        "overlay_dir": str(overlay_root),
        "embedded_root": "/",
    }
