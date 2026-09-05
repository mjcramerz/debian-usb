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

lap_filter_legacy_source_file() (
  source_file=$1
  [ -f "${source_file}" ] || return 0

  source_dir=${source_file%/*}
  old_umask=$(umask)
  umask 077
  temp_file=$(mktemp "${source_dir}/.debian-usb-list.XXXXXX")
  change_file=$(mktemp "${source_dir}/.debian-usb-list-change.XXXXXX")
  umask "${old_umask}"
  trap 'rm -f -- "${temp_file}" "${change_file}"' 0 INT TERM

  awk -v change_file="${change_file}" '
    function local_live_uri(uri, normalized) {
      normalized = tolower(uri)
      return normalized ~ /^cdrom:/ ||
        normalized ~ /^file:\/+(run\/live\/medium|lib\/live\/mount\/medium|cdrom)(\/|$)/
    }
    function normalized_option(option) {
      option = tolower(option)
      sub(/^\[/, "", option)
      sub(/\]$/, "", option)
      return option
    }
    function source_is_disabled(first, last, loop_index, option) {
      for (loop_index = first; loop_index <= last; loop_index++) {
        option = normalized_option(fields[loop_index])
        if (option == "enabled=no" || option == "enabled=false" || option == "enabled=0") {
          return 1
        }
      }
      return 0
    }
    {
      trimmed = $0
      sub(/^[[:space:]]+/, "", trimmed)
      count = split(trimmed, fields, /[[:space:]]+/)
      if (count < 2 || (fields[1] != "deb" && fields[1] != "deb-src")) {
        print
        next
      }

      uri_index = 2
      if (fields[uri_index] ~ /^\[/) {
        option_start = uri_index
        while (uri_index <= count && fields[uri_index] !~ /\]$/) {
          uri_index++
        }
        option_end = uri_index
        uri_index++
        if (source_is_disabled(option_start, option_end)) {
          print
          next
        }
      }
      if (uri_index <= count && local_live_uri(fields[uri_index])) {
        changed = 1
        next
      }
      print
    }
    END {
      if (changed) {
        print "changed" >change_file
      }
    }
  ' "${source_file}" >"${temp_file}"

  if [ ! -s "${change_file}" ]; then
    rm -f -- "${temp_file}" "${change_file}"
    trap - 0 INT TERM
    return 0
  fi
  chmod --reference="${source_file}" "${temp_file}" 2>/dev/null || chmod 0644 "${temp_file}"
  mv -f -- "${temp_file}" "${source_file}"
  rm -f -- "${change_file}"
  trap - 0 INT TERM
)

lap_filter_deb822_source_file() (
  source_file=$1
  [ -f "${source_file}" ] || return 0

  source_dir=${source_file%/*}
  old_umask=$(umask)
  umask 077
  temp_file=$(mktemp "${source_dir}/.debian-usb-sources.XXXXXX")
  change_file=$(mktemp "${source_dir}/.debian-usb-sources-change.XXXXXX")
  umask "${old_umask}"
  trap 'rm -f -- "${temp_file}" "${change_file}"' 0 INT TERM

  awk -v change_file="${change_file}" '
    function local_live_uri(uri, normalized) {
      normalized = tolower(uri)
      return normalized ~ /^cdrom:/ ||
        normalized ~ /^file:\/+(run\/live\/medium|lib\/live\/mount\/medium|cdrom)(\/|$)/
    }
    function append_line(buffer, line) {
      return buffer (buffer == "" ? "" : "\n") line
    }
    BEGIN {
      RS = ""
      ORS = ""
    }
    {
      line_count = split($0, lines, "\n")
      enabled = 1
      for (line_index = 1; line_index <= line_count; line_index++) {
        if (tolower(lines[line_index]) ~ /^enabled:[[:space:]]*(no|false|0)([[:space:]]|$)/) {
          enabled = 0
        }
      }
      if (!enabled) {
        print $0 "\n\n"
        next
      }

      output = ""
      saw_uri_field = 0
      removed_local_uri = 0
      kept_uri_count = 0
      for (line_index = 1; line_index <= line_count; line_index++) {
        line = lines[line_index]
        if (tolower(line) !~ /^uris:[[:space:]]*/) {
          output = append_line(output, line)
          continue
        }

        saw_uri_field = 1
        values = line
        sub(/^[^:]*:[[:space:]]*/, "", values)
        while (line_index < line_count && lines[line_index + 1] ~ /^[[:space:]]+/) {
          line_index++
          continuation = lines[line_index]
          sub(/^[[:space:]]+/, "", continuation)
          values = values " " continuation
        }

        kept_values = ""
        value_count = split(values, uri_values, /[[:space:]]+/)
        for (value_index = 1; value_index <= value_count; value_index++) {
          uri = uri_values[value_index]
          if (uri == "") {
            continue
          }
          if (local_live_uri(uri)) {
            removed_local_uri = 1
            changed = 1
            continue
          }
          kept_values = kept_values (kept_values == "" ? "" : " ") uri
          kept_uri_count++
        }
        if (kept_values != "") {
          output = append_line(output, "URIs: " kept_values)
        }
      }

      if (!removed_local_uri) {
        print $0 "\n\n"
        next
      }
      if (saw_uri_field && kept_uri_count == 0) {
        next
      }
      print output "\n\n"
    }
    END {
      if (changed) {
        print "changed" >change_file
      }
    }
  ' "${source_file}" >"${temp_file}"

  if [ ! -s "${change_file}" ]; then
    rm -f -- "${temp_file}" "${change_file}"
    trap - 0 INT TERM
    return 0
  fi
  chmod --reference="${source_file}" "${temp_file}" 2>/dev/null || chmod 0644 "${temp_file}"
  mv -f -- "${temp_file}" "${source_file}"
  rm -f -- "${change_file}"
  trap - 0 INT TERM
)

