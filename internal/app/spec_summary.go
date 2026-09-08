package app

import (
	"fmt"
	"os"
	"path/filepath"
	"strings"
)

const bytesPerMiB int64 = 1024 * 1024

func bytesToMiB(bytes int64) int64 {
	if bytes <= 0 {
		return 0
	}
	return (bytes + bytesPerMiB - 1) / bytesPerMiB
}

func formatBytesAndMiB(bytes int64) string {
	if bytes <= 0 {
		return "<unknown>"
	}
	return fmt.Sprintf("%d bytes (%d MiB)", bytes, bytesToMiB(bytes))
}

func formatMiB(mib int64) string {
	if mib < 0 {
		return "<invalid>"
	}
	return fmt.Sprintf("%d MiB", mib)
}

func statFileSize(path string) (int64, error) {
	info, err := os.Stat(path)
	if err != nil {
		return 0, err
	}
	if !info.Mode().IsRegular() {
		return 0, fmt.Errorf("not a regular file")
	}
	return info.Size(), nil
}

func isoSizeSummary(path string) string {
	size, err := statFileSize(path)
	if err != nil {
		return "stat_error=" + err.Error()
	}
	return formatBytesAndMiB(size)
}

func deviceFullName(device Device) string {
	vendor := strings.TrimSpace(device.Vendor)
	model := strings.TrimSpace(device.Model)
	switch {
	case vendor != "" && model != "":
		if strings.Contains(strings.ToLower(model), strings.ToLower(vendor)) {
			return model
		}
		return vendor + " " + model
	case model != "":
		return model
	case vendor != "":
		return vendor
	default:
		return blankIfEmpty(device.Name, device.Path)
	}
}

func deviceSpecificationRows(device Device) []infoRow {
	size := blankIfEmpty(device.SizeHuman, "<unknown>")
	if device.SizeBytes > 0 {
		size = fmt.Sprintf("%s | %s", size, formatBytesAndMiB(device.SizeBytes))
	}
	mountState := "unmounted"
	if device.Mounted {
		mountState = "mounted"
	}
	return []infoRow{
		{Label: "Node", Value: device.Path},
		{Label: "Kernel name", Value: blankIfEmpty(device.Name, "<unknown>")},
		{Label: "Device name", Value: deviceFullName(device)},
		{Label: "Serial", Value: blankIfEmpty(device.Serial, "<none>")},
		{Label: "Transport", Value: blankIfEmpty(device.Transport, "<unknown>")},
		{Label: "Size", Value: size},
		{Label: "Block UUID", Value: blankIfEmpty(device.UUID, "<none>")},
		{Label: "Partition table UUID", Value: blankIfEmpty(device.PTUUID, "<none>")},
		{Label: "Mount state", Value: mountState},
	}
}

func reviewSaveState(created bool) string {
	return mapBoolLabel(created, "new saved plan created at review", "matching saved plan reused at review")
}

func technicalDeviceSummary(device Device) string {
	size := blankIfEmpty(device.SizeHuman, "<unknown>")
	if device.SizeBytes > 0 {
		size = formatBytesAndMiB(device.SizeBytes)
	}
	mountState := "unmounted"
	if device.Mounted {
		mountState = "mounted"
	}
	return strings.Join([]string{
		"node=" + blankIfEmpty(device.Path, "<unknown>"),
		"name=" + blankIfEmpty(device.Name, "<unknown>"),
		"model=" + deviceFullName(device),
		"serial=" + blankIfEmpty(device.Serial, "<none>"),
		"transport=" + blankIfEmpty(device.Transport, "<unknown>"),
		"size=" + size,
		"mount=" + mountState,
	}, " | ")
}

func savedPlanSummary(savedPlanID string, planState string) string {
	if strings.TrimSpace(savedPlanID) == "" {
		return "<not saved>"
	}
	if strings.TrimSpace(planState) == "" {
		return savedPlanID
	}
	return savedPlanID + " | " + planState
}

func singleTechnicalSpecRows(plan CreatePlan, device Device, spec profileSpec, savedPlanID string, planState string) []infoRow {
	rows := []infoRow{
		{Label: "Write", Value: fmt.Sprintf("single-os | profile=%s | mode=%s | strategy=%s | overwrite=yes", spec.MenuLabel, plan.WriteMode, plan.Strategy)},
		{Label: "Target USB", Value: technicalDeviceSummary(device)},
		{Label: "Source ISO", Value: fmt.Sprintf("file=%s | size=%s | path=%s", filepath.Base(plan.ISOPath), isoSizeSummary(plan.ISOPath), plan.ISOPath)},
		{Label: "Media", Value: fmt.Sprintf("class=%s | firmware=%s | managed=%s", blankIfEmpty(plan.MediaClass, "<unknown>"), joinOrNone(plan.Firmware), mapBoolLabel(plan.ManagedSupported, "yes", "no"))},
		{Label: "USB Layout", Value: singleUSBLayoutSummary(plan, device)},
		{Label: "Boot/GRUB", Value: singleBootloaderSummary(plan)},
		{Label: "Entries", Value: singleEntriesSummary(plan)},
		{Label: "Automation", Value: singleAutomationSummary(plan)},
		{Label: "Plan/Confirm", Value: fmt.Sprintf("saved=%s | destructive=yes | confirm target=%s", savedPlanSummary(savedPlanID, planState), blankIfEmpty(device.Path, "<unknown>"))},
	}
	return rows
}

