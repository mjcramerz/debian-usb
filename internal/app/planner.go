package app

import (
	"fmt"
	"os"
	"path/filepath"
	"strings"
)

func managedPayloadLayoutSummary(layout string) string {
	switch strings.TrimSpace(layout) {
	case "raw-iso":
		return "stage an ISO9660 payload partition with a managed UEFI redirect to the ESP menu"
	case "iso-store":
		return "store ISO files and copied boot assets on a managed ext4 payload partition"
	case "shared-data":
		return "store selected ISO files on one shared ext4 ISO-store partition behind a removable-path ESP"
	case "extracted":
		return "extract the ISO contents into a managed payload filesystem"
	default:
		return "build a managed payload partition for the detected boot media"
	}
}

func managedPayloadLayoutLabel(layout string) string {
	switch strings.TrimSpace(layout) {
	case "raw-iso":
		return "ISO9660 + managed UEFI redirect"
	case "iso-store":
		return "ISO-store filesystem"
	case "shared-data":
		return "Shared ISO-store partition"
	case "extracted":
		return "Extracted filesystem tree"
	default:
		return blankIfEmpty(layout, "<auto>")
	}
}

func plannedPayloadISOName(isoPath string, sourceRole string) string {
	if strings.TrimSpace(sourceRole) == multiOSSourceRoleNetboot {
		return ""
	}
	normalizedPath := normalizePathInput(isoPath)
	if strings.TrimSpace(normalizedPath) == "" {
		return ""
	}
	info, err := os.Stat(normalizedPath)
	if err != nil {
		return ""
	}
	if info.Mode().IsRegular() {
		if strings.EqualFold(filepath.Ext(normalizedPath), ".iso") {
			return filepath.Base(normalizedPath)
		}
		return ""
	}
	if !info.IsDir() {
		return ""
	}
	if matches, _ := filepath.Glob(filepath.Join(normalizedPath, "payload", "*.iso")); len(matches) == 1 {
		return filepath.Base(matches[0])
	}
	return ""
}

