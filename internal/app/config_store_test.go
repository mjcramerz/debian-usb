package app

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func repoConfigPath() string {
	return filepath.Join("..", "..", "configs", "debian-usb.conf")
}

func replaceConfigAssignmentForTest(t *testing.T, content, key, value string) string {
	t.Helper()
	prefix := key + "="
	lines := strings.Split(content, "\n")
	replaced := 0
	for index, line := range lines {
		if !strings.HasPrefix(line, prefix) {
			continue
		}
		lines[index] = prefix + `"` + value + `"`
		replaced++
	}
	if replaced != 1 {
		t.Fatalf("expected exactly one %s assignment, found %d", key, replaced)
	}
	return strings.Join(lines, "\n")
}

func TestRuntimeConfigPreservesUserArgsInEveryArgumentField(t *testing.T) {
	raw, err := parseConfigFile(repoConfigPath())
	if err != nil {
		t.Fatal(err)
	}
	keys := make(map[string]bool)
	for _, key := range kernelArgKeys() {
		keys[key] = true
	}
	for key := range raw {
		if isAdditionalKernelArgConfigKey(key) {
			keys[key] = true
		}
	}
	for _, key := range []string{"PRESEED_CUSTOM_ARGS_DEBIAN_DE", "PRESEED_WIFI_KERNEL_ARGS", "SITE_CUSTOM_ARGS"} {
		keys[key] = true
	}
	args := preseedNetworkArgsForTest
	for _, name := range previouslyRestrictedArgNamesForTest {
		// Put every formerly restricted name through every args field.
		args += " " + name + "=fixture-value"
	}
	for key := range keys {
		raw[key] = args
	}
	normalized, err := normalizeConfigMap(raw)
	if err != nil {
		t.Fatalf("valid user arguments were rejected: %v", err)
	}
	path := filepath.Join(t.TempDir(), "user-args.conf")
	if _, err := saveRuntimeConfig(path, runtimeConfigFromMap(path, normalized)); err != nil {
		t.Fatal(err)
	}
	cfg, err := loadRuntimeConfig(path)
	if err != nil {
		t.Fatal(err)
	}
	saved := configMapFromRuntimeConfig(cfg)
	for key := range keys {
		if normalized[key] != args {
			t.Errorf("normalization changed %s", key)
		}
		if saved[key] != args {
			t.Errorf("load/save changed %s", key)
		}
	}
}

func TestOriginalFifthDebianPresetLoadsAndRoundTripsUnchanged(t *testing.T) {
	raw, err := parseConfigFile(repoConfigPath())
	if err != nil {
		t.Fatal(err)
	}
	cfg, err := loadRuntimeConfig(repoConfigPath())
	if err != nil {
		t.Fatalf("supplied config must load: %v", err)
	}
	const key = "PRESEED_FIVE_ARGS_DEBIAN_DE"
	if cfg.ExtraValues[key] != raw[key] {
		t.Fatal("loading changed the supplied fifth preset")
	}
	path := filepath.Join(t.TempDir(), "round-trip.conf")
	if _, err := saveRuntimeConfig(path, cfg); err != nil {
		t.Fatal(err)
	}
	reloaded, err := loadRuntimeConfig(path)
	if err != nil {
		t.Fatal(err)
	}
	if reloaded.ExtraValues[key] != raw[key] {
		t.Fatal("saving changed the supplied fifth preset")
	}
}

