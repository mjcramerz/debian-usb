package app

import (
	"bufio"
	"fmt"
	"os"
)

type profileSpec struct {
	Key                   string
	MenuLabel             string
	MultiOSLabel          string
	SupportsManaged       bool
	SupportsPersistence   bool
	PreferredMedia        string
	SupportsLiveOverrides bool
	LiveBootFamily        string
	DefaultLiveMenuLabel  string
}

type policyOption struct {
	Name        string
	Description string
}

type menuAction int

const (
	menuStay menuAction = iota
	menuBack
	menuExit
)

var profileSpecs = map[string]profileSpec{
	profileDebian: {
		Key:                   profileDebian,
		MenuLabel:             "Debian (Live / Netinst / Netboot)",
		MultiOSLabel:          "Debian",
		SupportsManaged:       true,
		SupportsPersistence:   true,
		PreferredMedia:        "live",
		SupportsLiveOverrides: true,
		LiveBootFamily:        "live-boot",
		DefaultLiveMenuLabel:  "Debian Live",
	},
	profileUbuntuDesktop: {
		Key:                   profileUbuntuDesktop,
		MenuLabel:             "Ubuntu Desktop",
		MultiOSLabel:          "Ubuntu Desktop",
		SupportsManaged:       true,
		SupportsPersistence:   true,
		PreferredMedia:        "live",
		SupportsLiveOverrides: true,
		LiveBootFamily:        "casper",
		DefaultLiveMenuLabel:  "Ubuntu Desktop Live",
	},
	profileUbuntuServer: {
		Key:                   profileUbuntuServer,
		MenuLabel:             "Ubuntu Server",
		MultiOSLabel:          "Ubuntu Server",
		SupportsManaged:       true,
		SupportsPersistence:   false,
		PreferredMedia:        "installer",
		SupportsLiveOverrides: true,
		LiveBootFamily:        "casper",
		DefaultLiveMenuLabel:  "Ubuntu Server",
	},
	profileKaliLinux: {
		Key:                   profileKaliLinux,
		MenuLabel:             "Kali Linux (Live / Netinst / Netboot)",
		MultiOSLabel:          "Kali Linux",
		SupportsManaged:       true,
		SupportsPersistence:   true,
		PreferredMedia:        "live",
		SupportsLiveOverrides: true,
		LiveBootFamily:        "live-boot",
		DefaultLiveMenuLabel:  "Kali Live",
	},
	profileKaliPurple: {
		Key:                   profileKaliPurple,
		MenuLabel:             "Kali Purple",
		MultiOSLabel:          "Kali Purple",
		SupportsManaged:       true,
		SupportsPersistence:   false,
		PreferredMedia:        "installer",
		SupportsLiveOverrides: false,
		LiveBootFamily:        "",
		DefaultLiveMenuLabel:  "Kali Purple Installer",
	},
	profileTails: {
		Key:                   profileTails,
		MenuLabel:             "Tails",
		MultiOSLabel:          "Tails",
		SupportsManaged:       true,
		SupportsPersistence:   true,
		PreferredMedia:        "live",
		SupportsLiveOverrides: true,
		LiveBootFamily:        "live-boot",
		DefaultLiveMenuLabel:  "Tails Live",
	},
}

var profileOrder = []string{
	profileDebian,
	profileUbuntuDesktop,
	profileUbuntuServer,
	profileKaliLinux,
	profileKaliPurple,
	profileTails,
}

var liveBootPolicyOptions = []policyOption{
	{
		Name:        "balanced",
		Description: "Boot-safe general default.",
	},
	{
		Name:        "performance",
		Description: "Bias toward throughput and responsiveness.",
	},
	{
		Name:        "hardened",
		Description: "Bias toward stricter kernel hardening.",
	},
}

var installerPolicyOptions = []policyOption{
	{
		Name:        "preserve",
		Description: "Keep upstream installer arguments unchanged.",
	},
	{
		Name:        "installer-preseed",
		Description: "Append profile-safe unattended installer arguments and any configured installer URL.",
	},
}

type App struct {
	backend *Backend
	reader  *bufio.Reader
	stdin   *os.File
	config  RuntimeConfig
}

func New() (*App, error) {
	backend := NewBackend()
	config, err := backend.ShowConfig()
	if err != nil {
		return nil, err
	}
	return &App{
		backend: backend,
		reader:  bufio.NewReader(os.Stdin),
		stdin:   os.Stdin,
		config:  config,
	}, nil
}

func (a *App) Run(args []string) error {
	if len(args) > 0 {
		switch args[0] {
		case "--help", "-h":
			a.printHelp()
			return nil
		case "--version", "-V":
			fmt.Printf("%s %s\n", a.config.AppName, a.config.AppVersion)
			return nil
		default:
			return fmt.Errorf("unknown argument: %s", args[0])
		}
	}
	return a.mainMenu()
}

