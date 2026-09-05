# debian-usb
DEBCONF_DEBUG=5 for debugging.

## Debian Netinst Testing ISO
https://cdimage.debian.org/cdimage/daily-builds/daily/20260704-7/amd64/iso-cd/

## Debian Netinst Initrd and Vmlinuz
https://d-i.debian.org/daily-images/amd64/20260704-04:51/hd-media/initrd.gz
https://d-i.debian.org/daily-images/amd64/20260704-04:51/hd-media/vmlinuz

`debian-usb` is a local-ISO USB creator for Debian-family images, and it now also includes a Debian-first custom ISO builder.

The app uses:

- Go for the interactive CLI, managed runtime config, kernel-arg planning, and menu flow
- Python for ISO inspection, GRUB/syslinux parsing, managed GRUB rendering, and Debian ISO build orchestration
- POSIX `sh` (Debian `dash`) for installation, removal, disk writing, managed USB assembly, and privileged Debian ISO build dependency/bootstrap entrypoints

## Current workflow

This build prefers local media, but supported profiles can also download configured managed sources on demand.

You pick one of these profiles:

- Debian
- Ubuntu Desktop
- Ubuntu Server
- Kali Linux
- Kali Purple

Then the app:

1. shows any ISO files it already detects in common locations
2. lets you either enter the absolute path to a local ISO or download a configured managed source when that profile exposes one
3. inspects the ISO before any write is offered and reports:
   - whether the image is live, installer-only, or hybrid
   - whether UEFI boot markers are present
   - whether encrypted persistence is supported for the selected live profile
   - which top-level boot menu groups were detected
4. lets you choose either:
   - write the ISO as-is directly to the whole USB disk device
   - build a managed GRUB USB layout when the selected profile and media support it
5. for managed custom-menu builds, renders Live entries only for explicit Live sources and renders installer, HTTP preseed, and USB preseed entries only for explicit installer source roles
6. preserves the upstream GRUB menu variants alongside the curated entries
7. keeps live and installer kernel defaults controlled by Settings instead of prompting for per-run manual kernel overrides
8. blocks unreadable or structurally unsupported ISO files before the device write starts
9. prompts for the Secure Boot trust mode during managed USB planning: either reuse the persistent USB-side `MOK.der` enrollment flow or target firmware `db` import, then stages matching signed boot assets, Microsoft-compatible combined db material, and enrollment tooling for that choice
10. saves or reuses the planned execution under `/data/cfg/debian-usb/planned-executions/` as soon as the review screen is shown, so it remains available even if the write is cancelled before execution

The Create USB menu also includes a Multi-OS managed workflow for Debian-based media. The deterministic layout uses a removable-path FAT32 ESP on partition 1, one shared ext4 ISO store on partition 2, and one dedicated partition starting at partition 3 for every Live source that has persistence enabled. Live, Netinst, and Netboot are independent source roles. Ubuntu Desktop and Ubuntu Server are deliberately rejected by both Multi-OS planners and remain available only through their separate single-OS workflow.

For Debian downloads, the release-channel selector maps `Stable (Trixie)` to the configured `DEBIAN_*_STABLE_URL` values and `Testing (Forky)` to the configured `DEBIAN_*_TESTING_URL` values in `configs/debian-usb.conf`. A Netinst download uses one channel consistently for its opaque ISO, separate hd-media `vmlinuz`, and separate hd-media `initrd.gz`; assets from Stable and Testing are never mixed in one prepared bundle.

Each managed download key has its own sticky cache slot under `/data/downloads/debian-usb/iso/` or `/data/downloads/debian-usb/boot/`. If that exact key's slot already contains a non-empty payload, the most recently modified valid payload in the slot is reused before any checksum-manifest lookup, HTTP request, or torrent request. This applies to versioned files and moving aliases such as `current`, `current-live`, weekly, daily, and `arch-latest`; an upstream alias change does not trigger another download. Stable and Testing keys, source roles, distributions, ISOs, kernels, and initrds remain isolated because they use different slots. To request a fresh artifact explicitly, remove only the relevant lower-case key directory (with the trailing `_url` omitted) and run the download again.

- A Live source is one opaque ISO. It is copied to a profile-specific `*-live` directory and booted with `set isofile`, `loopback loop $isofile`, `(loop)/live/...`, and `findiso=$isofile`. Installer entries embedded in hybrid Live media are ignored. When RAM mode is enabled for Debian live-boot media, GRUB emits `toram=<detected-rootfs-module>` (for example, `toram=filesystem.squashfs` or `toram=filesystem.erofs`) instead of bare `toram`; live-boot therefore copies only that Live root filesystem module rather than the entire shared Multi-OS partition and its sibling ISOs. The writer does not separately stage a Live kernel, Live initrd, root filesystem, `.disk`, or package metadata.
- A Netinst source is a prepared directory containing separate downloaded `hd-media/vmlinuz`, `hd-media/initrd.gz`, and exactly one opaque `payload/*.iso`. The kernel and initrd are copied from `hd-media`; they are never extracted from the ISO. GRUB sets separate `kernel`, `initrd`, and `isofile` variables, boots the hd-media pair directly, and passes `iso-scan/filename=$isofile` for the opaque Netinst ISO. Every bare or valued `toram` argument is removed from installer entries.
- A Netboot source contains separate `netboot/vmlinuz` and `netboot/initrd.gz` and carries no ISO payload. Netboot entries also reject/remove all `toram` forms.

Stock Debian `iso-scan` records `iso-scan/filename` only after scanning and choosing an image; it does not use the preseeded value to restrict the scan. On a shared filesystem that allowed a sibling Live ISO to be selected and made d-i load `live-installer`. Netinst preparation therefore patches only the copied hd-media `initrd.gz`: at `iso-scan` state 19, a non-empty `iso-scan/filename` must resolve to that exact file on the selected device or the installer aborts without calling the sibling-ISO scan. The original downloaded initrd and the Netinst ISO remain unchanged.

The patch is selected from the state-19 first-pass scan structure rather than a Debian release name or package-version string. It has been checked against the official `iso-scan` 1.98 script used by Trixie and the 1.100 script available for Forky; those scripts currently have the same relevant control flow. Kernel and initrd compatibility remains ABI-driven from the separately downloaded hd-media files. If a later `iso-scan` changes that control flow, preparation fails closed instead of producing a bundle that could scan a sibling ISO.

