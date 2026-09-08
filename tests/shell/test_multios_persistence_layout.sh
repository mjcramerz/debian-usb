#!/bin/sh
# Exercise the actual writer allocator with real state files and mocked parted.
# Fake device names are never opened; all hardware commands are replaced.
set -eu
repo_root=$(CDPATH='' cd -P "$(dirname -- "$0")/../.." && pwd -P)
DEBIAN_USB_WRITER_SOURCE_ONLY=1
export DEBIAN_USB_WRITER_SOURCE_ONLY
. "${repo_root}/scripts/write_usb.sh"
fixture=$(mktemp -d)
trap 'rm -rf -- "${fixture}"' 0
fail() { printf 'persistence-layout: %s\n' "$*" >&2; exit 1; }
parted() ( IFS=' '; printf '%s\n' "$*" >> "${fixture}/parted.log"; )
run_case() (
  device=$1
  expected_count=$2
  shift 2
  state="${fixture}/state"
  rm -rf -- "${state}"
  mkdir -p "${state}"
  : > "${fixture}/parted.log"
  index=0
  for enabled in "$@"; do
    mkdir -p "${state}/items/${index}"
    duw_multios_state_set "${state}" "${index}" persist_flag "${enabled}"
    duw_multios_state_set "${state}" "${index}" persist_size 2
    duw_multios_state_set "${state}" "${index}" persist_partlabel "OS${index}-PERSIST"
    duw_multios_state_set "${state}" "${index}" persist_part ''
    index=$((index + 1))
  done
  duw_multios_create_persistence_partitions "${state}" "${index}" "${device}" 8192
  [ "$(wc -l < "${fixture}/parted.log" | tr -d ' ')" = "${expected_count}" ] || fail 'wrong partition count'
  index=0
  number=3
  cursor=8192
  for enabled in "$@"; do
    actual=$(duw_multios_state_get "${state}" "${index}" persist_part)
    if [ "${enabled}" = 1 ]; then
      expected=$(duw_partition_path "${device}" "${number}")
      [ "${actual}" = "${expected}" ] || fail "OS ${index}: expected ${expected}, got ${actual}"
      end=$((cursor + 2048))
      grep -Fx -- "-s ${device} unit MiB mkpart OS${index}-PERSIST ext4 ${cursor} ${end}" "${fixture}/parted.log" >/dev/null || fail 'partition boundaries have gaps or overlap'
      cursor=${end}
      number=$((number + 1))
    else
      [ -z "${actual}" ] || fail 'disabled source consumed a partition'
    fi
    index=$((index + 1))
  done
)
for device in /dev/fake /dev/nvme99n1 /dev/mmcblk99; do
  run_case "${device}" 0 0 0 0
  run_case "${device}" 1 0 0 1
  run_case "${device}" 3 1 0 1 0 1
  run_case "${device}" 3 0 1 1 1
  run_case "${device}" 4 1 1 1 1
done
printf 'Multi-OS persistence allocator: 15 hardware-free layout scenarios passed.\n'
