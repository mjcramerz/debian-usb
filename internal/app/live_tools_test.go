package app

import (
	"bufio"
	"reflect"
	"strings"
	"testing"
)

func TestLoadLiveToolCatalogUsesVersionTwoOrderedGroups(t *testing.T) {
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
	if !reflect.DeepEqual(groups, catalog.allGroupIDs()) {
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
	if !reflect.DeepEqual(groups, catalog.allGroupIDs()) {
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
		DefaultLiveHooks:        true,
		DefaultLiveArgsHooks:    "live-config.hooks=medium",
		DefaultLiveWifiESSID:    "InstallNet",
		DefaultLiveWifiSecurity: "open",
	}
	livePlan := BuildISOPlan{Distro: buildISODistroDebian, InstallerMode: buildISOInstallerModeLive}
	applyLiveHookKernelArgsToBuildPlan(config, &livePlan)
	if !strings.Contains(livePlan.LiveBootAppend, "live-config.hooks=medium") || !strings.Contains(livePlan.LiveBootAppend, "live_wifi_essid_b64=SW5zdGFsbE5ldA") {
		t.Fatalf("expected Debian Live hook arguments, got %q", livePlan.LiveBootAppend)
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

func TestLiveHookKernelArgsOmitUnusedWifiValues(t *testing.T) {
	missingESSID := RuntimeConfig{
		DefaultLiveHooks:        true,
		DefaultLiveArgsHooks:    "live-config.hooks=medium",
		DefaultLiveWifiSecurity: "wpa",
	}
	if got := liveHookKernelArgsForConfig(missingESSID, profileDebian); got != "live-config.hooks=medium" {
		t.Fatalf("expected missing ESSID to omit every Wi-Fi argument, got %q", got)
	}

	openNetwork := RuntimeConfig{
		DefaultLiveHooks:        true,
		DefaultLiveArgsHooks:    "live-config.hooks=medium live_wifi_psk_b64=MustNotLeak123",
		DefaultLiveWifiESSID:    "Guest Net",
		DefaultLiveWifiSecurity: "open",
	}
	got := liveHookKernelArgsForConfig(openNetwork, profileDebian)
	if !strings.Contains(got, "live_wifi_essid_b64=R3Vlc3QgTmV0") || !strings.Contains(got, "live_wifi_security=open") {
		t.Fatalf("expected open Wi-Fi arguments, got %q", got)
	}
	if strings.Contains(got, "live_wifi_psk_b64=") || strings.Contains(got, "MustNotLeak123") {
		t.Fatalf("Live Wi-Fi kernel arguments retained a forbidden passphrase transport: %q", got)
	}
}