func singleUSBLayoutSummary(plan CreatePlan, device Device) string {
	espLabel := createPlanESPLabel(plan)
	payloadFSLabel, _ := createPlanPayloadLabels(plan)
	if plan.WriteMode == writeModeDirect {
		return fmt.Sprintf("direct hybrid image: dd ISO -> %s whole disk; no partition target", blankIfEmpty(device.Path, "<unknown>"))
	}
	if singleUsesSharedISOStoreLayout(plan) {
		if splitInstallerProfile(plan.Profile, plan.SourceRole) {
			return fmt.Sprintf("GPT: p3 BIOSBOOT 1-3MiB | p1 ESP FAT32 %s 3-515MiB | p2 %s ext4 515MiB-end | separate Desktop/Server boot assets and opt-in preseed folders", espLabel, payloadFSLabel)
		}
		return fmt.Sprintf("GPT: BIOSBOOT 1-3MiB | ESP FAT32 %s 3-515MiB | %s ext4 515MiB-end | payload=/boot/iso + /boot assets", espLabel, payloadFSLabel)
	}
	layout := fmt.Sprintf("GPT: ESP FAT32 %s first 512MiB | managed payload ext4 %s", espLabel, payloadFSLabel)
	if singlePayloadPartitionPrecedesESP(plan) {
		if plan.ManagedPayloadLayout == "raw-iso" {
			layout = fmt.Sprintf("GPT: raw ISO payload %s first | ESP FAT32 %s next 512MiB", payloadFSLabel, espLabel)
		} else {
			layout = fmt.Sprintf("GPT: managed payload %s first | ESP FAT32 %s next 512MiB", payloadFSLabel, espLabel)
		}
	} else if plan.ManagedPayloadLayout == "raw-iso" {
		layout = fmt.Sprintf("GPT: ESP FAT32 %s first 512MiB | raw ISO payload %s next", espLabel, payloadFSLabel)
	}
	if plan.Persistence {
		layout += fmt.Sprintf(" + persistence %s %dGiB", blankIfEmpty(plan.PersistenceMode, persistenceModePlain), plan.PersistenceSizeGiB)
	}
	if plan.Preseed {
		if singlePayloadPartitionPrecedesESP(plan) {
			layout += " | USB preseed on ESP:/preseed/<os>"
		} else {
			layout += " | USB preseed on PAYLOAD:/preseed/<os>"
		}
	}
	return layout + " | payload_layout=" + blankIfEmpty(plan.ManagedPayloadLayout, "<auto>")
}

func createPlanESPLabel(plan CreatePlan) string {
	return blankIfEmpty(plan.ESPLabel, "<unset>")
}

func createPlanPayloadLabels(plan CreatePlan) (string, string) {
	fsLabel := blankIfEmpty(plan.PayloadFSLabel, "PAYLOAD")
	partLabel := blankIfEmpty(plan.PayloadPartLabel, fsLabel)
	return fsLabel, partLabel
}

func createPlanPersistenceLabels(plan CreatePlan) (string, string) {
	fsLabel := strings.TrimSpace(plan.PersistenceFSLabel)
	partLabel := strings.TrimSpace(plan.PersistencePartLabel)
	if fsLabel == "" && partLabel == "" {
		return "", ""
	}
	if fsLabel == "" {
		fsLabel = partLabel
	}
	if partLabel == "" {
		partLabel = fsLabel
	}
	return fsLabel, partLabel
}

func multiOSPlanESPLabel(plan MultiOSPlan) string {
	return blankIfEmpty(plan.ESPLabel, "<unset>")
}

