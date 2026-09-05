package app

import (
	"encoding/json"
	"fmt"
	"os"
)

type bundledBuildISOProfile struct {
	Key         string
	Title       string
	Detail      string
	JSONRelPath string
	ConfRelPath string
}

var bundledBuildISOProfiles = []bundledBuildISOProfile{
	{
		Key:         "debian-live",
		Title:       "Debian Live Profile",
		Detail:      "bundled live-build profile",
		JSONRelPath: "live/debian/live-build.json",
		ConfRelPath: "live/debian/live-build.conf",
	},
	{
		Key:         "debian-netinst",
		Title:       "Debian Netinst Profile",
		Detail:      "bundled installer profile",
		JSONRelPath: "d-i/debian/netinst-build.json",
		ConfRelPath: "d-i/debian/netinst-build.conf",
	},
	{
		Key:         "kali-live",
		Title:       "Kali Live Profile",
		Detail:      "bundled live-build profile",
		JSONRelPath: "live/kali-linux/live-build.json",
		ConfRelPath: "live/kali-linux/live-build.conf",
	},
	{
		Key:         "kali-netinst",
		Title:       "Kali Netinst Profile",
		Detail:      "bundled installer profile",
		JSONRelPath: "d-i/kali-linux/netinst-build.json",
		ConfRelPath: "d-i/kali-linux/netinst-build.conf",
	},
}

func bundledBuildISOProfileByKey(key string) (bundledBuildISOProfile, bool) {
	for _, profile := range bundledBuildISOProfiles {
		if profile.Key == key {
			return profile, true
		}
	}
	return bundledBuildISOProfile{}, false
}

func loadBundledBuildISOProfile(profile bundledBuildISOProfile) (BuildISOPlan, string, string, error) {
	jsonPath := managedSpecFilePath(profile.JSONRelPath)
	if jsonPath == "" {
		return BuildISOPlan{}, "", "", fmt.Errorf("missing bundled build profile JSON: %s", profile.JSONRelPath)
	}
	confPath := managedSpecFilePath(profile.ConfRelPath)
	if confPath == "" {
		return BuildISOPlan{}, "", "", fmt.Errorf("missing bundled build profile config: %s", profile.ConfRelPath)
	}
	payload, err := os.ReadFile(jsonPath)
	if err != nil {
		return BuildISOPlan{}, "", "", err
	}
	var plan BuildISOPlan
	if err := json.Unmarshal(payload, &plan); err != nil {
		return BuildISOPlan{}, "", "", fmt.Errorf("parse bundled build profile %s: %w", jsonPath, err)
	}
	return plan, jsonPath, confPath, nil
}
