package app

import (
	"bufio"
	"fmt"
	"net"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
)

type configSection struct {
	Title    string
	Comments []string
	Keys     []string
}

var (
	liveOverrideProfiles       = []string{profileDebian, profileUbuntuDesktop, profileUbuntuServer, profileKaliLinux, profileTails}
	installerOverrideProfiles  = []string{profileDebian, profileUbuntuDesktop, profileUbuntuServer, profileKaliLinux, profileKaliPurple}
	forensicsOverrideProfiles  = []string{profileDebian, profileUbuntuDesktop, profileUbuntuServer, profileKaliLinux, profileTails}
	fallbackLiveKernelProfiles = []string{profileDebian, profileUbuntuDesktop, profileUbuntuServer, profileKaliLinux, profileTails}
	preseedURLProfiles         = []string{profileDebian, profileKaliLinux, profileKaliPurple}
	legacySecretKernelArgNames = map[string]struct{}{
		"fruux_username":         {},
		"fruux_password":         {},
		"primary_user":           {},
		"primary_password":       {},
		"primary_gpg_passphrase": {},
		"root_password":          {},
		"crowdsec_token":         {},
		"tailscale_authkey":      {},
		"telegram_chat_id":       {},
		"telegram_api_key":       {},
		"cf_r2_access_key":       {},
		"cf_r2_secret_key":       {},
		"obs_username":           {},
		"obs_password":           {},
	}
	liveWifiSecretKernelArgNames = map[string]struct{}{
		"DEFAULT_LIVE_WIFI_PSK":   {},
		"PRESEED_WIFI_PASSPHRASE": {},
		"live_wifi_psk":           {},
		"live_wifi_psk_b64":       {},
		"live_wifi_wpa":           {},
		"netcfg/wireless_wpa":     {},
	}
	legacyKeyAliases = map[string]string{
		"KALI_LIVE_KERNEL_EXTRAS":      "KALI_LINUX_LIVE_KERNEL_EXTRAS",
		"KALI_INSTALLER_KERNEL_EXTRAS": "KALI_LINUX_INSTALLER_KERNEL_EXTRAS",
		"DEBIAN_PRESEED_URL":           "DEBIAN_PRESEED_INTERNAL_URL",
		"KALI_PRESEED_URL":             "KALI_LINUX_PRESEED_INTERNAL_URL",
		"KALI_LINUX_PRESEED_URL":       "KALI_LINUX_PRESEED_INTERNAL_URL",
		"KALI_PURPLE_PRESEED_URL":      "KALI_PURPLE_PRESEED_INTERNAL_URL",
	}
)

func loadRuntimeConfig(path string) (RuntimeConfig, error) {
	raw, err := parseConfigFile(path)
	if err != nil {
		return RuntimeConfig{}, err
	}
	normalized, err := normalizeConfigMap(raw)
	if err != nil {
		return RuntimeConfig{}, err
	}
	return runtimeConfigFromMap(path, normalized), nil
}

func saveRuntimeConfig(path string, cfg RuntimeConfig) (RuntimeConfig, error) {
	normalized, err := normalizeConfigMap(configMapFromRuntimeConfig(cfg))
	if err != nil {
		return RuntimeConfig{}, err
	}
	lines := []string{
		"# Managed by debian-usb.",
		"# This file is the install-time source of truth for managed runtime defaults.",
		"# Update values through the Settings menu unless you are doing a controlled edit.",
		"",
	}
	sections := runtimeConfigSections()
	for index, section := range sections {
		lines = append(lines, "# "+section.Title)
		for _, comment := range section.Comments {
			lines = append(lines, "# "+comment)
		}
		for _, key := range section.Keys {
			lines = append(lines, fmt.Sprintf(`%s="%s"`, key, escapeConfigValue(normalized[key])))
		}
		if index != len(sections)-1 {
			lines = append(lines, "")
		}
	}
	extraKeys := sortedExtraConfigKeys(normalized)
	if len(extraKeys) > 0 {
		if len(lines) > 0 && lines[len(lines)-1] != "" {
			lines = append(lines, "")
		}
		lines = append(lines, "# Additional preserved config keys")
		lines = append(lines, "# These values are loaded and rewritten even when they are not exposed in the Settings UI.")
		for _, key := range extraKeys {
			lines = append(lines, fmt.Sprintf(`%s="%s"`, key, escapeConfigValue(normalized[key])))
		}
	}
	if err := os.MkdirAll(filepath.Dir(path), 0755); err != nil {
		return RuntimeConfig{}, err
	}
	if err := os.WriteFile(path, []byte(strings.Join(lines, "\n")+"\n"), 0644); err != nil {
		return RuntimeConfig{}, err
	}
	if err := os.Chmod(path, 0644); err != nil {
		return RuntimeConfig{}, err
	}
	return runtimeConfigFromMap(path, normalized), nil
}

func parseConfigFile(path string) (map[string]string, error) {
	file, err := os.Open(path)
	if err != nil {
		return nil, err
	}
	defer file.Close()

	data := make(map[string]string)
	scanner := bufio.NewScanner(file)
	scanner.Buffer(make([]byte, 0, 64*1024), 1024*1024)
	for scanner.Scan() {
		line := strings.TrimSpace(scanner.Text())
		if line == "" || strings.HasPrefix(line, "#") {
			continue
		}
		key, value, found := strings.Cut(line, "=")
		if !found {
			continue
		}
		data[strings.TrimSpace(key)] = stripConfigQuotes(strings.TrimSpace(value))
	}
	if err := scanner.Err(); err != nil {
		return nil, err
	}
	return data, nil
}

