> Historical architecture record. The September 8 behavior changes are in [REFRACTOR-2026-09-08.md](REFRACTOR-2026-09-08.md).

# Deferred USB build pipeline and Live APT repair

Revision date: 2026-09-05. See [VALIDATION.md](VALIDATION.md) for the executed checks and remaining hardware acceptance gates. This is a source release; it does not contain a prebuilt ISO or an assertion of firmware/hardware certification.

## Selection and execution contract

The USB flows now use the same phases:

```
Select sources and options (read-only inspection is allowed)
    -> select the whole target disk
    -> save the declarative plan and show technical tables
    -> final explicit confirmation (default: No)
    -> recheck disk identity and every selected input
    -> download/materialize/prepare all sources
    -> inspect the prepared artifacts and required crypto support
    -> recheck the disk identity
    -> invoke the existing privileged USB writer
```

Selecting modules, an overlay, a preseed, tools, or encrypted persistence no longer downloads assets or rebuilds an initrd. A declined confirmation can leave a saved JSON plan, but must not create a prepared source or change the USB. Standalone ISO build/rebuild dependency installation also runs only after its final confirmation. Normal ISO inspection can extract members to temporary scratch space; that is not a remaster.

`SourcePreparation` stores local paths and metadata, configured download keys and URLs, installer kernel/initrd inputs, module strategy, preseed, and overlay. It is carried through single-OS, Multi-OS, saved execution editing/replay, and USB update. Local paths are resolved and checked for regular, nonempty files. Size and modification time guard against accidental replacement, not malicious changes preserving metadata. Downloaded images must still be authenticated against the distribution's published checksums/signatures.

A reviewed remote source has no fabricated inspection result: the UI records it as pending and execution inspects the actual download before writing. A changed configured URL aborts the reviewed plan. A cache entry for a different URL is not silently substituted. Moving upstream aliases can still resolve to a newer file at execution; select a verified local ISO or a pinned version URL when bit-for-bit reproducibility is required.

`prepareSelectedSource` is the common transformation boundary. An as-is/direct write never enters the live remaster pipeline. Debian managed Live selections always include the mandatory runtime policy; choosing no optional tools does not disable that policy. Missing encryption capability is recorded as a build requirement rather than prompting for an immediate rebuild. Prepared encrypted sources are inspected again and a failed capability check prevents the writer from running.

## One customization pass

For supported SquashFS-based sources, existing Live ISO customization combines optional administration packages, the selected initrd overlay, and any needed cryptsetup/initramfs support in one root filesystem workspace. Package-driven `update-initramfs -c` and `-u` calls are temporarily deferred with a local dpkg diversion of the appropriate executable. The package-installed command is restored before final initrds are generated, once per distinct referenced kernel ABI, and the resulting root filesystem is packed once.

Debian Live's pre-existing `live-tools` diversion and symlink wrapper remain in place throughout package installation. A temporary local diversion defers the `live-update-initramfs` executable behind that symlink, and the latest package-installed executable is restored even on package-install failure. Merged-`/usr` wrapper aliases share the temporary protection without moving the underlying file twice. Unknown diversions are rejected rather than silently overwritten. The initrd overlay is installed as a final initramfs-tools hook, so installing packages cannot replace an earlier overlay-bearing initrd. Future initrd regeneration can also retain the selected overlay.

Netinst and Netboot use one lazy `_InstallerInitrdSession`. Module customization, overlay content, embedded `/preseed.cfg`, and the Netinst exact-ISO selection guard share one extracted tree and one final repack. The exact-ISO guard is applied last. Source downloads remain untouched; only the generated bundle is modified. Netinst uses the separate hd-media kernel/initrd plus exactly one opaque installer ISO. Netboot has no ISO payload. Ambiguous kernel ABI or incompatible installer assets fail closed.

This does not mean the entire USB operation executes only one ISO-container command. For example, a managed raw-ISO layout may still assemble a final UEFI redirect after its ESP identity is known. That is separate from repeatedly rebuilding the Live rootfs/initrd during menu selection.

## Multi-OS guarantees

All selected inputs are validated before preparing the first item. Every item is prepared before the writer receives the plan. The executable copy of the plan is separate from the saved selection plan. Generated ISO filenames are refreshed in `PayloadISOName`, so GRUB references the actual remastered filename rather than the original source name. A prepared-source flag prevents the writer's compatibility fallback from remastering the same inputs again.

The shared ISO store does not acquire sibling `/live`, `/.disk`, or `dists` trees. Live ISOs stay opaque and use their exact `findiso` locator. Netinst uses its exact requested installer ISO, with no sibling scan fallback. Persistence remains per Live item. Ubuntu continues to use its existing separate single-OS workflow; it is not newly enabled in Multi-OS.

Target review displays the whole disk, not a payload partition: path, model/vendor, capacity, serial, transport, and mount state. Path, size, model, vendor, serial, transport, partition-table identity, removability, and system-disk status are checked again at execution boundaries. Disks without serials cannot have cryptographically unique identity; identical unidentifiable hot-swapped devices remain a physical-operations risk.

