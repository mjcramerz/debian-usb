from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from debian_usb.bootconfig import inspect_media


class BootInspectTests(unittest.TestCase):
    def test_inspect_media_reports_raw_iso_payload_for_debian(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "EFI/boot").mkdir(parents=True)
            (root / "EFI/boot/bootx64.efi").write_text("", encoding="utf-8")
            (root / "isolinux").mkdir(parents=True)
            (root / "live").mkdir()
            (root / "live/vmlinuz").write_text("", encoding="utf-8")
            (root / "live/initrd.img").write_text("", encoding="utf-8")
            (root / "isolinux/live.cfg").write_text(
                """
label live
  menu label Live
  linux /live/vmlinuz
  append initrd=/live/initrd.img boot=live components quiet
""",
                encoding="utf-8",
            )

            payload = inspect_media(str(root), "debian")

            self.assertEqual(payload["media_class"], "live")
            self.assertEqual(payload["managed_payload_layout"], "raw-iso")
            self.assertEqual(payload["firmware"], ["uefi"])

    def test_inspect_media_reports_iso_store_payload_for_ubuntu(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "EFI/boot").mkdir(parents=True)
            (root / "EFI/boot/bootx64.efi").write_text("", encoding="utf-8")
            (root / "boot/grub").mkdir(parents=True)
            (root / "casper").mkdir()
            (root / "casper/vmlinuz").write_text("", encoding="utf-8")
            (root / "casper/initrd").write_text("", encoding="utf-8")
            (root / "boot/grub/grub.cfg").write_text(
                """
menuentry 'Try Ubuntu' {
    linux /casper/vmlinuz boot=casper quiet splash ---
    initrd /casper/initrd
}
""",
                encoding="utf-8",
            )

            payload = inspect_media(str(root), "ubuntu-desktop")

            self.assertEqual(payload["media_class"], "live")
            self.assertEqual(payload["managed_payload_layout"], "extracted")
            self.assertEqual(payload["firmware"], ["uefi"])

    def test_inspect_media_keeps_raw_iso_payload_for_custom_grub_on_debian(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "EFI/boot").mkdir(parents=True)
            (root / "EFI/boot/bootx64.efi").write_text("", encoding="utf-8")
            (root / "isolinux").mkdir(parents=True)
            (root / "live").mkdir()
            (root / "live/vmlinuz").write_text("", encoding="utf-8")
            (root / "live/initrd.img").write_text("", encoding="utf-8")
            (root / "isolinux/live.cfg").write_text(
                """
label live
  menu label Live
  linux /live/vmlinuz
  append initrd=/live/initrd.img boot=live components quiet
""",
                encoding="utf-8",
            )

            payload = inspect_media(str(root), "debian", use_custom_menu=True)

            self.assertEqual(payload["managed_payload_layout"], "raw-iso")

    def test_inspect_media_keeps_extracted_payload_for_custom_grub_on_ubuntu(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "EFI/boot").mkdir(parents=True)
            (root / "EFI/boot/bootx64.efi").write_text("", encoding="utf-8")
            (root / "boot/grub").mkdir(parents=True)
            (root / "casper").mkdir()
            (root / "casper/vmlinuz").write_text("", encoding="utf-8")
            (root / "casper/initrd").write_text("", encoding="utf-8")
            (root / "boot/grub/grub.cfg").write_text(
                """
menuentry 'Try Ubuntu' {
    linux /casper/vmlinuz boot=casper quiet splash ---
    initrd /casper/initrd
}
""",
                encoding="utf-8",
            )

            payload = inspect_media(str(root), "ubuntu-desktop", use_custom_menu=True)

            self.assertEqual(payload["managed_payload_layout"], "extracted")

    def test_inspect_media_warns_when_secure_boot_enabled_and_kernel_is_unsigned(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "EFI/boot").mkdir(parents=True)
            (root / "EFI/boot/bootx64.efi").write_text("", encoding="utf-8")
            (root / "isolinux").mkdir(parents=True)
            (root / "live").mkdir()
            (root / "live/vmlinuz").write_text("", encoding="utf-8")
            (root / "live/initrd.img").write_text("", encoding="utf-8")
            (root / "isolinux/live.cfg").write_text(
                """
label live
  menu label Live
  linux /live/vmlinuz
  append initrd=/live/initrd.img boot=live components quiet
""",
                encoding="utf-8",
            )
            with patch("debian_usb.boot_inspect._host_secure_boot_enabled", return_value=True):
                with patch("debian_usb.boot_inspect._kernel_has_pe_signature_directory", return_value=False):
                    payload = inspect_media(str(root), "kali-linux")
            self.assertTrue(any("Secure Boot enabled" in warning for warning in payload["warnings"]))

    def test_inspect_media_reports_encrypted_persistence_support_for_tails(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "EFI/boot").mkdir(parents=True)
            (root / "EFI/boot/bootx64.efi").write_text("", encoding="utf-8")
            (root / "isolinux").mkdir(parents=True)
            (root / "live").mkdir()
            (root / "live/vmlinuz").write_text("", encoding="utf-8")
            (root / "live/initrd.img").write_text("", encoding="utf-8")
            (root / "live/Tails.module").write_text("", encoding="utf-8")
            (root / "isolinux/live.cfg").write_text(
                """
label live
  menu label Tails
  linux /live/vmlinuz
  append initrd=/live/initrd.img boot=live components quiet
""",
                encoding="utf-8",
            )

            payload = inspect_media(str(root), "tails")

            self.assertTrue(payload["supports_persistence"])
            self.assertTrue(payload["supports_encrypted_persistence"])


if __name__ == "__main__":
    unittest.main()
