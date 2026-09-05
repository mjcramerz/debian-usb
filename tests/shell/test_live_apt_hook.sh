#!/bin/sh
set -eu
IFS=$(printf '\n\t')

repo_root=$(CDPATH='' cd -P "$(dirname "$0")/../.." && pwd -P)
DEBIAN_USB_HOOK_SOURCE_ONLY=1
export DEBIAN_USB_HOOK_SOURCE_ONLY
# shellcheck disable=SC1091
. "${repo_root}/config-hooks/0500-apt-live-medium.sh"

fail() {
  printf 'test_live_apt_hook: %s\n' "$*" >&2
  exit 1
}

write_os_release() (
  root=$1
  distro_id=$2
  suite=$3
  mkdir -p -- "${root}/etc/apt/sources.list.d" "${root}/etc"
  cat >"${root}/etc/os-release" <<EOF_OS
ID=${distro_id}
VERSION_CODENAME=${suite}
EOF_OS
)

assert_official_source() (
  root=$1
  suite=$2
  source_file=${root}/etc/apt/sources.list.d/debian-usb-live.sources
  [ -f "${source_file}" ] || fail "official fallback source was not created for ${suite}"
  [ "$(stat -c '%a' "${source_file}")" = 644 ] || fail "official fallback source mode is not 0644 for ${suite}"
  expected_file=${root}/expected-debian-usb-live.sources
  cat >"${expected_file}" <<EOF_EXPECTED
Types: deb
URIs: https://deb.debian.org/debian
Suites: ${suite}
Components: main contrib non-free non-free-firmware
Signed-By: /usr/share/keyrings/debian-archive-keyring.gpg
EOF_EXPECTED
  cmp -- "${expected_file}" "${source_file}" || fail "unexpected official fallback content for ${suite}"
)

assert_idempotent_apt_tree() (
  root=$1
  snapshot=$2
  rm -rf -- "${snapshot}"
  mkdir -p -- "${snapshot}"
  cp -a -- "${root}/etc/apt/." "${snapshot}/"
  lap_main "${root}"
  diff -ru -- "${snapshot}" "${root}/etc/apt" >/dev/null || fail "second hook run changed the APT source tree"
)

temp_root=$(mktemp -d)
trap 'rm -rf -- "${temp_root}"' 0 INT TERM

mixed_root=${temp_root}/mixed
write_os_release "${mixed_root}" debian trixie
cat >"${mixed_root}/etc/apt/sources.list" <<'EOF_LIST'
# Existing network and disabled entries must survive.
deb [trusted=yes] file:/run/live/medium trixie main
deb file:///run/live/medium/ trixie main
deb file:/lib/live/mount/medium trixie main
deb file:///lib/live/mount/medium/ trixie main
deb file:/cdrom trixie main
deb file:///cdrom/ trixie main
deb cdrom:Debian trixie main
deb-src file:/run/live/medium trixie main
deb [trusted=yes enabled=no] file:/cdrom trixie main
deb https://packages.example.invalid/debian trixie main
EOF_LIST
cat >"${mixed_root}/etc/apt/sources.list.d/custom.list" <<'EOF_CUSTOM_LIST'
# Persistent custom repository.
deb https://mirror.example.invalid/debian trixie main
deb-src https://mirror.example.invalid/debian trixie main
EOF_CUSTOM_LIST
chmod 0600 "${mixed_root}/etc/apt/sources.list.d/custom.list"
cat >"${mixed_root}/etc/apt/sources.list.d/mixed.sources" <<'EOF_MIXED_SOURCES'
Types: deb
URIs: file:/run/live/medium
 https://vendor.example.invalid/debian
Suites: trixie
Components: main

Types: deb
URIs: file:///lib/live/mount/medium
Suites: trixie
Components: main

Types: deb
URIs: file:///cdrom
Suites: trixie
Components: main

Types: deb
URIs: cdrom:Debian
Suites: trixie
Components: main
EOF_MIXED_SOURCES
cat >"${mixed_root}/etc/apt/sources.list.d/disabled.sources" <<'EOF_DISABLED_SOURCES'
Types: deb
URIs: file:/run/live/medium
Suites: trixie
Components: main
Enabled: no
EOF_DISABLED_SOURCES
cp -- "${mixed_root}/etc/apt/sources.list.d/custom.list" "${temp_root}/custom.list.before"
cp -- "${mixed_root}/etc/apt/sources.list.d/disabled.sources" "${temp_root}/disabled.sources.before"

lap_main "${mixed_root}"
cat >"${temp_root}/mixed-sources.list.expected" <<'EOF_EXPECTED_LIST'
# Existing network and disabled entries must survive.
deb [trusted=yes enabled=no] file:/cdrom trixie main
deb https://packages.example.invalid/debian trixie main
EOF_EXPECTED_LIST
cmp -- "${temp_root}/mixed-sources.list.expected" "${mixed_root}/etc/apt/sources.list" || fail "legacy local sources were not filtered precisely"
cmp -- "${temp_root}/custom.list.before" "${mixed_root}/etc/apt/sources.list.d/custom.list" || fail "custom legacy network source changed"
cmp -- "${temp_root}/disabled.sources.before" "${mixed_root}/etc/apt/sources.list.d/disabled.sources" || fail "disabled Deb822 local source changed"
[ "$(stat -c '%a' "${mixed_root}/etc/apt/sources.list.d/custom.list")" = 600 ] || fail "custom source mode changed"
cat >"${temp_root}/mixed.sources.expected" <<'EOF_EXPECTED_SOURCES'
Types: deb
URIs: https://vendor.example.invalid/debian
Suites: trixie
Components: main

