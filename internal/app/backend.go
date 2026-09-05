package app

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"sync/atomic"
	"time"
)

const sudoCredentialRefreshInterval = 30 * time.Second

type Backend struct {
	configPath                    string
	pythonHelper                  string
	writeHelper                   string
	buildISOHelper                string
	plannedExecutionDir           string
	initrdRoot                    string
	preseedRoot                   string
	effectiveUID                  func() int
	sudoCredentialRefreshInterval time.Duration
	sudoNonInteractive            atomic.Bool
}

func NewBackend() *Backend {
	return &Backend{
		configPath:          envOrRepoOrDefault("DEBIAN_USB_CONFIG", filepath.Join("configs", "debian-usb.conf"), "/etc/debian-usb/debian-usb.conf"),
		pythonHelper:        envOrRepoOrDefault("DEBIAN_USB_PYTHON_HELPER", filepath.Join("scripts", "debian-usb-python"), "/usr/lib/debian-usb/debian-usb-python"),
		writeHelper:         envOrRepoOrDefault("DEBIAN_USB_WRITE_HELPER", filepath.Join("scripts", "write_usb.sh"), "/usr/lib/debian-usb/debian-usb-write"),
		buildISOHelper:      envOrRepoOrDefault("DEBIAN_USB_BUILD_ISO_HELPER", filepath.Join("scripts", "build_iso.sh"), "/usr/lib/debian-usb/debian-usb-build-iso"),
		plannedExecutionDir: envOrDefault("DEBIAN_USB_PLANNED_EXECUTIONS_DIR", "/data/cfg/debian-usb/planned-executions"),
		initrdRoot:          envOrRepoDirOrDefault("DEBIAN_USB_INITRD_DIR", "initrd", "/usr/lib/debian-usb/initrd"),
		preseedRoot:         envOrRepoDirOrDefault("DEBIAN_USB_PRESEED_DIR", filepath.Join("configs", "preseed"), "/usr/lib/debian-usb/preseed"),
	}
}

func envOrDefault(name, fallback string) string {
	if value := os.Getenv(name); value != "" {
		return value
	}
	return fallback
}

func envOrRepoOrDefault(name, repoRelativePath, fallback string) string {
	if value := os.Getenv(name); value != "" {
		return value
	}
	if candidate, ok := repoAssetPath(repoRelativePath); ok {
		return candidate
	}
	return fallback
}

func envOrRepoDirOrDefault(name, repoRelativePath, fallback string) string {
	if value := os.Getenv(name); value != "" {
		return value
	}
	if candidate, ok := repoAssetDirPath(repoRelativePath); ok {
		return candidate
	}
	return fallback
}

func repoAssetPath(repoRelativePath string) (string, bool) {
	candidateRoots := make([]string, 0, 2)
	if cwd, err := os.Getwd(); err == nil && cwd != "" {
		candidateRoots = append(candidateRoots, cwd)
	}
	if executablePath, err := os.Executable(); err == nil && executablePath != "" {
		candidateRoots = append(candidateRoots, filepath.Clean(filepath.Join(filepath.Dir(executablePath), "..")))
	}
	for _, root := range candidateRoots {
		candidate := filepath.Join(root, repoRelativePath)
		info, err := os.Stat(candidate)
		if err == nil && !info.IsDir() {
			return candidate, true
		}
	}
	return "", false
}

func repoAssetDirPath(repoRelativePath string) (string, bool) {
	candidateRoots := make([]string, 0, 2)
	if cwd, err := os.Getwd(); err == nil && cwd != "" {
		candidateRoots = append(candidateRoots, cwd)
	}
	if executablePath, err := os.Executable(); err == nil && executablePath != "" {
		candidateRoots = append(candidateRoots, filepath.Clean(filepath.Join(filepath.Dir(executablePath), "..")))
	}
	for _, root := range candidateRoots {
		candidate := filepath.Join(root, repoRelativePath)
		info, err := os.Stat(candidate)
		if err == nil && info.IsDir() {
			return candidate, true
		}
	}
	return "", false
}

func (b *Backend) ShowConfig() (RuntimeConfig, error) {
	return loadRuntimeConfig(b.configPath)
}

func (b *Backend) SetDefaultPersistenceSize(sizeGiB int) (RuntimeConfig, error) {
	return b.updateConfig(func(cfg *RuntimeConfig) error {
		if sizeGiB <= 0 {
			return errors.New("persistence size must be a positive integer")
		}
		cfg.DefaultPersistenceSizeGiB = sizeGiB
		return nil
	})
}

