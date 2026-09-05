package app

import (
	"strings"
	"testing"
	"unicode/utf8"
)

func TestWrapTextWrapsLongTokensAndParagraphs(t *testing.T) {
	lines := wrapText("alpha beta /this/path/is/long enough", 12)
	if len(lines) < 3 {
		t.Fatalf("expected wrapped output, got %#v", lines)
	}
	for _, line := range lines {
		if utf8.RuneCountInString(line) > 12 {
			t.Fatalf("line exceeds wrap width: %q", line)
		}
	}
	if !strings.Contains(strings.Join(lines, " "), "alpha beta") {
		t.Fatalf("expected wrapped content to preserve words, got %#v", lines)
	}
}

func TestDeviceMenuDetailIncludesStateAndTransport(t *testing.T) {
	detail := deviceMenuDetail(Device{
		Name:      "sdb",
		Model:     "Fast Stick",
		Transport: "usb",
		SizeHuman: "57.3 GiB",
		Mounted:   true,
	})
	for _, fragment := range []string{"57.3 GiB", "Fast Stick", "usb", "mounted"} {
		if !strings.Contains(detail, fragment) {
			t.Fatalf("expected %q in detail %q", fragment, detail)
		}
	}
}

func TestPolicyMenuEntriesMarksCurrentDefault(t *testing.T) {
	entries := policyMenuEntries(liveBootPolicyOptions, "balanced")
	if len(entries) == 0 {
		t.Fatal("expected menu entries")
	}
	if !strings.Contains(entries[0].Detail, "Current default.") {
		t.Fatalf("expected current marker in %q", entries[0].Detail)
	}
	if entries[len(entries)-1].Key != "b" {
		t.Fatalf("expected cancel entry at end, got %#v", entries[len(entries)-1])
	}
}

func TestProfileOverrideSummaryUsesCompactStates(t *testing.T) {
	application := App{
		config: RuntimeConfig{
			ProfileLiveKernelExtras: map[string]string{
				profileDebian: "components",
			},
			ProfileForensicsKernelExtras: map[string]string{},
			ProfileInstallerKernelExtras: map[string]string{
				profileDebian: "",
			},
			ProfilePreseedURLs: map[string]string{
				profileDebian: "",
			},
		},
	}

	summary := application.profileOverrideSummary(profileSpecs[profileDebian])
	for _, fragment := range []string{"live set", "forensics default", "installer default", "URL default"} {
		if !strings.Contains(summary, fragment) {
			t.Fatalf("expected %q in summary %q", fragment, summary)
		}
	}
}
