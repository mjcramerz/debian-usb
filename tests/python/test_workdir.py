from pathlib import Path
import os
import tempfile
import unittest
from unittest import mock

from debian_usb.workdir import temporary_work_dir, work_root


class WorkDirTests(unittest.TestCase):
    def test_work_root_uses_environment_override(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            override = Path(temp_dir) / "custom-work"
            previous = os.environ.get("DEBIAN_USB_WORK_DIR")
            os.environ["DEBIAN_USB_WORK_DIR"] = str(override)
            try:
                resolved = work_root()
            finally:
                if previous is None:
                    del os.environ["DEBIAN_USB_WORK_DIR"]
                else:
                    os.environ["DEBIAN_USB_WORK_DIR"] = previous
            self.assertEqual(resolved, override.resolve())
            self.assertTrue(resolved.is_dir())

    def test_temporary_work_dir_creates_subdirectory_under_work_root(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            override = Path(temp_dir) / "custom-work"
            previous = os.environ.get("DEBIAN_USB_WORK_DIR")
            os.environ["DEBIAN_USB_WORK_DIR"] = str(override)
            try:
                with temporary_work_dir("usb-build-") as work_dir:
                    path = Path(work_dir).resolve()
                    self.assertEqual(path.parent, override.resolve())
                    self.assertTrue(path.is_dir())
            finally:
                if previous is None:
                    del os.environ["DEBIAN_USB_WORK_DIR"]
                else:
                    os.environ["DEBIAN_USB_WORK_DIR"] = previous
            self.assertFalse(any(override.iterdir()) if override.exists() else False)

    def test_work_root_falls_back_when_default_path_is_not_writable(self) -> None:
        previous_override = os.environ.pop("DEBIAN_USB_WORK_DIR", None)
        previous_runtime = os.environ.pop("XDG_RUNTIME_DIR", None)
        real_temporary_directory = tempfile.TemporaryDirectory
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                fallback_root = Path(temp_dir) / f"debian-usb-{os.getuid()}"
                with mock.patch("tempfile.gettempdir", return_value=temp_dir):
                    with mock.patch("tempfile.TemporaryDirectory") as tempdir_mock:
                        def fake_temporary_directory(*, prefix: str, dir: str) -> tempfile.TemporaryDirectory[str]:
                            if Path(dir) == Path("/data/tmp/debian-usb"):
                                raise PermissionError("not writable")
                            return real_temporary_directory(prefix=prefix, dir=dir)

                        tempdir_mock.side_effect = fake_temporary_directory
                        resolved = work_root()
                self.assertEqual(resolved, fallback_root.resolve())
                self.assertTrue(resolved.is_dir())
        finally:
            if previous_override is not None:
                os.environ["DEBIAN_USB_WORK_DIR"] = previous_override
            if previous_runtime is not None:
                os.environ["XDG_RUNTIME_DIR"] = previous_runtime


if __name__ == "__main__":
    unittest.main()
