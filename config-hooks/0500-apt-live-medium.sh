#!/bin/sh
set -eu
IFS=$(printf '\n\t')

lap_log() (
  printf 'debian-usb-live-apt: %s\n' "$*" >&2
)

lap_warn() (
  printf 'debian-usb-live-apt: warning: %s\n' "$*" >&2
)

lap_validate_root() (
  root=$1
  case ${root} in
    /*) ;;
    *) return 1 ;;
  esac
  case ${root} in
    */../*|*/..) return 1 ;;
  esac
  [ -d "${root}" ]
)

lap_root_path() (
  root=$1
  path=$2
  if [ "${root}" = / ]; then
    printf '%s\n' "${path}"
  else
    printf '%s%s\n' "${root%/}" "${path}"
  fi
)

lap_strip_quotes() (
  value=$1
  case ${value} in
    \"*\") value=${value#\"}; value=${value%\"} ;;
    \'*\') value=${value#\'}; value=${value%\'} ;;
  esac
  printf '%s\n' "${value}"
)

lap_os_release_value() (
  root=$1
  key=$2
  os_release=$(lap_root_path "${root}" /etc/os-release)
  [ -r "${os_release}" ] || return 1
  while IFS= read -r line || [ -n "${line}" ]; do
    case ${line} in *=*) ;; *) continue ;; esac
    name=${line%%=*}
    [ "${name}" = "${key}" ] || continue
    value=${line#*=}
    lap_strip_quotes "${value}"
    return 0
  done <"${os_release}"
  return 1
)

lap_cmdline_value() (
  key=$1
  cmdline_path=${LAP_CMDLINE_PATH:-/proc/cmdline}
  [ -r "${cmdline_path}" ] || return 1
  old_ifs=${IFS}
  IFS=' '
  # Intentional splitting: kernel command lines are space-delimited tokens.
  # shellcheck disable=SC2013,SC2046
  for token in $(cat "${cmdline_path}"); do
    case ${token} in
      "${key}="*)
        value=${token#*=}
        lap_strip_quotes "${value}"
        return 0
        ;;
    esac
  done
  IFS=${old_ifs}
  return 1
)

lap_validate_suite() (
  suite=$1
  [ "${#suite}" -le 64 ] || return 1
  case ${suite} in
    ''|[!a-z]*|*[!a-z0-9-]*) return 1 ;;
    *) return 0 ;;
  esac
)

lap_resolve_suite() (
  root=$1
  suite=${LIVE_APT_SUITE:-}
  if [ -z "${suite}" ]; then
    suite=$(lap_cmdline_value live_apt_suite 2>/dev/null || true)
  fi
  if [ -z "${suite}" ]; then
    suite=$(lap_os_release_value "${root}" VERSION_CODENAME 2>/dev/null || true)
  fi
  lap_validate_suite "${suite}" || return 1
  printf '%s\n' "${suite}"
)

lap_write_official_sources() (
  apt_root=$1
  suite=$2
  parts_dir=${apt_root}/sources.list.d
  mkdir -p -- "${parts_dir}"
  chmod 0755 "${parts_dir}"

  : >"${apt_root}/sources.list"
  chmod 0644 "${apt_root}/sources.list"
  for source_file in "${parts_dir}"/*.list "${parts_dir}"/*.sources; do
    [ -e "${source_file}" ] || [ -L "${source_file}" ] || continue
    rm -f -- "${source_file}"
  done

  old_umask=$(umask)
  umask 022
  temp_file=$(mktemp "${parts_dir}/.debian.sources.XXXXXX")
  umask "${old_umask}"
  cat >"${temp_file}" <<EOF_SOURCES
Types: deb
URIs: https://deb.debian.org/debian
Suites: ${suite}
Components: main contrib non-free non-free-firmware
Signed-By: /usr/share/keyrings/debian-archive-keyring.gpg
EOF_SOURCES
  chmod 0644 "${temp_file}"
  mv -f -- "${temp_file}" "${parts_dir}/debian.sources"
)

lap_main() (
  root=${1:-/}
  lap_validate_root "${root}" || {
    lap_warn "refusing unsafe live root: ${root}"
    return 1
  }
  distro_id=$(lap_os_release_value "${root}" ID 2>/dev/null || true)
  if [ "${distro_id}" != debian ]; then
    lap_log "non-Debian Live root detected; leaving APT sources unchanged"
    return 0
  fi
  suite=$(lap_resolve_suite "${root}") || {
    lap_warn "could not determine a safe Debian suite; leaving APT sources unchanged"
    return 1
  }
  apt_root=$(lap_root_path "${root}" /etc/apt)
  mkdir -p -- "${apt_root}"
  chmod 0755 "${apt_root}"
  lap_write_official_sources "${apt_root}" "${suite}"
  lap_log "configured official Debian upstream sources for suite ${suite}"
)

if [ "${DEBIAN_USB_HOOK_SOURCE_ONLY:-0}" != 1 ]; then
  lap_main / || lap_warn "could not configure Debian Live APT sources"
  exit 0
fi
