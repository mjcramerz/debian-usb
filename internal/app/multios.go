package app

import (
	"fmt"
	"os"
	"path/filepath"
	"strconv"
	"strings"
)

func (a *App) handleCreateMultiOS() (menuAction, error) {
	request, action, err := a.collectMultiOSRequest()
	if err != nil {
		return menuStay, err
	}
	if action != menuStay {
		return action, nil
	}
	plan, err := buildMultiOSPlan(a.config, request)
	if err != nil {
		return menuStay, err
	}
	device, action, err := a.chooseDevice()
	if err != nil {
		return menuStay, err
	}
	if action != menuStay {
		return action, nil
	}
	savedExecution, created, err := a.backend.SaveOrReusePlannedExecution(newMultiOSPlannedExecution(plan, device.Path))
	if err != nil {
		return menuStay, err
	}
	a.printMultiOSPlanSummary(plan, device, savedExecution.RunID, reviewSaveState(created))
	confirmed, err := a.promptYesNo("Proceed with Multi-OS USB creation", false)
	if err != nil {
		return menuStay, err
	}
	if !confirmed {
		fmt.Printf("Multi-OS USB creation cancelled. Planned execution %s remains saved.\n", savedExecution.RunID)
		return menuStay, nil
	}
	if err := a.backend.ExecuteMultiOSCreate(plan, device.Path); err != nil {
		return menuStay, err
	}
	savedExecution = a.recordSuccessfulPlannedExecution(savedExecution, device.Path)
	printHeader("Multi-OS USB creation complete")
	printSection(
		"Result",
		infoRow{Label: "Profiles", Value: strings.Join(multiOSItemTitles(plan.Items), ", ")},
		infoRow{Label: "Device", Value: device.Path},
		infoRow{Label: "Write mode", Value: plan.WriteMode},
		infoRow{Label: "Planned execution", Value: savedExecution.RunID},
	)
	printBulletList(
		"Next steps",
		"Boot-test the USB on the target hardware and verify each OS submenu appears.",
		"Verify Debian/Kali/Kali Purple HTTP and USB preseed entries only after the configured network URLs or PRESEED_HOST_*_PATH trees are populated.",
	)
	return menuStay, nil
}

func (a *App) collectMultiOSRequest() (MultiOSRequest, menuAction, error) {
	for {
		selections, action, err := a.chooseMultiOSSources()
		if err != nil || action != menuStay {
			return MultiOSRequest{}, action, err
		}
		request := MultiOSRequest{
			UseCustomGrubMenu:           true,
			PreserveUpstreamGrubEntries: true,
			ProfileOfflinePreseedDirs:   make(map[string]string, len(selections)),
			Items:                       make([]MultiOSRequestItem, 0, len(selections)),
		}
		for index, selection := range selections {
			spec := profileSpecs[selection.Profile]
			item, action, err := a.collectMultiOSItem(spec, index+1, len(selections), selection.SourceRole)
			if err != nil || action != menuStay {
				return request, action, err
			}
			if createPreseedEligible(selection.Profile, selection.SourceRole, true, item.Inspection) {
				item.Preseed = true
			}
			request.Items = append(request.Items, item)
		}
		trustMode, action, err := a.chooseSecureBootTrustMode()
		if err != nil {
			return request, menuStay, err
		}
		if action != menuStay {
			return request, action, nil
		}
		request.SecureBootTrust = trustMode
		return request, menuStay, nil
	}
}

type multiOSSourceSelection struct {
	Profile    string
	SourceRole string
}

type multiOSSourceOption struct {
	Profile    string
	SourceRole string
	Label      string
	Detail     string
}