func TestSaveRuntimeConfigRoundTripsManagedFields(t *testing.T) {
	cfg, err := loadRuntimeConfig(writeBackendLiveHookTestConfig(t))
	if err != nil {
		t.Fatalf("load synthetic runtime config: %v", err)
	}
	cfg.DefaultPreseedPublicArgs = "debian-installer/allow_unauthenticated_ssl=true"
	cfg.DefaultPreseedInternalArgs = ""
	if cfg.DefaultPreseedPublicArgs != "debian-installer/allow_unauthenticated_ssl=true" {
		t.Fatalf("expected repo public preseed args to enable d-i HTTPS certificate bypass, got %q", cfg.DefaultPreseedPublicArgs)
	}
	if cfg.DefaultPreseedInternalArgs != "" {
		t.Fatalf("expected repo internal preseed args to allow an empty default, got %q", cfg.DefaultPreseedInternalArgs)
	}

	cfg.DefaultPersistenceSizeGiB = 40
	cfg.DefaultBootPolicy = "performance"
	cfg.DefaultLiveToram = true
	cfg.DefaultLiveMemGiB = 16
	cfg.DefaultLiveHooks = true
	cfg.DefaultLiveArgsHooks = "live-config.hooks=medium"
	cfg.DefaultPreseedPublicURL = "https://example.test/public-preseed.cfg"
	cfg.DefaultPreseedPublicArgs = "debian-installer/allow_unauthenticated_ssl=true public=1"
	cfg.DefaultPreseedInternalArgs = "internal=1"
	cfg.DefaultPartitionLabels[configLabelESP] = "ESPTEST"
	cfg.DefaultPartitionLabels[configLabelDebianNetinst] = "DEB-NET"
	cfg.DefaultLiveKernelExtras = "foo=bar"
	cfg.ProfileLiveKernelExtras[profileUbuntuServer] = "server=1"
	cfg.ProfileInstallerKernelExtras[profileUbuntuDesktop] = "autoinstall"
	cfg.ProfilePreseedURLs[profileKaliLinux] = "https://example.test/kali-internal.cfg"
	cfg.ManagedSourceURLs["DEBIAN_LIVE_ISO_STABLE_URL"] = "https://example.test/debian-stable.iso"
	cfg.ManagedSourceURLs["DEBIAN_LIVE_ISO_TESTING_URL"] = "https://example.test/debian-testing.iso"

	path := filepath.Join(t.TempDir(), "debian-usb.conf")
	if _, err := saveRuntimeConfig(path, cfg); err != nil {
		t.Fatalf("save runtime config: %v", err)
	}

	reloaded, err := loadRuntimeConfig(path)
	if err != nil {
		t.Fatalf("reload runtime config: %v", err)
	}
	if reloaded.DefaultPersistenceSizeGiB != 40 {
		t.Fatalf("expected persistence size 40, got %d", reloaded.DefaultPersistenceSizeGiB)
	}
	if reloaded.DefaultBootPolicy != "performance" {
		t.Fatalf("expected performance boot policy, got %q", reloaded.DefaultBootPolicy)
	}
	if !reloaded.DefaultLiveToram {
		t.Fatalf("expected toram to persist")
	}
	if reloaded.DefaultLiveMemGiB != 16 {
		t.Fatalf("expected live mem 16, got %d", reloaded.DefaultLiveMemGiB)
	}
	if !reloaded.DefaultLiveHooks {
		t.Fatalf("expected live hooks to persist")
	}
	if reloaded.DefaultLiveArgsHooks != "live-config.hooks=medium" {
		t.Fatalf("expected live hook args to round-trip, got %q", reloaded.DefaultLiveArgsHooks)
	}
	if reloaded.DefaultPreseedPublicURL != "https://example.test/public-preseed.cfg" {
		t.Fatalf("expected shared public preseed URL to round-trip, got %q", reloaded.DefaultPreseedPublicURL)
	}
	if reloaded.DefaultPreseedPublicArgs != "debian-installer/allow_unauthenticated_ssl=true public=1" {
		t.Fatalf("expected public preseed args to round-trip, got %q", reloaded.DefaultPreseedPublicArgs)
	}
	if reloaded.DefaultPreseedInternalArgs != "internal=1" {
		t.Fatalf("expected internal preseed args to round-trip, got %q", reloaded.DefaultPreseedInternalArgs)
	}
	if got := reloaded.DefaultPartitionLabels[configLabelESP]; got != "ESPTEST" {
		t.Fatalf("expected ESP label to round-trip, got %q", got)
	}
	if got := reloaded.DefaultPartitionLabels[configLabelDebianNetinst]; got != "DEB-NET" {
		t.Fatalf("expected Debian netinst label to round-trip, got %q", got)
	}
	if reloaded.DefaultLiveKernelExtras != "foo=bar" {
		t.Fatalf("expected live extras to round-trip, got %q", reloaded.DefaultLiveKernelExtras)
	}
	if got := reloaded.ProfileLiveKernelExtras[profileUbuntuServer]; got != "server=1" {
		t.Fatalf("expected ubuntu-server live extras to round-trip, got %q", got)
	}
	if got := reloaded.ProfileInstallerKernelExtras[profileUbuntuDesktop]; got != "autoinstall" {
		t.Fatalf("expected ubuntu-desktop installer extras to round-trip, got %q", got)
	}
	if got := reloaded.ProfilePreseedURLs[profileKaliLinux]; got != "https://example.test/kali-internal.cfg" {
		t.Fatalf("expected kali-linux internal preseed URL to round-trip, got %q", got)
	}
	if got := reloaded.ManagedSourceURLs["DEBIAN_LIVE_ISO_STABLE_URL"]; got != "https://example.test/debian-stable.iso" {
		t.Fatalf("expected stable Debian source URL to round-trip, got %q", got)
	}
	if got := reloaded.ManagedSourceURLs["DEBIAN_LIVE_ISO_TESTING_URL"]; got != "https://example.test/debian-testing.iso" {
		t.Fatalf("expected testing Debian source URL to round-trip, got %q", got)
	}
}

