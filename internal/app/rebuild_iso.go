package app

import (
	"fmt"
	"path/filepath"
	"strings"
)

const defaultRebuildInstallerISOOutputDir = "/data/downloads/debian-usb/iso/rebuild"

func (a *App) rebuildInstallerISOMenu() (menuAction, error) {
	for {
		printHeader("Rebuild Installer ISO")
		printSection(
			"Targets",
			infoRow{Label: "Debian", Value: "Implemented"},
			infoRow{Label: "Ubuntu", Value: "Placeholder"},
			infoRow{Label: "Kali", Value: "Placeholder"},
		)
		printMenu(
			menuEntry{Key: "1", Label: "Debian", Detail: "existing installer ISO"},
			menuEntry{Key: "2", Label: "Ubuntu", Detail: "placeholder"},
			menuEntry{Key: "3", Label: "Kali", Detail: "placeholder"},
			menuEntry{Key: "b", Label: "Go Back"},
			menuEntry{Key: "e", Label: "Exit"},
		)
		choice, err := a.promptChoice("Select a rebuild target")
		if err != nil {
			return menuStay, err
		}
		switch choice {
		case "1":
			action, err := a.handleRebuildInstallerISODebian()
			if err != nil {
				return menuStay, err
			}
			if action == menuExit {
				return menuExit, nil
			}
		case "2":
			printHeader("Rebuild Installer ISO - Ubuntu")
			printSection("Status", infoRow{Label: "Implementation", Value: "Not implemented yet in this rollout."})
		case "3":
			printHeader("Rebuild Installer ISO - Kali")
			printSection("Status", infoRow{Label: "Implementation", Value: "Not implemented yet in this rollout."})
		case "b":
			return menuStay, nil
		case "e":
			return menuExit, nil
		default:
			fmt.Println("Invalid selection.")
		}
	}
}

func (a *App) handleRebuildInstallerISODebian() (menuAction, error) {
	printHeader("Rebuild Installer ISO - Debian")
	printSection(
		"Source",
		infoRow{Label: "Accepted media", Value: "Debian live installer ISO or netinst ISO"},
		infoRow{Label: "Backend", Value: "d-i kernel/udeb rebuild + live root remaster"},
	)
	a.printDetectedISOs()

	sourceISOPath, err := a.promptRequiredString("Enter the absolute path to the local installer ISO", "")
	if err != nil {
		return menuStay, err
	}
	resolvedISOPath, err := resolveAbsolutePathInput(sourceISOPath)
	if err != nil {
		return menuStay, err
	}
	inspection, err := a.backend.InspectDebianRebuildISO(resolvedISOPath)
	if err != nil {
		return menuStay, err
	}

	for {
		printHeader("Rebuild Installer ISO - Debian")
		printSection("Source", rebuildInstallerInspectionRows(inspection)...)
		printMenu(
			menuEntry{Key: "1", Label: "D-I", Detail: "installer kernel + udebs"},
			menuEntry{Key: "2", Label: "Live Host", Detail: "live rootfs kernel + debs"},
			menuEntry{Key: "b", Label: "Back"},
			menuEntry{Key: "e", Label: "Cancel"},
		)
		if len(inspection.Warnings) > 0 {
			printBulletList("Warnings", inspection.Warnings...)
		}
		choice, err := a.promptChoice("Select a rebuild scope")
		if err != nil {
			return menuStay, err
		}
		switch choice {
		case "1":
			action, err := a.rebuildInstallerDIDebianMenu(inspection)
			if err != nil {
				return menuStay, err
			}
			if action == menuExit {
				return menuExit, nil
			}
		case "2":
			action, err := a.rebuildInstallerLiveHostDebianMenu(inspection)
			if err != nil {
				return menuStay, err
			}
			if action == menuExit {
				return menuExit, nil
			}
		case "b", "e":
			return menuStay, nil
		default:
			fmt.Println("Invalid selection.")
		}
	}
}