Preparation failure happens before the destructive writer boundary. Once writing starts, disk operations are not transactional: power loss or writer failure can leave a partially written USB. Use a sacrificial target and retain backups.

## Live APT: real repository data, not a fake index

The supplied failure was a file repository pointing into a medium view without its `dists` package indexes. The extracted/mounted root filesystem is not itself a Debian package archive. An installed package database cannot reconstruct downloadable `.deb` payloads.

The repair is baked into the Live rootfs at `/usr/lib/debian-usb/`, together with a Live-only systemd service and an APT update pre-invoke hook. It therefore does not depend solely on finding `/live/config-hooks` on a medium that module-only RAM boot may no longer expose. A boot command-line guard prevents it running against an installed non-Live system, and a lock serializes the service, medium hook and APT invocation.

The repository helper checks Release or InRelease metadata, suite/codename, architecture, component index SHA256/size, decompression, and every package index's referenced file and declared size. Paths and symlinks must remain inside the selected medium. It supports `Packages.xz`, `.gz`, `.bz2`, and uncompressed `Packages`. It does not pretend missing indexes or absent package pools are usable.

When the real ISO repository is available, it writes one managed Deb822 source. If module-only `toram` hides that ISO, the helper can reopen the exact `findiso` payload from the exact `live-media=/dev/disk/by-uuid/...` store, read-only, below `/run/debian-usb/`. It never scans sibling ISOs and never copies the package pool into RAM. An unavailable/missing/disconnected ISO disables the local source rather than leaving APT probing nonexistent indexes. Existing enabled network sources are preserved. If none exists, a signed Debian fallback is added; release suites bullseye/bookworm/trixie include their updates/security stanzas, while testing-style suites are not assigned guessed security repositories.

Local ISO sources deliberately use `Trusted: yes`, scoped only to that read-only local source, because media/remaster repositories may be unsigned. **Only use trusted, authenticated source ISOs.** This is not a signature verification mechanism. Network sources retain their keyring-based verification. APT still verifies package hashes when consuming repository packages.

Remastering preserves the original ISO's package repository when one exists. It does not guarantee an offline `.deb` for every optional tool installed into the squashfs. Custom live-build generation has its existing binary-repository validation. An upstream ISO that never contained a complete offline repository correctly falls back to network APT; a completely disconnected session cannot download packages absent from the medium.

## RAM policy

Normal managed Live boot is USB-backed: no `toram` copy unless explicitly enabled by the configured setting or a RAM-labelled GRUB variant. An explicit off choice removes inherited `toram` tokens from source/config extras. Installer entries remove all `toram` forms.

`/run/live/rootfs/filesystem.squashfs/` is normally the mount point exposing a compressed, read-only filesystem; seeing its directory contents does not imply the entire tree has been extracted into RAM. With `toram` disabled, the compressed image remains USB-backed and normal kernel caching still uses reclaimable RAM. With RAM mode enabled, live-boot copies the selected compressed root module; the writable overlay still needs RAM or a persistence backing store. Keeping offline APT attached to the USB also means the USB must remain connected even during a RAM boot when those offline packages are needed.

## Interface and configuration notes

Interactive terminals use Up/Down, Home/End, Page Up/Down, Enter/Space, numeric shortcuts, Back and Exit. Active entries and chosen options are highlighted. Device and settings tables wrap complete paths instead of silently truncating the target identity. `NO_COLOR`, `TERM=dumb`, and piped input keep readable fallback behavior; cursor and terminal mode are restored on return/interruption.

Saved plans now retain source role and preparation details. Save uses `s`, avoiding the former collision with live-overrides option `8`; saved-plan rows also participate in arrow navigation.

The uploaded Live Wi-Fi template had enabled WPA settings with no passphrase. It now starts unconfigured (empty ESSID and address fields). Repository files under `initrd/` have no required Unix mode; generated Live root and medium copies are staged with mode 0600. Configure the local `initrd/debian/live/live.env` before building when Wi-Fi is needed. Embedded credentials remain extractable by anyone possessing the image; generated-file mode is not media encryption.

## Upstream references

- Debian live-boot(7): https://manpages.debian.org/trixie/live-boot-doc/live-boot.7.en.html
- Debian APT sources.list(5): https://manpages.debian.org/trixie/apt/sources.list.5.en.html
- Debian live-tools update-initramfs(8): https://manpages.debian.org/trixie/live-tools/update-initramfs.8.en.html
- Debian dpkg-divert(1): https://manpages.debian.org/trixie/dpkg/dpkg-divert.1.en.html

These references explain the upstream behavior; the included tests document which implementation paths were exercised here.

## September 6 build repair

Firmware-refresh masks are no longer installed before live-build package configuration. In remaster chroots, only these managed masks are lifted while `policy-rc.d` blocks starts, then reapplied before the service-start guard is restored. Installer source selection asks only about the stage overlay, not a separate preseed template; an optional `preseed.cfg` in that overlay is included by the existing single-repack pipeline. Explicit legacy saved-plan preseed paths remain compatible. See `BUILD-REPAIR-2026-09-06.md`.