func singleBootloaderSummary(plan CreatePlan) string {
	if plan.WriteMode == writeModeDirect {
		return "upstream hybrid ISO boot sectors and upstream boot menu are preserved"
	}
	menu := grubMenuModeLabel(plan.UseCustomGrubMenu, plan.PreserveUpstreamGrubEntries)
	trust := secureBootTrustModeTechnicalLabel(plan.SecureBootTrust)
	if singleUsesSharedISOStoreLayout(plan) {
		payloadFSLabel, _ := createPlanPayloadLabels(plan)
		return "BIOS i386-pc + UEFI removable x86_64-efi | grub.cfg=/boot/grub/grub.cfg on " + payloadFSLabel + " | secureboot=" + trust + " | menu=" + menu
	}
	if plan.ManagedPayloadLayout == "raw-iso" {
		return "UEFI removable x86_64-efi | grub.cfg=ESP:/boot/grub/grub.cfg | raw payload UEFI redirects to ESP menu | secureboot=" + trust + " | menu=" + menu
	}
	return "UEFI removable x86_64-efi | grub.cfg=ESP:/boot/grub/grub.cfg | secureboot=" + trust + " | menu=" + menu
}

func singleEntriesSummary(plan CreatePlan) string {
	if plan.WriteMode == writeModeDirect {
		return "upstream ISO entries only"
	}
	entrySummary := grubBootEntriesSummary(plan.UseCustomGrubMenu, plan.PreserveUpstreamGrubEntries)
	if len(plan.TopLevelEntries) > 0 {
		entrySummary += " | detected=" + compactStringList(plan.TopLevelEntries, 4)
	}
	if plan.MenuLabel != "" {
		entrySummary += " | primary_label=" + plan.MenuLabel
	}
	if plan.KernelPath != "" || plan.InitrdPath != "" {
		entrySummary += " | overrides="
		entrySummary += "kernel:" + blankIfEmpty(plan.KernelPath, "auto")
		entrySummary += ",initrd:" + blankIfEmpty(plan.InitrdPath, "auto")
	}
	return entrySummary
}

func singleAutomationSummary(plan CreatePlan) string {
	if plan.WriteMode == writeModeDirect {
		if len(plan.LiveToolGroups) > 0 {
			return "live_tools=" + compactStringList(plan.LiveToolGroups, 5) + "; source ISO is remastered before the otherwise unchanged direct write"
		}
		return "none; direct write does not add persistence, preseed, GRUB, or kernel argument changes"
	}
	preseed := "off"
	if plan.Preseed {
		preseed = "http+usb"
	}
	if splitInstallerProfile(plan.Profile, plan.SourceRole) {
		preseed = "Desktop/Server: https+http+initrd+usb; HD-MEDIA " + hdMediaPreseedSummary(plan.HDMediaPreseedDirs)
	}
	if plan.PreseedURL != "" {
		preseed += " url=" + plan.PreseedURL
	}
	if plan.OfflinePreseedSourceDir != "" {
		preseed += " offline_dir=" + plan.OfflinePreseedSourceDir
	}
	persistence := "off"
	if plan.Persistence {
		persistence = fmt.Sprintf("%s %dGiB", blankIfEmpty(plan.PersistenceMode, persistenceModePlain), plan.PersistenceSizeGiB)
	}
	parts := []string{
		"preseed=" + preseed,
		"persistence=" + persistence,
		"secureboot=" + secureBootTrustModeTechnicalLabel(plan.SecureBootTrust),
		"toram=" + onOffLabel(plan.LiveToram),
		"live_policy=" + blankIfEmpty(plan.BootPolicy, "<none>"),
		"installer_policy=" + blankIfEmpty(plan.InstallerPolicy, "<none>"),
	}
	if len(plan.LiveToolGroups) > 0 {
		parts = append(parts, "live_tools="+compactStringList(plan.LiveToolGroups, 5))
	}
	return strings.Join(parts, " | ")
}

func multiOSTechnicalSpecRows(plan MultiOSPlan, device Device, savedPlanID string, planState string) []infoRow {
	bootSummary := "UEFI removable x86_64-efi | grub.cfg=ESP:/boot/grub/grub.cfg | raw payload UEFI redirects to ESP menu | secureboot=" + secureBootTrustModeTechnicalLabel(plan.SecureBootTrust) + " | menu=" + grubMenuModeLabel(plan.UseCustomGrubMenu, plan.PreserveUpstreamGrubEntries)
	if multiOSUsesSharedISOStoreLayout(plan) {
		bootSummary = "UEFI removable x86_64-efi | grub.cfg=ESP:/boot/grub/grub.cfg | exact ISO selection from p2 | secureboot=" + secureBootTrustModeTechnicalLabel(plan.SecureBootTrust) + " | menu=" + grubMenuModeLabel(plan.UseCustomGrubMenu, plan.PreserveUpstreamGrubEntries)
	}
	return []infoRow{
		{Label: "Write", Value: fmt.Sprintf("multi-os | sources=%d | mode=%s | overwrite=yes", len(plan.Items), plan.WriteMode)},
		{Label: "Target USB", Value: technicalDeviceSummary(device)},
		{Label: "Source ISOs", Value: multiOSSourceSummary(plan)},
		{Label: "USB Layout", Value: multiOSLayoutSummary(plan)},
		{Label: "Boot/GRUB", Value: bootSummary},
		{Label: "OS Entries", Value: compactStringList(multiOSItemTitles(plan.Items), 6)},
		{Label: "Automation", Value: multiOSAutomationSummary(plan)},
		{Label: "Plan/Confirm", Value: fmt.Sprintf("saved=%s | destructive=yes | confirm target=%s", savedPlanSummary(savedPlanID, planState), blankIfEmpty(device.Path, "<unknown>"))},
	}
}

