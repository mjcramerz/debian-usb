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
  printf 'test_multios_live_tools: %s\n' "$*" >&2
  exit 1
}

temp_root="$(mktemp -d)"
# Match literal variable references in the shipped writer.
# shellcheck disable=SC2016
trap 'rm -rf -- "${temp_root}"' 0
source_iso="${temp_root}/debian-live.iso"
helper="${temp_root}/fake-python-helper"
helper_log="${temp_root}/helper.log"
# Match literal variable references in the shipped writer.
# shellcheck disable=SC2016
printf 'iso-fixture\n' >"${source_iso}"

# Match literal variable references in the shipped writer.
# shellcheck disable=SC2016
cat >"${helper}" <<'EOF'
#!/bin/sh
set -eu
# Match literal variable references in the shipped writer.
# shellcheck disable=SC2016
printf '%s\n' "$*" >>"${FAKE_HELPER_LOG}"
[ "${1-}" = "remaster-live-tools-source" ] || exit 2
shift
source_iso=""
output_dir=""
profile=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --source-iso) source_iso="$2"; shift 2 ;;
    --output-dir) output_dir="$2"; shift 2 ;;
    --profile) profile="$2"; shift 2 ;;
    *) exit 2 ;;
  esac
done
mkdir -p -- "${output_dir}"
output_iso="${output_dir}/${profile}-admin-tools.iso"
cp -- "${source_iso}" "${output_iso}"
# Match literal variable references in the shipped writer.
# shellcheck disable=SC2016
printf '{"iso_path":"%s"}\n' "${output_iso}"
EOF
chmod 0755 "${helper}"
export DEBIAN_USB_PYTHON_HELPER="${helper}"
export FAKE_HELPER_LOG="${helper_log}"

metadata_root="${temp_root}/shared-data"
mkdir -p -- \
  "${metadata_root}/.disk" \
  "${metadata_root}/live" \
  "${metadata_root}/casper" \
  "${metadata_root}/install" \
  "${metadata_root}/install.amd" \
  "${metadata_root}/d-i" \
  "${metadata_root}/debian-installer"
# Match literal variable references in the shipped writer.
# shellcheck disable=SC2016
printf 'stale installer metadata\n' >"${metadata_root}/.disk/info"
# Match literal variable references in the shipped writer.
# shellcheck disable=SC2016
printf 'stale live rootfs\n' >"${metadata_root}/live/filesystem.squashfs"
duw_remove_shared_store_media_metadata "${metadata_root}"
for forbidden_root in .disk live casper install install.amd d-i debian-installer; do
  [ ! -e "${metadata_root}/${forbidden_root}" ] || \
    fail "shared ISO-store cleanup left root-level installation-media path: /${forbidden_root}"
done
if cleanup_output="$(duw_remove_shared_store_media_metadata / 2>&1)"; then
  fail "shared ISO-store metadata cleanup accepted /: ${cleanup_output}"
fi
# Match literal variable references in the shipped writer.
# shellcheck disable=SC2016
printf '%s\n' "${cleanup_output}" | grep -Fq 'refusing unsafe shared ISO-store metadata cleanup path' || \
  fail "unexpected unsafe metadata cleanup error: ${cleanup_output}"

netinst_bundle="${temp_root}/prepared-netinst"
staged_store="${temp_root}/staged-store"
mkdir -p -- "${netinst_bundle}/hd-media" "${netinst_bundle}/payload" "${staged_store}"
# Match literal variable references in the shipped writer.
# shellcheck disable=SC2016
printf 'downloaded hd-media kernel\n' >"${netinst_bundle}/hd-media/vmlinuz"
# Match literal variable references in the shipped writer.
# shellcheck disable=SC2016
printf 'downloaded hd-media initrd\n' >"${netinst_bundle}/hd-media/initrd.gz"
# Match literal variable references in the shipped writer.
# shellcheck disable=SC2016
printf 'opaque netinst ISO\n' >"${netinst_bundle}/payload/debian-netinst.iso"
# Match literal variable references in the shipped writer.
# shellcheck disable=SC2016
stage_payload="$(python3 - "${netinst_bundle}" <<'PY'
import json
import sys