The shared ISO-store root is also scrubbed and validated so `/.disk`, `/live`, `/casper`, `/install`, `/install.amd`, `/d-i`, and `/debian-installer` cannot masquerade as metadata for any one payload. Those paths may exist only inside their opaque profile-specific ISO. Recovery or administration packages must be included by selecting an ISO produced by the separate `Build Custom ISO` workflow or by explicitly running the remaster helper before USB creation.

## Build Custom ISO

`Build Custom ISO` is a separate top-level workflow from `Create USB`.

The current rollout focuses on Debian only:

- `Debian` is implemented.
- `Ubuntu Desktop` is a visible placeholder.
- `Kali Linux` is a visible placeholder.

The Debian build path:

1. checks and installs missing host-side build dependencies only when you select the Debian Build Custom ISO path
2. collects a build recipe for suite, architecture, archive areas, installer mode, package lists, repo-managed `configs/spec/**` profiles, local `.deb` directories, prebuilt `.udeb` directories, `config/includes.{chroot,binary,installer}` overlays, optional `config/bootloaders` overrides, custom kernel mode, and root filesystem mode
3. generates a live-build workspace under `/data/tmp/debian-usb/build-iso/<run-id>/live-build`
4. writes manifests under `/var/lib/debian-usb/build-iso/<run-id>/`
5. writes logs under `/var/log/debian-usb/build-iso/<run-id>.log`
6. copies the finished ISO into `/data/downloads/debian-usb/iso/debian-live/` by default so the Create USB flow can discover it automatically
7. keeps live-build APT indexes enabled, stages the Live administration profile into both the squashfs and the medium package archive, and refuses to publish an image unless its suite release plus an architecture package index for every populated `pool/` component are present
8. scans the completed Debian Live kernel tree, including `*.ko*` files and `modules.builtin`, and refuses to replace the final output ISO unless the mandatory Live boot, compression, overlay, persistence, storage, and CH341 module contract is satisfied

The bundled Debian Live and Debian Netinst profiles both prompt for the suite, so Trixie 13 and Forky 14 are selectable without switching to the advanced planner. The role contracts remain different: Live uses `--system live --debian-installer live`, while Netinst uses `--debian-installer netinst` and deliberately includes no `--system` option. `--binary-images iso-hybrid` selects only the output image format; it does not select the installer role, and this workflow has no `--debian-netinst` option. For both Debian profiles, the Debian Installer distribution follows the selected suite, so `trixie` and `forky` consistently drive both `--distribution` and `--debian-installer-distribution`. A Netinst plan must use `rootfs_format: "none"` and cannot contain Live base packages, Live specs, Live overlays, Live initramfs modules, Live filesystem-module entries, storage-tool packages for a Live chroot, a custom Live kernel, or `debian-installer-launcher`. The post-build validator rejects any Netinst result containing `/live`, `/casper`, Live boot entries, or a `live-installer` udeb instead of publishing it as Netinst media.

EROFS support is implemented as a custom binary-stage replacement. The build keeps the upstream `live/filesystem.squashfs` filename for compatibility with current `live-boot` discovery, but the actual filesystem payload can be EROFS and is recorded in the build manifest.

The Debian Build Custom ISO plan also now covers the live-build integration points that matter for custom Debian media work:

- optional Debian Installer overrides through `--debian-installer-distribution` and `--bootappend-install`
- `config/includes.chroot`-style live overlays for static filesystem content
- `config/includes.binary` overlays for medium-only assets such as release notes or helper payloads
- `config/includes.installer` overlays for installer-side content
- optional `config/bootloaders` overrides when the stock menus must be replaced
- explicit `live/filesystem.module` ordering, with EROFS defaulting to `filesystem.squashfs`
- repo-managed live and installer spec profiles under `configs/spec/`, including Debian Live defaults for EROFS, xxhash/LZ4 compression, overlay persistence, USB storage, NVMe, device mapper/crypt, and CH341 serial support
- a spec-driven UDEB rebuild path that can fetch Debian source with `apt source`, patch `debian/control`, build `.udeb` artifacts, and stage them into the live-build tree
- an installer-side EROFS audit that tracks the d-i integration surface you intend to touch, including `build-config`, `kernel-wedge`, `iso-scan`, `partman-auto`, `partconf`, `os-prober`, and `rescue`

The Build Custom ISO prompt flow also now supports pre-build kernel evidence checks for the exact module set you intend to use. You can point it at a specific kernel version, inspect current `CONFIG_*` symbols plus module-file presence from `/lib/modules/<version>/...`, and optionally let it download and extract matching kernel packages into the managed cache when that version is not already present on the host.

When you choose EROFS together with a Debian Installer mode, the builder now has an explicit installer-side EROFS policy:

- `Warn` keeps the build moving but records missing `.udeb` or `config/includes.installer` evidence in an installer audit.
- `Require` blocks the build unless you stage both installer-side `.udeb` content and `config/includes.installer` content, then writes an installer audit JSON artifact next to the build manifest.

That keeps the flow aligned with Debian’s split between the live boot path and the separate installer kernel/initrd path, instead of treating EROFS as a live-kernel-only customization.

For broader Debian Installer customization, the Debian Build Custom ISO flow now also accepts an optional UDEB rebuild specification JSON file. When present, the builder:

- runs `apt source <source_package>` in a dedicated workspace
- attempts `apt-get build-dep` and can fall back to explicit `build_dep_packages` from the spec
- extends `debian/control` with a `Package-Type: udeb` stanza instead of repacking finished `.deb` files by hand
- copies basic packaging manifests such as `.install`, `.dirs`, and `.links` to the new udeb package name when needed
- adds `override_dh_makeshlibs --add-udeb=<package>` automatically for simple library-udeb cases, while requiring a source overlay for more complex existing `override_dh_makeshlibs` rules
- stages the resulting `.udeb` artifacts into `config/packages.binary/` for the live-build path
- exports a reusable installer workspace under the build state directory with `localudebs/`, `pkg-lists/local`, and `sources.list.udeb.local`

A minimal rebuild spec looks like this:

```json
{
  "schema_version": 1,
  "rebuilds": [
    {
      "source_package": "foo",
      "binary_package": "foo",
      "udeb_package": "foo-udeb",
      "package_role": "generic"
    },
    {
      "source_package": "libbar",
      "binary_package": "libbar1",
      "udeb_package": "libbar1-udeb",
      "package_role": "library",
      "library_shlibs_udeb": "libbar1-udeb"
    }
  ]
}
```