func multiOSHasPersistence(plan MultiOSPlan) bool {
	for _, item := range plan.Items {
		if item.Persistence {
			return true
		}
	}
	return false
}

func multiOSUsesSharedISOStoreLayout(plan MultiOSPlan) bool {
	if len(plan.Items) == 0 {
		return false
	}
	for _, item := range plan.Items {
		if strings.TrimSpace(item.ManagedPayloadLayout) != "shared-data" {
			return false
		}
	}
	return true
}

func multiOSLayoutSummary(plan MultiOSPlan) string {
	espLabel := multiOSPlanESPLabel(plan)
	if multiOSUsesSharedISOStoreLayout(plan) {
		if multiOSHasPersistence(plan) {
			return fmt.Sprintf("GPT: p1 FAT32 %s as the removable ESP | p2 ext4 %s as the ISO store | p3+ one persistence partition per enabled Live source", espLabel, blankIfEmpty(plan.DataFSLabel, "<unset>"))
		}
		return fmt.Sprintf("GPT: p1 FAT32 %s as the removable ESP | p2 ext4 %s as the ISO store", espLabel, blankIfEmpty(plan.DataFSLabel, "<unset>"))
	}
	if multiOSHasPersistence(plan) {
		return fmt.Sprintf("GPT: raw ISO payload partitions with netinst/installer sources first, then live sources | ESP FAT32 %s after payloads for GRUB + /preseed | appended persistence partitions per enabled live source", espLabel)
	}
	return fmt.Sprintf("GPT: raw ISO payload partitions with netinst/installer sources first, then live sources | ESP FAT32 %s after payloads for GRUB + /preseed", espLabel)
}

func multiOSSourceSummary(plan MultiOSPlan) string {
	totalBytes := int64(0)
	parts := make([]string, 0, len(plan.Items))
	for _, item := range plan.Items {
		if size, err := statFileSize(item.ISOPath); err == nil {
			totalBytes += size
		}
		preseed := "off"
		if item.Preseed {
			preseed = "http+usb"
		}
		if splitInstallerProfile(item.Profile, item.SourceRole) {
			preseed = "Desktop/Server:https+http+initrd+usb"
		}
		parts = append(parts, fmt.Sprintf("%s[%s,%s,%s,preseed=%s]=%s", item.Title, blankIfEmpty(item.SourceRole, multiOSSourceRolePrimary), blankIfEmpty(item.MediaClass, "<unknown>"), managedPayloadLayoutLabel(item.ManagedPayloadLayout), preseed, item.ISOPath))
	}
	total := "<unknown>"
	if totalBytes > 0 {
		total = formatBytesAndMiB(totalBytes)
	}
	return fmt.Sprintf("count=%d | total_iso_size=%s | %s", len(plan.Items), total, compactStringList(parts, 4))
}

func multiOSAutomationSummary(plan MultiOSPlan) string {
	preseed := make([]string, 0)
	persistence := make([]string, 0)
	liveTools := make([]string, 0)
	hdMedia := make([]string, 0)
	for _, item := range plan.Items {
		if item.Preseed || splitInstallerProfile(item.Profile, item.SourceRole) {
			preseed = append(preseed, item.Title)
		}
		if splitInstallerProfile(item.Profile, item.SourceRole) {
			hdMedia = append(hdMedia, item.Title+": "+hdMediaPreseedSummary(item.HDMediaPreseedDirs))
		}
		if item.Persistence {
			persistence = append(persistence, fmt.Sprintf("%s:%s %dGiB", item.Title, blankIfEmpty(item.PersistenceMode, persistenceModePlain), item.PersistenceSizeGiB))
		}
		if len(item.LiveToolGroups) > 0 {
			liveTools = append(liveTools, fmt.Sprintf("%s:%s", item.Title, compactStringList(item.LiveToolGroups, 3)))
		}
	}
	parts := []string{
		"preseed=" + compactStringListOrOff(preseed, 4),
		"persistence=" + compactStringListOrOff(persistence, 4),
		"secureboot=" + secureBootTrustModeTechnicalLabel(plan.SecureBootTrust),
		"default_preseed_sources=PRESEED_HOST_{DEBIAN,KALI,PURPLE}_PATH",
		"preseed_stage=dirname(PRESEED_USB_*_FILE)",
		"payloads=" + mapBoolLabel(multiOSUsesSharedISOStoreLayout(plan), "ISO store on p2", "raw-iso partitions per source"),
	}
	if len(hdMedia) > 0 {
		parts[3] = "installer_preseed_sources=explicit consent only (HOST paths are prompt defaults)"
		parts[4] = "installer_preseed_stage=p2:/<distro>-preseed-{de,srv}"
		parts = append(parts, "HD-MEDIA="+strings.Join(hdMedia, "; "))
	}
	if len(liveTools) > 0 {
		parts = append(parts, "live_tools="+compactStringList(liveTools, 4))
	}
	return strings.Join(parts, " | ")
}

