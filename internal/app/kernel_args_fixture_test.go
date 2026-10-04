package app

import (
	"strings"
	"testing"
)

// Historical blacklist entries are regression data, not runtime policy.
var previouslyRestrictedArgNamesForTest = []string{
	"cf_r2_access_key",
	"cf_r2_secret_key",
	"crowdsec_token",
	"fruux_password",
	"fruux_username",
	"obs_password",
	"obs_username",
	"primary_gpg_passphrase",
	"primary_password",
	"primary_user",
	"root_password",
	"tailscale_authkey",
	"telegram_api_key",
	"telegram_chat_id",
	"DEFAULT_LIVE_WIFI_CIDR",
	"DEFAULT_LIVE_WIFI_ESSID",
	"DEFAULT_LIVE_WIFI_GATEWAY",
	"DEFAULT_LIVE_WIFI_INTERFACE",
	"DEFAULT_LIVE_WIFI_NAMESERVERS",
	"DEFAULT_LIVE_WIFI_PSK",
	"DEFAULT_LIVE_WIFI_SECURITY",
	"LIVE_WIFI_CIDR",
	"LIVE_WIFI_ESSID",
	"LIVE_WIFI_GATEWAY",
	"LIVE_WIFI_INTERFACE",
	"LIVE_WIFI_NAMESERVERS",
	"LIVE_WIFI_PASSPHRASE",
	"LIVE_WIFI_SECURITY",
	"PRESEED_WIFI_PASSPHRASE",
	"live_wifi",
	"live_wifi_cidr",
	"live_wifi_enabled",
	"live_wifi_essid",
	"live_wifi_essid_b64",
	"live_wifi_gateway",
	"live_wifi_iface",
	"live_wifi_interface",
	"live_wifi_nameservers",
	"live_wifi_psk",
	"live_wifi_psk_b64",
	"live_wifi_security",
	"live_wifi_ssid",
	"live_wifi_wpa",
	"netcfg/choose_interface",
	"netcfg/wireless_essid",
	"netcfg/wireless_security_type",
	"netcfg/wireless_wpa",
}

const preseedNetworkArgsForTest = "netcfg/choose_interface=wlan0 netcfg/wireless_essid=Fixture_5Ghz netcfg/wireless_essid_again=Fixture_5Ghz netcfg/wireless_security_type=wpa netcfg/wireless_wpa=Example_1122!! netcfg/get_ipaddress=192.0.2.90 ipv6_address=2001:db8::90/64 classes=prod;desktop;standard custom/preseed_option=fixture"

func assertUserArgsPreservedForTest(t *testing.T, got, args string) {
	t.Helper()
	tokens := make(map[string]bool)
	for _, token := range strings.Fields(got) {
		tokens[token] = true
	}
	for _, token := range strings.Fields(args) {
		if !tokens[token] {
			t.Errorf("explicit argument %q was removed", token)
		}
	}
}
