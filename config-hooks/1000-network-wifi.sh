#!/bin/sh
set -eu
IFS=$(printf '\n\t')

lw_log() (
  printf '1000-network-wifi: %s\n' "$*"
)

lw_warn() (
  printf '1000-network-wifi: WARNING: %s\n' "$*" >&2
)

lw_have() (
  command -v "$1" >/dev/null 2>&1
)

lw_has_default_route() (
  [ -r /proc/net/route ] || return 1
  while read -r iface destination _gateway _flags _rest; do
    [ "${iface}" != Iface ] || continue
    if [ "${destination}" = 00000000 ]; then
      return 0
    fi
  done </proc/net/route
  return 1
)

lw_strip_quotes() (
  value=$1
  value=${value%\"}
  value=${value#\"}
  value=${value%\'}
  value=${value#\'}
  printf '%s\n' "${value}"
)

lw_cmdline_value() (
  key=$1
  [ -r /proc/cmdline ] || return 1
  old_ifs=${IFS}
  IFS=' '
  # Intentional splitting: kernel command lines are space-delimited tokens.
  # shellcheck disable=SC2013,SC2046
  for token in $(cat /proc/cmdline); do
    case ${token} in
      "${key}="*)
        value=${token#*=}
        lw_strip_quotes "${value}"
        return 0
        ;;
    esac
  done
  IFS=${old_ifs}
  return 1
)

lw_env_value() (
  case $1 in
    LIVE_WIFI_ENABLED) printf '%s\n' "${LIVE_WIFI_ENABLED:-}" ;;
    LIVE_WIFI_INTERFACE) printf '%s\n' "${LIVE_WIFI_INTERFACE:-}" ;;
    LIVE_WIFI_SSID) printf '%s\n' "${LIVE_WIFI_SSID:-}" ;;
    LIVE_WIFI_SECURITY) printf '%s\n' "${LIVE_WIFI_SECURITY:-}" ;;
    LIVE_WIFI_PSK) printf '%s\n' "${LIVE_WIFI_PSK:-}" ;;
    LIVE_WIFI_CIDR) printf '%s\n' "${LIVE_WIFI_CIDR:-}" ;;
    LIVE_WIFI_NAMESERVERS) printf '%s\n' "${LIVE_WIFI_NAMESERVERS:-}" ;;
    LIVE_WIFI_GATEWAY) printf '%s\n' "${LIVE_WIFI_GATEWAY:-}" ;;
    *) return 1 ;;
  esac
)

lw_assignment_value() (
  assignment_path=$1
  assignment_key=$2
  awk -v key="${assignment_key}" '
    index($0, key "=") != 1 { next }
    {
      value = substr($0, length(key) + 2)
      if (length(value) >= 2) {
        first = substr(value, 1, 1)
        last = substr(value, length(value), 1)
        if ((first == "\047" && last == "\047") || (first == "\"" && last == "\"")) {
          value = substr(value, 2, length(value) - 2)
        }
      }
      print value
      found = 1
      exit
    }
    END { if (!found) exit 1 }
  ' "${assignment_path}" 2>/dev/null
)

lw_live_wifi_passphrase() (
  value=$(lw_env_value LIVE_WIFI_PSK 2>/dev/null || true)
  if [ -n "${value}" ]; then
    printf '%s\n' "${value}"
    return 0
  fi
  lw_assignment_value \
    "${DEBIAN_USB_LIVE_ENV_PATH:-/run/initramfs/debian-usb/live.env}" \
    PRESEED_WIFI_PASSPHRASE
)

lw_config_value() (
  env_name=$1
  shift
  value=$(lw_env_value "${env_name}" 2>/dev/null || true)
  if [ -n "${value}" ]; then
    printf '%s\n' "${value}"
    return 0
  fi
  for key in "$@"; do
    if value=$(lw_cmdline_value "${key}"); then
      printf '%s\n' "${value}"
      return 0
    fi
  done
  return 1
)

lw_decode_base64url() (
  value=$1
  case ${value} in
    ''|*[!A-Za-z0-9_-]*) return 1 ;;
  esac
  remainder=$((${#value} % 4))
  [ "${remainder}" -ne 1 ] || return 1
  value=$(printf '%s' "${value}" | tr '_-' '/+')
  case ${remainder} in
    2) value=${value}== ;;
    3) value=${value}= ;;
  esac
  decoded=$(printf '%s' "${value}" | base64 --decode 2>/dev/null) || return 1
  printf '%s' "${decoded}"
)

