#!/bin/sh
set -eu
IFS=$(printf '\n\t')
PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin${PATH:+:${PATH}}"
export PATH

duw_report_command_failure() {
  exit_code=${1:-1}
  line_no=${2:-unknown}
  command=${3:-unknown}
  printf 'write_usb.sh: command failed at line %s (exit %s): %s\n' "${line_no}" "${exit_code}" "${command}" >&2
  exit "${exit_code}"
}

duw_die() {
  printf 'write_usb.sh: %s\n' "$*" >&2
  exit 1
}

duw_note() (
  printf '%s\n' "$*"
)

duw_step() (
  printf 'write_usb.sh: %s\n' "$*"
)

duw_grub_menu_mode_label() (
  use_custom_grub_menu=${1:-0}
  preserve_upstream_grub_entries=${2:-0}
  if [ "${use_custom_grub_menu}" = 1 ] && [ "${preserve_upstream_grub_entries}" = 1 ]; then
    printf 'custom grouped menu + preserved upstream-managed entries\n'
  elif [ "${use_custom_grub_menu}" = 1 ]; then
    printf 'custom grouped menu\n'
  else
    printf 'preserved upstream-managed menu\n'
  fi
)

duw_strip_source_boot_configs() (
  mount_root=$1
  for relative_path in \
    boot/grub/grub.cfg \
    boot/grub/loopback.cfg \
    boot/grub/install_start.cfg \
    boot/grub/install.cfg \
    EFI/BOOT/grub.cfg \
    isolinux/txt.cfg \
    isolinux/live.cfg \
    isolinux/install.cfg \
    syslinux/live.cfg \
    syslinux.cfg
  do
    target_path=${mount_root}/${relative_path}
    if [ -f "${target_path}" ]; then
      rm -f -- "${target_path}" || duw_die "failed to remove source boot config from custom payload: ${target_path}"
    fi
  done
)

duw_work_root() (
  override=${DEBIAN_USB_WORK_DIR:-}
  if [ -n "${override}" ]; then
    set -- "${override}"
  else
    set -- /data/tmp/debian-usb
    if [ -n "${XDG_RUNTIME_DIR:-}" ]; then
      set -- "$@" "${XDG_RUNTIME_DIR%/}/debian-usb"
    fi
    set -- "$@" "${TMPDIR:-/tmp}/debian-usb-$(id -u)"
  fi

  for candidate in "$@"; do
    if install -d -m 0700 -- "${candidate}" 2>/dev/null; then
      if probe=$(mktemp -d -p "${candidate}" .probe.XXXXXX 2>/dev/null); then
        rm -rf -- "${probe}"
        printf '%s\n' "${candidate}"
        return 0
      fi
    fi
    if [ -n "${override}" ]; then
      duw_die "failed to use work directory override: ${override}"
    fi
  done
  duw_die "failed to resolve a writable temporary work directory"
)

duw_create_temp_root() (
  prefix=$1
  work_root=$(duw_work_root)
  mktemp -d -p "${work_root}" "${prefix}.XXXXXX" || duw_die "failed to create temporary work directory in ${work_root}"
)

duw_read_hidden_value() (
  prompt=$1
  input=
  carriage_return=$(printf '\n')

  if [ -r /dev/tty ] && [ -w /dev/tty ]; then
    printf '%s: ' "${prompt}" >/dev/tty
    stty -echo </dev/tty || duw_die "failed to disable terminal echo"
    if ! IFS= read -r input </dev/tty; then
      stty echo </dev/tty || true
      printf '\n' >/dev/tty
      duw_die "failed to read interactive input from /dev/tty"
    fi
    stty echo </dev/tty || duw_die "failed to restore terminal echo"
    printf '\n' >/dev/tty
  elif [ -t 0 ]; then
    printf '%s: ' "${prompt}" >&2
    stty -echo || duw_die "failed to disable terminal echo"
    if ! IFS= read -r input; then
      stty echo || true
      printf '\n' >&2
      duw_die "failed to read interactive input"
    fi
    stty echo || duw_die "failed to restore terminal echo"
    printf '\n' >&2
  else
    duw_die "encrypted persistence requires an interactive terminal for passphrase entry"
  fi

  case ${input} in *"${carriage_return}"*) duw_die "passphrase input must be single-line" ;; esac
  printf '%s' "${input}"
)

duw_prompt_note() (
  message=$1
  if [ -w /dev/tty ]; then
    printf '%s\n' "${message}" >/dev/tty
  else
    printf '%s\n' "${message}" >&2
  fi
)

duw_collect_luks_passphrase_file() (
  label=$1
  output_file=$2
  while :; do
    first=$(duw_read_hidden_value "Enter passphrase for ${label}")
    if [ -z "${first}" ]; then
      duw_prompt_note "Passphrase cannot be empty. Try again."
      continue
    fi
    second=$(duw_read_hidden_value "Verify passphrase for ${label}")
    if [ "${first}" != "${second}" ]; then
      duw_prompt_note "Passphrases do not match. Try again."
      continue
    fi
    umask 077
    printf '%s' "${first}" >"${output_file}"
    chmod 0600 "${output_file}" || duw_die "failed to secure passphrase file: ${output_file}"
    return 0
  done
)

duw_mount_targets_under_path() (
  path=$1
  (findmnt -rn --output TARGET 2>/dev/null || true) |
    while IFS= read -r target; do
      [ -n "${target}" ] || continue
      case ${target} in "${path}"|"${path}"/*) printf '%s\n' "${target}" ;; esac
    done
)

duw_cleanup_temp_root_best_effort() (
  temp_root=$1
  cd / >/dev/null 2>&1 || true
  mountpoints=$(duw_mount_targets_under_path "${temp_root}")
  if [ -n "${mountpoints}" ]; then
    printf '%s\n' "${mountpoints}" | awk '{ paths[NR]=$0 } END { for (i=NR; i>0; i--) print paths[i] }' |
      while IFS= read -r mount_path; do
        [ -n "${mount_path}" ] || continue
        mountpoint -q -- "${mount_path}" || continue
        umount -- "${mount_path}" >/dev/null 2>&1 || true
      done
  fi
  mountpoints=$(duw_mount_targets_under_path "${temp_root}")
  if [ -n "${mountpoints}" ]; then
    summary=$(printf '%s' "${mountpoints}" | tr '\n' ' ')
    duw_note "Leaving temporary workspace in place because mounts remain under ${temp_root}: ${summary}" >&2
    return 0
  fi
  rm -rf -- "${temp_root}" >/dev/null 2>&1 || true
)

duw_cleanup_temp_root_strict() (
  temp_root=$1
  attempts=${2:-5}
  delay_s=${3:-0.5}
  try=1
  cd / >/dev/null 2>&1 || true
  while [ "${try}" -le "${attempts}" ]; do
    mountpoints=$(duw_mount_targets_under_path "${temp_root}")
    if [ -z "${mountpoints}" ]; then
      rm -rf -- "${temp_root}" || duw_die "failed to remove temporary workspace: ${temp_root}"
      return 0
    fi
    printf '%s\n' "${mountpoints}" | awk '{ paths[NR]=$0 } END { for (i=NR; i>0; i--) print paths[i] }' |
      while IFS= read -r mount_path; do
        [ -n "${mount_path}" ] || continue
        mountpoint -q -- "${mount_path}" || continue
        if ! umount -- "${mount_path}" >/dev/null 2>&1; then
          duw_note "Cleanup retry ${try}/${attempts} could not unmount ${mount_path}; waiting for mount holders to release"
        fi
      done
    sync
    duw_settle_block_state
    if [ "${try}" -lt "${attempts}" ]; then sleep "${delay_s}"; fi
    try=$((try + 1))
  done
  mountpoints=$(duw_mount_targets_under_path "${temp_root}")
  if [ -n "${mountpoints}" ]; then
    summary=$(printf '%s' "${mountpoints}" | tr '\n' ' ')
    duw_die "failed to clean up temporary mount tree under ${temp_root}: ${summary}"
  fi
  rm -rf -- "${temp_root}" || duw_die "failed to remove temporary workspace: ${temp_root}"
)

duw_finalize_temp_root() (
  temp_root=$1
  duw_cleanup_temp_root_strict "${temp_root}"
  trap - 0
)

duw_set_cleanup_trap() {
  DUSB_CLEANUP_TEMP_ROOT=$1
  trap 'duw_cleanup_temp_root_best_effort "${DUSB_CLEANUP_TEMP_ROOT}"' 0
}

duw_load_install_env() {
  _duw_script_dir=$(CDPATH='' cd -P "$(dirname "$0")" && pwd -P)
  _duw_env_file=
  if [ -r "${_duw_script_dir}/../configs/install.env" ]; then
    _duw_env_file=${_duw_script_dir}/../configs/install.env
  elif [ -r "${_duw_script_dir}/install.env" ]; then
    _duw_env_file=${_duw_script_dir}/install.env
  fi
  if [ -n "${_duw_env_file}" ]; then
    # shellcheck disable=SC1090
    . "${_duw_env_file}"
  fi
}

duw_init_custom_grub_defaults() {
  : "${CUSTOM_GRUB_PAYLOAD_PRESEED_DIR:=preseed}"
}

duw_validate_preseed_source_dir() (
  source_dir=$1
  [ -n "${source_dir}" ] || return 0
  [ -d "${source_dir}" ] || duw_die "offline preseed source directory is not a directory: ${source_dir}"
)

duw_preseed_target_path() (
  printf '/%s\n' "${CUSTOM_GRUB_PAYLOAD_PRESEED_DIR#/}"
)

duw_preseed_source_dir() (
  source_dir=$1
  if [ -n "${source_dir}" ]; then printf '%s\n' "${source_dir}"; return 0; fi
  if [ -n "${DEBIAN_USB_PRESEED_DIR:-}" ]; then printf '%s\n' "${DEBIAN_USB_PRESEED_DIR}"; return 0; fi
  printf '\n'
)

duw_stage_preseed_tree() (
  mount_root=$1
  requested_source_dir=${2:-}
  source_dir=$(duw_preseed_source_dir "${requested_source_dir}")
  [ -n "${source_dir}" ] || return 0
  duw_validate_preseed_source_dir "${source_dir}"
  target_dir=${mount_root}$(duw_preseed_target_path)
  rm -rf -- "${target_dir}" || duw_die "failed to replace staged preseed directory: ${target_dir}"
  mkdir -p -- "${target_dir}" || duw_die "failed to create staged preseed directory: ${target_dir}"
  cp -a -- "${source_dir}/." "${target_dir}/" || duw_die "failed to stage managed preseed tree from ${source_dir}"
)

duw_preseed_dir_size_mib() (
  requested_source_dir=${1:-}
  source_dir=$(duw_preseed_source_dir "${requested_source_dir}")
  if [ -z "${source_dir}" ]; then printf '0\n'; return 0; fi
  duw_validate_preseed_source_dir "${source_dir}"
  output=$(du -sm --apparent-size -- "${source_dir}" 2>/dev/null) || duw_die "failed to measure offline preseed directory: ${source_dir}"
  output=${output%%[[:space:]]*}
  case ${output} in ''|*[!0-9]*) duw_die "unexpected size output for offline preseed directory ${source_dir}: ${output}" ;; esac
  printf '%s\n' "${output}"
)

duw_directory_size_mib_or_zero() (
  source_dir=$1
  if [ -z "${source_dir}" ] || [ ! -d "${source_dir}" ]; then printf '0\n'; return 0; fi
  output=$(du -sm --apparent-size -- "${source_dir}" 2>/dev/null) || duw_die "failed to measure directory: ${source_dir}"
  output=${output%%[[:space:]]*}
  case ${output} in ''|*[!0-9]*) duw_die "unexpected size output for directory ${source_dir}: ${output}" ;; esac
  printf '%s\n' "${output}"
)

duw_profile_preseed_size_mib() (
  profile=$1
  requested_source_dir=${2:-}
  if [ -n "${requested_source_dir}" ]; then
    duw_validate_preseed_source_dir "${requested_source_dir}"
    duw_directory_size_mib_or_zero "${requested_source_dir}"
    return 0
  fi
  source_dir=$(duw_profile_preseed_host_path "${profile}")
  if [ -z "${source_dir}" ]; then printf '0\n'; return 0; fi
  duw_directory_size_mib_or_zero "${source_dir}"
)

duw_rebuild_iso_with_preseed_tree() (
  source_iso=$1
  requested_source_dir=$2
  output_iso=$3
  output_volid=${4:-}
  source_dir=$(duw_preseed_source_dir "${requested_source_dir}")
  [ -n "${source_dir}" ] || duw_die "offline preseed source directory is required for ISO rebuild"
  duw_validate_preseed_source_dir "${source_dir}"
  rm -f -- "${output_iso}" || duw_die "failed to remove previous rebuilt ISO path: ${output_iso}"
  duw_step "Rebuilding ${source_iso} with offline preseed content from ${source_dir} at $(duw_preseed_target_path)"
  set -- -indev "${source_iso}" -outdev "${output_iso}" -boot_image any replay
  if [ -n "${output_volid}" ]; then set -- "$@" -volid "${output_volid}"; fi
  set -- "$@" -map "${source_dir}" "$(duw_preseed_target_path)" -commit -end
  if output=$(xorriso "$@" 2>&1); then
    return 0
  else
    exit_code=$?
    duw_die "failed to rebuild ${source_iso} with offline preseed content (exit ${exit_code}): ${output}"
  fi
)

duw_validate_grub_token() (
  value=$1
  label=$2
  [ -n "${value}" ] && [ "${#value}" -le 128 ] || duw_die "invalid ${label} for GRUB config: ${value}"
  case ${value} in *[!A-Za-z0-9._:-]*) duw_die "invalid ${label} for GRUB config: ${value}" ;; esac
)

duw_raw_iso_managed_uefi_overhead_mib() (
  printf '64\n'
)

duw_esp_base_reserve_mib() (
  printf '128\n'
)

duw_esp_min_size_mib() (
  printf '512\n'
)

duw_round_mib_up() (
  value=$1
  quantum=${2:-64}
  printf '%s\n' "$(( ((value + quantum - 1) / quantum) * quantum ))"
)

duw_write_managed_uefi_redirect_grub_cfg() (
  esp_uuid=$1
  output_cfg=$2
  include_updatevars=0
  duw_validate_grub_token "${esp_uuid}" "ESP UUID"
  managed_menu_title=$(duw_grub_redirect_entry_title managed-usb-menu "Managed USB Installer Menu")
  if duw_updatevars_available; then
    include_updatevars=1
    updatevars_titles=$(duw_grub_updatevars_titles)
    old_ifs=${IFS}
    IFS=$(printf '')
    set -f
    # Intentional splitting: the helper emits exactly three unit-separated titles.
    # shellcheck disable=SC2086
    set -- ${updatevars_titles}
    set +f
    IFS=${old_ifs}
    updatevars_title=${1:-}
    updatevars_user_title=${2:-}
    updatevars_setup_title=${3:-}
  fi
  uefi_title=$(duw_grub_static_entry_title uefi "UEFI Settings")
  mkdir -p -- "$(dirname "${output_cfg}")" || duw_die "failed to create GRUB redirect config directory: ${output_cfg}"
  cat >"${output_cfg}" <<EOF
set timeout=0
set default=0

insmod part_gpt
insmod part_msdos
insmod search_fs_uuid
insmod fat
insmod chain

search --no-floppy --fs-uuid --set=boot_root ${esp_uuid}
configfile (\${boot_root})/boot/grub/grub.cfg

menuentry "${managed_menu_title}" {
    search --no-floppy --fs-uuid --set=boot_root ${esp_uuid}
    configfile (\${boot_root})/boot/grub/grub.cfg
}

menuentry "${uefi_title}" {
    fwsetup
}
EOF
  if [ "${include_updatevars}" -eq 1 ]; then
    cat >>"${output_cfg}" <<EOF

submenu "${updatevars_title}" {
    menuentry "${updatevars_user_title}" {
        search --no-floppy --fs-uuid --set=root ${esp_uuid}
        chainloader /EFI/debian-usb/updatevars/UpdateVars.efi -a db /secureboot/db.auth
        boot
    }

    menuentry "${updatevars_setup_title}" {
        search --no-floppy --fs-uuid --set=root ${esp_uuid}
        chainloader /EFI/debian-usb/updatevars/UpdateVars.efi -a -e db /secureboot/db.esl
        boot
    }
}
EOF
  fi
)

duw_rebuild_raw_iso_with_managed_uefi_redirect() (
  source_iso=$1
  esp_uuid=$2
  output_iso=$3
  output_volid=${4:-}
  profile=${5:-}
  [ -f "${source_iso}" ] && [ -s "${source_iso}" ] || duw_die "raw ISO source is missing or empty: ${source_iso}"
  duw_validate_grub_token "${esp_uuid}" "ESP UUID"
  rm -f -- "${output_iso}" || duw_die "failed to remove previous managed UEFI raw ISO path: ${output_iso}"
  redirect_cfg=${output_iso}.managed-uefi-grub.cfg
  duw_write_managed_uefi_redirect_grub_cfg "${esp_uuid}" "${redirect_cfg}"
  patch_root=$(duw_create_temp_root raw-iso-initrd-patch)

  set -- -indev "${source_iso}" -outdev "${output_iso}" -boot_image any replay
  if [ -n "${output_volid}" ]; then set -- "$@" -volid "${output_volid}"; fi
  set -- "$@" \
    -map "${redirect_cfg}" /boot/grub/grub.cfg \
    -map "${redirect_cfg}" /EFI/BOOT/grub.cfg \
    -map "${redirect_cfg}" /EFI/boot/grub.cfg
  for member_path in \
    /install.amd/initrd.gz \
    /install.amd/gtk/initrd.gz \
    /install/initrd.gz \
    /install/gtk/initrd.gz
  do
    duw_iso_member_exists "${source_iso}" "${member_path}" || continue
    patched_member=${patch_root}${member_path}
    duw_extract_iso_member "${source_iso}" "${member_path}" "${patched_member}"
    duw_initrd_has_cdrom_detect_postinst "${patched_member}" || continue
    duw_step "Patching raw ISO installer initrd cdrom-detect media binding at ${member_path}"
    duw_patch_installer_initrd_cdrom_detect "${patched_member}"
    set -- "$@" -map "${patched_member}" "${member_path}"
  done
  live_hooks_dir=$(duw_collect_live_hook_map_args "${source_iso}" "${profile}")
  if [ -n "${live_hooks_dir}" ]; then
    set -- "$@" -map "${live_hooks_dir}" /live/config-hooks \
      -chmod 0755 /live/config-hooks/0500-apt-live-medium.sh /live/config-hooks/1000-network-wifi.sh --
  fi
  set -- "$@" -commit -end
  duw_step "Embedding managed UEFI menu redirect into raw ISO payload ${source_iso}"
  if output=$(xorriso "$@" 2>&1); then
    rm -f -- "${redirect_cfg}" || true
    rm -rf -- "${patch_root}" || true
    return 0
  else
    exit_code=$?
    rm -f -- "${redirect_cfg}" || true
    rm -rf -- "${patch_root}" || true
    duw_die "failed to embed managed UEFI menu redirect into ${source_iso} (exit ${exit_code}): ${output}"
  fi
)

duw_live_hooks_enabled() {
  [ "${DEFAULT_LIVE_HOOKS:-0}" = 1 ]
}

duw_live_hooks_source_dir() (
  if [ -n "${DEBIAN_USB_LIVE_HOOKS_DIR:-}" ]; then
    [ -d "${DEBIAN_USB_LIVE_HOOKS_DIR}" ] || duw_die "DEBIAN_USB_LIVE_HOOKS_DIR is not a directory: ${DEBIAN_USB_LIVE_HOOKS_DIR}"
    printf '%s\n' "${DEBIAN_USB_LIVE_HOOKS_DIR}"
    return 0
  fi
  script_dir=$(CDPATH='' cd -P "$(dirname "$0")" && pwd -P)
  for candidate in "${script_dir}/../config-hooks" "${script_dir}/config-hooks"; do
    if [ -d "${candidate}" ]; then printf '%s\n' "${candidate}"; return 0; fi
  done
  duw_die "DEFAULT_LIVE_HOOKS=1 but no live config hook directory was found"
)

duw_raw_iso_has_live_hook_target() (
  source_iso=$1
  duw_iso_member_exists "${source_iso}" /live/filesystem.squashfs || \
    duw_iso_member_exists "${source_iso}" /live/vmlinuz || \
    duw_iso_member_exists "${source_iso}" /live/initrd.img
)

duw_collect_live_hook_map_args() (
  source_iso=$1
  profile=$2
  duw_live_hooks_enabled || return 0
  case ${profile} in debian) ;; *) return 0 ;; esac
  if ! duw_raw_iso_has_live_hook_target "${source_iso}"; then
    duw_note "DEFAULT_LIVE_HOOKS=1 but ${source_iso} does not expose a /live payload; skipping live config hooks" >&2
    return 0
  fi
  hooks_dir=$(duw_live_hooks_source_dir)
  apt_hook_path=${hooks_dir}/0500-apt-live-medium.sh
  wifi_hook_path=${hooks_dir}/1000-network-wifi.sh
  [ -f "${apt_hook_path}" ] && [ -r "${apt_hook_path}" ] || duw_die "missing or unreadable live APT hook: ${apt_hook_path}"
  [ -f "${wifi_hook_path}" ] && [ -r "${wifi_hook_path}" ] || duw_die "missing or unreadable live Wi-Fi hook: ${wifi_hook_path}"
  duw_step "Embedding live config hooks into raw ISO payload under /live/config-hooks" >&2
  printf '%s\n' "${hooks_dir}"
)

duw_have() (
  command -v "$1" >/dev/null 2>&1
)

duw_internal_list_contains() (
  list=$1
  separator=$2
  expected=$3
  [ -n "${list}" ] || return 1
  IFS=${separator}
  set -f
  for item in ${list}; do
    [ "${item}" = "${expected}" ] && return 0
  done
  return 1
)

duw_internal_list_remove() (
  list=$1
  separator=$2
  removed=$3
  result=
  [ -n "${list}" ] || return 0
  IFS=${separator}
  set -f
  for item in ${list}; do
    [ "${item}" = "${removed}" ] && continue
    result=${result}${result:+${separator}}${item}
  done
  printf '%s' "${result}"
)

duw_internal_list_first() (
  list=$1
  separator=$2
  [ -n "${list}" ] || return 0
  IFS=${separator}
  set -f
  for item in ${list}; do
    printf '%s\n' "${item}"
    return 0
  done
)

duw_require_root() (
  [ "$(id -u)" -eq 0 ] || duw_die "root privileges are required to write USB devices"
)

duw_python_helper_path() (
  if [ -n "${DEBIAN_USB_PYTHON_HELPER:-}" ] && [ -f "${DEBIAN_USB_PYTHON_HELPER}" ]; then printf '%s\n' "${DEBIAN_USB_PYTHON_HELPER}"; return 0; fi
  script_dir=$(CDPATH='' cd -P "$(dirname "$0")" && pwd -P)
  for candidate in "${script_dir}/debian-usb-python" "${script_dir}/../scripts/debian-usb-python"; do
    if [ -f "${candidate}" ]; then printf '%s\n' "${candidate}"; return 0; fi
  done
  duw_die "unable to locate the debian-usb-python helper"
)

duw_run_python_helper() (
  helper=$1
  shift
  updatevars_path=$(duw_updatevars_source_path 2>/dev/null || true)
  if [ -x "${helper}" ]; then
    DEBIAN_USB_UPDATEVARS_EFI_PATH=${updatevars_path} "${helper}" "$@"
  else
    DEBIAN_USB_UPDATEVARS_EFI_PATH=${updatevars_path} sh "${helper}" "$@"
  fi
)

duw_prepare_multios_live_tools_iso() (
  profile=$1
  source_role=$2
  media_class=$3
  source_iso=$4
  output_dir=$5
  if [ "${DUSB_LIVE_TOOLS_PREPARED:-0}" = 1 ]; then printf '%s\n' "${source_iso}"; return 0; fi
  if [ "${source_role}" != primary ]; then printf '%s\n' "${source_iso}"; return 0; fi
  case ${media_class} in live|hybrid) ;; *) printf '%s\n' "${source_iso}"; return 0 ;; esac
  case ${profile} in debian|kali-linux|ubuntu-desktop) ;; *) printf '%s\n' "${source_iso}"; return 0 ;; esac
  case ${source_iso} in /*) ;; *) duw_die "Live administration tool remaster source must be an absolute non-empty ISO path: ${source_iso}" ;; esac
  [ -f "${source_iso}" ] && [ -s "${source_iso}" ] || duw_die "Live administration tool remaster source must be an absolute non-empty ISO path: ${source_iso}"
  case ${output_dir} in /*) ;; *) duw_die "Live administration tool remaster output directory must be absolute: ${output_dir}" ;; esac
  install -d -m 0700 -- "${output_dir}" || duw_die "failed to create Live administration tool remaster directory: ${output_dir}"
  helper=$(duw_python_helper_path)
  duw_step "Remastering ${profile} Live payload with the repo-managed recovery and administration tool profile" >&2
  output=$(duw_run_python_helper "${helper}" remaster-live-tools-source --profile "${profile}" --source-iso "${source_iso}" --output-dir "${output_dir}") || duw_die "failed to remaster ${profile} Live payload with administration tools"
  prepared_iso=$(duw_json_field "${output}" iso_path)
  [ -n "${prepared_iso}" ] && [ -f "${prepared_iso}" ] && [ -s "${prepared_iso}" ] || duw_die "Live administration tool remaster did not produce a non-empty ISO: ${prepared_iso:-<empty>}"
  resolved_output_dir=$(duw_resolved_path "${output_dir}")
  resolved_prepared_iso=$(duw_resolved_path "${prepared_iso}")
  case ${resolved_prepared_iso} in "${resolved_output_dir}"/*) ;; *) duw_die "Live administration tool remaster returned an ISO outside its managed output directory: ${prepared_iso}" ;; esac
  printf '%s\n' "${resolved_prepared_iso}"
)

duw_updatevars_source_path() (
  if [ "${DEBIAN_USB_UPDATEVARS_EFI_PATH+x}" = x ]; then
    candidate=${DEBIAN_USB_UPDATEVARS_EFI_PATH}
    [ -n "${candidate}" ] && [ -f "${candidate}" ] && [ -s "${candidate}" ] || return 1
    printf '%s\n' "${candidate}"
    return 0
  fi
  for candidate in /usr/lib/efitools/x86_64-linux-gnu/UpdateVars.efi /usr/lib/efitools/UpdateVars.efi /usr/share/efitools/efi/UpdateVars.efi; do
    if [ -f "${candidate}" ] && [ -s "${candidate}" ]; then printf '%s\n' "${candidate}"; return 0; fi
  done
  return 1
)

duw_updatevars_available() (
  path=$(duw_updatevars_source_path 2>/dev/null) || return 1
  [ -n "${path}" ]
)

duw_grub_spec_dir() (
  if [ -n "${DEBIAN_USB_SPEC_DIR:-}" ] && [ -d "${DEBIAN_USB_SPEC_DIR}" ]; then printf '%s\n' "${DEBIAN_USB_SPEC_DIR}"; return 0; fi
  script_dir=$(CDPATH='' cd -P "$(dirname "$0")" && pwd -P)
  for candidate in "${script_dir}/../configs/spec/grub" "${script_dir}/../spec/grub" /usr/lib/debian-usb/spec/grub; do
    if [ -d "${candidate}" ]; then printf '%s\n' "${candidate}"; return 0; fi
  done
  duw_die "unable to locate the custom GRUB spec directory"
)

duw_grub_static_entry_title() (
  entry_id="$1"
  default_title="$2"
  spec_dir="$(duw_grub_spec_dir)"
  python3 - "${spec_dir}/main.json" "${entry_id}" "${default_title}" <<'PY'
import json
import sys
from pathlib import Path

spec_path = Path(sys.argv[1])
entry_id = sys.argv[2]
default_title = sys.argv[3]
main_spec = json.loads(spec_path.read_text(encoding="utf-8"))
title = default_title
for item in main_spec.get("static_entries", []):
    if not isinstance(item, dict):
        continue
    if str(item.get("id") or "").strip() != entry_id:
        continue
    configured = str(item.get("title") or "").strip()
    if configured:
        title = configured
    break
if len(title) > 128:
    raise SystemExit(f"GRUB static entry title is too long for {entry_id}: {len(title)} > 128")
if any(ord(ch) < 32 or ord(ch) == 127 for ch in title):
    raise SystemExit(f"GRUB static entry title contains control characters for {entry_id}")
print(title.replace("\\", "\\\\").replace('"', '\\"'))
PY
)

duw_grub_updatevars_titles() (
  spec_dir="$(duw_grub_spec_dir)"
  python3 - "${spec_dir}/main.json" <<'PY'
import json
import sys
from pathlib import Path

def escape_title(title: str) -> str:
    if len(title) > 128:
        raise SystemExit(f"GRUB menu title is too long: {len(title)} > 128")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in title):
        raise SystemExit("GRUB menu title contains control characters")
    return title.replace("\\", "\\\\").replace('"', '\\"')

def entry_spec(items: object, entry_id: str) -> dict[str, object]:
    if not isinstance(items, list):
        return {}
    for item in items:
        if isinstance(item, dict) and str(item.get("id") or "").strip() == entry_id:
            return item
    return {}

main_spec = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
updatevars = entry_spec(main_spec.get("static_entries", []), "updatevars")
title = str(updatevars.get("title") or "").strip() or "UEFI UpdateVars"
children = updatevars.get("entries", [])
user_title = str(entry_spec(children, "user-mode").get("title") or "").strip() or f"{title} User Mode (PK Installed) [.auth]"
setup_title = str(entry_spec(children, "setup-mode").get("title") or "").strip() or f"{title} Setup Mode (PK Not Installed) [.esl]"
print("\x1f".join(escape_title(value) for value in (title, user_title, setup_title)))
PY
)

duw_grub_redirect_entry_title() (
  entry_id="$1"
  default_title="$2"
  spec_dir="$(duw_grub_spec_dir)"
  python3 - "${spec_dir}/main.json" "${entry_id}" "${default_title}" <<'PY'
import json
import sys
from pathlib import Path

spec_path = Path(sys.argv[1])
entry_id = sys.argv[2]
default_title = sys.argv[3]
main_spec = json.loads(spec_path.read_text(encoding="utf-8"))
title = default_title
redirect_entries = main_spec.get("raw_iso_redirect", [])
if isinstance(redirect_entries, list):
    for item in redirect_entries:
        if not isinstance(item, dict):
            continue
        if str(item.get("id") or "").strip() != entry_id:
            continue
        configured = str(item.get("title") or "").strip()
        if configured:
            title = configured
        break
if len(title) > 128:
    raise SystemExit(f"GRUB raw ISO redirect title is too long for {entry_id}: {len(title)} > 128")
if any(ord(ch) < 32 or ord(ch) == 127 for ch in title):
    raise SystemExit(f"GRUB raw ISO redirect title contains control characters for {entry_id}")
print(title.replace("\\", "\\\\").replace('"', '\\"'))
PY
)

duw_partition_path() (
  device="$1"
  number="$2"
  case "${device}" in
    *[0-9]) printf '%sp%s\n' "${device}" "${number}" ;;
    *) printf '%s%s\n' "${device}" "${number}" ;;
  esac
)

duw_lsblk_field() (
  device="$1"
  field="$2"
  lsblk -dn -o "${field}" "${device}" 2>/dev/null | head -n1 | sed 's/^[[:space:]]*//; s/[[:space:]]*$//'
)

duw_device_pretty_name() (
  device="$1"
  size="$(duw_lsblk_field "${device}" "SIZE")"
  model="$(duw_lsblk_field "${device}" "MODEL")"
  transport="$(duw_lsblk_field "${device}" "TRAN")"
  [ -n "${size}" ] || size="unknown size"
  [ -n "${model}" ] || model="$(basename -- "${device}")"
  [ -n "${transport}" ] || transport="unknown transport"
  printf '%s | %s | %s | %s\n' "${device}" "${size}" "${model}" "${transport}"
)

duw_strip_quotes() (
  value="$1"
  value="${value%\"}"
  value="${value#\"}"
  value="${value%\'}"
  value="${value#\'}"
  printf '%s\n' "${value}"
)

duw_load_config() {
  _duw_config_path=$1
  [ -r "${_duw_config_path}" ] || duw_die "missing config file: ${_duw_config_path}"
  while IFS='=' read -r _duw_key _duw_value; do
    [ -n "${_duw_key}" ] || continue
    case ${_duw_key} in \#*) continue ;; esac
    _duw_value=$(duw_strip_quotes "${_duw_value}")
    case ${_duw_key} in
      DEFAULT_PERSISTENCE_SIZE_GIB) DEFAULT_PERSISTENCE_SIZE_GIB=${_duw_value} ;;
      DEFAULT_LIVE_HOOKS) DEFAULT_LIVE_HOOKS=${_duw_value} ;;
      DEFAULT_LIVE_ARGS_HOOKS) DEFAULT_LIVE_ARGS_HOOKS=${_duw_value} ;;
      DEFAULT_LIVE_WIFI_SECURITY) DEFAULT_LIVE_WIFI_SECURITY=${_duw_value} ;;
      DEFAULT_LIVE_WIFI_CIDR) DEFAULT_LIVE_WIFI_CIDR=${_duw_value} ;;
      DEFAULT_LIVE_WIFI_GATEWAY) DEFAULT_LIVE_WIFI_GATEWAY=${_duw_value} ;;
      DEFAULT_LIVE_WIFI_NAMESERVERS) DEFAULT_LIVE_WIFI_NAMESERVERS=${_duw_value} ;;
      DEFAULT_ESP_LABEL) DEFAULT_ESP_LABEL=${_duw_value} ;;
      DEFAULT_MULTI_DATA_LABEL) DEFAULT_MULTI_DATA_LABEL=${_duw_value} ;;
      DEFAULT_DEBIAN_LIVE_LABEL) DEFAULT_DEBIAN_LIVE_LABEL=${_duw_value} ;;
      DEFAULT_DEBIAN_NETINST_LABEL) DEFAULT_DEBIAN_NETINST_LABEL=${_duw_value} ;;
      DEFAULT_DEBIAN_NETBOOT_LABEL) DEFAULT_DEBIAN_NETBOOT_LABEL=${_duw_value} ;;
      DEFAULT_DEBIAN_PERSIST_LABEL) DEFAULT_DEBIAN_PERSIST_LABEL=${_duw_value} ;;
      DEFAULT_KALI_LIVE_LABEL) DEFAULT_KALI_LIVE_LABEL=${_duw_value} ;;
      DEFAULT_KALI_NETINST_LABEL) DEFAULT_KALI_NETINST_LABEL=${_duw_value} ;;
      DEFAULT_KALI_NETBOOT_LABEL) DEFAULT_KALI_NETBOOT_LABEL=${_duw_value} ;;
      DEFAULT_KALI_PERSIST_LABEL) DEFAULT_KALI_PERSIST_LABEL=${_duw_value} ;;
      DEFAULT_KALI_PURPLE_NETINST_LABEL) DEFAULT_KALI_PURPLE_NETINST_LABEL=${_duw_value} ;;
      DEFAULT_TAILS_LIVE_LABEL) DEFAULT_TAILS_LIVE_LABEL=${_duw_value} ;;
      DEFAULT_TAILS_PERSIST_LABEL) DEFAULT_TAILS_PERSIST_LABEL=${_duw_value} ;;
      DEFAULT_UBUNTU_LIVE_LABEL) DEFAULT_UBUNTU_LIVE_LABEL=${_duw_value} ;;
      DEFAULT_UBUNTU_NETINST_LABEL) DEFAULT_UBUNTU_NETINST_LABEL=${_duw_value} ;;
      DEFAULT_UBUNTU_PERSIST_LABEL) DEFAULT_UBUNTU_PERSIST_LABEL=${_duw_value} ;;
      DEFAULT_UBUNTU_PERSIST_PARTLABEL) DEFAULT_UBUNTU_PERSIST_PARTLABEL=${_duw_value} ;;
      PRESEED_USB_DEBIAN_FILE) PRESEED_USB_DEBIAN_FILE=${_duw_value} ;;
      PRESEED_USB_KALI_FILE) PRESEED_USB_KALI_FILE=${_duw_value} ;;
      PRESEED_USB_PURPLE_FILE) PRESEED_USB_PURPLE_FILE=${_duw_value} ;;
      PRESEED_HOST_DEBIAN_PATH) PRESEED_HOST_DEBIAN_PATH=${_duw_value} ;;
      PRESEED_HOST_KALI_PATH) PRESEED_HOST_KALI_PATH=${_duw_value} ;;
      PRESEED_HOST_PURPLE_PATH) PRESEED_HOST_PURPLE_PATH=${_duw_value} ;;
    esac
  done <"${_duw_config_path}"
  # These values are consumed by the Python GRUB renderer from the same config file.
  : "${DEFAULT_LIVE_ARGS_HOOKS:-}" "${DEFAULT_LIVE_WIFI_CIDR:-}" "${DEFAULT_LIVE_WIFI_GATEWAY:-}" "${DEFAULT_LIVE_WIFI_NAMESERVERS:-}"
  [ -n "${DEFAULT_PERSISTENCE_SIZE_GIB:-}" ] || duw_die "DEFAULT_PERSISTENCE_SIZE_GIB is required"
  case ${DEFAULT_LIVE_HOOKS:-} in 0|1) ;; *) duw_die "DEFAULT_LIVE_HOOKS must be 0 or 1" ;; esac
  if duw_live_hooks_enabled; then
    [ -n "${DEFAULT_LIVE_WIFI_SECURITY:-}" ] || duw_die "DEFAULT_LIVE_WIFI_SECURITY is required when DEFAULT_LIVE_HOOKS=1"
    case ${DEFAULT_LIVE_WIFI_SECURITY} in open|wpa|sae) ;; *) duw_die "DEFAULT_LIVE_WIFI_SECURITY must be one of: open, wpa, sae" ;; esac
  fi

  DEFAULT_ESP_LABEL=$(duw_config_label_value DEFAULT_ESP_LABEL ESPBOOT)
  DEFAULT_MULTI_DATA_LABEL=$(duw_config_label_value DEFAULT_MULTI_DATA_LABEL MULTIBOOT)
  DEFAULT_DEBIAN_LIVE_LABEL=$(duw_config_label_value DEFAULT_DEBIAN_LIVE_LABEL DEBIAN-LIVE)
  DEFAULT_DEBIAN_NETINST_LABEL=$(duw_config_label_value DEFAULT_DEBIAN_NETINST_LABEL DEBIAN-NETINST)
  DEFAULT_DEBIAN_NETBOOT_LABEL=$(duw_config_label_value DEFAULT_DEBIAN_NETBOOT_LABEL DEBIAN-NETBOOT)
  DEFAULT_DEBIAN_PERSIST_LABEL=$(duw_config_label_value DEFAULT_DEBIAN_PERSIST_LABEL DEBIAN-PERSIST)
  DEFAULT_KALI_LIVE_LABEL=$(duw_config_label_value DEFAULT_KALI_LIVE_LABEL KALI-LIVE)
  DEFAULT_KALI_NETINST_LABEL=$(duw_config_label_value DEFAULT_KALI_NETINST_LABEL KALI-NETINST)
  DEFAULT_KALI_NETBOOT_LABEL=$(duw_config_label_value DEFAULT_KALI_NETBOOT_LABEL KALI-NETBOOT)
  DEFAULT_KALI_PERSIST_LABEL=$(duw_config_label_value DEFAULT_KALI_PERSIST_LABEL KALI-PERSIST)
  DEFAULT_KALI_PURPLE_NETINST_LABEL=$(duw_config_label_value DEFAULT_KALI_PURPLE_NETINST_LABEL KPURPLE-NETINST)
  DEFAULT_TAILS_LIVE_LABEL=$(duw_config_label_value DEFAULT_TAILS_LIVE_LABEL TAILS-LIVE)
  DEFAULT_TAILS_PERSIST_LABEL=$(duw_config_label_value DEFAULT_TAILS_PERSIST_LABEL TailsData)
  DEFAULT_UBUNTU_LIVE_LABEL=$(duw_config_label_value DEFAULT_UBUNTU_LIVE_LABEL UBUNTU-LIVE)
  DEFAULT_UBUNTU_NETINST_LABEL=$(duw_config_label_value DEFAULT_UBUNTU_NETINST_LABEL UBUNTU-NETINST)
  DEFAULT_UBUNTU_PERSIST_LABEL=$(duw_config_label_value DEFAULT_UBUNTU_PERSIST_LABEL casper-rw)
  DEFAULT_UBUNTU_PERSIST_PARTLABEL=$(duw_config_label_value DEFAULT_UBUNTU_PERSIST_PARTLABEL writable)
  for _duw_label_spec in \
    DEFAULT_ESP_LABEL:11 DEFAULT_MULTI_DATA_LABEL:16 DEFAULT_DEBIAN_LIVE_LABEL:16 \
    DEFAULT_DEBIAN_NETINST_LABEL:16 DEFAULT_DEBIAN_NETBOOT_LABEL:16 DEFAULT_DEBIAN_PERSIST_LABEL:16 \
    DEFAULT_KALI_LIVE_LABEL:16 DEFAULT_KALI_NETINST_LABEL:16 DEFAULT_KALI_NETBOOT_LABEL:16 \
    DEFAULT_KALI_PERSIST_LABEL:16 DEFAULT_KALI_PURPLE_NETINST_LABEL:16 DEFAULT_TAILS_LIVE_LABEL:16 \
    DEFAULT_TAILS_PERSIST_LABEL:16 DEFAULT_UBUNTU_LIVE_LABEL:16 DEFAULT_UBUNTU_NETINST_LABEL:16 \
    DEFAULT_UBUNTU_PERSIST_LABEL:16 DEFAULT_UBUNTU_PERSIST_PARTLABEL:16
  do
    _duw_label_key=${_duw_label_spec%:*}
    _duw_label_max=${_duw_label_spec#*:}
    _duw_label_value=$(duw_config_label_value "${_duw_label_key}" '')
    duw_validate_config_label "${_duw_label_key}" "${_duw_label_value}" "${_duw_label_max}"
  done
}

