# Installer, multi-OS persistence, Kali Live and Wi-Fi refactor

Date: September 8, 2026. This document describes the delivered source, not a
certification of real USB or firmware behavior. See `VALIDATION.md` for the
executed checks and hardware acceptance gates.

## Build and deploy the matching source

The archive contains the complete application source, configuration, installer
codebases, tests and documentation. It is not a bootable ISO. The input archive's
stale compiled executable and stale rendered GRUB examples have been removed;
otherwise running an old executable would retain the reported configuration
failure even with new Python/scripts installed.

Use a Go toolchain satisfying the unchanged `go.mod` requirement (Go 1.24.0 or
newer), then run as your normal user:

```sh
make check
make build
make run
# To install the complete matching runtime instead:
make install
```

Do not use `sudo make`. The application and install workflow request privileged
operations through sudo. `make check` installs its fixture under a temporary
DESTDIR, not onto the real host. Review your configured URLs, unattended-install
settings and target device before confirming any destructive operation.

## Desktop and Server are independent

Debian and Kali each have separate Desktop and Server initrd roots for both
netinst and netboot. Each is generated from a clean source kernel/initrd pair;
Server never receives Desktop's preseed tree. Netinst retains its opaque ISO
and hd-media boot pair. Netboot uses its own prepared kernel/initrd assets, not
the netinst ISO.

The menu hierarchy and each flavor's HTTPS, HTTP LAN, embedded-initrd and
HD-MEDIA transport remain independent. External transports suppress the
embedded preseed, preventing it from running ahead of the selected source.
Netboot HD-MEDIA uses the exact USB data-filesystem UUID. It requires storage
and ext4 support for the selected installer kernel ABI; a bare `file=` argument
cannot supply missing kernel drivers.

See `installer-profiles.md` for all directories, configuration keys and the
transport matrix. This separation is covered with real cpio archive fixtures,
not just string comparisons of menu labels.

## Host preseed paths are consent-only

These variables are removed from shipped configuration and required keys:

```text
PRESEED_HOST_DEBIAN_DE_PATH
PRESEED_HOST_DEBIAN_SRV_PATH
PRESEED_HOST_KALI_DE_PATH
PRESEED_HOST_KALI_SRV_PATH
```

Go and Python discard these keys during migration, including old unqualified
host-path aliases. They are not used as a hidden fallback. Kali Purple's
unrelated legacy single-profile host setting is not changed.

The Desktop/Server prompt first asks whether to add HD-MEDIA content. Only an
explicit Yes opens the host-path prompt. Enter the full host path to
`preseed.cfg`; the application copies its entire parent directory, including
companion scripts, dotfiles and permissions, into the matching target on USB
partition 2. Existing checks reject missing seeds, unsafe object types,
source/destination overlap and symlinked destination directories. Publication is
atomic so a failed replacement preserves the previous target.

No means no copy and no host-path validation. It does **not** remove the
HD-MEDIA menu entry or its `file=` argument. Add the codebase later by mounting
partition 2 and populating the appropriate directory:

| Flavor | Path relative to the partition-2 mountpoint |
| --- | --- |
| Debian Desktop | `debian-preseed-de/preseed.cfg` |
| Debian Server | `debian-preseed-srv/preseed.cfg` |
| Kali Desktop | `kali-preseed-de/preseed.cfg` |
| Kali Server | `kali-preseed-srv/preseed.cfg` |

For example, `<mountpoint>/debian-preseed-de/preseed.cfg` is loaded by the
installer as `/hd-media/debian-preseed-de/preseed.cfg`. Do not create another
`hd-media` directory at the partition root. The default destinations can be
changed with the corresponding `PRESEED_USB_*_FILE` settings.

Saved plans record explicit choices in `hd_media_preseed_dirs`; an empty map
never authorizes copying. Netinst and netboot of the same family share their
flavor's HD-MEDIA folder, and conflicting explicit source paths are rejected.

## One persistence partition per selected supported Live OS

The real shared-layout writer now calls one small, directly tested allocator:
`duw_multios_create_persistence_partitions`.

