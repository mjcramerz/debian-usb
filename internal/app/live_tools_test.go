package app

import (
	"bufio"
	"reflect"
	"strings"
	"testing"
)

func TestLoadLiveToolCatalogUsesVersionThreeScopedGroups(t *testing.T) {
	catalog, err := loadLiveToolCatalog()
	if err != nil {
		t.Fatalf("load Live tool catalog: %v", err)
	}
	if catalog.SchemaVersion != liveToolCatalogSchemaVersion {
		t.Fatalf("expected schema version %d, got %d", liveToolCatalogSchemaVersion, catalog.SchemaVersion)
	}
	if len(catalog.PackageGroups) < 10 {
		t.Fatalf("expected the recovery catalog groups, got %#v", catalog.PackageGroups)
	}
	if catalog.PackageGroups[0].ID != "filesystem_core" {
		t.Fatalf("expected catalog order to start with filesystem_core, got %q", catalog.PackageGroups[0].ID)
	}
	if !catalog.supportsProfile(profileDebian) || catalog.supportsProfile(profileTails) {
		t.Fatalf("unexpected supported profile set: %#v", catalog.SupportedProfiles)
	}
	foundProgrammerGroup := false
	for _, group := range catalog.PackageGroups {
		if group.ID != "firmware_programming" {
			continue
		}
		foundProgrammerGroup = true
		foundFlashrom := false
		for _, packageName := range group.Packages {
			if packageName == "flashrom" {
				foundFlashrom = true
				break
			}
		}
		if !foundFlashrom {
			t.Fatalf("expected firmware_programming to include flashrom, got %#v", group.Packages)
		}
	}
	if !foundProgrammerGroup {
		t.Fatal("expected firmware_programming group for CH341A and related programmers")
	}
	if catalog.CommandPackages["flashrom"] != "flashrom" || catalog.CommandPackages["openocd"] != "openocd" {
		t.Fatalf("unexpected firmware programmer command mappings: %#v", catalog.CommandPackages)
	}
}

func TestPromptLiveToolGroupsSupportsAll(t *testing.T) {
	catalog, err := loadLiveToolCatalog()
	if err != nil {
		t.Fatalf("load Live tool catalog: %v", err)
	}
	application := App{reader: bufio.NewReader(strings.NewReader("a\n"))}

	groups, action, err := application.promptLiveToolGroups(profileDebian, nil)
	if err != nil {
		t.Fatalf("prompt Live tool groups: %v", err)
	}
	if action != menuStay {
		t.Fatalf("expected menuStay, got %v", action)
	}
	if !reflect.DeepEqual(groups, catalog.forProfile(profileDebian).allGroupIDs()) {
		t.Fatalf("expected all groups, got %#v", groups)
	}
}

func TestPromptLiveToolGroupsSupportsNone(t *testing.T) {
	application := App{reader: bufio.NewReader(strings.NewReader("n\n"))}

	groups, action, err := application.promptLiveToolGroups(profileDebian, nil)
	if err != nil {
		t.Fatalf("prompt Live tool groups: %v", err)
	}
	if action != menuStay {
		t.Fatalf("expected menuStay, got %v", action)
	}
	if groups == nil || len(groups) != 0 {
		t.Fatalf("expected an explicit empty tool selection, got %#v", groups)
	}
}

func TestPromptLiveToolGroupsSelectToolsOpensIndividualToggleMenu(t *testing.T) {
	application := App{reader: bufio.NewReader(strings.NewReader("s\n2\nc\n"))}

	groups, action, err := application.promptLiveToolGroups(profileDebian, nil)
	if err != nil {
		t.Fatalf("prompt Live tool groups: %v", err)
	}
	if action != menuStay {
		t.Fatalf("expected menuStay, got %v", action)
	}
	if !reflect.DeepEqual(groups, []string{"nvme"}) {
		t.Fatalf("expected only the selected NVMe group, got %#v", groups)
	}
}

func TestPromptLiveToolGroupsSelectToolsPreservesCurrentSelection(t *testing.T) {
	application := App{reader: bufio.NewReader(strings.NewReader("s\n2\nc\n"))}

	groups, action, err := application.promptLiveToolGroups(profileDebian, []string{"filesystem_core"})
	if err != nil {
		t.Fatalf("prompt Live tool groups: %v", err)
	}
	if action != menuStay {
		t.Fatalf("expected menuStay, got %v", action)
	}
	if !reflect.DeepEqual(groups, []string{"filesystem_core", "nvme"}) {
		t.Fatalf("expected the current group plus selected NVMe group, got %#v", groups)
	}
}

func TestPromptLiveToolGroupsIndividualBackReturnsToActionMenu(t *testing.T) {
	catalog, err := loadLiveToolCatalog()
	if err != nil {
		t.Fatalf("load Live tool catalog: %v", err)
	}
	application := App{reader: bufio.NewReader(strings.NewReader("s\nb\na\n"))}

	groups, action, err := application.promptLiveToolGroups(profileDebian, nil)
	if err != nil {
		t.Fatalf("prompt Live tool groups: %v", err)
	}
	if action != menuStay {
		t.Fatalf("expected menuStay, got %v", action)
	}
	if !reflect.DeepEqual(groups, catalog.forProfile(profileDebian).allGroupIDs()) {
		t.Fatalf("expected All after returning to the action menu, got %#v", groups)
	}
}