func TestSaveRuntimeConfigAllowsEmptyPreseedVariantArgs(t *testing.T) {
	cfg, err := loadRuntimeConfig(repoConfigPath())
	if err != nil {
		t.Fatalf("load runtime config: %v", err)
	}
	cfg.DefaultPreseedPublicArgs = ""
	cfg.DefaultPreseedInternalArgs = ""

	path := filepath.Join(t.TempDir(), "empty-preseed-variant-args.conf")
	if _, err := saveRuntimeConfig(path, cfg); err != nil {
		t.Fatalf("save runtime config: %v", err)
	}

	reloaded, err := loadRuntimeConfig(path)
	if err != nil {
		t.Fatalf("reload runtime config: %v", err)
	}
	if reloaded.DefaultPreseedPublicArgs != "" || reloaded.DefaultPreseedInternalArgs != "" {
		t.Fatalf(
			"expected empty preseed variant args to round-trip, got public=%q internal=%q",
			reloaded.DefaultPreseedPublicArgs,
			reloaded.DefaultPreseedInternalArgs,
		)
	}
}

func TestSaveRuntimeConfigNormalizesFileModeTo0644(t *testing.T) {
	cfg, err := loadRuntimeConfig(repoConfigPath())
	if err != nil {
		t.Fatalf("load runtime config: %v", err)
	}

	path := filepath.Join(t.TempDir(), "debian-usb.conf")
	if err := os.WriteFile(path, []byte("placeholder\n"), 0600); err != nil {
		t.Fatalf("seed config path: %v", err)
	}

	if _, err := saveRuntimeConfig(path, cfg); err != nil {
		t.Fatalf("save runtime config: %v", err)
	}

	info, err := os.Stat(path)
	if err != nil {
		t.Fatalf("stat saved config: %v", err)
	}
	if got := info.Mode().Perm(); got != 0644 {
		t.Fatalf("expected config mode 0644, got %04o", got)
	}
}

func TestSaveRuntimeConfigPreservesAdditionalKeys(t *testing.T) {
	cfg, err := loadRuntimeConfig(repoConfigPath())
	if err != nil {
		t.Fatalf("load runtime config: %v", err)
	}
	if cfg.ExtraValues == nil {
		t.Fatalf("expected repo config to expose additional preserved keys")
	}
	if got := cfg.ExtraValues["PRESEED_USB_DEBIAN_DE_FILE"]; got == "" {
		t.Fatalf("expected additional Debian USB preseed file to be preserved, got %q", got)
	}

	path := filepath.Join(t.TempDir(), "extra-values.conf")
	if _, err := saveRuntimeConfig(path, cfg); err != nil {
		t.Fatalf("save runtime config: %v", err)
	}

	reloaded, err := loadRuntimeConfig(path)
	if err != nil {
		t.Fatalf("reload runtime config: %v", err)
	}
	if got := reloaded.ExtraValues["PRESEED_USB_DEBIAN_DE_FILE"]; got != cfg.ExtraValues["PRESEED_USB_DEBIAN_DE_FILE"] {
		t.Fatalf("expected extra value to round-trip, got %q", got)
	}
}

