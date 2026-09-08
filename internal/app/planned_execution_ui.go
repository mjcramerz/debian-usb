package app

import (
	"fmt"
	"path/filepath"
	"strconv"
	"strings"
)

func (a *App) handleRunPlannedExecution() (menuAction, error) {
	execution, action, err := a.choosePlannedExecution("Run Planned Execution")
	if err != nil || action != menuStay {
		return action, err
	}
	return a.executePlannedExecution(execution)
}

func (a *App) plannedExecutionsMenu() (menuAction, error) {
	for {
		execution, action, err := a.choosePlannedExecution("Planned Executions")
		if err != nil || action != menuStay {
			return action, err
		}
		printHeader("Planned Execution")
		printSection("Selected", plannedExecutionRows(execution)...)
		a.printMenu(
			menuEntry{Key: "1", Label: "Run this planned execution", Detail: "Rewrite a USB using the saved plan parameters."},
			menuEntry{Key: "2", Label: "Edit this planned execution", Detail: "Change stored source paths, target device, persistence, and override fields."},
			menuEntry{Key: "3", Label: "Delete this planned execution", Detail: "Remove the stored JSON plan from disk."},
			menuEntry{Key: "b", Label: "Go Back"},
			menuEntry{Key: "e", Label: "Exit"},
		)
		choice, err := a.promptChoice("Select an action")
		if err != nil {
			return menuStay, err
		}
		switch choice {
		case "1":
			return a.executePlannedExecution(execution)
		case "2":
			updated, action, err := a.editPlannedExecution(execution)
			if err != nil {
				return menuStay, err
			}
			if action == menuExit {
				return menuExit, nil
			}
			if updated {
				fmt.Printf("Updated planned execution %s.\n", execution.RunID)
			}
		case "3":
			deleted, err := a.deletePlannedExecution(execution)
			if err != nil {
				return menuStay, err
			}
			if deleted {
				fmt.Printf("Deleted planned execution %s.\n", execution.RunID)
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

func (a *App) choosePlannedExecution(title string) (PlannedExecution, menuAction, error) {
	executions, err := a.backend.ListPlannedExecutions()
	if err != nil {
		return PlannedExecution{}, menuStay, err
	}
	if len(executions) == 0 {
		printHeader(title)
		fmt.Println("No planned executions were found.")
		return PlannedExecution{}, menuBack, nil
	}
	for {
		printHeader(title)
		entries := make([]menuEntry, 0, len(executions)+2)
		for index, execution := range executions {
			entries = append(entries, menuEntry{Key: strconv.Itoa(index + 1), Label: execution.RunID, Detail: plannedExecutionDetail(execution)})
		}
		entries = append(entries, menuEntry{Key: "b", Label: "Go Back"}, menuEntry{Key: "e", Label: "Exit"})
		a.printMenu(entries...)
		choice, err := a.promptChoice("Select a planned execution")
		if err != nil {
			return PlannedExecution{}, menuStay, err
		}
		switch choice {
		case "b":
			return PlannedExecution{}, menuBack, nil
		case "e":
			return PlannedExecution{}, menuExit, nil
		default:
			index, err := strconv.Atoi(choice)
			if err != nil || index < 1 || index > len(executions) {
				fmt.Println("Invalid selection.")
				continue
			}
			return executions[index-1], menuStay, nil
		}
	}
}

func plannedExecutionDetail(execution PlannedExecution) string {
	target := strings.TrimSpace(execution.TargetDevicePath)
	if target == "" {
		target = "target selected at run time"
	}
	return fmt.Sprintf("%s | %s", execution.summary(), target)
}

func plannedExecutionRows(execution PlannedExecution) []infoRow {
	rows := []infoRow{
		{Label: "ID", Value: execution.RunID},
		{Label: "Kind", Value: execution.Kind},
		{Label: "Title", Value: execution.displayTitle()},
		{Label: "Target device", Value: blankIfEmpty(execution.TargetDevicePath, "selected at run time")},
		{Label: "Updated", Value: execution.UpdatedAt},
	}
	if strings.TrimSpace(execution.LastReviewedAt) != "" {
		rows = append(rows, infoRow{Label: "Last reviewed", Value: execution.LastReviewedAt})
	}
	if strings.TrimSpace(execution.LastExecutedAt) != "" {
		rows = append(rows, infoRow{Label: "Last executed", Value: execution.LastExecutedAt})
	}
	rows = append(rows, infoRow{Label: "Summary", Value: execution.summary()})
	return rows
}

func (a *App) executePlannedExecution(execution PlannedExecution) (menuAction, error) {
	targetPath, action, err := a.targetDeviceForPlannedExecution(execution)
	if err != nil || action != menuStay {
		return action, err
	}
	printHeader("Review Planned Execution")
	if err := a.reviewSavedPlan(&execution, targetPath, false); err != nil {
		return menuStay, err
	}
	confirmed, err := a.promptYesNo("Proceed with planned USB creation", false)
	if err != nil {
		return menuStay, err
	}
	if !confirmed {
		fmt.Println("Planned execution cancelled.")
		return menuStay, nil
	}
	switch execution.Kind {
	case plannedExecutionKindSingle:
		if execution.SinglePlan == nil {
			return menuStay, fmt.Errorf("planned execution %s is missing a single plan", execution.RunID)
		}
		if err := a.backend.ExecuteCreate(*execution.SinglePlan, targetPath, execution.SinglePlan.PersistenceSizeGiB); err != nil {
			return menuStay, err
		}
	case plannedExecutionKindMultiOS:
		if execution.MultiOSPlan == nil {
			return menuStay, fmt.Errorf("planned execution %s is missing a Multi-OS plan", execution.RunID)
		}
		if err := a.backend.ExecuteMultiOSCreate(*execution.MultiOSPlan, targetPath); err != nil {
			return menuStay, err
		}
	default:
		return menuStay, fmt.Errorf("unsupported planned execution kind: %s", execution.Kind)
	}
	execution = a.recordSuccessfulPlannedExecution(execution, targetPath)
	printHeader("Planned USB creation complete")
	printSection("Result", infoRow{Label: "Planned execution", Value: execution.RunID}, infoRow{Label: "Device", Value: targetPath})
	return menuStay, nil
}

func (a *App) targetDeviceForPlannedExecution(execution PlannedExecution) (string, menuAction, error) {
	if savedTarget := strings.TrimSpace(execution.TargetDevicePath); savedTarget != "" {
		useSaved, err := a.promptYesNo("Use saved target device "+savedTarget, false)
		if err != nil {
			return "", menuStay, err
		}
		if useSaved {
			if err := a.validateEligibleDevicePath(savedTarget); err != nil {
				fmt.Printf("Saved target device %s is not currently eligible. Choose another device.\n", savedTarget)
			} else {
				return savedTarget, menuStay, nil
			}
		}
	}
	device, action, err := a.chooseDevice()
	if err != nil || action != menuStay {
		return "", action, err
	}
	return device.Path, menuStay, nil
}

func (a *App) validateEligibleDevicePath(path string) error {
	devices, err := a.backend.ListDevices()
	if err != nil {
		return err
	}
	for _, device := range eligibleDevices(devices) {
		if device.Path == path {
			return nil
		}
	}
	return fmt.Errorf("saved target device is not currently eligible: %s", path)
}

func (a *App) deletePlannedExecution(execution PlannedExecution) (bool, error) {
	confirmed, err := a.promptYesNo("Delete planned execution "+execution.RunID, false)
	if err != nil {
		return false, err
	}
	if !confirmed {
		fmt.Println("Delete cancelled.")
		return false, nil
	}
	return true, a.backend.DeletePlannedExecution(execution.RunID)
}

func (a *App) editPlannedExecution(execution PlannedExecution) (bool, menuAction, error) {
	updated := execution
	changed := false
	for {
		printHeader("Edit Planned Execution")
		printSection("Current", plannedExecutionRows(updated)...)
		entries := []menuEntry{
			{Key: "1", Label: "Set target device path", Detail: "Edit or clear the saved target device path."},
		}
		if updated.Kind == plannedExecutionKindSingle {
			entries = append(entries, singlePlannedExecutionEditMenuEntries(updated)...)
		} else {
			entries = append(entries, multiOSPlannedExecutionEditMenuEntries(updated)...)
		}
		entries = append(entries,
			menuEntry{Key: "s", Label: "Save Changes"},
			menuEntry{Key: "b", Label: "Discard and Go Back"},
			menuEntry{Key: "e", Label: "Exit"},
		)
		a.printMenu(entries...)
		choice, err := a.promptChoice("Select an edit")
		if err != nil {
			return false, menuStay, err
		}
		switch choice {
		case "1":
			target, err := a.promptEditableOptionalString("Target device path", updated.TargetDevicePath)
			if err != nil {
				return false, menuStay, err
			}
			updated.TargetDevicePath = normalizePathInput(target)
			changed = true
		case "2", "3", "4", "5", "6", "7", "8":
			var didChange bool
			if updated.Kind == plannedExecutionKindSingle {
				didChange, err = a.editSinglePlannedExecutionField(&updated, choice)
			} else {
				didChange, err = a.editMultiOSPlannedExecutionField(&updated, choice)
			}
			if err != nil {
				return false, menuStay, err
			}
			changed = changed || didChange
		case "s":
			if !changed {
				fmt.Println("No changes made.")
				return false, menuStay, nil
			}
			if _, err := a.backend.SavePlannedExecution(updated); err != nil {
				return false, menuStay, err
			}
			return true, menuStay, nil
		case "b":
			return false, menuStay, nil
		case "e":
			return false, menuExit, nil
		default:
			fmt.Println("Invalid selection.")
		}
	}
}

func singlePlannedExecutionEditMenuEntries(execution PlannedExecution) []menuEntry {
	if execution.SinglePlan == nil {
		return nil
	}
	plan := *execution.SinglePlan
	entries := []menuEntry{
		{Key: "2", Label: "Set ISO path", Detail: plan.ISOPath},
		{Key: "3", Label: "Set write mode", Detail: plan.WriteMode},
		{Key: "4", Label: "Toggle live toram", Detail: onOffLabel(plan.LiveToram)},
		{Key: "5", Label: "Toggle persistence", Detail: onOffLabel(plan.Persistence)},
		{Key: "6", Label: "Set persistence mode and size", Detail: fmt.Sprintf("%s / %d GiB", blankIfEmpty(plan.PersistenceMode, "none"), plan.PersistenceSizeGiB)},
	}
	if plan.WriteMode == writeModeManaged {
		if splitInstallerProfile(plan.Profile, plan.SourceRole) {
			entries = append(entries, menuEntry{Key: "7", Label: "Edit Desktop/Server HD-MEDIA copy", Detail: hdMediaPreseedSummary(plan.HDMediaPreseedDirs)})
		} else if singleCreatePreseedEligible(plan.UseCustomGrubMenu, inspectionFromCreatePlan(plan)) {
			detail := onOffLabel(plan.Preseed)
			if plan.Preseed && strings.TrimSpace(plan.OfflinePreseedSourceDir) != "" {
				detail += " | offline source: " + plan.OfflinePreseedSourceDir
			}
			entries = append(entries, menuEntry{Key: "7", Label: "Toggle Netinst preseed entries", Detail: detail})
		}
	}
	if singlePlanSupportsLiveOverrides(plan) {
		entries = append(entries, menuEntry{Key: "8", Label: "Edit live overrides", Detail: "Kernel args, kernel path, initrd path, and menu label."})
	}
	return entries
}

func multiOSPlannedExecutionEditMenuEntries(execution PlannedExecution) []menuEntry {
	detail := "No Multi-OS plan"
	if execution.MultiOSPlan != nil {
		detail = fmt.Sprintf("%d payload(s)", len(execution.MultiOSPlan.Items))
	}
	return []menuEntry{
		{Key: "2", Label: "Edit payload item", Detail: detail},
	}
}

func (a *App) editSinglePlannedExecutionField(execution *PlannedExecution, choice string) (bool, error) {
	if execution.SinglePlan == nil {
		return false, fmt.Errorf("planned execution %s is missing a single plan", execution.RunID)
	}
	plan := *execution.SinglePlan
	req := createRequestFromPlan(plan)
	switch choice {
	case "2":
		preparation, action, err := a.collectSourceSelection(profileSpecs[req.Profile], req.SourceRole)
		if err != nil || action != menuStay {
			return false, err
		}
		if req.Preparation != nil && req.SourceRole == multiOSSourceRolePrimary {
			preparation.InitrdOverlayDir = req.Preparation.InitrdOverlayDir
		}
		req.Preparation = preparation
		req.ISOPath = preparation.ISO.Path
		req.Inspection, err = a.inspectSelectedSource(profileSpecs[req.Profile], req.SourceRole, preparation)
		if err != nil {
			return false, err
		}
	case "3":
		spec := profileSpecs[req.Profile]
		mode, action, err := a.chooseWriteMode(spec, req.Inspection, req.SourceRole)
		if err != nil {
			return false, err
		}
		if action != menuStay {
			return false, nil
		}
		req.WriteMode = mode
		if mode == writeModeDirect {
			req.Persistence = false
			req.PersistenceMode = persistenceModeNone
			req.PersistenceSizeGiB = 0
			req.OfflinePreseedSourceDir = ""
			req.HDMediaPreseedDirs = nil
			req.Preseed = false
			req.LiveToram = false
			req.KernelArgs = ""
			req.KernelPath = ""
			req.InitrdPath = ""
			req.LiveToolGroups = []string{}
		}
	case "4":
		if req.WriteMode != writeModeManaged || !isLiveCapableMedia(req.Inspection.MediaClass) {
			fmt.Println("Live toram only applies to managed live or hybrid media.")
			return false, nil
		}
		req.LiveToram = !req.LiveToram
	case "5":
		if req.WriteMode != writeModeManaged || !isLiveCapableMedia(req.Inspection.MediaClass) {
			fmt.Println("Persistence only applies to managed live or hybrid media.")
			return false, nil
		}
		spec := profileSpecs[req.Profile]
		if !spec.SupportsPersistence || !req.Inspection.SupportsPersistence {
			fmt.Println("Persistence is not supported for this profile/media combination.")
			return false, nil
		}
		req.Persistence = !req.Persistence
		if req.Persistence {
			req.PersistenceMode = persistenceModePlain
			if req.Profile == profileTails {
				req.PersistenceMode = persistenceModeEncrypted
			}
			req.PersistenceSizeGiB = positiveOrDefault(req.PersistenceSizeGiB, a.config.DefaultPersistenceSizeGiB)
		} else {
			req.PersistenceMode = persistenceModeNone
			req.PersistenceSizeGiB = 0
		}
	case "6":
		if !req.Persistence {
			fmt.Println("Enable persistence before setting persistence mode or size.")
			return false, nil
		}
		if req.Inspection.SupportsEncryptedPersistence || remasterEncryptedPersistenceEligible(profileSpecs[req.Profile], req.SourceRole, req.Inspection) {
			mode, cancelled, err := a.choosePersistenceModeForProfile(req.Profile)
			if err != nil {
				return false, err
			}
			if cancelled {
				return false, nil
			}
			req.PersistenceMode = mode
		} else if req.Profile == profileTails {
			fmt.Println("Tails Persistent Storage requires encrypted support in the live source.")
			return false, nil
		} else {
			req.PersistenceMode = persistenceModePlain
		}
		size, err := a.promptPositiveInt("Persistence size in GiB", positiveOrDefault(req.PersistenceSizeGiB, a.config.DefaultPersistenceSizeGiB))
		if err != nil {
			return false, err
		}
		req.PersistenceSizeGiB = size
	case "7":
		if splitInstallerProfile(req.Profile, req.SourceRole) {
			var err error
			req.HDMediaPreseedDirs, err = a.promptHDMediaPreseedDirs(req.Profile)
			if err != nil {
				return false, err
			}
			req.OfflinePreseedSourceDir = ""
			req.Preseed = true
			break
		}
		if !singleCreatePreseedEligible(req.UseCustomGrubMenu, req.Inspection) {
			fmt.Println("Preseed entries are only available for managed custom-GRUB Netinst payloads.")
			return false, nil
		}
		req.Preseed = !req.Preseed
		if req.Preseed {
			offlinePreseedSourceDir, err := a.promptOfflinePreseedSourceDir(profileLabel(req.Profile) + " Netinst")
			if err != nil {
				return false, err
			}
			req.OfflinePreseedSourceDir = offlinePreseedSourceDir
		} else {
			req.OfflinePreseedSourceDir = ""
		}
	case "8":
		if !singlePlanSupportsLiveOverrides(plan) {
			fmt.Println("Live overrides are not available for this saved plan.")
			return false, nil
		}
		kernelArgs, err := a.promptEditableOptionalString("Kernel boot arguments", req.KernelArgs)
		if err != nil {
			return false, err
		}
		req.KernelArgs = kernelArgs
		kernelPath, err := a.promptEditableOptionalString("Kernel path inside the ISO", req.KernelPath)
		if err != nil {
			return false, err
		}
		req.KernelPath = normalizePathInput(kernelPath)
		initrdPath, err := a.promptEditableOptionalString("Initrd path inside the ISO", req.InitrdPath)
		if err != nil {
			return false, err
		}
		req.InitrdPath = normalizePathInput(initrdPath)
		menuLabel, err := a.promptEditableOptionalString("Primary menu label", req.MenuLabel)
		if err != nil {
			return false, err
		}
		req.MenuLabel = menuLabel
	default:
		return false, nil
	}
	updatedPlan, err := buildCreatePlan(a.config, req)
	if err != nil {
		return false, err
	}
	updatedPlan.TargetDevice = plan.TargetDevice
	execution.SinglePlan = &updatedPlan
	execution.Title = updatedPlan.Title
	return true, nil
}

func createRequestFromPlan(plan CreatePlan) CreateRequest {
	return CreateRequest{
		Profile:                     plan.Profile,
		SourceRole:                  blankIfEmpty(plan.SourceRole, multiOSSourceRolePrimary),
		Preparation:                 cloneSourcePreparation(plan.Preparation),
		WriteMode:                   plan.WriteMode,
		ISOPath:                     plan.ISOPath,
		Inspection:                  inspectionFromCreatePlan(plan),
		SecureBootTrust:             normalizeSecureBootTrustMode(plan.SecureBootTrust),
		UseCustomGrubMenu:           plan.UseCustomGrubMenu,
		PreserveUpstreamGrubEntries: plan.PreserveUpstreamGrubEntries,
		Preseed:                     plan.Preseed,
		LiveToram:                   plan.LiveToram,
		Persistence:                 plan.Persistence,
		PersistenceMode:             plan.PersistenceMode,
		PersistenceSizeGiB:          plan.PersistenceSizeGiB,
		LiveToolGroups:              append([]string{}, plan.LiveToolGroups...),
		OfflinePreseedSourceDir:     plan.OfflinePreseedSourceDir,
		HDMediaPreseedDirs:          cloneHDMediaPreseedDirs(plan.HDMediaPreseedDirs),
		MenuLabel:                   plan.MenuLabel,
		KernelArgs:                  plan.KernelArgs,
		KernelPath:                  plan.KernelPath,
		InitrdPath:                  plan.InitrdPath,
	}
}

func inspectionFromCreatePlan(plan CreatePlan) ISOInspection {
	return ISOInspection{
		ISOPath:                      plan.ISOPath,
		MediaClass:                   plan.MediaClass,
		Firmware:                     append([]string{}, plan.Firmware...),
		ManagedSupported:             plan.ManagedSupported,
		SupportsPersistence:          plan.SupportsPersistence,
		SupportsEncryptedPersistence: plan.SupportsEncrypted,
		ManagedPayloadLayout:         plan.ManagedPayloadLayout,
		TopLevelEntries:              append([]string{}, plan.TopLevelEntries...),
	}
}

func (a *App) editMultiOSPlannedExecutionField(execution *PlannedExecution, choice string) (bool, error) {
	if choice != "2" {
		return false, nil
	}
	if execution.MultiOSPlan == nil {
		return false, fmt.Errorf("planned execution %s is missing a Multi-OS plan", execution.RunID)
	}
	index, ok, err := a.chooseMultiOSPlanItem(*execution.MultiOSPlan)
	if err != nil || !ok {
		return false, err
	}
	request := multiOSRequestFromPlan(*execution.MultiOSPlan)
	if index < 0 || index >= len(request.Items) {
		return false, fmt.Errorf("invalid Multi-OS item index: %d", index)
	}
	changed, err := a.editMultiOSRequestItem(&request, index)
	if err != nil || !changed {
		return changed, err
	}
	updatedPlan, err := buildMultiOSPlan(a.config, request)
	if err != nil {
		return false, err
	}
	execution.MultiOSPlan = &updatedPlan
	execution.Title = updatedPlan.Title
	return true, nil
}

func (a *App) chooseMultiOSPlanItem(plan MultiOSPlan) (int, bool, error) {
	for {
		printHeader("Multi-OS Payload Items")
		entries := make([]menuEntry, 0, len(plan.Items)+1)
		for index, item := range plan.Items {
			entries = append(entries, menuEntry{
				Key:    strconv.Itoa(index + 1),
				Label:  item.Title,
				Detail: fmt.Sprintf("%s | %s", item.SourceRole, filepath.Base(item.ISOPath)),
			})
		}
		entries = append(entries, menuEntry{Key: "b", Label: "Go Back"})
		a.printMenu(entries...)
		choice, err := a.promptChoice("Select a payload item")
		if err != nil {
			return 0, false, err
		}
		if choice == "b" || choice == "back" {
			return 0, false, nil
		}
		index, err := strconv.Atoi(choice)
		if err != nil || index < 1 || index > len(plan.Items) {
			fmt.Println("Invalid selection.")
			continue
		}
		return index - 1, true, nil
	}
}

func (a *App) editMultiOSRequestItem(request *MultiOSRequest, index int) (bool, error) {
	if request == nil {
		return false, fmt.Errorf("missing Multi-OS request")
	}
	if index < 0 || index >= len(request.Items) {
		return false, fmt.Errorf("invalid Multi-OS item index: %d", index)
	}
	if request.ProfileOfflinePreseedDirs == nil {
		request.ProfileOfflinePreseedDirs = map[string]string{}
	}
	item := &request.Items[index]
	for {
		printHeader("Edit Multi-OS Payload")
		printSection(
			"Current",
			infoRow{Label: "Profile", Value: profileLabel(item.Profile)},
			infoRow{Label: "Source role", Value: blankIfEmpty(item.SourceRole, multiOSSourceRolePrimary)},
			infoRow{Label: "ISO", Value: item.ISOPath},
			infoRow{Label: "Media class", Value: item.Inspection.MediaClass},
			infoRow{Label: "Persistence", Value: onOffLabel(item.Persistence)},
			infoRow{Label: "Preseed entries", Value: mapBoolLabel(item.Preseed, "Enabled", "Disabled")},
			infoRow{Label: "Offline preseed source", Value: displayValueOrNone(request.ProfileOfflinePreseedDirs[item.Profile])},
			infoRow{Label: "Desktop/Server HD-MEDIA", Value: hdMediaPreseedSummary(item.HDMediaPreseedDirs)},
		)
		entries := []menuEntry{
			{Key: "1", Label: "Set ISO path"},
			{Key: "2", Label: "Toggle live toram"},
			{Key: "3", Label: "Toggle persistence"},
			{Key: "4", Label: "Set persistence mode and size"},
			{Key: "5", Label: "Set offline preseed source directory"},
		}
		if item.SourceRole == multiOSSourceRoleNetinst || item.SourceRole == multiOSSourceRoleNetboot {
			entries = append(entries, menuEntry{Key: "6", Label: "Toggle Installer Preseed Entries"})
		} else if multiOSRequestItemSupportsLiveOverrides(*item) {
			entries = append(entries, menuEntry{Key: "6", Label: "Edit live overrides"})
		}
		entries = append(entries, menuEntry{Key: "b", Label: "Go Back"})
		a.printMenu(entries...)
		choice, err := a.promptChoice("Select an edit")
		if err != nil {
			return false, err
		}
		switch choice {
		case "1":
			preparation, action, err := a.collectSourceSelection(profileSpecs[item.Profile], item.SourceRole)
			if err != nil || action != menuStay {
				return false, err
			}
			if item.Preparation != nil && item.SourceRole == multiOSSourceRolePrimary {
				preparation.InitrdOverlayDir = item.Preparation.InitrdOverlayDir
			}
			item.Preparation = preparation
			item.ISOPath = preparation.ISO.Path
			item.Inspection, err = a.inspectSelectedSource(profileSpecs[item.Profile], item.SourceRole, preparation)
			if err != nil {
				return false, err
			}
			return true, nil
		case "2":
			if item.SourceRole == multiOSSourceRoleNetinst || item.SourceRole == multiOSSourceRoleNetboot || !isLiveCapableMedia(item.Inspection.MediaClass) {
				fmt.Println("Live toram only applies to primary live or hybrid media.")
				return false, nil
			}
			item.LiveToram = !item.LiveToram
			return true, nil
		case "3":
			if item.SourceRole == multiOSSourceRoleNetinst || item.SourceRole == multiOSSourceRoleNetboot || !isLiveCapableMedia(item.Inspection.MediaClass) {
				fmt.Println("Persistence only applies to primary live or hybrid media.")
				return false, nil
			}
			spec := profileSpecs[item.Profile]
			if !spec.SupportsPersistence || !item.Inspection.SupportsPersistence {
				fmt.Println("Persistence is not supported for this profile/media combination.")
				return false, nil
			}
			item.Persistence = !item.Persistence
			if item.Persistence {
				item.PersistenceMode = persistenceModePlain
				if item.Profile == profileTails {
					item.PersistenceMode = persistenceModeEncrypted
				}
				item.PersistenceSizeGiB = positiveOrDefault(item.PersistenceSizeGiB, a.config.DefaultPersistenceSizeGiB)
			} else {
				item.PersistenceMode = persistenceModeNone
				item.PersistenceSizeGiB = 0
			}
			return true, nil
		case "4":
			if !item.Persistence {
				fmt.Println("Enable persistence before setting persistence mode or size.")
				return false, nil
			}
			if item.Inspection.SupportsEncryptedPersistence || remasterEncryptedPersistenceEligible(profileSpecs[item.Profile], item.SourceRole, item.Inspection) {
				mode, cancelled, err := a.choosePersistenceModeForProfile(item.Profile)
				if err != nil {
					return false, err
				}
				if cancelled {
					return false, nil
				}
				item.PersistenceMode = mode
			} else if item.Profile == profileTails {
				fmt.Println("Tails Persistent Storage requires encrypted support in the live source.")
				return false, nil
			} else {
				item.PersistenceMode = persistenceModePlain
			}
			size, err := a.promptPositiveInt("Persistence size in GiB", positiveOrDefault(item.PersistenceSizeGiB, a.config.DefaultPersistenceSizeGiB))
			if err != nil {
				return false, err
			}
			item.PersistenceSizeGiB = size
			return true, nil
		case "5":
			if splitInstallerProfile(item.Profile, item.SourceRole) {
				selections, err := a.promptHDMediaPreseedDirs(item.Profile)
				if err != nil {
					return false, err
				}
				for other := range request.Items {
					if request.Items[other].Profile == item.Profile && splitInstallerProfile(item.Profile, request.Items[other].SourceRole) {
						request.Items[other].HDMediaPreseedDirs = cloneHDMediaPreseedDirs(selections)
						request.Items[other].Preseed = true
					}
				}
				delete(request.ProfileOfflinePreseedDirs, item.Profile)
				return true, nil
			}
			if (item.SourceRole != multiOSSourceRoleNetinst && item.SourceRole != multiOSSourceRoleNetboot) || !item.Preseed {
				fmt.Println("Offline preseed content applies only to Netinst or Netboot payloads with preseed entries enabled.")
				return false, nil
			}
			offlinePreseedSourceDir, err := a.promptEditableOptionalString("Offline preseed source directory", request.ProfileOfflinePreseedDirs[item.Profile])
			if err != nil {
				return false, err
			}
			request.ProfileOfflinePreseedDirs[item.Profile], err = resolveOptionalExistingDir(offlinePreseedSourceDir)
			if err != nil {
				return false, err
			}
			return true, nil
		case "6":
			if splitInstallerProfile(item.Profile, item.SourceRole) {
				fmt.Println("Desktop/Server transport menus are always available. Use option 5 to change HD-MEDIA copying.")
				return false, nil
			}
			if item.SourceRole == multiOSSourceRoleNetinst || item.SourceRole == multiOSSourceRoleNetboot {
				item.Preseed = !item.Preseed
				if !item.Preseed {
					delete(request.ProfileOfflinePreseedDirs, item.Profile)
				}
				return true, nil
			}
			if !multiOSRequestItemSupportsLiveOverrides(*item) {
				fmt.Println("Live overrides are not available for this saved payload.")
				return false, nil
			}
			kernelArgs, err := a.promptEditableOptionalString("Kernel boot arguments", item.KernelArgs)
			if err != nil {
				return false, err
			}
			item.KernelArgs = kernelArgs
			kernelPath, err := a.promptEditableOptionalString("Kernel path inside the ISO", item.KernelPath)
			if err != nil {
				return false, err
			}
			item.KernelPath = normalizePathInput(kernelPath)
			initrdPath, err := a.promptEditableOptionalString("Initrd path inside the ISO", item.InitrdPath)
			if err != nil {
				return false, err
			}
			item.InitrdPath = normalizePathInput(initrdPath)
			menuLabel, err := a.promptEditableOptionalString("Primary menu label", item.MenuLabel)
			if err != nil {
				return false, err
			}
			item.MenuLabel = menuLabel
			return true, nil
		case "b":
			return false, nil
		default:
			fmt.Println("Invalid selection.")
		}
	}
}

func multiOSRequestFromPlan(plan MultiOSPlan) MultiOSRequest {
	request := MultiOSRequest{
		SecureBootTrust:             normalizeSecureBootTrustMode(plan.SecureBootTrust),
		UseCustomGrubMenu:           plan.UseCustomGrubMenu,
		PreserveUpstreamGrubEntries: plan.PreserveUpstreamGrubEntries,
		ProfileOfflinePreseedDirs:   make(map[string]string, len(plan.Items)),
		Items:                       make([]MultiOSRequestItem, 0, len(plan.Items)),
	}
	for _, item := range plan.Items {
		if item.OfflinePreseedSourceDir != "" {
			request.ProfileOfflinePreseedDirs[item.Profile] = item.OfflinePreseedSourceDir
		}
		request.Items = append(request.Items, MultiOSRequestItem{
			Profile:            item.Profile,
			Preparation:        cloneSourcePreparation(item.Preparation),
			HDMediaPreseedDirs: cloneHDMediaPreseedDirs(item.HDMediaPreseedDirs),
			SourceRole:         blankIfEmpty(item.SourceRole, multiOSSourceRolePrimary),
			ISOPath:            item.ISOPath,
			Inspection:         inspectionFromMultiOSPlanItem(item),
			Preseed:            item.Preseed,
			LiveToram:          item.LiveToram,
			Persistence:        item.Persistence,
			PersistenceMode:    item.PersistenceMode,
			PersistenceSizeGiB: item.PersistenceSizeGiB,
			LiveToolGroups:     append([]string{}, item.LiveToolGroups...),
			MenuLabel:          item.MenuLabel,
			KernelArgs:         item.KernelArgs,
			KernelPath:         item.KernelPath,
			InitrdPath:         item.InitrdPath,
		})
	}
	return request
}

func inspectionFromMultiOSPlanItem(item MultiOSPlanItem) ISOInspection {
	return ISOInspection{
		ISOPath:                      item.ISOPath,
		MediaClass:                   item.MediaClass,
		Firmware:                     append([]string{}, item.Firmware...),
		ManagedSupported:             true,
		SupportsPersistence:          profileSpecs[item.Profile].SupportsPersistence && isLiveCapableMedia(item.MediaClass),
		SupportsEncryptedPersistence: item.SupportsEncrypted,
		ManagedPayloadLayout:         item.ManagedPayloadLayout,
		TopLevelEntries:              append([]string{}, item.TopLevelEntries...),
		BestLiveTitle:                item.BestLiveTitle,
		BestInstallerTitle:           item.BestInstallerTitle,
	}
}

func isLiveCapableMedia(mediaClass string) bool {
	return mediaClass == "live" || mediaClass == "hybrid"
}

func positiveOrDefault(value, fallback int) int {
	if value > 0 {
		return value
	}
	return fallback
}

func singlePlanSupportsLiveOverrides(plan CreatePlan) bool {
	spec, ok := profileSpecs[plan.Profile]
	if !ok {
		return false
	}
	return spec.SupportsLiveOverrides && isLiveCapableMedia(plan.MediaClass)
}

func multiOSRequestItemSupportsLiveOverrides(item MultiOSRequestItem) bool {
	spec, ok := profileSpecs[item.Profile]
	if !ok {
		return false
	}
	return item.SourceRole != multiOSSourceRoleNetinst && item.SourceRole != multiOSSourceRoleNetboot && spec.SupportsLiveOverrides && isLiveCapableMedia(item.Inspection.MediaClass)
}
