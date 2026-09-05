# Multi-OS build repair - September 6, 2026

## Delivered changes

This repair addresses the reported abort during Debian Live preparation inside a
Multi-OS build, the firmware-refresh preset conflict, and the duplicate installer
preseed question. It changes the shared preparation code used by single-OS and
Multi-OS flows. It does not ignore package errors or permit USB writing after a
failed preparation.

### 1. Initramfs deferral no longer dismantles live-tools' diversion

The reported exception was raised by the old cleanup code, not by the displayed
iptables, multipath-tools, or initramfs-tools trigger messages. The old code saved
the live-tools symlink, removed its package-owned diversion, temporarily replaced
that diversion with a LOCAL diversion, and then tried to restore the vendor
layout with `dpkg-divert --package live-tools --add --rename`.

With real package ownership metadata, dpkg-divert may successfully register that
vendor diversion without renaming a path owned by live-tools. The engine remained
at the wrapper path, so cleanup raised `update-initramfs wrapper changed during
package installation`. The original tests omitted the package's file-ownership
records and therefore did not exercise this behavior. Adding those records to the
old tests reproduced the exact exception on success and simulated APT failure.

A real generated `.deb` upgrade exposed a further defect: while the vendor
diversion was replaced by a LOCAL diversion, initramfs-tools could conflict with
live-tools' ownership of the same command path. Merely changing the rename or
suppressing the final exception would not fix that problem.

The final implementation in `src/python/debian_usb/rebuild_iso.py` therefore:

- Leaves the existing live-tools diversion and update-initramfs symlink in place
  throughout package installation.
- Temporarily diverts the `live-update-initramfs` executable behind that symlink,
  installs a no-op shim there, and restores the latest package-installed wrapper
  before final initrd generation. Ordinary roots without live-tools defer the
  ordinary update-initramfs executable instead.
- Protects both standard path spellings on merged-/usr roots. Alias diversions
  use `--no-rename`, so the shared underlying file is moved exactly once.
- Preserves normal dpkg ownership routing when initramfs-tools and live-tools
  are upgraded. Temporary diversions are removed on successful installation and
  exceptions. The restored command must still be a regular file when it existed
  at entry.
- Rejects unknown diversions, malformed diversion databases, unexpected wrapper
  targets, and unfinished temporary state instead of overwriting them.

Package-triggered `-c` and `-u` calls remain deferred. After packages and selected
overlays/persistence policies are applied, the existing pipeline generates final
initrds once per distinct referenced ABI, then repacks the filesystem. A regression
fixture using real dpkg-divert and live-tools ownership metadata now reaches both
final initrd generation and filesystem repacking. Its ISO/mount/apt operations
remain simulated; it is not a boot test.

### 2. Firmware-refresh masks are applied after package configuration

The generated live-build tree no longer contains automatic fwupd masks in the
early `config/includes.chroot` tree. They remain in the after-packages tree and
the runtime-policy hook.

For remaster package installation, `_temporary_chroot_systemd_unmask` temporarily
removes only the managed `fwupd-refresh.service` and `fwupd-refresh.timer` masks.
The existing temporary `policy-rc.d` guard is active before those masks are lifted
and remains active until after they are restored. This allows offline systemd
presets to succeed without starting services. On success or failure, the final
masks are re-created and the timer's enablement link is removed. Unrelated masks
are left alone. A pre-existing policy-rc.d file is restored with its previous
contents and permissions.

Three tests execute real `systemctl --root=... preset` commands against temporary
unit trees. They reproduce the masked-unit error, verify successful presets
during guarded installation, and check restoration on success and injected APT
failure, including zero-length unit-file masks. They never contact host systemd.

Messages saying daemon-reload is ignored in a chroot or policy-rc.d returned 101
are expected service-start suppression. The guard has not been removed to hide
those messages. Real APT and preparation errors still propagate.

### 3. One interactive source for root-level installer initrd content

