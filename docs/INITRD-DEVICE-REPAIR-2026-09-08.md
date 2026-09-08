# Installer initrd device-node repair - September 8, 2026

## Reported failure and root cause

The previous source release contained a real defect in
`src/python/debian_usb/installer_profiles.py:prepare_installer_profiles()`.
After the shared initrd was prepared, the Desktop/Server splitter extracted it
into `clean/` and called `shutil.copytree(clean, expanded, symlinks=True)`.
`copytree` uses ordinary file-copy operations, which must not be used to clone
character/block devices or FIFOs. It attempted to open `dev/console` and
`dev/null` as data files, matching the reported `[Errno 13] Permission denied`.
The previous synthetic installer fixtures contained ordinary files and did not
cover this case. This was a code/test-coverage error, not a requirement for the
user to relax device permissions or add a preseed-host variable.

The same implementation also broke internal hard links and created each output
as a new `.gz` archive, losing a source's original compression and any leading
early-cpio segments. New regression fixtures demonstrate these failures before
the fix and pass after it.

## Corrected implementation

For each Desktop/Server flavor independently, the builder now:

1. Copies the regular, prepared source **archive** into the staged flavor output.
2. Extracts that copy with the existing cpio archive reader. It does not read
   device contents or clone an extracted tree with `shutil.copytree`.
3. Rejects a shared embedded preseed, then applies only the selected flavor's
   overlay, optional Desktop seed, and native preseed transport dispatcher.
4. Repackages the same copied archive. The existing writer preserves detected
   compression, archive file mode, and leading early-cpio bytes, while cpio
   preserves node types/numbers, internal hard links, symlinks and entry modes.
   As before, output cpio ownership is normalized to root.
5. Removes the expanded workspace before preparing the next flavor. Both
   finished flavors and their manifest are published together only after both
   builds succeed. Failed profile-only rebuilds preserve the preceding pair.

The original shared archive and kernel are unchanged by profile splitting.
Neither deleting `/dev` nor replacing devices with regular files is a workaround.
Permission failures now identify the distribution, source role and flavor and
explain the need for a privileged helper with device-node creation capability.

This change uses the same path for Debian/Kali and netinst/netboot. The preceding
HD-MEDIA consent handling, separate Desktop/Server preseeds, per-OS persistence,
Live networking, and Tails guards are retained. No Tails support claim or Live
remaster policy was broadened by this fix.

## Regression coverage

`tests/python/test_installer_profile_archives.py` adds eight tests. They exercise:

- Real FIFO archives for Debian/Kali netinst/netboot, without requiring root.
- Character/block-device cpio headers for all eight family/role/flavor outputs:
  console 5:1, null 1:3, zero 1:5, and a fixture block node 8:0. The tests preserve
  type, major/minor, modes, root ownership, symlinks and zero device payload size.
- A guard which rejects any Python file-copy attempt on a device/FIFO, recreating
  the reported EACCES shape in the old code. A separate FIFO test reproduces an
  actual cpio/copytree failure without that guard.
- Internal hard links, symlinks, dotfiles, private-file permissions and separation
  between the independently extracted flavors.
- gzip, xz, bzip2, zstd (when installed), and uncompressed archives, each with and
  without two leading early-cpio segments, including read-only source archives.
- No calls to `shutil.copytree` in profile splitting, clear permission diagnostics,
  and preservation of an existing profile pair when the second extraction or
  second repack fails. Temporary profile-build trees are cleaned after failure.

The new `make check-installer-archives` target runs this focused suite without a
Go build or an ISO download. `fakeroot` is now an explicit test/build dependency
in `configs/install.env`; the installer verifies that it is present. Real image
preparation still runs via the application's actual sudo-managed helper.

## Executed validation

The full `make check` recipe passed with 417 Python tests, no skips, the Go tests,
all six shell fixtures, and its staged-install assertions. Separate fresh Go JSON
results contain 164 top-level tests / 236 passing test events; `go vet`, the race
detector, compilation, and the executable's help command passed. Syntax checks
passed for 55 Python files, 19 POSIX shell scripts and 37 JSON documents.
The entire 417-test Python suite and the focused eight-test suite also passed
as unprivileged uid/gid 65534, with no skips.

Logs and the independent device-header report are included under
`docs/validation/2026-09-08-initrd-device-repair/`. Earlier validation reports in
this repository are historical; they did not cover this device-copy regression.

### Environment and qualification

The runtime is Debian 13 with Python 3.13.5, GNU cpio and fakeroot. This container
forbids native `mknod`, so the character/block-device test runs within one fakeroot
session. Its output is independently inspected as actual compressed newc headers;
this validates archive metadata, not real kernel device I/O. Ordinary/FIFO/archive
operations use the real cpio, compression tools and filesystem. No physical USB,
VM boot, or end-to-end build against the user's exact Debian 13.6.0 ISO was run.
Public upstream downloads were attempted but unavailable through the execution
environment's network, so generated archive fixtures were used explicitly.

The available Go toolchain is 1.23.2, while the unchanged project `go.mod` requires
1.24.0. As in the previous release, Go checks used an external compatibility
modfile changing only the Go directive to 1.23.0. No such modfile or compatibility
binary is installed or shipped as the application. Run `make check` and build on
a host with Go 1.24 or newer before deployment. The exact compatibility commands
used here are represented by:

```sh
GOTOOLCHAIN=local GOFLAGS=-modfile=/path/to/external/go.compat.mod make check
GOTOOLCHAIN=local GOFLAGS=-modfile=/path/to/external/go.compat.mod go vet ./...
GOTOOLCHAIN=local GOFLAGS=-modfile=/path/to/external/go.compat.mod \
  GORACE=atexit_sleep_ms=0 go test -race -count=1 -timeout=90s ./...
```

These results establish regression-level validation of the reported code defect;
they do not certify every ISO/hardware/Secure Boot/persistence combination for
production. The hardware acceptance matrix in `docs/VALIDATION.md` remains open.

## Upgrade and recovery

Extract the corrected tarball into a **new directory**, not over the old checkout.
Keep original downloads, existing working USBs, and your private configuration.
Restore reviewed site-specific configuration and overlays, not old runtime source
files, a stale `build/debian-usb`, generated bundles or generated GRUB snapshots.

On the properly provisioned Debian host, run these as the normal interactive user:

```sh
make check-installer-archives
make clean
make build
make check
make run
```

When a managed host installation is used, run `make install` as that same normal
user to update its helpers. Do not run `sudo make`. Close an older running process
before starting the corrected one. Prepare a **new** affected netinst/netboot
bundle from the original downloaded kernel/initrd and matching netinst ISO. An
incomplete bundle from the failed run is not a finished source. No manual `chmod`
on device nodes, preseed-host config variable, or removal of the entire download
root is needed.

The tarball is the complete source distribution, not a patch. Its source manifest
can be checked with `sha256sum -c SOURCE-MANIFEST.sha256` after extraction and before
modifying configuration or running tests which intentionally inspect source files.

## Technical references

- Python shutil documentation: https://docs.python.org/3.13/library/shutil.html
- Linux initramfs/newc format: https://www.kernel.org/doc/html/latest/driver-api/early-userspace/buffer-format.html
- GNU cpio manual: https://www.gnu.org/software/cpio/manual/cpio.html
