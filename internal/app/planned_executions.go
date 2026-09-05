package app

import (
	"bytes"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strings"
	"time"
)

var plannedExecutionIDPattern = regexp.MustCompile(`^[a-z0-9][a-z0-9._-]{0,79}$`)

func newSinglePlannedExecution(plan CreatePlan, targetDevicePath string) PlannedExecution {
	now := currentTimestampUTC()
	runID := newPlannedExecutionID(plannedExecutionKindSingle)
	return PlannedExecution{
		SchemaVersion:    plannedExecutionSchemaVersion,
		RunID:            runID,
		Kind:             plannedExecutionKindSingle,
		Title:            plan.Title,
		CreatedAt:        now,
		UpdatedAt:        now,
		LastReviewedAt:   now,
		TargetDevicePath: strings.TrimSpace(targetDevicePath),
		SinglePlan:       &plan,
	}
}

func newMultiOSPlannedExecution(plan MultiOSPlan, targetDevicePath string) PlannedExecution {
	now := currentTimestampUTC()
	runID := newPlannedExecutionID(plannedExecutionKindMultiOS)
	return PlannedExecution{
		SchemaVersion:    plannedExecutionSchemaVersion,
		RunID:            runID,
		Kind:             plannedExecutionKindMultiOS,
		Title:            plan.Title,
		CreatedAt:        now,
		UpdatedAt:        now,
		LastReviewedAt:   now,
		TargetDevicePath: strings.TrimSpace(targetDevicePath),
		MultiOSPlan:      &plan,
	}
}

func currentTimestampUTC() string {
	return time.Now().UTC().Format(time.RFC3339)
}

func newPlannedExecutionID(kind string) string {
	prefix := "execution"
	switch kind {
	case plannedExecutionKindSingle:
		prefix = "single"
	case plannedExecutionKindMultiOS:
		prefix = "multios"
	}
	return fmt.Sprintf("%s-%s", prefix, time.Now().UTC().Format("20060102t150405000000000z"))
}

func (execution PlannedExecution) fileName() (string, error) {
	runID := strings.TrimSpace(execution.RunID)
	if !plannedExecutionIDPattern.MatchString(runID) {
		return "", fmt.Errorf("invalid planned execution id: %s", runID)
	}
	return runID + ".json", nil
}

func (execution PlannedExecution) displayTitle() string {
	if title := strings.TrimSpace(execution.Title); title != "" {
		return title
	}
	switch execution.Kind {
	case plannedExecutionKindSingle:
		if execution.SinglePlan != nil {
			return execution.SinglePlan.Title
		}
	case plannedExecutionKindMultiOS:
		if execution.MultiOSPlan != nil {
			return execution.MultiOSPlan.Title
		}
	}
	return execution.RunID
}

func (execution PlannedExecution) summary() string {
	switch execution.Kind {
	case plannedExecutionKindSingle:
		if execution.SinglePlan == nil {
			return "single plan unavailable"
		}
		plan := *execution.SinglePlan
		return fmt.Sprintf("%s | %s | %s", profileLabel(plan.Profile), plan.WriteMode, filepath.Base(plan.ISOPath))
	case plannedExecutionKindMultiOS:
		if execution.MultiOSPlan == nil {
			return "Multi-OS plan unavailable"
		}
		return fmt.Sprintf("%d payload(s) | %s", len(execution.MultiOSPlan.Items), strings.Join(multiOSItemTitles(execution.MultiOSPlan.Items), ", "))
	default:
		return "unknown planned execution"
	}
}

func markPlannedExecutionReviewed(execution PlannedExecution) PlannedExecution {
	execution.LastReviewedAt = currentTimestampUTC()
	return execution
}

func markPlannedExecutionExecuted(execution PlannedExecution, targetDevicePath string) PlannedExecution {
	execution.TargetDevicePath = strings.TrimSpace(targetDevicePath)
	execution.LastExecutedAt = currentTimestampUTC()
	return execution
}

func profileLabel(profile string) string {
	if spec, ok := profileSpecs[profile]; ok {
		return spec.MultiOSLabel
	}
	return profile
}

func (b *Backend) plannedExecutionsDir() string {
	if dir := strings.TrimSpace(b.plannedExecutionDir); dir != "" {
		return dir
	}
	return "/data/cfg/debian-usb/planned-executions"
}

