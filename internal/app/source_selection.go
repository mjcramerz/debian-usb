package app

import (
	"fmt"
	"net/url"
	"os"
	"path/filepath"
	"strings"
)

// SourceInput is a selected input, not a generated artifact. URL and URLKey are
// both saved so changing configuration cannot silently change a confirmed plan.
// Local file metadata guards against accidental replacement during a long build.
type SourceInput struct {
	Path         string `json:"path,omitempty"`
	URLKey       string `json:"url_key,omitempty"`
	URL          string `json:"url,omitempty"`
	SizeBytes    int64  `json:"size_bytes,omitempty"`
	ModTimeNanos int64  `json:"mod_time_nanos,omitempty"`
}

type SourcePreparation struct {
	ISO                  SourceInput `json:"iso"`
	Kernel               SourceInput `json:"kernel"`
	Initrd               SourceInput `json:"initrd"`
	ExtraModules         []string    `json:"extra_modules,omitempty"`
	ModuleSourceStrategy string      `json:"module_source_strategy,omitempty"`
	InitrdPreseedPath    string      `json:"initrd_preseed_path,omitempty"`
	InitrdOverlayDir     string      `json:"initrd_overlay_dir,omitempty"`
}

func cloneSourcePreparation(p *SourcePreparation) *SourcePreparation {
	if p == nil {
		return nil
	}
	copy := *p
	copy.ExtraModules = append([]string(nil), p.ExtraModules...)
	return &copy
}

func (s SourceInput) display() string {
	if s.Path != "" {
		return s.Path
	}
	return s.URL
}

func localSourceInput(value string, iso bool) (SourceInput, error) {
	path, err := resolveAbsolutePathInput(value)
	if err != nil {
		return SourceInput{}, err
	}
	path, err = filepath.EvalSymlinks(path)
	if err != nil {
		return SourceInput{}, fmt.Errorf("resolve source input: %w", err)
	}
	info, err := os.Stat(path)
	if err != nil {
		return SourceInput{}, err
	}
	if !info.Mode().IsRegular() || info.Size() <= 0 {
		return SourceInput{}, fmt.Errorf("source input must be a non-empty regular file: %s", path)
	}
	if iso && !strings.EqualFold(filepath.Ext(path), ".iso") {
		return SourceInput{}, fmt.Errorf("expected an .iso file: %s", path)
	}
	return SourceInput{Path: path, SizeBytes: info.Size(), ModTimeNanos: info.ModTime().UnixNano()}, nil
}

func (s SourceInput) validate(required, iso bool) error {
	if s.Path == "" && s.URLKey == "" && s.URL == "" {
		if required {
			return fmt.Errorf("a required source input is missing")
		}
		return nil
	}
	if s.Path != "" {
		if s.URLKey != "" || s.URL != "" {
			return fmt.Errorf("source input cannot be both local and remote")
		}
		if !filepath.IsAbs(s.Path) {
			return fmt.Errorf("source input must be absolute: %s", s.Path)
		}
		actual, err := localSourceInput(s.Path, iso)
		if err != nil {
			return err
		}
		if actual.Path != s.Path || (s.SizeBytes != 0 && s.SizeBytes != actual.SizeBytes) || (s.ModTimeNanos != 0 && s.ModTimeNanos != actual.ModTimeNanos) {
			return fmt.Errorf("source changed since selection; select it again: %s", s.Path)
		}
		return nil
	}
	parsed, err := url.Parse(s.URL)
	if err != nil || parsed.Host == "" || (parsed.Scheme != "https" && parsed.Scheme != "http") || s.URLKey == "" {
		return fmt.Errorf("invalid managed source URL: %s", s.URL)
	}
	return nil
}

func (p *SourcePreparation) validate(role string) error {
	if p == nil {
		return nil
	}
	if err := p.ISO.validate(role != multiOSSourceRoleNetboot, true); err != nil {
		return err
	}
	installer := role == multiOSSourceRoleNetinst || role == multiOSSourceRoleNetboot
	if err := p.Kernel.validate(installer, false); err != nil {
		return err
	}
	if err := p.Initrd.validate(installer, false); err != nil {
		return err
	}
	if !installer && (p.Kernel.display() != "" || p.Initrd.display() != "" || len(p.ExtraModules) > 0 || p.InitrdPreseedPath != "") {
		return fmt.Errorf("installer assets/options are not valid for a primary ISO")
	}
	if role == multiOSSourceRoleNetboot && p.ISO.display() != "" {
		return fmt.Errorf("netboot must not include an ISO")
	}
	if p.InitrdOverlayDir != "" {
		if _, err := resolveOptionalExistingDir(p.InitrdOverlayDir); err != nil {
			return err
		}
	}
	if p.InitrdPreseedPath != "" {
		if _, err := localSourceInput(p.InitrdPreseedPath, false); err != nil {
			return err
		}
	}
	return nil
}

func validatePlannedSourcePath(path, role string, preparation *SourcePreparation) (string, error) {
	if preparation != nil {
		if err := preparation.validate(role); err != nil {
			return "", err
		}
		if path != preparation.ISO.Path {
			return "", fmt.Errorf("plan ISO path differs from selected input")
		}
		return path, nil
	}
	return validateManagedSourcePath(path, role)
}