var multiOSSourceOptions = []multiOSSourceOption{
	{Profile: profileDebian, SourceRole: multiOSSourceRolePrimary, Label: "Debian Live", Detail: "Store the ISO under /debian-live and boot that exact ISO"},
	{Profile: profileDebian, SourceRole: multiOSSourceRoleNetinst, Label: "Debian Netinst", Detail: "Use separate hd-media kernel/initrd assets and one opaque ISO under /debian-netinst"},
	{Profile: profileDebian, SourceRole: multiOSSourceRoleNetboot, Label: "Debian Netboot", Detail: "Stage vmlinuz + initrd.gz into /debian-netboot"},
	{Profile: profileKaliLinux, SourceRole: multiOSSourceRolePrimary, Label: "Kali Live", Detail: "Store the ISO under /kali-live and boot that exact ISO"},
	{Profile: profileKaliLinux, SourceRole: multiOSSourceRoleNetinst, Label: "Kali Netinst", Detail: "Use separate hd-media kernel/initrd assets and one opaque ISO under /kali-netinst"},
	{Profile: profileKaliLinux, SourceRole: multiOSSourceRoleNetboot, Label: "Kali Netboot", Detail: "Stage vmlinuz + initrd.gz into /kali-netboot"},
	{Profile: profileKaliPurple, SourceRole: multiOSSourceRolePrimary, Label: "Kali Purple Installer", Detail: "Stage installer ISO + assets into /kali-purple-installer"},
	{Profile: profileTails, SourceRole: multiOSSourceRolePrimary, Label: "Tails Live", Detail: "Store the ISO under /tails-live and boot that exact ISO"},
}

func (a *App) chooseMultiOSSources() ([]multiOSSourceSelection, menuAction, error) {
	selected := make(map[string]bool, len(multiOSSourceOptions))
	for {
		printHeader("Create USB - Multi-OS")
		printSection(
			"Selection",
			infoRow{Label: "Mode", Value: "Managed Multi-OS ISO-store USB"},
			infoRow{Label: "Sources", Value: "Toggle any combination of live, netinst, netboot, and installer sources."},
			infoRow{Label: "Selected", Value: multiOSSelectedLabel(selected)},
		)
		entries := make([]menuEntry, 0, len(multiOSSourceOptions)+3)
		for index, option := range multiOSSourceOptions {
			state := "not selected"
			if selected[multiOSSourceSelectionKey(option.Profile, option.SourceRole)] {
				state = "selected"
			}
			entries = append(entries, menuEntry{
				Key:    strconv.Itoa(index + 1),
				Label:  option.Label,
				Detail: fmt.Sprintf("%s (%s)", option.Detail, state),
			})
		}
		continueKey := strconv.Itoa(len(multiOSSourceOptions) + 1)
		entries = append(entries,
			menuEntry{Key: continueKey, Label: "Continue"},
			menuEntry{Key: "b", Label: "Go Back"},
			menuEntry{Key: "e", Label: "Exit"},
		)
		printMenu(entries...)
		choice, err := a.promptChoice("Select an option")
		if err != nil {
			return nil, menuStay, err
		}
		switch choice {
		case continueKey:
			sources := selectedSourcesInOrder(selected)
			if len(sources) == 0 {
				fmt.Println("Select at least one boot source before continuing.")
				continue
			}
			return sources, menuStay, nil
		case "b":
			return nil, menuBack, nil
		case "e":
			return nil, menuExit, nil
		default:
			index, err := strconv.Atoi(choice)
			if err != nil || index < 1 || index > len(multiOSSourceOptions) {
				fmt.Println("Invalid selection.")
				continue
			}
			option := multiOSSourceOptions[index-1]
			key := multiOSSourceSelectionKey(option.Profile, option.SourceRole)
			selected[key] = !selected[key]
		}
	}
}

