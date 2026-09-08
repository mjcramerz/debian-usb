"""Regression: Wi-Fi assessment packages belong to Kali, not Debian Live.

Package planning and orchestration are real; ISO extraction/chroot writes are
mocked in the pipeline test. This suite never modifies the build host or USBs.
"""
from __future__ import annotations

from contextlib import ExitStack
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from debian_usb import build_iso, live_hooks, live_tools, rebuild_iso
from test_build_iso import minimal_plan, minimal_netinst_plan
from test_rebuild_iso import FakeEntry, FakeSource


BASELINE = Path(__file__).resolve().parents[1] / "fixtures/debian-live-admin-baseline.json"
KALI_ONLY_PACKAGES = {
    "wifite", "aircrack-ng", "reaver", "pixiewps", "hcxdumptool", "hcxtools",
    "hashcat", "tshark", "wireshark", "wavemon", "horst", "hostapd", "arp-scan",
    "python3-scapy", "kismet", "kali-tools-wireless", "kali-tools-802-11",
    "airgeddon", "bettercap", "wifiphisher",
}
WIFI_RUNTIME = {"network-manager", "iw", "wpasupplicant", "rfkill", "iproute2"}


class LiveToolScopeTests(unittest.TestCase):
    def assert_no_kali_tools(self, packages: list[str]) -> None:
        self.assertFalse(KALI_ONLY_PACKAGES.intersection(packages), packages)

    def test_debian_default_all_exactly_matches_original_uploaded_catalog(self) -> None:
        baseline = json.loads(BASELINE.read_text(encoding="utf-8"))
        packages, profile = live_tools.live_tool_packages_for_profile("debian")
        expected_packages = list(dict.fromkeys(
            package for group in baseline["package_groups"] for package in group["packages"]
        ))
        self.assertEqual(packages, expected_packages)
        self.assertEqual(profile["selected_groups"], [group["id"] for group in baseline["package_groups"]])
        self.assertEqual(profile["command_packages"], baseline["command_packages"])
        self.assertEqual(profile["optional_packages"], [])
        self.assert_no_kali_tools(packages)
        for current, original in zip(profile["package_group_options"], baseline["package_groups"]):
            self.assertEqual({key: current[key] for key in original}, original)

    def test_build_and_remaster_defaults_agree_for_all_supported_profiles(self) -> None:
        for distro, profile in (("debian", "debian"), ("kali-linux", "kali-linux"), ("ubuntu", "ubuntu-desktop")):
            with self.subTest(distro=distro):
                build_packages, build_metadata = live_tools.live_tool_packages_for_build_distro(distro)
                remaster_packages, metadata = live_tools.live_tool_packages_for_profile(profile)
                self.assertEqual(build_packages, remaster_packages)
                self.assertEqual(build_metadata["selected_groups"], metadata["selected_groups"])
                if profile != "kali-linux":
                    self.assert_no_kali_tools(build_packages)
                    self.assertNotIn("wireless_security", metadata["package_groups"])
                    self.assertNotIn("wifite", metadata["command_packages"])

    def test_kali_all_and_wireless_only_keep_the_complete_wireless_group(self) -> None:
        for selection in (None, ["wireless_security"]):
            with self.subTest(selection=selection):
                packages, metadata = live_tools.live_tool_packages_for_profile("kali-linux", selected_groups=selection)
                self.assertTrue(KALI_ONLY_PACKAGES.issubset(packages))
                self.assertIn("wireless_security", metadata["selected_groups"])
                self.assertEqual(packages.count("iw"), 1)
                self.assertEqual(packages.count("tcpdump"), 1)

    def test_explicit_empty_selection_does_not_force_security_tools_on_any_profile(self) -> None:
        for profile in ("debian", "kali-linux", "ubuntu-desktop"):
            with self.subTest(profile=profile):
                packages, metadata = live_tools.live_tool_packages_for_profile(profile, selected_groups=[])
                self.assertEqual(packages, [])
                self.assertEqual(metadata["selected_groups"], [])
                self.assertEqual(metadata["optional_packages"], [])

    def test_saved_wireless_selections_are_rejected_for_debian_and_ubuntu(self) -> None:
        for profile in ("debian", "ubuntu-desktop"):
            with self.subTest(profile=profile), self.assertRaisesRegex(ValueError, "Kali Live only"):
                live_tools.live_tool_packages_for_profile(profile, selected_groups=["network_core", "wireless_security"])

    def test_sequential_kali_and_debian_resolutions_do_not_share_mutable_packages(self) -> None:
        for _ in range(2):
            _, kali = live_tools.live_tool_packages_for_profile("kali-linux")
            kali["package_groups"]["network_core"].append("wifite")
            packages, debian = live_tools.live_tool_packages_for_profile("debian")
            self.assert_no_kali_tools(packages)
            self.assertNotIn("wireless_security", debian["selected_groups"])

    def test_legacy_unscoped_catalog_is_rejected(self) -> None:
        payload = json.loads(live_tools.live_tool_profile_path().read_text(encoding="utf-8"))
        payload["schema_version"] = 2
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "legacy.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "replace the legacy catalog"):
                live_tools.live_tool_packages_for_profile("debian", profile_path=path)

    def test_wireless_group_cannot_be_made_global_by_omitting_scope(self) -> None:
        for scopes in (None, [], ["debian"], ["debian", "kali-linux"], ["unknown"]):
            with self.subTest(scopes=scopes), tempfile.TemporaryDirectory() as directory:
                payload = json.loads(live_tools.live_tool_profile_path().read_text(encoding="utf-8"))
                group = next(group for group in payload["package_groups"] if group["id"] == "wireless_security")
                if scopes is None:
                    group.pop("profiles")
                else:
                    group["profiles"] = scopes
                path = Path(directory) / "bad.json"
                path.write_text(json.dumps(payload), encoding="utf-8")
                with self.assertRaises(ValueError):
                    live_tools.load_live_tool_profile(path)

    def test_cross_profile_additions_and_optional_packages_are_rejected(self) -> None:
        for key, value in (("profile_additions", {"debian": {"wireless_security": ["wifite"]}}),
                           ("optional_packages", {"debian": ["wifite"]})):
            with self.subTest(key=key), tempfile.TemporaryDirectory() as directory:
                payload = json.loads(live_tools.live_tool_profile_path().read_text(encoding="utf-8"))
                payload[key] = value
                path = Path(directory) / "bad.json"
                path.write_text(json.dumps(payload), encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "(?:unavailable|not available) for profile"):
                    live_tools.load_live_tool_profile(path)

    def test_debian_build_default_all_and_none_keep_wifi_without_attack_tools(self) -> None:
        for mode in ("live", "none"):
            for selection in (None, []):
                with self.subTest(mode=mode, selection=selection):
                    payload = minimal_plan("/tmp/live-tool-scope-test-output")
                    payload.update(installer_mode=mode, live_tool_groups=selection)
                    plan = build_iso._validate_build_iso_plan(payload)
                    for field in ("base_packages", "storage_tool_packages", "optional_live_packages"):
                        self.assert_no_kali_tools(plan[field])
                    self.assertTrue(WIFI_RUNTIME.issubset(plan["base_packages"]))
                    self.assertNotIn("wireless_security", plan["live_tool_groups"])

    def test_debian_build_rejects_stale_wireless_group(self) -> None:
        plan = minimal_plan("/tmp/live-tool-scope-test-output")
        plan["live_tool_groups"] = ["wireless_security"]
        with self.assertRaisesRegex(ValueError, "Kali Live only"):
            build_iso._validate_build_iso_plan(plan)

    def test_flattened_old_build_package_lists_cannot_bypass_profile_scope(self) -> None:
        for field in ("base_packages", "storage_tool_packages", "extra_chroot_packages", "extra_binary_packages"):
            with self.subTest(field=field):
                plan = minimal_plan("/tmp/live-tool-scope-test-output")
                plan["live_tool_groups"] = []
                plan[field] = [*plan[field], "wifite", "kali-tools-wireless"]
                with self.assertRaisesRegex(ValueError, "Kali Live only"):
                    build_iso._validate_build_iso_plan(plan)

    def test_raw_package_scope_preserves_shared_tools_and_custom_unrelated_packages(self) -> None:
        live_tools.validate_live_tool_package_scope_for_build_distro("debian", ["iw", "rfkill", "tcpdump", "nmap", "custom-recovery-package"])
        live_tools.validate_live_tool_package_scope_for_build_distro("kali-linux", sorted(KALI_ONLY_PACKAGES))

    def test_materialized_build_package_lists_and_hooks_are_profile_scoped(self) -> None:
        repo = Path(__file__).resolve().parents[2]
        for profile in ("debian", "kali-linux"):
            with self.subTest(profile=profile), tempfile.TemporaryDirectory() as directory:
                payload = json.loads((repo / f"configs/spec/live/{profile}/live-build.json").read_text(encoding="utf-8"))
                payload["output_dir"] = str(Path(directory) / "output")
                payload["live_tool_groups"] = None
                plan = build_iso._validate_build_iso_plan(payload)
                workspace = Path(directory) / "build"
                build_iso._materialize_workspace(workspace, plan, io.StringIO(), [])
                package_lists = "\n".join(path.read_text(encoding="utf-8") for path in (workspace / "config/package-lists").iterdir())
                # Check every optional-package hook, without depending on its
                # ordering prefix, so a filename change cannot weaken the test.
                hook_content = "\n".join(path.read_text(encoding="utf-8") for path in (workspace / "config/hooks/normal").glob("*optional*"))
                combined = package_lists + "\n" + hook_content
                if profile == "debian":
                    self.assert_no_kali_tools(plan["storage_tool_packages"] + plan["optional_live_packages"])
                    for package in KALI_ONLY_PACKAGES:
                        self.assertNotIn(package, combined)
                else:
                    self.assertTrue(KALI_ONLY_PACKAGES.issubset(plan["optional_live_packages"]))
                    for package in KALI_ONLY_PACKAGES:
                        self.assertIn(package, hook_content)

    def test_netinst_does_not_receive_live_packages(self) -> None:
        plan = build_iso._validate_build_iso_plan(minimal_netinst_plan("/tmp/live-tool-scope-test-output"))
        self.assertEqual(plan["live_tool_groups"], [])
        self.assertEqual(plan["optional_live_packages"], [])
        self.assert_no_kali_tools(plan["base_packages"] + plan["storage_tool_packages"])
        payload = minimal_netinst_plan("/tmp/live-tool-scope-test-output")
        payload["live_tool_groups"] = ["wireless_security"]
        with self.assertRaisesRegex(ValueError, "forbidden.*netinst"):
            build_iso._validate_build_iso_plan(payload)

    def test_rejected_remaster_does_not_extract_or_install_packages(self) -> None:
        with patch.object(rebuild_iso, "ensure_debian_rebuild_deps") as deps, \
                patch.object(rebuild_iso, "_run_logged") as run, \
                patch.object(rebuild_iso, "open_source") as source:
            with self.assertRaisesRegex(ValueError, "Kali Live only"):
                rebuild_iso.remaster_live_tools_source("/not-read.iso", "debian", selected_groups=["wireless_security"])
            deps.assert_not_called()
            run.assert_not_called()
            source.assert_not_called()

    def test_both_normal_wifi_runtimes_are_unchanged_and_independent_of_tool_selection(self) -> None:
        for profile in ("debian", "kali-linux"):
            with self.subTest(profile=profile), tempfile.TemporaryDirectory() as directory:
                packages = live_hooks.live_hook_packages(profile)
                self.assertTrue(WIFI_RUNTIME.issubset(packages))
                self.assert_no_kali_tools(packages)
                root = Path(directory) / "root"
                live_hooks.stage_live_wifi_runtime(root, profile=profile)
                self.assertTrue((root / "etc/skel/wifi-connect.sh").is_file())
                self.assertTrue((root / "etc/debian-usb/live.env").is_file())
                self.assertTrue((root / "usr/local/lib/debian-usb/live-wifi.py").is_file())

    def test_full_remaster_orchestration_keeps_per_iso_package_lists_separate(self) -> None:
        # Real resolver and orchestration, fake ISO/chroot commands only. Run
        # Kali first to catch cross-item mutation in a Multi-OS style session.
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            root = Path(directory)
            source = root / "source.iso"
            source.write_bytes(b"unchanged source ISO fixture")
            for name, path in (("DEFAULT_WORK_DIR", root / "work"), ("DEFAULT_STATE_DIR", root / "state"),
                               ("DEFAULT_LOG_DIR", root / "logs")):
                stack.enter_context(patch.object(rebuild_iso, name, path))
            for name, value in (("open_source", FakeSource()), ("_find_boot_entries", []),
                                ("_media_class", "live"), ("_host_architecture", "amd64"),
                                ("_select_entry", FakeEntry(title="Live", kernel_path="/live/vmlinuz", initrd_path="/live/initrd.img")),
                                ("_squashfs_processor_count", 1), ("ensure_debian_rebuild_deps", {}),
                                ("_preflight_live_tools_storage", None), ("_rewrite_checksum_files", None)):
                stack.enter_context(patch.object(rebuild_iso, name, return_value=value))
            apply = stack.enter_context(patch.object(rebuild_iso, "_apply_live_tools_remaster", return_value=["/live/filesystem.squashfs"]))

            def fake_run(command: list[str], **_kwargs: object) -> None:
                if "-outdev" in command:
                    Path(command[command.index("-outdev") + 1]).write_bytes(b"mock remaster output")

            stack.enter_context(patch.object(rebuild_iso, "_run_logged", side_effect=fake_run))
            for profile in ("kali-linux", "debian", "kali-linux", "debian"):
                result = rebuild_iso.remaster_live_tools_source(str(source), profile, str(root / profile))
                packages = result["packages"]
                self.assertTrue(WIFI_RUNTIME.issubset(packages))
                self.assertEqual(apply.call_args.kwargs["profile"], profile)
                if profile == "debian":
                    self.assert_no_kali_tools(packages)
                    self.assert_no_kali_tools(apply.call_args.kwargs["optional_packages"])
                    self.assertNotIn("wireless_security", result["selected_groups"])
                else:
                    self.assertTrue(KALI_ONLY_PACKAGES.issubset(packages))
                    self.assertIn("wireless_security", result["selected_groups"])
                self.assertEqual(source.read_bytes(), b"unchanged source ISO fixture")
                self.assertTrue(Path(result["iso_path"]).is_file())
            self.assertEqual(apply.call_count, 4)


if __name__ == "__main__":
    unittest.main()
