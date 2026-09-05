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

write_root_fixture() (
  root=$1
  distro_id=$2
  suite=$3

  mkdir -p -- "${root}/etc/apt/sources.list.d" "${root}/etc"
  cat >"${root}/etc/os-release" <<EOF_OS
ID=${distro_id}
VERSION_CODENAME=${suite}
EOF_OS
  cat >"${root}/etc/apt/sources.list" <<'EOF_LIST'
deb [trusted=yes] file:/run/live/medium stale main
deb https://packages.example.invalid/debian stale main
EOF_LIST
  printf '%s\n' 'deb https://mirror.example.invalid/debian stale main' >"${root}/etc/apt/sources.list.d/vendor.list"
  cat >"${root}/etc/apt/sources.list.d/vendor.sources" <<'EOF_SOURCES'
Types: deb
URIs: https://vendor.example.invalid/debian
Suites: stale
Components: main
EOF_SOURCES
)

assert_official_suite() (
  root=$1
  suite=$2
  source_file=${root}/etc/apt/sources.list.d/debian.sources

  [ ! -s "${root}/etc/apt/sources.list" ] || fail "legacy sources.list was not wiped for ${suite}"
  [ -f "${source_file}" ] || fail "official Deb822 source was not created for ${suite}"
  [ "$(stat -c '%a' "${source_file}")" = 644 ] || fail "official source mode is not 0644 for ${suite}"
  expected=$(cat <<EOF_EXPECTED
Types: deb
URIs: https://deb.debian.org/debian
Suites: ${suite}
Components: main contrib non-free non-free-firmware
Signed-By: /usr/share/keyrings/debian-archive-keyring.gpg
EOF_EXPECTED
)
  [ "$(cat "${source_file}")" = "${expected}" ] || fail "unexpected official source content for ${suite}"
  [ -z "$(find "${root}/etc/apt/sources.list.d" -maxdepth 1 -type f -name '*.list' -print -quit)" ] || fail "an old .list source survived for ${suite}"
  [ "$(find "${root}/etc/apt/sources.list.d" -maxdepth 1 -type f -name '*.sources' -printf '%f\n')" = debian.sources ] || fail "an old Deb822 source survived for ${suite}"
  ! grep -R -E 'file:/|example\.invalid|security\.debian\.org' "${root}/etc/apt" >/dev/null || fail "a non-selected APT source survived for ${suite}"
)

temp_root=$(mktemp -d)
trap 'rm -rf -- "${temp_root}"' 0 INT TERM

for suite in trixie forky; do
  root=${temp_root}/${suite}
  write_root_fixture "${root}" debian "${suite}"
  lap_main "${root}"
  assert_official_suite "${root}" "${suite}"

  cp -- "${root}/etc/apt/sources.list" "${root}/sources.list.once"
  cp -- "${root}/etc/apt/sources.list.d/debian.sources" "${root}/debian.sources.once"
  lap_main "${root}"
  cmp -- "${root}/sources.list.once" "${root}/etc/apt/sources.list" || fail "second run changed sources.list for ${suite}"
  cmp -- "${root}/debian.sources.once" "${root}/etc/apt/sources.list.d/debian.sources" || fail "second run changed Deb822 source for ${suite}"
done

cmdline_root=${temp_root}/cmdline
write_root_fixture "${cmdline_root}" debian trixie
printf '%s\n' 'boot=live live_apt_suite=forky quiet' >"${cmdline_root}/cmdline"
LAP_CMDLINE_PATH="${cmdline_root}/cmdline" lap_main "${cmdline_root}"
assert_official_suite "${cmdline_root}" forky

non_debian_root=${temp_root}/ubuntu
write_root_fixture "${non_debian_root}" ubuntu noble
cp -- "${non_debian_root}/etc/apt/sources.list" "${non_debian_root}/sources.list.before"
lap_main "${non_debian_root}"
cmp -- "${non_debian_root}/sources.list.before" "${non_debian_root}/etc/apt/sources.list" || fail "non-Debian root was modified"
[ -f "${non_debian_root}/etc/apt/sources.list.d/vendor.sources" ] || fail "non-Debian Deb822 source was removed"

invalid_root=${temp_root}/invalid
write_root_fixture "${invalid_root}" debian 'forky;halt'
cp -- "${invalid_root}/etc/apt/sources.list" "${invalid_root}/sources.list.before"
if lap_main "${invalid_root}" >/dev/null 2>&1; then
  fail "unsafe Debian suite was accepted"
fi
cmp -- "${invalid_root}/sources.list.before" "${invalid_root}/etc/apt/sources.list" || fail "invalid-suite failure changed APT sources"

if lap_main relative/path >/dev/null 2>&1; then
  fail "relative test root was accepted"
fi
