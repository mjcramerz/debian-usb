# Validation record

## September 8, 2026 - installer device-node repair (current)

The previous profile splitter used an ordinary-file tree copier on an extracted
initrd and failed on device nodes. This release copies and independently extracts
each source archive instead. It also preserves internal hard links, original
compression, leading early-cpio segments and archive mode. See
`INITRD-DEVICE-REPAIR-2026-09-08.md` for changes, reproduction and recovery steps.

The full check passed: **417 Python tests, no skips**, **164 top-level Go tests**
(236 passing test events), six shell fixtures and staged-install assertions.
Separate Go vet/race/build/help checks passed. The entire 417-test Python suite and the eight new archive tests also
passed as an unprivileged user, with no skips. Syntax checks cover 55 Python files, 19 POSIX shell
scripts and 37 JSON documents. Current logs are in
`validation/2026-09-08-initrd-device-repair/`.

Character/block-device tests use fakeroot metadata with real cpio and independent
newc-header inspection, because native mknod is denied in this container. No exact
user ISO build, physical USB or VM boot was tested. Go checks used 1.23.2 with an
external compatibility modfile; the shipped `go.mod` still requires 1.24.0 and no
compatibility executable is shipped. Repeat the checks with the deployment
host's matching toolchain. The hardware acceptance gates below remain open.

The records below are historical. Their passing ordinary-file fixtures did not
cover the defect reported after the initial September 8 release.

## September 8, 2026 - installer, persistence and Live networking refactor

The complete check recipe passed after this refactor, including 409 Python
tests (no skips), all Go tests, six shell fixtures and a staged installation
of both Debian and Kali Live assets. Go JSON results contain 164 top-level
tests and 236 passing test events including subtests. Separate `go vet` and
race-detector runs passed. Individually checked syntax: 53 Python files,
19 POSIX shell scripts and 34 JSON documents. The Makefile now checks shell
files individually rather than passing extra filenames as script arguments.

New regression coverage exercises consent-only host preseed migration, the
shared Wi-Fi runtime, exact SSID matching, private configuration writes,
failed activation rollback, DNS/route priority, separate Debian/Kali staging,
optional target-repository package resolution, and all public Tails remaster
guards. The real writer's persistence allocator passes 15 mocked layout
scenarios spanning disabled/selected items and sdX/NVMe/MMC naming. Existing
installer tests exercise independent Desktop/Server archives and transport
selection using generated cpio fixtures.

Current logs are in `validation/2026-09-08-refactor/`. Earlier records below
are historical, not new measurements. Tests use fake disks, mocked network
activation and isolated package/archive fixtures. They do not perform a full
Kali/Debian ISO remaster, install the complete live toolset from online
repositories, boot a VM, create real USB partitions, exercise Wi-Fi radio
hardware, or verify Secure Boot/LUKS in firmware. Package availability is
resolved against the target repositories when an actual build runs.

The environment has Go 1.23.2 and Python 3.13.5. The unchanged `go.mod` declares
Go 1.24.0; its automatic toolchain download could not resolve the download
host here. As in the earlier repair, validation used an external temporary
modfile with only the Go directive changed to 1.23.0. Commands were:

```sh
GOTOOLCHAIN=local GOFLAGS=-modfile=/mnt/data/go.validation.mod make check
GOTOOLCHAIN=local GOFLAGS=-modfile=/mnt/data/go.validation.mod go vet ./...
GORACE=atexit_sleep_ms=0 GOTOOLCHAIN=local \
  GOFLAGS=-modfile=/mnt/data/go.validation.mod go test -race -timeout=90s ./...
```

That external modfile is not included and is not required on a correctly
provisioned host. Build and repeat `make check` with Go 1.24+ before deployment.
The delivered tarball is source-only; the stale input executable and obsolete
rendered GRUB snapshots are deliberately removed, not represented as rebuilt
binaries or current boot menus.

### Remaining hardware acceptance gates

On a disposable USB, confirm the exact target identity before creation. Test
Debian and Kali Live separately and together, with persistence off, enabled
only for the later-selected OS, and enabled for both. Verify partition numbers,
UUIDs, sizes and labels; write distinct files in each persistent root and reboot
each OS to prove isolation. Test plain and LUKS modes independently.