func (b *Backend) SetDefaultBootPolicy(policy string) (RuntimeConfig, error) {
	return b.updateConfig(func(cfg *RuntimeConfig) error {
		policy = strings.TrimSpace(policy)
		if err := validateLiveBootPolicy(policy); err != nil {
			return err
		}
		cfg.DefaultBootPolicy = policy
		return nil
	})
}

func (b *Backend) SetDefaultInstallerPolicy(policy string) (RuntimeConfig, error) {
	return b.updateConfig(func(cfg *RuntimeConfig) error {
		policy = strings.TrimSpace(policy)
		if err := validateInstallerPolicy(policy); err != nil {
			return err
		}
		cfg.DefaultInstallerPolicy = policy
		return nil
	})
}

func (b *Backend) SetProfilePreseedURL(profile string, url string) (RuntimeConfig, error) {
	return b.updateConfig(func(cfg *RuntimeConfig) error {
		if !supportsProfile(preseedURLProfiles, profile) {
			return fmt.Errorf("installer URLs are not supported for profile: %s", profile)
		}
		normalized, err := normalizeOptionalURLString(url)
		if err != nil {
			return err
		}
		if cfg.ProfilePreseedURLs == nil {
			cfg.ProfilePreseedURLs = make(map[string]string, len(profileOrder))
		}
		cfg.ProfilePreseedURLs[profile] = normalized
		return nil
	})
}

func (b *Backend) SetDefaultLiveToram(enabled bool) (RuntimeConfig, error) {
	return b.updateConfig(func(cfg *RuntimeConfig) error {
		cfg.DefaultLiveToram = enabled
		return nil
	})
}

func (b *Backend) SetDefaultLiveMemGiB(sizeGiB int) (RuntimeConfig, error) {
	return b.updateConfig(func(cfg *RuntimeConfig) error {
		if sizeGiB < 0 {
			return errors.New("live memory limit must be zero or greater")
		}
		cfg.DefaultLiveMemGiB = sizeGiB
		return nil
	})
}

func (b *Backend) SetDefaultLiveKernelExtras(kernelArgs string) (RuntimeConfig, error) {
	return b.updateConfig(func(cfg *RuntimeConfig) error {
		cfg.DefaultLiveKernelExtras = collapseWhitespace(kernelArgs)
		return nil
	})
}

func (b *Backend) SetDefaultInstallerKernelExtras(kernelArgs string) (RuntimeConfig, error) {
	return b.updateConfig(func(cfg *RuntimeConfig) error {
		cfg.DefaultInstallerKernelExtras = collapseWhitespace(kernelArgs)
		return nil
	})
}

func (b *Backend) SetDefaultForensicsKernelExtras(kernelArgs string) (RuntimeConfig, error) {
	return b.updateConfig(func(cfg *RuntimeConfig) error {
		cfg.DefaultForensicsKernelExtras = collapseWhitespace(kernelArgs)
		return nil
	})
}

func (b *Backend) SetProfileLiveKernelExtras(profile string, kernelArgs string) (RuntimeConfig, error) {
	return b.updateConfig(func(cfg *RuntimeConfig) error {
		if !supportsProfile(liveOverrideProfiles, profile) {
			return fmt.Errorf("live kernel extras are not supported for profile: %s", profile)
		}
		if cfg.ProfileLiveKernelExtras == nil {
			cfg.ProfileLiveKernelExtras = make(map[string]string, len(profileOrder))
		}
		cfg.ProfileLiveKernelExtras[profile] = collapseWhitespace(kernelArgs)
		return nil
	})
}

func (b *Backend) SetProfileForensicsKernelExtras(profile string, kernelArgs string) (RuntimeConfig, error) {
	return b.updateConfig(func(cfg *RuntimeConfig) error {
		if !supportsProfile(forensicsOverrideProfiles, profile) {
			return fmt.Errorf("forensics kernel extras are not supported for profile: %s", profile)
		}
		if cfg.ProfileForensicsKernelExtras == nil {
			cfg.ProfileForensicsKernelExtras = make(map[string]string, len(profileOrder))
		}
		cfg.ProfileForensicsKernelExtras[profile] = collapseWhitespace(kernelArgs)
		return nil
	})
}

