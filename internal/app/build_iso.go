package app

import (
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"
)

const (
	defaultBuildISOOutputDir = "/data/downloads/debian-usb/iso/debian-live"
	defaultBuildISOImageName = "debian-live-custom.iso"
	defaultBuildISOSuite     = "trixie"
	defaultBuildISOArch      = "amd64"
)

var (
	defaultBuildISOArchiveAreas = []string{"main", "contrib", "non-free", "non-free-firmware"}
	defaultBuildISOBasePackages = []string{
		"live-boot", "live-boot-initramfs-tools", "live-config", "live-config-systemd", "live-tools",
		"live-task-base", "live-task-standard", "live-task-recommended", "ca-certificates", "debian-archive-keyring",
		"locales", "initramfs-tools", "kmod", "xxhash", "lz4", "zstd", "usbutils", "pciutils", "flashrom",
		"i2c-tools", "iproute2", "iw", "wpasupplicant", "dhcpcd-base", "rfkill", "wireless-regdb",
		"bluez-firmware", "firmware-linux", "firmware-iwlwifi", "firmware-ipw2x00", "firmware-intel-graphics",
		"firmware-intel-misc", "firmware-intel-sound", "firmware-sof-signed", "intel-microcode", "firmware-atheros",
		"firmware-realtek", "firmware-brcm80211", "firmware-mediatek", "firmware-libertas",
	}
	defaultBuildISOInitramfsModules = []string{
		"erofs", "xxhash", "xxhash_generic", "lz4", "lz4_compress", "lz4_decompress", "loop", "squashfs",
		"overlay", "ext4", "dm_mod", "dm_crypt", "usb_storage", "uas", "nvme", "usbserial", "ch341",
	}
	defaultBuildISOKernelConfigSymbols = []string{
		"CONFIG_EROFS_FS", "CONFIG_EROFS_FS_XATTR", "CONFIG_EROFS_FS_ZIP", "CONFIG_XXHASH", "CONFIG_CRYPTO_XXHASH",
		"CONFIG_CRYPTO_LZ4", "CONFIG_LZ4_COMPRESS", "CONFIG_LZ4_DECOMPRESS", "CONFIG_BLK_DEV_LOOP", "CONFIG_SQUASHFS",
		"CONFIG_OVERLAY_FS", "CONFIG_EXT4_FS", "CONFIG_BLK_DEV_DM", "CONFIG_DM_CRYPT", "CONFIG_USB_STORAGE",
		"CONFIG_USB_UAS", "CONFIG_BLK_DEV_NVME", "CONFIG_USB_SERIAL", "CONFIG_USB_SERIAL_CH341",
	}
	defaultBuildISOModuleAliases = []string{
		"xxhash64", "xxhash64-generic", "xxhash64_generic", "xxhash_generic", "crypto-xxhash64",
		"crypto-xxhash64-generic", "crypto_xxhash64", "crypto_xxhash64_generic",
	}
	defaultBuildISOEROFSInstallerComponents = []string{"build-config", "kernel-wedge", "iso-scan", "partman-auto", "partconf", "os-prober", "rescue"}
)

const defaultInstalledSpecDir = "/usr/lib/debian-usb/spec"

var (
	defaultBuildISODebianLiveModuleSpecRelPath = filepath.Join("live", "debian", "modules", "erofs-xxhash-generic.json")
	defaultBuildISODebianLiveDebSpecRelPath    = filepath.Join("live", "debian", "deb", "base.json")
	defaultBuildISODebianLiveUdebSpecRelPath   = filepath.Join("live", "debian", "udeb", "base.json")
	defaultBuildISODebianDIModuleSpecRelPath   = filepath.Join("d-i", "debian", "modules", "erofs-xxhash-generic.json")
	defaultBuildISODebianDIDebSpecRelPath      = filepath.Join("d-i", "debian", "deb", "base.json")
	defaultBuildISODebianDIUdebSpecRelPath     = filepath.Join("d-i", "debian", "udeb", "base.json")
	defaultBuildISODebianOverlayRelPath        = filepath.Join("d-i", "debian", "modules", "overlays", "erofs-xxhash-generic")
)

func managedSpecFilePath(relativePath string) string {
	repoRelativePath := filepath.Join("configs", "spec", relativePath)
	if candidate, ok := repoAssetPath(repoRelativePath); ok {
		return candidate
	}
	candidate := filepath.Join(defaultInstalledSpecDir, relativePath)
	info, err := os.Stat(candidate)
	if err == nil && !info.IsDir() {
		return candidate
	}
	return ""
}

func managedSpecDirPath(relativePath string) string {
	repoRelativePath := filepath.Join("configs", "spec", relativePath)
	if candidate, ok := repoAssetDirPath(repoRelativePath); ok {
		return candidate
	}
	candidate := filepath.Join(defaultInstalledSpecDir, relativePath)
	info, err := os.Stat(candidate)
	if err == nil && info.IsDir() {
		return candidate
	}
	return ""
}

func detectPreferredBuildISOKernelVersion() string {
	if releaseBytes, err := os.ReadFile("/proc/sys/kernel/osrelease"); err == nil {
		release := strings.TrimSpace(string(releaseBytes))
		if release != "" {
			if _, statErr := os.Stat(filepath.Join("/lib/modules", release)); statErr == nil {
				return release
			}
		}
	}
	entries, err := os.ReadDir("/lib/modules")
	if err != nil {
		return ""
	}
	names := make([]string, 0, len(entries))
	for _, entry := range entries {
		if entry.IsDir() {
			names = append(names, entry.Name())
		}
	}
	if len(names) == 0 {
		return ""
	}
	sort.Strings(names)
	return names[len(names)-1]
}

func (a *App) buildISOMenu() (menuAction, error) {
	for {
		printHeader("Build Custom ISO")
		printSection(
			"Profiles",
			infoRow{Label: "Bundled", Value: "Debian Live, Debian Netinst, Kali Live, Kali Netinst"},
			infoRow{Label: "Advanced", Value: "The legacy Debian manual planner remains available for full custom plans"},
		)
		a.printMenu(
			menuEntry{Key: "1", Label: "Debian Live Profile", Detail: "bundled live-build.json + live-build.conf"},
			menuEntry{Key: "2", Label: "Debian Netinst Profile", Detail: "bundled netinst-build.json + netinst-build.conf"},
			menuEntry{Key: "3", Label: "Kali Live Profile", Detail: "bundled live-build.json + live-build.conf"},
			menuEntry{Key: "4", Label: "Kali Netinst Profile", Detail: "bundled netinst-build.json + netinst-build.conf"},
			menuEntry{Key: "8", Label: "Advanced Debian Plan", Detail: "interactive Debian-only live-build planner"},
			menuEntry{Key: "b", Label: "Go Back"},
			menuEntry{Key: "e", Label: "Exit"},
		)
		choice, err := a.promptChoice("Select a build target")
		if err != nil {
			return menuStay, err
		}
		switch choice {
		case "1":
			action, err := a.handleBundledBuildISOProfile("debian-live")
			if err != nil {
				return menuStay, err
			}
			if action == menuExit {
				return menuExit, nil
			}
		case "2":
			action, err := a.handleBundledBuildISOProfile("debian-netinst")
			if err != nil {
				return menuStay, err
			}
			if action == menuExit {
				return menuExit, nil
			}
		case "3":
			action, err := a.handleBundledBuildISOProfile("kali-live")
			if err != nil {
				return menuStay, err
			}
			if action == menuExit {
				return menuExit, nil
			}
		case "4":
			action, err := a.handleBundledBuildISOProfile("kali-netinst")
			if err != nil {
				return menuStay, err
			}
			if action == menuExit {
				return menuExit, nil
			}
		case "8":
			action, err := a.handleBuildISODebian()
			if err != nil {
				return menuStay, err
			}
			if action == menuExit {
				return menuExit, nil
			}
		case "b":
			return menuStay, nil
		case "e":
			return menuExit, nil
		default:
			fmt.Println("Invalid selection.")
		}
	}
}