lw_config_encoded_value() (
  env_name=$1
  encoded_key=$2
  shift 2
  value=$(lw_env_value "${env_name}" 2>/dev/null || true)
  if [ -n "${value}" ]; then
    printf '%s\n' "${value}"
    return 0
  fi
  if encoded=$(lw_cmdline_value "${encoded_key}"); then
    lw_decode_base64url "${encoded}"
    return
  fi
  lw_config_value "${env_name}" "$@"
)

lw_valid_interface_name() (
  value=$1
  [ -n "${value}" ] && [ "${#value}" -le 64 ] || return 1
  case ${value} in
    *[!A-Za-z0-9_.:-]*) return 1 ;;
    *) return 0 ;;
  esac
)

lw_text_byte_length() (
  LC_ALL=C
  export LC_ALL
  printf '%s' "$1" | wc -c | tr -d '[:space:]'
  printf '\n'
)

lw_valid_wifi_text() (
  value=$1
  carriage_return=$(printf '\r')
  case ${value} in
    *"${carriage_return}"*|*"
"*|*"	"*) return 1 ;;
    *) return 0 ;;
  esac
)

lw_valid_ipv4() (
  printf '%s\n' "$1" | awk -F. '
    BEGIN { valid = 1 }
    NF != 4 { valid = 0 }
    {
      for (i = 1; valid && i <= 4; i++) {
        if ($i !~ /^[0-9][0-9]*$/ || length($i) > 3 || ($i + 0) > 255) valid = 0
      }
    }
    END { exit(valid ? 0 : 1) }
  '
)

lw_normalize_cidr() (
  cidr=$1
  case ${cidr} in */*) ;; *) return 1 ;; esac
  address=${cidr%/*}
  prefix=${cidr##*/}
  lw_valid_ipv4 "${address}" || return 1
  case ${prefix} in ''|*[!0-9]*) return 1 ;; esac
  [ "${prefix}" -le 32 ] || return 1
  printf '%s/%s\n' "${address}" "${prefix}"
)

lw_prefix_netmask() (
  prefix=$1
  case ${prefix} in ''|*[!0-9]*) return 1 ;; esac
  [ "${prefix}" -le 32 ] || return 1
  remaining=${prefix}
  result=
  octet_index=1
  while [ "${octet_index}" -le 4 ]; do
    if [ "${remaining}" -ge 8 ]; then
      value=255
      remaining=$((remaining - 8))
    elif [ "${remaining}" -gt 0 ]; then
      host_bits=$((8 - remaining))
      host_values=1
      while [ "${host_bits}" -gt 0 ]; do
        host_values=$((host_values * 2))
        host_bits=$((host_bits - 1))
      done
      value=$((256 - host_values))
      remaining=0
    else
      value=0
    fi
    result=${result}${result:+.}${value}
    octet_index=$((octet_index + 1))
  done
  printf '%s\n' "${result}"
)

lw_cidr_netmask() (
  cidr=$1
  case ${cidr} in */*) ;; *) return 1 ;; esac
  lw_prefix_netmask "${cidr##*/}"
)

lw_nameserver_lines() (
  nameservers=$1
  [ -n "${nameservers}" ] || return 0
  printf '%s\n' "${nameservers}" | tr ', ' '\n' |
    while IFS= read -r server; do
      [ -n "${server}" ] || continue
      if ! lw_valid_ipv4 "${server}"; then
        lw_warn "ignoring invalid IPv4 nameserver: ${server}"
        continue
      fi
      printf '%s\n' "${server}"
    done
)

lw_wait_for_interface() (
  interface=$1
  attempt=1
  while [ "${attempt}" -le 40 ]; do
    if [ -d "/sys/class/net/${interface}" ]; then
      return 0
    fi
    sleep 0.25
    attempt=$((attempt + 1))
  done
  return 1
)

