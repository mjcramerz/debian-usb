package app

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestBuildCreatePlanIncludesConfiguredManagedKernelDefaults(t *testing.T) {
	cfg, err := loadRuntimeConfig(repoConfigPath())
	if err != nil {
		t.Fatalf("load runtime config: %v", err)
	}
	cfg.DefaultLiveToram = true
	cfg.DefaultLiveMemGiB = 4
	cfg.DefaultLiveKernelExtras = "nvme_core.default_ps_max_latency_us=3200"
	cfg.ProfileLiveKernelExtras[profileDebian] = "components"
	cfg.DefaultForensicsKernelExtras = "rd.shell=0"

	isoPath := filepath.Join(t.TempDir(), "debian.iso")
	if err := os.WriteFile(isoPath, []byte("iso"), 0644); err != nil {
		t.Fatalf("write iso: %v", err)
	}

	plan, err := buildCreatePlan(cfg, CreateRequest{
		Profile:   profileDebian,
		WriteMode: writeModeManaged,
		ISOPath:   isoPath,
		LiveToram: true,
		Inspection: ISOInspection{
			MediaClass:                   "hybrid",
			Firmware:                     []string{"uefi"},
			ManagedSupported:             true,
			ManagedPayloadLayout:         "iso-store",
			SupportsPersistence:          true,
			SupportsEncryptedPersistence: true,
			TopLevelEntries:              []string{"Live system (amd64)", "Graphical installer"},
		},
	})
	if err != nil {
		t.Fatalf("build create plan: %v", err)
	}
	if plan.Strategy != "managed-live" {
		t.Fatalf("expected managed-live strategy, got %q", plan.Strategy)
	}
	if !containsNote(plan.Notes, "Configured live kernel defaults will also be applied: balanced toram=filesystem.squashfs mem=4G nvme_core.default_ps_max_latency_us=3200") {
		t.Fatalf("expected configured live defaults note, got %#v", plan.Notes)
	}
	if !containsNote(plan.Notes, "Debian (Live / Netinst / Netboot) profile live extras will also be applied: components") {
		t.Fatalf("expected profile live extras note, got %#v", plan.Notes)
	}
	if !containsNote(plan.Notes, "The Live source role keeps only Live entries from hybrid media; embedded installer entries are ignored.") {
		t.Fatalf("expected explicit Live/installer isolation note, got %#v", plan.Notes)
	}
	if !containsNote(plan.Notes, "Preserved forensic live entries will also receive configured forensic extras: rd.shell=0") {
		t.Fatalf("expected forensics extras note, got %#v", plan.Notes)
	}
	if !containsNote(plan.Notes, "Managed payload layout: ISO-store filesystem") {
		t.Fatalf("expected managed payload layout note, got %#v", plan.Notes)
	}
}

func TestBuildCreatePlanKeepsUbuntuLiveRoleIsolatedFromInstallerSettings(t *testing.T) {
	cfg, err := loadRuntimeConfig(writeBackendLiveHookTestConfig(t))
	if err != nil {
		t.Fatalf("load synthetic runtime config: %v", err)
	}
	cfg.ProfilePreseedURLs[profileUbuntuDesktop] = "https://example.test/autoinstall.yaml"
	cfg.DefaultInstallerKernelExtras = "ipv6.disable=1"
	cfg.ProfileInstallerKernelExtras[profileUbuntuDesktop] = "autoinstall"

	isoPath := filepath.Join(t.TempDir(), "ubuntu.iso")
	if err := os.WriteFile(isoPath, []byte("iso"), 0644); err != nil {
		t.Fatalf("write iso: %v", err)
	}

	plan, err := buildCreatePlan(cfg, CreateRequest{
		Profile:   profileUbuntuDesktop,
		WriteMode: writeModeManaged,
		ISOPath:   isoPath,
		Inspection: ISOInspection{
			MediaClass:           "hybrid",
			Firmware:             []string{"uefi"},
			ManagedSupported:     true,
			ManagedPayloadLayout: "extracted",
			TopLevelEntries:      []string{"Try or Install Ubuntu"},
		},
	})
	if err != nil {
		t.Fatalf("build create plan: %v", err)
	}
	if plan.PreseedURL != "https://example.test/autoinstall.yaml" {
		t.Fatalf("expected installer URL to flow into plan, got %q", plan.PreseedURL)
	}
	if containsNote(plan.Notes, "configured per-profile installer URL handling through those entries") {
		t.Fatalf("did not expect installer URL handling in a primary Live role, got %#v", plan.Notes)
	}
	if containsNote(plan.Notes, "Ubuntu Desktop installer extras will also be applied: autoinstall") {
		t.Fatalf("did not expect installer extras in a primary Live role, got %#v", plan.Notes)
	}
	if !containsNote(plan.Notes, "extract the ISO contents into a managed payload filesystem") {
		t.Fatalf("expected extracted payload note, got %#v", plan.Notes)
	}
}

