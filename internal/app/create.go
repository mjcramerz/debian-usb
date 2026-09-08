package app

import (
	"errors"
	"fmt"
	"path/filepath"
	"strconv"
	"strings"
)

type installerModuleOption struct {
	Name   string
	Detail string
}

var debianNetinstExtraModuleOptions = []installerModuleOption{
	{Name: "xxhash_generic", Detail: "Enable the Debian installer crypto xxhash module in the rebuilt installer initrd."},
	{Name: "lz4", Detail: "Enable the Debian installer crypto lz4 module and its matching compression support."},
}

func (a *App) collectCreateRequest(spec profileSpec) (CreateRequest, menuAction, error) {
	req := CreateRequest{Profile: spec.Key}

	sourceRole, action, err := a.chooseCreateSourceRole(spec)
	if err != nil {
		return req, menuStay, err
	}
	if action != menuStay {
		return req, action, nil
	}
	req.SourceRole = sourceRole

	printStepHeader(1, 4, spec.MenuLabel+" Source")
	preparation, action, err := a.collectSourceSelection(spec, req.SourceRole)
	if err != nil {
		return req, menuStay, err
	}
	if action != menuStay {
		return req, action, nil
	}
	req.Preparation = preparation
	req.ISOPath = preparation.ISO.Path

	inspection, err := a.inspectSelectedSource(spec, req.SourceRole, preparation)
	if err != nil {
		return req, menuStay, err
	}
	if err := validateManagedSourceSelection(spec, req.SourceRole, inspection, req.ISOPath); err != nil {
		return req, menuStay, err
	}
	req.Inspection = inspection
	mode, action, err := a.chooseWriteMode(spec, inspection, req.SourceRole)
	if err != nil {
		return req, menuStay, err
	}
	if action != menuStay {
		return req, action, nil
	}
	req.WriteMode = mode
	if req.WriteMode == writeModeManaged && req.SourceRole == multiOSSourceRolePrimary && isLiveCapableMedia(inspection.MediaClass) {
		overlay, action, err := a.collectLiveInitrdOverlay(spec, inspection)
		if err != nil || action != menuStay {
			return req, action, err
		}
		req.Preparation.InitrdOverlayDir = overlay
		liveToolGroups, action, err := a.promptLiveToolGroups(spec.Key, nil)
		if err != nil {
			return req, menuStay, err
		}
		if action != menuStay {
			return req, action, nil
		}
		req.LiveToolGroups = liveToolGroups
	}

	if req.WriteMode == writeModeManaged {
		trustMode, action, err := a.chooseSecureBootTrustMode()
		if err != nil {
			return req, menuStay, err
		}
		if action != menuStay {
			return req, action, nil
		}
		req.SecureBootTrust = trustMode
		req.UseCustomGrubMenu = true
		req.PreserveUpstreamGrubEntries = true
		if createPreseedEligible(spec.Key, req.SourceRole, req.UseCustomGrubMenu, inspection) {
			if splitInstallerProfile(spec.Key, req.SourceRole) {
				printSection("Preseed",
					infoRow{Label: "Profiles", Value: "Desktop and Server; HTTPS WEB, HTTP LAN, INITRD PRESEED, USB HD-MEDIA"},
					infoRow{Label: "USB source", Value: "Copy each complete preseed.cfg parent directory only when explicitly selected. Menus remain available when copying is declined."})
			} else {
				printSection("Preseed",
					infoRow{Label: "Mode", Value: "HTTP and USB preseed entries are rendered for installer-capable Debian/Kali managed sources"},
					infoRow{Label: "USB source", Value: "The writer stages the configured PRESEED_HOST_*_PATH tree to the directory implied by PRESEED_USB_*_FILE"})
			}
			req.Preseed = true
			if splitInstallerProfile(spec.Key, req.SourceRole) {
				req.HDMediaPreseedDirs, err = a.promptHDMediaPreseedDirs(spec.Key)
				if err != nil {
					return req, menuStay, err
				}
			}
		}
		req.Persistence, req.PersistenceMode, req.PersistenceSizeGiB, req.ISOPath, req.Inspection, err = a.promptPersistenceSettings(spec.MultiOSLabel, spec, req.ISOPath, req.SourceRole, inspection)
		if err != nil {
			return req, menuStay, err
		}
		if isLiveCapableMedia(req.Inspection.MediaClass) {
			req.MenuLabel = spec.DefaultLiveMenuLabel
		}
	}
	return req, menuStay, nil
}