func (b *Backend) SetProfileInstallerKernelExtras(profile string, kernelArgs string) (RuntimeConfig, error) {
	return b.updateConfig(func(cfg *RuntimeConfig) error {
		if !supportsProfile(installerOverrideProfiles, profile) {
			return fmt.Errorf("installer kernel extras are not supported for profile: %s", profile)
		}
		if cfg.ProfileInstallerKernelExtras == nil {
			cfg.ProfileInstallerKernelExtras = make(map[string]string, len(profileOrder))
		}
		cfg.ProfileInstallerKernelExtras[profile] = collapseWhitespace(kernelArgs)
		return nil
	})
}

func (b *Backend) requiresWritePrivilegesForConfig() bool {
	if os.Geteuid() == 0 {
		return false
	}
	info, err := os.Stat(b.configPath)
	if err == nil && !info.IsDir() {
		file, openErr := os.OpenFile(b.configPath, os.O_WRONLY|os.O_APPEND, 0)
		if openErr == nil {
			_ = file.Close()
			return false
		}
		return true
	}
	parent := filepath.Dir(b.configPath)
	parentInfo, statErr := os.Stat(parent)
	if statErr != nil || !parentInfo.IsDir() {
		return true
	}
	probe, createErr := os.CreateTemp(parent, ".debian-usb-write-probe-*")
	if createErr != nil {
		return true
	}
	probePath := probe.Name()
	_ = probe.Close()
	_ = os.Remove(probePath)
	return false
}

func (b *Backend) updateConfig(mutate func(*RuntimeConfig) error) (RuntimeConfig, error) {
	cfg, err := loadRuntimeConfig(b.configPath)
	if err != nil {
		return RuntimeConfig{}, err
	}
	if err := mutate(&cfg); err != nil {
		return RuntimeConfig{}, err
	}
	if b.requiresWritePrivilegesForConfig() {
		tempPath, err := os.CreateTemp("", "debian-usb-config-*.tmp")
		if err != nil {
			return RuntimeConfig{}, err
		}
		_ = tempPath.Close()
		defer os.Remove(tempPath.Name())
		saved, err := saveRuntimeConfig(tempPath.Name(), cfg)
		if err != nil {
			return RuntimeConfig{}, err
		}
		if err := b.runCommand(true, "cp", "-f", "--", tempPath.Name(), b.configPath); err != nil {
			return RuntimeConfig{}, err
		}
		saved.ConfigPath = b.configPath
		return saved, nil
	}
	return saveRuntimeConfig(b.configPath, cfg)
}

func (b *Backend) InspectISO(profile, isoPath string) (ISOInspection, error) {
	return b.InspectSource(profile, isoPath, multiOSSourceRolePrimary)
}

func (b *Backend) InspectSource(profile, sourcePath, sourceRole string) (ISOInspection, error) {
	var inspection ISOInspection
	args := []string{
		"inspect-iso",
		"--iso-path", sourcePath,
	}
	if profile != "" {
		args = append(args, "--profile", profile)
	}
	if sourceRole != "" {
		args = append(args, "--source-role", sourceRole)
	}
	err := b.runJSON(false, &inspection, args...)
	return inspection, err
}

func (b *Backend) ListLocalISOs() ([]ISOOption, error) {
	var isos []ISOOption
	err := b.runJSON(false, &isos, "list-local-isos")
	return isos, err
}

func (b *Backend) ListDevices() ([]Device, error) {
	var devices []Device
	err := b.runJSON(false, &devices, "list-devices")
	return devices, err
}

func (b *Backend) DownloadManagedSource(key string) (string, error) {
	var payload struct {
		Path string `json:"path"`
	}
	if err := b.runJSON(false, &payload, "download-managed-source", "--key", key, "--config", b.configPath); err != nil {
		return "", err
	}
	if strings.TrimSpace(payload.Path) == "" {
		return "", fmt.Errorf("download-managed-source returned an empty path for %s", key)
	}
	return payload.Path, nil
}