func (a *App) handleBundledBuildISOProfile(key string) (menuAction, error) {
	profile, ok := bundledBuildISOProfileByKey(key)
	if !ok {
		return menuStay, fmt.Errorf("unsupported bundled build profile: %s", key)
	}
	plan, jsonPath, confPath, err := loadBundledBuildISOProfile(profile)
	if err != nil {
		return menuStay, err
	}

	printHeader("Build Custom ISO - " + profile.Title)
	printSection(
		"Profile",
		infoRow{Label: "JSON plan", Value: jsonPath},
		infoRow{Label: "Companion config", Value: confPath},
		infoRow{Label: "Distro", Value: plan.Distro},
		infoRow{Label: "Suite", Value: plan.Suite},
		infoRow{Label: "Installer mode", Value: plan.InstallerMode},
	)

	outputDir, err := a.promptRequiredString("Output directory for the finished ISO", plan.OutputDir)
	if err != nil {
		return menuStay, err
	}
	plan.OutputDir, err = resolveAbsolutePathInput(outputDir)
	if err != nil {
		return menuStay, err
	}

	imageName, err := a.promptRequiredString("ISO filename", plan.ImageName)
	if err != nil {
		return menuStay, err
	}
	plan.ImageName = strings.TrimSpace(imageName)
	if !strings.HasSuffix(strings.ToLower(plan.ImageName), ".iso") {
		return menuStay, fmt.Errorf("ISO filename must end with .iso: %s", plan.ImageName)
	}
	if plan.Distro == buildISODistroDebian {
		buildVariant := "Live"
		if plan.InstallerMode == buildISOInstallerModeNetinst {
			buildVariant = "Netinst"
		}
		suite, err := a.promptRequiredString("Debian suite for this "+buildVariant+" build", plan.Suite)
		if err != nil {
			return menuStay, err
		}
		plan.Suite = collapseWhitespace(suite)
		if plan.InstallerMode != buildISOInstallerModeNone {
			plan.InstallerDistribution = plan.Suite
		}
	}
	liveToolAction, err := a.promptBuildISOLiveToolGroups(&plan)
	if err != nil {
		return menuStay, err
	}
	if liveToolAction != menuStay {
		return liveToolAction, nil
	}
	applyLiveHookKernelArgsToBuildPlan(a.config, &plan)

	printHeader("Review Bundled Build Profile")
	printSection(
		"Profile",
		infoRow{Label: "Title", Value: profile.Title},
		infoRow{Label: "JSON plan", Value: jsonPath},
		infoRow{Label: "Companion config", Value: confPath},
	)
	a.printDebianBuildISOPlan(plan)
	confirmed, err := a.promptYesNo("Install required host dependencies and build this ISO", false)
	if err != nil {
		return menuStay, err
	}
	if !confirmed {
		fmt.Println("Bundled ISO build cancelled.")
		return menuStay, nil
	}
	if err := a.backend.EnsureDebianBuildDeps(); err != nil {
		return menuStay, err
	}
	result, err := a.backend.BuildDebianISO(plan)
	if err != nil {
		return menuStay, err
	}
	printHeader("ISO build complete")
	printSection(
		"Result",
		infoRow{Label: "ISO", Value: result.ISOPath},
		infoRow{Label: "Workspace", Value: result.WorkspaceDir},
		infoRow{Label: "Build log", Value: result.LogPath},
		infoRow{Label: "Manifest", Value: result.ManifestPath},
		infoRow{Label: "Installer audit", Value: result.InstallerAuditPath},
		infoRow{Label: "UDEB rebuild manifest", Value: result.UDEBRebuildManifestPath},
		infoRow{Label: "Installer localudeb repo", Value: result.InstallerLocalUdebRepoPath},
		infoRow{Label: "Direct d-i build manifest", Value: result.DirectDIBuildManifestPath},
	)
	printBulletList("Warnings", result.Warnings...)
	return menuStay, nil
}

func (a *App) handleBuildISODebian() (menuAction, error) {
	printHeader("Build Custom ISO - Debian")
	printSection(
		"Builder",
		infoRow{Label: "Input model", Value: "live-build plan"},
		infoRow{Label: "Kernel model", Value: "Debian packages or repo stubs"},
		infoRow{Label: "Installer", Value: "optional d-i + udeb integration"},
	)

	plan, action, err := a.collectDebianBuildISOPlan()
	if err != nil {
		return menuStay, err
	}
	if action != menuStay {
		return action, nil
	}
	if err := a.backend.EnsureDebianBuildDeps(); err != nil {
		return menuStay, err
	}
	result, err := a.backend.BuildDebianISO(plan)
	if err != nil {
		return menuStay, err
	}
	printHeader("ISO build complete")
	printSection(
		"Result",
		infoRow{Label: "ISO", Value: result.ISOPath},
		infoRow{Label: "Workspace", Value: result.WorkspaceDir},
		infoRow{Label: "Build log", Value: result.LogPath},
		infoRow{Label: "Manifest", Value: result.ManifestPath},
		infoRow{Label: "Installer audit", Value: result.InstallerAuditPath},
		infoRow{Label: "UDEB rebuild manifest", Value: result.UDEBRebuildManifestPath},
		infoRow{Label: "Installer localudeb repo", Value: result.InstallerLocalUdebRepoPath},
		infoRow{Label: "Direct d-i build manifest", Value: result.DirectDIBuildManifestPath},
	)
	printBulletList("Warnings", result.Warnings...)
	return menuStay, nil
}

