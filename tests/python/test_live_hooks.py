from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from debian_usb import live_hooks


class LiveRootPolicyTests(unittest.TestCase):
    def test_systemd_policy_removes_refresh_enablement_and_masks_units(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            systemd_dir = root / "etc" / "systemd" / "system"
            wants_dir = systemd_dir / "timers.target.wants"
            wants_dir.mkdir(parents=True)
            refresh_link = wants_dir / "fwupd-refresh.timer"
            refresh_link.symlink_to("/usr/lib/systemd/system/fwupd-refresh.timer")
            (systemd_dir / "fwupd-refresh.service").write_text("fixture\n", encoding="utf-8")

            staged = live_hooks.stage_live_systemd_masks(root)
            live_hooks.stage_live_systemd_masks(root)

            self.assertFalse(refresh_link.is_symlink())
            self.assertFalse(refresh_link.exists())
            self.assertEqual(
                [path.name for path in staged],
                ["fwupd-refresh.service", "fwupd-refresh.timer"],
            )
            for unit in live_hooks.LIVE_SYSTEMD_MASK_UNITS:
                mask_path = systemd_dir / unit
                self.assertTrue(mask_path.is_symlink())
                self.assertEqual(mask_path.readlink(), Path("/dev/null"))

    def test_locale_policy_enables_en_us_utf8_and_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "etc" / "default").mkdir(parents=True)
            (root / "etc" / "locale.gen").write_text(
                "# en_US.UTF-8 UTF-8\nde_DE.UTF-8 UTF-8\n",
                encoding="utf-8",
            )
            (root / "etc" / "default" / "locale").write_text(
                "LANG=C\nLANGUAGE=C\nLC_TIME=sv_SE.UTF-8\n",
                encoding="utf-8",
            )

            live_hooks.stage_debian_live_locale(root)
            live_hooks.stage_debian_live_locale(root)

            locale_gen = (root / "etc" / "locale.gen").read_text(encoding="utf-8").splitlines()
            default_locale = (root / "etc" / "default" / "locale").read_text(encoding="utf-8").splitlines()
            self.assertEqual(locale_gen.count("en_US.UTF-8 UTF-8"), 1)
            self.assertIn("de_DE.UTF-8 UTF-8", locale_gen)
            self.assertEqual(default_locale.count("LANG=en_US.UTF-8"), 1)
            self.assertEqual(default_locale.count("LANGUAGE=en_US:en"), 1)
            self.assertIn("LC_TIME=sv_SE.UTF-8", default_locale)

    def test_locale_policy_preserves_upstream_relative_symlink_and_creates_target(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            default_dir = root / "etc" / "default"
            default_dir.mkdir(parents=True)
            locale_link = default_dir / "locale"
            locale_link.symlink_to("../locale.conf")

            live_hooks.stage_debian_live_locale(root)
            live_hooks.stage_debian_live_locale(root)

            locale_conf = root / "etc" / "locale.conf"
            self.assertTrue(locale_link.is_symlink())
            self.assertEqual(locale_link.readlink(), Path("../locale.conf"))
            self.assertTrue(locale_conf.is_file())
            rendered = locale_conf.read_text(encoding="utf-8").splitlines()
            self.assertEqual(rendered.count("LANG=en_US.UTF-8"), 1)
            self.assertEqual(rendered.count("LANGUAGE=en_US:en"), 1)

    def test_locale_policy_interprets_absolute_symlink_inside_live_root(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            default_dir = root / "etc" / "default"
            default_dir.mkdir(parents=True)
            locale_link = default_dir / "locale"
            locale_link.symlink_to("/etc/locale.conf")
            original_write_text = Path.write_text
            write_paths: list[Path] = []

            def guarded_write_text(path: Path, data: str, *args: object, **kwargs: object) -> int:
                self.assertFalse(path.is_symlink(), f"refusing to write through symlink {path}")
                try:
                    path.relative_to(root)
                except ValueError as exc:
                    self.fail(f"refusing to write outside Live root: {path}: {exc}")
                write_paths.append(path)
                return original_write_text(path, data, *args, **kwargs)

            with patch.object(Path, "write_text", new=guarded_write_text):
                live_hooks.stage_debian_live_locale(root)

            locale_conf = root / "etc" / "locale.conf"
            self.assertTrue(locale_link.is_symlink())
            self.assertEqual(locale_link.readlink(), Path("/etc/locale.conf"))
            self.assertIn(locale_conf, write_paths)
            self.assertIn("LANG=en_US.UTF-8", locale_conf.read_text(encoding="utf-8").splitlines())

    def test_locale_policy_rejects_relative_symlink_escape(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            root = base / "live-root"
            default_dir = root / "etc" / "default"
            default_dir.mkdir(parents=True)
            outside = base / "outside"
            outside.write_text("unchanged\n", encoding="utf-8")
            (default_dir / "locale").symlink_to("../../../outside")

            with self.assertRaisesRegex(ValueError, "symlink escapes the Live root"):
                live_hooks.stage_debian_live_locale(root)

            self.assertEqual(outside.read_text(encoding="utf-8"), "unchanged\n")

    def test_locale_policy_rejects_symlink_loop(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            default_dir = root / "etc" / "default"
            default_dir.mkdir(parents=True)
            (default_dir / "locale").symlink_to("../locale.conf")
            (root / "etc" / "locale.conf").symlink_to("default/locale")

            with self.assertRaisesRegex(ValueError, "symlink loop detected"):
                live_hooks.stage_debian_live_locale(root)

    def test_locale_policy_rejects_directory_symlink_target(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            default_dir = root / "etc" / "default"
            default_dir.mkdir(parents=True)
            (default_dir / "locale").symlink_to("../locale.conf")
            (root / "etc" / "locale.conf").mkdir()

            with self.assertRaisesRegex(ValueError, "path is not a regular file"):
                live_hooks.stage_debian_live_locale(root)

    def test_locale_policy_rejects_directory_syntax_for_missing_target(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            default_dir = root / "etc" / "default"
            default_dir.mkdir(parents=True)
            (default_dir / "locale").symlink_to("../locale.conf/")

            with self.assertRaisesRegex(ValueError, "target does not name a regular file"):
                live_hooks.stage_debian_live_locale(root)

            self.assertFalse((root / "etc" / "locale.conf").exists())

    def test_locale_policy_rejects_missing_target_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            default_dir = root / "etc" / "default"
            default_dir.mkdir(parents=True)
            (default_dir / "locale").symlink_to("../missing/locale.conf")

            with self.assertRaisesRegex(ValueError, "target directory does not exist"):
                live_hooks.stage_debian_live_locale(root)

    def test_locale_policy_rejects_symlinked_target_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            default_dir = root / "etc" / "default"
            default_dir.mkdir(parents=True)
            target_dir = root / "var" / "lib" / "locale-data"
            target_dir.mkdir(parents=True)
            (root / "etc" / "locale-data").symlink_to("../var/lib/locale-data")
            (default_dir / "locale").symlink_to("../locale-data/locale.conf")

            with self.assertRaisesRegex(ValueError, "directory must not be a symlink"):
                live_hooks.stage_debian_live_locale(root)


if __name__ == "__main__":
    unittest.main()