func (b *Backend) PrepareManagedInstallerSource(profile, sourceRole, kernelPath, initrdPath, isoPath string, extraModules []string, moduleSourceStrategy string, initrdPreseedPath string, initrdOverlayDir string) (string, error) {
	var payload struct {
		SourcePath string `json:"source_path"`
	}
	args := []string{
		"prepare-managed-installer-source",
		"--profile", profile,
		"--source-role", sourceRole,
		"--kernel-path", kernelPath,
		"--initrd-path", initrdPath,
	}
	if strings.TrimSpace(isoPath) != "" {
		args = append(args, "--iso-path", isoPath)
	}
	if strings.TrimSpace(moduleSourceStrategy) != "" {
		args = append(args, "--module-source-strategy", moduleSourceStrategy)
	}
	if strings.TrimSpace(initrdPreseedPath) != "" {
		args = append(args, "--initrd-preseed-path", initrdPreseedPath)
	}
	if strings.TrimSpace(initrdOverlayDir) != "" {
		args = append(args, "--initrd-overlay-dir", initrdOverlayDir)
	}
	for _, module := range extraModules {
		module = strings.TrimSpace(module)
		if module == "" {
			continue
		}
		args = append(args, "--extra-module", module)
	}
	// Managed installer bundles are written below the shared download root by
	// default. That tree can be root-owned even when its downloaded inputs are
	// readable by the invoking user, so every material preparation run must use
	// the privileged helper path. The Python helper restores the completed
	// bundle to SUDO_UID/SUDO_GID before returning it.
	if strings.TrimSpace(isoPath) != "" {
		var preflightPayload struct {
			Aligned bool `json:"aligned"`
		}
		preflightArgs := []string{
			"prepare-managed-installer-source",
			"--profile", profile,
			"--source-role", sourceRole,
			"--kernel-path", kernelPath,
			"--initrd-path", initrdPath,
			"--iso-path", isoPath,
			"--preflight-only", "1",
		}
		if strings.TrimSpace(initrdPreseedPath) != "" {
			preflightArgs = append(preflightArgs, "--initrd-preseed-path", initrdPreseedPath)
		}
		if strings.TrimSpace(initrdOverlayDir) != "" {
			preflightArgs = append(preflightArgs, "--initrd-overlay-dir", initrdOverlayDir)
		}
		if err := b.runJSON(false, &preflightPayload, preflightArgs...); err != nil {
			return "", err
		}
	}
	if err := b.runJSON(true, &payload, args...); err != nil {
		return "", err
	}
	if strings.TrimSpace(payload.SourcePath) == "" {
		return "", fmt.Errorf("prepare-managed-installer-source returned an empty source path for %s/%s", profile, sourceRole)
	}
	return payload.SourcePath, nil
}

func (b *Backend) RemasterLiveInitrdSource(profile, sourceISOPath, overlayDir string) (string, error) {
	var payload struct {
		ISOPath string `json:"iso_path"`
	}
	if err := b.runJSON(
		true,
		&payload,
		"remaster-live-initrd-source",
		"--profile", profile,
		"--source-iso", sourceISOPath,
		"--overlay-dir", overlayDir,
	); err != nil {
		return "", err
	}
	if strings.TrimSpace(payload.ISOPath) == "" {
		return "", fmt.Errorf("remaster-live-initrd-source returned an empty ISO path for %s", profile)
	}
	return payload.ISOPath, nil
}

func (b *Backend) RemasterLivePersistenceSource(profile, sourceISOPath string) (string, error) {
	var payload struct {
		ISOPath string `json:"iso_path"`
	}
	if err := b.runJSON(true, &payload, "remaster-live-persistence-source", "--profile", profile, "--source-iso", sourceISOPath); err != nil {
		return "", err
	}
	if strings.TrimSpace(payload.ISOPath) == "" {
		return "", fmt.Errorf("remaster-live-persistence-source returned an empty iso path for %s", profile)
	}
	return payload.ISOPath, nil
}

func (b *Backend) remasterLiveToolsSource(profile, sourceISOPath string, groups []string, requireSudo bool) (string, error) {
	var payload struct {
		ISOPath string `json:"iso_path"`
	}
	args := []string{
		"remaster-live-tools-source",
		"--profile", profile,
		"--source-iso", sourceISOPath,
	}
	for _, group := range groups {
		if strings.TrimSpace(group) == "" {
			return "", fmt.Errorf("Live administration tool group must not be empty")
		}
		args = append(args, "--group", group)
	}
	if groups != nil && len(groups) == 0 {
		args = append(args, "--no-tools")
	}
	if profile == profileDebian {
		args = append(args, "--live-kernel-args", mandatoryDebianLiveHookKernelArgs)
	}
	if err := b.runJSON(requireSudo, &payload, args...); err != nil {
		return "", err
	}
	preparedPath, err := filepath.Abs(normalizePathInput(payload.ISOPath))
	if err != nil {
		return "", fmt.Errorf("resolve remastered Live ISO path: %w", err)
	}
	info, err := os.Stat(preparedPath)
	if err != nil {
		return "", fmt.Errorf("stat remastered Live ISO: %w", err)
	}
	if !info.Mode().IsRegular() || info.Size() <= 0 || !strings.EqualFold(filepath.Ext(preparedPath), ".iso") {
		return "", fmt.Errorf("remaster-live-tools-source returned an invalid ISO path: %s", preparedPath)
	}
	return preparedPath, nil
}

