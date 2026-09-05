package app

import "fmt"

func (a *App) updateUSBMenu() (menuAction, error) {
	execution, action, err := a.choosePlannedExecution("Update USB")
	if err != nil || action != menuStay {
		return action, err
	}
	return a.updatePlannedExecutionUSB(execution)
}

func (a *App) updatePlannedExecutionUSB(execution PlannedExecution) (menuAction, error) {
	switch execution.Kind {
	case plannedExecutionKindSingle:
		if execution.SinglePlan == nil {
			return menuStay, fmt.Errorf("planned execution %s is missing a single plan", execution.RunID)
		}
		if execution.SinglePlan.WriteMode != writeModeManaged {
			return menuStay, fmt.Errorf("Update USB only supports managed planned executions; saved write mode is %s", execution.SinglePlan.WriteMode)
		}
	case plannedExecutionKindMultiOS:
		if execution.MultiOSPlan == nil {
			return menuStay, fmt.Errorf("planned execution %s is missing a Multi-OS plan", execution.RunID)
		}
	default:
		return menuStay, fmt.Errorf("unsupported planned execution kind: %s", execution.Kind)
	}

	targetPath, action, err := a.targetDeviceForPlannedExecution(execution)
	if err != nil || action != menuStay {
		return action, err
	}

	printHeader("Review USB Update")
	printSection(
		"Update Scope",
		infoRow{Label: "Planned execution", Value: execution.RunID},
		infoRow{Label: "Target device", Value: targetPath},
		infoRow{Label: "Actions", Value: "Refresh managed GRUB, staged preseed trees, Secure Boot assets, and repo-managed boot files without repartitioning"},
	)
	printSection("Technical Specs", plannedExecutionTechnicalSpecRows(execution, Device{Path: targetPath})...)
	confirmed, err := a.promptYesNo("Proceed with USB update", false)
	if err != nil {
		return menuStay, err
	}
	if !confirmed {
		fmt.Println("USB update cancelled.")
		return menuStay, nil
	}

	switch execution.Kind {
	case plannedExecutionKindSingle:
		if err := a.backend.UpdateCreate(*execution.SinglePlan, targetPath, execution.SinglePlan.PersistenceSizeGiB); err != nil {
			return menuStay, err
		}
	case plannedExecutionKindMultiOS:
		if err := a.backend.UpdateMultiOSCreate(*execution.MultiOSPlan, targetPath); err != nil {
			return menuStay, err
		}
	}

	execution = a.recordSuccessfulPlannedExecution(execution, targetPath)
	printHeader("USB update complete")
	printSection("Result", infoRow{Label: "Planned execution", Value: execution.RunID}, infoRow{Label: "Device", Value: targetPath})
	return menuStay, nil
}
