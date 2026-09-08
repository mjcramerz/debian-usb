package app

import (
	"bufio"
	"encoding/json"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
)

func TestEligibleDevicesSkipsSystemAndFixedDisks(t *testing.T) {
	devices := []Device{
		{Path: "/dev/sda", Removable: true, SystemDisk: true},
		{Path: "/dev/sdb", Removable: false},
		{Path: "/dev/sdc", Removable: true},
	}

	got := eligibleDevices(devices)
	if len(got) != 1 {
		t.Fatalf("expected exactly one eligible device, got %#v", got)
	}
	if got[0].Path != "/dev/sdc" {
		t.Fatalf("expected /dev/sdc to remain eligible, got %#v", got)
	}
}

func TestChooseWriteModeGoBackReturnsMenuBack(t *testing.T) {
	application := App{
		reader: bufio.NewReader(strings.NewReader("b\n")),
	}

	mode, action, err := application.chooseWriteMode(
		profileSpecs["debian"],
		ISOInspection{MediaClass: "hybrid", Firmware: []string{"uefi"}, ManagedSupported: true, ManagedPayloadLayout: "iso-store"},
		multiOSSourceRolePrimary,
	)
	if err != nil {
		t.Fatalf("choose write mode: %v", err)
	}
	if mode != "" {
		t.Fatalf("expected no write mode when going back, got %q", mode)
	}
	if action != menuBack {
		t.Fatalf("expected menuBack action, got %v", action)
	}
}

func TestChooseMultiOSSourcesReturnsSelectedDebianAndKaliLiveSources(t *testing.T) {
	application := App{
		reader: bufio.NewReader(strings.NewReader("1\n4\n9\n")),
	}

	sources, action, err := application.chooseMultiOSSources()
	if err != nil {
		t.Fatalf("choose Multi-OS sources: %v", err)
	}
	if action != menuStay {
		t.Fatalf("expected menuStay action, got %v", action)
	}
	if len(sources) != 2 || sources[0].Profile != profileDebian || sources[0].SourceRole != multiOSSourceRolePrimary || sources[1].Profile != profileKaliLinux || sources[1].SourceRole != multiOSSourceRolePrimary {
		t.Fatalf("unexpected selected sources: %#v", sources)
	}
}

func TestChooseMultiOSSourcesAllowsSingleNetinstSelection(t *testing.T) {
	application := App{
		reader: bufio.NewReader(strings.NewReader("2\n9\n")),
	}

	sources, action, err := application.chooseMultiOSSources()
	if err != nil {
		t.Fatalf("choose Multi-OS sources: %v", err)
	}
	if action != menuStay {
		t.Fatalf("expected menuStay action, got %v", action)
	}
	if len(sources) != 1 {
		t.Fatalf("unexpected selected sources: %#v", sources)
	}
	if sources[0].Profile != profileDebian || sources[0].SourceRole != multiOSSourceRoleNetinst {
		t.Fatalf("expected Debian Netinst, got %#v", sources[0])
	}
}

func TestMultiOSSourceOptionsKeepLiveAndNetinstIndependent(t *testing.T) {
	selected := map[string]bool{
		multiOSSourceSelectionKey(profileDebian, multiOSSourceRoleNetinst): true,
	}

	if label := multiOSSelectedLabel(selected); label != "Debian Netinst" {
		t.Fatalf("expected an isolated Debian Netinst selection, got %q", label)
	}
	for _, option := range multiOSSourceOptions {
		spec := profileSpecs[option.Profile]
		if option.SourceRole == multiOSSourceRolePrimary && spec.PreferredMedia == "live" && strings.Contains(strings.ToLower(option.Label), "installer") {
			t.Fatalf("live source option must not expose a combined installer choice: %#v", option)
		}
	}
}

func TestMultiOSSourceOptionsExcludeUbuntu(t *testing.T) {
	for _, option := range multiOSSourceOptions {
		if option.Profile == profileUbuntuDesktop || option.Profile == profileUbuntuServer {
			t.Fatalf("Ubuntu must remain a separate single-OS workflow, got Multi-OS option %#v", option)
		}
	}
}

