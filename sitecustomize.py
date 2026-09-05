from __future__ import annotations

import os
from pathlib import Path
import shutil
import sys

REPO_ROOT = Path(__file__).resolve().parent
PYCACHE_PREFIX = Path(os.environ.get("DEBIAN_USB_PYCACHE_DIR", "/tmp/debian-usb-pycache"))


def _configure_python_cache() -> None:
    os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")
    sys.dont_write_bytecode = True
    try:
        PYCACHE_PREFIX.mkdir(parents=True, exist_ok=True)
    except OSError:
        return
    os.environ.setdefault("PYTHONPYCACHEPREFIX", str(PYCACHE_PREFIX))
    sys.pycache_prefix = str(PYCACHE_PREFIX)


def _prune_repo_pycache_dirs() -> None:
    for pycache_dir in REPO_ROOT.rglob("__pycache__"):
        try:
            shutil.rmtree(pycache_dir)
        except OSError:
            continue


_configure_python_cache()
_prune_repo_pycache_dirs()
