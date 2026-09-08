package app

import (
	"bufio"
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func testPreseedTree(t *testing.T, name string) string {
	t.Helper()
	dir := filepath.Join(t.TempDir(), name)
	if err := os.MkdirAll(dir, 0750); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, "preseed.cfg"), []byte("# test profile\n"), 0600); err != nil {
		t.Fatal(err)
	}
	return dir
}

func TestHDMediaPromptDeclineNeverSelectsConfiguredHostTrees(t *testing.T) {
	dir := testPreseedTree(t, "configured")
	a := App{reader: bufio.NewReader(strings.NewReader("n\nn\n")), config: RuntimeConfig{
		ExtraValues: map[string]string{"PRESEED_HOST_DEBIAN_DE_PATH": dir, "PRESEED_HOST_DEBIAN_SRV_PATH": dir},
	}}
	selected, err := a.promptHDMediaPreseedDirs(profileDebian)
	if err != nil || len(selected) != 0 {
		t.Fatalf("declined staging must be empty: %v, %v", selected, err)
	}
}

func TestHDMediaPromptCopiesExplicitDesktopAndServerParents(t *testing.T) {
	desktop, server := testPreseedTree(t, "desktop space"), testPreseedTree(t, "server")
	input := "y\n" + filepath.Join(desktop, "preseed.cfg") + "\ny\n" + filepath.Join(server, "preseed.cfg") + "\n"
	a := App{reader: bufio.NewReader(strings.NewReader(input))}
	selected, err := a.promptHDMediaPreseedDirs(profileKaliLinux)
	if err != nil {
		t.Fatal(err)
	}
	if selected["desktop"] != desktop || selected["server"] != server {
		t.Fatalf("wrong selected codebases: %v", selected)
	}
}

func TestHDMediaValidationRejectsNonInstallerAndInvalidProfiles(t *testing.T) {
	dir := testPreseedTree(t, "codebase")
	for _, tc := range []struct{ profile, role, flavor, path string }{
		{profileDebian, "primary", "desktop", dir},
		{profileKaliLinux, "netinst", "typo", dir},
		{profileDebian, "netboot", "server", filepath.Join(dir, "missing")},
	} {
		if _, err := normalizeHDMediaPreseedDirs(tc.profile, tc.role, map[string]string{tc.flavor: tc.path}); err == nil {
			t.Fatalf("expected validation failure: %+v", tc)
		}
	}
	if _, err := normalizeHDMediaPreseedDirs(profileDebian, "primary", nil); err != nil {
		t.Fatal(err)
	}
}

func TestHDMediaSavedPlanRoundTripDoesNotAliasOriginalMap(t *testing.T) {
	plan := CreatePlan{Profile: profileDebian, SourceRole: "netinst", HDMediaPreseedDirs: map[string]string{"desktop": "/code/de", "server": "/code/srv"}}
	data, err := json.Marshal(plan)
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(data), `"hd_media_preseed_dirs"`) {
		t.Fatalf("missing persisted consent: %s", data)
	}
	var restored CreatePlan
	if err := json.Unmarshal(data, &restored); err != nil {
		t.Fatal(err)
	}
	req := createRequestFromPlan(restored)
	req.HDMediaPreseedDirs["desktop"] = "/edited"
	if restored.HDMediaPreseedDirs["desktop"] != "/code/de" || req.HDMediaPreseedDirs["server"] != "/code/srv" {
		t.Fatal("profile consent did not round-trip independently")
	}
}
