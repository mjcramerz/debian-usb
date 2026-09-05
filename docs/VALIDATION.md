# Validation record

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