```text
partition 1: ESP
partition 2: shared ISO/data store
partition 3: first Live item with persistence enabled
partition 4: second Live item with persistence enabled
partition 5: third Live item with persistence enabled
...         continue in plan order
```

An installer or a Live item with persistence disabled does not increment the
counter. When only Kali enables persistence, for example, it receives partition
3 even if Debian and installer entries precede it. Each enabled OS uses its own
size, filesystem/partition labels and matching boot selection. Invalid boolean
values, unsupported media and conflicting labels are rejected before writing.
The allocator tests include skipped entries, multiple selections, contiguous
boundaries and `sdX`, NVMe and MMC partition-name conventions. They mock `parted`
and never open the fake devices.

This does not add arbitrary new OS support to the existing multi-OS catalogue.
Tails is deliberately excluded from generic persistence, as explained below.

## Shared Debian/Kali Live rebuild pipeline

Debian and Kali Live always receive their own required Live overlay and shared
Wi-Fi runtime even when optional administration-tool selection is None. A
single preparation pass extracts the squashfs root, installs packages, applies
runtime/overlay policy, regenerates initrds once per referenced kernel ABI, and
repacks the ISO. Persistence-only remastering delegates to this same pipeline
instead of maintaining a second implementation. Installer initrds in hybrid
images are not repurposed as Live initrds. The original source ISO is immutable;
publication occurs only after successful preparation, before USB writing.

Debian retains its existing APT repository repair and locale policy. Kali uses
its own repositories/keyring and the shared Wi-Fi setup, never Debian APT
source repair. Firmware and optional tool candidates are resolved inside the
target distribution, not against packages installed on the build host.

In **Kali Live only**, select **Kali-only Wi-Fi penetration testing and defensive analysis** (`wireless_security`) or
**All**. This group is not offered to or installed by Debian/Ubuntu tool selection. It requests wifite, aircrack-ng,
reaver, pixiewps, hcxdumptool, hcxtools, hashcat, tshark, Wireshark, wavemon,
horst, hostapd, arp-scan, tcpdump, python3-scapy, iw, rfkill and kismet. Kali also
requests `kali-tools-wireless`, `kali-tools-802-11`, airgeddon, bettercap and
wifiphisher. Metapackage dependency coverage changes with Kali's repository;
there is no fixed promise that every wireless tool ever published exists in a
particular image's repositories. These tools are for authorized analysis and
testing of networks you own or have permission to assess.

Core networking/initramfs packages remain mandatory for both Debian and Kali. Kali optional tools/firmware
are candidate-resolved. Debian does not receive the wireless penetration-testing package group. Missing optional
candidates are named and recorded in the Live root at:

```text
/var/log/debian-usb/optional-packages.json
```

The report distinguishes requested, available, unavailable and verified
installed packages. Missing packages are not silently described as installed.
APT index-update errors, signatures, dependency resolution, installation errors
and incompletely configured packages remain fatal. No foreign distribution
repository or signature bypass is added. Broad wireless metapackages can
substantially increase the root filesystem, download size and scratch-space
requirements.

## Independent live.env and Wi-Fi priority

Edit these files **before** rebuilding each image:

```text
initrd/debian/live/live.env
initrd/kali/live/live.env
```

Both shipped templates intentionally have an empty ESSID and passphrase. The
parser accepts only seven `LIVE_WIFI_*` assignments and treats values as literal
data: never source this file in a shell. It validates SSID/interface/security,
IPv4 CIDR, gateway, DNS addresses and WPA/SAE credential length. It rejects
unknown/duplicate keys, control characters, symlinks and oversized files.
A static gateway requires a static CIDR; leave both empty for DHCP.

```text
LIVE_WIFI_INTERFACE="auto"
LIVE_WIFI_ESSID="Your exact network name"
LIVE_WIFI_SECURITY="wpa"
LIVE_WIFI_CIDR=""
LIVE_WIFI_GATEWAY=""
LIVE_WIFI_NAMESERVERS=""
LIVE_WIFI_PASSPHRASE="replace-this-locally"
```