duw_config_label_value() (
  key=$1
  fallback=$2
  case ${key} in
    DEFAULT_ESP_LABEL) value=${DEFAULT_ESP_LABEL:-} ;;
    DEFAULT_MULTI_DATA_LABEL) value=${DEFAULT_MULTI_DATA_LABEL:-} ;;
    DEFAULT_DEBIAN_LIVE_LABEL) value=${DEFAULT_DEBIAN_LIVE_LABEL:-} ;;
    DEFAULT_DEBIAN_NETINST_LABEL) value=${DEFAULT_DEBIAN_NETINST_LABEL:-} ;;
    DEFAULT_DEBIAN_NETBOOT_LABEL) value=${DEFAULT_DEBIAN_NETBOOT_LABEL:-} ;;
    DEFAULT_DEBIAN_PERSIST_LABEL) value=${DEFAULT_DEBIAN_PERSIST_LABEL:-} ;;
    DEFAULT_KALI_LIVE_LABEL) value=${DEFAULT_KALI_LIVE_LABEL:-} ;;
    DEFAULT_KALI_NETINST_LABEL) value=${DEFAULT_KALI_NETINST_LABEL:-} ;;
    DEFAULT_KALI_NETBOOT_LABEL) value=${DEFAULT_KALI_NETBOOT_LABEL:-} ;;
    DEFAULT_KALI_PERSIST_LABEL) value=${DEFAULT_KALI_PERSIST_LABEL:-} ;;
    DEFAULT_KALI_PURPLE_NETINST_LABEL) value=${DEFAULT_KALI_PURPLE_NETINST_LABEL:-} ;;
    DEFAULT_TAILS_LIVE_LABEL) value=${DEFAULT_TAILS_LIVE_LABEL:-} ;;
    DEFAULT_TAILS_PERSIST_LABEL) value=${DEFAULT_TAILS_PERSIST_LABEL:-} ;;
    DEFAULT_UBUNTU_LIVE_LABEL) value=${DEFAULT_UBUNTU_LIVE_LABEL:-} ;;
    DEFAULT_UBUNTU_NETINST_LABEL) value=${DEFAULT_UBUNTU_NETINST_LABEL:-} ;;
    DEFAULT_UBUNTU_PERSIST_LABEL) value=${DEFAULT_UBUNTU_PERSIST_LABEL:-} ;;
    DEFAULT_UBUNTU_PERSIST_PARTLABEL) value=${DEFAULT_UBUNTU_PERSIST_PARTLABEL:-} ;;
    *) value= ;;
  esac
  if [ -n "${value}" ]; then printf '%s\n' "${value}"; else printf '%s\n' "${fallback}"; fi
)

duw_is_printable_ascii_without_whitespace() (
  LC_ALL=C
  export LC_ALL
  printf '%s\n' "$1" | grep '^[!-~][!-~]*$' >/dev/null
)

duw_validate_config_label() (
  key=$1
  value=$2
  max_length=$3
  [ -n "${value}" ] || duw_die "${key} is required"
  [ "${#value}" -le "${max_length}" ] || duw_die "${key} is longer than ${max_length} characters: ${value}"
  duw_is_printable_ascii_without_whitespace "${value}" || duw_die "${key} must contain printable ASCII without whitespace: ${value}"
)

duw_effective_esp_label() (
  if [ -n "${DUSB_OVERRIDE_ESP_LABEL:-}" ]; then
    printf '%s\n' "${DUSB_OVERRIDE_ESP_LABEL}"
  else
    printf '%s\n' "${DEFAULT_ESP_LABEL}"
  fi
)

duw_usage() (
  cat <<'EOF'
Usage:
  write_usb.sh --config <path> --profile <name> --write-mode <direct|managed> --device <path> --iso <path> [--with-persistence] [--persistence-mode <plain|encrypted>] [--persistence-size-gib <int>] [--kernel-args <args>] [--kernel-path <path>] [--initrd-path <path>] [--menu-label <label>] [--esp-label <label>] [--payload-fs-label <label>] [--payload-partlabel <label>] [--persistence-fs-label <label>] [--persistence-partlabel <label>] [--use-custom-grub-menu <0|1>] [--preserve-upstream-grub-entries <0|1>] [--include-preseed <0|1>] [--offline-preseed-dir <path>] [--secure-boot-trust <mok|firmware-db>] [--update-existing <0|1>]
  write_usb.sh --config <path> --write-mode multi-os --device <path> --multi-os-plan <path> [--update-existing <0|1>]
EOF
)

duw_require_option_value() (
  option="$1"
  value="${2-}"
  [ -n "${value}" ] || duw_die "missing value for ${option}"
  case ${value} in --*) duw_die "missing value for ${option}" ;; esac
)

duw_require_commands() (
  for command in blkid blockdev cert-to-efi-sig-list cpio cryptsetup dd du findmnt git gpg grub-install gzip lsblk mkfs.ext4 mkfs.vfat mount mountpoint openssl parted partprobe python3 sbsign sbverify sign-efi-sig-list swapoff sync timeout umount xorriso; do
    command -v "${command}" >/dev/null 2>&1 || duw_die "required command not found: ${command}"
  done
)

duw_root_disk_path() (

  root_source="$(findmnt -n -o SOURCE / 2>/dev/null || true)"
  case ${root_source} in /dev/*) ;; *) return 0 ;; esac

  if resolved_source="$(readlink -f -- "${root_source}" 2>/dev/null)" && [ -b "${resolved_source}" ]; then
    root_source="${resolved_source}"
  fi

  if source_type="$(lsblk -dn -o TYPE "${root_source}" 2>/dev/null | tr -d '[:space:]')" && [ "${source_type}" = "disk" ]; then
    printf '%s\n' "${root_source}"
    return 0
  fi

  if ! root_parent="$(lsblk -no PKNAME "${root_source}" 2>/dev/null | tail -n1 | tr -d '[:space:]')"; then
    return 0
  fi
  [ -n "${root_parent}" ] || return 0
  printf '/dev/%s\n' "${root_parent}"
)

duw_validate_device() (
  device="$1"
  [ -b "${device}" ] || duw_die "target device is not a block device: ${device}"
  [ "$(lsblk -dn -o TYPE "${device}" | tr -d '[:space:]')" = "disk" ] || duw_die "target path must be a whole-disk block device: ${device}"
  [ "$(lsblk -dn -o RO "${device}" | tr -d '[:space:]')" = "0" ] || duw_die "target device is read-only: ${device}"
  removable="$(lsblk -dn -o RM "${device}" | tr -d '[:space:]')"
  hotplug="$(lsblk -dn -o HOTPLUG "${device}" | tr -d '[:space:]')"
  transport="$(lsblk -dn -o TRAN "${device}" | tr -d '[:space:]')"
  if [ "${removable}" != "1" ] && [ "${hotplug}" != "1" ] && [ "${transport}" != "usb" ]; then
    duw_die "refusing to overwrite a non-removable device: ${device}"
  fi
  root_disk="$(duw_root_disk_path)"
  if [ -n "${root_disk}" ] && [ "${root_disk}" = "${device}" ]; then
    duw_die "refusing to overwrite the current system disk: ${device}"
  fi
)

duw_close_stale_mapper() (
  mapper_name="$1"
  mapper_path="/dev/mapper/${mapper_name}"
  if cryptsetup status "${mapper_name}" >/dev/null 2>&1; then
    duw_step "Closing stale dm-crypt mapping ${mapper_name} before reuse"
    cryptsetup luksClose "${mapper_name}" || duw_die "failed to close stale dm-crypt mapping: ${mapper_name}"
    duw_settle_block_state
  fi
  if [ -e "${mapper_path}" ]; then
    if duw_have dmsetup; then
      dmsetup remove "${mapper_name}" >/dev/null 2>&1 || true
      duw_settle_block_state
    fi
  fi
  [ ! -e "${mapper_path}" ] || duw_die "dm-crypt mapper name is still busy: ${mapper_name}"
)

duw_active_swap_device() (
  path="$1"
  resolved_path=""
  entry_path=""
  resolved_entry=""

  resolved_path="$(readlink -f -- "${path}" 2>/dev/null || true)"
  [ -n "${resolved_path}" ] || resolved_path="${path}"
  [ -r /proc/swaps ] || return 1

  while read -r entry_path _; do
    [ -n "${entry_path}" ] && [ "${entry_path}" != "Filename" ] || continue
    if [ "${entry_path}" = "${path}" ]; then
      printf '%s\n' "${entry_path}"
      return 0
    fi
    resolved_entry="$(readlink -f -- "${entry_path}" 2>/dev/null || true)"
    [ -n "${resolved_entry}" ] || resolved_entry="${entry_path}"
    if [ "${resolved_entry}" = "${resolved_path}" ]; then
      printf '%s\n' "${entry_path}"
      return 0
    fi
  done </proc/swaps
  return 1
)

duw_block_holders() (
  path=$1
  resolved_path=$(readlink -f -- "${path}" 2>/dev/null || true)
  [ -n "${resolved_path}" ] && [ -b "${resolved_path}" ] || return 0
  block_name=$(basename "${resolved_path}")
  for holder_path in /sys/class/block/"${block_name}"/holders/*; do
    [ -e "${holder_path}" ] || continue
    printf '%s\t%s\n' "${path}" "$(basename "${holder_path}")"
  done
)

duw_block_name_for_path() (
  path="$1"
  resolved_path=""
  resolved_path="$(readlink -f -- "${path}" 2>/dev/null || true)"
  [ -n "${resolved_path}" ] && [ -b "${resolved_path}" ] || return 0
  basename -- "${resolved_path}"
)

duw_mapper_name_for_block_path() (
  path="$1"
  block_name="$(duw_block_name_for_path "${path}")"
  [ -n "${block_name}" ] || return 0
  if [ -r "/sys/class/block/${block_name}/dm/name" ]; then
    mapper_name=$(cat -- "/sys/class/block/${block_name}/dm/name")
    printf '%s\n' "${mapper_name}"
    return 0
  fi
  lsblk -dn -o NAME "${path}" 2>/dev/null | head -n1 | tr -d '[:space:]'
)

duw_block_holder_paths() (
  path=$1
  holder_rows=$(duw_block_holders "${path}")
  [ -n "${holder_rows}" ] || return 0
  while IFS=$(printf '\t') read -r holder_path holder_name; do
    [ -n "${holder_path}" ] && [ -n "${holder_name}" ] || continue
    for candidate in "/dev/mapper/${holder_name}" "/dev/${holder_name}"; do
      if [ -b "${candidate}" ]; then printf '%s\n' "${candidate}"; break; fi
    done
  done <<EOF_ROWS
${holder_rows}
EOF_ROWS
)

duw_mount_targets_for_block_path() (
  path=$1
  resolved_path=$(readlink -f -- "${path}" 2>/dev/null || true)
  [ -n "${resolved_path}" ] && [ -b "${resolved_path}" ] || resolved_path=${path}
  {
    encoded_mounts=$(lsblk -dn -o MOUNTPOINTS "${path}" 2>/dev/null | head -n1 || true)
    if [ -n "${encoded_mounts}" ]; then printf '%b\n' "${encoded_mounts}"; fi
    mount_rows=$(findmnt -rn --raw --output SOURCE,TARGET 2>/dev/null || true)
    if [ -n "${mount_rows}" ]; then
      while read -r source target; do
        [ -n "${source}" ] && [ -n "${target}" ] || continue
        source_path=${source%%\[*}
        [ -n "${source_path}" ] || source_path=${source}
        resolved_source=$(readlink -f -- "${source_path}" 2>/dev/null || true)
        [ -n "${resolved_source}" ] || resolved_source=${source_path}
        if [ "${source_path}" = "${path}" ] || [ "${resolved_source}" = "${resolved_path}" ]; then
          printf '%s\n' "${target}"
        fi
      done <<EOF_MOUNTS
${mount_rows}
EOF_MOUNTS
    fi
  } | awk 'NF && !seen[$0]++'
)

duw_release_block_path() (
  path=$1
  depth=${2:-0}
  close_self=${3:-1}
  fail_fast=${4:-1}
  [ -n "${path}" ] && [ -b "${path}" ] || return 0
  [ "${depth}" -lt 8 ] || duw_die "block holder recursion limit exceeded while releasing ${path}"

  holder_paths=$(duw_block_holder_paths "${path}")
  old_ifs=${IFS}; IFS=$(printf '\n\t'); set -f
  for holder_path in ${holder_paths}; do
    [ -n "${holder_path}" ] && [ -b "${holder_path}" ] || continue
    duw_release_block_path "${holder_path}" "$((depth + 1))" 1 "${fail_fast}" || return 1
  done
  set +f; IFS=${old_ifs}

  mounts=$(duw_mount_targets_for_block_path "${path}" | awk '{ values[NR]=$0 } END { for (i=NR; i>0; i--) print values[i] }')
  old_ifs=${IFS}; IFS=$(printf '\n\t'); set -f
  for mountpoint in ${mounts}; do
    [ -n "${mountpoint}" ] || continue
    if ! umount -- "${mountpoint}"; then
      if [ "${fail_fast}" = 1 ]; then duw_die "failed to unmount ${path} from ${mountpoint}"; fi
      return 1
    fi
  done
  set +f; IFS=${old_ifs}

  if swap_device=$(duw_active_swap_device "${path}"); then
    if ! swapoff -- "${swap_device}"; then
      if [ "${fail_fast}" = 1 ]; then duw_die "failed to disable swap on ${swap_device}"; fi
      return 1
    fi
  fi
  block_type=$(lsblk -dn -o TYPE "${path}" 2>/dev/null | tr -d '[:space:]' || true)
  if [ "${block_type}" = crypt ] && [ "${close_self}" = 1 ]; then
    mapper_name=$(duw_mapper_name_for_block_path "${path}")
    if [ -n "${mapper_name}" ] && cryptsetup status "${mapper_name}" >/dev/null 2>&1; then
      if ! cryptsetup close "${mapper_name}"; then
        if [ "${fail_fast}" = 1 ]; then duw_die "failed to close dm-crypt mapping ${mapper_name}"; fi
        return 1
      fi
      duw_settle_block_state
    fi
  fi
)

duw_device_in_use_reasons() (
  device=$1
  children=$(lsblk -ln -o PATH "${device}" 2>/dev/null | tail -n +2)
  old_ifs=${IFS}; IFS=$(printf '\n\t'); set -f
  for child in ${children}; do
    [ -n "${child}" ] && [ "${child}" != "${device}" ] || continue
    mounts=$(duw_mount_targets_for_block_path "${child}")
    for mountpoint in ${mounts}; do
      [ -n "${mountpoint}" ] || continue
      printf '%s mounted on %s\n' "${child}" "${mountpoint}"
      mount_openers=$(duw_mountpoint_openers "${mountpoint}")
      for opener in ${mount_openers}; do [ -n "${opener}" ] && printf '%s held open by %s\n' "${mountpoint}" "${opener}"; done
    done
    if swap_device=$(duw_active_swap_device "${child}"); then printf '%s still active as swap\n' "${swap_device}"; fi
    holders=$(duw_block_holders "${child}")
    if [ -n "${holders}" ]; then
      while IFS=$(printf '\t') read -r holder_path holder_name; do
        [ -n "${holder_path}" ] && [ -n "${holder_name}" ] && printf '%s held by %s\n' "${holder_path}" "${holder_name}"
      done <<EOF_HOLDERS
${holders}
EOF_HOLDERS
    fi
    openers=$(duw_block_path_openers "${child}")
    for opener in ${openers}; do [ -n "${opener}" ] && printf '%s opened by %s\n' "${child}" "${opener}"; done
  done
  set +f; IFS=${old_ifs}

  holders=$(duw_block_holders "${device}")
  if [ -n "${holders}" ]; then
    while IFS=$(printf '\t') read -r holder_path holder_name; do
      [ -n "${holder_path}" ] && [ -n "${holder_name}" ] && printf '%s held by %s\n' "${holder_path}" "${holder_name}"
    done <<EOF_HOLDERS
${holders}
EOF_HOLDERS
  fi
  mounts=$(duw_mount_targets_for_block_path "${device}")
  old_ifs=${IFS}; IFS=$(printf '\n\t'); set -f
  for mountpoint in ${mounts}; do
    [ -n "${mountpoint}" ] || continue
    printf '%s mounted on %s\n' "${device}" "${mountpoint}"
    mount_openers=$(duw_mountpoint_openers "${mountpoint}")
    for opener in ${mount_openers}; do [ -n "${opener}" ] && printf '%s held open by %s\n' "${mountpoint}" "${opener}"; done
  done
  openers=$(duw_block_path_openers "${device}")
  for opener in ${openers}; do [ -n "${opener}" ] && printf '%s opened by %s\n' "${device}" "${opener}"; done
  set +f; IFS=${old_ifs}
)

duw_scrub_selected_device_signatures() (
  device=$1
  duw_have wipefs || return 0
  children=$(lsblk -ln -o PATH "${device}" 2>/dev/null | tail -n +2)
  old_ifs=${IFS}; IFS=$(printf '\n\t'); set -f
  for child in ${children}; do
    [ -n "${child}" ] && [ -b "${child}" ] || continue
    wipefs -a -f -- "${child}" >/dev/null 2>&1 || true
  done
  set +f; IFS=${old_ifs}
  wipefs -a -f -- "${device}" >/dev/null 2>&1 || true
  duw_settle_block_state
)

duw_release_selected_device_for_write() (
  device=$1
  device_in_use=
  for attempt in 1 2 3 4 5; do
    children=$(lsblk -ln -o PATH "${device}" 2>/dev/null | tail -n +2 | awk '{ values[NR]=$0 } END { for (i=NR; i>0; i--) print values[i] }')
    duw_udisks_unmount_device_children "${device}"
    old_ifs=${IFS}; IFS=$(printf '\n\t'); set -f
    for child in ${children}; do
      [ -n "${child}" ] && [ "${child}" != "${device}" ] || continue
      if ! duw_release_block_path "${child}" 0 1 0; then :; fi
    done
    set +f; IFS=${old_ifs}
    if ! duw_release_block_path "${device}" 0 1 0; then :; fi

    duw_udisks_unmount_device_children "${device}"
    duw_settle_block_state
    device_in_use=$(duw_device_in_use_reasons "${device}")
    [ -n "${device_in_use}" ] || return 0
    if [ "${attempt}" -lt 5 ]; then
      summary=$(printf '%s' "${device_in_use}" | awk 'BEGIN { ORS="" } { if (NR>1) printf "; "; printf "%s", $0 }')
      duw_note "Selected-device write-preflight attempt ${attempt}/5 still found active mounts or openers on ${device}; close file-manager windows or shells using the listed paths; retrying: ${summary}"
      sleep "${attempt}"
    fi
  done
  summary=$(printf '%s' "${device_in_use}" | awk 'BEGIN { ORS="" } { if (NR>1) printf "; "; printf "%s", $0 }')
  duw_die "device ${device} is still in use before write operations; refusing a forced or lazy unmount: ${summary}"
)

duw_unmount_device_children() (
  device=$1
  device_in_use=
  for attempt in 1 2 3 4 5; do
    children=$(lsblk -ln -o PATH "${device}" 2>/dev/null | tail -n +2 | awk '{ values[NR]=$0 } END { for (i=NR; i>0; i--) print values[i] }')
    duw_udisks_unmount_device_children "${device}"
    old_ifs=${IFS}; IFS=$(printf '\n\t'); set -f
    for child in ${children}; do
      [ -n "${child}" ] && [ "${child}" != "${device}" ] || continue
      if ! duw_release_block_path "${child}" 0 1 0; then :; fi
    done
    set +f; IFS=${old_ifs}
    if ! duw_release_block_path "${device}" 0 1 0; then :; fi
    duw_udisks_unmount_device_children "${device}"
    duw_settle_block_state
    device_in_use=$(duw_device_in_use_reasons "${device}")
    if [ -z "${device_in_use}" ]; then
      duw_scrub_selected_device_signatures "${device}"
      duw_reset_partition_table_state "${device}"
      duw_udisks_unmount_device_children "${device}"
      duw_settle_block_state
      device_in_use=$(duw_device_in_use_reasons "${device}")
      [ -n "${device_in_use}" ] || return 0
    fi
    if [ "${attempt}" -lt 5 ]; then
      summary=$(printf '%s' "${device_in_use}" | awk 'BEGIN { ORS="" } { if (NR>1) printf "; "; printf "%s", $0 }')
      duw_note "Selected-device release attempt ${attempt}/5 still found active mounts or openers on ${device}; close file-manager windows or shells using the listed paths; retrying: ${summary}"
      sleep "${attempt}"
    fi
  done
  summary=$(printf '%s' "${device_in_use}" | awk 'BEGIN { ORS="" } { if (NR>1) printf "; "; printf "%s", $0 }')
  duw_die "device ${device} is still in use after release attempts; refusing a forced or lazy unmount before destructive writes: ${summary}"
)

duw_reset_partition_table_state() (
  device="$1"

  if duw_have partx; then
    partx -d -- "${device}" >/dev/null 2>&1 || true
  fi
  if duw_have blockdev; then
    blockdev --rereadpt "${device}" >/dev/null 2>&1 || true
  fi
  if duw_have partprobe; then
    partprobe "${device}" >/dev/null 2>&1 || true
  fi
  duw_settle_block_state
)

duw_write_gpt_label() (
  device="$1"
  output=""
  exit_code=0
  status_summary=""

  for attempt in 1 2 3; do
    duw_unmount_device_children "${device}"
    if output="$(parted -s "${device}" mklabel gpt 2>&1)"; then
      exit_code=0
    else
      exit_code=$?
    fi
    if [ "${exit_code}" -eq 0 ] && ! printf '%s\n' "${output}" | grep -Fq 'Error:'; then
      return 0
    fi
    if [ "${attempt}" -ge "3" ]; then
      status_summary="exit ${exit_code}"
      if [ "${exit_code}" -eq "0" ]; then
        status_summary="exit 0 with parted error output"
      fi
      duw_die "failed to write a GPT label on ${device} after ${attempt} attempts (${status_summary}): ${output}"
    fi
    duw_note "GPT relabel attempt ${attempt} failed on ${device}; releasing holders and refreshing kernel partition state before retry: ${output}"
    duw_unmount_device_children "${device}"
    duw_reset_partition_table_state "${device}"
    sleep 1
  done
)

duw_write_hybrid_iso() (
  iso_path="$1"
  device="$2"
  [ "$(lsblk -dn -o TYPE "${device}" 2>/dev/null | tr -d '[:space:]')" = "disk" ] || \
    duw_die "hybrid ISO writes must target a whole-disk block device such as /dev/sdX, not a partition: ${device}"
  duw_unmount_device_children "${device}"
  duw_note "Writing ${iso_path} to ${device} as a raw whole-disk hybrid ISO"
  dd if="${iso_path}" of="${device}" bs=4M conv=fsync status=progress
  sync
  partprobe "${device}" || true
)

duw_bytes_to_mib() (
  bytes="$1"
  printf '%s\n' "$(( (bytes + 1048576 - 1) / 1048576 ))"
)

duw_require_file_fits_partition() (
  file_path="$1"
  partition="$2"
  label="$3"

  [ -f "${file_path}" ] || duw_die "${label} is not a regular file: ${file_path}"
  file_bytes="$(stat -c '%s' "${file_path}")"
  partition_bytes="$(blockdev --getsize64 "${partition}")"
  if [ "${file_bytes}" -gt "${partition_bytes}" ]; then
    duw_die "${label} does not fit in ${partition}; file=${file_bytes} bytes partition=${partition_bytes} bytes"
  fi
)

duw_wait_for_block_device() (
  path="$1"
  attempts="${2:-20}"
  delay_s="${3:-0.2}"
  try=1

  while [ "${try}" -le "${attempts}" ]; do
    if [ -b "${path}" ]; then
      return 0
    fi
    sleep "${delay_s}"
    try=$((try + 1))
  done
  duw_die "timed out waiting for block device node: ${path}"
)

duw_crypt_mapper_block_path() (
  mapper_name="$1"
  mapper_path="/dev/mapper/${mapper_name}"
  sys_name_path=""
  block_name=""

  if [ -b "${mapper_path}" ]; then
    printf '%s\n' "${mapper_path}"
    return 0
  fi
  for sys_name_path in /sys/class/block/dm-*/dm/name; do
    [ -r "${sys_name_path}" ] || continue
    if [ "$(cat -- "${sys_name_path}")" = "${mapper_name}" ]; then
      block_name="$(basename -- "${sys_name_path%/dm/name}")"
      if [ -b "/dev/${block_name}" ]; then
        printf '/dev/%s\n' "${block_name}"
        return 0
      fi
    fi
  done
  return 0
)

duw_wait_for_crypt_mapper() (
  mapper_name="$1"
  attempts="${2:-60}"
  delay_s="${3:-0.2}"
  try=1
  mapper_path=""
  dm_summary=""

  while [ "${try}" -le "${attempts}" ]; do
    mapper_path="$(duw_crypt_mapper_block_path "${mapper_name}")"
    if [ -n "${mapper_path}" ] && [ -b "${mapper_path}" ]; then
      printf '%s\n' "${mapper_path}"
      return 0
    fi
    duw_settle_block_state
    sleep "${delay_s}"
    try=$((try + 1))
  done
  if duw_have dmsetup; then
    dm_summary="$(dmsetup info -c --noheadings -o name,blkdevname 2>/dev/null | tr '\n' '; ' || true)"
  fi
  duw_die "timed out waiting for dm-crypt mapper block device: ${mapper_name}${dm_summary:+; dmsetup=${dm_summary}}"
)

duw_partition_number_from_path() (
  path="$1"
  suffix=""

  suffix="${path##*[!0-9]}"
  [ -n "${suffix}" ] || duw_die "failed to determine partition number from path: ${path}"
  printf '%s\n' "${suffix}"
)

duw_wait_for_partition_device() (
  device="$1"
  number="$2"
  attempts="${3:-30}"
  delay_s="${4:-0.5}"
  try=1
  candidate=""
  fallback_path=""
  lsblk_summary=""

  fallback_path="$(duw_partition_path "${device}" "${number}")"
  while [ "${try}" -le "${attempts}" ]; do
    candidate="$(lsblk -ln -o PATH,PARTN "${device}" 2>/dev/null | awk -v partn="${number}" '$2 == partn { print $1; exit }' || true)"
    if [ -n "${candidate}" ] && [ -b "${candidate}" ]; then
      printf '%s\n' "${candidate}"
      return 0
    fi
    if [ -b "${fallback_path}" ]; then
      printf '%s\n' "${fallback_path}"
      return 0
    fi
    duw_udisks_unmount_device_children "${device}"
    duw_release_selected_device_for_write "${device}"
    if duw_have partx; then
      partx -u -- "${device}" >/dev/null 2>&1 || true
    fi
    duw_reset_partition_table_state "${device}"
    duw_settle_block_state
    sleep "${delay_s}"
    try=$((try + 1))
  done

  lsblk_summary="$(lsblk -ln -o PATH,PARTN,TYPE,FSTYPE,MOUNTPOINTS "${device}" 2>/dev/null | tr '\n' '; ' || true)"
  duw_die "timed out waiting for ${device} partition ${number}; current partition state: ${lsblk_summary}"
)

duw_settle_block_state() (
  if duw_have udevadm; then
    udevadm settle --timeout=10 >/dev/null 2>&1 || true
  fi
)

duw_wait_for_filesystem_signature() (
  path="$1"
  attempts="${2:-20}"
  delay_s="${3:-0.2}"
  try=1

  while [ "${try}" -le "${attempts}" ]; do
    if blkid "${path}" >/dev/null 2>&1; then
      return 0
    fi
    duw_settle_block_state
    sleep "${delay_s}"
    try=$((try + 1))
  done
  duw_die "timed out waiting for a filesystem signature on ${path}"
)

duw_resolved_path() (
  path="$1"
  resolved_path=""
  resolved_path="$(readlink -f -- "${path}" 2>/dev/null || true)"
  if [ -n "${resolved_path}" ]; then
    printf '%s\n' "${resolved_path}"
    return 0
  fi
  printf '%s\n' "${path}"
)

duw_mount_source_for_target() (
  mount_path="$1"
  findmnt -n -o SOURCE --target "${mount_path}" 2>/dev/null | head -n1 | sed 's/^[[:space:]]*//; s/[[:space:]]*$//'
)

duw_require_mount_source() (
  device="$1"
  mount_path="$2"

  mountpoint -q -- "${mount_path}" || duw_die "expected ${mount_path} to be mounted from ${device}, but it is not a mountpoint"
  actual_source="$(duw_mount_source_for_target "${mount_path}")"
  [ -n "${actual_source}" ] || duw_die "failed to determine mount source for ${mount_path}"
  actual_path="${actual_source%%\[*}"
  [ -n "${actual_path}" ] || actual_path="${actual_source}"
  expected_source="$(duw_resolved_path "${device}")"
  expected_resolved="$(duw_resolved_path "${expected_source}")"
  actual_resolved="$(duw_resolved_path "${actual_path}")"
  [ "${actual_resolved}" = "${expected_resolved}" ] || \
    duw_die "mount source mismatch for ${mount_path}: expected ${device}, got ${actual_source}"
)

duw_mount_partition() (
  device="$1"
  mount_path="$2"
  fstype="$3"
  attempts="${4:-10}"
  delay_s="${5:-0.5}"
  try=1
  output=""

  while [ "${try}" -le "${attempts}" ]; do
    if output="$(mount -t "${fstype}" -- "${device}" "${mount_path}" 2>&1)"; then
      duw_require_mount_source "${device}" "${mount_path}"
      duw_note "Mounted ${device} on ${mount_path} (${fstype})"
      return 0
    fi
    duw_settle_block_state
    sleep "${delay_s}"
    try=$((try + 1))
  done
  duw_die "failed to mount ${device} on ${mount_path}: ${output}"
)

duw_release_partition_mounts() (
  partition=$1
  attempts=${2:-5}
  delay_s=${3:-0.5}
  try=1
  while [ "${try}" -le "${attempts}" ]; do
    mounts=$(findmnt -rn --source "${partition}" --output TARGET 2>/dev/null || true)
    [ -n "${mounts}" ] || return 0
    mounts=$(printf '%s\n' "${mounts}" | awk '{ values[NR]=$0 } END { for (i=NR; i>0; i--) print values[i] }')
    old_ifs=${IFS}; IFS=$(printf '\n\t'); set -f
    for mountpoint in ${mounts}; do
      umount -- "${mountpoint}" || duw_die "failed to unmount ${partition} from ${mountpoint} before formatting"
    done
    set +f; IFS=${old_ifs}
    duw_settle_block_state
    sleep "${delay_s}"
    try=$((try + 1))
  done
  mounts=$(findmnt -rn --source "${partition}" --output TARGET 2>/dev/null || true)
  [ -z "${mounts}" ] || duw_die "partition ${partition} is still mounted before formatting: $(printf '%s' "${mounts}" | tr '\n' ' ')"
)

duw_parent_disk_path() (
  path="$1"
  parent_name=""

  parent_name="$(lsblk -dn -o PKNAME "${path}" 2>/dev/null | head -n1 | tr -d '[:space:]' || true)"
  [ -n "${parent_name}" ] || return 0
  [ -b "/dev/${parent_name}" ] || return 0
  printf '/dev/%s\n' "${parent_name}"
)

duw_udisks_unmount_device_children() (
  device=$1
  duw_have udisksctl || return 0
  udisksctl unmount -b "${device}" >/dev/null 2>&1 || true
  children=$(lsblk -ln -o PATH "${device}" 2>/dev/null | tail -n +2 | awk '{ values[NR]=$0 } END { for (i=NR; i>0; i--) print values[i] }')
  old_ifs=${IFS}; IFS=$(printf '\n\t'); set -f
  for child in ${children}; do
    [ -n "${child}" ] && [ -b "${child}" ] || continue
    udisksctl unmount -b "${child}" >/dev/null 2>&1 || true
  done
  set +f; IFS=${old_ifs}
)

duw_block_path_openers() (
  path=$1
  if duw_have fuser; then
    pids=$(fuser "${path}" 2>/dev/null | tr -cs '0-9' '\n' | sed '/^$/d' || true)
    if [ -n "${pids}" ]; then
      old_ifs=${IFS}; IFS=$(printf '\n\t'); set -f
      for pid in ${pids}; do
        [ -n "${pid}" ] || continue
        comm=$(ps -o comm= -p "${pid}" 2>/dev/null | head -n1 | tr -d '[:space:]' || true)
        [ -n "${comm}" ] || comm=unknown
        printf '%s:%s\n' "${pid}" "${comm}"
      done
      set +f; IFS=${old_ifs}
      return 0
    fi
  fi
  if duw_have lsof; then
    pids=$(lsof -w -t -- "${path}" 2>/dev/null || true)
    old_ifs=${IFS}; IFS=$(printf '\n\t'); set -f
    for pid in ${pids}; do
      [ -n "${pid}" ] || continue
      comm=$(ps -o comm= -p "${pid}" 2>/dev/null | head -n1 | tr -d '[:space:]' || true)
      [ -n "${comm}" ] || comm=unknown
      printf '%s:%s\n' "${pid}" "${comm}"
    done
    set +f; IFS=${old_ifs}
  fi
)