func TestCollectSourceSelectionDefersTestingChannelDownload(t *testing.T) {
	application := App{reader: bufio.NewReader(strings.NewReader("1\n2\n")), config: RuntimeConfig{ManagedSourceURLs: map[string]string{
		"TAILS_LIVE_ISO_STABLE_URL":  "https://example.test/tails-stable.iso",
		"TAILS_LIVE_ISO_TESTING_URL": "https://example.test/tails-testing.iso",
	}}}
	// A nil backend is deliberate: recording remote inputs cannot invoke a helper.
	source, action, err := application.collectSourceSelection(profileSpecs[profileTails], multiOSSourceRolePrimary)
	if err != nil || action != menuStay {
		t.Fatalf("collect remote selection: %v, %v", action, err)
	}
	if source.ISO.Path != "" || source.ISO.URLKey != "TAILS_LIVE_ISO_TESTING_URL" || source.ISO.URL != "https://example.test/tails-testing.iso" {
		t.Fatalf("unexpected deferred source: %#v", source)
	}
}

func TestCollectCreateRequestUsesDefaultKernelArgsWhenOverridingLiveConfig(t *testing.T) {
	a, iso := selectionFixture(t, "hybrid", true, "1\n{iso}\n2\na\n1\nn\n")
	req, action, err := a.collectCreateRequest(profileSpecs[profileDebian])
	if err != nil || action != menuStay {
		t.Fatalf("collect: %v %v", action, err)
	}
	if req.ISOPath != iso || req.Preparation == nil || req.Preparation.InitrdOverlayDir == "" {
		t.Fatalf("expected original source + pending overlay: %#v", req)
	}
	if !req.UseCustomGrubMenu || !req.PreserveUpstreamGrubEntries || req.SecureBootTrust != secureBootTrustMOK {
		t.Fatalf("managed defaults: %#v", req)
	}
	if req.Preseed || req.KernelArgs != "" || req.PersistenceSizeGiB != 0 {
		t.Fatalf("unexpected live defaults: %#v", req)
	}
}

func TestCollectCreateRequestDoesNotCopyLiveFilesystemToRAMByDefault(t *testing.T) {
	a, iso := selectionFixture(t, "live", false, "1\n{iso}\n2\na\n1\nn\n")
	req, action, err := a.collectCreateRequest(profileSpecs[profileDebian])
	if err != nil || action != menuStay {
		t.Fatalf("collect: %v %v", action, err)
	}
	if req.ISOPath != iso || req.LiveToram || req.KernelArgs != "" {
		t.Fatalf("unexpected live defaults: %#v", req)
	}
}

func TestCollectLiveInitrdOverlayAutomaticallyRecordsDebianLiveRoot(t *testing.T) {
	root := t.TempDir()
	overlay := filepath.Join(root, "debian", "live")
	if err := os.MkdirAll(overlay, 0755); err != nil {
		t.Fatal(err)
	}
	application := App{backend: &Backend{initrdRoot: root}, reader: bufio.NewReader(strings.NewReader(""))}
	selected, action, err := application.collectLiveInitrdOverlay(profileSpecs[profileDebian], ISOInspection{MediaClass: "live"})
	if err != nil || action != menuStay || selected != overlay {
		t.Fatalf("record required overlay: %q, %v, %v", selected, action, err)
	}
}

func TestCollectCreateRequestAllowsManagedWriteForInstallerOnlyMedia(t *testing.T) {
	a, _ := selectionFixture(t, "installer", false, "{iso}\n2\n1\n")
	req, action, err := a.collectCreateRequest(profileSpecs[profileKaliPurple])
	if err != nil || action != menuStay {
		t.Fatalf("collect: %v %v", action, err)
	}
	if req.WriteMode != writeModeManaged || req.SecureBootTrust != secureBootTrustMOK || !req.Preseed || !req.PreserveUpstreamGrubEntries {
		t.Fatalf("installer settings: %#v", req)
	}
}

func TestPromptManagedInstallerSourceExtraModulesSupportsMultiSelect(t *testing.T) {
	application := App{
		reader: bufio.NewReader(strings.NewReader(strings.Join([]string{
			"1",
			"2",
			"3",
		}, "\n") + "\n")),
	}

	modules, action, err := application.promptManagedInstallerSourceExtraModules(
		profileSpecs["debian"],
		multiOSSourceRoleNetinst,
		"/tmp/vmlinuz",
		"/tmp/initrd.gz",
	)
	if err != nil {
		t.Fatalf("prompt netinst extra modules: %v", err)
	}
	if action != menuStay {
		t.Fatalf("expected menuStay action, got %v", action)
	}
	if !reflect.DeepEqual(modules, []string{"xxhash_generic", "lz4"}) {
		t.Fatalf("unexpected selected modules: %#v", modules)
	}
}

