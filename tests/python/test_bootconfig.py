from pathlib import Path
import json
import os
import shutil
import tempfile
import unittest
from unittest.mock import patch

from debian_usb.bootconfig import (
    BootEntry,
    _adapt_entry_for_managed,
    parse_grub_entries,
    parse_syslinux_entries,
    render_managed_grub,
    resolve_installer_boot,
    resolve_live_boot,
)
from debian_usb.boot_render import (
    _LIVE_TORAM_MODULE_CONFIG_KEY,
    _apply_installer_settings,
    _apply_live_settings,
    _live_hook_kernel_args,
    _managed_boot_asset_path,
    _merge_kernel_args,
    _render_grub_entry,
    _set_installer_seed_transport,
    build_custom_profile_preserved_entries,
    render_custom_main_menu,
)
from debian_usb.config import (
    LEGACY_SECRET_KERNEL_ARG_NAMES,
    load_config,
    load_template_config,
    save_config,
)
from debian_usb.installer_sources import prepare_managed_installer_source


def _pre_separator_tokens(kernel_args: str) -> list[str]:
    tokens = kernel_args.split()
    try:
        return tokens[: tokens.index("---")]
    except ValueError:
        return tokens


def _seed_transport_tokens(kernel_args: str) -> list[str]:
    transport_keys = {"url", "file", "preseed/url", "preseed/file", "url/preseed", "file/preseed"}
    return [
        token
        for token in _pre_separator_tokens(kernel_args)
        if token.split("=", 1)[0] in transport_keys
    ]


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


def _write_prepared_netinst_assets(root: Path, payload_name: str = "fixture-netinst.iso") -> None:
    (root / "hd-media").mkdir(parents=True, exist_ok=True)
    (root / "hd-media/vmlinuz").write_text("downloaded-hd-media-kernel", encoding="utf-8")
    (root / "hd-media/initrd.gz").write_text("downloaded-hd-media-initrd", encoding="utf-8")
    (root / "payload").mkdir(parents=True, exist_ok=True)
    (root / "payload" / payload_name).write_text("opaque-netinst-iso", encoding="utf-8")