func (a *App) collectDebianBuildISOPlan() (BuildISOPlan, menuAction, error) {
	plan := BuildISOPlan{
		SchemaVersion:            buildISOSchemaVersion,
		Distro:                   buildISODistroDebian,
		OutputDir:                defaultBuildISOOutputDir,
		ImageName:                defaultBuildISOImageName,
		Suite:                    defaultBuildISOSuite,
		Architecture:             defaultBuildISOArch,
		ArchiveAreas:             append([]string{}, defaultBuildISOArchiveAreas...),
		InstallerMode:            buildISOInstallerModeLive,
		IncludeNonFreeFirmware:   true,
		BasePackages:             append([]string{}, defaultBuildISOBasePackages...),
		RootFSFormat:             buildISORootFSFormatSquashFS,
		KernelMode:               buildISOKernelModeStockDebian,
		CleanupMode:              buildISOCleanupModePurgeWorkspace,
		EROFSCompressor:          "zstd",
		EROFSInstallerPolicy:     buildISOEROFSInstallerPolicyRequire,
		EROFSInstallerComponents: append([]string{}, defaultBuildISOEROFSInstallerComponents...),
		LiveBootAppend:           mandatoryDebianLiveHookKernelArgs,
		InitramfsModules:         append([]string{}, defaultBuildISOInitramfsModules...),
		KernelInspectionModules:  append([]string{}, defaultBuildISOInitramfsModules...),
		KernelConfigSymbols:      append([]string{}, defaultBuildISOKernelConfigSymbols...),
		ModuleAliasCandidates:    append([]string{}, defaultBuildISOModuleAliases...),
	}
	repoManagedSpecsEnabled := false

	printStepHeader(1, 4, "Debian ISO Base")
	printSection(
		"Defaults",
		infoRow{Label: "Output directory", Value: plan.OutputDir},
		infoRow{Label: "Suite", Value: plan.Suite},
		infoRow{Label: "Architecture", Value: plan.Architecture},
		infoRow{Label: "Installer mode", Value: plan.InstallerMode},
	)

	outputDir, err := a.promptRequiredString("Output directory for the finished ISO", plan.OutputDir)
	if err != nil {
		return plan, menuStay, err
	}
	outputDir, err = resolveAbsolutePathInput(outputDir)
	if err != nil {
		return plan, menuStay, err
	}
	plan.OutputDir = outputDir

	imageName, err := a.promptRequiredString("ISO filename", plan.ImageName)
	if err != nil {
		return plan, menuStay, err
	}
	imageName = strings.TrimSpace(imageName)
	if !strings.HasSuffix(strings.ToLower(imageName), ".iso") {
		return plan, menuStay, fmt.Errorf("ISO filename must end with .iso: %s", imageName)
	}
	plan.ImageName = imageName

	suite, err := a.promptRequiredString("Debian suite", plan.Suite)
	if err != nil {
		return plan, menuStay, err
	}
	plan.Suite = collapseWhitespace(suite)

	arch, err := a.promptRequiredString("Architecture", plan.Architecture)
	if err != nil {
		return plan, menuStay, err
	}
	plan.Architecture = collapseWhitespace(arch)

	archiveAreas, err := a.promptRequiredString("Archive areas", strings.Join(plan.ArchiveAreas, " "))
	if err != nil {
		return plan, menuStay, err
	}
	plan.ArchiveAreas = parseDelimitedValues(archiveAreas)
	if len(plan.ArchiveAreas) == 0 {
		return plan, menuStay, fmt.Errorf("at least one archive area is required")
	}

	installerMode, action, err := a.chooseBuildISOInstallerMode(plan.InstallerMode)
	if err != nil {
		return plan, menuStay, err
	}
	if action != menuStay {
		return plan, action, nil
	}
	plan.InstallerMode = installerMode
	netinstOnly := plan.InstallerMode == buildISOInstallerModeNetinst
	if netinstOnly {
		plan.RootFSFormat = buildISORootFSFormatNone
		plan.BasePackages = nil
	}

	specPrompt := "Apply repo-managed Debian live and d-i spec profiles when available"
	if netinstOnly {
		specPrompt = "Apply repo-managed Debian Installer spec profiles when available"
	}
	enableRepoManagedSpecs, err := a.promptYesNo(specPrompt, true)
	if err != nil {
		return plan, menuStay, err
	}
	if enableRepoManagedSpecs {
		repoManagedSpecsEnabled = true
		if !netinstOnly {
			plan.LiveDebSpecPath = managedSpecFilePath(defaultBuildISODebianLiveDebSpecRelPath)
			plan.LiveUdebSpecPath = managedSpecFilePath(defaultBuildISODebianLiveUdebSpecRelPath)
		}
		if plan.InstallerMode != buildISOInstallerModeNone {
			plan.DIDebSpecPath = managedSpecFilePath(defaultBuildISODebianDIDebSpecRelPath)
			plan.DIUdebSpecPath = managedSpecFilePath(defaultBuildISODebianDIUdebSpecRelPath)
		}
	}

	if !netinstOnly {
		launcher, err := a.promptYesNo("Include debian-installer-launcher in the live environment", false)
		if err != nil {
			return plan, menuStay, err
		}
		plan.IncludeInstallerLauncher = launcher
	}

	firmwarePrompt := "Include live-task-non-free-firmware-pc"
	if netinstOnly {
		firmwarePrompt = "Include the non-free-firmware archive area for Debian Installer"
	}
	includeFirmware, err := a.promptYesNo(firmwarePrompt, plan.IncludeNonFreeFirmware)
	if err != nil {
		return plan, menuStay, err
	}
	plan.IncludeNonFreeFirmware = includeFirmware
	if plan.IncludeNonFreeFirmware {
		plan.ArchiveAreas = appendUniqueValue(plan.ArchiveAreas, "non-free-firmware")
	}
	liveToolAction, err := a.promptBuildISOLiveToolGroups(&plan)
	if err != nil {
		return plan, menuStay, err
	}
	if liveToolAction != menuStay {
		return plan, liveToolAction, nil
	}

	printStepHeader(2, 4, "Packages and Installer Inputs")
	optionalInputNotes := []string{
		"Prebuilt .udeb files are copied into config/packages.binary/ for Debian Installer staging.",
		"Automatic installer-kernel rebuild can match the Debian installer kernel carried by a source ISO, add extra module names to the installer kernel-image lists, and force raw CONFIG entries into the rebuilt Debian kernel source before dpkg-buildpackage runs.",
		"Automatic source-package UDEB rebuild can resolve ordinary package names through APT metadata, run apt source, and rebuild them into installer-side udebs without hand-writing a rebuild spec.",
		"A UDEB rebuild specification can fetch Debian source packages, patch packaging metadata, build .udebs, and export a localudebs/pkg-list workspace for Debian Installer.",
	}
	if netinstOnly {
		optionalInputNotes = append([]string{
			"Netinst mode uses --debian-installer netinst with no --system option and rejects every Live rootfs input.",
			"Only Debian Installer specs, installer overlays, binary overlays, and installer-side package inputs are accepted.",
		}, optionalInputNotes...)
	} else {
		optionalInputNotes = append([]string{
			"Extra package names are installed into the live filesystem through config/package-lists/*.list.chroot.",
			"Local .deb files are renamed through dpkg-name and copied into config/packages.chroot/.",
		}, optionalInputNotes...)
		optionalInputNotes = append(optionalInputNotes, "Repo-managed Live and d-i spec profiles under configs/spec/ can preseed module, package, and rebuild defaults when enabled.")
	}
	printBulletList("Optional inputs", optionalInputNotes...)

	if !netinstOnly {
		extraPackages, err := a.promptOptionalString("Extra live package names (space or comma separated, optional)")
		if err != nil {
			return plan, menuStay, err
		}
		plan.ExtraChrootPackages = parseDelimitedValues(extraPackages)

		extraBinaryPackages, err := a.promptOptionalString("Extra medium-only package names (space or comma separated, optional)")
		if err != nil {
			return plan, menuStay, err
		}
		plan.ExtraBinaryPackages = parseDelimitedValues(extraBinaryPackages)

		localDebDir, err := a.promptOptionalString("Absolute path to a local .deb directory to preload into the live system (optional)")
		if err != nil {
			return plan, menuStay, err
		}
		plan.LocalDebDir, err = resolveOptionalExistingDir(localDebDir)
		if err != nil {
			return plan, menuStay, err
		}
	}

	localUdebDir, err := a.promptOptionalString("Absolute path to a local .udeb directory for Debian Installer staging (optional)")
	if err != nil {
		return plan, menuStay, err
	}
	plan.LocalUdebDir, err = resolveOptionalExistingDir(localUdebDir)
	if err != nil {
		return plan, menuStay, err
	}

	if plan.InstallerMode != buildISOInstallerModeNone {
		autoInstallerKernelRebuild, err := a.promptYesNo("Auto-rebuild the Debian Installer kernel from a source ISO with extra modules and CONFIG entries", false)
		if err != nil {
			return plan, menuStay, err
		}
		plan.InstallerKernelRebuildEnabled = autoInstallerKernelRebuild
		if plan.InstallerKernelRebuildEnabled {
			if a.backend != nil {
				a.printDetectedISOs()
			}
			sourceISOPath, err := a.promptRequiredString("Absolute path to the source ISO whose Debian Installer kernel should be matched", "")
			if err != nil {
				return plan, menuStay, err
			}
			plan.InstallerKernelSourceISOPath, err = resolveOptionalExistingFile(sourceISOPath)
			if err != nil {
				return plan, menuStay, err
			}
			if plan.InstallerKernelSourceISOPath == "" {
				return plan, menuStay, fmt.Errorf("a source ISO is required when automatic Debian Installer kernel rebuild is enabled")
			}
			installerKernelModules, err := a.promptOptionalString("Extra Debian Installer kernel modules to add to kernel-image lists (space or comma separated, optional)")
			if err != nil {
				return plan, menuStay, err
			}
			plan.InstallerKernelModules = parseDelimitedValues(installerKernelModules)
			installerKernelConfigs, err := a.promptOptionalString("Debian Installer kernel CONFIG entries to force (comma separated, e.g. CONFIG_EROFS_FS=y,CONFIG_XXHASH=m)")
			if err != nil {
				return plan, menuStay, err
			}
			plan.InstallerKernelConfigEntries = parseCommaDelimitedValues(installerKernelConfigs)
			if len(plan.InstallerKernelModules) == 0 && len(plan.InstallerKernelConfigEntries) == 0 {
				return plan, menuStay, fmt.Errorf("provide at least one installer kernel module or CONFIG entry when automatic Debian Installer kernel rebuild is enabled")
			}
		}

		autoUDEBPackages, err := a.promptOptionalString("Binary package names to auto-download with apt source and rebuild into udebs for Debian Installer (space or comma separated, optional)")
		if err != nil {
			return plan, menuStay, err
		}
		plan.InstallerAutoUDEBPackages = parseDelimitedValues(autoUDEBPackages)

		additionalUDEBRebuildSpecPath, err := a.promptOptionalString("Absolute path to an additional UDEB rebuild specification JSON file (optional)")
		if err != nil {
			return plan, menuStay, err
		}
		plan.UDEBRebuildSpecPath, err = resolveOptionalExistingFile(additionalUDEBRebuildSpecPath)
		if err != nil {
			return plan, menuStay, err
		}

		directDIBuildEnabled, err := a.promptYesNo("Run a standalone Debian Installer build from the exported localudeb workspace", false)
		if err != nil {
			return plan, menuStay, err
		}
		plan.DirectDIBuildEnabled = directDIBuildEnabled
		if plan.DirectDIBuildEnabled {
			sourceMode, action, err := a.chooseBuildISODirectDIBuildSourceMode(plan.DirectDIBuildSourceMode)
			if err != nil {
				return plan, menuStay, err
			}
			if action != menuStay {
				return plan, action, nil
			}
			plan.DirectDIBuildSourceMode = sourceMode
			if plan.DirectDIBuildSourceMode == buildISODirectDIBuildSourceModeLocalTree {
				sourceTree, err := a.promptRequiredString("Absolute path to the Debian Installer source tree", "")
				if err != nil {
					return plan, menuStay, err
				}
				plan.DirectDIBuildSourceTree, err = resolveOptionalExistingDir(sourceTree)
				if err != nil {
					return plan, menuStay, err
				}
				if plan.DirectDIBuildSourceTree == "" {
					return plan, menuStay, fmt.Errorf("a Debian Installer source tree is required")
				}
			} else {
				sourcePackage, err := a.promptRequiredString("APT source package name for Debian Installer", "debian-installer")
				if err != nil {
					return plan, menuStay, err
				}
				plan.DirectDIBuildSourcePackage = collapseWhitespace(sourcePackage)
			}
			targets, err := a.promptRequiredString("Direct d-i build make targets", "build_netboot")
			if err != nil {
				return plan, menuStay, err
			}
			plan.DirectDIBuildTargets = parseDelimitedValues(targets)
			if len(plan.DirectDIBuildTargets) == 0 {
				return plan, menuStay, fmt.Errorf("at least one direct d-i build target is required")
			}
			depPackages, err := a.promptOptionalString("Extra packages needed before running the direct d-i build (optional)")
			if err != nil {
				return plan, menuStay, err
			}
			plan.DirectDIBuildDepPackages = parseDelimitedValues(depPackages)
			reallyCleanBefore, err := a.promptYesNo("Run make reallyclean before the direct d-i build", true)
			if err != nil {
				return plan, menuStay, err
			}
			plan.DirectDIBuildReallyCleanBefore = reallyCleanBefore
			reallyCleanAfter, err := a.promptYesNo("Run make reallyclean after the direct d-i build", true)
			if err != nil {
				return plan, menuStay, err
			}
			plan.DirectDIBuildReallyCleanAfter = reallyCleanAfter
		}
	}

	preseedPath, err := a.promptOptionalString("Absolute path to a preseed.cfg file (optional)")
	if err != nil {
		return plan, menuStay, err
	}
	plan.PreseedPath, err = resolveOptionalExistingFile(preseedPath)
	if err != nil {
		return plan, menuStay, err
	}

	installerIncludeDir, err := a.promptOptionalString("Absolute path to an extra config/includes.installer overlay directory (optional)")
	if err != nil {
		return plan, menuStay, err
	}
	plan.InstallerIncludeDir, err = resolveOptionalExistingDir(installerIncludeDir)
	if err != nil {
		return plan, menuStay, err
	}

	if !netinstOnly {
		liveIncludeDir, err := a.promptOptionalString("Absolute path to an extra config/includes.chroot overlay directory (optional)")
		if err != nil {
			return plan, menuStay, err
		}
		plan.LiveIncludeDir, err = resolveOptionalExistingDir(liveIncludeDir)
		if err != nil {
			return plan, menuStay, err
		}
	}

	binaryIncludeDir, err := a.promptOptionalString("Absolute path to an extra config/includes.binary overlay directory (optional)")
	if err != nil {
		return plan, menuStay, err
	}
	plan.BinaryIncludeDir, err = resolveOptionalExistingDir(binaryIncludeDir)
	if err != nil {
		return plan, menuStay, err
	}

	bootloaderOverrideDir, err := a.promptOptionalString("Absolute path to a config/bootloaders override directory (optional)")
	if err != nil {
		return plan, menuStay, err
	}
	plan.BootloaderOverrideDir, err = resolveOptionalExistingDir(bootloaderOverrideDir)
	if err != nil {
		return plan, menuStay, err
	}

	if plan.InstallerMode != buildISOInstallerModeNone {
		installerDistribution, err := a.promptOptionalString("Optional Debian Installer distribution override (optional)")
		if err != nil {
			return plan, menuStay, err
		}
		plan.InstallerDistribution = collapseWhitespace(installerDistribution)

		installerBootAppend, err := a.promptOptionalString("Optional --bootappend-install kernel arguments (optional)")
		if err != nil {
			return plan, menuStay, err
		}
		plan.InstallerBootAppend = strings.TrimSpace(installerBootAppend)
	}

	if !netinstOnly {
		printStepHeader(3, 4, "Kernel and Storage Tooling")
		kernelMode, action, err := a.chooseBuildISOKernelMode(plan.KernelMode)
		if err != nil {
			return plan, menuStay, err
		}
		if action != menuStay {
			return plan, action, nil
		}
		plan.KernelMode = kernelMode
		if plan.KernelMode != buildISOKernelModeStockDebian {
			stub, err := a.promptRequiredString("Kernel package stub for live-build", "linux-image")
			if err != nil {
				return plan, menuStay, err
			}
			plan.KernelPackageStub = collapseWhitespace(stub)

			flavours, err := a.promptRequiredString("Kernel flavours", plan.Architecture)
			if err != nil {
				return plan, menuStay, err
			}
			plan.KernelFlavours = parseDelimitedValues(flavours)
			if len(plan.KernelFlavours) == 0 {
				return plan, menuStay, fmt.Errorf("at least one kernel flavour is required")
			}
		}
		switch plan.KernelMode {
		case buildISOKernelModeLocalDebDir:
			kernelDebDir, err := a.promptRequiredString("Absolute path to local kernel .deb directory", "")
			if err != nil {
				return plan, menuStay, err
			}
			plan.KernelDebDir, err = resolveOptionalExistingDir(kernelDebDir)
			if err != nil {
				return plan, menuStay, err
			}
			if plan.KernelDebDir == "" {
				return plan, menuStay, fmt.Errorf("a local kernel .deb directory is required")
			}
		case buildISOKernelModeCustomRepo:
			customRepo, err := a.promptRequiredString("APT repository line for kernel packages", "")
			if err != nil {
				return plan, menuStay, err
			}
			plan.CustomAPTRepo = strings.TrimSpace(customRepo)

			customBinaryRepo, err := a.promptOptionalString("Optional runtime APT repository line to embed in the ISO (optional)")
			if err != nil {
				return plan, menuStay, err
			}
			plan.CustomBinaryAPTRepo = strings.TrimSpace(customBinaryRepo)

			repoKeyPath, err := a.promptOptionalString("Absolute path to ASCII-armored repository key file (optional)")
			if err != nil {
				return plan, menuStay, err
			}
			plan.CustomAPTRepoKeyPath, err = resolveOptionalExistingFile(repoKeyPath)
			if err != nil {
				return plan, menuStay, err
			}

			repoPin, err := a.promptOptionalString("Optional APT pinning content for the custom repository (optional)")
			if err != nil {
				return plan, menuStay, err
			}
			plan.CustomAPTRepoPin = strings.TrimSpace(repoPin)
		}

		storagePackages, err := a.promptOptionalString("Additional storage-tool package names such as nvme or SED utilities (optional)")
		if err != nil {
			return plan, menuStay, err
		}
		plan.StorageToolPackages = parseDelimitedValues(storagePackages)
	}

	reviewTitle := "Root Filesystem and Review"
	if netinstOnly {
		reviewTitle = "Debian Installer Review"
	}
	printStepHeader(4, 4, reviewTitle)
	if netinstOnly {
		plan.RootFSFormat = buildISORootFSFormatNone
		plan.BasePackages = nil
		plan.ExtraChrootPackages = nil
		plan.LiveModuleSpecPath = ""
		plan.LiveDebSpecPath = ""
		plan.LiveUdebSpecPath = ""
		plan.LocalDebDir = ""
		plan.LiveIncludeDir = ""
		plan.LiveBootAppend = ""
		plan.FilesystemModuleEntries = nil
		plan.InitramfsModules = nil
		plan.KernelInspectionModules = nil
		plan.KernelConfigSymbols = nil
		plan.StorageToolPackages = nil
		plan.IncludeInstallerLauncher = false
		plan.KernelMode = buildISOKernelModeStockDebian
		plan.KernelPackageStub = ""
		plan.KernelFlavours = nil
		plan.KernelDebDir = ""
		plan.CustomAPTRepo = ""
		plan.CustomBinaryAPTRepo = ""
		plan.CustomAPTRepoKeyPath = ""
		plan.CustomAPTRepoPin = ""
	} else {
		rootfsFormat, action, err := a.chooseBuildISORootFSFormat(plan.RootFSFormat)
		if err != nil {
			return plan, menuStay, err
		}
		if action != menuStay {
			return plan, action, nil
		}
		plan.RootFSFormat = rootfsFormat
		if plan.RootFSFormat == buildISORootFSFormatEROFS {
			if repoManagedSpecsEnabled {
				plan.LiveModuleSpecPath = managedSpecFilePath(defaultBuildISODebianLiveModuleSpecRelPath)
				if plan.InstallerMode != buildISOInstallerModeNone {
					plan.DIModuleSpecPath = managedSpecFilePath(defaultBuildISODebianDIModuleSpecRelPath)
				}
			}
			compressor, err := a.promptRequiredString("EROFS compressor", plan.EROFSCompressor)
			if err != nil {
				return plan, menuStay, err
			}
			plan.EROFSCompressor = collapseWhitespace(compressor)

			extraArgs, err := a.promptOptionalString("Optional extra mkfs.erofs arguments (optional)")
			if err != nil {
				return plan, menuStay, err
			}
			plan.EROFSExtraArgs = strings.TrimSpace(extraArgs)

			moduleInput, err := a.promptRequiredString("Initramfs modules to include early", strings.Join(defaultBuildISOInitramfsModules, " "))
			if err != nil {
				return plan, menuStay, err
			}
			plan.InitramfsModules = parseDelimitedValues(moduleInput)
			inspectionModules, err := a.promptEditableOptionalString("Kernel modules to inspect for the target live/d-i kernel version", strings.Join(defaultBuildISOInitramfsModules, " "))
			if err != nil {
				return plan, menuStay, err
			}
			plan.KernelInspectionModules = parseDelimitedValues(inspectionModules)

			if plan.InstallerMode != buildISOInstallerModeNone {
				policy, action, err := a.chooseBuildISOEROFSInstallerPolicy(plan.EROFSInstallerPolicy)
				if err != nil {
					return plan, menuStay, err
				}
				if action != menuStay {
					return plan, action, nil
				}
				plan.EROFSInstallerPolicy = policy

				componentInput, err := a.promptRequiredString("Installer-side EROFS components to audit", strings.Join(plan.EROFSInstallerComponents, " "))
				if err != nil {
					return plan, menuStay, err
				}
				plan.EROFSInstallerComponents = parseDelimitedValues(componentInput)
				if len(plan.EROFSInstallerComponents) == 0 {
					return plan, menuStay, fmt.Errorf("at least one installer-side EROFS component is required")
				}
			} else {
				plan.EROFSInstallerPolicy = ""
				plan.EROFSInstallerComponents = nil
			}
		} else {
			plan.LiveModuleSpecPath = ""
			plan.DIModuleSpecPath = ""
			moduleInput, err := a.promptEditableOptionalString(
				"Initramfs modules to include early",
				strings.Join(defaultBuildISOInitramfsModules, " "),
			)
			if err != nil {
				return plan, menuStay, err
			}
			plan.InitramfsModules = parseDelimitedValues(moduleInput)
			if len(plan.InitramfsModules) > 0 {
				plan.KernelInspectionModules = append([]string{}, plan.InitramfsModules...)
			}
			plan.EROFSInstallerPolicy = ""
			plan.EROFSInstallerComponents = nil
		}

		kernelVersionDefault := ""
		if plan.RootFSFormat == buildISORootFSFormatEROFS || len(plan.KernelInspectionModules) > 0 || len(plan.KernelConfigSymbols) > 0 {
			kernelVersionDefault = detectPreferredBuildISOKernelVersion()
		}
		kernelVersionInput, err := a.promptEditableOptionalString("Target kernel version for live/d-i module inspection", kernelVersionDefault)
		if err != nil {
			return plan, menuStay, err
		}
		plan.KernelTargetVersion = collapseWhitespace(kernelVersionInput)

		moduleTreeDir, err := a.promptEditableOptionalString("Absolute path to the module tree root for that kernel version (optional)", plan.KernelModuleTreeDir)
		if err != nil {
			return plan, menuStay, err
		}
		plan.KernelModuleTreeDir, err = resolveOptionalExistingDir(moduleTreeDir)
		if err != nil {
			return plan, menuStay, err
		}

		configSymbolDefault := strings.Join(plan.KernelConfigSymbols, " ")
		if configSymbolDefault == "" && plan.RootFSFormat == buildISORootFSFormatEROFS {
			configSymbolDefault = strings.Join(defaultBuildISOKernelConfigSymbols, " ")
		}
		configSymbolsInput, err := a.promptEditableOptionalString("Kernel CONFIG symbols to inspect before build", configSymbolDefault)
		if err != nil {
			return plan, menuStay, err
		}
		plan.KernelConfigSymbols = parseDelimitedValues(configSymbolsInput)

		if plan.KernelTargetVersion != "" || len(plan.KernelInspectionModules) > 0 || len(plan.KernelConfigSymbols) > 0 {
			downloadIfMissing, err := a.promptYesNo("Download and extract kernel packages into the cache when the target kernel version is missing locally", false)
			if err != nil {
				return plan, menuStay, err
			}
			plan.KernelDownloadIfMissing = downloadIfMissing

			showKernelEvidence, err := a.promptYesNo("Show current kernel module and CONFIG evidence before continuing", true)
			if err != nil {
				return plan, menuStay, err
			}
			if showKernelEvidence {
				evidence, evidenceErr := a.backend.InspectBuildISOKernelEvidence(BuildISOKernelInspectRequest{
					KernelVersion:         plan.KernelTargetVersion,
					ModuleNames:           plan.KernelInspectionModules,
					ModuleAliasCandidates: plan.ModuleAliasCandidates,
					ConfigSymbols:         plan.KernelConfigSymbols,
					ModuleTreeDir:         plan.KernelModuleTreeDir,
					DownloadIfMissing:     plan.KernelDownloadIfMissing,
				})
				if evidenceErr != nil {
					return plan, menuStay, evidenceErr
				}
				a.printBuildISOKernelEvidence(evidence)
			}
		}

		if plan.InstallerMode != buildISOInstallerModeNone {
			if plan.UDEBRebuildSourceOverlayDir == "" && plan.DIModuleSpecPath != "" {
				plan.UDEBRebuildSourceOverlayDir = managedSpecDirPath(defaultBuildISODebianOverlayRelPath)
			}
			if plan.UDEBRebuildSpecPath != "" && plan.UDEBRebuildSourceOverlayDir == "" {
				overlayDir, err := a.promptOptionalString("Optional absolute path to a source overlay directory for your custom linux-installer-kernel rebuild spec")
				if err != nil {
					return plan, menuStay, err
				}
				plan.UDEBRebuildSourceOverlayDir, err = resolveOptionalExistingDir(overlayDir)
				if err != nil {
					return plan, menuStay, err
				}
			}
		}

		filesystemModuleEntries, err := a.promptOptionalString("Optional live/filesystem.module entries in order (space or comma separated, optional)")
		if err != nil {
			return plan, menuStay, err
		}
		plan.FilesystemModuleEntries = parseDelimitedValues(filesystemModuleEntries)
		if plan.RootFSFormat == buildISORootFSFormatEROFS && len(plan.FilesystemModuleEntries) == 0 {
			plan.FilesystemModuleEntries = []string{"filesystem.squashfs"}
		}
	}

	cleanupOnSuccess, err := a.promptYesNo("Purge the build workspace after a successful build", true)
	if err != nil {
		return plan, menuStay, err
	}
	if cleanupOnSuccess {
		plan.CleanupMode = buildISOCleanupModePurgeWorkspace
	} else {
		plan.CleanupMode = buildISOCleanupModeKeepWorkspace
	}
	applyLiveHookKernelArgsToBuildPlan(a.config, &plan)

	a.printDebianBuildISOPlan(plan)
	confirmed, err := a.promptYesNo("Install required host dependencies and build this ISO", false)
	if err != nil {
		return plan, menuStay, err
	}
	if !confirmed {
		fmt.Println("Debian ISO build cancelled.")
		return plan, menuBack, nil
	}
	return plan, menuStay, nil
}

