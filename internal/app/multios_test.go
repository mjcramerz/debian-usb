package app

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestBuildMultiOSPlanRequiresAtLeastOneSource(t *testing.T) {
	cfg, err := loadRuntimeConfig(repoConfigPath())
	if err != nil {
		t.Fatalf("load runtime config: %v", err)
	}
	_, err = buildMultiOSPlan(cfg, MultiOSRequest{})
	if err == nil || !strings.Contains(err.Error(), "at least one") {
		t.Fatalf("expected at least one source error, got %v", err)
	}
}

func TestBuildMultiOSPlanRejectsUbuntuProfiles(t *testing.T) {
	cfg, err := loadRuntimeConfig(repoConfigPath())
	if err != nil {
		t.Fatalf("load runtime config: %v", err)
	}
	for _, profile := range []string{profileUbuntuDesktop, profileUbuntuServer} {
		t.Run(profile, func(t *testing.T) {
			_, err := buildMultiOSPlan(cfg, MultiOSRequest{Items: []MultiOSRequestItem{{
				Profile:    profile,
				SourceRole: multiOSSourceRolePrimary,
				ISOPath:    writeTestISO(t, profile+".iso"),
				Inspection: ISOInspection{
					MediaClass:       "live",
					ManagedSupported: true,
				},
			}}})
			if err == nil || !strings.Contains(err.Error(), "not supported in Multi-OS") {
				t.Fatalf("expected Ubuntu Multi-OS rejection, got %v", err)
			}
		})
	}
}

func TestBuildMultiOSPlanAllowsSingleProfileWithNetinst(t *testing.T) {
	cfg, err := loadRuntimeConfig(repoConfigPath())
	if err != nil {
		t.Fatalf("load runtime config: %v", err)
	}
	request := MultiOSRequest{
		UseCustomGrubMenu: true,
		Items: []MultiOSRequestItem{
			{
				Profile:    profileDebian,
				SourceRole: multiOSSourceRolePrimary,
				ISOPath:    writeTestISO(t, "debian-live.iso"),
				Inspection: ISOInspection{
					MediaClass:           "hybrid",
					Firmware:             []string{"uefi"},
					ManagedSupported:     true,
					ManagedPayloadLayout: "iso-store",
					SupportsPersistence:  true,
					BestInstallerTitle:   "Install",
				},
			},
			{
				Profile:    profileDebian,
				SourceRole: multiOSSourceRoleNetinst,
				ISOPath:    writePreparedInstallerSource(t, multiOSSourceRoleNetinst, "debian-netinst.iso"),
				Inspection: ISOInspection{
					MediaClass:           "installer",
					Firmware:             []string{"uefi"},
					ManagedSupported:     true,
					ManagedPayloadLayout: "iso-store",
					BestInstallerTitle:   "Install",
				},
			},
		},
	}
	plan, err := buildMultiOSPlan(cfg, request)
	if err != nil {
		t.Fatalf("build Multi-OS plan: %v", err)
	}
	if len(plan.Items) != 2 {
		t.Fatalf("expected two items, got %#v", plan.Items)
	}
	if plan.Items[0].Profile != profileDebian || plan.Items[1].Profile != profileDebian {
		t.Fatalf("expected both items to stay on Debian, got %#v", plan.Items)
	}
}

func TestBuildMultiOSPlanRejectsBareNetinstISOWithoutHDMediaAssets(t *testing.T) {
	cfg, err := loadRuntimeConfig(repoConfigPath())
	if err != nil {
		t.Fatalf("load runtime config: %v", err)
	}
	_, err = buildMultiOSPlan(cfg, MultiOSRequest{
		Items: []MultiOSRequestItem{{
			Profile:    profileDebian,
			SourceRole: multiOSSourceRoleNetinst,
			ISOPath:    writeTestISO(t, "debian-netinst.iso"),
			Inspection: ISOInspection{
				MediaClass:           "installer",
				Firmware:             []string{"uefi"},
				ManagedSupported:     true,
				ManagedPayloadLayout: "iso-store",
				BestInstallerTitle:   "Install",
			},
		}},
	})
	if err == nil || !strings.Contains(err.Error(), "prepared hd-media source directory") {
		t.Fatalf("expected bare netinst ISO rejection, got %v", err)
	}
}

