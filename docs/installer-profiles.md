# Debian and Kali installer profiles

This change applies to the managed **netinst** and **netboot** source roles for
Debian and Kali Linux. Live preparation and persistence are documented separately in
`REFRACTOR-2026-09-08.md`. Kali Purple remains the
existing separate installer-only profile.

## Menu order

The custom GRUB hierarchy is:

```text
Debian ...
  Debian Netinst ...
    DEBIAN DESKTOP
      Debian Netinst Install (HTTPS WEB) ...
      Debian Netinst Install (HTTP LAN) ...
      Debian Netinst Install (INITRD PRESEED) ...
      Debian Netinst Install (USB HD-MEDIA) ...
    DEBIAN SERVER
      [the same four transports, using Server settings]
  Debian Netboot ...
    DEBIAN DESKTOP
      [the same four transports, using Netboot assets]
    DEBIAN SERVER
      [the same four transports, using Netboot assets]
```

Kali follows the same structure with `KALI DESKTOP`, `KALI SERVER`, and Kali
transport titles. A transport submenu contains the corresponding flavor's
configured preset leaves. Existing manual/legacy helpers remain outside the
Desktop/Server transport hierarchy.

The `installer-profile-menu` nodes in `configs/spec/grub/debian.json` and
`kali-linux.json` select the flavor. Their transport children use the matching
`debian-de`, `debian-srv`, `kali-de`, or `kali-srv` preset set.

## Independent configuration

Edit `configs/debian-usb.conf` before installation, or the active installed
configuration used by the application. These are examples of real key names:

```sh
PRESEED_ONE_ARGS_DEBIAN_DE="classes=desktop"
PRESEED_ONE_ARGS_DEBIAN_SRV="classes=server"
PRESEED_ONE_ARGS_KALI_DE="classes=desktop"
PRESEED_ONE_ARGS_KALI_SRV="classes=server"

DEBIAN_DE_PRESEED_PUBLIC_URL="https://preseed.example/desktop/preseed.cfg"
DEBIAN_SRV_PRESEED_PUBLIC_URL="https://preseed.example/server/preseed.cfg"
DEBIAN_DE_PRESEED_INTERNAL_URL="http://192.0.2.10/desktop/preseed.cfg"
DEBIAN_SRV_PRESEED_INTERNAL_URL="http://192.0.2.10/server/preseed.cfg"

KALI_LINUX_DE_PRESEED_PUBLIC_URL="https://preseed.example/kali/desktop/preseed.cfg"
KALI_LINUX_SRV_PRESEED_PUBLIC_URL="https://preseed.example/kali/server/preseed.cfg"
KALI_LINUX_DE_PRESEED_INTERNAL_URL="http://192.0.2.10/kali/desktop/preseed.cfg"
KALI_LINUX_SRV_PRESEED_INTERNAL_URL="http://192.0.2.10/kali/server/preseed.cfg"


PRESEED_USB_DEBIAN_DE_FILE="/hd-media/debian-preseed-de/preseed.cfg"
PRESEED_USB_DEBIAN_SRV_FILE="/hd-media/debian-preseed-srv/preseed.cfg"
PRESEED_USB_KALI_DE_FILE="/hd-media/kali-preseed-de/preseed.cfg"
PRESEED_USB_KALI_SRV_FILE="/hd-media/kali-preseed-srv/preseed.cfg"
```

The example endpoints above are placeholders, not servers provided by this
project. Each URL also has its own `_ARGS` key, for example
`DEBIAN_SRV_PRESEED_PUBLIC_ARGS`. PUBLIC must be HTTPS and INTERNAL must be HTTP
when rendered as these transports. Empty URLs produce an explanatory disabled
entry instead of silently falling back to another flavor's endpoint.

Nine preset argument slots per family/flavor are accepted. The shipped Desktop
menus reference ONE through FIVE; Server menus reference ONE through FOUR.
Spare slots can be added to the JSON preset sets. Existing site-specific Desktop
values and the former server preset values are preserved. Server entries from
the old mixed SIX..NINE slots now occupy Server ONE..FOUR. New Server endpoints
and Kali public endpoints are deliberately empty; configure your own.

`PRESEED_COMMON_KERNEL_ARGS` remains shared. Its existing network settings are
site-specific and must be reviewed. Source-device selection for Netinst is
replaced by the selected USB data UUID; it is not a hardcoded `/dev/sda2`.
Target-installation disk policy remains the responsibility of the preseed.

## Transport behavior

