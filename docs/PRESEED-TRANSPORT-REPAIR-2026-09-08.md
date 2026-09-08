# Installer preseed transport repair - 2026-09-08, revision r3

## Scope and failure

This is the complete source distribution based on `debian-usb-fixed-20260908.tar.gz`, with the installer preseed startup discovery and transport implementation repaired. It is not a patch-only delivery. The earlier device-node/archive-copy repair remains in place, as do the Desktop/Server split, optional HD-MEDIA codebase copying, per-OS Live persistence, and Debian/Kali Live changes.

The reported failure happened after module preparation and the exact ISO-selection patch:

```text
installer initrd must contain exactly one native initrd-preseed startup script; use an unmodified Debian/Kali installer initrd
```

The old detector scanned only `lib/debian-installer-startup.d`, excluded symlinks, and counted every regular file containing `/preseed.cfg`. That is not a reliable identification of a native loader: a diagnostic, comment, or existence check can contain the same path. It also misses a linked loader or a different supported startup layout. A regression fixture with the native loader plus an unrelated seed observer reproduces the old two-candidate failure.

The supplied log does not distinguish zero candidates from multiple candidates, and the exact failing binary initrd was not supplied. This repair therefore tests both discovery failure classes; it does not claim to identify the exact contents of that particular initrd.

A second defect was found in the old transport rewrite. The native source uses `preseed_location file:///preseed.cfg`; the previous path-substitution expression did not cover this URL operand. Suppressing the discovery error alone would leave netboot USB preseeding incorrect.

## Implementation

`src/python/debian_usb/installer_preseed.py` now owns loader discovery, safe guest-path resolution, and transport generation. It identifies native filenames and optional udeb ownership, with a command-position loader signature as a compatibility fallback. It inspects `/lib` and `/usr/lib` startup layouts, deduplicates merged-directory aliases, and resolves relative and absolute links inside the extracted initrd rather than the build host. Ordinary seed references and diagnostic messages are not loaders.

The native script is saved byte-for-byte. Its symlink or hard-link target is not modified when replacing a startup entry. Scripts are syntax-checked with `sh -n`, never executed on the build host. Existing managed backups are checked before reuse. Two genuinely competing loaders, malformed scripts, missing required libraries, and unsafe links still cause explicit errors with discovery evidence.

When a compact installer omits the startup hook but contains both the preseed-common loader API and early-command entry point in an actual Debian Installer startup layout, an equivalent small adapter is generated. Its manifest origin is explicitly `preseed-common-adapter`. This is not a fallback for arbitrary Linux or Live initramfs images.

The mode dispatcher behaves as follows:

| Mode | Netinst | Netboot |
| --- | --- | --- |
| `initrd` | Execute the untouched native loader | Execute the untouched native loader |
| `http` / `https` | Do not load the embedded seed; leave network preseeding to the installer | Same |
| `usb` | Leave HD-MEDIA/file-preseed loading to its normal later stage | Mount the selected partition, then call the installer's own preseed-common API with the selected `file://` URL |
| Invalid | Fail with an explicit mode error | Same |

The netboot USB loader validates the seed path and optional checksum, uses `preseed_location` for native fetch/include/checksum behavior, and runs `preseed/early_command` once. It does not rewrite downloaded shell code or use `eval` on the selected path. The mount helper still requires the selected partition UUID and does not mount arbitrary disks by guessed device order.

Managed source preparation performs read-only loader discovery before module rebuilding, using the already shared extraction session. The Desktop and Server archives remain independent and are published together only after both succeed. Each flavor records `preseed_transport` metadata including version 2, role, selected startup path, origin, and native-script SHA-256. Old profile manifests without version 2 are rejected instead of silently reusing the broken transport; the error explains how to regenerate the source, without modifying that old bundle.

The Makefile now includes the new module in staged-install checks and runs the expanded installer tests through `make check-installer-archives`. Two Go orchestration tests also had missing sudo stubs: their nested profile preparation could call the host's real sudo when tests ran as a normal user. Those tests now use a restricted test helper and an explicit non-root identity. Production sudo handling was not weakened.

## Validation results

Logs for this revision are in `docs/validation/preseed-transport-v2/`. Older validation directories describe earlier revisions, not this final run.