func (a *App) printDebianBuildISOPlan(plan BuildISOPlan) {
	printHeader("Review " + buildISODistroLabel(plan.Distro) + " ISO Build Plan")
	printSection(
		"Build",
		infoRow{Label: "Suite", Value: plan.Suite},
		infoRow{Label: "Architecture", Value: plan.Architecture},
		infoRow{Label: "Archive areas", Value: strings.Join(plan.ArchiveAreas, " ")},
		infoRow{Label: "Mirror bootstrap", Value: plan.MirrorBootstrap},
		infoRow{Label: "Mirror chroot", Value: plan.MirrorChroot},
		infoRow{Label: "Mirror binary", Value: plan.MirrorBinary},
		infoRow{Label: "Keyring packages", Value: joinOrNone(plan.KeyringPackages)},
		infoRow{Label: "Output directory", Value: plan.OutputDir},
		infoRow{Label: "ISO filename", Value: plan.ImageName},
		infoRow{Label: "Installer mode", Value: plan.InstallerMode},
		infoRow{Label: "Live bootappend", Value: plan.LiveBootAppend},
		infoRow{Label: "ISO volume", Value: plan.ISOVolume},
		infoRow{Label: "Installer launcher", Value: mapBoolLabel(plan.IncludeInstallerLauncher, "Enabled", "Disabled")},
		infoRow{Label: "Non-free firmware task", Value: mapBoolLabel(plan.IncludeNonFreeFirmware, "Enabled", "Disabled")},
	)
	printSection(
		"Packages",
		infoRow{Label: "Base live packages", Value: joinOrNone(plan.BasePackages)},
		infoRow{Label: "Extra live packages", Value: joinOrNone(plan.ExtraChrootPackages)},
		infoRow{Label: "Extra medium packages", Value: joinOrNone(plan.ExtraBinaryPackages)},
		infoRow{Label: "Live module spec", Value: plan.LiveModuleSpecPath},
		infoRow{Label: "Live deb spec", Value: plan.LiveDebSpecPath},
		infoRow{Label: "Live udeb spec", Value: plan.LiveUdebSpecPath},
		infoRow{Label: "d-i module spec", Value: plan.DIModuleSpecPath},
		infoRow{Label: "d-i deb spec", Value: plan.DIDebSpecPath},
		infoRow{Label: "d-i udeb spec", Value: plan.DIUdebSpecPath},
		infoRow{Label: "Live tool groups", Value: joinOrNone(plan.LiveToolGroups)},
		infoRow{Label: "Storage tool packages", Value: joinOrNone(plan.StorageToolPackages)},
		infoRow{Label: "Local .deb directory", Value: plan.LocalDebDir},
		infoRow{Label: "Local .udeb directory", Value: plan.LocalUdebDir},
		infoRow{Label: "Auto installer-kernel rebuild", Value: mapBoolLabel(plan.InstallerKernelRebuildEnabled, "Enabled", "Disabled")},
		infoRow{Label: "Installer-kernel source ISO", Value: plan.InstallerKernelSourceISOPath},
		infoRow{Label: "Installer-kernel modules", Value: joinOrNone(plan.InstallerKernelModules)},
		infoRow{Label: "Installer-kernel CONFIG entries", Value: joinOrNone(plan.InstallerKernelConfigEntries)},
		infoRow{Label: "Auto UDEB package rebuilds", Value: joinOrNone(plan.InstallerAutoUDEBPackages)},
		infoRow{Label: "UDEB rebuild spec", Value: plan.UDEBRebuildSpecPath},
		infoRow{Label: "UDEB source overlay", Value: plan.UDEBRebuildSourceOverlayDir},
		infoRow{Label: "Direct d-i build", Value: mapBoolLabel(plan.DirectDIBuildEnabled, "Enabled", "Disabled")},
		infoRow{Label: "Direct d-i source mode", Value: plan.DirectDIBuildSourceMode},
		infoRow{Label: "Direct d-i source tree", Value: plan.DirectDIBuildSourceTree},
		infoRow{Label: "Direct d-i source package", Value: plan.DirectDIBuildSourcePackage},
		infoRow{Label: "Direct d-i targets", Value: joinOrNone(plan.DirectDIBuildTargets)},
	)
	printSection(
		"Kernel and rootfs",
		infoRow{Label: "Kernel mode", Value: plan.KernelMode},
		infoRow{Label: "Kernel package stub", Value: plan.KernelPackageStub},
		infoRow{Label: "Kernel flavours", Value: joinOrNone(plan.KernelFlavours)},
		infoRow{Label: "Kernel .deb directory", Value: plan.KernelDebDir},
		infoRow{Label: "Custom APT repository", Value: plan.CustomAPTRepo},
		infoRow{Label: "Root filesystem format", Value: plan.RootFSFormat},
		infoRow{Label: "EROFS compressor", Value: plan.EROFSCompressor},
		infoRow{Label: "EROFS installer policy", Value: plan.EROFSInstallerPolicy},
		infoRow{Label: "EROFS installer components", Value: joinOrNone(plan.EROFSInstallerComponents)},
		infoRow{Label: "Initramfs modules", Value: joinOrNone(plan.InitramfsModules)},
		infoRow{Label: "Kernel inspection modules", Value: joinOrNone(plan.KernelInspectionModules)},
		infoRow{Label: "Kernel target version", Value: plan.KernelTargetVersion},
		infoRow{Label: "Kernel module tree", Value: plan.KernelModuleTreeDir},
		infoRow{Label: "Kernel CONFIG symbols", Value: joinOrNone(plan.KernelConfigSymbols)},
		infoRow{Label: "Download missing kernel packages", Value: mapBoolLabel(plan.KernelDownloadIfMissing, "Enabled", "Disabled")},
	)
	printSection(
		"Installer inputs",
		infoRow{Label: "Preseed path", Value: plan.PreseedPath},
		infoRow{Label: "Installer overlay", Value: plan.InstallerIncludeDir},
		infoRow{Label: "Live overlay", Value: plan.LiveIncludeDir},
		infoRow{Label: "Binary overlay", Value: plan.BinaryIncludeDir},
		infoRow{Label: "Bootloader overrides", Value: plan.BootloaderOverrideDir},
		infoRow{Label: "Installer distribution", Value: plan.InstallerDistribution},
		infoRow{Label: "Installer bootappend", Value: plan.InstallerBootAppend},
		infoRow{Label: "filesystem.module entries", Value: joinOrNone(plan.FilesystemModuleEntries)},
		infoRow{Label: "Direct d-i build deps", Value: joinOrNone(plan.DirectDIBuildDepPackages)},
		infoRow{Label: "Direct d-i reallyclean before", Value: mapBoolLabel(plan.DirectDIBuildReallyCleanBefore, "Enabled", "Disabled")},
		infoRow{Label: "Direct d-i reallyclean after", Value: mapBoolLabel(plan.DirectDIBuildReallyCleanAfter, "Enabled", "Disabled")},
		infoRow{Label: "Cleanup mode", Value: plan.CleanupMode},
	)
}