func normalizeConfigMap(raw map[string]string) (map[string]string, error) {
	data := applyLegacyKeyAliases(raw)
	normalized := make(map[string]string, len(data))
	for key, value := range data {
		normalized[key] = value
	}

	required := requiredConfigKeys()
	optional := optionalEmptyKeys()
	for _, key := range required {
		value, exists := normalized[key]
		if exists && strings.TrimSpace(value) != "" {
			continue
		}
		if optional[key] {
			normalized[key] = ""
			continue
		}
		return nil, fmt.Errorf("missing required config key: %s", key)
	}

	appName := strings.TrimSpace(normalized["APP_NAME"])
	if appName == "" {
		return nil, fmt.Errorf("APP_NAME is required")
	}
	normalized["APP_NAME"] = appName

	appVersion := strings.TrimSpace(normalized["APP_VERSION"])
	if appVersion == "" {
		return nil, fmt.Errorf("APP_VERSION is required")
	}
	normalized["APP_VERSION"] = appVersion

	defaultPersistenceSize, err := normalizePositiveIntString(normalized["DEFAULT_PERSISTENCE_SIZE_GIB"], "DEFAULT_PERSISTENCE_SIZE_GIB")
	if err != nil {
		return nil, err
	}
	normalized["DEFAULT_PERSISTENCE_SIZE_GIB"] = defaultPersistenceSize

	defaultBootPolicy := strings.TrimSpace(normalized["DEFAULT_BOOT_POLICY"])
	if err := validateLiveBootPolicy(defaultBootPolicy); err != nil {
		return nil, err
	}
	normalized["DEFAULT_BOOT_POLICY"] = defaultBootPolicy

	defaultInstallerPolicy := strings.TrimSpace(normalized["DEFAULT_INSTALLER_POLICY"])
	if err := validateInstallerPolicy(defaultInstallerPolicy); err != nil {
		return nil, err
	}
	normalized["DEFAULT_INSTALLER_POLICY"] = defaultInstallerPolicy

	defaultToram, err := normalizeBoolString(normalized["DEFAULT_LIVE_TORAM"], "DEFAULT_LIVE_TORAM")
	if err != nil {
		return nil, err
	}
	normalized["DEFAULT_LIVE_TORAM"] = defaultToram

	defaultLiveMemGiB, err := normalizeNonNegativeIntString(normalized["DEFAULT_LIVE_MEM_GIB"], "DEFAULT_LIVE_MEM_GIB")
	if err != nil {
		return nil, err
	}
	normalized["DEFAULT_LIVE_MEM_GIB"] = defaultLiveMemGiB
	defaultLiveHooks, err := normalizeBoolString(normalized["DEFAULT_LIVE_HOOKS"], "DEFAULT_LIVE_HOOKS")
	if err != nil {
		return nil, err
	}
	normalized["DEFAULT_LIVE_HOOKS"] = defaultLiveHooks
	normalized["DEFAULT_LIVE_ARGS_HOOKS"] = collapseWhitespace(normalized["DEFAULT_LIVE_ARGS_HOOKS"])
	defaultLiveWifiInterface, err := normalizeSingleTokenString(normalized["DEFAULT_LIVE_WIFI_INTERFACE"], "DEFAULT_LIVE_WIFI_INTERFACE")
	if err != nil {
		return nil, err
	}
	normalized["DEFAULT_LIVE_WIFI_INTERFACE"] = defaultLiveWifiInterface
	defaultLiveWifiESSID, err := normalizeLiveWifiTextString(normalized["DEFAULT_LIVE_WIFI_ESSID"], "DEFAULT_LIVE_WIFI_ESSID", 32)
	if err != nil {
		return nil, err
	}
	normalized["DEFAULT_LIVE_WIFI_ESSID"] = defaultLiveWifiESSID
	defaultLiveWifiSecurity, err := validateLiveWifiSecurityString(normalized["DEFAULT_LIVE_WIFI_SECURITY"])
	if err != nil {
		return nil, err
	}
	normalized["DEFAULT_LIVE_WIFI_SECURITY"] = defaultLiveWifiSecurity
	defaultLiveWifiCIDR, err := normalizeIPv4CIDRString(normalized["DEFAULT_LIVE_WIFI_CIDR"], "DEFAULT_LIVE_WIFI_CIDR")
	if err != nil {
		return nil, err
	}
	normalized["DEFAULT_LIVE_WIFI_CIDR"] = defaultLiveWifiCIDR
	defaultLiveWifiGateway, err := normalizeSingleTokenString(normalized["DEFAULT_LIVE_WIFI_GATEWAY"], "DEFAULT_LIVE_WIFI_GATEWAY")
	if err != nil {
		return nil, err
	}
	normalized["DEFAULT_LIVE_WIFI_GATEWAY"] = defaultLiveWifiGateway
	normalized["DEFAULT_LIVE_WIFI_NAMESERVERS"] = normalizeCommaListString(normalized["DEFAULT_LIVE_WIFI_NAMESERVERS"])

	for _, key := range partitionLabelConfigKeys {
		value, err := normalizePartitionLabel(normalized[key], key)
		if err != nil {
			return nil, err
		}
		normalized[key] = value
	}

	for _, key := range kernelArgKeys() {
		normalized[key] = collapseWhitespace(normalized[key])
		if err := validateNoLegacySecretKernelArgs(normalized[key], key); err != nil {
			return nil, err
		}
	}
	for key, value := range normalized {
		if !isAdditionalKernelArgConfigKey(key) {
			continue
		}
		normalized[key] = collapseWhitespace(value)
		if err := validateNoLegacySecretKernelArgs(normalized[key], key); err != nil {
			return nil, err
		}
	}
	for _, profile := range preseedURLProfiles {
		key := profileConfigKey(profile, "PRESEED_INTERNAL_URL")
		value, err := normalizeOptionalURLString(normalized[key])
		if err != nil {
			return nil, err
		}
		normalized[key] = value
	}
	publicPreseedURL, err := normalizeOptionalURLString(normalized["DEBIAN_PRESEED_PUBLIC_URL"])
	if err != nil {
		return nil, err
	}
	normalized["DEBIAN_PRESEED_PUBLIC_URL"] = publicPreseedURL
	for _, key := range managedSourceURLKeys() {
		value, err := normalizeOptionalURLString(normalized[key])
		if err != nil {
			return nil, fmt.Errorf("%s: %w", key, err)
		}
		normalized[key] = value
	}
	return normalized, nil
}