func (a *App) printHelp() {
	fmt.Printf("%s\n\nUsage:\n  debian-usb\n\nLocal ISO workflows: create bootable USB media, build custom Debian ISOs, or rebuild existing Debian installer ISOs.\n", a.config.AppName)
}

func (a *App) mainMenu() error {
	for {
		printHeader(a.config.AppName)
		printSection("Session", infoRow{Label: "Version", Value: a.config.AppVersion})
		printMenu(
			menuEntry{Key: "1", Label: "Create USB", Detail: "local ISO -> USB"},
			menuEntry{Key: "2", Label: "Build Custom ISO", Detail: "Debian live-build"},
			menuEntry{Key: "3", Label: "Rebuild Installer ISO", Detail: "existing Debian installer ISO"},
			menuEntry{Key: "4", Label: "Settings", Detail: "runtime defaults"},
			menuEntry{Key: "e", Label: "Exit"},
		)
		choice, err := a.promptChoice("Select an option")
		if err != nil {
			return err
		}
		switch choice {
		case "1":
			action, err := a.createMenu()
			if err != nil {
				return err
			}
			if action == menuExit {
				return nil
			}
		case "2":
			action, err := a.buildISOMenu()
			if err != nil {
				return err
			}
			if action == menuExit {
				return nil
			}
		case "3":
			action, err := a.rebuildInstallerISOMenu()
			if err != nil {
				return err
			}
			if action == menuExit {
				return nil
			}
		case "4":
			action, err := a.settingsMenu()
			if err != nil {
				return err
			}
			if action == menuExit {
				return nil
			}
		case "e":
			return nil
		default:
			fmt.Println("Invalid selection.")
		}
	}
}