func TestBuildMultiOSPlanAllowsSingleNetbootSource(t *testing.T) {
	cfg, err := loadRuntimeConfig(repoConfigPath())
	if err != nil {
		t.Fatalf("load runtime config: %v", err)
	}
	netbootDir := writePreparedInstallerSource(t, multiOSSourceRoleNetboot, "")
	request := MultiOSRequest{
		UseCustomGrubMenu: true,
		Items: []MultiOSRequestItem{{
			Profile:    profileDebian,
			SourceRole: multiOSSourceRoleNetboot,
			ISOPath:    netbootDir,
			Inspection: ISOInspection{
				MediaClass:           "installer",
				Firmware:             []string{"uefi"},
				ManagedSupported:     true,
				ManagedPayloadLayout: "iso-store",
				BestInstallerTitle:   "Install",
			},
		}},
	}
	plan, err := buildMultiOSPlan(cfg, request)
	if err != nil {
		t.Fatalf("build Multi-OS plan: %v", err)
	}
	if len(plan.Items) != 1 || plan.Items[0].SourceRole != multiOSSourceRoleNetboot {
		t.Fatalf("expected one Debian netboot item, got %#v", plan.Items)
	}
}

func TestBuildMultiOSPlanRejectsPrimaryInstallerForDebian(t *testing.T) {
	cfg, err := loadRuntimeConfig(repoConfigPath())
	if err != nil {
		t.Fatalf("load runtime config: %v", err)
	}
	request := MultiOSRequest{
		UseCustomGrubMenu: true,
		Items: []MultiOSRequestItem{{
			Profile:    profileDebian,
			SourceRole: multiOSSourceRolePrimary,
			ISOPath:    writeTestISO(t, "debian-installer.iso"),
			Inspection: ISOInspection{
				MediaClass:           "installer",
				Firmware:             []string{"uefi"},
				ManagedSupported:     true,
				ManagedPayloadLayout: "iso-store",
				BestInstallerTitle:   "Install",
			},
		}},
	}
	_, err = buildMultiOSPlan(cfg, request)
	if err == nil || !strings.Contains(err.Error(), "primary source must be live or hybrid") {
		t.Fatalf("expected primary-source validation error, got %v", err)
	}
}

func TestBuildMultiOSPlanRejectsDuplicateProfiles(t *testing.T) {
	cfg, err := loadRuntimeConfig(repoConfigPath())
	if err != nil {
		t.Fatalf("load runtime config: %v", err)
	}
	isoPath := writeTestISO(t, "debian.iso")
	request := MultiOSRequest{UseCustomGrubMenu: true, Items: []MultiOSRequestItem{
		{
			Profile:    profileDebian,
			SourceRole: multiOSSourceRolePrimary,
			ISOPath:    isoPath,
			Inspection: ISOInspection{
				MediaClass:           "hybrid",
				Firmware:             []string{"uefi"},
				ManagedSupported:     true,
				ManagedPayloadLayout: "iso-store",
			},
		},
		{
			Profile:    profileDebian,
			SourceRole: multiOSSourceRolePrimary,
			ISOPath:    isoPath,
			Inspection: ISOInspection{
				MediaClass:           "hybrid",
				Firmware:             []string{"uefi"},
				ManagedSupported:     true,
				ManagedPayloadLayout: "iso-store",
			},
		},
	}}
	_, err = buildMultiOSPlan(cfg, request)
	if err == nil || !strings.Contains(err.Error(), "profile/source role selected more than once") {
		t.Fatalf("expected duplicate profile error, got %v", err)
	}
}