func liveSourceNeedsRemaster(profile string, groups []string) bool {
	switch profile {
	case profileDebian:
		return true
	case profileKaliLinux, profileUbuntuDesktop:
		return groups == nil || len(groups) > 0
	default:
		return false
	}
}

func (b *Backend) ExecuteCreate(plan CreatePlan, devicePath string, persistenceSizeGiB int) error {
	return b.withSudoCredentialLease(func() error {
		return b.executeCreate(plan, devicePath, persistenceSizeGiB, true)
	})
}

func (b *Backend) UpdateCreate(plan CreatePlan, devicePath string, persistenceSizeGiB int) error {
	return b.withSudoCredentialLease(func() error {
		return b.executeCreate(plan, devicePath, persistenceSizeGiB, true, "--update-existing", "1")
	})
}

func (b *Backend) executeCreate(plan CreatePlan, devicePath string, persistenceSizeGiB int, requireSudo bool, extraArgs ...string) error {
	sourceRole := blankIfEmpty(plan.SourceRole, multiOSSourceRolePrimary)
	remasterEligible := sourceRole == multiOSSourceRolePrimary && isLiveCapableMedia(plan.MediaClass)
	if len(plan.LiveToolGroups) > 0 && !remasterEligible {
		return fmt.Errorf("Live administration tool groups require a primary Live or Hybrid source")
	}
	if len(plan.LiveToolGroups) > 0 && !liveSourceSupportsToolRemaster(plan.Profile) {
		return fmt.Errorf("Live administration tool selection is not supported for profile: %s", plan.Profile)
	}
	if remasterEligible && liveSourceNeedsRemaster(plan.Profile, plan.LiveToolGroups) {
		if !liveSourceSupportsToolRemaster(plan.Profile) {
			return fmt.Errorf("Live administration tool selection is not supported for profile: %s", plan.Profile)
		}
		preparedISOPath, err := b.remasterLiveToolsSource(plan.Profile, plan.ISOPath, plan.LiveToolGroups, requireSudo)
		if err != nil {
			return err
		}
		plan.ISOPath = preparedISOPath
	}
	args := []string{
		"--config", b.configPath,
		"--profile", plan.Profile,
		"--source-role", sourceRole,
		"--write-mode", plan.WriteMode,
		"--device", devicePath,
		"--iso", plan.ISOPath,
	}
	if plan.MenuLabel != "" {
		args = append(args, "--menu-label", plan.MenuLabel)
	}
	if plan.KernelArgs != "" {
		args = append(args, "--kernel-args", plan.KernelArgs)
	}
	if plan.KernelPath != "" {
		args = append(args, "--kernel-path", plan.KernelPath)
	}
	if plan.InitrdPath != "" {
		args = append(args, "--initrd-path", plan.InitrdPath)
	}
	if plan.ESPLabel != "" {
		args = append(args, "--esp-label", plan.ESPLabel)
	}
	if plan.PayloadFSLabel != "" {
		args = append(args, "--payload-fs-label", plan.PayloadFSLabel)
	}
	if plan.PayloadPartLabel != "" {
		args = append(args, "--payload-partlabel", plan.PayloadPartLabel)
	}
	if plan.PersistenceFSLabel != "" {
		args = append(args, "--persistence-fs-label", plan.PersistenceFSLabel)
	}
	if plan.PersistencePartLabel != "" {
		args = append(args, "--persistence-partlabel", plan.PersistencePartLabel)
	}
	if plan.WriteMode == writeModeManaged {
		args = append(args, "--live-toram", boolFlag(plan.LiveToram))
		args = append(args, "--use-custom-grub-menu", boolFlag(plan.UseCustomGrubMenu))
		args = append(args, "--preserve-upstream-grub-entries", boolFlag(plan.PreserveUpstreamGrubEntries))
		args = append(args, "--include-preseed", boolFlag(plan.Preseed))
		if plan.OfflinePreseedSourceDir != "" {
			args = append(args, "--offline-preseed-dir", plan.OfflinePreseedSourceDir)
		}
		if plan.SecureBootTrust != "" {
			args = append(args, "--secure-boot-trust", plan.SecureBootTrust)
		}
	}
	if plan.Persistence {
		args = append(args, "--with-persistence")
		if plan.PersistenceMode != "" {
			args = append(args, "--persistence-mode", plan.PersistenceMode)
		}
		if persistenceSizeGiB > 0 {
			args = append(args, "--persistence-size-gib", strconv.Itoa(persistenceSizeGiB))
		}
	}
	args = append(args, extraArgs...)
	return b.runCommand(requireSudo, b.writeHelper, args...)
}

