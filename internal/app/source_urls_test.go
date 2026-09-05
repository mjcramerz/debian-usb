package app

import "testing"

func TestManagedSourceURLKeysIncludeStableAndTestingPairs(t *testing.T) {
	keys := managedSourceURLKeys()
	if len(keys) != len(managedSourceURLBaseOrder)*2 {
		t.Fatalf("expected %d managed source URL keys, got %d", len(managedSourceURLBaseOrder)*2, len(keys))
	}

	present := make(map[string]bool, len(keys))
	for _, key := range keys {
		present[key] = true
	}
	for _, baseKey := range managedSourceURLBaseOrder {
		for _, channel := range []managedSourceReleaseChannel{
			managedSourceReleaseStable,
			managedSourceReleaseTesting,
		} {
			key := managedSourceURLKey(baseKey, channel)
			if !present[key] {
				t.Fatalf("missing %s release URL key %q", managedSourceReleaseLabel(channel), key)
			}
		}
	}
}

func TestManagedSourceKeySelectionUsesSelectedReleaseChannel(t *testing.T) {
	tests := []struct {
		name    string
		stable  string
		testing string
		resolve func(managedSourceReleaseChannel) string
	}{
		{
			name:    "Debian live",
			stable:  "DEBIAN_LIVE_ISO_STABLE_URL",
			testing: "DEBIAN_LIVE_ISO_TESTING_URL",
			resolve: func(channel managedSourceReleaseChannel) string {
				return managedISOSourceRoleKey(profileDebian, multiOSSourceRolePrimary, channel)
			},
		},
		{
			name:    "Debian netinst kernel",
			stable:  "DEBIAN_NETINST_VMLINUZ_STABLE_URL",
			testing: "DEBIAN_NETINST_VMLINUZ_TESTING_URL",
			resolve: func(channel managedSourceReleaseChannel) string {
				return managedInstallerKernelURLKey(profileDebian, multiOSSourceRoleNetinst, channel)
			},
		},
		{
			name:    "Kali netboot initrd",
			stable:  "KALI_NETBOOT_INITRD_STABLE_URL",
			testing: "KALI_NETBOOT_INITRD_TESTING_URL",
			resolve: func(channel managedSourceReleaseChannel) string {
				return managedInstallerInitrdURLKey(profileKaliLinux, multiOSSourceRoleNetboot, channel)
			},
		},
		{
			name:    "Tails live",
			stable:  "TAILS_LIVE_ISO_STABLE_URL",
			testing: "TAILS_LIVE_ISO_TESTING_URL",
			resolve: func(channel managedSourceReleaseChannel) string {
				return managedISOSourceRoleKey(profileTails, multiOSSourceRolePrimary, channel)
			},
		},
	}

	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			if got := test.resolve(managedSourceReleaseStable); got != test.stable {
				t.Fatalf("stable key = %q, want %q", got, test.stable)
			}
			if got := test.resolve(managedSourceReleaseTesting); got != test.testing {
				t.Fatalf("testing key = %q, want %q", got, test.testing)
			}
		})
	}
}

func TestDebianTestingChannelUsesForkyConfiguredURLKeysForEverySourceRole(t *testing.T) {
	channel := managedSourceReleaseTesting
	wants := map[string]string{
		"live ISO":       "DEBIAN_LIVE_ISO_TESTING_URL",
		"netinst ISO":    "DEBIAN_NETINST_ISO_TESTING_URL",
		"netinst kernel": "DEBIAN_NETINST_VMLINUZ_TESTING_URL",
		"netinst initrd": "DEBIAN_NETINST_INITRD_TESTING_URL",
		"netboot kernel": "DEBIAN_NETBOOT_VMLINUZ_TESTING_URL",
		"netboot initrd": "DEBIAN_NETBOOT_INITRD_TESTING_URL",
	}
	got := map[string]string{
		"live ISO":       managedISOSourceRoleKey(profileDebian, multiOSSourceRolePrimary, channel),
		"netinst ISO":    managedISOSourceRoleKey(profileDebian, multiOSSourceRoleNetinst, channel),
		"netinst kernel": managedInstallerKernelURLKey(profileDebian, multiOSSourceRoleNetinst, channel),
		"netinst initrd": managedInstallerInitrdURLKey(profileDebian, multiOSSourceRoleNetinst, channel),
		"netboot kernel": managedInstallerKernelURLKey(profileDebian, multiOSSourceRoleNetboot, channel),
		"netboot initrd": managedInstallerInitrdURLKey(profileDebian, multiOSSourceRoleNetboot, channel),
	}

	for asset, want := range wants {
		if got[asset] != want {
			t.Fatalf("%s testing key = %q, want %q", asset, got[asset], want)
		}
	}
	if label := managedSourceReleaseProfileLabel(profileDebian, channel); label != "Testing (Forky)" {
		t.Fatalf("Debian testing label = %q, want %q", label, "Testing (Forky)")
	}
}