func TestBuildMultiOSPlanCreatesIndependentPersistenceLabels(t *testing.T) {
	cfg, err := loadRuntimeConfig(repoConfigPath())
	if err != nil {
		t.Fatalf("load runtime config: %v", err)
	}
	request := MultiOSRequest{UseCustomGrubMenu: true, Items: []MultiOSRequestItem{
		{
			Profile:            profileDebian,
			SourceRole:         multiOSSourceRolePrimary,
			ISOPath:            writeTestISO(t, "debian.iso"),
			Persistence:        true,
			PersistenceMode:    persistenceModePlain,
			PersistenceSizeGiB: 4,
			KernelArgs:         "boot=live persistence persistence-label=persistence persistence-media=removable-usb",
			Inspection: ISOInspection{
				MediaClass:           "hybrid",
				Firmware:             []string{"uefi"},
				ManagedSupported:     true,
				ManagedPayloadLayout: "iso-store",
				SupportsPersistence:  true,
				TopLevelEntries:      []string{"Live"},
			},
		},
		{
			Profile:            profileKaliLinux,
			SourceRole:         multiOSSourceRolePrimary,
			ISOPath:            writeTestISO(t, "kali.iso"),
			Persistence:        true,
			PersistenceMode:    persistenceModeEncrypted,
			PersistenceSizeGiB: 8,
			Inspection: ISOInspection{
				MediaClass:                   "hybrid",
				Firmware:                     []string{"uefi"},
				ManagedSupported:             true,
				ManagedPayloadLayout:         "iso-store",
				SupportsPersistence:          true,
				SupportsEncryptedPersistence: true,
				TopLevelEntries:              []string{"Live"},
			},
		},
	}}
	plan, err := buildMultiOSPlan(cfg, request)
	if err != nil {
		t.Fatalf("build Multi-OS plan: %v", err)
	}
	if plan.WriteMode != writeModeMultiOS {
		t.Fatalf("expected write mode %q, got %q", writeModeMultiOS, plan.WriteMode)
	}
	if got := plan.Items[0].PersistenceFSLabel; got != "DEBIAN-PERSIST" {
		t.Fatalf("expected Debian persistence label, got %q", got)
	}
	if got := plan.Items[1].PersistenceFSLabel; got != "KALI-PERSIST" {
		t.Fatalf("expected Kali persistence label, got %q", got)
	}
	if plan.DataFSLabel == "" {
		t.Fatalf("expected shared data label to be populated: %#v", plan)
	}
	if plan.Items[0].PayloadFSLabel != "" || plan.Items[1].PayloadFSLabel != "" {
		t.Fatalf("expected per-item payload labels to be empty when using the shared data layout, got %#v", plan.Items)
	}
}