EOF_EXPECTED_SOURCES
cmp -- "${temp_root}/mixed.sources.expected" "${mixed_root}/etc/apt/sources.list.d/mixed.sources" || fail "mixed Deb822 source filtering was incorrect"
[ ! -e "${mixed_root}/etc/apt/sources.list.d/debian-usb-live.sources" ] || fail "fallback source was added despite usable network sources"
lap_has_network_source "${mixed_root}/etc/apt" || fail "preserved network sources were not recognized"
assert_idempotent_apt_tree "${mixed_root}" "${temp_root}/mixed-snapshot"

for suite in trixie forky; do
  root=${temp_root}/local-only-${suite}
  write_os_release "${root}" debian "${suite}"
  cat >"${root}/etc/apt/sources.list" <<'EOF_LOCAL_LIST'
# Local binary sources must be removed; disabled and source-only network entries survive.
deb file:/run/live/medium stable main
deb file:///run/live/medium stable main
deb file:/lib/live/mount/medium stable main
deb file:///lib/live/mount/medium stable main
deb file:/cdrom stable main
deb file:///cdrom stable main
deb cdrom:Debian stable main
deb [enabled=no] https://disabled.example.invalid/debian stable main
deb-src https://sources.example.invalid/debian stable main
EOF_LOCAL_LIST
  cat >"${root}/etc/apt/sources.list.d/local.sources" <<'EOF_LOCAL_SOURCES'
Types: deb
URIs: file:/run/live/medium file:///cdrom
Suites: stable
Components: main
EOF_LOCAL_SOURCES
  cat >"${root}/etc/apt/sources.list.d/disabled-network.sources" <<'EOF_DISABLED_NETWORK'
Types: deb
URIs: https://disabled-deb822.example.invalid/debian
Suites: stable
Components: main
Enabled: no
EOF_DISABLED_NETWORK

  lap_main "${root}"
  assert_official_source "${root}" "${suite}"
  [ ! -s "${root}/etc/apt/sources.list.d/local.sources" ] || fail "local-only Deb822 stanza survived for ${suite}"
  ! grep -E '^deb([[:space:]]|.*[[:space:]])(\[[^]]*\][[:space:]]+)?(file:/+|cdrom:)' "${root}/etc/apt/sources.list" >/dev/null || fail "active local legacy source survived for ${suite}"
  grep -F 'deb [enabled=no] https://disabled.example.invalid/debian stable main' "${root}/etc/apt/sources.list" >/dev/null || fail "disabled network source was not preserved for ${suite}"
  grep -F 'deb-src https://sources.example.invalid/debian stable main' "${root}/etc/apt/sources.list" >/dev/null || fail "source-only repository was not preserved for ${suite}"
  assert_idempotent_apt_tree "${root}" "${temp_root}/local-only-${suite}-snapshot"
done

cmdline_root=${temp_root}/cmdline
write_os_release "${cmdline_root}" debian trixie
printf '%s\n' 'deb file:/run/live/medium trixie main' >"${cmdline_root}/etc/apt/sources.list"
printf '%s\n' 'boot=live live_apt_suite=forky quiet' >"${cmdline_root}/cmdline"
LAP_CMDLINE_PATH="${cmdline_root}/cmdline" lap_main "${cmdline_root}"
assert_official_source "${cmdline_root}" forky

non_debian_root=${temp_root}/ubuntu
write_os_release "${non_debian_root}" ubuntu noble
printf '%s\n' 'deb file:/run/live/medium noble main' >"${non_debian_root}/etc/apt/sources.list"
cat >"${non_debian_root}/etc/apt/sources.list.d/vendor.sources" <<'EOF_NON_DEBIAN'
Types: deb
URIs: https://vendor.example.invalid/ubuntu
Suites: noble
Components: main
EOF_NON_DEBIAN
mkdir -p -- "${temp_root}/ubuntu-before"
cp -a -- "${non_debian_root}/etc/apt/." "${temp_root}/ubuntu-before/"
lap_main "${non_debian_root}"
diff -ru -- "${temp_root}/ubuntu-before" "${non_debian_root}/etc/apt" >/dev/null || fail "non-Debian APT sources were modified"

invalid_root=${temp_root}/invalid
write_os_release "${invalid_root}" debian 'forky;halt'
printf '%s\n' 'deb file:/run/live/medium stable main' >"${invalid_root}/etc/apt/sources.list"
cp -- "${invalid_root}/etc/apt/sources.list" "${temp_root}/invalid-sources.before"
if lap_main "${invalid_root}" >/dev/null 2>&1; then
  fail "unsafe Debian suite was accepted"
fi
cmp -- "${temp_root}/invalid-sources.before" "${invalid_root}/etc/apt/sources.list" || fail "invalid-suite failure changed APT sources"

if lap_main relative/path >/dev/null 2>&1; then
  fail "relative test root was accepted"
fi
