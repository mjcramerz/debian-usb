from __future__ import annotations

from pathlib import Path
import io
import os
import tempfile
import unittest
from unittest.mock import patch

from debian_usb import downloads
from debian_usb.config import load_config


class DownloadTests(unittest.TestCase):
    def test_reviewed_url_rejects_changed_configuration_before_cache_use(self) -> None:
        with patch.object(downloads, "_managed_source_url", return_value="https://example.test/new.iso"), patch.object(
            downloads, "_cached_managed_destination"
        ) as cache:
            with self.assertRaisesRegex(ValueError, "changed since review"):
                downloads.download_managed_source("config", "DEBIAN_LIVE_ISO_URL",
                                                  expected_url="https://example.test/reviewed.iso")
            cache.assert_not_called()

    def test_reviewed_url_does_not_reuse_different_cached_payload(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            key = "DEBIAN_NETINST_INITRD_STABLE_URL"
            url = "https://example.test/reviewed/initrd.gz"
            with patch.object(downloads, "BOOT_ASSET_DOWNLOAD_DIR", root), patch.object(
                downloads, "_managed_source_url", return_value=url
            ):
                destination = downloads._download_destination(key, url)
                destination.parent.mkdir(parents=True)
                destination.write_bytes(b"stale")
                downloads._write_cached_source_url(destination, "https://example.test/other/initrd.gz")
                def fetch(_url: str, path: Path) -> None:
                    self.assertEqual(_url, url)
                    path.write_bytes(b"reviewed")
                with patch.object(downloads, "_download_http", side_effect=fetch) as fetched:
                    result = downloads.download_managed_source("config", key, expected_url=url)
                self.assertFalse(result["cached"])
                self.assertEqual(destination.read_bytes(), b"reviewed")
                fetched.assert_called_once()

    def test_download_managed_source_scopes_cache_by_key(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            original_root = downloads.DOWNLOAD_ROOT
            original_iso_dir = downloads.ISO_DOWNLOAD_DIR
            original_boot_dir = downloads.BOOT_ASSET_DOWNLOAD_DIR
            downloads.DOWNLOAD_ROOT = root
            downloads.ISO_DOWNLOAD_DIR = root / "iso"
            downloads.BOOT_ASSET_DOWNLOAD_DIR = root / "boot"
            stderr = io.StringIO()
            try:
                fetched: list[tuple[str, Path]] = []
                url_map = {
                    "DEBIAN_NETINST_INITRD_STABLE_URL": (
                        "https://ftp.debian.org/debian/dists/stable/main/installer-amd64/current/images/hd-media/initrd.gz"
                    ),
                    "DEBIAN_NETINST_INITRD_TESTING_URL": (
                        "https://d-i.debian.org/daily-images/amd64/daily/hd-media/initrd.gz"
                    ),
                }

                def fake_download_http(url: str, destination: Path) -> None:
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    destination.write_text(url, encoding="utf-8")
                    fetched.append((url, destination))

                with patch("debian_usb.downloads._download_http", side_effect=fake_download_http), patch(
                    "debian_usb.downloads._managed_source_url",
                    side_effect=lambda _config_path, key: url_map[key],
                ):
                    with patch("sys.stderr", stderr):
                        stable = downloads.download_managed_source("configs/debian-usb.conf", "DEBIAN_NETINST_INITRD_STABLE_URL")
                        testing = downloads.download_managed_source("configs/debian-usb.conf", "DEBIAN_NETINST_INITRD_TESTING_URL")
                        stable_cached = downloads.download_managed_source("configs/debian-usb.conf", "DEBIAN_NETINST_INITRD_STABLE_URL")
                        testing_cached = downloads.download_managed_source("configs/debian-usb.conf", "DEBIAN_NETINST_INITRD_TESTING_URL")
                        stable_path = Path(stable["path"])
                        testing_path = Path(testing["path"])
                        stable_text = stable_path.read_text(encoding="utf-8")
                        testing_text = testing_path.read_text(encoding="utf-8")
            finally:
                downloads.DOWNLOAD_ROOT = original_root
                downloads.ISO_DOWNLOAD_DIR = original_iso_dir
                downloads.BOOT_ASSET_DOWNLOAD_DIR = original_boot_dir

        self.assertFalse(stable["cached"])
        self.assertFalse(testing["cached"])
        self.assertTrue(stable_cached["cached"])
        self.assertTrue(testing_cached["cached"])
        self.assertNotEqual(stable_path, testing_path)
        self.assertEqual(stable_path.name, "initrd.gz")
        self.assertEqual(testing_path.name, "initrd.gz")
        self.assertEqual(stable_path.parent.name, "debian_netinst_initrd_stable")
        self.assertEqual(testing_path.parent.name, "debian_netinst_initrd_testing")
        self.assertIn("/stable/main/installer-amd64/current/images/hd-media/initrd.gz", stable_text)
        self.assertIn("/daily-images/amd64/daily/hd-media/initrd.gz", testing_text)
        self.assertEqual(len(fetched), 2)

    def test_download_managed_source_strips_torrent_suffix_from_destination(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            original_root = downloads.DOWNLOAD_ROOT
            original_iso_dir = downloads.ISO_DOWNLOAD_DIR
            original_boot_dir = downloads.BOOT_ASSET_DOWNLOAD_DIR
            downloads.DOWNLOAD_ROOT = root
            downloads.ISO_DOWNLOAD_DIR = root / "iso"
            downloads.BOOT_ASSET_DOWNLOAD_DIR = root / "boot"
            stderr = io.StringIO()
            try:
                fetched: list[str] = []

                def fake_download_torrent(url: str, destination: Path) -> None:
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    destination.write_text("torrent-payload", encoding="utf-8")
                    fetched.append(url)

                with patch(
                    "debian_usb.downloads._managed_source_url",
                    return_value="https://cdimage.kali.org/current/kali-linux-2026.1-live-amd64.iso.torrent",
                ), patch(
                    "debian_usb.downloads._download_small_text",
                    return_value="deadbeef  kali-linux-2026.2-live-amd64.iso",
                ), patch(
                    "debian_usb.downloads._download_torrent",
                    side_effect=fake_download_torrent,
                ):
                    with patch("sys.stderr", stderr):
                        payload = downloads.download_managed_source(
                            "configs/debian-usb.conf",
                            "KALI_LIVE_ISO_STABLE_URL",
                        )
                        path = Path(payload["path"])
                        exists = path.is_file()
            finally:
                downloads.DOWNLOAD_ROOT = original_root
                downloads.ISO_DOWNLOAD_DIR = original_iso_dir
                downloads.BOOT_ASSET_DOWNLOAD_DIR = original_boot_dir

        self.assertEqual(path.name, "kali-linux-2026.2-live-amd64.iso")
        self.assertEqual(path.parent.name, "kali_live_iso_stable")
        self.assertTrue(exists)
        self.assertEqual(
            fetched,
            ["https://cdimage.kali.org/current/kali-linux-2026.2-live-amd64.iso.torrent"],
        )

    def test_download_managed_source_reports_status_messages(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            original_root = downloads.DOWNLOAD_ROOT
            original_iso_dir = downloads.ISO_DOWNLOAD_DIR
            original_boot_dir = downloads.BOOT_ASSET_DOWNLOAD_DIR
            downloads.DOWNLOAD_ROOT = root
            downloads.ISO_DOWNLOAD_DIR = root / "iso"
            downloads.BOOT_ASSET_DOWNLOAD_DIR = root / "boot"
            stderr = io.StringIO()
            try:
                def fake_download_http(_url: str, destination: Path) -> None:
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    destination.write_text("payload", encoding="utf-8")

                with patch("debian_usb.downloads._download_http", side_effect=fake_download_http):
                    with patch("sys.stderr", stderr):
                        payload = downloads.download_managed_source("configs/debian-usb.conf", "DEBIAN_NETINST_VMLINUZ_TESTING_URL")
            finally:
                downloads.DOWNLOAD_ROOT = original_root
                downloads.ISO_DOWNLOAD_DIR = original_iso_dir
                downloads.BOOT_ASSET_DOWNLOAD_DIR = original_boot_dir

        status = stderr.getvalue()
        self.assertIn("[download] DEBIAN_NETINST_VMLINUZ_TESTING_URL: downloading", status)
        self.assertIn("[download] DEBIAN_NETINST_VMLINUZ_TESTING_URL: destination", status)
        self.assertIn("[download] DEBIAN_NETINST_VMLINUZ_TESTING_URL: saved", status)
        self.assertEqual(Path(payload["path"]).parent.name, "debian_netinst_vmlinuz_testing")

    def test_download_http_forces_ipv4(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            destination = Path(temp_dir) / "payload.bin"
            observed_commands: list[list[str]] = []

            def fake_run_subprocess(command: list[str], *, timeout: int, stream_output: bool = False) -> None:
                observed_commands.append(command)
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_text("payload", encoding="utf-8")

            with patch("debian_usb.downloads._run_subprocess", side_effect=fake_run_subprocess):
                downloads._download_http("https://example.test/payload.bin", destination)

        self.assertEqual(len(observed_commands), 1)
        self.assertEqual(observed_commands[0][0], "curl")
        self.assertIn("-4", observed_commands[0])

    def test_download_managed_source_reuses_vmlinuz_when_url_metadata_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            original_root = downloads.DOWNLOAD_ROOT
            original_iso_dir = downloads.ISO_DOWNLOAD_DIR
            original_boot_dir = downloads.BOOT_ASSET_DOWNLOAD_DIR
            downloads.DOWNLOAD_ROOT = root
            downloads.ISO_DOWNLOAD_DIR = root / "iso"
            downloads.BOOT_ASSET_DOWNLOAD_DIR = root / "boot"
            stderr = io.StringIO()
            try:
                destination = downloads.BOOT_ASSET_DOWNLOAD_DIR / "debian_netinst_vmlinuz_testing" / "vmlinuz"
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_text("stale-payload", encoding="utf-8")

                with patch("debian_usb.downloads._download_http") as download_http, patch(
                    "debian_usb.downloads._managed_source_url",
                    return_value="https://example.test/pinned/20260704-04:51/vmlinuz",
                ), patch(
                    "debian_usb.downloads._resolve_current_release_iso_url",
                ) as resolve_current_release_iso_url, patch("sys.stderr", stderr):
                    kernel_payload = downloads.download_managed_source("configs/debian-usb.conf", "DEBIAN_NETINST_VMLINUZ_TESTING_URL")
                    kernel_text = Path(kernel_payload["path"]).read_text(encoding="utf-8")
                    download_http.assert_not_called()
                    resolve_current_release_iso_url.assert_not_called()
            finally:
                downloads.DOWNLOAD_ROOT = original_root
                downloads.ISO_DOWNLOAD_DIR = original_iso_dir
                downloads.BOOT_ASSET_DOWNLOAD_DIR = original_boot_dir

        self.assertTrue(kernel_payload["cached"])
        self.assertEqual(kernel_text, "stale-payload")
        status = stderr.getvalue()
        self.assertIn("using cached file", status)

    def test_download_managed_source_reuses_vmlinuz_when_url_uses_moving_alias(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            original_root = downloads.DOWNLOAD_ROOT
            original_iso_dir = downloads.ISO_DOWNLOAD_DIR
            original_boot_dir = downloads.BOOT_ASSET_DOWNLOAD_DIR
            downloads.DOWNLOAD_ROOT = root
            downloads.ISO_DOWNLOAD_DIR = root / "iso"
            downloads.BOOT_ASSET_DOWNLOAD_DIR = root / "boot"
            stderr = io.StringIO()
            try:
                fetched: list[tuple[str, Path]] = []

                def fake_download_http(url: str, destination: Path) -> None:
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    destination.write_text(f"{url}#{len(fetched) + 1}", encoding="utf-8")
                    fetched.append((url, destination))

                with patch("debian_usb.downloads._download_http", side_effect=fake_download_http), patch(
                    "debian_usb.downloads._managed_source_url",
                    return_value="https://d-i.debian.org/daily-images/amd64/daily/hd-media/vmlinuz",
                ), patch("sys.stderr", stderr):
                    first = downloads.download_managed_source("configs/debian-usb.conf", "DEBIAN_NETINST_VMLINUZ_TESTING_URL")
                    second = downloads.download_managed_source("configs/debian-usb.conf", "DEBIAN_NETINST_VMLINUZ_TESTING_URL")
                    downloaded_text = Path(second["path"]).read_text(encoding="utf-8")
            finally:
                downloads.DOWNLOAD_ROOT = original_root
                downloads.ISO_DOWNLOAD_DIR = original_iso_dir
                downloads.BOOT_ASSET_DOWNLOAD_DIR = original_boot_dir

        self.assertFalse(first["cached"])
        self.assertTrue(second["cached"])
        self.assertEqual(len(fetched), 1)
        self.assertIn("#1", downloaded_text)
        self.assertIn("using cached file", stderr.getvalue())

    def test_dated_daily_build_urls_are_not_treated_as_moving_aliases(self) -> None:
        self.assertFalse(
            downloads._source_url_uses_moving_alias(
                "https://cdimage.debian.org/cdimage/daily-builds/daily/20260704-7/amd64/iso-cd/debian-testing-amd64-netinst.iso"
            )
        )
        self.assertFalse(
            downloads._source_url_uses_moving_alias(
                "https://d-i.debian.org/daily-images/amd64/20260704-04:51/hd-media/initrd.gz"
            )
        )
        self.assertTrue(
            downloads._source_url_uses_moving_alias(
                "https://cdimage.debian.org/cdimage/daily-builds/daily/arch-latest/amd64/iso-cd/debian-testing-amd64-netinst.iso"
            )
        )
        self.assertTrue(
            downloads._source_url_uses_moving_alias(
                "https://d-i.debian.org/daily-images/amd64/daily/hd-media/initrd.gz"
            )
        )
        self.assertTrue(
            downloads._source_url_uses_moving_alias(
                "https://nightly.tails.net/build_Tails_ISO_web-release-7.10/lastSuccessful/archive/latest.iso"
            )
        )

    def test_resolve_current_debian_iso_uses_checksum_manifest(self) -> None:
        manifest = "\n".join(
            [
                "deadbeef  debian-live-13.6.0-amd64-standard.iso",
                "cafebabe  debian-live-13.6.0-amd64-xfce.iso",
            ]
        )
        configured_url = (
            "https://cdimage.debian.org/debian-cd/current-live/amd64/iso-hybrid/"
            "debian-live-13.5.0-amd64-standard.iso"
        )
        with patch("debian_usb.downloads._download_small_text", return_value=manifest):
            resolved_url, was_resolved = downloads._resolve_current_release_iso_url(configured_url)

        self.assertTrue(was_resolved)
        self.assertEqual(
            resolved_url,
            "https://cdimage.debian.org/debian-cd/current-live/amd64/iso-hybrid/"
            "debian-live-13.6.0-amd64-standard.iso",
        )

    def test_resolve_current_kali_iso_uses_checksum_manifest(self) -> None:
        manifest = "\n".join(
            [
                "deadbeef  kali-linux-2026.2-live-amd64.iso",
                "cafebabe  kali-linux-2026.2-installer-netinst-amd64.iso",
            ]
        )
        configured_url = "https://cdimage.kali.org/current/kali-linux-2026.1-live-amd64.iso"
        with patch("debian_usb.downloads._download_small_text", return_value=manifest):
            resolved_url, was_resolved = downloads._resolve_current_release_iso_url(configured_url)

        self.assertTrue(was_resolved)
        self.assertEqual(resolved_url, "https://cdimage.kali.org/current/kali-linux-2026.2-live-amd64.iso")

    def test_resolve_current_kali_torrent_preserves_torrent_suffix(self) -> None:
        manifest = "\n".join(
            [
                "deadbeef  kali-linux-2026.2-live-amd64.iso",
                "cafebabe  kali-linux-2026.2-installer-netinst-amd64.iso",
            ]
        )
        configured_url = "https://cdimage.kali.org/current/kali-linux-2026.1-live-amd64.iso.torrent"
        with patch("debian_usb.downloads._download_small_text", return_value=manifest):
            resolved_url, was_resolved = downloads._resolve_current_release_iso_url(configured_url)

        self.assertTrue(was_resolved)
        self.assertEqual(
            resolved_url,
            "https://cdimage.kali.org/current/kali-linux-2026.2-live-amd64.iso.torrent",
        )

    def test_resolve_current_kali_netinst_selects_netinst_pattern(self) -> None:
        manifest = "\n".join(
            [
                "deadbeef  kali-linux-2026.2-live-amd64.iso",
                "cafebabe  kali-linux-2026.2-installer-netinst-amd64.iso",
            ]
        )
        configured_url = "https://cdimage.kali.org/current/kali-linux-2026.1-installer-netinst-amd64.iso"
        with patch("debian_usb.downloads._download_small_text", return_value=manifest):
            resolved_url, was_resolved = downloads._resolve_current_release_iso_url(configured_url)

        self.assertTrue(was_resolved)
        self.assertEqual(
            resolved_url,
            "https://cdimage.kali.org/current/kali-linux-2026.2-installer-netinst-amd64.iso",
        )

    def test_download_managed_source_reuses_manifest_resolved_debian_iso(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            original_root = downloads.DOWNLOAD_ROOT
            original_iso_dir = downloads.ISO_DOWNLOAD_DIR
            original_boot_dir = downloads.BOOT_ASSET_DOWNLOAD_DIR
            downloads.DOWNLOAD_ROOT = root
            downloads.ISO_DOWNLOAD_DIR = root / "iso"
            downloads.BOOT_ASSET_DOWNLOAD_DIR = root / "boot"
            fetched: list[str] = []
            configured_url = (
                "https://cdimage.debian.org/debian-cd/current-live/amd64/iso-hybrid/"
                "debian-live-13.5.0-amd64-standard.iso"
            )
            resolved_url = (
                "https://cdimage.debian.org/debian-cd/current-live/amd64/iso-hybrid/"
                "debian-live-13.6.0-amd64-standard.iso"
            )
            manifest = "deadbeef  debian-live-13.6.0-amd64-standard.iso"
            try:
                def fake_download_http(url: str, destination: Path) -> None:
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    destination.write_text(url, encoding="utf-8")
                    fetched.append(url)

                with patch(
                    "debian_usb.downloads._managed_source_url",
                    return_value=configured_url,
                ), patch(
                    "debian_usb.downloads._download_small_text",
                    return_value=manifest,
                ) as download_small_text, patch(
                    "debian_usb.downloads._download_torrent",
                ) as download_torrent, patch(
                    "debian_usb.downloads._download_http",
                    side_effect=fake_download_http,
                ) as download_http:
                    first = downloads.download_managed_source(
                        "configs/debian-usb.conf",
                        "DEBIAN_LIVE_ISO_STABLE_URL",
                    )
                    second = downloads.download_managed_source(
                        "configs/debian-usb.conf",
                        "DEBIAN_LIVE_ISO_STABLE_URL",
                    )

                    self.assertEqual(download_small_text.call_count, 1)
                    self.assertEqual(download_http.call_count, 1)
                    download_torrent.assert_not_called()
            finally:
                downloads.DOWNLOAD_ROOT = original_root
                downloads.ISO_DOWNLOAD_DIR = original_iso_dir
                downloads.BOOT_ASSET_DOWNLOAD_DIR = original_boot_dir

        self.assertFalse(first["cached"])
        self.assertTrue(second["cached"])
        self.assertEqual(first["url"], resolved_url)
        self.assertEqual(second["url"], resolved_url)
        self.assertEqual(fetched, [resolved_url])

    def test_download_managed_source_selects_newest_valid_iso_without_remote_calls(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            original_root = downloads.DOWNLOAD_ROOT
            original_iso_dir = downloads.ISO_DOWNLOAD_DIR
            original_boot_dir = downloads.BOOT_ASSET_DOWNLOAD_DIR
            downloads.DOWNLOAD_ROOT = root
            downloads.ISO_DOWNLOAD_DIR = root / "iso"
            downloads.BOOT_ASSET_DOWNLOAD_DIR = root / "boot"
            try:
                cache_dir = downloads.ISO_DOWNLOAD_DIR / "debian_live_iso_stable"
                cache_dir.mkdir(parents=True, exist_ok=True)
                older = cache_dir / "debian-live-13.5.0-amd64-standard.iso"
                newest = cache_dir / "debian-live-13.6.0-amd64-standard.iso"
                older.write_text("older", encoding="utf-8")
                newest.write_text("newest", encoding="utf-8")
                (cache_dir / "debian-live-13.7.0-amd64-standard.iso").touch()
                (cache_dir / "newer-download.part").write_text("partial", encoding="utf-8")
                (cache_dir / "release.torrent").write_text("descriptor", encoding="utf-8")
                outside_cache = root / "outside-cache.iso"
                outside_cache.write_text("outside", encoding="utf-8")
                (cache_dir / "linked.iso").symlink_to(outside_cache)
                downloads._metadata_destination(newest).write_text(
                    "https://example.test/releases/debian-live-13.6.0-amd64-standard.iso\n",
                    encoding="utf-8",
                )
                os.utime(older, (1_700_000_000, 1_700_000_000))
                os.utime(newest, (1_700_000_100, 1_700_000_100))

                with patch(
                    "debian_usb.downloads._managed_source_url",
                    return_value=(
                        "https://cdimage.debian.org/debian-cd/current-live/amd64/iso-hybrid/"
                        "debian-live-13.7.0-amd64-standard.iso"
                    ),
                ), patch(
                    "debian_usb.downloads._download_small_text",
                ) as download_small_text, patch(
                    "debian_usb.downloads._download_http",
                ) as download_http, patch(
                    "debian_usb.downloads._download_torrent",
                ) as download_torrent:
                    payload = downloads.download_managed_source(
                        "configs/debian-usb.conf",
                        "DEBIAN_LIVE_ISO_STABLE_URL",
                    )

                    download_small_text.assert_not_called()
                    download_http.assert_not_called()
                    download_torrent.assert_not_called()
            finally:
                downloads.DOWNLOAD_ROOT = original_root
                downloads.ISO_DOWNLOAD_DIR = original_iso_dir
                downloads.BOOT_ASSET_DOWNLOAD_DIR = original_boot_dir

        self.assertTrue(payload["cached"])
        self.assertEqual(Path(payload["path"]), newest)
        self.assertEqual(
            payload["url"],
            "https://example.test/releases/debian-live-13.6.0-amd64-standard.iso",
        )

    def test_debian_managed_installer_asset_urls_separate_stable_and_testing_channels(self) -> None:
        config = load_config("configs/debian-usb.conf")
        expected_prefix = "https://ftp.debian.org/debian/dists/stable/main/installer-amd64/current/images/"
        self.assertEqual(
            config["DEBIAN_NETINST_ISO_STABLE_URL"],
            "https://cdimage.debian.org/debian-cd/current/amd64/iso-cd/debian-13.6.0-amd64-netinst.iso",
        )
        self.assertEqual(
            config["DEBIAN_NETINST_ISO_TESTING_URL"],
            "https://cdimage.debian.org/cdimage/daily-builds/daily/arch-latest/amd64/iso-cd/debian-testing-amd64-netinst.iso",
        )
        self.assertEqual(
            config["DEBIAN_NETINST_VMLINUZ_STABLE_URL"],
            expected_prefix + "hd-media/vmlinuz",
        )
        self.assertEqual(
            config["DEBIAN_NETINST_VMLINUZ_TESTING_URL"],
            "https://d-i.debian.org/daily-images/amd64/daily/hd-media/vmlinuz",
        )
        self.assertEqual(
            config["DEBIAN_NETINST_INITRD_STABLE_URL"],
            expected_prefix + "hd-media/initrd.gz",
        )
        self.assertEqual(
            config["DEBIAN_NETINST_INITRD_TESTING_URL"],
            "https://d-i.debian.org/daily-images/amd64/daily/hd-media/initrd.gz",
        )
        self.assertEqual(config["DEBIAN_NETBOOT_VMLINUZ_STABLE_URL"], expected_prefix + "netboot/debian-installer/amd64/linux")
        self.assertEqual(
            config["DEBIAN_NETBOOT_VMLINUZ_TESTING_URL"],
            "https://d-i.debian.org/daily-images/amd64/daily/netboot/debian-installer/amd64/linux",
        )
        self.assertEqual(config["DEBIAN_NETBOOT_INITRD_STABLE_URL"], expected_prefix + "netboot/debian-installer/amd64/initrd.gz")
        self.assertEqual(
            config["DEBIAN_NETBOOT_INITRD_TESTING_URL"],
            "https://d-i.debian.org/daily-images/amd64/daily/netboot/debian-installer/amd64/initrd.gz",
        )

    def test_list_local_isos_discovers_nested_managed_downloads(self) -> None:
        from debian_usb.devices import list_local_isos

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            nested_dir = root / "debian_live_iso"
            nested_dir.mkdir(parents=True, exist_ok=True)
            nested_iso = nested_dir / "debian-live.iso"
            nested_iso.write_text("iso", encoding="utf-8")
            original_iso_dir = downloads.ISO_DOWNLOAD_DIR
            old_cwd = Path.cwd()
            downloads.ISO_DOWNLOAD_DIR = root
            try:
                with tempfile.TemporaryDirectory() as cwd_dir:
                    os.chdir(cwd_dir)
                    payload = list_local_isos()
            finally:
                downloads.ISO_DOWNLOAD_DIR = original_iso_dir
                os.chdir(old_cwd)

        managed = [item for item in payload if item["path"] == str(nested_iso.resolve())]
        self.assertEqual(len(managed), 1)
        self.assertEqual(managed[0]["source"], "managed downloads")


if __name__ == "__main__":
    unittest.main()
