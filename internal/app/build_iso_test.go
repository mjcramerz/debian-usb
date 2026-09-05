package app

import (
	"bufio"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestParseDelimitedValuesDeduplicatesAndPreservesOrder(t *testing.T) {
	got := parseDelimitedValues(" live-build, erofs-utils  live-build xorriso ")
	want := []string{"live-build", "erofs-utils", "xorriso"}
	if len(got) != len(want) {
		t.Fatalf("expected %d values, got %#v", len(want), got)
	}
	for index, value := range want {
		if got[index] != value {
			t.Fatalf("expected %q at index %d, got %#v", value, index, got)
		}
	}
}

func TestParseCommaDelimitedValuesDeduplicatesAndPreservesAssignments(t *testing.T) {
	got := parseCommaDelimitedValues(" CONFIG_EROFS_FS=y, CONFIG_XXHASH=m, CONFIG_EROFS_FS=y ")
	want := []string{"CONFIG_EROFS_FS=y", "CONFIG_XXHASH=m"}
	if len(got) != len(want) {
		t.Fatalf("expected %d values, got %#v", len(want), got)
	}
	for index, value := range want {
		if got[index] != value {
			t.Fatalf("expected %q at index %d, got %#v", value, index, got)
		}
	}
}

func TestResolveOptionalExistingDirRejectsFiles(t *testing.T) {
	tempDir := t.TempDir()
	filePath := filepath.Join(tempDir, "not-a-dir")
	if err := os.WriteFile(filePath, []byte("x"), 0644); err != nil {
		t.Fatalf("write file: %v", err)
	}
	_, err := resolveOptionalExistingDir(filePath)
	if err == nil {
		t.Fatalf("expected resolveOptionalExistingDir to reject a file path")
	}
}

func TestCollectDebianBuildISOPlanCapturesAdvancedSelections(t *testing.T) {
	tempDir := t.TempDir()
	localDebDir := filepath.Join(tempDir, "debs")
	localUdebDir := filepath.Join(tempDir, "udebs")
	installerOverlayDir := filepath.Join(tempDir, "installer-overlay")
	liveOverlayDir := filepath.Join(tempDir, "live-overlay")
	binaryOverlayDir := filepath.Join(tempDir, "binary-overlay")
	bootloaderOverrideDir := filepath.Join(tempDir, "bootloaders")
	preseedPath := filepath.Join(tempDir, "preseed.cfg")
	for _, path := range []string{localDebDir, localUdebDir, installerOverlayDir, liveOverlayDir, binaryOverlayDir, bootloaderOverrideDir} {
		if err := os.MkdirAll(path, 0755); err != nil {
			t.Fatalf("mkdir %s: %v", path, err)
		}
	}
	if err := os.WriteFile(preseedPath, []byte("d-i debian-installer/locale string en_US\n"), 0644); err != nil {
		t.Fatalf("write preseed: %v", err)
	}

	input := strings.Join([]string{
		filepath.Join(tempDir, "output"),
		"custom.iso",
		"trixie",
		"amd64",
		"main contrib non-free non-free-firmware",
		"3",
		"n",
		"y",
		"y",
		"a",
		"vim, htop",
		"firmware-iwlwifi",
		localDebDir,
		localUdebDir,
		"",
		"",
		"",
		"n",
		preseedPath,
		installerOverlayDir,
		liveOverlayDir,
		binaryOverlayDir,
		bootloaderOverrideDir,
		"trixie",
		"auto=true priority=critical",
		"4",
		"linux-image-xanmod",
		"amd64",
		"deb https://example.test/xanmod stable main",
		"deb https://example.test/xanmod stable main",
		"",
		"",
		"nvme-cli,sedutil-cli",
		"2",
		"zstd",
		"-C 65536",
		"erofs xxhash xxhash_generic",
		"erofs xxhash xxhash_generic",
		"2",
		"kernel-wedge iso-scan partconf os-prober rescue",
		"-",
		"",
		"-",
		"n",
		"n",
		"filesystem.squashfs filesystem.module",
		"n",
		"y",
	}, "\n") + "\n"

	application := App{
		reader: bufio.NewReader(strings.NewReader(input)),
	}

	plan, action, err := application.collectDebianBuildISOPlan()
	if err != nil {
		t.Fatalf("collectDebianBuildISOPlan: %v", err)
	}
	if action != menuStay {
		t.Fatalf("expected menuStay, got %v", action)
	}
	if plan.OutputDir != filepath.Join(tempDir, "output") {
		t.Fatalf("unexpected output dir: %q", plan.OutputDir)
	}
	if plan.ImageName != "custom.iso" {
		t.Fatalf("unexpected image name: %q", plan.ImageName)
	}
	if plan.InstallerMode != buildISOInstallerModeLive {
		t.Fatalf("unexpected installer mode: %q", plan.InstallerMode)
	}
	if !plan.IncludeInstallerLauncher {
		t.Fatalf("expected installer launcher to be enabled")
	}
	if plan.KernelMode != buildISOKernelModeCustomRepo {
		t.Fatalf("unexpected kernel mode: %q", plan.KernelMode)
	}
	if plan.KernelPackageStub != "linux-image-xanmod" {
		t.Fatalf("unexpected kernel package stub: %q", plan.KernelPackageStub)
	}
	if plan.RootFSFormat != buildISORootFSFormatEROFS {
		t.Fatalf("unexpected rootfs format: %q", plan.RootFSFormat)
	}
	if plan.CleanupMode != buildISOCleanupModeKeepWorkspace {
		t.Fatalf("expected keep-workspace cleanup mode, got %q", plan.CleanupMode)
	}
	if plan.PreseedPath != preseedPath {
		t.Fatalf("unexpected preseed path: %q", plan.PreseedPath)
	}
	if plan.UDEBRebuildSpecPath != "" {
		t.Fatalf("expected empty UDEB rebuild spec path, got %q", plan.UDEBRebuildSpecPath)
	}
	if plan.DirectDIBuildEnabled {
		t.Fatalf("expected direct d-i build to be disabled")
	}
	if plan.LiveIncludeDir != liveOverlayDir {
		t.Fatalf("unexpected live include dir: %q", plan.LiveIncludeDir)
	}
	if plan.BinaryIncludeDir != binaryOverlayDir {
		t.Fatalf("unexpected binary include dir: %q", plan.BinaryIncludeDir)
	}
	if plan.BootloaderOverrideDir != bootloaderOverrideDir {
		t.Fatalf("unexpected bootloader override dir: %q", plan.BootloaderOverrideDir)
	}
	if plan.InstallerDistribution != "trixie" {
		t.Fatalf("unexpected installer distribution: %q", plan.InstallerDistribution)
	}
	if plan.InstallerBootAppend != "auto=true priority=critical" {
		t.Fatalf("unexpected installer bootappend: %q", plan.InstallerBootAppend)
	}
	if plan.EROFSInstallerPolicy != buildISOEROFSInstallerPolicyRequire {
		t.Fatalf("unexpected EROFS installer policy: %q", plan.EROFSInstallerPolicy)
	}
	if len(plan.EROFSInstallerComponents) != 5 {
		t.Fatalf("unexpected EROFS installer components: %#v", plan.EROFSInstallerComponents)
	}
	if len(plan.InitramfsModules) != 3 {
		t.Fatalf("unexpected initramfs modules: %#v", plan.InitramfsModules)
	}
	if len(plan.FilesystemModuleEntries) != 2 {
		t.Fatalf("unexpected filesystem.module entries: %#v", plan.FilesystemModuleEntries)
	}
}

func TestCollectDebianBuildISOPlanKeepsNetinstFreeOfLiveInputs(t *testing.T) {
	tempDir := t.TempDir()
	input := strings.Join([]string{
		filepath.Join(tempDir, "output"),
		"debian-netinst.iso",
		"trixie",
		"amd64",
		"main contrib non-free-firmware",
		"2",
		"n",
		"y",
		"",
		"n",
		"",
		"",
		"n",
		"",
		"",
		"",
		"",
		"",
		"",
		"y",
		"y",
	}, "\n") + "\n"

	application := App{reader: bufio.NewReader(strings.NewReader(input))}
	plan, action, err := application.collectDebianBuildISOPlan()
	if err != nil {
		t.Fatalf("collect netinst Build ISO plan: %v", err)
	}
	if action != menuStay {
		t.Fatalf("expected menuStay, got %v", action)
	}
	if plan.InstallerMode != buildISOInstallerModeNetinst {
		t.Fatalf("expected netinst installer mode, got %q", plan.InstallerMode)
	}
	if plan.RootFSFormat != buildISORootFSFormatNone {
		t.Fatalf("expected no live rootfs, got %q", plan.RootFSFormat)
	}
	if len(plan.BasePackages) != 0 || len(plan.ExtraChrootPackages) != 0 || len(plan.StorageToolPackages) != 0 {
		t.Fatalf("netinst plan retained live packages: %#v", plan)
	}
	if plan.LiveModuleSpecPath != "" || plan.LiveDebSpecPath != "" || plan.LiveUdebSpecPath != "" || plan.LiveIncludeDir != "" {
		t.Fatalf("netinst plan retained live specs or overlays: %#v", plan)
	}
	if plan.IncludeInstallerLauncher || plan.KernelMode != buildISOKernelModeStockDebian {
		t.Fatalf("netinst plan retained live launcher or kernel settings: %#v", plan)
	}
}