| Selection | Kernel locator | Content used |
| --- | --- | --- |
| HTTPS WEB | `url=https://...` | This flavor's PUBLIC URL and PUBLIC_ARGS |
| HTTP LAN | `url=http://...` | This flavor's INTERNAL URL and INTERNAL_ARGS |
| INITRD PRESEED | No `url=` or `file=` | Root `/preseed.cfg` in this flavor's initrd |
| USB HD-MEDIA | `file=/hd-media/<flavor-folder>/preseed.cfg` | This flavor's codebase on USB partition 2 |

Stale URL/file aliases from upstream entries, shared extras, and preset argument
blocks are removed before the selected transport is applied. The generated
initrd dispatcher reads `DUSB_PRESEED_MODE` and suppresses its embedded preseed
for external transports. This prevents an embedded Desktop/Server preseed from
running before the user-selected network or USB preseed.

For Netinst, the existing hd-media/file-preseed path loads the USB file after
mounting the selected ISO source. For Netboot, a generated startup helper mounts
the data filesystem by `DUSB_HD_MEDIA_UUID`, read-only at `/hd-media`, and invokes
the initrd's native preseed loader with the selected file. It refuses a different
already-mounted device and reports missing media or seed files.

**Netboot hardware requirement:** USB storage and ext4 support for the exact
installer kernel ABI must be present in the chosen netboot initrd (or supplied
through its profile overlay/module preparation). The helper attempts to load
USB host, storage, SCSI, and ext4 modules; it does not download missing drivers.
This route needs boot testing with your actual installer image and hardware.
A bare `file=` argument alone is not sufficient for a stock netboot installer.

Upstream references:
- https://www.debian.org/releases/stable/amd64/apbs02.en.html
- https://d-i.debian.org/doc/installation-guide/en.amd64/apbs01.html

## Initrd overlays and one downloaded source pair

```text
initrd/
  debian/
    live/                         # independent Live environment and overlay
    netinst/
      desktop/                    # former netinst contents moved here
      server/
    netboot/
      desktop/
      server/
  kali/
    live/                         # independent Live environment and overlay
    netinst/
      desktop/
      server/
    netboot/
      desktop/
      server/
```

Put each profile's `preseed.cfg` and codebase inside its flavor directory.
A `preseed.cfg` directly inside `desktop/` becomes `/preseed.cfg` in the Desktop
initrd, and similarly for Server. No Desktop files are merged into Server.
Selecting the overlay includes both independent trees; declining it builds both
profiles without repository payloads. Empty profile directories are valid but
do not themselves provide an unattended installation. The supplied repository
contains no new server installation recipe; provide your own codebase.

Downloads remain one kernel/initrd pair per distro, release channel, and source
role, not one per flavor. A managed source retains the common pair under
`hd-media/` or `netboot/` and derives these isolated copies:

```text
<prepared-source>/.debian-usb/installer-profiles/
  manifest.json
  desktop/{vmlinuz,initrd.gz}
  server/{vmlinuz,initrd.gz}
```

The common source must not already contain a root `preseed.cfg`: preparation
refuses that ambiguity rather than leaking a built-in Desktop preseed into
Server. Profile generation uses a temporary pair and publishes it only after
both profiles have been packed successfully. Original downloaded files and the
opaque Netinst ISO are not changed.

On USB partition 2, custom entries reference separate boot directories:

```text
/debian-netinst-de/{vmlinuz,initrd.gz}
/debian-netinst-srv/{vmlinuz,initrd.gz}
/debian-netboot-de/{vmlinuz,initrd.gz}
/debian-netboot-srv/{vmlinuz,initrd.gz}
/kali-netinst-de/{vmlinuz,initrd.gz}
/kali-netinst-srv/{vmlinuz,initrd.gz}
/kali-netboot-de/{vmlinuz,initrd.gz}
/kali-netboot-srv/{vmlinuz,initrd.gz}
```

Only selected distro/role directories are written. The opaque Netinst ISO and
legacy manual boot assets remain in their existing role directory. Secure Boot
also stages isolated copies in its existing ESP asset namespace.

## Optional HD-MEDIA copying

Creation/planning asks separately for Desktop and Server:

```text
Add HD-MEDIA preseed.cfg and its entire codebase for DEBIAN DESKTOP [y/N]
Path to preseed.cfg (ALL content in its parent directory will be copied)
```

Yes plus `/home/operator/desktop-code/preseed.cfg` copies everything under
`/home/operator/desktop-code/`, not just the seed, into the selected flavor's
preseed directory. Dotfiles, executable modes, and symlinks are preserved.
Special files, root-directory sources, overlapping source/destination trees,
and symlinked destination directories are rejected. Selecting an existing
profile destination replaces that whole codebase rather than merging stale
files. Failed copies preserve the previous profile directory.