func runtimeConfigFromMap(path string, data map[string]string) RuntimeConfig {
	cfg := RuntimeConfig{
		AppName:                       data["APP_NAME"],
		AppVersion:                    data["APP_VERSION"],
		ConfigPath:                    path,
		DefaultBootPolicy:             data["DEFAULT_BOOT_POLICY"],
		DefaultInstallerPolicy:        data["DEFAULT_INSTALLER_POLICY"],
		SharedLiveBaseKernelArgs:      data["SHARED_LIVE_BASE_KERNEL_ARGS"],
		BootPolicyBalancedKernelArgs:  data["BOOT_POLICY_BALANCED_KERNEL_ARGS"],
		BootPolicyPerformanceArgs:     data["BOOT_POLICY_PERFORMANCE_KERNEL_ARGS"],
		BootPolicyHardenedArgs:        data["BOOT_POLICY_HARDENED_KERNEL_ARGS"],
		InstallerPolicyPreserveArgs:   data["INSTALLER_POLICY_PRESERVE_KERNEL_ARGS"],
		DefaultLiveKernelExtras:       data["DEFAULT_LIVE_KERNEL_EXTRAS"],
		DefaultInstallerKernelExtras:  data["DEFAULT_INSTALLER_KERNEL_EXTRAS"],
		DefaultForensicsKernelExtras:  data["DEFAULT_FORENSICS_KERNEL_EXTRAS"],
		DefaultLiveToram:              data["DEFAULT_LIVE_TORAM"] == "1",
		DefaultLiveHooks:              data["DEFAULT_LIVE_HOOKS"] == "1",
		DefaultLiveArgsHooks:          data["DEFAULT_LIVE_ARGS_HOOKS"],
		DefaultLiveWifiInterface:      data["DEFAULT_LIVE_WIFI_INTERFACE"],
		DefaultLiveWifiESSID:          data["DEFAULT_LIVE_WIFI_ESSID"],
		DefaultLiveWifiSecurity:       data["DEFAULT_LIVE_WIFI_SECURITY"],
		DefaultLiveWifiCIDR:           data["DEFAULT_LIVE_WIFI_CIDR"],
		DefaultLiveWifiGateway:        data["DEFAULT_LIVE_WIFI_GATEWAY"],
		DefaultLiveWifiNameservers:    data["DEFAULT_LIVE_WIFI_NAMESERVERS"],
		DefaultPreseedPublicURL:       data["DEBIAN_PRESEED_PUBLIC_URL"],
		DefaultPreseedPublicArgs:      data["DEBIAN_PRESEED_PUBLIC_ARGS"],
		DefaultPreseedInternalArgs:    data["DEBIAN_PRESEED_INTERNAL_ARGS"],
		DefaultPartitionLabels:        partitionLabelValuesFromMap(data),
		ProfileFallbackLiveKernelArgs: make(map[string]string, len(profileOrder)),
		ProfileLiveKernelExtras:       make(map[string]string, len(profileOrder)),
		ProfileForensicsKernelExtras:  make(map[string]string, len(profileOrder)),
		ProfileInstallerKernelExtras:  make(map[string]string, len(profileOrder)),
		ProfilePreseedURLs:            make(map[string]string, len(profileOrder)),
		ManagedSourceURLs:             make(map[string]string, len(managedSourceURLOrder)),
		ExtraValues:                   extraConfigValues(data),
	}
	cfg.DefaultPersistenceSizeGiB, _ = strconv.Atoi(data["DEFAULT_PERSISTENCE_SIZE_GIB"])
	cfg.DefaultLiveMemGiB, _ = strconv.Atoi(data["DEFAULT_LIVE_MEM_GIB"])
	for _, profile := range profileOrder {
		cfg.ProfileFallbackLiveKernelArgs[profile] = strings.TrimSpace(data[profileConfigKey(profile, "FALLBACK_LIVE_KERNEL_ARGS")])
		cfg.ProfileLiveKernelExtras[profile] = strings.TrimSpace(data[profileConfigKey(profile, "LIVE_KERNEL_EXTRAS")])
		cfg.ProfileForensicsKernelExtras[profile] = strings.TrimSpace(data[profileConfigKey(profile, "FORENSICS_KERNEL_EXTRAS")])
		cfg.ProfileInstallerKernelExtras[profile] = strings.TrimSpace(data[profileConfigKey(profile, "INSTALLER_KERNEL_EXTRAS")])
		cfg.ProfilePreseedURLs[profile] = strings.TrimSpace(data[profileConfigKey(profile, "PRESEED_INTERNAL_URL")])
	}
	for _, key := range managedSourceURLKeys() {
		cfg.ManagedSourceURLs[key] = strings.TrimSpace(data[key])
	}
	return cfg
}