For packages that need more than the generated control/rules changes, provide a `source_overlay_dir` in the rebuild spec. That overlay is copied into the unpacked source tree before the builder writes the final udeb stanza, which gives you a controlled way to add custom `debian/rules`, `*.install`, maintainer scripts, or other d-i-specific packaging files.

The same spec file can also carry a Debian `linux` source rebuild entry for installer-kernel UDEBs. That path is for `kernel-wedge`-driven installer module packaging, not the generic `Package-Type: udeb` conversion flow:

```json
{
  "schema_version": 1,
  "rebuilds": [
    {
      "rebuild_kind": "linux-installer-kernel",
      "source_package": "linux",
      "module_targets": [
        {
          "path": "debian/installer/modules/kernel-image",
          "modules": ["erofs", "xxhash", "xxhash_generic"],
          "merge_strategy": "append-unique"
        },
        {
          "path": "debian/installer/modules/amd64/kernel-image",
          "modules": ["erofs", "xxhash", "xxhash_generic"],
          "merge_strategy": "append-unique"
        }
      ],
      "package_list_append_text": "Package: erofs-modules\nKernel-Version: yes",
      "pkg_list_local_entries": ["erofs-modules"]
    }
  ]
}
```

That kernel-specific rebuild path fetches the Debian `linux` source, applies any optional overlay, updates the selected `debian/installer/modules/*` files, optionally appends raw text to `debian/installer/package-list`, runs `dpkg-buildpackage`, stages every resulting kernel `.udeb`, and exports them through the same installer `localudebs`/repo workspace.

Repository-side install defaults now live under `configs/`:

- `configs/install.env` controls install-time paths, package dependencies, and helper locations
- `configs/debian-usb.conf` controls installed runtime defaults such as persistence size, per-profile installer URLs, boot policy, toram, Debian Live hook settings, shared live policy arguments, installer policy arguments, per-profile fallback live kernel lines, and per-profile live/install/forensics kernel extras
- `initrd/debian/live/live.env` is the sole source for Debian Live Wi-Fi interface, ESSID, security, addressing, resolver, and passphrase settings

Edit those files before `make install` if you want the installed app to start with your preferred defaults. `configs/debian-usb.conf` is the install-time source of truth for the runtime knobs the managed planner and renderer consume. The app can still change the installed runtime config later through its Settings flow, including per-profile live, forensics, and installer kernel extras.

Managed live and installer kernel behavior is controlled through Settings. The renderer merges the configured live, installer, forensics, and per-profile extras into curated and preserved entries during the managed render.

Debian Live payloads receive repo-managed live-config hooks under `/live/config-hooks`. These hooks and their required runtime packages are staged into custom Debian Live builds, existing Debian Live remasters, and raw Debian Live ISO payloads even when the user explicitly selects no optional administration tools. `live-config.hooks=medium` is mandatory on Debian Live boot entries so the medium hooks execute. `DEFAULT_LIVE_HOOKS` can gate only additional non-Wi-Fi tokens from `DEFAULT_LIVE_ARGS_HOOKS`; it cannot disable the managed APT/Wi-Fi hooks, and every canonical or legacy Wi-Fi kernel argument is stripped or rejected. Kali, Ubuntu, Tails, Debian Netinst, and Debian Netboot do not receive the Debian APT or Wi-Fi hooks, firmware bundle, or hook selector.

The Debian Live initramfs policy writes the explicit module list to `/usr/share/initramfs-tools/modules.d/debian-usb-live`, sets `MODULES=most` in `/usr/share/initramfs-tools/conf.d/debian-usb-live`, and mirrors the list to `/etc/modules-load.d/debian-usb-live.conf` for deterministic userspace loading. Existing-ISO remasters rebuild and replace every Live initrd referenced by a Live boot entry after staging this policy. The required module groups are:

| Purpose | Modules |
| --- | --- |
| Compression and hashing | `xxhash`, `xxhash_generic`, `lz4`, `lz4_compress`, `lz4_decompress` |
| Live root and full-root overlay | `loop`, `squashfs`, `overlay`, `ext4` |
| Encrypted persistence | `dm_mod`, `dm_crypt` |
| Removable and high-performance storage | `usb_storage`, `uas`, `nvme` |
| CH341 serial devices | `usbserial`, `ch341` |

Mandatory Debian Live packages include `initramfs-tools`, `kmod`, the `xxhash`, `lz4`, and `zstd` CLIs, USB/PCI/I2C inspection tools, `flashrom`, Intel microcode, Intel Wi-Fi/graphics/misc/sound/SOF firmware, and the existing broad Atheros, Realtek, Broadcom, MediaTek, Libertas, Linux, and Bluetooth firmware coverage. CH341 serial mode is handled by the kernel's `usbserial` and `ch341` modules. CH341A SPI programming is a userspace flashrom backend (`flashrom -p ch341a_spi`); this project does not invent or depend on a separate CH341A firmware package.

At Debian Live boot, `0500-apt-live-medium.sh` first confirms `/etc/os-release` identifies Debian and validates the selected suite. It atomically filters legacy `.list` files and Deb822 `.sources` stanzas, removing only enabled Live-medium/CD-ROM URIs such as `file:/run/live/medium`, `file:/lib/live/mount/medium`, `file:/cdrom`, and `cdrom:`. HTTP/HTTPS repositories, disabled entries, source-only repositories, comments, file modes, and persistent custom repository changes are preserved. The hook creates one deterministic fallback Deb822 source only when no enabled binary HTTP/HTTPS source remains:

| Field | Debian Live value |
| --- | --- |
| URI | `https://deb.debian.org/debian` |
| Suite | Exact validated codename from `live_apt_suite=` or `VERSION_CODENAME`, such as `trixie` or `forky` |
| Components | `main contrib non-free non-free-firmware` |
| Keyring | `/usr/share/keyrings/debian-archive-keyring.gpg` |

No `-updates` or `-security` suite is synthesized. A non-Debian root or an unsafe/missing suite is left unchanged rather than being assigned a guessed repository. This removes the broken source that made APT probe missing `Packages.xz`, `Packages.bz2`, `Packages.lzma`, `Packages.gz`, `Packages.lz4`, `Packages.zst`, and uncompressed `Packages` files under `/run/live/medium`; installing more decompressor packages would not repair absent repository metadata.