func TestBuildMultiOSPlanUsesConfiguredPartitionLabels(t *testing.T) {
	cfg, err := loadRuntimeConfig(repoConfigPath())
	if err != nil {
		t.Fatalf("load runtime config: %v", err)
	}
	cfg.DefaultPartitionLabels[configLabelESP] = "EFITEST"
	cfg.DefaultPartitionLabels[configLabelDebianLive] = "DEB-LIVE"
	cfg.DefaultPartitionLabels[configLabelDebianNetinst] = "DEB-NET"
	cfg.DefaultPartitionLabels[configLabelDebianPersist] = "DEB-PERSIST"
	request := MultiOSRequest{UseCustomGrubMenu: true, Items: []MultiOSRequestItem{
		{
			Profile:            profileDebian,
			SourceRole:         multiOSSourceRolePrimary,
			ISOPath:            writeTestISO(t, "debian-live.iso"),
			Persistence:        true,
			PersistenceMode:    persistenceModePlain,
			PersistenceSizeGiB: 4,
			KernelArgs:         "boot=live persistence persistence-label=persistence persistence-media=removable-usb",
			Inspection: ISOInspection{
				MediaClass:           "hybrid",
				Firmware:             []string{"uefi"},
				ManagedSupported:     true,
				ManagedPayloadLayout: "iso-store",
				SupportsPersistence:  true,
				BestInstallerTitle:   "Install",
			},
		},
		{
			Profile:    profileDebian,
			SourceRole: multiOSSourceRoleNetinst,
			ISOPath:    writePreparedInstallerSource(t, multiOSSourceRoleNetinst, "debian-netinst.iso"),
			Inspection: ISOInspection{
				MediaClass:           "installer",
				Firmware:             []string{"uefi"},
				ManagedSupported:     true,
				ManagedPayloadLayout: "iso-store",
				BestInstallerTitle:   "Install",
			},
		},
	}}
	plan, err := buildMultiOSPlan(cfg, request)
	if err != nil {
		t.Fatalf("build Multi-OS plan: %v", err)
	}
	if plan.ESPLabel != "EFITEST" {
		t.Fatalf("expected configured ESP label, got %q", plan.ESPLabel)
	}
	if got := plan.DataFSLabel; got != "MULTIBOOT" {
		t.Fatalf("expected configured shared data label, got %q", got)
	}
	if got := plan.Items[0].PersistenceFSLabel; got != "DEB-PERSIST" {
		t.Fatalf("expected configured persistence label, got %q", got)
	}
	if got := plan.Items[0].KernelArgs; !strings.Contains(got, "persistence-label=DEB-PERSIST") {
		t.Fatalf("expected configured persistence label in kernel args, got %q", got)
	}
}

func TestBuildMultiOSPlanNormalizesOverrideBasisToPerOSPersistenceLabel(t *testing.T) {
	cfg, err := loadRuntimeConfig(repoConfigPath())
	if err != nil {
		t.Fatalf("load runtime config: %v", err)
	}
	request := MultiOSRequest{Items: []MultiOSRequestItem{
		{
			Profile:            profileDebian,
			SourceRole:         multiOSSourceRolePrimary,
			ISOPath:            writeTestISO(t, "debian.iso"),
			Persistence:        true,
			PersistenceMode:    persistenceModePlain,
			PersistenceSizeGiB: 4,
			KernelArgs:         "boot=live quiet splash persistence persistence-label=persistence persistence-media=removable-usb",
			Inspection: ISOInspection{
				MediaClass:           "hybrid",
				Firmware:             []string{"uefi"},
				ManagedSupported:     true,
				ManagedPayloadLayout: "iso-store",
				SupportsPersistence:  true,
			},
		},
		{
			Profile:            profileKaliLinux,
			SourceRole:         multiOSSourceRolePrimary,
			ISOPath:            writeTestISO(t, "kali.iso"),
			Persistence:        true,
			PersistenceMode:    persistenceModeEncrypted,
			PersistenceSizeGiB: 4,
			KernelArgs:         "boot=live quiet splash persistence persistence-label=persistence",
			Inspection: ISOInspection{
				MediaClass:                   "hybrid",
				Firmware:                     []string{"uefi"},
				ManagedSupported:             true,
				ManagedPayloadLayout:         "iso-store",
				SupportsPersistence:          true,
				SupportsEncryptedPersistence: true,
			},
		},
	}}
	plan, err := buildMultiOSPlan(cfg, request)
	if err != nil {
		t.Fatalf("build Multi-OS plan: %v", err)
	}
	if got := plan.Items[0].KernelArgs; !strings.Contains(got, "persistence-label=DEBIAN-PERSIST") {
		t.Fatalf("expected Debian override basis to use Multi-OS label, got %q", got)
	}
	if got := plan.Items[1].KernelArgs; !strings.Contains(got, "persistence-label=KALI-PERSIST") {
		t.Fatalf("expected Kali override basis to use Multi-OS label, got %q", got)
	}
}