func (a *App) rebuildInstallerDIDebianMenu(inspection RebuildInstallerISOInspection) (menuAction, error) {
	hostSupport, hostSupportErr := a.inspectInstallerKernelHostSupport(inspection)
	for {
		printHeader("Rebuild Installer ISO - Debian - D-I")
		printSection(
			"Installer",
			infoRow{Label: "Kernel", Value: blankIfEmpty(inspection.InstallerKernelVersion, "<undetected>")},
			infoRow{Label: "Initrd", Value: blankIfEmpty(inspection.InstallerInitrdPath, "<undetected>")},
			infoRow{Label: "Architecture", Value: blankIfEmpty(inspection.Architecture, "<undetected>")},
		)
		if hostSupportErr != nil {
			printBulletList("Host kernel warnings", hostSupportErr.Error())
		} else {
			printSection("Host Kernel Tree", rebuildInstallerHostKernelRows(hostSupport)...)
		}
		printMenu(
			menuEntry{Key: "1", Label: "Add Kernel Modules", Detail: "kernel-wedge + rebuilt kernel udebs"},
			menuEntry{Key: "2", Label: "Add Udeb Packages", Detail: "source-package -> udeb rebuild"},
			menuEntry{Key: "3", Label: "Update Kernel", Detail: "replace installer kernel ABI"},
			menuEntry{Key: "b", Label: "Back"},
			menuEntry{Key: "e", Label: "Cancel"},
		)
		choice, err := a.promptChoice("Select a Debian Installer action")
		if err != nil {
			return menuStay, err
		}
		switch choice {
		case "1":
			modules, err := a.promptValidatedInstallerKernelModules(inspection, hostSupport)
			if err != nil {
				return menuStay, err
			}
			plan := newRebuildInstallerISOPlan(inspection, rebuildInstallerISOScopeDI, rebuildInstallerISOActionAddKernelModules)
			plan.InstallerKernelModules = modules
			return a.executeRebuildInstallerISOPlan(inspection, plan)
		case "2":
			printSection(
				"Source Package Flow",
				infoRow{Label: "Resolve", Value: "APT metadata is used to map each binary package to its Debian source package"},
				infoRow{Label: "Fetch", Value: "The backend runs apt source and apt-get build-dep for each selected package"},
				infoRow{Label: "Build", Value: "Resulting .udebs are rebuilt, staged, and exported into a localudeb repo"},
			)
			packagesInput, err := a.promptRequiredString("Udeb source package names to include", "")
			if err != nil {
				return menuStay, err
			}
			plan := newRebuildInstallerISOPlan(inspection, rebuildInstallerISOScopeDI, rebuildInstallerISOActionAddUdebPackages)
			plan.InstallerUdebPackages = parseDelimitedValues(packagesInput)
			return a.executeRebuildInstallerISOPlan(inspection, plan)
		case "3":
			kernelVersion, err := a.promptRequiredString("Target installer kernel version", blankIfEmpty(inspection.InstallerKernelVersion, "6.19.10+deb13-amd64"))
			if err != nil {
				return menuStay, err
			}
			plan := newRebuildInstallerISOPlan(inspection, rebuildInstallerISOScopeDI, rebuildInstallerISOActionUpdateKernel)
			plan.TargetKernelVersion = collapseWhitespace(kernelVersion)
			return a.executeRebuildInstallerISOPlan(inspection, plan)
		case "b", "e":
			return menuStay, nil
		default:
			fmt.Println("Invalid selection.")
		}
	}
}

func ansiGreen(value string) string {
	return "\x1b[32m" + value + "\x1b[0m"
}

func (a *App) inspectInstallerKernelHostSupport(inspection RebuildInstallerISOInspection) (BuildISOKernelInspectResult, error) {
	if strings.TrimSpace(inspection.InstallerKernelVersion) == "" {
		return BuildISOKernelInspectResult{}, fmt.Errorf("installer kernel version was not detected from the selected source initrd")
	}
	return a.backend.InspectBuildISOKernelEvidence(BuildISOKernelInspectRequest{
		KernelVersion:     inspection.InstallerKernelVersion,
		DownloadIfMissing: false,
	})
}