bundle = sys.argv[1]
print(json.dumps({
    "iso_payloads": [
        {
            "source_path": f"{bundle}/payload/debian-netinst.iso",
            "target_path": "/debian-netinst/debian-netinst.iso",
        }
    ],
    "payload_boot_assets": [
        {
            "iso_path": bundle,
            "source_path": "/hd-media/vmlinuz",
            "target_path": "/debian-netinst/vmlinuz",
        },
        {
            "iso_path": bundle,
            "source_path": "/hd-media/initrd.gz",
            "target_path": "/debian-netinst/initrd.gz",
        },
    ],
    "payload_extra_assets": [],
}))
PY
)"
duw_lock_down_payload_dir() { :; }
duw_lock_down_payload_file() { :; }
duw_sign_grub_data_file() { :; }
duw_stage_iso_store_from_render_payload "${stage_payload}" "${staged_store}" "${staged_store}"
cmp -- "${netinst_bundle}/payload/debian-netinst.iso" "${staged_store}/debian-netinst/debian-netinst.iso" || \
  fail "shared ISO-store staging did not copy the opaque Netinst ISO"
cmp -- "${netinst_bundle}/hd-media/vmlinuz" "${staged_store}/debian-netinst/vmlinuz" || \
  fail "shared ISO-store staging did not copy the separate hd-media kernel"
cmp -- "${netinst_bundle}/hd-media/initrd.gz" "${staged_store}/debian-netinst/initrd.gz" || \
  fail "shared ISO-store staging did not copy the separate hd-media initrd"

prepared="$(duw_prepare_multios_live_tools_iso \
  debian \
  primary \
  live \
  "${source_iso}" \
  "${temp_root}/prepared-debian")"
[ "${prepared}" = "${temp_root}/prepared-debian/debian-admin-tools.iso" ] || fail "unexpected Debian prepared ISO path: ${prepared}"
cmp -- "${source_iso}" "${prepared}" || fail "prepared ISO did not originate from the source fixture"
# Match literal variable references in the shipped writer.
# shellcheck disable=SC2016
grep -Fq 'remaster-live-tools-source --profile debian' "${helper_log}" || fail "Debian remaster command was not issued"

before_count="$(wc -l <"${helper_log}")"
skipped="$(duw_prepare_multios_live_tools_iso debian netinst installer "${source_iso}" "${temp_root}/skip-netinst")"
[ "${skipped}" = "${source_iso}" ] || fail "netinst source was not preserved"
skipped="$(duw_prepare_multios_live_tools_iso tails primary live "${source_iso}" "${temp_root}/skip-tails")"
[ "${skipped}" = "${source_iso}" ] || fail "Tails source was not preserved"
after_count="$(wc -l <"${helper_log}")"
[ "${before_count}" = "${after_count}" ] || fail "unsupported or installer-only sources invoked the remaster helper"

outside_iso="${temp_root}/outside-admin-tools.iso"
outside_helper="${temp_root}/outside-python-helper"
cp -- "${source_iso}" "${outside_iso}"
# Match literal variable references in the shipped writer.
# shellcheck disable=SC2016
cat >"${outside_helper}" <<'EOF'
#!/bin/sh
set -eu
# Match literal variable references in the shipped writer.
# shellcheck disable=SC2016
printf '{"iso_path":"%s"}\n' "${FAKE_OUTSIDE_ISO}"
EOF
chmod 0755 "${outside_helper}"
export DEBIAN_USB_PYTHON_HELPER="${outside_helper}"
export FAKE_OUTSIDE_ISO="${outside_iso}"
if escaped_output="$(duw_prepare_multios_live_tools_iso debian primary live "${source_iso}" "${temp_root}/reject-outside" 2>&1)"; then
  fail "remaster helper accepted an ISO outside its managed output directory: ${escaped_output}"