func configMapFromRuntimeConfig(cfg RuntimeConfig) map[string]string {
	data := make(map[string]string, len(requiredConfigKeys())+len(cfg.ExtraValues))
	for key, value := range cfg.ExtraValues {
		data[key] = strings.TrimSpace(value)
	}
	data["APP_NAME"] = strings.TrimSpace(cfg.AppName)
	data["APP_VERSION"] = strings.TrimSpace(cfg.AppVersion)
	data["DEFAULT_PERSISTENCE_SIZE_GIB"] = strconv.Itoa(cfg.DefaultPersistenceSizeGiB)
	data["DEFAULT_BOOT_POLICY"] = strings.TrimSpace(cfg.DefaultBootPolicy)
	data["DEFAULT_INSTALLER_POLICY"] = strings.TrimSpace(cfg.DefaultInstallerPolicy)
	data["DEFAULT_LIVE_TORAM"] = boolFlag(cfg.DefaultLiveToram)
	data["DEFAULT_LIVE_MEM_GIB"] = strconv.Itoa(cfg.DefaultLiveMemGiB)
	data["DEFAULT_LIVE_HOOKS"] = boolFlag(cfg.DefaultLiveHooks)
	data["DEFAULT_LIVE_ARGS_HOOKS"] = collapseWhitespace(cfg.DefaultLiveArgsHooks)
	data["DEFAULT_LIVE_WIFI_INTERFACE"] = strings.TrimSpace(cfg.DefaultLiveWifiInterface)
	data["DEFAULT_LIVE_WIFI_ESSID"] = strings.TrimSpace(cfg.DefaultLiveWifiESSID)
	data["DEFAULT_LIVE_WIFI_SECURITY"] = strings.TrimSpace(cfg.DefaultLiveWifiSecurity)
	data["DEFAULT_LIVE_WIFI_CIDR"] = strings.TrimSpace(cfg.DefaultLiveWifiCIDR)
	data["DEFAULT_LIVE_WIFI_GATEWAY"] = strings.TrimSpace(cfg.DefaultLiveWifiGateway)
	data["DEFAULT_LIVE_WIFI_NAMESERVERS"] = normalizeCommaListString(cfg.DefaultLiveWifiNameservers)
	data["DEBIAN_PRESEED_PUBLIC_URL"] = strings.TrimSpace(cfg.DefaultPreseedPublicURL)
	data["DEBIAN_PRESEED_PUBLIC_ARGS"] = collapseWhitespace(cfg.DefaultPreseedPublicArgs)
	data["DEBIAN_PRESEED_INTERNAL_ARGS"] = collapseWhitespace(cfg.DefaultPreseedInternalArgs)
	for _, key := range partitionLabelConfigKeys {
		data[key] = partitionLabelValue(cfg, key)
	}
	data["SHARED_LIVE_BASE_KERNEL_ARGS"] = collapseWhitespace(cfg.SharedLiveBaseKernelArgs)
	data["BOOT_POLICY_BALANCED_KERNEL_ARGS"] = collapseWhitespace(cfg.BootPolicyBalancedKernelArgs)
	data["BOOT_POLICY_PERFORMANCE_KERNEL_ARGS"] = collapseWhitespace(cfg.BootPolicyPerformanceArgs)
	data["BOOT_POLICY_HARDENED_KERNEL_ARGS"] = collapseWhitespace(cfg.BootPolicyHardenedArgs)
	data["INSTALLER_POLICY_PRESERVE_KERNEL_ARGS"] = collapseWhitespace(cfg.InstallerPolicyPreserveArgs)
	data["DEFAULT_LIVE_KERNEL_EXTRAS"] = collapseWhitespace(cfg.DefaultLiveKernelExtras)
	data["DEFAULT_INSTALLER_KERNEL_EXTRAS"] = collapseWhitespace(cfg.DefaultInstallerKernelExtras)
	data["DEFAULT_FORENSICS_KERNEL_EXTRAS"] = collapseWhitespace(cfg.DefaultForensicsKernelExtras)
	for _, profile := range profileOrder {
		data[profileConfigKey(profile, "FALLBACK_LIVE_KERNEL_ARGS")] = collapseWhitespace(cfg.ProfileFallbackLiveKernelArgs[profile])
		data[profileConfigKey(profile, "LIVE_KERNEL_EXTRAS")] = collapseWhitespace(cfg.ProfileLiveKernelExtras[profile])
		data[profileConfigKey(profile, "FORENSICS_KERNEL_EXTRAS")] = collapseWhitespace(cfg.ProfileForensicsKernelExtras[profile])
		data[profileConfigKey(profile, "INSTALLER_KERNEL_EXTRAS")] = collapseWhitespace(cfg.ProfileInstallerKernelExtras[profile])
		data[profileConfigKey(profile, "PRESEED_INTERNAL_URL")] = strings.TrimSpace(cfg.ProfilePreseedURLs[profile])
	}
	for _, key := range managedSourceURLKeys() {
		data[key] = managedSourceURLValue(cfg, key)
	}
	return data
}