func TestPromptInitrdOverlayContentMapsEveryOSAndStage(t *testing.T) {
	root := t.TempDir()
	tests := []struct {
		name       string
		profile    string
		sourceRole string
		mediaClass string
		want       string
	}{
		{name: "debian netinst", profile: profileDebian, sourceRole: multiOSSourceRoleNetinst, want: filepath.Join(root, "debian", "netinst")},
		{name: "kali netboot", profile: profileKaliLinux, sourceRole: multiOSSourceRoleNetboot, want: filepath.Join(root, "kali", "netboot")},
		{name: "ubuntu live", profile: profileUbuntuDesktop, sourceRole: multiOSSourceRolePrimary, mediaClass: "live", want: filepath.Join(root, "ubuntu", "live")},
		{name: "tails live", profile: profileTails, sourceRole: multiOSSourceRolePrimary, mediaClass: "live", want: filepath.Join(root, "tails", "live")},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			if err := os.MkdirAll(test.want, 0755); err != nil {
				t.Fatalf("create initrd overlay fixture: %v", err)
			}
			application := App{
				backend: &Backend{initrdRoot: root},
				reader:  bufio.NewReader(strings.NewReader("y\n")),
			}

			got, action, err := application.promptInitrdOverlayContent(
				profileSpecs[test.profile],
				test.sourceRole,
				test.mediaClass,
			)
			if err != nil {
				t.Fatalf("prompt initrd overlay content: %v", err)
			}
			if action != menuStay {
				t.Fatalf("expected menuStay, got %v", action)
			}
			if got != test.want {
				t.Fatalf("expected %q, got %q", test.want, got)
			}
		})
	}
}

func TestPromptManagedInstallerSourceStrategyAllowsHostKernelSelection(t *testing.T) {
	application := App{
		reader: bufio.NewReader(strings.NewReader("1\n")),
	}

	strategy, action, err := application.promptManagedInstallerSourceStrategy(
		profileSpecs["debian"],
		multiOSSourceRoleNetinst,
		"/tmp/vmlinuz",
		"/tmp/initrd.gz",
		[]string{"xxhash_generic", "lz4"},
	)
	if err != nil {
		t.Fatalf("prompt module strategy: %v", err)
	}
	if action != menuStay {
		t.Fatalf("expected menuStay action, got %v", action)
	}
	if strategy != installerModuleSourceStrategyHostKernel {
		t.Fatalf("expected host kernel strategy, got %q", strategy)
	}
}

func TestPromptManagedInstallerSourceStrategyAllowsSourceRebuildSelection(t *testing.T) {
	application := App{
		reader: bufio.NewReader(strings.NewReader("2\n")),
	}

	strategy, action, err := application.promptManagedInstallerSourceStrategy(
		profileSpecs["debian"],
		multiOSSourceRoleNetinst,
		"/tmp/vmlinuz",
		"/tmp/initrd.gz",
		[]string{"xxhash_generic", "lz4"},
	)
	if err != nil {
		t.Fatalf("prompt module strategy: %v", err)
	}
	if action != menuStay {
		t.Fatalf("expected menuStay action, got %v", action)
	}
	if strategy != installerModuleSourceStrategySourceUDEB {
		t.Fatalf("expected source udeb strategy, got %q", strategy)
	}
}

func TestCollectCreateRequestDefersMissingEncryptedPersistenceSupport(t *testing.T) {
	a, iso := selectionFixture(t, "live", false, "1\n{iso}\n2\na\n1\ny\n2\n8\n")
	req, action, err := a.collectCreateRequest(profileSpecs[profileDebian])
	if err != nil || action != menuStay {
		t.Fatalf("collect: %v %v", action, err)
	}
	if req.PersistenceMode != persistenceModeEncrypted || req.ISOPath != iso || req.Inspection.SupportsEncryptedPersistence {
		t.Fatalf("expected pending automatic crypto support: %#v", req)
	}
	if _, err := buildCreatePlan(a.config, req); err != nil {
		t.Fatalf("pending crypto plan must be valid: %v", err)
	}
}