class BootConfigTests(unittest.TestCase):
    def test_live_boot_toram_copies_only_the_selected_root_filesystem_module(self) -> None:
        config_data = dict(load_template_config())
        config_data["DEFAULT_LIVE_TORAM"] = "1"
        config_data[_LIVE_TORAM_MODULE_CONFIG_KEY] = "filesystem.erofs"

        kernel_args = _apply_live_settings(
            "boot=live components toram toram=old.squashfs ---",
            config_data,
            "debian",
        )

        tokens = _pre_separator_tokens(kernel_args)
        self.assertIn("toram=filesystem.erofs", tokens)
        self.assertNotIn("toram", tokens)
        self.assertNotIn("toram=old.squashfs", tokens)

    def test_installer_settings_remove_live_only_toram_arguments(self) -> None:
        config_data = dict(load_template_config())

        kernel_args = _apply_installer_settings(
            "auto=true toram toram=filesystem.squashfs ---",
            config_data,
            "debian",
        )

        self.assertFalse(
            any(token == "toram" or token.startswith("toram=") for token in _pre_separator_tokens(kernel_args))
        )

    def test_installer_seed_transport_replaces_all_legacy_forms_with_one_canonical_argument(self) -> None:
        kernel_args = (
            "auto=true url=https://old.example/url preseed/url=https://old.example/preseed-url "
            "url/preseed=https://old.example/url-preseed file=/old/file preseed/file=/old/preseed-file "
            "file/preseed=/old/file-preseed ---"
        )

        with_url = _set_installer_seed_transport(kernel_args, url="https://example.test/preseed.cfg")
        with_file = _set_installer_seed_transport(kernel_args, seed_file="/hd-media/preseed/debian/preseed.cfg")
        without_seed = _set_installer_seed_transport(kernel_args)

        self.assertEqual(_seed_transport_tokens(with_url), ["url=https://example.test/preseed.cfg"])
        self.assertEqual(_seed_transport_tokens(with_file), ["file=/hd-media/preseed/debian/preseed.cfg"])
        self.assertEqual(_seed_transport_tokens(without_seed), [])
        with self.assertRaisesRegex(ValueError, "exactly one"):
            _set_installer_seed_transport(kernel_args, url="https://example.test/preseed.cfg", seed_file="/preseed.cfg")

    def test_debian_and_kali_grub_specs_keep_nested_preseed_menus_without_flat_legacy_entries(self) -> None:
        forbidden = ("Online Preseed", "Offline Preseed", "Kali Installer")
        for spec_path in (Path("configs/spec/grub/debian.json"), Path("configs/spec/grub/kali-linux.json")):
            payload = json.loads(spec_path.read_text(encoding="utf-8"))
            rendered = json.dumps(payload, sort_keys=True)
            for item in forbidden:
                self.assertNotIn(item, rendered, msg=f"{item!r} leaked into {spec_path}")
            self.assertNotIn("preseed_networks", payload, msg=f"preseed networks must be nested under preseed menus in {spec_path}")
            for profile in payload["profiles"]:
                self.assertNotIn("custom_entries", profile, msg=f"flat custom entries leaked into {spec_path}")
                for menu in profile["menus"]:
                    for entry in menu["entries"]:
                        if entry["type"] != "preseed-preset-menu":
                            continue
                        self.assertIn("network_menus", entry)
                        self.assertEqual(
                            [network["title"] for network in entry["network_menus"]],
                            ["Preseed Internal ...", "Preseed Public ..."],
                        )
                        self.assertEqual(
                            [network["args_key"] for network in entry["network_menus"]],
                            ["DEBIAN_PRESEED_INTERNAL_ARGS", "DEBIAN_PRESEED_PUBLIC_ARGS"],
                        )

    def test_render_custom_main_menu_uses_family_and_static_entry_order(self) -> None:
        family_entries = {
            "ubuntu": [
                BootEntry(
                    title="Ubuntu sample",
                    kernel_path="/casper/vmlinuz",
                    initrd_path="/casper/initrd",
                    kernel_args="boot=casper",
                    source="generated/ubuntu",
                    order=0,
                    payload_uuid="/dev/disk/by-partuuid/12345678-1234-1234-1234-123456789abc",
                    asset_namespace="ubuntu",
                )
            ],
            "kali": [
                BootEntry(
                    title="Kali sample",
                    kernel_path="/live/vmlinuz",
                    initrd_path="/live/initrd.img",
                    kernel_args="boot=live",
                    source="generated/kali",
                    order=0,
                    payload_uuid="KALI-UUID",
                    asset_namespace="kali",
                )
            ],
            "debian": [
                BootEntry(
                    title="Debian sample",
                    kernel_path="/live/vmlinuz",
                    initrd_path="/live/initrd.img",
                    kernel_args="boot=live",
                    source="generated/debian",
                    order=0,
                    payload_uuid="DEBIAN-UUID",
                    asset_namespace="debian",
                )
            ],
        }
        grub_cfg = render_custom_main_menu(
            family_entries=family_entries,
            module_lines=[],
            boot_assets_uuid="BOOT-ASSETS-UUID",
        )
        self.assertLess(grub_cfg.index('submenu "Debian ..."'), grub_cfg.index('submenu "Kali ..."'))
        self.assertLess(grub_cfg.index('submenu "Kali ..."'), grub_cfg.index('submenu "Ubuntu ..."'))
        self.assertLess(grub_cfg.index('submenu "Ubuntu ..."'), grub_cfg.index('submenu "Boot from Internal Drive"'))
        self.assertLess(grub_cfg.index('submenu "Boot from Internal Drive"'), grub_cfg.index('menuentry "MOK Enrollment"'))
        self.assertLess(grub_cfg.index('menuentry "MOK Enrollment"'), grub_cfg.index('submenu "UEFI Keys"'))
        self.assertLess(grub_cfg.index('submenu "UEFI Keys"'), grub_cfg.index('menuentry "UEFI Settings"'))
        self.assertIn("probe --fs-uuid --set=debian_usb_internal_fsuuid", grub_cfg)
        self.assertIn("probe --part-uuid --set=debian_usb_internal_partuuid", grub_cfg)
        self.assertIn("/boot/grub/custom.cfg", grub_cfg)
        self.assertIn("/boot/grub/grub.cfg", grub_cfg)
        self.assertIn("/EFI/debian/shimx64.efi", grub_cfg)
        self.assertIn("/EFI/Microsoft/Boot/bootmgfw.efi", grub_cfg)
        self.assertIn("set check_signatures=no", grub_cfg)
        self.assertIn('menuentry "UEFI Keys User Mode (PK Installed) [.auth]"', grub_cfg)
        self.assertIn('menuentry "UEFI Keys Setup Mode (PK Not Installed) [.esl]"', grub_cfg)
        self.assertIn("chainloader /EFI/debian-usb/updatevars/UpdateVars.efi -a db /secureboot/db.auth", grub_cfg)
        self.assertIn("chainloader /EFI/debian-usb/updatevars/UpdateVars.efi -a -e db /secureboot/db.esl", grub_cfg)

    def test_render_custom_main_menu_uses_configured_updatevars_child_titles(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            spec_dir = Path(temp_dir)
            (spec_dir / "main.json").write_text(
                json.dumps(
                    {
                        "timeout": -1,
                        "top_level_families": [],
                        "static_entries": [
                            {"id": "mok", "title": "Enroll Owner Key"},
                            {
                                "id": "updatevars",
                                "title": "Firmware Key Tools",
                                "entries": [
                                    {"id": "user-mode", "title": "Apply db.auth"},
                                    {"id": "setup-mode", "title": "Apply db.esl"},
                                ],
                            },
                            {"id": "uefi", "title": "Firmware Setup"},
                        ],
                    }
                ),
                encoding="utf-8",
            )
            with patch.dict(os.environ, {"DEBIAN_USB_SPEC_DIR": str(spec_dir)}):
                grub_cfg = render_custom_main_menu(
                    family_entries={},
                    module_lines=[],
                    boot_assets_uuid="BOOT-ASSETS-UUID",
                )

        self.assertIn('menuentry "Enroll Owner Key"', grub_cfg)
        self.assertIn('submenu "Firmware Key Tools"', grub_cfg)
        self.assertIn('menuentry "Apply db.auth"', grub_cfg)
        self.assertIn('menuentry "Apply db.esl"', grub_cfg)
        self.assertIn('menuentry "Firmware Setup"', grub_cfg)

    def test_render_custom_main_menu_omits_updatevars_entries_when_host_updatevars_is_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            spec_dir = Path(temp_dir)
            (spec_dir / "main.json").write_text(
                json.dumps(
                    {
                        "timeout": -1,
                        "top_level_families": [],
                        "static_entries": [
                            {"id": "mok", "title": "Enroll Owner Key"},
                            {"id": "updatevars", "title": "Firmware Key Tools"},
                            {"id": "uefi", "title": "Firmware Setup"},
                        ],
                    }
                ),
                encoding="utf-8",
            )
            with patch.dict(
                os.environ,
                {
                    "DEBIAN_USB_SPEC_DIR": str(spec_dir),
                    "DEBIAN_USB_UPDATEVARS_EFI_PATH": "",
                },
                clear=False,
            ):
                grub_cfg = render_custom_main_menu(
                    family_entries={},
                    module_lines=[],
                    boot_assets_uuid="BOOT-ASSETS-UUID",
                )

        self.assertIn('menuentry "Enroll Owner Key"', grub_cfg)
        self.assertNotIn('submenu "Firmware Key Tools"', grub_cfg)
        self.assertNotIn("UpdateVars.efi", grub_cfg)

    def test_preserved_entries_use_configured_entry_title_template(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            spec_dir = Path(temp_dir)
            (spec_dir / "debian.json").write_text(
                json.dumps(
                    {
                        "family_id": "debian",
                        "preserved_title": "Saved Debian ...",
                        "profiles": [
                            {
                                "profile": "debian",
                                "source_role": "primary",
                                "media_class": "live",
                                "preserved": {
                                    "title": "Saved Debian Live ...",
                                    "entry_title_template": "Saved: {title}",
                                    "entry_title_overrides": [
                                        {"match_title": "Expert install", "title": "Saved Expert Install"}
                                    ],
                                },
                                "menus": [],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            with patch.dict(os.environ, {"DEBIAN_USB_SPEC_DIR": str(spec_dir)}):
                entries = build_custom_profile_preserved_entries(
                    profile="debian",
                    source_role="primary",
                    source_path="/tmp/debian.iso",
                    media_class="live",
                    rendered_entries=[
                        BootEntry(
                            title="Live system",
                            kernel_path="/live/vmlinuz",
                            initrd_path="/live/initrd.img",
                            kernel_args="boot=live",
                            source="test",
                            kind="live",
                        ),
                        BootEntry(
                            title="Expert install",
                            kernel_path="/install/vmlinuz",
                            initrd_path="/install/initrd.gz",
                            kernel_args="priority=low",
                            source="test",
                            kind="expert-installer",
                        )
                    ],
                    payload_uuid="PAYLOAD-UUID",
                    asset_namespace="debian",
                )

        self.assertEqual(entries[0].title, "Saved: Live system")
        self.assertEqual(entries[1].title, "Saved Expert Install")
        self.assertEqual(entries[0].menu_path, ("Saved Debian ...",))

    def test_parse_grub_entries(self) -> None:
        text = """
menuentry 'Try Ubuntu' {
\tlinux\t/casper/vmlinuz boot=casper quiet splash ---
\tinitrd\t/casper/initrd
}
"""
        entries = parse_grub_entries(text, "boot/grub/grub.cfg")
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].kernel_path, "/casper/vmlinuz")
        self.assertEqual(entries[0].initrd_path, "/casper/initrd")

    def test_parse_syslinux_entries(self) -> None:
        text = """
label live
  menu label Live
  linux /live/vmlinuz
  initrd /live/initrd.img
  append boot=live components quiet
"""
        entries = parse_syslinux_entries(text, "isolinux/live.cfg")
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].kernel_args, "boot=live components quiet")

    def test_merge_kernel_args_replaces_upstream_keyed_values_with_config_values(self) -> None:
        merged = _merge_kernel_args(
            "priority=low locale=sv_SE.UTF-8 video=LVDS-1:e:1024x768@60 --- quiet",
            "priority=critical locale=en_US.UTF-8 video=HDMI-1:e:1920x1080@60 video=eDP-1:e:1920x1080@60",
        )
        self.assertEqual(
            merged,
            "priority=critical locale=en_US.UTF-8 video=HDMI-1:e:1920x1080@60 video=eDP-1:e:1920x1080@60 --- quiet",
        )

    def test_parse_grub_entries_follows_source_includes(self) -> None:
        text = """
menuentry 'Start installer' {
\tlinux\t/install/gtk/vmlinuz vga=788  --- quiet
\tinitrd\t/install/gtk/initrd.gz
}
source /boot/grub/install.cfg
"""
        included = """
submenu 'Advanced install options ...' {
menuentry 'Automated install' {
\tlinux\t/install/vmlinuz auto=true priority=critical vga=788  --- quiet
\tinitrd\t/install/initrd.gz
}
}
"""
        entries = parse_grub_entries(
            text,
            "boot/grub/grub.cfg",
            include_loader=lambda path: included if path == "/boot/grub/install.cfg" else None,
        )
        self.assertEqual([entry.title for entry in entries], ["Start installer", "Automated install"])
        self.assertEqual(entries[1].menu_path, ("Advanced install options ...",))

    def test_parse_grub_entries_supports_hotkey_flags_before_titles(self) -> None:
        text = """
menuentry --hotkey=g 'Graphical install' {
\tlinux\t/install.amd/vmlinuz vga=788 --- quiet
\tinitrd\t/install.amd/gtk/initrd.gz
}
menuentry --hotkey=i 'Install' {
\tlinux\t/install.amd/vmlinuz vga=788 --- quiet
\tinitrd\t/install.amd/initrd.gz
}
submenu --hotkey=a 'Advanced options ...' {
menuentry --hotkey=x '... Expert install' {
\tlinux\t/install.amd/vmlinuz priority=low vga=788 ---
\tinitrd\t/install.amd/initrd.gz
}
}
"""
        entries = parse_grub_entries(text, "boot/grub/grub.cfg")
        self.assertEqual([entry.title for entry in entries], ["Graphical install", "Install", "... Expert install"])
        self.assertEqual(entries[2].menu_path, ("Advanced options ...",))

    def test_live_boot_persistence_templates_cover_the_complete_root(self) -> None:
        for relative_path in (
            "configs/persistence-debian.conf",
            "configs/persistence-kali.conf",
        ):
            with self.subTest(relative_path=relative_path):
                directives = [
                    line.strip()
                    for line in Path(relative_path).read_text(encoding="utf-8").splitlines()
                    if line.strip() and not line.lstrip().startswith("#")
                ]
                self.assertEqual(directives, ["/ union"])

    def test_adapt_managed_kali_persistence_keeps_persistence_enabled(self) -> None:
        entry = BootEntry(
            title="Live system with USB persistence",
            kernel_path="/live/vmlinuz",
            initrd_path="/live/initrd.img",
            kernel_args=(
                "boot=live components quiet splash findiso=${iso_path} persistence "
                "persistence-storage=overlay persistence-method=legacy union=aufs"
            ),
            source="boot/grub/grub.cfg",
            kind="live-persistence",
        )
        adapted = _adapt_entry_for_managed(entry, "kali-linux", "dead-beef", dict(load_template_config()), "plain")
        self.assertIn("ignore_uuid", adapted.kernel_args)
        self.assertIn("live-media=/dev/disk/by-uuid/dead-beef", adapted.kernel_args)
        self.assertIn("persistence", adapted.kernel_args)
        self.assertIn("persistence-label=KALI-PERSIST", adapted.kernel_args)
        self.assertIn("persistence-media=removable-usb", adapted.kernel_args)
        self.assertIn("persistence-storage=filesystem", adapted.kernel_args)
        self.assertIn("union=overlay", adapted.kernel_args)
        self.assertNotIn("persistence-storage=overlay", adapted.kernel_args)
        self.assertNotIn("persistence-method=legacy", adapted.kernel_args)
        self.assertNotIn("union=aufs", adapted.kernel_args)
        self.assertNotIn("findiso=", adapted.kernel_args)

    def test_adapt_managed_kali_encrypted_persistence_preserves_encryption_flags(self) -> None:
        entry = BootEntry(
            title="Live system with USB Encrypted persistence",
            kernel_path="/live/vmlinuz",
            initrd_path="/live/initrd.img",
            kernel_args="boot=live components quiet splash findiso=${iso_path} persistent=cryptsetup persistence-encryption=luks persistence",
            source="boot/grub/grub.cfg",
            kind="live-encrypted-persistence",
        )
        adapted = _adapt_entry_for_managed(entry, "kali-linux", "dead-beef", dict(load_template_config()), "encrypted")
        self.assertIn("ignore_uuid", adapted.kernel_args)
        self.assertIn("live-media=/dev/disk/by-uuid/dead-beef", adapted.kernel_args)
        self.assertIn("persistent=cryptsetup", adapted.kernel_args)
        self.assertIn("persistence-encryption=luks", adapted.kernel_args)
        self.assertIn("persistence-media=removable-usb", adapted.kernel_args)
        self.assertIn("persistence-storage=filesystem", adapted.kernel_args)
        self.assertIn("union=overlay", adapted.kernel_args)
        self.assertNotIn("findiso=", adapted.kernel_args)

    def test_adapt_managed_forensics_entry_applies_configured_forensics_extras(self) -> None:
        entry = BootEntry(
            title="Kali Forensics",
            kernel_path="/live/vmlinuz",
            initrd_path="/live/initrd.img",
            kernel_args="boot=live components quiet splash forensic",
            source="boot/grub/grub.cfg",
            kind="live-forensics",
        )
        config_data = dict(load_template_config())
        config_data["DEFAULT_FORENSICS_KERNEL_EXTRAS"] = "rd.shell=0"
        config_data["KALI_LINUX_FORENSICS_KERNEL_EXTRAS"] = "noprompt"
        adapted = _adapt_entry_for_managed(entry, "kali-linux", "dead-beef", config_data, "")
        self.assertIn("forensic", adapted.kernel_args)
        self.assertIn("rd.shell=0", adapted.kernel_args)
        self.assertIn("noprompt", adapted.kernel_args)
        self.assertIn("ignore_uuid", adapted.kernel_args)
        self.assertNotIn("persistence", adapted.kernel_args)

    def test_adapt_managed_ubuntu_live_entry_inherits_configured_live_defaults(self) -> None:
        entry = BootEntry(
            title="Try Ubuntu",
            kernel_path="/casper/vmlinuz",
            initrd_path="/casper/initrd",
            kernel_args="boot=casper quiet splash ---",
            source="boot/grub/grub.cfg",
            kind="live",
        )
        config_data = dict(load_template_config())
        config_data["DEFAULT_LIVE_TORAM"] = "1"
        config_data["DEFAULT_LIVE_MEM_GIB"] = "4"
        config_data["DEFAULT_LIVE_KERNEL_EXTRAS"] = "foo=bar"
        config_data["UBUNTU_DESKTOP_LIVE_KERNEL_EXTRAS"] = "baz=qux"
        adapted = _adapt_entry_for_managed(entry, "ubuntu-desktop", "ABCD-1234", config_data, "")
        self.assertIn("systemd.unified_cgroup_hierarchy=1", adapted.kernel_args)
        self.assertIn("foo=bar", adapted.kernel_args)
        self.assertIn("baz=qux", adapted.kernel_args)
        self.assertIn("toram", adapted.kernel_args)
        self.assertIn("mem=4G", adapted.kernel_args)
        self.assertIn("uuid=ABCD-1234", adapted.kernel_args)
        self.assertNotIn("ignore_uuid", adapted.kernel_args)
        self.assertIn("boot=casper", adapted.kernel_args)

    def test_adapt_managed_live_entry_preserves_both_fwupd_refresh_masks(self) -> None:
        entry = BootEntry(
            title="Live system",
            kernel_path="/live/vmlinuz",
            initrd_path="/live/initrd.img",
            kernel_args="boot=live components quiet ---",
            source="boot/grub/grub.cfg",
            kind="live",
        )

        adapted = _adapt_entry_for_managed(
            entry,
            "debian",
            "ABCD-1234",
            dict(load_template_config()),
            "",
        )

        masks = [token for token in adapted.kernel_args.split() if token.startswith("systemd.mask=")]
        self.assertEqual(
            masks,
            [
                "systemd.mask=fwupd-refresh.service",
                "systemd.mask=fwupd-refresh.timer",
            ],
        )

    def test_render_managed_grub_honors_per_run_toram_with_persistence(self) -> None:
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

            rendered = render_managed_grub(
                source_path=str(root),
                profile="ubuntu-desktop",
                live_uuid="ABCD-1234",
                persistence=True,
                persistence_mode="plain",
                live_toram=True,
            )

            self.assertIn("toram", rendered["grub_cfg"])
            self.assertIn("persistent", rendered["grub_cfg"])

    def test_adapt_managed_ubuntu_installer_entry_applies_url_and_installer_extras(self) -> None:
        entry = BootEntry(
            title="Install Ubuntu",
            kernel_path="/casper/vmlinuz",
            initrd_path="/casper/initrd",
            kernel_args="automatic-ubiquity quiet ---",
            source="boot/grub/grub.cfg",
            kind="installer",
        )
        config_data = dict(load_template_config())
        config_data["DEFAULT_INSTALLER_KERNEL_EXTRAS"] = "ipv6.disable=1"
        config_data["UBUNTU_DESKTOP_INSTALLER_KERNEL_EXTRAS"] = "autoinstall"
        adapted = _adapt_entry_for_managed(entry, "ubuntu-desktop", "ABCD-1234", config_data, "")
        self.assertIn("ipv6.disable=1", adapted.kernel_args)
        self.assertIn("autoinstall", adapted.kernel_args)
        self.assertNotIn("url=", adapted.kernel_args)
        self.assertNotIn("preseed/url=", adapted.kernel_args)
        self.assertNotIn("auto=true", adapted.kernel_args)

    def test_adapt_managed_debian_installer_entry_keeps_preseed_policy_and_profile_extras(self) -> None:
        entry = BootEntry(
            title="Install",
            kernel_path="/install.amd/vmlinuz",
            initrd_path="/install.amd/initrd.gz",
            kernel_args="vga=788 ---",
            source="boot/grub/grub.cfg",
            kind="installer",
        )
        config_data = dict(load_template_config())
        config_data["DEFAULT_INSTALLER_KERNEL_EXTRAS"] = "ipv6.disable=1"
        config_data["DEBIAN_INSTALLER_KERNEL_EXTRAS"] = "net.ifnames=0"
        config_data["DEBIAN_PRESEED_INTERNAL_URL"] = "https://example.test/preseed.cfg"
        adapted = _adapt_entry_for_managed(entry, "debian", "dead-beef", config_data, "")
        self.assertIn("auto=true", adapted.kernel_args)
        self.assertEqual(_seed_transport_tokens(adapted.kernel_args), ["url=https://example.test/preseed.cfg"])
        self.assertIn("ipv6.disable=1", adapted.kernel_args)
        self.assertIn("net.ifnames=0", adapted.kernel_args)

    def test_adapt_managed_kali_purple_installer_entry_uses_kali_installer_policy(self) -> None:
        entry = BootEntry(
            title="Graphical install",
            kernel_path="/install.amd/vmlinuz",
            initrd_path="/install.amd/initrd.gz",
            kernel_args="vga=788 --- quiet",
            source="boot/grub/grub.cfg",
            kind="installer",
        )
        config_data = dict(load_template_config())
        config_data["KALI_PURPLE_INSTALLER_KERNEL_EXTRAS"] = "net.ifnames=0"
        config_data["KALI_PURPLE_PRESEED_INTERNAL_URL"] = "https://example.test/kali-purple.cfg"
        adapted = _adapt_entry_for_managed(entry, "kali-purple", "dead-beef", config_data, "")
        self.assertIn("auto=true", adapted.kernel_args)
        self.assertEqual(_seed_transport_tokens(adapted.kernel_args), ["url=https://example.test/kali-purple.cfg"])
        self.assertIn("net.ifnames=0", adapted.kernel_args)

    def test_resolve_live_boot_adds_uuid_for_ubuntu(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
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
            resolved = resolve_live_boot(
                root=str(root),
                profile="ubuntu-desktop",
                live_uuid="1234-ABCD",
                persistence=True,
            )
            self.assertIn("uuid=1234-ABCD", resolved["kernel_args"])
            self.assertIn("persistent", resolved["kernel_args"])
            self.assertEqual(resolved["persistence_fs_label"], "casper-rw")
            self.assertNotIn("ignore_uuid", resolved["kernel_args"])

    def test_resolve_live_boot_adds_uuid_for_ubuntu_server(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "boot/grub").mkdir(parents=True)
            (root / "casper").mkdir()
            (root / "casper/vmlinuz").write_text("", encoding="utf-8")
            (root / "casper/initrd").write_text("", encoding="utf-8")
            (root / "boot/grub/grub.cfg").write_text(
                """
menuentry 'Ubuntu Server' {
    linux /casper/vmlinuz boot=casper quiet splash ---
    initrd /casper/initrd
}
""",
                encoding="utf-8",
            )
            resolved = resolve_live_boot(
                root=str(root),
                profile="ubuntu-server",
                live_uuid="1234-ABCD",
                persistence=False,
            )
            self.assertIn("uuid=1234-ABCD", resolved["kernel_args"])
            self.assertNotIn("ignore_uuid", resolved["kernel_args"])

    def test_resolve_live_boot_adds_persistence_for_debian(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
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
            resolved = resolve_live_boot(
                root=str(root),
                profile="debian",
                live_uuid="dead-beef",
                persistence=True,
            )
            self.assertIn("ignore_uuid", resolved["kernel_args"])
            self.assertIn("live-media=/dev/disk/by-uuid/dead-beef", resolved["kernel_args"])
            self.assertIn("persistence", resolved["kernel_args"])
            self.assertIn("persistence-label=DEBIAN-PERSIST", resolved["kernel_args"])
            self.assertIn("persistence-storage=filesystem", resolved["kernel_args"])
            self.assertIn("union=overlay", resolved["kernel_args"])

    def test_resolve_live_boot_uses_configured_debian_persistence_label(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config_path = root / "debian-usb.conf"
            save_config(str(config_path), {"DEFAULT_DEBIAN_PERSIST_LABEL": "DEB-PERSIST"})
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
            resolved = resolve_live_boot(
                root=str(root),
                profile="debian",
                live_uuid="dead-beef",
                persistence=True,
                config_path=str(config_path),
            )
            self.assertIn("persistence-label=DEB-PERSIST", resolved["kernel_args"])
            self.assertEqual(resolved["persistence_fs_label"], "DEB-PERSIST")

    def test_resolve_live_boot_keeps_tails_persistence_out_of_kernel_args(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "EFI/boot").mkdir(parents=True)
            (root / "EFI/boot/bootx64.efi").write_text("", encoding="utf-8")
            (root / "live").mkdir()
            (root / "live/vmlinuz").write_text("", encoding="utf-8")
            (root / "live/initrd.img").write_text("", encoding="utf-8")
            (root / "live/Tails.module").write_text("", encoding="utf-8")
            (root / "isolinux").mkdir(parents=True)
            (root / "isolinux/live.cfg").write_text(
                """
label live
  menu label Tails
  linux /live/vmlinuz
  append initrd=/live/initrd.img boot=live components quiet
""",
                encoding="utf-8",
            )

            resolved = resolve_live_boot(
                root=str(root),
                profile="tails",
                live_uuid="TAILS-UUID",
                persistence=True,
            )

            self.assertEqual(resolved["persistence_fs_label"], "TailsData")
            for fragment in ("persistence", "persistent=cryptsetup", "persistence-label=", "persistence-media="):
                self.assertNotIn(fragment, resolved["kernel_args"])

    def test_resolve_live_boot_replaces_ubuntu_ignore_uuid_with_live_uuid(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "boot/grub").mkdir(parents=True)
            (root / "casper").mkdir()
            (root / "casper/vmlinuz").write_text("", encoding="utf-8")
            (root / "casper/initrd").write_text("", encoding="utf-8")
            (root / "boot/grub/grub.cfg").write_text(
                """
menuentry 'Try Ubuntu' {
    linux /casper/vmlinuz boot=casper ignore_uuid quiet splash ---
    initrd /casper/initrd
}
""",
                encoding="utf-8",
            )
            resolved = resolve_live_boot(
                root=str(root),
                profile="ubuntu-desktop",
                live_uuid="ABCD-1234",
                persistence=False,
            )
            self.assertIn("uuid=ABCD-1234", resolved["kernel_args"])
            self.assertNotIn("ignore_uuid", resolved["kernel_args"])

    def test_resolve_live_boot_uses_ubuntu_fallback_when_no_entry_is_detected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "casper").mkdir(parents=True)
            (root / "casper/vmlinuz").write_text("", encoding="utf-8")
            (root / "casper/initrd").write_text("", encoding="utf-8")
            resolved = resolve_live_boot(
                root=str(root),
                profile="ubuntu-desktop",
                live_uuid="ABCD-1234",
                persistence=False,
                kernel_path_override="/casper/vmlinuz",
                initrd_path_override="/casper/initrd",
            )
            self.assertEqual(resolved["entry_source"], "fallback")
            self.assertEqual(resolved["kernel_args"], "boot=casper quiet splash noeject uuid=ABCD-1234")

    def test_resolve_live_boot_applies_configured_live_settings_when_auto_detecting(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config_path = root / "debian-usb.conf"
            save_config(
                str(config_path),
                {
                    "DEFAULT_LIVE_TORAM": "1",
                    "DEFAULT_LIVE_MEM_GIB": "4",
                    "DEFAULT_LIVE_KERNEL_EXTRAS": "nvme_core.default_ps_max_latency_us=3200",
                },
            )
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
            resolved = resolve_live_boot(
                root=str(root),
                profile="debian",
                live_uuid="dead-beef",
                persistence=False,
                config_path=str(config_path),
            )
            self.assertIn("systemd.unified_cgroup_hierarchy=1", resolved["kernel_args"])
            self.assertIn("threadirqs", resolved["kernel_args"])
            self.assertIn("pcie_aspm=performance", resolved["kernel_args"])
            self.assertIn("audit_backlog_limit=0", resolved["kernel_args"])
            self.assertIn("preempt=full", resolved["kernel_args"])
            self.assertIn("hardened_usercopy=on", resolved["kernel_args"])
            self.assertIn("nvme_core.default_ps_max_latency_us=3200", resolved["kernel_args"])
            self.assertIn("toram", resolved["kernel_args"])
            self.assertIn("mem=4G", resolved["kernel_args"])
            self.assertIn("live-media=/dev/disk/by-uuid/dead-beef", resolved["kernel_args"])

    def test_resolve_live_boot_does_not_reapply_configured_live_settings_to_manual_override(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config_path = root / "debian-usb.conf"
            save_config(
                str(config_path),
                {
                    "DEFAULT_LIVE_TORAM": "1",
                    "DEFAULT_LIVE_MEM_GIB": "4",
                    "DEFAULT_LIVE_KERNEL_EXTRAS": "nvme_core.default_ps_max_latency_us=3200",
                },
            )
            (root / "live").mkdir()
            (root / "live/vmlinuz").write_text("", encoding="utf-8")
            (root / "live/initrd.img").write_text("", encoding="utf-8")
            resolved = resolve_live_boot(
                root=str(root),
                profile="debian",
                live_uuid="dead-beef",
                persistence=False,
                kernel_args_override="boot=live components quiet splash noeject",
                kernel_path_override="/live/vmlinuz",
                initrd_path_override="/live/initrd.img",
                config_path=str(config_path),
            )
            self.assertEqual(
                resolved["kernel_args"],
                "boot=live components quiet splash noeject ignore_uuid live-media=/dev/disk/by-uuid/dead-beef",
            )

    def test_resolve_installer_boot_detects_installer_entry_and_applies_extras(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config_path = root / "debian-usb.conf"
            save_config(
                str(config_path),
                {
                    "DEFAULT_INSTALLER_KERNEL_EXTRAS": (
                        "auto=true priority=critical interface=auto "
                        "preseed/url=https://legacy.example.test/preseed.cfg "
                        "file/preseed=/legacy/preseed.cfg"
                    ),
                    "DEBIAN_PRESEED_INTERNAL_URL": "https://example.test/preseed.cfg",
                },
            )
            (root / "boot/grub").mkdir(parents=True)
            (root / "install.amd").mkdir()
            (root / "install.amd/vmlinuz").write_text("", encoding="utf-8")
            (root / "install.amd/initrd.gz").write_text("", encoding="utf-8")
            (root / "boot/grub/grub.cfg").write_text(
                """
menuentry 'Install' {
    linux /install.amd/vmlinuz vga=788 ---
    initrd /install.amd/initrd.gz
}
""",
                encoding="utf-8",
            )
            resolved = resolve_installer_boot(
                root=str(root),
                profile="debian",
                config_path=str(config_path),
            )
            self.assertTrue(resolved["available"])
            self.assertEqual(resolved["menu_label"], "Install")
            kernel_args = resolved["kernel_args"]
            for token in ("vga=788", "auto=true", "priority=critical", "interface=auto", "---"):
                self.assertIn(token, kernel_args)
            self.assertEqual(_seed_transport_tokens(kernel_args), ["url=https://example.test/preseed.cfg"])

    def test_render_managed_grub_preserves_repeated_keyed_args_from_debian_preseed_common_args(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config_path = root / "debian-usb.conf"
            save_config(
                str(config_path),
                {
                    "PRESEED_COMMON_KERNEL_ARGS": "priority=critical video=HDMI-1:e:1920x1080@60 video=eDP-1:e:1920x1080@60",
                    "PRESEED_ONE_ARGS_DEBIAN": "classes=prod\\;desktop\\;amd64\\;intel\\;baremetal\\;intel-uhd\\;enhanced\\;dhcp\\;nvme",
                    "DEBIAN_PRESEED_INTERNAL_URL": "https://example.test/preseed.cfg",
                },
            )
            (root / "EFI/boot").mkdir(parents=True)
            (root / "EFI/boot/bootx64.efi").write_text("", encoding="utf-8")
            _write_prepared_netinst_assets(root)
            (root / "boot/grub").mkdir(parents=True)
            (root / "boot/grub/grub.cfg").write_text(
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
}
""",
                encoding="utf-8",
            )

            rendered = render_managed_grub(
                source_path=str(root),
                profile="debian",
                live_uuid="DEBIAN-NETINST-UUID",
                persistence=False,
                config_path=str(config_path),
                use_custom_menu=True,
                include_preseed_entries=True,
            )

            entries = parse_grub_entries(rendered["grub_cfg"], "boot/grub/grub.cfg")
            online_preset = next(
                entry
                for entry in entries
                if entry.menu_path
                == ("Debian ...", "Debian Netinst ...", "Debian Netinst Install (HTTP Preseed) ...", "Preseed Internal ...")
            )
            self.assertIn("priority=critical", online_preset.kernel_args)
            self.assertNotIn("priority=low", online_preset.kernel_args)
            self.assertEqual(_seed_transport_tokens(online_preset.kernel_args), ["url=https://example.test/preseed.cfg"])
            self.assertIn("classes=prod\\;desktop\\;amd64\\;intel\\;baremetal\\;intel-uhd\\;enhanced\\;dhcp\\;nvme", online_preset.kernel_args)

    def test_render_managed_grub_uses_internal_and_public_preseed_urls_per_variant_menu(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config_path = root / "debian-usb.conf"
            save_config(
                str(config_path),
                {
                    "PRESEED_ONE_ARGS_DEBIAN": "classes=prod\\;desktop\\;standard\\;dhcp;nvidia",
                    "PRESEED_TWO_ARGS_DEBIAN": "netcfg/get_ipaddress=10.0.0.10 classes=prod\\;desktop\\;standard\\;static",
                    "DEBIAN_PRESEED_INTERNAL_URL": "https://example.test/internal-preseed.cfg",
                    "DEBIAN_PRESEED_PUBLIC_URL": "https://example.test/public-preseed.cfg",
                    "DEBIAN_PRESEED_INTERNAL_ARGS": "internal-only=1",
                    "DEBIAN_PRESEED_PUBLIC_ARGS": (
                        "debian-installer/allow_unauthenticated_ssl=true public-only=1 "
                        "url=https://ignored.example.test/preseed.cfg file=/ignored/preseed.cfg"
                    ),
                },
            )
            (root / "EFI/boot").mkdir(parents=True)
            (root / "EFI/boot/bootx64.efi").write_text("", encoding="utf-8")
            _write_prepared_netinst_assets(root)
            (root / "boot/grub").mkdir(parents=True)
            (root / "boot/grub/grub.cfg").write_text(
                """
menuentry 'Install' {
  linux /hd-media/vmlinuz ---
  initrd /hd-media/initrd.gz
}
""",
                encoding="utf-8",
            )

            rendered = render_managed_grub(
                source_path=str(root),
                profile="debian",
                live_uuid="DEBIAN-NETINST-UUID",
                persistence=False,
                config_path=str(config_path),
                use_custom_menu=True,
                include_preseed_entries=True,
            )
            for legacy_name in LEGACY_SECRET_KERNEL_ARG_NAMES:
                self.assertNotIn(f"{legacy_name}=", rendered["grub_cfg"])
            entries = parse_grub_entries(rendered["grub_cfg"], "boot/grub/grub.cfg")
            internal_preset = next(
                entry
                for entry in entries
                if entry.menu_path
                == ("Debian ...", "Debian Netinst ...", "Debian Netinst Install (HTTP Preseed) ...", "Preseed Internal ...")
            )
            public_preset = next(
                entry
                for entry in entries
                if entry.menu_path
                == ("Debian ...", "Debian Netinst ...", "Debian Netinst Install (HTTP Preseed) ...", "Preseed Public ...")
            )
            static_preset = next(
                entry
                for entry in entries
                if entry.menu_path
                == ("Debian ...", "Debian Netinst ...", "Debian Netinst Install (HTTP Preseed) ...", "Preseed Internal ...")
                and "netcfg/get_ipaddress=10.0.0.10" in entry.kernel_args
            )
            offline_internal_preset = next(
                entry
                for entry in entries
                if entry.menu_path
                == ("Debian ...", "Debian Netinst ...", "Debian Netinst Install (USB Preseed) ...", "Preseed Internal ...")
            )
            offline_public_preset = next(
                entry
                for entry in entries
                if entry.menu_path
                == ("Debian ...", "Debian Netinst ...", "Debian Netinst Install (USB Preseed) ...", "Preseed Public ...")
            )

            self.assertEqual(
                _seed_transport_tokens(internal_preset.kernel_args),
                ["url=https://example.test/internal-preseed.cfg"],
            )
            self.assertEqual(
                _seed_transport_tokens(public_preset.kernel_args),
                ["url=https://example.test/public-preseed.cfg"],
            )
            self.assertEqual(
                _seed_transport_tokens(offline_internal_preset.kernel_args),
                ["file=/hd-media/preseed/debian/preseed.cfg"],
            )
            self.assertEqual(
                _seed_transport_tokens(offline_public_preset.kernel_args),
                ["url=https://example.test/public-preseed.cfg"],
            )
            for entry in (internal_preset, offline_internal_preset):
                tokens = _pre_separator_tokens(entry.kernel_args)
                self.assertIn("internal-only=1", tokens)
                self.assertNotIn("debian-installer/allow_unauthenticated_ssl=true", tokens)
                self.assertNotIn("public-only=1", tokens)
            for entry in (public_preset, offline_public_preset):
                tokens = _pre_separator_tokens(entry.kernel_args)
                self.assertIn("debian-installer/allow_unauthenticated_ssl=true", tokens)
                self.assertIn("public-only=1", tokens)
                self.assertNotIn("internal-only=1", tokens)
            internal_tokens = _pre_separator_tokens(internal_preset.kernel_args)
            public_tokens = _pre_separator_tokens(public_preset.kernel_args)
            static_tokens = _pre_separator_tokens(static_preset.kernel_args)
            self.assertEqual(internal_tokens[-1], "classes=prod\\;desktop\\;standard\\;dhcp\\;nvidia")
            self.assertEqual(public_tokens[-1], "classes=prod\\;desktop\\;standard\\;dhcp\\;nvidia")
            self.assertEqual(static_tokens[-1], "classes=prod\\;desktop\\;standard\\;static")
            self.assertIn("netcfg/get_ipaddress=10.0.0.10", static_preset.kernel_args)

    def test_render_managed_grub_renders_spec_added_preseed_preset_key(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            spec_dir = root / "spec"
            shutil.copytree("configs/spec/grub", spec_dir)
            debian_spec_path = spec_dir / "debian.json"
            debian_spec = json.loads(debian_spec_path.read_text(encoding="utf-8"))
            debian_spec["preseed"]["preset_sets"]["debian"].append(
                {
                    "label": " (ROLE=TEST,GPU=Nvidia,NET=DHCP,BOOT=Dualboot)",
                    "args_key": "PRESEED_ELEVEN_ARGS_DEBIAN",
                }
            )
            debian_spec_path.write_text(json.dumps(debian_spec), encoding="utf-8")

            config_path = root / "debian-usb.conf"
            save_config(
                str(config_path),
                {
                    "PRESEED_ELEVEN_ARGS_DEBIAN": "classes=prod\\;test\\;dhcp\\;dualboot",
                    "DEBIAN_PRESEED_INTERNAL_URL": "https://example.test/preseed.cfg",
                },
            )
            (root / "EFI/boot").mkdir(parents=True)
            (root / "EFI/boot/bootx64.efi").write_text("", encoding="utf-8")
            _write_prepared_netinst_assets(root)
            (root / "boot/grub").mkdir(parents=True)
            (root / "boot/grub/grub.cfg").write_text(
                """
menuentry 'Install' {
  linux /hd-media/vmlinuz ---
  initrd /hd-media/initrd.gz
}
""",
                encoding="utf-8",
            )

            with patch.dict(os.environ, {"DEBIAN_USB_SPEC_DIR": str(spec_dir)}):
                rendered = render_managed_grub(
                    source_path=str(root),
                    profile="debian",
                    live_uuid="DEBIAN-NETINST-UUID",
                    persistence=False,
                    config_path=str(config_path),
                    use_custom_menu=True,
                    include_preseed_entries=True,
                )

            entries = parse_grub_entries(rendered["grub_cfg"], "boot/grub/grub.cfg")
            added_entry = next(
                entry
                for entry in entries
                if entry.title == "... Debian Netinst Install (ROLE=TEST,GPU=Nvidia,NET=DHCP,BOOT=Dualboot)"
                and entry.menu_path
                == (
                    "Debian ...",
                    "Debian Netinst ...",
                    "Debian Netinst Install (HTTP Preseed) ...",
                    "Preseed Internal ...",
                )
            )
            self.assertIn("classes=prod\\;test\\;dhcp\\;dualboot", added_entry.kernel_args)
            self.assertIn("url=https://example.test/preseed.cfg", added_entry.kernel_args)

    def test_render_managed_grub_omits_removed_preseed_preset_from_spec(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            spec_dir = root / "spec"
            shutil.copytree("configs/spec/grub", spec_dir)
            debian_spec_path = spec_dir / "debian.json"
            debian_spec = json.loads(debian_spec_path.read_text(encoding="utf-8"))
            removed_label = next(
                preset["label"]
                for preset in debian_spec["preseed"]["preset_sets"]["debian"]
                if preset["args_key"] == "PRESEED_ONE_ARGS_DEBIAN"
            )
            retained_label = next(
                preset["label"]
                for preset in debian_spec["preseed"]["preset_sets"]["debian"]
                if preset["args_key"] == "PRESEED_THREE_ARGS_DEBIAN"
            )
            debian_spec["preseed"]["preset_sets"]["debian"] = [
                preset
                for preset in debian_spec["preseed"]["preset_sets"]["debian"]
                if preset["args_key"] != "PRESEED_ONE_ARGS_DEBIAN"
            ]
            debian_spec_path.write_text(json.dumps(debian_spec), encoding="utf-8")

            config_path = root / "debian-usb.conf"
            save_config(
                str(config_path),
                {
                    "DEBIAN_PRESEED_INTERNAL_URL": "https://example.test/preseed.cfg",
                },
            )
            (root / "EFI/boot").mkdir(parents=True)
            (root / "EFI/boot/bootx64.efi").write_text("", encoding="utf-8")
            _write_prepared_netinst_assets(root)
            (root / "boot/grub").mkdir(parents=True)
            (root / "boot/grub/grub.cfg").write_text(
                """
menuentry 'Install' {
  linux /hd-media/vmlinuz ---
  initrd /hd-media/initrd.gz
}
""",
                encoding="utf-8",
            )

            with patch.dict(os.environ, {"DEBIAN_USB_SPEC_DIR": str(spec_dir)}):
                rendered = render_managed_grub(
                    source_path=str(root),
                    profile="debian",
                    live_uuid="DEBIAN-NETINST-UUID",
                    persistence=False,
                    config_path=str(config_path),
                    use_custom_menu=True,
                    include_preseed_entries=True,
                )

            grub_cfg = rendered["grub_cfg"]
            self.assertNotIn(f'menuentry "... Debian Netinst Install{removed_label}"', grub_cfg)
            self.assertIn(f'menuentry "... Debian Netinst Install{retained_label}"', grub_cfg)

    def test_render_managed_grub_uses_iso_store_payload_modules_for_debian(self) -> None:
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

            rendered = render_managed_grub(
                source_path=str(root),
                profile="debian",
                live_uuid="2026-03-14-11-25-23-00",
                persistence=False,
            )

            self.assertIn("insmod iso9660", rendered["grub_cfg"])
            self.assertNotIn("insmod ext2", rendered["grub_cfg"])

    def test_render_managed_grub_enforces_mandatory_wifi_free_live_hook_args(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config_path = root / "debian-usb.conf"
            save_config(
                str(config_path),
                {
                    "DEFAULT_LIVE_HOOKS": "1",
                    "DEFAULT_LIVE_ARGS_HOOKS": "live-config.hooks=medium",
                },
            )
            (root / "EFI/boot").mkdir(parents=True)
            (root / "EFI/boot/bootx64.efi").write_text("", encoding="utf-8")
            (root / "isolinux").mkdir(parents=True)
            (root / "live").mkdir()
            (root / "live/vmlinuz").write_text("", encoding="utf-8")
            (root / "live/initrd.img").write_text("", encoding="utf-8")
            (root / "live/filesystem.squashfs").write_text("", encoding="utf-8")
            (root / "isolinux/live.cfg").write_text(
                """
label live
  menu label Live
  linux /live/vmlinuz
  append initrd=/live/initrd.img boot=live components quiet live_wifi_psk_b64=cmV0aXJlZA LIVE_WIFI_ESSID=retired DEFAULT_LIVE_WIFI_GATEWAY=192.0.2.1 netcfg/wireless_essid=retired netcfg/wireless_wpa=retired
""",
                encoding="utf-8",
            )

            rendered = render_managed_grub(
                source_path=str(root),
                profile="debian",
                live_uuid="DEBIAN-UUID",
                persistence=False,
                config_path=str(config_path),
            )
            entries = parse_grub_entries(rendered["grub_cfg"], "boot/grub/grub.cfg")
            live_entry = next(entry for entry in entries if entry.title == "Live")
            self.assertIn("live-config.hooks=medium", live_entry.kernel_args)
            for token in live_entry.kernel_args.split():
                key = token.split("=", 1)[0]
                self.assertFalse(key.startswith("live_wifi_"), token)
                self.assertFalse(key.startswith("LIVE_WIFI_"), token)
                self.assertFalse(key.startswith("DEFAULT_LIVE_WIFI_"), token)
                self.assertFalse(key.startswith("netcfg/wireless_"), token)

    def test_live_hook_args_are_limited_to_debian_profile(self) -> None:
        config_data = {
            "DEFAULT_LIVE_HOOKS": "1",
            "DEFAULT_LIVE_ARGS_HOOKS": "live-config.hooks=medium",
        }

        self.assertEqual(_live_hook_kernel_args(config_data, "debian"), "live-config.hooks=medium")
        disabled_config = dict(config_data)
        disabled_config["DEFAULT_LIVE_HOOKS"] = "0"
        disabled_config["DEFAULT_LIVE_ARGS_HOOKS"] = "live-config.hooks=filesystem"
        self.assertEqual(_live_hook_kernel_args(disabled_config, "debian"), "live-config.hooks=medium")
        self.assertEqual(_live_hook_kernel_args(config_data, "kali-linux"), "")
        self.assertEqual(_live_hook_kernel_args(config_data, "tails"), "")
        self.assertEqual(_live_hook_kernel_args(config_data, "ubuntu-desktop"), "")

    def test_live_hook_args_strip_all_wifi_transports(self) -> None:
        config_data = {
            "DEFAULT_LIVE_HOOKS": "1",
            "DEFAULT_LIVE_ARGS_HOOKS": (
                "live-config.hooks=filesystem debug=1 "
                "live_wifi_essid_b64=R3Vlc3QgTmV0 LIVE_WIFI_SECURITY=open "
                "DEFAULT_LIVE_WIFI_INTERFACE=wlan0 netcfg/wireless_essid=retired"
            ),
        }

        rendered = _live_hook_kernel_args(config_data, "debian")

        self.assertEqual(rendered, "debug=1 live-config.hooks=medium")

    def test_render_managed_grub_with_secure_boot_assets_includes_mok_entry(self) -> None:
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

            rendered = render_managed_grub(
                source_path=str(root),
                profile="debian",
                live_uuid="PAYLOAD-UUID",
                persistence=False,
                boot_assets_uuid="ESP-UUID",
            )

            self.assertIn("MOK Enrollment", rendered["grub_cfg"])
            self.assertIn("set check_signatures=enforce", rendered["grub_cfg"])
            self.assertIn("trust --skip-sig (${trust_root})/secureboot/grub-signing.pub", rendered["grub_cfg"])
            self.assertLess(
                rendered["grub_cfg"].index("trust --skip-sig (${trust_root})/secureboot/grub-signing.pub"),
                rendered["grub_cfg"].index("set check_signatures=enforce"),
            )
            self.assertIn("Loading signed kernel. Trust it via the selected Secure Boot mode before booting.", rendered["grub_cfg"])
            self.assertIn("insmod chain", rendered["grub_cfg"])
            self.assertIn("search --no-floppy --fs-uuid --set=root ESP-UUID", rendered["grub_cfg"])
            self.assertIn("chainloader /EFI/debian-usb/mok/mmx64.efi", rendered["grub_cfg"])
            self.assertIn("chainloader /EFI/debian-usb/mok/shimx64.efi", rendered["grub_cfg"])
            self.assertIn("echo Direct MokManager launch failed - trying shim fallback.", rendered["grub_cfg"])
            self.assertNotIn("echo Direct MokManager launch failed; trying shim fallback.", rendered["grub_cfg"])
            self.assertIn("UEFI Keys", rendered["grub_cfg"])
            self.assertIn("chainloader /EFI/debian-usb/updatevars/UpdateVars.efi -a db /secureboot/db.auth", rendered["grub_cfg"])
            self.assertNotIn("chainloader (${boot_root})/EFI/debian-usb/mok/shimx64.efi", rendered["grub_cfg"])
            self.assertIn("linux (${boot_root})/EFI/debian-usb/assets/debian/live/vmlinuz", rendered["grub_cfg"])
            self.assertIn("initrd (${boot_root})/EFI/debian-usb/assets/debian/live/initrd.img", rendered["grub_cfg"])
            self.assertEqual(
                rendered["signed_kernel_assets"],
                [
                    {
                        "iso_path": str(root),
                        "source_path": "/live/vmlinuz",
                        "asset_path": "/EFI/debian-usb/assets/debian/live/vmlinuz",
                    }
                ],
            )
            self.assertEqual(
                rendered["boot_initrd_assets"],
                [
                    {
                        "iso_path": str(root),
                        "source_path": "/live/initrd.img",
                        "asset_path": "/EFI/debian-usb/assets/debian/live/initrd.img",
                    }
                ],
            )

    def test_render_managed_grub_custom_menu_groups_debian_entries(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "EFI/boot").mkdir(parents=True)
            (root / "EFI/boot/bootx64.efi").write_text("", encoding="utf-8")
            (root / "boot/grub").mkdir(parents=True)
            (root / "live").mkdir()
            (root / "live/vmlinuz").write_text("", encoding="utf-8")
            (root / "live/initrd.img").write_text("", encoding="utf-8")
            (root / "install").mkdir()
            (root / "install/vmlinuz").write_text("", encoding="utf-8")
            (root / "install/initrd.gz").write_text("", encoding="utf-8")
            (root / "boot/grub/grub.cfg").write_text(
                """
menuentry 'Live system (amd64)' {
    linux /live/vmlinuz boot=live components quiet splash ---
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

            rendered = render_managed_grub(
                source_path=str(root),
                profile="debian",
                live_uuid="DEBIAN-UUID",
                persistence=True,
                persistence_mode="plain",
                use_custom_menu=True,
            )

            grub_cfg = rendered["grub_cfg"]
            self.assertIn("set timeout=-1", grub_cfg)
            self.assertIn("insmod iso9660", grub_cfg)
            self.assertIn("insmod ext2", grub_cfg)
            self.assertIn("insmod btrfs", grub_cfg)
            self.assertIn('submenu "Boot from Internal Drive"', grub_cfg)
            self.assertIn('submenu "Debian ..."', grub_cfg)
            self.assertIn('submenu "Debian Live ..."', grub_cfg)
            self.assertIn('menuentry "... Debian Live Environment"', grub_cfg)
            self.assertIn('menuentry "... Debian Live Environment (RAM)"', grub_cfg)
            self.assertIn('menuentry "... Debian Live Environment (Persistence)"', grub_cfg)
            self.assertIn('menuentry "... Debian Live Environment (RAM + Persistence)"', grub_cfg)
            self.assertNotIn('submenu "Debian Live Install (HTTP Preseed) ..."', grub_cfg)
            self.assertNotIn('submenu "Debian Live Install (USB Preseed) ..."', grub_cfg)
            self.assertNotIn('menuentry "... Debian Live Install [Expert]"', grub_cfg)

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
                "did not expect live-install preset entries when only the live tree is staged",
            )

    def test_managed_boot_asset_path_preserves_versioned_live_basenames(self) -> None:
        self.assertEqual(
            _managed_boot_asset_path("debian", "live", "/live/vmlinuz-6.12.86+deb13-amd64", initrd=False),
            "/boot/debian/live/vmlinuz-6.12.86+deb13-amd64",
        )
        self.assertEqual(
            _managed_boot_asset_path("debian", "live", "/live/initrd.img-6.12.86+deb13-amd64", initrd=True),
            "/boot/debian/live/initrd.img-6.12.86+deb13-amd64",
        )

    def test_render_managed_grub_custom_menu_prefers_text_expert_entries_when_graphical_and_text_titles_match(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "EFI/boot").mkdir(parents=True)
            (root / "EFI/boot/bootx64.efi").write_text("", encoding="utf-8")
            _write_prepared_netinst_assets(root)
            (root / "boot/grub").mkdir(parents=True)
            (root / "boot/grub/grub.cfg").write_text(
                """
submenu 'Graphical installer ...' {
menuentry 'Expert install' {
    linux /hd-media/vmlinuz priority=low source=graphical ---
    initrd /hd-media/initrd.gz
}
menuentry 'Automated install' {
    linux /hd-media/vmlinuz auto=true priority=critical source=graphical ---
    initrd /hd-media/initrd.gz
}
}
submenu 'Text installer ...' {
menuentry 'Expert install' {
    linux /hd-media/vmlinuz priority=low source=text ---
    initrd /hd-media/initrd.gz
}
menuentry 'Automated install' {
    linux /hd-media/vmlinuz auto=true priority=critical source=text ---
    initrd /hd-media/initrd.gz
}
}
""",
                encoding="utf-8",
            )

            rendered = render_managed_grub(
                source_path=str(root),
                profile="debian",
                live_uuid="DEBIAN-UUID",
                persistence=False,
                use_custom_menu=True,
                include_preseed_entries=True,
            )

            entries = parse_grub_entries(rendered["grub_cfg"], "boot/grub/grub.cfg")
            online_preset = next(
                entry
                for entry in entries
                if entry.menu_path
                == ("Debian ...", "Debian Netinst ...", "Debian Netinst Install (HTTP Preseed) ...", "Preseed Internal ...")
            )
            self.assertEqual(online_preset.kernel_path, "/boot/debian/netinst/vmlinuz")
            self.assertEqual(online_preset.initrd_path, "/boot/debian/netinst/initrd.gz")
            self.assertIn("source=text", online_preset.kernel_args)
            self.assertIn(
                "iso-scan/filename=/boot/iso/debian/netinst/fixture-netinst.iso",
                online_preset.kernel_args,
            )
            self.assertNotIn("INSTALL_MEDIA_DEV=", online_preset.kernel_args)
            self.assertIn("url=", online_preset.kernel_args)

    def test_render_managed_grub_custom_menu_can_append_preserved_upstream_entries(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "EFI/boot").mkdir(parents=True)
            (root / "EFI/boot/bootx64.efi").write_text("", encoding="utf-8")
            (root / "boot/grub").mkdir(parents=True)
            (root / "live").mkdir()
            (root / "live/vmlinuz").write_text("", encoding="utf-8")
            (root / "live/initrd.img").write_text("", encoding="utf-8")
            (root / "install").mkdir()
            (root / "install/vmlinuz").write_text("", encoding="utf-8")
            (root / "install/initrd.gz").write_text("", encoding="utf-8")
            (root / "boot/grub/grub.cfg").write_text(
                """
menuentry 'Live system (amd64)' {
    linux /live/vmlinuz boot=live components quiet splash ---
    initrd /live/initrd.img
}
submenu 'Text installer ...' {
menuentry 'Expert install' {
    linux /install/vmlinuz priority=low ---
    initrd /install/initrd.gz
}
}
""",
                encoding="utf-8",
            )

            rendered = render_managed_grub(
                source_path=str(root),
                profile="debian",
                live_uuid="DEBIAN-UUID",
                persistence=False,
                use_custom_menu=True,
                preserve_upstream_entries=True,
            )

            grub_cfg = rendered["grub_cfg"]
            self.assertIn('submenu "Debian Legacy ..."', grub_cfg)
            self.assertNotIn('submenu "Debian Live Preserved ..."', grub_cfg)
            self.assertIn('menuentry "Live system (amd64)" {', grub_cfg)
            self.assertNotIn('menuentry "Expert install" {', grub_cfg)

    def test_render_managed_grub_custom_menu_preserved_entries_keep_secure_boot_namespace_and_payload_uuid(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "EFI/boot").mkdir(parents=True)
            (root / "EFI/boot/bootx64.efi").write_text("", encoding="utf-8")
            (root / "boot/grub").mkdir(parents=True)
            (root / "live").mkdir()
            (root / "live/vmlinuz").write_text("", encoding="utf-8")
            (root / "live/initrd.img").write_text("", encoding="utf-8")
            (root / "boot/grub/grub.cfg").write_text(
                """
menuentry 'Live system (amd64)' {
    linux /live/vmlinuz boot=live components quiet splash ---
    initrd /live/initrd.img
}
""",
                encoding="utf-8",
            )

            rendered = render_managed_grub(
                source_path=str(root),
                profile="debian",
                live_uuid="DEBIAN-UUID",
                persistence=False,
                use_custom_menu=True,
                preserve_upstream_entries=True,
                boot_assets_uuid="ESP-UUID",
            )

            grub_cfg = rendered["grub_cfg"]
            self.assertIn("MOK Enrollment", grub_cfg)
            self.assertIn("set check_signatures=enforce", grub_cfg)
            self.assertIn("trust --skip-sig (${trust_root})/secureboot/grub-signing.pub", grub_cfg)
            self.assertIn("/EFI/debian-usb/assets/debian/live/vmlinuz", grub_cfg)
            self.assertIn("initrd (${boot_root})/EFI/debian-usb/assets/debian/live/initrd.img", grub_cfg)
            self.assertNotIn("set=payload_root", grub_cfg)

    def test_render_managed_grub_custom_menu_uses_debian_netinst_labels_for_installer_media(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "EFI/boot").mkdir(parents=True)
            (root / "EFI/boot/bootx64.efi").write_text("", encoding="utf-8")
            _write_prepared_netinst_assets(root)
            (root / "boot/grub").mkdir(parents=True)
            (root / "boot/grub/grub.cfg").write_text(
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

            rendered = render_managed_grub(
                source_path=str(root),
                profile="debian",
                live_uuid="DEBIAN-NETINST-UUID",
                persistence=False,
                use_custom_menu=True,
                include_preseed_entries=True,
            )

            grub_cfg = rendered["grub_cfg"]
            self.assertIn('submenu "Debian ..."', grub_cfg)
            self.assertIn('submenu "Debian Netinst ..."', grub_cfg)
            self.assertIn('submenu "Debian Netinst Install (HTTP Preseed) ..."', grub_cfg)
            self.assertIn('submenu "Debian Netinst Install (USB Preseed) ..."', grub_cfg)
            self.assertIn('submenu "Preseed Internal ..."', grub_cfg)
            self.assertIn('submenu "Preseed Public ..."', grub_cfg)
            self.assertNotIn('menuentry "... Debian Netinst Install [Expert]"', grub_cfg)
            self.assertNotIn('menuentry "Debian Live"', grub_cfg)

            entries = parse_grub_entries(grub_cfg, "boot/grub/grub.cfg")
            default_title = "... Debian Netinst Install" + _repo_preset_label(
                "debian", "debian", "PRESEED_ONE_ARGS_DEBIAN"
            )
            dualboot_title = "... Debian Netinst Install" + _repo_preset_label(
                "debian", "debian", "PRESEED_THREE_ARGS_DEBIAN"
            )
            placeholder_title = "... Debian Netinst Install" + _repo_preset_label(
                "debian", "debian", "PRESEED_EIGHT_ARGS_DEBIAN"
            )
            configured_interface = _template_kernel_arg("PRESEED_COMMON_KERNEL_ARGS", "interface")
            public_url = load_template_config()["DEBIAN_PRESEED_PUBLIC_URL"]

            legacy_install = next(
                entry
                for entry in entries
                if entry.title == "... Debian Netinst Install"
                and entry.menu_path == ("Debian ...", "Debian Legacy ...")
            )
            self.assertEqual(legacy_install.kernel_path, "/boot/debian/netinst/vmlinuz")
            self.assertFalse(
                any(
                    entry.title == "... Debian Netinst Install"
                    and entry.menu_path == ("Debian ...", "Debian Netinst ...")
                    for entry in entries
                )
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
            self.assertIn("iso-scan/filename=/boot/iso/debian/netinst/fixture-netinst.iso", online_preset.kernel_args)
            self.assertNotIn("INSTALL_MEDIA_DEV=", online_preset.kernel_args)
            self.assertIn("url=", online_preset.kernel_args)
            self.assertIn(configured_interface, online_preset.kernel_args.split())

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
            self.assertIn("dualboot_efi=1", dualboot_preset.kernel_args.split())
            self.assertIn("dualboot_debian=5", dualboot_preset.kernel_args.split())

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
            self.assertEqual(_seed_transport_tokens(offline_preset.kernel_args), [f"url={public_url}"])

            ssh_preset = next(
                entry
                for entry in entries
                if entry.title == placeholder_title
                and entry.menu_path
                == (
                    "Debian ...",
                    "Debian Netinst ...",
                    "Debian Netinst Install (USB Preseed) ...",
                    "Preseed Internal ...",
                )
                and _grub_escaped_config_arg("PRESEED_EIGHT_ARGS_DEBIAN", "classes")
                in entry.kernel_args.split()
            )
            self.assertIn(
                _grub_escaped_config_arg("PRESEED_EIGHT_ARGS_DEBIAN", "classes"),
                ssh_preset.kernel_args.split(),
            )

    def test_render_grub_entry_escapes_special_kernel_arg_characters(self) -> None:
        entry = BootEntry(
            title="Debian Netinst Install",
            kernel_path="/install.amd/vmlinuz",
            initrd_path="/install.amd/initrd.gz",
            kernel_args=(
                "auto=true "
                "classes=prod\\;desktop\\;static "
                "diagnostic_tag=5B&876@key3@%0V$09#wq27$8LzmZ2!&CnR^E^G "
                "crowdsec_token=retired-secret-transport "
                "url=https://example.test/preseed.cfg?token=a&mode=b"
            ),
            source="generated/test",
            kind="installer",
        )

        rendered = "\n".join(_render_grub_entry(entry, "DEBIAN-UUID"))

        self.assertIn("linux /install.amd/vmlinuz auto=true", rendered)
        self.assertIn(r"classes=prod\;desktop\;static", rendered)
        self.assertIn(r"diagnostic_tag=5B\&876@key3@%0V\$09\#wq27\$8LzmZ2\!\&CnR^E^G", rendered)
        self.assertNotIn("crowdsec_token=", rendered)
        self.assertNotIn("retired-secret-transport", rendered)
        self.assertIn(r"url=https://example.test/preseed.cfg?token=a\&mode=b", rendered)
        self.assertIn("initrd /install.amd/initrd.gz", rendered)

    def test_managed_boot_asset_path_preserves_installer_initrd_gz_basename(self) -> None:
        self.assertEqual(
            _managed_boot_asset_path("debian", "installer", "/install/initrd.gz", initrd=True),
            "/boot/debian/installer/initrd.gz",
        )
        self.assertEqual(
            _managed_boot_asset_path("debian", "installer", "/install/gtk/initrd.gz", initrd=True),
            "/boot/debian/installer/gtk/initrd.gz",
        )

    def test_render_managed_grub_installer_media_ignores_live_menu_override_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "EFI/boot").mkdir(parents=True)
            (root / "EFI/boot/bootx64.efi").write_text("", encoding="utf-8")
            _write_prepared_netinst_assets(root)
            (root / "boot/grub").mkdir(parents=True)
            (root / "boot/grub/grub.cfg").write_text(
                """
menuentry 'Install' {
  linux /hd-media/vmlinuz ---
  initrd /hd-media/initrd.gz
}
""",
                encoding="utf-8",
            )

            rendered = render_managed_grub(
                source_path=str(root),
                profile="debian",
                live_uuid="DEBIAN-NETINST-UUID",
                persistence=False,
                menu_label_override="Debian Live",
            )

            self.assertIn('set kernel="/boot/debian/netinst/vmlinuz"', rendered["grub_cfg"])
            self.assertIn('set initrd="/boot/debian/netinst/initrd.gz"', rendered["grub_cfg"])
            self.assertIn('set isofile="/boot/iso/debian/netinst/fixture-netinst.iso"', rendered["grub_cfg"])
            self.assertIn("iso-scan/filename=$isofile", rendered["grub_cfg"])
            self.assertNotIn("INSTALL_MEDIA_DEV=", rendered["grub_cfg"])

    def test_render_managed_grub_custom_menu_uses_kali_installer_labels_for_installer_media(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "EFI/boot").mkdir(parents=True)
            (root / "EFI/boot/bootx64.efi").write_text("", encoding="utf-8")
            _write_prepared_netinst_assets(root)
            (root / "boot/grub").mkdir(parents=True)
            (root / "boot/grub/grub.cfg").write_text(
                """
menuentry 'Install' {
  linux /hd-media/vmlinuz net.ifnames=0 preseed/file=/cdrom/simple-cdd/default.preseed ---
  initrd /hd-media/initrd.gz
}
submenu 'Advanced options ...' {
menuentry '... Expert install' {
  linux /hd-media/vmlinuz priority=low net.ifnames=0 preseed/file=/cdrom/simple-cdd/default.preseed ---
  initrd /hd-media/initrd.gz
}
menuentry '... Automated install' {
  linux /hd-media/vmlinuz auto=true priority=critical net.ifnames=0 preseed/file=/cdrom/simple-cdd/default.preseed ---
  initrd /hd-media/initrd.gz
}
menuentry '... Rescue mode' {
  linux /hd-media/vmlinuz rescue/enable=true net.ifnames=0 preseed/file=/cdrom/simple-cdd/default.preseed ---
  initrd /hd-media/initrd.gz
}
}
""",
                encoding="utf-8",
            )

            rendered = render_managed_grub(
                source_path=str(root),
                profile="kali-linux",
                live_uuid="KALI-INSTALLER-UUID",
                persistence=False,
                use_custom_menu=True,
                include_preseed_entries=True,
            )

            grub_cfg = rendered["grub_cfg"]
            self.assertIn('submenu "Kali ..."', grub_cfg)
            self.assertIn('submenu "Kali Netinst ..."', grub_cfg)
            self.assertIn('submenu "Kali Netinst Install (HTTP Preseed) ..."', grub_cfg)
            self.assertIn('submenu "Kali Netinst Install (USB Preseed) ..."', grub_cfg)
            self.assertIn('submenu "Preseed Internal ..."', grub_cfg)
            self.assertIn('submenu "Preseed Public ..."', grub_cfg)
            self.assertIn('menuentry "... Kali Netinst Install (ROLE=Desktop,GPU=Nvidia,NET=DHCP)"', grub_cfg)
            self.assertIn('menuentry "... Kali Netinst Install"', grub_cfg)
            self.assertIn('menuentry "... Kali Netinst Expert Install"', grub_cfg)
            self.assertIn('menuentry "... Kali Netinst Rescue Environment"', grub_cfg)
            self.assertNotIn('menuentry "... Kali Netinst Install (HTTP Preseed)"', grub_cfg)
            self.assertNotIn('menuentry "... Kali Netinst Install (USB Preseed)"', grub_cfg)
            self.assertNotIn('menuentry "Kali Installer (Offline Normal)"', grub_cfg)
            self.assertNotIn('menuentry "Kali Live"', grub_cfg)
            entries = parse_grub_entries(grub_cfg, "boot/grub/grub.cfg")
            configured_interface = _template_kernel_arg("PRESEED_COMMON_KERNEL_ARGS", "interface")
            public_url = load_template_config()["DEBIAN_PRESEED_PUBLIC_URL"]
            default_title = "... Kali Netinst Install" + _repo_preset_label(
                "kali-linux", "kali", "PRESEED_ONE_ARGS_KALI"
            )

            online_preseed = next(
                entry
                for entry in entries
                if entry.title == default_title
                and entry.menu_path
                == ("Kali ...", "Kali Netinst ...", "Kali Netinst Install (HTTP Preseed) ...", "Preseed Internal ...")
            )
            self.assertEqual(online_preseed.kernel_path, "/boot/kali/netinst/vmlinuz")
            self.assertIn("iso-scan/filename=/boot/iso/kali/netinst/fixture-netinst.iso", online_preseed.kernel_args)
            self.assertNotIn("INSTALL_MEDIA_DEV=", online_preseed.kernel_args)
            self.assertIn("url=", online_preseed.kernel_args)
            self.assertIn(configured_interface, online_preseed.kernel_args.split())
            self.assertIn("net.ifnames=0", online_preseed.kernel_args.split())

            legacy_install = next(
                entry
                for entry in entries
                if entry.title == "... Kali Netinst Install"
                and entry.menu_path == ("Kali ...", "Kali Legacy ...")
            )
            self.assertEqual(legacy_install.kernel_path, "/boot/kali/netinst/vmlinuz")
            self.assertFalse(
                any(
                    entry.title == "... Kali Netinst Install"
                    and entry.menu_path == ("Kali ...", "Kali Netinst ...")
                    for entry in entries
                )
            )

            offline_preseed = next(
                entry
                for entry in entries
                if entry.title == default_title
                and entry.menu_path
                == ("Kali ...", "Kali Netinst ...", "Kali Netinst Install (USB Preseed) ...", "Preseed Public ...")
            )
            self.assertEqual(offline_preseed.kernel_path, "/boot/kali/netinst/vmlinuz")
            self.assertEqual(_seed_transport_tokens(offline_preseed.kernel_args), [f"url={public_url}"])
            self.assertNotIn("/cdrom/simple-cdd/default.preseed", offline_preseed.kernel_args)

    def test_render_managed_grub_custom_menu_adds_kali_encrypted_live_entries(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "EFI/boot").mkdir(parents=True)
            (root / "EFI/boot/bootx64.efi").write_text("", encoding="utf-8")
            (root / "boot/grub").mkdir(parents=True)
            (root / "live").mkdir()
            (root / "live/vmlinuz").write_text("", encoding="utf-8")
            (root / "live/initrd.img").write_text("", encoding="utf-8")
            (root / "boot/grub/grub.cfg").write_text(
                """
menuentry 'Live system (amd64)' {
    linux /live/vmlinuz boot=live components quiet splash ---
    initrd /live/initrd.img
}
""",
                encoding="utf-8",
            )

            with patch("debian_usb.boot_render._supports_encrypted_persistence", return_value=True):
                rendered = render_managed_grub(
                    source_path=str(root),
                    profile="kali-linux",
                    live_uuid="KALI-UUID",
                    persistence=False,
                    use_custom_menu=True,
                )

            grub_cfg = rendered["grub_cfg"]
            self.assertIn('menuentry "... Kali Live Environment (Encrypted Persistence)"', grub_cfg)
            self.assertIn('menuentry "... Kali Live Environment (RAM + Encrypted Persistence)"', grub_cfg)

            entries = parse_grub_entries(grub_cfg, "boot/grub/grub.cfg")
            encrypted_entry = next(
                entry
                for entry in entries
                if entry.title == "... Kali Live Environment (Encrypted Persistence)"
                and entry.menu_path == ("Kali ...", "Kali Live ...")
            )
            self.assertIn("persistent=cryptsetup", encrypted_entry.kernel_args)
            self.assertIn("persistence-encryption=luks", encrypted_entry.kernel_args)
            ram_encrypted_entry = next(
                entry
                for entry in entries
                if entry.title == "... Kali Live Environment (RAM + Encrypted Persistence)"
                and entry.menu_path == ("Kali ...", "Kali Live ...")
            )
            self.assertIn("persistent=cryptsetup", ram_encrypted_entry.kernel_args)
            self.assertIn("persistence-encryption=luks", ram_encrypted_entry.kernel_args)
            self.assertIn("persistence-storage=filesystem", ram_encrypted_entry.kernel_args)
            self.assertIn("union=overlay", ram_encrypted_entry.kernel_args)
            self.assertIn("toram=filesystem.squashfs", ram_encrypted_entry.kernel_args.split())

    def test_render_managed_grub_prepared_hd_media_netinst_uses_fs_uuid_search_for_boot_payload(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "EFI/boot").mkdir(parents=True)
            (root / "EFI/boot/bootx64.efi").write_text("", encoding="utf-8")
            _write_prepared_netinst_assets(root)
            (root / "boot/grub").mkdir(parents=True)
            (root / "boot/grub/grub.cfg").write_text(
                """
menuentry 'Install' {
  linux /hd-media/vmlinuz ---
  initrd /hd-media/initrd.gz
}
""",
                encoding="utf-8",
            )

            rendered = render_managed_grub(
                source_path=str(root),
                profile="debian",
                live_uuid="DEBIAN-NETINST-UUID",
                persistence=False,
                use_custom_menu=True,
                include_preseed_entries=True,
                boot_assets_uuid="ESP-UUID",
            )

            grub_cfg = rendered["grub_cfg"]
            self.assertIn('set kernel="(${boot_root})/EFI/debian-usb/assets/debian/hd-media/vmlinuz"', grub_cfg)
            self.assertIn('set initrd="(${boot_root})/EFI/debian-usb/assets/debian/hd-media/initrd.gz"', grub_cfg)
            self.assertIn('set isofile="/boot/iso/debian/netinst/fixture-netinst.iso"', grub_cfg)
            self.assertIn("linux $kernel", grub_cfg)
            self.assertIn("iso-scan/filename=$isofile", grub_cfg)
            self.assertIn("initrd $initrd", grub_cfg)
            self.assertNotIn("INSTALL_MEDIA_DEV=", grub_cfg)
            self.assertNotIn("set=payload_root", grub_cfg)
            self.assertNotIn("(hd0,gpt", grub_cfg)
            self.assertNotIn("/dev/disk/by-partuuid/", grub_cfg)
            self.assertEqual(rendered["installer_boot_initrd_patches"], [])

    def test_render_managed_grub_loads_ext2_for_extracted_payload_profiles(self) -> None:
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

            rendered = render_managed_grub(
                source_path=str(root),
                profile="ubuntu-desktop",
                live_uuid="ABCD-1234",
                persistence=False,
            )

            self.assertIn("insmod ext2", rendered["grub_cfg"])
            self.assertNotIn("insmod iso9660", rendered["grub_cfg"])
            self.assertNotIn("INSTALL_MEDIA_DEV=", rendered["grub_cfg"])

    def test_render_managed_grub_custom_menu_adds_ubuntu_ram_entry(self) -> None:
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

            for profile, title in (
                ("ubuntu-desktop", "Ubuntu Desktop"),
                ("ubuntu-server", "Ubuntu Server"),
            ):
                with self.subTest(profile=profile):
                    rendered = render_managed_grub(
                        source_path=str(root),
                        profile=profile,
                        live_uuid="ABCD-1234",
                        persistence=False,
                        use_custom_menu=True,
                    )

                    grub_cfg = rendered["grub_cfg"]
                    self.assertIn('submenu "Ubuntu ..."', grub_cfg)
                    self.assertIn(f'menuentry "{title}"', grub_cfg)
                    self.assertIn(f'menuentry "{title} (RAM)"', grub_cfg)
                    entries = parse_grub_entries(grub_cfg, "boot/grub/grub.cfg")
                    ubuntu_ram = next(
                        entry
                        for entry in entries
                        if entry.title == f"{title} (RAM)" and entry.menu_path == ("Ubuntu ...",)
                    )
                    self.assertIn("toram", ubuntu_ram.kernel_args)

    def test_render_managed_grub_custom_menu_uses_tails_fixed_live_and_family_legacy_menus(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "EFI/boot").mkdir(parents=True)
            (root / "EFI/boot/bootx64.efi").write_text("", encoding="utf-8")
            (root / "boot/grub").mkdir(parents=True)
            (root / "live").mkdir()
            (root / "live/vmlinuz").write_text("", encoding="utf-8")
            (root / "live/initrd.img").write_text("", encoding="utf-8")
            (root / "live/Tails.module").write_text("", encoding="utf-8")
            (root / "boot/grub/grub.cfg").write_text(
                """
menuentry 'Tails' {
    linux /live/vmlinuz boot=live quiet splash ---
    initrd /live/initrd.img
}
menuentry 'Tails (Troubleshooting)' {
    linux /live/vmlinuz boot=live nomodeset ---
    initrd /live/initrd.img
}
""",
                encoding="utf-8",
            )

            with patch("debian_usb.bootconfig._supports_encrypted_persistence", return_value=True):
                rendered = render_managed_grub(
                    source_path=str(root),
                    profile="tails",
                    live_uuid="TAILS-UUID",
                    persistence=False,
                    use_custom_menu=True,
                    preserve_upstream_entries=True,
                )

            grub_cfg = rendered["grub_cfg"]
            self.assertIn('submenu "Tails ..."', grub_cfg)
            self.assertIn('submenu "Tails Live ..."', grub_cfg)
            self.assertIn('submenu "Tails Legacy ..."', grub_cfg)
            self.assertNotIn('submenu "Tails Live Preserved ..."', grub_cfg)
            for title in (
                'menuentry "... Tails Live Environment"',
                'menuentry "... Tails Live Environment (RAM)"',
                'menuentry "... Tails Live Environment (Persistence)"',
                'menuentry "... Tails Live Environment (RAM + Persistence)"',
                'menuentry "... Tails Live Environment (Encrypted Persistence)"',
                'menuentry "... Tails Live Environment (RAM + Encrypted Persistence)"',
            ):
                self.assertIn(title, grub_cfg)

    def test_render_managed_grub_netboot_bundle_uses_boot_assets_without_iso_scan(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            kernel = root / "linux"
            initrd = root / "initrd.gz"
            kernel.write_text("netboot-kernel", encoding="utf-8")
            initrd.write_text("netboot-initrd", encoding="utf-8")
            bundle = prepare_managed_installer_source(
                "debian",
                "netboot",
                str(kernel),
                str(initrd),
                output_dir=str(root / "bundle"),
            )

            rendered = render_managed_grub(
                source_path=bundle["source_path"],
                profile="debian",
                live_uuid="DEBIAN-NETBOOT-UUID",
                persistence=False,
                config_path="configs/debian-usb.conf",
                use_custom_menu=True,
                include_preseed_entries=True,
                source_role="netboot",
            )

            grub_cfg = rendered["grub_cfg"]
            self.assertEqual(rendered["iso_payloads"], [])
            self.assertIn('menuentry "... Debian Netboot Install"', grub_cfg)
            self.assertIn('menuentry "... Debian Netboot Expert Install"', grub_cfg)
            self.assertIn('menuentry "... Debian Netboot Rescue Environment"', grub_cfg)
            entries = parse_grub_entries(grub_cfg, "boot/grub/grub.cfg")
            install_entry = next(
                entry
                for entry in entries
                if entry.title == "... Debian Netboot Install" and entry.menu_path == ("Debian ...", "Debian Legacy ...")
            )
            self.assertEqual(install_entry.kernel_path, "/boot/debian/netboot/vmlinuz")
            self.assertEqual(install_entry.initrd_path, "/boot/debian/netboot/initrd.gz")
            self.assertNotIn("iso-scan/filename=", install_entry.kernel_args)

    def test_render_managed_grub_netinst_bundle_uses_bundled_iso_name_and_exposes_installer_variants(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            kernel = root / "linux"
            initrd = root / "initrd.gz"
            iso = root / "debian-netinst.iso"
            kernel.write_text("hd-media-kernel", encoding="utf-8")
            initrd.write_text("hd-media-initrd", encoding="utf-8")
            iso.write_text("iso", encoding="utf-8")
            with patch(
                "debian_usb.installer_sources.validate_netinst_payload_iso",
                return_value={"media_class": "installer", "installer_entry_count": 1, "live_entry_count": 0},
            ), patch(
                "debian_usb.installer_sources._ensure_installer_assets_align_with_iso",
                return_value=None,
            ), patch(
                "debian_usb.installer_sources._enforce_exact_iso_scan_filename",
                return_value={"enforced": True, "changed": True},
            ):
                bundle = prepare_managed_installer_source(
                    "debian",
                    "netinst",
                    str(kernel),
                    str(initrd),
                    iso_path=str(iso),
                    output_dir=str(root / "bundle"),
                )

            rendered = render_managed_grub(
                source_path=bundle["source_path"],
                profile="debian",
                live_uuid="DEBIAN-NETINST-UUID",
                persistence=False,
                config_path="configs/debian-usb.conf",
                use_custom_menu=True,
                include_preseed_entries=True,
                source_role="netinst",
            )

            self.assertEqual(
                rendered["iso_payloads"],
                [
                    {
                        "source_path": bundle["iso_path"],
                        "target_path": "/boot/iso/debian/netinst/debian-netinst.iso",
                    }
                ],
            )
            grub_cfg = rendered["grub_cfg"]
            self.assertIn('menuentry "... Debian Netinst Install"', grub_cfg)
            self.assertIn('menuentry "... Debian Netinst Expert Install"', grub_cfg)
            self.assertIn('menuentry "... Debian Netinst Rescue Environment"', grub_cfg)
            self.assertIn('set kernel="/boot/debian/netinst/vmlinuz"', grub_cfg)
            self.assertIn('set initrd="/boot/debian/netinst/initrd.gz"', grub_cfg)
            self.assertIn('set isofile="/boot/iso/debian/netinst/debian-netinst.iso"', grub_cfg)
            self.assertIn("linux $kernel", grub_cfg)
            self.assertIn("iso-scan/filename=$isofile", grub_cfg)
            self.assertIn("initrd $initrd", grub_cfg)
            self.assertNotIn("/live/", grub_cfg)
            self.assertNotIn("loopback", grub_cfg)


if __name__ == "__main__":
    unittest.main()