func (b *Backend) ListPlannedExecutions() ([]PlannedExecution, error) {
	planDir := b.plannedExecutionsDir()
	entries, err := os.ReadDir(planDir)
	if err != nil {
		if os.IsNotExist(err) {
			return nil, nil
		}
		return nil, fmt.Errorf("failed to read planned executions directory %s: %w", planDir, err)
	}
	executions := make([]PlannedExecution, 0, len(entries))
	for _, entry := range entries {
		if entry.IsDir() || filepath.Ext(entry.Name()) != ".json" {
			continue
		}
		path := filepath.Join(planDir, entry.Name())
		execution, err := loadPlannedExecutionFile(path)
		if err != nil {
			return nil, err
		}
		execution.path = path
		executions = append(executions, execution)
	}
	sort.Slice(executions, func(i, j int) bool {
		return executions[i].UpdatedAt > executions[j].UpdatedAt
	})
	return executions, nil
}

func loadPlannedExecutionFile(path string) (PlannedExecution, error) {
	payload, err := os.ReadFile(path)
	if err != nil {
		return PlannedExecution{}, fmt.Errorf("failed to read planned execution %s: %w", path, err)
	}
	var execution PlannedExecution
	if err := json.Unmarshal(payload, &execution); err != nil {
		return PlannedExecution{}, fmt.Errorf("failed to parse planned execution %s: %w", path, err)
	}
	execution = normalizePlannedExecution(execution)
	if err := validatePlannedExecution(execution); err != nil {
		return PlannedExecution{}, fmt.Errorf("invalid planned execution %s: %w", path, err)
	}
	return execution, nil
}

func validatePlannedExecution(execution PlannedExecution) error {
	if execution.SchemaVersion != plannedExecutionSchemaVersion {
		return fmt.Errorf("unsupported schema_version: %d", execution.SchemaVersion)
	}
	if !plannedExecutionIDPattern.MatchString(strings.TrimSpace(execution.RunID)) {
		return fmt.Errorf("invalid run_id: %s", execution.RunID)
	}
	switch execution.Kind {
	case plannedExecutionKindSingle:
		if execution.SinglePlan == nil || execution.MultiOSPlan != nil {
			return fmt.Errorf("single planned execution must contain single_plan only")
		}
	case plannedExecutionKindMultiOS:
		if execution.MultiOSPlan == nil || execution.SinglePlan != nil {
			return fmt.Errorf("Multi-OS planned execution must contain multi_os_plan only")
		}
	default:
		return fmt.Errorf("unsupported planned execution kind: %s", execution.Kind)
	}
	return nil
}

func (b *Backend) SavePlannedExecution(execution PlannedExecution) (PlannedExecution, error) {
	return b.savePlannedExecution(execution, true)
}

func (b *Backend) SavePlannedExecutionWithoutSudo(execution PlannedExecution) (PlannedExecution, error) {
	return b.savePlannedExecution(execution, false)
}

func (b *Backend) savePlannedExecution(execution PlannedExecution, allowSudo bool) (PlannedExecution, error) {
	execution = normalizePlannedExecution(execution)
	if strings.TrimSpace(execution.RunID) == "" {
		execution.RunID = newPlannedExecutionID(execution.Kind)
	}
	now := currentTimestampUTC()
	if strings.TrimSpace(execution.CreatedAt) == "" {
		execution.CreatedAt = now
	}
	execution.UpdatedAt = now
	if strings.TrimSpace(execution.Title) == "" {
		execution.Title = execution.displayTitle()
	}
	execution.SchemaVersion = plannedExecutionSchemaVersion
	if err := validatePlannedExecution(execution); err != nil {
		return PlannedExecution{}, err
	}
	fileName, err := execution.fileName()
	if err != nil {
		return PlannedExecution{}, err
	}
	planPath := filepath.Join(b.plannedExecutionsDir(), fileName)
	data, err := marshalPlannedExecution(execution)
	if err != nil {
		return PlannedExecution{}, err
	}
	var writeErr error
	if allowSudo {
		writeErr = b.writeManagedFile(planPath, data, 0o644)
	} else {
		writeErr = writeManagedFileWithoutSudo(planPath, data, 0o644)
	}
	if writeErr != nil {
		return PlannedExecution{}, writeErr
	}
	execution.path = planPath
	return execution, nil
}

