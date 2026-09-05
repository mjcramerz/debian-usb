from pathlib import Path
import json
import tempfile
import unittest
from unittest.mock import patch

from debian_usb.boot_render import _iso_payload_manifest
from debian_usb.bootconfig import parse_grub_entries
from debian_usb.config import load_template_config
from debian_usb.multios import load_multios_plan, payload_uuid_args_to_map, render_multios_grub, validate_multios_plan


def _seed_transport_tokens(kernel_args: str) -> list[str]:
    transport_keys = {"url", "file", "preseed/url", "preseed/file", "url/preseed", "file/preseed"}
    return [token for token in kernel_args.split() if token.split("=", 1)[0] in transport_keys]


def _repo_preset_label(spec_name: str, preset_set: str, args_key: str) -> str:
    spec_path = Path(__file__).resolve().parents[2] / "configs" / "spec" / "grub" / f"{spec_name}.json"
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    return next(
        str(preset["label"])
        for preset in spec["preseed"]["preset_sets"][preset_set]
        if preset["args_key"] == args_key
    )


def _template_kernel_arg(config_key: str, argument_name: str) -> str:
    return next(
        token
        for token in load_template_config()[config_key].split()
        if token.split("=", 1)[0] == argument_name
    )


def _grub_escaped_config_arg(config_key: str, argument_name: str) -> str:
    token = _template_kernel_arg(config_key, argument_name).replace("\\;", ";")
    return token.replace(";", "\\;")


