package app

import (
	"fmt"
	"strings"
)

const (
	configLabelESP               = "DEFAULT_ESP_LABEL"
	configLabelMultiData         = "DEFAULT_MULTI_DATA_LABEL"
	configLabelDebianLive        = "DEFAULT_DEBIAN_LIVE_LABEL"
	configLabelDebianNetinst     = "DEFAULT_DEBIAN_NETINST_LABEL"
	configLabelDebianNetboot     = "DEFAULT_DEBIAN_NETBOOT_LABEL"
	configLabelDebianPersist     = "DEFAULT_DEBIAN_PERSIST_LABEL"
	configLabelKaliLive          = "DEFAULT_KALI_LIVE_LABEL"
	configLabelKaliNetinst       = "DEFAULT_KALI_NETINST_LABEL"
	configLabelKaliNetboot       = "DEFAULT_KALI_NETBOOT_LABEL"
	configLabelKaliPersist       = "DEFAULT_KALI_PERSIST_LABEL"
	configLabelKaliPurpleNetinst = "DEFAULT_KALI_PURPLE_NETINST_LABEL"
	configLabelTailsLive         = "DEFAULT_TAILS_LIVE_LABEL"
	configLabelTailsPersist      = "DEFAULT_TAILS_PERSIST_LABEL"
	configLabelUbuntuLive        = "DEFAULT_UBUNTU_LIVE_LABEL"
	configLabelUbuntuNetinst     = "DEFAULT_UBUNTU_NETINST_LABEL"
	configLabelUbuntuPersist     = "DEFAULT_UBUNTU_PERSIST_LABEL"
	configLabelUbuntuPersistPart = "DEFAULT_UBUNTU_PERSIST_PARTLABEL"
)

var multiOSPartitionLabelConfigKeys = []string{
	configLabelESP,
	configLabelMultiData,
	configLabelDebianPersist,
	configLabelKaliPersist,
	configLabelTailsPersist,
}

var singleOSPartitionLabelConfigKeys = []string{
	configLabelDebianLive,
	configLabelDebianNetinst,
	configLabelDebianNetboot,
	configLabelKaliLive,
	configLabelKaliNetinst,
	configLabelKaliNetboot,
	configLabelKaliPurpleNetinst,
	configLabelTailsLive,
	configLabelUbuntuLive,
	configLabelUbuntuNetinst,
	configLabelUbuntuPersist,
	configLabelUbuntuPersistPart,
}

var partitionLabelConfigKeys = append(append([]string{}, multiOSPartitionLabelConfigKeys...), singleOSPartitionLabelConfigKeys...)

func partitionLabelValue(config RuntimeConfig, key string) string {
	if config.DefaultPartitionLabels != nil {
		if value := strings.TrimSpace(config.DefaultPartitionLabels[key]); value != "" {
			return value
		}
	}
	return ""
}

func normalizePartitionLabel(value, key string) (string, error) {
	normalized := strings.TrimSpace(value)
	if normalized == "" {
		return "", fmt.Errorf("%s is required", key)
	}
	maxLength := 16
	if key == configLabelESP {
		maxLength = 11
	}
	if len(normalized) > maxLength {
		return "", fmt.Errorf("%s %q is longer than %d characters", key, normalized, maxLength)
	}
	for _, char := range normalized {
		if char < 33 || char > 126 {
			return "", fmt.Errorf("%s %q must contain printable ASCII without whitespace", key, normalized)
		}
	}
	return normalized, nil
}

func managedESPLabel(config RuntimeConfig) string {
	return partitionLabelValue(config, configLabelESP)
}

func managedSharedDataLabels(config RuntimeConfig) (string, string) {
	label := partitionLabelValue(config, configLabelMultiData)
	return label, label
}

func managedPersistenceLabels(config RuntimeConfig, profile string) (string, string) {
	switch profile {
	case profileDebian:
		label := partitionLabelValue(config, configLabelDebianPersist)
		return label, label
	case profileUbuntuDesktop:
		// Ubuntu persistence still relies on casper's documented filesystem label
		// and writable partition name conventions.
		return partitionLabelValue(config, configLabelUbuntuPersist), partitionLabelValue(config, configLabelUbuntuPersistPart)
	case profileKaliLinux:
		label := partitionLabelValue(config, configLabelKaliPersist)
		return label, label
	case profileTails:
		label := partitionLabelValue(config, configLabelTailsPersist)
		return label, label
	default:
		return "", ""
	}
}

func managedPayloadLabels(config RuntimeConfig, profile string, sourceRole string, mediaClass string) (string, string) {
	labelKey := ""
	switch profile {
	case profileDebian:
		switch {
		case sourceRole == multiOSSourceRoleNetboot:
			labelKey = configLabelDebianNetboot
		case sourceRole == multiOSSourceRoleNetinst || mediaClass == "installer":
			labelKey = configLabelDebianNetinst
		default:
			labelKey = configLabelDebianLive
		}
	case profileUbuntuDesktop:
		if sourceRole == multiOSSourceRoleNetinst || mediaClass == "installer" {
			labelKey = configLabelUbuntuNetinst
		} else {
			labelKey = configLabelUbuntuLive
		}
	case profileUbuntuServer:
		labelKey = configLabelUbuntuNetinst
	case profileKaliLinux:
		switch {
		case sourceRole == multiOSSourceRoleNetboot:
			labelKey = configLabelKaliNetboot
		case sourceRole == multiOSSourceRoleNetinst || mediaClass == "installer":
			labelKey = configLabelKaliNetinst
		default:
			labelKey = configLabelKaliLive
		}
	case profileKaliPurple:
		labelKey = configLabelKaliPurpleNetinst
	case profileTails:
		labelKey = configLabelTailsLive
	default:
		return "PAYLOAD", "PAYLOAD"
	}
	label := partitionLabelValue(config, labelKey)
	return label, label
}