func TestCollectCreateRequestLeavesTailsUnmodifiedWithoutPersistence(t *testing.T) {
	a, iso := selectionFixture(t, "live", false, "{iso}\n2\n1\ny\n1\n8\n")
	a.backend.initrdRoot = "" // No optional Tails overlay installed in this fixture.
	req, action, err := a.collectCreateRequest(profileSpecs[profileTails])
	if err != nil || action != menuStay {
		t.Fatalf("collect: %v %v", action, err)
	}
	if req.PersistenceMode != persistenceModeNone || req.Persistence || req.ISOPath != iso || req.Inspection.SupportsEncryptedPersistence {
		t.Fatalf("expected Tails without generic persistence or remaster: %#v", req)
	}
}

func TestCollectCreateRequestCapturesEncryptedPersistenceSettings(t *testing.T) {
	a, iso := selectionFixture(t, "live", true, "1\n{iso}\n2\na\n1\ny\n2\n12\n")
	req, action, err := a.collectCreateRequest(profileSpecs[profileDebian])
	if err != nil || action != menuStay {
		t.Fatalf("collect: %v %v", action, err)
	}
	if req.ISOPath != iso || !req.Persistence || req.PersistenceMode != persistenceModeEncrypted || req.PersistenceSizeGiB != 12 {
		t.Fatalf("persistence settings: %#v", req)
	}
}

func TestCollectMultiOSItemCapturesPersistenceSettingsForPrimarySource(t *testing.T) {
	a, iso := selectionFixture(t, "live", false, "{iso}\na\ny\n2\n16\n")
	item, action, err := a.collectMultiOSItem(profileSpecs[profileDebian], 1, 2, multiOSSourceRolePrimary)
	if err != nil || action != menuStay {
		t.Fatalf("collect: %v %v", action, err)
	}
	if item.ISOPath != iso || item.Preparation == nil || !item.Persistence || item.PersistenceMode != persistenceModeEncrypted || item.PersistenceSizeGiB != 16 {
		t.Fatalf("deferred Multi-OS selection: %#v", item)
	}
}

func TestPromptRequiredStringAllowsOverridingDefaultValue(t *testing.T) {
	application := App{
		reader: bufio.NewReader(strings.NewReader("boot=casper quiet splash noeject nomodeset\n")),
	}

	got, err := application.promptRequiredString("Kernel boot arguments", "boot=casper quiet splash noeject")
	if err != nil {
		t.Fatalf("prompt required string: %v", err)
	}
	if got != "boot=casper quiet splash noeject nomodeset" {
		t.Fatalf("expected typed override to win, got %q", got)
	}
}

func TestNormalizePathInputStripsShellQuotes(t *testing.T) {
	got := normalizePathInput("  '/tmp/debian.iso'  ")
	if got != "/tmp/debian.iso" {
		t.Fatalf("expected quoted path to be unwrapped, got %q", got)
	}
}

func TestResolveAbsolutePathInputStripsShellQuotesAndMakesAbsolute(t *testing.T) {
	want, err := filepath.Abs("./configs/debian-usb.conf")
	if err != nil {
		t.Fatalf("filepath.Abs: %v", err)
	}
	got, err := resolveAbsolutePathInput("  './configs/debian-usb.conf'  ")
	if err != nil {
		t.Fatalf("resolveAbsolutePathInput: %v", err)
	}
	if got != want {
		t.Fatalf("expected %q, got %q", want, got)
	}
}

func TestEditMultiOSRequestItemCanUpdateMenuLabel(t *testing.T) {
	application := App{
		reader: bufio.NewReader(strings.NewReader(strings.Join([]string{
			"6",
			"",
			"",
			"",
			"Updated Debian Live",
		}, "\n") + "\n")),
	}
	request := MultiOSRequest{
		ProfileOfflinePreseedDirs: map[string]string{},
		Items: []MultiOSRequestItem{
			{
				Profile:    profileDebian,
				SourceRole: multiOSSourceRolePrimary,
				ISOPath:    "/tmp/debian.iso",
				Inspection: ISOInspection{MediaClass: "hybrid"},
				MenuLabel:  "Debian Live",
				KernelArgs: "boot=live quiet",
				KernelPath: "/live/vmlinuz",
				InitrdPath: "/live/initrd.img",
			},
		},
	}

	changed, err := application.editMultiOSRequestItem(&request, 0)
	if err != nil {
		t.Fatalf("editMultiOSRequestItem: %v", err)
	}
	if !changed {
		t.Fatal("expected editMultiOSRequestItem to report a change")
	}
	if request.Items[0].MenuLabel != "Updated Debian Live" {
		t.Fatalf("expected updated menu label, got %q", request.Items[0].MenuLabel)
	}
	if request.Items[0].KernelArgs != "boot=live quiet" {
		t.Fatalf("expected kernel args to stay unchanged, got %q", request.Items[0].KernelArgs)
	}
}