duw_mountpoint_openers() (
  mountpoint=$1
  duw_have fuser || return 0
  pids=$(fuser -m -- "${mountpoint}" 2>/dev/null | tr -cs '0-9' '\n' | sed '/^$/d' || true)
  old_ifs=${IFS}; IFS=$(printf '\n\t'); set -f
  for pid in ${pids}; do
    [ -n "${pid}" ] || continue
    comm=$(ps -o comm= -p "${pid}" 2>/dev/null | head -n1 | tr -d '[:space:]' || true)
    [ -n "${comm}" ] || comm=unknown
    printf '%s:%s\n' "${pid}" "${comm}"
  done
  set +f; IFS=${old_ifs}
)


duw_block_path_state_summary() (
  path=$1
  separator=$(printf '\037')
  details=
  mounts=$(duw_mount_targets_for_block_path "${path}")
  old_ifs=${IFS}; IFS=$(printf '\n\t'); set -f
  for mountpoint in ${mounts}; do [ -n "${mountpoint}" ] && details=${details}${details:+${separator}}mounted:${mountpoint}; done
  if swap_device=$(duw_active_swap_device "${path}"); then details=${details}${details:+${separator}}swap:${swap_device}; fi
  holders=$(duw_block_holders "${path}")
  if [ -n "${holders}" ]; then
    while IFS=$(printf '\t') read -r holder_path holder_name; do
      [ -n "${holder_name}" ] && details=${details}${details:+${separator}}holder:${holder_name}
    done <<EOF_HOLDERS
${holders}
EOF_HOLDERS
  fi
  openers=$(duw_block_path_openers "${path}")
  for opener in ${openers}; do [ -n "${opener}" ] && details=${details}${details:+${separator}}opener:${opener}; done
  set +f; IFS=${old_ifs}
  if [ -z "${details}" ]; then printf 'idle\n'; return 0; fi
  IFS=${separator}
  set -f
  first=1
  for detail in ${details}; do
    [ "${first}" = 1 ] || printf '; '
    printf '%s' "${detail}"
    first=0
  done
  printf '\n'
)

duw_wait_for_block_path_openers_to_clear() (
  path="$1"
  attempts="${2:-20}"
  delay_s="${3:-0.2}"
  try=1
  opener=""

  while [ "${try}" -le "${attempts}" ]; do
    opener="$(duw_block_path_openers "${path}" | head -n1 || true)"
    [ -z "${opener}" ] && return 0
    duw_settle_block_state
    sleep "${delay_s}"
    try=$((try + 1))
  done
  duw_die "timed out waiting for openers to release ${path}: $(duw_block_path_state_summary "${path}")"
)

duw_prepare_block_path_for_write() (
  path="$1"
  target_device="${2:-}"
  parent_disk=""
  block_type=""
  close_self=1

  # Whole-device release happens before partitioning. Per-partition writes must
  # not unmount sibling partitions that the managed workflow already mounted.
  block_type="$(lsblk -dn -o TYPE "${path}" 2>/dev/null | tr -d '[:space:]' || true)"
  if [ "${block_type}" = "crypt" ]; then
    close_self=0
  fi
  duw_release_block_path "${path}" 0 "${close_self}"
  duw_release_partition_mounts "${path}" 3 0.2
  duw_settle_block_state
  parent_disk="${target_device}"
  if [ -z "${parent_disk}" ]; then
    parent_disk="$(duw_parent_disk_path "${path}")"
  fi
  if [ -n "${parent_disk}" ]; then
    if duw_have partprobe; then
      partprobe "${parent_disk}" >/dev/null 2>&1 || true
    fi
    duw_settle_block_state
    if [ "${parent_disk}" != "${path}" ]; then
      duw_wait_for_block_path_openers_to_clear "${parent_disk}" 20 0.2
    fi
  fi
  duw_wait_for_block_device "${path}" 20 0.2
  duw_wait_for_block_path_openers_to_clear "${path}" 20 0.2
  if duw_have wipefs; then
    wipefs -a -f -- "${path}" >/dev/null 2>&1 || true
    duw_settle_block_state
  fi
  duw_wait_for_block_path_openers_to_clear "${path}" 20 0.2
)

duw_make_vfat_filesystem() (
  partition="$1"
  label="$2"
  target_device="${3:-}"
  output=""
  exit_code=0
  state_summary=""

  duw_step "Formatting ${partition} as FAT32 (${label})"
  for attempt in 1 2 3 4 5; do
    duw_prepare_block_path_for_write "${partition}" "${target_device}"
    if output="$(mkfs.vfat -F 32 -n "${label}" "${partition}" 2>&1)"; then
      return 0
    else
      exit_code=$?
    fi
    state_summary="$(duw_block_path_state_summary "${partition}")"
    if [ "${attempt}" -lt "5" ]; then
      duw_note "Retrying FAT32 format on ${partition} after releasing mounts and holders (attempt ${attempt}/5, state=${state_summary}): ${output}"
      duw_settle_block_state
      sleep "${attempt}"
    fi
  done
  duw_die "failed to format ${partition} as FAT32 (${label}) (exit ${exit_code}, state=$(duw_block_path_state_summary "${partition}")): ${output}"
)

duw_make_ext4_filesystem() (
  partition="$1"
  label="$2"
  target_device="${3:-}"
  output=""
  exit_code=0
  state_summary=""

  duw_step "Formatting ${partition} as ext4 (${label})"
  # Fully initialize ext4 metadata at write time so first boot does not stall on lazy inode/journal init.
  for attempt in 1 2 3 4 5; do
    duw_prepare_block_path_for_write "${partition}" "${target_device}"
    if output="$(mkfs.ext4 -F -L "${label}" -E lazy_itable_init=0,lazy_journal_init=0 "${partition}" 2>&1)"; then
      return 0
    else
      exit_code=$?
    fi
    state_summary="$(duw_block_path_state_summary "${partition}")"
    if [ "${attempt}" -lt "5" ]; then
      duw_note "Retrying ext4 format on ${partition} after releasing mounts and holders (attempt ${attempt}/5, state=${state_summary}): ${output}"
      duw_settle_block_state
      sleep "${attempt}"
    fi
  done
  duw_die "failed to format ${partition} as ext4 (${label}) (exit ${exit_code}, state=$(duw_block_path_state_summary "${partition}")): ${output}"
)

duw_extract_iso_contents() (
  iso_path="$1"
  live_mount="$2"
  output=""
  exit_code=0

  duw_step "Extracting ISO contents from ${iso_path} into ${live_mount}; this can take a while"
  if output=$(xorriso -osirrox on -indev "${iso_path}" -extract / "${live_mount}" 2>&1); then
    return 0
  else
    exit_code=$?
    duw_die "failed to extract ISO contents from ${iso_path} into ${live_mount} (exit ${exit_code}): ${output}"
  fi
)

duw_write_iso_payload_partition() (
  iso_path=$1
  partition=$2
  target_device=${3:-}
  output_file=$(mktemp "${TMPDIR:-/tmp}/debian-usb-dd.XXXXXX") || duw_die "failed to create temporary dd log file"
  status_file=${output_file}.status
  duw_step "Writing managed ISO payload from ${iso_path} into ${partition}"
  for attempt in 1 2 3; do
    duw_prepare_block_path_for_write "${partition}" "${target_device}"
    : >"${output_file}" || duw_die "failed to reset temporary dd log file: ${output_file}"
    rm -f -- "${status_file}"
    { if dd if="${iso_path}" of="${partition}" bs=8M conv=fsync status=progress oflag=direct; then dd_status=0; else dd_status=$?; fi; printf '%s\n' "${dd_status}" >"${status_file}"; } 2>&1 | tee "${output_file}" >&2
    exit_code=$(cat "${status_file}")
    if [ "${exit_code}" -eq 0 ]; then
      sync
      rm -f -- "${output_file}" "${status_file}" || true
      return 0
    fi
    output=$(tail -n 40 -- "${output_file}" 2>/dev/null || true)
    state_summary=$(duw_block_path_state_summary "${partition}")
    if [ "${attempt}" -lt 3 ]; then
      duw_note "Retrying raw ISO payload write to ${partition} after releasing mounts and holders (attempt ${attempt}/3, state=${state_summary}): ${output}"
      duw_settle_block_state
      sleep "${attempt}"
    fi
  done
  rm -f -- "${output_file}" "${status_file}" || true
  duw_die "failed to write the preserved ISO payload into ${partition} (exit ${exit_code}, state=$(duw_block_path_state_summary "${partition}")): ${output}"
)

duw_partition_uuid() (
  path="$1"
  attempts="${2:-20}"
  delay_s="${3:-0.2}"
  try=1
  output=""

  while [ "${try}" -le "${attempts}" ]; do
    if output="$(blkid -s UUID -o value "${path}" 2>/dev/null)" && [ -n "${output}" ]; then
      printf '%s\n' "${output}"
      return 0
    fi
    duw_settle_block_state
    sleep "${delay_s}"
    try=$((try + 1))
  done
  duw_die "timed out waiting for the filesystem UUID on ${path}"
)

duw_partition_partuuid() (
  path="$1"
  attempts="${2:-20}"
  delay_s="${3:-0.2}"
  try=1
  output=""

  while [ "${try}" -le "${attempts}" ]; do
    if output="$(blkid -s PARTUUID -o value "${path}" 2>/dev/null)" && [ -n "${output}" ]; then
      printf '%s\n' "${output}"
      return 0
    fi
    duw_settle_block_state
    sleep "${delay_s}"
    try=$((try + 1))
  done
  duw_die "timed out waiting for the partition UUID on ${path}"
)

duw_device_partition_by_partlabel() (
  device=$1
  partlabel=$2
  rows=$(lsblk -prno PATH,PARTLABEL "${device}" 2>/dev/null || true)
  while read -r path value; do
    [ -n "${path}" ] && [ -n "${value}" ] || continue
    if [ "${value}" = "${partlabel}" ]; then printf '%s\n' "${path}"; return 0; fi
  done <<EOF_ROWS
${rows}
EOF_ROWS
  duw_die "failed to find a partition on ${device} with PARTLABEL=${partlabel}"
)

duw_device_partition_by_label() (
  device=$1
  label=$2
  rows=$(lsblk -prno PATH,LABEL "${device}" 2>/dev/null || true)
  while read -r path value; do
    [ -n "${path}" ] && [ -n "${value}" ] || continue
    if [ "${value}" = "${label}" ]; then printf '%s\n' "${path}"; return 0; fi
  done <<EOF_ROWS
${rows}
EOF_ROWS
  duw_die "failed to find a partition on ${device} with LABEL=${label}"
)

duw_payload_locator_for_render() (
  profile="$1"
  _media_class="$2"
  layout="$3"
  partition="$4"
  partuuid=""

  if [ "${layout}" = "raw-iso" ]; then
    case "${profile}" in
      ubuntu-desktop|ubuntu-server)
        duw_partition_uuid "${partition}"
        return 0
        ;;
    esac
    partuuid="$(duw_partition_partuuid "${partition}")"
    printf '/dev/disk/by-partuuid/%s\n' "${partuuid}"
    return 0
  fi

  duw_partition_uuid "${partition}"
)

duw_grub_install_modules() (
  printf '%s\n' "part_gpt part_msdos search search_fs_uuid search_fs_file search_label probe regexp fat ext2 btrfs xfs iso9660 configfile chain normal linux test crypto gcry_rsa gcry_sha256 pgp"
)

duw_install_grub_bootloader() (
  esp_part=$1
  esp_mount=$2
  set -- --target=x86_64-efi --efi-directory="${esp_mount}" --boot-directory="${esp_mount}/boot" --removable --no-nvram --recheck --modules="$(duw_grub_install_modules)"
  if [ "${DUSB_SECURE_BOOT_TRUST}" = mok ] && [ -f /usr/lib/shim/shimx64.efi.signed ] && [ -f /usr/lib/grub/x86_64-efi-signed/grubx64.efi.signed ]; then set -- "$@" --uefi-secure-boot; fi
  if [ -n "${DUSB_GRUB_GPG_PUBKEY:-}" ]; then set -- "$@" --pubkey="${DUSB_GRUB_GPG_PUBKEY}"; fi
  duw_require_mount_source "${esp_part}" "${esp_mount}"
  duw_step "Installing removable-path UEFI GRUB into ${esp_part} (mounted at ${esp_mount})"
  if output=$(grub-install "$@" 2>&1); then
    duw_stage_owned_removable_secure_boot_chain "${esp_mount}"
    return 0
  else
    exit_code=$?
    duw_die "failed to install the removable UEFI GRUB bootloader into ${esp_part} via ${esp_mount} (exit ${exit_code}): ${output}"
  fi
)

duw_install_multiboot_grub_bootloaders() (
  device=$1; data_part=$2; data_mount=$3; esp_part=$4; esp_mount=$5
  duw_require_mount_source "${data_part}" "${data_mount}"
  duw_require_mount_source "${esp_part}" "${esp_mount}"
  mkdir -p -- "${data_mount}/boot/grub" || duw_die "failed to create GRUB directory on ${data_mount}"
  duw_step "Installing BIOS GRUB into ${device} with boot directory on ${data_part}"
  set -- --target=i386-pc --boot-directory="${data_mount}/boot" --recheck
  if [ -n "${DUSB_GRUB_GPG_PUBKEY:-}" ]; then set -- "$@" --pubkey="${DUSB_GRUB_GPG_PUBKEY}"; fi
  if output=$(grub-install "$@" "${device}" 2>&1); then
    :
  else
    exit_code=$?
    duw_die "failed to install BIOS GRUB into ${device} (exit ${exit_code}): ${output}"
  fi

  set -- --target=x86_64-efi --efi-directory="${esp_mount}" --boot-directory="${data_mount}/boot" --removable --no-nvram --recheck --modules="$(duw_grub_install_modules)"
  if [ "${DUSB_SECURE_BOOT_TRUST}" = mok ] && [ -f /usr/lib/shim/shimx64.efi.signed ] && [ -f /usr/lib/grub/x86_64-efi-signed/grubx64.efi.signed ]; then set -- "$@" --uefi-secure-boot; fi
  if [ -n "${DUSB_GRUB_GPG_PUBKEY:-}" ]; then set -- "$@" --pubkey="${DUSB_GRUB_GPG_PUBKEY}"; fi
  duw_step "Installing removable-path UEFI GRUB into ${esp_part} with boot directory on ${data_part}"
  if output=$(grub-install "$@" 2>&1); then
    duw_stage_owned_removable_secure_boot_chain "${esp_mount}"
    return 0
  else
    exit_code=$?
    duw_die "failed to install removable-path UEFI GRUB via ${esp_mount} (exit ${exit_code}): ${output}"
  fi
)

duw_profile_payload_fs_label() (
  profile="$1"
  source_role="${2:-primary}"
  media_class="${3:-}"
  installer_like=0
  if [ -n "${DUSB_OVERRIDE_PAYLOAD_FS_LABEL:-}" ]; then
    printf '%s\n' "${DUSB_OVERRIDE_PAYLOAD_FS_LABEL}"
    return 0
  fi
  if [ "${source_role}" = "netinst" ] || [ "${media_class}" = "installer" ]; then
    installer_like=1
  fi
  case "${profile}" in
    debian)
      if [ "${source_role}" = "netboot" ]; then
        printf '%s\n' "${DEFAULT_DEBIAN_NETBOOT_LABEL}"
      elif [ "${installer_like}" -eq 1 ]; then
        printf '%s\n' "${DEFAULT_DEBIAN_NETINST_LABEL}"
      else
        printf '%s\n' "${DEFAULT_DEBIAN_LIVE_LABEL}"
      fi
      ;;
    ubuntu-desktop)
      if [ "${installer_like}" -eq "1" ]; then
        printf '%s\n' "${DEFAULT_UBUNTU_NETINST_LABEL}"
      else
        printf '%s\n' "${DEFAULT_UBUNTU_LIVE_LABEL}"
      fi
      ;;
	    ubuntu-server) printf '%s\n' "${DEFAULT_UBUNTU_NETINST_LABEL}" ;;
		    kali-linux)
		      if [ "${source_role}" = "netboot" ]; then
		        printf '%s\n' "${DEFAULT_KALI_NETBOOT_LABEL}"
		      elif [ "${installer_like}" -eq 1 ]; then
		        printf '%s\n' "${DEFAULT_KALI_NETINST_LABEL}"
		      else
		        printf '%s\n' "${DEFAULT_KALI_LIVE_LABEL}"
	      fi
	      ;;
	    kali-purple) printf '%s\n' "${DEFAULT_KALI_PURPLE_NETINST_LABEL}" ;;
	    tails) printf '%s\n' "${DEFAULT_TAILS_LIVE_LABEL}" ;;
	    *) printf 'PAYLOAD\n' ;;
	  esac
)

duw_profile_payload_partlabel() (
  if [ -n "${DUSB_OVERRIDE_PAYLOAD_PARTLABEL:-}" ]; then
    printf '%s\n' "${DUSB_OVERRIDE_PAYLOAD_PARTLABEL}"
    return 0
  fi
  duw_profile_payload_fs_label "$@"
)

duw_profile_payload_iso_volid() (
  duw_iso_volid_from_label "$(duw_profile_payload_fs_label "$@")"
)

duw_iso_volid_from_label() (
  LC_ALL=C
  export LC_ALL
  printf '%s' "$1" | tr '[:lower:]' '[:upper:]' | sed 's/[^A-Z0-9]/_/g' | cut -c 1-32
  printf '\n'
)

duw_profile_persistence_fs_label() (
  profile="$1"
  if [ -n "${DUSB_OVERRIDE_PERSISTENCE_FS_LABEL:-}" ]; then
    printf '%s\n' "${DUSB_OVERRIDE_PERSISTENCE_FS_LABEL}"
    return 0
  fi
	  case "${profile}" in
	    ubuntu-desktop) printf '%s\n' "${DEFAULT_UBUNTU_PERSIST_LABEL}" ;;
	    debian) printf '%s\n' "${DEFAULT_DEBIAN_PERSIST_LABEL}" ;;
	    kali-linux) printf '%s\n' "${DEFAULT_KALI_PERSIST_LABEL}" ;;
	    tails) printf '%s\n' "${DEFAULT_TAILS_PERSIST_LABEL}" ;;
	    *) duw_die "profile does not support persistence: ${profile}" ;;
	  esac
)

duw_profile_persistence_partlabel() (
  profile="$1"
  if [ -n "${DUSB_OVERRIDE_PERSISTENCE_PARTLABEL:-}" ]; then
    printf '%s\n' "${DUSB_OVERRIDE_PERSISTENCE_PARTLABEL}"
    return 0
  fi
	case "${profile}" in
	    ubuntu-desktop) printf '%s\n' "${DEFAULT_UBUNTU_PERSIST_PARTLABEL}" ;;
	    debian) printf '%s\n' "${DEFAULT_DEBIAN_PERSIST_LABEL}" ;;
	    kali-linux) printf '%s\n' "${DEFAULT_KALI_PERSIST_LABEL}" ;;
	    tails) printf '%s\n' "${DEFAULT_TAILS_PERSIST_LABEL}" ;;
	    *) printf '\n' ;;
	  esac
)

duw_shared_data_fs_label() (
  printf '%s\n' "${DEFAULT_MULTI_DATA_LABEL}"
)

duw_shared_data_partlabel() (
  printf '%s\n' "${DEFAULT_MULTI_DATA_LABEL}"
)

duw_profile_shared_data_root() (
  profile="$1"
  source_role="${2:-primary}"
  media_class="${3:-}"
  installer_like=0
  if [ "${source_role}" = "netinst" ] || [ "${media_class}" = "installer" ]; then
    installer_like=1
  fi
  case "${profile}" in
    debian)
      if [ "${source_role}" = "netboot" ]; then
        printf '%s\n' "/debian-netboot"
      elif [ "${installer_like}" -eq 1 ]; then
        printf '%s\n' "/debian-netinst"
      else
        printf '%s\n' "/debian-live"
      fi
      ;;
    kali-linux)
      if [ "${source_role}" = "netboot" ]; then
        printf '%s\n' "/kali-netboot"
      elif [ "${installer_like}" -eq 1 ]; then
        printf '%s\n' "/kali-netinst"
      else
        printf '%s\n' "/kali-live"
      fi
      ;;
    kali-purple) printf '%s\n' "/kali-purple-installer" ;;
    tails) printf '%s\n' "/tails-live" ;;
    ubuntu-desktop)
      if [ "${installer_like}" -eq "1" ]; then
        printf '%s\n' "/ubuntu"
      else
        printf '%s\n' "/ubuntu"
      fi
      ;;
    ubuntu-server) printf '%s\n' "/ubuntu-server" ;;
    *) printf '%s\n' "/payload" ;;
  esac
)

duw_profile_persistence_template_path() (
  profile=$1
  case ${profile} in
    debian) candidate=persistence-debian.conf ;;
    kali-linux) candidate=persistence-kali.conf ;;
    tails) candidate=persistence-tails.conf ;;
    *) printf '\n'; return 0 ;;
  esac
  if [ -n "${DEBIAN_USB_PERSISTENCE_DIR:-}" ] && [ -f "${DEBIAN_USB_PERSISTENCE_DIR}/${candidate}" ]; then printf '%s\n' "${DEBIAN_USB_PERSISTENCE_DIR}/${candidate}"; return 0; fi
  script_dir=$(CDPATH='' cd -P "$(dirname "$0")" && pwd -P)
  for base_dir in "${script_dir}/.." "${script_dir}"; do
    if [ -f "${base_dir}/configs/${candidate}" ]; then printf '%s\n' "${base_dir}/configs/${candidate}"; return 0; fi
  done
  if [ -n "${PERSISTENCE_DIR:-}" ]; then
    case ${profile} in debian) candidate=debian.conf ;; kali-linux) candidate=kali.conf ;; tails) candidate=tails.conf ;; esac
    if [ -f "${PERSISTENCE_DIR}/${candidate}" ]; then printf '%s\n' "${PERSISTENCE_DIR}/${candidate}"; return 0; fi
  fi
  duw_die "missing managed persistence template for ${profile}"
)

duw_write_profile_persistence_conf() (
  profile="$1"
  persist_mount="$2"
  template_path=""

  if [ "${profile}" = "tails" ]; then
    duw_step "Skipping persistence.conf for Tails; Tails manages encrypted Persistent Storage internally"
    return 0
  fi
  template_path="$(duw_profile_persistence_template_path "${profile}")"
  [ -n "${template_path}" ] || duw_die "profile does not provide a persistence template: ${profile}"
  duw_step "Writing persistence.conf into ${persist_mount}"
  cp -f -- "${template_path}" "${persist_mount}/persistence.conf" || \
    duw_die "failed to copy persistence template ${template_path} into ${persist_mount}/persistence.conf"
)

duw_configure_persistence_partition() (
  profile="$1"
  fs_label="$2"
  persist_part="$3"
  persist_mount="$4"
  target_device="${5:-}"
  duw_step "Configuring plain persistence on ${persist_part}"
  duw_make_ext4_filesystem "${persist_part}" "${fs_label}" "${target_device}"
  if [ "${profile}" = "ubuntu-desktop" ]; then
    return 0
  fi
  sync
  duw_wait_for_filesystem_signature "${persist_part}"
  duw_mount_partition "${persist_part}" "${persist_mount}" ext4
  duw_write_profile_persistence_conf "${profile}" "${persist_mount}"
  sync
  umount -- "${persist_mount}"
)

duw_configure_encrypted_persistence_partition() (
  persist_part=$1
  persist_mount=$2
  mapper_name=${3:-debian-usb-persist}
  profile=${4:-debian}
  fs_label=${5:-DEBIAN-PERSIST}
  key_file=${6:-}
  target_device=${7:-}
  mapper_path=/dev/mapper/${mapper_name}
  ephemeral_key_root=
  cleanup_key_file=0
  # The function is invoked by the trap installed below.
  # shellcheck disable=SC2329
  cleanup_encrypted_persistence() {
    mountpoint -q "${persist_mount}" && umount -- "${persist_mount}" || true
    cryptsetup status "${mapper_name}" >/dev/null 2>&1 && cryptsetup luksClose "${mapper_name}" || true
    if [ "${cleanup_key_file}" -eq 1 ]; then rm -rf -- "${ephemeral_key_root}" || true; fi
  }
  trap cleanup_encrypted_persistence 0 INT TERM
  duw_step "Configuring LUKS encrypted persistence on ${persist_part}"
  if [ -z "${key_file}" ]; then
    ephemeral_key_root=$(duw_create_temp_root luks-passphrase)
    key_file=${ephemeral_key_root}/passphrase
    duw_collect_luks_passphrase_file "${persist_part}" "${key_file}"
    cleanup_key_file=1
  fi
  duw_close_stale_mapper "${mapper_name}"
  cryptsetup --batch-mode --type luks2 --key-file "${key_file}" luksFormat "${persist_part}"
  cryptsetup luksOpen --key-file "${key_file}" "${persist_part}" "${mapper_name}"
  mapper_path=$(duw_wait_for_crypt_mapper "${mapper_name}" 60 0.2)
  duw_make_ext4_filesystem "${mapper_path}" "${fs_label}" "${target_device}"
  sync
  duw_wait_for_filesystem_signature "${mapper_path}"
  duw_mount_partition "${mapper_path}" "${persist_mount}" ext4
  duw_write_profile_persistence_conf "${profile}" "${persist_mount}"
  sync
  umount -- "${persist_mount}"
  cryptsetup luksClose "${mapper_name}"
  if [ "${cleanup_key_file}" -eq 1 ]; then rm -rf -- "${ephemeral_key_root}" || true; fi
  trap - 0 INT TERM
)

duw_json_field() (
  payload="$1"
  key="$2"
  duw_json_python "${payload}" "${key}" <<'PY'
import json
import sys

data = json.load(open(3, encoding="utf-8"))
value = data[sys.argv[1]]
if isinstance(value, str):
    sys.stdout.write(value)
else:
    sys.stdout.write(str(value))
PY
)

duw_json_python() (
  payload=$1
  shift
  payload_file=$(mktemp "${TMPDIR:-/tmp}/debian-usb-json.XXXXXX") || duw_die "failed to create JSON payload file"
  trap 'rm -f -- "${payload_file}"' 0 INT TERM
  printf '%s' "${payload}" >"${payload_file}"
  python3 - "$@" 3<"${payload_file}"
)

duw_json_signed_assets_tsv() (
  payload="$1"
  duw_json_python "${payload}" <<'PY'
import json

data = json.load(open(3, encoding="utf-8"))
for item in data.get("signed_kernel_assets", []):
    print("\t".join((str(item["iso_path"]), str(item["source_path"]), str(item["asset_path"]))))
PY
)

duw_json_boot_initrd_assets_tsv() (
  payload="$1"
  duw_json_python "${payload}" <<'PY'
import json

data = json.load(open(3, encoding="utf-8"))
for item in data.get("boot_initrd_assets", []):
    print("\t".join((str(item["iso_path"]), str(item["source_path"]), str(item["asset_path"]))))
PY
)

duw_json_direct_secure_boot_asset_count() (
  payload="$1"
  duw_json_python "${payload}" <<'PY'
import json

data = json.load(open(3, encoding="utf-8"))
print(len(data.get("signed_kernel_assets", [])) + len(data.get("boot_initrd_assets", [])))
PY
)

duw_render_payload_needs_direct_secure_boot_assets() (
  count=$(duw_json_direct_secure_boot_asset_count "$1")
  case ${count} in ''|*[!0-9]*) duw_die "invalid direct Secure Boot asset count: ${count}" ;; esac
  [ "${count}" -gt 0 ]
)

duw_json_installer_boot_initrd_patches_tsv() (
  payload="$1"
  duw_json_python "${payload}" <<'PY'
import json

data = json.load(open(3, encoding="utf-8"))
for item in data.get("installer_boot_initrd_patches", []):
    print("\t".join((str(item["iso_path"]), str(item["source_path"]), str(item["asset_path"]))))
PY
)

duw_json_iso_payloads_tsv() (
  payload="$1"
  source_filter="${2:-}"
  duw_json_python "${payload}" "${source_filter}" <<'PY'
import json
import sys

data = json.load(open(3, encoding="utf-8"))
source_filter = sys.argv[1]
seen = set()
for item in data.get("iso_payloads", []):
    row = (str(item["source_path"]), str(item["target_path"]))
    if source_filter and row[0] != source_filter:
        continue
    if row in seen:
        continue
    seen.add(row)
    print("\t".join(row))
PY
)

duw_json_payload_boot_assets_tsv() (
  payload="$1"
  source_filter="${2:-}"
  duw_json_python "${payload}" "${source_filter}" <<'PY'
import json
import sys

data = json.load(open(3, encoding="utf-8"))
source_filter = sys.argv[1]
seen = set()
for item in data.get("payload_boot_assets", []):
    row = (str(item["iso_path"]), str(item["source_path"]), str(item["target_path"]))
    if source_filter and row[0] != source_filter:
        continue
    if row in seen:
        continue
    seen.add(row)
    print("\t".join(row))
PY
)

duw_json_payload_extra_assets_tsv() (
  payload="$1"
  source_filter="${2:-}"
  duw_json_python "${payload}" "${source_filter}" <<'PY'
import json
import sys

data = json.load(open(3, encoding="utf-8"))
source_filter = sys.argv[1]
seen = set()
for item in data.get("payload_extra_assets", []):
    row = (str(item["iso_path"]), str(item["source_path"]), str(item["target_path"]))
    if source_filter and row[0] != source_filter:
        continue
    if row in seen:
        continue
    seen.add(row)
    print("\t".join(row))
PY
)

duw_iso_member_exists() (
  iso_path="$1"
  member_path="$2"

  [ -e "${iso_path}" ] || return 1
  case ${member_path} in /*) ;; *) return 1 ;; esac
  if [ -d "${iso_path}" ]; then
    [ -e "${iso_path}${member_path}" ]
    return
  fi
  [ -f "${iso_path}" ] && [ -s "${iso_path}" ] || return 1
  xorriso -indev "${iso_path}" -find "${member_path}" -exec report_lba >/dev/null 2>&1
)

duw_iso_member_size_bytes() (
  iso_path=$1
  member_path=$2
  [ -e "${iso_path}" ] || duw_die "media source is missing: ${iso_path}"
  case ${member_path} in /*) ;; *) duw_die "refusing to measure non-absolute ISO member path: ${member_path}" ;; esac
  if [ -d "${iso_path}" ]; then
    source_path=${iso_path}${member_path}
    [ -f "${source_path}" ] || duw_die "media member is missing or not a regular file: ${source_path}"
    stat -c '%s' -- "${source_path}" || duw_die "failed to stat media member size: ${source_path}"
    return 0
  fi
  [ -f "${iso_path}" ] && [ -s "${iso_path}" ] || duw_die "ISO source is missing or empty: ${iso_path}"
  if output=$(xorriso -indev "${iso_path}" -find "${member_path}" -exec report_lba 2>&1); then :; else exit_code=$?; duw_die "failed to measure ${member_path} in ${iso_path} (exit ${exit_code}): ${output}"; fi
  size=$(printf '%s\n' "${output}" | awk -F',' '/^File data lba:/ { value=$4; gsub(/^[[:space:]]+|[[:space:]]+$/, "", value); print value; found=1; exit } END { if (!found) exit 1 }') || duw_die "failed to parse size for ${member_path} in ${iso_path}: ${output}"
  case ${size} in ''|*[!0-9]*) duw_die "unexpected size for ${member_path} in ${iso_path}: ${size}" ;; esac
  printf '%s\n' "${size}"
)

duw_media_source_size_bytes() (
  source_path="$1"

  [ -e "${source_path}" ] || duw_die "media source is missing: ${source_path}"
  if [ -d "${source_path}" ]; then
    du -sb -- "${source_path}" | awk '{print $1}' || duw_die "failed to measure media source directory: ${source_path}"
    return 0
  fi
  [ -f "${source_path}" ] && [ -s "${source_path}" ] || duw_die "media source file is missing or empty: ${source_path}"
  stat -c '%s' -- "${source_path}" || duw_die "failed to stat media source file: ${source_path}"
)

duw_render_payload_grub_cfg_bytes() (
  payload="$1"
  duw_json_field "${payload}" "grub_cfg" | wc -c | tr -d '[:space:]'
)

duw_render_payload_esp_required_bytes() (
  payload=$1
  preseed_mib=${2:-0}
  list_separator=$(printf '\037')
  case ${preseed_mib} in ''|*[!0-9]*) duw_die "invalid ESP preseed size: ${preseed_mib}" ;; esac
  total=$(($(duw_esp_base_reserve_mib) * 1024 * 1024 + preseed_mib * 1024 * 1024))
  for host_asset in /usr/lib/shim/shimx64.efi.signed /usr/lib/shim/mmx64.efi.signed /usr/lib/grub/x86_64-efi-signed/grubx64.efi.signed; do
    [ -f "${host_asset}" ] || duw_die "required Secure Boot host asset is missing: ${host_asset}"
    total=$((total + $(stat -c '%s' "${host_asset}")))
  done
  grub_bytes=$(duw_render_payload_grub_cfg_bytes "${payload}")
  case ${grub_bytes} in ''|*[!0-9]*) duw_die "invalid rendered GRUB config size: ${grub_bytes}" ;; esac
  total=$((total + grub_bytes))

  patch_initrds=
  rows=$(duw_json_installer_boot_initrd_patches_tsv "${payload}")
  while IFS=$(printf '\t') read -r iso_path member_path asset_path; do
    [ -n "${iso_path}" ] && [ -n "${member_path}" ] && [ -n "${asset_path}" ] || continue
    case ${member_path} in /*) ;; *) duw_die "refusing to patch non-absolute initrd member path: ${member_path}" ;; esac
    case ${asset_path} in /EFI/debian-usb/assets/*) ;; *) duw_die "refusing to patch initrd outside ESP boot assets: ${asset_path}" ;; esac
    patch_initrds=${patch_initrds}${patch_initrds:+${list_separator}}${asset_path}
  done <<EOF_ROWS
${rows}
EOF_ROWS

  seen_assets=
  rows=$(duw_json_signed_assets_tsv "${payload}")
  while IFS=$(printf '\t') read -r iso_path member_path asset_path; do
    [ -n "${iso_path}" ] && [ -n "${member_path}" ] && [ -n "${asset_path}" ] || continue
    case ${asset_path} in /EFI/debian-usb/assets/*) ;; *) duw_die "refusing to size signed asset outside ESP boot assets: ${asset_path}" ;; esac
    duw_internal_list_contains "${seen_assets}" "${list_separator}" "${asset_path}" && continue
    seen_assets=${seen_assets}${seen_assets:+${list_separator}}${asset_path}
    asset_size=$(duw_iso_member_size_bytes "${iso_path}" "${member_path}")
    total=$((total + asset_size + 2 * 1024 * 1024))
  done <<EOF_ROWS
${rows}
EOF_ROWS

  rows=$(duw_json_boot_initrd_assets_tsv "${payload}")
  while IFS=$(printf '\t') read -r iso_path member_path asset_path; do
    [ -n "${iso_path}" ] && [ -n "${member_path}" ] && [ -n "${asset_path}" ] || continue
    case ${asset_path} in /EFI/debian-usb/assets/*) ;; *) duw_die "refusing to size initrd outside ESP boot assets: ${asset_path}" ;; esac
    duw_internal_list_contains "${seen_assets}" "${list_separator}" "${asset_path}" && continue
    seen_assets=${seen_assets}${seen_assets:+${list_separator}}${asset_path}
    asset_size=$(duw_iso_member_size_bytes "${iso_path}" "${member_path}")
    total=$((total + asset_size + 1024 * 1024))
    if duw_internal_list_contains "${patch_initrds}" "${list_separator}" "${asset_path}"; then total=$((total + 2 * 1024 * 1024)); fi
  done <<EOF_ROWS
${rows}
EOF_ROWS
  printf '%s\n' "${total}"
)

duw_required_esp_size_mib_from_render_payload() (
  payload="$1"
  preseed_mib="${2:-0}"
  required_bytes="$(duw_render_payload_esp_required_bytes "${payload}" "${preseed_mib}")"
  required_mib="$(duw_bytes_to_mib "${required_bytes}")"
  min_mib="$(duw_esp_min_size_mib)"
  if [ "${required_mib}" -lt "${min_mib}" ]; then
    required_mib="${min_mib}"
  fi
  duw_round_mib_up "${required_mib}" 64
)

duw_note_dynamic_esp_size() (
  esp_size_mib="$1"
  min_mib="$(duw_esp_min_size_mib)"
  if [ "${esp_size_mib}" -gt "${min_mib}" ]; then
    duw_note "Managed ESP size increased to ${esp_size_mib} MiB for rendered GRUB, Secure Boot assets, initrds, and preseed content"
  fi
)

duw_extract_iso_member() (
  iso_path="$1"
  member_path="$2"
  destination="$3"
  destination_dir=""
  extract_root=""
  temp_member=""
  output=""
  exit_code=0

  [ -e "${iso_path}" ] || duw_die "media source is missing: ${iso_path}"
  case ${member_path} in /*) ;; *) duw_die "refusing to extract non-absolute ISO member path: ${member_path}" ;; esac
  [ -n "${destination}" ] && [ "${destination}" != "/" ] || duw_die "refusing unsafe ISO member destination: ${destination}"
  destination_dir="$(dirname -- "${destination}")"
  mkdir -p -- "${destination_dir}" || duw_die "failed to create ISO member destination directory: ${destination_dir}"
  if [ -d "${iso_path}" ]; then
    temp_member="${iso_path}${member_path}"
    [ -e "${temp_member}" ] || duw_die "missing extracted media member ${member_path} in ${iso_path}"
    cp -f -- "${temp_member}" "${destination}" || duw_die "failed to copy extracted media member ${member_path} to ${destination}"
    return 0
  fi
  [ -f "${iso_path}" ] && [ -s "${iso_path}" ] || duw_die "ISO source is missing or empty: ${iso_path}"
  extract_root="$(duw_create_temp_root "iso-extract")"
  temp_member="${extract_root}/member"
  if output=$(xorriso -osirrox on -indev "${iso_path}" -extract "${member_path}" "${temp_member}" 2>&1); then
    cp -f -- "${temp_member}" "${destination}" || {
      exit_code=$?
      rm -rf -- "${extract_root}" || true
      duw_die "failed to copy extracted ISO member ${member_path} to ${destination} (exit ${exit_code})"
    }
    rm -rf -- "${extract_root}" || true
    return 0
  else
    exit_code=$?
    rm -rf -- "${extract_root}" || true
    duw_die "failed to extract ${member_path} from ${iso_path} (exit ${exit_code}): ${output}"
  fi
)

duw_lock_down_payload_dir() (
  path="$1"
  [ -d "${path}" ] || return 0
  chown root:root -- "${path}" || duw_die "failed to set payload directory owner: ${path}"
  chmod 0700 -- "${path}" || duw_die "failed to set payload directory mode: ${path}"
)

duw_lock_down_payload_file() (
  path="$1"
  [ -f "${path}" ] || return 0
  chown root:root -- "${path}" || duw_die "failed to set payload file owner: ${path}"
  chmod 0600 -- "${path}" || duw_die "failed to set payload file mode: ${path}"
)

duw_stage_iso_store_payloads_from_render_payload() (
  payload=$1; payload_mount=$2; source_filter=${3:-}
  rows=$(duw_json_iso_payloads_tsv "${payload}" "${source_filter}")
  while IFS=$(printf '\t') read -r source_iso target_path; do
    [ -n "${source_iso}" ] && [ -n "${target_path}" ] || continue
    case ${target_path} in /*) ;; *) duw_die "refusing to stage ISO at a non-absolute managed path: ${target_path}" ;; esac
    [ -f "${source_iso}" ] && [ -s "${source_iso}" ] || duw_die "rendered ISO source is missing or empty: ${source_iso}"
    destination=${payload_mount}${target_path}
    mkdir -p -- "$(dirname "${destination}")" || duw_die "failed to create ISO payload directory: ${destination}"
    duw_lock_down_payload_dir "$(dirname "${destination}")"
    duw_step "Staging ISO payload ${source_iso} -> ${target_path}"
    cp -f -- "${source_iso}" "${destination}" || duw_die "failed to stage ISO payload at ${target_path}"
    duw_lock_down_payload_file "${destination}"
    duw_sign_grub_data_file "${destination}"
    duw_lock_down_payload_file "${destination}.sig"
  done <<EOF_ROWS
${rows}
EOF_ROWS
)

duw_remove_shared_store_media_metadata() (
  data_mount=$1
  [ -n "${data_mount}" ] && [ "${data_mount}" != / ] && [ -d "${data_mount}" ] || duw_die "refusing unsafe shared ISO-store metadata cleanup path: ${data_mount}"
  for relative_path in .disk live casper install install.amd d-i debian-installer; do
    if [ -e "${data_mount}/${relative_path}" ] || [ -L "${data_mount}/${relative_path}" ]; then
      duw_step "Removing stale root-level installation-media path from the shared ISO store: /${relative_path}"
      rm -rf -- "${data_mount:?}/${relative_path}" || duw_die "failed to remove stale shared ISO-store media path: ${data_mount}/${relative_path}"
    fi
  done
  for relative_path in .disk live casper install install.amd d-i debian-installer; do
    [ ! -e "${data_mount}/${relative_path}" ] && [ ! -L "${data_mount}/${relative_path}" ] || duw_die "shared ISO store must not expose root-level installation-media path: /${relative_path}"
  done
)

duw_stage_iso_store_boot_assets_from_render_payload() (
  payload=$1; payload_mount=$2; source_filter=${3:-}
  rows=$(duw_json_payload_boot_assets_tsv "${payload}" "${source_filter}")
  while IFS=$(printf '\t') read -r source_iso member_path target_path; do
    [ -n "${source_iso}" ] && [ -n "${member_path}" ] && [ -n "${target_path}" ] || continue
    case ${target_path} in /*) ;; *) duw_die "refusing to stage a boot asset at a non-absolute managed path: ${target_path}" ;; esac
    destination=${payload_mount}${target_path}
    if [ -d "${source_iso}" ]; then source_desc=${source_iso}${member_path}; else source_desc=${source_iso}:${member_path}; fi
    duw_step "Staging boot asset ${source_desc} -> ${target_path}"
    duw_extract_iso_member "${source_iso}" "${member_path}" "${destination}"
    duw_lock_down_payload_dir "$(dirname "${destination}")"
    duw_lock_down_payload_file "${destination}"
    duw_sign_grub_data_file "${destination}"
    duw_lock_down_payload_file "${destination}.sig"
  done <<EOF_ROWS
${rows}
EOF_ROWS
)

duw_stage_payload_extra_assets_from_render_payload() (
  payload=$1; payload_mount=$2; source_filter=${3:-}
  rows=$(duw_json_payload_extra_assets_tsv "${payload}" "${source_filter}")
  while IFS=$(printf '\t') read -r source_iso member_path target_path; do
    [ -n "${source_iso}" ] && [ -n "${member_path}" ] && [ -n "${target_path}" ] || continue
    case ${target_path} in /*) ;; *) duw_die "refusing to stage an extra payload asset at a non-absolute managed path: ${target_path}" ;; esac
    destination=${payload_mount}${target_path}
    duw_step "Staging payload asset ${member_path} -> ${target_path}"
    duw_extract_iso_member "${source_iso}" "${member_path}" "${destination}"
    duw_lock_down_payload_dir "$(dirname "${destination}")"
    duw_lock_down_payload_file "${destination}"
  done <<EOF_ROWS
${rows}
EOF_ROWS
)

duw_stage_iso_store_from_render_payload() (
  payload="$1"
  payload_mount="$2"
  boot_mount="$3"
  source_filter="${4:-}"
  duw_stage_iso_store_payloads_from_render_payload "${payload}" "${payload_mount}" "${source_filter}"
  duw_stage_iso_store_boot_assets_from_render_payload "${payload}" "${boot_mount}" "${source_filter}"
  duw_stage_payload_extra_assets_from_render_payload "${payload}" "${payload_mount}" "${source_filter}"
)

duw_validate_absolute_managed_dir() (
  path="$1"
  label="$2"
  case ${path} in /*) ;; *) duw_die "${label} must be an absolute path: ${path}" ;; esac
  case ${path} in */../*|../*|*/..) duw_die "${label} must not contain parent-directory segments: ${path}" ;; esac
  [ "${path}" != "/" ] || duw_die "${label} must not be /"
)