func TestLoadRuntimeConfigRejectsInvalidManagedSourceURL(t *testing.T) {
	content, err := os.ReadFile(repoConfigPath())
	if err != nil {
		t.Fatalf("read repo config: %v", err)
	}
	text := strings.Replace(
		string(content),
		`TAILS_LIVE_ISO_TESTING_URL="https://nightly.tails.net/build_Tails_ISO_web-release-7.10/lastSuccessful/archive/latest.iso"`,
		`TAILS_LIVE_ISO_TESTING_URL="file:///tmp/tails.iso"`,
		1,
	)
	path := filepath.Join(t.TempDir(), "bad-managed-source-url.conf")
	if err := os.WriteFile(path, []byte(text), 0644); err != nil {
		t.Fatalf("write config: %v", err)
	}

	if _, err := loadRuntimeConfig(path); err == nil || !strings.Contains(err.Error(), "TAILS_LIVE_ISO_TESTING_URL") {
		t.Fatalf("expected managed source URL validation error, got %v", err)
	}
}

func TestLoadRuntimeConfigRejectsInvalidPartitionLabels(t *testing.T) {
	content, err := os.ReadFile(repoConfigPath())
	if err != nil {
		t.Fatalf("read repo config: %v", err)
	}
	cases := []struct {
		name    string
		oldText string
		newText string
		want    string
	}{
		{
			name:    "esp-too-long",
			oldText: `DEFAULT_ESP_LABEL="ESPBOOT"`,
			newText: `DEFAULT_ESP_LABEL="ESPBOOT-TOO-LONG"`,
			want:    "DEFAULT_ESP_LABEL",
		},
		{
			name:    "payload-whitespace",
			oldText: `DEFAULT_DEBIAN_LIVE_LABEL="DEBIAN-LIVE"`,
			newText: `DEFAULT_DEBIAN_LIVE_LABEL="DEBIAN LIVE"`,
			want:    "DEFAULT_DEBIAN_LIVE_LABEL",
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			text := strings.Replace(string(content), tc.oldText, tc.newText, 1)
			path := filepath.Join(t.TempDir(), "bad-label.conf")
			if err := os.WriteFile(path, []byte(text), 0644); err != nil {
				t.Fatalf("write config: %v", err)
			}
			if _, err := loadRuntimeConfig(path); err == nil || !strings.Contains(err.Error(), tc.want) {
				t.Fatalf("expected %s validation error, got %v", tc.want, err)
			}
		})
	}
}

func TestLoadRuntimeConfigDropsObsoleteGlobalLiveWifiFields(t *testing.T) {
	content, err := os.ReadFile(repoConfigPath())
	if err != nil {
		t.Fatalf("read repo config: %v", err)
	}
	legacy := string(content) + `
DEFAULT_LIVE_WIFI_INTERFACE="wlan0"
DEFAULT_LIVE_WIFI_ESSID="Install Net"
DEFAULT_LIVE_WIFI_SECURITY="wpa"
DEFAULT_LIVE_WIFI_CIDR="192.168.50.45/24"
DEFAULT_LIVE_WIFI_GATEWAY="192.168.50.1"
DEFAULT_LIVE_WIFI_NAMESERVERS="192.168.50.1"
`
	path := filepath.Join(t.TempDir(), "legacy-live-wifi.conf")
	if err := os.WriteFile(path, []byte(legacy), 0644); err != nil {
		t.Fatalf("write config: %v", err)
	}
	cfg, err := loadRuntimeConfig(path)
	if err != nil {
		t.Fatalf("load config with obsolete Live Wi-Fi fields: %v", err)
	}
	if _, err := saveRuntimeConfig(path, cfg); err != nil {
		t.Fatalf("rewrite config without obsolete Live Wi-Fi fields: %v", err)
	}
	rendered, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("read rewritten config: %v", err)
	}
	if strings.Contains(string(rendered), "DEFAULT_LIVE_WIFI_") {
		t.Fatalf("rewritten config retained obsolete Live Wi-Fi fields:\n%s", rendered)
	}
}