class MultiOSTests(unittest.TestCase):
    def test_validate_multios_plan_rejects_ubuntu_profiles(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            for profile in ("ubuntu-desktop", "ubuntu-server"):
                with self.subTest(profile=profile):
                    iso = root / f"{profile}.iso"
                    iso.write_text("iso", encoding="utf-8")
                    plan = {
                        "schema_version": 1,
                        "write_mode": "multi-os",
                        "items": [self._plan_item("os1", profile, iso)],
                    }
                    with self.assertRaisesRegex(ValueError, "not supported in Multi-OS"):
                        validate_multios_plan(plan)

    def test_shared_data_live_payload_manifest_copies_exact_bare_iso(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            live_iso = Path(temp_dir) / "debian-live.iso"
            live_iso.write_bytes(b"opaque-live-iso")

            self.assertEqual(
                _iso_payload_manifest(
                    "debian",
                    "primary",
                    "live",
                    str(live_iso),
                    payload_layout="shared-data",
                ),
                [
                    {
                        "source_path": str(live_iso),
                        "target_path": "/debian-live/debian-live.iso",
                    }
                ],
            )

    def test_load_multios_plan_rejects_duplicate_profiles(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            iso = root / "debian.iso"
            iso.write_text("iso", encoding="utf-8")
            plan = {
                "schema_version": 1,
                "write_mode": "multi-os",
                "use_custom_grub_menu": True,
                "items": [
                    self._plan_item("os1", "debian", iso),
                    self._plan_item("os2", "debian", iso),
                ],
            }
            plan_path = root / "plan.json"
            plan_path.write_text(json.dumps(plan), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "duplicate Multi-OS profile"):
                load_multios_plan(str(plan_path))

    def test_payload_uuid_args_to_map_requires_id_equals_uuid(self) -> None:
        self.assertEqual(payload_uuid_args_to_map(["os1=AAAA", "os2=BBBB"]), {"os1": "AAAA", "os2": "BBBB"})
        with self.assertRaisesRegex(ValueError, "payload UUID values"):
            payload_uuid_args_to_map(["bad"])

    def test_validate_multios_plan_rejects_invalid_esp_label(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            iso = Path(temp_dir) / "debian.iso"
            iso.write_text("iso", encoding="utf-8")
            plan = {
                "schema_version": 1,
                "write_mode": "multi-os",
                "esp_label": "ESPBOOT-TOO-LONG",
                "items": [
                    self._plan_item("os1", "debian", iso),
                    self._plan_item("os2", "kali-linux", iso),
                ],
            }
            with self.assertRaisesRegex(ValueError, "esp_label"):
                validate_multios_plan(plan)

    def test_validate_multios_plan_inspects_media_when_requested(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            iso = Path(temp_dir) / "debian.iso"
            iso.write_text("iso", encoding="utf-8")
            plan = {
                "schema_version": 1,
                "write_mode": "multi-os",
                "use_custom_grub_menu": True,
                "items": [
                    self._plan_item("os1", "debian", iso),
                    self._plan_item("os2", "kali-linux", iso),
                ],
            }
            with patch("debian_usb.multios.inspect_media", return_value={"managed_supported": False}):
                with self.assertRaisesRegex(ValueError, "not managed-capable"):
                    validate_multios_plan(plan, inspect_sources=True)

    def test_validate_multios_plan_inspection_rejects_stale_netinst_initrd(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "debian-netinst"
            (source / "hd-media").mkdir(parents=True)
            (source / "hd-media/vmlinuz").write_bytes(b"downloaded-hd-media-kernel")
            (source / "hd-media/initrd.gz").write_bytes(b"stale-hd-media-initrd")
            (source / "payload").mkdir()
            (source / "payload/debian-netinst.iso").write_bytes(b"opaque-netinst-iso")
            plan = {
                "schema_version": 1,
                "write_mode": "multi-os",
                "use_custom_grub_menu": True,
                "items": [
                    {
                        **self._plan_item("debian-netinst", "debian", source),
                        "source_role": "netinst",
                    }
                ],
            }

            with patch(
                "debian_usb.boot_inspect.validate_prepared_netinst_initrd",
                side_effect=ValueError("stale Netinst initrd rejected"),
            ) as validate_initrd:
                with self.assertRaisesRegex(ValueError, "stale Netinst initrd rejected"):
                    validate_multios_plan(plan, inspect_sources=True)

            validate_initrd.assert_called_once_with(source / "hd-media/initrd.gz")

    def test_render_multios_grub_uses_os_submenus_and_unique_persistence_labels(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            debian_root = self._live_boot_root(root / "debian-root", "live")
            kali_root = self._live_boot_root(root / "kali-root", "live")
            plan = {
                "schema_version": 1,
                "write_mode": "multi-os",
                "items": [
                    {
                        **self._plan_item("os1", "debian", debian_root),
                        "title": "Debian",
                        "persistence": True,
                        "persistence_mode": "plain",
                        "persistence_size_gib": 4,
                        "persistence_fs_label": "DEBIAN-PERSIST",
                        "persistence_partlabel": "DEBIAN-PERSIST",
                    },
                    {
                        **self._plan_item("os2", "kali-linux", kali_root),
                        "title": "Kali Linux",
                        "persistence": True,
                        "persistence_mode": "plain",
                        "persistence_size_gib": 4,
                        "persistence_fs_label": "KALI-PERSIST",
                        "persistence_partlabel": "KALI-PERSIST",
                    },
                ],
            }
            with patch("debian_usb.multios.load_multios_plan", return_value=plan):
                with patch("debian_usb.multios._supports_encrypted_persistence", return_value=True):
                    rendered = render_multios_grub("unused.json", "", {"os1": "DEBIAN-UUID", "os2": "KALI-UUID"})
            grub_cfg = rendered["grub_cfg"]
            self.assertIn('submenu "Debian"', grub_cfg)
            self.assertIn('submenu "Kali Linux"', grub_cfg)
            self.assertIn("search --no-floppy --fs-uuid --set=root DEBIAN-UUID", grub_cfg)
            self.assertIn("search --no-floppy --fs-uuid --set=root KALI-UUID", grub_cfg)
            self.assertIn('set isofile="/boot/iso/debian/live/', grub_cfg)
            self.assertIn('set isofile="/boot/iso/kali/live/', grub_cfg)
            self.assertIn("findiso=$isofile", grub_cfg)
            self.assertIn("loopback loop $isofile", grub_cfg)
            self.assertIn("persistence-label=DEBIAN-PERSIST", grub_cfg)
            self.assertIn("persistence-label=KALI-PERSIST", grub_cfg)
            entries = parse_grub_entries(grub_cfg, "boot/grub/grub.cfg")
            for persistence_label in ("DEBIAN-PERSIST", "KALI-PERSIST"):
                persistent_entry = next(
                    entry
                    for entry in entries
                    if f"persistence-label={persistence_label}" in entry.kernel_args
                )
                self.assertIn("persistence-storage=filesystem", persistent_entry.kernel_args.split())
                self.assertIn("union=overlay", persistent_entry.kernel_args.split())

    def test_render_shared_data_multios_isolates_live_iso_from_netinst_and_uses_persistence_label(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            debian_live_root = self._live_boot_root(root / "debian-live-root", "live")
            debian_live_iso = debian_live_root / "payload/debian-live.iso"
            debian_live_iso.parent.mkdir(parents=True)
            debian_live_iso.write_text("live-iso", encoding="utf-8")
            debian_netinst_root = self._installer_boot_root(root / "debian-netinst-root", payload_name="debian-netinst.iso")
            debian_netinst_iso = debian_netinst_root / "payload/debian-netinst.iso"
            debian_netinst_iso.parent.mkdir(parents=True, exist_ok=True)
            debian_netinst_iso.write_text("netinst-iso", encoding="utf-8")
            plan = {
                "schema_version": 1,
                "write_mode": "multi-os",
                "items": [
                    {
                        **self._plan_item("os1", "debian", debian_live_root),
                        "title": "Debian Live",
                        "managed_payload_layout": "shared-data",
                        "persistence": True,
                        "persistence_mode": "plain",
                        "persistence_size_gib": 4,
                        "persistence_fs_label": "DEBIAN-PERSIST",
                        "persistence_partlabel": "DEBIAN-PERSIST",
                    },
                    {
                        **self._plan_item("os2", "debian", debian_netinst_root),
                        "source_role": "netinst",
                        "title": "Debian Netinst",
                        "managed_payload_layout": "shared-data",
                    },
                ],
            }

            with patch("debian_usb.multios.load_multios_plan", return_value=plan):
                rendered = render_multios_grub(
                    "unused.json",
                    "",
                    {"os1": "MULTIBOOT-UUID", "os2": "MULTIBOOT-UUID"},
                )

            grub_cfg = rendered["grub_cfg"]
            self.assertIn('set isofile="/debian-live/debian-live.iso"', grub_cfg)
            self.assertIn("findiso=$isofile", grub_cfg)
            self.assertIn("loopback loop $isofile", grub_cfg)
            self.assertIn("persistence-label=DEBIAN-PERSIST", grub_cfg)
            self.assertIn('set isofile="/debian-netinst/debian-netinst.iso"', grub_cfg)
            self.assertIn("iso-scan/filename=$isofile", grub_cfg)
            self.assertNotIn("live-media-path=/debian-live", grub_cfg)
            self.assertNotIn("live-media=/dev/disk/by-uuid/MULTIBOOT-UUID", grub_cfg)
            self.assertNotIn("findiso=/debian-netinst", grub_cfg)
            self.assertEqual(
                rendered["iso_payloads"],
                [
                    {
                        "source_path": str(debian_live_iso),
                        "target_path": "/debian-live/debian-live.iso",
                    },
                    {
                        "source_path": str(debian_netinst_iso),
                        "target_path": "/debian-netinst/debian-netinst.iso",
                    },
                ],
            )
            self.assertEqual(rendered["payload_extra_assets"], [])

    def test_render_shared_data_uses_live_loopback_and_separate_hd_media_netinst_assets(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            debian_live_root = self._live_boot_root(root / "debian-live-root", "live")
            debian_live_iso = debian_live_root / "payload/debian-live.iso"
            debian_live_iso.parent.mkdir(parents=True)
            debian_live_iso.write_text("opaque-live-iso", encoding="utf-8")
            (debian_live_root / ".disk").mkdir()
            (debian_live_root / ".disk/info").write_text("live metadata", encoding="utf-8")
            (debian_live_root / "install.amd").mkdir()
            (debian_live_root / "install.amd/vmlinuz").write_text("iso-installer-kernel", encoding="utf-8")
            (debian_live_root / "install.amd/initrd.gz").write_text("iso-installer-initrd", encoding="utf-8")
            (debian_live_root / "boot/grub").mkdir(parents=True)
            (debian_live_root / "boot/grub/grub.cfg").write_text(
                """
menuentry 'Live' {
  linux /live/vmlinuz boot=live components quiet
  initrd /live/initrd.img
}
menuentry 'Install from hybrid ISO' {
  linux /install.amd/vmlinuz ---
  initrd /install.amd/initrd.gz
}
""",
                encoding="utf-8",
            )

            debian_netinst_root = self._hd_media_boot_root(root / "debian-netinst-root")
            debian_netinst_iso = debian_netinst_root / "payload/debian-netinst.iso"
            debian_netinst_iso.parent.mkdir(parents=True)
            debian_netinst_iso.write_text("opaque-netinst-iso", encoding="utf-8")
            (debian_netinst_root / ".disk").mkdir()
            (debian_netinst_root / ".disk/info").write_text("netinst metadata", encoding="utf-8")
            (debian_netinst_root / "live").mkdir()
            (debian_netinst_root / "live/vmlinuz").write_text("contaminating-live-kernel", encoding="utf-8")
            (debian_netinst_root / "live/initrd.img").write_text("contaminating-live-initrd", encoding="utf-8")
            with (debian_netinst_root / "boot/grub/grub.cfg").open("a", encoding="utf-8") as handle:
                handle.write(
                    """
menuentry 'Live contaminant' {
  linux /live/vmlinuz boot=live components quiet
  initrd /live/initrd.img
}
"""
                )

            plan = {
                "schema_version": 1,
                "write_mode": "multi-os",
                "use_custom_grub_menu": True,
                "items": [
                    {
                        **self._plan_item("debian-live", "debian", debian_live_root),
                        "title": "Debian Live",
                        "managed_payload_layout": "shared-data",
                    },
                    {
                        **self._plan_item("debian-netinst", "debian", debian_netinst_root),
                        "source_role": "netinst",
                        "title": "Debian Netinst",
                        "managed_payload_layout": "shared-data",
                    },
                ],
            }

            with patch("debian_usb.multios.load_multios_plan", return_value=plan):
                rendered = render_multios_grub(
                    "unused.json",
                    "",
                    {"debian-live": "MULTIBOOT-UUID", "debian-netinst": "MULTIBOOT-UUID"},
                )

            grub_cfg = rendered["grub_cfg"]
            live_block = self._menuentry_block(grub_cfg, "... Debian Live Environment")
            self.assertIn('set isofile="/debian-live/debian-live.iso"', live_block)
            self.assertIn("loopback loop $isofile", live_block)
            self.assertIn("linux (loop)/live/vmlinuz", live_block)
            self.assertIn("findiso=$isofile", live_block)
            self.assertIn("initrd (loop)/live/initrd.img", live_block)
            self.assertNotIn("set kernel=", live_block)
            self.assertNotIn("set initrd=", live_block)
            self.assertNotIn("/install.amd/", live_block)

            netinst_block = self._menuentry_block(grub_cfg, "... Debian Netinst Install")
            self.assertIn('set kernel="/debian-netinst/vmlinuz"', netinst_block)
            self.assertIn('set initrd="/debian-netinst/initrd.gz"', netinst_block)
            self.assertIn('set isofile="/debian-netinst/debian-netinst.iso"', netinst_block)
            self.assertIn("linux $kernel", netinst_block)
            self.assertIn("iso-scan/filename=$isofile", netinst_block)
            self.assertIn("initrd $initrd", netinst_block)
            self.assertNotIn("loopback", netinst_block)
            self.assertNotIn("/live/", netinst_block)
            self.assertNotIn("squashfs", netinst_block)
            self.assertNotIn("Live contaminant", grub_cfg)
            self.assertNotIn("Install from hybrid ISO", grub_cfg)

            self.assertEqual(
                rendered["payload_boot_assets"],
                [
                    {
                        "iso_path": str(debian_netinst_root),
                        "source_path": "/hd-media/vmlinuz",
                        "target_path": "/debian-netinst/vmlinuz",
                    },
                    {
                        "iso_path": str(debian_netinst_root),
                        "source_path": "/hd-media/initrd.gz",
                        "target_path": "/debian-netinst/initrd.gz",
                    },
                ],
            )
            manifests = (
                rendered["iso_payloads"]
                + rendered["payload_boot_assets"]
                + rendered["payload_extra_assets"]
                + rendered["installer_media_trees"]
            )
            self.assertFalse(any(".disk" in str(value) for row in manifests for value in row.values()))

    def test_validate_multios_plan_rejects_netinst_iso_without_hd_media_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            primary_iso = root / "debian-live.iso"
            netinst_iso = root / "debian-netinst.iso"
            primary_iso.write_text("live", encoding="utf-8")
            netinst_iso.write_text("netinst", encoding="utf-8")
            plan = {
                "schema_version": 1,
                "write_mode": "multi-os",
                "items": [
                    self._plan_item("os1", "debian", primary_iso),
                    {
                        **self._plan_item("os2", "debian", netinst_iso),
                        "source_role": "netinst",
                    },
                ],
            }

            with self.assertRaisesRegex(ValueError, "prepared hd-media source directory"):
                validate_multios_plan(plan)

    def test_render_multios_rejects_netinst_entries_that_boot_iso_internal_kernel(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            netinst_root = self._installer_boot_root(root / "debian-netinst-root")
            (netinst_root / "install.amd").mkdir()
            (netinst_root / "install.amd/vmlinuz").write_text("iso-kernel", encoding="utf-8")
            (netinst_root / "install.amd/initrd.gz").write_text("iso-initrd", encoding="utf-8")
            (netinst_root / "boot/grub/grub.cfg").write_text(
                """
menuentry 'Install from ISO internals' {
  linux /install.amd/vmlinuz ---
  initrd /install.amd/initrd.gz
}
""",
                encoding="utf-8",
            )
            plan = {
                "schema_version": 1,
                "write_mode": "multi-os",
                "items": [
                    {
                        **self._plan_item("netinst", "debian", netinst_root),
                        "source_role": "netinst",
                    }
                ],
            }

            with patch("debian_usb.multios.load_multios_plan", return_value=plan):
                with self.assertRaisesRegex(
                    ValueError,
                    "must use separate hd-media/vmlinuz and hd-media/initrd.gz",
                ):
                    render_multios_grub("unused.json", "", {"netinst": "NETINST-UUID"})

    def test_render_shared_data_multios_rejects_live_source_without_iso_payload(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            debian_live_root = self._live_boot_root(Path(temp_dir) / "debian-live-root", "live")
            plan = {
                "schema_version": 1,
                "write_mode": "multi-os",
                "items": [
                    {
                        **self._plan_item("os1", "debian", debian_live_root),
                        "managed_payload_layout": "shared-data",
                    }
                ],
            }

            with patch("debian_usb.multios.load_multios_plan", return_value=plan):
                with self.assertRaisesRegex(ValueError, "requires an ISO payload"):
                    render_multios_grub("unused.json", "", {"os1": "MULTIBOOT-UUID"})

    def test_render_multios_grub_rewrites_stale_override_persistence_labels(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            debian_root = self._live_boot_root(root / "debian-root", "live")
            kali_root = self._live_boot_root(root / "kali-root", "live")
            plan = {
                "schema_version": 1,
                "write_mode": "multi-os",
                "items": [
                    {
                        **self._plan_item("os1", "debian", debian_root),
                        "title": "Debian",
                        "kernel_args": "boot=live quiet splash persistence persistence-label=persistence persistence-media=removable-usb",
                        "persistence": True,
                        "persistence_mode": "plain",
                        "persistence_size_gib": 4,
                        "persistence_fs_label": "DEBIAN-PERSIST",
                        "persistence_partlabel": "DEBIAN-PERSIST",
                    },
                    {
                        **self._plan_item("os2", "kali-linux", kali_root),
                        "title": "Kali Linux",
                        "kernel_args": "boot=live quiet splash persistence",
                        "persistence": True,
                        "persistence_mode": "encrypted",
                        "persistence_size_gib": 4,
                        "persistence_fs_label": "KALI-PERSIST",
                        "persistence_partlabel": "KALI-PERSIST",
                    },
                ],
            }
            with patch("debian_usb.multios.load_multios_plan", return_value=plan):
                with patch("debian_usb.multios._supports_encrypted_persistence", return_value=True):
                    rendered = render_multios_grub("unused.json", "", {"os1": "DEBIAN-UUID", "os2": "KALI-UUID"})
            grub_cfg = rendered["grub_cfg"]
            self.assertIn("persistence-label=DEBIAN-PERSIST", grub_cfg)
            self.assertIn("persistence-label=KALI-PERSIST", grub_cfg)
            self.assertNotIn("persistence-label=persistence", grub_cfg)

    def test_render_multios_grub_with_boot_assets_uuid_includes_mok_entry_and_assets(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            debian_root = self._live_boot_root(root / "debian-root", "live")
            kali_root = self._live_boot_root(root / "kali-root", "live")
            plan = {
                "schema_version": 1,
                "write_mode": "multi-os",
                "items": [
                    self._plan_item("os1", "debian", debian_root),
                    self._plan_item("os2", "kali-linux", kali_root),
                ],
            }
            with patch("debian_usb.multios.load_multios_plan", return_value=plan):
                rendered = render_multios_grub("unused.json", "", {"os1": "DEBIAN-UUID", "os2": "KALI-UUID"}, boot_assets_uuid="ESP-UUID")
            grub_cfg = rendered["grub_cfg"]
            self.assertIn("MOK Enrollment", grub_cfg)
            self.assertIn("set check_signatures=enforce", grub_cfg)
            self.assertIn("trust --skip-sig (${trust_root})/secureboot/grub-signing.pub", grub_cfg)
            self.assertLess(
                grub_cfg.index("trust --skip-sig (${trust_root})/secureboot/grub-signing.pub"),
                grub_cfg.index("set check_signatures=enforce"),
            )
            self.assertNotIn("Loading signed kernel. Trust it via the selected Secure Boot mode before booting.", grub_cfg)
            self.assertIn("loopback loop $isofile", grub_cfg)
            self.assertIn("chainloader /EFI/debian-usb/mok/mmx64.efi", grub_cfg)
            self.assertIn("chainloader /EFI/debian-usb/mok/shimx64.efi", grub_cfg)
            self.assertNotIn("echo Direct MokManager launch failed; trying shim fallback.", grub_cfg)
            self.assertIn("UEFI Keys", grub_cfg)
            self.assertIn("chainloader /EFI/debian-usb/updatevars/UpdateVars.efi -a -e db /secureboot/db.esl", grub_cfg)
            self.assertNotIn("chainloader (${boot_root})/EFI/debian-usb/mok/shimx64.efi", grub_cfg)
            self.assertNotIn("/EFI/debian-usb/assets/os1/live/", grub_cfg)
            self.assertNotIn("/EFI/debian-usb/assets/os2/live/", grub_cfg)
            self.assertEqual(rendered["signed_kernel_assets"], [])
            self.assertEqual(rendered["boot_initrd_assets"], [])

    def test_render_shared_data_secure_boot_keeps_live_in_signed_iso_and_netinst_in_hd_media_assets(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            debian_live_root = self._live_boot_root(root / "debian-live-root", "live")
            debian_live_iso = debian_live_root / "payload/debian-live.iso"
            debian_live_iso.parent.mkdir(parents=True)
            debian_live_iso.write_text("opaque-live-iso", encoding="utf-8")
            debian_netinst_root = self._installer_boot_root(
                root / "debian-netinst-root",
                payload_name="debian-netinst.iso",
            )
            plan = {
                "schema_version": 1,
                "write_mode": "multi-os",
                "use_custom_grub_menu": True,
                "items": [
                    {
                        **self._plan_item("live", "debian", debian_live_root),
                        "managed_payload_layout": "shared-data",
                    },
                    {
                        **self._plan_item("netinst", "debian", debian_netinst_root),
                        "source_role": "netinst",
                        "managed_payload_layout": "shared-data",
                    },
                ],
            }

            with patch("debian_usb.multios.load_multios_plan", return_value=plan):
                rendered = render_multios_grub(
                    "unused.json",
                    "",
                    {"live": "MULTIBOOT-UUID", "netinst": "MULTIBOOT-UUID"},
                    boot_assets_uuid="ESP-UUID",
                )

            live_block = self._menuentry_block(rendered["grub_cfg"], "... Debian Live Environment")
            self.assertIn('set isofile="/debian-live/debian-live.iso"', live_block)
            self.assertIn("if loopback loop $isofile; then", live_block)
            self.assertIn("linux (loop)/live/vmlinuz", live_block)
            self.assertIn("initrd (loop)/live/initrd.img", live_block)
            self.assertNotIn("set kernel=", live_block)
            self.assertNotIn("set initrd=", live_block)

            netinst_block = self._menuentry_block(rendered["grub_cfg"], "... Debian Netinst Install")
            self.assertIn(
                'set kernel="(${boot_root})/EFI/debian-usb/assets/netinst/hd-media/vmlinuz"',
                netinst_block,
            )
            self.assertIn(
                'set initrd="(${boot_root})/EFI/debian-usb/assets/netinst/hd-media/initrd.gz"',
                netinst_block,
            )
            self.assertIn('set isofile="/debian-netinst/debian-netinst.iso"', netinst_block)
            self.assertIn("linux $kernel", netinst_block)
            self.assertIn("iso-scan/filename=$isofile", netinst_block)
            self.assertIn("initrd $initrd", netinst_block)
            self.assertNotIn("loopback", netinst_block)
            self.assertNotIn("/live/", netinst_block)

            self.assertEqual(
                rendered["signed_kernel_assets"],
                [
                    {
                        "iso_path": str(debian_netinst_root),
                        "source_path": "/hd-media/vmlinuz",
                        "asset_path": "/EFI/debian-usb/assets/netinst/hd-media/vmlinuz",
                    }
                ],
            )
            self.assertEqual(
                rendered["boot_initrd_assets"],
                [
                    {
                        "iso_path": str(debian_netinst_root),
                        "source_path": "/hd-media/initrd.gz",
                        "asset_path": "/EFI/debian-usb/assets/netinst/hd-media/initrd.gz",
                    }
                ],
            )
            self.assertFalse(
                any(
                    "/live/" in asset["source_path"]
                    for asset in rendered["signed_kernel_assets"] + rendered["boot_initrd_assets"]
                )
            )

    def test_render_multios_grub_custom_menu_preserved_entries_keep_secure_boot_namespaces(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            debian_root = self._live_boot_root(root / "debian-root", "live")
            kali_root = self._live_boot_root(root / "kali-root", "live")
            (debian_root / "live/failsafe-vmlinuz").write_text("", encoding="utf-8")
            (debian_root / "live/failsafe-initrd.img").write_text("", encoding="utf-8")
            (debian_root / "boot/grub").mkdir(parents=True)
            (debian_root / "boot/grub/grub.cfg").write_text(
                """
menuentry 'Live' {
  linux /live/vmlinuz boot=live components quiet
  initrd /live/initrd.img
}
menuentry 'Live failsafe' {
  linux /live/failsafe-vmlinuz boot=live components debug
  initrd /live/failsafe-initrd.img
}
""",
                encoding="utf-8",
            )
            plan = {
                "schema_version": 1,
                "write_mode": "multi-os",
                "use_custom_grub_menu": True,
                "preserve_upstream_grub_entries": True,
                "items": [
                    self._plan_item("os1", "debian", debian_root),
                    self._plan_item("os2", "kali-linux", kali_root),
                ],
            }
            with patch("debian_usb.multios.load_multios_plan", return_value=plan):
                rendered = render_multios_grub(
                    "unused.json",
                    "",
                    {"os1": "DEBIAN-UUID", "os2": "KALI-UUID"},
                    boot_assets_uuid="ESP-UUID",
                )
            grub_cfg = rendered["grub_cfg"]
            self.assertIn('submenu "Debian Legacy ..."', grub_cfg)
            self.assertIn('submenu "Kali Legacy ..."', grub_cfg)
            self.assertNotIn('submenu "Debian Live Preserved ..."', grub_cfg)
            self.assertNotIn('submenu "Kali Live Preserved ..."', grub_cfg)
            self.assertIn("linux (loop)/live/vmlinuz", grub_cfg)
            self.assertIn("initrd (loop)/live/initrd.img", grub_cfg)
            self.assertIn("linux (loop)/live/failsafe-vmlinuz", grub_cfg)
            self.assertEqual(rendered["signed_kernel_assets"], [])
            self.assertEqual(rendered["boot_initrd_assets"], [])
            self.assertNotIn("set=payload_root", grub_cfg)

    def test_render_multios_grub_custom_secure_boot_omits_memtest_assets(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            debian_root = self._live_boot_root(root / "debian-root", "live")
            kali_root = self._live_boot_root(root / "kali-root", "live")
            (kali_root / "boot/grub").mkdir(parents=True)
            (kali_root / "boot/grub/grub.cfg").write_text(
                """
menuentry 'Memory test' {
  linux /live/memtest.bin
}
menuentry 'Live system (amd64)' {
  linux /live/vmlinuz boot=live components quiet
  initrd /live/initrd.img
}
""",
                encoding="utf-8",
            )
            plan = {
                "schema_version": 1,
                "write_mode": "multi-os",
                "use_custom_grub_menu": True,
                "preserve_upstream_grub_entries": True,
                "items": [
                    self._plan_item("os1", "debian", debian_root),
                    self._plan_item("os2", "kali-linux", kali_root),
                ],
            }
            with patch("debian_usb.multios.load_multios_plan", return_value=plan):
                rendered = render_multios_grub(
                    "unused.json",
                    "",
                    {"os1": "DEBIAN-UUID", "os2": "KALI-UUID"},
                    boot_assets_uuid="ESP-UUID",
                )

            grub_cfg = rendered["grub_cfg"]
            self.assertNotIn("memtest", grub_cfg.lower())
            self.assertNotIn(
                {
                    "iso_path": str(kali_root),
                    "source_path": "/live/memtest.bin",
                    "asset_path": "/EFI/debian-usb/assets/os2/live/memtest.bin",
                },
                rendered["signed_kernel_assets"],
            )
            self.assertIn("linux (loop)/live/vmlinuz", grub_cfg)

    def test_render_multios_grub_keeps_same_family_boot_assets_separate(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            debian_live_root = self._live_boot_root(root / "debian-live-root", "live")
            debian_netinst_root = self._installer_boot_root(root / "debian-netinst-root")
            plan = {
                "schema_version": 1,
                "write_mode": "multi-os",
                "use_custom_grub_menu": True,
                "items": [
                    self._plan_item("os1", "debian", debian_live_root),
                    {
                        **self._plan_item("os2", "debian", debian_netinst_root),
                        "source_role": "netinst",
                        "preseed": True,
                    },
                ],
            }
            with patch("debian_usb.multios.load_multios_plan", return_value=plan):
                rendered = render_multios_grub(
                    "unused.json",
                    "",
                    {"os1": "DEBIAN-LIVE-UUID", "os2": "DEBIAN-NETINST-UUID"},
                )
            grub_cfg = rendered["grub_cfg"]
            targets = [payload["target_path"] for payload in rendered["iso_payloads"]]
            self.assertEqual(len(targets), len(set(targets)))
            entries = parse_grub_entries(grub_cfg, "boot/grub/grub.cfg")
            live_ram = next(entry for entry in entries if entry.title == "... Debian Live Environment (RAM)")
            self.assertIn("toram=filesystem.squashfs", live_ram.kernel_args.split())
            netinst_entries = [entry for entry in entries if "Debian Netinst" in " ".join(entry.menu_path)]
            self.assertTrue(netinst_entries)
            self.assertFalse(
                any(
                    token == "toram" or token.startswith("toram=")
                    for entry in netinst_entries
                    for token in entry.kernel_args.split()
                )
            )

    def test_render_multios_grub_honors_item_live_toram_with_persistence(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            debian_root = self._live_boot_root(root / "debian-root", "live")
            kali_root = self._live_boot_root(root / "kali-root", "live")
            plan = {
                "schema_version": 1,
                "write_mode": "multi-os",
                "items": [
                    {
                        **self._plan_item("os1", "debian", debian_root),
                        "live_toram": True,
                        "persistence": True,
                        "persistence_mode": "plain",
                        "persistence_size_gib": 4,
                        "persistence_fs_label": "DEBIAN-PERSIST",
                        "persistence_partlabel": "DEBIAN-PERSIST",
                    },
                    self._plan_item("os2", "kali-linux", kali_root),
                ],
            }
            with patch("debian_usb.multios.load_multios_plan", return_value=plan):
                rendered = render_multios_grub("unused.json", "", {"os1": "DEBIAN-UUID", "os2": "KALI-UUID"})
            grub_cfg = rendered["grub_cfg"]
            entries = parse_grub_entries(grub_cfg, "boot/grub/grub.cfg")
            ram_persistence = next(
                entry
                for entry in entries
                if "persistence-label=DEBIAN-PERSIST" in entry.kernel_args
                and any(token.startswith("toram=") for token in entry.kernel_args.split())
            )
            tokens = ram_persistence.kernel_args.split()
            self.assertIn("toram=filesystem.squashfs", tokens)
            self.assertIn("persistence-label=DEBIAN-PERSIST", tokens)
            self.assertIn("persistence-storage=filesystem", tokens)
            self.assertIn("union=overlay", tokens)

    def test_render_multios_grub_adds_netinst_submenu_for_same_profile(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            debian_live_root = self._live_boot_root(root / "debian-live-root", "live")
            debian_netinst_root = self._installer_boot_root(root / "debian-netinst-root")
            kali_root = self._live_boot_root(root / "kali-root", "live")
            plan = {
                "schema_version": 1,
                "write_mode": "multi-os",
                "items": [
                    {
                        **self._plan_item("os1", "debian", debian_live_root),
                        "title": "Debian",
                    },
                    {
                        **self._plan_item("os2", "debian", debian_netinst_root),
                        "source_role": "netinst",
                        "title": "Debian Netinst",
                        "preseed": True,
                    },
                    {
                        **self._plan_item("os3", "kali-linux", kali_root),
                        "title": "Kali Linux",
                    },
                ],
            }
            with patch("debian_usb.multios.load_multios_plan", return_value=plan):
                rendered = render_multios_grub(
                    "unused.json",
                    "",
                    {"os1": "DEBIAN-LIVE-UUID", "os2": "DEBIAN-NETINST-UUID", "os3": "KALI-UUID"},
                )
            grub_cfg = rendered["grub_cfg"]
            self.assertIn('submenu "Debian Netinst"', grub_cfg)
            self.assertIn("search --no-floppy --fs-uuid --set=root DEBIAN-NETINST-UUID", grub_cfg)
            self.assertIn('set kernel="/boot/debian/netinst/vmlinuz"', grub_cfg)
            self.assertIn('set initrd="/boot/debian/netinst/initrd.gz"', grub_cfg)
            self.assertIn('set isofile="/boot/iso/debian/netinst/debian-netinst-root.iso"', grub_cfg)
            self.assertIn("linux $kernel", grub_cfg)
            self.assertIn("iso-scan/filename=$isofile", grub_cfg)
            self.assertIn("initrd $initrd", grub_cfg)
            self.assertIn('menuentry "Install"', grub_cfg)
            self.assertIn('menuentry "Graphical install"', grub_cfg)
            self.assertIn("auto=true", grub_cfg)
            self.assertNotIn("live-media=/dev/disk/by-uuid/DEBIAN-NETINST-UUID", grub_cfg)
            self.assertEqual(rendered["media_classes"], ["live", "installer", "live"])

    def test_render_multios_grub_keeps_fixed_debian_preseed_submenus_even_when_netinst_preseed_flag_is_false(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            debian_live_root = self._live_boot_root(root / "debian-live-root", "live")
            debian_netinst_root = self._installer_boot_root(root / "debian-netinst-root")
            kali_root = self._live_boot_root(root / "kali-root", "live")
            plan = {
                "schema_version": 1,
                "write_mode": "multi-os",
                "use_custom_grub_menu": True,
                "items": [
                    {
                        **self._plan_item("os1", "debian", debian_live_root),
                        "title": "Debian",
                    },
                    {
                        **self._plan_item("os2", "debian", debian_netinst_root),
                        "source_role": "netinst",
                        "title": "Debian Netinst",
                        "preseed": False,
                    },
                    {
                        **self._plan_item("os3", "kali-linux", kali_root),
                        "title": "Kali Linux",
                    },
                ],
            }
            with patch("debian_usb.multios.load_multios_plan", return_value=plan):
                rendered = render_multios_grub(
                    "unused.json",
                    "",
                    {"os1": "DEBIAN-LIVE-UUID", "os2": "DEBIAN-NETINST-UUID", "os3": "KALI-UUID"},
                )
            grub_cfg = rendered["grub_cfg"]
            self.assertIn('submenu "Debian Netinst ..."', grub_cfg)
            self.assertIn('submenu "Debian Netinst Install (HTTP Preseed) ..."', grub_cfg)
            self.assertIn('submenu "Debian Netinst Install (USB Preseed) ..."', grub_cfg)
            self.assertIn('submenu "Preseed Internal ..."', grub_cfg)
            self.assertIn('submenu "Preseed Public ..."', grub_cfg)

    def test_render_multios_grub_applies_configured_network_interface_to_all_netinst_installer_entries(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            configured_interface = _template_kernel_arg("PRESEED_COMMON_KERNEL_ARGS", "interface")
            debian_netinst_root = self._installer_boot_root(root / "debian-netinst-root")
            kali_root = self._live_boot_root(root / "kali-root", "live")
            plan = {
                "schema_version": 1,
                "write_mode": "multi-os",
                "items": [
                    {
                        **self._plan_item("os1", "debian", debian_netinst_root),
                        "source_role": "netinst",
                        "title": "Debian Netinst",
                    },
                    {
                        **self._plan_item("os2", "kali-linux", kali_root),
                        "title": "Kali Linux",
                    },
                ],
            }
            with patch("debian_usb.multios.load_multios_plan", return_value=plan):
                rendered = render_multios_grub(
                    "unused.json",
                    "",
                    {"os1": "DEBIAN-NETINST-UUID", "os2": "KALI-UUID"},
                )
            grub_cfg = rendered["grub_cfg"]
            install_start = grub_cfg.index('menuentry "Install" {')
            install_end = grub_cfg.index("\n    }", install_start)
            install_block = grub_cfg[install_start:install_end]
            graphical_start = grub_cfg.index('menuentry "Graphical install" {')
            graphical_end = grub_cfg.index("\n    }", graphical_start)
            graphical_installer_block = grub_cfg[graphical_start:graphical_end]
            # The configured installer interface must be present on every installer
            # entry, including the graphical installer.
            self.assertIn(configured_interface, install_block.split())
            self.assertIn(configured_interface, graphical_installer_block.split())
            self.assertIn("iso-scan/filename=$isofile", install_block)
            self.assertIn("iso-scan/filename=$isofile", graphical_installer_block)
            self.assertIn("/boot/debian/netinst/vmlinuz", install_block)
            self.assertIn("/boot/debian/netinst/vmlinuz", graphical_installer_block)

    def test_validate_multios_plan_allows_single_profile_with_primary_and_netinst(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            primary_iso = root / "debian-live.iso"
            primary_iso.write_text("iso", encoding="utf-8")
            netinst_root = self._installer_boot_root(root / "debian-netinst-root")
            plan = {
                "schema_version": 1,
                "write_mode": "multi-os",
                "items": [
                    self._plan_item("os1", "debian", primary_iso),
                    {
                        **self._plan_item("os2", "debian", netinst_root),
                        "source_role": "netinst",
                    },
                ],
            }
            validated = validate_multios_plan(plan)
            self.assertEqual(len(validated["items"]), 2)

    def test_validate_multios_plan_rejects_unsupported_netinst_source_role_for_tails(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            debian_iso = root / "debian.iso"
            debian_iso.write_text("iso", encoding="utf-8")
            tails_iso = root / "tails-netinst.iso"
            tails_iso.write_text("iso", encoding="utf-8")
            plan = {
                "schema_version": 1,
                "write_mode": "multi-os",
                "items": [
                    self._plan_item("os1", "debian", debian_iso),
                    {
                        **self._plan_item("os2", "tails", tails_iso),
                        "source_role": "netinst",
                    },
                ],
            }
            with self.assertRaisesRegex(ValueError, "profile does not support source role"):
                validate_multios_plan(plan)

    def test_validate_multios_plan_allows_netboot_directory_source(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            debian_iso = root / "debian.iso"
            debian_iso.write_text("iso", encoding="utf-8")
            netboot_root = self._netboot_boot_root(root / "debian-netboot-root")
            plan = {
                "schema_version": 1,
                "write_mode": "multi-os",
                "items": [
                    self._plan_item("os1", "debian", debian_iso),
                    {
                        **self._plan_item("os2", "debian", netboot_root),
                        "source_role": "netboot",
                    },
                ],
            }
            validated = validate_multios_plan(plan)
            self.assertEqual(validated["items"][1]["iso_path"], str(netboot_root.resolve()))

    def test_validate_multios_plan_rejects_plain_tails_persistence(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            tails_iso = root / "tails.iso"
            tails_iso.write_text("iso", encoding="utf-8")
            plan = {
                "schema_version": 1,
                "write_mode": "multi-os",
                "items": [
                    {
                        **self._plan_item("os1", "tails", tails_iso),
                        "persistence": True,
                        "persistence_mode": "plain",
                        "persistence_size_gib": 4,
                        "persistence_fs_label": "TailsData",
                        "persistence_partlabel": "TailsData",
                    }
                ],
            }
            with self.assertRaisesRegex(ValueError, "Tails persistence must be encrypted"):
                validate_multios_plan(plan)

    def test_validate_multios_plan_rejects_preseed_on_fixed_primary_live_source(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            iso = Path(temp_dir) / "debian.iso"
            iso.write_text("iso", encoding="utf-8")
            plan = {
                "schema_version": 1,
                "write_mode": "multi-os",
                "use_custom_grub_menu": True,
                "items": [
                    {
                        **self._plan_item("os1", "debian", iso),
                        "preseed": True,
                    },
                    self._plan_item("os2", "kali-linux", iso),
                ],
            }
            with self.assertRaisesRegex(ValueError, "preseed entries require an installer-capable managed source role"):
                validate_multios_plan(plan)

    def test_validate_multios_plan_rejects_preseed_on_nonfixed_primary_source(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            iso = Path(temp_dir) / "tails.iso"
            iso.write_text("iso", encoding="utf-8")
            plan = {
                "schema_version": 1,
                "write_mode": "multi-os",
                "use_custom_grub_menu": True,
                "items": [
                    {
                        **self._plan_item("os1", "tails", iso),
                        "preseed": True,
                    },
                    self._plan_item("os2", "kali-linux", iso),
                ],
            }
            with self.assertRaisesRegex(ValueError, "preseed entries require an installer-capable managed source role"):
                validate_multios_plan(plan)

    def test_render_multios_grub_custom_menu_groups_os_families_and_skips_single_profile_submenu(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)

            debian_live_root = root / "debian-live-root"
            (debian_live_root / "EFI/boot").mkdir(parents=True)
            (debian_live_root / "EFI/boot/bootx64.efi").write_text("", encoding="utf-8")
            (debian_live_root / "boot/grub").mkdir(parents=True)
            (debian_live_root / "live").mkdir()
            (debian_live_root / "live/vmlinuz").write_text("", encoding="utf-8")
            (debian_live_root / "live/initrd.img").write_text("", encoding="utf-8")
            (debian_live_root / "install").mkdir()
            (debian_live_root / "install/vmlinuz").write_text("", encoding="utf-8")
            (debian_live_root / "install/initrd.gz").write_text("", encoding="utf-8")
            (debian_live_root / "boot/grub/grub.cfg").write_text(
                """
menuentry 'Live system (amd64)' {
  linux /live/vmlinuz boot=live components quiet ---
  initrd /live/initrd.img
}
menuentry 'Start installer' {
  linux /install/vmlinuz ---
  initrd /install/initrd.gz
}
submenu 'Text installer ...' {
menuentry 'Expert install' {
  linux /install/vmlinuz priority=low ---
  initrd /install/initrd.gz
}
menuentry 'Automated install' {
  linux /install/vmlinuz auto=true priority=critical ---
  initrd /install/initrd.gz
}
menuentry 'Rescue mode' {
  linux /install/vmlinuz rescue/enable=true ---
  initrd /install/initrd.gz
}
}
""",
                encoding="utf-8",
            )

            debian_netinst_root = root / "debian-netinst-root"
            (debian_netinst_root / "EFI/boot").mkdir(parents=True)
            (debian_netinst_root / "EFI/boot/bootx64.efi").write_text("", encoding="utf-8")
            (debian_netinst_root / "hd-media").mkdir(parents=True)
            (debian_netinst_root / "hd-media/vmlinuz").write_text("", encoding="utf-8")
            (debian_netinst_root / "hd-media/initrd.gz").write_text("", encoding="utf-8")
            (debian_netinst_root / "payload").mkdir(parents=True)
            (debian_netinst_root / "payload/debian-netinst-root.iso").write_text("iso", encoding="utf-8")
            (debian_netinst_root / "boot/grub").mkdir(parents=True)
            (debian_netinst_root / "boot/grub/grub.cfg").write_text(
                """
menuentry 'Install' {
  linux /hd-media/vmlinuz ---
  initrd /hd-media/initrd.gz
}
submenu 'Advanced options ...' {
menuentry '... Expert install' {
  linux /hd-media/vmlinuz priority=low ---
  initrd /hd-media/initrd.gz
}
menuentry '... Automated install' {
  linux /hd-media/vmlinuz auto=true priority=critical ---
  initrd /hd-media/initrd.gz
}
menuentry '... Rescue mode' {
  linux /hd-media/vmlinuz rescue/enable=true ---
  initrd /hd-media/initrd.gz
}
}
""",
                encoding="utf-8",
            )

            tails_root = self._live_boot_root(root / "tails-root", "live")

            plan = {
                "schema_version": 1,
                "write_mode": "multi-os",
                "use_custom_grub_menu": True,
                "items": [
                    {
                        **self._plan_item("os1", "debian", debian_live_root),
                        "title": "Debian Live",
                        "persistence": True,
                        "persistence_mode": "plain",
                        "persistence_size_gib": 4,
                        "persistence_fs_label": "DEBIAN-PERSIST",
                        "persistence_partlabel": "DEBIAN-PERSIST",
                    },
                    {
                        **self._plan_item("os2", "debian", debian_netinst_root),
                        "source_role": "netinst",
                        "title": "Debian Netinst",
                        "preseed": True,
                    },
                    {
                        **self._plan_item("os3", "tails", tails_root),
                        "managed_payload_layout": "iso-store",
                        "title": "Tails",
                    },
                ],
            }

            with patch("debian_usb.multios.load_multios_plan", return_value=plan):
                rendered = render_multios_grub(
                    "unused.json",
                    "",
                    {"os1": "DEBIAN-LIVE-UUID", "os2": "DEBIAN-NETINST-UUID", "os3": "TAILS-UUID"},
                )

            grub_cfg = rendered["grub_cfg"]
            self.assertIn("set timeout=-1", grub_cfg)
            self.assertIn('submenu "Debian ..."', grub_cfg)
            self.assertIn('submenu "Debian Live ..."', grub_cfg)
            self.assertIn('submenu "Debian Netinst ..."', grub_cfg)
            self.assertIn('menuentry "... Debian Live Environment (Persistence)"', grub_cfg)
            self.assertIn('menuentry "... Debian Live Environment (RAM + Persistence)"', grub_cfg)
            self.assertNotIn('submenu "Debian Live Install (HTTP Preseed) ..."', grub_cfg)
            self.assertNotIn('submenu "Debian Live Install (USB Preseed) ..."', grub_cfg)
            self.assertIn('submenu "Preseed Internal ..."', grub_cfg)
            self.assertIn('submenu "Preseed Public ..."', grub_cfg)
            self.assertIn('submenu "Tails ..."', grub_cfg)
            self.assertIn('submenu "Tails Live ..."', grub_cfg)
            self.assertNotIn('submenu "Ubuntu ..."', grub_cfg)
            self.assertLess(grub_cfg.index('submenu "Debian Live ..."'), grub_cfg.index('submenu "Debian Netinst ..."'))
            self.assertLess(grub_cfg.index('submenu "Tails ..."'), grub_cfg.index('submenu "Boot from Internal Drive"'))

            entries = parse_grub_entries(grub_cfg, "boot/grub/grub.cfg")
            self.assertTrue(
                any(
                    entry.title == "... Debian Live Environment"
                    and entry.menu_path == ("Debian ...", "Debian Live ...")
                    for entry in entries
                )
            )
            self.assertFalse(
                any("Debian Live Install (" in entry.title for entry in entries),
                "Live source entries must not be reused as installer preset entries",
            )
            tails_ram = next(
                entry
                for entry in entries
                if entry.title == "... Tails Live Environment (RAM)"
                and entry.menu_path == ("Tails ...", "Tails Live ...")
            )
            self.assertIn("toram=filesystem.squashfs", tails_ram.kernel_args.split())
            self.assertNotIn("toram", tails_ram.kernel_args.split())

            default_title = "... Debian Netinst Install" + _repo_preset_label(
                "debian", "debian", "PRESEED_ONE_ARGS_DEBIAN"
            )
            dualboot_title = "... Debian Netinst Install" + _repo_preset_label(
                "debian", "debian", "PRESEED_THREE_ARGS_DEBIAN"
            )
            online_preset = next(
                entry
                for entry in entries
                if entry.title == default_title
                and entry.menu_path
                == (
                    "Debian ...",
                    "Debian Netinst ...",
                    "Debian Netinst Install (HTTP Preseed) ...",
                    "Preseed Internal ...",
                )
            )
            self.assertEqual(online_preset.kernel_path, "/boot/debian/netinst/vmlinuz")
            self.assertEqual(online_preset.initrd_path, "/boot/debian/netinst/initrd.gz")
            configured_priority = _template_kernel_arg("PRESEED_COMMON_KERNEL_ARGS", "priority")
            priority_tokens = [
                token for token in online_preset.kernel_args.split() if token.startswith("priority=")
            ]
            self.assertEqual(priority_tokens, [configured_priority])
            self.assertIn("iso-scan/filename=/boot/iso/debian/netinst/debian-netinst-root.iso", online_preset.kernel_args)
            self.assertIn(
                _template_kernel_arg("PRESEED_COMMON_KERNEL_ARGS", "interface"),
                online_preset.kernel_args.split(),
            )
            self.assertFalse(
                any(token == "toram" or token.startswith("toram=") for token in online_preset.kernel_args.split())
            )

            offline_preset = next(
                entry
                for entry in entries
                if entry.title == default_title
                and entry.menu_path
                == (
                    "Debian ...",
                    "Debian Netinst ...",
                    "Debian Netinst Install (USB Preseed) ...",
                    "Preseed Public ...",
                )
            )
            self.assertEqual(offline_preset.kernel_path, "/boot/debian/netinst/vmlinuz")
            self.assertEqual(offline_preset.initrd_path, "/boot/debian/netinst/initrd.gz")
            self.assertEqual(
                _seed_transport_tokens(offline_preset.kernel_args),
                [f"url={load_template_config()['DEBIAN_PRESEED_PUBLIC_URL']}"],
            )

            dualboot_preset = next(
                entry
                for entry in entries
                if entry.title == dualboot_title
                and entry.menu_path
                == (
                    "Debian ...",
                    "Debian Netinst ...",
                    "Debian Netinst Install (USB Preseed) ...",
                    "Preseed Internal ...",
                )
            )
            self.assertIn(
                _grub_escaped_config_arg("PRESEED_THREE_ARGS_DEBIAN", "classes"),
                dualboot_preset.kernel_args.split(),
            )

    def test_render_multios_grub_keeps_family_legacy_submenu_after_netboot(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            debian_netinst_root = self._installer_boot_root(root / "debian-netinst-root")
            debian_netboot_root = self._netboot_boot_root(root / "debian-netboot-root")
            plan = {
                "schema_version": 1,
                "write_mode": "multi-os",
                "use_custom_grub_menu": True,
                "items": [
                    {
                        **self._plan_item("os1", "debian", debian_netinst_root),
                        "source_role": "netinst",
                        "title": "Debian Netinst",
                        "preseed": True,
                        "managed_payload_layout": "shared-data",
                    },
                    {
                        **self._plan_item("os2", "debian", debian_netboot_root),
                        "source_role": "netboot",
                        "title": "Debian Netboot",
                        "preseed": True,
                        "managed_payload_layout": "shared-data",
                    },
                ],
            }

            with patch("debian_usb.multios.load_multios_plan", return_value=plan):
                rendered = render_multios_grub(
                    "unused.json",
                    "",
                    {"os1": "DEBIAN-NETINST-UUID", "os2": "DEBIAN-NETBOOT-UUID"},
                )

            grub_cfg = rendered["grub_cfg"]
            self.assertIn('submenu "Debian Netinst ..."', grub_cfg)
            self.assertIn('submenu "Debian Netboot ..."', grub_cfg)
            self.assertIn('submenu "Debian Legacy ..."', grub_cfg)
            self.assertLess(grub_cfg.index('submenu "Debian Netinst ..."'), grub_cfg.index('submenu "Debian Netboot ..."'))
            self.assertLess(grub_cfg.index('submenu "Debian Netboot ..."'), grub_cfg.index('submenu "Debian Legacy ..."'))

            entries = parse_grub_entries(grub_cfg, "boot/grub/grub.cfg")
            legacy_install = next(
                entry
                for entry in entries
                if entry.title == "... Debian Netboot Install" and entry.menu_path == ("Debian ...", "Debian Legacy ...")
            )
            self.assertEqual(legacy_install.kernel_path, "/debian-netboot/vmlinuz")
            self.assertEqual(legacy_install.initrd_path, "/debian-netboot/initrd.gz")

    def test_render_multios_grub_custom_menu_uses_expert_family_preseed_sources_for_kali(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)

            kali_live_root = root / "kali-live-root"
            (kali_live_root / "EFI/boot").mkdir(parents=True)
            (kali_live_root / "EFI/boot/bootx64.efi").write_text("", encoding="utf-8")
            (kali_live_root / "boot/grub").mkdir(parents=True)
            (kali_live_root / "live").mkdir()
            (kali_live_root / "live/vmlinuz").write_text("", encoding="utf-8")
            (kali_live_root / "live/initrd.img").write_text("", encoding="utf-8")
            (kali_live_root / "install").mkdir()
            (kali_live_root / "install/vmlinuz").write_text("", encoding="utf-8")
            (kali_live_root / "install/initrd.gz").write_text("", encoding="utf-8")
            (kali_live_root / "install/gtk").mkdir(parents=True)
            (kali_live_root / "install/gtk/vmlinuz").write_text("", encoding="utf-8")
            (kali_live_root / "install/gtk/initrd.gz").write_text("", encoding="utf-8")
            (kali_live_root / "boot/grub/grub.cfg").write_text(
                """
menuentry 'Live system (amd64)' {
  linux /live/vmlinuz boot=live components quiet ---
  initrd /live/initrd.img
}
menuentry 'Start installer' {
  linux /install/vmlinuz ---
  initrd /install/initrd.gz
}
submenu 'Text installer ...' {
menuentry 'Expert install' {
  linux /install/vmlinuz priority=low ---
  initrd /install/initrd.gz
}
menuentry 'Automated install' {
  linux /install/gtk/vmlinuz auto=true priority=critical ---
  initrd /install/gtk/initrd.gz
}
menuentry 'Rescue mode' {
  linux /install/vmlinuz rescue/enable=true ---
  initrd /install/initrd.gz
}
}
""",
                encoding="utf-8",
            )

            kali_netinst_root = root / "kali-netinst-root"
            (kali_netinst_root / "EFI/boot").mkdir(parents=True)
            (kali_netinst_root / "EFI/boot/bootx64.efi").write_text("", encoding="utf-8")
            (kali_netinst_root / "hd-media").mkdir(parents=True)
            (kali_netinst_root / "hd-media/vmlinuz").write_text("", encoding="utf-8")
            (kali_netinst_root / "hd-media/initrd.gz").write_text("", encoding="utf-8")
            (kali_netinst_root / "payload").mkdir(parents=True)
            (kali_netinst_root / "payload/kali-netinst-root.iso").write_text("iso", encoding="utf-8")
            (kali_netinst_root / "boot/grub").mkdir(parents=True)
            (kali_netinst_root / "boot/grub/grub.cfg").write_text(
                """
menuentry 'Install' {
  linux /hd-media/vmlinuz ---
  initrd /hd-media/initrd.gz
}
submenu 'Advanced options ...' {
menuentry '... Expert install' {
  linux /hd-media/vmlinuz priority=low ---
  initrd /hd-media/initrd.gz
}
menuentry '... Automated install' {
  linux /hd-media/vmlinuz auto=true priority=critical ---
  initrd /hd-media/initrd.gz
}
menuentry '... Rescue mode' {
  linux /hd-media/vmlinuz rescue/enable=true ---
  initrd /hd-media/initrd.gz
}
}
""",
                encoding="utf-8",
            )

            tails_root = self._live_boot_root(root / "tails-root", "live")

            plan = {
                "schema_version": 1,
                "write_mode": "multi-os",
                "use_custom_grub_menu": True,
                "items": [
                    {
                        **self._plan_item("os1", "kali-linux", kali_live_root),
                        "title": "Kali Live",
                    },
                    {
                        **self._plan_item("os2", "kali-linux", kali_netinst_root),
                        "source_role": "netinst",
                        "title": "Kali Netinst",
                        "preseed": True,
                    },
                    {
                        **self._plan_item("os3", "tails", tails_root),
                        "title": "Tails",
                    },
                ],
            }

            with patch("debian_usb.multios.load_multios_plan", return_value=plan):
                rendered = render_multios_grub(
                    "unused.json",
                    "",
                    {"os1": "KALI-LIVE-UUID", "os2": "KALI-NETINST-UUID", "os3": "TAILS-UUID"},
            )

            grub_cfg = rendered["grub_cfg"]
            self.assertIn('submenu "Kali Netinst Install (HTTP Preseed) ..."', grub_cfg)
            self.assertIn('submenu "Kali Netinst Install (USB Preseed) ..."', grub_cfg)
            self.assertIn('submenu "Preseed Internal ..."', grub_cfg)
            self.assertIn('submenu "Preseed Public ..."', grub_cfg)

            default_title = "... Kali Netinst Install" + _repo_preset_label(
                "kali-linux", "kali", "PRESEED_ONE_ARGS_KALI"
            )
            entries = parse_grub_entries(grub_cfg, "boot/grub/grub.cfg")
            online_preseed = next(
                entry
                for entry in entries
                if entry.title == default_title
                and entry.menu_path
                == ("Kali ...", "Kali Netinst ...", "Kali Netinst Install (HTTP Preseed) ...", "Preseed Internal ...")
            )
            self.assertEqual(online_preseed.kernel_path, "/boot/kali/kali-linux/netinst/vmlinuz")
            self.assertEqual(online_preseed.initrd_path, "/boot/kali/kali-linux/netinst/initrd.gz")
            self.assertIn("iso-scan/filename=/boot/iso/kali/netinst/kali-netinst-root.iso", online_preseed.kernel_args)
            self.assertIn(
                _template_kernel_arg("PRESEED_COMMON_KERNEL_ARGS", "interface"),
                online_preseed.kernel_args.split(),
            )
            self.assertFalse(
                any(token == "toram" or token.startswith("toram=") for token in online_preseed.kernel_args.split())
            )

            offline_preseed = next(
                entry
                for entry in entries
                if entry.title == default_title
                and entry.menu_path
                == ("Kali ...", "Kali Netinst ...", "Kali Netinst Install (USB Preseed) ...", "Preseed Public ...")
            )
            self.assertEqual(offline_preseed.kernel_path, "/boot/kali/kali-linux/netinst/vmlinuz")
            self.assertEqual(offline_preseed.initrd_path, "/boot/kali/kali-linux/netinst/initrd.gz")
            self.assertIn("iso-scan/filename=/boot/iso/kali/netinst/kali-netinst-root.iso", offline_preseed.kernel_args)
            self.assertEqual(
                _seed_transport_tokens(offline_preseed.kernel_args),
                [f"url={load_template_config()['DEBIAN_PRESEED_PUBLIC_URL']}"],
            )

    def test_render_multios_grub_binds_netinst_preseed_entries_for_iso_store_layout(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            debian_live_root = self._live_boot_root(root / "debian-live-root", "live")
            debian_netinst_root = self._installer_boot_root(root / "debian-netinst-root")
            tails_root = self._live_boot_root(root / "tails-root", "live")
            plan = {
                "schema_version": 1,
                "write_mode": "multi-os",
                "use_custom_grub_menu": True,
                "items": [
                    {
                        **self._plan_item("os1", "debian", debian_live_root),
                        "title": "Debian Live",
                    },
                    {
                        **self._plan_item("os2", "debian", debian_netinst_root),
                        "source_role": "netinst",
                        "title": "Debian Netinst",
                        "preseed": True,
                        "managed_payload_layout": "iso-store",
                    },
                    {
                        **self._plan_item("os3", "tails", tails_root),
                        "title": "Tails",
                    },
                ],
            }

            with patch("debian_usb.multios.load_multios_plan", return_value=plan):
                rendered = render_multios_grub(
                    "unused.json",
                    "",
                    {"os1": "DEBIAN-LIVE-UUID", "os2": "DEBIAN-NETINST-UUID", "os3": "TAILS-UUID"},
                )

            entries = parse_grub_entries(rendered["grub_cfg"], "boot/grub/grub.cfg")
            online_preset = next(
                entry
                for entry in entries
                if entry.menu_path
                == ("Debian ...", "Debian Netinst ...", "Debian Netinst Install (HTTP Preseed) ...", "Preseed Internal ...")
            )
            offline_preset = next(
                entry
                for entry in entries
                if entry.menu_path
                == ("Debian ...", "Debian Netinst ...", "Debian Netinst Install (USB Preseed) ...", "Preseed Internal ...")
            )
            public_preset = next(
                entry
                for entry in entries
                if entry.menu_path
                == ("Debian ...", "Debian Netinst ...", "Debian Netinst Install (USB Preseed) ...", "Preseed Public ...")
            )
            self.assertIn("iso-scan/filename=/boot/iso/debian/netinst/debian-netinst-root.iso", online_preset.kernel_args)
            self.assertIn("iso-scan/filename=/boot/iso/debian/netinst/debian-netinst-root.iso", offline_preset.kernel_args)
            online_seed_tokens = _seed_transport_tokens(online_preset.kernel_args)
            self.assertEqual(len(online_seed_tokens), 1)
            self.assertTrue(online_seed_tokens[0].startswith("url="))
            self.assertEqual(
                _seed_transport_tokens(offline_preset.kernel_args),
                ["file=/hd-media/preseed/debian/preseed.cfg"],
            )
            public_seed_tokens = _seed_transport_tokens(public_preset.kernel_args)
            self.assertEqual(len(public_seed_tokens), 1)
            self.assertTrue(public_seed_tokens[0].startswith("url=https://"))
            for token in load_template_config()["DEBIAN_PRESEED_PUBLIC_ARGS"].split():
                self.assertIn(token, public_preset.kernel_args.split())
                self.assertNotIn(token, offline_preset.kernel_args.split())

    def test_render_multios_grub_uses_payload_iso_name_for_netinst_iso_scan_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            debian_netinst_root = self._installer_boot_root(root / "debian-netinst-root")
            kali_netinst_root = self._installer_boot_root(root / "kali-netinst-root")
            plan = {
                "schema_version": 1,
                "write_mode": "multi-os",
                "use_custom_grub_menu": True,
                "items": [
                    {
                        **self._plan_item("os1", "debian", debian_netinst_root),
                        "source_role": "netinst",
                        "title": "Debian Netinst",
                        "preseed": True,
                        "managed_payload_layout": "iso-store",
                        "payload_iso_name": "debian-testing-amd64-netinst.iso",
                    },
                    {
                        **self._plan_item("os2", "kali-linux", kali_netinst_root),
                        "source_role": "netinst",
                        "title": "Kali Netinst",
                        "preseed": True,
                        "managed_payload_layout": "iso-store",
                        "payload_iso_name": "kali-linux-2026.2-installer-netinst-amd64.iso",
                    },
                ],
            }

            with patch("debian_usb.multios.load_multios_plan", return_value=plan):
                rendered = render_multios_grub(
                    "unused.json",
                    "",
                    {"os1": "DEBIAN-NETINST-UUID", "os2": "KALI-NETINST-UUID"},
                )

            grub_cfg = rendered["grub_cfg"]
            self.assertIn(
                'set isofile="/boot/iso/debian/netinst/debian-testing-amd64-netinst.iso"',
                grub_cfg,
            )
            self.assertIn(
                'set isofile="/boot/iso/kali/netinst/kali-linux-2026.2-installer-netinst-amd64.iso"',
                grub_cfg,
            )

    def test_render_multios_grub_rejects_raw_iso_layout_for_netinst(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            debian_live_root = self._live_boot_root(root / "debian-live-root", "live")
            debian_netinst_root = self._installer_boot_root(root / "debian-netinst-root")
            plan = {
                "schema_version": 1,
                "write_mode": "multi-os",
                "use_custom_grub_menu": True,
                "items": [
                    {
                        **self._plan_item("os1", "debian", debian_live_root),
                        "title": "Debian",
                        "managed_payload_layout": "raw-iso",
                    },
                    {
                        **self._plan_item("os2", "debian", debian_netinst_root),
                        "source_role": "netinst",
                        "title": "Debian Netinst",
                        "managed_payload_layout": "raw-iso",
                    },
                ],
            }
            with patch("debian_usb.multios.load_multios_plan", return_value=plan):
                with self.assertRaisesRegex(ValueError, "netinst source requires an ISO-store payload layout"):
                    render_multios_grub(
                        "unused.json",
                        "",
                        {
                            "os1": "/dev/disk/by-partuuid/DEBIAN-LIVE-PARTUUID",
                            "os2": "/dev/disk/by-partuuid/DEBIAN-NETINST-PARTUUID",
                        },
                        boot_assets_uuid="ESP-UUID",
                    )

    def test_render_multios_grub_rejects_live_only_netinst_sources(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            debian_live_root = self._live_boot_root(root / "debian-live-root", "live")
            debian_bogus_netinst_root = self._live_boot_root(root / "debian-bogus-netinst-root", "live")
            tails_root = self._live_boot_root(root / "tails-root", "live")
            plan = {
                "schema_version": 1,
                "write_mode": "multi-os",
                "use_custom_grub_menu": True,
                "items": [
                    {
                        **self._plan_item("os1", "debian", debian_live_root),
                        "title": "Debian Live",
                    },
                    {
                        **self._plan_item("os2", "debian", debian_bogus_netinst_root),
                        "source_role": "netinst",
                        "title": "Debian Netinst",
                        "preseed": True,
                    },
                    {
                        **self._plan_item("os3", "tails", tails_root),
                        "title": "Tails",
                    },
                ],
            }

            with patch("debian_usb.multios.load_multios_plan", return_value=plan):
                with self.assertRaisesRegex(ValueError, "netinst source has no installer boot entries"):
                    render_multios_grub(
                        "unused.json",
                        "",
                        {"os1": "DEBIAN-LIVE-UUID", "os2": "DEBIAN-NETINST-UUID", "os3": "TAILS-UUID"},
                    )

    def test_render_multios_grub_custom_menu_keeps_selected_netinst_preseed_entries_per_profile(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            kali_live_root = self._live_boot_root(root / "kali-live-root", "live")
            kali_netinst_root = self._installer_boot_root(root / "kali-netinst-root")
            kali_purple_root = self._installer_iso_boot_root(root / "kali-purple-root")
            tails_root = self._live_boot_root(root / "tails-root", "live")

            plan = {
                "schema_version": 1,
                "write_mode": "multi-os",
                "use_custom_grub_menu": True,
                "items": [
                    {
                        **self._plan_item("os1", "kali-linux", kali_live_root),
                        "title": "Kali Live",
                    },
                    {
                        **self._plan_item("os2", "kali-linux", kali_netinst_root),
                        "source_role": "netinst",
                        "title": "Kali Netinst",
                        "preseed": True,
                    },
                    {
                        **self._plan_item("os3", "kali-purple", kali_purple_root),
                        "title": "Kali Purple",
                    },
                    {
                        **self._plan_item("os4", "tails", tails_root),
                        "title": "Tails",
                    },
                ],
            }

            with patch("debian_usb.multios.load_multios_plan", return_value=plan):
                rendered = render_multios_grub(
                    "unused.json",
                    "",
                    {
                        "os1": "KALI-LIVE-UUID",
                        "os2": "KALI-NETINST-UUID",
                        "os3": "KALI-PURPLE-UUID",
                        "os4": "TAILS-UUID",
                    },
                )

            entries = parse_grub_entries(rendered["grub_cfg"], "boot/grub/grub.cfg")
            kali_netinst_online = next(
                entry
                for entry in entries
                if entry.title == "... Kali Netinst Install (ROLE=Desktop,GPU=Nvidia,NET=DHCP)"
                and entry.menu_path
                == ("Kali ...", "Kali Netinst ...", "Kali Netinst Install (HTTP Preseed) ...", "Preseed Internal ...")
            )
            kali_purple_online = next(
                entry
                for entry in entries
                if entry.title == "... Kali Purple Installer (ROLE=Desktop,GPU=Nvidia,NET=DHCP)"
                and entry.menu_path
                == (
                    "Kali ...",
                    "Kali Purple Installer ...",
                    "Kali Purple Installer (HTTP Preseed) ...",
                    "Preseed Internal ...",
                )
            )
            self.assertEqual(kali_netinst_online.kernel_path, "/boot/kali/kali-linux/netinst/vmlinuz")
            self.assertEqual(kali_purple_online.kernel_path, "/boot/kali/kali-purple/installer/vmlinuz")
            self.assertIn("iso-scan/filename=/boot/iso/kali/netinst/kali-netinst-root.iso", kali_netinst_online.kernel_args)
            self.assertIn("iso-scan/filename=/boot/iso/kali/installer/kali-purple-root.iso", kali_purple_online.kernel_args)
            self.assertFalse(
                any(
                    "Preseed (Default)" in entry.title
                    and "KALI-PURPLE-UUID" in entry.kernel_args
                    for entry in entries
                )
            )

    def test_render_multios_grub_custom_menu_adds_debian_and_kali_netboot_entries_without_iso_scan(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            debian_netboot_root = self._netboot_boot_root(root / "debian-netboot-root")
            kali_netboot_root = self._netboot_boot_root(root / "kali-netboot-root")
            plan = {
                "schema_version": 1,
                "write_mode": "multi-os",
                "use_custom_grub_menu": True,
                "items": [
                    {
                        **self._plan_item("os1", "debian", debian_netboot_root),
                        "source_role": "netboot",
                        "title": "Debian Netboot",
                        "preseed": True,
                        "managed_payload_layout": "shared-data",
                    },
                    {
                        **self._plan_item("os2", "kali-linux", kali_netboot_root),
                        "source_role": "netboot",
                        "title": "Kali Netboot",
                        "preseed": True,
                        "managed_payload_layout": "shared-data",
                    },
                ],
            }

            with patch("debian_usb.multios.load_multios_plan", return_value=plan):
                rendered = render_multios_grub(
                    "unused.json",
                    "",
                    {"os1": "DEBIAN-NETBOOT-UUID", "os2": "KALI-NETBOOT-UUID"},
                )

            grub_cfg = rendered["grub_cfg"]
            self.assertIn('submenu "Debian Netboot ..."', grub_cfg)
            self.assertIn('submenu "Kali Netboot ..."', grub_cfg)
            self.assertIn('menuentry "... Debian Netboot Install"', grub_cfg)
            self.assertIn('menuentry "... Kali Netboot Install"', grub_cfg)
            self.assertIn('submenu "Debian Netboot Install (HTTP Preseed) ..."', grub_cfg)
            self.assertIn('submenu "Kali Netboot Install (HTTP Preseed) ..."', grub_cfg)

            entries = parse_grub_entries(grub_cfg, "boot/grub/grub.cfg")
            debian_default_title = "... Debian Netboot Install" + _repo_preset_label(
                "debian", "debian", "PRESEED_ONE_ARGS_DEBIAN"
            )
            kali_default_title = "... Kali Netboot Install" + _repo_preset_label(
                "kali-linux", "kali", "PRESEED_ONE_ARGS_KALI"
            )
            debian_manual = next(
                entry
                for entry in entries
                if entry.title == "... Debian Netboot Install"
                and entry.menu_path == ("Debian ...", "Debian Legacy ...")
            )
            debian_netboot = next(
                entry
                for entry in entries
                if entry.title == debian_default_title
                and entry.menu_path
                == ("Debian ...", "Debian Netboot ...", "Debian Netboot Install (HTTP Preseed) ...", "Preseed Internal ...")
            )
            kali_manual = next(
                entry
                for entry in entries
                if entry.title == "... Kali Netboot Install"
                and entry.menu_path == ("Kali ...", "Kali Legacy ...")
            )
            kali_netboot = next(
                entry
                for entry in entries
                if entry.title == kali_default_title
                and entry.menu_path
                == ("Kali ...", "Kali Netboot ...", "Kali Netboot Install (HTTP Preseed) ...", "Preseed Internal ...")
            )
            for entry, kernel_path, initrd_path in (
                (debian_manual, "/debian-netboot/vmlinuz", "/debian-netboot/initrd.gz"),
                (debian_netboot, "/debian-netboot/vmlinuz", "/debian-netboot/initrd.gz"),
                (kali_manual, "/kali-netboot/vmlinuz", "/kali-netboot/initrd.gz"),
                (kali_netboot, "/kali-netboot/vmlinuz", "/kali-netboot/initrd.gz"),
            ):
                self.assertEqual(entry.kernel_path, kernel_path)
                self.assertEqual(entry.initrd_path, initrd_path)
                self.assertNotIn("iso-scan/filename=", entry.kernel_args)
                self.assertFalse(
                    any(token == "toram" or token.startswith("toram=") for token in entry.kernel_args.split())
                )

    def _plan_item(self, item_id: str, profile: str, iso_path: Path) -> dict[str, object]:
        return {
            "id": item_id,
            "profile": profile,
            "source_role": "primary",
            "title": profile,
            "iso_path": str(iso_path),
            "managed_payload_layout": "iso-store",
            "live_toram": False,
            "persistence": False,
            "persistence_mode": "",
            "persistence_size_gib": 0,
            "persistence_fs_label": "",
            "persistence_partlabel": "",
            "payload_fs_label": item_id,
            "payload_partlabel": item_id,
            "kernel_args": "",
            "menu_label": "",
            "kernel_path": "",
            "initrd_path": "",
        }

    def _live_boot_root(self, root: Path, family: str) -> Path:
        (root / "EFI/boot").mkdir(parents=True)
        (root / "EFI/boot/bootx64.efi").write_text("", encoding="utf-8")
        (root / family).mkdir(parents=True)
        (root / family / "vmlinuz").write_text("", encoding="utf-8")
        (root / family / "initrd.img").write_text("", encoding="utf-8")
        (root / "isolinux").mkdir(parents=True)
        (root / "isolinux/live.cfg").write_text(
            """
label live
  menu label Live
  linux /live/vmlinuz
  append initrd=/live/initrd.img boot=live components quiet
""",
            encoding="utf-8",
        )
        return root

    def _installer_boot_root(self, root: Path, *, payload_name: str = "") -> Path:
        (root / "EFI/boot").mkdir(parents=True)
        (root / "EFI/boot/bootx64.efi").write_text("", encoding="utf-8")
        (root / "hd-media").mkdir(parents=True)
        (root / "hd-media/vmlinuz").write_text("hd-media-kernel", encoding="utf-8")
        (root / "hd-media/initrd.gz").write_text("hd-media-initrd", encoding="utf-8")
        (root / "payload").mkdir(parents=True)
        (root / "payload" / (payload_name or f"{root.name}.iso")).write_text("iso", encoding="utf-8")
        (root / "boot/grub").mkdir(parents=True)
        (root / "boot/grub/grub.cfg").write_text(
            """
menuentry --hotkey=g 'Graphical install' {
  linux /hd-media/vmlinuz vga=788 --- quiet
  initrd /hd-media/initrd.gz
}
menuentry --hotkey=i 'Install' {
  linux /hd-media/vmlinuz ---
  initrd /hd-media/initrd.gz
}
""",
            encoding="utf-8",
        )
        (root / "isolinux").mkdir(parents=True)
        (root / "isolinux/install.cfg").write_text(
            """
label install
  menu label Install
  linux /hd-media/vmlinuz
  append initrd=/hd-media/initrd.gz priority=low
label installgui
  menu label Graphical install
  linux /hd-media/vmlinuz
  append initrd=/hd-media/initrd.gz vga=788 quiet
""",
            encoding="utf-8",
        )
        return root

    def _netboot_boot_root(self, root: Path) -> Path:
        (root / "EFI/boot").mkdir(parents=True)
        (root / "EFI/boot/bootx64.efi").write_text("", encoding="utf-8")
        (root / "netboot").mkdir(parents=True)
        (root / "netboot/vmlinuz").write_text("netboot-kernel", encoding="utf-8")
        (root / "netboot/initrd.gz").write_text("netboot-initrd", encoding="utf-8")
        (root / "boot/grub").mkdir(parents=True)
        (root / "boot/grub/grub.cfg").write_text(
            """
menuentry 'Install' {
  linux /netboot/vmlinuz ---
  initrd /netboot/initrd.gz
}
menuentry 'Expert install' {
  linux /netboot/vmlinuz priority=low ---
  initrd /netboot/initrd.gz
}
menuentry 'Rescue mode' {
  linux /netboot/vmlinuz rescue/enable=true ---
  initrd /netboot/initrd.gz
}
""",
            encoding="utf-8",
        )
        return root

    def _installer_iso_boot_root(self, root: Path) -> Path:
        (root / "EFI/boot").mkdir(parents=True)
        (root / "EFI/boot/bootx64.efi").write_text("", encoding="utf-8")
        (root / "install.amd").mkdir(parents=True)
        (root / "install.amd/vmlinuz").write_text("", encoding="utf-8")
        (root / "install.amd/initrd.gz").write_text("", encoding="utf-8")
        (root / "payload").mkdir(parents=True)
        (root / "payload" / f"{root.name}.iso").write_text("iso", encoding="utf-8")
        (root / "boot/grub").mkdir(parents=True)
        (root / "boot/grub/grub.cfg").write_text(
            """
menuentry 'Install' {
  linux /install.amd/vmlinuz ---
  initrd /install.amd/initrd.gz
}
menuentry 'Expert install' {
  linux /install.amd/vmlinuz priority=low ---
  initrd /install.amd/initrd.gz
}
menuentry 'Rescue mode' {
  linux /install.amd/vmlinuz rescue/enable=true ---
  initrd /install.amd/initrd.gz
}
""",
            encoding="utf-8",
        )
        return root

    def _hd_media_boot_root(self, root: Path) -> Path:
        (root / "EFI/boot").mkdir(parents=True)
        (root / "EFI/boot/bootx64.efi").write_text("", encoding="utf-8")
        (root / "hd-media").mkdir(parents=True)
        (root / "hd-media/vmlinuz").write_text("downloaded-kernel", encoding="utf-8")
        (root / "hd-media/initrd.gz").write_text("downloaded-initrd", encoding="utf-8")
        (root / "boot/grub").mkdir(parents=True)
        (root / "boot/grub/grub.cfg").write_text(
            """
menuentry 'Install' {
  linux /hd-media/vmlinuz ---
  initrd /hd-media/initrd.gz
}
menuentry 'Expert install' {
  linux /hd-media/vmlinuz priority=low ---
  initrd /hd-media/initrd.gz
}
menuentry 'Rescue mode' {
  linux /hd-media/vmlinuz rescue/enable=true ---
  initrd /hd-media/initrd.gz
}
""",
            encoding="utf-8",
        )
        return root

    def _menuentry_block(self, grub_cfg: str, title: str) -> str:
        marker = f'menuentry "{title}" {{'
        start = grub_cfg.index(marker)
        lines = grub_cfg[start:].splitlines()
        return "\n".join(lines[: lines.index(next(line for line in lines[1:] if line.strip() == "}")) + 1])


if __name__ == "__main__":
    unittest.main()