| Validation | Result and scope |
| --- | --- |
| Full Python suite | 448 passed as root and 448 passed as the ordinary `oai` user; no skips |
| Focused installer target | 56 passed as an ordinary user: archive metadata, preseed discovery/transport, and Desktop/Server integration |
| Go suite | 164 top-level tests; 236 passing events including subtests; separate uncached JSON run |
| Go race, vet, build, executable help | Passed with the compatibility toolchain qualification below |
| Complete `make check` | Passed as an ordinary user, including all six shell fixtures and staged installation of the new module |
| BusyBox chroot checks | 15 passed using unchanged guest paths and generated dispatch scripts; debconf, preseed-common, and USB mounting are explicit test doubles |
| Real donor-module pipeline | Actual ABI detection, `xxhash_generic`/`lz4` dependency copying, depmod, exact-ISO script patch, and both flavor archives passed with installed `6.12.96+deb13-amd64` kernel/modules |
| Syntax | All 60 Python files parsed, JSON documents parsed, `.sh` scripts syntax-checked, and the Makefile's additional shell checks passed |

The real-module pipeline uses a synthetic installer userspace, real cpio, and fakeroot device metadata. It checks both flavor outputs for the module dependency files, exact-ISO policy, unchanged native script, and preserved device-node metadata. Only work/state/log directory locations are redirected; the module-copy, archive, ISO-policy, and flavor-building functions are real. It does not inspect or remaster a real ISO payload.

The environment has Go 1.23.2. Go tests/builds and `make check` used an external compatibility modfile that is not distributed. The delivered `go.mod` remains `go 1.24.0`. No compatibility-built executable is included; build and repeat the checks with Go 1.24 or newer on the deployment host.

No complete downloaded Debian/Kali initrd was available in this environment. The native-loader fixture uses source syntax from the official Kali repository; it is explicitly not represented as a current binary installer. The exact reported `6.12.94+deb13-amd64` initrd, full ISO remaster, VM boot, physical USB boot, Wi-Fi radio, Secure Boot, and Tails boot were not tested in this repair. Fakeroot preserves device metadata for tests but is not evidence of native device-node creation in this restricted container. These are acceptance limits, not a claim of hardware-certified production readiness.

## Upgrade and rebuild

Verify the archive checksum, extract into a new directory, and verify `SOURCE-MANIFEST.sha256` before restoring your reviewed configuration. Do not copy an old executable or runtime Python files into the new checkout.

With Go 1.24 or newer and the project's test prerequisites installed, run as your normal user:

```sh
make check-installer-archives
make check
make run
```

`make run` uses the checkout's helper paths. Remove any old `DEBIAN_USB_PYTHONPATH` override that deliberately selects another installation. To update the installed command instead, run `make install` from this checkout and restart the application. Do not use `sudo make`.

Keep your downloaded kernel, initrd, and matching netinst ISO. Prepare a new managed source with the selected Desktop and Server overlays. Do not select the incomplete generated source from the failed run as a finished bundle. No device-node deletion, permission relaxation, or removal of the download directory is required.

The new runtime reports its selected loader before module preparation and logs completion in this form:

```text
Installer preseed loader: /lib/debian-installer-startup.d/S30initrd-preseed (native).
Desktop installer ready: /lib/debian-installer-startup.d/S30initrd-preseed (native, transport v2).
Server installer ready: /lib/debian-installer-startup.d/S30initrd-preseed (native, transport v2).
```

The path and origin can legitimately differ for another supported layout. Seeing the old exactly-one error indicates that old runtime code is still being executed; that message no longer exists in this revision's production source.

## Optional check against your actual input archive

The non-destructive checker copies its inputs into a temporary directory, preserves device metadata under fakeroot, constructs both flavor archives, checks the input hashes, reports the discovered transport metadata, and removes its temporary outputs. It requires no sudo and neither mounts filesystems nor writes USB devices. Use only trusted, checksum-verified inputs.

```sh
python3 scripts/verify-installer-initrd.py \
  --kernel /path/to/vmlinuz \
  --initrd /path/to/initrd.gz \
  --profile debian --role netinst \
  --overlay-dir initrd/debian/netinst
```

The checker validates archive splitting and preseed transport construction only. It does not validate the kernel/initrd/ISO ABI alignment or boot the result; the normal application retains its alignment checks. For Kali use `--profile kali-linux`; for netboot use `--role netboot` and its matching overlay directory.

The optional deeper fixtures are also included:

```sh
# Requires an already-installed matching donor kernel/modules and fakeroot:
fakeroot python3 tests/python/installer_module_pipeline.py --kernel /boot/vmlinuz-<ABI>

# Requires root/CAP_SYS_CHROOT; uses temporary files and no mounts or real disks:
python3 tests/python/installer_preseed_chroot.py
```

## Source references

The small native-loader source fixture and API contract were checked against the official Kali packaging repository. These references establish source behavior, not the release provenance of the user's binary initrd:

- Native startup loader: https://gitlab.com/kalilinux/packages/preseed/-/raw/kali/master/debian-installer-startup.d/S30initrd-preseed
- Preseed-common API: https://gitlab.com/kalilinux/packages/preseed/-/raw/kali/master/preseed.sh