func (b *Backend) ExecuteMultiOSCreate(plan MultiOSPlan, devicePath string) error {
	return b.withSudoCredentialLease(func() error {
		return b.executeMultiOSCreate(plan, devicePath, true)
	})
}

func (b *Backend) UpdateMultiOSCreate(plan MultiOSPlan, devicePath string) error {
	return b.withSudoCredentialLease(func() error {
		return b.executeMultiOSCreate(plan, devicePath, true, "--update-existing", "1")
	})
}

func (b *Backend) executeMultiOSCreate(plan MultiOSPlan, devicePath string, requireSudo bool, extraArgs ...string) error {
	executionPlan := plan
	executionPlan.Items = append([]MultiOSPlanItem(nil), plan.Items...)
	for index := range executionPlan.Items {
		item := &executionPlan.Items[index]
		sourceRole := blankIfEmpty(item.SourceRole, multiOSSourceRolePrimary)
		remasterEligible := sourceRole == multiOSSourceRolePrimary && isLiveCapableMedia(item.MediaClass)
		if len(item.LiveToolGroups) > 0 && !remasterEligible {
			return fmt.Errorf("Live administration tool groups require a primary Live or Hybrid source for %s", item.Title)
		}
		if len(item.LiveToolGroups) > 0 && !liveSourceSupportsToolRemaster(item.Profile) {
			return fmt.Errorf("Live administration tool selection is not supported for profile: %s", item.Profile)
		}
		if !remasterEligible || !liveSourceNeedsRemaster(item.Profile, item.LiveToolGroups) {
			continue
		}
		if !liveSourceSupportsToolRemaster(item.Profile) {
			return fmt.Errorf("Live administration tool selection is not supported for profile: %s", item.Profile)
		}
		preparedISOPath, err := b.remasterLiveToolsSource(item.Profile, item.ISOPath, item.LiveToolGroups, requireSudo)
		if err != nil {
			return err
		}
		item.ISOPath = preparedISOPath
	}
	tempFile, err := os.CreateTemp("", "debian-usb-multios-*.json")
	if err != nil {
		return err
	}
	tempPath := tempFile.Name()
	defer os.Remove(tempPath)
	encoder := json.NewEncoder(tempFile)
	encoder.SetIndent("", "  ")
	if err := encoder.Encode(executionPlan); err != nil {
		_ = tempFile.Close()
		return err
	}
	if err := tempFile.Close(); err != nil {
		return err
	}
	args := []string{
		"--config", b.configPath,
		"--write-mode", writeModeMultiOS,
		"--device", devicePath,
		"--multi-os-plan", tempPath,
		"--live-tools-prepared", "1",
	}
	args = append(args, extraArgs...)
	return b.runCommand(requireSudo, b.writeHelper, args...)
}

func liveSourceSupportsToolRemaster(profile string) bool {
	switch profile {
	case profileDebian, profileKaliLinux, profileUbuntuDesktop:
		return true
	default:
		return false
	}
}

func (b *Backend) EnsureDebianBuildDeps() error {
	var payload map[string]any
	return b.runJSONCommand(true, b.buildISOHelper, &payload, "--ensure-debian-deps")
}

func (b *Backend) EnsureDebianRebuildDeps() error {
	var payload map[string]any
	return b.runJSONCommand(true, b.buildISOHelper, &payload, "--ensure-debian-rebuild-deps")
}

func (b *Backend) BuildDebianISO(plan BuildISOPlan) (BuildISOResult, error) {
	var result BuildISOResult
	tempFile, err := os.CreateTemp("", "debian-usb-build-iso-*.json")
	if err != nil {
		return result, err
	}
	tempPath := tempFile.Name()
	defer os.Remove(tempPath)
	encoder := json.NewEncoder(tempFile)
	encoder.SetIndent("", "  ")
	if err := encoder.Encode(plan); err != nil {
		_ = tempFile.Close()
		return result, err
	}
	if err := tempFile.Close(); err != nil {
		return result, err
	}
	err = b.runJSONCommand(true, b.buildISOHelper, &result, "--plan", tempPath)
	return result, err
}