func (a *App) chooseCreateSourceRole(spec profileSpec) (string, menuAction, error) {
	if !profileSupportsManagedNetinst(spec.Key) && !profileSupportsManagedNetboot(spec.Key) {
		return multiOSSourceRolePrimary, menuStay, nil
	}

	for {
		printStepHeader(1, 4, spec.MenuLabel+" Source Role")
		printSection(
			"Source Role",
			infoRow{Label: "Live", Value: "Boot the selected live ISO intact through an isolated loopback entry"},
			infoRow{Label: "Netinst", Value: "Stage an opaque ISO plus separately downloaded hd-media/vmlinuz and hd-media/initrd.gz"},
			infoRow{Label: "Netboot", Value: "Stage dedicated kernel/initrd assets only; no ISO payload is used"},
		)
		a.printMenu(
			menuEntry{Key: "1", Label: "Live ISO", Detail: "Opaque live ISO; installer entries in hybrid media are ignored"},
			menuEntry{Key: "2", Label: "Netinst (hd-media)", Detail: "Opaque netinst ISO plus separate hd-media kernel/initrd"},
			menuEntry{Key: "3", Label: "Netboot", Detail: "Managed kernel/initrd assets without an ISO payload"},
			menuEntry{Key: "b", Label: "Go Back"},
			menuEntry{Key: "e", Label: "Exit"},
		)
		choice, err := a.promptChoice("Select the source role")
		if err != nil {
			return "", menuStay, err
		}
		switch choice {
		case "1":
			return multiOSSourceRolePrimary, menuStay, nil
		case "2":
			return multiOSSourceRoleNetinst, menuStay, nil
		case "3":
			return multiOSSourceRoleNetboot, menuStay, nil
		case "b":
			return "", menuBack, nil
		case "e":
			return "", menuExit, nil
		default:
			fmt.Println("Invalid selection.")
		}
	}
}

func (a *App) promptManagedSourceReleaseChannel(profile string) (managedSourceReleaseChannel, menuAction, error) {
	for {
		stableLabel := managedSourceReleaseProfileLabel(profile, managedSourceReleaseStable)
		testingLabel := managedSourceReleaseProfileLabel(profile, managedSourceReleaseTesting)
		printSection(
			"Release Channel",
			infoRow{Label: stableLabel, Value: "Published release ISO and installer assets"},
			infoRow{Label: testingLabel, Value: "Pre-release or testing ISO and installer assets"},
		)
		a.printMenu(
			menuEntry{Key: "1", Label: stableLabel, Detail: "Use the configured stable release URLs"},
			menuEntry{Key: "2", Label: testingLabel, Detail: "Use the configured testing release URLs"},
			menuEntry{Key: "b", Label: "Go Back"},
			menuEntry{Key: "e", Label: "Exit"},
		)
		choice, err := a.promptChoice("Select the release channel")
		if err != nil {
			return "", menuStay, err
		}
		switch choice {
		case "1":
			return managedSourceReleaseStable, menuStay, nil
		case "2":
			return managedSourceReleaseTesting, menuStay, nil
		case "b":
			return "", menuBack, nil
		case "e":
			return "", menuExit, nil
		default:
			fmt.Println("Invalid selection.")
		}
	}
}

func (a *App) promptManagedInstallerSourceExtraModules(
	spec profileSpec,
	sourceRole string,
	kernelPath string,
	initrdPath string,
) ([]string, menuAction, error) {
	if spec.Key != profileDebian || sourceRole != multiOSSourceRoleNetinst {
		return nil, menuStay, nil
	}
	selected := make(map[string]bool, len(debianNetinstExtraModuleOptions))
	continueKey := strconv.Itoa(len(debianNetinstExtraModuleOptions) + 1)
	for {
		printHeader(spec.MultiOSLabel + " Netinst Source")
		printSection(
			"Installer Initrd Modules",
			infoRow{Label: "Kernel", Value: kernelPath},
			infoRow{Label: "Initrd", Value: initrdPath},
			infoRow{Label: "Behavior", Value: "Selected modules are rebuilt into initrd.gz using the exact kernel version detected from the installer initrd. Continue with nothing selected to skip."},
			infoRow{Label: "Selected", Value: managedInstallerSourceModuleSelectionLabel(selected)},
		)
		entries := make([]menuEntry, 0, len(debianNetinstExtraModuleOptions)+3)
		for index, option := range debianNetinstExtraModuleOptions {
			state := "not selected"
			if selected[option.Name] {
				state = "selected"
			}
			entries = append(entries, menuEntry{
				Key:      strconv.Itoa(index + 1),
				Label:    option.Name,
				Selected: selected[option.Name],
				Detail:   fmt.Sprintf("%s (%s)", option.Detail, state),
			})
		}
		entries = append(entries,
			menuEntry{Key: continueKey, Label: "Continue", Detail: "Proceed with the selected module set, or skip when none are selected."},
			menuEntry{Key: "b", Label: "Go Back"},
			menuEntry{Key: "e", Label: "Exit"},
		)
		a.printMenu(entries...)
		choice, err := a.promptChoice("Select an option")
		if err != nil {
			return nil, menuStay, err
		}
		switch choice {
		case continueKey:
			return managedInstallerSourceSelectedModules(selected), menuStay, nil
		case "b":
			return nil, menuBack, nil
		case "e":
			return nil, menuExit, nil
		default:
			index, err := strconv.Atoi(choice)
			if err != nil || index < 1 || index > len(debianNetinstExtraModuleOptions) {
				fmt.Println("Invalid selection.")
				continue
			}
			option := debianNetinstExtraModuleOptions[index-1]
			selected[option.Name] = !selected[option.Name]
		}
	}
}

