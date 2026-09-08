package app

import (
	"bufio"
	"reflect"
	"strings"
	"testing"
)

func TestLiveToolWirelessSuiteIsKaliOnly(t *testing.T) {
	catalog, err := loadLiveToolCatalog()
	if err != nil {
		t.Fatal(err)
	}
	kaliOnly := []string{"wifite", "aircrack-ng", "reaver", "pixiewps", "hcxdumptool", "hcxtools", "hashcat", "tshark", "wireshark", "kismet", "airgeddon", "bettercap", "wifiphisher", "kali-tools-wireless", "kali-tools-802-11"}
	for _, profile := range []string{profileKaliLinux, profileDebian, profileUbuntuDesktop, profileKaliLinux, profileDebian} {
		t.Run(profile, func(t *testing.T) {
			scoped := catalog.forProfile(profile)
			packages := map[string]bool{}
			for _, group := range scoped.PackageGroups {
				if profile != profileKaliLinux && group.ID == "wireless_security" {
					t.Fatal("Kali-only group visible to non-Kali profile")
				}
				for _, name := range group.Packages {
					packages[name] = true
				}
			}
			for _, name := range kaliOnly {
				if packages[name] != (profile == profileKaliLinux) {
					t.Fatalf("unexpected availability of %s for %s", name, profile)
				}
			}
			for _, name := range []string{"iw", "wpasupplicant", "network-manager", "rfkill", "nmap", "tcpdump", "nvme-cli"} {
				if !packages[name] {
					t.Fatalf("normal tool removed: %s", name)
				}
			}
			if profile != profileKaliLinux && scoped.CommandPackages["wifite"] != "" {
				t.Fatal("Kali command mapping leaked")
			}
		})
	}
}

func TestLiveToolSelectionRejectsCrossProfileSavedWirelessGroup(t *testing.T) {
	for _, profile := range []string{profileDebian, profileUbuntuDesktop} {
		if _, err := validateLiveToolGroupSelection(profile, []string{"network_core", "wireless_security"}); err == nil || !strings.Contains(err.Error(), "available only for: kali-linux") {
			t.Fatalf("%s accepted stale wireless selection: %v", profile, err)
		}
	}
	if _, err := validateLiveToolGroupSelection(profileKaliLinux, []string{"wireless_security"}); err != nil {
		t.Fatal(err)
	}
}

func TestLiveToolMenuAllIsProfileScopedAcrossOneSession(t *testing.T) {
	catalog, err := loadLiveToolCatalog()
	if err != nil {
		t.Fatal(err)
	}
	application := App{reader: bufio.NewReader(strings.NewReader("a\na\nn\n"))}
	for _, profile := range []string{profileKaliLinux, profileDebian} {
		groups, action, err := application.promptLiveToolGroups(profile, nil)
		if err != nil || action != menuStay {
			t.Fatalf("%s: %v, %v", profile, action, err)
		}
		if !reflect.DeepEqual(groups, catalog.forProfile(profile).allGroupIDs()) {
			t.Fatalf("%s: wrong All selection: %v", profile, groups)
		}
	}
	groups, action, err := application.promptLiveToolGroups(profileKaliLinux, nil)
	if err != nil || action != menuStay || groups == nil || len(groups) != 0 {
		t.Fatalf("None must remain empty: %v %v %v", groups, action, err)
	}
}

func TestLiveToolCatalogRejectsLegacyOrInvalidWirelessScopes(t *testing.T) {
	for _, scopes := range [][]string{nil, {}, {profileDebian}, {profileKaliLinux, profileDebian}, {"unknown"}} {
		catalog, err := loadLiveToolCatalog()
		if err != nil {
			t.Fatal(err)
		}
		for i := range catalog.PackageGroups {
			if catalog.PackageGroups[i].ID == "wireless_security" {
				catalog.PackageGroups[i].Profiles = scopes
			}
		}
		if err := validateLiveToolCatalog(catalog); err == nil {
			t.Fatalf("accepted invalid wireless scopes: %#v", scopes)
		}
	}
	catalog, err := loadLiveToolCatalog()
	if err != nil {
		t.Fatal(err)
	}
	catalog.SchemaVersion = 2
	if err := validateLiveToolCatalog(catalog); err == nil || !strings.Contains(err.Error(), "replace the legacy catalog") {
		t.Fatalf("legacy catalog accepted: %v", err)
	}
}