func TestPromptLiveToolGroupsPropagatesWorkflowNavigation(t *testing.T) {
	tests := []struct {
		name   string
		input  string
		action menuAction
	}{
		{name: "back", input: "b\n", action: menuBack},
		{name: "exit", input: "e\n", action: menuExit},
		{name: "exit from individual selector", input: "s\ne\n", action: menuExit},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			application := App{reader: bufio.NewReader(strings.NewReader(test.input))}

			groups, action, err := application.promptLiveToolGroups(profileDebian, nil)
			if err != nil {
				t.Fatalf("prompt Live tool groups: %v", err)
			}
			if action != test.action {
				t.Fatalf("expected %v, got %v", test.action, action)
			}
			if groups != nil {
				t.Fatalf("expected no groups on navigation, got %#v", groups)
			}
		})
	}
}

func TestApplyLiveHookKernelArgsToBuildPlanIsDebianLiveOnly(t *testing.T) {
	config := RuntimeConfig{
		DefaultLiveHooks:     true,
		DefaultLiveArgsHooks: "live-config.hooks=medium",
	}
	livePlan := BuildISOPlan{Distro: buildISODistroDebian, InstallerMode: buildISOInstallerModeLive}
	applyLiveHookKernelArgsToBuildPlan(config, &livePlan)
	if livePlan.LiveBootAppend != mandatoryDebianLiveHookKernelArgs {
		t.Fatalf("expected only mandatory Debian Live hook arguments, got %q", livePlan.LiveBootAppend)
	}

	netinstPlan := BuildISOPlan{Distro: buildISODistroDebian, InstallerMode: buildISOInstallerModeNetinst}
	applyLiveHookKernelArgsToBuildPlan(config, &netinstPlan)
	if netinstPlan.LiveBootAppend != "" {
		t.Fatalf("expected Debian Netinst bootappend to remain empty, got %q", netinstPlan.LiveBootAppend)
	}

	kaliPlan := BuildISOPlan{Distro: buildISODistroKali, InstallerMode: buildISOInstallerModeLive}
	applyLiveHookKernelArgsToBuildPlan(config, &kaliPlan)
	if kaliPlan.LiveBootAppend != "" {
		t.Fatalf("expected Kali Live bootappend to remain unchanged, got %q", kaliPlan.LiveBootAppend)
	}
}

func TestLiveHookKernelArgsKeepMandatorySelectorWhenOptionalHooksAreDisabled(t *testing.T) {
	config := RuntimeConfig{
		DefaultLiveHooks:     false,
		DefaultLiveArgsHooks: "live-config.hooks=filesystem live_wifi_essid_b64=MustNotLeak",
	}
	if got := liveHookKernelArgsForConfig(config, profileDebian); got != mandatoryDebianLiveHookKernelArgs {
		t.Fatalf("expected only mandatory Debian Live hook selector, got %q", got)
	}
	if got := liveHookKernelArgsForConfig(config, profileKaliLinux); got != "" {
		t.Fatalf("expected Debian hook selector to stay out of Kali args, got %q", got)
	}
}

func TestLiveHookKernelArgsStripEveryWifiTransport(t *testing.T) {
	config := RuntimeConfig{
		DefaultLiveHooks: true,
		DefaultLiveArgsHooks: "live-config.hooks=filesystem custom=1 " +
			"live_wifi_interface=wlan0 live_wifi_essid_b64=SW5zdGFsbE5ldA " +
			"LIVE_WIFI_PASSPHRASE=MustNotLeak netcfg/wireless_essid=InstallNet",
	}
	got := liveHookKernelArgsForConfig(config, profileDebian)
	if !strings.Contains(got, "custom=1") || !strings.Contains(got, mandatoryDebianLiveHookKernelArgs) {
		t.Fatalf("expected non-Wi-Fi hook arguments and mandatory selector, got %q", got)
	}
	for _, forbidden := range []string{"live_wifi_", "LIVE_WIFI_", "netcfg/wireless_"} {
		if strings.Contains(got, forbidden) {
			t.Fatalf("Live Wi-Fi transport %q survived in kernel args %q", forbidden, got)
		}
	}
}

func TestLiveToolCatalogProfileAdditionsAreIsolated(t *testing.T) {
	catalog, err := loadLiveToolCatalog()
	if err != nil {
		t.Fatal(err)
	}
	kali := catalog.forProfile(profileKaliLinux)
	debian := catalog.forProfile(profileDebian)
	find := func(c liveToolCatalog, name string) bool {
		for _, group := range c.PackageGroups {
			for _, pkg := range group.Packages {
				if pkg == name {
					return true
				}
			}
		}
		return false
	}
	if !find(kali, "kali-tools-wireless") || !find(kali, "wifite") {
		t.Fatal("Kali wireless additions are missing")
	}
	if find(debian, "kali-tools-wireless") || find(catalog, "kali-tools-wireless") {
		t.Fatal("Kali-only packages contaminated common/Debian catalog")
	}
	if !reflect.DeepEqual(kali, kali.forProfile(profileKaliLinux)) {
		t.Fatal("profile expansion must be idempotent")
	}
}

func TestLiveToolCatalogRejectsUnsafeProfileAdditions(t *testing.T) {
	catalog, err := loadLiveToolCatalog()
	if err != nil {
		t.Fatal(err)
	}
	catalog.ProfileAdditions = map[string]map[string][]string{
		profileKaliLinux: {"wireless_security": {"good;bad"}},
	}
	if err := validateLiveToolCatalog(catalog); err == nil {
		t.Fatal("unsafe package accepted")
	}
	catalog.ProfileAdditions = map[string]map[string][]string{
		profileKaliLinux: {"nonexistent_group": {"wifite"}},
	}
	if err := validateLiveToolCatalog(catalog); err == nil {
		t.Fatal("unknown group accepted")
	}
}