func managedInstallerSourceModuleSelectionLabel(selected map[string]bool) string {
	modules := managedInstallerSourceSelectedModules(selected)
	if len(modules) == 0 {
		return "<none>"
	}
	return strings.Join(modules, ", ")
}

func managedInstallerSourceSelectedModules(selected map[string]bool) []string {
	modules := make([]string, 0, len(debianNetinstExtraModuleOptions))
	for _, option := range debianNetinstExtraModuleOptions {
		if selected[option.Name] {
			modules = append(modules, option.Name)
		}
	}
	return modules
}

func (a *App) promptManagedInstallerSourceStrategy(
	spec profileSpec,
	sourceRole string,
	kernelPath string,
	initrdPath string,
	extraModules []string,
) (string, menuAction, error) {
	if spec.Key != profileDebian || sourceRole != multiOSSourceRoleNetinst || len(extraModules) == 0 {
		return "", menuStay, nil
	}
	for {
		printHeader(spec.MultiOSLabel + " Netinst Source")
		printSection(
			"Module Application Strategy",
			infoRow{Label: "Kernel", Value: kernelPath},
			infoRow{Label: "Initrd", Value: initrdPath},
			infoRow{Label: "Modules", Value: strings.Join(extraModules, ", ")},
			infoRow{Label: "Use Host Kernel", Value: "Recommended. Use /lib/modules/<abi> on the host, or download matching binary kernel packages for that same ABI, then copy the required modules into initrd.gz."},
			infoRow{Label: "Build UDEBs From Source", Value: "Slower fallback. Rebuild matching Debian installer kernel udebs from the Debian linux source package for the detected installer ABI."},
		)
		a.printMenu(
			menuEntry{Key: "1", Label: "Use Host Kernel", Detail: "Fast path. Prefer the installed or downloaded matching binary kernel packages for the same ABI."},
			menuEntry{Key: "2", Label: "Build UDEBs From Source", Detail: "Slow path. Use Debian kernel source and dpkg-buildpackage for the detected installer ABI."},
			menuEntry{Key: "b", Label: "Go Back"},
			menuEntry{Key: "e", Label: "Exit"},
		)
		choice, err := a.promptChoice("Select the module application strategy")
		if err != nil {
			return "", menuStay, err
		}
		switch choice {
		case "1":
			return installerModuleSourceStrategyHostKernel, menuStay, nil
		case "2":
			return installerModuleSourceStrategySourceUDEB, menuStay, nil
		case "b":
			return "", menuBack, nil
		case "e":
			return "", menuExit, nil
		default:
			fmt.Println("Invalid selection.")
		}
	}
}

func initrdOverlayFamily(profile string) string {
	switch profile {
	case profileDebian:
		return "debian"
	case profileKaliLinux, profileKaliPurple:
		return "kali"
	case profileUbuntuDesktop, profileUbuntuServer:
		return "ubuntu"
	case profileTails:
		return "tails"
	default:
		return ""
	}
}

func initrdOverlayStage(sourceRole string, mediaClass string) string {
	switch sourceRole {
	case multiOSSourceRoleNetinst, multiOSSourceRoleNetboot:
		return sourceRole
	case multiOSSourceRolePrimary:
		if isLiveCapableMedia(mediaClass) {
			return "live"
		}
	}
	return ""
}

