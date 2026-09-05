import unittest

from debian_usb.catalog import profile_for


class CatalogTests(unittest.TestCase):
    def test_ubuntu_desktop_uses_documented_persistence_labels(self) -> None:
        profile = profile_for("ubuntu-desktop")
        self.assertTrue(profile.supports_managed)
        self.assertTrue(profile.supports_live_overrides)
        self.assertEqual(profile.managed_payload_layout, "extracted")
        self.assertEqual(profile.live_boot_family, "casper")
        self.assertEqual(profile.persistence_fs_label, "casper-rw")
        self.assertEqual(profile.persistence_partlabel, "writable")

    def test_ubuntu_server_uses_casper_live_family(self) -> None:
        profile = profile_for("ubuntu-server")
        self.assertTrue(profile.supports_managed)
        self.assertTrue(profile.supports_live_overrides)
        self.assertEqual(profile.managed_payload_layout, "extracted")
        self.assertEqual(profile.live_boot_family, "casper")

    def test_kali_purple_uses_managed_installer_profile(self) -> None:
        profile = profile_for("kali-purple")
        self.assertTrue(profile.supports_managed)
        self.assertFalse(profile.supports_persistence)
        self.assertEqual(profile.managed_payload_layout, "raw-iso")
        self.assertEqual(profile.preferred_media, "installer")

    def test_debian_and_kali_profiles_use_raw_iso_managed_payloads(self) -> None:
        self.assertEqual(profile_for("debian").managed_payload_layout, "raw-iso")
        self.assertEqual(profile_for("kali-linux").managed_payload_layout, "raw-iso")


if __name__ == "__main__":
    unittest.main()