func TestLoadRuntimeConfigPreservesLegacyPresetAliasWithWifi(t *testing.T) {
	raw, err := parseConfigFile(repoConfigPath())
	if err != nil {
		t.Fatal(err)
	}
	delete(raw, "PRESEED_FIVE_ARGS_DEBIAN_DE")
	raw["PRESEED_FIVE_ARGS_DEBIAN"] = preseedNetworkArgsForTest
	normalized, err := normalizeConfigMap(raw)
	if err != nil {
		t.Fatal(err)
	}
	if normalized["PRESEED_FIVE_ARGS_DEBIAN_DE"] != preseedNetworkArgsForTest {
		t.Fatal("legacy alias lost explicit preseed Wi-Fi arguments")
	}
}

func TestLoadRuntimeConfigSupportsLegacyKaliAliases(t *testing.T) {
	fixturePath := writeBackendLiveHookTestConfig(t)
	content, err := os.ReadFile(fixturePath)
	if err != nil {
		t.Fatalf("read synthetic config: %v", err)
	}
	text := string(content)
	text = strings.Replace(text, `KALI_LINUX_LIVE_KERNEL_EXTRAS=""`, `KALI_LINUX_LIVE_KERNEL_EXTRAS=""
KALI_LIVE_KERNEL_EXTRAS="legacy-live"`, 1)
	text = strings.Replace(text, `KALI_LINUX_INSTALLER_KERNEL_EXTRAS=""`, `KALI_LINUX_INSTALLER_KERNEL_EXTRAS=""
KALI_INSTALLER_KERNEL_EXTRAS="legacy-installer"`, 1)
	text = strings.Replace(text, `KALI_LINUX_DE_PRESEED_INTERNAL_URL=""`, `KALI_LINUX_DE_PRESEED_INTERNAL_URL=""
	KALI_PRESEED_URL="https://example.test/kali.cfg"`, 1)

	path := filepath.Join(t.TempDir(), "legacy-kali.conf")
	if err := os.WriteFile(path, []byte(text), 0644); err != nil {
		t.Fatalf("write legacy config: %v", err)
	}

	cfg, err := loadRuntimeConfig(path)
	if err != nil {
		t.Fatalf("load legacy config: %v", err)
	}
	if got := cfg.ProfileLiveKernelExtras[profileKaliLinux]; got != "legacy-live" {
		t.Fatalf("expected legacy live alias to remap, got %q", got)
	}
	if got := cfg.ProfileInstallerKernelExtras[profileKaliLinux]; got != "legacy-installer" {
		t.Fatalf("expected legacy installer alias to remap, got %q", got)
	}
	if got := cfg.ProfilePreseedURLs[profileKaliLinux]; got != "https://example.test/kali.cfg" {
		t.Fatalf("expected legacy preseed alias to remap, got %q", got)
	}
}

func TestRetiredInstallerHostPathsAreDropped(t *testing.T) {
	values := map[string]string{
		"PRESEED_HOST_DEBIAN_PATH":     "/old/debian/preseed.cfg",
		"PRESEED_HOST_DEBIAN_DE_PATH":  "/desktop/preseed.cfg",
		"PRESEED_HOST_DEBIAN_SRV_PATH": "/server/preseed.cfg",
		"PRESEED_HOST_KALI_PATH":       "/old/kali/preseed.cfg",
		"PRESEED_HOST_KALI_DE_PATH":    "/kali-desktop/preseed.cfg",
		"PRESEED_HOST_KALI_SRV_PATH":   "/kali-server/preseed.cfg",
	}
	migrated := applyLegacyKeyAliases(values)
	for key := range migrated {
		if strings.HasPrefix(key, "PRESEED_HOST_DEBIAN_") || strings.HasPrefix(key, "PRESEED_HOST_KALI_") {
			t.Fatalf("retired path survived migration: %s", key)
		}
	}
	if _, err := loadRuntimeConfig(repoConfigPath()); err != nil {
		t.Fatalf("configuration without retired host paths must load: %v", err)
	}
}
