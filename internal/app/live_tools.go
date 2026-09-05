package app

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
)

const (
	liveToolCatalogSchemaVersion = 2
	liveToolCatalogMaxBytes      = 1024 * 1024
	liveToolCatalogEnv           = "DEBIAN_USB_LIVE_TOOL_PROFILE"
)

var (
	liveToolGroupIDPattern = regexp.MustCompile(`^[a-z0-9][a-z0-9_-]*$`)
	liveToolPackagePattern = regexp.MustCompile(`^[a-z0-9][a-z0-9+.-]*$`)
	liveToolProfilePattern = regexp.MustCompile(`^[a-z0-9][a-z0-9-]*$`)
)

type liveToolPackageGroup struct {
	ID          string   `json:"id"`
	Title       string   `json:"title"`
	Description string   `json:"description"`
	Packages    []string `json:"packages"`
}

type liveToolCatalog struct {
	SchemaVersion     int                    `json:"schema_version"`
	Name              string                 `json:"name"`
	Description       string                 `json:"description"`
	SupportedProfiles []string               `json:"supported_profiles"`
	BuildDistros      map[string]string      `json:"build_distros"`
	PackageGroups     []liveToolPackageGroup `json:"package_groups"`
	CommandPackages   map[string]string      `json:"command_packages"`
	Notes             []string               `json:"notes"`

	path string
}

func loadLiveToolCatalog() (liveToolCatalog, error) {
	path, err := resolveLiveToolCatalogPath()
	if err != nil {
		return liveToolCatalog{}, err
	}
	info, err := os.Stat(path)
	if err != nil {
		return liveToolCatalog{}, fmt.Errorf("stat Live tool catalog: %w", err)
	}
	if !info.Mode().IsRegular() {
		return liveToolCatalog{}, fmt.Errorf("Live tool catalog is not a regular file: %s", path)
	}
	if info.Size() <= 0 || info.Size() > liveToolCatalogMaxBytes {
		return liveToolCatalog{}, fmt.Errorf("Live tool catalog size must be between 1 and %d bytes: %s", liveToolCatalogMaxBytes, path)
	}
	payload, err := os.ReadFile(path)
	if err != nil {
		return liveToolCatalog{}, fmt.Errorf("read Live tool catalog: %w", err)
	}
	var catalog liveToolCatalog
	if err := json.Unmarshal(payload, &catalog); err != nil {
		return liveToolCatalog{}, fmt.Errorf("parse Live tool catalog %s: %w", path, err)
	}
	catalog.path = path
	if err := validateLiveToolCatalog(catalog); err != nil {
		return liveToolCatalog{}, fmt.Errorf("invalid Live tool catalog %s: %w", path, err)
	}
	return catalog, nil
}

func resolveLiveToolCatalogPath() (string, error) {
	if override := strings.TrimSpace(os.Getenv(liveToolCatalogEnv)); override != "" {
		if !filepath.IsAbs(override) {
			return "", fmt.Errorf("%s must be an absolute path: %s", liveToolCatalogEnv, override)
		}
		return filepath.Clean(override), nil
	}
	if cwd, err := os.Getwd(); err == nil {
		for root := filepath.Clean(cwd); ; root = filepath.Dir(root) {
			repoPath := filepath.Join(root, "configs", "spec", "live", "admin-tools.json")
			if info, statErr := os.Stat(repoPath); statErr == nil && info.Mode().IsRegular() {
				return repoPath, nil
			}
			parent := filepath.Dir(root)
			if parent == root {
				break
			}
		}
	}
	if path := managedSpecFilePath(filepath.Join("live", "admin-tools.json")); path != "" {
		return path, nil
	}
	return "", fmt.Errorf("could not locate the Debian-family Live administration tool catalog")
}