func TestLiveToolCatalogRejectsCrossProfileAdditionsAndOptionalPackages(t *testing.T) {
	catalog, err := loadLiveToolCatalog()
	if err != nil {
		t.Fatal(err)
	}
	catalog.ProfileAdditions[profileDebian] = map[string][]string{"wireless_security": {"wifite"}}
	if err := validateLiveToolCatalog(catalog); err == nil {
		t.Fatal("cross-profile additions accepted")
	}
	delete(catalog.ProfileAdditions, profileDebian)
	catalog.OptionalPackages[profileDebian] = []string{"wifite"}
	if err := validateLiveToolCatalog(catalog); err == nil {
		t.Fatal("cross-profile optional packages accepted")
	}
}

func TestMultiOSExecutionKeepsKaliWirelessSelectionOutOfDebian(t *testing.T) {
	backend, root, inputs := pipelineFixture(t)
	plan := pipelineMulti(inputs)
	catalog, err := loadLiveToolCatalog()
	if err != nil {
		t.Fatal(err)
	}
	for i := range plan.Items {
		plan.Items[i].LiveToolGroups = catalog.forProfile(plan.Items[i].Profile).allGroupIDs()
	}
	if err := backend.executeMultiOSCreate(plan, "/dev/test-usb", false); err != nil {
		t.Fatal(err)
	}
	remasters := 0
	for _, line := range strings.Split(pipelineTrace(t, root), "\n") {
		if !strings.HasPrefix(line, "remaster-live-tools-source|") {
			continue
		}
		remasters++
		hasWireless := strings.Contains(line, "|--group|wireless_security")
		isKali := strings.Contains(line, "|--profile|kali-linux|")
		if hasWireless != isKali {
			t.Fatalf("cross-profile wireless group: %s", line)
		}
	}
	if remasters != 2 {
		t.Fatalf("expected two independent remasters, got %d", remasters)
	}
}

func TestMultiOSRejectsStaleDebianWirelessSelectionBeforePreparingAnyItem(t *testing.T) {
	backend, root, inputs := pipelineFixture(t)
	plan := pipelineMulti(inputs)
	plan.Items[0], plan.Items[1] = plan.Items[1], plan.Items[0]
	plan.Items[1].LiveToolGroups = []string{"wireless_security"}
	err := backend.executeMultiOSCreate(plan, "/dev/test-usb", false)
	if err == nil || !strings.Contains(err.Error(), "available only for: kali-linux") {
		t.Fatalf("expected scope error: %v", err)
	}
	trace := pipelineTrace(t, root)
	if strings.Contains(trace, "remaster") || strings.Contains(trace, "writer") {
		t.Fatalf("prepared/wrote before scope validation: %s", trace)
	}
}

func TestSingleExecutionRejectsStaleDebianWirelessSelectionBeforePreparing(t *testing.T) {
	backend, root, inputs := pipelineFixture(t)
	plan := pipelinePlan(inputs[0])
	plan.LiveToolGroups = []string{"wireless_security"}
	err := backend.executeCreate(plan, "/dev/test-usb", 8, false)
	if err == nil || !strings.Contains(err.Error(), "available only for: kali-linux") {
		t.Fatalf("expected scope error: %v", err)
	}
	trace := pipelineTrace(t, root)
	if strings.Contains(trace, "remaster") || strings.Contains(trace, "writer") {
		t.Fatalf("prepared/wrote before scope validation: %s", trace)
	}
}