func (a *App) collectMultiOSItem(spec profileSpec, index int, total int, sourceRole string) (MultiOSRequestItem, menuAction, error) {
	item := MultiOSRequestItem{Profile: spec.Key, SourceRole: sourceRole}
	sourceTitle := spec.MultiOSLabel + " Primary Source"
	flow := []string{
		"Live-capable primary source.",
		"The selected ISO is copied unchanged to partition 2 and booted by its exact ISO path.",
	}
	if sourceRole == multiOSSourceRoleNetinst {
		sourceTitle = spec.MultiOSLabel + " Netinst Source"
		flow = []string{
			"Prepared source provides separate downloaded hd-media/vmlinuz and hd-media/initrd.gz.",
			"Exactly one opaque Netinst ISO is stored separately from Live ISOs on partition 2.",
			"Netinst entries are installer-only; no Live, loopback, squashfs, or persistence handling is applied.",
		}
	} else if sourceRole == multiOSSourceRoleNetboot {
		sourceTitle = spec.MultiOSLabel + " Netboot Source"
		flow = []string{
			"Netboot sources stage only the managed kernel/initrd bundle.",
			"No ISO payload is used for netboot entries.",
		}
	}
	printStepHeader(index, total, sourceTitle)
	a.printDetectedISOs()
	printSection("Source", infoRow{Label: "Order", Value: strings.Join(flow, " | ")})
	isoPath, action, err := a.resolveCreateSourcePath(spec, sourceRole)
	if err != nil {
		return item, menuStay, err
	}
	if action != menuStay {
		return item, action, nil
	}
	item.ISOPath = isoPath

	inspection, err := a.backend.InspectSource(spec.Key, item.ISOPath, sourceRole)
	if err != nil {
		return item, menuStay, err
	}
	if err := validateManagedSourceSelection(spec, sourceRole, inspection, item.ISOPath); err != nil {
		return item, menuStay, err
	}
	if sourceRole == multiOSSourceRolePrimary && isLiveCapableMedia(inspection.MediaClass) {
		var liveInitrdAction menuAction
		item.ISOPath, inspection, liveInitrdAction, err = a.promptAndApplyLiveInitrdOverlay(spec, item.ISOPath, inspection)
		if err != nil {
			return item, menuStay, err
		}
		if liveInitrdAction != menuStay {
			return item, liveInitrdAction, nil
		}
	}
	item.Inspection = inspection
	printSection("Inspection", inspectionSummaryRows(spec, inspection)...)
	printSection("Capabilities", inspectionCapabilityRows(spec, inspection)...)
	if !inspection.ManagedSupported || !spec.SupportsManaged {
		return item, menuStay, fmt.Errorf("%s ISO is not supported for Multi-OS managed mode: %s", spec.MultiOSLabel, item.ISOPath)
	}
	if sourceRole == multiOSSourceRoleNetinst {
		if !inspectionHasInstaller(inspection) {
			return item, menuStay, fmt.Errorf("%s netinst source must expose installer boot entries: %s", spec.MultiOSLabel, item.ISOPath)
		}
		return item, menuStay, nil
	}
	if sourceRole == multiOSSourceRolePrimary && isLiveCapableMedia(inspection.MediaClass) {
		liveToolGroups, action, err := a.promptLiveToolGroups(spec.Key, nil)
		if err != nil {
			return item, menuStay, err
		}
		if action != menuStay {
			return item, action, nil
		}
		item.LiveToolGroups = liveToolGroups
	}

	item.Persistence, item.PersistenceMode, item.PersistenceSizeGiB, item.ISOPath, item.Inspection, err = a.promptPersistenceSettings(spec.MultiOSLabel, spec, item.ISOPath, sourceRole, inspection)
	if err != nil {
		return item, menuStay, err
	}
	if isLiveCapableMedia(item.Inspection.MediaClass) {
		item.MenuLabel = spec.DefaultLiveMenuLabel
	}
	return item, menuStay, nil
}