No means no host-tree staging. Debian/Kali HOST path variables are retired: they
are neither configured nor required, and legacy values are discarded on migration.
The host path is requested only after Yes; there is no configuration-derived default. The menu is still emitted. Later, mount **partition 2** and place the
whole codebase directly under `debian-preseed-de/`, `debian-preseed-srv/`,
`kali-preseed-de/`, or `kali-preseed-srv/`. Do not add a leading `hd-media/`
directory on the filesystem. For example, the first path is physically
`<mountpoint>/debian-preseed-de/preseed.cfg`.

Netinst and Netboot for one distro share these preseed folders. Multi-OS prompts
once per distro/flavor; conflicting paths in edited plans are rejected. Saved
plans record consent in `hd_media_preseed_dirs`, for example:

```json
{"desktop": "/home/operator/desktop-code", "server": "/home/operator/server-code"}
```

An absent/empty map never authorizes copying. Updates preserve unselected preseed
folders, so manually added future codebases are not removed.

## Existing sources, plans, and USBs

Legacy config names are accepted as aliases: old USB paths and endpoints
map to Desktop; retired host paths are dropped, and old server preset slots map to the Server set. Explicit new
keys take precedence. Saving writes the current names. Legacy default USB seed
paths supplied through old keys migrate to the new Desktop folder.

Reuse of an old clean prepared installer source can generate the missing profile
assets without downloading another pair. To explicitly rebuild profile assets
after changing overlays, use the preparation flow again, or run:

```sh
scripts/debian-usb-python prepare-installer-profiles \
  --profile debian --source-role netinst \
  --source-path /path/to/prepared-source \
  --overlay-root "$PWD/initrd/debian/netinst"
```

Use `kali-linux` and `initrd/kali/...` for Kali. This modifies the prepared source,
not a USB. The application can then update the USB from that source. An old
explicit `initrd_preseed_path` applies only to Desktop; prefer profile overlays.
Old saved `offline_preseed_dir` plans must be edited and resaved using the new
explicit per-profile selections. Simply reusing a valid prepared source does
not silently rebuild it from later overlay edits.

New single-OS installer sticks use GPT partition 1 for the ESP, partition 2 for
ext4 data, and partition 3 for the small BIOS boot area located at the front.
Multi-OS keeps its existing partition-1 ESP / partition-2 data arrangement.
Older single-OS installer sticks with data on partition 3 must be recreated;
the updater refuses to silently repartition them. Live layouts are unchanged.

## Git hooks and secrets

`make install` installs `.githooks/pre-commit` and `.githooks/pre-push` into the
hooks directory resolved by Git. `make nuke` removes only managed installed
copies. Both actions preserve unrelated hooks; installation refuses conflicting
user-owned hook files rather than overwriting them. Tracked sources remain.
`make install-git-hooks` and `sh scripts/install-git-hooks.sh --remove` provide
the standalone actions. Run normal install/nuke as a non-root user as before.

The original Debian Netinst `preseed.env` is now in `desktop/`. Secret-management
recognizes present `preseed.env` files in all new Debian/Kali profile directories;
its interactive installer values apply to those present active files. Edit
profiles individually when different credentials are required. Live credential
naming remains separate; the manager includes present Debian and Kali Live files. Hooks already scan recursively.
Treat embedded/USB codebases as sensitive: filesystem permissions are not
at-rest encryption. Review unattended partitioning commands before booting a
real machine; neither a menu label nor a passing unit test validates your recipe.

## Archive/device-node handling

Each flavor is built by copying the role's regular `initrd.gz` archive, extracting
that copy with cpio, applying only that flavor's overlay and transport dispatcher,
and repacking the same copy. An extracted tree is never cloned using Python
`shutil.copytree()`: that API cannot preserve character/block devices and FIFOs,
and does not preserve the hard links needed for a faithful tree clone.

Repacking the existing archive retains its detected compression and original
leading early-cpio bytes, even when its filename ends in `.gz` but its payload
uses a different supported compression. Final archive ownership is normalized to
root by the existing cpio writer; original archive/file permissions are preserved.
The immutable input and any previous complete profile pair survive a failed
profile-only preparation. The encompassing full-source rebuild should still use
a new managed bundle rather than overwrite a known-good bundle in place.

`make check-installer-archives` exercises device metadata through fakeroot and
real cpio, FIFOs as an unprivileged user, hard links and symlinks, compressed and
uncompressed archives with early segments, read-only source archives, and failed
second-flavor extraction/repacking. See the repair record for validation limits.