duw_validate_secure_boot_trust() (
  case $1 in mok|firmware-db) ;; *) duw_die "secure boot trust must be mok or firmware-db: $1" ;; esac
)

duw_effective_secure_boot_trust() (
  value="${1:-mok}"
  if [ -z "${value}" ]; then
    value="mok"
  fi
  duw_validate_secure_boot_trust "${value}"
  printf '%s\n' "${value}"
)

duw_secure_boot_mok_dir() (
  mok_dir="${DEBIAN_USB_MOK_DIR:-/data/cfg/debian-usb/secureboot/mok}"
  duw_validate_absolute_managed_dir "${mok_dir}" "DEBIAN_USB_MOK_DIR"
  printf '%s\n' "${mok_dir}"
)

duw_secure_boot_pki_dir() (
  pki_dir="${DEBIAN_USB_SECURE_BOOT_PKI_DIR:-/data/pki/secureboot}"
  duw_validate_absolute_managed_dir "${pki_dir}" "DEBIAN_USB_SECURE_BOOT_PKI_DIR"
  printf '%s\n' "${pki_dir}"
)

duw_secure_boot_support_root_dir() (
  if [ "${DUSB_SECURE_BOOT_TRUST}" = "mok" ]; then
    dirname -- "$(duw_secure_boot_mok_dir)"
    return 0
  fi
  duw_secure_boot_pki_dir
)

duw_uuid4() (
  python3 - <<'PY'
import uuid

print(uuid.uuid4())
PY
)

duw_valid_uuid() (
  value=$1
  [ "${#value}" -eq 36 ] || return 1
  printf '%s\n' "${value}" | grep -Eq '^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}$'
)

duw_valid_openpgp_fingerprint() (
  value=$1
  [ "${#value}" -eq 40 ] || return 1
  printf '%s\n' "${value}" | grep -Eq '^[0-9A-Fa-f]{40}$'
)

duw_generate_secure_boot_keypair() {
  _duw_mok_output_dir=$1
  _duw_mok_old_umask=
  _duw_mok_key_bits=${DEBIAN_USB_MOK_KEY_BITS:-2048}
  case ${_duw_mok_key_bits} in 2048|3072|4096) ;; *) duw_die "DEBIAN_USB_MOK_KEY_BITS must be one of: 2048, 3072, 4096" ;; esac
  install -d -m 0700 -- "${_duw_mok_output_dir}" || duw_die "failed to create secure boot output directory: ${_duw_mok_output_dir}"
  DUSB_SB_KEY=${_duw_mok_output_dir}/MOK.priv
  DUSB_SB_CERT_PEM=${_duw_mok_output_dir}/MOK.pem
  DUSB_SB_CERT_DER=${_duw_mok_output_dir}/MOK.der
  DUSB_SB_CERT_ESL=${_duw_mok_output_dir}/MOK.esl
  DUSB_SB_CERT_GUID=${_duw_mok_output_dir}/MOK.guid
  _duw_mok_owner_guid=$(duw_uuid4)
  _duw_mok_old_umask=$(umask)
  umask 077
  openssl req \
    -new -x509 -newkey "rsa:${_duw_mok_key_bits}" -sha256 -days 3650 -nodes \
    -subj "/CN=debian-usb Secure Boot/" \
    -addext "basicConstraints=critical,CA:FALSE" \
    -addext "keyUsage=critical,digitalSignature" \
    -addext "extendedKeyUsage=codeSigning" \
    -keyout "${DUSB_SB_KEY}" \
    -out "${DUSB_SB_CERT_PEM}" >/dev/null 2>&1 || duw_die "failed to generate Secure Boot MOK key pair"
  printf '%s\n' "${_duw_mok_owner_guid}" >"${DUSB_SB_CERT_GUID}" || duw_die "failed to write Secure Boot MOK owner GUID"
  umask "${_duw_mok_old_umask}"
  chmod 0600 "${DUSB_SB_KEY}" || duw_die "failed to secure generated MOK private key: ${DUSB_SB_KEY}"
  duw_regenerate_secure_boot_mok_public_artifacts
}

duw_regenerate_secure_boot_mok_public_artifacts() (
  [ -f "${DUSB_SB_CERT_PEM}" ] && [ -s "${DUSB_SB_CERT_PEM}" ] || duw_die "MOK.pem is missing or empty: ${DUSB_SB_CERT_PEM}"
  if [ ! -f "${DUSB_SB_CERT_GUID}" ] || [ ! -s "${DUSB_SB_CERT_GUID}" ]; then
    duw_uuid4 >"${DUSB_SB_CERT_GUID}" || duw_die "failed to create Secure Boot MOK owner GUID"
  fi
  owner_guid=$(tr -d '[:space:]' <"${DUSB_SB_CERT_GUID}")
  duw_valid_uuid "${owner_guid}" || duw_die "invalid Secure Boot MOK owner GUID in ${DUSB_SB_CERT_GUID}: ${owner_guid}"
  openssl x509 -outform DER -in "${DUSB_SB_CERT_PEM}" -out "${DUSB_SB_CERT_DER}" >/dev/null 2>&1 || duw_die "failed to export Secure Boot MOK certificate"
  cert-to-efi-sig-list -g "${owner_guid}" "${DUSB_SB_CERT_PEM}" "${DUSB_SB_CERT_ESL}" >/dev/null 2>&1 || duw_die "failed to export Secure Boot MOK.esl from ${DUSB_SB_CERT_PEM}"
  chmod 0600 "${DUSB_SB_KEY}" || duw_die "failed to secure MOK private key: ${DUSB_SB_KEY}"
  chmod 0644 "${DUSB_SB_CERT_PEM}" "${DUSB_SB_CERT_DER}" "${DUSB_SB_CERT_ESL}" "${DUSB_SB_CERT_GUID}" || duw_die "failed to set MOK public artifact permissions"
)

duw_prepare_secure_boot_keypair() {
  _duw_mok_dir=$(duw_secure_boot_mok_dir)
  DUSB_SB_KEY=${_duw_mok_dir}/MOK.priv
  DUSB_SB_CERT_PEM=${_duw_mok_dir}/MOK.pem
  DUSB_SB_CERT_DER=${_duw_mok_dir}/MOK.der
  DUSB_SB_CERT_ESL=${_duw_mok_dir}/MOK.esl
  DUSB_SB_CERT_GUID=${_duw_mok_dir}/MOK.guid
  if [ -f "${DUSB_SB_KEY}" ] || [ -f "${DUSB_SB_CERT_PEM}" ] || [ -f "${DUSB_SB_CERT_DER}" ] || [ -f "${DUSB_SB_CERT_ESL}" ]; then
    [ -f "${DUSB_SB_KEY}" ] && [ -f "${DUSB_SB_CERT_PEM}" ] || duw_die "incomplete persistent Secure Boot MOK keypair under ${_duw_mok_dir}; expected MOK.priv and MOK.pem"
    [ -s "${DUSB_SB_KEY}" ] && [ -s "${DUSB_SB_CERT_PEM}" ] || duw_die "persistent Secure Boot MOK keypair under ${_duw_mok_dir} contains empty files"
    duw_regenerate_secure_boot_mok_public_artifacts
    DUSB_SB_DB_AUTH_KEY=${DUSB_SB_KEY}
    DUSB_SB_DB_AUTH_CERT_PEM=${DUSB_SB_CERT_PEM}
    DUSB_SB_DB_AUTH_LABEL='generated MOK'
    duw_note "Using persistent generated MOK certificate from ${DUSB_SB_CERT_DER}"
    return 0
  fi
  duw_generate_secure_boot_keypair "${_duw_mok_dir}"
  DUSB_SB_DB_AUTH_KEY=${DUSB_SB_KEY}
  DUSB_SB_DB_AUTH_CERT_PEM=${DUSB_SB_CERT_PEM}
  DUSB_SB_DB_AUTH_LABEL='generated MOK'
  duw_note "Generated persistent MOK certificate at ${DUSB_SB_CERT_DER}; enroll this same certificate once on target Secure Boot hosts"
}

duw_generate_secure_boot_owner_db() {
  _duw_db_output_dir=$1
  _duw_db_old_umask=
  _duw_db_key_bits=${DEBIAN_USB_DB_KEY_BITS:-2048}
  case ${_duw_db_key_bits} in 2048|3072|4096) ;; *) duw_die "DEBIAN_USB_DB_KEY_BITS must be one of: 2048, 3072, 4096" ;; esac
  install -d -m 0700 -- "${_duw_db_output_dir}" || duw_die "failed to create Secure Boot PKI directory: ${_duw_db_output_dir}"
  DUSB_SB_KEY=${_duw_db_output_dir}/db.key
  DUSB_SB_CERT_PEM=${_duw_db_output_dir}/db.crt
  DUSB_SB_CERT_DER=${_duw_db_output_dir}/db.cer
  DUSB_SB_CERT_ESL=${_duw_db_output_dir}/db.esl
  DUSB_SB_CERT_GUID=${_duw_db_output_dir}/db.guid
  _duw_db_owner_guid=$(duw_uuid4)
  _duw_db_old_umask=$(umask)
  umask 077
  openssl req \
    -new -x509 -newkey "rsa:${_duw_db_key_bits}" -sha256 -days 3650 -nodes \
    -subj "/CN=debian-usb Secure Boot db/" \
    -addext "basicConstraints=critical,CA:FALSE" \
    -addext "keyUsage=critical,digitalSignature" \
    -addext "extendedKeyUsage=codeSigning" \
    -keyout "${DUSB_SB_KEY}" \
    -out "${DUSB_SB_CERT_PEM}" >/dev/null 2>&1 || duw_die "failed to generate Secure Boot owner db key pair"
  printf '%s\n' "${_duw_db_owner_guid}" >"${DUSB_SB_CERT_GUID}" || duw_die "failed to write Secure Boot db owner GUID"
  umask "${_duw_db_old_umask}"
  chmod 0600 "${DUSB_SB_KEY}" || duw_die "failed to secure Secure Boot db private key: ${DUSB_SB_KEY}"
  duw_regenerate_secure_boot_owner_db_public_artifacts
}

duw_regenerate_secure_boot_owner_db_public_artifacts() (
  [ -f "${DUSB_SB_CERT_PEM}" ] && [ -s "${DUSB_SB_CERT_PEM}" ] || duw_die "Secure Boot db.crt is missing or empty: ${DUSB_SB_CERT_PEM}"
  if [ ! -f "${DUSB_SB_CERT_GUID}" ] || [ ! -s "${DUSB_SB_CERT_GUID}" ]; then
    duw_uuid4 >"${DUSB_SB_CERT_GUID}" || duw_die "failed to create Secure Boot db owner GUID"
  fi
  owner_guid=$(tr -d '[:space:]' <"${DUSB_SB_CERT_GUID}")
  duw_valid_uuid "${owner_guid}" || duw_die "invalid Secure Boot db owner GUID in ${DUSB_SB_CERT_GUID}: ${owner_guid}"
  openssl x509 -outform DER -in "${DUSB_SB_CERT_PEM}" -out "${DUSB_SB_CERT_DER}" >/dev/null 2>&1 || duw_die "failed to export Secure Boot db.cer from ${DUSB_SB_CERT_PEM}"
  cert-to-efi-sig-list -g "${owner_guid}" "${DUSB_SB_CERT_PEM}" "${DUSB_SB_CERT_ESL}" >/dev/null 2>&1 || duw_die "failed to export Secure Boot db.esl from ${DUSB_SB_CERT_PEM}"
  chmod 0600 "${DUSB_SB_KEY}" || duw_die "failed to secure Secure Boot db private key: ${DUSB_SB_KEY}"
  chmod 0644 "${DUSB_SB_CERT_PEM}" "${DUSB_SB_CERT_DER}" "${DUSB_SB_CERT_ESL}" "${DUSB_SB_CERT_GUID}" || duw_die "failed to set Secure Boot db public artifact permissions"
)

duw_generate_secure_boot_owner_kek() {
  _duw_kek_output_dir=$1
  _duw_kek_old_umask=
  _duw_kek_key_bits=${DEBIAN_USB_KEK_KEY_BITS:-2048}
  case ${_duw_kek_key_bits} in 2048|3072|4096) ;; *) duw_die "DEBIAN_USB_KEK_KEY_BITS must be one of: 2048, 3072, 4096" ;; esac
  install -d -m 0700 -- "${_duw_kek_output_dir}" || duw_die "failed to create Secure Boot PKI directory: ${_duw_kek_output_dir}"
  DUSB_SB_DB_AUTH_KEY=${_duw_kek_output_dir}/KEK.key
  DUSB_SB_DB_AUTH_CERT_PEM=${_duw_kek_output_dir}/KEK.crt
  DUSB_SB_DB_AUTH_CERT_DER=${_duw_kek_output_dir}/KEK.cer
  DUSB_SB_DB_AUTH_CERT_ESL=${_duw_kek_output_dir}/KEK.esl
  DUSB_SB_DB_AUTH_CERT_GUID=${_duw_kek_output_dir}/KEK.guid
  _duw_kek_owner_guid=$(duw_uuid4)
  _duw_kek_old_umask=$(umask)
  umask 077
  openssl req \
    -new -x509 -newkey "rsa:${_duw_kek_key_bits}" -sha256 -days 3650 -nodes \
    -subj "/CN=debian-usb Secure Boot KEK/" \
    -addext "basicConstraints=critical,CA:FALSE" \
    -addext "keyUsage=critical,digitalSignature" \
    -addext "extendedKeyUsage=codeSigning" \
    -keyout "${DUSB_SB_DB_AUTH_KEY}" \
    -out "${DUSB_SB_DB_AUTH_CERT_PEM}" >/dev/null 2>&1 || duw_die "failed to generate Secure Boot owner KEK key pair"
  printf '%s\n' "${_duw_kek_owner_guid}" >"${DUSB_SB_DB_AUTH_CERT_GUID}" || duw_die "failed to write Secure Boot KEK owner GUID"
  umask "${_duw_kek_old_umask}"
  chmod 0600 "${DUSB_SB_DB_AUTH_KEY}" || duw_die "failed to secure Secure Boot KEK private key: ${DUSB_SB_DB_AUTH_KEY}"
  duw_regenerate_secure_boot_owner_kek_public_artifacts
}

duw_regenerate_secure_boot_owner_kek_public_artifacts() (
  [ -f "${DUSB_SB_DB_AUTH_CERT_PEM}" ] && [ -s "${DUSB_SB_DB_AUTH_CERT_PEM}" ] || duw_die "Secure Boot KEK.crt is missing or empty: ${DUSB_SB_DB_AUTH_CERT_PEM}"
  if [ ! -f "${DUSB_SB_DB_AUTH_CERT_GUID}" ] || [ ! -s "${DUSB_SB_DB_AUTH_CERT_GUID}" ]; then
    duw_uuid4 >"${DUSB_SB_DB_AUTH_CERT_GUID}" || duw_die "failed to create Secure Boot KEK owner GUID"
  fi
  owner_guid=$(tr -d '[:space:]' <"${DUSB_SB_DB_AUTH_CERT_GUID}")
  duw_valid_uuid "${owner_guid}" || duw_die "invalid Secure Boot KEK owner GUID in ${DUSB_SB_DB_AUTH_CERT_GUID}: ${owner_guid}"
  openssl x509 -outform DER -in "${DUSB_SB_DB_AUTH_CERT_PEM}" -out "${DUSB_SB_DB_AUTH_CERT_DER}" >/dev/null 2>&1 || duw_die "failed to export Secure Boot KEK.cer from ${DUSB_SB_DB_AUTH_CERT_PEM}"
  cert-to-efi-sig-list -g "${owner_guid}" "${DUSB_SB_DB_AUTH_CERT_PEM}" "${DUSB_SB_DB_AUTH_CERT_ESL}" >/dev/null 2>&1 || duw_die "failed to export Secure Boot KEK.esl from ${DUSB_SB_DB_AUTH_CERT_PEM}"
  chmod 0600 "${DUSB_SB_DB_AUTH_KEY}" || duw_die "failed to secure Secure Boot KEK private key: ${DUSB_SB_DB_AUTH_KEY}"
  chmod 0644 "${DUSB_SB_DB_AUTH_CERT_PEM}" "${DUSB_SB_DB_AUTH_CERT_DER}" "${DUSB_SB_DB_AUTH_CERT_ESL}" "${DUSB_SB_DB_AUTH_CERT_GUID}" || duw_die "failed to set Secure Boot KEK public artifact permissions"
)

duw_prepare_secure_boot_owner_kek() {
  _duw_kek_pki_dir=$1
  DUSB_SB_DB_AUTH_KEY=${_duw_kek_pki_dir}/KEK.key
  DUSB_SB_DB_AUTH_CERT_PEM=${_duw_kek_pki_dir}/KEK.crt
  DUSB_SB_DB_AUTH_CERT_DER=${_duw_kek_pki_dir}/KEK.cer
  DUSB_SB_DB_AUTH_CERT_ESL=${_duw_kek_pki_dir}/KEK.esl
  DUSB_SB_DB_AUTH_CERT_GUID=${_duw_kek_pki_dir}/KEK.guid
  DUSB_SB_DB_AUTH_LABEL='Secure Boot owner KEK'
  if [ -f "${DUSB_SB_DB_AUTH_KEY}" ] || [ -f "${DUSB_SB_DB_AUTH_CERT_PEM}" ] || [ -f "${DUSB_SB_DB_AUTH_CERT_DER}" ] || [ -f "${DUSB_SB_DB_AUTH_CERT_ESL}" ]; then
    [ -f "${DUSB_SB_DB_AUTH_KEY}" ] && [ -f "${DUSB_SB_DB_AUTH_CERT_PEM}" ] || duw_die "incomplete Secure Boot owner KEK material under ${_duw_kek_pki_dir}; expected KEK.key and KEK.crt"
    [ -s "${DUSB_SB_DB_AUTH_KEY}" ] && [ -s "${DUSB_SB_DB_AUTH_CERT_PEM}" ] || duw_die "Secure Boot owner KEK material under ${_duw_kek_pki_dir} contains empty files"
    duw_regenerate_secure_boot_owner_kek_public_artifacts
    duw_note "Using persistent Secure Boot owner KEK certificate from ${DUSB_SB_DB_AUTH_CERT_DER}; private key remains under ${_duw_kek_pki_dir}"
    return 0
  fi
  duw_generate_secure_boot_owner_kek "${_duw_kek_pki_dir}"
  duw_note "Generated Secure Boot owner KEK certificate at ${DUSB_SB_DB_AUTH_CERT_DER}; enroll KEK.esl before using db.auth on User Mode firmware"
}

duw_prepare_secure_boot_owner_db() {
  _duw_owner_pki_dir=$(duw_secure_boot_pki_dir)
  duw_prepare_secure_boot_owner_kek "${_duw_owner_pki_dir}"
  DUSB_SB_KEY=${_duw_owner_pki_dir}/db.key
  DUSB_SB_CERT_PEM=${_duw_owner_pki_dir}/db.crt
  DUSB_SB_CERT_DER=${_duw_owner_pki_dir}/db.cer
  DUSB_SB_CERT_ESL=${_duw_owner_pki_dir}/db.esl
  DUSB_SB_CERT_GUID=${_duw_owner_pki_dir}/db.guid
  if [ -f "${DUSB_SB_KEY}" ] || [ -f "${DUSB_SB_CERT_PEM}" ] || [ -f "${DUSB_SB_CERT_DER}" ] || [ -f "${DUSB_SB_CERT_ESL}" ]; then
    [ -f "${DUSB_SB_KEY}" ] && [ -f "${DUSB_SB_CERT_PEM}" ] || duw_die "incomplete Secure Boot owner db material under ${_duw_owner_pki_dir}; expected db.key and db.crt"
    [ -s "${DUSB_SB_KEY}" ] && [ -s "${DUSB_SB_CERT_PEM}" ] || duw_die "Secure Boot owner db material under ${_duw_owner_pki_dir} contains empty files"
    duw_regenerate_secure_boot_owner_db_public_artifacts
    duw_note "Using persistent Secure Boot owner db certificate from ${DUSB_SB_CERT_DER}; private key remains under ${_duw_owner_pki_dir}"
    return 0
  fi
  duw_generate_secure_boot_owner_db "${_duw_owner_pki_dir}"
  duw_note "Generated Secure Boot owner db certificate at ${DUSB_SB_CERT_DER}; import secureboot/db.cer or secureboot/db.esl into firmware db once"
}

duw_prepare_grub_gpg_key() {
  _duw_gpg_pki_dir=$1
  _duw_gpg_key_dir=${_duw_gpg_pki_dir}/grub-gpg
  _duw_gpg_key_home=${_duw_gpg_key_dir}/home
  _duw_gpg_fingerprint_file=${_duw_gpg_key_dir}/grub-signing.fpr
  _duw_gpg_fingerprint=
  _duw_gpg_output=
  _duw_gpg_exit_code=0
  install -d -m 0700 -- "${_duw_gpg_key_home}" || duw_die "failed to create GRUB GPG home: ${_duw_gpg_key_home}"
  DUSB_GRUB_GPG_HOME=${_duw_gpg_key_home}
  DUSB_GRUB_GPG_PUBKEY=${_duw_gpg_key_dir}/grub-signing.pub
  DUSB_GRUB_GPG_PUBKEY_ASC=${_duw_gpg_key_dir}/grub-signing.pub.asc
  if [ ! -f "${_duw_gpg_fingerprint_file}" ] || [ ! -s "${_duw_gpg_fingerprint_file}" ]; then
    if _duw_gpg_output=$(gpg --homedir "${DUSB_GRUB_GPG_HOME}" --batch --generate-key 2>&1 <<'EOF'
Key-Type: RSA
Key-Length: 3072
Key-Usage: sign
Name-Real: debian-usb GRUB signing
Name-Email: debian-usb-grub@localhost
Expire-Date: 10y
%no-protection
%commit
EOF
); then
      :
    else
      _duw_gpg_exit_code=$?
      duw_die "failed to generate GRUB GPG signing key (exit ${_duw_gpg_exit_code}): ${_duw_gpg_output}"
    fi
    _duw_gpg_fingerprint=$(gpg --homedir "${DUSB_GRUB_GPG_HOME}" --batch --with-colons --list-secret-keys --fingerprint "debian-usb GRUB signing" | awk -F: '$1 == "fpr" { print $10; exit }') || duw_die "failed to resolve generated GRUB GPG key fingerprint"
    duw_valid_openpgp_fingerprint "${_duw_gpg_fingerprint}" || duw_die "invalid generated GRUB GPG key fingerprint: ${_duw_gpg_fingerprint}"
    printf '%s\n' "${_duw_gpg_fingerprint}" >"${_duw_gpg_fingerprint_file}" || duw_die "failed to persist GRUB GPG key fingerprint"
  fi
  _duw_gpg_fingerprint=$(tr -d '[:space:]' <"${_duw_gpg_fingerprint_file}")
  duw_valid_openpgp_fingerprint "${_duw_gpg_fingerprint}" || duw_die "invalid GRUB GPG key fingerprint in ${_duw_gpg_fingerprint_file}: ${_duw_gpg_fingerprint}"
  gpg --homedir "${DUSB_GRUB_GPG_HOME}" --batch --list-secret-keys "${_duw_gpg_fingerprint}" >/dev/null 2>&1 || duw_die "GRUB GPG fingerprint ${_duw_gpg_fingerprint} is recorded but the private key is missing from ${DUSB_GRUB_GPG_HOME}"
  gpg --homedir "${DUSB_GRUB_GPG_HOME}" --batch --yes --export "${_duw_gpg_fingerprint}" >"${DUSB_GRUB_GPG_PUBKEY}" || duw_die "failed to export GRUB GPG public key"
  gpg --homedir "${DUSB_GRUB_GPG_HOME}" --batch --yes --armor --export "${_duw_gpg_fingerprint}" >"${DUSB_GRUB_GPG_PUBKEY_ASC}" || duw_die "failed to export armored GRUB GPG public key"
  chmod 0600 "${_duw_gpg_fingerprint_file}" || duw_die "failed to secure GRUB GPG fingerprint file"
  chmod 0644 "${DUSB_GRUB_GPG_PUBKEY}" "${DUSB_GRUB_GPG_PUBKEY_ASC}" || duw_die "failed to set GRUB GPG public key permissions"
  DUSB_GRUB_GPG_FINGERPRINT=${_duw_gpg_fingerprint}
}

duw_generate_kernel_module_signing_key() {
  _duw_module_output_dir=$1
  _duw_module_old_umask=
  _duw_module_key_bits=${DEBIAN_USB_MODULE_KEY_BITS:-2048}
  case ${_duw_module_key_bits} in 2048|3072|4096) ;; *) duw_die "DEBIAN_USB_MODULE_KEY_BITS must be one of: 2048, 3072, 4096" ;; esac
  DUSB_MODULE_KEY=${_duw_module_output_dir}/kernel-module.key
  DUSB_MODULE_CERT_PEM=${_duw_module_output_dir}/kernel-module.crt
  DUSB_MODULE_CERT_DER=${_duw_module_output_dir}/kernel-module.cer
  _duw_module_old_umask=$(umask)
  umask 077
  openssl req \
    -new -x509 -newkey "rsa:${_duw_module_key_bits}" -sha256 -days 3650 -nodes \
    -subj "/CN=debian-usb Kernel Module Signing/" \
    -addext "basicConstraints=critical,CA:FALSE" \
    -addext "keyUsage=critical,digitalSignature" \
    -addext "extendedKeyUsage=codeSigning" \
    -keyout "${DUSB_MODULE_KEY}" \
    -out "${DUSB_MODULE_CERT_PEM}" >/dev/null 2>&1 || duw_die "failed to generate kernel module signing key pair"
  umask "${_duw_module_old_umask}"
  openssl x509 -outform DER -in "${DUSB_MODULE_CERT_PEM}" -out "${DUSB_MODULE_CERT_DER}" >/dev/null 2>&1 || duw_die "failed to export kernel module signing certificate"
  chmod 0600 "${DUSB_MODULE_KEY}" || duw_die "failed to secure kernel module signing private key: ${DUSB_MODULE_KEY}"
  chmod 0644 "${DUSB_MODULE_CERT_PEM}" "${DUSB_MODULE_CERT_DER}" || duw_die "failed to set kernel module signing certificate permissions"
}

duw_prepare_kernel_module_signing_key() {
  _duw_module_pki_dir=$1
  DUSB_MODULE_KEY=${_duw_module_pki_dir}/kernel-module.key
  DUSB_MODULE_CERT_PEM=${_duw_module_pki_dir}/kernel-module.crt
  DUSB_MODULE_CERT_DER=${_duw_module_pki_dir}/kernel-module.cer
  if [ -f "${DUSB_MODULE_KEY}" ] || [ -f "${DUSB_MODULE_CERT_PEM}" ] || [ -f "${DUSB_MODULE_CERT_DER}" ]; then
    [ -f "${DUSB_MODULE_KEY}" ] && [ -f "${DUSB_MODULE_CERT_PEM}" ] || duw_die "incomplete kernel module signing material under ${_duw_module_pki_dir}; expected kernel-module.key and kernel-module.crt"
    [ -s "${DUSB_MODULE_KEY}" ] && [ -s "${DUSB_MODULE_CERT_PEM}" ] || duw_die "kernel module signing material under ${_duw_module_pki_dir} contains empty files"
    openssl x509 -outform DER -in "${DUSB_MODULE_CERT_PEM}" -out "${DUSB_MODULE_CERT_DER}" >/dev/null 2>&1 || duw_die "failed to regenerate kernel module signing certificate"
    chmod 0600 "${DUSB_MODULE_KEY}" || duw_die "failed to secure kernel module signing private key: ${DUSB_MODULE_KEY}"
    chmod 0644 "${DUSB_MODULE_CERT_PEM}" "${DUSB_MODULE_CERT_DER}" || duw_die "failed to set kernel module signing certificate permissions"
    return 0
  fi
  duw_generate_kernel_module_signing_key "${_duw_module_pki_dir}"
}

duw_prepare_secure_boot_identity() {
  DUSB_SECURE_BOOT_TRUST=$(duw_effective_secure_boot_trust "${DUSB_SECURE_BOOT_TRUST:-}")
  if [ "${DUSB_SECURE_BOOT_TRUST}" = mok ]; then duw_prepare_secure_boot_keypair; else duw_prepare_secure_boot_owner_db; fi
  _duw_identity_support_root=$(duw_secure_boot_support_root_dir)
  DUSB_SB_SIGNING_LABEL=${DUSB_SECURE_BOOT_TRUST}
  if [ "${DUSB_SECURE_BOOT_TRUST}" = mok ]; then DUSB_SB_SIGNING_LABEL='generated MOK'; else DUSB_SB_SIGNING_LABEL='Secure Boot owner db'; fi
  duw_prepare_grub_gpg_key "${_duw_identity_support_root}"
  duw_prepare_kernel_module_signing_key "${_duw_identity_support_root}"
}

duw_microsoft_secure_boot_objects_repo_url() (
  repo_url="${DEBIAN_USB_MICROSOFT_SECURE_BOOT_OBJECTS_REPO:-https://github.com/microsoft/secureboot_objects.git}"
  case "${repo_url}" in
    https://github.com/microsoft/secureboot_objects|https://github.com/microsoft/secureboot_objects.git) ;;
    *) duw_die "DEBIAN_USB_MICROSOFT_SECURE_BOOT_OBJECTS_REPO must be the official Microsoft secureboot_objects repository URL" ;;
  esac
  printf '%s\n' "${repo_url}"
)

duw_microsoft_secure_boot_objects_ref() (
  ref=${DEBIAN_USB_MICROSOFT_SECURE_BOOT_OBJECTS_REF:-main}
  [ -n "${ref}" ] && [ "${#ref}" -le 128 ] || duw_die "DEBIAN_USB_MICROSOFT_SECURE_BOOT_OBJECTS_REF contains unsupported characters: ${ref}"
  case ${ref} in *[!A-Za-z0-9._/+:-]*) duw_die "DEBIAN_USB_MICROSOFT_SECURE_BOOT_OBJECTS_REF contains unsupported characters: ${ref}" ;; esac
  printf '%s\n' "${ref}"
)

duw_microsoft_secure_boot_template() (
  template="${DEBIAN_USB_MICROSOFT_SECURE_BOOT_TEMPLATE:-MostCompatible.toml}"
  case "${template}" in
    LegacyFirmwareDefaults.toml|MicrosoftAndOptionRoms.toml|MicrosoftAndThirdParty.toml|MicrosoftOnly.toml|MostCompatible.toml) ;;
    *) duw_die "DEBIAN_USB_MICROSOFT_SECURE_BOOT_TEMPLATE must be an official template name, got: ${template}" ;;
  esac
  printf '%s\n' "${template}"
)