func rebuildInstallerHostKernelRows(result BuildISOKernelInspectResult) []infoRow {
	status := "Not detected in common host module paths"
	moduleTree := "<none>"
	moduleCount := "<none>"
	if result.ModuleTreeDir != "" {
		status = ansiGreen("Detected on host")
		moduleTree = result.ModuleTreeDir
		moduleCount = fmt.Sprintf("%d", result.ModuleCount)
	}
	return []infoRow{
		{Label: "Status", Value: status},
		{Label: "Kernel version", Value: blankIfEmpty(result.KernelVersion, "<undetected>")},
		{Label: "Module tree", Value: moduleTree},
		{Label: "Detected modules", Value: moduleCount},
		{Label: "Config path", Value: blankIfEmpty(result.ConfigPath, "<none>")},
	}
}

func (a *App) promptValidatedInstallerKernelModules(
	inspection RebuildInstallerISOInspection,
	hostSupport BuildISOKernelInspectResult,
) ([]string, error) {
	for {
		modulesInput, err := a.promptRequiredString("Kernel modules to include (comma or space separated)", "")
		if err != nil {
			return nil, err
		}
		modules := parseDelimitedValues(modulesInput)
		if len(modules) == 0 {
			fmt.Println("Enter at least one kernel module name.")
			continue
		}
		if hostSupport.ModuleTreeDir == "" {
			printBulletList(
				"Host validation",
				"No matching host module tree was detected for this installer kernel version.",
				"Continuing without host-side module path validation; the rebuild step will still use Debian source packages.",
			)
			return modules, nil
		}
		evidence, err := a.backend.InspectBuildISOKernelEvidence(BuildISOKernelInspectRequest{
			KernelVersion:         inspection.InstallerKernelVersion,
			ModuleNames:           modules,
			ModuleAliasCandidates: append([]string{}, defaultBuildISOModuleAliases...),
			ModuleTreeDir:         hostSupport.ModuleTreeDir,
			DownloadIfMissing:     false,
		})
		if err != nil {
			return nil, err
		}
		rows := make([]infoRow, 0, len(evidence.Modules))
		missing := make([]string, 0)
		for _, module := range evidence.Modules {
			value := module.Path
			if !module.Found {
				value = "<not found>"
				missing = append(missing, module.Name)
			}
			rows = append(rows, infoRow{Label: module.Name, Value: value})
		}
		printSection("Module Lookup", rows...)
		if len(missing) == 0 {
			return modules, nil
		}
		fmt.Printf("Module lookup failed for: %s. Enter the module names again.\n", strings.Join(missing, ", "))
	}
}

func (a *App) rebuildInstallerLiveHostDebianMenu(inspection RebuildInstallerISOInspection) (menuAction, error) {
	for {
		printHeader("Rebuild Installer ISO - Debian - Live Host")
		printSection(
			"Live",
			infoRow{Label: "RootFS", Value: blankIfEmpty(inspection.LiveRootFSPath, "<undetected>")},
			infoRow{Label: "Kernel", Value: blankIfEmpty(inspection.LiveKernelPath, "<undetected>")},
			infoRow{Label: "Initrd", Value: blankIfEmpty(inspection.LiveInitrdPath, "<undetected>")},
		)
		printMenu(
			menuEntry{Key: "1", Label: "Replace Kernel", Detail: "install new live kernel + initrd"},
			menuEntry{Key: "2", Label: "Add Deb Packages", Detail: "install packages into live rootfs"},
			menuEntry{Key: "b", Label: "Back"},
			menuEntry{Key: "e", Label: "Cancel"},
		)
		choice, err := a.promptChoice("Select a Live Host action")
		if err != nil {
			return menuStay, err
		}
		switch choice {
		case "1":
			kernelVersion, err := a.promptRequiredString("Target live kernel version", "6.19.10+deb13-amd64")
			if err != nil {
				return menuStay, err
			}
			plan := newRebuildInstallerISOPlan(inspection, rebuildInstallerISOScopeLiveHost, rebuildInstallerISOActionReplaceKernel)
			plan.TargetKernelVersion = collapseWhitespace(kernelVersion)
			return a.executeRebuildInstallerISOPlan(inspection, plan)
		case "2":
			packagesInput, err := a.promptRequiredString("Deb packages to install into the live rootfs", "")
			if err != nil {
				return menuStay, err
			}
			plan := newRebuildInstallerISOPlan(inspection, rebuildInstallerISOScopeLiveHost, rebuildInstallerISOActionAddDebPackages)
			plan.LiveDebPackages = parseDelimitedValues(packagesInput)
			return a.executeRebuildInstallerISOPlan(inspection, plan)
		case "b", "e":
			return menuStay, nil
		default:
			fmt.Println("Invalid selection.")
		}
	}
}