fi
# Match literal variable references in the shipped writer.
# shellcheck disable=SC2016
printf '%s\n' "${escaped_output}" | grep -Fq 'outside its managed output directory' || \
  fail "unexpected error for remaster output outside the managed directory: ${escaped_output}"
export DEBIAN_USB_PYTHON_HELPER="${helper}"

render_helper="${temp_root}/fake-render-helper"
render_args="${temp_root}/render-args.log"
cat >"${render_helper}" <<'EOF'
#!/bin/sh
set -eu
printf '%s\n' "$@" >"${FAKE_RENDER_ARGS}"
printf '%s\n' '{"grub_cfg":"menu","entry_count":2,"media_class":"mixed"}'
EOF
chmod 0755 "${render_helper}"
export DEBIAN_USB_PYTHON_HELPER="${render_helper}"
export FAKE_RENDER_ARGS="${render_args}"
duw_render_multios_grub \
  /tmp/effective-plan.json \
  /tmp/debian-usb.conf \
  ESP-UUID \
  os1=DATA-UUID \
  os2=DATA-UUID
expected_render_args=$(cat <<'EOF'
render-multios-grub
--plan
/tmp/effective-plan.json
--config
/tmp/debian-usb.conf
--boot-assets-uuid
ESP-UUID
--payload-uuid
os1=DATA-UUID
--payload-uuid
os2=DATA-UUID
EOF
)
actual_render_args=$(cat -- "${render_args}")
[ "${actual_render_args}" = "${expected_render_args}" ] || \
  fail "Multi-OS renderer did not preserve one --payload-uuid argument per item"
export DEBIAN_USB_PYTHON_HELPER="${helper}"

export DEFAULT_LIVE_HOOKS=1
export DEBIAN_USB_LIVE_HOOKS_DIR="${repo_root}/config-hooks"
duw_raw_iso_has_live_hook_target() {
  return 0
}
hook_args=$(duw_collect_live_hook_map_args "${source_iso}" tails)
[ -z "${hook_args}" ] || fail "Tails raw ISO unexpectedly received live hooks"
hook_args=$(duw_collect_live_hook_map_args "${source_iso}" kali-linux)
[ -z "${hook_args}" ] || fail "Kali raw ISO unexpectedly received Debian Live hooks"
hook_args=$(duw_collect_live_hook_map_args "${source_iso}" debian)
[ -n "${hook_args}" ] || fail "Debian raw ISO did not receive live hooks"

DUSB_LIVE_TOOLS_PREPARED=1
export DUSB_LIVE_TOOLS_PREPARED
before_count="$(wc -l <"${helper_log}")"
skipped="$(duw_prepare_multios_live_tools_iso debian primary live "${source_iso}" "${temp_root}/already-prepared")"
after_count="$(wc -l <"${helper_log}")"
[ "${skipped}" = "${source_iso}" ] || fail "pre-remastered Live source path was changed"
[ "${before_count}" = "${after_count}" ] || fail "pre-remastered Live source was remastered twice"
unset DUSB_LIVE_TOOLS_PREPARED

extract_writer_function() (
  function_name=$1
  sed -n "/^${function_name}() [({]$/,/^[})]$/p" "${writer_path}"
)

