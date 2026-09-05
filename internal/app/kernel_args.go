package app

import (
	"fmt"
	"strings"
)

const mandatoryDebianLiveHookKernelArgs = "live-config.hooks=medium"

func preseedCommonKernelArgs(config RuntimeConfig) string {
	if config.ExtraValues == nil {
		return ""
	}
	return collapseWhitespace(config.ExtraValues["PRESEED_COMMON_KERNEL_ARGS"])
}

func livePolicyKernelArgsForConfig(config RuntimeConfig) string {
	base := strings.TrimSpace(config.SharedLiveBaseKernelArgs)
	policy := ""
	switch config.DefaultBootPolicy {
	case "balanced":
		policy = strings.TrimSpace(config.BootPolicyBalancedKernelArgs)
	case "performance":
		policy = strings.TrimSpace(config.BootPolicyPerformanceArgs)
	case "hardened":
		policy = strings.TrimSpace(config.BootPolicyHardenedArgs)
	}
	return collapseWhitespace(strings.TrimSpace(base + " " + policy))
}

func (a *App) livePolicyKernelArgs() string {
	return livePolicyKernelArgsForConfig(a.config)
}

func removeSecretKernelArgs(kernelArgs string) string {
	filtered := make([]string, 0, len(strings.Fields(kernelArgs)))
	for _, item := range strings.Fields(collapseWhitespace(kernelArgs)) {
		name, _, _ := strings.Cut(item, "=")
		if _, forbidden := legacySecretKernelArgNames[name]; forbidden {
			continue
		}
		if _, forbidden := liveWifiSecretKernelArgNames[name]; forbidden {
			continue
		}
		filtered = append(filtered, item)
	}
	return collapseWhitespace(strings.Join(filtered, " "))
}

func liveHookKernelArgsForConfig(config RuntimeConfig, profile string) string {
	if profile != profileDebian {
		return ""
	}
	optionalArgs := ""
	if config.DefaultLiveHooks {
		optionalArgs = removeSecretKernelArgs(config.DefaultLiveArgsHooks)
	}
	return mergeKernelArgs(optionalArgs, mandatoryDebianLiveHookKernelArgs)
}

func applyLiveHookKernelArgsToBuildPlan(config RuntimeConfig, plan *BuildISOPlan) {
	if plan == nil || plan.Distro != buildISODistroDebian || plan.InstallerMode == buildISOInstallerModeNetinst {
		return
	}
	plan.LiveBootAppend = removeSecretKernelArgs(plan.LiveBootAppend)
	if hookArgs := liveHookKernelArgsForConfig(config, profileDebian); hookArgs != "" {
		plan.LiveBootAppend = mergeKernelArgs(plan.LiveBootAppend, hookArgs)
	}
	plan.LiveBootAppend = removeSecretKernelArgs(plan.LiveBootAppend)
}

func firstNonEmpty(values ...string) string {
	for _, value := range values {
		if strings.TrimSpace(value) != "" {
			return strings.TrimSpace(value)
		}
	}
	return ""
}

func profileConfigValue(values map[string]string, profile string) string {
	if values == nil {
		return ""
	}
	return strings.TrimSpace(values[profile])
}

func defaultLiveKernelArgsForSelection(config RuntimeConfig, spec profileSpec, persistenceMode string, liveToram bool) string {
	args := collapseWhitespace(profileConfigValue(config.ProfileFallbackLiveKernelArgs, spec.Key))
	args = removeKernelArgsByExact(args, "toram")
	args = removeKernelArgsByPrefix(args, "toram=")
	if policyArgs := livePolicyKernelArgsForConfig(config); policyArgs != "" {
		args = mergeKernelArgs(args, policyArgs)
	}
	if config.DefaultLiveKernelExtras != "" {
		args = mergeKernelArgs(args, config.DefaultLiveKernelExtras)
	}
	if profileExtras := profileConfigValue(config.ProfileLiveKernelExtras, spec.Key); profileExtras != "" {
		args = mergeKernelArgs(args, profileExtras)
	}
	if hookArgs := liveHookKernelArgsForConfig(config, spec.Key); hookArgs != "" {
		args = mergeKernelArgs(args, hookArgs)
	}
	if liveToram {
		toramArg := "toram"
		if spec.LiveBootFamily == "live-boot" {
			toramArg = "toram=filesystem.squashfs"
		}
		args = mergeKernelArgs(args, toramArg)
	}
	if config.DefaultLiveMemGiB > 0 {
		args = mergeKernelArgs(args, fmt.Sprintf("mem=%dG", config.DefaultLiveMemGiB))
	}
	if spec.LiveBootFamily == "casper" {
		args = removeKernelArgsByExact(args, "ignore_uuid", "persistent", "nopersistent")
		args = removeKernelArgsByPrefix(args, "uuid=", "iso-scan/filename=")
		if persistenceMode == "plain" && spec.SupportsPersistence {
			args = mergeKernelArgs(args, "persistent")
		}
		args = mergeKernelArgs(args, "iso-scan/filename=${isofile}")
		return removeSecretKernelArgs(args)
	}
	args = removeKernelArgsByExact(args, "ignore_uuid", "persistence", "nopersistence", "persistent=cryptsetup")
	args = removeKernelArgsByPrefix(
		args,
		"uuid=", "findiso=", "fromiso=", "iso-scan/filename=", "persistence-label=", "persistence-encryption=",
		"persistence-media=", "persistence-storage=", "persistence-method=", "union=",
	)
	args = mergeKernelArgs(args, "findiso=${isofile}")
	if spec.Key == profileTails {
		return removeSecretKernelArgs(args)
	}
	if persistenceMode == "encrypted" {
		if spec.Key == profileKaliLinux {
			args = mergeKernelArgs(args, "persistent=cryptsetup persistence-encryption=luks persistence persistence-media=removable-usb persistence-storage=filesystem union=overlay")
		} else {
			args = mergeKernelArgs(args, "persistence persistence-encryption=luks persistence-media=removable-usb persistence-storage=filesystem union=overlay")
		}
		if fsLabel, _ := managedPersistenceLabels(config, spec.Key); fsLabel != "" {
			args = mergeKernelArgs(args, "persistence-label="+fsLabel)
		}
	} else if persistenceMode == "plain" {
		fsLabel, _ := managedPersistenceLabels(config, spec.Key)
		if fsLabel == "" {
			fsLabel = "persistence"
		}
		args = mergeKernelArgs(args, "persistence persistence-label="+fsLabel+" persistence-media=removable-usb persistence-storage=filesystem union=overlay")
	}
	return removeSecretKernelArgs(args)
}

