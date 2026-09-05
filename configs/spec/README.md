# Build ISO Spec Profiles

This tree carries repo-managed module, `.deb`, and `.udeb` spec profiles for the `Build ISO` workflow.

Layout:

- `d-i/` contains Debian Installer-oriented profiles.
- `live/` contains live-environment-oriented profiles.
- `modules/`, `deb/`, and `udeb/` group the primary customization type.
- distro-specific profiles live under `configs/spec/<stage>/<distro>/<kind>/`.

Conventions:

- Profile JSON paths may be selected directly by the Go `Build ISO` flow.
- Relative paths inside a profile are resolved relative to the profile file itself.
- Debian `linux-installer-kernel` rebuild profiles may point at a `udeb_rebuild_spec_path` and a `source_overlay_dir`.
- Bundled `live-build.conf`/`.json` and `netinst-build.conf`/`.json` profiles now ship for Debian and Kali Linux under their stage-specific directories; Ubuntu remains schema-compatible for future rollout work.

## Live administration tool catalog

`configs/spec/live/admin-tools.json` is the authoritative package catalog for optional tools installed into supported Live root filesystems. Schema version `2` has this contract:

| Field | Contract |
| --- | --- |
| `schema_version` | Must be `2`. |
| `supported_profiles` | Live remaster profiles allowed to consume the catalog. |
| `build_distros` | Build-ISO distro names mapped to supported Live profiles. |
| `package_groups` | Ordered objects containing a stable `id`, user-facing `title`, `description`, and non-empty `packages` list. |
| `command_packages` | Auditable command-to-package mappings; every mapped package must occur in at least one group. |
| `notes` | User/operator constraints that do not alter resolution behavior. |

Selection preserves catalog order and deduplicates packages across groups. A missing selection means all groups for legacy/default behavior; an explicit empty list means no optional packages; unknown group IDs fail validation. The selector and these fields apply only to Live or Hybrid media. Netinst and Netboot plans normalize to an empty `live_tool_groups` list and must not receive Live packages or hooks.

The catalog's `firmware_programming` group includes `flashrom` for CH341A-compatible SPI programmers plus `avrdude`, `dfu-util`, `openocd`, and `i2c-tools`. Package inclusion does not relax hardware permissions or authorize a firmware write.

## GRUB spec contract

- `configs/spec/grub/*.json` are the source of truth for custom GRUB menu structure.
- Adding or removing `preseed` preset entries in `configs/spec/grub/debian.json` or `configs/spec/grub/kali-linux.json` changes the rendered managed and Multi-OS GRUB menus directly.
- Any `args_key`, `common_args_key`, or `preseed_common_args_key` referenced by those GRUB specs must exist in the active `debian-usb.conf`. Preset argument blocks retain their existing required/optional policy; the shipped `DEBIAN_PRESEED_INTERNAL_ARGS` and `DEBIAN_PRESEED_PUBLIC_ARGS` network overlays are explicitly allowed to be empty.
- `label` inside `preseed.preset_sets.*[]` is appended verbatim to the matching `title_prefix`, so you can rename each generated preset leaf without Python changes.
- `title` fields rename menu/submenu/entry labels directly for the supported entry types: `live`, `installer`, and `preseed-preset-menu`.
- `network_menus[].title` renames a variant submenu, `network_menus[].args_key` selects the config-backed GRUB overlay for that variant, and `legacy_entries[].title` renames the preserved fallback installer leaves.
- `preserved_title` renames the family-level preserved submenu, and a profile-level `preserved` block can customize saved-entry titles via `title`, `entry_title_template`, `entry_title_overrides`, `include_kinds`, and `flatten_menu_path`.
- See `configs/spec/grub/debian.json.example` for a valid end-to-end customization example, including an added `PRESEED_ELEVEN_ARGS_DEBIAN` preset.