func TestSinglePlannedExecutionEditMenuEntriesHideLiveOverridesForInstallerOnlyPlan(t *testing.T) {
	entries := singlePlannedExecutionEditMenuEntries(PlannedExecution{
		SinglePlan: &CreatePlan{
			Profile:    profileKaliPurple,
			MediaClass: "installer",
		},
	})
	for _, entry := range entries {
		if entry.Key == "8" {
			t.Fatalf("did not expect live override entry for installer-only saved plan: %#v", entries)
		}
	}
}

func TestTargetDeviceForPlannedExecutionFallsBackWhenSavedDeviceIsNotEligible(t *testing.T) {
	tempDir := t.TempDir()
	helperPath := filepath.Join(tempDir, "helper.sh")
	script := "#!/bin/sh\nset -eu\nIFS=$(printf '\\n\\t')\ncase \"${1-}\" in\n  list-devices) printf '[{\"name\":\"USB\",\"path\":\"/dev/sdc\",\"model\":\"Test\",\"transport\":\"usb\",\"size_human\":\"16G\",\"removable\":true,\"mounted\":false,\"system_disk\":false}]\\n' ;;\n  *) printf 'unexpected command: %s\\n' \"${1-}\" >&2; exit 1 ;;\nesac\n"
	if err := os.WriteFile(helperPath, []byte(script), 0755); err != nil {
		t.Fatalf("write helper: %v", err)
	}

	application := App{
		backend: &Backend{pythonHelper: helperPath},
		reader:  bufio.NewReader(strings.NewReader("y\n1\n")),
	}

	target, action, err := application.targetDeviceForPlannedExecution(PlannedExecution{
		TargetDevicePath: "/dev/sdz",
	})
	if err != nil {
		t.Fatalf("targetDeviceForPlannedExecution: %v", err)
	}
	if action != menuStay {
		t.Fatalf("expected menuStay action, got %v", action)
	}
	if target != "/dev/sdc" {
		t.Fatalf("expected fallback target /dev/sdc, got %q", target)
	}
}

func TestMultiOSRequestItemSupportsLiveOverridesOnlyForLiveCapablePrimaryItems(t *testing.T) {
	if !multiOSRequestItemSupportsLiveOverrides(MultiOSRequestItem{
		Profile:    profileDebian,
		SourceRole: multiOSSourceRolePrimary,
		Inspection: ISOInspection{MediaClass: "hybrid"},
	}) {
		t.Fatal("expected primary Debian hybrid item to support live overrides")
	}
	if multiOSRequestItemSupportsLiveOverrides(MultiOSRequestItem{
		Profile:    profileUbuntuServer,
		SourceRole: multiOSSourceRolePrimary,
		Inspection: ISOInspection{MediaClass: "installer"},
	}) {
		t.Fatal("did not expect installer-only item to support live overrides")
	}
	if multiOSRequestItemSupportsLiveOverrides(MultiOSRequestItem{
		Profile:    profileDebian,
		SourceRole: multiOSSourceRoleNetinst,
		Inspection: ISOInspection{MediaClass: "installer"},
	}) {
		t.Fatal("did not expect netinst item to support live overrides")
	}
}

func TestChoosePersistenceModeCanDisablePersistence(t *testing.T) {
	application := App{
		reader: bufio.NewReader(strings.NewReader("b\n")),
	}

	mode, cancelled, err := application.choosePersistenceMode()
	if err != nil {
		t.Fatalf("choose persistence mode: %v", err)
	}
	if mode != "" {
		t.Fatalf("expected empty mode when disabling persistence, got %q", mode)
	}
	if !cancelled {
		t.Fatalf("expected cancellation flag to be true")
	}
}