lw_detect_wifi_interface() (
  for path in /sys/class/net/*; do
    [ -d "${path}" ] && [ -d "${path}/wireless" ] || continue
    interface=$(basename "${path}")
    [ "${interface}" != lo ] || continue
    printf '%s\n' "${interface}"
    return 0
  done
  if lw_have iw; then
    interface=$(iw dev 2>/dev/null | awk '$1 == "Interface" { print $2; exit }')
    if [ -n "${interface}" ]; then
      printf '%s\n' "${interface}"
      return 0
    fi
  fi
  return 1
)

lw_resolve_interface() (
  requested=$1
  if [ -n "${requested}" ] && [ "${requested}" != auto ]; then
    lw_valid_interface_name "${requested}" || return 1
    lw_wait_for_interface "${requested}" || return 1
    printf '%s\n' "${requested}"
    return 0
  fi
  requested=$(lw_detect_wifi_interface) || return 1
  [ -n "${requested}" ] || return 1
  lw_valid_interface_name "${requested}" || return 1
  lw_wait_for_interface "${requested}" || return 1
  printf '%s\n' "${requested}"
)

lw_escape_wpa_string() (
  printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g'
)

lw_validate_security() (
  case $1 in
    open|wpa|sae) printf '%s\n' "$1" ;;
    *) return 1 ;;
  esac
)

lw_is_hex_psk() (
  [ "${#1}" -eq 64 ] || return 1
  case $1 in *[!0123456789abcdefABCDEF]*) return 1 ;; *) return 0 ;; esac
)

lw_write_plain_wpa_config() (
  output=$1
  ssid=$2
  psk=$3
  security=$4
  escaped_ssid=$(lw_escape_wpa_string "${ssid}")
  {
    printf 'ctrl_interface=/run/wpa_supplicant\n'
    printf 'update_config=0\n'
    printf 'network={\n'
    printf '    ssid="%s"\n' "${escaped_ssid}"
    if [ "${security}" = open ]; then
      printf '    key_mgmt=NONE\n'
    elif [ "${security}" = sae ]; then
      escaped_psk=$(lw_escape_wpa_string "${psk}")
      printf '    key_mgmt=SAE\n'
      printf '    proto=RSN\n'
      printf '    ieee80211w=2\n'
      printf '    sae_password="%s"\n' "${escaped_psk}"
    else
      printf '    key_mgmt=WPA-PSK\n'
      printf '    proto=RSN\n'
      if lw_is_hex_psk "${psk}"; then
        printf '    psk=%s\n' "${psk}"
      else
        escaped_psk=$(lw_escape_wpa_string "${psk}")
        printf '    psk="%s"\n' "${escaped_psk}"
      fi
    fi
    printf '}\n'
  } >"${output}"
)

lw_build_wpa_config() (
  output=$1
  ssid=$2
  psk=$3
  security=$4
  validated_security=$(lw_validate_security "${security}") || return 1
  umask 077
  lw_write_plain_wpa_config "${output}" "${ssid}" "${psk}" "${validated_security}"
  chmod 0600 "${output}"
)

lw_run_bounded() (
  timeout_seconds=$1
  shift
  if lw_have timeout; then
    timeout "${timeout_seconds}" "$@"
    return
  fi
  "$@"
)

lw_collect_missing_packages() (
  interface_request=$1
  cidr=$2
  lw_have ip || printf '%s\n' iproute2
  lw_have wpa_supplicant || printf '%s\n' wpasupplicant
  if [ "${interface_request}" = auto ] && ! lw_have iw; then
    printf '%s\n' iw
  fi
  if [ -z "${cidr}" ] && ! lw_have dhcpcd && ! lw_have dhclient && ! lw_have udhcpc; then
    printf '%s\n' dhcpcd-base
  fi
)

lw_apt_update_reachable() (
  lw_have apt-get || return 1
  lw_has_default_route || return 1
  DEBIAN_FRONTEND=noninteractive \
    lw_run_bounded 90 \
    apt-get \
      -o Acquire::Retries=0 \
      -o Acquire::http::Timeout=10 \
      -o Acquire::https::Timeout=10 \
      -o DPkg::Lock::Timeout=30 \
      update >/dev/null 2>&1
)

lw_install_missing_packages() (
  [ "$#" -gt 0 ] || return 0
  DEBIAN_FRONTEND=noninteractive \
    lw_run_bounded 180 \
    apt-get \
      -o Acquire::Retries=1 \
      -o Acquire::http::Timeout=10 \
      -o Acquire::https::Timeout=10 \
      -o DPkg::Lock::Timeout=30 \
      install -y --no-install-recommends "$@" >/dev/null 2>&1
)

lw_ensure_required_tools() (
  interface_request=$1
  cidr=$2
  packages=$(lw_collect_missing_packages "${interface_request}" "${cidr}")
  [ -n "${packages}" ] || return 0

  old_ifs=${IFS}
  IFS=$(printf '\n\t')
  set -f
  # Intentional splitting: the collector emits one package name per line.
  # shellcheck disable=SC2086
  set -- ${packages}
  set +f
  IFS=${old_ifs}
  summary=$(printf '%s' "${packages}" | tr '\n' ' ')
  lw_log "missing required tools; attempting package install: ${summary}"

  if ! lw_apt_update_reachable; then
    lw_log "skipping.. tools not available and no network reachability."
    return 1
  fi
  if ! lw_install_missing_packages "$@"; then
    lw_warn "required tool installation failed for: ${summary}"
    return 1
  fi
  if lw_collect_missing_packages "${interface_request}" "${cidr}" | grep -q .; then
    lw_warn "required tools are still unavailable after package installation"
    return 1
  fi
  lw_log "installed required live Wi-Fi tools: ${summary}"
)

lw_wpa_completed() (
  interface=$1
  if lw_have wpa_cli && wpa_cli -i "${interface}" status 2>/dev/null | grep -q '^wpa_state=COMPLETED$'; then
    return 0
  fi
  if lw_have iw && iw dev "${interface}" link 2>/dev/null | grep -q '^Connected to '; then
    return 0
  fi
  return 1
)

lw_ssid_visible() (
  interface=$1
  requested_ssid=$2
  lw_have iw || return 1
  if lw_have rfkill; then
    rfkill unblock wifi >/dev/null 2>&1 || true
  fi
  ip link set "${interface}" up >/dev/null 2>&1 || return 1
  scan_output=$(lw_run_bounded 20 iw dev "${interface}" scan 2>/dev/null) || return 1
  while IFS= read -r line; do
    visible_ssid=$(printf '%s\n' "${line}" | sed -n 's/^[[:space:]]*SSID:[[:space:]]*//p')
    [ -n "${visible_ssid}" ] || continue
    if [ "${visible_ssid}" = "${requested_ssid}" ]; then
      return 0
    fi
  done <<EOF_SCAN
${scan_output}
EOF_SCAN
  return 1
)