func TestBuildCreatePlanUsesPerRunToramInsteadOfConfigDefault(t *testing.T) {
	cfg, err := loadRuntimeConfig(repoConfigPath())
	if err != nil {
		t.Fatalf("load runtime config: %v", err)
	}
	cfg.DefaultLiveToram = false

	isoPath := filepath.Join(t.TempDir(), "debian.iso")
	if err := os.WriteFile(isoPath, []byte("iso"), 0644); err != nil {
		t.Fatalf("write iso: %v", err)
	}

	plan, err := buildCreatePlan(cfg, CreateRequest{
		Profile:   profileDebian,
		WriteMode: writeModeManaged,
		ISOPath:   isoPath,
		LiveToram: true,
		Inspection: ISOInspection{
			MediaClass:       "live",
			Firmware:         []string{"uefi"},
			ManagedSupported: true,
		},
	})
	if err != nil {
		t.Fatalf("build create plan: %v", err)
	}
	if !plan.LiveToram {
		t.Fatalf("expected plan live toram to be true")
	}
	if !containsNote(plan.Notes, "balanced toram") {
		t.Fatalf("expected plan notes to include toram, got %#v", plan.Notes)
	}
}

func TestBuildCreatePlanCarriesOfflinePreseedSourceDirForManagedNetinstMedia(t *testing.T) {
	cfg, err := loadRuntimeConfig(repoConfigPath())
	if err != nil {
		t.Fatalf("load runtime config: %v", err)
	}

	tempDir := t.TempDir()
	isoPath := filepath.Join(tempDir, "debian-netinst")
	if err := os.MkdirAll(isoPath, 0755); err != nil {
		t.Fatalf("mkdir prepared source: %v", err)
	}
	for _, name := range []string{"hd-media/vmlinuz", "hd-media/initrd.gz", "payload/netinst.iso"} {
		target := filepath.Join(isoPath, name)
		if err := os.MkdirAll(filepath.Dir(target), 0755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(target, []byte("fixture"), 0644); err != nil {
			t.Fatal(err)
		}
	}
	offlinePreseedDir := filepath.Join(tempDir, "preseed")
	if err := os.MkdirAll(offlinePreseedDir, 0755); err != nil {
		t.Fatalf("mkdir offline preseed dir: %v", err)
	}

	plan, err := buildCreatePlan(cfg, CreateRequest{
		Profile:                 profileDebian,
		SourceRole:              multiOSSourceRoleNetinst,
		WriteMode:               writeModeManaged,
		ISOPath:                 isoPath,
		UseCustomGrubMenu:       true,
		Preseed:                 true,
		OfflinePreseedSourceDir: offlinePreseedDir,
		Inspection: ISOInspection{
			MediaClass:           "installer",
			Firmware:             []string{"uefi"},
			ManagedSupported:     true,
			ManagedPayloadLayout: "iso-store",
			BestInstallerTitle:   "Install",
		},
	})
	if err != nil {
		t.Fatalf("build create plan: %v", err)
	}
	if plan.OfflinePreseedSourceDir != offlinePreseedDir {
		t.Fatalf("expected offline preseed source dir %q, got %q", offlinePreseedDir, plan.OfflinePreseedSourceDir)
	}
	if plan.MenuLabel != "" {
		t.Fatalf("expected netinst managed plan to avoid a live menu label, got %q", plan.MenuLabel)
	}
	if !containsNote(plan.Notes, "Offline preseed content from "+offlinePreseedDir+" will be staged at the directory implied by the configured PRESEED_USB_*_FILE path for this managed payload.") {
		t.Fatalf("expected offline preseed note, got %#v", plan.Notes)
	}
}

func TestBuildCreatePlanRejectsPersistenceForDirectWrite(t *testing.T) {
	cfg, err := loadRuntimeConfig(repoConfigPath())
	if err != nil {
		t.Fatalf("load runtime config: %v", err)
	}

	isoPath := filepath.Join(t.TempDir(), "direct.iso")
	if err := os.WriteFile(isoPath, []byte("iso"), 0644); err != nil {
		t.Fatalf("write iso: %v", err)
	}

	_, err = buildCreatePlan(cfg, CreateRequest{
		Profile:     profileDebian,
		WriteMode:   writeModeDirect,
		ISOPath:     isoPath,
		Persistence: true,
		Inspection: ISOInspection{
			MediaClass:           "hybrid",
			Firmware:             []string{"uefi"},
			ManagedSupported:     true,
			ManagedPayloadLayout: "iso-store",
		},
	})
	if err == nil || !strings.Contains(err.Error(), "persistence is only supported with the managed USB workflow") {
		t.Fatalf("expected direct write persistence validation error, got %v", err)
	}
}

func TestBuildCreatePlanAcceptsQuotedISOPath(t *testing.T) {
	cfg, err := loadRuntimeConfig(repoConfigPath())
	if err != nil {
		t.Fatalf("load runtime config: %v", err)
	}

	isoPath := filepath.Join(t.TempDir(), "quoted.iso")
	if err := os.WriteFile(isoPath, []byte("iso"), 0644); err != nil {
		t.Fatalf("write iso: %v", err)
	}

	plan, err := buildCreatePlan(cfg, CreateRequest{
		Profile:   profileDebian,
		WriteMode: writeModeManaged,
		ISOPath:   "'" + isoPath + "'",
		Inspection: ISOInspection{
			MediaClass:       "hybrid",
			Firmware:         []string{"uefi"},
			ManagedSupported: true,
		},
	})
	if err != nil {
		t.Fatalf("build create plan: %v", err)
	}
	if plan.ISOPath != isoPath {
		t.Fatalf("expected normalized iso path %q, got %q", isoPath, plan.ISOPath)
	}
}

func TestBuildMultiOSPlanPersistsPreparedSourcePayloadISOName(t *testing.T) {
	cfg, err := loadRuntimeConfig(repoConfigPath())
	if err != nil {
		t.Fatalf("load runtime config: %v", err)
	}
	root := t.TempDir()
	preparedRoot := filepath.Join(root, "debian-netinst-root")
	if err := os.MkdirAll(filepath.Join(preparedRoot, "payload"), 0755); err != nil {
		t.Fatalf("mkdir payload: %v", err)
	}
	if err := os.WriteFile(filepath.Join(preparedRoot, "payload", "debian-testing-amd64-netinst.iso"), []byte("iso"), 0644); err != nil {
		t.Fatalf("write payload iso: %v", err)
	}
	if err := os.MkdirAll(filepath.Join(preparedRoot, "EFI", "boot"), 0755); err != nil {
		t.Fatalf("mkdir efi: %v", err)
	}
	if err := os.WriteFile(filepath.Join(preparedRoot, "EFI", "boot", "bootx64.efi"), []byte(""), 0644); err != nil {
		t.Fatalf("write efi marker: %v", err)
	}
	if err := os.MkdirAll(filepath.Join(preparedRoot, "hd-media"), 0755); err != nil {
		t.Fatalf("mkdir hd-media: %v", err)
	}
	for _, name := range []string{"vmlinuz", "initrd.gz"} {
		if err := os.WriteFile(filepath.Join(preparedRoot, "hd-media", name), []byte(name), 0644); err != nil {
			t.Fatalf("write %s: %v", name, err)
		}
	}

	plan, err := buildMultiOSPlan(cfg, MultiOSRequest{
		UseCustomGrubMenu: true,
		Items: []MultiOSRequestItem{
			{
				Profile:    profileDebian,
				SourceRole: multiOSSourceRoleNetinst,
				ISOPath:    preparedRoot,
				Inspection: ISOInspection{
					MediaClass:           "installer",
					Firmware:             []string{"uefi"},
					ManagedSupported:     true,
					ManagedPayloadLayout: "iso-store",
					BestInstallerTitle:   "Install",
				},
			},
		},
	})
	if err != nil {
		t.Fatalf("build Multi-OS plan: %v", err)
	}
	if len(plan.Items) != 1 {
		t.Fatalf("expected one item, got %#v", plan.Items)
	}
	if got := plan.Items[0].PayloadISOName; got != "debian-testing-amd64-netinst.iso" {
		t.Fatalf("expected payload ISO name to persist, got %q", got)
	}
}

func TestBuildCreatePlanKeepsManagedWriteForInstallerOnlyMedia(t *testing.T) {
	cfg, err := loadRuntimeConfig(repoConfigPath())
	if err != nil {
		t.Fatalf("load runtime config: %v", err)
	}

	isoPath := filepath.Join(t.TempDir(), "kali-purple.iso")
	if err := os.WriteFile(isoPath, []byte("iso"), 0644); err != nil {
		t.Fatalf("write iso: %v", err)
	}

	plan, err := buildCreatePlan(cfg, CreateRequest{
		Profile:   profileKaliPurple,
		WriteMode: writeModeManaged,
		ISOPath:   isoPath,
		Inspection: ISOInspection{
			MediaClass:           "installer",
			Firmware:             []string{"uefi"},
			ManagedSupported:     true,
			ManagedPayloadLayout: "iso-store",
			TopLevelEntries:      []string{"Graphical install"},
		},
	})
	if err != nil {
		t.Fatalf("build create plan: %v", err)
	}
	if plan.WriteMode != writeModeManaged {
		t.Fatalf("expected managed write mode, got %q", plan.WriteMode)
	}
	if plan.Strategy != "managed-installer" {
		t.Fatalf("expected managed-installer strategy, got %q", plan.Strategy)
	}
	if plan.Profile != profileKaliPurple {
		t.Fatalf("expected kali-purple profile, got %q", plan.Profile)
	}
	if plan.Preseed {
		t.Fatalf("did not expect managed preseed entries for kali-purple installer media")
	}
}

func TestBuildCreatePlanAcceptsPreparedNetbootSourceDirectory(t *testing.T) {
	cfg, err := loadRuntimeConfig(repoConfigPath())
	if err != nil {
		t.Fatalf("load runtime config: %v", err)
	}

	sourceDir := writePreparedInstallerSource(t, multiOSSourceRoleNetboot, "")

	plan, err := buildCreatePlan(cfg, CreateRequest{
		Profile:    profileDebian,
		SourceRole: multiOSSourceRoleNetboot,
		WriteMode:  writeModeManaged,
		ISOPath:    sourceDir,
		Inspection: ISOInspection{
			MediaClass:           "installer",
			Firmware:             []string{"uefi"},
			ManagedSupported:     true,
			ManagedPayloadLayout: "raw-iso",
			BestInstallerTitle:   "Install",
		},
	})
	if err != nil {
		t.Fatalf("build create plan: %v", err)
	}
	if plan.SourceRole != multiOSSourceRoleNetboot {
		t.Fatalf("expected source role %q, got %q", multiOSSourceRoleNetboot, plan.SourceRole)
	}
	if plan.ManagedPayloadLayout != "iso-store" {
		t.Fatalf("expected netboot prepared source to force iso-store layout, got %q", plan.ManagedPayloadLayout)
	}
	if plan.PayloadFSLabel != "DEBIAN-NETBOOT" {
		t.Fatalf("expected netboot payload label, got %q", plan.PayloadFSLabel)
	}
}

func TestBuildCreatePlanRejectsPlainTailsPersistence(t *testing.T) {
	cfg, err := loadRuntimeConfig(repoConfigPath())
	if err != nil {
		t.Fatalf("load runtime config: %v", err)
	}

	isoPath := filepath.Join(t.TempDir(), "tails.iso")
	if err := os.WriteFile(isoPath, []byte("iso"), 0644); err != nil {
		t.Fatalf("write iso: %v", err)
	}

	_, err = buildCreatePlan(cfg, CreateRequest{
		Profile:         profileTails,
		WriteMode:       writeModeManaged,
		ISOPath:         isoPath,
		Persistence:     true,
		PersistenceMode: persistenceModePlain,
		Inspection: ISOInspection{
			MediaClass:                   "hybrid",
			Firmware:                     []string{"uefi"},
			ManagedSupported:             true,
			ManagedPayloadLayout:         "iso-store",
			SupportsPersistence:          true,
			SupportsEncryptedPersistence: true,
		},
	})
	if err == nil || !strings.Contains(err.Error(), "Tails persistence must be encrypted") {
		t.Fatalf("expected Tails encrypted persistence validation error, got %v", err)
	}
}

func TestBuildCreatePlanTracksCustomGrubMenuChoice(t *testing.T) {
	cfg, err := loadRuntimeConfig(repoConfigPath())
	if err != nil {
		t.Fatalf("load runtime config: %v", err)
	}

	isoPath := filepath.Join(t.TempDir(), "debian-custom.iso")
	if err := os.WriteFile(isoPath, []byte("iso"), 0644); err != nil {
		t.Fatalf("write iso: %v", err)
	}

	plan, err := buildCreatePlan(cfg, CreateRequest{
		Profile:                     profileDebian,
		WriteMode:                   writeModeManaged,
		ISOPath:                     isoPath,
		UseCustomGrubMenu:           true,
		PreserveUpstreamGrubEntries: true,
		Inspection: ISOInspection{
			MediaClass:           "hybrid",
			Firmware:             []string{"uefi"},
			ManagedSupported:     true,
			ManagedPayloadLayout: "iso-store",
		},
	})
	if err != nil {
		t.Fatalf("build create plan: %v", err)
	}
	if !plan.UseCustomGrubMenu {
		t.Fatalf("expected plan to keep custom GRUB menu enabled")
	}
	if !plan.PreserveUpstreamGrubEntries {
		t.Fatalf("expected plan to keep preserved upstream GRUB entries enabled")
	}
	if plan.ManagedPayloadLayout != "iso-store" {
		t.Fatalf("expected custom GRUB plan to use ISO-store payload layout, got %q", plan.ManagedPayloadLayout)
	}
	if !containsNote(plan.Notes, "Custom GRUB mode will group boot entries into Debian, Kali, Tails, and Ubuntu family menus") {
		t.Fatalf("expected custom GRUB note, got %#v", plan.Notes)
	}
	if !containsNote(plan.Notes, "stores the opaque Live ISO under /boot/iso/... and loads its kernel/initrd through loopback without copying Live boot assets") {
		t.Fatalf("expected ISO-store Live isolation note, got %#v", plan.Notes)
	}
	if !containsNote(plan.Notes, "preserved upstream-managed entry trees alongside the curated family menus") {
		t.Fatalf("expected combined custom/preserved note, got %#v", plan.Notes)
	}
}

func TestSinglePartitionSummaryUsesWholeDiskForDirectHybridWrite(t *testing.T) {
	isoPath := filepath.Join(t.TempDir(), "debian.iso")
	if err := os.WriteFile(isoPath, []byte("iso"), 0644); err != nil {
		t.Fatalf("write iso: %v", err)
	}
	rows := singlePartitionSpecificationRows(CreatePlan{
		WriteMode: writeModeDirect,
		ISOPath:   isoPath,
	}, Device{Path: "/dev/sdz", SizeBytes: 16 * 1024 * 1024 * 1024})
	if len(rows) != 1 {
		t.Fatalf("expected one direct disk-image row, got %#v", rows)
	}
	if !strings.Contains(rows[0].Value, "target=/dev/sdz | writer=dd") {
		t.Fatalf("expected whole-disk dd target in summary, got %#v", rows[0])
	}
	if strings.Contains(rows[0].Value, "/dev/sdz1") {
		t.Fatalf("direct hybrid write summary must not target a partition: %#v", rows[0])
	}
}

func TestSharedISOStorePartitionSummaryMatchesManagedWriterLayout(t *testing.T) {
	rows := singlePartitionSpecificationRows(CreatePlan{
		WriteMode:            writeModeManaged,
		ManagedPayloadLayout: "iso-store",
		PayloadFSLabel:       "DEBIAN-LIVE",
	}, Device{Path: "/dev/sdz", SizeBytes: 16 * 1024 * 1024 * 1024})
	combined := rowsToString(rows)
	for _, fragment := range []string{"p1 ESP", "p2 ISO STORE", "label=DEBIAN-LIVE", "role=ISO store"} {
		if !strings.Contains(combined, fragment) {
			t.Fatalf("expected %q in partition summary:\n%s", fragment, combined)
		}
	}
}

func TestMultiOSPartitionSummaryMatchesIsolatedRawISOWriterLayout(t *testing.T) {
	debianISO := filepath.Join(t.TempDir(), "debian.iso")
	if err := os.WriteFile(debianISO, []byte("iso"), 0644); err != nil {
		t.Fatalf("write iso: %v", err)
	}
	rows := multiOSPartitionSpecificationRows(MultiOSPlan{
		Items: []MultiOSPlanItem{
			{ID: "os1", Title: "Debian", ISOPath: debianISO, PayloadPartLabel: "DEBIAN-LIVE"},
		},
	}, Device{Path: "/dev/sdz", SizeBytes: 32 * 1024 * 1024 * 1024})
	combined := rowsToString(rows)
	for _, fragment := range []string{"p1 os1", "fs=raw-iso9660", "label=DEBIAN-LIVE", "p2 ESP", "role=uefi-removable + /preseed/<os>"} {
		if !strings.Contains(combined, fragment) {
			t.Fatalf("expected %q in Multi-OS partition summary:\n%s", fragment, combined)
		}
	}
}

func TestMultiOSPartitionSummaryIncludesPersistencePartitionsWhenEnabled(t *testing.T) {
	rows := multiOSPartitionSpecificationRows(
		MultiOSPlan{
			ESPLabel:    "ESPBOOT",
			DataFSLabel: "MULTIBOOT",
			Items: []MultiOSPlanItem{
				{
					ID:                   "os1",
					Title:                "Debian",
					ManagedPayloadLayout: "shared-data",
					Persistence:          true,
					PersistenceMode:      persistenceModeEncrypted,
					PersistenceSizeGiB:   8,
					PersistenceFSLabel:   "DEBIAN-PERSIST",
				},
				{
					ID:                   "os2",
					Title:                "Kali Live",
					ManagedPayloadLayout: "shared-data",
					Persistence:          true,
					PersistenceMode:      persistenceModePlain,
					PersistenceSizeGiB:   4,
					PersistenceFSLabel:   "KALI-PERSIST",
				},
				{
					ID:                   "os3",
					Title:                "Debian Netinst",
					ManagedPayloadLayout: "shared-data",
				},
			},
		},
		Device{Path: "/dev/sdz", SizeBytes: 64 * 1024 * 1024 * 1024},
	)
	combined := rowsToString(rows)
	for _, fragment := range []string{
		"p1 ESP",
		"p2 ISO STORE",
		"role=ISO store",
		"p3 os1 persistence",
		"label=DEBIAN-PERSIST",
		"fs=luks2/ext4",
		"p4 os2 persistence",
		"label=KALI-PERSIST",
	} {
		if !strings.Contains(combined, fragment) {
			t.Fatalf("expected %q in Multi-OS persistence partition summary:\n%s", fragment, combined)
		}
	}
}

func TestSingleTechnicalSpecRowsStayCompact(t *testing.T) {
	isoPath := filepath.Join(t.TempDir(), "debian.iso")
	if err := os.WriteFile(isoPath, []byte("iso"), 0644); err != nil {
		t.Fatalf("write iso: %v", err)
	}
	rows := singleTechnicalSpecRows(
		CreatePlan{
			Profile:                     profileDebian,
			WriteMode:                   writeModeManaged,
			Strategy:                    "managed-live",
			ISOPath:                     isoPath,
			MediaClass:                  "hybrid",
			Firmware:                    []string{"uefi"},
			ManagedSupported:            true,
			ManagedPayloadLayout:        "iso-store",
			UseCustomGrubMenu:           true,
			PreserveUpstreamGrubEntries: true,
			ESPLabel:                    "ESPBOOT",
			PayloadFSLabel:              "DEBIAN-LIVE",
			PayloadPartLabel:            "DEBIAN-LIVE",
			BootPolicy:                  "balanced",
			InstallerPolicy:             "installer-preseed",
			TopLevelEntries:             []string{"Live system (amd64)", "Graphical installer"},
		},
		Device{Path: "/dev/sdz", Name: "sdz", Model: "USB Disk", Transport: "usb", SizeBytes: 16 * 1024 * 1024 * 1024},
		profileSpecs[profileDebian],
		"run-1",
		"new saved plan created at review",
	)
	if len(rows) != 9 {
		t.Fatalf("expected 9 compact technical spec rows, got %#v", rows)
	}
	combined := rowsToString(rows)
	for _, fragment := range []string{
		"Write=single-os",
		"Target USB=node=/dev/sdz",
		"USB Layout=GPT: BIOSBOOT 1-3MiB | ESP FAT32 ESPBOOT 3-515MiB | DEBIAN-LIVE ext4 515MiB-end",
		"Boot/GRUB=BIOS i386-pc + UEFI removable x86_64-efi",
		"Plan/Confirm=saved=run-1 | new saved plan created at review | destructive=yes | confirm target=/dev/sdz",
	} {
		if !strings.Contains(combined, fragment) {
			t.Fatalf("expected %q in compact technical spec summary:\n%s", fragment, combined)
		}
	}
	for _, forbidden := range []string{"p1 BIOSBOOT=", "p2 ESP=", "p3 MULTIBOOT=", "Persistence label=", "Offline preseed source="} {
		if strings.Contains(combined, forbidden) {
			t.Fatalf("did not expect per-partition or per-field sprawl %q in:\n%s", forbidden, combined)
		}
	}
}

func TestMultiOSTechnicalSpecRowsStayCompact(t *testing.T) {
	debianISO := filepath.Join(t.TempDir(), "debian.iso")
	if err := os.WriteFile(debianISO, []byte("iso"), 0644); err != nil {
		t.Fatalf("write debian iso: %v", err)
	}
	kaliISO := filepath.Join(t.TempDir(), "kali.iso")
	if err := os.WriteFile(kaliISO, []byte("iso"), 0644); err != nil {
		t.Fatalf("write kali iso: %v", err)
	}
	rows := multiOSTechnicalSpecRows(
		MultiOSPlan{
			WriteMode:                   writeModeMultiOS,
			ESPLabel:                    "ESPBOOT",
			UseCustomGrubMenu:           true,
			PreserveUpstreamGrubEntries: true,
			Items: []MultiOSPlanItem{
				{ID: "os1", Profile: profileDebian, Title: "Debian", ISOPath: debianISO, MediaClass: "hybrid", ManagedPayloadLayout: "iso-store", SourceRole: multiOSSourceRolePrimary},
				{ID: "os2", Profile: profileKaliLinux, Title: "Kali Linux Netinst", ISOPath: kaliISO, MediaClass: "installer", ManagedPayloadLayout: "iso-store", SourceRole: multiOSSourceRoleNetinst, Preseed: true},
			},
		},
		Device{Path: "/dev/sdz", Name: "sdz", Model: "USB Disk", Transport: "usb", SizeBytes: 32 * 1024 * 1024 * 1024},
		"run-2",
		"matching saved plan reused at review",
	)
	if len(rows) != 8 {
		t.Fatalf("expected 8 compact Multi-OS technical spec rows, got %#v", rows)
	}
	combined := rowsToString(rows)
	for _, fragment := range []string{
		"Write=multi-os | sources=2 | mode=multi-os | overwrite=yes",
		"USB Layout=GPT: raw ISO payload partitions with netinst/installer sources first, then live sources | ESP FAT32 ESPBOOT after payloads for GRUB + /preseed",
		"OS Entries=Debian; Kali Linux Netinst",
		"Automation=preseed=Kali Linux Netinst | persistence=off | secureboot=mok | default_preseed_sources=PRESEED_HOST_{DEBIAN,KALI,PURPLE}_PATH | preseed_stage=dirname(PRESEED_USB_*_FILE) | payloads=raw-iso partitions per source",
	} {
		if !strings.Contains(combined, fragment) {
			t.Fatalf("expected %q in compact Multi-OS technical spec summary:\n%s", fragment, combined)
		}
	}
	for _, forbidden := range []string{"Debian=", "Kali Linux Netinst=", "Source role=", "Payload label="} {
		if strings.Contains(combined, forbidden) {
			t.Fatalf("did not expect per-item sprawl %q in:\n%s", forbidden, combined)
		}
	}
}

func TestMultiOSTechnicalSpecRowsReflectSharedPersistenceLayout(t *testing.T) {
	debianISO := filepath.Join(t.TempDir(), "debian.iso")
	if err := os.WriteFile(debianISO, []byte("iso"), 0644); err != nil {
		t.Fatalf("write iso: %v", err)
	}
	netinstISO := filepath.Join(t.TempDir(), "debian-netinst.iso")
	if err := os.WriteFile(netinstISO, []byte("iso"), 0644); err != nil {
		t.Fatalf("write netinst iso: %v", err)
	}
	rows := multiOSTechnicalSpecRows(
		MultiOSPlan{
			WriteMode:                   writeModeMultiOS,
			ESPLabel:                    "ESPBOOT",
			DataFSLabel:                 "MULTIBOOT",
			UseCustomGrubMenu:           true,
			PreserveUpstreamGrubEntries: true,
			Items: []MultiOSPlanItem{
				{
					ID:                   "os1",
					Profile:              profileDebian,
					Title:                "Debian",
					ISOPath:              debianISO,
					ManagedPayloadLayout: "shared-data",
					MediaClass:           "hybrid",
					Persistence:          true,
					PersistenceMode:      persistenceModeEncrypted,
					PersistenceSizeGiB:   8,
				},
				{
					ID:                   "os2",
					Profile:              profileDebian,
					Title:                "Debian Netinst",
					ISOPath:              netinstISO,
					ManagedPayloadLayout: "shared-data",
					MediaClass:           "installer",
					Preseed:              true,
				},
			},
		},
		Device{Path: "/dev/sdz", Name: "sdz", Model: "USB Disk", Transport: "usb", SizeBytes: 64 * 1024 * 1024 * 1024},
		"run-3",
		"matching saved plan reused at review",
	)
	combined := rowsToString(rows)
	for _, fragment := range []string{
		"USB Layout=GPT: p1 FAT32 ESPBOOT as the removable ESP | p2 ext4 MULTIBOOT as the ISO store | p3+ one persistence partition per enabled Live source",
		"Boot/GRUB=UEFI removable x86_64-efi | grub.cfg=ESP:/boot/grub/grub.cfg | exact ISO selection from p2",
		"Automation=preseed=Debian Netinst | persistence=Debian:encrypted 8GiB | secureboot=mok | default_preseed_sources=PRESEED_HOST_{DEBIAN,KALI,PURPLE}_PATH | preseed_stage=dirname(PRESEED_USB_*_FILE) | payloads=ISO store on p2",
	} {
		if !strings.Contains(combined, fragment) {
			t.Fatalf("expected %q in Multi-OS technical spec summary:\n%s", fragment, combined)
		}
	}
}

func TestPlannedExecutionTechnicalSpecRowsReuseCompactSummary(t *testing.T) {
	isoPath := filepath.Join(t.TempDir(), "debian.iso")
	if err := os.WriteFile(isoPath, []byte("iso"), 0644); err != nil {
		t.Fatalf("write iso: %v", err)
	}
	rows := plannedExecutionTechnicalSpecRows(
		PlannedExecution{
			RunID: "planned-1",
			Kind:  plannedExecutionKindSingle,
			SinglePlan: &CreatePlan{
				Profile:              profileDebian,
				WriteMode:            writeModeDirect,
				Strategy:             "hybrid-dd",
				ISOPath:              isoPath,
				MediaClass:           "hybrid",
				Firmware:             []string{"uefi"},
				ManagedPayloadLayout: "iso-store",
			},
		},
		Device{Path: "/dev/sdz"},
	)
	combined := rowsToString(rows)
	if !strings.Contains(combined, "Plan/Confirm=saved=planned-1 | saved planned execution | destructive=yes | confirm target=/dev/sdz") {
		t.Fatalf("expected planned execution technical spec summary, got:\n%s", combined)
	}
	if strings.Contains(combined, "Summary=") {
		t.Fatalf("planned execution review should now use technical specs instead of the old summary row:\n%s", combined)
	}
}

func rowsToString(rows []infoRow) string {
	var builder strings.Builder
	for _, row := range rows {
		builder.WriteString(row.Label)
		builder.WriteString("=")
		builder.WriteString(row.Value)
		builder.WriteString("\n")
	}
	return builder.String()
}

func containsNote(notes []string, fragment string) bool {
	for _, note := range notes {
		if strings.Contains(note, fragment) {
			return true
		}
	}
	return false
}