func TestBuildMultiOSPlanAllowsNetinstItemForSelectedProfile(t *testing.T) {
	cfg, err := loadRuntimeConfig(repoConfigPath())
	if err != nil {
		t.Fatalf("load runtime config: %v", err)
	}
	offlinePreseedDir := filepath.Join(t.TempDir(), "preseed")
	if err := os.MkdirAll(offlinePreseedDir, 0755); err != nil {
		t.Fatalf("mkdir offline preseed dir: %v", err)
	}
	request := MultiOSRequest{
		UseCustomGrubMenu: true,
		ProfileOfflinePreseedDirs: map[string]string{
			profileDebian: offlinePreseedDir,
		},
		Items: []MultiOSRequestItem{
			{
				Profile:    profileDebian,
				SourceRole: multiOSSourceRolePrimary,
				ISOPath:    writeTestISO(t, "debian-live.iso"),
				Inspection: ISOInspection{
					MediaClass:           "hybrid",
					Firmware:             []string{"uefi"},
					ManagedSupported:     true,
					ManagedPayloadLayout: "iso-store",
					SupportsPersistence:  true,
					BestInstallerTitle:   "Install",
				},
			},
			{
				Profile:    profileDebian,
				SourceRole: multiOSSourceRoleNetinst,
				ISOPath:    writePreparedInstallerSource(t, multiOSSourceRoleNetinst, "debian-netinst.iso"),
				Preseed:    true,
				Inspection: ISOInspection{
					MediaClass:           "installer",
					Firmware:             []string{"uefi"},
					ManagedSupported:     true,
					ManagedPayloadLayout: "iso-store",
					BestInstallerTitle:   "Install",
				},
			},
			{
				Profile:    profileKaliLinux,
				SourceRole: multiOSSourceRolePrimary,
				ISOPath:    writeTestISO(t, "kali.iso"),
				Inspection: ISOInspection{
					MediaClass:           "hybrid",
					Firmware:             []string{"uefi"},
					ManagedSupported:     true,
					ManagedPayloadLayout: "iso-store",
					SupportsPersistence:  true,
					BestInstallerTitle:   "Install",
				},
			},
		}}
	plan, err := buildMultiOSPlan(cfg, request)
	if err != nil {
		t.Fatalf("build Multi-OS plan: %v", err)
	}
	if len(plan.Items) != 3 {
		t.Fatalf("expected three plan items, got %#v", plan.Items)
	}
	netinst := plan.Items[1]
	if netinst.SourceRole != multiOSSourceRoleNetinst {
		t.Fatalf("expected netinst source role, got %q", netinst.SourceRole)
	}
	if netinst.Title != "Debian Netinst" {
		t.Fatalf("expected netinst title, got %q", netinst.Title)
	}
	if netinst.Persistence || netinst.LiveToram {
		t.Fatalf("netinst item should not carry live-only options: %#v", netinst)
	}
	if !netinst.Preseed {
		t.Fatalf("expected netinst preseed to be enabled: %#v", netinst)
	}
	if netinst.ManagedPayloadLayout != "shared-data" {
		t.Fatalf("expected netinst payload to use the shared data partition layout, got %q", netinst.ManagedPayloadLayout)
	}
	if plan.Items[0].ManagedPayloadLayout != "shared-data" || plan.Items[2].ManagedPayloadLayout != "shared-data" {
		t.Fatalf("expected every Multi-OS payload to use the shared data partition, got %#v", plan.Items)
	}
	if netinst.OfflinePreseedSourceDir != offlinePreseedDir {
		t.Fatalf("expected offline preseed dir %q, got %q", offlinePreseedDir, netinst.OfflinePreseedSourceDir)
	}
	if netinst.MenuLabel != "" || netinst.KernelArgs != "" || netinst.KernelPath != "" || netinst.InitrdPath != "" {
		t.Fatalf("netinst item should not carry live override fields: %#v", netinst)
	}
	if plan.Items[0].PayloadFSLabel != "" || netinst.PayloadFSLabel != "" || plan.Items[2].PayloadFSLabel != "" {
		t.Fatalf("expected per-item payload labels to stay empty for the shared data layout, got %#v", plan.Items)
	}
	if !containsNote(plan.Notes, "Debian/Kali HTTP and USB preseed GRUB entries will be generated for: Debian Netinst.") {
		t.Fatalf("expected fixed-profile preseed note, got %#v", plan.Notes)
	}
}