lw_connect_wifi() (
  interface=$1
  ssid=$2
  psk=$3
  security=$4
  state_dir=/run/debian-usb-live-wifi
  config_file=${state_dir}/wpa_supplicant.conf
  pid_file=${state_dir}/wpa_supplicant-${interface}.pid

  lw_have ip || {
    lw_warn "ip command is unavailable; cannot configure ${interface}"
    return 1
  }
  ip link set "${interface}" up || return 1

  mkdir -p -- "${state_dir}" || return 1
  chmod 0700 "${state_dir}" || return 1
  lw_build_wpa_config "${config_file}" "${ssid}" "${psk}" "${security}" || return 1
  if ! lw_wpa_completed "${interface}"; then
    lw_have wpa_supplicant || {
      lw_warn "wpa_supplicant is unavailable; cannot connect ${interface} to configured Wi-Fi"
      return 1
    }
    if ! lw_run_bounded 20 wpa_supplicant -B -i "${interface}" -c "${config_file}" -P "${pid_file}" >/dev/null 2>&1; then
      if lw_have wpa_cli; then
        wpa_cli -i "${interface}" reconfigure >/dev/null 2>&1 || true
      fi
    fi
  fi

  attempt=1
  while [ "${attempt}" -le 30 ]; do
    if lw_wpa_completed "${interface}"; then
      return 0
    fi
    sleep 1
    attempt=$((attempt + 1))
  done
  return 1
)