The separate interactive preseed-embedding helper and its call are removed from
`internal/app/create.go` and `internal/app/source_selection.go`.

The stage-overlay question remains. Selecting
`initrd/debian/netinst` includes its contents at `/` in the separate managed
installer initrd. An optional file named `preseed.cfg` in that directory becomes
`/preseed.cfg`. No template from `configs/preseed` is silently copied or enabled.
The existing `preseed.env` and other overlay files are unchanged.

The same single-question behavior is used for Debian/Kali Netinst and Netboot.
Eight Go subtests cover both profiles, both installer roles, and yes/no answers.
They verify that no second question consumes the following input, no legacy
preseed directory is needed, and selection does not modify the overlay.

Explicit `initrd_preseed_path` values in older saved plans and the noninteractive
helper option remain supported for compatibility. Newly collected interactive
selections leave that field empty. The existing installer overlay/repack pipeline
is retained.

## Validation performed on the repaired source

| Check | Result |
| --- | --- |
| Complete `make check` recipe | Passed with the Go compatibility setup described below |
| Python unittest discovery | 355 passed, no skips; implicit-encoding warnings promoted to errors |
| Go tests | 157 top-level tests passed; 229 pass events including subtests |
| Go race detector | Passed |
| Go vet | Passed |
| Initramfs regression/guard suite | 20 passed, including real dpkg-divert and generated `.deb` upgrades |
| Offline systemd preset suite | 3 passed, using real systemctl against temporary roots |
| Shell syntax | 15 shell files passed individual dash syntax checks |
| Shell fixtures | Device release, Live APT, Live Wi-Fi, Multi-OS tools all passed |
| Staged install | Passed as part of make check; installed CLI/helper/config assets checked |
| Patch whitespace check | Passed |

The logs and machine-readable results are in
`docs/validation/2026-09-06-build-repair/`. The pre-fix reproduction log intentionally
shows errors from the original implementation; it is evidence of the bug, not a
failure of the repaired release. The September 5 records are retained as history.

### Toolchain qualification

The project still declares `go 1.24.0`; that file was not lowered. The container
provides Go 1.23.2 and could not download the required toolchain. Go checks used a
temporary modfile outside the repository, changing only the go directive to
1.23.0. For the full make recipe, an external wrapper injected that modfile into
Go build/test/vet commands, including the staged-install build. Python was 3.13.5;
dpkg-divert was 1.22.22; systemd was 257.

This is a compatibility validation, not a claim that the required Go 1.24+
toolchain was executed here. Build and check with a toolchain satisfying go.mod
on the deployment host. The delivered tarball is source-only: the old precompiled
`build/debian-usb` executable is deliberately omitted so it cannot retain the
removed prompt or stale behavior. All original non-binary source, configuration,
initrd content, helper scripts, examples, and historical logs are retained.

### Tests not performed

No actual Debian/Kali ISO was supplied for a full privileged remaster, and no
physical USB or booted VM was available. End-to-end ISO creation, real USB writes,
BIOS/UEFI boot, Secure Boot, network installation, and persistence unlock have not
been certified by these tests. Existing SquashFS/EROFS and multi-layer rootfs scope
limits in VALIDATION.md remain unchanged. The isolated `.deb`, dpkg-divert, and
systemctl tests do not constitute a full network APT installation of fwupd.

## Using this source release

Extract into a fresh directory, retain the original input ISOs, and run as the
normal interactive user on the Debian host:

```sh
make build
make check
make run
```

For a managed host installation rather than repository-local execution, use
`make install` as the normal user; it invokes sudo for the privileged operations.
Do not use `sudo make`. Rebuild from the original source images rather than using
a half-modified live-root directory from an aborted build as a new source.

There is no additional preseed confirmation to answer. To embed a preseed file,
place it in the selected overlay under the exact name `preseed.cfg` before the
build. No preseed file is required by this change.
