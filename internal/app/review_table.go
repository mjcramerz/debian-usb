package app

import (
	"fmt"
	"strings"
	"unicode/utf8"
)

// Cells wrap; disk paths and serials are never silently truncated.
func printSpecTable(title string, headers []string, widths []int, rows [][]string, dangerRow int) {
	widths = append([]int(nil), widths...)
	columns, _ := terminalDimensions()
	// Keep the complete identity visible even in a standard 80-column terminal.
	for {
		total, largest := 1, 0
		for i, width := range widths {
			total += width + 3
			if width > widths[largest] {
				largest = i
			}
		}
		if total < columns || widths[largest] <= 4 {
			break
		}
		widths[largest]--
	}
	fmt.Println(styled(title, "1;36"))
	border := "+"
	for _, width := range widths {
		border += strings.Repeat("-", width+2) + "+"
	}
	fmt.Println(styled(border, "2"))
	all := append([][]string{headers}, rows...)
	for index, row := range all {
		wrapped := make([][]string, len(widths))
		height := 1
		for col, width := range widths {
			value := "-"
			if col < len(row) && strings.TrimSpace(row[col]) != "" {
				value = safeTerminalText(row[col])
			}
			wrapped[col] = wrapText(value, width)
			if len(wrapped[col]) > height {
				height = len(wrapped[col])
			}
		}
		for line := 0; line < height; line++ {
			text := "|"
			for col, width := range widths {
				value := ""
				if line < len(wrapped[col]) {
					value = wrapped[col][line]
				}
				text += " " + value + strings.Repeat(" ", maxInt(0, width-utf8.RuneCountInString(value))) + " |"
			}
			if index == 0 {
				text = styled(text, "1;36")
			} else if index-1 == dangerRow {
				text = styled(text, "1;37;41")
			}
			fmt.Println(text)
		}
		if index == 0 {
			fmt.Println(styled(border, "2"))
		}
	}
	fmt.Println(styled(border, "2"))
	fmt.Println()
}

func printTargetReview(device Device, update bool) {
	title := "TARGET DISK - ALL DATA WILL BE ERASED"
	if update {
		title = "TARGET DISK - MANAGED FILES WILL BE UPDATED"
	}
	printSpecTable(title, []string{"Target", "Device", "Model", "Capacity"}, []int{8, 20, 38, 12},
		[][]string{{"DISK", device.Path, deviceFullName(device), compactGiB(device.SizeBytes)}}, 0)
	printSpecTable("Identity", []string{"Serial", "Transport", "Mount state"}, []int{38, 14, 29},
		[][]string{{blankIfEmpty(device.Serial, "not reported"), device.Transport, mapBoolLabel(device.Mounted, "mounted - writer will release", "unmounted")}}, -1)
}

func persistenceReview(enabled bool, mode string, size int) string {
	if !enabled {
		return "off"
	}
	if mode == persistenceModeEncrypted {
		return fmt.Sprintf("LUKS2 + ext4 / %d GiB", size)
	}
	return fmt.Sprintf("ext4 / %d GiB", size)
}
func toolsReview(groups []string) string {
	if groups == nil {
		return "default groups"
	}
	if len(groups) == 0 {
		return "none"
	}
	return strings.Join(groups, ", ")
}
func preparationReview(profile, path string, p *SourcePreparation) [][]string {
	if p == nil {
		return [][]string{{profile + " source", path}}
	}
	rows := [][]string{}
	for _, input := range []struct {
		name  string
		input SourceInput
	}{{"ISO", p.ISO}, {"Kernel", p.Kernel}, {"Initrd", p.Initrd}} {
		if input.input.display() != "" {
			rows = append(rows, []string{profile + " " + input.name, input.input.display()})
		}
	}
	if p.InitrdOverlayDir != "" {
		rows = append(rows, []string{"Initrd overlay", p.InitrdOverlayDir})
	}
	if p.InitrdPreseedPath != "" {
		rows = append(rows, []string{"Initrd preseed", p.InitrdPreseedPath})
	}
	if len(p.ExtraModules) > 0 {
		rows = append(rows, []string{"Extra modules", strings.Join(p.ExtraModules, ", ")}, []string{"Module source", p.ModuleSourceStrategy})
	}
	return rows
}

