package app

import (
	"encoding/json"
	"io"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestSaveListAndDeleteSinglePlannedExecution(t *testing.T) {
	tempDir := t.TempDir()
	backend := &Backend{plannedExecutionDir: tempDir}
	plan := CreatePlan{
		Title:              "Debian USB",
		Profile:            profileDebian,
		WriteMode:          writeModeManaged,
		ISOPath:            "/tmp/debian.iso",
		Persistence:        true,
		PersistenceMode:    persistenceModePlain,
		PersistenceSizeGiB: 16,
	}

	saved, err := backend.SavePlannedExecution(newSinglePlannedExecution(plan, "/dev/sdz"))
	if err != nil {
		t.Fatalf("save planned execution: %v", err)
	}
	if saved.RunID == "" {
		t.Fatalf("expected generated run id")
	}

	planPath := filepath.Join(tempDir, saved.RunID+".json")
	info, err := os.Stat(planPath)
	if err != nil {
		t.Fatalf("stat saved planned execution: %v", err)
	}
	if got := info.Mode().Perm(); got != 0644 {
		t.Fatalf("saved planned execution mode = %04o, want 0644", got)
	}

	executions, err := backend.ListPlannedExecutions()
	if err != nil {
		t.Fatalf("list planned executions: %v", err)
	}
	if len(executions) != 1 {
		t.Fatalf("expected one planned execution, got %#v", executions)
	}
	loaded := executions[0]
	if loaded.Kind != plannedExecutionKindSingle || loaded.SinglePlan == nil {
		t.Fatalf("unexpected loaded execution: %#v", loaded)
	}
	if loaded.TargetDevicePath != "/dev/sdz" {
		t.Fatalf("target path = %q, want /dev/sdz", loaded.TargetDevicePath)
	}
	if loaded.SinglePlan.PersistenceSizeGiB != 16 {
		t.Fatalf("persistence size = %d, want 16", loaded.SinglePlan.PersistenceSizeGiB)
	}

	if err := backend.DeletePlannedExecution(saved.RunID); err != nil {
		t.Fatalf("delete planned execution: %v", err)
	}
	executions, err = backend.ListPlannedExecutions()
	if err != nil {
		t.Fatalf("list after delete: %v", err)
	}
	if len(executions) != 0 {
		t.Fatalf("expected no planned executions after delete, got %#v", executions)
	}
}

func TestRecordSuccessfulPlannedExecutionNeverInvokesSudo(t *testing.T) {
	tempDir := t.TempDir()
	sudoMarkerPath := filepath.Join(tempDir, "sudo-used.txt")
	sudoPath := filepath.Join(tempDir, "sudo")
	sudoScript := "#!/bin/sh\nset -eu\nprintf 'used\\n' >\"" + sudoMarkerPath + "\"\nexit 99\n"
	if err := os.WriteFile(sudoPath, []byte(sudoScript), 0o755); err != nil {
		t.Fatalf("write sudo stub: %v", err)
	}
	t.Setenv("PATH", tempDir+string(os.PathListSeparator)+os.Getenv("PATH"))

	plan := CreatePlan{
		Title:     "Debian USB",
		Profile:   profileDebian,
		WriteMode: writeModeManaged,
		ISOPath:   "/tmp/debian.iso",
	}
	execution := newSinglePlannedExecution(plan, "/dev/sdz")
	application := App{
		backend: &Backend{plannedExecutionDir: "/proc/debian-usb-planned-executions-test"},
	}

	readEnd, writeEnd, err := os.Pipe()
	if err != nil {
		t.Fatalf("create stderr pipe: %v", err)
	}
	originalStderr := os.Stderr
	os.Stderr = writeEnd
	updated := application.recordSuccessfulPlannedExecution(execution, "/dev/sdz")
	_ = writeEnd.Close()
	os.Stderr = originalStderr
	warning, readErr := io.ReadAll(readEnd)
	_ = readEnd.Close()
	if readErr != nil {
		t.Fatalf("read warning: %v", readErr)
	}

	if updated.LastExecutedAt == "" {
		t.Fatal("expected successful in-memory execution timestamp")
	}
	if _, err := os.Stat(sudoMarkerPath); !os.IsNotExist(err) {
		t.Fatalf("post-success bookkeeping invoked sudo")
	}
	if !strings.Contains(string(warning), "USB operation succeeded") || !strings.Contains(string(warning), "without another sudo prompt") {
		t.Fatalf("unexpected post-success warning: %s", warning)
	}
}

func TestSavePlannedExecutionAtomicallyReplacesReadOnlyFile(t *testing.T) {
	tempDir := t.TempDir()
	backend := &Backend{plannedExecutionDir: tempDir}
	plan := CreatePlan{
		Title:     "Debian USB",
		Profile:   profileDebian,
		WriteMode: writeModeManaged,
		ISOPath:   "/tmp/debian.iso",
	}
	execution := newSinglePlannedExecution(plan, "/dev/sdz")
	planPath := filepath.Join(tempDir, execution.RunID+".json")
	if err := os.WriteFile(planPath, []byte("stale\n"), 0o444); err != nil {
		t.Fatalf("write read-only plan fixture: %v", err)
	}

	saved, err := backend.SavePlannedExecutionWithoutSudo(execution)
	if err != nil {
		t.Fatalf("replace read-only plan through writable directory: %v", err)
	}
	payload, err := os.ReadFile(planPath)
	if err != nil {
		t.Fatalf("read replaced plan: %v", err)
	}
	if !strings.Contains(string(payload), saved.RunID) {
		t.Fatalf("replaced plan does not contain run id %q: %s", saved.RunID, payload)
	}
	info, err := os.Stat(planPath)
	if err != nil {
		t.Fatalf("stat replaced plan: %v", err)
	}
	if got := info.Mode().Perm(); got != 0o644 {
		t.Fatalf("replaced plan mode = %04o, want 0644", got)
	}
}

func TestSaveMultiOSPlannedExecution(t *testing.T) {
	backend := &Backend{plannedExecutionDir: t.TempDir()}
	plan := MultiOSPlan{
		SchemaVersion: 1,
		Title:         "Multi-OS USB",
		WriteMode:     writeModeMultiOS,
		Items: []MultiOSPlanItem{
			{ID: "os1", Profile: profileDebian, Title: "Debian", ISOPath: "/tmp/debian.iso", PersistenceSizeGiB: 4},
			{ID: "os2", Profile: profileKaliLinux, Title: "Kali", ISOPath: "/tmp/kali.iso", PersistenceSizeGiB: 8},
		},
	}

	saved, err := backend.SavePlannedExecution(newMultiOSPlannedExecution(plan, "/dev/sdy"))
	if err != nil {
		t.Fatalf("save Multi-OS planned execution: %v", err)
	}
	executions, err := backend.ListPlannedExecutions()
	if err != nil {
		t.Fatalf("list planned executions: %v", err)
	}
	if len(executions) != 1 {
		t.Fatalf("expected one planned execution, got %#v", executions)
	}
	loaded := executions[0]
	if loaded.RunID != saved.RunID || loaded.Kind != plannedExecutionKindMultiOS || loaded.MultiOSPlan == nil {
		t.Fatalf("unexpected loaded Multi-OS execution: %#v", loaded)
	}
	if got := loaded.MultiOSPlan.Items[1].PersistenceSizeGiB; got != 8 {
		t.Fatalf("second item persistence size = %d, want 8", got)
	}
}

func TestSaveOrReusePlannedExecutionReusesMatchingSinglePlan(t *testing.T) {
	backend := &Backend{plannedExecutionDir: t.TempDir()}
	plan := CreatePlan{
		Title:              "Debian USB",
		Profile:            profileDebian,
		WriteMode:          writeModeManaged,
		ISOPath:            "/tmp/debian.iso",
		Persistence:        true,
		PersistenceMode:    persistenceModePlain,
		PersistenceSizeGiB: 16,
	}

	first, created, err := backend.SaveOrReusePlannedExecution(newSinglePlannedExecution(plan, "/dev/sdz"))
	if err != nil {
		t.Fatalf("SaveOrReusePlannedExecution first error: %v", err)
	}
	if !created {
		t.Fatal("expected first SaveOrReusePlannedExecution call to create a saved plan")
	}

	second, created, err := backend.SaveOrReusePlannedExecution(newSinglePlannedExecution(plan, "/dev/sdz"))
	if err != nil {
		t.Fatalf("SaveOrReusePlannedExecution second error: %v", err)
	}
	if created {
		t.Fatal("expected second SaveOrReusePlannedExecution call to reuse the saved plan")
	}
	if second.RunID != first.RunID {
		t.Fatalf("expected reused run id %q, got %q", first.RunID, second.RunID)
	}

	executions, err := backend.ListPlannedExecutions()
	if err != nil {
		t.Fatalf("list planned executions: %v", err)
	}
	if len(executions) != 1 {
		t.Fatalf("expected one saved execution after reuse, got %#v", executions)
	}
	if executions[0].LastReviewedAt == "" {
		t.Fatalf("expected LastReviewedAt to be recorded, got %#v", executions[0])
	}
}

func TestSavePlannedExecutionNormalizesLegacyNetinstOverrides(t *testing.T) {
	tempDir := t.TempDir()
	backend := &Backend{plannedExecutionDir: tempDir}
	execution := PlannedExecution{
		SchemaVersion: plannedExecutionSchemaVersion,
		RunID:         "multios-legacy",
		Kind:          plannedExecutionKindMultiOS,
		Title:         "Multi-OS USB",
		CreatedAt:     "2026-05-10T00:00:00Z",
		UpdatedAt:     "2026-05-10T00:00:00Z",
		MultiOSPlan: &MultiOSPlan{
			SchemaVersion: 1,
			Title:         "Multi-OS USB",
			WriteMode:     writeModeMultiOS,
			Items: []MultiOSPlanItem{
				{ID: "os1", Profile: profileDebian, SourceRole: multiOSSourceRolePrimary, Title: "Debian", ISOPath: "/tmp/debian.iso"},
				{
					ID:                   "os2",
					Profile:              profileDebian,
					SourceRole:           multiOSSourceRoleNetinst,
					Title:                "Debian Netinst",
					ISOPath:              "/tmp/debian-netinst.iso",
					LiveToram:            true,
					Persistence:          true,
					PersistenceMode:      persistenceModePlain,
					PersistenceSizeGiB:   8,
					PersistenceFSLabel:   "DEBIAN-PERSIST",
					PersistencePartLabel: "DEBIAN-PERSIST",
					MenuLabel:            "Bad Label",
					KernelArgs:           "boot=live quiet",
					KernelPath:           "/live/vmlinuz",
					InitrdPath:           "/live/initrd.img",
				},
			},
		},
	}

	saved, err := backend.SavePlannedExecution(execution)
	if err != nil {
		t.Fatalf("save planned execution: %v", err)
	}
	netinst := saved.MultiOSPlan.Items[1]
	if netinst.LiveToram || netinst.Persistence || netinst.PersistenceMode != "" || netinst.PersistenceSizeGiB != 0 {
		t.Fatalf("expected netinst live-only settings to be cleared, got %#v", netinst)
	}
	if netinst.PersistenceFSLabel != "" || netinst.PersistencePartLabel != "" {
		t.Fatalf("expected netinst persistence labels to be cleared, got %#v", netinst)
	}
	if netinst.MenuLabel != "" || netinst.KernelArgs != "" || netinst.KernelPath != "" || netinst.InitrdPath != "" {
		t.Fatalf("expected netinst live override fields to be cleared, got %#v", netinst)
	}

	payload, err := os.ReadFile(filepath.Join(tempDir, "multios-legacy.json"))
	if err != nil {
		t.Fatalf("read saved plan: %v", err)
	}
	var persisted PlannedExecution
	if err := json.Unmarshal(payload, &persisted); err != nil {
		t.Fatalf("unmarshal saved plan: %v", err)
	}
	persistedNetinst := persisted.MultiOSPlan.Items[1]
	if persistedNetinst.MenuLabel != "" || persistedNetinst.KernelArgs != "" || persistedNetinst.KernelPath != "" || persistedNetinst.InitrdPath != "" {
		t.Fatalf("expected persisted netinst live override fields to be cleared, got %#v", persistedNetinst)
	}
}

func TestListPlannedExecutionsNormalizesLegacyNetinstOverrides(t *testing.T) {
	tempDir := t.TempDir()
	execution := PlannedExecution{
		SchemaVersion: plannedExecutionSchemaVersion,
		RunID:         "multios-legacy-load",
		Kind:          plannedExecutionKindMultiOS,
		Title:         "Multi-OS USB",
		CreatedAt:     "2026-05-10T00:00:00Z",
		UpdatedAt:     "2026-05-10T00:00:00Z",
		MultiOSPlan: &MultiOSPlan{
			SchemaVersion: 1,
			Title:         "Multi-OS USB",
			WriteMode:     writeModeMultiOS,
			Items: []MultiOSPlanItem{
				{ID: "os1", Profile: profileDebian, SourceRole: multiOSSourceRolePrimary, Title: "Debian", ISOPath: "/tmp/debian.iso"},
				{
					ID:         "os2",
					Profile:    profileDebian,
					SourceRole: multiOSSourceRoleNetinst,
					Title:      "Debian Netinst",
					ISOPath:    "/tmp/debian-netinst.iso",
					MenuLabel:  "Bad Label",
					KernelArgs: "boot=live quiet",
					KernelPath: "/live/vmlinuz",
					InitrdPath: "/live/initrd.img",
				},
			},
		},
	}
	payload, err := marshalPlannedExecution(execution)
	if err != nil {
		t.Fatalf("marshal planned execution: %v", err)
	}
	if err := os.WriteFile(filepath.Join(tempDir, "multios-legacy-load.json"), payload, 0644); err != nil {
		t.Fatalf("write legacy plan: %v", err)
	}

	backend := &Backend{plannedExecutionDir: tempDir}
	executions, err := backend.ListPlannedExecutions()
	if err != nil {
		t.Fatalf("list planned executions: %v", err)
	}
	if len(executions) != 1 {
		t.Fatalf("expected one planned execution, got %#v", executions)
	}
	netinst := executions[0].MultiOSPlan.Items[1]
	if netinst.MenuLabel != "" || netinst.KernelArgs != "" || netinst.KernelPath != "" || netinst.InitrdPath != "" {
		t.Fatalf("expected loaded netinst live override fields to be cleared, got %#v", netinst)
	}
}