Debian Live Wi-Fi configuration comes only from `initrd/debian/live/live.env`:

| Key | Debian Live behavior |
| --- | --- |
| `LIVE_WIFI_INTERFACE` | Use the named Linux interface, or `auto` to select the first detected wireless interface. |
| `LIVE_WIFI_ESSID` | Network name. An empty value disables automatic Wi-Fi setup. |
| `LIVE_WIFI_SECURITY` | `open`, `wpa` for WPA2-PSK/RSN, or `sae` for WPA3-SAE with required management-frame protection. |
| `LIVE_WIFI_CIDR` | Optional static IPv4 address/prefix. An empty value requests IPv4 through DHCP. |
| `LIVE_WIFI_GATEWAY` | Optional static IPv4 default gateway; the Wi-Fi route is normalized to metric `600`. |
| `LIVE_WIFI_NAMESERVERS` | Optional comma- or whitespace-separated IPv4 resolver list. |
| `LIVE_WIFI_PASSPHRASE` | WPA2 accepts 8-63 UTF-8 bytes or a 64-digit hexadecimal PSK; SAE accepts 1-63 UTF-8 bytes; open networks ignore it. |

No Wi-Fi value is rendered into GRUB or `/proc/cmdline`. The build-side parser uses an exact key allowlist and never sources or evaluates the file. It rejects symlinks, non-regular or oversized files, unsupported or duplicate keys, malformed values, invalid interface/security/address data, and invalid WPA/SAE lengths. A source containing a passphrase must have mode `0600`. The direct raw-ISO writer runs the same validator before invoking `xorriso`; malformed or insecure input therefore fails before the ISO rebuild and before any USB-device mutation.

Custom Debian Live builds and Debian Live persistence, Live Host, and administration-tool remasters atomically stage the canonical file with mode `0600` at both locations required by the runtime:

```text
Live squashfs root: /etc/debian-usb/live.env
Live medium:        /live/debian-usb-live.env
```

The Debian Live Create and Multi-OS paths also apply `initrd/debian/live` automatically to every referenced Debian Live initrd; this overlay is required rather than prompted. Its POSIX `init-bottom` helper copies `/live.env` to `/run/initramfs/debian-usb/live.env` with mode `0600`. At boot, `1000-network-wifi.sh` checks the initramfs handoff first, then the squashfs copy and the standard Live-medium mount locations. It reads only the seven `LIVE_WIFI_*` assignments, does not use `source` or `eval`, never logs the passphrase, and skips safely when no ESSID or required credential is configured.

When configured, the hook unblocks Wi-Fi, waits for the requested interface (or detects one), verifies that the ESSID is visible, generates a private `wpa_supplicant` configuration, performs bounded association, applies the static IPv4 settings or runs DHCP, installs the requested gateway, and applies the configured nameservers. Ethernet and any other established link remain up; Wi-Fi receives default-route metric `600`, and per-link DNS is prevented from displacing a resolver owned by another default-route interface.

Mode `0600` prevents ordinary users in the running system or build tree from reading the file, but it does not encrypt the media. Any ISO or initrd containing `LIVE_WIFI_PASSPHRASE` must be treated as sensitive because a person with the image can extract it.

### Live recovery and administration tools

`configs/spec/live/admin-tools.json` is the single source of truth for the ordered Live tool groups and package payload. Single-OS Create, Multi-OS Create, and custom Live ISO build flows now begin with one compact choice: `s) Select Tools`, `n) None`, or `a) All` (plus Back and Exit). `Select Tools` opens the numbered group-toggle screen and starts empty for a new selection, while an existing selection is preserved when it is edited. `None` immediately records an explicit empty selection, and `All` immediately records every catalog group; a new flow no longer silently defaults to all tools. An explicit empty selection adds no optional tools, while Debian Live still receives the mandatory APT/Wi-Fi hook runtime packages.

For an existing supported Live ISO, the backend completes and validates the remaster before it invokes the USB writer. The writer receives only the remastered ISO path; a remaster error therefore stops before any device mutation. Multi-OS plans are cloned for execution, and the writer receives a prepared-source flag so it cannot remaster the same ISO a second time. Netinst, Netboot, installer-only media, and Tails bypass this path.

The catalog includes storage/filesystem, networking, firmware, diagnostics, recovery, and general administration groups, including these command providers:

| Commands | Package |
| --- | --- |
| `blkdiscard`, `lsblk`, `wipefs` | `util-linux` |
| `fdisk`, `sfdisk` | `fdisk` |
| `nvme` | `nvme-cli` |
| `mkfs.f2fs`, `fsck.f2fs` | `f2fs-tools` |
| `btrfs` | `btrfs-progs` |
| `mkfs.xfs`, `xfs_repair` | `xfsprogs` |
| `nmap` | `nmap` |
| `flashrom` (including `-p ch341a_spi`) | `flashrom` |
| `avrdude` | `avrdude` |
| `dfu-util` | `dfu-util` |
| `openocd` | `openocd` |
| `i2cdetect` | `i2c-tools` |

The `Firmware and SPI programmer tools` group adds CH341A-compatible SPI flash access through flashrom together with AVR, USB DFU, JTAG/SWD, and I2C tooling. Firmware reads and writes intentionally retain each tool's normal device-permission requirements; for example, use flashrom with explicit root/sudo authority rather than a broad permissive udev rule.

The same profile also covers common partitioning, LVM, MD RAID, LUKS, SMART, filesystem repair/recovery, packet capture, DNS, routing, SSH/rsync, firewall, hardware inventory, tracing, terminal, archive, and process-inspection tools. Custom live-build images place selected packages in both the chroot and binary package lists so the tools are present in the squashfs and the generated medium archive is non-empty. Existing-ISO remasters install into the squashfs with `--no-install-recommends`; downloaded package archives are cleaned before repacking. Generated and remastered Live roots remove the `fwupd-refresh.timer` enablement link and statically mask both `fwupd-refresh.timer` and `fwupd-refresh.service`; managed Live GRUB entries repeat those masks through `systemd.mask=` kernel arguments. Remaster package installation uses a temporary `policy-rc.d` refusal so maintainer scripts cannot start services through the bind-mounted chroot runtime, and the policy file is removed or restored before repacking. Debian Live tool roots also install `locales`, generate `en_US.UTF-8`, and select `LANG=en_US.UTF-8` with `LANGUAGE=en_US:en`; chroot package and maintenance commands use `C.UTF-8` until that locale is available. If upstream `/etc/default/locale` is a relative or absolute chroot-style symlink, the remaster preserves it and updates or creates its root-contained regular-file target; escaping, looping, directory, and symlinked-directory targets are rejected.