func (a *App) executeRebuildInstallerISOPlan(inspection RebuildInstallerISOInspection, plan RebuildInstallerISOPlan) (menuAction, error) {
	printHeader("Rebuild Installer ISO - Review")
	printSection("Plan", rebuildInstallerPlanRows(inspection, plan)...)
	if len(inspection.Warnings) > 0 {
		printBulletList("Source warnings", inspection.Warnings...)
	}

	ensureDeps, err := a.promptYesNo("Check and install missing Debian rebuild host dependencies now", true)
	if err != nil {
		return menuStay, err
	}
	if !ensureDeps {
		fmt.Println("Installer ISO rebuild cancelled before dependency validation.")
		return menuStay, nil
	}
	if err := a.backend.EnsureDebianRebuildDeps(); err != nil {
		return menuStay, err
	}

	confirmed, err := a.promptYesNo("Proceed with installer ISO rebuild", false)
	if err != nil {
		return menuStay, err
	}
	if !confirmed {
		fmt.Println("Installer ISO rebuild cancelled.")
		return menuStay, nil
	}

	result, err := a.backend.RebuildDebianInstallerISO(plan)
	if err != nil {
		return menuStay, err
	}
	printHeader("Installer ISO rebuild complete")
	printSection(
		"Result",
		infoRow{Label: "ISO", Value: result.ISOPath},
		infoRow{Label: "Workspace", Value: result.WorkspaceDir},
		infoRow{Label: "Build log", Value: result.LogPath},
		infoRow{Label: "Manifest", Value: result.ManifestPath},
		infoRow{Label: "Installer udeb repo", Value: result.StagedUdebRepoPath},
		infoRow{Label: "Modified paths", Value: strings.Join(result.ModifiedPaths, ", ")},
	)
	if len(result.Warnings) > 0 {
		printBulletList("Warnings", result.Warnings...)
	}
	return menuStay, nil
}

func newRebuildInstallerISOPlan(inspection RebuildInstallerISOInspection, scope, action string) RebuildInstallerISOPlan {
	return RebuildInstallerISOPlan{
		SchemaVersion: rebuildInstallerISOSchemaVersion,
		Distro:        rebuildInstallerISODistroDebian,
		SourceISOPath: inspection.SourcePath,
		OutputDir:     defaultRebuildInstallerISOOutputDir,
		ImageName:     defaultRebuildInstallerISOImageName(inspection.SourcePath, action),
		Scope:         scope,
		Action:        action,
		Architecture:  blankIfEmpty(inspection.Architecture, "amd64"),
	}
}