func buildMultiOSPlan(config RuntimeConfig, request MultiOSRequest) (MultiOSPlan, error) {
	if len(request.Items) == 0 {
		return MultiOSPlan{}, fmt.Errorf("Multi-OS requires at least one boot source")
	}
	if !request.UseCustomGrubMenu {
		request.PreserveUpstreamGrubEntries = false
	}
	if request.ProfileOfflinePreseedDirs == nil {
		request.ProfileOfflinePreseedDirs = map[string]string{}
	}
	seenProfileSources := make(map[string]struct{}, len(request.Items))
	preseedProfileLabels := []string{}
	dataFSLabel, dataPartLabel := managedSharedDataLabels(config)
	plan := MultiOSPlan{
		SchemaVersion:               1,
		Title:                       "Multi-OS USB",
		WriteMode:                   writeModeMultiOS,
		SecureBootTrust:             normalizeSecureBootTrustMode(request.SecureBootTrust),
		ESPLabel:                    managedESPLabel(config),
		DataFSLabel:                 dataFSLabel,
		DataPartLabel:               dataPartLabel,
		UseCustomGrubMenu:           request.UseCustomGrubMenu,
		PreserveUpstreamGrubEntries: request.PreserveUpstreamGrubEntries,
		Items:                       make([]MultiOSPlanItem, 0, len(request.Items)),
		Notes: []string{
			"Multi-OS installs removable-path UEFI GRUB on a dedicated FAT32 ESP and stores each selected ISO in one shared ext4 ISO-store partition.",
			"Partition 1 is the ESP, partition 2 is the ISO store, and each persistence-enabled Live source receives its own partition starting at partition 3.",
			"Netinst sources direct-load their separate hd-media kernel/initrd and use a patched copied initrd that treats their own iso-scan/filename path as exact and fail-closed.",
			"Live sources keep their original ISO intact on partition 2 and boot only that exact ISO through findiso; extracted rootfs and package-list payloads are not merged into the ISO store.",
			"Curated Debian/Kali/Tails entries are rendered from the managed GRUB specs and can still add preserved upstream submenus when requested.",
		},
	}
	if plan.SecureBootTrust == "" {
		return MultiOSPlan{}, fmt.Errorf("invalid Secure Boot trust mode: %s", request.SecureBootTrust)
	}
	plan.Notes = append(plan.Notes, fmt.Sprintf("Secure Boot trust mode: %s", secureBootTrustModeLabel(plan.SecureBootTrust)))
	if plan.SecureBootTrust == secureBootTrustMOK {
		plan.Notes = append(plan.Notes, "Managed Secure Boot assets will reuse the persistent MOK store, stage EFI/debian-usb/mok/MOK.der for one-time enrollment, and stage combined Microsoft-compatible db.esl/db.auth for UEFI UpdateVars.")
	} else {
		plan.Notes = append(plan.Notes, "Managed Secure Boot assets will target firmware db trust and stage secureboot/db.cer plus combined Microsoft-compatible db.esl/db.auth for firmware import or UEFI UpdateVars.")
	}
	if request.UseCustomGrubMenu {
		plan.Notes = append(plan.Notes,
			"Custom GRUB mode groups entries under Debian, Kali, and Tails family menus and keeps GRUB at the menu until you choose an entry.",
			"Custom GRUB mode replaces the source ISO boot menus with curated GRUB entries derived from the ISO boot assets.",
			"Debian/Kali HTTP and USB preseed entries are emitted deterministically for installer-capable netinst, netboot, and Kali Purple sources.",
			"The writer copies the configured PRESEED_HOST_*_PATH trees to the directories implied by PRESEED_USB_*_FILE and warns when they are missing.",
		)
		if request.PreserveUpstreamGrubEntries {
			plan.Notes = append(plan.Notes, "Custom GRUB mode will also render preserved upstream-managed entry trees alongside the curated family menus.")
		}
	} else {
		plan.Notes = append(plan.Notes, "Standard managed GRUB mode preserves the normalized upstream entry trees for each selected payload while keeping GRUB on the menu until you choose an entry.")
	}
	for index, item := range request.Items {
		spec, ok := profileSpecs[item.Profile]
		if !ok {
			return MultiOSPlan{}, fmt.Errorf("unsupported profile: %s", item.Profile)
		}
		if !multiOSProfileSupported(item.Profile) {
			return MultiOSPlan{}, fmt.Errorf("%s is not supported in Multi-OS; use its separate single-OS workflow", spec.MenuLabel)
		}
		sourceRole, err := normalizeMultiOSSourceRole(item.SourceRole)
		if err != nil {
			return MultiOSPlan{}, err
		}
		profileSourceKey := item.Profile + "\x00" + sourceRole
		if _, exists := seenProfileSources[profileSourceKey]; exists {
			return MultiOSPlan{}, fmt.Errorf("profile/source role selected more than once: %s/%s", item.Profile, sourceRole)
		}
		seenProfileSources[profileSourceKey] = struct{}{}
		if !profileSupportsSourceRole(item.Profile, sourceRole) {
			return MultiOSPlan{}, fmt.Errorf("%s does not support source role %s", spec.MultiOSLabel, sourceRole)
		}
		if !spec.SupportsManaged {
			return MultiOSPlan{}, fmt.Errorf("%s does not support managed mode", spec.MenuLabel)
		}
		isoPath, err := validateManagedSourcePath(item.ISOPath, sourceRole)
		if err != nil {
			return MultiOSPlan{}, err
		}
		if item.Preseed && !request.UseCustomGrubMenu {
			return MultiOSPlan{}, fmt.Errorf("preseed menu entries require custom GRUB mode for %s", spec.MultiOSLabel)
		}
		inspection := item.Inspection
		if err := validateManagedSourceSelection(spec, sourceRole, inspection, isoPath); err != nil {
			return MultiOSPlan{}, err
		}
		if item.Preseed && !createPreseedEligible(item.Profile, sourceRole, request.UseCustomGrubMenu, inspection) {
			return MultiOSPlan{}, fmt.Errorf("preseed menu entries require an installer-capable managed source role for %s", spec.MultiOSLabel)
		}
		if !inspection.ManagedSupported {
			return MultiOSPlan{}, fmt.Errorf("managed USB mode is not available for this ISO: %s", isoPath)
		}
		if len(item.LiveToolGroups) > 0 && (sourceRole != multiOSSourceRolePrimary || !isLiveCapableMedia(inspection.MediaClass)) {
			return MultiOSPlan{}, fmt.Errorf("Live administration tool groups require a primary Live or Hybrid source for %s", spec.MultiOSLabel)
		}
		liveToolGroups, err := validateLiveToolGroupSelection(item.Profile, item.LiveToolGroups)
		if err != nil {
			return MultiOSPlan{}, err
		}
		preseed := createPreseedEligible(item.Profile, sourceRole, request.UseCustomGrubMenu, inspection)
		offlinePreseedSourceDir := ""
		if preseed {
			preseedProfileLabels = append(preseedProfileLabels, multiOSPlanItemTitle(spec, sourceRole))
		}
		if preseed && strings.TrimSpace(request.ProfileOfflinePreseedDirs[item.Profile]) != "" {
			var err error
			offlinePreseedSourceDir, err = resolveOptionalExistingDir(request.ProfileOfflinePreseedDirs[item.Profile])
			if err != nil {
				return MultiOSPlan{}, err
			}
			request.ProfileOfflinePreseedDirs[item.Profile] = offlinePreseedSourceDir
		}
		persistenceMode := strings.TrimSpace(item.PersistenceMode)
		if sourceRole == multiOSSourceRoleNetinst || sourceRole == multiOSSourceRoleNetboot {
			item.LiveToram = false
			item.Persistence = false
			item.PersistenceSizeGiB = 0
			item.KernelArgs = ""
			item.KernelPath = ""
			item.InitrdPath = ""
			item.MenuLabel = ""
			persistenceMode = persistenceModeNone
		}
		if item.Persistence && persistenceMode == persistenceModeNone {
			if item.Profile == profileTails {
				persistenceMode = persistenceModeEncrypted
			} else {
				persistenceMode = persistenceModePlain
			}
		}
		if !item.Persistence {
			persistenceMode = persistenceModeNone
		}
		switch persistenceMode {
		case persistenceModeNone, persistenceModePlain, persistenceModeEncrypted:
		default:
			return MultiOSPlan{}, fmt.Errorf("persistence mode must be plain or encrypted for %s", spec.MultiOSLabel)
		}
		if item.Profile == profileTails && persistenceMode == persistenceModePlain {
			return MultiOSPlan{}, fmt.Errorf("Tails persistence must be encrypted")
		}
		if item.Persistence && !spec.SupportsPersistence {
			return MultiOSPlan{}, fmt.Errorf("%s does not support persistence", spec.MenuLabel)
		}
		if item.Persistence && !inspection.SupportsPersistence {
			return MultiOSPlan{}, fmt.Errorf("selected ISO does not support persistence for %s: %s", spec.MultiOSLabel, isoPath)
		}
		if persistenceMode == persistenceModeEncrypted && !inspection.SupportsEncryptedPersistence {
			return MultiOSPlan{}, fmt.Errorf("encrypted persistence is not supported for %s: %s", spec.MultiOSLabel, isoPath)
		}
		if item.Persistence && item.PersistenceSizeGiB <= 0 {
			return MultiOSPlan{}, fmt.Errorf("persistence size must be positive for %s", spec.MultiOSLabel)
		}
		persistenceFSLabel, persistencePartLabel := multiOSPersistenceLabels(config, item.Profile)
		payloadFSLabel, payloadPartLabel := managedPayloadLabels(config, item.Profile, sourceRole, inspection.MediaClass)
		effectiveLayout := "shared-data"
		if effectiveLayout != "shared-data" {
			for labelName, label := range map[string]string{
				"payload filesystem label": payloadFSLabel,
				"payload partition label":  payloadPartLabel,
			} {
				if err := validateMultiOSLabel(labelName, label); err != nil {
					return MultiOSPlan{}, err
				}
			}
		} else {
			payloadFSLabel = ""
			payloadPartLabel = ""
		}
		if item.Persistence {
			for labelName, label := range map[string]string{
				"persistence filesystem label": persistenceFSLabel,
				"persistence partition label":  persistencePartLabel,
			} {
				if err := validateMultiOSLabel(labelName, label); err != nil {
					return MultiOSPlan{}, err
				}
			}
		} else {
			persistenceFSLabel = ""
			persistencePartLabel = ""
		}
		kernelArgs := strings.TrimSpace(item.KernelArgs)
		if kernelArgs != "" {
			kernelArgs = normalizeMultiOSKernelArgs(spec, kernelArgs, persistenceMode, persistenceFSLabel)
		}
		title := multiOSPlanItemTitle(spec, sourceRole)
		menuLabel := strings.TrimSpace(item.MenuLabel)
		if sourceRole == multiOSSourceRoleNetinst || sourceRole == multiOSSourceRoleNetboot {
			menuLabel = ""
		} else if menuLabel == "" && isLiveCapableMedia(inspection.MediaClass) {
			menuLabel = spec.DefaultLiveMenuLabel
		}
		plan.Items = append(plan.Items, MultiOSPlanItem{
			ID:         fmt.Sprintf("os%d", index+1),
			Profile:    item.Profile,
			SourceRole: sourceRole,
			Title:      title,
			ISOPath:    isoPath,
			MediaClass: inspection.MediaClass,
			Firmware:   append([]string{}, inspection.Firmware...),
			ManagedPayloadLayout: blankIfEmpty(
				effectiveLayout,
				"iso-store",
			),
			Preseed:                 preseed,
			LiveToram:               item.LiveToram,
			Persistence:             item.Persistence,
			PersistenceMode:         persistenceMode,
			PersistenceSizeGiB:      item.PersistenceSizeGiB,
			LiveToolGroups:          liveToolGroups,
			PersistenceFSLabel:      persistenceFSLabel,
			PersistencePartLabel:    persistencePartLabel,
			PayloadFSLabel:          payloadFSLabel,
			PayloadPartLabel:        payloadPartLabel,
			PayloadISOName:          plannedPayloadISOName(isoPath, sourceRole),
			OfflinePreseedSourceDir: offlinePreseedSourceDir,
			MenuLabel:               menuLabel,
			KernelArgs:              kernelArgs,
			KernelPath:              strings.TrimSpace(item.KernelPath),
			InitrdPath:              strings.TrimSpace(item.InitrdPath),
			TopLevelEntries:         append([]string{}, inspection.TopLevelEntries...),
			SupportsEncrypted:       inspection.SupportsEncryptedPersistence,
			BestLiveTitle:           inspection.BestLiveTitle,
			BestInstallerTitle:      inspection.BestInstallerTitle,
		})
		if item.LiveToolGroups != nil {
			if len(liveToolGroups) == 0 {
				plan.Notes = append(plan.Notes, fmt.Sprintf("%s keeps its upstream Live package set unchanged.", title))
			} else {
				plan.Notes = append(plan.Notes, fmt.Sprintf("%s will be remastered before device writes with Live tool groups: %s.", title, strings.Join(liveToolGroups, ", ")))
			}
		}
	}
	if request.UseCustomGrubMenu {
		if len(preseedProfileLabels) > 0 {
			plan.Notes = append(plan.Notes, fmt.Sprintf("Debian/Kali HTTP and USB preseed GRUB entries will be generated for: %s.", strings.Join(preseedProfileLabels, ", ")))
		} else {
			plan.Notes = append(plan.Notes, "No Debian/Kali installer-capable sources were selected for fixed preseed GRUB entries.")
		}
	}
	return plan, nil
}

