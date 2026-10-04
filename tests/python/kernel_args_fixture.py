"""User-supplied argument names previously rejected or silently stripped."""

LEGACY_SECRET_KERNEL_ARG_NAMES = (
    'cf_r2_access_key',
    'cf_r2_secret_key',
    'crowdsec_token',
    'fruux_password',
    'fruux_username',
    'obs_password',
    'obs_username',
    'primary_gpg_passphrase',
    'primary_password',
    'primary_user',
    'root_password',
    'tailscale_authkey',
    'telegram_api_key',
    'telegram_chat_id',
)

LIVE_WIFI_SECRET_KERNEL_ARG_NAMES = (
    'DEFAULT_LIVE_WIFI_CIDR',
    'DEFAULT_LIVE_WIFI_ESSID',
    'DEFAULT_LIVE_WIFI_GATEWAY',
    'DEFAULT_LIVE_WIFI_INTERFACE',
    'DEFAULT_LIVE_WIFI_NAMESERVERS',
    'DEFAULT_LIVE_WIFI_PSK',
    'DEFAULT_LIVE_WIFI_SECURITY',
    'LIVE_WIFI_CIDR',
    'LIVE_WIFI_ESSID',
    'LIVE_WIFI_GATEWAY',
    'LIVE_WIFI_INTERFACE',
    'LIVE_WIFI_NAMESERVERS',
    'LIVE_WIFI_PASSPHRASE',
    'LIVE_WIFI_SECURITY',
    'PRESEED_WIFI_PASSPHRASE',
    'live_wifi',
    'live_wifi_cidr',
    'live_wifi_enabled',
    'live_wifi_essid',
    'live_wifi_essid_b64',
    'live_wifi_gateway',
    'live_wifi_iface',
    'live_wifi_interface',
    'live_wifi_nameservers',
    'live_wifi_psk',
    'live_wifi_psk_b64',
    'live_wifi_security',
    'live_wifi_ssid',
    'live_wifi_wpa',
    'netcfg/choose_interface',
    'netcfg/wireless_essid',
    'netcfg/wireless_security_type',
    'netcfg/wireless_wpa',
)

USER_KERNEL_ARG_NAMES = LEGACY_SECRET_KERNEL_ARG_NAMES + LIVE_WIFI_SECRET_KERNEL_ARG_NAMES

PRESEED_NETWORK_ARGS = (
    "netcfg/choose_interface=wlan0 netcfg/wireless_essid=Fixture_5Ghz "
    "netcfg/wireless_essid_again=Fixture_5Ghz netcfg/wireless_security_type=wpa "
    "netcfg/wireless_wpa=Example_1122!! netcfg/get_ipaddress=192.0.2.90 "
    "ipv6_address=2001:db8::90/64 classes=prod;desktop;standard "
    "custom/preseed_option=fixture"
)
