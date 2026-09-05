package app

import (
	"bufio"
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

func TestResolveCreateSourcePathDownloadsTestingChannel(t *testing.T) {
	tempDir := t.TempDir()
	helperPath := filepath.Join(tempDir, "helper.sh")
	argsPath := filepath.Join(tempDir, "args.txt")
	script := "#!/bin/sh\nset -eu\nIFS=$(printf '\\n\\t')\ncase \"${1-}\" in\n  list-local-isos) printf '[]\\n' ;;\n  download-managed-source) printf '%s\\n' \"$@\" >\"" + argsPath + "\"; printf '{\"path\":\"/data/downloads/debian-usb/iso/tails_live_iso_testing/testing.iso\"}\\n' ;;\n  *) printf 'unexpected command: %s\\n' \"${1-}\" >&2; exit 1 ;;\nesac\n"
	if err := os.WriteFile(helperPath, []byte(script), 0755); err != nil {
		t.Fatalf("write helper: %v", err)
	}

	application := App{
		backend: &Backend{pythonHelper: helperPath, configPath: "configs/debian-usb.conf"},
		reader:  bufio.NewReader(strings.NewReader("1\n2\n")),
		config: RuntimeConfig{
			ManagedSourceURLs: map[string]string{
				"TAILS_LIVE_ISO_STABLE_URL":  "https://example.test/tails-stable.iso",
				"TAILS_LIVE_ISO_TESTING_URL": "https://example.test/tails-testing.iso",
			},
		},
	}

	path, action, err := application.resolveCreateSourcePath(profileSpecs[profileTails], multiOSSourceRolePrimary)
	if err != nil {
		t.Fatalf("resolve Multi-OS source path: %v", err)
	}
	if action != menuStay {
		t.Fatalf("expected menuStay action, got %v", action)
	}
	if path != "/data/downloads/debian-usb/iso/tails_live_iso_testing/testing.iso" {
		t.Fatalf("unexpected downloaded path: %q", path)
	}
	args, err := os.ReadFile(argsPath)
	if err != nil {
		t.Fatalf("read helper args: %v", err)
	}
	if !strings.Contains(string(args), "--key\nTAILS_LIVE_ISO_TESTING_URL") {
		t.Fatalf("expected testing source key in helper args:\n%s", args)
	}
}

func TestCollectCreateRequestUsesDefaultKernelArgsWhenOverridingLiveConfig(t *testing.T) {
	tempDir := t.TempDir()
	helperPath := filepath.Join(tempDir, "helper.sh")
	script := "#!/bin/sh\nset -eu\nIFS=$(printf '\\n\\t')\ncase \"${1-}\" in\n  list-local-isos) printf '[]\\n' ;;\n  inspect-iso) printf '{\"iso_path\":\"/tmp/example.iso\",\"volume_id\":\"example\",\"media_class\":\"hybrid\",\"firmware\":[\"uefi\"],\"managed_supported\":true,\"supports_persistence\":true,\"supports_encrypted_persistence\":true,\"managed_payload_layout\":\"iso-store\",\"top_level_entries\":[\"Live system (amd64)\"],\"best_live_title\":\"Live system (amd64)\",\"best_installer_title\":\"Automated install\",\"warnings\":[]}' ;;\n  *) printf 'unexpected command: %s\\n' \"${1-}\" >&2; exit 1 ;;\nesac\n"
	if err := os.WriteFile(helperPath, []byte(script), 0755); err != nil {
		t.Fatalf("write helper: %v", err)
	}

	application := App{
		backend: &Backend{pythonHelper: helperPath},
		reader: bufio.NewReader(strings.NewReader(strings.Join([]string{
			"1",
			"/tmp/example.iso",
			"2",
			"a",
			"1",
			"n",
		}, "\n") + "\n")),
		config: RuntimeConfig{
			DefaultPersistenceSizeGiB: 30,
			DefaultBootPolicy:         "balanced",
			ProfileFallbackLiveKernelArgs: map[string]string{
				"debian": "boot=live components quiet splash noeject",
			},
		},
	}

	req, action, err := application.collectCreateRequest(profileSpecs["debian"])
	if err != nil {
		t.Fatalf("collect create request: %v", err)
	}
	if action != menuStay {
		t.Fatalf("expected menuStay action, got %v", action)
	}
	if !req.UseCustomGrubMenu {
		t.Fatalf("expected custom GRUB menu prompt to default to enabled")
	}
	if !req.PreserveUpstreamGrubEntries {
		t.Fatalf("expected preserved upstream GRUB entries to stay enabled by default")
	}
	if req.SecureBootTrust != secureBootTrustMOK {
		t.Fatalf("expected default managed trust mode to use MOK, got %q", req.SecureBootTrust)
	}
	if req.Preseed {
		t.Fatalf("expected Debian live-primary flow to avoid fixed preseed menus")
	}
	if req.KernelArgs != "" {
		t.Fatalf("expected no manual live override kernel args, got %q", req.KernelArgs)
	}
	if req.PersistenceSizeGiB != 0 {
		t.Fatalf("expected deterministic flow to skip prompted persistence sizing, got %d", req.PersistenceSizeGiB)
	}
}

func TestCollectCreateRequestSupportsPerRunToramSelection(t *testing.T) {
	tempDir := t.TempDir()
	helperPath := filepath.Join(tempDir, "helper.sh")
	script := "#!/bin/sh\nset -eu\nIFS=$(printf '\\n\\t')\ncase \"${1-}\" in\n  list-local-isos) printf '[]\\n' ;;\n  inspect-iso) printf '{\"iso_path\":\"/tmp/example.iso\",\"volume_id\":\"example\",\"media_class\":\"live\",\"firmware\":[\"uefi\"],\"managed_supported\":true,\"supports_persistence\":true,\"supports_encrypted_persistence\":false,\"managed_payload_layout\":\"iso-store\",\"top_level_entries\":[\"Live\"],\"best_live_title\":\"Live\",\"best_installer_title\":\"\",\"warnings\":[]}' ;;\n  *) printf 'unexpected command: %s\\n' \"${1-}\" >&2; exit 1 ;;\nesac\n"
	if err := os.WriteFile(helperPath, []byte(script), 0755); err != nil {
		t.Fatalf("write helper: %v", err)
	}
	application := App{
		backend: &Backend{pythonHelper: helperPath},
		reader: bufio.NewReader(strings.NewReader(strings.Join([]string{
			"1",
			"/tmp/example.iso",
			"2",
			"a",
			"1",
			"n",
		}, "\n") + "\n")),
		config: RuntimeConfig{
			DefaultPersistenceSizeGiB: 8,
			DefaultBootPolicy:         "balanced",
			ProfileFallbackLiveKernelArgs: map[string]string{
				"debian": "boot=live components quiet splash noeject",
			},
		},
	}
	req, action, err := application.collectCreateRequest(profileSpecs["debian"])
	if err != nil {
		t.Fatalf("collect create request: %v", err)
	}
	if action != menuStay {
		t.Fatalf("expected menuStay action, got %v", action)
	}
	if !req.UseCustomGrubMenu {
		t.Fatalf("expected custom GRUB menu to stay enabled for the per-run flow")
	}
	if !req.PreserveUpstreamGrubEntries {
		t.Fatalf("expected preserved upstream GRUB entries to be enabled by default")
	}
	if req.LiveToram {
		t.Fatalf("expected deterministic flow to skip per-run toram toggles")
	}
	if req.KernelArgs != "" {
		t.Fatalf("expected deterministic flow to skip manual live override kernel args, got %q", req.KernelArgs)
	}
}

func TestCollectCreateRequestAllowsManagedWriteForInstallerOnlyMedia(t *testing.T) {
	tempDir := t.TempDir()
	helperPath := filepath.Join(tempDir, "helper.sh")
	script := "#!/bin/sh\nset -eu\nIFS=$(printf '\\n\\t')\ncase \"${1-}\" in\n  list-local-isos) printf '[]\\n' ;;\n  inspect-iso) printf '{\"iso_path\":\"/tmp/netinst.iso\",\"volume_id\":\"netinst\",\"media_class\":\"installer\",\"firmware\":[\"uefi\"],\"managed_supported\":true,\"supports_persistence\":false,\"supports_encrypted_persistence\":false,\"managed_payload_layout\":\"iso-store\",\"top_level_entries\":[\"Install\"],\"best_live_title\":\"\",\"best_installer_title\":\"Install\",\"warnings\":[]}' ;;\n  *) printf 'unexpected command: %s\\n' \"${1-}\" >&2; exit 1 ;;\nesac\n"
	if err := os.WriteFile(helperPath, []byte(script), 0755); err != nil {
		t.Fatalf("write helper: %v", err)
	}
	application := App{
		backend: &Backend{pythonHelper: helperPath},
		reader: bufio.NewReader(strings.NewReader(strings.Join([]string{
			"/tmp/netinst.iso",
			"2",
			"1",
		}, "\n") + "\n")),
		config: RuntimeConfig{DefaultBootPolicy: "balanced"},
	}
	req, action, err := application.collectCreateRequest(profileSpecs["kali-purple"])
	if err != nil {
		t.Fatalf("collect create request: %v", err)
	}
	if action != menuStay {
		t.Fatalf("expected menuStay action, got %v", action)
	}
	if req.WriteMode != writeModeManaged {
		t.Fatalf("expected managed write mode, got %q", req.WriteMode)
	}
	if req.SecureBootTrust != secureBootTrustMOK {
		t.Fatalf("expected installer-only managed flow to keep MOK trust mode, got %q", req.SecureBootTrust)
	}
	if !req.Preseed {
		t.Fatalf("expected Kali Purple installer media to enable managed preseed entries")
	}
	if !req.PreserveUpstreamGrubEntries {
		t.Fatalf("expected preserved upstream entries for managed installer media")
	}
	if req.MenuLabel != "" {
		t.Fatalf("expected installer media to avoid seeding a live menu label, got %q", req.MenuLabel)
	}
	if req.OfflinePreseedSourceDir != "" {
		t.Fatalf("did not expect offline preseed source by default, got %q", req.OfflinePreseedSourceDir)
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

func TestPromptManagedInstallerSourceInitrdPreseedSupportsNetboot(t *testing.T) {
	root := t.TempDir()
	preseedPath := filepath.Join(root, "preseed-debian.cfg")
	if err := os.WriteFile(preseedPath, []byte("d-i auto-install/enable boolean true\n"), 0644); err != nil {
		t.Fatalf("write preseed fixture: %v", err)
	}
	application := App{
		backend: &Backend{preseedRoot: root},
		reader:  bufio.NewReader(strings.NewReader("y\n")),
	}

	got, action, err := application.promptManagedInstallerSourceInitrdPreseed(
		profileSpecs[profileDebian],
		multiOSSourceRoleNetboot,
	)
	if err != nil {
		t.Fatalf("prompt netboot preseed: %v", err)
	}
	if action != menuStay {
		t.Fatalf("expected menuStay, got %v", action)
	}
	if got != preseedPath {
		t.Fatalf("expected %q, got %q", preseedPath, got)
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

func TestCollectCreateRequestCanRemasterLiveSourceForEncryptedPersistence(t *testing.T) {
	tempDir := t.TempDir()
	helperPath := filepath.Join(tempDir, "helper.sh")
	script := "#!/bin/sh\nset -eu\nIFS=$(printf '\\n\\t')\ncase \"${1-}\" in\n  list-local-isos) printf '[]\\n' ;;\n  inspect-iso)\n    iso_path=''\n    shift\n    while [ \"$#\" -gt 0 ]; do\n      if [ \"$1\" = '--iso-path' ]]; then iso_path=\"$2\"; break; fi\n      shift\n    done\n    if [ \"$iso_path\" = '/tmp/remastered.iso' ]; then\n      printf '{\"iso_path\":\"/tmp/remastered.iso\",\"volume_id\":\"remastered\",\"media_class\":\"hybrid\",\"firmware\":[\"uefi\"],\"managed_supported\":true,\"supports_persistence\":true,\"supports_encrypted_persistence\":true,\"managed_payload_layout\":\"iso-store\",\"top_level_entries\":[\"Live\"],\"best_live_title\":\"Live\",\"best_installer_title\":\"Install\",\"warnings\":[]}\\n'\n    else\n      printf '{\"iso_path\":\"/tmp/source.iso\",\"volume_id\":\"source\",\"media_class\":\"hybrid\",\"firmware\":[\"uefi\"],\"managed_supported\":true,\"supports_persistence\":true,\"supports_encrypted_persistence\":false,\"managed_payload_layout\":\"iso-store\",\"top_level_entries\":[\"Live\"],\"best_live_title\":\"Live\",\"best_installer_title\":\"Install\",\"warnings\":[]}\\n'\n    fi\n    ;;\n  remaster-live-persistence-source) printf '{\"iso_path\":\"/tmp/remastered.iso\"}\\n' ;;\n  *) printf 'unexpected command: %s\\n' \"${1-}\" >&2; exit 1 ;;\nesac\n"
	if err := os.WriteFile(helperPath, []byte(script), 0755); err != nil {
		t.Fatalf("write helper: %v", err)
	}
	sudoPath := filepath.Join(tempDir, "sudo")
	if err := os.WriteFile(sudoPath, []byte("#!/bin/sh\nexec \"$@\"\n"), 0755); err != nil {
		t.Fatalf("write sudo shim: %v", err)
	}
	t.Setenv("PATH", tempDir+string(os.PathListSeparator)+os.Getenv("PATH"))

	application := App{
		backend: &Backend{pythonHelper: helperPath},
		reader: bufio.NewReader(strings.NewReader(strings.Join([]string{
			"1",
			"/tmp/source.iso",
			"2",
			"a",
			"1",
			"y",
			"y",
			"2",
			"8",
		}, "\n") + "\n")),
		config: RuntimeConfig{DefaultPersistenceSizeGiB: 8, DefaultBootPolicy: "balanced"},
	}
	req, action, err := application.collectCreateRequest(profileSpecs["debian"])
	if err != nil {
		t.Fatalf("collect create request: %v", err)
	}
	if action != menuStay {
		t.Fatalf("expected menuStay action, got %v", action)
	}
	if req.PersistenceMode != persistenceModeEncrypted {
		t.Fatalf("expected encrypted persistence after remaster, got %q", req.PersistenceMode)
	}
	if req.ISOPath != "/tmp/remastered.iso" {
		t.Fatalf("expected remastered ISO path, got %q", req.ISOPath)
	}
	if !req.Inspection.SupportsEncryptedPersistence {
		t.Fatalf("expected remastered inspection to report encrypted persistence support: %#v", req.Inspection)
	}
}

func TestCollectCreateRequestCanRemasterTailsSourceForEncryptedPersistence(t *testing.T) {
	tempDir := t.TempDir()
	helperPath := filepath.Join(tempDir, "helper.sh")
	script := "#!/bin/sh\nset -eu\nIFS=$(printf '\\n\\t')\ncase \"${1-}\" in\n  list-local-isos) printf '[]\\n' ;;\n  inspect-iso)\n    iso_path=''\n    shift\n    while [ \"$#\" -gt 0 ]; do\n      if [ \"$1\" = '--iso-path' ]]; then iso_path=\"$2\"; break; fi\n      shift\n    done\n    if [ \"$iso_path\" = '/tmp/remastered-tails.iso' ]; then\n      printf '{\"iso_path\":\"/tmp/remastered-tails.iso\",\"volume_id\":\"tails\",\"media_class\":\"hybrid\",\"firmware\":[\"uefi\"],\"managed_supported\":true,\"supports_persistence\":true,\"supports_encrypted_persistence\":true,\"managed_payload_layout\":\"iso-store\",\"top_level_entries\":[\"Tails\"],\"best_live_title\":\"Tails\",\"best_installer_title\":\"\",\"warnings\":[]}\\n'\n    else\n      printf '{\"iso_path\":\"/tmp/source-tails.iso\",\"volume_id\":\"tails\",\"media_class\":\"hybrid\",\"firmware\":[\"uefi\"],\"managed_supported\":true,\"supports_persistence\":true,\"supports_encrypted_persistence\":false,\"managed_payload_layout\":\"iso-store\",\"top_level_entries\":[\"Tails\"],\"best_live_title\":\"Tails\",\"best_installer_title\":\"\",\"warnings\":[]}\\n'\n    fi\n    ;;\n  remaster-live-persistence-source) printf '{\"iso_path\":\"/tmp/remastered-tails.iso\"}\\n' ;;\n  *) printf 'unexpected command: %s\\n' \"${1-}\" >&2; exit 1 ;;\nesac\n"
	if err := os.WriteFile(helperPath, []byte(script), 0755); err != nil {
		t.Fatalf("write helper: %v", err)
	}
	sudoPath := filepath.Join(tempDir, "sudo")
	if err := os.WriteFile(sudoPath, []byte("#!/bin/sh\nexec \"$@\"\n"), 0755); err != nil {
		t.Fatalf("write sudo shim: %v", err)
	}
	t.Setenv("PATH", tempDir+string(os.PathListSeparator)+os.Getenv("PATH"))

	application := App{
		backend: &Backend{pythonHelper: helperPath},
		reader: bufio.NewReader(strings.NewReader(strings.Join([]string{
			"2",
			"/tmp/source-tails.iso",
			"2",
			"1",
			"y",
			"y",
			"1",
			"8",
		}, "\n") + "\n")),
		config: RuntimeConfig{DefaultPersistenceSizeGiB: 8, DefaultBootPolicy: "balanced"},
	}
	req, action, err := application.collectCreateRequest(profileSpecs["tails"])
	if err != nil {
		t.Fatalf("collect create request: %v", err)
	}
	if action != menuStay {
		t.Fatalf("expected menuStay action, got %v", action)
	}
	if req.PersistenceMode != persistenceModeEncrypted {
		t.Fatalf("expected encrypted Tails persistence after remaster, got %q", req.PersistenceMode)
	}
	if req.ISOPath != "/tmp/remastered-tails.iso" {
		t.Fatalf("expected remastered Tails ISO path, got %q", req.ISOPath)
	}
	if !req.Inspection.SupportsEncryptedPersistence {
		t.Fatalf("expected remastered Tails inspection to report encrypted persistence support: %#v", req.Inspection)
	}
}

func TestCollectCreateRequestCapturesEncryptedPersistenceSettings(t *testing.T) {
	tempDir := t.TempDir()
	helperPath := filepath.Join(tempDir, "helper.sh")
	script := "#!/bin/sh\nset -eu\nIFS=$(printf '\\n\\t')\ncase \"${1-}\" in\n  list-local-isos) printf '[]\\n' ;;\n  inspect-iso) printf '{\"iso_path\":\"/tmp/example.iso\",\"volume_id\":\"example\",\"media_class\":\"hybrid\",\"firmware\":[\"uefi\"],\"managed_supported\":true,\"supports_persistence\":true,\"supports_encrypted_persistence\":true,\"managed_payload_layout\":\"iso-store\",\"top_level_entries\":[\"Live\"],\"best_live_title\":\"Live\",\"best_installer_title\":\"Install\",\"warnings\":[]}' ;;\n  *) printf 'unexpected command: %s\\n' \"${1-}\" >&2; exit 1 ;;\nesac\n"
	if err := os.WriteFile(helperPath, []byte(script), 0755); err != nil {
		t.Fatalf("write helper: %v", err)
	}

	application := App{
		backend: &Backend{pythonHelper: helperPath},
		reader: bufio.NewReader(strings.NewReader(strings.Join([]string{
			"1",
			"/tmp/example.iso",
			"2",
			"a",
			"1",
			"y",
			"2",
			"12",
		}, "\n") + "\n")),
		config: RuntimeConfig{
			DefaultPersistenceSizeGiB: 8,
			DefaultBootPolicy:         "balanced",
		},
	}

	req, action, err := application.collectCreateRequest(profileSpecs["debian"])
	if err != nil {
		t.Fatalf("collect create request: %v", err)
	}
	if action != menuStay {
		t.Fatalf("expected menuStay action, got %v", action)
	}
	if !req.Persistence {
		t.Fatal("expected persistence to be enabled")
	}
	if req.PersistenceMode != persistenceModeEncrypted {
		t.Fatalf("expected encrypted persistence mode, got %q", req.PersistenceMode)
	}
	if req.PersistenceSizeGiB != 12 {
		t.Fatalf("expected persistence size 12 GiB, got %d", req.PersistenceSizeGiB)
	}
}

func TestCollectMultiOSItemCapturesPersistenceSettingsForPrimarySource(t *testing.T) {
	tempDir := t.TempDir()
	helperPath := filepath.Join(tempDir, "helper.sh")
	script := "#!/bin/sh\nset -eu\nIFS=$(printf '\\n\\t')\ncase \"${1-}\" in\n  list-local-isos) printf '[]\\n' ;;\n  inspect-iso) printf '{\"iso_path\":\"/tmp/example.iso\",\"volume_id\":\"example\",\"media_class\":\"hybrid\",\"firmware\":[\"uefi\"],\"managed_supported\":true,\"supports_persistence\":true,\"supports_encrypted_persistence\":true,\"managed_payload_layout\":\"iso-store\",\"top_level_entries\":[\"Live\"],\"best_live_title\":\"Live\",\"best_installer_title\":\"Install\",\"warnings\":[]}' ;;\n  *) printf 'unexpected command: %s\\n' \"${1-}\" >&2; exit 1 ;;\nesac\n"
	if err := os.WriteFile(helperPath, []byte(script), 0755); err != nil {
		t.Fatalf("write helper: %v", err)
	}

	application := App{
		backend: &Backend{pythonHelper: helperPath},
		reader: bufio.NewReader(strings.NewReader(strings.Join([]string{
			"/tmp/example.iso",
			"a",
			"y",
			"2",
			"16",
		}, "\n") + "\n")),
		config: RuntimeConfig{
			DefaultPersistenceSizeGiB: 8,
			DefaultBootPolicy:         "balanced",
		},
	}

	item, action, err := application.collectMultiOSItem(profileSpecs["debian"], 1, 2, multiOSSourceRolePrimary)
	if err != nil {
		t.Fatalf("collect Multi-OS item: %v", err)
	}
	if action != menuStay {
		t.Fatalf("expected menuStay action, got %v", action)
	}
	if !item.Persistence {
		t.Fatal("expected Multi-OS item persistence to be enabled")
	}
	if item.PersistenceMode != persistenceModeEncrypted {
		t.Fatalf("expected encrypted Multi-OS persistence mode, got %q", item.PersistenceMode)
	}
	if item.PersistenceSizeGiB != 16 {
		t.Fatalf("expected Multi-OS persistence size 16 GiB, got %d", item.PersistenceSizeGiB)
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

func TestChoosePersistenceModeForTailsIsEncryptedOnly(t *testing.T) {
	application := App{
		reader: bufio.NewReader(strings.NewReader("1\n")),
	}

	mode, cancelled, err := application.choosePersistenceModeForProfile(profileTails)
	if err != nil {
		t.Fatalf("choose Tails persistence mode: %v", err)
	}
	if cancelled {
		t.Fatal("did not expect Tails persistence mode selection to cancel")
	}
	if mode != persistenceModeEncrypted {
		t.Fatalf("expected encrypted Tails persistence mode, got %q", mode)
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
	want := "boot=live components quiet splash noeject base=1 policy=2 extra=3 toram=filesystem.squashfs mem=6G findiso=${isofile} persistence persistence-label=DEBIAN-PERSIST persistence-media=removable-usb"
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
			DefaultBootPolicy:          "balanced",
			DefaultLiveHooks:           true,
			DefaultLiveArgsHooks:       "live-config.hooks=medium",
			DefaultLiveWifiInterface:   "wlan0",
			DefaultLiveWifiESSID:       "InstallNet",
			DefaultLiveWifiSecurity:    "wpa",
			DefaultLiveWifiCIDR:        "192.168.50.45/24",
			DefaultLiveWifiGateway:     "192.168.50.1",
			DefaultLiveWifiNameservers: "192.168.50.1,9.9.9.9",
			ProfileFallbackLiveKernelArgs: map[string]string{
				"debian": "boot=live components quiet splash noeject",
			},
		},
	}

	got := application.defaultLiveKernelArgs(profileSpecs["debian"], "")
	for _, token := range []string{
		"live-config.hooks=medium",
		"live_wifi_interface=wlan0",
		"live_wifi_security=wpa",
		"live_wifi_essid_b64=SW5zdGFsbE5ldA",
		"live_wifi_cidr=192.168.50.45/24",
		"live_wifi_gateway=192.168.50.1",
		"live_wifi_nameservers=192.168.50.1,9.9.9.9",
	} {
		if !strings.Contains(got, token) {
			t.Fatalf("expected %q in live args, got %q", token, got)
		}
	}
	for _, forbidden := range []string{"live_wifi_psk=", "live_wifi_psk_b64=", "netcfg/wireless_wpa="} {
		if strings.Contains(got, forbidden) {
			t.Fatalf("expected Live Wi-Fi passphrase transport %q to stay out of kernel args, got %q", forbidden, got)
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
