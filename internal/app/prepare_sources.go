package app

import (
	"fmt"
	"os"
	"path/filepath"
	"strings"
)

// verifyTargetDevice runs before preparation and immediately before invoking the
// writer. A path alone is not identity: USB hotplug can reuse /dev/sdX.
func (b *Backend) verifyTargetDevice(expected *Device, path string) error {
	if expected == nil {
		return nil
	} // legacy saved plans are rebound by the review UI
	if path != expected.Path {
		return fmt.Errorf("target differs from reviewed device; review the plan again")
	}
	devices, err := b.ListDevices()
	if err != nil {
		return fmt.Errorf("recheck target device: %w", err)
	}
	for _, current := range devices {
		if current.Path != path {
			continue
		}
		if current.SystemDisk || !current.Removable {
			return fmt.Errorf("target is no longer an eligible external disk: %s", path)
		}
		if current.SizeBytes != expected.SizeBytes || current.Model != expected.Model || current.Vendor != expected.Vendor ||
			current.Serial != expected.Serial || current.Transport != expected.Transport || current.PTUUID != expected.PTUUID {
			return fmt.Errorf("device identity changed since review: %s; select the device again", path)
		}
		return nil
	}
	return fmt.Errorf("reviewed device is no longer connected: %s", path)
}

func (b *Backend) resolveSourceInput(input SourceInput) (string, error) {
	if input.Path != "" {
		if err := input.validate(true, false); err != nil {
			return "", err
		}
		return input.Path, nil
	}
	if input.URLKey == "" && input.URL == "" {
		return "", nil
	}
	if err := input.validate(true, false); err != nil {
		return "", err
	}
	var result struct {
		Path string `json:"path"`
	}
	err := b.runJSON(false, &result, "download-managed-source", "--config", b.configPath,
		"--key", input.URLKey, "--expected-url", input.URL)
	if err != nil {
		return "", err
	}
	actual, err := localSourceInput(result.Path, false)
	if err != nil {
		return "", fmt.Errorf("invalid downloaded source: %w", err)
	}
	return actual.Path, nil
}

func (b *Backend) materializeSelectedSource(profile, role, path string, p *SourcePreparation) (string, error) {
	if p == nil {
		if splitInstallerProfile(profile, role) {
			var result map[string]any
			if err := b.runJSON(true, &result, "ensure-installer-profiles", "--source-path", path,
				"--profile", profile, "--source-role", role); err != nil {
				return "", err
			}
		}
		return path, nil
	}
	if err := p.validate(role); err != nil {
		return "", err
	}
	iso, err := b.resolveSourceInput(p.ISO)
	if err != nil {
		return "", err
	}
	if role == multiOSSourceRolePrimary {
		return iso, nil
	}
	kernel, err := b.resolveSourceInput(p.Kernel)
	if err != nil {
		return "", err
	}
	initrd, err := b.resolveSourceInput(p.Initrd)
	if err != nil {
		return "", err
	}
	return b.PrepareManagedInstallerSource(profile, role, kernel, initrd, iso,
		p.ExtraModules, p.ModuleSourceStrategy, p.InitrdPreseedPath, p.InitrdOverlayDir)
}

