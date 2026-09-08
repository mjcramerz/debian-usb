#!/bin/sh
set -eu
IFS=$(printf '\n\t')

repo_root=$(CDPATH='' cd -P "$(dirname "$0")/../.." && pwd -P)
DEBIAN_USB_HOOK_SOURCE_ONLY=1
export DEBIAN_USB_HOOK_SOURCE_ONLY
# shellcheck disable=SC1091
. "${repo_root}/config-hooks/1000-network-wifi.sh"

fail() {
  printf 'test_live_wifi_hook: %s\n' "$*" >&2
  exit 1
}

temp_root=$(mktemp -d)
trap 'rm -rf -- "${temp_root}"' 0 INT TERM
fake_bin=${temp_root}/bin
command_log=${temp_root}/commands.log
scan_file=${temp_root}/scan.txt
mkdir -p -- "${fake_bin}"
: >"${command_log}"

cat >"${fake_bin}/ip" <<'EOF_IP'
#!/bin/sh
printf 'ip %s\n' "$*" >>"${LW_TEST_COMMAND_LOG}"
if [ "$*" = '-4 route show default dev wlan-test' ]; then
  printf '%s\n' 'default via 192.0.2.1 dev wlan-test proto dhcp metric 50'
fi
EOF_IP
cat >"${fake_bin}/iw" <<'EOF_IW'
#!/bin/sh
printf 'iw %s\n' "$*" >>"${LW_TEST_COMMAND_LOG}"
if [ "$*" = 'dev wlan-test scan' ]; then
  cat "${LW_TEST_SCAN_FILE}"
elif [ "$*" = 'dev wlan-test link' ]; then
  printf '%s\n' 'Connected to 00:11:22:33:44:55 (on wlan-test)'
fi
EOF_IW
for command in rfkill wpa_supplicant dhcpcd resolvectl; do
  cat >"${fake_bin}/${command}" <<'EOF_COMMAND'
