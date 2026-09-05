#!/bin/sh
set -eu
IFS=$(printf '\n\t')

repo_root=$(CDPATH='' cd -P "$(dirname -- "$0")/../.." && pwd -P)
writer_path=${DEBIAN_USB_WRITER_PATH:-${repo_root}/scripts/write_usb.sh}
DEBIAN_USB_WRITER_SOURCE_ONLY=1
export DEBIAN_USB_WRITER_SOURCE_ONLY
# shellcheck disable=SC1090
. "${writer_path}"

fail() {
  printf 'test_device_release: %s\n' "$*" >&2
  exit 1
}

temp_root=$(mktemp -d)
# shellcheck disable=SC2016
trap 'rm -rf -- "${temp_root}"' 0

separator=$(printf '\037')
list=first${separator}second${separator}third
duw_internal_list_contains "${list}" "${separator}" second || fail "internal list did not retain its second item"
[ "$(duw_internal_list_first "${list}" "${separator}")" = first ] || fail "internal list returned the wrong first item"
[ "$(duw_internal_list_remove "${list}" "${separator}" second)" = "first${separator}third" ] || fail "internal list removal corrupted remaining items"

(
  duw_mount_targets_for_block_path() {
    printf '%s\n' /run/media/test/MULTIBOOT /run/media/test/EXTRA
  }
  duw_active_swap_device() {
    printf '%s\n' /dev/fake-swap
  }
  duw_block_holders() {
    printf '%s\t%s\n' /dev/fake dm-test
  }
  duw_block_path_openers() {
    printf '%s\n' 42:test-process
  }
  summary=$(duw_block_path_state_summary /dev/fake)
  expected='mounted:/run/media/test/MULTIBOOT; mounted:/run/media/test/EXTRA; swap:/dev/fake-swap; holder:dm-test; opener:42:test-process'
  [ "${summary}" = "${expected}" ] || fail "block-state summary corrupted list separators: ${summary}"
)

(
  duw_have() {
    [ "$1" = fuser ]
  }
  fuser() {
    printf '%s\n' '101 202'
  }
  ps() {
    pid=
    previous=
    for argument in "$@"; do
      if [ "${previous}" = -p ]; then pid=${argument}; fi
      previous=${argument}
    done
    case ${pid} in
      101) printf '%s\n' file-manager ;;
      202) printf '%s\n' shell ;;
      *) return 1 ;;
    esac
  }
  openers=$(duw_mountpoint_openers /run/media/test/MULTIBOOT)
  expected=$(printf '%s\n' 101:file-manager 202:shell)
  [ "${openers}" = "${expected}" ] || fail "mountpoint opener diagnostics were incomplete: ${openers}"
)

(
  release_log=${temp_root}/transient-release.log
  scrub_log=${temp_root}/transient-scrub.log
  output_log=${temp_root}/transient-output.log
  : >"${release_log}"
  lsblk() {
    printf '%s\n' /dev/fake /dev/fake1
  }
  duw_udisks_unmount_device_children() { :; }
  duw_release_block_path() {
    call_count=$(wc -l <"${release_log}")
    printf '%s:%s\n' "$1" "${4:-missing}" >>"${release_log}"
    [ "${call_count}" -gt 0 ]
  }
  duw_device_in_use_reasons() {
    call_count=$(wc -l <"${release_log}")
    if [ "${call_count}" -le 2 ]; then
      printf '%s\n' '/dev/fake1 mounted on /run/media/test/MULTIBOOT'
    fi
  }
  duw_scrub_selected_device_signatures() {
    printf '%s\n' scrubbed >"${scrub_log}"
  }
  duw_reset_partition_table_state() { :; }
  duw_settle_block_state() { :; }
  sleep() { :; }

  if ! duw_unmount_device_children /dev/fake >"${output_log}" 2>&1; then
    cat -- "${output_log}" >&2
    fail "transient busy mount did not reach the second release attempt"
  fi
  [ "$(wc -l <"${release_log}")" -eq 4 ] || fail "transient busy mount did not perform exactly two bounded attempts"
  grep -Fq 'Selected-device release attempt 1/5' "${output_log}" || fail "transient busy retry was not reported"
  grep -Fq ':0' "${release_log}" || fail "selected-device retries did not request non-fatal release mode"
  [ -f "${scrub_log}" ] || fail "signatures were not scrubbed after the simulated device became idle"
)

(
  release_log=${temp_root}/persistent-release.log
  scrub_log=${temp_root}/persistent-scrub.log
  : >"${release_log}"
  lsblk() {
    printf '%s\n' /dev/fake /dev/fake1
  }
  duw_udisks_unmount_device_children() { :; }
  duw_release_block_path() {
    printf '%s:%s\n' "$1" "${4:-missing}" >>"${release_log}"
    return 1
  }
  duw_device_in_use_reasons() {
    printf '%s\n' \
      '/dev/fake1 mounted on /run/media/test/MULTIBOOT' \
      '/run/media/test/MULTIBOOT held open by 101:file-manager'
  }
  duw_scrub_selected_device_signatures() {
    printf '%s\n' scrubbed >"${scrub_log}"
  }
  duw_reset_partition_table_state() { :; }
  duw_settle_block_state() { :; }
  sleep() { :; }

  if output=$(duw_unmount_device_children /dev/fake 2>&1); then
    fail "persistent busy mount was accepted for destructive writes"
  fi
  [ "$(wc -l <"${release_log}")" -eq 10 ] || fail "persistent busy mount did not exhaust five bounded attempts"
  [ ! -e "${scrub_log}" ] || fail "signatures were scrubbed while the simulated device was still busy"
  printf '%s\n' "${output}" | grep -Fq 'held open by 101:file-manager' || fail "persistent busy failure omitted the holder"
  printf '%s\n' "${output}" | grep -Fq 'refusing a forced or lazy unmount before destructive writes' || fail "persistent busy failure did not preserve the safe unmount boundary"
)