func printSingleReview(plan CreatePlan, device Device, id, state string, update bool) {
	toolLabel := "not applicable"
	if plan.WriteMode == writeModeManaged && plan.SourceRole != multiOSSourceRoleNetinst && plan.SourceRole != multiOSSourceRoleNetboot && isLiveCapableMedia(plan.MediaClass) && liveSourceSupportsToolRemaster(plan.Profile) {
		toolLabel = toolsReview(plan.LiveToolGroups)
	}
	printTargetReview(device, update)
	rows := [][]string{{"Mode", plan.WriteMode}, {"Source role", blankIfEmpty(plan.SourceRole, multiOSSourceRolePrimary)},
		{"Layout", plan.ManagedPayloadLayout}, {"Secure Boot", secureBootTrustModeTechnicalLabel(plan.SecureBootTrust)},
		{"GRUB", mapBoolLabel(plan.UseCustomGrubMenu, "custom", "upstream")},
		{"Persistence", persistenceReview(plan.Persistence, plan.PersistenceMode, plan.PersistenceSizeGiB)},
		{"RAM copy", mapBoolLabel(plan.LiveToram, "enabled / toram", "off / USB-backed default")},
		{"Live tools", toolLabel}, {"Preseed", mapBoolLabel(plan.Preseed, "HTTP + USB", "off")},
		{"Saved plan", id + " / " + state}, {"Label", plan.MenuLabel}, {"ESP", plan.ESPLabel}}
	if plan.Preparation != nil {
		rows = append(rows, []string{"Build timing", "prepare once, after confirmation"})
	}
	if plan.PersistenceMode == persistenceModeEncrypted {
		rows = append(rows, []string{"Crypt support", "verify / add automatically"})
	}
	if plan.KernelArgs != "" {
		rows = append(rows, []string{"Kernel arguments", plan.KernelArgs})
	}
	printSpecTable("BUILD SPECIFICATION", []string{"Setting", "Selection"}, []int{20, 63}, rows, -1)
	printSpecTable("INPUTS", []string{"Input", "Path / URL"}, []int{20, 63}, preparationReview(plan.Profile, plan.ISOPath, plan.Preparation), -1)
}

func printMultiReview(plan MultiOSPlan, device Device, id, state string, update bool) {
	printTargetReview(device, update)
	printSpecTable("BUILD SPECIFICATION", []string{"Setting", "Selection"}, []int{20, 63}, [][]string{
		{"Mode", "Multi-OS / managed GRUB"}, {"Layout", "GPT / ESP FAT32 / shared ext4 ISO store / persistence"},
		{"Secure Boot", secureBootTrustModeTechnicalLabel(plan.SecureBootTrust)}, {"Saved plan", id + " / " + state}}, -1)
	rows := [][]string{}
	inputs := [][]string{}
	for _, item := range plan.Items {
		tools := "not applicable"
		if item.SourceRole == multiOSSourceRolePrimary && isLiveCapableMedia(item.MediaClass) && liveSourceSupportsToolRemaster(item.Profile) {
			tools = toolsReview(item.LiveToolGroups)
		}
		rows = append(rows, []string{item.Title, blankIfEmpty(item.SourceRole, "primary"), persistenceReview(item.Persistence, item.PersistenceMode, item.PersistenceSizeGiB), tools})
		inputs = append(inputs, []string{item.Title + " boot", "RAM " + onOffLabel(item.LiveToram) + " / preseed " + onOffLabel(item.Preseed)})
		if item.MenuLabel != "" {
			inputs = append(inputs, []string{item.Title + " label", item.MenuLabel})
		}
		if item.Persistence {
			inputs = append(inputs, []string{item.Title + " persistence", item.PersistenceFSLabel + " / " + item.PersistencePartLabel})
		}
		inputs = append(inputs, preparationReview(item.Title, item.ISOPath, item.Preparation)...)
		if item.KernelArgs != "" {
			inputs = append(inputs, []string{item.Title + " args", item.KernelArgs})
		}
	}
	printSpecTable("BOOT SOURCES", []string{"OS", "Role", "Persistence", "Live tools"}, []int{21, 10, 27, 19}, rows, -1)
	printSpecTable("INPUTS", []string{"Input", "Path / URL"}, []int{20, 63}, inputs, -1)
}

func (a *App) reviewSavedPlan(execution *PlannedExecution, path string, update bool) error {
	devices, err := a.backend.ListDevices()
	if err != nil {
		return err
	}
	for _, device := range devices {
		if device.Path != path {
			continue
		}
		if device.SystemDisk || !device.Removable {
			return fmt.Errorf("ineligible target disk: %s", path)
		}
		switch execution.Kind {
		case plannedExecutionKindSingle:
			if execution.SinglePlan == nil {
				return fmt.Errorf("missing single plan")
			}
			copy := *execution.SinglePlan
			copy.TargetDevice = &device
			execution.SinglePlan = &copy
			printSingleReview(copy, device, execution.RunID, "saved", update)
		case plannedExecutionKindMultiOS:
			if execution.MultiOSPlan == nil {
				return fmt.Errorf("missing Multi-OS plan")
			}
			copy := *execution.MultiOSPlan
			copy.TargetDevice = &device
			execution.MultiOSPlan = &copy
			printMultiReview(copy, device, execution.RunID, "saved", update)
		default:
			return fmt.Errorf("unsupported plan kind: %s", execution.Kind)
		}
		return nil
	}
	return fmt.Errorf("target is not a connected whole disk: %s", path)
}