func multiOSProfileSupported(profile string) bool {
	switch profile {
	case profileDebian, profileKaliLinux, profileKaliPurple, profileTails:
		return true
	default:
		return false
	}
}

func validateManagedSourcePath(value string, sourceRole string) (string, error) {
	isoPath, err := filepath.Abs(normalizePathInput(value))
	if err != nil {
		return "", err
	}
	info, err := os.Stat(isoPath)
	if err != nil {
		return "", fmt.Errorf("ISO path does not exist: %s", isoPath)
	}
	if sourceRole == multiOSSourceRolePrimary {
		if !info.Mode().IsRegular() {
			return "", fmt.Errorf("ISO path is not a regular file: %s", isoPath)
		}
		if info.Size() <= 0 {
			return "", fmt.Errorf("ISO file is empty: %s", isoPath)
		}
		if strings.ToLower(filepath.Ext(isoPath)) != ".iso" {
			return "", fmt.Errorf("expected an .iso file: %s", isoPath)
		}
		return isoPath, nil
	}
	if !info.IsDir() {
		if sourceRole == multiOSSourceRoleNetinst {
			return "", fmt.Errorf("Netinst installer source must be a prepared hd-media source directory: %s", isoPath)
		}
		return "", fmt.Errorf("%s source must be a prepared source directory: %s", multiOSSourceRoleSummary(sourceRole), isoPath)
	}
	if err := validatePreparedInstallerSourcePath(isoPath, sourceRole); err != nil {
		return "", err
	}
	return isoPath, nil
}