func buildISODistroLabel(distro string) string {
	switch strings.TrimSpace(distro) {
	case buildISODistroDebian:
		return "Debian"
	case buildISODistroKali:
		return "Kali"
	case buildISODistroUbuntu:
		return "Ubuntu"
	default:
		return blankIfEmpty(distro, "Custom")
	}
}

func (a *App) printBuildISOKernelEvidence(result BuildISOKernelInspectResult) {
	printHeader("Kernel Evidence")
	moduleStatuses := make([]infoRow, 0, len(result.Modules))
	for _, module := range result.Modules {
		value := "missing"
		if module.Found {
			value = module.Path
		}
		moduleStatuses = append(moduleStatuses, infoRow{Label: module.Name, Value: value})
	}
	configStatuses := make([]infoRow, 0, len(result.ConfigSymbols))
	for _, symbol := range result.ConfigSymbols {
		value := symbol.Value
		if value == "" {
			value = "not found"
		}
		if symbol.Line != "" {
			value = symbol.Line
		}
		configStatuses = append(configStatuses, infoRow{Label: symbol.Symbol, Value: value})
	}
	printSection(
		"Kernel source",
		infoRow{Label: "Kernel version", Value: result.KernelVersion},
		infoRow{Label: "Module tree", Value: result.ModuleTreeDir},
		infoRow{Label: "Config path", Value: result.ConfigPath},
		infoRow{Label: "Download attempted", Value: mapBoolLabel(result.DownloadAttempted, "Yes", "No")},
		infoRow{Label: "Downloaded cache used", Value: mapBoolLabel(result.DownloadUsed, "Yes", "No")},
		infoRow{Label: "Download cache dir", Value: result.DownloadCacheDir},
	)
	printSection("Modules", moduleStatuses...)
	printSection("Kernel CONFIG", configStatuses...)
	printBulletList("Notes", result.Notes...)
}