func buildCreatePlan(config RuntimeConfig, req CreateRequest) (CreatePlan, error) {
	spec, ok := profileSpecs[req.Profile]
	if !ok {
		return CreatePlan{}, fmt.Errorf("unsupported profile: %s", req.Profile)
	}
	sourceRole, err := normalizeMultiOSSourceRole(req.SourceRole)
	if err != nil {
		return CreatePlan{}, err
	}
	req.SourceRole = sourceRole
	if !profileSupportsSourceRole(req.Profile, req.SourceRole) {
		return CreatePlan{}, fmt.Errorf("%s does not support source role %s", spec.MenuLabel, req.SourceRole)
	}
	if req.WriteMode != writeModeDirect && req.WriteMode != writeModeManaged {
		return CreatePlan{}, fmt.Errorf("write mode must be direct or managed")
	}
	if req.SourceRole != multiOSSourceRolePrimary && req.WriteMode != writeModeManaged {
		return CreatePlan{}, fmt.Errorf("%s source requires the managed USB workflow", multiOSSourceRoleSummary(req.SourceRole))
	}
	if req.WriteMode == writeModeManaged && !spec.SupportsManaged {
		return CreatePlan{}, fmt.Errorf("%s does not support the managed USB workflow", spec.MenuLabel)
	}
	if req.WriteMode == writeModeDirect && req.Persistence {
		return CreatePlan{}, fmt.Errorf("persistence is only supported with the managed USB workflow")
	}
	if req.WriteMode == writeModeDirect && strings.TrimSpace(req.OfflinePreseedSourceDir) != "" {
		return CreatePlan{}, fmt.Errorf("offline preseed content is only supported with the managed USB workflow")
	}
	if req.WriteMode == writeModeDirect && req.Preseed {
		return CreatePlan{}, fmt.Errorf("preseed menu entries are only supported with the managed USB workflow")
	}
	if req.WriteMode == writeModeManaged {
		rawSecureBootTrust := req.SecureBootTrust
		req.SecureBootTrust = normalizeSecureBootTrustMode(req.SecureBootTrust)
		if req.SecureBootTrust == "" {
			return CreatePlan{}, fmt.Errorf("invalid Secure Boot trust mode: %s", rawSecureBootTrust)
		}
	} else {
		req.SecureBootTrust = ""
	}
	if !req.UseCustomGrubMenu {
		req.PreserveUpstreamGrubEntries = false
	}
	if req.Persistence && !spec.SupportsPersistence {
		return CreatePlan{}, fmt.Errorf("%s does not support persistence", spec.MenuLabel)
	}
	offlinePreseedSourceDir, err := resolveOptionalExistingDir(req.OfflinePreseedSourceDir)
	if err != nil {
		return CreatePlan{}, err
	}
	req.OfflinePreseedSourceDir = offlinePreseedSourceDir

	isoPath, err := validatePlannedSourcePath(req.ISOPath, req.SourceRole, req.Preparation)
	if err != nil {
		return CreatePlan{}, err
	}

	inspection := req.Inspection
	if err := validateManagedSourceSelection(spec, req.SourceRole, inspection, isoPath); err != nil {
		return CreatePlan{}, err
	}
	if req.WriteMode == writeModeManaged && !inspection.ManagedSupported {
		return CreatePlan{}, fmt.Errorf("managed USB mode is not available for this ISO: %s", isoPath)
	}
	if req.Persistence && !inspection.SupportsPersistence {
		return CreatePlan{}, fmt.Errorf("selected ISO does not support persistence: %s", isoPath)
	}
	if len(req.LiveToolGroups) > 0 && (req.SourceRole != multiOSSourceRolePrimary || !isLiveCapableMedia(inspection.MediaClass)) {
		return CreatePlan{}, fmt.Errorf("Live administration tool groups require a primary Live or Hybrid source")
	}
	liveToolGroups, err := validateLiveToolGroupSelection(req.Profile, req.LiveToolGroups)
	if err != nil {
		return CreatePlan{}, err
	}
	if req.WriteMode == writeModeManaged && req.Preseed && !createPreseedEligible(req.Profile, req.SourceRole, req.UseCustomGrubMenu, inspection) {
		return CreatePlan{}, fmt.Errorf("preseed menu entries require Debian/Kali custom GRUB mode with installer-capable media")
	}
	if req.WriteMode != writeModeManaged || !createPreseedEligible(req.Profile, req.SourceRole, req.UseCustomGrubMenu, inspection) {
		req.Preseed = false
	}
	if strings.TrimSpace(req.OfflinePreseedSourceDir) != "" && !req.Preseed {
		return CreatePlan{}, fmt.Errorf("offline preseed content requires enabled preseed menu entries")
	}

	persistenceMode := strings.TrimSpace(req.PersistenceMode)
	if req.Persistence && persistenceMode == persistenceModeNone {
		if req.Profile == profileTails {
			persistenceMode = persistenceModeEncrypted
		} else {
			persistenceMode = persistenceModePlain
		}
	}
	if !req.Persistence {
		persistenceMode = persistenceModeNone
	}
	switch persistenceMode {
	case persistenceModeNone, persistenceModePlain, persistenceModeEncrypted:
	default:
		return CreatePlan{}, fmt.Errorf("persistence mode must be plain or encrypted")
	}
	if req.Profile == profileTails && persistenceMode == persistenceModePlain {
		return CreatePlan{}, fmt.Errorf("Tails persistence must be encrypted")
	}
	if persistenceMode == persistenceModeEncrypted && !inspection.SupportsEncryptedPersistence && !remasterEncryptedPersistenceEligible(spec, req.SourceRole, inspection) {
		return CreatePlan{}, fmt.Errorf("encrypted persistence is not supported for this ISO/profile combination: %s", isoPath)
	}

	notes := []string{
		"Source preparation and downloads run only after build confirmation.",
	}
	if req.LiveToolGroups != nil {
		if len(liveToolGroups) == 0 {
			notes = append(notes, "No optional Live administration tool groups will be added to the source ISO.")
		} else {
			notes = append(notes, fmt.Sprintf("The Live source will be remastered before device writes with these tool groups: %s.", strings.Join(liveToolGroups, ", ")))
		}
	}
	strategy := "hybrid-dd"
	effectiveLayout := effectiveManagedPayloadLayout(config, req.Profile, inspection.ManagedPayloadLayout, req.UseCustomGrubMenu)
	liveSourceRole := req.SourceRole == multiOSSourceRolePrimary && spec.PreferredMedia == "live"
	installerSourceRole := req.SourceRole == multiOSSourceRoleNetinst || req.SourceRole == multiOSSourceRoleNetboot ||
		(req.SourceRole == multiOSSourceRolePrimary && !liveSourceRole && (inspection.MediaClass == "installer" || inspection.MediaClass == "hybrid"))
	if req.SourceRole == multiOSSourceRoleNetinst || req.SourceRole == multiOSSourceRoleNetboot {
		effectiveLayout = "iso-store"
	}
	if req.WriteMode == writeModeDirect {
		notes = append(notes, "The ISO will be written as-is to the whole target disk device to preserve the upstream hybrid boot layout.")
		if installerOnlyMedia(inspection) {
			notes = append(notes,
				"Installer-only/netinst media can also use the managed raw-ISO partition workflow when you want custom GRUB, EFI staging, and preseed entries.",
				"Raw whole-disk hybrid writes may surface upstream ISO block-size warnings from the source image because the ISO is copied byte-for-byte.",
			)
		}
	} else {
		switch {
		case (inspection.MediaClass == "live" || inspection.MediaClass == "hybrid") && req.Persistence:
			if persistenceMode == persistenceModeEncrypted {
				strategy = "encrypted-persistent-managed"
			} else {
				strategy = "persistent-managed"
			}
		case inspection.MediaClass == "installer":
			strategy = "managed-installer"
		default:
			strategy = "managed-live"
		}
		notes = append(notes, fmt.Sprintf("Managed mode will %s instead of dd-writing the whole device directly.", managedPayloadLayoutSummary(effectiveLayout)))
		if req.SourceRole == multiOSSourceRoleNetinst {
			notes = append(notes, "Netinst uses the prepared source's separate hd-media kernel/initrd and one opaque payload ISO; no Live entry or ISO-internal installer kernel is reused.")
		} else if liveSourceRole {
			notes = append(notes, "The Live source role keeps only Live entries from hybrid media; embedded installer entries are ignored.")
		} else if installerSourceRole {
			notes = append(notes, "Installer kernel settings are carried only for the selected installer-capable source role.")
		}
		if inspection.MediaClass == "installer" && effectiveLayout == "raw-iso" {
			notes = append(notes, "Installer-only/netinst managed mode stages the source ISO as its own raw payload partition, injects a UEFI redirect to the managed ESP menu, and stages GRUB, EFI, and USB preseed assets on the separate FAT32 ESP.")
		}
		if effectiveLayout != "" {
			notes = append(notes, fmt.Sprintf("Managed payload layout: %s", managedPayloadLayoutLabel(effectiveLayout)))
		}
		if req.UseCustomGrubMenu {
			notes = append(notes,
				"Custom GRUB mode will group boot entries into Debian, Kali, Tails, and Ubuntu family menus and keep GRUB on the menu until you choose an entry.",
				"Custom GRUB mode replaces the source ISO boot menus with curated GRUB entries derived from the ISO boot assets.",
			)
			if effectiveLayout == "extracted" {
				notes = append(notes, "Custom GRUB mode can stage caller-selected offline preseed content onto the installer-visible payload partition under /preseed/...")
			}
			if effectiveLayout == "raw-iso" {
				notes = append(notes, "Custom GRUB mode keeps the payload as an ISO9660 installer image and injects a UEFI redirect so firmware that boots the payload still lands in the managed installer menu.")
			}
			if effectiveLayout == "iso-store" {
				if req.SourceRole == multiOSSourceRoleNetinst {
					notes = append(notes, "Custom GRUB mode stores the opaque Netinst ISO under /boot/iso/... and the separate hd-media kernel/initrd under /boot/...; the copied initrd makes iso-scan/filename exact and fails instead of scanning sibling ISOs.")
				} else if liveSourceRole {
					notes = append(notes, "Custom GRUB mode stores the opaque Live ISO under /boot/iso/... and loads its kernel/initrd through loopback without copying Live boot assets into the shared root.")
				} else {
					notes = append(notes, "Custom GRUB mode stores ISO files under /boot/iso/... and any role-specific copied boot assets under /boot/....")
				}
			}
			if req.PreserveUpstreamGrubEntries {
				notes = append(notes, "Custom GRUB mode will also render preserved upstream-managed entry trees alongside the curated family menus.")
			}
			if req.Preseed {
				notes = append(notes, "Custom GRUB mode will add deterministic HTTP and USB preseed entries for Debian/Kali installer-capable media.")
			} else if createPreseedEligible(req.Profile, req.SourceRole, req.UseCustomGrubMenu, inspection) {
				notes = append(notes, "Custom GRUB mode can add HTTP and USB preseed entries for this Debian/Kali payload.")
			}
			if strings.TrimSpace(inspection.ManagedPayloadLayout) != effectiveLayout {
				if effectiveLayout == "extracted" {
					notes = append(notes, "Custom GRUB mode switches this profile to an extracted writable payload so the curated GRUB menu fully replaces the source ISO boot configs on the payload.")
				} else if effectiveLayout == "raw-iso" {
					notes = append(notes, "Custom GRUB mode switches this profile back to an ISO9660 payload so installer-capable media boot from a real Debian CD layout while payload UEFI GRUB redirects to the managed menu.")
				} else if effectiveLayout == "iso-store" {
					notes = append(notes, "Custom GRUB mode keeps this profile on the ISO-store layout with role-specific paths instead of exposing one merged installation-media tree.")
				}
			}
		} else if liveSourceRole {
			notes = append(notes, "Standard managed GRUB mode preserves the normalized Live entry tree for this payload and ignores installer entries embedded in hybrid Live media.")
		} else {
			notes = append(notes, "Standard managed GRUB mode will preserve the normalized upstream entry tree for this payload while keeping GRUB on the menu until you choose an entry.")
		}
		if inspection.MediaClass != "" {
			notes = append(notes, fmt.Sprintf("Detected media class: %s", inspection.MediaClass))
		}
		if len(inspection.Firmware) > 0 {
			notes = append(notes, fmt.Sprintf("Detected firmware support: %s", strings.Join(inspection.Firmware, ", ")))
		}
		notes = append(notes, fmt.Sprintf("Secure Boot trust mode: %s", secureBootTrustModeLabel(req.SecureBootTrust)))
		if req.SecureBootTrust == secureBootTrustMOK {
			notes = append(notes, "Managed Secure Boot assets will reuse the persistent MOK store, stage EFI/debian-usb/mok/MOK.der for one-time enrollment, and stage combined Microsoft-compatible db.esl/db.auth for UEFI UpdateVars.")
		} else if req.SecureBootTrust == secureBootTrustFirmwareDB {
			notes = append(notes, "Managed Secure Boot assets will target firmware db trust and stage secureboot/db.cer plus combined Microsoft-compatible db.esl/db.auth for firmware import or UEFI UpdateVars.")
		}
		if len(inspection.TopLevelEntries) > 0 {
			if req.UseCustomGrubMenu {
				notes = append(notes, fmt.Sprintf("Custom GRUB mode replaces these detected source ISO entry groups with curated menu entries: %s", strings.Join(inspection.TopLevelEntries, ", ")))
			} else {
				notes = append(notes, fmt.Sprintf("Managed menu will preserve upstream entry groups: %s", strings.Join(inspection.TopLevelEntries, ", ")))
			}
		}
		if req.Persistence {
			if persistenceMode == persistenceModeEncrypted {
				notes = append(notes, "Encrypted persistence will be stored in a LUKS-backed ext4 partition on the USB.")
			} else {
				notes = append(notes, "Persistence will be stored on a separate ext4 partition on the USB.")
			}
		}
		if strings.TrimSpace(req.KernelArgs) != "" {
			notes = append(notes, "A synthetic primary live entry will use the explicit kernel override you supplied.")
		} else if isLiveCapableMedia(inspection.MediaClass) {
			notes = append(notes, "Managed live entries will preserve the upstream menu tree while inheriting the configured live boot defaults during the managed render.")
			liveSettings := []string{config.DefaultBootPolicy}
			if req.LiveToram {
				toramArg := "toram"
				if spec.LiveBootFamily == "live-boot" {
					toramArg = "toram=filesystem.squashfs"
				}
				liveSettings = append(liveSettings, toramArg)
			}
			if config.DefaultLiveMemGiB > 0 {
				liveSettings = append(liveSettings, fmt.Sprintf("mem=%dG", config.DefaultLiveMemGiB))
			}
			if config.DefaultLiveKernelExtras != "" {
				liveSettings = append(liveSettings, config.DefaultLiveKernelExtras)
			}
			if len(liveSettings) > 0 {
				notes = append(notes, fmt.Sprintf("Configured live kernel defaults will also be applied: %s", strings.Join(liveSettings, " ")))
			}
			if profileExtras := profileConfigValue(config.ProfileLiveKernelExtras, req.Profile); profileExtras != "" {
				notes = append(notes, fmt.Sprintf("%s profile live extras will also be applied: %s", spec.MenuLabel, profileExtras))
			}
		} else if inspection.MediaClass == "installer" {
			notes = append(notes, "Managed installer entries will preserve the upstream installer tree while inheriting the configured installer defaults during the managed render.")
		}
		if installerSourceRole && anyConfiguredValues(config.ProfilePreseedURLs) {
			notes = append(notes, "If installer entries are available, the managed menu will carry the configured per-profile installer URL handling through those entries.")
		}
		if req.OfflinePreseedSourceDir != "" {
			if effectiveLayout == "raw-iso" {
				notes = append(notes, fmt.Sprintf("Offline preseed content from %s will be staged onto the managed ESP at the directory implied by the configured PRESEED_USB_*_FILE path; the raw ISO payload receives only the managed UEFI menu redirect.", req.OfflinePreseedSourceDir))
			} else {
				notes = append(notes, fmt.Sprintf("Offline preseed content from %s will be staged at the directory implied by the configured PRESEED_USB_*_FILE path for this managed payload.", req.OfflinePreseedSourceDir))
			}
			if profileUSBPreseedFile(config, req.Profile) == "" {
				notes = append(notes, "The configured PRESEED_USB_*_FILE value for this profile is empty, so USB preseed entries will still render file= and preseed/file= with empty paths.")
			}
		} else if req.Preseed {
			hostPath := profileHostPreseedPath(config, req.Profile)
			usbFile := profileUSBPreseedFile(config, req.Profile)
			if effectiveLayout == "raw-iso" {
				notes = append(notes, fmt.Sprintf("The writer will copy %s to the managed ESP directory implied by %s when present; missing folders produce a write-time warning and USB preseed entries will not work until populated.", blankIfEmpty(hostPath, "PRESEED_HOST_*_PATH"), blankIfEmpty(usbFile, "PRESEED_USB_*_FILE")))
			} else {
				notes = append(notes, fmt.Sprintf("The writer will copy %s to the directory implied by %s when present; missing folders produce a write-time warning and USB preseed entries will not work until populated.", blankIfEmpty(hostPath, "PRESEED_HOST_*_PATH"), blankIfEmpty(usbFile, "PRESEED_USB_*_FILE")))
			}
		}
		if installerSourceRole && config.DefaultInstallerKernelExtras != "" {
			notes = append(notes, fmt.Sprintf("Shared installer extras will also be applied: %s", config.DefaultInstallerKernelExtras))
		}
		if profileInstallerExtras := profileConfigValue(config.ProfileInstallerKernelExtras, req.Profile); installerSourceRole && profileInstallerExtras != "" {
			notes = append(notes, fmt.Sprintf("%s installer extras will also be applied: %s", spec.MenuLabel, profileInstallerExtras))
		}
		forensicsSettings := make([]string, 0, 2)
		if config.DefaultForensicsKernelExtras != "" {
			forensicsSettings = append(forensicsSettings, config.DefaultForensicsKernelExtras)
		}
		if profileForensicsExtras := profileConfigValue(config.ProfileForensicsKernelExtras, req.Profile); profileForensicsExtras != "" {
			forensicsSettings = append(forensicsSettings, profileForensicsExtras)
		}
		if len(forensicsSettings) > 0 {
			notes = append(notes, fmt.Sprintf("Preserved forensic live entries will also receive configured forensic extras: %s", strings.Join(forensicsSettings, " ")))
		}
		for _, warning := range inspection.Warnings {
			notes = append(notes, "Inspection warning: "+warning)
		}
	}

	menuLabel := strings.TrimSpace(req.MenuLabel)
	if menuLabel == "" && isLiveCapableMedia(inspection.MediaClass) {
		menuLabel = spec.DefaultLiveMenuLabel
	}
	if req.SourceRole != multiOSSourceRolePrimary {
		notes = append(notes, fmt.Sprintf("Source role: %s. The managed flow will stage a prepared installer bundle instead of writing a raw source ISO directly.", multiOSSourceRoleSummary(req.SourceRole)))
	}
	payloadFSLabel, payloadPartLabel := managedPayloadLabels(config, req.Profile, req.SourceRole, inspection.MediaClass)
	persistenceFSLabel, persistencePartLabel := managedPersistenceLabels(config, req.Profile)
	payloadISOName := plannedPayloadISOName(isoPath, req.SourceRole)
	if !req.Persistence {
		persistenceFSLabel = ""
		persistencePartLabel = ""
	}

	return CreatePlan{
		Preparation:                 cloneSourcePreparation(req.Preparation),
		Title:                       fmt.Sprintf("%s USB", spec.MenuLabel),
		Profile:                     req.Profile,
		SourceRole:                  req.SourceRole,
		WriteMode:                   req.WriteMode,
		ISOPath:                     isoPath,
		Strategy:                    strategy,
		MediaClass:                  inspection.MediaClass,
		Firmware:                    inspection.Firmware,
		ManagedSupported:            inspection.ManagedSupported,
		ManagedPayloadLayout:        effectiveLayout,
		SupportsManaged:             spec.SupportsManaged,
		SupportsPersistence:         spec.SupportsPersistence,
		SupportsEncrypted:           inspection.SupportsEncryptedPersistence,
		SecureBootTrust:             req.SecureBootTrust,
		UseCustomGrubMenu:           req.UseCustomGrubMenu,
		PreserveUpstreamGrubEntries: req.PreserveUpstreamGrubEntries,
		Preseed:                     req.Preseed,
		OfflinePreseedSourceDir:     req.OfflinePreseedSourceDir,
		LiveToram:                   req.LiveToram,
		Persistence:                 req.Persistence,
		PersistenceMode:             persistenceMode,
		PersistenceSizeGiB:          req.PersistenceSizeGiB,
		LiveToolGroups:              liveToolGroups,
		DefaultPersistenceSizeGiB:   config.DefaultPersistenceSizeGiB,
		BootPolicy:                  config.DefaultBootPolicy,
		InstallerPolicy:             config.DefaultInstallerPolicy,
		PreseedURL:                  profileConfigValue(config.ProfilePreseedURLs, req.Profile),
		MenuLabel:                   menuLabel,
		KernelArgs:                  strings.TrimSpace(req.KernelArgs),
		KernelPath:                  strings.TrimSpace(req.KernelPath),
		InitrdPath:                  strings.TrimSpace(req.InitrdPath),
		ESPLabel:                    managedESPLabel(config),
		PersistenceFSLabel:          persistenceFSLabel,
		PersistencePartLabel:        persistencePartLabel,
		PayloadFSLabel:              payloadFSLabel,
		PayloadPartLabel:            payloadPartLabel,
		PayloadISOName:              payloadISOName,
		TopLevelEntries:             append([]string{}, inspection.TopLevelEntries...),
		Notes:                       notes,
	}, nil
}

func anyConfiguredValues(values map[string]string) bool {
	for _, value := range values {
		if strings.TrimSpace(value) != "" {
			return true
		}
	}
	return false
}