func (a *App) promptInitrdOverlayContent(
	spec profileSpec,
	sourceRole string,
	mediaClass string,
) (string, menuAction, error) {
	if a.backend == nil || strings.TrimSpace(a.backend.initrdRoot) == "" {
		return "", menuStay, nil
	}
	family := initrdOverlayFamily(spec.Key)
	stage := initrdOverlayStage(sourceRole, mediaClass)
	if family == "" || stage == "" {
		return "", menuStay, nil
	}
	overlayDir, err := resolveOptionalExistingDir(filepath.Join(a.backend.initrdRoot, family, stage))
	if err != nil {
		return "", menuStay, err
	}
	prompt := fmt.Sprintf("Include the contents of initrd/%s/%s at the root of the %s %s initrd", family, stage, spec.MultiOSLabel, stage)
	if splitInstallerProfile(spec.Key, sourceRole) {
		prompt = fmt.Sprintf("Embed initrd/%s/%s/desktop and server into their separate installer initrds", family, stage)
	}
	include, err := a.promptYesNo(prompt, false)
	if err != nil {
		return "", menuStay, err
	}
	if !include {
		return "", menuStay, nil
	}
	printSection(
		"Initrd Content",
		infoRow{Label: "Source", Value: overlayDir},
		infoRow{Label: "Target", Value: "/ in the selected " + stage + " initrd"},
	)
	return overlayDir, menuStay, nil
}

// collectLiveInitrdOverlay records the overlay without opening or rebuilding an initrd.
func (a *App) collectLiveInitrdOverlay(spec profileSpec, inspection ISOInspection) (string, menuAction, error) {
	if spec.Key == profileTails {
		fmt.Println("Tails: experimental stock-ISO boot only; no remaster, custom initrd or generic persistence. Use a dedicated official Tails USB for supported security and Persistent Storage.")
		return "", menuStay, nil
	}
	if (spec.Key != profileDebian && spec.Key != profileKaliLinux) || !isLiveCapableMedia(inspection.MediaClass) {
		return a.promptInitrdOverlayContent(spec, multiOSSourceRolePrimary, inspection.MediaClass)
	}
	if a.backend == nil || strings.TrimSpace(a.backend.initrdRoot) == "" {
		return "", menuStay, fmt.Errorf("%s Live requires the managed initrd overlay root", spec.MultiOSLabel)
	}
	family := "debian"
	if spec.Key == profileKaliLinux {
		family = "kali"
	}
	path, err := resolveOptionalExistingDir(filepath.Join(a.backend.initrdRoot, family, "live"))
	if err == nil && path == "" {
		err = fmt.Errorf("required %s Live initrd overlay is missing", spec.MultiOSLabel)
	}
	return path, menuStay, err
}

func (a *App) printManagedInstallerSourcePreparation(
	spec profileSpec,
	sourceRole string,
	kernelPath string,
	initrdPath string,
	extraModules []string,
	moduleSourceStrategy string,
	initrdPreseedPath string,
) {
	if sourceRole != multiOSSourceRoleNetinst || (len(extraModules) == 0 && strings.TrimSpace(initrdPreseedPath) == "") {
		return
	}
	strategyLabel := "Use Host Kernel"
	if moduleSourceStrategy == installerModuleSourceStrategySourceUDEB {
		strategyLabel = "Build UDEBs From Source"
	}
	moduleLabel := "<none>"
	if len(extraModules) > 0 {
		moduleLabel = strings.Join(extraModules, ", ")
	}
	taskDetail := "Preparing the managed netinst source bundle."
	if len(extraModules) > 0 && initrdPreseedPath != "" {
		taskDetail = "Preparing the managed netinst source bundle, updating installer modules, and embedding the repo preseed.cfg into initrd.gz."
	} else if len(extraModules) > 0 {
		taskDetail = "Preparing the managed netinst source and updating the installer initrd for the selected modules."
	} else if initrdPreseedPath != "" {
		taskDetail = "Preparing the managed netinst source and embedding the repo preseed.cfg into initrd.gz."
	}
	printSection(
		"Working",
		infoRow{Label: "Task", Value: taskDetail},
		infoRow{Label: "Kernel", Value: kernelPath},
		infoRow{Label: "Initrd", Value: initrdPath},
		infoRow{Label: "Modules", Value: moduleLabel},
		infoRow{Label: "Strategy", Value: strategyLabel},
		infoRow{Label: "Embedded preseed", Value: blankIfEmpty(initrdPreseedPath, "<disabled>")},
	)
	checklist := []string{
		"1. Verify the helper tools needed to inspect and rebuild the installer initrd.",
		"2. Detect the installer kernel ABI from initrd.gz and confirm that vmlinuz matches it.",
	}
	if len(extraModules) > 0 {
		checklist = append(
			checklist,
			"3. Inspect whether each selected module is already built in, available in the matching kernel packages, or needs a Debian source rebuild.",
			"4. Apply the selected strategy: copy modules from the host/downloaded matching kernel packages, or rebuild matching installer udebs from Debian source.",
		)
	} else {
		checklist = append(checklist, "3. Skip optional installer-module changes because no extra modules were selected.")
	}
	if initrdPreseedPath != "" {
		checklist = append(checklist, "5. Copy the selected repo preseed file into initrd.gz as /preseed.cfg.")
	} else {
		checklist = append(checklist, "5. Leave /preseed.cfg out of initrd.gz.")
	}
	checklist = append(checklist, "6. Refresh any changed initrd metadata, repack initrd.gz, and write manifests for the prepared source bundle.")
	printBulletList("Execution Checklist", checklist...)
	fmt.Println("After you enter the sudo password, live checklist updates will stream below. No further input is required unless an error is shown.")
	fmt.Println()
}