The remaster helper supports Debian Live, Kali Live, and the backend's Ubuntu Desktop/Casper Live profile for optional tool installation, but only Debian receives the APT/Wi-Fi policy described above. A remaster source must expose a squashfs Live root filesystem and must match the host architecture when the ISO architecture can be inferred. If all requested packages are not present in the ISO's own complete APT archive, working configured network repositories are required during remastering. Remastering needs enough temporary disk space to extract and repack the complete Live root filesystem, and it produces a new ISO whose upstream whole-image checksum/signature no longer matches the original even though bootloader assets are replayed and internal checksum files are regenerated.

Live administration-tool remasters keep their transient extraction tree under `/data/tmp/debian-usb/remaster-live-tools/<run-id>/` (or `DEBIAN_USB_WORK_DIR`), state under `/var/lib/debian-usb/remaster-live-tools/<run-id>/`, and logs under `/var/log/debian-usb/remaster-live-tools/`. Before extracting the ISO and again before expanding its squashfs, the helper verifies both free bytes and free inodes on every involved filesystem, aggregating requirements when work and output share one filesystem. The transient tree is removed after success or failure only after confirming that no mount remains beneath it; partial output ISOs are removed after a failed output write. Squashfs inspection, extraction, and repacking use half of the CPU cores available to the process through its CPU-affinity mask, with a minimum of one worker.

Create and Update USB execution authenticates `sudo` once before the privileged operation begins, refreshes that credential non-interactively every 30 seconds while long remasters run, and forces the remaster and subsequent writer phases to use `sudo -n`. A completed ISO build can therefore continue directly into the USB writer without another password prompt; if the credential cannot be refreshed, the next privileged phase fails immediately instead of waiting unattended for input. This does not install a passwordless sudoers rule. Real privilege remains necessary for chroot mounts and removable-device writes, which `fakeroot` cannot provide.

Managed mode rebuilds the USB layout and normalizes upstream boot entries for that managed boot path. Debian and Kali default to a raw ISO payload partition plus a separate removable-path FAT32 ESP. The payload stays an ISO9660 installer image, but its UEFI GRUB config is rebuilt with a managed redirect so firmware that starts the payload UEFI loader still chains into the custom ESP menu. Profiles that still use ISO-store keep a managed ext4 payload partition plus the removable-path ESP. Raw-ISO Debian/Kali payloads use the written partition identity for live and installer media binding so duplicate ISO filesystem UUIDs do not select the wrong payload; raw-ISO installer initrds that use `INSTALL_MEDIA_DEV` are patched so `cdrom-detect` mounts that explicit partition before its upstream already-mounted and auto-scan branches can select another ISO. ISO-store managed entries stage ISO files under `/boot/iso/<family>/<role>/...` and copied kernel/initrd members under `/boot/<family>/<role>/...`. The configured live boot policy and shared/profile extras are merged into preserved live entries, and verification entries are kept when the ISO exposes them.

Legacy single-OS plans can still carry encrypted persistence for Debian and Kali live media when the inspected live initrd and package set show the required cryptsetup support. In that compatibility path, the writer creates a LUKS container, formats the mapped device as ext4 with the `persistence` label, writes `persistence.conf`, and carries the matching live-boot kernel parameters into the managed menu.

For Debian media, the Create flow now asks for one explicit source role:

- `Live ISO` accepts one opaque Debian Live ISO and ignores installer entries present in hybrid Live media.
- `Netinst (hd-media)` accepts only a prepared source directory with separate downloaded `hd-media/vmlinuz`, `hd-media/initrd.gz`, and exactly one `payload/*.iso`; a bare Netinst ISO is rejected.
- `Netboot` accepts a prepared source directory with `netboot/vmlinuz` and `netboot/initrd.gz` and no ISO payload.

Managed Netinst/Netboot source preparation writes its generated bundle below the shared download root through the privileged helper path, then restores the completed bundle to the invoking `sudo` user's UID/GID. This fixes root-owned download-tree failures without changing installer assets, GRUB rendering, preseed behavior, or installation semantics.

### Stage-specific initrd overlays and secrets

Repository initrd content is separated by operating-system family and boot role:

```text
initrd/
|-- debian/{live,netinst,netboot}/
|-- kali/{live,netinst,netboot}/
|-- ubuntu/{live,netinst,netboot}/
`-- tails/{live,netinst,netboot}/
```

For Debian Live, `initrd/debian/live` is required and applied automatically to every Live initrd referenced by the selected ISO. For Debian/Kali Netinst and Netboot plus non-Debian Live profiles, the Create flow continues to ask `Include the contents of initrd/<family>/<stage> at the root ...?` independently for each selected source. Opting in passes every entry below that one directory through `cpio` at `/` in only the matching initrd. Those generic optional stage overlays have no required filenames, required assignments, fixed entry list, or allowed filesystem-object-kind schema. Netinst and Netboot rebuild their separately downloaded/copied `hd-media/initrd.gz` or `netboot/initrd.gz`; the opaque Netinst ISO and any initrd inside it are never unpacked or modified by the overlay flow. Live remastering changes only initrd members referenced by Live boot entries and excludes installer initrds found in hybrid media. For a concatenated Live initramfs, every leading early cpio segment, including CPU microcode, is preserved byte-for-byte; only the final main archive is unpacked and rebuilt with its detected original compression, and any target or checksum modes temporarily relaxed after xorriso extraction are restored before the output ISO is written. When a rebuild runs through `sudo`, the complete temporary ISO is assigned to the invoking `SUDO_UID:SUDO_GID` with mode `0600` before its atomic rename, and the repository-managed rebuild directory chain is restored to setgid mode `2770`; the following non-root inspection can therefore open the published ISO while failed builds still preserve any previous valid output. Privileged Netinst and Netboot source preparation likewise hands the completed bundle tree to `SUDO_UID:SUDO_GID` and restores only the managed `sources` ancestor chain to group-traversable setgid mode `2770`; caller-supplied output parents are not changed. The separate prompt to embed `configs/preseed/preseed-debian.cfg` or `configs/preseed/preseed-kali.cfg` as `/preseed.cfg` remains available after the stage-overlay prompt.

Debian Netinst managed values remain in `initrd/debian/netinst/preseed.env`, where its Wi-Fi credential remains `PRESEED_WIFI_PASSPHRASE`. Debian Live uses the separate `LIVE_WIFI_PASSPHRASE` assignment in `initrd/debian/live/live.env`. `./secrets.sh --set` prompts separately for both values and writes each only to its matching active file; it never copies the Netinst credential into Live or the Live credential into Netinst. The retired `DEFAULT_LIVE_WIFI_PSK` config field is removed rather than rendered or prompted. Before committing or pushing, run `./secrets.sh --clear` to sanitize active files plus optional examples and backups that are present. Missing initrd env files, examples, backups, and individual assignments are not errors, and missing managed assignments are upserted in active files that are present. Migrated legacy names and every Live Wi-Fi name are rejected in all GRUB kernel-argument config fields.

| Former GRUB argument | Initrd environment field |
| --- | --- |
| `fruux_username` | `PRESEED_FRUUX_USERNAME` |
| `fruux_password` | `PRESEED_FRUUX_PASSWORD` |
| `primary_user` | `PRESEED_PRIMARY_USERNAME` |
| `primary_password` | `PRESEED_PRIMARY_PASSWORD` |
| `primary_gpg_passphrase` | `PRESEED_PRIMARY_GPG_PASSPHRASE` |
| `root_password` | `PRESEED_ROOT_PASSWORD` |
| `crowdsec_token` | `PRESEED_CROWDSEC_TOKEN` |
| `tailscale_authkey` | `PRESEED_TAILSCALE_TOKEN` |
| `telegram_chat_id` | `PRESEED_TELEGRAM_CHAT_ID` |
| `telegram_api_key` | `PRESEED_TELEGRAM_API_KEY` |
| `cf_r2_access_key` | `PRESEED_CF_APTLY_ACCESS_KEY` |
| `cf_r2_secret_key` | `PRESEED_CF_APTLY_SECRET_KEY` |
| `obs_username` | `PRESEED_OBS_USERNAME` |
| `obs_password` | `PRESEED_OBS_PASSWORD` |

When explicit installer entries exist, the managed menu carries the configured installer defaults. Each preseed-capable GRUB entry receives exactly one transport argument: `url=` for HTTP/internal-public URL variants or `file=` for USB-local variants. Legacy `preseed/url=`, `preseed/file=`, `url/preseed=`, and `file/preseed=` tokens from upstream entries or configured extras are removed before the canonical argument is applied. `DEBIAN_PRESEED_INTERNAL_ARGS` is merged only into Preseed Internal entries, and `DEBIAN_PRESEED_PUBLIC_ARGS` is merged only into Preseed Public entries; either value may be empty. The shipped `DEBIAN_PRESEED_PUBLIC_ARGS` value is empty. If an operator explicitly sets `debian-installer/allow_unauthenticated_ssl=true`, d-i's GNU Wget may retrieve an HTTPS preseed without validating its certificate; the separate public `url=https://...` argument remains the single web-preseed locator. Leave the overlay empty when normal CA validation is required. Debian/Kali custom menus render deterministic HTTP and USB preseed entries for Netinst and Netboot sources, plus supported installer-only profiles such as Kali Purple. USB-local preseed entries use `/hd-media/preseed/<os>/preseed.cfg`; at write time, the writer copies `/data/cfg/preseed/debian` and `/data/cfg/preseed/kali` to `/preseed/<os>` on the managed data filesystem when those folders exist, and warns when they are missing.

In Multi-OS mode, Debian Live, Debian Netinst, Debian Netboot, Kali Live, Kali Netinst, and Kali Netboot are independent toggles. Selecting both Live and Netinst asks for two different inputs and creates two isolated roles: the Live submenu contains only loopback Live entries, while the Netinst submenu contains only entries that direct-load the separately downloaded hd-media kernel/initrd and reference that profile's one opaque Netinst ISO. Debian/Kali preseed entries are generated deterministically rather than through a second preseed-selection prompt.

## Rebuild Installer ISO

`Rebuild Installer ISO` is a separate top-level workflow for existing Debian installer media.

The current rollout focuses on Debian only:

- `Debian` is implemented.
- `Ubuntu` is a visible placeholder.
- `Kali` is a visible placeholder.

The Debian rebuild path accepts a local live-installer ISO or netinst ISO, inspects its boot layout, detects the installer kernel version, and then exposes two rebuild scopes:

- `D-I`
  - `Add Kernel Modules`
  - `Add Udeb Packages`
  - `Update Kernel`
- `Live Host`
  - `Replace Kernel`
  - `Add Deb Packages`

The rebuild backend extracts the source ISO into a managed workspace under `/data/tmp/debian-usb/rebuild-installer-iso/<run-id>/`, writes manifests under `/var/lib/debian-usb/rebuild-installer-iso/<run-id>/`, writes logs under `/var/log/debian-usb/rebuild-installer-iso/<run-id>.log`, and emits the rebuilt ISO into `/data/downloads/debian-usb/iso/rebuild/` by default.

## Menus

Main menu:

```text
[1] Create USB
[2] Build Custom ISO
[3] Rebuild Installer ISO
[4] Settings
[0] Exit
```

Create menu:

```text
[1] Debian
[2] Ubuntu Desktop
[3] Ubuntu Server
[4] Kali Linux
[5] Kali Purple
[6] Multi-OS
[7] Planned Execution
[9] Go Back
[0] Exit
```

Build Custom ISO menu:

```text
[1] Debian
[2] Ubuntu Desktop
[3] Kali Linux
[9] Go Back
[0] Exit
```

Rebuild Installer ISO menu:

```text
[1] Debian
[2] Ubuntu
[3] Kali
[9] Go Back
[0] Exit
```

Settings now exposes:

```text
[1] Live Defaults
[2] Installer Defaults
[3] Profile Overrides
[4] Review Effective Managed Boot Defaults
[5] Planned Executions
[9] Go Back
[0] Exit
```

The top-level Settings screen is now grouped by intent:

- `Live Defaults` covers persistence size, live boot policy, toram, memory limit, and shared live kernel extras.
- The `toram` setting in `Live Defaults` is applied automatically to new managed live builds.
- `Installer Defaults` covers installer policy and shared installer kernel extras.
- `Profile Overrides` lets you pick a profile and only shows the overrides that actually apply to that profile, including dedicated forensic-entry kernel extras when the ISO exposes forensic live entries.
- `Review Effective Managed Boot Defaults` prints the fully merged live and installer arguments that will be used for managed USB builds.
- `Planned Executions` lists saved single-OS and Multi-OS executions from `/data/cfg/debian-usb/planned-executions/`, lets you edit stored ISO paths, target device paths, persistence settings, and live overrides, and lets you delete plans that should no longer be reused.
- Host installation creates that dedicated planned-execution directory for the invoking non-root user. Routine plan creation, edits, and `last_executed_at` updates therefore do not require sudo after installation.
- Review screens now save or reuse a planned execution before the destructive confirmation prompt, so backing out of the write still leaves the plan available in Settings and the Create menu.
- After a USB write or update succeeds, `last_executed_at` bookkeeping is attempted without starting another `sudo` command. An unwritable planned-execution directory produces a metadata warning but cannot turn a successful device operation into a reported failure or a second password prompt.

## Ubuntu live note

Ubuntu live handling does **not** hardcode a release-specific pretty name.

If you build a managed Ubuntu live USB, the app does not hardcode a release-specific pretty name or a fixed Ubuntu boot line. It parses the ISO bootloader config, preserves the upstream entry tree, and adds the runtime-specific UUID and persistence arguments only where the managed rebuilt layout requires them. This now applies consistently to both Ubuntu Desktop and Ubuntu Server casper-style media.

## Kernel cmdline defaults

- Managed live boot policy defaults are profile-aware and boot-safe.
- The shipped live policies are `balanced`, `performance`, and `hardened`.
- Structured live settings can add RAM mode, `mem=<GiB>G`, and other extra kernel parameters without forcing you to rewrite the whole line every time.
- Managed Debian/Kali/Tails live-boot entries translate RAM mode to `toram=<detected-rootfs-module>` and remove pre-existing bare/value forms first. Ubuntu's separate single-OS casper flow retains casper's supported bare `toram` form. Netinst, Netboot, and other installer entries receive no `toram` argument.
- The managed renderer now consumes the shared live base kernel arguments plus the selected policy block directly from `configs/debian-usb.conf`, and it merges them into preserved live entries as part of the managed rebuild flow so the config file, the interactive preview, and the written GRUB menu stay aligned.
- Installer policy is stored separately. The shipped installer policies are `preserve` and `installer-preseed`.
- When a per-profile `<PROFILE>_PRESEED_URL` value is set, installer-capable entries receive one `url=` argument. USB-local entries receive one `file=` argument instead; a Preseed Public variant uses its configured public `url=` in place of the local file transport.
- `DEBIAN_PRESEED_INTERNAL_ARGS` and `DEBIAN_PRESEED_PUBLIC_ARGS` add optional variant-specific GRUB arguments after common/preset arguments. The public default is `debian-installer/allow_unauthenticated_ssl=true`, which makes d-i use GNU Wget with `--no-check-certificate` for HTTPS and therefore disables certificate verification; setting either key to an empty string disables that overlay without removing the submenu.
- Encrypted persistence uses LUKS for Debian and Kali live media. Ubuntu profiles currently expose standard persistence only.

## Persistence note

- Debian and Kali persistence templates contain one effective directive, `/ union`, so the writable overlay covers the complete root filesystem: newly installed packages, `/etc`, home directories, and other runtime changes persist.
- Generated Debian/Kali live-boot arguments normalize stale persistence settings and use `persistence-media=removable-usb persistence-storage=filesystem union=overlay` together with the profile-specific persistence label. Plain and LUKS-backed ext4 persistence partitions therefore use the same full-root overlay contract.
- RAM plus persistence keeps the immutable Live root filesystem in RAM through `toram=<detected-rootfs-module>` while the persistence filesystem remains the overlay upper/work layer:

```text
immutable Live rootfs --toram--> RAM
                                 |
                                 +-- overlay lowerdir
persistence ext4 partition ------+-- overlay upper/work
                                 |
                                 +--> merged writable /
```

- Ubuntu Desktop persistence uses the documented `casper-rw` filesystem label, while the GPT partition name is set to `writable` for current Ubuntu persistent-media conventions.
- Managed partition and filesystem label defaults are configurable in `configs/debian-usb.conf` through `DEFAULT_ESP_LABEL`, `DEFAULT_DEBIAN_{LIVE,NETINST,PERSIST}_LABEL`, `DEFAULT_KALI_{LIVE,NETINST,PERSIST}_LABEL`, `DEFAULT_KALI_PURPLE_NETINST_LABEL`, `DEFAULT_UBUNTU_{LIVE,NETINST,PERSIST}_LABEL`, and `DEFAULT_UBUNTU_PERSIST_PARTLABEL`.
- Multi-OS planning prompts independently for each persistence-capable Live source. Every enabled source receives a dedicated ext4 or LUKS-backed partition starting at partition 3; netinst, netboot, and installer-only sources never receive persistence partitions.

## Managed Layout

- `Write ISO As-Is` uses a direct hybrid write so the upstream ISO boot layout is preserved.
- Direct hybrid writes target the whole-disk device, for example `/dev/sdX`, not a partition such as `/dev/sdX1`.
- Direct whole-disk hybrid writes may surface upstream ISO block-size warnings because the source image is copied byte-for-byte.
- Deterministic managed mode creates a custom GPT layout with:
  - a removable-path UEFI ESP for GRUB, signed kernel assets, and optional `/preseed/<os>` trees
  - either a raw ISO payload partition rebuilt with managed boot metadata or, for ISO-store profiles, one managed payload partition
- Multi-OS managed mode creates a custom GPT layout with:
  - partition 1: one removable-path FAT32 UEFI ESP for GRUB and signed boot assets
  - partition 2: one ext4 ISO store containing separate per-profile ISO paths and preseed data, with no root-level `/.disk`, `/live`, `/casper`, `/install*`, `/d-i`, or `/debian-installer` tree
  - partition 3 and later: one dedicated persistence partition for each persistence-enabled Live source