func (a *App) createMenu() (menuAction, error) {
	for {
		printHeader("Create USB")
		printSection(
			"Modes",
			infoRow{Label: "Create", Value: "Provision a new USB from a local ISO or a saved plan"},
			infoRow{Label: "Update", Value: "Refresh managed GRUB, preseed trees, and staged boot assets on an existing managed USB"},
		)
		printMenu(
			menuEntry{Key: "1", Label: "Create New USB", Detail: "profile-driven create flow"},
			menuEntry{Key: "2", Label: "Update USB", Detail: "refresh an existing managed USB without repartitioning"},
			menuEntry{Key: "3", Label: "Planned Execution", Detail: "saved USB plan"},
			menuEntry{Key: "b", Label: "Go Back"},
			menuEntry{Key: "e", Label: "Exit"},
		)
		choice, err := a.promptChoice("Select a mode")
		if err != nil {
			return menuStay, err
		}
		switch choice {
		case "1":
			action, err := a.createNewUSBFamilyMenu()
			if err != nil {
				return menuStay, err
			}
			if action == menuExit {
				return menuExit, nil
			}
		case "2":
			action, err := a.updateUSBMenu()
			if err != nil {
				return menuStay, err
			}
			if action == menuExit {
				return menuExit, nil
			}
		case "3":
			action, err := a.handleRunPlannedExecution()
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

func (a *App) createNewUSBFamilyMenu() (menuAction, error) {
	for {
		printHeader("Create New USB")
		printSection(
			"Families",
			infoRow{Label: "Debian-based", Value: "Debian, Kali Linux, Kali Purple, Tails"},
			infoRow{Label: "Ubuntu-based", Value: "Ubuntu"},
		)
		printMenu(
			menuEntry{Key: "1", Label: "Debian-based USB", Detail: "Debian, Kali Linux, Kali Purple, Tails, and Debian-family Multi-OS"},
			menuEntry{Key: "2", Label: "Ubuntu-based USB", Detail: "Ubuntu"},
			menuEntry{Key: "b", Label: "Go Back"},
			menuEntry{Key: "e", Label: "Exit"},
		)
		choice, err := a.promptChoice("Select a family")
		if err != nil {
			return menuStay, err
		}
		switch choice {
		case "1":
			action, err := a.debianBasedCreateMenu()
			if err != nil {
				return menuStay, err
			}
			if action == menuExit {
				return menuExit, nil
			}
		case "2":
			action, err := a.ubuntuBasedCreateMenu()
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

func (a *App) debianBasedCreateMenu() (menuAction, error) {
	for {
		printHeader("Create New USB - Debian-based")
		printSection(
			"Profiles",
			infoRow{Label: "Managed", Value: "Debian, Kali Linux, Kali Purple, Tails"},
			infoRow{Label: "Persistence", Value: "Debian, Kali Linux, Tails"},
		)
		printMenu(
			menuEntry{Key: "1", Label: "Debian", Detail: "live | netinst | netboot"},
			menuEntry{Key: "2", Label: "Kali Linux", Detail: "live | netinst | netboot"},
			menuEntry{Key: "3", Label: "Kali Purple", Detail: "installer"},
			menuEntry{Key: "4", Label: "Tails", Detail: "live"},
			menuEntry{Key: "5", Label: "Multi-OS", Detail: "Debian-family managed GRUB"},
			menuEntry{Key: "b", Label: "Go Back"},
			menuEntry{Key: "e", Label: "Exit"},
		)
		choice, err := a.promptChoice("Select a profile")
		if err != nil {
			return menuStay, err
		}
		switch choice {
		case "1":
			action, err := a.handleCreate(profileDebian)
			if err != nil {
				return menuStay, err
			}
			if action == menuExit {
				return menuExit, nil
			}
		case "2":
			action, err := a.handleCreate(profileKaliLinux)
			if err != nil {
				return menuStay, err
			}
			if action == menuExit {
				return menuExit, nil
			}
		case "3":
			action, err := a.handleCreate(profileKaliPurple)
			if err != nil {
				return menuStay, err
			}
			if action == menuExit {
				return menuExit, nil
			}
		case "4":
			action, err := a.handleCreate(profileTails)
			if err != nil {
				return menuStay, err
			}
			if action == menuExit {
				return menuExit, nil
			}
		case "5":
			action, err := a.handleCreateMultiOS()
			if err != nil {
				return menuStay, err
			}
			if action == menuExit {
				return menuExit, nil
			}
		case "b":
			return menuBack, nil
		case "e":
			return menuExit, nil
		default:
			fmt.Println("Invalid selection.")
		}
	}
}

func (a *App) ubuntuBasedCreateMenu() (menuAction, error) {
	for {
		printHeader("Create New USB - Ubuntu-based")
		printSection(
			"Profiles",
			infoRow{Label: "Managed", Value: "Ubuntu"},
			infoRow{Label: "Persistence", Value: "Ubuntu"},
		)
		printMenu(
			menuEntry{Key: "1", Label: "Ubuntu", Detail: "casper live"},
			menuEntry{Key: "b", Label: "Go Back"},
			menuEntry{Key: "e", Label: "Exit"},
		)
		choice, err := a.promptChoice("Select a profile")
		if err != nil {
			return menuStay, err
		}
		switch choice {
		case "1":
			action, err := a.handleCreate(profileUbuntuDesktop)
			if err != nil {
				return menuStay, err
			}
			if action == menuExit {
				return menuExit, nil
			}
		case "b":
			return menuBack, nil
		case "e":
			return menuExit, nil
		default:
			fmt.Println("Invalid selection.")
		}
	}
}

func (a *App) handleCreate(profileKey string) (menuAction, error) {
	spec, ok := profileSpecs[profileKey]
	if !ok {
		return menuStay, fmt.Errorf("unsupported profile: %s", profileKey)
	}
	req, action, err := a.collectCreateRequest(spec)
	if err != nil {
		return menuStay, err
	}
	if action != menuStay {
		return action, nil
	}
	plan, err := buildCreatePlan(a.config, req)
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
	savedExecution, created, err := a.backend.SaveOrReusePlannedExecution(newSinglePlannedExecution(plan, device.Path))
	if err != nil {
		return menuStay, err
	}
	a.printPlanSummary(plan, device, spec, savedExecution.RunID, reviewSaveState(created))
	confirmed, err := a.promptYesNo("Proceed with USB creation", false)
	if err != nil {
		return menuStay, err
	}
	if !confirmed {
		fmt.Printf("USB creation cancelled. Planned execution %s remains saved.\n", savedExecution.RunID)
		return menuStay, nil
	}
	if err := a.backend.ExecuteCreate(plan, device.Path, plan.PersistenceSizeGiB); err != nil {
		return menuStay, err
	}
	savedExecution = a.recordSuccessfulPlannedExecution(savedExecution, device.Path)
	printHeader("USB creation complete")
	printSection(
		"Result",
		infoRow{Label: "Profile", Value: spec.MenuLabel},
		infoRow{Label: "Device", Value: device.Path},
		infoRow{Label: "Write mode", Value: plan.WriteMode},
		infoRow{Label: "Planned execution", Value: savedExecution.RunID},
	)
	printBulletList(
		"Next steps",
		"Wait for any remaining buffered writes to finish before unplugging the device.",
		"Boot-test the USB on the target hardware and verify the expected menu entries appear.",
	)
	return menuStay, nil
}