// prepareSelectedSource is the sole source transformation pipeline for create,
// update and saved executions. Live changes are passed together to one remaster.
func (b *Backend) prepareSelectedSource(profile, role, path, media, writeMode string,
	p *SourcePreparation, groups []string, encrypted, requireSudo bool) (string, error) {
	if profile == profileTails && (encrypted || len(groups) > 0 || (p != nil && p.InitrdOverlayDir != "")) {
		return "", fmt.Errorf("Tails requires an unmodified stock ISO; remastering and native Persistent Storage are not supported in managed/multi-OS mode")
	}
	// Revalidate persisted selections before downloads or remastering. A saved
	// pre-scope All selection must not send Kali's wireless group to Debian.
	if writeMode != writeModeDirect {
		validated, err := validateLiveToolGroupSelection(profile, groups)
		if err != nil {
			return "", err
		}
		groups = validated
	}
	materialized, err := b.materializeSelectedSource(profile, role, path, p)
	if err != nil {
		return "", err
	}
	var inspection ISOInspection
	if p != nil {
		inspection, err = b.InspectSource(profile, materialized, role)
		if err != nil {
			return "", err
		}
		if err = validateManagedSourceSelection(profileSpecs[profile], role, inspection, materialized); err != nil {
			return "", err
		}
		if writeMode != writeModeDirect && !inspection.ManagedSupported {
			return "", fmt.Errorf("source does not support managed boot: %s", materialized)
		}
		media = inspection.MediaClass
	}
	// As-is really means as-is, including Debian Live. Never rebuild it.
	if writeMode == writeModeDirect {
		return materialized, nil
	}
	live := role == multiOSSourceRolePrimary && isLiveCapableMedia(media)
	if len(groups) > 0 && (!live || !liveSourceSupportsToolRemaster(profile)) {
		return "", fmt.Errorf("Live tool groups are not supported for %s/%s", profile, role)
	}
	if !live {
		return materialized, nil
	}
	overlay := ""
	if p != nil {
		overlay = p.InitrdOverlayDir
	}
	// Legacy Debian plans also need the mandatory overlay, applied AFTER package
	// installation by the initramfs hook, not to an initrd that will be replaced.
	if overlay == "" && (profile == profileDebian || profile == profileKaliLinux) && b.initrdRoot != "" {
		family := "debian"
		if profile == profileKaliLinux {
			family = "kali"
		}
		overlay = filepath.Join(b.initrdRoot, family, "live")
	}
	needCrypto := encrypted && (p == nil || !inspection.SupportsEncryptedPersistence)
	if needCrypto && !remasterEncryptedPersistenceEligible(profileSpecs[profile], role, ISOInspection{MediaClass: media}) {
		return "", fmt.Errorf("cannot add encrypted persistence support to %s", profile)
	}
	if !liveSourceNeedsRemaster(profile, groups) && overlay == "" && !needCrypto {
		return materialized, nil
	}
	prepared, err := b.remasterLiveSource(profile, materialized, groups, overlay, needCrypto, requireSudo)
	if err != nil {
		return "", err
	}
	if p != nil || encrypted {
		final, err := b.InspectSource(profile, prepared, role)
		if err != nil {
			return "", err
		}
		if err := validateManagedSourceSelection(profileSpecs[profile], role, final, prepared); err != nil {
			return "", err
		}
		if !final.ManagedSupported {
			return "", fmt.Errorf("prepared ISO lost managed boot support: %s", prepared)
		}
		if encrypted && !final.SupportsEncryptedPersistence {
			return "", fmt.Errorf("prepared ISO is missing required cryptsetup/LUKS support: %s", prepared)
		}
	}
	return prepared, nil
}

func (b *Backend) remasterLiveSource(profile, source string, groups []string, overlay string, encrypted, requireSudo bool) (string, error) {
	args := []string{"remaster-live-tools-source", "--profile", profile, "--source-iso", source}
	if !liveSourceSupportsToolRemaster(profile) || (groups != nil && len(groups) == 0) {
		args = append(args, "--no-tools")
	}
	for _, group := range groups {
		if strings.TrimSpace(group) == "" {
			return "", fmt.Errorf("Live administration tool group must not be empty")
		}
		args = append(args, "--group", group)
	}
	if overlay != "" {
		args = append(args, "--overlay-dir", overlay)
	}
	if encrypted {
		args = append(args, "--ensure-encrypted-persistence")
	}
	if profile == profileDebian || profile == profileKaliLinux {
		args = append(args, "--live-kernel-args", mandatoryDebianLiveHookKernelArgs)
	}
	var result struct {
		ISOPath string `json:"iso_path"`
	}
	if err := b.runJSON(requireSudo, &result, args...); err != nil {
		return "", err
	}
	if strings.TrimSpace(result.ISOPath) == "" {
		return "", fmt.Errorf("Live remaster returned no ISO path")
	}
	path, err := filepath.Abs(normalizePathInput(result.ISOPath))
	if err != nil {
		return "", err
	}
	info, err := os.Stat(path)
	if err != nil || !info.Mode().IsRegular() || info.Size() <= 0 || !strings.EqualFold(filepath.Ext(path), ".iso") {
		return "", fmt.Errorf("Live remaster returned an invalid ISO: %s", path)
	}
	return path, nil
}