duw_secure_boot_objects_clone_timeout_seconds() (
  value=${DEBIAN_USB_SECURE_BOOT_OBJECTS_CLONE_TIMEOUT_SECONDS:-120}
  case ${value} in ''|*[!0-9]*) duw_die "DEBIAN_USB_SECURE_BOOT_OBJECTS_CLONE_TIMEOUT_SECONDS must be an integer" ;; esac
  [ "${value}" -ge 10 ] && [ "${value}" -le 600 ] || duw_die "DEBIAN_USB_SECURE_BOOT_OBJECTS_CLONE_TIMEOUT_SECONDS must be between 10 and 600"
  printf '%s\n' "${value}"
)

duw_clone_microsoft_secure_boot_objects() (
  temp_root="$1"
  output=""
  exit_code=0
  [ -n "${temp_root}" ] && [ -d "${temp_root}" ] || duw_die "temporary root is required for Microsoft secureboot_objects clone"
  repo_url="$(duw_microsoft_secure_boot_objects_repo_url)"
  ref="$(duw_microsoft_secure_boot_objects_ref)"
  clone_timeout="$(duw_secure_boot_objects_clone_timeout_seconds)"
  repo_parent="$(mktemp -d -p "${temp_root}" "microsoft-secureboot-objects.XXXXXX")" || \
    duw_die "failed to create Microsoft secureboot_objects clone workspace under ${temp_root}"
  repo_dir="${repo_parent}/repo"

  duw_step "Cloning Microsoft secureboot_objects (${ref}) into ${repo_dir}" >&2
  if output=$(timeout "${clone_timeout}" git clone --depth 1 --filter=blob:none --sparse --branch "${ref}" "${repo_url}" "${repo_dir}" 2>&1); then
    :
  else
    exit_code=$?
    duw_die "failed to clone Microsoft secureboot_objects from ${repo_url} at ${ref} (exit ${exit_code}): ${output}"
  fi
  if output=$(git -C "${repo_dir}" sparse-checkout set Templates PreSignedObjects/DB/Certificates 2>&1); then
    :
  else
    exit_code=$?
    duw_die "failed to restrict Microsoft secureboot_objects sparse checkout (exit ${exit_code}): ${output}"
  fi
  printf '%s\n' "${repo_dir}"
)

duw_microsoft_db_entries_tsv() (
  repo_dir="$1"
  template="$2"
  python3 - "${repo_dir}" "${template}" <<'PY'
from __future__ import annotations

from pathlib import Path
import re
import sys
import tomllib

repo = Path(sys.argv[1]).resolve()
template = sys.argv[2]
if not re.fullmatch(r"[A-Za-z0-9._+-]{1,96}\.toml", template):
    raise SystemExit(f"invalid Microsoft secureboot_objects template name: {template}")
template_path = (repo / "Templates" / template).resolve()
if repo not in template_path.parents or not template_path.is_file():
    raise SystemExit(f"Microsoft secureboot_objects template is missing: {template}")
data = tomllib.loads(template_path.read_text(encoding="utf-8"))
db = data.get("DB")
if not isinstance(db, dict):
    raise SystemExit(f"Microsoft secureboot_objects template has no DB table: {template}")
files = db.get("files")
if not isinstance(files, list) or not files:
    raise SystemExit(f"Microsoft secureboot_objects template has no DB files: {template}")
guid_re = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
for item in files:
    if not isinstance(item, dict):
        raise SystemExit("Microsoft secureboot_objects DB file entry is not a table")
    rel = str(item.get("path") or "").strip()
    owner = str(item.get("signature_owner") or "").strip()
    if not rel or rel.startswith("/") or ".." in Path(rel).parts:
        raise SystemExit(f"invalid Microsoft secureboot_objects DB path: {rel}")
    if not guid_re.fullmatch(owner):
        raise SystemExit(f"invalid Microsoft secureboot_objects signature owner for {rel}: {owner}")
    path = (repo / rel).resolve()
    if repo not in path.parents or not path.is_file():
        raise SystemExit(f"Microsoft secureboot_objects DB certificate is missing: {rel}")
    size = path.stat().st_size
    if size <= 0 or size > 65536:
        raise SystemExit(f"Microsoft secureboot_objects DB certificate has invalid size: {rel} ({size} bytes)")
    print(f"{owner}\t{path}")
PY
)

duw_build_combined_secure_boot_db() {
  _duw_db_temp_root=$1
  [ -n "${DUSB_SB_CERT_ESL:-}" ] && [ -f "${DUSB_SB_CERT_ESL}" ] && [ -s "${DUSB_SB_CERT_ESL}" ] || duw_die "generated Secure Boot db ESL is missing before building combined db"
  [ -n "${DUSB_SB_DB_AUTH_KEY:-}" ] && [ -f "${DUSB_SB_DB_AUTH_KEY}" ] && [ -s "${DUSB_SB_DB_AUTH_KEY}" ] || duw_die "Secure Boot db authenticated-update signing key is missing"
  [ -n "${DUSB_SB_DB_AUTH_CERT_PEM:-}" ] && [ -f "${DUSB_SB_DB_AUTH_CERT_PEM}" ] && [ -s "${DUSB_SB_DB_AUTH_CERT_PEM}" ] || duw_die "Secure Boot db authenticated-update signing certificate is missing"
  _duw_db_output_dir=$(mktemp -d -p "${_duw_db_temp_root}" combined-secureboot-db.XXXXXX) || duw_die "failed to create combined Secure Boot db workspace under ${_duw_db_temp_root}"
  DUSB_SB_COMBINED_DB_ESL=${_duw_db_output_dir}/db.esl
  DUSB_SB_COMBINED_DB_AUTH=${_duw_db_output_dir}/db.auth
  : >"${DUSB_SB_COMBINED_DB_ESL}" || duw_die "failed to create combined Secure Boot db.esl"
  cp -f -- "${DUSB_SB_CERT_ESL}" "${_duw_db_output_dir}/generated-owner.esl" || duw_die "failed to copy generated Secure Boot owner ESL"
  cat "${_duw_db_output_dir}/generated-owner.esl" >>"${DUSB_SB_COMBINED_DB_ESL}" || duw_die "failed to append generated Secure Boot owner ESL"
  _duw_db_repo_dir=$(duw_clone_microsoft_secure_boot_objects "${_duw_db_temp_root}")
  _duw_db_template=$(duw_microsoft_secure_boot_template)
  _duw_db_rows=$(duw_microsoft_db_entries_tsv "${_duw_db_repo_dir}" "${_duw_db_template}")
  _duw_db_count=0
  while IFS=$(printf '\t') read -r _duw_db_owner _duw_db_cert_path; do
    [ -n "${_duw_db_owner}" ] && [ -n "${_duw_db_cert_path}" ] || continue
    _duw_db_count=$((_duw_db_count + 1))
    _duw_db_pem=${_duw_db_output_dir}/microsoft-${_duw_db_count}.pem
    _duw_db_esl=${_duw_db_output_dir}/microsoft-${_duw_db_count}.esl
    openssl x509 -inform DER -in "${_duw_db_cert_path}" -out "${_duw_db_pem}" >/dev/null 2>&1 || duw_die "failed to convert Microsoft Secure Boot certificate to PEM: ${_duw_db_cert_path}"
    cert-to-efi-sig-list -g "${_duw_db_owner}" "${_duw_db_pem}" "${_duw_db_esl}" >/dev/null 2>&1 || duw_die "failed to convert Microsoft Secure Boot certificate to ESL: ${_duw_db_cert_path}"
    cat "${_duw_db_esl}" >>"${DUSB_SB_COMBINED_DB_ESL}" || duw_die "failed to append Microsoft Secure Boot ESL for ${_duw_db_cert_path}"
  done <<EOF_ROWS
${_duw_db_rows}
EOF_ROWS
  [ "${_duw_db_count}" -gt 0 ] || duw_die "Microsoft secureboot_objects template ${_duw_db_template} did not provide any db certificates"
  if _duw_db_auth_output=$(sign-efi-sig-list -a -k "${DUSB_SB_DB_AUTH_KEY}" -c "${DUSB_SB_DB_AUTH_CERT_PEM}" db "${DUSB_SB_COMBINED_DB_ESL}" "${DUSB_SB_COMBINED_DB_AUTH}" 2>&1); then
    :
  else
    _duw_db_exit_code=$?
    duw_die "failed to sign combined Secure Boot db.auth with ${DUSB_SB_DB_AUTH_LABEL:-configured signer} (exit ${_duw_db_exit_code}): ${_duw_db_auth_output}"
  fi
  chmod 0644 "${DUSB_SB_COMBINED_DB_ESL}" "${DUSB_SB_COMBINED_DB_AUTH}" || duw_die "failed to set combined Secure Boot db artifact permissions"
  duw_note "Built combined Secure Boot db.esl/db.auth with generated owner identity plus ${_duw_db_count} Microsoft db certificates from ${_duw_db_template}"
}

duw_stage_secure_boot_updatevars() (
  esp_mount="$1"
  temp_root="$2"
  secureboot_dir="${esp_mount}/secureboot"
  updatevars_dir="${esp_mount}/EFI/debian-usb/updatevars"
  updatevars_source=""

  [ -n "${temp_root}" ] && [ -d "${temp_root}" ] || duw_die "temporary root is required for UEFI UpdateVars staging"
  if ! updatevars_source="$(duw_updatevars_source_path)"; then
    duw_note "Secure Boot: UpdateVars.efi is unavailable on this host, so UpdateVars staging and menu entries will be skipped."
    return 0
  fi
  install -d -- "${secureboot_dir}" "${updatevars_dir}" || duw_die "failed to create UEFI UpdateVars directories on the ESP"

  duw_build_combined_secure_boot_db "${temp_root}"
  cp -f -- "${DUSB_SB_COMBINED_DB_ESL}" "${secureboot_dir}/db.esl" || duw_die "failed to stage combined secureboot/db.esl"
  cp -f -- "${DUSB_SB_COMBINED_DB_AUTH}" "${secureboot_dir}/db.auth" || duw_die "failed to stage combined secureboot/db.auth"
  cp -f -- "${DUSB_SB_CERT_ESL}" "${secureboot_dir}/db-owner.esl" || duw_die "failed to stage generated owner-only db-owner.esl"

  if [ "${DUSB_SECURE_BOOT_TRUST}" = "firmware-db" ] && [ -n "${DUSB_SB_DB_AUTH_CERT_DER:-}" ] && [ -f "${DUSB_SB_DB_AUTH_CERT_DER}" ]; then
    cp -f -- "${DUSB_SB_DB_AUTH_CERT_DER}" "${secureboot_dir}/KEK.cer" || duw_die "failed to stage Secure Boot KEK.cer"
    cp -f -- "${DUSB_SB_DB_AUTH_CERT_PEM}" "${secureboot_dir}/KEK.crt" || duw_die "failed to stage Secure Boot KEK.crt"
    cp -f -- "${DUSB_SB_DB_AUTH_CERT_ESL}" "${secureboot_dir}/KEK.esl" || duw_die "failed to stage Secure Boot KEK.esl"
  fi

  duw_sign_pe_binary "${updatevars_source}" "${updatevars_dir}/UpdateVars.efi"
  duw_sign_grub_data_file "${updatevars_dir}/UpdateVars.efi"
)

duw_stage_secure_boot_support() (
  esp_mount="$1"
  temp_root="${2:-}"
  secureboot_dir="${esp_mount}/secureboot"
  mok_dir="${esp_mount}/EFI/debian-usb/mok"
  [ -n "${temp_root}" ] && [ -d "${temp_root}" ] || duw_die "temporary root is required for Secure Boot support staging"
  install -d -- "${secureboot_dir}" "${mok_dir}" || duw_die "failed to create Secure Boot directories on the ESP"
  cp -f -- "${DUSB_GRUB_GPG_PUBKEY}" "${secureboot_dir}/grub-signing.pub" || duw_die "failed to stage GRUB GPG public key on the ESP"
  cp -f -- "${DUSB_GRUB_GPG_PUBKEY_ASC}" "${secureboot_dir}/grub-signing.pub.asc" || duw_die "failed to stage armored GRUB GPG public key on the ESP"
  cp -f -- "${DUSB_MODULE_CERT_DER}" "${secureboot_dir}/kernel-module.cer" || duw_die "failed to stage kernel module signing certificate on the ESP"
  cp -f -- "${DUSB_MODULE_CERT_PEM}" "${secureboot_dir}/kernel-module.crt" || duw_die "failed to stage kernel module signing PEM certificate on the ESP"
  cp -f -- "${DUSB_SB_CERT_DER}" "${mok_dir}/MOK.der" || duw_die "failed to stage optional MOK certificate on the ESP"
  cp -f -- "${DUSB_SB_CERT_ESL}" "${mok_dir}/MOK.esl" || duw_die "failed to stage optional MOK ESL on the ESP"
  duw_stage_secure_boot_updatevars "${esp_mount}" "${temp_root}"
  if [ "${DUSB_SECURE_BOOT_TRUST}" = "mok" ]; then
    cp -f -- /usr/lib/shim/shimx64.efi.signed "${mok_dir}/shimx64.efi" || duw_die "failed to stage shim launcher on the ESP"
    cp -f -- /usr/lib/shim/mmx64.efi.signed "${mok_dir}/grubx64.efi" || duw_die "failed to stage MokManager shim second stage on the ESP"
    cp -f -- /usr/lib/shim/mmx64.efi.signed "${mok_dir}/mmx64.efi" || duw_die "failed to stage MokManager on the ESP"
    duw_sign_grub_data_file "${mok_dir}/shimx64.efi"
    duw_sign_grub_data_file "${mok_dir}/grubx64.efi"
    duw_sign_grub_data_file "${mok_dir}/mmx64.efi"
    cat >"${secureboot_dir}/README.txt" <<'EOF'
Secure Boot MOK workflow:

1. The managed USB reuses the persistent debian-usb MOK store from the build
   host and stages EFI/debian-usb/mok/MOK.der on the USB.
2. Boot the USB and select "MOK Enrollment".
3. In MokManager, choose "Enroll key from disk".
4. Open EFI/debian-usb/mok/MOK.der and confirm enrollment.
5. Reboot and boot the managed entries signed by this same MOK certificate.
6. GRUB assets use detached OpenPGP signatures from secureboot/grub-signing.pub.
7. The configured UpdateVars submenu stages signed UpdateVars.efi plus
   secureboot/db.esl and secureboot/db.auth. Both db files combine this
   generated signing identity with Microsoft db certificates from the official
   microsoft/secureboot_objects MostCompatible template by default.

The writer reuses one persistent generated MOK keypair by default, so the same
MOK.der can be enrolled once and reused by future USBs. If MokManager reports
"Volume Full", clear stale firmware/MOK variables from the target firmware/OS;
the USB cannot enroll a MOK while the target firmware refuses EFI variable
writes.

Use the Setup Mode UpdateVars entry only after clearing PK. Use the User Mode
UpdateVars entry only when the firmware KEK database already trusts the
certificate that signed secureboot/db.auth.
EOF
    return 0
  fi
  cp -f -- "${DUSB_SB_CERT_DER}" "${secureboot_dir}/db.cer" || duw_die "failed to stage Secure Boot db.cer on the ESP"
  cp -f -- "${DUSB_SB_CERT_PEM}" "${secureboot_dir}/db.crt" || duw_die "failed to stage Secure Boot db.crt on the ESP"
  cp -f -- "${DUSB_SB_CERT_ESL}" "${secureboot_dir}/db-owner.esl" || duw_die "failed to stage owner-only Secure Boot db-owner.esl on the ESP"
  duw_sign_pe_binary /usr/lib/shim/shimx64.efi.signed "${mok_dir}/shimx64.efi"
  duw_sign_pe_binary /usr/lib/shim/mmx64.efi.signed "${mok_dir}/mmx64.efi"
  duw_sign_grub_data_file "${mok_dir}/shimx64.efi"
  duw_sign_grub_data_file "${mok_dir}/mmx64.efi"
  cat >"${secureboot_dir}/README.txt" <<'EOF'
Secure Boot owner-db workflow:

1. The private firmware db signing key is intentionally not on this USB. It
   remains on the build host as /data/pki/secureboot/db.key by default.
2. Import secureboot/db.cer or secureboot/db.esl into the firmware Secure Boot
   db once, using the firmware's custom/Secure Boot key management UI.
3. BOOTX64.EFI is shim, mmx64.efi is beside shim, and grubx64.efi is signed
   with this db key. Custom kernels and EFI tools are signed by the same key.
4. GRUB assets use detached OpenPGP signatures from secureboot/grub-signing.pub.
5. Kernel modules should be signed with /data/pki/secureboot/kernel-module.key
   and enrolled/trusted with secureboot/kernel-module.cer as required by the
   installed target kernel policy.
6. The configured UpdateVars submenu stages signed UpdateVars.efi plus
   secureboot/db.esl and secureboot/db.auth. Both db files combine this owner
   db certificate with Microsoft db certificates from the official
   microsoft/secureboot_objects MostCompatible template by default.
7. secureboot/KEK.esl is staged as the public KEK material for authenticated
   db.auth updates. User Mode db.auth enrollment only works after that KEK is
   already present in firmware KEK; otherwise use Setup Mode db.esl.
8. If you use MokManager instead of firmware db import, enroll
   EFI/debian-usb/mok/MOK.der once only. Do not repeatedly import/delete MOK
   entries without cleaning pending firmware variables.

If MokManager reports "Volume Full", the target firmware EFI variable store is
full. Clear stale firmware/MOK variables from the target firmware/OS before
attempting another enrollment.
EOF
)

duw_note_secure_boot_requirement() (
  if [ "${DUSB_SECURE_BOOT_TRUST}" = "mok" ]; then
    duw_note "Secure Boot: managed entries are signed with the persistent debian-usb MOK certificate staged at EFI/debian-usb/mok/MOK.der."
    duw_note "Secure Boot: boot the USB, select MOK Enrollment, enroll MOK.der once, then reboot and boot any managed entry."
    duw_note "Secure Boot: UEFI UpdateVars stages signed UpdateVars.efi plus combined secureboot/db.esl and db.auth containing the generated MOK identity and Microsoft db certificates."
    return 0
  fi
  duw_note "Secure Boot: import the staged secureboot/db.cer or combined secureboot/db.esl into firmware db once; db.key remains private under /data/pki/secureboot by default."
  duw_note "Secure Boot: BOOTX64.EFI is shim, mmx64.efi is staged next to shim, and grubx64.efi/custom EFI binaries are signed by the owner db key."
  duw_note "Secure Boot: UEFI UpdateVars stages signed UpdateVars.efi; secureboot/db.auth is signed by the generated KEK and only works after that KEK is installed in firmware KEK."
  duw_note "Secure Boot: optional MOK enrollment uses the same certificate at EFI/debian-usb/mok/MOK.der; do not enroll one MOK per distro."
)

duw_profile_preseed_key_suffix() (
  profile="$1"
  case "${profile}" in
    debian) printf 'DEBIAN\n' ;;
    kali-linux) printf 'KALI\n' ;;
    kali-purple) printf 'PURPLE\n' ;;
    *) printf '\n' ;;
  esac
)

duw_validate_preseed_usb_file() (
  preseed_file="$1"
  label="$2"
  [ -n "${preseed_file}" ] || duw_die "${label} is required"
  case ${preseed_file} in /*) ;; *) duw_die "${label} must be an absolute path: ${preseed_file}" ;; esac
  case ${preseed_file} in */../*|../*|*/..) duw_die "${label} must not contain parent-directory segments: ${preseed_file}" ;; esac
  [ "$(basename -- "${preseed_file}")" = "preseed.cfg" ] || \
    duw_die "${label} must point to preseed.cfg: ${preseed_file}"
)

duw_profile_preseed_usb_file() (
  case $1 in
    debian) printf '%s\n' "${PRESEED_USB_DEBIAN_FILE:-}" ;;
    kali-linux) printf '%s\n' "${PRESEED_USB_KALI_FILE:-}" ;;
    kali-purple) printf '%s\n' "${PRESEED_USB_PURPLE_FILE:-}" ;;
    *) printf '\n' ;;
  esac
)

duw_profile_preseed_host_path() (
  case $1 in
    debian) printf '%s\n' "${PRESEED_HOST_DEBIAN_PATH:-}" ;;
    kali-linux) printf '%s\n' "${PRESEED_HOST_KALI_PATH:-}" ;;
    kali-purple) printf '%s\n' "${PRESEED_HOST_PURPLE_PATH:-}" ;;
    *) printf '\n' ;;
  esac
)

duw_profile_preseed_target_dir() (
  profile="$1"
  preseed_file="$(duw_profile_preseed_usb_file "${profile}")"
  [ -n "${preseed_file}" ] || {
    printf '\n'
    return 0
  }
  duw_validate_preseed_usb_file "${preseed_file}" "configured USB preseed file for ${profile}"
  target_dir="$(dirname -- "${preseed_file}")"
  printf '%s\n' "${target_dir}"
)

duw_stage_profile_default_preseed_tree() (
  payload_mount="$1"
  profile="$2"

  preseed_file="$(duw_profile_preseed_usb_file "${profile}")"
  source_dir="$(duw_profile_preseed_host_path "${profile}")"
  target_rel="$(duw_profile_preseed_target_dir "${profile}")"
  [ -n "${preseed_file}" ] && [ -n "${source_dir}" ] && [ -n "${target_rel}" ] || return 0
  target_dir="${payload_mount}${target_rel}"
  if [ ! -d "${source_dir}" ]; then
    duw_note "WARNING: ${source_dir} is missing; USB preseed menu entries for ${profile} will not work until ${preseed_file} is present on the written USB"
    return 0
  fi
  if [ ! -f "${source_dir}/preseed.cfg" ]; then
    duw_note "WARNING: ${source_dir}/preseed.cfg is missing; copying ${source_dir}, but USB preseed menu entries for ${profile} may not work"
  fi
  rm -rf -- "${target_dir}" || duw_die "failed to replace staged preseed directory: ${target_dir}"
  mkdir -p -- "${target_dir}" || duw_die "failed to create staged preseed directory: ${target_dir}"
  cp -a -- "${source_dir}/." "${target_dir}/" || duw_die "failed to stage configured preseed tree from ${source_dir}"
  duw_note "Staged ${source_dir} -> ${target_rel}"
)

duw_stage_profile_preseed_tree_from_dir() (
  payload_mount="$1"
  profile="$2"
  source_dir="$3"

  [ -n "${source_dir}" ] || return 0
  preseed_file="$(duw_profile_preseed_usb_file "${profile}")"
  target_rel="$(duw_profile_preseed_target_dir "${profile}")"
  [ -n "${preseed_file}" ] && [ -n "${target_rel}" ] || return 0
  duw_validate_preseed_source_dir "${source_dir}"
  target_dir="${payload_mount}${target_rel}"
  rm -rf -- "${target_dir}" || duw_die "failed to replace staged preseed directory: ${target_dir}"
  mkdir -p -- "${target_dir}" || duw_die "failed to create staged preseed directory: ${target_dir}"
  cp -a -- "${source_dir}/." "${target_dir}/" || duw_die "failed to stage ${profile} preseed tree from ${source_dir}"
  duw_note "Staged ${source_dir} -> ${target_rel}"
)

duw_stage_single_profile_preseed_tree() (
  mount_root="$1"
  profile="$2"
  source_dir="${3:-}"

  if [ -n "${source_dir}" ]; then
    duw_stage_profile_preseed_tree_from_dir "${mount_root}" "${profile}" "${source_dir}"
    return 0
  fi
  duw_stage_profile_default_preseed_tree "${mount_root}" "${profile}"
)

duw_stage_default_preseed_trees() (
  payload_mount=$1
  shift
  seen_profiles=' '
  for profile in "$@"; do
    [ -n "${profile}" ] || continue
    case ${seen_profiles} in *" ${profile} "*) continue ;; esac
    seen_profiles=${seen_profiles}${profile}' '
    duw_stage_profile_default_preseed_tree "${payload_mount}" "${profile}"
  done
)

duw_sign_pe_binary() (
  source_path="$1"
  destination_path="$2"
  output=""
  exit_code=0
  [ -f "${source_path}" ] && [ -s "${source_path}" ] || duw_die "refusing to sign missing or empty PE/COFF asset: ${source_path}"
  mkdir -p -- "$(dirname -- "${destination_path}")"
  if output=$(sbsign --key "${DUSB_SB_KEY}" --cert "${DUSB_SB_CERT_PEM}" --output "${destination_path}" "${source_path}" 2>&1); then
    if output=$(sbverify --cert "${DUSB_SB_CERT_PEM}" "${destination_path}" 2>&1); then
      return 0
    else
      exit_code=$?
      duw_die "signed boot asset does not verify against ${DUSB_SB_SIGNING_LABEL} certificate ${DUSB_SB_CERT_PEM}: ${destination_path} (exit ${exit_code}): ${output}"
    fi
  else
    exit_code=$?
    duw_die "failed to sign boot asset ${source_path} with ${DUSB_SB_SIGNING_LABEL} key (exit ${exit_code}): ${output}"
  fi
)

duw_sign_pe_binary_in_place() (
  source_path="$1"
  temp_path=""
  exit_code=0

  [ -f "${source_path}" ] && [ -s "${source_path}" ] || duw_die "refusing to sign missing or empty PE/COFF asset in place: ${source_path}"
  temp_path="$(mktemp "$(dirname -- "${source_path}")/.signed.$(basename -- "${source_path}").XXXXXX")" || \
    duw_die "failed to create temporary signed PE/COFF path for ${source_path}"
  rm -f -- "${temp_path}" || duw_die "failed to reset temporary signed PE/COFF path: ${temp_path}"
  if duw_sign_pe_binary "${source_path}" "${temp_path}"; then
    mv -f -- "${temp_path}" "${source_path}" || {
      exit_code=$?
      rm -f -- "${temp_path}" || true
      duw_die "failed to replace ${source_path} with signed asset (exit ${exit_code})"
    }
    return 0
  fi
)

duw_sign_grub_data_file() (
  source_path="$1"
  signature_path="${source_path}.sig"
  output=""
  exit_code=0

  [ -f "${source_path}" ] && [ -s "${source_path}" ] || duw_die "refusing to sign missing or empty data asset: ${source_path}"
  mkdir -p -- "$(dirname -- "${signature_path}")"
  if output=$(gpg --homedir "${DUSB_GRUB_GPG_HOME}" --batch --yes --pinentry-mode loopback --local-user "${DUSB_GRUB_GPG_FINGERPRINT}" --detach-sign --output "${signature_path}" "${source_path}" 2>&1); then
    :
  else
    exit_code=$?
    rm -f -- "${signature_path}" || true
    duw_die "failed to sign GRUB data asset ${source_path} with OpenPGP key (exit ${exit_code}): ${output}"
  fi
  if output=$(gpg --homedir "${DUSB_GRUB_GPG_HOME}" --batch --verify "${signature_path}" "${source_path}" 2>&1); then
    :
  else
    exit_code=$?
    rm -f -- "${signature_path}" || true
    duw_die "GRUB data asset signature does not verify for ${source_path} (exit ${exit_code}): ${output}"
  fi
)

duw_stage_owned_removable_secure_boot_chain() (
  esp_mount="$1"
  boot_dir="${esp_mount}/EFI/BOOT"
  if [ "${DUSB_SECURE_BOOT_TRUST}" = "mok" ]; then
    return 0
  fi
  install -d -- "${boot_dir}" || duw_die "failed to create removable EFI boot directory"

  duw_step "Signing removable shim/GRUB boot chain with Secure Boot owner db key"
  if [ ! -f "${boot_dir}/grubx64.efi" ] && [ -f "${boot_dir}/BOOTX64.EFI" ]; then
    cp -f -- "${boot_dir}/BOOTX64.EFI" "${boot_dir}/grubx64.efi" || duw_die "failed to preserve generated GRUB as grubx64.efi"
  fi
  if [ -f "${boot_dir}/grubx64.efi" ]; then
    duw_sign_pe_binary_in_place "${boot_dir}/grubx64.efi"
  elif [ -f /usr/lib/grub/x86_64-efi-signed/grubx64.efi.signed ]; then
    duw_sign_pe_binary /usr/lib/grub/x86_64-efi-signed/grubx64.efi.signed "${boot_dir}/grubx64.efi"
  else
    duw_die "grubx64.efi was not installed and no signed host GRUB fallback exists"
  fi
  duw_sign_pe_binary /usr/lib/shim/shimx64.efi.signed "${boot_dir}/BOOTX64.EFI"
  cp -f -- "${boot_dir}/BOOTX64.EFI" "${boot_dir}/shimx64.efi" || duw_die "failed to stage shimx64.efi next to BOOTX64.EFI"
  duw_sign_pe_binary /usr/lib/shim/mmx64.efi.signed "${boot_dir}/mmx64.efi"
  duw_sign_grub_data_file "${boot_dir}/BOOTX64.EFI"
  duw_sign_grub_data_file "${boot_dir}/shimx64.efi"
  duw_sign_grub_data_file "${boot_dir}/mmx64.efi"
  duw_sign_grub_data_file "${boot_dir}/grubx64.efi"
)

duw_stage_secure_boot_manifest() (
  esp_mount=$1
  boot_cfg=$2
  boot_cfg_label=$3
  manifest=${esp_mount}/secureboot/manifest.sha256
  install -d -- "$(dirname "${manifest}")" || duw_die "failed to create Secure Boot manifest directory"
  python3 - "${esp_mount}" "${boot_cfg}" "${boot_cfg_label}" "${manifest}" <<'PY'
from __future__ import annotations
import hashlib
from pathlib import Path
import sys
esp, boot_cfg, boot_label, manifest = map(Path, sys.argv[1:])
rows: list[str] = []
for root in (esp / "EFI", esp / "secureboot"):
    if not root.is_dir():
        continue
    for path in sorted((item for item in root.rglob("*") if item.is_file()), key=lambda item: str(item)):
        if path == manifest or path.suffix == ".sig" or path.name.endswith(".pubkey.pem"):
            continue
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        rows.append(f"{digest}  ESP:{path.relative_to(esp).as_posix()}")
if boot_cfg.is_file():
    rows.append(f"{hashlib.sha256(boot_cfg.read_bytes()).hexdigest()}  {sys.argv[3]}")
manifest.write_text("\n".join(rows) + "\n", encoding="utf-8")
PY
  duw_sign_grub_data_file "${manifest}"
)

duw_stage_multios_preseed_trees() (
  payload_mount=$1
  shift
  seen_profiles=' '
  separator=$(printf '\037')
  for row in "$@"; do
    profile=${row%%"${separator}"*}
    source_dir=${row#*"${separator}"}
    [ -n "${profile}" ] || continue
    if [ -n "${source_dir}" ]; then
      duw_stage_profile_preseed_tree_from_dir "${payload_mount}" "${profile}" "${source_dir}"
      seen_profiles=${seen_profiles}${profile}' '
      continue
    fi
    case ${seen_profiles} in *" ${profile} "*) continue ;; esac
    seen_profiles=${seen_profiles}${profile}' '
    duw_stage_profile_default_preseed_tree "${payload_mount}" "${profile}"
  done
)

duw_stage_secure_boot_kernel_asset() (
  source_path="$1"
  destination_path="$2"
  member_path="$3"

  duw_step "Signing kernel ${member_path} with Secure Boot owner db key"
  duw_sign_pe_binary "${source_path}" "${destination_path}"
  duw_sign_grub_data_file "${destination_path}"
)

duw_stage_signed_kernel_assets_from_render_payload() (
  payload=$1; temp_root=$2; esp_mount=$3
  rows=$(duw_json_signed_assets_tsv "${payload}")
  while IFS=$(printf '\t') read -r iso_path member_path asset_path; do
    [ -n "${iso_path}" ] && [ -n "${member_path}" ] && [ -n "${asset_path}" ] || continue
    source_temp=${temp_root}/secureboot-src$(printf '%s' "${member_path}")
    duw_extract_iso_member "${iso_path}" "${member_path}" "${source_temp}"
    duw_stage_secure_boot_kernel_asset "${source_temp}" "${esp_mount}${asset_path}" "${member_path}"
  done <<EOF_ROWS
${rows}
EOF_ROWS
)

duw_patch_cdrom_detect_postinst_file() (
  postinst="$1"
  python3 - "${postinst}" <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
text = path.read_text(encoding="utf-8")
marker = "# debian-usb: explicit installer media binding v2."
if marker in text:
    raise SystemExit(0)

insert_anchor = "# Is a cdrom already mounted? If so, assume it's the right one.\n"
if insert_anchor not in text:
    raise SystemExit("missing cdrom-detect already-mounted anchor")

patch_block = """# debian-usb: explicit installer media binding v2.
DUSB_INSTALL_MEDIA_MOUNTED=0
dusbf_cmdline_install_media_dev() {
\tfor word in $(cat /proc/cmdline 2>/dev/null); do
\t\tcase "$word" in
\t\t\tINSTALL_MEDIA_DEV=*|install_media_dev=*)
\t\t\t\tvalue=${word#*=}
\t\t\t\tvalue=${value%\\"}
\t\t\t\tvalue=${value#\\"}
\t\t\t\tvalue=${value%\\'}
\t\t\t\tvalue=${value#\\'}
\t\t\t\tif [ -n "$value" ]; then
\t\t\t\t\tprintf '%s\\n' "$value"
\t\t\t\t\treturn 0
\t\t\t\tfi
\t\t\t\t;;
\t\tesac
\tdone
\treturn 1
}

dusbf_resolve_install_media_dev() {
\tdusbf_requested_dev="$1"
\tif [ -e "$dusbf_requested_dev" ]; then
\t\tprintf '%s\\n' "$dusbf_requested_dev"
\t\treturn 0
\tfi
\tcase "$dusbf_requested_dev" in
\t\t/dev/disk/by-partuuid/*)
\t\t\tdusbf_blkid_key=PARTUUID
\t\t\tdusbf_blkid_value=${dusbf_requested_dev##*/}
\t\t\t;;
\t\t/dev/disk/by-uuid/*)
\t\t\tdusbf_blkid_key=UUID
\t\t\tdusbf_blkid_value=${dusbf_requested_dev##*/}
\t\t\t;;
\t\t*)
\t\t\treturn 1
\t\t\t;;
\tesac
\tfor dusbf_candidate_dev in $(blkid -t "$dusbf_blkid_key=$dusbf_blkid_value" -o device 2>/dev/null); do
\t\tif [ -e "$dusbf_candidate_dev" ]; then
\t\t\tprintf '%s\\n' "$dusbf_candidate_dev"
\t\t\treturn 0
\t\tfi
\tdone
\treturn 1
}

dusbf_install_media_dev="${INSTALL_MEDIA_DEV:-}"
if [ -z "$dusbf_install_media_dev" ]; then
\tdusbf_install_media_dev=$(dusbf_cmdline_install_media_dev || true)
fi

if [ -n "$dusbf_install_media_dev" ]; then
\tINSTALL_MEDIA_DEV="$dusbf_install_media_dev"
\tDUSB_RESOLVED_INSTALL_MEDIA_DEV=
\tlog "Trying explicit installer media device from INSTALL_MEDIA_DEV: $INSTALL_MEDIA_DEV"
\tmkdir /cdrom 2>/dev/null || true
\tfor count in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15; do
\t\tDUSB_RESOLVED_INSTALL_MEDIA_DEV=$(dusbf_resolve_install_media_dev "$INSTALL_MEDIA_DEV" || true)
\t\tif [ -n "$DUSB_RESOLVED_INSTALL_MEDIA_DEV" ]; then
\t\t\tbreak
\t\tfi
\t\tlog "Waiting for explicit installer media device: $INSTALL_MEDIA_DEV"
\t\tudevadm settle --timeout=1 2>/dev/null || true
\t\tsleep 1
\tdone
\tif [ -n "$DUSB_RESOLVED_INSTALL_MEDIA_DEV" ]; then
\t\tif [ "$DUSB_RESOLVED_INSTALL_MEDIA_DEV" != "$INSTALL_MEDIA_DEV" ]; then
\t\t\tlog "Resolved explicit installer media device: $INSTALL_MEDIA_DEV -> $DUSB_RESOLVED_INSTALL_MEDIA_DEV"
\t\tfi
\t\tif mount | grep -q 'on /cdrom'; then
\t\t\tlog "Unmounting pre-mounted /cdrom before binding explicit installer media: $DUSB_RESOLVED_INSTALL_MEDIA_DEV"
\t\t\tumount /cdrom 2>/dev/null || true
\t\tfi
\t\tif try_mount "$DUSB_RESOLVED_INSTALL_MEDIA_DEV" "$CDFS"; then
\t\t\tdb_set cdrom-detect/hybrid true
\t\t\tDUSB_INSTALL_MEDIA_MOUNTED=1
\t\telse
\t\t\tlog "Explicit installer media mount failed; refusing automatic scan to avoid selecting the wrong ISO: device=$DUSB_RESOLVED_INSTALL_MEDIA_DEV fstype=$CDFS"
\t\t\tfail
\t\tfi
\telse
\t\tlog "Explicit installer media device did not appear; refusing automatic scan to avoid selecting the wrong ISO: $INSTALL_MEDIA_DEV"
\t\tfail
\tfi
fi

"""

insert_at = text.index(insert_anchor)
text = text[:insert_at] + patch_block + text[insert_at:]

scan_start = insert_at + len(patch_block)
wait_anchor = 'if [ "$OS" = "linux" ]; then'
wait_replacement = 'if [ "$DUSB_INSTALL_MEDIA_MOUNTED" != "1" ] && [ "$OS" = "linux" ]; then'
wait_at = text.find(wait_anchor, scan_start)
if wait_at == -1:
    raise SystemExit("missing cdrom-detect usb wait anchor")
text = text[:wait_at] + wait_replacement + text[wait_at + len(wait_anchor):]

loop_anchor = "while true; do"
loop_replacement = 'while [ "$DUSB_INSTALL_MEDIA_MOUNTED" != "1" ]; do'
loop_at = text.find(loop_anchor, scan_start)
if loop_at == -1:
    raise SystemExit("missing cdrom-detect auto-scan loop anchor")
text = text[:loop_at] + loop_replacement + text[loop_at + len(loop_anchor):]

path.write_text(text, encoding="utf-8")
PY
)

