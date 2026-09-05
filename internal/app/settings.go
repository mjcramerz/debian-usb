package app

import (
	"fmt"
	"strconv"
	"strings"
)

func (a *App) settingsMenu() (menuAction, error) {
	for {
		printHeader("Settings")
		printSection(
			"Overview",
			infoRow{Label: "Configured profile overrides", Value: fmt.Sprintf("%d", a.configuredProfileOverrideCount())},
			infoRow{Label: "Managed profiles", Value: fmt.Sprintf("%d", len(profileOrder))},
		)
		printSection(
			"Live defaults",
			infoRow{Label: "Persistence size", Value: fmt.Sprintf("%d GiB", a.config.DefaultPersistenceSizeGiB)},
			infoRow{Label: "Boot policy", Value: a.config.DefaultBootPolicy},
			infoRow{Label: "Copy live system to RAM", Value: onOffLabel(a.config.DefaultLiveToram)},
			infoRow{Label: "Live memory limit", Value: memGiBLabel(a.config.DefaultLiveMemGiB)},
			infoRow{Label: "Additional live kernel parameters", Value: configuredState(a.config.DefaultLiveKernelExtras)},
			infoRow{Label: "Additional forensics kernel parameters", Value: configuredState(a.config.DefaultForensicsKernelExtras)},
		)
		printSection(
			"Installer defaults",
			infoRow{Label: "Installer policy", Value: a.config.DefaultInstallerPolicy},
			infoRow{Label: "Additional installer kernel parameters", Value: configuredState(a.config.DefaultInstallerKernelExtras)},
		)
		a.printMenu(
			menuEntry{Key: "1", Label: "Live Defaults", Detail: "Persistence size, live boot policy, toram, memory limit, and shared live/forensics extras."},
			menuEntry{Key: "2", Label: "Installer Defaults", Detail: "Default installer policy and shared installer extras applied to installer-capable media."},
			menuEntry{Key: "3", Label: "Profile Overrides", Detail: "Per-profile live, forensics, installer, and installer-URL overrides."},
			menuEntry{Key: "4", Label: "Review Effective Managed Boot Defaults", Detail: "Expanded preview of the merged boot arguments that the managed writer will apply."},
			menuEntry{Key: "5", Label: "Planned Executions", Detail: "Edit, run, or remove saved planned executions under /data/cfg/debian-usb."},
			menuEntry{Key: "b", Label: "Go Back"},
			menuEntry{Key: "e", Label: "Exit"},
		)
		choice, err := a.promptChoice("Select an option")
		if err != nil {
			return menuStay, err
		}
		switch choice {
		case "1":
			action, err := a.liveDefaultsMenu()
			if err != nil {
				return menuStay, err
			}
			if action == menuExit {
				return menuExit, nil
			}
		case "2":
			action, err := a.installerDefaultsMenu()
			if err != nil {
				return menuStay, err
			}
			if action == menuExit {
				return menuExit, nil
			}
		case "3":
			action, err := a.profileOverridesMenu()
			if err != nil {
				return menuStay, err
			}
			if action == menuExit {
				return menuExit, nil
			}
		case "4":
			a.printEffectiveManagedBootDefaults()
		case "5":
			action, err := a.plannedExecutionsMenu()
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

func (a *App) liveDefaultsMenu() (menuAction, error) {
	for {
		printHeader("Live Defaults")
		printSection(
			"Current values",
			infoRow{Label: "Persistence size", Value: fmt.Sprintf("%d GiB", a.config.DefaultPersistenceSizeGiB)},
			infoRow{Label: "Live boot policy", Value: a.config.DefaultBootPolicy},
			infoRow{Label: "Effective policy kernel arguments", Value: a.livePolicyKernelArgs()},
			infoRow{Label: "Copy live system to RAM", Value: onOffLabel(a.config.DefaultLiveToram)},
			infoRow{Label: "Live memory limit", Value: memGiBLabel(a.config.DefaultLiveMemGiB)},
			infoRow{Label: "Additional live kernel parameters", Value: displayValueOrNone(a.config.DefaultLiveKernelExtras)},
			infoRow{Label: "Additional forensics kernel parameters", Value: displayValueOrNone(a.config.DefaultForensicsKernelExtras)},
		)
		a.printMenu(
			menuEntry{Key: "1", Label: "Set Default Persistence Size", Detail: fmt.Sprintf("Current: %d GiB", a.config.DefaultPersistenceSizeGiB)},
			menuEntry{Key: "2", Label: "Set Default Live Boot Policy", Detail: fmt.Sprintf("Current: %s", a.config.DefaultBootPolicy)},
			menuEntry{Key: "3", Label: "Toggle Copy Live System To RAM", Detail: fmt.Sprintf("Current: %s", onOffLabel(a.config.DefaultLiveToram))},
			menuEntry{Key: "4", Label: "Set Default Live Memory Limit", Detail: fmt.Sprintf("Current: %s", memGiBLabel(a.config.DefaultLiveMemGiB))},
			menuEntry{Key: "5", Label: "Set Additional Live Kernel Parameters", Detail: fmt.Sprintf("Current: %s", displayValueOrNone(a.config.DefaultLiveKernelExtras))},
			menuEntry{Key: "6", Label: "Set Additional Forensics Kernel Parameters", Detail: fmt.Sprintf("Current: %s", displayValueOrNone(a.config.DefaultForensicsKernelExtras))},
			menuEntry{Key: "b", Label: "Go Back"},
			menuEntry{Key: "e", Label: "Exit"},
		)
		choice, err := a.promptChoice("Select an option")
		if err != nil {
			return menuStay, err
		}
		switch choice {
		case "1":
			size, err := a.promptPositiveInt("Default persistence size in GiB", a.config.DefaultPersistenceSizeGiB)
			if err != nil {
				return menuStay, err
			}
			cfg, err := a.backend.SetDefaultPersistenceSize(size)
			if err != nil {
				return menuStay, err
			}
			a.config = cfg
			fmt.Printf("Default persistence size updated to %d GiB.\n", size)
		case "2":
			policy, err := a.chooseLiveBootPolicy()
			if err != nil {
				return menuStay, err
			}
			cfg, err := a.backend.SetDefaultBootPolicy(policy)
			if err != nil {
				return menuStay, err
			}
			a.config = cfg
			fmt.Printf("Default live boot policy set to %s.\n", a.config.DefaultBootPolicy)
		case "3":
			cfg, err := a.backend.SetDefaultLiveToram(!a.config.DefaultLiveToram)
			if err != nil {
				return menuStay, err
			}
			a.config = cfg
			fmt.Printf("Copy live system to RAM set to %s.\n", onOffLabel(a.config.DefaultLiveToram))
		case "4":
			size, err := a.promptNonNegativeInt("Default live memory limit in GiB (0 disables the limit)", a.config.DefaultLiveMemGiB)
			if err != nil {
				return menuStay, err
			}
			cfg, err := a.backend.SetDefaultLiveMemGiB(size)
			if err != nil {
				return menuStay, err
			}
			a.config = cfg
			fmt.Printf("Default live memory limit updated to %s.\n", memGiBLabel(a.config.DefaultLiveMemGiB))
		case "5":
			kernelArgs, err := a.promptEditableOptionalString(
				"Additional live kernel parameters",
				a.config.DefaultLiveKernelExtras,
			)
			if err != nil {
				return menuStay, err
			}
			cfg, err := a.backend.SetDefaultLiveKernelExtras(kernelArgs)
			if err != nil {
				return menuStay, err
			}
			a.config = cfg
			fmt.Printf("Additional live kernel parameters updated to %s.\n", displayValueOrNone(a.config.DefaultLiveKernelExtras))
		case "6":
			kernelArgs, err := a.promptEditableOptionalString(
				"Additional forensics kernel parameters",
				a.config.DefaultForensicsKernelExtras,
			)
			if err != nil {
				return menuStay, err
			}
			cfg, err := a.backend.SetDefaultForensicsKernelExtras(kernelArgs)
			if err != nil {
				return menuStay, err
			}
			a.config = cfg
			fmt.Printf("Additional forensics kernel parameters updated to %s.\n", displayValueOrNone(a.config.DefaultForensicsKernelExtras))
		case "b":
			return menuStay, nil
		case "e":
			return menuExit, nil
		default:
			fmt.Println("Invalid selection.")
		}
	}
}

func (a *App) installerDefaultsMenu() (menuAction, error) {
	for {
		printHeader("Installer Defaults")
		printSection(
			"Current values",
			infoRow{Label: "Installer policy", Value: a.config.DefaultInstallerPolicy},
			infoRow{Label: "Effective shared installer arguments", Value: displayValueOrNone(a.defaultInstallerKernelArgs())},
			infoRow{Label: "Additional installer kernel parameters", Value: displayValueOrNone(a.config.DefaultInstallerKernelExtras)},
		)
		a.printMenu(
			menuEntry{Key: "1", Label: "Set Default Installer Policy", Detail: fmt.Sprintf("Current: %s", a.config.DefaultInstallerPolicy)},
			menuEntry{Key: "2", Label: "Set Additional Installer Kernel Parameters", Detail: fmt.Sprintf("Current: %s", displayValueOrNone(a.config.DefaultInstallerKernelExtras))},
			menuEntry{Key: "b", Label: "Go Back"},
			menuEntry{Key: "e", Label: "Exit"},
		)
		choice, err := a.promptChoice("Select an option")
		if err != nil {
			return menuStay, err
		}
		switch choice {
		case "1":
			policy, err := a.chooseInstallerPolicy()
			if err != nil {
				return menuStay, err
			}
			cfg, err := a.backend.SetDefaultInstallerPolicy(policy)
			if err != nil {
				return menuStay, err
			}
			a.config = cfg
			fmt.Printf("Default installer policy set to %s.\n", a.config.DefaultInstallerPolicy)
		case "2":
			kernelArgs, err := a.promptEditableOptionalString(
				"Additional installer kernel parameters",
				a.config.DefaultInstallerKernelExtras,
			)
			if err != nil {
				return menuStay, err
			}
			cfg, err := a.backend.SetDefaultInstallerKernelExtras(kernelArgs)
			if err != nil {
				return menuStay, err
			}
			a.config = cfg
			fmt.Printf("Installer kernel parameters updated to %s.\n", displayValueOrNone(a.config.DefaultInstallerKernelExtras))
		case "b":
			return menuStay, nil
		case "e":
			return menuExit, nil
		default:
			fmt.Println("Invalid selection.")
		}
	}
}

func (a *App) profileOverridesMenu() (menuAction, error) {
	for {
		printHeader("Profile Overrides")
		printSection("Profiles", infoRow{Label: "Configured override values", Value: fmt.Sprintf("%d", a.configuredProfileOverrideCount())})
		entries := make([]menuEntry, 0, len(profileOrder)+2)
		for index, key := range profileOrder {
			spec := profileSpecs[key]
			entries = append(entries, menuEntry{
				Key:    strconv.Itoa(index + 1),
				Label:  spec.MenuLabel,
				Detail: a.profileOverrideSummary(spec),
			})
		}
		entries = append(entries, menuEntry{Key: "b", Label: "Go Back"}, menuEntry{Key: "e", Label: "Exit"})
		a.printMenu(entries...)
		choice, err := a.promptChoice("Select a profile")
		if err != nil {
			return menuStay, err
		}
		switch choice {
		case "b":
			return menuStay, nil
		case "e":
			return menuExit, nil
		default:
			index, err := strconv.Atoi(choice)
			if err != nil || index < 1 || index > len(profileOrder) {
				fmt.Println("Invalid selection.")
				continue
			}
			action, err := a.profileSettingsMenu(profileOrder[index-1])
			if err != nil {
				return menuStay, err
			}
			if action == menuExit {
				return menuExit, nil
			}
		}
	}
}

func (a *App) profileSettingsMenu(profile string) (menuAction, error) {
	spec, ok := profileSpecs[profile]
	if !ok {
		return menuStay, fmt.Errorf("unsupported profile: %s", profile)
	}
	for {
		if !spec.SupportsManaged {
			printHeader(spec.MenuLabel + " Overrides")
			printSection(
				"Profile state",
				infoRow{Label: "Managed mode", Value: "Direct write only"},
				infoRow{Label: "Overrides", Value: "No managed boot overrides apply to this profile"},
			)
			a.printMenu(menuEntry{Key: "b", Label: "Go Back"}, menuEntry{Key: "e", Label: "Exit"})
			choice, err := a.promptChoice("Select an option")
			if err != nil {
				return menuStay, err
			}
			switch choice {
			case "b":
				return menuStay, nil
			case "e":
				return menuExit, nil
			default:
				fmt.Println("Invalid selection.")
			}
			continue
		}

		liveSummary := "Not used for this profile"
		forensicsSummary := "Not used for this profile"
		if spec.SupportsLiveOverrides {
			liveSummary = summarizeValue(a.profileLiveKernelExtras(profile), 96)
			forensicsSummary = summarizeValue(a.profileForensicsKernelExtras(profile), 96)
		}
		printHeader(spec.MenuLabel + " Overrides")
		printSection(
			"Current values",
			infoRow{Label: "Managed mode", Value: "Supported"},
			infoRow{Label: "Live kernel parameters", Value: liveSummary},
			infoRow{Label: "Forensics kernel parameters", Value: forensicsSummary},
			infoRow{Label: "Installer kernel parameters", Value: displayValueOrNone(a.profileInstallerKernelExtras(profile))},
			infoRow{Label: "Installer URL", Value: displayValueOrNone(a.profilePreseedURL(profile))},
		)
		liveOption := ""
		forensicsOption := ""
		installerOption := ""
		preseedOption := ""
		nextOption := 1
		entries := make([]menuEntry, 0, 6)
		if spec.SupportsLiveOverrides {
			liveOption = strconv.Itoa(nextOption)
			entries = append(entries, menuEntry{
				Key:    liveOption,
				Label:  "Set Additional Live Kernel Parameters",
				Detail: fmt.Sprintf("Current: %s", displayValueOrNone(a.profileLiveKernelExtras(profile))),
			})
			nextOption++
			forensicsOption = strconv.Itoa(nextOption)
			entries = append(entries, menuEntry{
				Key:    forensicsOption,
				Label:  "Set Additional Forensics Kernel Parameters",
				Detail: fmt.Sprintf("Current: %s", displayValueOrNone(a.profileForensicsKernelExtras(profile))),
			})
			nextOption++
		}
		installerOption = strconv.Itoa(nextOption)
		entries = append(entries, menuEntry{
			Key:    installerOption,
			Label:  "Set Additional Installer Kernel Parameters",
			Detail: fmt.Sprintf("Current: %s", displayValueOrNone(a.profileInstallerKernelExtras(profile))),
		})
		nextOption++
		preseedOption = strconv.Itoa(nextOption)
		entries = append(entries, menuEntry{
			Key:    preseedOption,
			Label:  "Set Installer URL",
			Detail: fmt.Sprintf("Current: %s", displayValueOrNone(a.profilePreseedURL(profile))),
		})
		entries = append(entries, menuEntry{Key: "b", Label: "Go Back"}, menuEntry{Key: "e", Label: "Exit"})
		a.printMenu(entries...)

		choice, err := a.promptChoice("Select an option")
		if err != nil {
			return menuStay, err
		}
		switch choice {
		case liveOption:
			if liveOption == "" {
				fmt.Println("Invalid selection.")
				continue
			}
			kernelArgs, err := a.promptEditableOptionalString(
				fmt.Sprintf("%s live kernel parameters", spec.MenuLabel),
				a.profileLiveKernelExtras(profile),
			)
			if err != nil {
				return menuStay, err
			}
			cfg, err := a.backend.SetProfileLiveKernelExtras(profile, kernelArgs)
			if err != nil {
				return menuStay, err
			}
			a.config = cfg
			fmt.Printf("%s live kernel parameters updated to %s.\n", spec.MenuLabel, displayValueOrNone(a.profileLiveKernelExtras(profile)))
		case forensicsOption:
			if forensicsOption == "" {
				fmt.Println("Invalid selection.")
				continue
			}
			kernelArgs, err := a.promptEditableOptionalString(
				fmt.Sprintf("%s forensics kernel parameters", spec.MenuLabel),
				a.profileForensicsKernelExtras(profile),
			)
			if err != nil {
				return menuStay, err
			}
			cfg, err := a.backend.SetProfileForensicsKernelExtras(profile, kernelArgs)
			if err != nil {
				return menuStay, err
			}
			a.config = cfg
			fmt.Printf("%s forensics kernel parameters updated to %s.\n", spec.MenuLabel, displayValueOrNone(a.profileForensicsKernelExtras(profile)))
		case installerOption:
			kernelArgs, err := a.promptEditableOptionalString(
				fmt.Sprintf("%s installer kernel parameters", spec.MenuLabel),
				a.profileInstallerKernelExtras(profile),
			)
			if err != nil {
				return menuStay, err
			}
			cfg, err := a.backend.SetProfileInstallerKernelExtras(profile, kernelArgs)
			if err != nil {
				return menuStay, err
			}
			a.config = cfg
			fmt.Printf("%s installer kernel parameters updated to %s.\n", spec.MenuLabel, displayValueOrNone(a.profileInstallerKernelExtras(profile)))
		case preseedOption:
			url, err := a.promptEditableOptionalString(
				fmt.Sprintf("%s installer URL", spec.MenuLabel),
				a.profilePreseedURL(profile),
			)
			if err != nil {
				return menuStay, err
			}
			cfg, err := a.backend.SetProfilePreseedURL(profile, url)
			if err != nil {
				return menuStay, err
			}
			a.config = cfg
			fmt.Printf("%s installer URL updated to %s.\n", spec.MenuLabel, displayValueOrNone(a.profilePreseedURL(profile)))
		case "b":
			return menuStay, nil
		case "e":
			return menuExit, nil
		default:
			fmt.Println("Invalid selection.")
		}
	}
}

func (a *App) configuredProfileOverrideCount() int {
	count := 0
	for _, key := range profileOrder {
		spec := profileSpecs[key]
		if spec.SupportsManaged && spec.SupportsLiveOverrides && a.profileLiveKernelExtras(key) != "" {
			count++
		}
		if spec.SupportsManaged && spec.SupportsLiveOverrides && a.profileForensicsKernelExtras(key) != "" {
			count++
		}
		if spec.SupportsManaged && a.profileInstallerKernelExtras(key) != "" {
			count++
		}
		if spec.SupportsManaged && a.profilePreseedURL(key) != "" {
			count++
		}
	}
	return count
}

func (a *App) profileOverrideSummary(spec profileSpec) string {
	if !spec.SupportsManaged {
		return "managed rebuild unavailable"
	}
	liveState := "n/a"
	forensicsState := "n/a"
	if spec.SupportsLiveOverrides {
		liveState = overrideStateLabel(a.profileLiveKernelExtras(spec.Key))
		forensicsState = overrideStateLabel(a.profileForensicsKernelExtras(spec.Key))
	}
	return fmt.Sprintf(
		"live %s | forensics %s | installer %s | URL %s",
		liveState,
		forensicsState,
		overrideStateLabel(a.profileInstallerKernelExtras(spec.Key)),
		overrideStateLabel(a.profilePreseedURL(spec.Key)),
	)
}

func (a *App) profileLiveKernelExtras(profile string) string {
	if a.config.ProfileLiveKernelExtras == nil {
		return ""
	}
	return strings.TrimSpace(a.config.ProfileLiveKernelExtras[profile])
}

func (a *App) profileFallbackLiveKernelArgs(profile string) string {
	if a.config.ProfileFallbackLiveKernelArgs == nil {
		return ""
	}
	return strings.TrimSpace(a.config.ProfileFallbackLiveKernelArgs[profile])
}

func (a *App) profileInstallerKernelExtras(profile string) string {
	if a.config.ProfileInstallerKernelExtras == nil {
		return ""
	}
	return strings.TrimSpace(a.config.ProfileInstallerKernelExtras[profile])
}

func (a *App) profileForensicsKernelExtras(profile string) string {
	if a.config.ProfileForensicsKernelExtras == nil {
		return ""
	}
	return strings.TrimSpace(a.config.ProfileForensicsKernelExtras[profile])
}

func (a *App) profilePreseedURL(profile string) string {
	if a.config.ProfilePreseedURLs == nil {
		return ""
	}
	return strings.TrimSpace(a.config.ProfilePreseedURLs[profile])
}

func (a *App) printEffectiveManagedBootDefaults() {
	printHeader("Effective Managed Boot Defaults")
	printSection("Shared defaults", infoRow{Label: "Selected live boot policy", Value: fmt.Sprintf("%s -> %s", a.config.DefaultBootPolicy, a.livePolicyKernelArgs())})
	if a.config.DefaultForensicsKernelExtras != "" {
		printSection("Forensics", infoRow{Label: "Shared forensics arguments", Value: a.config.DefaultForensicsKernelExtras})
	}
	printSection("Installer", infoRow{Label: "Shared installer policy arguments", Value: displayValueOrNone(a.defaultInstallerKernelArgs())})
	for _, key := range profileOrder {
		spec := profileSpecs[key]
		if !spec.SupportsManaged {
			printSection(spec.MenuLabel, infoRow{Label: "Managed mode", Value: "Direct write only; overrides do not apply"})
			continue
		}
		rows := []infoRow{}
		if spec.SupportsLiveOverrides {
			rows = append(rows, infoRow{Label: "Live", Value: a.defaultLiveKernelArgs(spec, "")})
			if spec.SupportsPersistence && key != profileTails {
				rows = append(rows, infoRow{Label: "Live with persistence", Value: a.defaultLiveKernelArgs(spec, persistenceModePlain)})
			}
			if key == profileDebian || key == profileKaliLinux || key == profileTails {
				rows = append(rows, infoRow{Label: "Live with encrypted persistence", Value: a.defaultLiveKernelArgs(spec, persistenceModeEncrypted)})
			}
		}
		if url := a.profilePreseedURL(key); url != "" {
			rows = append(rows, infoRow{Label: "Installer URL", Value: url})
		}
		if spec.SupportsLiveOverrides {
			if extras := a.profileLiveKernelExtras(key); extras != "" {
				rows = append(rows, infoRow{Label: "Live extras", Value: extras})
			}
			if extras := a.profileForensicsKernelExtras(key); extras != "" {
				rows = append(rows, infoRow{Label: "Forensics extras", Value: extras})
			}
		}
		if extras := a.profileInstallerKernelExtras(key); extras != "" {
			rows = append(rows, infoRow{Label: "Installer extras", Value: extras})
		}
		printSection(spec.MenuLabel, rows...)
	}
	printBulletList("Notes", "Managed live entries add their ISO-store locator at render time, so findiso= or iso-scan/filename= is intentionally not shown above.")
}

func (a *App) chooseLiveBootPolicy() (string, error) {
	for {
		printHeader("Live Boot Policy")
		a.printMenu(policyMenuEntries(liveBootPolicyOptions, a.config.DefaultBootPolicy)...)
		choice, err := a.promptChoice("Select the default live boot policy")
		if err != nil {
			return "", err
		}
		switch choice {
		case "b":
			return a.config.DefaultBootPolicy, nil
		default:
			index, err := strconv.Atoi(choice)
			if err != nil || index < 1 || index > len(liveBootPolicyOptions) {
				fmt.Println("Invalid selection.")
				continue
			}
			return liveBootPolicyOptions[index-1].Name, nil
		}
	}
}

func (a *App) chooseInstallerPolicy() (string, error) {
	for {
		printHeader("Installer Policy")
		a.printMenu(policyMenuEntries(installerPolicyOptions, a.config.DefaultInstallerPolicy)...)
		choice, err := a.promptChoice("Select the default installer policy")
		if err != nil {
			return "", err
		}
		switch choice {
		case "b":
			return a.config.DefaultInstallerPolicy, nil
		default:
			index, err := strconv.Atoi(choice)
			if err != nil || index < 1 || index > len(installerPolicyOptions) {
				fmt.Println("Invalid selection.")
				continue
			}
			return installerPolicyOptions[index-1].Name, nil
		}
	}
}

func overrideStateLabel(value string) string {
	if strings.TrimSpace(value) == "" {
		return "default"
	}
	return "set"
}

func policyMenuEntries(options []policyOption, current string) []menuEntry {
	entries := make([]menuEntry, 0, len(options)+1)
	for index, option := range options {
		detail := option.Description
		if option.Name == current {
			detail = "Current default. " + detail
		}
		entries = append(entries, menuEntry{
			Key:    strconv.Itoa(index + 1),
			Label:  option.Name,
			Detail: detail,
		})
	}
	entries = append(entries, menuEntry{Key: "b", Label: "Cancel"})
	return entries
}