func plannedExecutionTechnicalSpecRows(execution PlannedExecution, device Device) []infoRow {
	switch {
	case execution.SinglePlan != nil:
		spec := profileSpecs[execution.SinglePlan.Profile]
		return singleTechnicalSpecRows(*execution.SinglePlan, device, spec, execution.RunID, "saved planned execution")
	case execution.MultiOSPlan != nil:
		return multiOSTechnicalSpecRows(*execution.MultiOSPlan, device, execution.RunID, "saved planned execution")
	default:
		return []infoRow{
			{Label: "Plan/Confirm", Value: fmt.Sprintf("saved=%s | kind=%s | target=%s | missing executable plan", execution.RunID, execution.Kind, blankIfEmpty(device.Path, "<unknown>"))},
		}
	}
}

func compactStringList(values []string, limit int) string {
	filtered := make([]string, 0, len(values))
	for _, value := range values {
		value = strings.TrimSpace(value)
		if value != "" {
			filtered = append(filtered, value)
		}
	}
	if len(filtered) == 0 {
		return "<none>"
	}
	if limit <= 0 || len(filtered) <= limit {
		return strings.Join(filtered, "; ")
	}
	return strings.Join(filtered[:limit], "; ") + fmt.Sprintf("; +%d more", len(filtered)-limit)
}

func compactStringListOrOff(values []string, limit int) string {
	if len(values) == 0 {
		return "off"
	}
	return compactStringList(values, limit)
}

func singleISOManifestRows(plan CreatePlan) []infoRow {
	rows := []infoRow{
		{Label: "Profile", Value: plan.Profile},
		{Label: "ISO path", Value: plan.ISOPath},
		{Label: "ISO file", Value: filepath.Base(plan.ISOPath)},
		{Label: "ISO size", Value: isoSizeSummary(plan.ISOPath)},
	}
	if plan.Preseed {
		rows = append(rows, infoRow{Label: "Preseed entries", Value: "online=1 offline=1"})
	}
	if plan.OfflinePreseedSourceDir != "" {
		rows = append(rows, infoRow{Label: "Offline preseed dir", Value: plan.OfflinePreseedSourceDir})
	}
	if splitInstallerProfile(plan.Profile, plan.SourceRole) {
		rows = append(rows, infoRow{Label: "HD-MEDIA copying", Value: hdMediaPreseedSummary(plan.HDMediaPreseedDirs)})
	}
	return rows
}

func multiOSISOManifestRows(plan MultiOSPlan) []infoRow {
	rows := make([]infoRow, 0, len(plan.Items))
	for _, item := range plan.Items {
		preseed := "0"
		if item.Preseed {
			preseed = "online=1 offline=1"
		}
		if splitInstallerProfile(item.Profile, item.SourceRole) {
			preseed = "Desktop/Server: https+http+initrd+usb"
		}
		value := fmt.Sprintf(
			"id=%s | role=%s | profile=%s | media=%s | file=%s | size=%s | preseed=%s",
			item.ID,
			item.SourceRole,
			item.Profile,
			blankIfEmpty(item.MediaClass, "<unknown>"),
			item.ISOPath,
			isoSizeSummary(item.ISOPath),
			preseed,
		)
		if item.OfflinePreseedSourceDir != "" {
			value += " | offline_preseed_dir=" + item.OfflinePreseedSourceDir
		}
		if splitInstallerProfile(item.Profile, item.SourceRole) {
			value += " | HD-MEDIA=" + hdMediaPreseedSummary(item.HDMediaPreseedDirs)
		}
		rows = append(rows, infoRow{Label: item.Title, Value: value})
	}
	return rows
}

func partitionSpecValue(number int, role, fs, label string, startMiB, endMiB int64) string {
	return fmt.Sprintf(
		"number=%d | start_mib=%d | end_mib=%d | size=%s | fs=%s | label=%s | role=%s",
		number,
		startMiB,
		endMiB,
		formatMiB(endMiB-startMiB),
		fs,
		blankIfEmpty(label, "<none>"),
		role,
	)
}

