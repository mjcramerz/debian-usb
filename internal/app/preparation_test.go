package app

import (
	"bufio"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// The process boundary is real, but every destructive/system command is a stub.
// Every invocation is recorded, so ordering and forbidden work are observable.
func pipelineFixture(t *testing.T) (*Backend, string, []SourceInput) {
	t.Helper()
	root := t.TempDir()
	var inputs []SourceInput
	for _, name := range []string{"debian.iso", "kali.iso"} {
		path := filepath.Join(root, name)
		if err := os.WriteFile(path, []byte("original ISO bytes"), 0644); err != nil {
			t.Fatal(err)
		}
		input, err := localSourceInput(path, true)
		if err != nil {
			t.Fatal(err)
		}
		inputs = append(inputs, input)
	}
	script := fmt.Sprintf(`#!/bin/sh
set -eu
command=${1-}
printf '%%s' "$command" >>'%[1]s/trace'
for arg in "$@"; do printf '|%%s' "$arg" >>'%[1]s/trace'; done
printf '\n' >>'%[1]s/trace'
source=''; profile=''; plan=''
while [ "$#" -gt 0 ]; do
 case "$1" in
  --source-iso|--iso-path) source=$2; shift 2;;
  --profile) profile=$2; shift 2;;
  --multi-os-plan) plan=$2; shift 2;;
  *) shift;;
 esac
done
case "$command" in
 list-devices)
  serial=reviewed
  if [ "${SWAP_DEVICE:-0}" = 1 ] && [ -f '%[1]s/built' ]; then serial=replaced; fi
  printf '{"devices":[]}' >/dev/null
  printf '[{"path":"/dev/test-usb","removable":true,"size_bytes":64000000000,"model":"SanDisk 3.2","serial":"%%s","transport":"usb"}]\n' "$serial";;
 list-local-isos) printf '[]\n';;
 inspect-iso)
  crypto=false
  case "$source" in *-prepared.iso) [ "${NO_CRYPTO:-0}" = 1 ] || crypto=true;; esac
  printf '{"iso_path":"%%s","media_class":"live","managed_supported":true,"supports_persistence":true,"supports_encrypted_persistence":%%s,"managed_payload_layout":"iso-store","best_live_title":"Live"}\n' "$source" "$crypto";;
 remaster-live-tools-source)
  if [ "${FAIL_PROFILE:-}" = "$profile" ]; then echo 'simulated preparation failure' >&2; exit 9; fi
  result=${source%%.iso}-prepared.iso
  cp -- "$source" "$result"
  touch '%[1]s/built'
  printf '{"iso_path":"%%s"}\n' "$result";;
 --config)
  printf 'writer\n' >>'%[1]s/trace'
  if [ -n "$plan" ]; then cp -- "$plan" '%[1]s/written-plan.json'; fi;;
 *) echo 'forbidden or unexpected helper' >&2; exit 99;;
esac
`, root)
	helper := filepath.Join(root, "helper.sh")
	if err := os.WriteFile(helper, []byte(script), 0755); err != nil {
		t.Fatal(err)
	}
	return &Backend{pythonHelper: helper, writeHelper: helper, configPath: "unused.conf", plannedExecutionDir: filepath.Join(root, "plans"), effectiveUID: func() int { return 0 }}, root, inputs
}

func pipelineTrace(t *testing.T, root string) string {
	t.Helper()
	b, err := os.ReadFile(filepath.Join(root, "trace"))
	if os.IsNotExist(err) {
		return ""
	}
	if err != nil {
		t.Fatal(err)
	}
	return string(b)
}
func pipelinePlan(input SourceInput) CreatePlan {
	return CreatePlan{Profile: profileDebian, SourceRole: multiOSSourceRolePrimary, WriteMode: writeModeManaged,
		ISOPath: input.Path, MediaClass: "live", Preparation: &SourcePreparation{ISO: input}, LiveToolGroups: []string{"nvme"}, Persistence: true, PersistenceMode: persistenceModeEncrypted, PersistenceSizeGiB: 8}
}
func pipelineMulti(inputs []SourceInput) MultiOSPlan {
	plan := MultiOSPlan{WriteMode: writeModeMultiOS}
	for i, input := range inputs {
		profile := profileDebian
		if i > 0 {
			profile = profileKaliLinux
		}
		plan.Items = append(plan.Items, MultiOSPlanItem{ID: profile, Profile: profile, Title: profile, SourceRole: multiOSSourceRolePrimary,
			ISOPath: input.Path, PayloadISOName: filepath.Base(input.Path), MediaClass: "live", Preparation: &SourcePreparation{ISO: input}, LiveToolGroups: []string{"nvme"}, Persistence: true, PersistenceMode: persistenceModeEncrypted, PersistenceSizeGiB: 8})
	}
	return plan
}

func TestSingleExecutionCombinesOverlayToolsAndAutomaticCryptoOnce(t *testing.T) {
	b, root, inputs := pipelineFixture(t)
	plan := pipelinePlan(inputs[0])
	overlay := filepath.Join(root, "overlay")
	if err := os.Mkdir(overlay, 0755); err != nil {
		t.Fatal(err)
	}
	plan.Preparation.InitrdOverlayDir = overlay
	if err := b.executeCreate(plan, "/dev/test-usb", 8, false); err != nil {
		t.Fatal(err)
	}
	trace := pipelineTrace(t, root)
	if strings.Count(trace, "remaster-live-tools-source|remaster-live-tools-source") != 1 {
		t.Fatalf("expected one combined remaster: %s", trace)
	}
	for _, option := range []string{"|--group|nvme", "|--overlay-dir|" + overlay, "|--ensure-encrypted-persistence", "debian-prepared.iso"} {
		if !strings.Contains(trace, option) {
			t.Fatalf("missing %q: %s", option, trace)
		}
	}
	if !strings.HasSuffix(trace, "writer\n") {
		t.Fatalf("writer must run last: %s", trace)
	}
	original, err := os.ReadFile(inputs[0].Path)
	if err != nil || string(original) != "original ISO bytes" {
		t.Fatal("source ISO changed")
	}
}

func TestRawExecutionNeverRemasters(t *testing.T) {
	b, root, inputs := pipelineFixture(t)
	plan := pipelinePlan(inputs[0])
	plan.WriteMode = writeModeDirect
	plan.Persistence = false
	plan.PersistenceMode = persistenceModeNone
	plan.LiveToolGroups = nil
	if err := b.executeCreate(plan, "/dev/test-usb", 0, false); err != nil {
		t.Fatal(err)
	}
	trace := pipelineTrace(t, root)
	if strings.Contains(trace, "remaster") || !strings.HasSuffix(trace, "writer\n") {
		t.Fatalf("raw execution: %s", trace)
	}
}

func TestMissingFinalCryptoSupportPreventsDeviceWrite(t *testing.T) {
	t.Setenv("NO_CRYPTO", "1")
	b, root, inputs := pipelineFixture(t)
	err := b.executeCreate(pipelinePlan(inputs[0]), "/dev/test-usb", 8, false)
	if err == nil || !strings.Contains(err.Error(), "cryptsetup/LUKS") {
		t.Fatalf("expected crypto verification error: %v", err)
	}
	if strings.Contains(pipelineTrace(t, root), "writer") {
		t.Fatal("writer ran after failed crypto verification")
	}
}

func TestMultiOSPreparesEverySourceBeforeOneWriteAndUpdatesExactFilenames(t *testing.T) {
	b, root, inputs := pipelineFixture(t)
	plan := pipelineMulti(inputs)
	if err := b.executeMultiOSCreate(plan, "/dev/test-usb", false); err != nil {
		t.Fatal(err)
	}
	trace := pipelineTrace(t, root)
	if strings.Count(trace, "remaster-live-tools-source|remaster-live-tools-source") != 2 || strings.Count(trace, "\nwriter\n") != 1 || !strings.HasSuffix(trace, "writer\n") {
		t.Fatalf("incorrect ordering: %s", trace)
	}
	var written MultiOSPlan
	raw, err := os.ReadFile(filepath.Join(root, "written-plan.json"))
	if err != nil {
		t.Fatal(err)
	}
	if err = json.Unmarshal(raw, &written); err != nil {
		t.Fatal(err)
	}
	for i, item := range written.Items {
		if !strings.HasSuffix(item.ISOPath, "-prepared.iso") || item.PayloadISOName != filepath.Base(item.ISOPath) || item.Preparation != nil {
			t.Fatalf("bad writer item: %#v", item)
		}
		if plan.Items[i].ISOPath != inputs[i].Path || plan.Items[i].Preparation == nil {
			t.Fatal("execution mutated reusable source plan")
		}
	}
	if !strings.Contains(trace, "|--live-tools-prepared|1") {
		t.Fatal("writer could rebuild sources again")
	}
}

func TestMultiOSFailureNeverStartsWriter(t *testing.T) {
	t.Setenv("FAIL_PROFILE", profileKaliLinux)
	b, root, inputs := pipelineFixture(t)
	if err := b.executeMultiOSCreate(pipelineMulti(inputs), "/dev/test-usb", false); err == nil {
		t.Fatal("expected source failure")
	}
	if strings.Contains(pipelineTrace(t, root), "writer") {
		t.Fatal("writer ran before all sources were ready")
	}
}

func TestChangedMultiOSInputRejectsPlanBeforeAnyPreparation(t *testing.T) {
	b, root, inputs := pipelineFixture(t)
	plan := pipelineMulti(inputs)
	if err := os.WriteFile(inputs[1].Path, []byte("changed since review"), 0644); err != nil {
		t.Fatal(err)
	}
	if err := b.executeMultiOSCreate(plan, "/dev/test-usb", false); err == nil {
		t.Fatal("expected changed-source error")
	}
	if pipelineTrace(t, root) != "" {
		t.Fatal("source preparation ran before all input snapshots were checked")
	}
}

func TestHotplugIdentityChangeDuringBuildPreventsWrite(t *testing.T) {
	t.Setenv("SWAP_DEVICE", "1")
	b, root, inputs := pipelineFixture(t)
	plan := pipelinePlan(inputs[0])
	plan.TargetDevice = &Device{Path: "/dev/test-usb", Removable: true, SizeBytes: 64000000000, Model: "SanDisk 3.2", Serial: "reviewed", Transport: "usb"}
	err := b.executeCreate(plan, "/dev/test-usb", 8, false)
	if err == nil || !strings.Contains(err.Error(), "identity changed") {
		t.Fatalf("expected target drift refusal: %v", err)
	}
	if strings.Contains(pipelineTrace(t, root), "writer") {
		t.Fatal("writer ran on replacement disk")
	}
}

func TestCancelAfterDeviceReviewDoesNotPrepareOrWrite(t *testing.T) {
	b, root, inputs := pipelineFixture(t)
	b.initrdRoot = filepath.Join(root, "initrd")
	if err := os.MkdirAll(filepath.Join(b.initrdRoot, "debian/live"), 0755); err != nil {
		t.Fatal(err)
	}
	cfg, err := loadRuntimeConfig(repoConfigPath())
	if err != nil {
		t.Fatal(err)
	}
	cfg.ManagedSourceURLs = nil
	a := App{backend: b, config: cfg, reader: bufio.NewReader(strings.NewReader("1\n" + inputs[0].Path + "\n2\na\n1\nn\n1\nn\n"))}
	action, err := a.handleCreate(profileDebian)
	if err != nil || action != menuStay {
		t.Fatalf("cancel flow: %v %v", action, err)
	}
	trace := pipelineTrace(t, root)
	if strings.Contains(trace, "remaster") || strings.Contains(trace, "writer") || strings.Contains(trace, "download") {
		t.Fatalf("work occurred before approval: %s", trace)
	}
	saved, err := b.ListPlannedExecutions()
	if err != nil || len(saved) != 1 || saved[0].SinglePlan.Preparation == nil {
		t.Fatalf("pending plan not saved: %#v %v", saved, err)
	}
}

func TestDeferredSourcePlanRoundTripPreservesRoleAndInputOptions(t *testing.T) {
	original := CreatePlan{Profile: profileDebian, SourceRole: multiOSSourceRoleNetboot, Preparation: &SourcePreparation{
		Kernel: SourceInput{URLKey: "KERNEL", URL: "https://example.test/linux"}, Initrd: SourceInput{URLKey: "INITRD", URL: "https://example.test/initrd.gz"}, InitrdPreseedPath: "/config/preseed.cfg", InitrdOverlayDir: "/config/overlay", ExtraModules: []string{"lz4"}}}
	data, err := json.Marshal(original)
	if err != nil {
		t.Fatal(err)
	}
	var decoded CreatePlan
	if err = json.Unmarshal(data, &decoded); err != nil {
		t.Fatal(err)
	}
	req := createRequestFromPlan(decoded)
	if req.SourceRole != multiOSSourceRoleNetboot || req.Preparation == nil || req.Preparation.Initrd.URL != original.Preparation.Initrd.URL || req.Preparation.InitrdOverlayDir != "/config/overlay" {
		t.Fatalf("lost deferred inputs: %#v", req)
	}
	req.Preparation.ExtraModules[0] = "changed"
	if decoded.Preparation.ExtraModules[0] != "lz4" {
		t.Fatal("editing mutated source plan")
	}
}

func TestTerminalTextAndMenuMovement(t *testing.T) {
	if menuMove(0, -1, 3) != 2 || menuMove(2, 1, 3) != 0 || menuMove(0, 5, 0) != 0 {
		t.Fatal("menu wrap")
	}
	if strings.ContainsAny(safeTerminalText("disk\x1b[31m\n\u202e"), "\x1b\n\u202e") {
		t.Fatal("terminal escape or direction override survived")
	}
	t.Setenv("NO_COLOR", "1")
	if strings.Contains(styled("disk", "1;31"), "\x1b") {
		t.Fatal("NO_COLOR ignored")
	}
}

func TestCancelledStandaloneRebuildDoesNotInstallDependencies(t *testing.T) {
	application := &App{reader: bufio.NewReader(strings.NewReader("n\n"))}
	// A nil backend would panic if dependency installation ran before consent.
	action, err := application.executeRebuildInstallerISOPlan(RebuildInstallerISOInspection{}, RebuildInstallerISOPlan{})
	if err != nil || action != menuStay {
		t.Fatalf("cancelling the final review should not execute work: %v / %v", action, err)
	}
}