#!/bin/sh
printf '%s %s\n' "$(basename "$0")" "$*" >>"${LW_TEST_COMMAND_LOG}"
EOF_COMMAND
done
cat >"${fake_bin}/wpa_cli" <<'EOF_WPACLI'
#!/bin/sh
printf 'wpa_cli %s\n' "$*" >>"${LW_TEST_COMMAND_LOG}"
printf '%s\n' 'wpa_state=COMPLETED'
EOF_WPACLI
chmod 0755 "${fake_bin}"/*

PATH=${fake_bin}:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
LW_TEST_COMMAND_LOG=${command_log}
LW_TEST_SCAN_FILE=${scan_file}
export PATH LW_TEST_COMMAND_LOG LW_TEST_SCAN_FILE

live_env=${temp_root}/live.env
cat >"${live_env}" <<'EOF_LIVE_ENV'
# Only explicit LIVE_WIFI_* assignments are consumed.
UNRELATED='ignored'
LIVE_WIFI_INTERFACE='wlan-test'
LIVE_WIFI_ESSID='Install Net'
LIVE_WIFI_SECURITY='wpa'
LIVE_WIFI_CIDR='192.0.2.44/24'
LIVE_WIFI_GATEWAY='192.0.2.1'
LIVE_WIFI_NAMESERVERS='192.0.2.53,198.51.100.53'
LIVE_WIFI_PASSPHRASE='123456789222aER$'
EOF_LIVE_ENV
chmod 0600 "${live_env}"

[ "$(lw_assignment_value "${live_env}" LIVE_WIFI_INTERFACE)" = wlan-test ] || fail "Live interface parsing failed"
[ "$(lw_assignment_value "${live_env}" LIVE_WIFI_ESSID)" = 'Install Net' ] || fail "Live ESSID parsing failed"
[ "$(lw_assignment_value "${live_env}" LIVE_WIFI_PASSPHRASE)" = '123456789222aER$' ] || fail "Live passphrase special-character parsing failed"
if lw_assignment_value "${live_env}" PRESEED_WIFI_PASSPHRASE >/dev/null 2>&1; then
  fail "legacy PRESEED_WIFI_PASSPHRASE was accepted for Debian Live"
fi

duplicate_env=${temp_root}/duplicate.env
printf '%s\n' "LIVE_WIFI_ESSID='one'" "LIVE_WIFI_ESSID='two'" >"${duplicate_env}"
if lw_assignment_value "${duplicate_env}" LIVE_WIFI_ESSID >/dev/null 2>&1; then
  fail "duplicate Live Wi-Fi assignments were accepted"
fi
ln -s -- "${live_env}" "${temp_root}/live-link.env"
DEBIAN_USB_LIVE_ENV_PATH=${temp_root}/live-link.env
export DEBIAN_USB_LIVE_ENV_PATH
if lw_live_env_path >/dev/null 2>&1; then
  fail "symlinked Live Wi-Fi environment was accepted"
fi
DEBIAN_USB_LIVE_ENV_PATH=relative/live.env
export DEBIAN_USB_LIVE_ENV_PATH
if lw_live_env_path >/dev/null 2>&1; then
  fail "relative Live Wi-Fi environment path was accepted"
fi
DEBIAN_USB_LIVE_ENV_PATH=${live_env}
export DEBIAN_USB_LIVE_ENV_PATH
[ "$(lw_live_env_path)" = "${live_env}" ] || fail "explicit Live Wi-Fi environment path was not selected"
[ "$(lw_live_wifi_passphrase "${live_env}")" = '123456789222aER$' ] || fail "LIVE_WIFI_PASSPHRASE handoff failed"

if grep -Eq 'lw_cmdline_value|live_wifi_essid_b64|netcfg/wireless_essid' "${repo_root}/config-hooks/1000-network-wifi.sh"; then
  fail "Wi-Fi hook retained a kernel-command-line configuration path"
fi

missing_packages=$({
  # shellcheck disable=SC2329  # Override consumed by sourced functions.
  lw_have() {
    case $1 in ip|iw|wpa_supplicant) return 0 ;; *) return 1 ;; esac
  }
  lw_collect_missing_packages auto ''
})
[ "${missing_packages}" = dhcpcd-base ] || fail "missing DHCP client did not select dhcpcd-base"

[ "$(lw_text_byte_length 'Install Net')" = 11 ] || fail "Wi-Fi byte length calculation failed"
tab=$(printf '\t')
if lw_valid_wifi_text "Install${tab}Net"; then
  fail "Wi-Fi control-character validation accepted a tab"
fi
lw_valid_ipv4 192.0.2.1 || fail "valid IPv4 address was rejected"
for invalid_ipv4 in 256.0.2.1 192.nope.2.1 192.0.2 192.0.2.1.9; do
  if lw_valid_ipv4 "${invalid_ipv4}"; then
    fail "invalid IPv4 address was accepted: ${invalid_ipv4}"
  fi
done

open_config=${temp_root}/open.conf
wpa_config=${temp_root}/wpa.conf
sae_config=${temp_root}/sae.conf
lw_build_wpa_config "${open_config}" InstallNet ignored-secret open
lw_build_wpa_config "${wpa_config}" 'Install Net' '123456789222aER$' wpa
lw_build_wpa_config "${sae_config}" InstallNet SafePass123 sae

grep -Fqx '    key_mgmt=NONE' "${open_config}" || fail "open network did not use key_mgmt=NONE"
! grep -Fq 'ignored-secret' "${open_config}" || fail "open network retained an ignored passphrase"
grep -Fqx '    key_mgmt=WPA-PSK' "${wpa_config}" || fail "WPA2 network did not use WPA-PSK"
grep -Fqx '    proto=RSN' "${wpa_config}" || fail "WPA2 network was not constrained to RSN"
grep -Fqx '    ssid="Install Net"' "${wpa_config}" || fail "WPA2 ESSID whitespace was not preserved"
grep -Fqx '    psk="123456789222aER$"' "${wpa_config}" || fail "WPA2 passphrase special characters were not preserved"
grep -Fqx '    key_mgmt=SAE' "${sae_config}" || fail "WPA3 network did not use SAE"
grep -Fqx '    ieee80211w=2' "${sae_config}" || fail "WPA3 network did not require management-frame protection"
[ "$(stat -c '%a' "${open_config}")" = 600 ] || fail "Wi-Fi config mode is not 0600"

cat >"${scan_file}" <<'EOF_SCAN'
BSS 00:11:22:33:44:55(on wlan-test)
        SSID: OtherNetwork
BSS 66:77:88:99:aa:bb(on wlan-test)
        SSID: Install Net
EOF_SCAN
lw_ssid_visible wlan-test 'Install Net' || fail "visible exact ESSID was not detected"
if lw_ssid_visible wlan-test MissingNetwork; then
  fail "missing ESSID was reported as visible"
fi

: >"${command_log}"
lw_configure_address wlan-test 192.0.2.44/24 192.0.2.1
route_log=$(cat "${command_log}")
printf '%s\n' "${route_log}" | grep -Fq 'ip addr replace 192.0.2.44/24 dev wlan-test' || fail "static Wi-Fi address was not configured"
printf '%s\n' "${route_log}" | grep -Fq 'ip -4 route add default via 192.0.2.1 dev wlan-test metric 50' || fail "Wi-Fi default route did not receive metric 50"
! printf '%s\n' "${route_log}" | grep -Fq eth || fail "Wi-Fi routing touched an Ethernet interface"

: >"${command_log}"
lw_configure_address wlan-test '' ''
dhcp_log=$(cat "${command_log}")
printf '%s\n' "${dhcp_log}" | grep -Fq 'dhcpcd -4 -1 -L -p --waitip=4 wlan-test' || fail "dynamic Wi-Fi address did not use dhcpcd one-shot IPv4 mode"

: >"${command_log}"
# shellcheck disable=SC2329
lw_has_other_default_route() { return 0; }
lw_configure_nameservers wlan-test '192.0.2.53,198.51.100.53'
resolver_log=$(cat "${command_log}")
printf '%s\n' "${resolver_log}" | grep -Fq 'resolvectl dns wlan-test 192.0.2.53 198.51.100.53' || fail "per-link DNS was not configured"
printf '%s\n' "${resolver_log}" | grep -Fq 'resolvectl default-route wlan-test yes' || fail "Wi-Fi DNS did not take priority"

# Full automatic path: all values must come from the private file.
auto_marker=${temp_root}/automatic-connect
address_marker=${temp_root}/automatic-address
resolver_marker=${temp_root}/automatic-resolver
# shellcheck disable=SC2329
lw_ensure_required_tools() { return 0; }
# shellcheck disable=SC2329
lw_resolve_interface() {
  [ "$1" = wlan-test ] || return 1
  printf '%s\n' wlan-test
}
# shellcheck disable=SC2329
lw_ssid_visible() {
  [ "$1" = wlan-test ] && [ "$2" = 'Install Net' ]
}
# shellcheck disable=SC2329
lw_connect_wifi() {
  [ "$1" = wlan-test ] && [ "$2" = 'Install Net' ] && [ "$3" = '123456789222aER$' ] && [ "$4" = wpa ] || return 1
  : >"${auto_marker}"
}
# shellcheck disable=SC2329
lw_configure_address() {
  [ "$1" = wlan-test ] && [ "$2" = 192.0.2.44/24 ] && [ "$3" = 192.0.2.1 ] || return 1
  : >"${address_marker}"
}
# shellcheck disable=SC2329
lw_configure_nameservers() {
  [ "$1" = wlan-test ] && [ "$2" = 192.0.2.53,198.51.100.53 ] || return 1
  : >"${resolver_marker}"
}
lw_main
[ -f "${auto_marker}" ] || fail "private Live environment did not trigger automatic Wi-Fi association"
[ -f "${address_marker}" ] || fail "private Live environment did not apply configured addressing"
[ -f "${resolver_marker}" ] || fail "private Live environment did not apply configured nameservers"

# Missing ESSID must remain a harmless no-op.
missing_essid_env=${temp_root}/missing-essid.env
printf '%s\n' "LIVE_WIFI_INTERFACE='wlan-test'" "LIVE_WIFI_SECURITY='open'" >"${missing_essid_env}"
chmod 0600 "${missing_essid_env}"
DEBIAN_USB_LIVE_ENV_PATH=${missing_essid_env}
export DEBIAN_USB_LIVE_ENV_PATH
rm -f -- "${auto_marker}"
lw_main
[ ! -e "${auto_marker}" ] || fail "missing ESSID unexpectedly attempted association"