func singlePartitionSpecificationRows(plan CreatePlan, device Device) []infoRow {
	isoSize, _ := statFileSize(plan.ISOPath)
	payloadFSLabel, payloadPartLabel := createPlanPayloadLabels(plan)
	if plan.WriteMode == writeModeDirect {
		return []infoRow{
			{Label: "disk-image", Value: fmt.Sprintf("target=%s | writer=dd | input_iso_size=%s | target_size=%s", device.Path, formatBytesAndMiB(isoSize), formatBytesAndMiB(device.SizeBytes))},
		}
	}
	if singleUsesSharedISOStoreLayout(plan) {
		if splitInstallerProfile(plan.Profile, plan.SourceRole) {
			return []infoRow{
				{Label: "p1 ESP", Value: "number=1 | start_mib=3 | end_mib=<3+runtime-ESP-size> | fs=vfat | label=" + createPlanESPLabel(plan)},
				{Label: "p2 DATA", Value: "number=2 | start_mib=<after-ESP> | end_mib=<device-end> | fs=ext4 | label=" + payloadFSLabel + " | Desktop/Server installer assets and optional full preseed trees"},
				{Label: "p3 BIOSBOOT", Value: "number=3 | start_mib=1 | end_mib=3 | role=BIOS GRUB core"},
			}
		}
		return sharedISOStorePartitionSpecificationRows(device, "single", createPlanESPLabel(plan), payloadFSLabel)
	}
	deviceMiB := bytesToMiB(device.SizeBytes)
	payloadBeforeESP := singlePayloadPartitionPrecedesESP(plan)
	espLabel := createPlanESPLabel(plan)
	if deviceMiB == 0 {
		if payloadBeforeESP {
			return []infoRow{
				{Label: "p1 PAYLOAD", Value: fmt.Sprintf("number=1 | start_mib=1 | end_mib=<iso-sized> | fs=raw-iso9660 | label=%s | role=raw ISO payload", payloadPartLabel)},
				{Label: "p2 ESP", Value: fmt.Sprintf("number=2 | start_mib=<after-payload> | end_mib=<after-payload+512> | size=512 MiB | fs=vfat | label=%s | role=uefi-removable + /preseed/<os>", espLabel)},
			}
		}
		return []infoRow{
			{Label: "p1 ESP", Value: fmt.Sprintf("number=1 | start_mib=1 | end_mib=513 | size=512 MiB | fs=vfat | label=%s | role=uefi-removable", espLabel)},
			{Label: "p2 PAYLOAD", Value: fmt.Sprintf("number=2 | start_mib=513 | end_mib=<device-dependent> | fs=ext4 | label=%s | role=managed payload", payloadFSLabel)},
		}
	}
	usableEnd := deviceMiB - 8
	if payloadBeforeESP {
		payloadStart := int64(1)
		payloadEnd := payloadStart + bytesToMiB(isoSize)
		espStart := payloadEnd
		espEnd := espStart + 512
		rows := []infoRow{
			{Label: "p1 PAYLOAD", Value: partitionSpecValue(1, "raw ISO payload", "raw-iso9660", payloadPartLabel, payloadStart, payloadEnd)},
			{Label: "p2 ESP", Value: partitionSpecValue(2, "uefi-removable + /preseed/<os>", "vfat", espLabel, espStart, espEnd)},
		}
		if plan.OfflinePreseedSourceDir != "" {
			rows = append(rows, infoRow{Label: "sizing basis", Value: "source_iso_stat=1 | offline_preseed_rebuild=runtime"})
		}
		if plan.Persistence {
			persistStart := espEnd
			persistEnd := persistStart + int64(plan.PersistenceSizeGiB)*1024
			fsLabel, partLabel := createPlanPersistenceLabels(plan)
			fs := "ext4"
			if plan.PersistenceMode == persistenceModeEncrypted {
				fs = "luks2/ext4"
			}
			rows = append(rows, infoRow{Label: "p3 PERSISTENCE", Value: partitionSpecValue(3, "persistence", fs, blankIfEmpty(fsLabel, partLabel), persistStart, persistEnd)})
		}
		rows = append(rows, infoRow{Label: "tail guard", Value: fmt.Sprintf("start_mib=%d | end_mib=%d | size=%s | role=partition-table slack", usableEnd, deviceMiB, formatMiB(deviceMiB-usableEnd))})
		return rows
	}
	espStart := int64(1)
	espEnd := espStart + 512
	payloadStart := espEnd
	rows := []infoRow{
		{Label: "p1 ESP", Value: partitionSpecValue(1, "uefi-removable", "vfat", espLabel, espStart, espEnd)},
	}
	if plan.OfflinePreseedSourceDir != "" {
		rows = append(rows, infoRow{Label: "sizing basis", Value: "source_iso_stat=1 | offline_preseed_rebuild=runtime"})
	}
	if usableEnd <= payloadStart {
		rows = append(rows, infoRow{Label: "layout_error", Value: fmt.Sprintf("device_mib=%d leaves no payload span after ESP", deviceMiB)})
		return rows
	}
	payloadEnd := usableEnd
	if plan.Persistence {
		persistSize := int64(plan.PersistenceSizeGiB) * 1024
		payloadEnd = usableEnd - persistSize
	}
	rows = append(rows, infoRow{Label: "p2 PAYLOAD", Value: partitionSpecValue(2, "managed payload", "ext4", payloadFSLabel, payloadStart, payloadEnd)})
	if plan.Persistence {
		fsLabel, partLabel := createPlanPersistenceLabels(plan)
		fs := "ext4"
		if plan.PersistenceMode == persistenceModeEncrypted {
			fs = "luks2/ext4"
		}
		rows = append(rows, infoRow{Label: "p3 PERSISTENCE", Value: partitionSpecValue(3, "persistence", fs, blankIfEmpty(fsLabel, partLabel), payloadEnd, usableEnd)})
	}
	rows = append(rows, infoRow{Label: "tail guard", Value: fmt.Sprintf("start_mib=%d | end_mib=%d | size=%s | role=partition-table slack", usableEnd, deviceMiB, formatMiB(deviceMiB-usableEnd))})
	return rows
}