lw_configure_address() (
  interface=$1
  cidr=$2
  gateway=$3

  if [ -n "${cidr}" ]; then
    normalized_cidr=$(lw_normalize_cidr "${cidr}") || {
      lw_warn "invalid static Wi-Fi CIDR, expected IPv4 address/prefix such as 192.168.50.43/24: ${cidr}"
      return 1
    }
    derived_netmask=$(lw_cidr_netmask "${normalized_cidr}") || return 1
    lw_log "using static Wi-Fi address ${normalized_cidr} with derived netmask ${derived_netmask}"
    ip addr replace "${normalized_cidr}" dev "${interface}" || return 1
  else
    if lw_have dhcpcd; then
      lw_run_bounded 45 dhcpcd -4 -1 -L -p --waitip=4 "${interface}" >/dev/null 2>&1 || return 1
    elif lw_have dhclient; then
      lw_run_bounded 45 dhclient -1 -v "${interface}" >/dev/null 2>&1 || return 1
    elif lw_have udhcpc; then
      lw_run_bounded 45 udhcpc -q -n -i "${interface}" >/dev/null 2>&1 || return 1
    fi
  fi

  if [ -n "${gateway}" ]; then
    lw_valid_ipv4 "${gateway}" || {
      lw_warn "ignoring invalid Wi-Fi gateway: ${gateway}"
      return 0
    }
    ip -4 route del default dev "${interface}" >/dev/null 2>&1 || true
    ip -4 route add default via "${gateway}" dev "${interface}" onlink metric 600 || true
  fi
  lw_reprioritize_wifi_default_route "${interface}"
)

lw_reprioritize_wifi_default_route() (
  interface=$1
  route=$(ip -4 route show default dev "${interface}" 2>/dev/null | head -n 1 || true)
  [ -n "${route}" ] || return 0
  gateway=
  old_ifs=${IFS}
  IFS=' '
  set -f
  # Intentional splitting: iproute2 route output is tokenized to locate "via".
  # shellcheck disable=SC2086
  set -- ${route}
  set +f
  IFS=${old_ifs}
  previous=
  for token in "$@"; do
    if [ "${previous}" = via ]; then
      gateway=${token}
      break
    fi
    previous=${token}
  done
  if [ -n "${gateway}" ]; then
    lw_valid_ipv4 "${gateway}" || return 0
  fi
  ip -4 route del default dev "${interface}" >/dev/null 2>&1 || true
  if [ -n "${gateway}" ]; then
    ip -4 route add default via "${gateway}" dev "${interface}" metric 600 >/dev/null 2>&1 || true
  else
    ip -4 route add default dev "${interface}" metric 600 >/dev/null 2>&1 || true
  fi
)

lw_has_other_default_route() (
  interface=$1
  [ -r /proc/net/route ] || return 1
  while read -r route_iface destination _gateway _flags _rest; do
    [ "${route_iface}" != Iface ] || continue
    if [ "${destination}" = 00000000 ] && [ "${route_iface}" != "${interface}" ]; then
      return 0
    fi
  done </proc/net/route
  return 1
)

lw_configure_nameservers() (
  interface=$1
  nameservers=$2
  servers=$(lw_nameserver_lines "${nameservers}")
  [ -n "${servers}" ] || return 0

  old_ifs=${IFS}
  IFS=$(printf '\n\t')
  set -f
  # Intentional splitting: the validator emits one address per line.
  # shellcheck disable=SC2086
  set -- ${servers}
  set +f
  IFS=${old_ifs}
  if lw_have resolvectl && resolvectl dns "${interface}" "$@" >/dev/null 2>&1; then
    if lw_has_other_default_route "${interface}"; then
      resolvectl default-route "${interface}" no >/dev/null 2>&1 || true
    else
      resolvectl default-route "${interface}" yes >/dev/null 2>&1 || true
    fi
    return 0
  fi

  if lw_has_other_default_route "${interface}"; then
    lw_log "keeping the existing resolver because another connected interface owns a default route"
    return 0
  fi
  if [ -e /etc/resolv.conf ] && [ ! -L /etc/resolv.conf ]; then
    cp -a -- /etc/resolv.conf /etc/resolv.conf.debian-usb-live-wifi.bak 2>/dev/null || true
  fi
  {
    for server in "$@"; do
      printf 'nameserver %s\n' "${server}"
    done
  } >/etc/resolv.conf
)