func TestChoosePersistenceModeForTailsRefusesGenericPersistence(t *testing.T) {
	application := App{reader: bufio.NewReader(strings.NewReader("1\n"))}
	mode, cancelled, err := application.choosePersistenceModeForProfile(profileTails)
	if err == nil || !cancelled || mode != persistenceModeNone {
		t.Fatalf("expected refusal without consuming a persistence choice: %q %v %v", mode, cancelled, err)
	}
}

func TestChooseLiveBootPolicySupportsHardened(t *testing.T) {
	application := App{
		reader: bufio.NewReader(strings.NewReader("3\n")),
		config: RuntimeConfig{DefaultBootPolicy: "balanced"},
	}

	policy, err := application.chooseLiveBootPolicy()
	if err != nil {
		t.Fatalf("choose live boot policy: %v", err)
	}
	if policy != "hardened" {
		t.Fatalf("expected hardened policy, got %q", policy)
	}
}

func TestDefaultLiveKernelArgsApplyConfiguredSettings(t *testing.T) {
	application := App{
		config: RuntimeConfig{
			DefaultBootPolicy: "balanced",
			DefaultLiveToram:  true,
			DefaultLiveMemGiB: 6,
			DefaultPartitionLabels: map[string]string{
				configLabelDebianPersist: "DEBIAN-PERSIST",
			},
			SharedLiveBaseKernelArgs:     "base=1",
			BootPolicyBalancedKernelArgs: "policy=2",
			DefaultLiveKernelExtras:      "extra=3",
			ProfileFallbackLiveKernelArgs: map[string]string{
				"debian": "boot=live components quiet splash noeject",
			},
		},
	}

	got := application.defaultLiveKernelArgs(profileSpecs["debian"], "plain")
	want := "boot=live components quiet splash noeject base=1 policy=2 extra=3 live-config.hooks=medium toram=filesystem.squashfs mem=6G findiso=${isofile} persistence persistence-label=DEBIAN-PERSIST persistence-media=removable-usb persistence-storage=filesystem union=overlay"
	if got != want {
		t.Fatalf("expected %q, got %q", want, got)
	}
}

func TestDefaultInstallerKernelArgsRemoveLiveOnlyToram(t *testing.T) {
	config := RuntimeConfig{
		DefaultInstallerPolicy:       "preserve",
		InstallerPolicyPreserveArgs:  "auto=true toram toram=filesystem.squashfs",
		DefaultInstallerKernelExtras: "priority=critical toram=filesystem.erofs",
	}

	got := defaultInstallerKernelArgsForConfig(config)
	for _, token := range strings.Fields(got) {
		if token == "toram" || strings.HasPrefix(token, "toram=") {
			t.Fatalf("installer arguments retained Live-only RAM token %q in %q", token, got)
		}
	}
	if !strings.Contains(got, "auto=true") || !strings.Contains(got, "priority=critical") {
		t.Fatalf("installer argument cleanup removed unrelated values: %q", got)
	}
}

func TestDefaultLiveKernelArgsApplyLiveHookSettingsWhenEnabled(t *testing.T) {
	application := App{
		config: RuntimeConfig{
			DefaultBootPolicy:    "balanced",
			DefaultLiveHooks:     true,
			DefaultLiveArgsHooks: "live-config.hooks=medium",
			ProfileFallbackLiveKernelArgs: map[string]string{
				"debian": "boot=live components quiet splash noeject",
			},
		},
	}

	got := application.defaultLiveKernelArgs(profileSpecs["debian"], "")
	if !strings.Contains(got, "live-config.hooks=medium") {
		t.Fatalf("expected mandatory Live hook selector in %q", got)
	}
	for _, forbidden := range []string{"live_wifi_", "LIVE_WIFI_", "netcfg/wireless_"} {
		if strings.Contains(got, forbidden) {
			t.Fatalf("expected Live Wi-Fi transport %q to stay out of kernel args, got %q", forbidden, got)
		}
	}
	kaliArgs := application.defaultLiveKernelArgs(profileSpecs[profileKaliLinux], "")
	if strings.Contains(kaliArgs, "live_wifi_") || strings.Contains(kaliArgs, "live-config.hooks=medium") {
		t.Fatalf("expected Debian-only hook arguments to stay out of Kali Live args, got %q", kaliArgs)
	}
}