func multiOSPartitionSpecificationRows(plan MultiOSPlan, device Device) []infoRow {
	espLabel := multiOSPlanESPLabel(plan)
	if len(plan.Items) == 0 {
		return []infoRow{
			{Label: "layout", Value: fmt.Sprintf("raw ISO payload partitions are allocated with netinst/installer sources first, then live sources, before the separate %s FAT32 ESP", espLabel)},
			{Label: "ESP", Value: fmt.Sprintf("GRUB, EFI assets, and /preseed/<os> live on the separate %s FAT32 partition", espLabel)},
		}
	}
	if multiOSUsesSharedISOStoreLayout(plan) {
		return sharedISOStorePersistencePartitionSpecificationRows(plan, device)
	}
	deviceMiB := bytesToMiB(device.SizeBytes)
	cursor := int64(1)
	rows := make([]infoRow, 0, len(plan.Items)+3)
	for index, item := range plan.Items {
		size, _ := statFileSize(item.ISOPath)
		payloadSize := bytesToMiB(size)
		start := cursor
		end := start + payloadSize
		value := partitionSpecValue(index+1, item.Title+" raw ISO payload", "raw-iso9660", blankIfEmpty(item.PayloadPartLabel, "PAYLOAD"), start, end)
		if payloadSize == 0 {
			value = fmt.Sprintf("number=%d | start_mib=%d | end_mib=<iso-sized> | fs=raw-iso9660 | label=%s | role=%s", index+1, start, blankIfEmpty(item.PayloadPartLabel, "PAYLOAD"), item.Title+" raw ISO payload")
			end = start + 1
		}
		rows = append(rows, infoRow{Label: fmt.Sprintf("p%d %s", index+1, item.ID), Value: value})
		cursor = end
	}
	espStart := cursor
	espEnd := espStart + 512
	rows = append(rows, infoRow{
		Label: fmt.Sprintf("p%d ESP", len(plan.Items)+1),
		Value: partitionSpecValue(len(plan.Items)+1, "uefi-removable + /preseed/<os>", "vfat", espLabel, espStart, espEnd),
	})
	cursor = espEnd
	partitionNumber := len(plan.Items) + 2
	for _, item := range plan.Items {
		if !item.Persistence {
			continue
		}
		start := cursor
		end := start + int64(item.PersistenceSizeGiB)*1024
		fs := "ext4"
		if item.PersistenceMode == persistenceModeEncrypted {
			fs = "luks2/ext4"
		}
		rows = append(rows, infoRow{
			Label: fmt.Sprintf("p%d %s persistence", partitionNumber, item.ID),
			Value: partitionSpecValue(partitionNumber, item.Title+" persistence", fs, blankIfEmpty(item.PersistenceFSLabel, item.PersistencePartLabel), start, end),
		})
		cursor = end
		partitionNumber++
	}
	if deviceMiB > 0 {
		usableEnd := deviceMiB - 8
		rows = append(rows, infoRow{Label: "tail guard", Value: fmt.Sprintf("start_mib=%d | end_mib=%d | size=%s | role=partition-table slack", usableEnd, deviceMiB, formatMiB(deviceMiB-usableEnd))})
	}
	return rows
}

