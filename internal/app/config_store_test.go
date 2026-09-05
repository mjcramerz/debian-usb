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

func TestValidateNoLegacySecretKernelArgsRejectsEveryManagedSecret(t *testing.T) {
	for legacyName := range legacySecretKernelArgNames {
		t.Run(legacyName, func(t *testing.T) {
			err := validateNoLegacySecretKernelArgs("classes=test "+legacyName+"=fixture-value", "PRESEED_ONE_ARGS_DEBIAN")
			if err == nil {
				t.Fatalf("expected %s to be rejected", legacyName)
			}
			if !strings.Contains(err.Error(), legacyName) || !strings.Contains(err.Error(), "initrd/debian/netinst/preseed.env") {
				t.Fatalf("expected key name and migration destination in error, got %v", err)
			}
		})
	}
}

func TestLoadRuntimeConfigRejectsLegacySecretInDynamicPreseedArgs(t *testing.T) {
	content, err := os.ReadFile(repoConfigPath())
	if err != nil {
		t.Fatalf("read repo config: %v", err)
	}
	text := strings.Replace(
		string(content),
		`PRESEED_ONE_ARGS_DEBIAN="`,
		`PRESEED_ONE_ARGS_DEBIAN="root_password=fixture-value `,
		1,
	)
	path := filepath.Join(t.TempDir(), "legacy-secret-preseed-args.conf")
	if err := os.WriteFile(path, []byte(text), 0644); err != nil {
		t.Fatalf("write config: %v", err)
	}

	_, err = loadRuntimeConfig(path)
	if err == nil || !strings.Contains(err.Error(), "PRESEED_ONE_ARGS_DEBIAN") || !strings.Contains(err.Error(), "root_password") {
		t.Fatalf("expected dynamic preseed secret rejection, got %v", err)
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
	if got := cfg.ExtraValues["PRESEED_USB_DEBIAN_FILE"]; got == "" {
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
	if got := reloaded.ExtraValues["PRESEED_USB_DEBIAN_FILE"]; got != cfg.ExtraValues["PRESEED_USB_DEBIAN_FILE"] {
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

func TestLoadRuntimeConfigRejectsAllLiveWifiKernelArguments(t *testing.T) {
	content, err := os.ReadFile(repoConfigPath())
	if err != nil {
		t.Fatalf("read repo config: %v", err)
	}
	for name := range liveWifiSecretKernelArgNames {
		t.Run(name, func(t *testing.T) {
			text := replaceConfigAssignmentForTest(
				t,
				string(content),
				"DEFAULT_LIVE_ARGS_HOOKS",
				"live-config.hooks=medium "+name+"=fixture",
			)
			path := filepath.Join(t.TempDir(), "live-wifi-secret.conf")
			if err := os.WriteFile(path, []byte(text), 0644); err != nil {
				t.Fatalf("write config: %v", err)
			}
			if _, err := loadRuntimeConfig(path); err == nil || !strings.Contains(err.Error(), "initrd/debian/live/live.env") {
				t.Fatalf("expected %s to be rejected with the Live env destination, got %v", name, err)
			}
		})
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
	text = strings.Replace(text, `KALI_LINUX_PRESEED_INTERNAL_URL=""`, `KALI_LINUX_PRESEED_INTERNAL_URL=""
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