func (a *App) promptOfflinePreseedSourceDir(profileLabel string) (string, error) {
	includeOfflinePreseed, err := a.promptYesNo("Include offline preseed content for "+profileLabel+" on the written USB", false)
	if err != nil {
		return "", err
	}
	if !includeOfflinePreseed {
		return "", nil
	}
	sourceDir, err := a.promptRequiredString(
		"Enter the absolute path to the offline preseed directory to stage under /preseed",
		"",
	)
	if err != nil {
		return "", err
	}
	return resolveOptionalExistingDir(sourceDir)
}

func singleCreatePreseedEligible(useCustomGrubMenu bool, inspection ISOInspection) bool {
	return useCustomGrubMenu && strings.TrimSpace(inspection.MediaClass) == "installer" && inspectionHasInstaller(inspection)
}

func fixedDebianKaliPreseedProfile(profile string) bool {
	return profile == profileDebian || profile == profileKaliLinux || profile == profileKaliPurple
}

func createPreseedEligible(profile, sourceRole string, useCustomGrubMenu bool, inspection ISOInspection) bool {
	if !useCustomGrubMenu || !fixedDebianKaliPreseedProfile(profile) || !inspectionHasInstaller(inspection) {
		return false
	}
	switch profile {
	case profileDebian, profileKaliLinux:
		return sourceRole == multiOSSourceRoleNetinst || sourceRole == multiOSSourceRoleNetboot
	case profileKaliPurple:
		return sourceRole == multiOSSourceRolePrimary && installerOnlyMedia(inspection)
	default:
		return false
	}
}

func validateManagedSourceSelection(spec profileSpec, sourceRole string, inspection ISOInspection, sourcePath string) error {
	if !profileSupportsSourceRole(spec.Key, sourceRole) {
		return fmt.Errorf("%s does not support source role %s", spec.MultiOSLabel, sourceRole)
	}
	if sourceRole == multiOSSourceRoleNetinst || sourceRole == multiOSSourceRoleNetboot {
		if !inspectionHasInstaller(inspection) {
			return fmt.Errorf("%s %s source must expose installer boot entries: %s", spec.MultiOSLabel, sourceRole, sourcePath)
		}
		return nil
	}
	switch spec.Key {
	case profileDebian, profileKaliLinux, profileTails:
		if !isLiveCapableMedia(inspection.MediaClass) {
			return fmt.Errorf("%s primary source must be live or hybrid media; use the netinst or netboot source role for installer-only media: %s", spec.MultiOSLabel, sourcePath)
		}
	case profileKaliPurple:
		if !inspectionHasInstaller(inspection) {
			return fmt.Errorf("%s primary source must expose installer boot entries: %s", spec.MultiOSLabel, sourcePath)
		}
	}
	return nil
}

func persistencePromptEligible(spec profileSpec, inspection ISOInspection) bool {
	return spec.SupportsPersistence && inspection.SupportsPersistence && isLiveCapableMedia(inspection.MediaClass)
}

func installerOnlyMedia(inspection ISOInspection) bool {
	return strings.TrimSpace(inspection.MediaClass) == "installer"
}

func normalizeSecureBootTrustMode(value string) string {
	switch strings.TrimSpace(value) {
	case "", secureBootTrustMOK:
		return secureBootTrustMOK
	case secureBootTrustFirmwareDB:
		return secureBootTrustFirmwareDB
	default:
		return ""
	}
}