Security values are `open`, `wpa` (WPA2-Personal/RSN), or `sae`
(WPA3-Personal). Hidden SSIDs, WEP and enterprise EAP are not implemented by
this simple helper. UTF-8 SSID length is validated in bytes. Do not put secrets
in GRUB, kernel arguments or a shared configuration committed to source control.

The validated environment is staged at `/etc/debian-usb/live.env`, on the Live
medium, and in each rebuilt initrd as `/live.env`. A late initramfs build hook
ensures the authoritative validated copy wins over earlier overlay content.
The init-bottom script copies it to `/run/initramfs/debian-usb/live.env`.
Networking tools run from the Live root after NetworkManager is ready; they
are not needlessly bundled into the early initramfs.

The shared runtime scans for the exact ESSID before activation. When connected,
Wi-Fi uses IPv4/IPv6 route metric 50; active Ethernet profiles are temporarily
adjusted to 600 without disconnecting them. A NetworkManager dispatcher keeps
that preference across link/DHCP changes. Negative DNS priority prefers the
Wi-Fi resolver while active. Absent SSID or failed Wi-Fi activation leaves
Ethernet available; existing Ethernet connections are never deliberately torn
down. This is not an internet-reachability/captive-portal failover monitor.
VPN-specific routing and DNS policy require separate validation.

`wifi-connect.sh` is installed in existing `/home/*` directories, in `/etc/skel`
for accounts created at boot, and in `/usr/local/bin`. Run:

```sh
~/wifi-connect.sh
```

It prompts for interface, exact ESSID, security, CIDR, gateway, nameservers and a
hidden passphrase. It uses sudo when necessary. Root-only NetworkManager
keyfiles are written atomically; no passphrase is passed in command arguments
or emitted in ordinary error output. Failed activation restores the previous
keyfile, but does not guarantee reconnection of an old Wi-Fi session. Successful
manual changes are saved to the Live root and persist across reboot only when
the appropriate filesystem is persistent.

`secrets.sh --set` manages the existing Debian and Kali Live credential files
separately from installer credentials. Its one Live value applies to both
present files; edit files individually for different credentials. `--clear`
sanitizes them before distribution. Mode 0600 is not encryption: anyone with
the ISO, initrd or physical USB can extract embedded credentials, including
from an otherwise nonpersistent session.

## Tails: explicit experimental boundary

Tails officially requires a dedicated USB and does not support third-party
multiboot installation methods. Apparent boot success does not establish its
normal security, upgrade or persistence behavior. A generic live-boot `/ union`
partition is not a valid replacement for native Tails Persistent Storage.

This project retains **experimental stock-ISO boot** in the shared ISO store.
It preserves upstream Tails boot/security options rather than injecting Debian
performance, login, Wi-Fi, locale or generic persistence policies. Kernel/initrd
overrides, all public remaster entrypoints, administration-tool additions, and
plain/encrypted generic persistence are blocked. Tails menu labels explicitly
state experimental/no persistence. Its existing directories/config placeholder
are retained only for compatibility and are not applied to stock Tails.

No real Tails ISO was booted in this validation environment. This is not a claim
that Tails works securely on every custom-GRUB or multi-OS USB. Use the official
Tails USB image and installation procedure on a dedicated device for supported
Tails operation, native encrypted Persistent Storage and upgrades.

## Primary references checked

- Tails installation and multiboot restrictions:
  https://tails.net/support/faq/index.en.html
- NetworkManager keyfiles, root-only permissions and SSID representation:
  https://www.networkmanager.dev/docs/api/1.46/nm-settings-keyfile.html
- NetworkManager route metrics, DNS priorities and wireless security settings:
  https://www.networkmanager.dev/docs/api/latest/nm-settings-nmcli.html
- Kali metapackages, including wireless and 802.11 tool collections:
  https://www.kali.org/tools/kali-meta/
- Debian Live persistence, includes and build hooks:
  https://live-team.pages.debian.net/live-manual/html/live-manual/index.en.html

Repository candidate resolution is the authority for the image being built;
these references do not substitute for boot testing the exact resulting image.