func rebuildInstallerInspectionRows(inspection RebuildInstallerISOInspection) []infoRow {
	rows := []infoRow{
		{Label: "ISO", Value: inspection.SourcePath},
		{Label: "Volume ID", Value: blankIfEmpty(inspection.VolumeID, "<unknown>")},
		{Label: "Media class", Value: blankIfEmpty(inspection.MediaClass, "<unknown>")},
		{Label: "Architecture", Value: blankIfEmpty(inspection.Architecture, "<unknown>")},
		{Label: "Firmware", Value: joinOrNone(inspection.Firmware)},
		{Label: "Installer entry", Value: blankIfEmpty(inspection.BestInstallerTitle, "<undetected>")},
		{Label: "Installer kernel", Value: blankIfEmpty(inspection.InstallerKernelVersion, "<undetected>")},
		{Label: "Live entry", Value: blankIfEmpty(inspection.BestLiveTitle, "<undetected>")},
		{Label: "Live rootfs", Value: blankIfEmpty(inspection.LiveRootFSPath, "<undetected>")},
	}
	return rows
}

func rebuildInstallerPlanRows(inspection RebuildInstallerISOInspection, plan RebuildInstallerISOPlan) []infoRow {
	rows := []infoRow{
		{Label: "Scope", Value: plan.Scope},
		{Label: "Action", Value: rebuildInstallerISOActionLabel(plan.Action)},
		{Label: "Source ISO", Value: plan.SourceISOPath},
		{Label: "Output", Value: filepath.Join(plan.OutputDir, plan.ImageName)},
		{Label: "Architecture", Value: blankIfEmpty(plan.Architecture, inspection.Architecture)},
	}
	if plan.Scope == rebuildInstallerISOScopeDI {
		rows = append(rows,
			infoRow{Label: "Installer kernel", Value: blankIfEmpty(inspection.InstallerKernelVersion, "<undetected>")},
			infoRow{Label: "Installer initrd", Value: blankIfEmpty(inspection.InstallerInitrdPath, "<undetected>")},
		)
	}
	switch plan.Action {
	case rebuildInstallerISOActionAddKernelModules:
		rows = append(rows, infoRow{Label: "Kernel modules", Value: joinOrNone(plan.InstallerKernelModules)})
	case rebuildInstallerISOActionAddUdebPackages:
		rows = append(rows, infoRow{Label: "Udeb packages", Value: joinOrNone(plan.InstallerUdebPackages)})
	case rebuildInstallerISOActionUpdateKernel, rebuildInstallerISOActionReplaceKernel:
		rows = append(rows, infoRow{Label: "Target kernel", Value: plan.TargetKernelVersion})
	case rebuildInstallerISOActionAddDebPackages:
		rows = append(rows, infoRow{Label: "Live packages", Value: joinOrNone(plan.LiveDebPackages)})
	}
	return rows
}

func rebuildInstallerISOActionLabel(action string) string {
	switch action {
	case rebuildInstallerISOActionAddKernelModules:
		return "Add Kernel Modules"
	case rebuildInstallerISOActionAddUdebPackages:
		return "Add Udeb Packages"
	case rebuildInstallerISOActionUpdateKernel:
		return "Update Kernel"
	case rebuildInstallerISOActionReplaceKernel:
		return "Replace Kernel"
	case rebuildInstallerISOActionAddDebPackages:
		return "Add Deb Packages"
	default:
		return action
	}
}

func defaultRebuildInstallerISOImageName(sourceISOPath, action string) string {
	baseName := strings.TrimSuffix(filepath.Base(sourceISOPath), filepath.Ext(sourceISOPath))
	suffix := "rebuilt"
	switch action {
	case rebuildInstallerISOActionAddKernelModules:
		suffix = "di-kmods"
	case rebuildInstallerISOActionAddUdebPackages:
		suffix = "di-udebs"
	case rebuildInstallerISOActionUpdateKernel:
		suffix = "di-kernel"
	case rebuildInstallerISOActionReplaceKernel:
		suffix = "live-kernel"
	case rebuildInstallerISOActionAddDebPackages:
		suffix = "live-packages"
	}
	if baseName == "" {
		baseName = "debian-installer"
	}
	return fmt.Sprintf("%s-%s.iso", baseName, suffix)
}