func validatePreparedInstallerSourcePath(sourcePath, sourceRole string) error {
	assetRoot := "netboot"
	if sourceRole == multiOSSourceRoleNetinst {
		assetRoot = "hd-media"
	}
	for _, name := range []string{"vmlinuz", "initrd.gz"} {
		assetPath := filepath.Join(sourcePath, assetRoot, name)
		info, err := os.Stat(assetPath)
		if err != nil || !info.Mode().IsRegular() {
			return fmt.Errorf("%s source is missing separate %s/%s: %s", sourceRole, assetRoot, name, sourcePath)
		}
		if info.Size() <= 0 {
			return fmt.Errorf("%s source contains an empty separate %s/%s: %s", sourceRole, assetRoot, name, sourcePath)
		}
	}
	if sourceRole != multiOSSourceRoleNetinst {
		return nil
	}
	payloadISOs, err := filepath.Glob(filepath.Join(sourcePath, "payload", "*.iso"))
	if err != nil {
		return fmt.Errorf("inspect netinst payload directory: %w", err)
	}
	if len(payloadISOs) != 1 {
		return fmt.Errorf("netinst source must contain exactly one separate payload/*.iso: %s", sourcePath)
	}
	info, err := os.Stat(payloadISOs[0])
	if err != nil || !info.Mode().IsRegular() {
		return fmt.Errorf("netinst payload ISO is not a regular file: %s", payloadISOs[0])
	}
	if info.Size() <= 0 {
		return fmt.Errorf("netinst payload ISO is empty: %s", payloadISOs[0])
	}
	return nil
}