func secureBootTrustModeLabel(value string) string {
	switch normalizeSecureBootTrustMode(value) {
	case secureBootTrustMOK:
		return "MOK Enrollment on USB"
	case secureBootTrustFirmwareDB:
		return "Firmware db import"
	default:
		return "<invalid>"
	}
}

func secureBootTrustModeTechnicalLabel(value string) string {
	switch normalizeSecureBootTrustMode(value) {
	case secureBootTrustMOK:
		return secureBootTrustMOK
	case secureBootTrustFirmwareDB:
		return secureBootTrustFirmwareDB
	default:
		return "<invalid>"
	}
}

func (a *App) chooseSecureBootTrustMode() (string, menuAction, error) {
	for {
		printHeader("Secure Boot Trust")
		printSection(
			"Trust Mode",
			infoRow{Label: "MOK on USB", Value: "Reuse the persistent MOK store, enroll EFI/debian-usb/mok/MOK.der once from the USB menu, then boot managed entries."},
			infoRow{Label: "Firmware db", Value: "Sign for the firmware Secure Boot db workflow and import secureboot/db.cer or combined secureboot/db.esl through firmware key management or UEFI UpdateVars."},
		)
		a.printMenu(
			menuEntry{Key: "1", Label: "MOK Enrollment on USB", Detail: "Recommended when you want to trust this USB via MOK.der."},
			menuEntry{Key: "2", Label: "Firmware db import", Detail: "Use firmware custom key enrollment instead of MokManager."},
			menuEntry{Key: "b", Label: "Go Back"},
			menuEntry{Key: "e", Label: "Exit"},
		)
		choice, err := a.promptChoice("Select the Secure Boot trust mode")
		if err != nil {
			return "", menuStay, err
		}
		switch choice {
		case "1":
			return secureBootTrustMOK, menuStay, nil
		case "2":
			return secureBootTrustFirmwareDB, menuStay, nil
		case "b":
			return "", menuBack, nil
		case "e":
			return "", menuExit, nil
		default:
			fmt.Println("Invalid selection.")
		}
	}
}

func (a *App) promptPersistenceSettings(label string, spec profileSpec, sourcePath, sourceRole string, inspection ISOInspection) (bool, string, int, string, ISOInspection, error) {
	if spec.Key == profileTails || !persistencePromptEligible(spec, inspection) {
		return false, persistenceModeNone, 0, sourcePath, inspection, nil
	}
	enabled, err := a.promptYesNo("Create a persistence partition for "+label, false)
	if err != nil || !enabled {
		return false, persistenceModeNone, 0, sourcePath, inspection, err
	}
	mode := persistenceModePlain
	if inspection.SupportsEncryptedPersistence || remasterEncryptedPersistenceEligible(spec, sourceRole, inspection) {
		var cancelled bool
		mode, cancelled, err = a.choosePersistenceModeForProfile(spec.Key)
		if err != nil || cancelled {
			return false, persistenceModeNone, 0, sourcePath, inspection, err
		}
	}
	size, err := a.promptPositiveInt(label+" persistence size in GiB", a.config.DefaultPersistenceSizeGiB)
	return true, mode, size, sourcePath, inspection, err
}

func remasterEncryptedPersistenceEligible(spec profileSpec, sourceRole string, inspection ISOInspection) bool {
	if sourceRole != multiOSSourceRolePrimary || !isLiveCapableMedia(inspection.MediaClass) {
		return false
	}
	switch spec.Key {
	case profileDebian, profileKaliLinux:
		return spec.SupportsPersistence
	default:
		return false
	}
}

func normalizePathInput(value string) string {
	trimmed := strings.TrimSpace(value)
	if len(trimmed) < 2 {
		return trimmed
	}
	if (trimmed[0] == '\'' && trimmed[len(trimmed)-1] == '\'') || (trimmed[0] == '"' && trimmed[len(trimmed)-1] == '"') {
		return strings.TrimSpace(trimmed[1 : len(trimmed)-1])
	}
	return trimmed
}

func resolveAbsolutePathInput(value string) (string, error) {
	normalized := normalizePathInput(value)
	if normalized == "" {
		return "", nil
	}
	return filepath.Abs(normalized)
}