func defaultLiveKernelArgsForConfig(config RuntimeConfig, spec profileSpec, persistenceMode string) string {
	return defaultLiveKernelArgsForSelection(config, spec, persistenceMode, config.DefaultLiveToram)
}

func (a *App) defaultLiveKernelArgs(spec profileSpec, persistenceMode string) string {
	return defaultLiveKernelArgsForConfig(a.config, spec, persistenceMode)
}

func (a *App) defaultLiveKernelArgsWithToram(spec profileSpec, persistenceMode string, liveToram bool) string {
	return defaultLiveKernelArgsForSelection(a.config, spec, persistenceMode, liveToram)
}

func defaultInstallerKernelArgsForConfig(config RuntimeConfig) string {
	args := ""
	switch config.DefaultInstallerPolicy {
	case "preserve":
		args = collapseWhitespace(config.InstallerPolicyPreserveArgs)
	case "installer-preseed":
		args = preseedCommonKernelArgs(config)
	}
	if config.DefaultInstallerKernelExtras != "" {
		args = mergeKernelArgs(args, config.DefaultInstallerKernelExtras)
	}
	args = removeKernelArgsByExact(args, "toram")
	args = removeKernelArgsByPrefix(args, "toram=")
	return removeSecretKernelArgs(args)
}

func (a *App) defaultInstallerKernelArgs() string {
	return defaultInstallerKernelArgsForConfig(a.config)
}

func collapseWhitespace(value string) string {
	return strings.Join(strings.Fields(value), " ")
}

func mergeKernelArgs(kernelArgs, additions string) string {
	args := strings.Fields(collapseWhitespace(kernelArgs))
	additionTokens := strings.Fields(collapseWhitespace(additions))
	if len(additionTokens) == 0 {
		return collapseWhitespace(kernelArgs)
	}
	separatorIndex := len(args)
	for index, item := range args {
		if item == "---" {
			separatorIndex = index
			break
		}
	}
	merged := append([]string{}, args[:separatorIndex]...)
	suffix := append([]string{}, args[separatorIndex:]...)
	for _, item := range additionTokens {
		filtered := merged[:0]
		if strings.Contains(item, "=") {
			key := strings.SplitN(item, "=", 2)[0]
			for _, current := range merged {
				if current == key || strings.HasPrefix(current, key+"=") {
					continue
				}
				filtered = append(filtered, current)
			}
			merged = append(filtered, item)
			continue
		}
		for _, current := range merged {
			if current == item {
				continue
			}
			filtered = append(filtered, current)
		}
		merged = append(filtered, item)
	}
	return collapseWhitespace(strings.Join(append(merged, suffix...), " "))
}

func removeKernelArgsByExact(kernelArgs string, exactArgs ...string) string {
	disallowed := make(map[string]struct{}, len(exactArgs))
	for _, item := range exactArgs {
		disallowed[item] = struct{}{}
	}
	filtered := make([]string, 0, len(strings.Fields(kernelArgs)))
	for _, item := range strings.Fields(collapseWhitespace(kernelArgs)) {
		if _, found := disallowed[item]; found {
			continue
		}
		filtered = append(filtered, item)
	}
	return collapseWhitespace(strings.Join(filtered, " "))
}

func removeKernelArgsByPrefix(kernelArgs string, prefixes ...string) string {
	filtered := make([]string, 0, len(strings.Fields(kernelArgs)))
	for _, item := range strings.Fields(collapseWhitespace(kernelArgs)) {
		skip := false
		for _, prefix := range prefixes {
			if strings.HasPrefix(item, prefix) {
				skip = true
				break
			}
		}
		if skip {
			continue
		}
		filtered = append(filtered, item)
	}
	return collapseWhitespace(strings.Join(filtered, " "))
}