func multiOSPersistenceLabels(config RuntimeConfig, profile string) (string, string) {
	return managedPersistenceLabels(config, profile)
}

func validateMultiOSLabel(labelName, label string) error {
	if label == "" {
		return fmt.Errorf("%s must not be empty", labelName)
	}
	if len(label) > 16 {
		return fmt.Errorf("%s %q is longer than 16 characters", labelName, label)
	}
	for _, value := range label {
		if value < 33 || value > 126 {
			return fmt.Errorf("%s %q must contain printable ASCII without whitespace", labelName, label)
		}
	}
	return nil
}

func normalizeMultiOSSourceRole(value string) (string, error) {
	sourceRole := strings.TrimSpace(value)
	if sourceRole == "" {
		sourceRole = multiOSSourceRolePrimary
	}
	switch sourceRole {
	case multiOSSourceRolePrimary, multiOSSourceRoleNetinst, multiOSSourceRoleNetboot:
		return sourceRole, nil
	default:
		return "", fmt.Errorf("unsupported Multi-OS source role: %s", sourceRole)
	}
}

func multiOSPlanItemTitle(spec profileSpec, sourceRole string) string {
	title := strings.TrimSpace(spec.MultiOSLabel)
	if title == "" {
		title = spec.MenuLabel
	}
	if sourceRole == multiOSSourceRoleNetinst {
		return strings.TrimSpace(title + " Netinst")
	}
	if sourceRole == multiOSSourceRoleNetboot {
		return strings.TrimSpace(title + " Netboot")
	}
	return title
}