duw_initrd_has_cdrom_detect_postinst() (
  initrd_path="$1"

  [ -f "${initrd_path}" ] && [ -s "${initrd_path}" ] || return 1
  gzip -dc "${initrd_path}" | cpio -i --to-stdout --no-absolute-filenames var/lib/dpkg/info/cdrom-detect.postinst >/dev/null 2>&1
)

duw_patch_installer_initrd_cdrom_detect() (
  initrd_path=$1
  output=
  exit_code=0
  [ -f "${initrd_path}" ] && [ -s "${initrd_path}" ] || duw_die "installer initrd is missing or empty: ${initrd_path}"
  temp_root=$(duw_create_temp_root initrd-patch)
  tree=${temp_root}/tree
  patched_initrd=${temp_root}/initrd.gz
  mkdir -p -- "${tree}" || {
    exit_code=$?
    rm -rf -- "${temp_root}" || true
    duw_die "failed to create initrd patch tree ${tree} (exit ${exit_code})"
  }
  if output=$(gzip -t "${initrd_path}" 2>&1); then :; else
    exit_code=$?
    rm -rf -- "${temp_root}" || true
    duw_die "installer initrd is not a valid gzip stream: ${initrd_path} (exit ${exit_code}): ${output}"
  fi
  if output=$( (cd "${tree}" && gzip -dc "${initrd_path}" | cpio -id --no-absolute-filenames --quiet) 2>&1); then :; else
    exit_code=$?
    rm -rf -- "${temp_root}" || true
    duw_die "failed to extract installer initrd for cdrom-detect patch: ${initrd_path} (exit ${exit_code}): ${output}"
  fi
  postinst=${tree}/var/lib/dpkg/info/cdrom-detect.postinst
  [ -f "${postinst}" ] || {
    rm -rf -- "${temp_root}" || true
    duw_die "installer initrd does not contain cdrom-detect.postinst: ${initrd_path}"
  }
  if output=$(duw_patch_cdrom_detect_postinst_file "${postinst}" 2>&1); then :; else
    exit_code=$?
    rm -rf -- "${temp_root}" || true
    duw_die "failed to patch cdrom-detect.postinst in ${initrd_path} (exit ${exit_code}): ${output}"
  fi
  if output=$( (cd "${tree}" && find . -print0 | sort -z | cpio --null -H newc -o --quiet | gzip -9n >"${patched_initrd}") 2>&1); then :; else
    exit_code=$?
    rm -rf -- "${temp_root}" || true
    duw_die "failed to rebuild patched installer initrd ${initrd_path} (exit ${exit_code}): ${output}"
  fi
  cp -f -- "${patched_initrd}" "${initrd_path}" || {
    exit_code=$?
    rm -rf -- "${temp_root}" || true
    duw_die "failed to install patched initrd ${initrd_path} (exit ${exit_code})"
  }
  rm -rf -- "${temp_root}" || duw_die "failed to remove initrd patch workspace: ${temp_root}"
)