func (a *App) chooseWriteMode(spec profileSpec, inspection ISOInspection, sourceRole string) (string, menuAction, error) {
	directDetail := "preserve upstream layout"
	managedDetail := "managed payload + GRUB"
	if sourceRole == multiOSSourceRoleNetinst || sourceRole == multiOSSourceRoleNetboot {
		directDetail = ""
		managedDetail = "managed ISO-store + custom GRUB + EFI"
	}
	if installerOnlyMedia(inspection) {
		directDetail = "preserve upstream whole-disk hybrid image"
		managedDetail = "raw ISO payload partition + custom GRUB + EFI"
	}
	for {
		printStepHeader(2, 4, spec.MenuLabel+" Write Mode")
		printSection("Inspection", inspectionSummaryRows(spec, inspection)...)
		printSection("Capabilities", inspectionCapabilityRows(spec, inspection)...)
		entries := []menuEntry{}
		if sourceRole == multiOSSourceRolePrimary {
			entries = append(entries, menuEntry{Key: "1", Label: "Write ISO As-Is", Detail: directDetail})
		}
		if inspection.ManagedSupported && spec.SupportsManaged {
			entries = append(entries, menuEntry{Key: "2", Label: "Build Managed USB", Detail: managedDetail})
		}
		entries = append(entries, menuEntry{Key: "b", Label: "Go Back"}, menuEntry{Key: "e", Label: "Exit"})
		a.printMenu(entries...)
		choice, err := a.promptChoice("Select the write mode")
		if err != nil {
			return "", menuStay, err
		}
		switch choice {
		case "1":
			if sourceRole != multiOSSourceRolePrimary {
				fmt.Println("Direct writes are only available for primary ISO sources.")
				continue
			}
			return writeModeDirect, menuStay, nil
		case "2":
			if inspection.ManagedSupported && spec.SupportsManaged {
				return writeModeManaged, menuStay, nil
			}
			fmt.Println("Managed USB mode is not available for this ISO/profile combination.")
		case "b":
			return "", menuBack, nil
		case "e":
			return "", menuExit, nil
		default:
			fmt.Println("Invalid selection.")
		}
	}
}

func (a *App) choosePersistenceMode() (string, bool, error) {
	return a.choosePersistenceModeForProfile("")
}

func (a *App) choosePersistenceModeForProfile(profile string) (string, bool, error) {
	if profile == profileTails {
		return persistenceModeNone, true, fmt.Errorf("Tails native Persistent Storage requires an official Tails USB on a dedicated device")
	}
	for {
		printHeader("Persistence Mode")
		printSection(
			"Modes",
			infoRow{Label: "Standard", Value: "ext4 persistence partition"},
			infoRow{Label: "Encrypted", Value: "LUKS container with ext4 persistence"},
		)
		a.printMenu(
			menuEntry{Key: "1", Label: "Standard"},
			menuEntry{Key: "2", Label: "Encrypted (LUKS)"},
			menuEntry{Key: "b", Label: "Back Without Persistence"},
		)
		choice, err := a.promptChoice("Select the persistence mode")
		if err != nil {
			return "", false, err
		}
		switch choice {
		case "1":
			return persistenceModePlain, false, nil
		case "2":
			return persistenceModeEncrypted, false, nil
		case "b":
			return "", true, nil
		default:
			fmt.Println("Invalid selection.")
		}
	}
}

func (a *App) chooseDevice() (Device, menuAction, error) {
	devices, err := a.backend.ListDevices()
	if err != nil {
		return Device{}, menuStay, err
	}
	candidates := eligibleDevices(devices)
	if len(candidates) == 0 {
		return Device{}, menuStay, errors.New("no eligible removable or external disks were found")
	}
	for {
		printStepHeader(3, 4, "Target Device")
		printSection(
			"Selection",
			infoRow{Label: "Eligible devices", Value: fmt.Sprintf("%d removable or external disk(s)", len(candidates))},
			infoRow{Label: "Filter", Value: "The current system disk and fixed internal disks are excluded automatically."},
		)
		a.printMenu(deviceMenuEntries(candidates)...)
		choice, err := a.promptChoice("Select the target device")
		if err != nil {
			return Device{}, menuStay, err
		}
		switch choice {
		case "b":
			return Device{}, menuBack, nil
		case "e":
			return Device{}, menuExit, nil
		default:
			index, err := strconv.Atoi(choice)
			if err != nil || index < 1 || index > len(candidates) {
				fmt.Println("Invalid selection.")
				continue
			}
			return candidates[index-1], menuStay, nil
		}
	}
}

func eligibleDevices(devices []Device) []Device {
	candidates := make([]Device, 0, len(devices))
	for _, device := range devices {
		if device.SystemDisk || !device.Removable {
			continue
		}
		candidates = append(candidates, device)
	}
	return candidates
}

