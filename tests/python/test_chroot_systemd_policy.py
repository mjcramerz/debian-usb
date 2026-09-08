from __future__ import annotations

from contextlib import nullcontext
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from debian_usb import rebuild_iso
from debian_usb.live_hooks import LIVE_SYSTEMD_MASK_UNITS, stage_live_systemd_masks


@unittest.skipUnless(shutil.which("systemctl"), "offline preset integration requires systemctl")
class ChrootSystemdPolicyTests(unittest.TestCase):
    def _exercise(self, failure: bool, *, empty_masks: bool = False) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            units = root / "usr/lib/systemd/system"
            units.mkdir(parents=True)
            (units / "fwupd-refresh.service").write_text(
                "[Service]\nType=oneshot\nExecStart=/bin/true\n", encoding="utf-8")
            (units / "fwupd-refresh.timer").write_text(
                "[Timer]\nOnBootSec=1h\nUnit=fwupd-refresh.service\n"
                "[Install]\nWantedBy=timers.target\n", encoding="utf-8")
            (units / "timers.target").write_text("[Unit]\nDescription=Fixture timers\n", encoding="utf-8")
            presets = root / "usr/lib/systemd/system-preset"
            presets.mkdir(parents=True)
            (presets / "00-fixture.preset").write_text("enable fwupd-refresh.timer\n", encoding="utf-8")
            stage_live_systemd_masks(root)
            systemd = root / "etc/systemd/system"
            unrelated = systemd / "unrelated.service"
            unrelated.symlink_to("/dev/null")
            if empty_masks:
                for unit in LIVE_SYSTEMD_MASK_UNITS:
                    path = systemd / unit
                    path.unlink()
                    path.write_text("", encoding="utf-8")

            def preset() -> subprocess.CompletedProcess[str]:
                # --root changes unit files only; it never contacts host systemd.
                return subprocess.run(["systemctl", "--root=" + str(root), "preset", "fwupd-refresh.timer"],
                                      capture_output=True, encoding="utf-8", check=False)

            blocked = preset()
            self.assertNotEqual(blocked.returncode, 0)
            self.assertIn("masked", blocked.stderr)
            policy = root / "usr/sbin/policy-rc.d"
            if failure:
                policy.parent.mkdir(parents=True)
                policy.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
                policy.chmod(0o700)
            calls: list[str] = []

            def apt(_root: Path, command: list[str], _log: object) -> None:
                self.assertEqual(subprocess.run([str(policy)], check=False).returncode, 101)
                self.assertEqual(unrelated.readlink(), Path("/dev/null"))
                for unit in LIVE_SYSTEMD_MASK_UNITS:
                    path = systemd / unit
                    self.assertFalse(path.is_symlink())
                    self.assertFalse(path.exists())
                if "install" in command:
                    calls.append("install")
                    configured = preset()
                    self.assertEqual(configured.returncode, 0, configured.stderr)
                    self.assertTrue((systemd / "timers.target.wants/fwupd-refresh.timer").is_symlink())
                    if failure:
                        raise RuntimeError("apt fixture failure")
                else:
                    calls.append(command[-1])

            config = {"list_path": "/tmp/sources.list", "parts_dir": "/tmp/sources.list.d"}
            expected = self.assertRaisesRegex(RuntimeError, "^apt fixture failure$") if failure else nullcontext()
            with expected, patch.object(rebuild_iso, "_temporary_chroot_apt_config", return_value=nullcontext(config)), \
                 patch.object(rebuild_iso, "_mounted_chroot", return_value=nullcontext()), \
                 patch.object(rebuild_iso, "_run_in_chroot", side_effect=apt):
                rebuild_iso._install_packages_in_chroot(root, ["fwupd"], None)
            self.assertEqual(calls, ["update", "install"] if failure else ["update", "install", "install", "clean"])
            for unit in LIVE_SYSTEMD_MASK_UNITS:
                self.assertEqual((systemd / unit).readlink(), Path("/dev/null"))
            self.assertFalse((systemd / "timers.target.wants/fwupd-refresh.timer").is_symlink())
            self.assertEqual(unrelated.readlink(), Path("/dev/null"))
            if failure:
                self.assertEqual(policy.read_text(encoding="utf-8"), "#!/bin/sh\nexit 0\n")
                self.assertEqual(policy.stat().st_mode & 0o777, 0o700)
            else:
                self.assertFalse(policy.exists())

    def test_preset_succeeds_during_guarded_install_and_masks_return(self) -> None:
        self._exercise(False)

    def test_apt_failure_is_not_hidden_and_both_policies_are_restored(self) -> None:
        self._exercise(True)

    def test_empty_unit_file_masks_are_also_temporarily_removed(self) -> None:
        self._exercise(False, empty_masks=True)