- Shared ISO-store Live entries use `set isofile`, loopback into that exact ISO, load `(loop)/live/...`, and pass `findiso=$isofile`; Live kernel/initrd files are not copied into direct or Secure Boot asset manifests.
- Netinst entries direct-load the prepared source's separate hd-media kernel/initrd, set their own profile-specific `isofile`, and pass `iso-scan/filename=$isofile`. The copied hd-media initrd makes that path authoritative and fails closed rather than scanning a sibling Live ISO.
- Managed mode installs removable-path `x86_64-efi` GRUB into the ESP and writes the generated GRUB menu under `ESP:/boot/grub/grub.cfg`. Netinst and Netboot Secure Boot entries direct-load their separately staged signed kernel/initrd assets; Live entries continue to load the kernel/initrd from their authenticated opaque ISO loopback.
- When `MOK Enrollment on USB` is selected, the writer reuses the persistent MOK store under `/data/cfg/debian-usb/secureboot/mok` by default, stages `EFI/debian-usb/mok/MOK.der`, and expects the target host to enroll that one certificate once through the `MOK Enrollment` menu entry.
- When `Firmware db import` is selected, the writer creates or reuses owner Secure Boot material under `/data/pki/secureboot/` by default: `db.key` stays private on the build host, while public `db.cer`, `db.crt`, and owner-only `db-owner.esl` are staged under `ESP:/secureboot/` for manual firmware db import. Set `DEBIAN_USB_SECURE_BOOT_PKI_DIR` to another absolute directory if the PKI must live elsewhere.
- Managed Secure Boot builds sparse-clone the official Microsoft `secureboot_objects` repository into the per-run temp tree, read the `MostCompatible.toml` DB template by default, and stage combined `ESP:/secureboot/db.esl` plus `ESP:/secureboot/db.auth` containing both the generated signing identity and Microsoft db certificates. Override the template with `DEBIAN_USB_MICROSOFT_SECURE_BOOT_TEMPLATE` if you need a different official Microsoft template.
- Custom GRUB labels are defined under `configs/spec/grub/*.json`: `main.json` owns top-level families, the internal-drive scanner entry, static Secure Boot entries, UpdateVars child entries, firmware setup, and raw-ISO redirect labels; the family JSON files own live, installer, preseed, preserved submenu labels, and preserved-entry title templates/overrides.
- The custom GRUB menu places the configured UpdateVars static entry below `MOK Enrollment` and above `UEFI Settings` (currently `UEFI Keys` in `configs/spec/grub/main.json`). Its User Mode entry runs signed `UpdateVars.efi` with `secureboot/db.auth`; that only succeeds when firmware KEK already trusts the certificate that signed the authenticated update. Its Setup Mode entry runs signed `UpdateVars.efi` with `secureboot/db.esl` after PK has been cleared.
- Both trust modes use the GRUB OpenPGP key under `*/grub-gpg/` to sign `grub.cfg`, staged kernels, initrds, and `secureboot/manifest.sha256`; kernel-module signing material is also staged under `ESP:/secureboot/` for target-side module trust policy. If MokManager reports `Volume Full`, the target firmware EFI variable store is full, not the USB filesystem.

## Install

Requirements on the host:

- Debian-based system
- `sudo`
- package installation rights

Install with:

```sh
make install
```

Run that as a normal user. `sudo make install` is intentionally blocked; the workflow escalates only for the host writes that actually need `sudo`.

Install the repository-managed pre-commit and pre-push secret-clearing hooks into this checkout with:

```sh
make install-git-hooks
```

Git does not version files below `.git/hooks`, so `.githooks/pre-commit` and `.githooks/pre-push` are the tracked sources and `scripts/install-git-hooks.sh` copies both into the hooks directory resolved by Git. The installer validates both targets before writing either one, refuses symlinks, and refuses to overwrite unrelated hooks.

Before every commit and push, the matching hook runs `./secrets.sh --clear-initrd --index`. It recursively inspects regular `.env`, `.env.*`, `.conf`, and `.conf.*` files below `initrd/`, clears non-empty assignments whose keys identify credentials (including passwords, passphrases, tokens, usernames, chat IDs, credentials, authentication values, and generic credential keys while exempting recognized public/GPG key metadata), and leaves unrelated assignments unchanged. Output lists only the affected worktree or index path and key name; secret values are never printed. The pre-commit index rewrite preserves other staged content independently from unrelated unstaged edits.

Both hooks always return success to Git, including when clearing reports an error, so they never stop a commit or push. The pre-push hook clears the worktree and index but intentionally does not rewrite immutable outgoing commits; a secret already committed with hooks bypassed can therefore still be pushed. Audit and rewrite that history before pushing when hooks were bypassed.

`make install` installs the required Debian packages for the existing USB-writer runtime before staging the managed host assets. That includes the ISO inspection and rebuild dependencies such as `xorriso`, `grub-install`, `cryptsetup`, `parted`, `dosfstools`, `e2fsprogs`, and `lsinitramfs`.

The Debian Build Custom ISO host dependencies are **not** installed during `make install`. They are installed lazily when you select `Build Custom ISO -> Debian`.

Remove installed files without cached state:

```sh
make uninstall
```

Remove installed files and managed state/log directories. The runtime download root under `/data/downloads/debian-usb` is preserved:

```sh
make nuke
```

## Development

Build:

```sh
make build
```

Run from the repository checkout:

```sh
make run
```

Run verification:

```sh
make check
```

The Python test discovery covers the ISO parsing, kernel-argument synthesis, and managed GRUB rendering logic directly so the supported profiles stay regression-tested without hardcoded local media paths in the repository.

## Repository layout

```text
cmd/debian-usb/               Go CLI entrypoint
internal/app/                 Go TUI, config store, plan builder, and helper integration
configs/                      Install-time defaults
  spec/                       Repo-managed live and d-i module/deb/udeb profiles
initrd/<family>/<stage>/       Opt-in root overlays for separate Live/Netinst/Netboot initrds
.githooks/{pre-commit,pre-push} Tracked non-blocking initrd secret clear hooks
src/python/debian_usb/        Python ISO/media inspection and GRUB rendering logic
  iso_source.py               ISO file access helpers
  boot_parse.py               GRUB/Syslinux parsing and entry selection
  boot_inspect.py             media classification and initrd capability checks
  boot_render.py              managed GRUB rendering and boot arg synthesis
  build_iso.py                Debian live-build validation and execution logic
scripts/                      POSIX sh launchers, installer, and USB writer
  build_iso.sh                privileged Debian ISO build helper
  check-secrets.py            Index and outgoing-commit managed-secret scanner
  install-git-hooks.sh        Safe installer for both tracked Git hooks
tests/python/                 Python unit tests
```