Boot each Debian/Kali netinst/netboot Desktop/Server entry with unique harmless
fixture preseeds for all four transports. Decline HD-MEDIA copying, confirm
creation still succeeds, then add the codebase to partition 2 and verify the
pre-existing menu works. Confirm the installer cannot consume another flavor's
embedded or HD-MEDIA preseed. Netboot requires the matching USB/ext4 drivers.

For both Live families, test simultaneous Ethernet/Wi-Fi, absent SSID, wrong
password, DHCP and static IPv4, explicit DNS, replug/DHCP renewal and the
interactive home launcher. Inspect IPv4 and IPv6 default routes and resolver
selection. Test supported firmware/chipsets and radio regulatory behavior.
Review `optional-packages.json`; missing requested tools must not be treated as
installed. Verify resulting initrds contain the matching environment and module
closure for each kernel ABI, without copying secrets into kernel arguments.

Tails custom-GRUB boot remains experimental and untested on hardware. Native
Tails persistence, upgrade integrity and its full security behavior are not
claimed. Use the official Tails USB image on a dedicated device for supported
operation. No generic Tails persistence partition is created by this code.


## September 6, 2026 - build repair

The repaired source passed the complete make-check recipe: 355 Python tests with
no skips, 157 top-level Go tests (229 pass events including subtests), all four
shell fixtures, and staged installation. Go race/vet checks and individual syntax
checks of 15 shell files also passed. New regression coverage uses real
dpkg-divert, generated `.deb` upgrades of initramfs-tools and live-tools, and
real offline systemctl presets. It reproduces the originally reported abort
before the fix and verifies continuation through the remaster pipeline afterward.

Go validation used the available Go 1.23.2 with an external temporary compatibility
modfile; go.mod still requires 1.24.0. No physical USB, booted VM, or full real-ISO
remaster was tested. The source tarball omits the stale precompiled executable.
See `BUILD-REPAIR-2026-09-06.md` and `validation/2026-09-06-build-repair/` for the
precise changes, logs, qualification, and deployment instructions.

## Historical September 5 record

The record below came with the supplied codebase. Its original diversion fixture
did not include real live-tools file ownership, and therefore did not cover the
reported failure. It is retained for provenance, not as proof that the old code
handled the newly reproduced case.

Date: 2026-09-05. The repository was checked in a Linux container. No real Debian ISO, physical USB, or booted VM was supplied. Results below must not be interpreted as completed hardware/firmware acceptance testing or production certification.

## Executed results

| Check | Result |
| --- | --- |
| Go unit/process-boundary tests | 157 tests, passed |
| Go race detector | Passed; child-process exit delay disabled with `GORACE=atexit_sleep_ms=0` |
| Go vet | Passed |
| Python regression suite | 337 tests, passed, no skips; executed in batches |
| Shell fixtures | All four passed: device release, Live APT, Live Wi-Fi, Multi-OS tools |
| POSIX shell syntax | Passed with dash |
| Real local APT integration | `apt-get update` and `.deb` download passed against a temporary, generated repository |
| Real dpkg diversion integration | Success/failure restoration, live-tools wrapper, and merged-/usr layout passed |
| Real installer archive integration | Overlay + preseed + exact-ISO policy used one extraction/repack; original archive unchanged |
| Terminal PTY smoke | Down/Enter, submenu, Back, Exit, ANSI highlight, cursor restoration and termios restoration passed |
| Staged install/uninstall | Installed CLI, Python profile loading, new helper, private Live env mode and uninstall passed |

Python batch totals were 141 + 79 + 102 + 15. Results/logs are in `docs/validation/`. The single-invocation suite was interrupted by the execution environment time limit; all discovered modules were then completed in bounded batches. The APT test has isolated configuration/state/cache directories and does not change the host's APT configuration or contact an external repository. Go USB execution tests use real process boundaries with explicit fake helpers: they validate ordering and arguments, **not physical writes**.

## Toolchain qualification

The supplied `go.mod` declares Go 1.24.0 and remains unchanged. This container has Go 1.23.2; automatic download of the requested toolchain could not resolve its download host. Validation therefore used a temporary modfile outside the repository with only the `go` directive lowered to 1.23.0. The code compiled, passed tests/race/vet, and built the staged CLI under that compatibility check. No compatibility binary is included in this source archive. Before deployment, build and rerun `make check` with a supported Go toolchain satisfying the repository's Go 1.24 requirement.

