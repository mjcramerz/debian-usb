from __future__ import annotations

import os
from pathlib import Path
import shutil
import stat
import tempfile
import unittest

from debian_usb.initrd_overlay import merge_initrd_overlay


REQUIRES_OVERLAY_TOOLS = unittest.skipUnless(
    all(shutil.which(command) for command in ("cpio", "find")),
    "requires cpio and find",
)


class InitrdOverlayTests(unittest.TestCase):
    @REQUIRES_OVERLAY_TOOLS
    def test_merge_places_all_selected_overlay_contents_at_initrd_root(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            overlay = root / "overlay"
            initrd_root = root / "initrd-root"
            (overlay / "etc/debian-usb").mkdir(parents=True)
            preseed = overlay / "preseed.env"
            preseed.write_text('PRESEED_PRIMARY_USERNAME=""\n', encoding="utf-8")
            preseed.chmod(0o600)
            (overlay / "etc/debian-usb/stage").write_text("netinst\n", encoding="utf-8")
            initrd_root.mkdir()

            manifest = merge_initrd_overlay(overlay, initrd_root)

            self.assertEqual(
                (initrd_root / "preseed.env").read_text(encoding="utf-8"),
                'PRESEED_PRIMARY_USERNAME=""\n',
            )
            self.assertEqual((initrd_root / "etc/debian-usb/stage").read_text(encoding="utf-8"), "netinst\n")
            self.assertEqual(stat.S_IMODE((initrd_root / "preseed.env").stat().st_mode), 0o600)
            self.assertEqual(manifest["embedded_root"], "/")

    @REQUIRES_OVERLAY_TOOLS
    @unittest.skipUnless(hasattr(os, "mkfifo"), "requires mkfifo")
    def test_merge_does_not_reject_initrd_filesystem_object_kinds(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            overlay = root / "overlay"
            destination = root / "initrd-root"
            overlay.mkdir()
            os.mkfifo(overlay / "initrd-pipe")

            merge_initrd_overlay(overlay, destination)

            self.assertTrue(stat.S_ISFIFO((destination / "initrd-pipe").lstat().st_mode))

    @REQUIRES_OVERLAY_TOOLS
    def test_empty_overlay_requires_no_fixed_entries(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            overlay = root / "overlay"
            destination = root / "initrd-root"
            overlay.mkdir()

            manifest = merge_initrd_overlay(overlay, destination)

            self.assertEqual(manifest["embedded_root"], "/")
            self.assertEqual(list(destination.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