func (a *App) recordSuccessfulPlannedExecution(execution PlannedExecution, targetDevicePath string) PlannedExecution {
	execution = markPlannedExecutionExecuted(execution, targetDevicePath)
	saved, err := a.backend.SavePlannedExecutionWithoutSudo(execution)
	if err != nil {
		fmt.Fprintf(
			os.Stderr,
			"debian-usb: USB operation succeeded; planned execution %s was not updated because the metadata path is not writable without another sudo prompt: %v\n",
			execution.RunID,
			err,
		)
		return execution
	}
	return saved
}

func (b *Backend) SaveOrReusePlannedExecution(execution PlannedExecution) (PlannedExecution, bool, error) {
	execution = normalizePlannedExecution(execution)
	saved, err := b.ListPlannedExecutions()
	if err != nil {
		return PlannedExecution{}, false, err
	}
	for _, candidate := range saved {
		same, err := samePlannedExecution(candidate, execution)
		if err != nil {
			return PlannedExecution{}, false, err
		}
		if !same {
			continue
		}
		if strings.TrimSpace(execution.LastReviewedAt) != "" {
			candidate.LastReviewedAt = execution.LastReviewedAt
		}
		if strings.TrimSpace(execution.LastExecutedAt) != "" {
			candidate.LastExecutedAt = execution.LastExecutedAt
		}
		if strings.TrimSpace(execution.TargetDevicePath) != "" {
			candidate.TargetDevicePath = execution.TargetDevicePath
		}
		if execution.SinglePlan != nil {
			plan := *execution.SinglePlan
			candidate.SinglePlan = &plan
			candidate.MultiOSPlan = nil
		}
		if execution.MultiOSPlan != nil {
			plan := *execution.MultiOSPlan
			candidate.MultiOSPlan = &plan
			candidate.SinglePlan = nil
		}
		candidate.Title = execution.displayTitle()
		updated, err := b.SavePlannedExecution(candidate)
		if err != nil {
			return PlannedExecution{}, false, err
		}
		return updated, false, nil
	}
	created, err := b.SavePlannedExecution(execution)
	if err != nil {
		return PlannedExecution{}, false, err
	}
	return created, true, nil
}

func normalizePlannedExecution(execution PlannedExecution) PlannedExecution {
	if execution.SinglePlan != nil {
		normalizedPlan := *execution.SinglePlan
		if normalizedPlan.WriteMode == writeModeManaged {
			normalizedPlan.SecureBootTrust = normalizeSecureBootTrustMode(normalizedPlan.SecureBootTrust)
			if normalizedPlan.SecureBootTrust == "" {
				normalizedPlan.SecureBootTrust = secureBootTrustMOK
			}
		}
		execution.SinglePlan = &normalizedPlan
	}
	if execution.MultiOSPlan == nil {
		return execution
	}
	normalizedPlan := *execution.MultiOSPlan
	normalizedPlan.SecureBootTrust = normalizeSecureBootTrustMode(normalizedPlan.SecureBootTrust)
	if normalizedPlan.SecureBootTrust == "" {
		normalizedPlan.SecureBootTrust = secureBootTrustMOK
	}
	normalizedPlan.Items = append([]MultiOSPlanItem(nil), normalizedPlan.Items...)
	for index := range normalizedPlan.Items {
		item := normalizedPlan.Items[index]
		sourceRole := strings.TrimSpace(item.SourceRole)
		if sourceRole == "" {
			sourceRole = multiOSSourceRolePrimary
		}
		item.SourceRole = sourceRole
		if sourceRole == multiOSSourceRoleNetinst || sourceRole == multiOSSourceRoleNetboot {
			item.LiveToram = false
			item.LiveToolGroups = nil
			item.Persistence = false
			item.PersistenceMode = persistenceModeNone
			item.PersistenceSizeGiB = 0
			item.PersistenceFSLabel = ""
			item.PersistencePartLabel = ""
			item.MenuLabel = ""
			item.KernelArgs = ""
			item.KernelPath = ""
			item.InitrdPath = ""
		} else {
			item.Preseed = false
		}
		normalizedPlan.Items[index] = item
	}
	execution.MultiOSPlan = &normalizedPlan
	return execution
}