func (b *Backend) InspectDebianRebuildISO(sourceISOPath string) (RebuildInstallerISOInspection, error) {
	var result RebuildInstallerISOInspection
	args := []string{"inspect-debian-rebuild-source", "--source-iso", sourceISOPath}
	err := b.runJSON(false, &result, args...)
	return result, err
}

func (b *Backend) RebuildDebianInstallerISO(plan RebuildInstallerISOPlan) (RebuildInstallerISOResult, error) {
	var result RebuildInstallerISOResult
	tempFile, err := os.CreateTemp("", "debian-usb-rebuild-installer-iso-*.json")
	if err != nil {
		return result, err
	}
	tempPath := tempFile.Name()
	defer os.Remove(tempPath)
	encoder := json.NewEncoder(tempFile)
	encoder.SetIndent("", "  ")
	if err := encoder.Encode(plan); err != nil {
		_ = tempFile.Close()
		return result, err
	}
	if err := tempFile.Close(); err != nil {
		return result, err
	}
	err = b.runJSONCommand(true, b.buildISOHelper, &result, "--rebuild-installer-plan", tempPath)
	return result, err
}

func (b *Backend) InspectBuildISOKernelEvidence(request BuildISOKernelInspectRequest) (BuildISOKernelInspectResult, error) {
	var result BuildISOKernelInspectResult
	args := []string{"inspect-build-kernel-support"}
	if request.KernelVersion != "" {
		args = append(args, "--kernel-version", request.KernelVersion)
	}
	if request.ModuleTreeDir != "" {
		args = append(args, "--module-tree-dir", request.ModuleTreeDir)
	}
	for _, moduleName := range request.ModuleNames {
		if strings.TrimSpace(moduleName) == "" {
			continue
		}
		args = append(args, "--module", moduleName)
	}
	for _, alias := range request.ModuleAliasCandidates {
		if strings.TrimSpace(alias) == "" {
			continue
		}
		args = append(args, "--module-alias", alias)
	}
	for _, symbol := range request.ConfigSymbols {
		if strings.TrimSpace(symbol) == "" {
			continue
		}
		args = append(args, "--config-symbol", symbol)
	}
	args = append(args, "--download-if-missing", boolFlag(request.DownloadIfMissing))
	err := b.runJSON(request.DownloadIfMissing, &result, args...)
	return result, err
}

func (b *Backend) runJSON(requireSudo bool, output any, args ...string) error {
	return b.runJSONCommand(requireSudo, b.pythonHelper, output, args...)
}

func (b *Backend) runJSONCommand(requireSudo bool, command string, output any, args ...string) error {
	cmdArgs := append([]string{command}, args...)
	cmd, err := b.command(requireSudo, cmdArgs...)
	if err != nil {
		return err
	}
	var stdout bytes.Buffer
	var stderr bytes.Buffer
	cmd.Stdout = &stdout
	cmd.Stderr = io.MultiWriter(os.Stderr, &stderr)
	cmd.Stdin = os.Stdin
	if err := cmd.Run(); err != nil {
		if stderr.Len() > 0 {
			return fmt.Errorf("%s", bytes.TrimSpace(stderr.Bytes()))
		}
		return err
	}
	if err := json.Unmarshal(stdout.Bytes(), output); err != nil {
		return fmt.Errorf("decode helper output: %w", err)
	}
	return nil
}

func (b *Backend) runCommand(requireSudo bool, command string, args ...string) error {
	cmd, err := b.command(requireSudo, append([]string{command}, args...)...)
	if err != nil {
		return err
	}
	var stdout bytes.Buffer
	var stderr bytes.Buffer
	cmd.Stdout = io.MultiWriter(os.Stdout, &stdout)
	cmd.Stderr = io.MultiWriter(os.Stderr, &stderr)
	cmd.Stdin = os.Stdin
	if err := cmd.Run(); err != nil {
		if stderr.Len() > 0 {
			return fmt.Errorf("%s", bytes.TrimSpace(stderr.Bytes()))
		}
		if stdout.Len() > 0 {
			return fmt.Errorf("%s", bytes.TrimSpace(stdout.Bytes()))
		}
		return fmt.Errorf("command %q failed: %w", command, err)
	}
	return nil
}

