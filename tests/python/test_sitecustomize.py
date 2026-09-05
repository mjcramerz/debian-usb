from __future__ import annotations

from pathlib import Path
import os
import subprocess
import unittest


class SiteCustomizeTests(unittest.TestCase):
    def test_repo_python_wrapper_does_not_create_repo_local_pycache_dirs(self) -> None:
        repo_root = Path(__file__).resolve().parents[2]
        env = os.environ.copy()
        wrapper = repo_root / "scripts" / "debian-usb-python"
        result = subprocess.run(
            [str(wrapper), "list-local-isos"],
            cwd=repo_root,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=True,
        )
        self.assertTrue(result.stdout.strip().startswith("["))
        pycache_dirs = [path for path in repo_root.rglob("__pycache__")]
        self.assertEqual(pycache_dirs, [])


if __name__ == "__main__":
    unittest.main()