func runtimeConfigSections() []configSection {
	return []configSection{
		{
			Title: "Application identity",
			Comments: []string{
				"Install-time runtime identity written into the managed host config.",
			},
			Keys: []string{
				"APP_NAME",
				"APP_VERSION",
			},
		},
		{
			Title: "General defaults",
			Comments: []string{
				"Shared defaults used by the Settings menu and the managed USB planner.",
			},
			Keys: []string{
				"DEFAULT_PERSISTENCE_SIZE_GIB",
				"DEFAULT_BOOT_POLICY",
				"DEFAULT_INSTALLER_POLICY",
				"DEFAULT_LIVE_TORAM",
				"DEFAULT_LIVE_MEM_GIB",
			},
		},
		{
			Title: "Multi-OS and shared labels",
			Comments: []string{
				"These labels are used by both single-OS and Multi-OS flows where applicable.",
				"In the shared ISO-store Multi-OS flow, only DEFAULT_ESP_LABEL, DEFAULT_MULTI_DATA_LABEL, and the Debian/Kali/Tails persistence labels are used.",
				"DEFAULT_ESP_LABEL must fit the FAT volume-label limit; other labels must fit ext4 and GPT label use.",
			},
			Keys: multiOSPartitionLabelConfigKeys,
		},
		{
			Title: "Single-OS payload labels",
			Comments: []string{
				"These labels are only used by single-OS managed payload flows.",
				"Debian and Kali now keep separate netinst and netboot payload labels so those installer source roles do not reuse the same filesystem label.",
				"Ubuntu persistence keeps separate filesystem and GPT partition-name defaults for casper compatibility.",
			},
			Keys: singleOSPartitionLabelConfigKeys,
		},
		{
			Title: "Live config hooks",
			Comments: []string{
				"When DEFAULT_LIVE_HOOKS is enabled, Debian raw-ISO Live payloads receive repo-managed live-config hooks.",
				"Only non-secret DEFAULT_LIVE_WIFI_* values are appended to Live entries.",
				"The passphrase is read from PRESEED_WIFI_PASSPHRASE in the selected Debian Live initrd overlay.",
				"Supported Wi-Fi security modes are open, wpa, and sae; sae maps to WPA3/SAE.",
			},
			Keys: []string{
				"DEFAULT_LIVE_HOOKS",
				"DEFAULT_LIVE_ARGS_HOOKS",
				"DEFAULT_LIVE_WIFI_INTERFACE",
				"DEFAULT_LIVE_WIFI_ESSID",
				"DEFAULT_LIVE_WIFI_SECURITY",
				"DEFAULT_LIVE_WIFI_CIDR",
				"DEFAULT_LIVE_WIFI_GATEWAY",
				"DEFAULT_LIVE_WIFI_NAMESERVERS",
			},
		},
		{
			Title: "Live boot policy kernel arguments",
			Comments: []string{
				"The selected live boot policy appends its kernel arguments after the shared live base.",
				"Keep these values boot-safe because the managed live renderer consumes them directly.",
			},
			Keys: []string{
				"SHARED_LIVE_BASE_KERNEL_ARGS",
				"BOOT_POLICY_BALANCED_KERNEL_ARGS",
				"BOOT_POLICY_PERFORMANCE_KERNEL_ARGS",
				"BOOT_POLICY_HARDENED_KERNEL_ARGS",
			},
		},
		{
			Title: "Installer policy kernel arguments",
			Comments: []string{
				"The selected installer policy appends these arguments before any configured installer URL and installer extras.",
			},
			Keys: []string{
				"INSTALLER_POLICY_PRESERVE_KERNEL_ARGS",
			},
		},
		{
			Title: "Per-profile fallback live kernel arguments",
			Comments: []string{
				"Used only when a live-family entry cannot be detected from the ISO boot config.",
			},
			Keys: profileConfigKeys(fallbackLiveKernelProfiles, "FALLBACK_LIVE_KERNEL_ARGS"),
		},
		{
			Title: "Shared kernel extras",
			Comments: []string{
				"Optional extras appended after the selected live or installer policy.",
			},
			Keys: []string{
				"DEFAULT_LIVE_KERNEL_EXTRAS",
				"DEFAULT_INSTALLER_KERNEL_EXTRAS",
				"DEFAULT_FORENSICS_KERNEL_EXTRAS",
			},
		},
		{
			Title: "Per-profile live kernel extras",
			Comments: []string{
				"Applied only when the selected profile builds a managed live-family entry.",
			},
			Keys: profileConfigKeys(liveOverrideProfiles, "LIVE_KERNEL_EXTRAS"),
		},
		{
			Title: "Per-profile forensic live kernel extras",
			Comments: []string{
				"Applied only to preserved forensic live entries for the selected managed profile.",
			},
			Keys: profileConfigKeys(forensicsOverrideProfiles, "FORENSICS_KERNEL_EXTRAS"),
		},
		{
			Title: "Per-profile installer kernel extras",
			Comments: []string{
				"Applied only when installer-capable entries are present for the selected managed profile.",
			},
			Keys: profileConfigKeys(installerOverrideProfiles, "INSTALLER_KERNEL_EXTRAS"),
		},
		{
			Title: "Preseed URL and variant argument defaults",
			Comments: []string{
				"Debian and Kali installer-capable entries use the internal profile URL by default.",
				"The shared public URL is used by custom GRUB Preseed Public submenus as the single url= transport.",
				"DEBIAN_PRESEED_INTERNAL_ARGS and DEBIAN_PRESEED_PUBLIC_ARGS are optional GRUB overlays applied only to their matching submenu variants.",
				"The shipped public overlay disables d-i HTTPS certificate validation; clear it to require normal CA validation.",
			},
			Keys: append(
				[]string{"DEBIAN_PRESEED_PUBLIC_URL"},
				append(
					profileConfigKeys(preseedURLProfiles, "PRESEED_INTERNAL_URL"),
					"DEBIAN_PRESEED_PUBLIC_ARGS",
					"DEBIAN_PRESEED_INTERNAL_ARGS",
				)...,
			),
		},
		{
			Title: "Managed source URLs",
			Comments: []string{
				"These URLs back the Download from Internet flows for Debian, Kali Linux, Kali Purple, and Tails.",
				"Each source has separate stable and testing URLs; the download flow asks which release channel to use.",
				"Kali Live uses its torrent URL and is downloaded through aria2c with seeding disabled.",
			},
			Keys: managedSourceURLKeys(),
		},
	}
}