type comparablePlannedExecution struct {
	Kind             string       `json:"kind"`
	TargetDevicePath string       `json:"target_device_path"`
	SinglePlan       *CreatePlan  `json:"single_plan,omitempty"`
	MultiOSPlan      *MultiOSPlan `json:"multi_os_plan,omitempty"`
}

func canonicalPlannedExecutionJSON(execution PlannedExecution) ([]byte, error) {
	execution = normalizePlannedExecution(execution)
	comparable := comparablePlannedExecution{
		Kind:             execution.Kind,
		TargetDevicePath: strings.TrimSpace(execution.TargetDevicePath),
		SinglePlan:       execution.SinglePlan,
		MultiOSPlan:      execution.MultiOSPlan,
	}
	data, err := json.Marshal(comparable)
	if err != nil {
		return nil, fmt.Errorf("failed to canonicalize planned execution: %w", err)
	}
	return data, nil
}

func samePlannedExecution(a PlannedExecution, b PlannedExecution) (bool, error) {
	aJSON, err := canonicalPlannedExecutionJSON(a)
	if err != nil {
		return false, err
	}
	bJSON, err := canonicalPlannedExecutionJSON(b)
	if err != nil {
		return false, err
	}
	return bytes.Equal(aJSON, bJSON), nil
}

func marshalPlannedExecution(execution PlannedExecution) ([]byte, error) {
	publicExecution := execution
	publicExecution.path = ""
	data, err := json.MarshalIndent(publicExecution, "", "  ")
	if err != nil {
		return nil, fmt.Errorf("failed to serialize planned execution: %w", err)
	}
	return append(data, '\n'), nil
}

func (b *Backend) DeletePlannedExecution(runID string) error {
	if !plannedExecutionIDPattern.MatchString(strings.TrimSpace(runID)) {
		return fmt.Errorf("invalid planned execution id: %s", runID)
	}
	planDir := b.plannedExecutionsDir()
	path := filepath.Join(planDir, runID+".json")
	if !strings.HasPrefix(filepath.Clean(path), filepath.Clean(planDir)+string(os.PathSeparator)) {
		return fmt.Errorf("refusing to delete outside planned execution directory: %s", path)
	}
	if err := os.Remove(path); err == nil || os.IsNotExist(err) {
		return nil
	}
	return b.runCommand(true, "rm", "--", path)
}

func (b *Backend) writeManagedFile(path string, data []byte, mode os.FileMode) error {
	if err := writeManagedFileWithoutSudo(path, data, mode); err == nil {
		return nil
	}
	tempFile, err := os.CreateTemp("", "debian-usb-plan-*.json")
	if err != nil {
		return err
	}
	tempPath := tempFile.Name()
	defer os.Remove(tempPath)
	if _, err := bytes.NewReader(data).WriteTo(tempFile); err != nil {
		_ = tempFile.Close()
		return err
	}
	if err := tempFile.Close(); err != nil {
		return err
	}
	if err := b.runCommand(true, "mkdir", "-p", "--", filepath.Dir(path)); err != nil {
		return err
	}
	if err := b.runCommand(true, "cp", "-f", "--", tempPath, path); err != nil {
		return err
	}
	if err := b.runCommand(true, "chmod", fmt.Sprintf("%04o", mode.Perm()), "--", path); err != nil {
		return err
	}
	return nil
}

func writeManagedFileWithoutSudo(path string, data []byte, mode os.FileMode) error {
	parentDir := filepath.Dir(path)
	if err := os.MkdirAll(parentDir, 0o755); err != nil {
		return err
	}
	tempFile, err := os.CreateTemp(parentDir, ".debian-usb-plan-*.tmp")
	if err != nil {
		return err
	}
	tempPath := tempFile.Name()
	defer os.Remove(tempPath)
	if err := tempFile.Chmod(mode.Perm()); err != nil {
		_ = tempFile.Close()
		return err
	}
	if _, err := tempFile.Write(data); err != nil {
		_ = tempFile.Close()
		return err
	}
	if err := tempFile.Close(); err != nil {
		return err
	}
	return os.Rename(tempPath, path)
}