shared_builder=$(extract_writer_function duw_build_multios_iso_store_usb)
raw_builder=$(extract_writer_function duw_build_multios_usb)
shared_update_builder=$(extract_writer_function duw_update_multios_shared_data_usb)
iso_payload_stager=$(extract_writer_function duw_stage_iso_store_payloads_from_render_payload)
builder_text=${raw_builder}
# Match literal variable references in the shipped function.
# Match literal variable references in the shipped writer.
# shellcheck disable=SC2016
cleanup_line=$(printf '%s\n' "${builder_text}" | grep -n -m1 'duw_set_cleanup_trap "${temp_root}"' | cut -d: -f1 || true)
# Match literal variable references in the shipped writer.
# shellcheck disable=SC2016
remaster_line=$(printf '%s\n' "${builder_text}" | grep -n -m1 'duw_prepare_multios_live_tools_iso' | cut -d: -f1 || true)
# Match literal variable references in the shipped writer.
# shellcheck disable=SC2016
device_line=$(printf '%s\n' "${builder_text}" | grep -n -m1 'duw_unmount_device_children' | cut -d: -f1 || true)
[ -n "${cleanup_line}" ] && [ -n "${remaster_line}" ] && [ -n "${device_line}" ] || \
  fail "raw Multi-OS builder is missing cleanup, remaster, or device-preparation integration"
[ "${cleanup_line}" -lt "${remaster_line}" ] || fail "raw Multi-OS builder does not install mount-aware cleanup before Live tool remastering"
[ "${remaster_line}" -lt "${device_line}" ] || fail "raw Multi-OS builder starts device mutation before Live tool remastering"

for builder_name in create update; do
  case ${builder_name} in
    create) builder_text=${shared_builder} ;;
    update) builder_text=${shared_update_builder} ;;
  esac
  # Match literal variable references in the shipped writer.
  # shellcheck disable=SC2016
  cleanup_line=$(printf '%s\n' "${builder_text}" | grep -n -m1 'duw_set_cleanup_trap "${temp_root}"' | cut -d: -f1 || true)
  # Match literal variable references in the shipped writer.
  # shellcheck disable=SC2016
  device_line=$(printf '%s\n' "${builder_text}" | grep -n -m1 'duw_unmount_device_children' | cut -d: -f1 || true)
  [ -n "${cleanup_line}" ] && [ -n "${device_line}" ] || fail "${builder_name} is missing cleanup or device-preparation integration"
  [ "${cleanup_line}" -lt "${device_line}" ] || fail "${builder_name} installs cleanup after device preparation"
  # Match literal variable references in the shipped writer.
  # shellcheck disable=SC2016
  ! printf '%s\n' "${builder_text}" | grep -Fq 'duw_prepare_multios_live_tools_iso' || fail "${builder_name} automatically remasters an ISO instead of preserving the selected ISO"
  # Match literal variable references in the shipped writer.
  # shellcheck disable=SC2016
  ! printf '%s\n' "${builder_text}" | grep -Fq 'duw_extract_iso_contents' || fail "${builder_name} extracts an ISO onto the shared ISO-store partition"
  # Match literal variable references in the shipped writer.
  # shellcheck disable=SC2016
  ! printf '%s\n' "${builder_text}" | grep -Fq 'duw_stage_live_hooks_on_medium' || fail "${builder_name} treats the shared ISO-store partition as a Live medium"
  # Match literal variable references in the shipped writer.
  # shellcheck disable=SC2016
  printf '%s\n' "${builder_text}" | grep -Fq 'duw_stage_iso_store_from_render_payload "${DUSB_MANAGED_RENDER_OUTPUT}" "${data_mount}" "${data_mount}"' || fail "${builder_name} does not stage both opaque ISOs and separate Netinst hd-media assets"
  # Match literal variable references in the shipped writer.
  # shellcheck disable=SC2016
  stage_line=$(printf '%s\n' "${builder_text}" | grep -n -m1 'duw_stage_iso_store_from_render_payload' | cut -d: -f1 || true)
  # Match literal variable references in the shipped writer.
  # shellcheck disable=SC2016
  final_metadata_check_line=$(printf '%s\n' "${builder_text}" | grep -n 'duw_remove_shared_store_media_metadata "${data_mount}"' | tail -n1 | cut -d: -f1 || true)
  [ -n "${stage_line}" ] && [ -n "${final_metadata_check_line}" ] || fail "${builder_name} is missing ISO-store staging or its final contamination check"
  [ "${stage_line}" -lt "${final_metadata_check_line}" ] || fail "${builder_name} does not recheck root-level installation-media contamination after staging"