func TestBuildMultiOSPlanUsesSharedISOStoreForEverySource(t *testing.T) {
	cfg, err := loadRuntimeConfig(repoConfigPath())
	if err != nil {
		t.Fatalf("load runtime config: %v", err)
	}
	request := MultiOSRequest{
		UseCustomGrubMenu:           true,
		PreserveUpstreamGrubEntries: true,
		Items: []MultiOSRequestItem{
			{
				Profile:    profileDebian,
				SourceRole: multiOSSourceRolePrimary,
				ISOPath:    writeTestISO(t, "debian-live.iso"),
				Inspection: ISOInspection{
					MediaClass:           "hybrid",
					Firmware:             []string{"uefi"},
					ManagedSupported:     true,
					ManagedPayloadLayout: "iso-store",
					SupportsPersistence:  true,
				},
			},
			{
				Profile:    profileKaliLinux,
				SourceRole: multiOSSourceRolePrimary,
				ISOPath:    writeTestISO(t, "kali-live.iso"),
				Inspection: ISOInspection{
					MediaClass:           "hybrid",
					Firmware:             []string{"uefi"},
					ManagedSupported:     true,
					ManagedPayloadLayout: "iso-store",
					SupportsPersistence:  true,
				},
			},
		},
	}

	plan, err := buildMultiOSPlan(cfg, request)
	if err != nil {
		t.Fatalf("build Multi-OS plan: %v", err)
	}
	for _, item := range plan.Items {
		if item.ManagedPayloadLayout != "shared-data" {
			t.Fatalf("expected shared ISO-store layout for every Multi-OS item, got %#v", plan.Items)
		}
	}
	if !plan.PreserveUpstreamGrubEntries {
		t.Fatalf("expected Multi-OS plan to preserve upstream GRUB entries when requested")
	}
	if !containsNote(plan.Notes, "Partition 1 is the ESP, partition 2 is the ISO store, and each persistence-enabled Live source receives its own partition starting at partition 3.") {
		t.Fatalf("expected shared ISO-store layout note, got %#v", plan.Notes)
	}
	if !containsNote(plan.Notes, "Live sources keep their original ISO intact on partition 2 and boot only that exact ISO through findiso; extracted rootfs and package-list payloads are not merged into the ISO store.") {
		t.Fatalf("expected isolated Live ISO staging note, got %#v", plan.Notes)
	}
	if !containsNote(plan.Notes, "preserved upstream-managed entry trees alongside the curated family menus") {
		t.Fatalf("expected combined custom/preserved note, got %#v", plan.Notes)
	}
}

func TestBuildMultiOSPlanRejectsNetinstWithoutInstallerEntries(t *testing.T) {
	cfg, err := loadRuntimeConfig(repoConfigPath())
	if err != nil {
		t.Fatalf("load runtime config: %v", err)
	}
	request := MultiOSRequest{Items: []MultiOSRequestItem{
		{
			Profile:    profileDebian,
			SourceRole: multiOSSourceRoleNetinst,
			ISOPath:    writePreparedInstallerSource(t, multiOSSourceRoleNetinst, "debian-netinst.iso"),
			Inspection: ISOInspection{
				MediaClass:           "live",
				Firmware:             []string{"uefi"},
				ManagedSupported:     true,
				ManagedPayloadLayout: "iso-store",
			},
		},
	}}
	_, err = buildMultiOSPlan(cfg, request)
	if err == nil || !strings.Contains(err.Error(), "netinst source must expose installer boot entries") {
		t.Fatalf("expected netinst installer validation error, got %v", err)
	}
}