func singleUsesSharedISOStoreLayout(plan CreatePlan) bool {
	return plan.ManagedPayloadLayout == "iso-store" && !plan.Persistence && plan.OfflinePreseedSourceDir == ""
}

func singlePayloadPartitionPrecedesESP(plan CreatePlan) bool {
	layout := strings.TrimSpace(plan.ManagedPayloadLayout)
	if layout == "raw-iso" {
		return true
	}
	if layout != "iso-store" {
		return false
	}
	switch strings.TrimSpace(plan.Profile) {
	case profileDebian, profileKaliLinux, profileKaliPurple:
		mediaClass := strings.TrimSpace(plan.MediaClass)
		return mediaClass == "installer" || mediaClass == "hybrid"
	default:
		return false
	}
}

func sharedISOStorePersistencePartitionSpecificationRows(plan MultiOSPlan, device Device) []infoRow {
	deviceMiB := bytesToMiB(device.SizeBytes)
	dataLabel := blankIfEmpty(plan.DataFSLabel, "<unset>")
	espStart := int64(1)
	espEnd := int64(513)
	payloadStart := espEnd
	usableEnd := int64(0)
	if deviceMiB > 0 {
		usableEnd = deviceMiB - 8
	}
	totalPersistMiB := int64(0)
	for _, item := range plan.Items {
		if item.Persistence {
			totalPersistMiB += int64(item.PersistenceSizeGiB) * 1024
		}
	}
	payloadEnd := usableEnd - totalPersistMiB
	rows := []infoRow{
		{Label: "p1 ESP", Value: partitionSpecValue(1, "uefi-removable", "vfat", multiOSPlanESPLabel(plan), espStart, espEnd)},
	}
	if payloadEnd <= payloadStart {
		rows = append(rows, infoRow{Label: "p2 ISO STORE", Value: "number=2 | start_mib=513 | end_mib=<device-dependent> | fs=ext4 | label=" + dataLabel + " | role=ISO store"})
		return rows
	}
	rows = append(rows, infoRow{Label: "p2 ISO STORE", Value: partitionSpecValue(2, "ISO store", "ext4", dataLabel, payloadStart, payloadEnd)})
	cursor := payloadEnd
	partitionNumber := 3
	for _, item := range plan.Items {
		if !item.Persistence {
			continue
		}
		start := cursor
		end := start + int64(item.PersistenceSizeGiB)*1024
		fs := "ext4"
		if item.PersistenceMode == persistenceModeEncrypted {
			fs = "luks2/ext4"
		}
		rows = append(rows, infoRow{
			Label: fmt.Sprintf("p%d %s persistence", partitionNumber, item.ID),
			Value: partitionSpecValue(partitionNumber, item.Title+" persistence", fs, blankIfEmpty(item.PersistenceFSLabel, item.PersistencePartLabel), start, end),
		})
		cursor = end
		partitionNumber++
	}
	rows = append(rows, infoRow{Label: "tail guard", Value: fmt.Sprintf("start_mib=%d | end_mib=%d | size=%s | role=partition-table slack", usableEnd, deviceMiB, formatMiB(deviceMiB-usableEnd))})
	return rows
}

func sharedISOStorePartitionSpecificationRows(device Device, scope string, espLabel string, payloadLabel string) []infoRow {
	deviceMiB := bytesToMiB(device.SizeBytes)
	espStart := int64(1)
	espEnd := int64(513)
	payloadStart := espEnd
	payloadEnd := int64(0)
	if deviceMiB > 0 {
		payloadEnd = deviceMiB - 8
	}
	rows := []infoRow{
		{Label: "p1 ESP", Value: partitionSpecValue(1, "uefi-removable", "vfat", blankIfEmpty(espLabel, "<unset>"), espStart, espEnd)},
	}
	payloadRole := "ISO store"
	dataLabel := blankIfEmpty(payloadLabel, "<unset>")
	if payloadEnd <= payloadStart {
		rows = append(rows, infoRow{Label: "p2 ISO STORE", Value: "number=2 | start_mib=513 | end_mib=<device-dependent> | fs=ext4 | label=" + dataLabel + " | role=" + payloadRole})
		return rows
	}
	rows = append(rows,
		infoRow{Label: "p2 ISO STORE", Value: partitionSpecValue(2, payloadRole, "ext4", dataLabel, payloadStart, payloadEnd)},
		infoRow{Label: "tail guard", Value: fmt.Sprintf("start_mib=%d | end_mib=%d | size=%s | role=partition-table slack", payloadEnd, deviceMiB, formatMiB(deviceMiB-payloadEnd))},
	)
	return rows
}