func multiOSSourceRoleSummary(sourceRole string) string {
	switch sourceRole {
	case multiOSSourceRoleNetinst:
		return "Netinst installer"
	case multiOSSourceRoleNetboot:
		return "Netboot installer"
	default:
		return "Primary source media"
	}
}

func inspectionHasInstaller(inspection ISOInspection) bool {
	mediaClass := strings.TrimSpace(inspection.MediaClass)
	return mediaClass == "installer" || mediaClass == "hybrid" || strings.TrimSpace(inspection.BestInstallerTitle) != ""
}

func multiOSSourceSelectionKey(profile, sourceRole string) string {
	return profile + "\x00" + sourceRole
}

func selectedSourcesInOrder(selected map[string]bool) []multiOSSourceSelection {
	selections := make([]multiOSSourceSelection, 0, len(selected))
	for _, option := range multiOSSourceOptions {
		if !selected[multiOSSourceSelectionKey(option.Profile, option.SourceRole)] {
			continue
		}
		selections = append(selections, multiOSSourceSelection{
			Profile:    option.Profile,
			SourceRole: option.SourceRole,
		})
	}
	return selections
}

func multiOSSelectedLabel(selected map[string]bool) string {
	selections := selectedSourcesInOrder(selected)
	if len(selections) == 0 {
		return "<none>"
	}
	labels := make([]string, 0, len(selections))
	for _, selection := range selections {
		labels = append(labels, multiOSPlanItemTitle(profileSpecs[selection.Profile], selection.SourceRole))
	}
	return strings.Join(labels, ", ")
}

func multiOSItemTitles(items []MultiOSPlanItem) []string {
	titles := make([]string, 0, len(items))
	for _, item := range items {
		titles = append(titles, item.Title)
	}
	return titles
}

func normalizeMultiOSKernelArgs(spec profileSpec, kernelArgs, persistenceMode, persistenceLabel string) string {
	args := collapseWhitespace(kernelArgs)
	if spec.LiveBootFamily == "casper" {
		args = removeKernelArgsByExact(args, "ignore_uuid", "persistent", "nopersistent")
		args = removeKernelArgsByPrefix(args, "uuid=", "iso-scan/filename=")
		if persistenceMode == persistenceModePlain && spec.SupportsPersistence {
			args = mergeKernelArgs(args, "persistent")
		}
		args = mergeKernelArgs(args, "iso-scan/filename=${isofile}")
		return args
	}
	args = removeKernelArgsByExact(args, "ignore_uuid", "persistence", "nopersistence", "persistent=cryptsetup")
	args = removeKernelArgsByPrefix(
		args,
		"uuid=", "findiso=", "fromiso=", "iso-scan/filename=", "persistence-label=", "persistence-encryption=",
		"persistence-media=", "persistence-storage=", "persistence-method=", "union=",
	)
	args = mergeKernelArgs(args, "findiso=${isofile}")
	if spec.Key == profileTails {
		return args
	}
	if persistenceMode == persistenceModeEncrypted {
		if spec.Key == profileKaliLinux {
			args = mergeKernelArgs(args, "persistent=cryptsetup persistence-encryption=luks persistence persistence-media=removable-usb persistence-storage=filesystem union=overlay")
		} else {
			args = mergeKernelArgs(args, "persistence persistence-encryption=luks persistence-media=removable-usb persistence-storage=filesystem union=overlay")
		}
		if persistenceLabel != "" {
			args = mergeKernelArgs(args, "persistence-label="+persistenceLabel)
		}
	} else if persistenceMode == persistenceModePlain {
		label := persistenceLabel
		if label == "" {
			label = "persistence"
		}
		args = mergeKernelArgs(args, "persistence persistence-label="+label+" persistence-media=removable-usb persistence-storage=filesystem union=overlay")
	}
	return args
}

func (a *App) printMultiOSPlanSummary(plan MultiOSPlan, device Device, savedPlanID string, planState string) {
	printStepHeader(4, 4, "Review Multi-OS Plan")
	printSection("Technical Specs", multiOSTechnicalSpecRows(plan, device, savedPlanID, planState)...)
}