done

# Match literal variable references in the shipped writer.
# shellcheck disable=SC2016
printf '%s\n' "${shared_builder}" | grep -Fq 'parted -s "${device}" unit MiB mkpart "${esp_label}" fat32 "${esp_start}" "${esp_end}"' || fail "shared Multi-OS builder does not create partition 1 as the FAT32 ESP"
# Match literal variable references in the shipped writer.
# shellcheck disable=SC2016
printf '%s\n' "${shared_builder}" | grep -Fq 'parted -s "${device}" unit MiB mkpart "$(duw_shared_data_partlabel)" ext4 "${data_start}"' || fail "shared Multi-OS builder does not create partition 2 as the ISO store"
# Match literal variable references in the shipped writer.
# shellcheck disable=SC2016
printf '%s\n' "${shared_builder}" | grep -Fq 'partition_number=3' || fail "shared Multi-OS builder does not begin persistence allocation at partition 3"
# Match literal variable references in the shipped writer.
# shellcheck disable=SC2016
printf '%s\n' "${shared_builder}" | grep -Fq 'duw_multios_state_set "${state_dir}" "${index}" persist_part "${persist_part}"' || fail "shared Multi-OS builder does not record a distinct partition per persistent Live item"
# Match literal variable references in the shipped writer.
# shellcheck disable=SC2016
printf '%s\n' "${iso_payload_stager}" | grep -Fq 'duw_sign_grub_data_file "${destination}"' || fail "shared ISO payloads are not signed before GRUB loopback verification"
# Match literal variable references in the shipped writer.
# shellcheck disable=SC2016
metadata_checks=$(printf '%s\n' "${shared_builder}" | grep -Fc 'duw_remove_shared_store_media_metadata "${data_mount}"' || true)
[ "${metadata_checks}" -ge 2 ] || fail "shared Multi-OS creation does not reject root-level installation-media paths before and after staging"
# Match literal variable references in the shipped writer.
# shellcheck disable=SC2016
metadata_checks=$(printf '%s\n' "${shared_update_builder}" | grep -Fc 'duw_remove_shared_store_media_metadata "${data_mount}"' || true)
[ "${metadata_checks}" -ge 2 ] || fail "shared Multi-OS update does not reject root-level installation-media paths before and after staging"
# Match literal variable references in the shipped writer.
# shellcheck disable=SC2016
printf '%s\n' "${shared_update_builder}" | grep -Fq '/tails-live /live' || fail "shared Multi-OS update does not remove the obsolete root-level Live-medium hook tree"

busy_root="${temp_root}/busy-root"
busy_mount="${busy_root}/sys"
cleanup_rm_log="${temp_root}/cleanup-rm.log"
(
  # shellcheck disable=SC2329  # Invoked indirectly by the sourced cleanup function.
  findmnt() {
    # Match literal variable references in the shipped writer.
    # shellcheck disable=SC2016
    printf '%s\n' "${busy_mount}"
  }
  # shellcheck disable=SC2329  # Invoked indirectly by the sourced cleanup function.
  mountpoint() {
    return 0
  }
  # shellcheck disable=SC2329  # Invoked indirectly by the sourced cleanup function.
  umount() {
    return 1
  }
  # shellcheck disable=SC2329  # Invoked indirectly by the sourced cleanup function.
  rm() {
    # Match literal variable references in the shipped writer.
    # shellcheck disable=SC2016
    printf '%s\n' "$*" >>"${cleanup_rm_log}"
  }
  duw_cleanup_temp_root_best_effort "${busy_root}" 2>/dev/null
)
[ ! -e "${cleanup_rm_log}" ] || fail "best-effort cleanup recursively removed a workspace with a live mount"