func validateLiveToolCatalog(catalog liveToolCatalog) error {
	if catalog.SchemaVersion != liveToolCatalogSchemaVersion {
		return fmt.Errorf("schema_version must be %d", liveToolCatalogSchemaVersion)
	}
	if strings.TrimSpace(catalog.Name) == "" {
		return fmt.Errorf("name must not be empty")
	}
	if len(catalog.SupportedProfiles) == 0 {
		return fmt.Errorf("supported_profiles must not be empty")
	}
	supportedProfiles := make(map[string]struct{}, len(catalog.SupportedProfiles))
	for _, profile := range catalog.SupportedProfiles {
		if !liveToolProfilePattern.MatchString(profile) {
			return fmt.Errorf("invalid supported profile: %s", profile)
		}
		if _, exists := supportedProfiles[profile]; exists {
			return fmt.Errorf("duplicate supported profile: %s", profile)
		}
		supportedProfiles[profile] = struct{}{}
	}
	for distro, profile := range catalog.BuildDistros {
		if !liveToolProfilePattern.MatchString(distro) {
			return fmt.Errorf("invalid build distro: %s", distro)
		}
		if _, exists := supportedProfiles[profile]; !exists {
			return fmt.Errorf("build distro %s references unsupported profile: %s", distro, profile)
		}
	}
	if len(catalog.PackageGroups) == 0 {
		return fmt.Errorf("package_groups must not be empty")
	}
	seenGroups := make(map[string]struct{}, len(catalog.PackageGroups))
	seenPackages := make(map[string]struct{})
	for _, group := range catalog.PackageGroups {
		if !liveToolGroupIDPattern.MatchString(group.ID) {
			return fmt.Errorf("invalid package group id: %s", group.ID)
		}
		if _, exists := seenGroups[group.ID]; exists {
			return fmt.Errorf("duplicate package group id: %s", group.ID)
		}
		seenGroups[group.ID] = struct{}{}
		if strings.TrimSpace(group.Title) == "" || strings.TrimSpace(group.Description) == "" {
			return fmt.Errorf("package group %s requires a title and description", group.ID)
		}
		if len(group.Packages) == 0 {
			return fmt.Errorf("package group %s must not be empty", group.ID)
		}
		groupPackages := make(map[string]struct{}, len(group.Packages))
		for _, packageName := range group.Packages {
			if !liveToolPackagePattern.MatchString(packageName) {
				return fmt.Errorf("invalid package name in group %s: %s", group.ID, packageName)
			}
			if _, exists := groupPackages[packageName]; exists {
				return fmt.Errorf("duplicate package in group %s: %s", group.ID, packageName)
			}
			groupPackages[packageName] = struct{}{}
			seenPackages[packageName] = struct{}{}
		}
	}
	if len(catalog.CommandPackages) == 0 {
		return fmt.Errorf("command_packages must not be empty")
	}
	for command, packageName := range catalog.CommandPackages {
		if strings.TrimSpace(command) == "" {
			return fmt.Errorf("command_packages contains an empty command")
		}
		if _, exists := seenPackages[packageName]; !exists {
			return fmt.Errorf("command %s references a package outside package_groups: %s", command, packageName)
		}
	}
	return nil
}

func (catalog liveToolCatalog) supportsProfile(profile string) bool {
	for _, supportedProfile := range catalog.SupportedProfiles {
		if profile == supportedProfile {
			return true
		}
	}
	return false
}

func (catalog liveToolCatalog) allGroupIDs() []string {
	groupIDs := make([]string, 0, len(catalog.PackageGroups))
	for _, group := range catalog.PackageGroups {
		groupIDs = append(groupIDs, group.ID)
	}
	return groupIDs
}

func (catalog liveToolCatalog) normalizeGroupSelection(groups []string) ([]string, error) {
	available := make(map[string]struct{}, len(catalog.PackageGroups))
	for _, group := range catalog.PackageGroups {
		available[group.ID] = struct{}{}
	}
	requested := make(map[string]struct{}, len(groups))
	for _, groupID := range groups {
		groupID = strings.TrimSpace(groupID)
		if !liveToolGroupIDPattern.MatchString(groupID) {
			return nil, fmt.Errorf("invalid Live tool package group: %s", groupID)
		}
		if _, exists := available[groupID]; !exists {
			return nil, fmt.Errorf("unsupported Live tool package group: %s", groupID)
		}
		requested[groupID] = struct{}{}
	}
	normalized := make([]string, 0, len(requested))
	for _, group := range catalog.PackageGroups {
		if _, selected := requested[group.ID]; selected {
			normalized = append(normalized, group.ID)
		}
	}
	return normalized, nil
}

func validateLiveToolGroupSelection(profile string, groups []string) ([]string, error) {
	if groups == nil {
		return nil, nil
	}
	catalog, err := loadLiveToolCatalog()
	if err != nil {
		return nil, err
	}
	if !catalog.supportsProfile(profile) {
		if len(groups) == 0 {
			return []string{}, nil
		}
		return nil, fmt.Errorf("Live administration tool selection is not supported for profile: %s", profile)
	}
	return catalog.normalizeGroupSelection(groups)
}

