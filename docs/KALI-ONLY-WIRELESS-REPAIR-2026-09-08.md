# r4: Kali-only wireless penetration-testing tools

## Corrected behavior

Wireless penetration-testing additions belong only to Kali Live. Debian Live
retains the recovery, administration and ordinary Wi-Fi functionality requested
before those additions. No penetration-testing package group is implicitly or
explicitly resolved for Debian or Ubuntu by the shipped catalog.

| Profile | Optional groups with All/default | Unique optional package requests | Wireless assessment group |
| --- | ---: | ---: | --- |
| Debian Live | 20 | 93 | Not available |
| Kali Live | 21 | 113 | Available only here |
| Ubuntu Desktop Live | 20 | 93 | Not available |

The Debian groups, package ordering, command mappings and complete optional
package list are compared against a baseline taken directly from the original
user-supplied `debian-usb.zip`, not from a previous refactored delivery. The
baseline is `tests/fixtures/debian-live-admin-baseline.json`.

Kali's entire resolved optional package list is unchanged from r3: no tools were
removed from Kali. Its wireless group includes wifite, aircrack-ng, reaver,
pixiewps, hcxdumptool, hcxtools, hashcat, Wireshark/tshark, kismet, airgeddon,
bettercap, wifiphisher and both Kali wireless metapackages, along with the other
configured analysis tools. These are package requests, not a claim of a
completed real-image APT installation. Existing repository-candidate reporting
and fatal handling of APT installation/dependency errors remain unchanged.

In Kali's menu, choose **Kali-only Wi-Fi penetration testing and defensive
analysis** or **All**. **None** continues to mean no added optional tool groups.
Normal Wi-Fi runtime packages are independent of this selection in both Debian
and Kali. Existing packages in an input ISO are not uninstalled.

## Root cause and implementation

The old schema-2 catalog placed `wireless_security` in the common group array.
Only five extra Kali packages were scoped by `profile_additions`; the common
wireless packages were still included in Debian's All/default expansion. The
previous Go group-selection validator also validated against the unfiltered
catalog rather than the selected distribution.

The schema-3 catalog now declares `profiles: ["kali-linux"]` on the entire
wireless group. Both Go and Python filter groups before expanding default/All
selections. Validation rejects an empty, omitted or widened scope on the
reserved wireless group, cross-profile additions and optional packages, and
schema-2 overrides. Normal common groups do not need an explicit scope.

The Go UI, plan validation and execution preflight use the same scoped catalog.
Multi-OS preflight validates every item's selection before preparing the first
ISO. Single-OS preparation validates before materializing or remastering a
source. The Python resolver independently rejects an explicit Debian/Ubuntu
wireless-group request, so a direct CLI invocation cannot bypass the UI.

Build-ISO validation also checks flattened package lists after feature-spec
merging. A stale saved plan containing wifite or another catalogued Kali-only
package in `base_packages`, `storage_tool_packages`, `extra_chroot_packages` or
`extra_binary_packages` fails before installation, even when the old group ID
has already been removed. Common tools such as iw, rfkill, tcpdump, nmap and
unrelated custom packages are not blocked.

A second defect appeared when testing the shipped Kali Live build profile:
`build_iso.py` called an undefined `_dedupe` helper. That branch now uses its
existing `_append_unique` helper. The tests validate both shipped build
profiles and materialize their actual package-list and optional-package hook
files, rather than checking catalog metadata alone.

## Preserved behavior

The shared normal Wi-Fi implementation, per-distribution `live.env` files,
`wifi-connect.sh` staging, route preference/fallback behavior and initrd
networking hooks are unchanged by this revision. Debian's original recovery
and administration groups, including its existing nmap and tcpdump choices,
remain available. Ordinary networking is not a penetration-testing group.

The installer Desktop/Server split, HD-MEDIA consent behavior, initrd device-node
and preseed transport repairs, USB persistence writer and Tails restrictions
from r3 are retained. The writer, Live runtime and installer implementation
files covered by the unchanged-file audit match r3 byte for byte; see
`docs/validation/kali-only-wireless/results.json` for 33 audited file hashes.

This fix prevents new unwanted package additions. It does not inspect and
purge an input ISO, remove a tool from an existing persistence partition, or
undo previous USB writes.

## Validation performed

- Full Python suite: **466 passed as root and 466 as an ordinary user**, no skips.
- New Python distribution-scope suite: **18 passed**. This includes original-ZIP
  baseline equality, legacy catalog rejection, cross-profile saved selections,
  flattened package-list validation, both shipped build workspaces, normal Wi-Fi
  runtime staging, and sequential Kali/Debian remaster orchestration.
- Go suite: **172 top-level tests passed**, **249 passing events with subtests**.
  New tests cover per-profile menus, catalog validation, Multi-OS source
  separation and rejection before preparation/writing.
- Go race detector, vet, application build and executable `--help`: passed.
- `make check-live-tool-scope`: passed as an ordinary user.
- `make check`: passed as an ordinary user, including all six existing shell
  fixtures and staged installation with new installed-catalog isolation checks.
- Python/JSON syntax parsing and archive integrity are checked during packaging.

The first unprivileged full-check invocation reached staged installation but
exceeded the command time limit. Its isolated staged-install rerun succeeded,
and complete unprivileged `make check` reruns subsequently finished with exit
status 0. The retained log records the final successful run.

### Validation limits

The available compiler was **Go 1.23.2**. Tests/builds used an external
compatibility modfile, leaving the delivered `go.mod` unchanged at **Go 1.24.0**.
The compatibility-built binary is not included. Build the application with
Go 1.24 or newer on the deployment host.

Package resolution and build workspace generation were executed. Destructive
ISO/chroot and USB operations in orchestration tests use explicit test doubles.
No complete real Debian/Kali ISO remaster, repository installation, USB write,
VM boot, physical boot or Wi-Fi-radio test was performed. This is not a
hardware-certified production release.

## Upgrade

Extract r4 into a new directory. Do not overwrite its runtime, executable or
`configs/spec/live/admin-tools.json` with files from earlier revisions. Restore
only your reviewed configuration, preseeds and intended overlays. The normal
configuration format and per-distribution `live.env` locations are unchanged.

With Go 1.24 or newer, run as your normal user:

```sh
sha256sum -c SOURCE-MANIFEST.sha256
make check-live-tool-scope
make check
make run
```

For an installed application, run `make install` from the new checkout and
restart the application. Do not use `sudo make`. Remove an old
`DEBIAN_USB_LIVE_TOOL_PROFILE` override or point it at the new schema-3 catalog.
Do not leave Python/helper overrides pointing at an old installation.

For Debian, use your original stock/source ISO, not an earlier `-admin-tools.iso`
that already contains the unwanted tools. A corrected package selector cannot
remove packages already in that input or in persistent storage.

Old saved Debian/Ubuntu plans explicitly containing `wireless_security` or its
Kali-only package entries must be edited or recreated. Remove only those entries
from Debian/Ubuntu; retain Kali's wireless selection. Ordinary Debian Wi-Fi and
recovery tooling do not need to be disabled.

The finished archive has a separate SHA-256 sidecar and a source manifest. The
final external artifact-verification JSON records tests run from a fresh
unprivileged extraction of that archive.