lw_main() (
  enabled=$(lw_config_value LIVE_WIFI_ENABLED live_wifi live_wifi_enabled 2>/dev/null || printf '1')
  case ${enabled} in
    0|false|False|FALSE|no|No|NO|off|Off|OFF)
      lw_log "disabled by live_wifi kernel argument"
      return 0
      ;;
  esac

  ssid=$(lw_config_encoded_value LIVE_WIFI_SSID live_wifi_essid_b64 live_wifi_ssid live_wifi_essid netcfg/wireless_essid 2>/dev/null || true)
  if [ -z "${ssid}" ]; then
    lw_log "no live Wi-Fi SSID configured; skipping"
    return 0
  fi
  if ! lw_valid_wifi_text "${ssid}"; then
    lw_warn "configured Wi-Fi SSID contains control characters; skipping"
    return 0
  fi
  ssid_bytes=$(lw_text_byte_length "${ssid}")
  if [ "${ssid_bytes}" -gt 32 ]; then
    lw_warn "configured Wi-Fi SSID exceeds 32 bytes; skipping"
    return 0
  fi

  interface_request=$(lw_config_value LIVE_WIFI_INTERFACE live_wifi_interface live_wifi_iface netcfg/choose_interface interface 2>/dev/null || printf 'auto')
  security=$(lw_config_value LIVE_WIFI_SECURITY live_wifi_security netcfg/wireless_security_type 2>/dev/null || printf 'wpa')
  security=$(lw_validate_security "${security}") || {
    lw_warn "unsupported live Wi-Fi security mode: ${security}"
    return 0
  }
  if [ "${security}" = open ]; then
    psk=
  else
    psk=$(lw_live_wifi_passphrase 2>/dev/null || true)
  fi
  cidr=$(lw_config_value LIVE_WIFI_CIDR live_wifi_cidr 2>/dev/null || true)
  nameservers=$(lw_config_value LIVE_WIFI_NAMESERVERS live_wifi_nameservers 2>/dev/null || true)
  gateway=$(lw_config_value LIVE_WIFI_GATEWAY live_wifi_gateway netcfg/get_gateway 2>/dev/null || true)
  if [ "${security}" != open ] && [ -z "${psk}" ]; then
    lw_warn "live Wi-Fi security ${security} requires a PSK; skipping"
    return 0
  fi
  if [ "${security}" != open ] && ! lw_valid_wifi_text "${psk}"; then
    lw_warn "live Wi-Fi passphrase contains control characters; skipping"
    return 0
  fi
  psk_bytes=$(lw_text_byte_length "${psk}")
  if [ "${security}" = wpa ] && ! lw_is_hex_psk "${psk}" && { [ "${psk_bytes}" -lt 8 ] || [ "${psk_bytes}" -gt 63 ]; }; then
    lw_warn "WPA2 passphrase must contain 8 to 63 characters, or be a 64-digit hexadecimal PSK; skipping"
    return 0
  fi
  if [ "${security}" = sae ] && { [ "${psk_bytes}" -lt 1 ] || [ "${psk_bytes}" -gt 63 ]; }; then
    lw_warn "WPA3 SAE passphrase must contain 1 to 63 characters; skipping"
    return 0
  fi

  lw_ensure_required_tools "${interface_request}" "${cidr}" || return 0
  interface=$(lw_resolve_interface "${interface_request}") || {
    lw_warn "no usable Wi-Fi interface found for request: ${interface_request}"
    return 0
  }
  if ! lw_ssid_visible "${interface}" "${ssid}"; then
    lw_log "configured Wi-Fi network is not visible; leaving Ethernet and existing links unchanged"
    return 0
  fi

  lw_log "configuring Wi-Fi on ${interface}"
  lw_connect_wifi "${interface}" "${ssid}" "${psk}" "${security}" || {
    lw_warn "Wi-Fi association did not complete on ${interface}"
    return 0
  }
  lw_configure_address "${interface}" "${cidr}" "${gateway}" || {
    lw_warn "Wi-Fi address configuration did not complete on ${interface}"
    return 0
  }
  lw_configure_nameservers "${interface}" "${nameservers}" || {
    lw_warn "Wi-Fi resolver configuration did not complete on ${interface}"
    return 0
  }
  lw_log "Wi-Fi configuration completed on ${interface}"
)

if [ "${DEBIAN_USB_HOOK_SOURCE_ONLY:-0}" != 1 ]; then
  lw_main "$@" || lw_warn "unexpected live Wi-Fi hook failure"
  exit 0
fi