func (a *App) promptLiveToolGroups(profile string, current []string) ([]string, menuAction, error) {
	catalog, err := loadLiveToolCatalog()
	if err != nil {
		return nil, menuStay, err
	}
	if !catalog.supportsProfile(profile) {
		return nil, menuStay, nil
	}

	currentGroups := current
	if currentGroups == nil {
		currentGroups = []string{}
	}
	currentGroups, err = catalog.normalizeGroupSelection(currentGroups)
	if err != nil {
		return nil, menuStay, err
	}

	for {
		currentLabel := "Not selected yet"
		if current != nil {
			switch len(currentGroups) {
			case 0:
				currentLabel = "None"
			case len(catalog.PackageGroups):
				currentLabel = "All"
			default:
				currentLabel = fmt.Sprintf("%d of %d groups", len(currentGroups), len(catalog.PackageGroups))
			}
		}

		printHeader("Live Environment Tools")
		printSection(
			"Live-only selection",
			infoRow{Label: "Profile", Value: profile},
			infoRow{Label: "Current selection", Value: currentLabel},
			infoRow{Label: "Scope", Value: "Installed only into the Live root filesystem; Netinst and Netboot are unchanged"},
		)
		printMenu(
			menuEntry{Key: "s", Label: "Select Tools", Detail: "Choose individual Live tool groups"},
			menuEntry{Key: "n", Label: "None", Detail: "Add no optional Live administration tools"},
			menuEntry{Key: "a", Label: "All", Detail: "Include every Live tool group"},
			menuEntry{Key: "b", Label: "Go Back"},
			menuEntry{Key: "e", Label: "Exit"},
		)
		choice, err := a.promptChoice("Choose what to include in the Live environment")
		if err != nil {
			return nil, menuStay, err
		}
		switch choice {
		case "s":
			groups, action, err := a.promptSpecificLiveToolGroups(catalog, currentGroups)
			if err != nil {
				return nil, menuStay, err
			}
			if action == menuBack {
				continue
			}
			return groups, action, nil
		case "n":
			return []string{}, menuStay, nil
		case "a":
			return catalog.allGroupIDs(), menuStay, nil
		case "b":
			return nil, menuBack, nil
		case "e":
			return nil, menuExit, nil
		default:
			fmt.Println("Invalid selection.")
		}
	}
}

func (a *App) promptSpecificLiveToolGroups(catalog liveToolCatalog, current []string) ([]string, menuAction, error) {
	selected := make(map[string]bool, len(catalog.PackageGroups))
	for _, groupID := range current {
		selected[groupID] = true
	}

	for {
		selectedCount := 0
		selectedPackageCount := 0
		seenPackages := make(map[string]struct{})
		for _, group := range catalog.PackageGroups {
			if !selected[group.ID] {
				continue
			}
			selectedCount++
			for _, packageName := range group.Packages {
				if _, exists := seenPackages[packageName]; exists {
					continue
				}
				seenPackages[packageName] = struct{}{}
				selectedPackageCount++
			}
		}

		printHeader("Select Live Environment Tools")
		printSection(
			"Individual tool groups",
			infoRow{Label: "Selected groups", Value: fmt.Sprintf("%d of %d", selectedCount, len(catalog.PackageGroups))},
			infoRow{Label: "Selected packages", Value: strconv.Itoa(selectedPackageCount)},
			infoRow{Label: "Instructions", Value: "Toggle groups by number, then use the selection"},
		)
		entries := make([]menuEntry, 0, len(catalog.PackageGroups)+3)
		for index, group := range catalog.PackageGroups {
			state := "not selected"
			if selected[group.ID] {
				state = "selected"
			}
			entries = append(entries, menuEntry{
				Key:    strconv.Itoa(index + 1),
				Label:  group.Title,
				Detail: fmt.Sprintf("%s (%d packages; %s)", group.Description, len(group.Packages), state),
			})
		}
		entries = append(entries,
			menuEntry{Key: "c", Label: "Use Selection", Detail: "Continue with the displayed individual tool groups"},
			menuEntry{Key: "b", Label: "Go Back", Detail: "Return to Select Tools, None, or All"},
			menuEntry{Key: "e", Label: "Exit"},
		)
		printMenu(entries...)
		choice, err := a.promptChoice("Select an individual Live tool option")
		if err != nil {
			return nil, menuStay, err
		}
		switch choice {
		case "c":
			groups := make([]string, 0, selectedCount)
			for _, group := range catalog.PackageGroups {
				if selected[group.ID] {
					groups = append(groups, group.ID)
				}
			}
			return groups, menuStay, nil
		case "b":
			return nil, menuBack, nil
		case "e":
			return nil, menuExit, nil
		default:
			index, conversionErr := strconv.Atoi(choice)
			if conversionErr != nil || index < 1 || index > len(catalog.PackageGroups) {
				fmt.Println("Invalid selection.")
				continue
			}
			groupID := catalog.PackageGroups[index-1].ID
			selected[groupID] = !selected[groupID]
		}
	}
}

func (a *App) promptBuildISOLiveToolGroups(plan *BuildISOPlan) (menuAction, error) {
	if plan.InstallerMode == buildISOInstallerModeNetinst {
		plan.LiveToolGroups = []string{}
		return menuStay, nil
	}
	catalog, err := loadLiveToolCatalog()
	if err != nil {
		return menuStay, err
	}
	profile := catalog.BuildDistros[strings.TrimSpace(plan.Distro)]
	if profile == "" {
		plan.LiveToolGroups = []string{}
		return menuStay, nil
	}
	groups, action, err := a.promptLiveToolGroups(profile, plan.LiveToolGroups)
	if err != nil || action != menuStay {
		return action, err
	}
	plan.LiveToolGroups = groups
	return menuStay, nil
}