lap_filter_local_sources() (
  apt_root=$1
  parts_dir=${apt_root}/sources.list.d

  lap_filter_legacy_source_file "${apt_root}/sources.list"
  for source_file in "${parts_dir}"/*.list; do
    [ -e "${source_file}" ] || continue
    lap_filter_legacy_source_file "${source_file}"
  done
  for source_file in "${parts_dir}"/*.sources; do
    [ -e "${source_file}" ] || continue
    lap_filter_deb822_source_file "${source_file}"
  done
)

lap_has_network_source() (
  apt_root=$1
  parts_dir=${apt_root}/sources.list.d

  for source_file in "${apt_root}/sources.list" "${parts_dir}"/*.list; do
    [ -f "${source_file}" ] || continue
    if awk '
      function normalized_option(option) {
        option = tolower(option)
        sub(/^\[/, "", option)
        sub(/\]$/, "", option)
        return option
      }
      function enabled_binary_network_source(line, count, uri_index, option) {
        sub(/^[[:space:]]+/, "", line)
        count = split(line, fields, /[[:space:]]+/)
        if (count < 2 || fields[1] != "deb") {
          return 0
        }
        uri_index = 2
        if (fields[uri_index] ~ /^\[/) {
          while (uri_index <= count && fields[uri_index] !~ /\]$/) {
            option = normalized_option(fields[uri_index])
            if (option == "enabled=no" || option == "enabled=false" || option == "enabled=0") {
              return 0
            }
            uri_index++
          }
          option = normalized_option(fields[uri_index])
          if (option == "enabled=no" || option == "enabled=false" || option == "enabled=0") {
            return 0
          }
          uri_index++
        }
        return uri_index <= count && tolower(fields[uri_index]) ~ /^https?:\/\//
      }
      enabled_binary_network_source($0) { found = 1; exit }
      END { exit(found ? 0 : 1) }
    ' "${source_file}"; then
      return 0
    fi
  done

  for source_file in "${parts_dir}"/*.sources; do
    [ -f "${source_file}" ] || continue
    if awk '
      BEGIN { RS = "" }
      {
        enabled = 1
        has_binary_type = 0
        has_network_uri = 0
        line_count = split($0, lines, "\n")
        for (line_index = 1; line_index <= line_count; line_index++) {
          line = lines[line_index]
          lowered = tolower(line)
          if (lowered ~ /^enabled:[[:space:]]*(no|false|0)([[:space:]]|$)/) {
            enabled = 0
          }
          if (lowered ~ /^types:[[:space:]]*/) {
            sub(/^[^:]*:[[:space:]]*/, "", lowered)
            while (line_index < line_count && lines[line_index + 1] ~ /^[[:space:]]+/) {
              line_index++
              continuation = tolower(lines[line_index])
              sub(/^[[:space:]]+/, "", continuation)
              lowered = lowered " " continuation
            }
            value_count = split(lowered, values, /[[:space:]]+/)
            for (value_index = 1; value_index <= value_count; value_index++) {
              if (values[value_index] == "deb") {
                has_binary_type = 1
              }
            }
          }
          if (lowered ~ /^uris:[[:space:]]*/) {
            sub(/^[^:]*:[[:space:]]*/, "", lowered)
            while (line_index < line_count && lines[line_index + 1] ~ /^[[:space:]]+/) {
              line_index++
              continuation = tolower(lines[line_index])
              sub(/^[[:space:]]+/, "", continuation)
              lowered = lowered " " continuation
            }
            value_count = split(lowered, values, /[[:space:]]+/)
            for (value_index = 1; value_index <= value_count; value_index++) {
              if (values[value_index] ~ /^https?:\/\//) {
                has_network_uri = 1
              }
            }
          }
        }
        if (enabled && has_binary_type && has_network_uri) {
          found = 1
          exit
        }
      }
      END { exit(found ? 0 : 1) }
    ' "${source_file}"; then
      return 0
    fi
  done
  return 1
)

lap_write_official_source() (
  apt_root=$1
  suite=$2
  parts_dir=${apt_root}/sources.list.d
  source_file=${parts_dir}/debian-usb-live.sources

  old_umask=$(umask)
  umask 022
  temp_file=$(mktemp "${parts_dir}/.debian-usb-live.sources.XXXXXX")
  umask "${old_umask}"
  trap 'rm -f -- "${temp_file}"' 0 INT TERM
  cat >"${temp_file}" <<EOF_SOURCES
Types: deb
URIs: https://deb.debian.org/debian
Suites: ${suite}
Components: main contrib non-free non-free-firmware
Signed-By: /usr/share/keyrings/debian-archive-keyring.gpg
EOF_SOURCES
  chmod 0644 "${temp_file}"
  mv -f -- "${temp_file}" "${source_file}"
  trap - 0 INT TERM
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
  parts_dir=${apt_root}/sources.list.d
  mkdir -p -- "${parts_dir}"
  chmod 0755 "${apt_root}" "${parts_dir}"
  lap_filter_local_sources "${apt_root}"
  if lap_has_network_source "${apt_root}"; then
    lap_log "removed active Live-medium/CD-ROM sources and preserved network sources"
  else
    lap_write_official_source "${apt_root}" "${suite}"
    lap_log "removed active Live-medium/CD-ROM sources and added Debian upstream suite ${suite}"
  fi
)

if [ "${DEBIAN_USB_HOOK_SOURCE_ONLY:-0}" != 1 ]; then
  lap_main / || lap_warn "could not configure Debian Live APT sources"
  exit 0
fi