func (a *App) printPlanSummary(plan CreatePlan, device Device, spec profileSpec, savedPlanID string, planState string) {
	printStepHeader(4, 4, "Review Plan")
	printSingleReview(plan, device, savedPlanID, planState, false)
}

func (a *App) printDetectedISOs() {
	isos, err := a.backend.ListLocalISOs()
	if err != nil || len(isos) == 0 {
		return
	}
	rows := make([]infoRow, 0, len(isos))
	for _, iso := range isos {
		rows = append(rows, infoRow{
			Label: iso.Name,
			Value: fmt.Sprintf("%s | %s | %s", iso.Source, iso.SizeHuman, iso.Path),
		})
	}
	printSection("Detected local ISO files", rows...)
}

func inspectionSummaryRows(spec profileSpec, inspection ISOInspection) []infoRow {
	rows := []infoRow{
		{Label: "Media class", Value: blankIfEmpty(inspection.MediaClass, "<unknown>")},
		{Label: "Firmware", Value: joinOrNone(inspection.Firmware)},
		{Label: "Managed rebuild", Value: mapBoolLabel(inspection.ManagedSupported && spec.SupportsManaged, "Available", "Unavailable")},
		{Label: "Persistence", Value: mapBoolLabel(inspection.SupportsPersistence && spec.SupportsPersistence, "Supported", "Unavailable")},
	}
	if inspection.ManagedPayloadLayout != "" {
		rows = append(rows, infoRow{Label: "Managed payload", Value: managedPayloadLayoutLabel(inspection.ManagedPayloadLayout)})
	}
	if inspection.SupportsEncryptedPersistence && spec.SupportsPersistence {
		rows = append(rows, infoRow{Label: "Encrypted persistence", Value: "Supported"})
	}
	if inspection.BestLiveTitle != "" {
		rows = append(rows, infoRow{Label: "Detected live entry", Value: inspection.BestLiveTitle})
	}
	if inspection.BestInstallerTitle != "" {
		rows = append(rows, infoRow{Label: "Detected installer entry", Value: inspection.BestInstallerTitle})
	}
	if len(inspection.TopLevelEntries) > 0 {
		rows = append(rows, infoRow{Label: "Top-level entry groups", Value: strings.Join(inspection.TopLevelEntries, ", ")})
	}
	return rows
}

func inspectionCapabilityRows(spec profileSpec, inspection ISOInspection) []infoRow {
	rows := []infoRow{}
	if inspection.ManagedSupported && spec.SupportsManaged {
		rows = append(rows, infoRow{Label: "Managed USB", Value: managedPayloadLayoutSummary(inspection.ManagedPayloadLayout)})
	} else {
		rows = append(rows, infoRow{Label: "Managed USB", Value: "Unavailable for this ISO/profile combination"})
	}
	if spec.SupportsPersistence && inspection.SupportsPersistence {
		rows = append(rows, infoRow{Label: "Persistence flow", Value: "Offered after managed mode selection"})
	}
	if spec.SupportsLiveOverrides && (inspection.MediaClass == "live" || inspection.MediaClass == "hybrid") {
		rows = append(rows, infoRow{Label: "Live overrides", Value: "Kernel, initrd, and menu label"})
	}
	if len(inspection.Warnings) > 0 {
		rows = append(rows, infoRow{Label: "Warnings", Value: strings.Join(inspection.Warnings, " | ")})
	}
	return rows
}

func deviceMenuEntries(devices []Device) []menuEntry {
	entries := make([]menuEntry, 0, len(devices)+2)
	for index, device := range devices {
		entries = append(entries, menuEntry{
			Key:    strconv.Itoa(index + 1),
			Label:  device.Path,
			Detail: deviceMenuDetail(device),
		})
	}
	entries = append(entries, menuEntry{Key: "b", Label: "Go Back"}, menuEntry{Key: "e", Label: "Exit"})
	return entries
}

func deviceMenuDetail(device Device) string {
	state := "not mounted"
	if device.Mounted {
		state = "mounted"
	}
	transport := device.Transport
	if strings.TrimSpace(transport) == "" {
		transport = "unknown transport"
	}
	identifier := strings.TrimSpace(device.PTUUID)
	if identifier == "" {
		identifier = strings.TrimSpace(device.UUID)
	}
	if identifier == "" {
		identifier = strings.TrimSpace(device.Serial)
	}
	return fmt.Sprintf("%s | %s | %s | id=%s | %s", device.SizeHuman, deviceFullName(device), transport, blankIfEmpty(identifier, "<none>"), state)
}