func (a *App) chooseBuildISOInstallerMode(current string) (string, menuAction, error) {
	for {
		printHeader("Installer Mode")
		a.printMenu(
			menuEntry{Key: "1", Label: "None", Detail: "Build a live ISO without Debian Installer integration."},
			menuEntry{Key: "2", Label: "Netinst", Detail: "Build installer-only media with --debian-installer netinst, no --system, and no Live rootfs inputs."},
			menuEntry{Key: "3", Label: "Live", Detail: "Include the live Debian Installer copy-to-disk path."},
			menuEntry{Key: "b", Label: "Go Back"},
			menuEntry{Key: "e", Label: "Exit"},
		)
		choice, err := a.promptChoice("Select the installer mode")
		if err != nil {
			return "", menuStay, err
		}
		switch choice {
		case "1":
			return buildISOInstallerModeNone, menuStay, nil
		case "2":
			return buildISOInstallerModeNetinst, menuStay, nil
		case "3":
			return buildISOInstallerModeLive, menuStay, nil
		case "b":
			if current == "" {
				return "", menuBack, nil
			}
			return current, menuStay, nil
		case "e":
			return "", menuExit, nil
		default:
			fmt.Println("Invalid selection.")
		}
	}
}

func (a *App) chooseBuildISOKernelMode(current string) (string, menuAction, error) {
	for {
		printHeader("Kernel Mode")
		a.printMenu(
			menuEntry{Key: "1", Label: "Stock Debian", Detail: "Use the suite default Debian kernel packages."},
			menuEntry{Key: "2", Label: "Repository Package Stub", Detail: "Use custom kernel package stubs available in configured APT repositories."},
			menuEntry{Key: "3", Label: "Local Kernel .deb Directory", Detail: "Stage local custom kernel .deb packages into the live-build tree."},
			menuEntry{Key: "4", Label: "Custom APT Repository", Detail: "Generate config/archives entries and use a custom repository such as XanMod."},
			menuEntry{Key: "b", Label: "Go Back"},
			menuEntry{Key: "e", Label: "Exit"},
		)
		choice, err := a.promptChoice("Select the kernel mode")
		if err != nil {
			return "", menuStay, err
		}
		switch choice {
		case "1":
			return buildISOKernelModeStockDebian, menuStay, nil
		case "2":
			return buildISOKernelModePackageStub, menuStay, nil
		case "3":
			return buildISOKernelModeLocalDebDir, menuStay, nil
		case "4":
			return buildISOKernelModeCustomRepo, menuStay, nil
		case "b":
			if current == "" {
				return "", menuBack, nil
			}
			return current, menuStay, nil
		case "e":
			return "", menuExit, nil
		default:
			fmt.Println("Invalid selection.")
		}
	}
}