Python validation used Python 3.13 with implicit-encoding warnings promoted to errors. `cpio`, `gzip`, `find`, `dpkg-divert`, `dpkg-deb`, APT and initramfs inspection tools were available. Full ISO mastering/chroot package installation, Secure Boot enrollment, LUKS unlock during boot, and USB hardware were not exercised end-to-end.

## Normal reproducible commands

Run as the normal interactive user on a Debian host with an appropriate Go toolchain:

```sh
make build
make check
go vet ./...
GORACE=atexit_sleep_ms=0 go test -race ./...
make install
```

`make install` prompts through sudo for privileged host operations; do not run `sudo make`. `make check` performs its install test in a temporary DESTDIR rather than your host filesystem. ISO builds can additionally need network access, host packages, scratch space, and root for chroot/mount operations. Use only trusted source images and authenticate downloaded artifacts independently.

The compatibility commands actually used for Go in this container were:

```sh
GOTOOLCHAIN=local go test -modfile=/mnt/data/work/test-go.mod ./...
GOTOOLCHAIN=local go vet -modfile=/mnt/data/work/test-go.mod ./...
GORACE=atexit_sleep_ms=0 GOTOOLCHAIN=local go test -race -timeout=30s -modfile=/mnt/data/work/test-go.mod ./...
```

The temporary modfile is not part of the project or required on a correctly provisioned host.

## Known scope limits

The existing Live rootfs remaster implementation is SquashFS-based (`unsquashfs`/`mksquashfs`). This refactor does not add EROFS extraction/repacking or multi-layer Ubuntu rootfs customization. In particular, an EROFS payload named `filesystem.squashfs` by the custom builder is not a SquashFS payload and cannot pass that remaster path. For the managed customization pipeline, build/select an actual SquashFS-based source. Direct/as-is writing bypasses rootfs remastering, but its bootability still depends on the source image and hardware. The standalone placeholder profile actions retained in the original repository are not newly implemented. These limits prevent an honest claim that every possible ISO/profile is production-certified.

## Required deployment acceptance matrix

Use disposable targets and recorded upstream ISO checksums. These are pending release gates, not completed tests:

| Scenario | Required observation |
| --- | --- |
| Debian managed Live, no persistence | BIOS/UEFI boot, USB-backed root, APT works |
| Debian managed Live, standard persistence | Create/reboot/retain a marker file |
| Debian source initially lacking crypto, encrypted persistence | One preparation pass, LUKS2 unlock, retained marker, wrong password rejected |
| Explicit RAM boot | Selected compressed root copied only; exact offline ISO repository available while USB remains attached |
| Raw/as-is ISO | Source and written bytes match for the ISO length; no remaster |
| Netinst stable/testing | Separate matched hd-media assets; exact requested ISO; no sibling Live ISO chosen |
| Netboot | No ISO requirement; successful network installer start |
| Multi-OS mixed Live/Netinst/Netboot | Correct entry-to-source mapping and distinct persistence; one failed source prevents writer entry |
| Saved plan replay/edit and USB update | Reconfirm real device; source changes rejected; persistence retained by update behavior |
| Secure Boot trust modes | Actual target-firmware enrollment and boot validation |
| Removal/failure cases | Disk swap rejected at recheck; aborted build leaves original sources; writer failure is reported accurately |

For a booted Debian Live session, useful non-destructive observations are:

```sh
cat /proc/cmdline
findmnt -T /run/live/medium
findmnt -T /run/live/rootfs/filesystem.squashfs
findmnt -R /run/debian-usb
systemctl status debian-usb-live-apt.service
cat /etc/apt/sources.list.d/debian-usb-medium.sources
sudo apt update
free -h
lsblk -o NAME,PATH,MODEL,SERIAL,SIZE,FSTYPE,MOUNTPOINTS
```

The managed local `.sources` file is intentionally absent when no complete local repository is available. Inspect network `.sources` files in that case; absence is not itself a failure. A successful network update does not prove offline package availability, and a mounted rootfs directory does not prove a full RAM copy.