func requiredConfigKeys() []string {
	keys := []string{
		"APP_NAME",
		"APP_VERSION",
		"DEFAULT_PERSISTENCE_SIZE_GIB",
		"DEFAULT_BOOT_POLICY",
		"DEFAULT_INSTALLER_POLICY",
		"DEFAULT_LIVE_TORAM",
		"DEFAULT_LIVE_MEM_GIB",
		"DEFAULT_LIVE_HOOKS",
		"DEFAULT_LIVE_ARGS_HOOKS",
		"DEFAULT_LIVE_WIFI_INTERFACE",
		"DEFAULT_LIVE_WIFI_ESSID",
		"DEFAULT_LIVE_WIFI_SECURITY",
		"DEFAULT_LIVE_WIFI_CIDR",
		"DEFAULT_LIVE_WIFI_GATEWAY",
		"DEFAULT_LIVE_WIFI_NAMESERVERS",
		"SHARED_LIVE_BASE_KERNEL_ARGS",
		"BOOT_POLICY_BALANCED_KERNEL_ARGS",
		"BOOT_POLICY_PERFORMANCE_KERNEL_ARGS",
		"BOOT_POLICY_HARDENED_KERNEL_ARGS",
		"INSTALLER_POLICY_PRESERVE_KERNEL_ARGS",
		"DEFAULT_LIVE_KERNEL_EXTRAS",
		"DEFAULT_INSTALLER_KERNEL_EXTRAS",
		"DEFAULT_FORENSICS_KERNEL_EXTRAS",
	}
	keys = append(keys, partitionLabelConfigKeys...)
	keys = append(keys, profileConfigKeys(fallbackLiveKernelProfiles, "FALLBACK_LIVE_KERNEL_ARGS")...)
	keys = append(keys, profileConfigKeys(liveOverrideProfiles, "LIVE_KERNEL_EXTRAS")...)
	keys = append(keys, profileConfigKeys(forensicsOverrideProfiles, "FORENSICS_KERNEL_EXTRAS")...)
	keys = append(keys, profileConfigKeys(installerOverrideProfiles, "INSTALLER_KERNEL_EXTRAS")...)
	keys = append(keys, "DEBIAN_PRESEED_PUBLIC_URL")
	keys = append(keys, "DEBIAN_PRESEED_PUBLIC_ARGS", "DEBIAN_PRESEED_INTERNAL_ARGS")
	keys = append(keys, profileConfigKeys(preseedURLProfiles, "PRESEED_INTERNAL_URL")...)
	keys = append(keys, managedSourceURLKeys()...)
	return keys
}

func optionalEmptyKeys() map[string]bool {
	optional := map[string]bool{
		"SHARED_LIVE_BASE_KERNEL_ARGS":          true,
		"BOOT_POLICY_BALANCED_KERNEL_ARGS":      true,
		"BOOT_POLICY_PERFORMANCE_KERNEL_ARGS":   true,
		"BOOT_POLICY_HARDENED_KERNEL_ARGS":      true,
		"INSTALLER_POLICY_PRESERVE_KERNEL_ARGS": true,
		"DEFAULT_LIVE_KERNEL_EXTRAS":            true,
		"DEFAULT_INSTALLER_KERNEL_EXTRAS":       true,
		"DEFAULT_FORENSICS_KERNEL_EXTRAS":       true,
		"DEFAULT_LIVE_ARGS_HOOKS":               true,
		"DEFAULT_LIVE_WIFI_INTERFACE":           true,
		"DEFAULT_LIVE_WIFI_ESSID":               true,
		"DEFAULT_LIVE_WIFI_SECURITY":            true,
		"DEFAULT_LIVE_WIFI_CIDR":                true,
		"DEFAULT_LIVE_WIFI_GATEWAY":             true,
		"DEFAULT_LIVE_WIFI_NAMESERVERS":         true,
		"DEBIAN_PRESEED_PUBLIC_ARGS":            true,
		"DEBIAN_PRESEED_INTERNAL_ARGS":          true,
	}
	for _, key := range profileConfigKeys(fallbackLiveKernelProfiles, "FALLBACK_LIVE_KERNEL_ARGS") {
		optional[key] = true
	}
	for _, key := range profileConfigKeys(liveOverrideProfiles, "LIVE_KERNEL_EXTRAS") {
		optional[key] = true
	}
	for _, key := range profileConfigKeys(forensicsOverrideProfiles, "FORENSICS_KERNEL_EXTRAS") {
		optional[key] = true
	}
	for _, key := range profileConfigKeys(installerOverrideProfiles, "INSTALLER_KERNEL_EXTRAS") {
		optional[key] = true
	}
	for _, key := range profileConfigKeys(preseedURLProfiles, "PRESEED_INTERNAL_URL") {
		optional[key] = true
	}
	optional["DEBIAN_PRESEED_PUBLIC_URL"] = false
	for _, key := range managedSourceURLKeys() {
		optional[key] = false
	}
	return optional
}

func isAdditionalKernelArgConfigKey(key string) bool {
	if key == "PRESEED_COMMON_KERNEL_ARGS" {
		return true
	}
	return strings.HasPrefix(key, "PRESEED_") && (strings.Contains(key, "_ARGS_") || strings.HasSuffix(key, "_KERNEL_ARGS"))
}

func kernelArgKeys() []string {
	keys := []string{
		"SHARED_LIVE_BASE_KERNEL_ARGS",
		"BOOT_POLICY_BALANCED_KERNEL_ARGS",
		"BOOT_POLICY_PERFORMANCE_KERNEL_ARGS",
		"BOOT_POLICY_HARDENED_KERNEL_ARGS",
		"INSTALLER_POLICY_PRESERVE_KERNEL_ARGS",
		"DEFAULT_LIVE_ARGS_HOOKS",
		"DEFAULT_LIVE_KERNEL_EXTRAS",
		"DEFAULT_INSTALLER_KERNEL_EXTRAS",
		"DEFAULT_FORENSICS_KERNEL_EXTRAS",
		"DEBIAN_PRESEED_PUBLIC_ARGS",
		"DEBIAN_PRESEED_INTERNAL_ARGS",
	}
	keys = append(keys, profileConfigKeys(fallbackLiveKernelProfiles, "FALLBACK_LIVE_KERNEL_ARGS")...)
	keys = append(keys, profileConfigKeys(liveOverrideProfiles, "LIVE_KERNEL_EXTRAS")...)
	keys = append(keys, profileConfigKeys(forensicsOverrideProfiles, "FORENSICS_KERNEL_EXTRAS")...)
	keys = append(keys, profileConfigKeys(installerOverrideProfiles, "INSTALLER_KERNEL_EXTRAS")...)
	return keys
}