func (a *App) chooseBuildISORootFSFormat(current string) (string, menuAction, error) {
	for {
		printHeader("Root Filesystem Format")
		a.printMenu(
			menuEntry{Key: "1", Label: "SquashFS", Detail: "Leave the upstream live-build root filesystem format unchanged."},
			menuEntry{Key: "2", Label: "EROFS", Detail: "Replace the generated live root image with an EROFS image through a binary hook while preserving the upstream filename."},
			menuEntry{Key: "b", Label: "Go Back"},
			menuEntry{Key: "e", Label: "Exit"},
		)
		choice, err := a.promptChoice("Select the root filesystem format")
		if err != nil {
			return "", menuStay, err
		}
		switch choice {
		case "1":
			return buildISORootFSFormatSquashFS, menuStay, nil
		case "2":
			return buildISORootFSFormatEROFS, menuStay, nil
		case "b":
			if current == "" {
				return "", menuBack, nil
			}
			return current, menuStay, nil
		case "e":
			return "", menuExit, nil
		default:
			fmt.Println("Invalid selection.")
		}
	}
}

func (a *App) chooseBuildISOEROFSInstallerPolicy(current string) (string, menuAction, error) {
	for {
		printHeader("EROFS Installer Policy")
		a.printMenu(
			menuEntry{Key: "1", Label: "Warn", Detail: "Allow the build to continue but warn when explicit installer-side EROFS integration inputs are incomplete."},
			menuEntry{Key: "2", Label: "Require", Detail: "Require installer-side .udeb inputs plus includes.installer content for EROFS-aware Debian Installer integration."},
			menuEntry{Key: "b", Label: "Go Back"},
			menuEntry{Key: "e", Label: "Exit"},
		)
		choice, err := a.promptChoice("Select the installer-side EROFS policy")
		if err != nil {
			return "", menuStay, err
		}
		switch choice {
		case "1":
			return buildISOEROFSInstallerPolicyWarn, menuStay, nil
		case "2":
			return buildISOEROFSInstallerPolicyRequire, menuStay, nil
		case "b":
			if current == "" {
				return "", menuBack, nil
			}
			return current, menuStay, nil
		case "e":
			return "", menuExit, nil
		default:
			fmt.Println("Invalid selection.")
		}
	}
}