duw_stage_boot_initrd_assets_from_render_payload() (
  payload=$1
  esp_mount=$2
  list_separator=$(printf '\037')
  patch_initrds=
  rows=$(duw_json_installer_boot_initrd_patches_tsv "${payload}")
  while IFS=$(printf '\t') read -r iso_path member_path asset_path; do
    [ -n "${iso_path}" ] && [ -n "${member_path}" ] && [ -n "${asset_path}" ] || continue
    case ${member_path} in /*) ;; *) duw_die "refusing to patch non-absolute initrd member path: ${member_path}" ;; esac
    case ${asset_path} in /EFI/debian-usb/assets/*) ;; *) duw_die "refusing to patch initrd outside ESP boot assets: ${asset_path}" ;; esac
    patch_initrds=${patch_initrds}${patch_initrds:+${list_separator}}${asset_path}
  done <<EOF_ROWS
${rows}
EOF_ROWS

  rows=$(duw_json_boot_initrd_assets_tsv "${payload}")
  while IFS=$(printf '\t') read -r iso_path member_path asset_path; do
    [ -n "${iso_path}" ] && [ -n "${member_path}" ] && [ -n "${asset_path}" ] || continue
    case ${asset_path} in /EFI/debian-usb/assets/*) ;; *) duw_die "refusing to stage initrd outside ESP boot assets: ${asset_path}" ;; esac
    duw_extract_iso_member "${iso_path}" "${member_path}" "${esp_mount}${asset_path}"
    if duw_internal_list_contains "${patch_initrds}" "${list_separator}" "${asset_path}"; then
      duw_step "Patching installer initrd cdrom-detect media binding at ${asset_path}"
      duw_patch_installer_initrd_cdrom_detect "${esp_mount}${asset_path}"
      patch_initrds=$(duw_internal_list_remove "${patch_initrds}" "${list_separator}" "${asset_path}")
    fi
    duw_step "Signing initrd OpenPGP companion signature for ${asset_path}"
    duw_sign_grub_data_file "${esp_mount}${asset_path}"
  done <<EOF_ROWS
${rows}
EOF_ROWS
  if [ -n "${patch_initrds}" ]; then
    first_missing=$(duw_internal_list_first "${patch_initrds}" "${list_separator}")
    duw_die "installer initrd patch was requested for an asset that was not staged: ${first_missing}"
  fi
)

duw_stage_secure_boot_assets_from_render_payload() (
  payload="$1"
  temp_root="$2"
  esp_mount="$3"

  duw_stage_signed_kernel_assets_from_render_payload "${payload}" "${temp_root}" "${esp_mount}"
  duw_stage_boot_initrd_assets_from_render_payload "${payload}" "${esp_mount}"
)

duw_inspect_managed_media() {
  _duw_inspect_profile=$1
  _duw_inspect_iso_path=$2
  _duw_inspect_config_path=${3:-}
  _duw_inspect_custom_menu=${4:-0}
  _duw_inspect_source_role=${5:-primary}
  _duw_inspect_helper=$(duw_python_helper_path)
  set -- inspect-iso --iso-path "${_duw_inspect_iso_path}" --profile "${_duw_inspect_profile}"
  [ -z "${_duw_inspect_config_path}" ] || set -- "$@" --config "${_duw_inspect_config_path}"
  [ -z "${_duw_inspect_custom_menu}" ] || set -- "$@" --use-custom-menu "${_duw_inspect_custom_menu}"
  [ -z "${_duw_inspect_source_role}" ] || set -- "$@" --source-role "${_duw_inspect_source_role}"
  _duw_inspect_output=$(duw_run_python_helper "${_duw_inspect_helper}" "$@") || duw_die "failed to inspect ${_duw_inspect_iso_path}"
  DUSB_INSPECTED_MEDIA_CLASS=$(duw_json_field "${_duw_inspect_output}" media_class)
  DUSB_MANAGED_PAYLOAD_LAYOUT=$(duw_json_field "${_duw_inspect_output}" managed_payload_layout)
  [ -n "${DUSB_MANAGED_PAYLOAD_LAYOUT}" ] || DUSB_MANAGED_PAYLOAD_LAYOUT=extracted
  case ${DUSB_MANAGED_PAYLOAD_LAYOUT} in extracted|raw-iso|iso-store|shared-data) ;; *) duw_die "unsupported managed payload layout: ${DUSB_MANAGED_PAYLOAD_LAYOUT}" ;; esac
}

duw_payload_partition_precedes_esp() (
  profile="$1"
  payload_layout="$2"
  media_class="$3"

  if [ "${payload_layout}" = "raw-iso" ]; then
    return 0
  fi
  if [ "${payload_layout}" != "iso-store" ]; then
    return 1
  fi
  case "${profile}" in
    debian|kali-linux|kali-purple)
      [ "${media_class}" = "installer" ] || [ "${media_class}" = "hybrid" ]
      return
      ;;
  esac
  return 1
)

duw_effective_payload_layout() (
  profile=$1; configured_layout=$2; config_path=${3:-}; use_custom_grub_menu=${4:-0}
  helper=$(duw_python_helper_path)
  set -- effective-payload-layout --profile "${profile}" --configured-layout "${configured_layout}"
  [ -z "${config_path}" ] || set -- "$@" --config "${config_path}"
  [ -z "${use_custom_grub_menu}" ] || set -- "$@" --use-custom-menu "${use_custom_grub_menu}"
  output=$(duw_run_python_helper "${helper}" "$@") || duw_die "failed to resolve the managed payload layout for ${profile}"
  duw_json_field "${output}" managed_payload_layout
)

duw_render_managed_grub() {
  _duw_render_profile=$1; _duw_render_iso_path=$2; _duw_render_live_uuid=$3; _duw_render_persistence=$4
  _duw_render_persistence_mode=$5; _duw_render_kernel_args=$6; _duw_render_menu_label=$7
  _duw_render_kernel_path=$8; _duw_render_initrd_path=$9; shift 9
  _duw_render_config_path=$1; _duw_render_boot_uuid=$2; _duw_render_toram=$3; _duw_render_custom=$4
  _duw_render_preserve=${5:-0}; _duw_render_preseed=${6:-0}; _duw_render_source_role=${7:-primary}
  _duw_render_helper=$(duw_python_helper_path)
  set -- render-managed-grub --iso-path "${_duw_render_iso_path}" --profile "${_duw_render_profile}" --live-uuid "${_duw_render_live_uuid}" --persistence "${_duw_render_persistence}"
  [ -z "${_duw_render_persistence_mode}" ] || set -- "$@" --persistence-mode "${_duw_render_persistence_mode}"
  [ -z "${_duw_render_config_path}" ] || set -- "$@" --config "${_duw_render_config_path}"
  [ -z "${_duw_render_menu_label}" ] || set -- "$@" --menu-label "${_duw_render_menu_label}"
  [ -z "${_duw_render_kernel_args}" ] || set -- "$@" --kernel-args "${_duw_render_kernel_args}"
  [ -z "${_duw_render_kernel_path}" ] || set -- "$@" --kernel-path "${_duw_render_kernel_path}"
  [ -z "${_duw_render_initrd_path}" ] || set -- "$@" --initrd-path "${_duw_render_initrd_path}"
  [ -z "${_duw_render_boot_uuid}" ] || set -- "$@" --boot-assets-uuid "${_duw_render_boot_uuid}"
  [ -z "${_duw_render_toram}" ] || set -- "$@" --live-toram "${_duw_render_toram}"
  [ -z "${_duw_render_custom}" ] || set -- "$@" --use-custom-menu "${_duw_render_custom}"
  [ -z "${_duw_render_preserve}" ] || set -- "$@" --preserve-upstream-grub-entries "${_duw_render_preserve}"
  [ -z "${_duw_render_preseed}" ] || set -- "$@" --include-preseed "${_duw_render_preseed}"
  [ -z "${_duw_render_source_role}" ] || set -- "$@" --source-role "${_duw_render_source_role}"
  _duw_render_output=$(duw_run_python_helper "${_duw_render_helper}" "$@") || duw_die "failed to render managed GRUB configuration"
  DUSB_MANAGED_RENDER_OUTPUT=${_duw_render_output}
  DUSB_MANAGED_GRUB_CFG=$(duw_json_field "${_duw_render_output}" grub_cfg)
  DUSB_MANAGED_ENTRY_COUNT=$(duw_json_field "${_duw_render_output}" entry_count)
  DUSB_MANAGED_MEDIA_CLASS=$(duw_json_field "${_duw_render_output}" media_class)
}

duw_validate_multios_plan() (
  plan_path="$1"
  [ -f "${plan_path}" ] || duw_die "Multi-OS plan is not a regular file: ${plan_path}"
  helper="$(duw_python_helper_path)"
  duw_run_python_helper "${helper}" validate-multios-plan --plan "${plan_path}" --inspect-media >/dev/null || duw_die "failed to validate Multi-OS plan"
)

duw_multios_items_tsv() (
  plan_path="$1"
  python3 - "${plan_path}" <<'PY'
import json
import sys
from pathlib import Path

plan = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
separator = "\x1f"
fields = (
    "id",
    "profile",
    "source_role",
    "title",
    "iso_path",
    "managed_payload_layout",
    "persistence",
    "persistence_mode",
    "persistence_size_gib",
    "persistence_fs_label",
    "persistence_partlabel",
    "payload_fs_label",
    "payload_partlabel",
    "offline_preseed_source_dir",
    "kernel_args",
    "menu_label",
    "kernel_path",
    "initrd_path",
)
for item in plan["items"]:
    values = []
    for field in fields:
        value = item.get(field, "")
        if isinstance(value, bool):
            value = "1" if value else "0"
        else:
            value = str(value)
        values.append(value.replace(separator, " ").replace("\t", " ").replace("\n", " "))
    print(separator.join(values))
PY
)

duw_multios_use_custom_grub_menu() (
  plan_path="$1"
  python3 - "${plan_path}" <<'PY'
import json
import sys
from pathlib import Path

plan = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
print("1" if bool(plan.get("use_custom_grub_menu", False)) else "0")
PY
)

duw_multios_preserve_upstream_grub_entries() (
  plan_path="$1"
  python3 - "${plan_path}" <<'PY'
import json
import sys
from pathlib import Path

plan = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
print("1" if bool(plan.get("preserve_upstream_grub_entries", False)) else "0")
PY
)

duw_multios_secure_boot_trust() (
  plan_path="$1"
  python3 - "${plan_path}" <<'PY'
import json
import sys
from pathlib import Path

plan = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
value = str(plan.get("secure_boot_trust") or "mok").strip() or "mok"
print(value)
PY
)

duw_multios_esp_label() (
  plan_path="$1"
  label="$(python3 - "${plan_path}" <<'PY'
import json
import sys
from pathlib import Path

plan = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
print(str(plan.get("esp_label") or "").strip())
PY
)"
  if [ -n "${label}" ]; then
    printf '%s\n' "${label}"
  else
    printf '%s\n' "${DEFAULT_ESP_LABEL}"
  fi
)

duw_render_multios_grub() {
  _duw_multi_plan=$1; _duw_multi_config=$2; _duw_multi_boot_uuid=$3
  shift 3
  _duw_multi_uuid_separator=$(printf '\037')
  _duw_multi_uuid_values=
  for _duw_multi_uuid in "$@"; do
    _duw_multi_uuid_values=${_duw_multi_uuid_values}${_duw_multi_uuid_values:+${_duw_multi_uuid_separator}}${_duw_multi_uuid}
  done
  _duw_multi_helper=$(duw_python_helper_path)
  set -- render-multios-grub --plan "${_duw_multi_plan}"
  [ -z "${_duw_multi_config}" ] || set -- "$@" --config "${_duw_multi_config}"
  [ -z "${_duw_multi_boot_uuid}" ] || set -- "$@" --boot-assets-uuid "${_duw_multi_boot_uuid}"
  _duw_multi_old_ifs=${IFS}; IFS=${_duw_multi_uuid_separator}; set -f
  for _duw_multi_uuid in ${_duw_multi_uuid_values}; do set -- "$@" --payload-uuid "${_duw_multi_uuid}"; done
  set +f; IFS=${_duw_multi_old_ifs}
  _duw_multi_output=$(duw_run_python_helper "${_duw_multi_helper}" "$@") || duw_die "failed to render Multi-OS GRUB configuration"
  DUSB_MANAGED_RENDER_OUTPUT=${_duw_multi_output}
  DUSB_MANAGED_GRUB_CFG=$(duw_json_field "${_duw_multi_output}" grub_cfg)
  DUSB_MANAGED_ENTRY_COUNT=$(duw_json_field "${_duw_multi_output}" entry_count)
  DUSB_MANAGED_MEDIA_CLASS=$(duw_json_field "${_duw_multi_output}" media_class)
}

duw_write_effective_multios_plan() (
  source_plan="$1"
  output_plan="$2"
  shift 2
  python3 - "${source_plan}" "${output_plan}" "$@" <<'PY'
import json
import sys
from pathlib import Path

source = Path(sys.argv[1])
output = Path(sys.argv[2])
replacements = {}
args = sys.argv[3:]
if len(args) % 3:
    raise SystemExit("effective plan replacements must use <id> <iso_path> <layout> triplets")
for index in range(0, len(args), 3):
    item_id, iso_path, layout = args[index : index + 3]
    if not item_id or not iso_path or not layout:
        raise SystemExit("effective plan replacement triplets must not contain empty fields")
    replacements[item_id] = (iso_path, layout)

plan = json.loads(source.read_text(encoding="utf-8"))
for item in plan.get("items", []):
    if not isinstance(item, dict):
        continue
    item_id = str(item.get("id", ""))
    if item_id in replacements:
        iso_path, layout = replacements[item_id]
        item["iso_path"] = iso_path
        item["managed_payload_layout"] = layout
output.write_text(json.dumps(plan, indent=2, sort_keys=True) + "\n", encoding="utf-8")
PY
)

duw_build_single_iso_store_usb() (
  profile="$1"
  iso_path="$2"
  device="$3"
  source_role="$4"
  kernel_args="$5"
  menu_label="$6"
  kernel_override="$7"
  initrd_override="$8"
  config_path="$9"
  live_toram_override="${10}"
  use_custom_grub_menu="${11}"
  preserve_upstream_grub_entries="${12}"
  include_preseed="${13}"
  esp_start=3

  # The caller supplies independent Secure Boot state to this function subshell.
  # shellcheck disable=SC2031
  DUSB_SECURE_BOOT_TRUST="$(duw_effective_secure_boot_trust "${DUSB_SECURE_BOOT_TRUST:-}")"
  iso_bytes="$(duw_media_source_size_bytes "${iso_path}")"
  iso_mib="$(duw_bytes_to_mib "${iso_bytes}")"
  duw_render_managed_grub "${profile}" "${iso_path}" "DUSB-PREFLIGHT-DATA" "0" "" "${kernel_args}" "${menu_label}" "${kernel_override}" "${initrd_override}" "${config_path}" "DUSB-PREFLIGHT-ESP" "${live_toram_override}" "${use_custom_grub_menu}" "${preserve_upstream_grub_entries}" "${include_preseed}" "${source_role}"
  esp_size_mib="$(duw_required_esp_size_mib_from_render_payload "${DUSB_MANAGED_RENDER_OUTPUT}" 0)"
  esp_end=$((esp_start + esp_size_mib))
  data_start="${esp_end}"
  required_mib=$((iso_mib + esp_size_mib + 256))
  device_bytes="$(blockdev --getsize64 "${device}")"
  device_mib="$(duw_bytes_to_mib "${device_bytes}")"
  usable_mib=$((device_mib - 8))
  if [ "${required_mib}" -gt "${usable_mib}" ]; then
    duw_die "device ${device} does not have enough space for the ISO-store USB layout; need at least ${required_mib} MiB"
  fi

  temp_root="$(duw_create_temp_root "single-isostore-usb")"
  data_mount="${temp_root}/multiboot"
  esp_mount="${data_mount}/boot/efi"
  mkdir -p -- "${esp_mount}"
  duw_set_cleanup_trap "${temp_root}" "${esp_mount}" "${data_mount}"


  payload_fs_label="$(duw_profile_payload_fs_label "${profile}" "${source_role}" "${DUSB_INSPECTED_MEDIA_CLASS}")"
  payload_partlabel="$(duw_profile_payload_partlabel "${profile}" "${source_role}" "${DUSB_INSPECTED_MEDIA_CLASS}")"

  duw_step "Preparing managed ISO-store USB layout on ${device} from ${iso_path}"
  duw_note "Target device: $(duw_device_pretty_name "${device}")"
  duw_note "Managed payload layout: ISO-store filesystem on payload partition ${payload_fs_label}"
  duw_note "GRUB menu mode: $(duw_grub_menu_mode_label "${use_custom_grub_menu}" "${preserve_upstream_grub_entries}")"
  duw_note_dynamic_esp_size "${esp_size_mib}"
  duw_unmount_device_children "${device}"
  duw_reset_partition_table_state "${device}"
  duw_step "Partitioning ${device}"
  duw_write_gpt_label "${device}"
  parted -s "${device}" unit MiB mkpart BIOSBOOT 1 3
  parted -s "${device}" set 1 bios_grub on
  parted -s "${device}" unit MiB mkpart "$(duw_effective_esp_label)" fat32 "${esp_start}" "${esp_end}"
  parted -s "${device}" set 2 esp on
  parted -s "${device}" unit MiB mkpart "${payload_partlabel}" ext4 "${data_start}" 100%
  partprobe "${device}" || duw_die "failed to refresh the kernel partition table for ${device}"
  duw_settle_block_state

  esp_part="$(duw_wait_for_partition_device "${device}" 2)"
  data_part="$(duw_wait_for_partition_device "${device}" 3)"
  duw_make_vfat_filesystem "${esp_part}" "$(duw_effective_esp_label)" "${device}"
  duw_make_ext4_filesystem "${data_part}" "${payload_fs_label}" "${device}"
  sync
  duw_wait_for_filesystem_signature "${esp_part}"
  duw_wait_for_filesystem_signature "${data_part}"
  esp_uuid="$(duw_partition_uuid "${esp_part}")"
  data_uuid="$(duw_partition_uuid "${data_part}")"

  duw_mount_partition "${data_part}" "${data_mount}" ext4
  mkdir -p -- "${esp_mount}"
  duw_mount_partition "${esp_part}" "${esp_mount}" vfat
  duw_prepare_secure_boot_identity
  duw_stage_secure_boot_support "${esp_mount}" "${temp_root}"
  duw_note_secure_boot_requirement
  if [ "${include_preseed}" = "1" ]; then
    duw_stage_single_profile_preseed_tree "${data_mount}" "${profile}"
  fi

  duw_step "Rendering managed GRUB menu"
  duw_render_managed_grub "${profile}" "${iso_path}" "${data_uuid}" "0" "" "${kernel_args}" "${menu_label}" "${kernel_override}" "${initrd_override}" "${config_path}" "${esp_uuid}" "${live_toram_override}" "${use_custom_grub_menu}" "${preserve_upstream_grub_entries}" "${include_preseed}"
  duw_stage_secure_boot_assets_from_render_payload "${DUSB_MANAGED_RENDER_OUTPUT}" "${temp_root}" "${esp_mount}"
  duw_stage_iso_store_from_render_payload "${DUSB_MANAGED_RENDER_OUTPUT}" "${data_mount}" "${data_mount}" "${iso_path}"
  duw_note "Rendered managed ${DUSB_MANAGED_MEDIA_CLASS} GRUB menu with ${DUSB_MANAGED_ENTRY_COUNT} entries"

  duw_install_multiboot_grub_bootloaders "${device}" "${data_part}" "${data_mount}" "${esp_part}" "${esp_mount}"
  boot_cfg="${data_mount}/boot/grub/grub.cfg"
  duw_require_mount_source "${data_part}" "${data_mount}"
  duw_step "Writing GRUB configuration to ${data_part}:/boot/grub/grub.cfg (mounted at ${data_mount})"
  mkdir -p -- "$(dirname -- "${boot_cfg}")"
  printf '%s' "${DUSB_MANAGED_GRUB_CFG}" >"${boot_cfg}"
  duw_sign_grub_data_file "${boot_cfg}"
  duw_stage_secure_boot_manifest "${esp_mount}" "${boot_cfg}" "${payload_fs_label}:/boot/grub/grub.cfg"
  duw_step "Finalizing USB writes"
  sync
  duw_finalize_temp_root "${temp_root}"
  duw_step "Managed ISO-store USB creation finished for ${device}"
)

duw_multios_state_validate_field() (
  case $1 in
    id|profile|source_role|title|iso_path|layout|persist_flag|persist_mode|persist_size|persist_fs_label|persist_partlabel|payload_fs_label|payload_partlabel|offline_preseed_dir|kernel_args|menu_label|kernel_path|initrd_path|media_class|payload_required_mib|payload_start|payload_end|payload_part|payload_uuid|persist_start|persist_end|persist_part|persist_key_file|payload_write_iso) ;;
    *) duw_die "invalid internal Multi-OS state field: $1" ;;
  esac
)

duw_multios_state_item_dir() (
  state_dir=$1
  index=$2
  case ${index} in ''|*[!0-9]*) duw_die "invalid internal Multi-OS item index: ${index}" ;; esac
  printf '%s/items/%s\n' "${state_dir}" "${index}"
)

duw_multios_state_set() (
  state_dir=$1
  index=$2
  field=$3
  value=${4-}
  duw_multios_state_validate_field "${field}"
  item_dir=$(duw_multios_state_item_dir "${state_dir}" "${index}")
  [ -d "${item_dir}" ] || duw_die "missing internal Multi-OS state item: ${index}"
  printf '%s' "${value}" >"${item_dir}/${field}" || duw_die "failed to write internal Multi-OS state field ${field} for item ${index}"
)

duw_multios_state_get() (
  state_dir=$1
  index=$2
  field=$3
  duw_multios_state_validate_field "${field}"
  item_dir=$(duw_multios_state_item_dir "${state_dir}" "${index}")
  [ -f "${item_dir}/${field}" ] || duw_die "missing internal Multi-OS state field ${field} for item ${index}"
  cat -- "${item_dir}/${field}"
)

duw_multios_state_count() (
  state_dir=$1
  count=$(cat -- "${state_dir}/count" 2>/dev/null) || duw_die "missing internal Multi-OS item count"
  case ${count} in ''|*[!0-9]*) duw_die "invalid internal Multi-OS item count: ${count}" ;; esac
  printf '%s\n' "${count}"
)

duw_multios_state_create() (
  plan_path=$1
  state_dir=$2
  rows_path=${state_dir}/rows.usv
  mkdir -p -- "${state_dir}/items" || duw_die "failed to create internal Multi-OS state directory"
  duw_multios_items_tsv "${plan_path}" >"${rows_path}" || duw_die "failed to read Multi-OS plan items"
  separator=$(printf '\037')
  index=0
  while IFS="${separator}" read -r id profile source_role title iso_path layout persist_flag persist_mode persist_size persist_fs_label persist_partlabel payload_fs_label payload_partlabel offline_preseed_dir kernel_args menu_label kernel_path initrd_path; do
    [ -n "${id}" ] || continue
    item_dir=${state_dir}/items/${index}
    mkdir -p -- "${item_dir}" || duw_die "failed to create internal Multi-OS item state: ${index}"
    printf '%s' "${id}" >"${item_dir}/id"
    printf '%s' "${profile}" >"${item_dir}/profile"
    printf '%s' "${source_role:-primary}" >"${item_dir}/source_role"
    printf '%s' "${title}" >"${item_dir}/title"
    printf '%s' "${iso_path}" >"${item_dir}/iso_path"
    printf '%s' "${layout}" >"${item_dir}/layout"
    printf '%s' "${persist_flag}" >"${item_dir}/persist_flag"
    printf '%s' "${persist_mode}" >"${item_dir}/persist_mode"
    printf '%s' "${persist_size}" >"${item_dir}/persist_size"
    printf '%s' "${persist_fs_label}" >"${item_dir}/persist_fs_label"
    printf '%s' "${persist_partlabel}" >"${item_dir}/persist_partlabel"
    printf '%s' "${payload_fs_label}" >"${item_dir}/payload_fs_label"
    printf '%s' "${payload_partlabel}" >"${item_dir}/payload_partlabel"
    printf '%s' "${offline_preseed_dir}" >"${item_dir}/offline_preseed_dir"
    printf '%s' "${kernel_args}" >"${item_dir}/kernel_args"
    printf '%s' "${menu_label}" >"${item_dir}/menu_label"
    printf '%s' "${kernel_path}" >"${item_dir}/kernel_path"
    printf '%s' "${initrd_path}" >"${item_dir}/initrd_path"
    index=$((index + 1))
  done <"${rows_path}"
  printf '%s\n' "${index}" >"${state_dir}/count"
)

duw_multios_write_effective_plan_from_state() (
  source_plan=$1
  output_plan=$2
  state_dir=$3
  item_count=$(duw_multios_state_count "${state_dir}")
  set -- "${source_plan}" "${output_plan}"
  index=0
  while [ "${index}" -lt "${item_count}" ]; do
    set -- "$@" \
      "$(duw_multios_state_get "${state_dir}" "${index}" id)" \
      "$(duw_multios_state_get "${state_dir}" "${index}" iso_path)" \
      "$(duw_multios_state_get "${state_dir}" "${index}" layout)"
    index=$((index + 1))
  done
  duw_write_effective_multios_plan "$@"
)

duw_multios_stage_default_preseed_from_state() (
  payload_mount=$1
  state_dir=$2
  item_count=$(duw_multios_state_count "${state_dir}")
  set -- "${payload_mount}"
  index=0
  while [ "${index}" -lt "${item_count}" ]; do
    set -- "$@" "$(duw_multios_state_get "${state_dir}" "${index}" profile)"
    index=$((index + 1))
  done
  duw_stage_default_preseed_trees "$@"
)

duw_multios_stage_selected_preseed_from_state() (
  payload_mount=$1
  state_dir=$2
  item_count=$(duw_multios_state_count "${state_dir}")
  separator=$(printf '\037')
  set -- "${payload_mount}"
  index=0
  while [ "${index}" -lt "${item_count}" ]; do
    profile=$(duw_multios_state_get "${state_dir}" "${index}" profile)
    case ${profile} in
      debian|kali-linux|kali-purple)
        source_dir=$(duw_multios_state_get "${state_dir}" "${index}" offline_preseed_dir)
        set -- "$@" "${profile}${separator}${source_dir}"
        ;;
    esac
    index=$((index + 1))
  done
  [ "$#" -eq 1 ] || duw_stage_multios_preseed_trees "$@"
)

duw_build_multios_iso_store_usb() (
  plan_path=$1
  device=$2
  config_path=$3
  esp_start=1

  duw_validate_multios_plan "${plan_path}"
  # This workflow intentionally keeps Secure Boot state inside its function subshell.
  # shellcheck disable=SC2030
  DUSB_SECURE_BOOT_TRUST=$(duw_effective_secure_boot_trust "$(duw_multios_secure_boot_trust "${plan_path}")")
  esp_label=$(duw_multios_esp_label "${plan_path}")
  use_custom_grub_menu=$(duw_multios_use_custom_grub_menu "${plan_path}")
  preserve_upstream_grub_entries=$(duw_multios_preserve_upstream_grub_entries "${plan_path}")
  temp_root=$(duw_create_temp_root multios-isostore-usb)
  duw_set_cleanup_trap "${temp_root}"
  state_dir=${temp_root}/plan-state
  duw_multios_state_create "${plan_path}" "${state_dir}"
  item_count=$(duw_multios_state_count "${state_dir}")
  [ "${item_count}" -ge 1 ] || duw_die "Multi-OS requires at least one OS item"

  total_iso_mib=0
  total_persist_mib=0
  index=0
  while [ "${index}" -lt "${item_count}" ]; do
    profile=$(duw_multios_state_get "${state_dir}" "${index}" profile)
    source_role=$(duw_multios_state_get "${state_dir}" "${index}" source_role)
    title=$(duw_multios_state_get "${state_dir}" "${index}" title)
    iso_path=$(duw_multios_state_get "${state_dir}" "${index}" iso_path)
    layout=$(duw_multios_state_get "${state_dir}" "${index}" layout)
    persist_flag=$(duw_multios_state_get "${state_dir}" "${index}" persist_flag)
    persist_mode=$(duw_multios_state_get "${state_dir}" "${index}" persist_mode)
    persist_size=$(duw_multios_state_get "${state_dir}" "${index}" persist_size)
    offline_preseed_dir=$(duw_multios_state_get "${state_dir}" "${index}" offline_preseed_dir)

    if [ "${source_role}" = netboot ]; then
      [ -d "${iso_path}" ] || duw_die "netboot source must be a prepared source directory: ${iso_path}"
    else
      [ -e "${iso_path}" ] || duw_die "media source does not exist: ${iso_path}"
      duw_inspect_managed_media "${profile}" "${iso_path}" "${config_path}" "${use_custom_grub_menu}" "${source_role}"
    fi
    [ -z "${offline_preseed_dir}" ] || duw_die "per-plan offline preseed directories are replaced by configured PRESEED_HOST_*_PATH staging"
    layout=$(duw_effective_payload_layout "${profile}" "${layout}" "${config_path}" "${use_custom_grub_menu}")
    [ "${layout}" = shared-data ] || duw_die "deterministic Multi-OS USB creation requires shared-data payloads, got ${layout} for ${title}"
    duw_multios_state_set "${state_dir}" "${index}" layout "${layout}"

    if [ "${persist_flag}" = 1 ]; then
      case ${persist_size} in ''|*[!0-9]*) duw_die "persistence size must be a positive integer for ${title}" ;; esac
      [ "${persist_size}" -gt 0 ] || duw_die "persistence size must be positive for ${title}"
      total_persist_mib=$((total_persist_mib + persist_size * 1024))
    else
      duw_multios_state_set "${state_dir}" "${index}" persist_mode ''
      duw_multios_state_set "${state_dir}" "${index}" persist_size 0
      duw_multios_state_set "${state_dir}" "${index}" persist_fs_label ''
      duw_multios_state_set "${state_dir}" "${index}" persist_partlabel ''
    fi
    iso_bytes=$(duw_media_source_size_bytes "${iso_path}")
    iso_mib=$(duw_bytes_to_mib "${iso_bytes}")
    total_iso_mib=$((total_iso_mib + iso_mib))
    index=$((index + 1))
  done

  data_mount=${temp_root}/multiboot
  esp_mount=${temp_root}/esp
  mkdir -p -- "${esp_mount}" "${data_mount}"
  duw_set_cleanup_trap "${temp_root}" "${esp_mount}" "${data_mount}"

  render_plan_path=${temp_root}/effective-multios-plan.json
  duw_multios_write_effective_plan_from_state "${plan_path}" "${render_plan_path}" "${state_dir}"
  set -- "${render_plan_path}" "${config_path}" DUSB-PREFLIGHT-ESP
  index=0
  while [ "${index}" -lt "${item_count}" ]; do
    id=$(duw_multios_state_get "${state_dir}" "${index}" id)
    set -- "$@" "${id}=DUSB-PREFLIGHT-DATA"
    index=$((index + 1))
  done
  duw_render_multios_grub "$@"
  esp_size_mib=$(duw_required_esp_size_mib_from_render_payload "${DUSB_MANAGED_RENDER_OUTPUT}" 0)
  esp_end=$((esp_start + esp_size_mib))
  data_start=${esp_end}
  required_mib=$((total_iso_mib + esp_size_mib + 64 + total_persist_mib))
  device_bytes=$(blockdev --getsize64 "${device}")
  device_mib=$(duw_bytes_to_mib "${device_bytes}")
  usable_mib=$((device_mib - 8))
  [ "${required_mib}" -le "${usable_mib}" ] || duw_die "device ${device} does not have enough space for Multi-OS ISO-store payloads and persistence; need at least ${required_mib} MiB"
  data_end_mib=$((usable_mib - total_persist_mib))
  [ "${data_end_mib}" -gt "${data_start}" ] || duw_die "device ${device} does not have enough space for the Multi-OS data partition after reserving the ${esp_size_mib} MiB ESP and persistence partitions"

  index=0
  while [ "${index}" -lt "${item_count}" ]; do
    duw_multios_state_set "${state_dir}" "${index}" persist_part ''
    duw_multios_state_set "${state_dir}" "${index}" persist_key_file ''
    persist_flag=$(duw_multios_state_get "${state_dir}" "${index}" persist_flag)
    persist_mode=$(duw_multios_state_get "${state_dir}" "${index}" persist_mode)
    if [ "${persist_flag}" = 1 ] && [ "${persist_mode}" = encrypted ]; then
      title=$(duw_multios_state_get "${state_dir}" "${index}" title)
      key_file=${temp_root}/persist-${index}.passphrase
      duw_multios_state_set "${state_dir}" "${index}" persist_key_file "${key_file}"
      duw_step "Collecting LUKS passphrase for ${title} encrypted persistence before partitioning starts"
      duw_collect_luks_passphrase_file "${title} encrypted persistence" "${key_file}"
    fi
    index=$((index + 1))
  done

  duw_step "Preparing deterministic Multi-OS ISO-store USB layout on ${device}"
  duw_note "Target device: $(duw_device_pretty_name "${device}")"
  duw_note "GRUB menu mode: $(duw_grub_menu_mode_label "${use_custom_grub_menu}" "${preserve_upstream_grub_entries}")"
  duw_note_dynamic_esp_size "${esp_size_mib}"
  duw_unmount_device_children "${device}"
  duw_reset_partition_table_state "${device}"
  duw_step "Partitioning ${device}"
  duw_write_gpt_label "${device}"
  parted -s "${device}" unit MiB mkpart "${esp_label}" fat32 "${esp_start}" "${esp_end}"
  parted -s "${device}" set 1 esp on
  if [ "${total_persist_mib}" -gt 0 ]; then
    parted -s "${device}" unit MiB mkpart "$(duw_shared_data_partlabel)" ext4 "${data_start}" "${data_end_mib}"
    cursor=${data_end_mib}
    partition_number=3
    index=0
    while [ "${index}" -lt "${item_count}" ]; do
      persist_flag=$(duw_multios_state_get "${state_dir}" "${index}" persist_flag)
      if [ "${persist_flag}" = 1 ]; then
        persist_size=$(duw_multios_state_get "${state_dir}" "${index}" persist_size)
        persist_partlabel=$(duw_multios_state_get "${state_dir}" "${index}" persist_partlabel)
        part_end=$((cursor + persist_size * 1024))
        parted -s "${device}" unit MiB mkpart "${persist_partlabel}" ext4 "${cursor}" "${part_end}"
        persist_part=$(duw_partition_path "${device}" "${partition_number}")
        duw_multios_state_set "${state_dir}" "${index}" persist_part "${persist_part}"
        cursor=${part_end}
        partition_number=$((partition_number + 1))
      fi
      index=$((index + 1))
    done
  else
    parted -s "${device}" unit MiB mkpart "$(duw_shared_data_partlabel)" ext4 "${data_start}" 100%
  fi
  partprobe "${device}" || duw_die "failed to refresh the kernel partition table for ${device}"
  duw_settle_block_state

  esp_part=$(duw_wait_for_partition_device "${device}" 1)
  data_part=$(duw_wait_for_partition_device "${device}" 2)
  index=0
  while [ "${index}" -lt "${item_count}" ]; do
    persist_part=$(duw_multios_state_get "${state_dir}" "${index}" persist_part)
    if [ -n "${persist_part}" ]; then
      persist_part=$(duw_wait_for_partition_device "${device}" "$(duw_partition_number_from_path "${persist_part}")")
      duw_multios_state_set "${state_dir}" "${index}" persist_part "${persist_part}"
    fi
    index=$((index + 1))
  done
  duw_make_vfat_filesystem "${esp_part}" "${esp_label}" "${device}"
  duw_make_ext4_filesystem "${data_part}" "$(duw_shared_data_fs_label)" "${device}"
  sync
  duw_wait_for_filesystem_signature "${esp_part}"
  duw_wait_for_filesystem_signature "${data_part}"
  esp_uuid=$(duw_partition_uuid "${esp_part}")
  data_uuid=$(duw_partition_uuid "${data_part}")

  duw_mount_partition "${data_part}" "${data_mount}" ext4
  duw_mount_partition "${esp_part}" "${esp_mount}" vfat
  duw_remove_shared_store_media_metadata "${data_mount}"
  duw_prepare_secure_boot_identity
  duw_stage_secure_boot_support "${esp_mount}" "${temp_root}"
  duw_note_secure_boot_requirement
  duw_multios_stage_default_preseed_from_state "${data_mount}" "${state_dir}"
  duw_remove_shared_store_media_metadata "${data_mount}"
  index=0
  while [ "${index}" -lt "${item_count}" ]; do
    persist_part=$(duw_multios_state_get "${state_dir}" "${index}" persist_part)
    if [ -n "${persist_part}" ]; then
      id=$(duw_multios_state_get "${state_dir}" "${index}" id)
      profile=$(duw_multios_state_get "${state_dir}" "${index}" profile)
      persist_mode=$(duw_multios_state_get "${state_dir}" "${index}" persist_mode)
      persist_fs_label=$(duw_multios_state_get "${state_dir}" "${index}" persist_fs_label)
      persist_key_file=$(duw_multios_state_get "${state_dir}" "${index}" persist_key_file)
      persist_mount=${temp_root}/persist-${index}
      mkdir -p -- "${persist_mount}"
      if [ "${persist_mode}" = encrypted ]; then
        duw_configure_encrypted_persistence_partition "${persist_part}" "${persist_mount}" "debian-usb-persist-${id}" "${profile}" "${persist_fs_label}" "${persist_key_file}" "${device}"
      else
        duw_configure_persistence_partition "${profile}" "${persist_fs_label}" "${persist_part}" "${persist_mount}" "${device}"
      fi
    fi
    index=$((index + 1))
  done

  duw_multios_write_effective_plan_from_state "${plan_path}" "${render_plan_path}" "${state_dir}"
  set -- "${render_plan_path}" "${config_path}" "${esp_uuid}"
  index=0
  while [ "${index}" -lt "${item_count}" ]; do
    id=$(duw_multios_state_get "${state_dir}" "${index}" id)
    set -- "$@" "${id}=${data_uuid}"
    index=$((index + 1))
  done
  duw_step "Rendering Multi-OS GRUB menu"
  duw_render_multios_grub "$@"
  duw_stage_secure_boot_assets_from_render_payload "${DUSB_MANAGED_RENDER_OUTPUT}" "${temp_root}" "${esp_mount}"
  duw_stage_iso_store_from_render_payload "${DUSB_MANAGED_RENDER_OUTPUT}" "${data_mount}" "${data_mount}"
  duw_remove_shared_store_media_metadata "${data_mount}"
  duw_note "Rendered ${DUSB_MANAGED_MEDIA_CLASS} GRUB menu with ${DUSB_MANAGED_ENTRY_COUNT} entries"

  duw_install_grub_bootloader "${esp_part}" "${esp_mount}"
  boot_cfg=${esp_mount}/boot/grub/grub.cfg
  duw_require_mount_source "${esp_part}" "${esp_mount}"
  duw_step "Writing GRUB configuration to ${esp_part}:/boot/grub/grub.cfg (mounted at ${esp_mount})"
  mkdir -p -- "$(dirname -- "${boot_cfg}")"
  printf '%s' "${DUSB_MANAGED_GRUB_CFG}" >"${boot_cfg}"
  duw_sign_grub_data_file "${boot_cfg}"
  duw_stage_secure_boot_manifest "${esp_mount}" "${boot_cfg}" ESP:/boot/grub/grub.cfg
  duw_step "Finalizing USB writes"
  sync
  duw_finalize_temp_root "${temp_root}"
  duw_step "Deterministic Multi-OS USB creation finished for ${device}"
)

duw_multios_uses_shared_iso_store_layout() (
  plan_path=$1
  config_path=${2:-}
  duw_validate_multios_plan "${plan_path}"
  use_custom_grub_menu=$(duw_multios_use_custom_grub_menu "${plan_path}")
  rows_path=$(mktemp "${TMPDIR:-/tmp}/debian-usb-layout.XXXXXX") || duw_die "failed to create temporary Multi-OS layout state"
  trap 'rm -f -- "${rows_path}"' 0
  duw_multios_items_tsv "${plan_path}" >"${rows_path}"
  separator=$(printf '\037')
  while IFS="${separator}" read -r id profile source_role title iso_path layout persist_flag persist_mode persist_size persist_fs_label persist_partlabel payload_fs_label payload_partlabel offline_preseed_dir kernel_args menu_label kernel_path initrd_path; do
    [ -n "${id}" ] || continue
    layout=$(duw_effective_payload_layout "${profile}" "${layout}" "${config_path}" "${use_custom_grub_menu}")
    [ "${layout}" = shared-data ] || return 1
    [ -z "${offline_preseed_dir}" ] || return 1
  done <"${rows_path}"
  return 0
)

duw_build_managed_usb() (
  profile="$1"
  iso_path="$2"
  device="$3"
  source_role="$4"
  with_persistence="$5"
  persistence_mode="$6"
  persistence_size_gib="$7"
  kernel_args="$8"
  menu_label="$9"
  kernel_override="${10}"
  initrd_override="${11}"
  config_path="${12}"
  live_toram_override="${13}"
  use_custom_grub_menu="${14}"
  preserve_upstream_grub_entries="${15:-0}"
  offline_preseed_source_dir="${16:-}"
  include_preseed="${17:-0}"
  payload_before_esp=0
  esp_preseed_mib=0
  encrypted_persistence_key_file=""
  # The caller supplies independent Secure Boot state to this function subshell.
  # shellcheck disable=SC2031
  DUSB_SECURE_BOOT_TRUST="$(duw_effective_secure_boot_trust "${DUSB_SECURE_BOOT_TRUST:-}")"
  duw_inspect_managed_media "${profile}" "${iso_path}" "${config_path}" "${use_custom_grub_menu}" "${source_role}"
  payload_layout="${DUSB_MANAGED_PAYLOAD_LAYOUT}"
  if [ "${source_role}" = "netinst" ] || [ "${source_role}" = "netboot" ]; then
    payload_layout="iso-store"
  fi
  payload_fs_label="$(duw_profile_payload_fs_label "${profile}" "${source_role}" "${DUSB_INSPECTED_MEDIA_CLASS}")"
  payload_partlabel="$(duw_profile_payload_partlabel "${profile}" "${source_role}" "${DUSB_INSPECTED_MEDIA_CLASS}")"
  payload_iso_volid="$(duw_profile_payload_iso_volid "${profile}" "${source_role}" "${DUSB_INSPECTED_MEDIA_CLASS}")"
  if [ "${payload_layout}" = "iso-store" ] && [ "${with_persistence}" = "0" ] && [ -z "${offline_preseed_source_dir}" ]; then
    duw_build_single_iso_store_usb "${profile}" "${iso_path}" "${device}" "${source_role}" "${kernel_args}" "${menu_label}" "${kernel_override}" "${initrd_override}" "${config_path}" "${live_toram_override}" "${use_custom_grub_menu}" "${preserve_upstream_grub_entries}" "${include_preseed}"
    return 0
  fi
  if duw_payload_partition_precedes_esp "${profile}" "${payload_layout}" "${DUSB_INSPECTED_MEDIA_CLASS}"; then
    payload_before_esp=1
  fi
  duw_validate_preseed_source_dir "${offline_preseed_source_dir}"
  temp_root="$(duw_create_temp_root "managed-usb")"
  duw_set_cleanup_trap "${temp_root}"
  payload_iso_path="${iso_path}"
  if [ "${payload_layout}" = "iso-store" ] && [ -n "${offline_preseed_source_dir}" ] && [ -f "${iso_path}" ]; then
    payload_iso_path="${temp_root}/payload-with-preseed.iso"
    duw_rebuild_iso_with_preseed_tree "${iso_path}" "${offline_preseed_source_dir}" "${payload_iso_path}" "${payload_iso_volid}"
  fi
  if [ "${payload_layout}" = "extracted" ]; then
    preseed_size_mib="$(duw_preseed_dir_size_mib "${offline_preseed_source_dir}")"
  else
    preseed_size_mib=0
  fi
  iso_bytes="$(duw_media_source_size_bytes "${payload_iso_path}")"
  iso_mib="$(duw_bytes_to_mib "${iso_bytes}")"
  if [ "${payload_layout}" = "raw-iso" ]; then
    required_live_mib=$((iso_mib + $(duw_raw_iso_managed_uefi_overhead_mib)))
  elif [ "${payload_layout}" = "iso-store" ]; then
    required_live_mib=$((iso_mib + 512))
  else
    required_live_mib=$((iso_mib + preseed_size_mib + 512))
  fi
  if [ "${include_preseed}" = "1" ] && [ "${payload_layout}" = "raw-iso" ]; then
    esp_preseed_mib="$(duw_profile_preseed_size_mib "${profile}" "${offline_preseed_source_dir}")"
  fi
  duw_render_managed_grub "${profile}" "${payload_iso_path}" "DUSB-PREFLIGHT-LIVE" "${with_persistence}" "${persistence_mode}" "${kernel_args}" "${menu_label}" "${kernel_override}" "${initrd_override}" "${config_path}" "DUSB-PREFLIGHT-ESP" "${live_toram_override}" "${use_custom_grub_menu}" "${preserve_upstream_grub_entries}" "${include_preseed}" "${source_role}"
  esp_size_mib="$(duw_required_esp_size_mib_from_render_payload "${DUSB_MANAGED_RENDER_OUTPUT}" "${esp_preseed_mib}")"
  device_bytes="$(blockdev --getsize64 "${device}")"
  device_mib="$(duw_bytes_to_mib "${device_bytes}")"
  persist_size_mib=$((persistence_size_gib * 1024))
  usable_end_mib=$((device_mib - 8))
  esp_partition_number=1
  live_partition_number=2
  persist_partition_number=3
  persist_start="${usable_end_mib}"
  persist_end="${usable_end_mib}"
  if [ "${payload_before_esp}" -eq "1" ]; then
    live_start=1
    live_end=$((live_start + required_live_mib))
    esp_start="${live_end}"
    esp_end=$((esp_start + esp_size_mib))
    esp_partition_number=2
    live_partition_number=1
    if [ "${with_persistence}" -eq "1" ]; then
      persist_start="${esp_end}"
      persist_end=$((persist_start + persist_size_mib))
      if [ "${persist_end}" -gt "${usable_end_mib}" ]; then
        duw_die "device ${device} does not have enough space for the raw ISO payload, the managed ESP, and ${persistence_size_gib} GiB of persistence"
      fi
    else
      if [ "${esp_end}" -gt "${usable_end_mib}" ]; then
        duw_die "device ${device} does not have enough space for the raw ISO payload and the managed ESP"
      fi
    fi
  else
    esp_start=1
    esp_end=$((esp_start + esp_size_mib))
    live_start="${esp_end}"
    if [ "${with_persistence}" -eq "1" ]; then
      persist_start=$((usable_end_mib - persist_size_mib))
      if [ "${persist_start}" -le "${live_start}" ]; then
        duw_die "device ${device} does not have enough space for the extracted ISO payload and ${persistence_size_gib} GiB of persistence"
      fi
      live_end="${persist_start}"
    else
      live_end="${usable_end_mib}"
    fi
    available_live_mib=$((live_end - live_start))
    if [ "${available_live_mib}" -lt "${required_live_mib}" ]; then
      duw_die "device ${device} does not have enough space for the extracted ISO payload; need at least ${required_live_mib} MiB for the managed payload partition"
    fi
  fi
  esp_mount="${temp_root}/esp"
  live_mount="${temp_root}/live"
  persist_mount="${temp_root}/persist"
  mkdir -p -- "${esp_mount}" "${live_mount}" "${persist_mount}"
  duw_set_cleanup_trap "${temp_root}" "${persist_mount}" "${live_mount}" "${esp_mount}"

  if [ "${with_persistence}" -eq "1" ] && [ "${persistence_mode}" = "encrypted" ]; then
    encrypted_persistence_key_file="${temp_root}/encrypted-persistence.passphrase"
    duw_step "Collecting LUKS passphrase for ${profile} encrypted persistence before partitioning starts"
    duw_collect_luks_passphrase_file "${profile} encrypted persistence" "${encrypted_persistence_key_file}"
  fi

  duw_step "Preparing managed USB layout on ${device} from ${iso_path}"
  duw_note "Target device: $(duw_device_pretty_name "${device}")"
  duw_note "Managed payload layout: ${payload_layout} (detected media class: ${DUSB_INSPECTED_MEDIA_CLASS})"
  duw_note "GRUB menu mode: $(duw_grub_menu_mode_label "${use_custom_grub_menu}" "${preserve_upstream_grub_entries}")"
  duw_note_dynamic_esp_size "${esp_size_mib}"
  if [ -n "${offline_preseed_source_dir}" ]; then
    duw_note "Offline preseed source: ${offline_preseed_source_dir} -> $(duw_preseed_target_path)"
  fi
  if [ "${payload_before_esp}" -eq "1" ]; then
    if [ "${payload_layout}" = "raw-iso" ]; then
      duw_note "Placing the raw ISO payload before the ESP so Debian-family installers detect ISO9660 media before the USB boot partition."
    else
      duw_note "Placing the managed payload before the ESP so Debian-family installer media resolves the staged ISO before the USB boot partition."
    fi
  fi
  duw_unmount_device_children "${device}"
  duw_reset_partition_table_state "${device}"
  duw_step "Partitioning ${device}"
  duw_write_gpt_label "${device}"
  if [ "${payload_before_esp}" -eq "1" ]; then
    parted -s "${device}" unit MiB mkpart "${payload_partlabel}" ext4 "${live_start}" "${live_end}"
    parted -s "${device}" unit MiB mkpart "$(duw_effective_esp_label)" fat32 "${esp_start}" "${esp_end}"
  else
    parted -s "${device}" unit MiB mkpart "$(duw_effective_esp_label)" fat32 "${esp_start}" "${esp_end}"
    parted -s "${device}" unit MiB mkpart "${payload_partlabel}" ext4 "${live_start}" "${live_end}"
  fi
  parted -s "${device}" set "${esp_partition_number}" esp on
  if [ "${with_persistence}" -eq "1" ]; then
    parted -s "${device}" unit MiB mkpart "$(duw_profile_persistence_partlabel "${profile}")" ext4 "${persist_start}" "${persist_end}"
  fi
  partprobe "${device}" || duw_die "failed to refresh the kernel partition table for ${device}"
  duw_settle_block_state

  esp_part="$(duw_wait_for_partition_device "${device}" "${esp_partition_number}")"
  live_part="$(duw_wait_for_partition_device "${device}" "${live_partition_number}")"
  persist_part=""
  if [ "${with_persistence}" -eq "1" ]; then
    persist_part="$(duw_wait_for_partition_device "${device}" "${persist_partition_number}")"
  fi

  duw_make_vfat_filesystem "${esp_part}" "$(duw_effective_esp_label)" "${device}"
  sync
  duw_wait_for_filesystem_signature "${esp_part}"
  esp_uuid="$(duw_partition_uuid "${esp_part}")"

  duw_step "Mounting working partitions"
  duw_mount_partition "${esp_part}" "${esp_mount}" vfat
  duw_prepare_secure_boot_identity
  duw_stage_secure_boot_support "${esp_mount}" "${temp_root}"
  duw_note_secure_boot_requirement
  if [ "${include_preseed}" = "1" ] && [ "${payload_layout}" = "raw-iso" ]; then
    duw_stage_single_profile_preseed_tree "${esp_mount}" "${profile}" "${offline_preseed_source_dir}"
  fi
  if [ "${with_persistence}" -eq "1" ]; then
    if [ "${persistence_mode}" = "encrypted" ]; then
      duw_configure_encrypted_persistence_partition "${persist_part}" "${persist_mount}" "debian-usb-persist" "${profile}" "$(duw_profile_persistence_fs_label "${profile}")" "${encrypted_persistence_key_file}" "${device}"
    else
      duw_configure_persistence_partition "${profile}" "$(duw_profile_persistence_fs_label "${profile}")" "${persist_part}" "${persist_mount}" "${device}"
    fi
  fi
  if [ "${payload_layout}" = "raw-iso" ]; then
    payload_write_iso="${temp_root}/payload-managed-uefi.iso"
    duw_rebuild_raw_iso_with_managed_uefi_redirect "${payload_iso_path}" "${esp_uuid}" "${payload_write_iso}" "${payload_iso_volid}" "${profile}"
    duw_require_file_fits_partition "${payload_write_iso}" "${live_part}" "managed raw ISO payload"
    duw_write_iso_payload_partition "${payload_write_iso}" "${live_part}" "${device}"
    payload_render_iso="${payload_iso_path}"
  else
    duw_make_ext4_filesystem "${live_part}" "${payload_fs_label}" "${device}"
    sync
    duw_wait_for_filesystem_signature "${live_part}"
    duw_mount_partition "${live_part}" "${live_mount}" ext4
    if [ "${include_preseed}" = "1" ]; then
      duw_stage_single_profile_preseed_tree "${live_mount}" "${profile}" "${offline_preseed_source_dir}"
    fi
    if [ "${payload_layout}" = "iso-store" ]; then
      payload_render_iso="${payload_iso_path}"
    else
      duw_extract_iso_contents "${iso_path}" "${live_mount}"
      if [ "${use_custom_grub_menu}" = "1" ]; then
        duw_strip_source_boot_configs "${live_mount}"
      fi
      payload_render_iso="${iso_path}"
    fi
  fi
  duw_wait_for_filesystem_signature "${live_part}"
  duw_step "Resolving managed payload locator"
  live_uuid="$(duw_payload_locator_for_render "${profile}" "${DUSB_INSPECTED_MEDIA_CLASS}" "${payload_layout}" "${live_part}")"

  duw_step "Rendering managed GRUB menu"
  duw_render_managed_grub "${profile}" "${payload_render_iso}" "${live_uuid}" "${with_persistence}" "${persistence_mode}" "${kernel_args}" "${menu_label}" "${kernel_override}" "${initrd_override}" "${config_path}" "${esp_uuid}" "${live_toram_override}" "${use_custom_grub_menu}" "${preserve_upstream_grub_entries}" "${include_preseed}" "${source_role}"
  duw_stage_secure_boot_assets_from_render_payload "${DUSB_MANAGED_RENDER_OUTPUT}" "${temp_root}" "${esp_mount}"
  if [ "${payload_layout}" = "iso-store" ]; then
    duw_stage_iso_store_from_render_payload "${DUSB_MANAGED_RENDER_OUTPUT}" "${live_mount}" "${live_mount}" "${payload_render_iso}"
  fi
  duw_note "Rendered managed ${DUSB_MANAGED_MEDIA_CLASS} GRUB menu with ${DUSB_MANAGED_ENTRY_COUNT} entries"

  duw_install_grub_bootloader "${esp_part}" "${esp_mount}"

  boot_cfg="${esp_mount}/boot/grub/grub.cfg"
  duw_require_mount_source "${esp_part}" "${esp_mount}"
  duw_step "Writing GRUB configuration to ${esp_part}:/boot/grub/grub.cfg (mounted at ${esp_mount})"
  mkdir -p -- "$(dirname -- "${boot_cfg}")"
  printf '%s' "${DUSB_MANAGED_GRUB_CFG}" >"${boot_cfg}"
  duw_sign_grub_data_file "${boot_cfg}"
  duw_stage_secure_boot_manifest "${esp_mount}" "${boot_cfg}" "ESP:/boot/grub/grub.cfg"
  duw_step "Finalizing USB writes"
  sync
  duw_finalize_temp_root "${temp_root}"
  duw_step "Managed USB creation finished for ${device}"
)

duw_build_multios_usb() (
  plan_path=$1
  device=$2
  config_path=$3
  esp_start=1
  esp_preseed_mib=0
  esp_after_payloads=0
  esp_partition_number=1
  direct_secure_boot_assets_required=0

  duw_validate_multios_plan "${plan_path}"
  # This workflow intentionally keeps Secure Boot state inside its function subshell.
  # shellcheck disable=SC2030
  DUSB_SECURE_BOOT_TRUST=$(duw_effective_secure_boot_trust "$(duw_multios_secure_boot_trust "${plan_path}")")
  esp_label=$(duw_multios_esp_label "${plan_path}")
  use_custom_grub_menu=$(duw_multios_use_custom_grub_menu "${plan_path}")
  preserve_upstream_grub_entries=$(duw_multios_preserve_upstream_grub_entries "${plan_path}")
  temp_root=$(duw_create_temp_root multios-usb)
  duw_set_cleanup_trap "${temp_root}"
  state_dir=${temp_root}/plan-state
  duw_multios_state_create "${plan_path}" "${state_dir}"
  item_count=$(duw_multios_state_count "${state_dir}")
  [ "${item_count}" -ge 1 ] || duw_die "Multi-OS requires at least one OS item"

  esp_size_mib=$(duw_esp_min_size_mib)
  esp_end=$((esp_start + esp_size_mib))
  total_required_mib=$((esp_size_mib + 8))
  index=0
  while [ "${index}" -lt "${item_count}" ]; do
    id=$(duw_multios_state_get "${state_dir}" "${index}" id)
    profile=$(duw_multios_state_get "${state_dir}" "${index}" profile)
    source_role=$(duw_multios_state_get "${state_dir}" "${index}" source_role)
    iso_path=$(duw_multios_state_get "${state_dir}" "${index}" iso_path)
    persist_flag=$(duw_multios_state_get "${state_dir}" "${index}" persist_flag)
    persist_size=$(duw_multios_state_get "${state_dir}" "${index}" persist_size)
    offline_preseed_dir=$(duw_multios_state_get "${state_dir}" "${index}" offline_preseed_dir)
    [ -f "${iso_path}" ] && [ -s "${iso_path}" ] || duw_die "ISO file does not exist or is empty: ${iso_path}"
    duw_validate_preseed_source_dir "${offline_preseed_dir}"

    # Non-shared Multi-OS media uses one opaque ISO partition per item. Installer
    # partitions are ordered before Live partitions so media scans cannot bind
    # an installer initrd to a sibling Live ISO.
    layout=raw-iso
    payload_write_iso=${iso_path}
    duw_inspect_managed_media "${profile}" "${payload_write_iso}" "${config_path}" "${use_custom_grub_menu}" "${source_role}"
    payload_write_iso=$(duw_prepare_multios_live_tools_iso \
      "${profile}" \
      "${source_role}" \
      "${DUSB_INSPECTED_MEDIA_CLASS}" \
      "${payload_write_iso}" \
      "${temp_root}/${id}-live-tools")
    if [ "${payload_write_iso}" != "${iso_path}" ]; then
      duw_inspect_managed_media "${profile}" "${payload_write_iso}" "${config_path}" "${use_custom_grub_menu}" "${source_role}"
    fi
    media_class=${DUSB_INSPECTED_MEDIA_CLASS}
    iso_bytes=$(stat -c '%s' "${payload_write_iso}")
    iso_mib=$(duw_bytes_to_mib "${iso_bytes}")
    required_mib=$((iso_mib + $(duw_raw_iso_managed_uefi_overhead_mib)))
    esp_after_payloads=1
    if [ "${use_custom_grub_menu}" = 1 ]; then
      case ${profile} in
        debian|kali-linux)
          profile_preseed_mib=$(duw_profile_preseed_size_mib "${profile}" "${offline_preseed_dir}")
          esp_preseed_mib=$((esp_preseed_mib + profile_preseed_mib))
          ;;
      esac
    fi

    duw_multios_state_set "${state_dir}" "${index}" layout "${layout}"
    duw_multios_state_set "${state_dir}" "${index}" media_class "${media_class}"
    duw_multios_state_set "${state_dir}" "${index}" iso_path "${payload_write_iso}"
    duw_multios_state_set "${state_dir}" "${index}" payload_write_iso "${payload_write_iso}"
    duw_multios_state_set "${state_dir}" "${index}" payload_required_mib "${required_mib}"
    total_required_mib=$((total_required_mib + required_mib))
    if [ "${persist_flag}" = 1 ]; then
      case ${persist_size} in ''|*[!0-9]*) duw_die "persistence size must be a positive integer" ;; esac
      [ "${persist_size}" -gt 0 ] || duw_die "persistence size must be positive"
      total_required_mib=$((total_required_mib + persist_size * 1024))
    fi
    index=$((index + 1))
  done

  render_plan_path=${temp_root}/effective-multios-plan.json
  duw_multios_write_effective_plan_from_state "${plan_path}" "${render_plan_path}" "${state_dir}"
  set -- "${render_plan_path}" "${config_path}" DUSB-PREFLIGHT-ESP
  index=0
  while [ "${index}" -lt "${item_count}" ]; do
    id=$(duw_multios_state_get "${state_dir}" "${index}" id)
    set -- "$@" "${id}=DUSB-PREFLIGHT-${index}"
    index=$((index + 1))
  done
  duw_render_multios_grub "$@"
  if duw_render_payload_needs_direct_secure_boot_assets "${DUSB_MANAGED_RENDER_OUTPUT}"; then
    direct_secure_boot_assets_required=1
  fi
  esp_size_mib=$(duw_required_esp_size_mib_from_render_payload "${DUSB_MANAGED_RENDER_OUTPUT}" "${esp_preseed_mib}")
  total_required_mib=$((total_required_mib - $(duw_esp_min_size_mib) + esp_size_mib))
  esp_end=$((esp_start + esp_size_mib))

  device_bytes=$(blockdev --getsize64 "${device}")
  device_mib=$(duw_bytes_to_mib "${device_bytes}")
  usable_end_mib=$((device_mib - 8))
  [ "${total_required_mib}" -le "${device_mib}" ] || duw_die "device ${device} does not have enough space for Multi-OS payloads and persistence; need at least ${total_required_mib} MiB"

  if [ "${esp_after_payloads}" -eq 1 ]; then cursor=1; else cursor=${esp_end}; fi
  for order_class in installer other; do
    index=0
    while [ "${index}" -lt "${item_count}" ]; do
      source_role=$(duw_multios_state_get "${state_dir}" "${index}" source_role)
      media_class=$(duw_multios_state_get "${state_dir}" "${index}" media_class)
      installer_item=0
      if [ "${source_role}" = netinst ] || [ "${media_class}" = installer ]; then installer_item=1; fi
      if { [ "${order_class}" = installer ] && [ "${installer_item}" -eq 1 ]; } || \
         { [ "${order_class}" = other ] && [ "${installer_item}" -eq 0 ]; }; then
        required_mib=$(duw_multios_state_get "${state_dir}" "${index}" payload_required_mib)
        payload_start=${cursor}
        payload_end=$((cursor + required_mib))
        duw_multios_state_set "${state_dir}" "${index}" payload_start "${payload_start}"
        duw_multios_state_set "${state_dir}" "${index}" payload_end "${payload_end}"
        cursor=${payload_end}
      fi
      index=$((index + 1))
    done
  done
  if [ "${esp_after_payloads}" -eq 1 ]; then
    esp_start=${cursor}
    esp_end=$((esp_start + esp_size_mib))
    cursor=${esp_end}
  fi
  index=0
  while [ "${index}" -lt "${item_count}" ]; do
    persist_flag=$(duw_multios_state_get "${state_dir}" "${index}" persist_flag)
    if [ "${persist_flag}" = 1 ]; then
      persist_size=$(duw_multios_state_get "${state_dir}" "${index}" persist_size)
      persist_start=${cursor}
      persist_end=$((cursor + persist_size * 1024))
      cursor=${persist_end}
    else
      persist_start=
      persist_end=
    fi
    duw_multios_state_set "${state_dir}" "${index}" persist_start "${persist_start}"
    duw_multios_state_set "${state_dir}" "${index}" persist_end "${persist_end}"
    duw_multios_state_set "${state_dir}" "${index}" persist_part ''
    duw_multios_state_set "${state_dir}" "${index}" persist_key_file ''
    index=$((index + 1))
  done
  [ "${cursor}" -le "${usable_end_mib}" ] || duw_die "device ${device} does not have enough usable space for the computed Multi-OS layout"

  esp_mount=${temp_root}/esp
  mkdir -p -- "${esp_mount}"
  duw_set_cleanup_trap "${temp_root}" "${esp_mount}"
  index=0
  while [ "${index}" -lt "${item_count}" ]; do
    persist_flag=$(duw_multios_state_get "${state_dir}" "${index}" persist_flag)
    persist_mode=$(duw_multios_state_get "${state_dir}" "${index}" persist_mode)
    if [ "${persist_flag}" = 1 ] && [ "${persist_mode}" = encrypted ]; then
      title=$(duw_multios_state_get "${state_dir}" "${index}" title)
      persist_key_file=${temp_root}/persist-${index}.passphrase
      duw_multios_state_set "${state_dir}" "${index}" persist_key_file "${persist_key_file}"
      duw_step "Collecting LUKS passphrase for ${title} encrypted persistence before partitioning starts"
      duw_collect_luks_passphrase_file "${title} encrypted persistence" "${persist_key_file}"
    fi
    index=$((index + 1))
  done

  duw_step "Preparing Multi-OS managed USB layout on ${device}"
  duw_note "Target device: $(duw_device_pretty_name "${device}")"
  duw_note "GRUB menu mode: $(duw_grub_menu_mode_label "${use_custom_grub_menu}" "${preserve_upstream_grub_entries}")"
  duw_note_dynamic_esp_size "${esp_size_mib}"
  if [ "${esp_after_payloads}" -eq 1 ]; then
    duw_note "Placing netinst/installer raw ISO payload partitions before live payloads and before the ESP so Debian-family media scans cannot select a live ISO first."
  fi
  duw_unmount_device_children "${device}"
  duw_reset_partition_table_state "${device}"
  duw_step "Partitioning ${device}"
  duw_write_gpt_label "${device}"
  if [ "${esp_after_payloads}" -eq 1 ]; then
    partition_number=1
    for order_class in installer other; do
      index=0
      while [ "${index}" -lt "${item_count}" ]; do
        source_role=$(duw_multios_state_get "${state_dir}" "${index}" source_role)
        media_class=$(duw_multios_state_get "${state_dir}" "${index}" media_class)
        installer_item=0
        if [ "${source_role}" = netinst ] || [ "${media_class}" = installer ]; then installer_item=1; fi
        if { [ "${order_class}" = installer ] && [ "${installer_item}" -eq 1 ]; } || \
           { [ "${order_class}" = other ] && [ "${installer_item}" -eq 0 ]; }; then
          payload_partlabel=$(duw_multios_state_get "${state_dir}" "${index}" payload_partlabel)
          payload_start=$(duw_multios_state_get "${state_dir}" "${index}" payload_start)
          payload_end=$(duw_multios_state_get "${state_dir}" "${index}" payload_end)
          parted -s "${device}" unit MiB mkpart "${payload_partlabel}" ext4 "${payload_start}" "${payload_end}"
          payload_part=$(duw_partition_path "${device}" "${partition_number}")
          duw_multios_state_set "${state_dir}" "${index}" payload_part "${payload_part}"
          partition_number=$((partition_number + 1))
        fi
        index=$((index + 1))
      done
    done
    esp_partition_number=${partition_number}
    parted -s "${device}" unit MiB mkpart "${esp_label}" fat32 "${esp_start}" "${esp_end}"
    partition_number=$((partition_number + 1))
  else
    esp_partition_number=1
    parted -s "${device}" unit MiB mkpart "${esp_label}" fat32 "${esp_start}" "${esp_end}"
    partition_number=2
    for order_class in installer other; do
      index=0
      while [ "${index}" -lt "${item_count}" ]; do
        source_role=$(duw_multios_state_get "${state_dir}" "${index}" source_role)
        media_class=$(duw_multios_state_get "${state_dir}" "${index}" media_class)
        installer_item=0
        if [ "${source_role}" = netinst ] || [ "${media_class}" = installer ]; then installer_item=1; fi
        if { [ "${order_class}" = installer ] && [ "${installer_item}" -eq 1 ]; } || \
           { [ "${order_class}" = other ] && [ "${installer_item}" -eq 0 ]; }; then
          payload_partlabel=$(duw_multios_state_get "${state_dir}" "${index}" payload_partlabel)
          payload_start=$(duw_multios_state_get "${state_dir}" "${index}" payload_start)
          payload_end=$(duw_multios_state_get "${state_dir}" "${index}" payload_end)
          parted -s "${device}" unit MiB mkpart "${payload_partlabel}" ext4 "${payload_start}" "${payload_end}"
          payload_part=$(duw_partition_path "${device}" "${partition_number}")
          duw_multios_state_set "${state_dir}" "${index}" payload_part "${payload_part}"
          partition_number=$((partition_number + 1))
        fi
        index=$((index + 1))
      done
    done
  fi
  parted -s "${device}" set "${esp_partition_number}" esp on
  index=0
  while [ "${index}" -lt "${item_count}" ]; do
    persist_flag=$(duw_multios_state_get "${state_dir}" "${index}" persist_flag)
    if [ "${persist_flag}" = 1 ]; then
      persist_partlabel=$(duw_multios_state_get "${state_dir}" "${index}" persist_partlabel)
      persist_start=$(duw_multios_state_get "${state_dir}" "${index}" persist_start)
      persist_end=$(duw_multios_state_get "${state_dir}" "${index}" persist_end)
      parted -s "${device}" unit MiB mkpart "${persist_partlabel}" ext4 "${persist_start}" "${persist_end}"
      persist_part=$(duw_partition_path "${device}" "${partition_number}")
      duw_multios_state_set "${state_dir}" "${index}" persist_part "${persist_part}"
      partition_number=$((partition_number + 1))
    fi
    index=$((index + 1))
  done
  partprobe "${device}" || duw_die "failed to refresh the kernel partition table for ${device}"
  duw_settle_block_state

  esp_part=$(duw_wait_for_partition_device "${device}" "${esp_partition_number}")
  index=0
  while [ "${index}" -lt "${item_count}" ]; do
    payload_part=$(duw_multios_state_get "${state_dir}" "${index}" payload_part)
    payload_part=$(duw_wait_for_partition_device "${device}" "$(duw_partition_number_from_path "${payload_part}")")
    duw_multios_state_set "${state_dir}" "${index}" payload_part "${payload_part}"
    persist_part=$(duw_multios_state_get "${state_dir}" "${index}" persist_part)
    if [ -n "${persist_part}" ]; then
      persist_part=$(duw_wait_for_partition_device "${device}" "$(duw_partition_number_from_path "${persist_part}")")
      duw_multios_state_set "${state_dir}" "${index}" persist_part "${persist_part}"
    fi
    index=$((index + 1))
  done

  duw_make_vfat_filesystem "${esp_part}" "${esp_label}" "${device}"
  sync
  duw_wait_for_filesystem_signature "${esp_part}"
  esp_uuid=$(duw_partition_uuid "${esp_part}")
  duw_mount_partition "${esp_part}" "${esp_mount}" vfat
  duw_prepare_secure_boot_identity
  duw_stage_secure_boot_support "${esp_mount}" "${temp_root}"
  if [ "${direct_secure_boot_assets_required}" -eq 1 ]; then
    duw_note_secure_boot_requirement
  else
    duw_note "Secure Boot: custom GRUB remains the front menu, and the MOK enrollment entry is staged for the owner certificate fallback."
  fi
  if [ "${use_custom_grub_menu}" = 1 ]; then
    duw_multios_stage_selected_preseed_from_state "${esp_mount}" "${state_dir}"
  fi

  index=0
  while [ "${index}" -lt "${item_count}" ]; do
    id=$(duw_multios_state_get "${state_dir}" "${index}" id)
    profile=$(duw_multios_state_get "${state_dir}" "${index}" profile)
    persist_flag=$(duw_multios_state_get "${state_dir}" "${index}" persist_flag)
    persist_mode=$(duw_multios_state_get "${state_dir}" "${index}" persist_mode)
    persist_part=$(duw_multios_state_get "${state_dir}" "${index}" persist_part)
    persist_fs_label=$(duw_multios_state_get "${state_dir}" "${index}" persist_fs_label)
    persist_key_file=$(duw_multios_state_get "${state_dir}" "${index}" persist_key_file)
    payload_mount=${temp_root}/payload-${index}
    persist_mount=${temp_root}/persist-${index}
    mkdir -p -- "${payload_mount}" "${persist_mount}"
    if [ "${persist_flag}" = 1 ]; then
      if [ "${persist_mode}" = encrypted ]; then
        duw_configure_encrypted_persistence_partition "${persist_part}" "${persist_mount}" "debian-usb-persist-${id}" "${profile}" "${persist_fs_label}" "${persist_key_file}" "${device}"
      else
        duw_configure_persistence_partition "${profile}" "${persist_fs_label}" "${persist_part}" "${persist_mount}" "${device}"
      fi
    fi
    index=$((index + 1))
  done

  index=0
  while [ "${index}" -lt "${item_count}" ]; do
    id=$(duw_multios_state_get "${state_dir}" "${index}" id)
    profile=$(duw_multios_state_get "${state_dir}" "${index}" profile)
    source_role=$(duw_multios_state_get "${state_dir}" "${index}" source_role)
    title=$(duw_multios_state_get "${state_dir}" "${index}" title)
    iso_path=$(duw_multios_state_get "${state_dir}" "${index}" iso_path)
    layout=$(duw_multios_state_get "${state_dir}" "${index}" layout)
    media_class=$(duw_multios_state_get "${state_dir}" "${index}" media_class)
    payload_part=$(duw_multios_state_get "${state_dir}" "${index}" payload_part)
    payload_fs_label=$(duw_multios_state_get "${state_dir}" "${index}" payload_fs_label)
    offline_preseed_dir=$(duw_multios_state_get "${state_dir}" "${index}" offline_preseed_dir)
    payload_mount=${temp_root}/payload-${index}
    duw_step "Writing ${title} payload"
    if [ -n "${offline_preseed_dir}" ]; then
      duw_note "${title} offline preseed source: ${offline_preseed_dir} -> $(duw_preseed_target_path)"
    fi
    if [ "${layout}" = raw-iso ]; then
      payload_write_iso=${temp_root}/${id}-managed-uefi.iso
      duw_rebuild_raw_iso_with_managed_uefi_redirect \
        "${iso_path}" \
        "${esp_uuid}" \
        "${payload_write_iso}" \
        "$(duw_profile_payload_iso_volid "${profile}" "${source_role}" "${media_class}")" \
        "${profile}"
      duw_multios_state_set "${state_dir}" "${index}" payload_write_iso "${payload_write_iso}"
      duw_require_file_fits_partition "${payload_write_iso}" "${payload_part}" "${title} managed raw ISO payload"
      duw_write_iso_payload_partition "${payload_write_iso}" "${payload_part}" "${device}"
    else
      duw_make_ext4_filesystem "${payload_part}" "${payload_fs_label}" "${device}"
      sync
      duw_wait_for_filesystem_signature "${payload_part}"
      if [ "${layout}" != iso-store ]; then
        duw_mount_partition "${payload_part}" "${payload_mount}" ext4
        duw_extract_iso_contents "${iso_path}" "${payload_mount}"
        if [ "${use_custom_grub_menu}" = 1 ]; then duw_strip_source_boot_configs "${payload_mount}"; fi
        duw_stage_preseed_tree "${payload_mount}" "${offline_preseed_dir}"
        umount -- "${payload_mount}"
      fi
    fi
    duw_wait_for_filesystem_signature "${payload_part}"
    payload_uuid=$(duw_payload_locator_for_render "${profile}" "${media_class}" "${layout}" "${payload_part}")
    duw_multios_state_set "${state_dir}" "${index}" payload_uuid "${payload_uuid}"
    index=$((index + 1))
  done

  duw_multios_write_effective_plan_from_state "${plan_path}" "${render_plan_path}" "${state_dir}"
  set -- "${render_plan_path}" "${config_path}" "${esp_uuid}"
  index=0
  while [ "${index}" -lt "${item_count}" ]; do
    id=$(duw_multios_state_get "${state_dir}" "${index}" id)
    payload_uuid=$(duw_multios_state_get "${state_dir}" "${index}" payload_uuid)
    set -- "$@" "${id}=${payload_uuid}"
    index=$((index + 1))
  done
  duw_step "Rendering Multi-OS GRUB menu"
  duw_render_multios_grub "$@"
  if duw_render_payload_needs_direct_secure_boot_assets "${DUSB_MANAGED_RENDER_OUTPUT}"; then
    if [ "${direct_secure_boot_assets_required}" -ne 1 ]; then
      duw_prepare_secure_boot_identity
      duw_stage_secure_boot_support "${esp_mount}" "${temp_root}"
      duw_note_secure_boot_requirement
      direct_secure_boot_assets_required=1
    fi
    duw_stage_secure_boot_assets_from_render_payload "${DUSB_MANAGED_RENDER_OUTPUT}" "${temp_root}" "${esp_mount}"
  fi
  index=0
  while [ "${index}" -lt "${item_count}" ]; do
    layout=$(duw_multios_state_get "${state_dir}" "${index}" layout)
    if [ "${layout}" = iso-store ]; then
      payload_part=$(duw_multios_state_get "${state_dir}" "${index}" payload_part)
      payload_write_iso=$(duw_multios_state_get "${state_dir}" "${index}" payload_write_iso)
      payload_mount=${temp_root}/payload-${index}
      duw_mount_partition "${payload_part}" "${payload_mount}" ext4
      duw_stage_iso_store_from_render_payload "${DUSB_MANAGED_RENDER_OUTPUT}" "${payload_mount}" "${payload_mount}" "${payload_write_iso}"
      umount -- "${payload_mount}"
    fi
    index=$((index + 1))
  done
  duw_note "Rendered ${DUSB_MANAGED_MEDIA_CLASS} GRUB menu with ${DUSB_MANAGED_ENTRY_COUNT} entries"
  duw_install_grub_bootloader "${esp_part}" "${esp_mount}"
  boot_cfg=${esp_mount}/boot/grub/grub.cfg
  duw_require_mount_source "${esp_part}" "${esp_mount}"
  duw_step "Writing GRUB configuration to ${esp_part}:/boot/grub/grub.cfg (mounted at ${esp_mount})"
  mkdir -p -- "$(dirname -- "${boot_cfg}")"
  printf '%s' "${DUSB_MANAGED_GRUB_CFG}" >"${boot_cfg}"
  duw_sign_grub_data_file "${boot_cfg}"
  duw_stage_secure_boot_manifest "${esp_mount}" "${boot_cfg}" ESP:/boot/grub/grub.cfg
  duw_step "Finalizing USB writes"
  sync
  duw_finalize_temp_root "${temp_root}"
  duw_step "Multi-OS USB creation finished for ${device}"
)

duw_update_managed_usb() (
  profile="$1"
  iso_path="$2"
  device="$3"
  with_persistence="$4"
  persistence_mode="$5"
  persistence_size_gib="$6"
  kernel_args="$7"
  menu_label="$8"
  kernel_override="$9"
  initrd_override="${10}"
  config_path="${11}"
  live_toram_override="${12}"
  use_custom_grub_menu="${13}"
  preserve_upstream_grub_entries="${14:-0}"
  offline_preseed_source_dir="${15:-}"
  include_preseed="${16:-0}"
  payload_render_iso="${iso_path}"

  # The caller supplies independent Secure Boot state to this function subshell.
  # shellcheck disable=SC2031
  DUSB_SECURE_BOOT_TRUST="$(duw_effective_secure_boot_trust "${DUSB_SECURE_BOOT_TRUST:-}")"
  duw_inspect_managed_media "${profile}" "${iso_path}" "${config_path}" "${use_custom_grub_menu}"
  payload_layout="${DUSB_MANAGED_PAYLOAD_LAYOUT}"
  payload_fs_label="$(duw_profile_payload_fs_label "${profile}" "primary" "${DUSB_INSPECTED_MEDIA_CLASS}")"
  payload_partlabel="$(duw_profile_payload_partlabel "${profile}" "primary" "${DUSB_INSPECTED_MEDIA_CLASS}")"

  duw_step "Updating managed USB assets on ${device} from ${iso_path}"
  duw_note "Target device: $(duw_device_pretty_name "${device}")"
  duw_note "Managed payload layout: ${payload_layout} (detected media class: ${DUSB_INSPECTED_MEDIA_CLASS})"
  duw_note "GRUB menu mode: $(duw_grub_menu_mode_label "${use_custom_grub_menu}" "${preserve_upstream_grub_entries}")"
  duw_unmount_device_children "${device}"

  temp_root="$(duw_create_temp_root "managed-usb-update")"
  esp_mount="${temp_root}/esp"
  payload_mount="${temp_root}/payload"
  mkdir -p -- "${esp_mount}" "${payload_mount}"
  duw_set_cleanup_trap "${temp_root}" "${payload_mount}" "${esp_mount}"

  esp_part="$(duw_device_partition_by_label "${device}" "$(duw_effective_esp_label)")"
  duw_mount_partition "${esp_part}" "${esp_mount}" vfat
  duw_prepare_secure_boot_identity
  duw_stage_secure_boot_support "${esp_mount}" "${temp_root}"
  duw_note_secure_boot_requirement
  esp_uuid="$(duw_partition_uuid "${esp_part}")"

  if singleUsesSharedISOStoreLayoutPlaceholder "${payload_layout}" "${with_persistence}" "${offline_preseed_source_dir}"; then
    payload_part="$(duw_device_partition_by_label "${device}" "${payload_fs_label}")"
    duw_mount_partition "${payload_part}" "${payload_mount}" ext4
    if [ "${include_preseed}" = "1" ]; then
      duw_stage_single_profile_preseed_tree "${payload_mount}" "${profile}" "${offline_preseed_source_dir}"
    fi
    payload_locator="$(duw_partition_uuid "${payload_part}")"
    duw_render_managed_grub "${profile}" "${payload_render_iso}" "${payload_locator}" "${with_persistence}" "${persistence_mode}" "${kernel_args}" "${menu_label}" "${kernel_override}" "${initrd_override}" "${config_path}" "${esp_uuid}" "${live_toram_override}" "${use_custom_grub_menu}" "${preserve_upstream_grub_entries}" "${include_preseed}"
    duw_stage_secure_boot_assets_from_render_payload "${DUSB_MANAGED_RENDER_OUTPUT}" "${temp_root}" "${esp_mount}"
    duw_stage_iso_store_from_render_payload "${DUSB_MANAGED_RENDER_OUTPUT}" "${payload_mount}" "${payload_mount}" "${payload_render_iso}"
    duw_install_multiboot_grub_bootloaders "${device}" "${payload_part}" "${payload_mount}" "${esp_part}" "${esp_mount}"
    boot_cfg="${payload_mount}/boot/grub/grub.cfg"
    manifest_label="${payload_fs_label}:/boot/grub/grub.cfg"
  else
    if [ "${payload_layout}" = "raw-iso" ]; then
      payload_part="$(duw_device_partition_by_partlabel "${device}" "${payload_partlabel}")"
      if [ "${include_preseed}" = "1" ]; then
        duw_stage_single_profile_preseed_tree "${esp_mount}" "${profile}" "${offline_preseed_source_dir}"
      fi
    else
      payload_part="$(duw_device_partition_by_label "${device}" "${payload_fs_label}")"
      duw_mount_partition "${payload_part}" "${payload_mount}" ext4
      if [ "${include_preseed}" = "1" ]; then
        duw_stage_single_profile_preseed_tree "${payload_mount}" "${profile}" "${offline_preseed_source_dir}"
      fi
    fi
    payload_locator="$(duw_payload_locator_for_render "${profile}" "${DUSB_INSPECTED_MEDIA_CLASS}" "${payload_layout}" "${payload_part}")"
    duw_render_managed_grub "${profile}" "${payload_render_iso}" "${payload_locator}" "${with_persistence}" "${persistence_mode}" "${kernel_args}" "${menu_label}" "${kernel_override}" "${initrd_override}" "${config_path}" "${esp_uuid}" "${live_toram_override}" "${use_custom_grub_menu}" "${preserve_upstream_grub_entries}" "${include_preseed}"
    duw_stage_secure_boot_assets_from_render_payload "${DUSB_MANAGED_RENDER_OUTPUT}" "${temp_root}" "${esp_mount}"
    if [ "${payload_layout}" = "iso-store" ]; then
      duw_stage_iso_store_from_render_payload "${DUSB_MANAGED_RENDER_OUTPUT}" "${payload_mount}" "${payload_mount}" "${payload_render_iso}"
    fi
    duw_install_grub_bootloader "${esp_part}" "${esp_mount}"
    boot_cfg="${esp_mount}/boot/grub/grub.cfg"
    manifest_label="ESP:/boot/grub/grub.cfg"
  fi

  duw_note "Rendered updated managed ${DUSB_MANAGED_MEDIA_CLASS} GRUB menu with ${DUSB_MANAGED_ENTRY_COUNT} entries"
  duw_step "Writing GRUB configuration update"
  mkdir -p -- "$(dirname -- "${boot_cfg}")"
  printf '%s' "${DUSB_MANAGED_GRUB_CFG}" >"${boot_cfg}"
  duw_sign_grub_data_file "${boot_cfg}"
  duw_stage_secure_boot_manifest "${esp_mount}" "${boot_cfg}" "${manifest_label}"
  sync
  duw_finalize_temp_root "${temp_root}"
  duw_step "Managed USB update finished for ${device}"
)

singleUsesSharedISOStoreLayoutPlaceholder() (
  payload_layout="$1"
  with_persistence="$2"
  offline_preseed_source_dir="${3:-}"
  [ "${payload_layout}" = "iso-store" ] && [ "${with_persistence}" = "0" ] && [ -z "${offline_preseed_source_dir}" ]
)

duw_update_multios_shared_data_usb() (
  plan_path=$1
  device=$2
  config_path=$3
  direct_secure_boot_assets_required=0

  duw_validate_multios_plan "${plan_path}"
  DUSB_SECURE_BOOT_TRUST=$(duw_effective_secure_boot_trust "$(duw_multios_secure_boot_trust "${plan_path}")")
  use_custom_grub_menu=$(duw_multios_use_custom_grub_menu "${plan_path}")
  preserve_upstream_grub_entries=$(duw_multios_preserve_upstream_grub_entries "${plan_path}")
  temp_root=$(duw_create_temp_root multios-shared-data-update)
  duw_set_cleanup_trap "${temp_root}"
  state_dir=${temp_root}/plan-state
  duw_multios_state_create "${plan_path}" "${state_dir}"
  item_count=$(duw_multios_state_count "${state_dir}")
  [ "${item_count}" -ge 1 ] || duw_die "Multi-OS requires at least one OS item"

  index=0
  while [ "${index}" -lt "${item_count}" ]; do
    profile=$(duw_multios_state_get "${state_dir}" "${index}" profile)
    source_role=$(duw_multios_state_get "${state_dir}" "${index}" source_role)
    title=$(duw_multios_state_get "${state_dir}" "${index}" title)
    iso_path=$(duw_multios_state_get "${state_dir}" "${index}" iso_path)
    layout=$(duw_multios_state_get "${state_dir}" "${index}" layout)
    offline_preseed_dir=$(duw_multios_state_get "${state_dir}" "${index}" offline_preseed_dir)
    if [ "${source_role}" = netboot ]; then
      [ -d "${iso_path}" ] || duw_die "netboot source must be a prepared source directory: ${iso_path}"
    else
      [ -e "${iso_path}" ] || duw_die "media source does not exist: ${iso_path}"
    fi
    [ -z "${offline_preseed_dir}" ] || duw_die "per-plan offline preseed directories are replaced by configured PRESEED_HOST_*_PATH staging"
    layout=$(duw_effective_payload_layout "${profile}" "${layout}" "${config_path}" "${use_custom_grub_menu}")
    [ "${layout}" = shared-data ] || duw_die "shared ISO-store Multi-OS update requires shared-data plan payloads, got ${layout} for ${title}"
    duw_multios_state_set "${state_dir}" "${index}" layout "${layout}"
    duw_inspect_managed_media "${profile}" "${iso_path}" "${config_path}" "${use_custom_grub_menu}" "${source_role}"
    duw_multios_state_set "${state_dir}" "${index}" media_class "${DUSB_INSPECTED_MEDIA_CLASS}"
    index=$((index + 1))
  done

  esp_mount=${temp_root}/esp
  data_mount=${temp_root}/multiboot
  mkdir -p -- "${esp_mount}" "${data_mount}"
  duw_set_cleanup_trap "${temp_root}" "${esp_mount}" "${data_mount}"

  duw_unmount_device_children "${device}"
  esp_part=$(duw_wait_for_partition_device "${device}" 1)
  data_part=$(duw_wait_for_partition_device "${device}" 2)
  duw_mount_partition "${esp_part}" "${esp_mount}" vfat
  duw_mount_partition "${data_part}" "${data_mount}" ext4
  duw_remove_shared_store_media_metadata "${data_mount}"
  duw_prepare_secure_boot_identity
  duw_stage_secure_boot_support "${esp_mount}" "${temp_root}"
  duw_note_secure_boot_requirement
  esp_uuid=$(duw_partition_uuid "${esp_part}")
  data_uuid=$(duw_partition_uuid "${data_part}")
  duw_multios_stage_default_preseed_from_state "${data_mount}" "${state_dir}"

  for root_path in /debian-live /debian-netinst /debian-netboot /kali-live /kali-netinst /kali-netboot /kali-purple-installer /tails-live /live; do
    rm -rf -- "${data_mount}${root_path}" || duw_die "failed to replace ISO-store payload root: ${data_mount}${root_path}"
  done
  rm -rf -- "${esp_mount}/EFI/debian-usb/assets" || duw_die "failed to reset staged Secure Boot asset namespace"

  render_plan_path=${temp_root}/effective-multios-plan.json
  duw_multios_write_effective_plan_from_state "${plan_path}" "${render_plan_path}" "${state_dir}"
  set -- "${render_plan_path}" "${config_path}" "${esp_uuid}"
  index=0
  while [ "${index}" -lt "${item_count}" ]; do
    id=$(duw_multios_state_get "${state_dir}" "${index}" id)
    set -- "$@" "${id}=${data_uuid}"
    index=$((index + 1))
  done
  duw_render_multios_grub "$@"
  duw_stage_secure_boot_assets_from_render_payload "${DUSB_MANAGED_RENDER_OUTPUT}" "${temp_root}" "${esp_mount}"
  duw_stage_iso_store_from_render_payload "${DUSB_MANAGED_RENDER_OUTPUT}" "${data_mount}" "${data_mount}"
  duw_remove_shared_store_media_metadata "${data_mount}"
  if duw_render_payload_needs_direct_secure_boot_assets "${DUSB_MANAGED_RENDER_OUTPUT}"; then
    direct_secure_boot_assets_required=1
  fi
  duw_note "Rendered updated ${DUSB_MANAGED_MEDIA_CLASS} shared ISO-store Multi-OS GRUB menu with ${DUSB_MANAGED_ENTRY_COUNT} entries"
  if [ "${direct_secure_boot_assets_required}" -eq 0 ]; then
    duw_note "Secure Boot: custom GRUB remains the front menu, and the MOK enrollment entry stays staged."
  fi

  duw_install_grub_bootloader "${esp_part}" "${esp_mount}"
  boot_cfg=${esp_mount}/boot/grub/grub.cfg
  mkdir -p -- "$(dirname -- "${boot_cfg}")"
  printf '%s' "${DUSB_MANAGED_GRUB_CFG}" >"${boot_cfg}"
  duw_sign_grub_data_file "${boot_cfg}"
  duw_stage_secure_boot_manifest "${esp_mount}" "${boot_cfg}" ESP:/boot/grub/grub.cfg
  sync
  duw_finalize_temp_root "${temp_root}"
  duw_step "Shared-data Multi-OS USB update finished for ${device}"
)

duw_update_multios_usb() (
  plan_path=$1
  device=$2
  config_path=$3
  direct_secure_boot_assets_required=0

  duw_validate_multios_plan "${plan_path}"
  DUSB_SECURE_BOOT_TRUST=$(duw_effective_secure_boot_trust "$(duw_multios_secure_boot_trust "${plan_path}")")
  esp_label=$(duw_multios_esp_label "${plan_path}")
  use_custom_grub_menu=$(duw_multios_use_custom_grub_menu "${plan_path}")
  preserve_upstream_grub_entries=$(duw_multios_preserve_upstream_grub_entries "${plan_path}")
  temp_root=$(duw_create_temp_root multios-usb-update)
  duw_set_cleanup_trap "${temp_root}"
  state_dir=${temp_root}/plan-state
  duw_multios_state_create "${plan_path}" "${state_dir}"
  item_count=$(duw_multios_state_count "${state_dir}")
  [ "${item_count}" -ge 2 ] || duw_die "Multi-OS requires at least two OS items"

  index=0
  while [ "${index}" -lt "${item_count}" ]; do
    profile=$(duw_multios_state_get "${state_dir}" "${index}" profile)
    source_role=$(duw_multios_state_get "${state_dir}" "${index}" source_role)
    iso_path=$(duw_multios_state_get "${state_dir}" "${index}" iso_path)
    layout=$(duw_multios_state_get "${state_dir}" "${index}" layout)
    [ "${layout}" = raw-iso ] || duw_die "Update USB currently supports only raw-iso Multi-OS payloads"
    duw_inspect_managed_media "${profile}" "${iso_path}" "${config_path}" "${use_custom_grub_menu}" "${source_role}"
    duw_multios_state_set "${state_dir}" "${index}" media_class "${DUSB_INSPECTED_MEDIA_CLASS}"
    index=$((index + 1))
  done

  esp_mount=${temp_root}/esp
  mkdir -p -- "${esp_mount}"
  duw_set_cleanup_trap "${temp_root}" "${esp_mount}"
  esp_part=$(duw_device_partition_by_label "${device}" "${esp_label}")
  duw_mount_partition "${esp_part}" "${esp_mount}" vfat
  duw_prepare_secure_boot_identity
  duw_stage_secure_boot_support "${esp_mount}" "${temp_root}"
  duw_note_secure_boot_requirement
  esp_uuid=$(duw_partition_uuid "${esp_part}")

  if [ "${use_custom_grub_menu}" = 1 ]; then
    duw_multios_stage_selected_preseed_from_state "${esp_mount}" "${state_dir}"
  fi

  render_plan_path=${temp_root}/effective-multios-plan.json
  duw_multios_write_effective_plan_from_state "${plan_path}" "${render_plan_path}" "${state_dir}"
  set -- "${render_plan_path}" "${config_path}" "${esp_uuid}"
  index=0
  while [ "${index}" -lt "${item_count}" ]; do
    id=$(duw_multios_state_get "${state_dir}" "${index}" id)
    profile=$(duw_multios_state_get "${state_dir}" "${index}" profile)
    media_class=$(duw_multios_state_get "${state_dir}" "${index}" media_class)
    layout=$(duw_multios_state_get "${state_dir}" "${index}" layout)
    payload_partlabel=$(duw_multios_state_get "${state_dir}" "${index}" payload_partlabel)
    payload_part=$(duw_device_partition_by_partlabel "${device}" "${payload_partlabel}")
    payload_uuid=$(duw_payload_locator_for_render "${profile}" "${media_class}" "${layout}" "${payload_part}")
    set -- "$@" "${id}=${payload_uuid}"
    index=$((index + 1))
  done
  duw_render_multios_grub "$@"
  if duw_render_payload_needs_direct_secure_boot_assets "${DUSB_MANAGED_RENDER_OUTPUT}"; then
    duw_stage_secure_boot_assets_from_render_payload "${DUSB_MANAGED_RENDER_OUTPUT}" "${temp_root}" "${esp_mount}"
    direct_secure_boot_assets_required=1
  fi
  duw_note "Rendered updated ${DUSB_MANAGED_MEDIA_CLASS} Multi-OS GRUB menu with ${DUSB_MANAGED_ENTRY_COUNT} entries"
  if [ "${direct_secure_boot_assets_required}" -eq 0 ]; then
    duw_note "Secure Boot: custom GRUB remains the front menu, and the MOK enrollment entry stays staged."
  fi
  duw_install_grub_bootloader "${esp_part}" "${esp_mount}"
  boot_cfg=${esp_mount}/boot/grub/grub.cfg
  mkdir -p -- "$(dirname -- "${boot_cfg}")"
  printf '%s' "${DUSB_MANAGED_GRUB_CFG}" >"${boot_cfg}"
  duw_sign_grub_data_file "${boot_cfg}"
  duw_stage_secure_boot_manifest "${esp_mount}" "${boot_cfg}" ESP:/boot/grub/grub.cfg
  sync
  duw_finalize_temp_root "${temp_root}"
  duw_step "Multi-OS USB update finished for ${device}"
)

main() (
  config_path=
  profile=
  write_mode=
  device=
  iso_path=
  source_role=primary
  with_persistence=0
  persistence_mode=
  persistence_size_gib=
  kernel_args=
  kernel_path=
  initrd_path=
  menu_label=
  esp_label_override=
  payload_fs_label_override=
  payload_partlabel_override=
  persistence_fs_label_override=
  persistence_partlabel_override=
  offline_preseed_dir=
  live_toram=
  secure_boot_trust=
  use_custom_grub_menu=0
  preserve_upstream_grub_entries=0
  include_preseed=0
  multi_os_plan=
  update_existing=0
  live_tools_prepared=0

  while [ "$#" -gt 0 ]; do
    case $1 in
      --config) duw_require_option_value "$1" "${2-}"; config_path=$2; shift 2 ;;
      --profile) duw_require_option_value "$1" "${2-}"; profile=$2; shift 2 ;;
      --source-role) duw_require_option_value "$1" "${2-}"; source_role=$2; shift 2 ;;
      --write-mode) duw_require_option_value "$1" "${2-}"; write_mode=$2; shift 2 ;;
      --device) duw_require_option_value "$1" "${2-}"; device=$2; shift 2 ;;
      --iso) duw_require_option_value "$1" "${2-}"; iso_path=$2; shift 2 ;;
      --with-persistence) with_persistence=1; shift ;;
      --persistence-mode) duw_require_option_value "$1" "${2-}"; persistence_mode=$2; shift 2 ;;
      --persistence-size-gib) duw_require_option_value "$1" "${2-}"; persistence_size_gib=$2; shift 2 ;;
      --kernel-args) duw_require_option_value "$1" "${2-}"; kernel_args=$2; shift 2 ;;
      --kernel-path) duw_require_option_value "$1" "${2-}"; kernel_path=$2; shift 2 ;;
      --initrd-path) duw_require_option_value "$1" "${2-}"; initrd_path=$2; shift 2 ;;
      --menu-label) duw_require_option_value "$1" "${2-}"; menu_label=$2; shift 2 ;;
      --esp-label) duw_require_option_value "$1" "${2-}"; esp_label_override=$2; shift 2 ;;
      --payload-fs-label) duw_require_option_value "$1" "${2-}"; payload_fs_label_override=$2; shift 2 ;;
      --payload-partlabel) duw_require_option_value "$1" "${2-}"; payload_partlabel_override=$2; shift 2 ;;
      --persistence-fs-label) duw_require_option_value "$1" "${2-}"; persistence_fs_label_override=$2; shift 2 ;;
      --persistence-partlabel) duw_require_option_value "$1" "${2-}"; persistence_partlabel_override=$2; shift 2 ;;
      --offline-preseed-dir) duw_require_option_value "$1" "${2-}"; offline_preseed_dir=$2; shift 2 ;;
      --live-toram) duw_require_option_value "$1" "${2-}"; live_toram=$2; shift 2 ;;
      --secure-boot-trust) duw_require_option_value "$1" "${2-}"; secure_boot_trust=$2; shift 2 ;;
      --use-custom-grub-menu) duw_require_option_value "$1" "${2-}"; use_custom_grub_menu=$2; shift 2 ;;
      --preserve-upstream-grub-entries) duw_require_option_value "$1" "${2-}"; preserve_upstream_grub_entries=$2; shift 2 ;;
      --include-preseed) duw_require_option_value "$1" "${2-}"; include_preseed=$2; shift 2 ;;
      --multi-os-plan) duw_require_option_value "$1" "${2-}"; multi_os_plan=$2; shift 2 ;;
      --update-existing) duw_require_option_value "$1" "${2-}"; update_existing=$2; shift 2 ;;
      --live-tools-prepared) duw_require_option_value "$1" "${2-}"; live_tools_prepared=$2; shift 2 ;;
      --help|-h) duw_usage; return 0 ;;
      *) duw_die "unknown argument: $1" ;;
    esac
  done

  duw_load_install_env
  duw_init_custom_grub_defaults

  [ -n "${config_path}" ] && [ -n "${write_mode}" ] && [ -n "${device}" ] || duw_die "missing required arguments"
  case ${source_role} in primary|netinst|netboot) ;; *) duw_die "source-role must be primary, netinst, or netboot" ;; esac
  if [ "${write_mode}" != multi-os ]; then
    [ -n "${profile}" ] && [ -n "${iso_path}" ] || duw_die "missing required arguments"
    if [ "${source_role}" = primary ]; then
      [ -f "${iso_path}" ] && [ -s "${iso_path}" ] || duw_die "ISO file does not exist or is empty: ${iso_path}"
      case ${iso_path} in *.iso|*.ISO) ;; *) duw_die "expected an ISO file: ${iso_path}" ;; esac
    else
      [ -d "${iso_path}" ] || duw_die "${source_role} source must be a prepared source directory: ${iso_path}"
    fi
  fi

  duw_require_root
  duw_require_commands
  duw_load_config "${config_path}"
  case ${live_tools_prepared} in 0|1) ;; *) duw_die "live-tools-prepared must be 0 or 1" ;; esac
  DUSB_LIVE_TOOLS_PREPARED=${live_tools_prepared}
  DUSB_OVERRIDE_ESP_LABEL=${esp_label_override}
  DUSB_OVERRIDE_PAYLOAD_FS_LABEL=${payload_fs_label_override}
  DUSB_OVERRIDE_PAYLOAD_PARTLABEL=${payload_partlabel_override}
  DUSB_OVERRIDE_PERSISTENCE_FS_LABEL=${persistence_fs_label_override}
  DUSB_OVERRIDE_PERSISTENCE_PARTLABEL=${persistence_partlabel_override}
  if [ -n "${DUSB_OVERRIDE_ESP_LABEL}" ]; then duw_validate_config_label DUSB_OVERRIDE_ESP_LABEL "${DUSB_OVERRIDE_ESP_LABEL}" 11; fi
  if [ -n "${DUSB_OVERRIDE_PAYLOAD_FS_LABEL}" ]; then duw_validate_config_label DUSB_OVERRIDE_PAYLOAD_FS_LABEL "${DUSB_OVERRIDE_PAYLOAD_FS_LABEL}" 16; fi
  if [ -n "${DUSB_OVERRIDE_PAYLOAD_PARTLABEL}" ]; then duw_validate_config_label DUSB_OVERRIDE_PAYLOAD_PARTLABEL "${DUSB_OVERRIDE_PAYLOAD_PARTLABEL}" 16; fi
  if [ -n "${DUSB_OVERRIDE_PERSISTENCE_FS_LABEL}" ]; then duw_validate_config_label DUSB_OVERRIDE_PERSISTENCE_FS_LABEL "${DUSB_OVERRIDE_PERSISTENCE_FS_LABEL}" 16; fi
  if [ -n "${DUSB_OVERRIDE_PERSISTENCE_PARTLABEL}" ]; then duw_validate_config_label DUSB_OVERRIDE_PERSISTENCE_PARTLABEL "${DUSB_OVERRIDE_PERSISTENCE_PARTLABEL}" 16; fi
  duw_validate_device "${device}"

  [ "${write_mode}" != live ] || duw_die "write-mode=live is no longer supported; use write-mode=managed"

  if [ "${write_mode}" = direct ]; then
    [ "${source_role}" = primary ] || duw_die "write-mode=direct only supports source-role=primary"
    [ "${update_existing}" = 0 ] || duw_die "Update USB is only supported with write-mode=managed or write-mode=multi-os"
    [ -z "${secure_boot_trust}" ] || duw_die "secure boot trust is only supported with write-mode=managed or a Multi-OS plan"
    [ -z "${offline_preseed_dir}" ] || duw_die "offline preseed content is only supported with write-mode=managed or a Multi-OS plan"
    [ "${include_preseed}" = 0 ] || duw_die "preseed entries are only supported with write-mode=managed"
    [ "${with_persistence}" -eq 0 ] || duw_die "persistence is only supported with write-mode=managed"
    duw_write_hybrid_iso "${iso_path}" "${device}"
    return 0
  fi

  if [ "${write_mode}" = multi-os ]; then
    [ -n "${multi_os_plan}" ] || duw_die "missing required argument: --multi-os-plan"
    [ -z "${secure_boot_trust}" ] || duw_die "write-mode=multi-os carries secure boot trust inside the plan; do not pass --secure-boot-trust"
    [ -z "${offline_preseed_dir}" ] || duw_die "write-mode=multi-os carries offline preseed directories inside the plan; do not pass --offline-preseed-dir"
    [ "${include_preseed}" = 0 ] || duw_die "write-mode=multi-os carries preseed selections inside the plan; do not pass --include-preseed"
    [ -z "${profile}" ] && [ -z "${iso_path}" ] || duw_die "write-mode=multi-os does not accept --profile or --iso"
    case ${update_existing} in 0|1) ;; *) duw_die "update-existing must be 0 or 1" ;; esac
    if duw_multios_uses_shared_iso_store_layout "${multi_os_plan}" "${config_path}"; then
      if [ "${update_existing}" = 1 ]; then
        duw_update_multios_shared_data_usb "${multi_os_plan}" "${device}" "${config_path}"
      else
        duw_build_multios_iso_store_usb "${multi_os_plan}" "${device}" "${config_path}"
      fi
    elif [ "${update_existing}" = 1 ]; then
      duw_update_multios_usb "${multi_os_plan}" "${device}" "${config_path}"
    else
      duw_build_multios_usb "${multi_os_plan}" "${device}" "${config_path}"
    fi
    return 0
  fi

  [ "${write_mode}" = managed ] || duw_die "write mode must be direct, managed, or multi-os"
  DUSB_SECURE_BOOT_TRUST=$(duw_effective_secure_boot_trust "${secure_boot_trust}")
  if [ -n "${live_toram}" ]; then case ${live_toram} in 0|1) ;; *) duw_die "live-toram must be 0 or 1" ;; esac; fi
  case ${use_custom_grub_menu} in 0|1) ;; *) duw_die "use-custom-grub-menu must be 0 or 1" ;; esac
  case ${preserve_upstream_grub_entries} in 0|1) ;; *) duw_die "preserve-upstream-grub-entries must be 0 or 1" ;; esac
  case ${include_preseed} in 0|1) ;; *) duw_die "include-preseed must be 0 or 1" ;; esac
  case ${update_existing} in 0|1) ;; *) duw_die "update-existing must be 0 or 1" ;; esac
  if [ "${include_preseed}" != 1 ] && [ -n "${offline_preseed_dir}" ]; then duw_die "offline preseed content requires --include-preseed 1"; fi
  if [ "${include_preseed}" = 1 ] && [ "${use_custom_grub_menu}" != 1 ]; then duw_die "preseed entries require --use-custom-grub-menu 1"; fi

  case ${profile} in
    debian|ubuntu-desktop|ubuntu-server|kali-linux|kali-purple|tails) ;;
    *) duw_die "profile does not support managed mode: ${profile}" ;;
  esac
  if [ "${source_role}" != primary ]; then
    case ${profile} in debian|kali-linux) ;; *) duw_die "source-role=${source_role} is only supported for Debian and Kali Linux managed flows" ;; esac
    [ "${update_existing}" = 0 ] || duw_die "Update USB is not implemented for source-role=${source_role}"
  fi

  if [ "${with_persistence}" -eq 1 ]; then
    [ -n "${persistence_mode}" ] || persistence_mode=plain
    case ${persistence_mode} in plain|encrypted) ;; *) duw_die "persistence mode must be plain or encrypted" ;; esac
    [ -n "${persistence_size_gib}" ] || persistence_size_gib=${DEFAULT_PERSISTENCE_SIZE_GIB}
    case ${persistence_size_gib} in ''|*[!0-9]*) duw_die "persistence size must be a positive integer" ;; esac
    [ "${persistence_size_gib}" -gt 0 ] || duw_die "persistence size must be positive"
    if [ "${profile}" = tails ] && [ "${persistence_mode}" != encrypted ]; then duw_die "Tails managed persistence is encrypted only"; fi
    if [ "${persistence_mode}" = encrypted ] && [ "${profile}" != debian ] && [ "${profile}" != kali-linux ] && [ "${profile}" != tails ]; then
      duw_die "encrypted persistence is currently supported only for Debian, Kali Linux, and Tails live media"
    fi
  else
    persistence_mode=
    persistence_size_gib=0
  fi

  if [ "${update_existing}" = 1 ]; then
    duw_update_managed_usb "${profile}" "${iso_path}" "${device}" "${with_persistence}" "${persistence_mode}" "${persistence_size_gib}" "${kernel_args}" "${menu_label}" "${kernel_path}" "${initrd_path}" "${config_path}" "${live_toram}" "${use_custom_grub_menu}" "${preserve_upstream_grub_entries}" "${offline_preseed_dir}" "${include_preseed}"
  else
    duw_build_managed_usb "${profile}" "${iso_path}" "${device}" "${source_role}" "${with_persistence}" "${persistence_mode}" "${persistence_size_gib}" "${kernel_args}" "${menu_label}" "${kernel_path}" "${initrd_path}" "${config_path}" "${live_toram}" "${use_custom_grub_menu}" "${preserve_upstream_grub_entries}" "${offline_preseed_dir}" "${include_preseed}"
  fi
)

if [ "${DEBIAN_USB_WRITER_SOURCE_ONLY:-0}" != 1 ]; then
  main "$@"
fi
