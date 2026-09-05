package app

import "strings"

func profileUSBPreseedFile(config RuntimeConfig, profile string) string {
	if config.ExtraValues == nil {
		return ""
	}
	switch strings.TrimSpace(profile) {
	case profileDebian:
		return strings.TrimSpace(config.ExtraValues["PRESEED_USB_DEBIAN_FILE"])
	case profileKaliLinux:
		return strings.TrimSpace(config.ExtraValues["PRESEED_USB_KALI_FILE"])
	case profileKaliPurple:
		return strings.TrimSpace(config.ExtraValues["PRESEED_USB_PURPLE_FILE"])
	default:
		return ""
	}
}

func profileHostPreseedPath(config RuntimeConfig, profile string) string {
	if config.ExtraValues == nil {
		return ""
	}
	switch strings.TrimSpace(profile) {
	case profileDebian:
		return strings.TrimSpace(config.ExtraValues["PRESEED_HOST_DEBIAN_PATH"])
	case profileKaliLinux:
		return strings.TrimSpace(config.ExtraValues["PRESEED_HOST_KALI_PATH"])
	case profileKaliPurple:
		return strings.TrimSpace(config.ExtraValues["PRESEED_HOST_PURPLE_PATH"])
	default:
		return ""
	}
}

func effectiveManagedPayloadLayout(config RuntimeConfig, profile, layout string, useCustomGrubMenu bool) string {
	normalized := strings.TrimSpace(layout)
	if normalized == "iso-store" {
		return "iso-store"
	}
	if normalized == "" {
		switch strings.TrimSpace(profile) {
		case profileDebian, profileKaliLinux, profileKaliPurple, profileTails:
			return "raw-iso"
		default:
			return "extracted"
		}
	}
	return normalized
}