func (a *App) inputFromManagedKey(key string) (SourceInput, error) {
	input := SourceInput{URLKey: key, URL: managedSourceURLValue(a.config, key)}
	return input, input.validate(true, false)
}

func (a *App) promptLocalSource(label string, iso bool) (SourceInput, error) {
	for {
		value, err := a.promptRequiredString(label, "")
		if err != nil {
			return SourceInput{}, err
		}
		input, err := localSourceInput(value, iso)
		if err == nil {
			return input, nil
		}
		fmt.Println(err)
	}
}

// collectSourceSelection performs only reads and prompts. Downloads, bundles,
// remasters and device operations belong exclusively to the execution phase.
func (a *App) collectSourceSelection(spec profileSpec, role string) (*SourcePreparation, menuAction, error) {
	installer := role == multiOSSourceRoleNetinst || role == multiOSSourceRoleNetboot
	p := &SourcePreparation{}
	stableKey := managedISOSourceRoleKey(spec.Key, role, managedSourceReleaseStable)
	testingKey := managedISOSourceRoleKey(spec.Key, role, managedSourceReleaseTesting)
	remoteAvailable := installer || managedSourceURLValue(a.config, stableKey) != "" || managedSourceURLValue(a.config, testingKey) != ""
	sourceMode := "2"
	if remoteAvailable {
		for {
			printSection("Source", infoRow{Label: "Preparation", Value: "After final confirmation"})
			a.printMenu(menuEntry{Key: "1", Label: "Download at build time"}, menuEntry{Key: "2", Label: "Use local assets"}, menuEntry{Key: "b", Label: "Go Back"}, menuEntry{Key: "e", Label: "Exit"})
			choice, err := a.promptChoice("Select the source mode")
			if err != nil {
				return nil, menuStay, err
			}
			if choice == "b" {
				return nil, menuBack, nil
			}
			if choice == "e" {
				return nil, menuExit, nil
			}
			if choice == "1" || choice == "2" {
				sourceMode = choice
				break
			}
			fmt.Println("Invalid selection.")
		}
	}
	var err error
	if sourceMode == "1" {
		channel, action, err := a.promptManagedSourceReleaseChannel(spec.Key)
		if err != nil || action != menuStay {
			return nil, action, err
		}
		if role != multiOSSourceRoleNetboot {
			p.ISO, err = a.inputFromManagedKey(managedISOSourceRoleKey(spec.Key, role, channel))
			if err != nil {
				return nil, menuStay, err
			}
		}
		if installer {
			p.Kernel, err = a.inputFromManagedKey(managedInstallerKernelURLKey(spec.Key, role, channel))
			if err != nil {
				return nil, menuStay, err
			}
			p.Initrd, err = a.inputFromManagedKey(managedInstallerInitrdURLKey(spec.Key, role, channel))
			if err != nil {
				return nil, menuStay, err
			}
		}
	} else {
		a.printDetectedISOs()
		if role != multiOSSourceRoleNetboot {
			p.ISO, err = a.promptLocalSource("ISO path", true)
			if err != nil {
				return nil, menuStay, err
			}
		}
		if installer {
			p.Kernel, err = a.promptLocalSource("Installer kernel path (vmlinuz/linux)", false)
			if err != nil {
				return nil, menuStay, err
			}
			p.Initrd, err = a.promptLocalSource("Installer initrd path (initrd.gz)", false)
			if err != nil {
				return nil, menuStay, err
			}
		}
	}
	if installer {
		var action menuAction
		p.ExtraModules, action, err = a.promptManagedInstallerSourceExtraModules(spec, role, p.Kernel.display(), p.Initrd.display())
		if err != nil || action != menuStay {
			return nil, action, err
		}
		p.InitrdOverlayDir, action, err = a.promptInitrdOverlayContent(spec, role, "")
		if err != nil || action != menuStay {
			return nil, action, err
		}
		// Root-level content, including an optional preseed.cfg, belongs to the
		// selected stage overlay. Do not prompt for a second, overriding preseed.
		// InitrdPreseedPath remains supported for explicit legacy saved plans.
		p.ModuleSourceStrategy, action, err = a.promptManagedInstallerSourceStrategy(spec, role, p.Kernel.display(), p.Initrd.display(), p.ExtraModules)
		if err != nil || action != menuStay {
			return nil, action, err
		}
	}
	return p, menuStay, p.validate(role)
}

func (a *App) inspectSelectedSource(spec profileSpec, role string, p *SourcePreparation) (ISOInspection, error) {
	if role == multiOSSourceRolePrimary && p.ISO.Path != "" {
		return a.backend.InspectSource(spec.Key, p.ISO.Path, role)
	}
	// Not evidence about the input: explicitly mark this as pending. Execution
	// must inspect the downloaded/prepared source before any device writes.
	media := spec.PreferredMedia
	if role != multiOSSourceRolePrimary {
		media = "installer"
	}
	inspection := ISOInspection{Pending: true, ISOPath: p.ISO.Path, MediaClass: media,
		ManagedSupported: spec.SupportsManaged, ManagedPayloadLayout: "iso-store",
		SupportsPersistence: role == multiOSSourceRolePrimary && spec.SupportsPersistence,
		Warnings:            []string{"Source inspection pending; verified before device writes"}}
	if media == "installer" {
		inspection.BestInstallerTitle = "Pending installer inspection"
	}
	return inspection, nil
}