func (a *App) chooseBuildISODirectDIBuildSourceMode(current string) (string, menuAction, error) {
	for {
		printHeader("Direct d-i Source")
		a.printMenu(
			menuEntry{Key: "1", Label: "APT Source", Detail: "Fetch the Debian Installer source package with apt source in an isolated workspace."},
			menuEntry{Key: "2", Label: "Local Tree", Detail: "Copy an existing Debian Installer source tree into the isolated build workspace."},
			menuEntry{Key: "b", Label: "Go Back"},
			menuEntry{Key: "e", Label: "Exit"},
		)
		choice, err := a.promptChoice("Select the direct d-i source mode")
		if err != nil {
			return "", menuStay, err
		}
		switch choice {
		case "1":
			return buildISODirectDIBuildSourceModeAptSource, menuStay, nil
		case "2":
			return buildISODirectDIBuildSourceModeLocalTree, menuStay, nil
		case "b":
			if current == "" {
				return "", menuBack, nil
			}
			return current, menuStay, nil
		case "e":
			return "", menuExit, nil
		default:
			fmt.Println("Invalid selection.")
		}
	}
}

func parseDelimitedValues(value string) []string {
	value = strings.ReplaceAll(value, ",", " ")
	fields := strings.Fields(value)
	if len(fields) == 0 {
		return nil
	}
	seen := make(map[string]struct{}, len(fields))
	result := make([]string, 0, len(fields))
	for _, field := range fields {
		field = strings.TrimSpace(field)
		if field == "" {
			continue
		}
		if _, ok := seen[field]; ok {
			continue
		}
		seen[field] = struct{}{}
		result = append(result, field)
	}
	return result
}

func parseCommaDelimitedValues(value string) []string {
	fields := strings.Split(value, ",")
	if len(fields) == 0 {
		return nil
	}
	seen := make(map[string]struct{}, len(fields))
	result := make([]string, 0, len(fields))
	for _, field := range fields {
		field = collapseWhitespace(strings.TrimSpace(field))
		if field == "" {
			continue
		}
		if _, ok := seen[field]; ok {
			continue
		}
		seen[field] = struct{}{}
		result = append(result, field)
	}
	return result
}

func appendUniqueValue(values []string, value string) []string {
	value = strings.TrimSpace(value)
	if value == "" {
		return values
	}
	for _, existing := range values {
		if existing == value {
			return values
		}
	}
	return append(values, value)
}

func resolveOptionalExistingDir(value string) (string, error) {
	resolved, err := resolveAbsolutePathInput(value)
	if err != nil {
		return "", err
	}
	if resolved == "" {
		return "", nil
	}
	info, err := os.Stat(resolved)
	if err != nil {
		return "", err
	}
	if !info.IsDir() {
		return "", fmt.Errorf("path is not a directory: %s", resolved)
	}
	return filepath.Clean(resolved), nil
}

func resolveOptionalExistingFile(value string) (string, error) {
	resolved, err := resolveAbsolutePathInput(value)
	if err != nil {
		return "", err
	}
	if resolved == "" {
		return "", nil
	}
	info, err := os.Stat(resolved)
	if err != nil {
		return "", err
	}
	if info.IsDir() {
		return "", fmt.Errorf("path is a directory, expected a file: %s", resolved)
	}
	return filepath.Clean(resolved), nil
}
