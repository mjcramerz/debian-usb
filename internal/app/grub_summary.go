package app

import "fmt"

func grubMenuModeLabel(useCustomGrubMenu bool, preserveUpstreamGrubEntries bool) string {
	if useCustomGrubMenu {
		if preserveUpstreamGrubEntries {
			return "Custom grouped menu + preserved upstream-managed entries"
		}
		return "Custom grouped menu"
	}
	return "Preserved upstream-managed menu"
}

func grubEntryGroupsLabel(useCustomGrubMenu bool, preserveUpstreamGrubEntries bool) string {
	if useCustomGrubMenu {
		if preserveUpstreamGrubEntries {
			return "Custom + preserved entry groups"
		}
		return "Custom GRUB replacement"
	}
	return "Preserved entry groups"
}

func grubBootEntriesSummary(useCustomGrubMenu bool, preserveUpstreamGrubEntries bool) string {
	if useCustomGrubMenu {
		if preserveUpstreamGrubEntries {
			return "Curated custom GRUB entries plus preserved upstream-managed trees derived from the ISO boot assets"
		}
		return "Replaced with curated custom GRUB entries derived from the ISO boot assets"
	}
	return "Preserved from the ISO boot config"
}

func grubEntryGroupsSummary(entries []string, useCustomGrubMenu bool, preserveUpstreamGrubEntries bool) string {
	if !useCustomGrubMenu {
		return joinOrNone(entries)
	}
	if preserveUpstreamGrubEntries {
		if len(entries) == 0 {
			return "Curated custom GRUB entries are rendered alongside preserved upstream-managed trees"
		}
		if len(entries) == 1 {
			return "1 source ISO boot group detected; curated custom GRUB entries are rendered alongside preserved upstream-managed trees"
		}
		return fmt.Sprintf("%d source ISO boot groups detected; curated custom GRUB entries are rendered alongside preserved upstream-managed trees", len(entries))
	}
	if len(entries) == 0 {
		return "Source ISO boot groups were replaced by curated custom GRUB entries"
	}
	if len(entries) == 1 {
		return "1 source ISO boot group detected and replaced by curated custom GRUB entries"
	}
	return fmt.Sprintf("%d source ISO boot groups detected and replaced by curated custom GRUB entries", len(entries))
}