func TestBuildMultiOSPlanRejectsPlainTailsPersistence(t *testing.T) {
	cfg, err := loadRuntimeConfig(repoConfigPath())
	if err != nil {
		t.Fatalf("load runtime config: %v", err)
	}
	request := MultiOSRequest{Items: []MultiOSRequestItem{
		{
			Profile:            profileTails,
			SourceRole:         multiOSSourceRolePrimary,
			ISOPath:            writeTestISO(t, "tails.iso"),
			Persistence:        true,
			PersistenceMode:    persistenceModePlain,
			PersistenceSizeGiB: 4,
			Inspection: ISOInspection{
				MediaClass:                   "hybrid",
				Firmware:                     []string{"uefi"},
				ManagedSupported:             true,
				ManagedPayloadLayout:         "iso-store",
				SupportsPersistence:          true,
				SupportsEncryptedPersistence: true,
			},
		},
	}}
	_, err = buildMultiOSPlan(cfg, request)
	if err == nil || !strings.Contains(err.Error(), "Tails persistence must be encrypted") {
		t.Fatalf("expected Tails encrypted persistence validation error, got %v", err)
	}
}

func TestBuildMultiOSPlanRejectsPreseedOnFixedPrimaryLiveSource(t *testing.T) {
	cfg, err := loadRuntimeConfig(repoConfigPath())
	if err != nil {
		t.Fatalf("load runtime config: %v", err)
	}
	request := MultiOSRequest{UseCustomGrubMenu: true, Items: []MultiOSRequestItem{
		{
			Profile:    profileDebian,
			SourceRole: multiOSSourceRolePrimary,
			ISOPath:    writeTestISO(t, "debian-live.iso"),
			Preseed:    true,
			Inspection: ISOInspection{
				MediaClass:           "hybrid",
				Firmware:             []string{"uefi"},
				ManagedSupported:     true,
				ManagedPayloadLayout: "iso-store",
			},
		},
		{
			Profile:    profileKaliLinux,
			SourceRole: multiOSSourceRolePrimary,
			ISOPath:    writeTestISO(t, "kali-live.iso"),
			Inspection: ISOInspection{
				MediaClass:           "hybrid",
				Firmware:             []string{"uefi"},
				ManagedSupported:     true,
				ManagedPayloadLayout: "iso-store",
			},
		},
	}}
	plan, err := buildMultiOSPlan(cfg, request)
	if err == nil || !strings.Contains(err.Error(), "installer-capable managed source role") {
		t.Fatalf("expected fixed-profile primary preseed rejection, got plan=%#v err=%v", plan, err)
	}
}

func writeTestISO(t *testing.T, name string) string {
	t.Helper()
	path := filepath.Join(t.TempDir(), name)
	if err := os.WriteFile(path, []byte("iso"), 0644); err != nil {
		t.Fatalf("write test iso: %v", err)
	}
	return path
}

func writePreparedInstallerSource(t *testing.T, sourceRole, isoName string) string {
	t.Helper()
	root := filepath.Join(t.TempDir(), sourceRole+"-source")
	assetRoot := "netboot"
	if sourceRole == multiOSSourceRoleNetinst {
		assetRoot = "hd-media"
	}
	if err := os.MkdirAll(filepath.Join(root, assetRoot), 0755); err != nil {
		t.Fatalf("mkdir %s source assets: %v", sourceRole, err)
	}
	for _, name := range []string{"vmlinuz", "initrd.gz"} {
		if err := os.WriteFile(filepath.Join(root, assetRoot, name), []byte(sourceRole+"-"+name), 0644); err != nil {
			t.Fatalf("write %s/%s: %v", assetRoot, name, err)
		}
	}
	if sourceRole == multiOSSourceRoleNetinst {
		if err := os.MkdirAll(filepath.Join(root, "payload"), 0755); err != nil {
			t.Fatalf("mkdir netinst payload: %v", err)
		}
		if err := os.WriteFile(filepath.Join(root, "payload", isoName), []byte("netinst-iso"), 0644); err != nil {
			t.Fatalf("write netinst payload ISO: %v", err)
		}
	}
	return root
}
