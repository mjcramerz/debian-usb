#!/bin/sh
# Run the real single-OS writer orchestration with all hardware operations mocked.
set -eu
repo_root=$(CDPATH='' cd -P "$(dirname -- "$0")/../.." && pwd -P)
DEBIAN_USB_WRITER_SOURCE_ONLY=1
export DEBIAN_USB_WRITER_SOURCE_ONLY
. "${repo_root}/scripts/write_usb.sh"
fixture=$(mktemp -d)
trap 'rm -rf -- "${fixture}"' 0
fail() { printf 'installer-layout: %s\n' "$*" >&2; exit 1; }
log() { printf '%s\n' "$*" >> "${fixture}/operations"; }
duw_validate_installer_profile_assets() { log "validate $1 $2"; }
duw_single_hd_media_preseeds() { log "explicit-preseed $1 $2 ${4:-preflight}"; printf '{"bytes":0}\n'; }
duw_stage_single_profile_preseed_tree() { log 'legacy-preseed'; }
duw_effective_esp_label() { printf 'ESPBOOT\n'; }
duw_effective_secure_boot_trust() { printf 'test\n'; }
duw_media_source_size_bytes() { printf '1048576\n'; }
duw_render_managed_grub() { DUSB_MANAGED_RENDER_OUTPUT='{}'; DUSB_MANAGED_GRUB_CFG='# test'; DUSB_MANAGED_ENTRY_COUNT=1; DUSB_MANAGED_MEDIA_CLASS=installer; }
duw_required_esp_size_mib_from_render_payload() { printf '512\n'; }
blockdev() { printf '17179869184\n'; }
duw_create_temp_root() { printf '%s/work\n' "${fixture}"; }
duw_set_cleanup_trap() { :; }
duw_profile_payload_fs_label() { printf 'PAYLOAD\n'; }
duw_profile_payload_partlabel() { printf 'PAYLOAD\n'; }
duw_device_pretty_name() { printf 'fake-device\n'; }
duw_grub_menu_mode_label() { printf 'custom\n'; }
duw_step() { :; }
duw_note() { :; }
duw_note_dynamic_esp_size() { :; }
duw_unmount_device_children() { :; }
duw_reset_partition_table_state() { :; }
duw_write_gpt_label() { log 'gpt'; }
parted() { ( IFS=' '; log "parted $*"; ); }
partprobe() { :; }
duw_settle_block_state() { :; }
duw_wait_for_partition_device() { printf '%s%s\n' "$1" "$2"; }
duw_make_vfat_filesystem() { log "vfat $1"; }
duw_make_ext4_filesystem() { log "ext4 $1"; }
sync() { :; }
duw_wait_for_filesystem_signature() { :; }
duw_partition_uuid() { printf 'test-uuid\n'; }
duw_mount_partition() { mkdir -p -- "$2"; log "mount $1 $3"; }
duw_prepare_secure_boot_identity() { :; }
duw_stage_secure_boot_support() { :; }
duw_note_secure_boot_requirement() { :; }
duw_stage_secure_boot_assets_from_render_payload() { :; }
duw_stage_iso_store_from_render_payload() { :; }
duw_install_multiboot_grub_bootloaders() { :; }
duw_require_mount_source() { :; }
duw_sign_grub_data_file() { :; }
duw_stage_secure_boot_manifest() { :; }
duw_finalize_temp_root() { :; }
DUSB_INSPECTED_MEDIA_CLASS=installer
for family in debian kali-linux; do
  for role in netinst netboot; do
    : > "${fixture}/operations"
    duw_build_single_iso_store_usb "${family}" /fake/source /dev/fake "${role}" '' '' '' '' "${repo_root}/configs/debian-usb.conf" 0 1 0 0
    grep -Fx 'vfat /dev/fake1' "${fixture}/operations" >/dev/null || fail 'installer ESP is not partition 1'
    grep -Fx 'ext4 /dev/fake2' "${fixture}/operations" >/dev/null || fail 'installer data is not partition 2'
    grep -F 'set 3 bios_grub on' "${fixture}/operations" >/dev/null || fail 'missing BIOS boot partition 3'
    grep -F 'explicit-preseed' "${fixture}/operations" >/dev/null || fail 'explicit profile staging was not considered'
    if grep -F 'legacy-preseed' "${fixture}/operations" >/dev/null; then fail 'installer used implicit legacy staging'; fi
  done
done
: > "${fixture}/operations"
DUSB_INSPECTED_MEDIA_CLASS=live
duw_build_single_iso_store_usb debian /fake/live.iso /dev/fake primary '' '' '' '' "${repo_root}/configs/debian-usb.conf" 0 1 0 0
grep -Fx 'vfat /dev/fake2' "${fixture}/operations" >/dev/null || fail 'legacy single Live ESP layout changed'
grep -Fx 'ext4 /dev/fake3' "${fixture}/operations" >/dev/null || fail 'legacy single Live data layout changed'
if grep -F 'explicit-preseed' "${fixture}/operations" >/dev/null; then fail 'Live entered installer profile staging'; fi
printf 'Installer profile partition/staging orchestration tests passed.\n'