func TestDefaultLiveKernelArgsKeepUbuntuServerCasperStyle(t *testing.T) {
	application := App{
		config: RuntimeConfig{
			DefaultBootPolicy: "balanced",
			ProfileFallbackLiveKernelArgs: map[string]string{
				"ubuntu-server": "boot=casper quiet splash noeject",
			},
		},
	}

	got := application.defaultLiveKernelArgs(profileSpecs["ubuntu-server"], "plain")
	if !strings.Contains(got, "boot=casper") {
		t.Fatalf("expected casper kernel args, got %q", got)
	}
	if strings.Contains(got, "ignore_uuid") {
		t.Fatalf("did not expect ignore_uuid for ubuntu-server casper args, got %q", got)
	}
	if strings.Contains(got, "persistent") {
		t.Fatalf("did not expect persistence flags for ubuntu-server, got %q", got)
	}
}

func TestDefaultLiveKernelArgsKeepTailsPersistenceOutOfKernelArgs(t *testing.T) {
	application := App{
		config: RuntimeConfig{
			DefaultBootPolicy: "balanced",
			DefaultPartitionLabels: map[string]string{
				configLabelTailsPersist: "TailsData",
			},
			ProfileFallbackLiveKernelArgs: map[string]string{
				"tails": "boot=live components quiet splash noeject",
			},
		},
	}

	got := application.defaultLiveKernelArgs(profileSpecs["tails"], persistenceModeEncrypted)
	if !strings.Contains(got, "findiso=${isofile}") {
		t.Fatalf("expected Tails live args to keep findiso, got %q", got)
	}
	for _, fragment := range []string{"persistence", "persistent=cryptsetup", "persistence-label=", "persistence-media="} {
		if strings.Contains(got, fragment) {
			t.Fatalf("did not expect %q in Tails live args: %q", fragment, got)
		}
	}
}

func TestLivePolicyKernelArgsUsesConfiguredSharedAndPolicyArgs(t *testing.T) {
	application := App{
		config: RuntimeConfig{
			DefaultBootPolicy:            "hardened",
			SharedLiveBaseKernelArgs:     "foo=1 base=yes",
			BootPolicyHardenedArgs:       "bar=2 baz=3",
			BootPolicyBalancedKernelArgs: "unused=1",
		},
	}

	got := application.livePolicyKernelArgs()
	want := "foo=1 base=yes bar=2 baz=3"
	if got != want {
		t.Fatalf("expected %q, got %q", want, got)
	}
}

func selectionFixture(t *testing.T, media string, encrypted bool, inputs string) (*App, string) {
	t.Helper()
	root := t.TempDir()
	iso := filepath.Join(root, "source.iso")
	if err := os.WriteFile(iso, []byte("fixture ISO"), 0644); err != nil {
		t.Fatal(err)
	}
	initrd := filepath.Join(root, "initrd")
	if err := os.MkdirAll(filepath.Join(initrd, "debian", "live"), 0755); err != nil {
		t.Fatal(err)
	}
	inspection := ISOInspection{ISOPath: iso, MediaClass: media, ManagedSupported: true, ManagedPayloadLayout: "iso-store", SupportsPersistence: media != "installer", SupportsEncryptedPersistence: encrypted, BestLiveTitle: "Live", BestInstallerTitle: "Install"}
	payload, err := json.Marshal(inspection)
	if err != nil {
		t.Fatal(err)
	}
	helper := filepath.Join(root, "helper.sh")
	script := "#!/bin/sh\nset -eu\ncase \"${1-}\" in\nlist-local-isos) printf '[]\\n';;\ninspect-iso) printf '%s\\n' '" + string(payload) + "';;\n*) echo 'BUILD DURING SELECTION' >&2; exit 99;;\nesac\n"
	if err := os.WriteFile(helper, []byte(script), 0755); err != nil {
		t.Fatal(err)
	}
	inputs = strings.ReplaceAll(inputs, "{iso}", iso)
	return &App{backend: &Backend{pythonHelper: helper, initrdRoot: initrd, effectiveUID: func() int { return 0 }}, reader: bufio.NewReader(strings.NewReader(inputs)), config: RuntimeConfig{DefaultPersistenceSizeGiB: 8, DefaultBootPolicy: "balanced"}}, iso
}