func (b *Backend) effectiveUserID() int {
	if b.effectiveUID != nil {
		return b.effectiveUID()
	}
	return os.Geteuid()
}

func (b *Backend) sudoRefreshInterval() time.Duration {
	if b.sudoCredentialRefreshInterval > 0 {
		return b.sudoCredentialRefreshInterval
	}
	return sudoCredentialRefreshInterval
}

func (b *Backend) withSudoCredentialLease(action func() error) error {
	if action == nil {
		return errors.New("missing privileged action")
	}
	if b.effectiveUserID() == 0 || b.sudoNonInteractive.Load() {
		return action()
	}
	sudoPath, err := exec.LookPath("sudo")
	if err != nil {
		return errors.New("sudo is required for this action")
	}

	fmt.Fprintln(os.Stderr, "debian-usb: authenticate sudo once; credentials will be refreshed during this operation so later phases cannot prompt again")
	authenticate := exec.Command(sudoPath, "-v")
	authenticate.Stdin = os.Stdin
	authenticate.Stdout = os.Stdout
	authenticate.Stderr = os.Stderr
	if err := authenticate.Run(); err != nil {
		return fmt.Errorf("sudo authentication failed: %w", err)
	}
	if err := refreshSudoCredential(sudoPath); err != nil {
		return fmt.Errorf("sudo credential is unavailable for unattended execution: %w", err)
	}

	b.sudoNonInteractive.Store(true)
	stop := make(chan struct{})
	done := make(chan struct{})
	go keepSudoCredentialAlive(sudoPath, b.sudoRefreshInterval(), stop, done)
	defer func() {
		close(stop)
		<-done
		b.sudoNonInteractive.Store(false)
	}()
	return action()
}

func refreshSudoCredential(sudoPath string) error {
	command := exec.Command(sudoPath, "-n", "-v")
	command.Stdin = nil
	command.Stdout = io.Discard
	var stderr bytes.Buffer
	command.Stderr = &stderr
	if err := command.Run(); err != nil {
		if stderr.Len() > 0 {
			return fmt.Errorf("%s", bytes.TrimSpace(stderr.Bytes()))
		}
		return err
	}
	return nil
}

func keepSudoCredentialAlive(sudoPath string, interval time.Duration, stop <-chan struct{}, done chan<- struct{}) {
	defer close(done)
	ticker := time.NewTicker(interval)
	defer ticker.Stop()
	for {
		select {
		case <-stop:
			return
		case <-ticker.C:
			if refreshSudoCredential(sudoPath) != nil {
				return
			}
		}
	}
}

func (b *Backend) command(requireSudo bool, args ...string) (*exec.Cmd, error) {
	if len(args) == 0 {
		return nil, errors.New("missing command")
	}
	command, commandArgs, err := resolveCommand(args[0], args[1:])
	if err != nil {
		return nil, err
	}
	if requireSudo && b.effectiveUserID() != 0 {
		sudoPath, err := exec.LookPath("sudo")
		if err != nil {
			return nil, errors.New("sudo is required for this action")
		}
		sudoArgs := make([]string, 0, len(commandArgs)+2)
		if b.sudoNonInteractive.Load() {
			sudoArgs = append(sudoArgs, "-n")
		}
		sudoArgs = append(sudoArgs, command)
		commandArgs = append(sudoArgs, commandArgs...)
		command = sudoPath
	}
	return exec.Command(command, commandArgs...), nil
}

func resolveCommand(command string, args []string) (string, []string, error) {
	if command == "" {
		return "", nil, errors.New("missing command")
	}
	if strings.Contains(command, string(os.PathSeparator)) {
		info, err := os.Stat(command)
		if err != nil {
			return "", nil, err
		}
		if info.IsDir() {
			return "", nil, fmt.Errorf("command path is a directory: %s", command)
		}
		if info.Mode()&0111 == 0 {
			shPath, err := exec.LookPath("sh")
			if err != nil {
				return "", nil, errors.New("sh is required to run helper scripts from the repository")
			}
			return shPath, append([]string{command}, args...), nil
		}
	}
	return command, args, nil
}

func boolFlag(value bool) string {
	if value {
		return "1"
	}
	return "0"
}

func supportsProfile(profiles []string, profile string) bool {
	for _, candidate := range profiles {
		if candidate == profile {
			return true
		}
	}
	return false
}