func knownConfigKeySet() map[string]struct{} {
	known := make(map[string]struct{}, len(requiredConfigKeys()))
	for _, key := range requiredConfigKeys() {
		known[key] = struct{}{}
	}
	return known
}

func extraConfigValues(data map[string]string) map[string]string {
	known := knownConfigKeySet()
	extras := make(map[string]string)
	for key, value := range data {
		if _, ok := known[key]; ok {
			continue
		}
		if !isPreservedAdditionalConfigKey(key) {
			continue
		}
		extras[key] = strings.TrimSpace(value)
	}
	if len(extras) == 0 {
		return nil
	}
	return extras
}

func sortedExtraConfigKeys(data map[string]string) []string {
	known := knownConfigKeySet()
	keys := make([]string, 0)
	for key := range data {
		if _, ok := known[key]; ok {
			continue
		}
		if !isPreservedAdditionalConfigKey(key) {
			continue
		}
		keys = append(keys, key)
	}
	sort.Strings(keys)
	return keys
}

func isPreservedAdditionalConfigKey(key string) bool {
	return strings.HasPrefix(key, "PRESEED_") || strings.HasSuffix(key, "_URL")
}

func partitionLabelValuesFromMap(data map[string]string) map[string]string {
	labels := make(map[string]string, len(partitionLabelConfigKeys))
	for _, key := range partitionLabelConfigKeys {
		labels[key] = strings.TrimSpace(data[key])
	}
	return labels
}

func applyLegacyKeyAliases(raw map[string]string) map[string]string {
	remapped := make(map[string]string, len(raw))
	for key, value := range raw {
		remapped[key] = value
	}
	for legacyKey, canonicalKey := range legacyKeyAliases {
		legacyValue := strings.TrimSpace(remapped[legacyKey])
		canonicalValue := strings.TrimSpace(remapped[canonicalKey])
		if legacyValue != "" && canonicalValue == "" {
			remapped[canonicalKey] = legacyValue
		}
	}
	return remapped
}

func profileConfigKeys(profiles []string, suffix string) []string {
	keys := make([]string, 0, len(profiles))
	for _, profile := range profiles {
		keys = append(keys, profileConfigKey(profile, suffix))
	}
	return keys
}

func profileConfigKey(profile, suffix string) string {
	return fmt.Sprintf("%s_%s", profileEnvPrefix(profile), suffix)
}

func profileEnvPrefix(profile string) string {
	return strings.ToUpper(strings.ReplaceAll(profile, "-", "_"))
}

