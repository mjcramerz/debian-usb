package app

import "strings"

type managedSourceReleaseChannel string

const (
	managedSourceReleaseStable  managedSourceReleaseChannel = "stable"
	managedSourceReleaseTesting managedSourceReleaseChannel = "testing"
)

var managedSourceURLBaseOrder = []string{
	"DEBIAN_LIVE_ISO_URL",
	"DEBIAN_NETINST_ISO_URL",
	"DEBIAN_NETINST_VMLINUZ_URL",
	"DEBIAN_NETINST_INITRD_URL",
	"DEBIAN_NETBOOT_VMLINUZ_URL",
	"DEBIAN_NETBOOT_INITRD_URL",
	"KALI_LIVE_ISO_URL",
	"KALI_NETINST_ISO_URL",
	"KALI_NETINST_VMLINUZ_URL",
	"KALI_NETINST_INITRD_URL",
	"KALI_PURPLE_ISO_URL",
	"KALI_NETBOOT_VMLINUZ_URL",
	"KALI_NETBOOT_INITRD_URL",
	"TAILS_LIVE_ISO_URL",
}

var managedSourceURLOrder = managedSourceURLKeysForChannels()

func managedSourceURLKeys() []string {
	keys := make([]string, len(managedSourceURLOrder))
	copy(keys, managedSourceURLOrder)
	return keys
}

func managedSourceURLKeysForChannels() []string {
	keys := make([]string, 0, len(managedSourceURLBaseOrder)*2)
	for _, baseKey := range managedSourceURLBaseOrder {
		for _, channel := range []managedSourceReleaseChannel{
			managedSourceReleaseStable,
			managedSourceReleaseTesting,
		} {
			keys = append(keys, managedSourceURLKey(baseKey, channel))
		}
	}
	return keys
}

func managedSourceURLKey(baseKey string, channel managedSourceReleaseChannel) string {
	baseKey = strings.TrimSpace(baseKey)
	if !strings.HasSuffix(baseKey, "_URL") {
		return ""
	}
	switch channel {
	case managedSourceReleaseStable, managedSourceReleaseTesting:
		return strings.TrimSuffix(baseKey, "_URL") + "_" + strings.ToUpper(string(channel)) + "_URL"
	default:
		return ""
	}
}

func managedSourceReleaseLabel(channel managedSourceReleaseChannel) string {
	switch channel {
	case managedSourceReleaseStable:
		return "Stable"
	case managedSourceReleaseTesting:
		return "Testing"
	default:
		return ""
	}
}

func managedSourceReleaseProfileLabel(profile string, channel managedSourceReleaseChannel) string {
	if profile == profileDebian {
		switch channel {
		case managedSourceReleaseStable:
			return "Stable (Trixie)"
		case managedSourceReleaseTesting:
			return "Testing (Forky)"
		}
	}
	return managedSourceReleaseLabel(channel)
}

func managedSourceURLValue(config RuntimeConfig, key string) string {
	if config.ManagedSourceURLs != nil {
		return strings.TrimSpace(config.ManagedSourceURLs[key])
	}
	return ""
}

func managedISOSourceKey(profile string, channel managedSourceReleaseChannel) string {
	return managedISOSourceRoleKey(profile, multiOSSourceRolePrimary, channel)
}

func managedISOSourceRoleKey(profile, sourceRole string, channel managedSourceReleaseChannel) string {
	var baseKey string
	switch profile {
	case profileDebian:
		if sourceRole == multiOSSourceRoleNetinst {
			baseKey = "DEBIAN_NETINST_ISO_URL"
			break
		}
		baseKey = "DEBIAN_LIVE_ISO_URL"
	case profileKaliLinux:
		if sourceRole == multiOSSourceRoleNetinst {
			baseKey = "KALI_NETINST_ISO_URL"
			break
		}
		baseKey = "KALI_LIVE_ISO_URL"
	case profileKaliPurple:
		baseKey = "KALI_PURPLE_ISO_URL"
	case profileTails:
		baseKey = "TAILS_LIVE_ISO_URL"
	}
	return managedSourceURLKey(baseKey, channel)
}

func managedInstallerKernelURLKey(profile, sourceRole string, channel managedSourceReleaseChannel) string {
	var baseKey string
	switch profile {
	case profileDebian:
		if sourceRole == multiOSSourceRoleNetboot {
			baseKey = "DEBIAN_NETBOOT_VMLINUZ_URL"
			break
		}
		if sourceRole == multiOSSourceRoleNetinst {
			baseKey = "DEBIAN_NETINST_VMLINUZ_URL"
		}
	case profileKaliLinux:
		if sourceRole == multiOSSourceRoleNetboot {
			baseKey = "KALI_NETBOOT_VMLINUZ_URL"
			break
		}
		if sourceRole == multiOSSourceRoleNetinst {
			baseKey = "KALI_NETINST_VMLINUZ_URL"
		}
	}
	return managedSourceURLKey(baseKey, channel)
}

func managedInstallerInitrdURLKey(profile, sourceRole string, channel managedSourceReleaseChannel) string {
	var baseKey string
	switch profile {
	case profileDebian:
		if sourceRole == multiOSSourceRoleNetboot {
			baseKey = "DEBIAN_NETBOOT_INITRD_URL"
			break
		}
		if sourceRole == multiOSSourceRoleNetinst {
			baseKey = "DEBIAN_NETINST_INITRD_URL"
		}
	case profileKaliLinux:
		if sourceRole == multiOSSourceRoleNetboot {
			baseKey = "KALI_NETBOOT_INITRD_URL"
			break
		}
		if sourceRole == multiOSSourceRoleNetinst {
			baseKey = "KALI_NETINST_INITRD_URL"
		}
	}
	return managedSourceURLKey(baseKey, channel)
}

func profileSupportsManagedNetinst(profile string) bool {
	switch profile {
	case profileDebian, profileKaliLinux:
		return true
	default:
		return false
	}
}

func profileSupportsManagedNetboot(profile string) bool {
	switch profile {
	case profileDebian, profileKaliLinux:
		return true
	default:
		return false
	}
}

func profileSupportsSourceRole(profile, sourceRole string) bool {
	switch strings.TrimSpace(sourceRole) {
	case "", multiOSSourceRolePrimary:
		return true
	case multiOSSourceRoleNetinst:
		return profileSupportsManagedNetinst(profile)
	case multiOSSourceRoleNetboot:
		return profileSupportsManagedNetboot(profile)
	default:
		return false
	}
}