func escapeConfigValue(value string) string {
	replacer := strings.NewReplacer(`\`, `\\`, `"`, `\"`)
	return replacer.Replace(value)
}

func stripConfigQuotes(value string) string {
	trimmed := strings.TrimSpace(value)
	if len(trimmed) >= 2 && trimmed[0] == '"' && trimmed[len(trimmed)-1] == '"' {
		trimmed = trimmed[1 : len(trimmed)-1]
		replacer := strings.NewReplacer(`\"`, `"`, `\\`, `\`)
		return replacer.Replace(trimmed)
	}
	if len(trimmed) >= 2 && trimmed[0] == '\'' && trimmed[len(trimmed)-1] == '\'' {
		return trimmed[1 : len(trimmed)-1]
	}
	return trimmed
}

func normalizeBoolString(value string, key string) (string, error) {
	switch strings.ToLower(strings.TrimSpace(value)) {
	case "1", "true", "yes", "on":
		return "1", nil
	case "", "0", "false", "no", "off":
		return "0", nil
	default:
		return "", fmt.Errorf("%s must be a boolean value", key)
	}
}

func normalizePositiveIntString(value string, key string) (string, error) {
	number, err := strconv.Atoi(strings.TrimSpace(value))
	if err != nil {
		return "", fmt.Errorf("%s must be an integer", key)
	}
	if number <= 0 {
		return "", fmt.Errorf("%s must be at least 1", key)
	}
	return strconv.Itoa(number), nil
}

func normalizeNonNegativeIntString(value string, key string) (string, error) {
	number, err := strconv.Atoi(strings.TrimSpace(value))
	if err != nil {
		return "", fmt.Errorf("%s must be an integer", key)
	}
	if number < 0 {
		return "", fmt.Errorf("%s must be zero or greater", key)
	}
	return strconv.Itoa(number), nil
}

func normalizeSingleTokenString(value string, key string) (string, error) {
	trimmed := strings.TrimSpace(value)
	if trimmed == "" {
		return "", nil
	}
	if strings.ContainsAny(trimmed, " \t\r\n") {
		return "", fmt.Errorf("%s must not contain whitespace", key)
	}
	return trimmed, nil
}

func normalizeLiveWifiTextString(value string, key string, maxBytes int) (string, error) {
	normalized := strings.TrimSpace(value)
	if normalized == "" {
		return "", nil
	}
	for _, char := range []byte(normalized) {
		if char < 0x20 || char == 0x7f {
			return "", fmt.Errorf("%s must not contain control characters", key)
		}
	}
	if len([]byte(normalized)) > maxBytes {
		return "", fmt.Errorf("%s must not exceed %d UTF-8 bytes", key, maxBytes)
	}
	return normalized, nil
}

func normalizeIPv4CIDRString(value string, key string) (string, error) {
	trimmed, err := normalizeSingleTokenString(value, key)
	if err != nil {
		return "", err
	}
	if trimmed == "" {
		return "", nil
	}
	address, prefix, found := strings.Cut(trimmed, "/")
	if !found || prefix == "" {
		return "", fmt.Errorf("%s must include a CIDR prefix such as 192.168.50.43/24", key)
	}
	if net.ParseIP(address).To4() == nil {
		return "", fmt.Errorf("%s must use an IPv4 address", key)
	}
	prefixNumber, err := strconv.Atoi(prefix)
	if err != nil || prefixNumber < 0 || prefixNumber > 32 {
		return "", fmt.Errorf("%s prefix must be between 0 and 32", key)
	}
	return fmt.Sprintf("%s/%d", address, prefixNumber), nil
}

func validateLiveWifiSecurityString(value string) (string, error) {
	trimmed, err := normalizeSingleTokenString(value, "DEFAULT_LIVE_WIFI_SECURITY")
	if err != nil {
		return "", err
	}
	switch trimmed {
	case "":
		return "", nil
	case "open", "wpa", "sae":
		return trimmed, nil
	default:
		return "", fmt.Errorf("DEFAULT_LIVE_WIFI_SECURITY must be one of: open, wpa, sae")
	}
}

func validateNoLegacySecretKernelArgs(value string, key string) error {
	found := make([]string, 0)
	seen := make(map[string]struct{})
	for _, token := range strings.Fields(value) {
		name, _, hasValue := strings.Cut(token, "=")
		if !hasValue {
			continue
		}
		if _, forbidden := legacySecretKernelArgNames[name]; !forbidden {
			continue
		}
		if _, duplicate := seen[name]; duplicate {
			continue
		}
		seen[name] = struct{}{}
		found = append(found, name)
	}
	if len(found) > 0 {
		sort.Strings(found)
		return fmt.Errorf(
			"%s contains forbidden legacy secret kernel argument(s): %s; store these values in initrd/debian/netinst/preseed.env",
			key,
			strings.Join(found, ", "),
		)
	}
	liveWifiFound := make([]string, 0)
	for _, token := range strings.Fields(value) {
		name, _, hasValue := strings.Cut(token, "=")
		if !hasValue {
			continue
		}
		if _, forbidden := liveWifiSecretKernelArgNames[name]; !forbidden {
			continue
		}
		if _, duplicate := seen[name]; duplicate {
			continue
		}
		seen[name] = struct{}{}
		liveWifiFound = append(liveWifiFound, name)
	}
	if len(liveWifiFound) == 0 {
		return nil
	}
	sort.Strings(liveWifiFound)
	return fmt.Errorf(
		"%s contains forbidden Live Wi-Fi passphrase kernel argument(s): %s; store the passphrase in initrd/debian/live/live.env",
		key,
		strings.Join(liveWifiFound, ", "),
	)
}

func validatePreseedNetworkKernelArgs(value string, key string) (string, error) {
	normalized := collapseWhitespace(value)
	for _, item := range strings.Fields(normalized) {
		if item == "d-i" {
			return "", fmt.Errorf("%s must use kernel boot-parameter syntax; remove standalone d-i preseed owner token", key)
		}
	}
	return normalized, nil
}

func validatePreseedWifiKernelArgs(value string) (string, error) {
	normalized, err := validatePreseedNetworkKernelArgs(value, "PRESEED_WIFI_KERNEL_ARGS")
	if err != nil {
		return "", err
	}
	for _, item := range strings.Fields(normalized) {
		if !strings.HasPrefix(item, "netcfg/wireless_security_type=") {
			continue
		}
		security := strings.TrimPrefix(item, "netcfg/wireless_security_type=")
		switch security {
		case "open", "wep", "wpa":
		default:
			return "", fmt.Errorf("PRESEED_WIFI_KERNEL_ARGS netcfg/wireless_security_type must be one of: open, wep, wpa")
		}
	}
	return normalized, nil
}

func normalizeCommaListString(value string) string {
	items := strings.FieldsFunc(value, func(r rune) bool {
		return r == ',' || r == ' ' || r == '\t' || r == '\n' || r == '\r'
	})
	normalized := make([]string, 0, len(items))
	for _, item := range items {
		item = strings.TrimSpace(item)
		if item == "" {
			continue
		}
		normalized = append(normalized, item)
	}
	return strings.Join(normalized, ",")
}

func normalizeOptionalURLString(value string) (string, error) {
	trimmed := strings.TrimSpace(value)
	if trimmed == "" {
		return "", nil
	}
	if !strings.HasPrefix(trimmed, "http://") && !strings.HasPrefix(trimmed, "https://") {
		return "", fmt.Errorf("installer URL values must start with http:// or https://")
	}
	return trimmed, nil
}

func validateLiveBootPolicy(value string) error {
	for _, option := range liveBootPolicyOptions {
		if option.Name == value {
			return nil
		}
	}
	allowed := make([]string, 0, len(liveBootPolicyOptions))
	for _, option := range liveBootPolicyOptions {
		allowed = append(allowed, option.Name)
	}
	return fmt.Errorf("DEFAULT_BOOT_POLICY must be one of: %s", strings.Join(allowed, ", "))
}

func validateInstallerPolicy(value string) error {
	for _, option := range installerPolicyOptions {
		if option.Name == value {
			return nil
		}
	}
	allowed := make([]string, 0, len(installerPolicyOptions))
	for _, option := range installerPolicyOptions {
		allowed = append(allowed, option.Name)
	}
	return fmt.Errorf("DEFAULT_INSTALLER_POLICY must be one of: %s", strings.Join(allowed, ", "))
}
