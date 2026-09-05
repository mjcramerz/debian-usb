package app

import (
	"encoding/json"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func writeBackendLiveHookTestConfig(t *testing.T) string {
	t.Helper()
	cfg := RuntimeConfig{
		AppName:                   "debian-usb-test",
		AppVersion:                "1",
		DefaultPersistenceSizeGiB: 8,
		DefaultBootPolicy:         "balanced",
		DefaultInstallerPolicy:    "preserve",
		DefaultLiveHooks:          true,
		DefaultLiveArgsHooks:      "live-config.hooks=medium",
		DefaultPreseedPublicURL:   "https://example.test/preseed.cfg",
		DefaultPartitionLabels:    make(map[string]string, len(partitionLabelConfigKeys)),
		ManagedSourceURLs:         make(map[string]string, len(managedSourceURLOrder)),
	}
	for index, key := range partitionLabelConfigKeys {
		cfg.DefaultPartitionLabels[key] = fmt.Sprintf("TEST%02d", index)
	}
	for index, key := range managedSourceURLKeys() {
		cfg.ManagedSourceURLs[key] = fmt.Sprintf("https://example.test/source-%02d", index)
	}
	path := filepath.Join(t.TempDir(), "debian-usb.conf")
	if _, err := saveRuntimeConfig(path, cfg); err != nil {
		t.Fatalf("write synthetic backend config: %v", err)
	}
	return path
}

func TestBoolFlag(t *testing.T) {
	if got := boolFlag(true); got != "1" {
		t.Fatalf("expected true to encode as 1, got %q", got)
	}
	if got := boolFlag(false); got != "0" {
		t.Fatalf("expected false to encode as 0, got %q", got)
	}
}

func TestResolveCommandFallsBackToShForNonExecutableScript(t *testing.T) {
	tempDir := t.TempDir()
	scriptPath := filepath.Join(tempDir, "helper.sh")
	if err := os.WriteFile(scriptPath, []byte("#!/bin/sh\nexit 0\n"), 0644); err != nil {
		t.Fatalf("write script: %v", err)
	}

	command, args, err := resolveCommand(scriptPath, []string{"arg1"})
	if err != nil {
		t.Fatalf("resolve command: %v", err)
	}
	if filepath.Base(command) != "sh" {
		t.Fatalf("expected sh wrapper, got %q", command)
	}
	if len(args) != 2 || args[0] != scriptPath || args[1] != "arg1" {
		t.Fatalf("unexpected wrapped args: %#v", args)
	}
}

func TestRunJSONUsesStandardInput(t *testing.T) {
	tempDir := t.TempDir()
	helperPath := filepath.Join(tempDir, "helper.sh")
	script := "#!/bin/sh\nset -eu\nIFS=$(printf '\\n\\t')\nread -r line || { echo 'stdin missing' >&2; exit 1; }\nprintf '{\"value\":\"%s\"}\\n' \"$line\"\n"
	if err := os.WriteFile(helperPath, []byte(script), 0755); err != nil {
		t.Fatalf("write helper: %v", err)
	}

	reader, writer, err := os.Pipe()
	if err != nil {
		t.Fatalf("pipe: %v", err)
	}
	defer reader.Close()

	oldStdin := os.Stdin
	os.Stdin = reader
	defer func() {
		os.Stdin = oldStdin
	}()

	if _, err := writer.WriteString("hello\n"); err != nil {
		t.Fatalf("write stdin payload: %v", err)
	}
	if err := writer.Close(); err != nil {
		t.Fatalf("close stdin writer: %v", err)
	}

	backend := &Backend{pythonHelper: helperPath}
	var payload struct {
		Value string `json:"value"`
	}
	if err := backend.runJSON(false, &payload); err != nil {
		t.Fatalf("runJSON: %v", err)
	}
	if payload.Value != "hello" {
		t.Fatalf("expected stdin payload to round-trip, got %q", payload.Value)
	}
}

func TestRunJSONStreamsHelperStderr(t *testing.T) {
	tempDir := t.TempDir()
	helperPath := filepath.Join(tempDir, "helper.sh")
	script := "#!/bin/sh\nset -eu\nIFS=$(printf '\\n\\t')\nprintf 'download progress\\n' >&2\nprintf '{\"value\":\"ok\"}\\n'\n"
	if err := os.WriteFile(helperPath, []byte(script), 0755); err != nil {
		t.Fatalf("write helper: %v", err)
	}

	reader, writer, err := os.Pipe()
	if err != nil {
		t.Fatalf("pipe: %v", err)
	}
	defer reader.Close()

	oldStderr := os.Stderr
	os.Stderr = writer
	defer func() {
		os.Stderr = oldStderr
	}()

	backend := &Backend{pythonHelper: helperPath}
	var payload struct {
		Value string `json:"value"`
	}
	if err := backend.runJSON(false, &payload); err != nil {
		t.Fatalf("runJSON: %v", err)
	}
	if err := writer.Close(); err != nil {
		t.Fatalf("close stderr writer: %v", err)
	}
	stderrRaw, err := io.ReadAll(reader)
	if err != nil {
		t.Fatalf("read stderr: %v", err)
	}
	if payload.Value != "ok" {
		t.Fatalf("expected helper payload, got %q", payload.Value)
	}
	if !strings.Contains(string(stderrRaw), "download progress") {
		t.Fatalf("expected streamed stderr, got %q", string(stderrRaw))
	}
}

func TestRunCommandReturnsHelperStderr(t *testing.T) {
	tempDir := t.TempDir()
	helperPath := filepath.Join(tempDir, "fail.sh")
	script := "#!/bin/sh\nset -eu\nIFS=$(printf '\\n\\t')\nprintf 'mount failed on /dev/sdz2\\n' >&2\nexit 32\n"
	if err := os.WriteFile(helperPath, []byte(script), 0755); err != nil {
		t.Fatalf("write helper: %v", err)
	}

	backend := &Backend{}
	err := backend.runCommand(false, helperPath)
	if err == nil {
		t.Fatal("expected helper failure")
	}
	if !strings.Contains(err.Error(), "mount failed on /dev/sdz2") {
		t.Fatalf("expected helper stderr in error, got %v", err)
	}
}

func TestRunCommandIncludesCommandContextWhenHelperIsSilent(t *testing.T) {
	tempDir := t.TempDir()
	helperPath := filepath.Join(tempDir, "silent-fail.sh")
	script := "#!/bin/sh\nset -eu\nIFS=$(printf '\\n\\t')\nexit 32\n"
	if err := os.WriteFile(helperPath, []byte(script), 0755); err != nil {
		t.Fatalf("write helper: %v", err)
	}

	backend := &Backend{}
	err := backend.runCommand(false, helperPath)
	if err == nil {
		t.Fatal("expected helper failure")
	}
	if !strings.Contains(err.Error(), helperPath) {
		t.Fatalf("expected helper path in error, got %v", err)
	}
	if !strings.Contains(err.Error(), "exit status 32") {
		t.Fatalf("expected exit status in error, got %v", err)
	}
}

func TestExecuteCreateKeepsSudoAliveAndMakesFollowupCommandsNonInteractive(t *testing.T) {
	tempDir := t.TempDir()
	configPath := writeBackendLiveHookTestConfig(t)
	sudoPath := filepath.Join(tempDir, "sudo")
	pythonHelper := filepath.Join(tempDir, "python-helper.sh")
	writeHelper := filepath.Join(tempDir, "writer.sh")
	sudoLogPath := filepath.Join(tempDir, "sudo-args.txt")
	preparedISOPath := filepath.Join(tempDir, "prepared.iso")
	writerMarkerPath := filepath.Join(tempDir, "writer-used.txt")

	sudoScript := "#!/bin/sh\nset -eu\nIFS=$(printf '\\n\\t')\n{ printf '%s' \"${1-}\"; first=1; for argument in \"$@\"; do if [ \"$first\" -eq 1 ]; then first=0; continue; fi; printf ' %s' \"${argument}\"; done; printf '\\n'; } >>\"" + sudoLogPath + "\"\nif [ \"$#\" -eq 1 ] && [ \"$1\" = '-v' ]; then exit 0; fi\nif [ \"${1-}\" = '-n' ]; then\n  shift\n  if [ \"$#\" -eq 1 ] && [ \"$1\" = '-v' ]; then exit 0; fi\n  exec \"$@\"\nfi\nprintf 'unexpected interactive sudo command\\n' >&2\nexit 97\n"
	if err := os.WriteFile(sudoPath, []byte(sudoScript), 0755); err != nil {
		t.Fatalf("write sudo stub: %v", err)
	}
	pythonScript := "#!/bin/sh\nset -eu\nsleep 0.04\nprintf 'prepared ISO\\n' >\"" + preparedISOPath + "\"\nprintf '{\"iso_path\":\"" + preparedISOPath + "\"}\\n'\n"
	if err := os.WriteFile(pythonHelper, []byte(pythonScript), 0755); err != nil {
		t.Fatalf("write remaster helper: %v", err)
	}
	writerScript := "#!/bin/sh\nset -eu\nprintf 'used\\n' >\"" + writerMarkerPath + "\"\n"
	if err := os.WriteFile(writeHelper, []byte(writerScript), 0755); err != nil {
		t.Fatalf("write writer helper: %v", err)
	}
	t.Setenv("PATH", tempDir+string(os.PathListSeparator)+os.Getenv("PATH"))

	backend := &Backend{
		configPath:                    configPath,
		pythonHelper:                  pythonHelper,
		writeHelper:                   writeHelper,
		effectiveUID:                  func() int { return 1000 },
		sudoCredentialRefreshInterval: 5 * time.Millisecond,
	}
	plan := CreatePlan{
		Profile:        profileDebian,
		SourceRole:     multiOSSourceRolePrimary,
		WriteMode:      writeModeManaged,
		ISOPath:        filepath.Join(tempDir, "source.iso"),
		MediaClass:     "live",
		LiveToolGroups: []string{"nvme"},
	}
	if err := backend.ExecuteCreate(plan, "/dev/sdz", 0); err != nil {
		t.Fatalf("execute create with sudo lease: %v", err)
	}
	if _, err := os.Stat(writerMarkerPath); err != nil {
		t.Fatalf("expected writer to run: %v", err)
	}

	sudoLogRaw, err := os.ReadFile(sudoLogPath)
	if err != nil {
		t.Fatalf("read sudo log: %v", err)
	}
	lines := strings.Split(strings.TrimSpace(string(sudoLogRaw)), "\n")
	if len(lines) < 4 || lines[0] != "-v" {
		t.Fatalf("expected one initial interactive validation followed by non-interactive calls, got: %#v", lines)
	}
	refreshes := 0
	remasterSeen := false
	writerSeen := false
	for _, line := range lines[1:] {
		if !strings.HasPrefix(line, "-n ") {
			t.Fatalf("follow-up sudo invocation was allowed to prompt: %q", line)
		}
		switch {
		case line == "-n -v":
			refreshes++
		case strings.HasPrefix(line, "-n "+pythonHelper+" "):
			remasterSeen = true
		case strings.HasPrefix(line, "-n "+writeHelper+" "):
			writerSeen = true
		}
	}
	if refreshes < 2 {
		t.Fatalf("expected the sudo credential to be refreshed during the remaster, got %d refreshes: %#v", refreshes, lines)
	}
	if !remasterSeen || !writerSeen {
		t.Fatalf("expected remaster and writer to share the non-interactive sudo lease: %#v", lines)
	}

	logInfo, err := os.Stat(sudoLogPath)
	if err != nil {
		t.Fatalf("stat sudo log: %v", err)
	}
	time.Sleep(20 * time.Millisecond)
	stoppedInfo, err := os.Stat(sudoLogPath)
	if err != nil {
		t.Fatalf("stat stopped sudo log: %v", err)
	}
	if stoppedInfo.Size() != logInfo.Size() {
		t.Fatalf("sudo credential refresher continued after the operation completed")
	}
}

func TestExecuteMultiOSCreateInvokesWriterWithPlanFile(t *testing.T) {
	tempDir := t.TempDir()
	helperPath := filepath.Join(tempDir, "writer.sh")
	argsPath := filepath.Join(tempDir, "args.txt")
	planCopyPath := filepath.Join(tempDir, "plan.json")
	script := "#!/bin/sh\nset -eu\nIFS=$(printf '\\n\\t')\nprintf '%s\\n' \"$@\" >\"" + argsPath + "\"\nplan=''\nwhile [ \"$#\" -gt 0 ]; do\n  if [ \"$1\" = '--multi-os-plan' ]; then plan=\"$2\"; break; fi\n  shift\ndone\n[ -n \"$plan\" ] && [ -s \"$plan\" ] || { echo 'missing plan' >&2; exit 1; }\ncp -- \"$plan\" \"" + planCopyPath + "\"\n"
	if err := os.WriteFile(helperPath, []byte(script), 0755); err != nil {
		t.Fatalf("write helper: %v", err)
	}
	backend := &Backend{configPath: "configs/debian-usb.conf", writeHelper: helperPath}
	plan := MultiOSPlan{
		SchemaVersion:     1,
		WriteMode:         writeModeMultiOS,
		SecureBootTrust:   secureBootTrustFirmwareDB,
		UseCustomGrubMenu: true,
		Items: []MultiOSPlanItem{
			{ID: "os1", Profile: profileDebian, Title: "Debian", ISOPath: "/tmp/debian.iso"},
			{ID: "os2", Profile: profileKaliLinux, Title: "Kali", ISOPath: "/tmp/kali.iso"},
		},
	}
	if err := backend.executeMultiOSCreate(plan, "/dev/sdz", false); err != nil {
		t.Fatalf("execute Multi-OS create: %v", err)
	}
	argsRaw, err := os.ReadFile(argsPath)
	if err != nil {
		t.Fatalf("read args: %v", err)
	}
	args := string(argsRaw)
	for _, fragment := range []string{"--write-mode\nmulti-os", "--device\n/dev/sdz", "--multi-os-plan\n"} {
		if !strings.Contains(args, fragment) {
			t.Fatalf("expected %q in args:\n%s", fragment, args)
		}
	}

	planRaw, err := os.ReadFile(planCopyPath)
	if err != nil {
		t.Fatalf("read copied plan: %v", err)
	}
	var savedPlan MultiOSPlan
	if err := json.Unmarshal(planRaw, &savedPlan); err != nil {
		t.Fatalf("decode copied plan: %v", err)
	}
	if !savedPlan.UseCustomGrubMenu {
		t.Fatalf("expected copied plan to preserve UseCustomGrubMenu, got %#v", savedPlan)
	}
	if savedPlan.SecureBootTrust != secureBootTrustFirmwareDB {
		t.Fatalf("expected copied plan to preserve SecureBootTrust, got %#v", savedPlan)
	}
}

func TestExecuteCreateRemastersDebianLiveBeforeWriter(t *testing.T) {
	testCases := []struct {
		name          string
		groups        []string
		wantFragments []string
		forbid        []string
	}{
		{
			name:          "selected subset",
			groups:        []string{"nvme", "firmware_programming"},
			wantFragments: []string{"--group\nnvme", "--group\nfirmware_programming"},
			forbid:        []string{"--no-tools"},
		},
		{
			name:          "explicit no tools",
			groups:        []string{},
			wantFragments: []string{"--no-tools"},
			forbid:        []string{"--group"},
		},
	}

	for _, testCase := range testCases {
		t.Run(testCase.name, func(t *testing.T) {
			tempDir := t.TempDir()
			configPath := writeBackendLiveHookTestConfig(t)
			pythonHelper := filepath.Join(tempDir, "python-helper.sh")
			writeHelper := filepath.Join(tempDir, "writer.sh")
			remasterArgsPath := filepath.Join(tempDir, "remaster-args.txt")
			writerArgsPath := filepath.Join(tempDir, "writer-args.txt")
			eventsPath := filepath.Join(tempDir, "events.txt")
			sourceISOPath := filepath.Join(tempDir, "source.iso")
			preparedISOPath := filepath.Join(tempDir, "prepared.iso")
			if err := os.WriteFile(sourceISOPath, []byte("source ISO\n"), 0644); err != nil {
				t.Fatalf("write source ISO: %v", err)
			}

			pythonScript := "#!/bin/sh\nset -eu\nIFS=$(printf '\\n\\t')\nprintf 'remaster-start\\n' >>\"" + eventsPath + "\"\nprintf '%s\\n' \"$@\" >\"" + remasterArgsPath + "\"\nprintf 'prepared ISO\\n' >\"" + preparedISOPath + "\"\nprintf 'remaster-complete\\n' >>\"" + eventsPath + "\"\nprintf '{\"iso_path\":\"" + preparedISOPath + "\"}\\n'\n"
			if err := os.WriteFile(pythonHelper, []byte(pythonScript), 0755); err != nil {
				t.Fatalf("write remaster helper: %v", err)
			}
			writerScript := "#!/bin/sh\nset -eu\nIFS=$(printf '\\n\\t')\nprintf 'writer-start\\n' >>\"" + eventsPath + "\"\nprintf '%s\\n' \"$@\" >\"" + writerArgsPath + "\"\n"
			if err := os.WriteFile(writeHelper, []byte(writerScript), 0755); err != nil {
				t.Fatalf("write writer helper: %v", err)
			}

			backend := &Backend{configPath: configPath, pythonHelper: pythonHelper, writeHelper: writeHelper}
			plan := CreatePlan{
				Profile:        profileDebian,
				SourceRole:     multiOSSourceRolePrimary,
				WriteMode:      writeModeManaged,
				ISOPath:        sourceISOPath,
				MediaClass:     "live",
				LiveToolGroups: testCase.groups,
			}
			if err := backend.executeCreate(plan, "/dev/sdz", 0, false); err != nil {
				t.Fatalf("execute Debian Live create: %v", err)
			}

			eventsRaw, err := os.ReadFile(eventsPath)
			if err != nil {
				t.Fatalf("read execution events: %v", err)
			}
			if got := strings.TrimSpace(string(eventsRaw)); got != "remaster-start\nremaster-complete\nwriter-start" {
				t.Fatalf("expected remaster to finish before writer start, got:\n%s", got)
			}
			remasterArgsRaw, err := os.ReadFile(remasterArgsPath)
			if err != nil {
				t.Fatalf("read remaster args: %v", err)
			}
			remasterArgs := string(remasterArgsRaw)
			for _, fragment := range append(testCase.wantFragments,
				"remaster-live-tools-source",
				"--profile\ndebian",
				"--source-iso\n"+sourceISOPath,
				"--live-kernel-args\nlive-config.hooks=medium",
			) {
				if !strings.Contains(remasterArgs, fragment) {
					t.Fatalf("expected %q in remaster args:\n%s", fragment, remasterArgs)
				}
			}
			for _, fragment := range testCase.forbid {
				if strings.Contains(remasterArgs, fragment) {
					t.Fatalf("did not expect %q in remaster args:\n%s", fragment, remasterArgs)
				}
			}
			writerArgsRaw, err := os.ReadFile(writerArgsPath)
			if err != nil {
				t.Fatalf("read writer args: %v", err)
			}
			writerArgs := string(writerArgsRaw)
			if !strings.Contains(writerArgs, "--iso\n"+preparedISOPath) {
				t.Fatalf("expected writer to receive remastered ISO path:\n%s", writerArgs)
			}
			if strings.Contains(writerArgs, "--iso\n"+sourceISOPath) {
				t.Fatalf("writer received the upstream ISO instead of the remastered ISO:\n%s", writerArgs)
			}
		})
	}
}

func TestExecuteCreateStopsBeforeWriterWhenLiveRemasterFails(t *testing.T) {
	tempDir := t.TempDir()
	configPath := writeBackendLiveHookTestConfig(t)
	pythonHelper := filepath.Join(tempDir, "python-helper.sh")
	writeHelper := filepath.Join(tempDir, "writer.sh")
	writerMarkerPath := filepath.Join(tempDir, "writer-used.txt")
	if err := os.WriteFile(pythonHelper, []byte("#!/bin/sh\nset -eu\nprintf 'synthetic remaster failure\\n' >&2\nexit 1\n"), 0755); err != nil {
		t.Fatalf("write remaster helper: %v", err)
	}
	writerScript := "#!/bin/sh\nset -eu\nprintf 'used\\n' >\"" + writerMarkerPath + "\"\n"
	if err := os.WriteFile(writeHelper, []byte(writerScript), 0755); err != nil {
		t.Fatalf("write writer helper: %v", err)
	}

	backend := &Backend{configPath: configPath, pythonHelper: pythonHelper, writeHelper: writeHelper}
	err := backend.executeCreate(CreatePlan{
		Profile:        profileDebian,
		SourceRole:     multiOSSourceRolePrimary,
		WriteMode:      writeModeManaged,
		ISOPath:        filepath.Join(tempDir, "source.iso"),
		MediaClass:     "live",
		LiveToolGroups: []string{"nvme"},
	}, "/dev/sdz", 0, false)
	if err == nil || !strings.Contains(err.Error(), "synthetic remaster failure") {
		t.Fatalf("expected remaster failure, got %v", err)
	}
	if _, statErr := os.Stat(writerMarkerPath); !os.IsNotExist(statErr) {
		t.Fatalf("expected writer to remain unused after remaster failure, stat error: %v", statErr)
	}
}

func TestExecuteCreateNeverRemastersInstallerOrTailsSources(t *testing.T) {
	tempDir := t.TempDir()
	pythonHelper := filepath.Join(tempDir, "python-helper.sh")
	writeHelper := filepath.Join(tempDir, "writer.sh")
	remasterMarkerPath := filepath.Join(tempDir, "remaster-used.txt")
	writerEventsPath := filepath.Join(tempDir, "writer-events.txt")
	pythonScript := "#!/bin/sh\nset -eu\nprintf 'used\\n' >\"" + remasterMarkerPath + "\"\nprintf '{\"iso_path\":\"/tmp/unexpected.iso\"}\\n'\n"
	if err := os.WriteFile(pythonHelper, []byte(pythonScript), 0755); err != nil {
		t.Fatalf("write remaster helper: %v", err)
	}
	writerScript := "#!/bin/sh\nset -eu\nprintf 'writer\\n' >>\"" + writerEventsPath + "\"\n"
	if err := os.WriteFile(writeHelper, []byte(writerScript), 0755); err != nil {
		t.Fatalf("write writer helper: %v", err)
	}

	backend := &Backend{configPath: filepath.Join(tempDir, "unused.conf"), pythonHelper: pythonHelper, writeHelper: writeHelper}
	for _, plan := range []CreatePlan{
		{Profile: profileDebian, SourceRole: multiOSSourceRoleNetinst, WriteMode: writeModeManaged, ISOPath: "/tmp/netinst", MediaClass: "installer"},
		{Profile: profileDebian, SourceRole: multiOSSourceRoleNetboot, WriteMode: writeModeManaged, ISOPath: "/tmp/netboot", MediaClass: "installer"},
		{Profile: profileTails, SourceRole: multiOSSourceRolePrimary, WriteMode: writeModeManaged, ISOPath: "/tmp/tails.iso", MediaClass: "live", LiveToolGroups: []string{}},
	} {
		if err := backend.executeCreate(plan, "/dev/sdz", 0, false); err != nil {
			t.Fatalf("execute isolated source %s/%s: %v", plan.Profile, plan.SourceRole, err)
		}
	}
	if _, err := os.Stat(remasterMarkerPath); !os.IsNotExist(err) {
		t.Fatalf("expected installer and Tails sources to bypass Live remastering, stat error: %v", err)
	}
	writerEventsRaw, err := os.ReadFile(writerEventsPath)
	if err != nil {
		t.Fatalf("read writer events: %v", err)
	}
	if got := strings.Count(string(writerEventsRaw), "writer\n"); got != 3 {
		t.Fatalf("expected three isolated writer calls, got %d", got)
	}
}

func TestExecuteCreateRemastersKaliWithoutDebianHookArguments(t *testing.T) {
	tempDir := t.TempDir()
	pythonHelper := filepath.Join(tempDir, "python-helper.sh")
	writeHelper := filepath.Join(tempDir, "writer.sh")
	remasterArgsPath := filepath.Join(tempDir, "remaster-args.txt")
	preparedISOPath := filepath.Join(tempDir, "kali-prepared.iso")
	pythonScript := "#!/bin/sh\nset -eu\nprintf '%s\\n' \"$@\" >\"" + remasterArgsPath + "\"\nprintf 'prepared ISO\\n' >\"" + preparedISOPath + "\"\nprintf '{\"iso_path\":\"" + preparedISOPath + "\"}\\n'\n"
	if err := os.WriteFile(pythonHelper, []byte(pythonScript), 0755); err != nil {
		t.Fatalf("write remaster helper: %v", err)
	}
	if err := os.WriteFile(writeHelper, []byte("#!/bin/sh\nset -eu\nexit 0\n"), 0755); err != nil {
		t.Fatalf("write writer helper: %v", err)
	}

	backend := &Backend{configPath: filepath.Join(tempDir, "must-not-be-read.conf"), pythonHelper: pythonHelper, writeHelper: writeHelper}
	if err := backend.executeCreate(CreatePlan{
		Profile:        profileKaliLinux,
		SourceRole:     multiOSSourceRolePrimary,
		WriteMode:      writeModeManaged,
		ISOPath:        filepath.Join(tempDir, "kali.iso"),
		MediaClass:     "live",
		LiveToolGroups: []string{"nmap"},
	}, "/dev/sdz", 0, false); err != nil {
		t.Fatalf("execute Kali Live create: %v", err)
	}
	remasterArgsRaw, err := os.ReadFile(remasterArgsPath)
	if err != nil {
		t.Fatalf("read remaster args: %v", err)
	}
	remasterArgs := string(remasterArgsRaw)
	if strings.Contains(remasterArgs, "--live-kernel-args") || strings.Contains(remasterArgs, "live_wifi_") || strings.Contains(remasterArgs, "live-config.hooks=medium") {
		t.Fatalf("expected Debian hook arguments to stay out of Kali remastering:\n%s", remasterArgs)
	}
}

func TestExecuteMultiOSCreateRemastersOnlyEligibleLiveItemsBeforeWriter(t *testing.T) {
	tempDir := t.TempDir()
	configPath := writeBackendLiveHookTestConfig(t)
	pythonHelper := filepath.Join(tempDir, "python-helper.sh")
	writeHelper := filepath.Join(tempDir, "writer.sh")
	remasterArgsPath := filepath.Join(tempDir, "remaster-args.txt")
	writerArgsPath := filepath.Join(tempDir, "writer-args.txt")
	planCopyPath := filepath.Join(tempDir, "executed-plan.json")
	eventsPath := filepath.Join(tempDir, "events.txt")
	preparedISOPath := filepath.Join(tempDir, "debian-prepared.iso")
	pythonScript := "#!/bin/sh\nset -eu\nIFS=$(printf '\\n\\t')\nprintf 'remaster-start\\n' >>\"" + eventsPath + "\"\nprintf '%s\\n' \"$@\" >\"" + remasterArgsPath + "\"\nprintf 'prepared ISO\\n' >\"" + preparedISOPath + "\"\nprintf 'remaster-complete\\n' >>\"" + eventsPath + "\"\nprintf '{\"iso_path\":\"" + preparedISOPath + "\"}\\n'\n"
	if err := os.WriteFile(pythonHelper, []byte(pythonScript), 0755); err != nil {
		t.Fatalf("write remaster helper: %v", err)
	}
	writerScript := "#!/bin/sh\nset -eu\nIFS=$(printf '\\n\\t')\nprintf 'writer-start\\n' >>\"" + eventsPath + "\"\nprintf '%s\\n' \"$@\" >\"" + writerArgsPath + "\"\nplan=''\nwhile [ \"$#\" -gt 0 ]; do\n  if [ \"$1\" = '--multi-os-plan' ]; then plan=\"$2\"; break; fi\n  shift\ndone\n[ -n \"$plan\" ] && [ -s \"$plan\" ] || { printf 'missing plan\\n' >&2; exit 1; }\ncp -- \"$plan\" \"" + planCopyPath + "\"\n"
	if err := os.WriteFile(writeHelper, []byte(writerScript), 0755); err != nil {
		t.Fatalf("write writer helper: %v", err)
	}

	sourceISOPath := filepath.Join(tempDir, "debian.iso")
	plan := MultiOSPlan{
		SchemaVersion: 1,
		WriteMode:     writeModeMultiOS,
		Items: []MultiOSPlanItem{
			{ID: "live", Profile: profileDebian, SourceRole: multiOSSourceRolePrimary, Title: "Debian Live", ISOPath: sourceISOPath, MediaClass: "hybrid", LiveToolGroups: []string{"nvme"}},
			{ID: "netinst", Profile: profileDebian, SourceRole: multiOSSourceRoleNetinst, Title: "Debian Netinst", ISOPath: "/tmp/netinst", MediaClass: "installer"},
			{ID: "netboot", Profile: profileDebian, SourceRole: multiOSSourceRoleNetboot, Title: "Debian Netboot", ISOPath: "/tmp/netboot", MediaClass: "installer"},
			{ID: "tails", Profile: profileTails, SourceRole: multiOSSourceRolePrimary, Title: "Tails", ISOPath: "/tmp/tails.iso", MediaClass: "live"},
		},
	}
	backend := &Backend{configPath: configPath, pythonHelper: pythonHelper, writeHelper: writeHelper}
	if err := backend.executeMultiOSCreate(plan, "/dev/sdz", false); err != nil {
		t.Fatalf("execute Multi-OS create: %v", err)
	}

	eventsRaw, err := os.ReadFile(eventsPath)
	if err != nil {
		t.Fatalf("read execution events: %v", err)
	}
	if got := strings.TrimSpace(string(eventsRaw)); got != "remaster-start\nremaster-complete\nwriter-start" {
		t.Fatalf("expected Live remaster to finish before Multi-OS writer start, got:\n%s", got)
	}
	writerArgsRaw, err := os.ReadFile(writerArgsPath)
	if err != nil {
		t.Fatalf("read writer args: %v", err)
	}
	if !strings.Contains(string(writerArgsRaw), "--live-tools-prepared\n1") {
		t.Fatalf("expected writer duplicate-remaster guard, got:\n%s", writerArgsRaw)
	}
	planRaw, err := os.ReadFile(planCopyPath)
	if err != nil {
		t.Fatalf("read executed plan: %v", err)
	}
	var executedPlan MultiOSPlan
	if err := json.Unmarshal(planRaw, &executedPlan); err != nil {
		t.Fatalf("decode executed plan: %v", err)
	}
	if executedPlan.Items[0].ISOPath != preparedISOPath {
		t.Fatalf("expected Debian Live item to use remastered ISO, got %q", executedPlan.Items[0].ISOPath)
	}
	for index, wantPath := range []string{"/tmp/netinst", "/tmp/netboot", "/tmp/tails.iso"} {
		if got := executedPlan.Items[index+1].ISOPath; got != wantPath {
			t.Fatalf("expected isolated item %d path %q, got %q", index+1, wantPath, got)
		}
	}
	if plan.Items[0].ISOPath != sourceISOPath {
		t.Fatalf("expected caller-owned plan to remain unchanged, got %q", plan.Items[0].ISOPath)
	}
	remasterArgsRaw, err := os.ReadFile(remasterArgsPath)
	if err != nil {
		t.Fatalf("read remaster args: %v", err)
	}
	if got := strings.Count(string(remasterArgsRaw), "remaster-live-tools-source\n"); got != 1 {
		t.Fatalf("expected one eligible Live remaster call, got %d:\n%s", got, remasterArgsRaw)
	}
}

func TestUpdateMultiOSCreatePassesUpdateExistingFlag(t *testing.T) {
	tempDir := t.TempDir()
	helperPath := filepath.Join(tempDir, "writer.sh")
	argsPath := filepath.Join(tempDir, "args.txt")
	script := "#!/bin/sh\nset -eu\nIFS=$(printf '\\n\\t')\nprintf '%s\\n' \"$@\" >\"" + argsPath + "\"\n"
	if err := os.WriteFile(helperPath, []byte(script), 0755); err != nil {
		t.Fatalf("write helper: %v", err)
	}
	backend := &Backend{configPath: "configs/debian-usb.conf", writeHelper: helperPath}
	plan := MultiOSPlan{
		SchemaVersion: 1,
		Title:         "Multi-OS USB",
		WriteMode:     writeModeMultiOS,
		Items: []MultiOSPlanItem{
			{ID: "os1", Profile: profileDebian, Title: "Debian", ISOPath: "/tmp/debian.iso"},
			{ID: "os2", Profile: profileKaliLinux, Title: "Kali", ISOPath: "/tmp/kali.iso"},
		},
	}
	if err := backend.executeMultiOSCreate(plan, "/dev/sdz", false, "--update-existing", "1"); err != nil {
		t.Fatalf("update Multi-OS create: %v", err)
	}
	argsRaw, err := os.ReadFile(argsPath)
	if err != nil {
		t.Fatalf("read args: %v", err)
	}
	args := string(argsRaw)
	for _, fragment := range []string{"--write-mode\nmulti-os", "--device\n/dev/sdz", "--update-existing\n1"} {
		if !strings.Contains(args, fragment) {
			t.Fatalf("expected %q in args:\n%s", fragment, args)
		}
	}
}

func TestExecuteCreatePassesCustomGrubMenuFlag(t *testing.T) {
	tempDir := t.TempDir()
	helperPath := filepath.Join(tempDir, "writer.sh")
	argsPath := filepath.Join(tempDir, "args.txt")
	offlinePreseedDir := filepath.Join(tempDir, "preseed")
	if err := os.MkdirAll(offlinePreseedDir, 0755); err != nil {
		t.Fatalf("mkdir offline preseed dir: %v", err)
	}
	script := "#!/bin/sh\nset -eu\nIFS=$(printf '\\n\\t')\nprintf '%s\\n' \"$@\" >\"" + argsPath + "\"\n"
	if err := os.WriteFile(helperPath, []byte(script), 0755); err != nil {
		t.Fatalf("write helper: %v", err)
	}
	backend := &Backend{configPath: "configs/debian-usb.conf", writeHelper: helperPath}
	plan := CreatePlan{
		Profile:                     profileDebian,
		WriteMode:                   writeModeManaged,
		ISOPath:                     "/tmp/debian.iso",
		SecureBootTrust:             secureBootTrustMOK,
		ESPLabel:                    "EFITEST",
		PayloadFSLabel:              "DEB-LIVE",
		PayloadPartLabel:            "DEB-LIVE",
		PersistenceFSLabel:          "DEB-PERSIST",
		PersistencePartLabel:        "DEB-PERSIST",
		LiveToram:                   true,
		UseCustomGrubMenu:           true,
		PreserveUpstreamGrubEntries: true,
		Preseed:                     true,
		OfflinePreseedSourceDir:     offlinePreseedDir,
	}
	if err := backend.executeCreate(plan, "/dev/sdz", 0, false); err != nil {
		t.Fatalf("execute create: %v", err)
	}
	argsRaw, err := os.ReadFile(argsPath)
	if err != nil {
		t.Fatalf("read args: %v", err)
	}
	args := string(argsRaw)
	for _, fragment := range []string{"--write-mode\nmanaged", "--secure-boot-trust\nmok", "--esp-label\nEFITEST", "--payload-fs-label\nDEB-LIVE", "--payload-partlabel\nDEB-LIVE", "--persistence-fs-label\nDEB-PERSIST", "--persistence-partlabel\nDEB-PERSIST", "--live-toram\n1", "--use-custom-grub-menu\n1", "--preserve-upstream-grub-entries\n1", "--include-preseed\n1", "--offline-preseed-dir\n" + offlinePreseedDir} {
		if !strings.Contains(args, fragment) {
			t.Fatalf("expected %q in args:\n%s", fragment, args)
		}
	}
}

func TestInspectBuildISOKernelEvidenceInvokesHelperWithModuleAndAliasFlags(t *testing.T) {
	tempDir := t.TempDir()
	helperPath := filepath.Join(tempDir, "python-helper.sh")
	argsPath := filepath.Join(tempDir, "args.txt")
	script := "#!/bin/sh\nset -eu\nIFS=$(printf '\\n\\t')\nprintf '%s\\n' \"$@\" >\"" + argsPath + "\"\nprintf '{\"kernel_version\":\"6.12.63+deb13-amd64\",\"module_tree_dir\":\"/lib/modules/6.12.63+deb13-amd64\",\"module_tree_searched\":[],\"config_path\":\"/boot/config-6.12.63+deb13-amd64\",\"config_paths_searched\":[],\"download_attempted\":false,\"download_used\":false,\"download_cache_dir\":\"/data/downloads/debian-usb/kernel-support/6.12.63+deb13-amd64\",\"downloaded_packages\":[],\"modules\":[{\"name\":\"xxhash_generic\",\"found\":true,\"path\":\"/lib/modules/6.12.63+deb13-amd64/kernel/crypto/xxhash_generic.ko\"}],\"config_symbols\":[{\"symbol\":\"CONFIG_EROFS_FS\",\"value\":\"m\",\"line\":\"CONFIG_EROFS_FS=m\"}],\"notes\":[]}\\n'\n"
	if err := os.WriteFile(helperPath, []byte(script), 0755); err != nil {
		t.Fatalf("write helper: %v", err)
	}

	backend := &Backend{pythonHelper: helperPath}
	result, err := backend.InspectBuildISOKernelEvidence(BuildISOKernelInspectRequest{
		KernelVersion:         "6.12.63+deb13-amd64",
		ModuleNames:           []string{"xxhash_generic"},
		ModuleAliasCandidates: []string{"crypto_xxhash64"},
		ConfigSymbols:         []string{"CONFIG_EROFS_FS"},
		ModuleTreeDir:         "/lib/modules/6.12.63+deb13-amd64",
		DownloadIfMissing:     false,
	})
	if err != nil {
		t.Fatalf("inspect kernel evidence: %v", err)
	}
	if result.KernelVersion != "6.12.63+deb13-amd64" {
		t.Fatalf("unexpected kernel version: %q", result.KernelVersion)
	}
	argsRaw, err := os.ReadFile(argsPath)
	if err != nil {
		t.Fatalf("read args: %v", err)
	}
	args := string(argsRaw)
	for _, fragment := range []string{"inspect-build-kernel-support", "--module\nxxhash_generic", "--module-alias\ncrypto_xxhash64", "--config-symbol\nCONFIG_EROFS_FS"} {
		if !strings.Contains(args, fragment) {
			t.Fatalf("expected %q in args:\n%s", fragment, args)
		}
	}
}

func TestInspectDebianRebuildISOInvokesHelperWithSourcePath(t *testing.T) {
	tempDir := t.TempDir()
	helperPath := filepath.Join(tempDir, "python-helper.sh")
	argsPath := filepath.Join(tempDir, "args.txt")
	script := "#!/bin/sh\nset -eu\nIFS=$(printf '\\n\\t')\nprintf '%s\\n' \"$@\" >\"" + argsPath + "\"\nprintf '{\"source_path\":\"/tmp/source.iso\",\"source_type\":\"iso\",\"volume_id\":\"DEBIAN_TEST\",\"media_class\":\"hybrid\",\"architecture\":\"amd64\",\"firmware\":[\"uefi\"],\"best_live_title\":\"Live\",\"best_installer_title\":\"Install\",\"installer_kernel_path\":\"/install.amd/vmlinuz\",\"installer_initrd_path\":\"/install.amd/initrd.gz\",\"installer_kernel_version\":\"6.12.63+deb13-amd64\",\"live_kernel_path\":\"/live/vmlinuz\",\"live_initrd_path\":\"/live/initrd.img\",\"live_rootfs_path\":\"/live/filesystem.squashfs\",\"warnings\":[]}\\n'\n"
	if err := os.WriteFile(helperPath, []byte(script), 0755); err != nil {
		t.Fatalf("write helper: %v", err)
	}

	backend := &Backend{pythonHelper: helperPath}
	result, err := backend.InspectDebianRebuildISO("/tmp/source.iso")
	if err != nil {
		t.Fatalf("inspect Debian rebuild ISO: %v", err)
	}
	if result.InstallerKernelVersion != "6.12.63+deb13-amd64" {
		t.Fatalf("unexpected installer kernel version: %q", result.InstallerKernelVersion)
	}
	argsRaw, err := os.ReadFile(argsPath)
	if err != nil {
		t.Fatalf("read args: %v", err)
	}
	args := string(argsRaw)
	for _, fragment := range []string{"inspect-debian-rebuild-source", "--source-iso\n/tmp/source.iso"} {
		if !strings.Contains(args, fragment) {
			t.Fatalf("expected %q in args:\n%s", fragment, args)
		}
	}
}

func TestDownloadManagedSourceRunsWithoutSudo(t *testing.T) {
	tempDir := t.TempDir()
	helperPath := filepath.Join(tempDir, "python-helper.sh")
	sudoPath := filepath.Join(tempDir, "sudo")
	sudoMarkerPath := filepath.Join(tempDir, "sudo-used.txt")
	argsPath := filepath.Join(tempDir, "args.txt")
	script := "#!/bin/sh\nset -eu\nIFS=$(printf '\\n\\t')\nprintf '%s\\n' \"$@\" >\"" + argsPath + "\"\nprintf '{\"path\":\"/data/downloads/debian-usb/iso/debian_live_iso/sample.iso\"}\\n'\n"
	if err := os.WriteFile(helperPath, []byte(script), 0755); err != nil {
		t.Fatalf("write helper: %v", err)
	}
	sudoScript := "#!/bin/sh\nset -eu\nIFS=$(printf '\\n\\t')\nprintf 'used\\n' >\"" + sudoMarkerPath + "\"\nexec \"$@\"\n"
	if err := os.WriteFile(sudoPath, []byte(sudoScript), 0755); err != nil {
		t.Fatalf("write sudo stub: %v", err)
	}
	oldPath := os.Getenv("PATH")
	if err := os.Setenv("PATH", tempDir+string(os.PathListSeparator)+oldPath); err != nil {
		t.Fatalf("set PATH: %v", err)
	}
	defer os.Setenv("PATH", oldPath)

	backend := &Backend{pythonHelper: helperPath, configPath: "configs/debian-usb.conf"}
	path, err := backend.DownloadManagedSource("DEBIAN_LIVE_ISO_STABLE_URL")
	if err != nil {
		t.Fatalf("download managed source: %v", err)
	}
	if path != "/data/downloads/debian-usb/iso/debian_live_iso/sample.iso" {
		t.Fatalf("unexpected path: %q", path)
	}
	if _, err := os.Stat(sudoMarkerPath); !os.IsNotExist(err) {
		t.Fatalf("expected download helper to run without sudo")
	}
	argsRaw, err := os.ReadFile(argsPath)
	if err != nil {
		t.Fatalf("read args: %v", err)
	}
	args := string(argsRaw)
	for _, fragment := range []string{"download-managed-source", "--key\nDEBIAN_LIVE_ISO_STABLE_URL", "--config\nconfigs/debian-usb.conf"} {
		if !strings.Contains(args, fragment) {
			t.Fatalf("expected %q in args:\n%s", fragment, args)
		}
	}
}

func TestPrepareManagedInstallerSourceUsesSudoForManagedBundle(t *testing.T) {
	tempDir := t.TempDir()
	helperPath := filepath.Join(tempDir, "python-helper.sh")
	sudoPath := filepath.Join(tempDir, "sudo")
	sudoMarkerPath := filepath.Join(tempDir, "sudo-used.txt")
	argsPath := filepath.Join(tempDir, "args.txt")
	script := "#!/bin/sh\nset -eu\nIFS=$(printf '\\n\\t')\nprintf '%s\\n' \"$@\" >\"" + argsPath + "\"\nprintf '{\"source_path\":\"/data/downloads/debian-usb/sources/debian/netinst/test\"}\\n'\n"
	if err := os.WriteFile(helperPath, []byte(script), 0755); err != nil {
		t.Fatalf("write helper: %v", err)
	}
	sudoScript := "#!/bin/sh\nset -eu\nIFS=$(printf '\\n\\t')\nprintf 'used\\n' >\"" + sudoMarkerPath + "\"\nexec \"$@\"\n"
	if err := os.WriteFile(sudoPath, []byte(sudoScript), 0755); err != nil {
		t.Fatalf("write sudo stub: %v", err)
	}
	oldPath := os.Getenv("PATH")
	if err := os.Setenv("PATH", tempDir+string(os.PathListSeparator)+oldPath); err != nil {
		t.Fatalf("set PATH: %v", err)
	}
	defer os.Setenv("PATH", oldPath)

	backend := &Backend{pythonHelper: helperPath}
	path, err := backend.PrepareManagedInstallerSource("debian", "netinst", "/tmp/vmlinuz", "/tmp/initrd.gz", "/tmp/source.iso", nil, "", "", "")
	if err != nil {
		t.Fatalf("prepare managed installer source: %v", err)
	}
	if path != "/data/downloads/debian-usb/sources/debian/netinst/test" {
		t.Fatalf("unexpected source path: %q", path)
	}
	if _, err := os.Stat(sudoMarkerPath); err != nil {
		t.Fatalf("expected sudo to be used for managed installer bundle preparation: %v", err)
	}
	argsRaw, err := os.ReadFile(argsPath)
	if err != nil {
		t.Fatalf("read args: %v", err)
	}
	args := string(argsRaw)
	for _, fragment := range []string{"prepare-managed-installer-source", "--profile\ndebian", "--source-role\nnetinst", "--kernel-path\n/tmp/vmlinuz", "--initrd-path\n/tmp/initrd.gz", "--iso-path\n/tmp/source.iso"} {
		if !strings.Contains(args, fragment) {
			t.Fatalf("expected %q in args:\n%s", fragment, args)
		}
	}
}

func TestPrepareManagedInstallerSourcePassesSelectedExtraModules(t *testing.T) {
	tempDir := t.TempDir()
	helperPath := filepath.Join(tempDir, "python-helper.sh")
	sudoPath := filepath.Join(tempDir, "sudo")
	sudoMarkerPath := filepath.Join(tempDir, "sudo-used.txt")
	argsPath := filepath.Join(tempDir, "args.txt")
	script := "#!/bin/sh\nset -eu\nIFS=$(printf '\\n\\t')\nprintf '%s\\n' \"$@\" >\"" + argsPath + "\"\nprintf '{\"source_path\":\"/data/downloads/debian-usb/sources/debian/netinst/test\"}\\n'\n"
	if err := os.WriteFile(helperPath, []byte(script), 0755); err != nil {
		t.Fatalf("write helper: %v", err)
	}
	sudoScript := "#!/bin/sh\nset -eu\nIFS=$(printf '\\n\\t')\nprintf 'used\\n' >\"" + sudoMarkerPath + "\"\nexec \"$@\"\n"
	if err := os.WriteFile(sudoPath, []byte(sudoScript), 0755); err != nil {
		t.Fatalf("write sudo stub: %v", err)
	}
	oldPath := os.Getenv("PATH")
	if err := os.Setenv("PATH", tempDir+string(os.PathListSeparator)+oldPath); err != nil {
		t.Fatalf("set PATH: %v", err)
	}
	defer os.Setenv("PATH", oldPath)

	backend := &Backend{pythonHelper: helperPath}
	_, err := backend.PrepareManagedInstallerSource(
		"debian",
		"netinst",
		"/tmp/vmlinuz",
		"/tmp/initrd.gz",
		"/tmp/source.iso",
		[]string{"xxhash_generic", "lz4"},
		"host-kernel",
		"",
		"",
	)
	if err != nil {
		t.Fatalf("prepare managed installer source with extra modules: %v", err)
	}
	if _, err := os.Stat(sudoMarkerPath); err != nil {
		t.Fatalf("expected sudo to be used for initrd module rebuild: %v", err)
	}
	argsRaw, err := os.ReadFile(argsPath)
	if err != nil {
		t.Fatalf("read args: %v", err)
	}
	args := string(argsRaw)
	for _, fragment := range []string{"--module-source-strategy\nhost-kernel", "--extra-module\nxxhash_generic", "--extra-module\nlz4"} {
		if !strings.Contains(args, fragment) {
			t.Fatalf("expected %q in args:\n%s", fragment, args)
		}
	}
}

func TestPrepareManagedInstallerSourcePreflightsAlignmentBeforeSudo(t *testing.T) {
	tempDir := t.TempDir()
	helperPath := filepath.Join(tempDir, "python-helper.sh")
	sudoPath := filepath.Join(tempDir, "sudo")
	sudoMarkerPath := filepath.Join(tempDir, "sudo-used.txt")
	argsPath := filepath.Join(tempDir, "args.txt")
	script := "#!/bin/sh\nset -eu\nIFS=$(printf '\\n\\t')\nprintf '%s\\n' \"$@\" >\"" + argsPath + "\"\nif printf '%s\\n' \"$@\" | grep -qx -- '--preflight-only'; then\n  printf 'installer ISO and selected installer assets do not match; use an aligned ISO/kernel/initrd set: ISO /install.amd/initrd.gz=7.0.14+deb14-amd64 != selected initrd=7.0.13+deb14-amd64\\n' >&2\n  exit 1\nfi\nprintf '{\"source_path\":\"/data/downloads/debian-usb/sources/debian/netinst/test\"}\\n'\n"
	if err := os.WriteFile(helperPath, []byte(script), 0755); err != nil {
		t.Fatalf("write helper: %v", err)
	}
	sudoScript := "#!/bin/sh\nset -eu\nIFS=$(printf '\\n\\t')\nprintf 'used\\n' >\"" + sudoMarkerPath + "\"\nexec \"$@\"\n"
	if err := os.WriteFile(sudoPath, []byte(sudoScript), 0755); err != nil {
		t.Fatalf("write sudo stub: %v", err)
	}
	oldPath := os.Getenv("PATH")
	if err := os.Setenv("PATH", tempDir+string(os.PathListSeparator)+oldPath); err != nil {
		t.Fatalf("set PATH: %v", err)
	}
	defer os.Setenv("PATH", oldPath)

	backend := &Backend{pythonHelper: helperPath}
	_, err := backend.PrepareManagedInstallerSource(
		"debian",
		"netinst",
		"/tmp/vmlinuz",
		"/tmp/initrd.gz",
		"/tmp/source.iso",
		[]string{"xxhash_generic"},
		"host-kernel",
		"",
		"",
	)
	if err == nil || !strings.Contains(err.Error(), "installer ISO and selected installer assets do not match") {
		t.Fatalf("expected preflight alignment failure, got %v", err)
	}
	if _, err := os.Stat(sudoMarkerPath); !os.IsNotExist(err) {
		t.Fatalf("expected alignment preflight to fail before sudo")
	}
	argsRaw, err := os.ReadFile(argsPath)
	if err != nil {
		t.Fatalf("read args: %v", err)
	}
	args := string(argsRaw)
	for _, fragment := range []string{"prepare-managed-installer-source", "--preflight-only\n1", "--iso-path\n/tmp/source.iso"} {
		if !strings.Contains(args, fragment) {
			t.Fatalf("expected %q in preflight args:\n%s", fragment, args)
		}
	}
}

func TestPrepareManagedInstallerSourcePassesRepoInitrdContentWithSudo(t *testing.T) {
	tempDir := t.TempDir()
	helperPath := filepath.Join(tempDir, "python-helper.sh")
	sudoPath := filepath.Join(tempDir, "sudo")
	sudoMarkerPath := filepath.Join(tempDir, "sudo-used.txt")
	argsPath := filepath.Join(tempDir, "args.txt")
	script := "#!/bin/sh\nset -eu\nIFS=$(printf '\\n\\t')\nprintf '%s\\n' \"$@\" >\"" + argsPath + "\"\nprintf '{\"source_path\":\"/data/downloads/debian-usb/sources/debian/netinst/test\"}\\n'\n"
	if err := os.WriteFile(helperPath, []byte(script), 0755); err != nil {
		t.Fatalf("write helper: %v", err)
	}
	sudoScript := "#!/bin/sh\nset -eu\nIFS=$(printf '\\n\\t')\nprintf 'used\\n' >\"" + sudoMarkerPath + "\"\nexec \"$@\"\n"
	if err := os.WriteFile(sudoPath, []byte(sudoScript), 0755); err != nil {
		t.Fatalf("write sudo stub: %v", err)
	}
	oldPath := os.Getenv("PATH")
	if err := os.Setenv("PATH", tempDir+string(os.PathListSeparator)+oldPath); err != nil {
		t.Fatalf("set PATH: %v", err)
	}
	defer os.Setenv("PATH", oldPath)

	backend := &Backend{pythonHelper: helperPath}
	_, err := backend.PrepareManagedInstallerSource(
		"debian",
		"netinst",
		"/tmp/vmlinuz",
		"/tmp/initrd.gz",
		"/tmp/source.iso",
		nil,
		"",
		"/worktree/configs/preseed/preseed-debian.cfg",
		"/worktree/initrd/debian/netinst",
	)
	if err != nil {
		t.Fatalf("prepare managed installer source with embedded initrd preseed: %v", err)
	}
	if _, err := os.Stat(sudoMarkerPath); err != nil {
		t.Fatalf("expected sudo to be used for managed installer bundle preparation: %v", err)
	}
	argsRaw, err := os.ReadFile(argsPath)
	if err != nil {
		t.Fatalf("read args: %v", err)
	}
	args := string(argsRaw)
	for _, fragment := range []string{
		"--initrd-preseed-path\n/worktree/configs/preseed/preseed-debian.cfg",
		"--initrd-overlay-dir\n/worktree/initrd/debian/netinst",
	} {
		if !strings.Contains(args, fragment) {
			t.Fatalf("expected repo initrd content arg %q in args:\n%s", fragment, args)
		}
	}
}

func TestRemasterLiveInitrdSourcePassesSelectedOverlayWithSudo(t *testing.T) {
	tempDir := t.TempDir()
	helperPath := filepath.Join(tempDir, "python-helper.sh")
	sudoPath := filepath.Join(tempDir, "sudo")
	argsPath := filepath.Join(tempDir, "args.txt")
	sudoMarkerPath := filepath.Join(tempDir, "sudo-used.txt")
	if err := os.WriteFile(helperPath, []byte("#!/bin/sh\nset -eu\nprintf '%s\n' \"$@\" >\""+argsPath+"\"\nprintf '{\"iso_path\":\"/tmp/live-overlay.iso\"}\n'\n"), 0755); err != nil {
		t.Fatalf("write helper: %v", err)
	}
	if err := os.WriteFile(sudoPath, []byte("#!/bin/sh\nset -eu\nprintf 'used\n' >\""+sudoMarkerPath+"\"\nexec \"$@\"\n"), 0755); err != nil {
		t.Fatalf("write sudo stub: %v", err)
	}
	t.Setenv("PATH", tempDir+string(os.PathListSeparator)+os.Getenv("PATH"))

	backend := &Backend{pythonHelper: helperPath}
	isoPath, err := backend.RemasterLiveInitrdSource("debian", "/tmp/source.iso", "/worktree/initrd/debian/live")
	if err != nil {
		t.Fatalf("remaster Live initrd source: %v", err)
	}
	if isoPath != "/tmp/live-overlay.iso" {
		t.Fatalf("unexpected remastered path: %q", isoPath)
	}
	if _, err := os.Stat(sudoMarkerPath); err != nil {
		t.Fatalf("expected sudo for Live initrd remaster: %v", err)
	}
	argsRaw, err := os.ReadFile(argsPath)
	if err != nil {
		t.Fatalf("read args: %v", err)
	}
	args := string(argsRaw)
	for _, fragment := range []string{
		"remaster-live-initrd-source",
		"--profile\ndebian",
		"--source-iso\n/tmp/source.iso",
		"--overlay-dir\n/worktree/initrd/debian/live",
	} {
		if !strings.Contains(args, fragment) {
			t.Fatalf("expected %q in args:\n%s", fragment, args)
		}
	}
}

func TestRebuildDebianInstallerISOInvokesHelperWithPlanFile(t *testing.T) {
	tempDir := t.TempDir()
	helperPath := filepath.Join(tempDir, "build-iso-helper.sh")
	argsPath := filepath.Join(tempDir, "args.txt")
	planCopyPath := filepath.Join(tempDir, "plan.json")
	sudoPath := filepath.Join(tempDir, "sudo")
	sudoScript := "#!/bin/sh\nset -eu\nIFS=$(printf '\\n\\t')\nexec \"$@\"\n"
	if err := os.WriteFile(sudoPath, []byte(sudoScript), 0755); err != nil {
		t.Fatalf("write sudo stub: %v", err)
	}
	oldPath := os.Getenv("PATH")
	if err := os.Setenv("PATH", tempDir+string(os.PathListSeparator)+oldPath); err != nil {
		t.Fatalf("set PATH: %v", err)
	}
	defer os.Setenv("PATH", oldPath)
	script := "#!/bin/sh\nset -eu\nIFS=$(printf '\\n\\t')\nprintf '%s\\n' \"$@\" >\"" + argsPath + "\"\nplan=''\nwhile [ \"$#\" -gt 0 ]; do\n  if [ \"$1\" = '--rebuild-installer-plan' ]; then plan=\"$2\"; break; fi\n  shift\ndone\n[ -n \"$plan\" ] && [ -s \"$plan\" ] || { echo 'missing rebuild plan' >&2; exit 1; }\ncp -- \"$plan\" \"" + planCopyPath + "\"\nprintf '{\"run_id\":\"20260522T010203.000000Z\",\"iso_path\":\"/tmp/output.iso\",\"workspace_dir\":\"/tmp/workspace\",\"log_path\":\"/tmp/build.log\",\"manifest_path\":\"/tmp/manifest.json\",\"staged_udeb_repo_path\":\"/tmp/repo\",\"modified_paths\":[\"/install.amd/initrd.gz\"],\"warnings\":[]}\\n'\n"
	if err := os.WriteFile(helperPath, []byte(script), 0755); err != nil {
		t.Fatalf("write helper: %v", err)
	}

	backend := &Backend{buildISOHelper: helperPath}
	plan := RebuildInstallerISOPlan{
		SchemaVersion:          rebuildInstallerISOSchemaVersion,
		Distro:                 rebuildInstallerISODistroDebian,
		SourceISOPath:          "/tmp/source.iso",
		OutputDir:              "/tmp/out",
		ImageName:              "source-di-kmods.iso",
		Scope:                  rebuildInstallerISOScopeDI,
		Action:                 rebuildInstallerISOActionAddKernelModules,
		Architecture:           "amd64",
		InstallerKernelModules: []string{"erofs", "xxhash_generic"},
	}
	result, err := backend.RebuildDebianInstallerISO(plan)
	if err != nil {
		t.Fatalf("rebuild Debian installer ISO: %v", err)
	}
	if result.ISOPath != "/tmp/output.iso" {
		t.Fatalf("unexpected ISO path: %q", result.ISOPath)
	}
	argsRaw, err := os.ReadFile(argsPath)
	if err != nil {
		t.Fatalf("read args: %v", err)
	}
	args := string(argsRaw)
	if !strings.Contains(args, "--rebuild-installer-plan\n") {
		t.Fatalf("expected rebuild plan flag in args:\n%s", args)
	}
	planRaw, err := os.ReadFile(planCopyPath)
	if err != nil {
		t.Fatalf("read copied plan: %v", err)
	}
	var savedPlan RebuildInstallerISOPlan
	if err := json.Unmarshal(planRaw, &savedPlan); err != nil {
		t.Fatalf("decode copied plan: %v", err)
	}
	if savedPlan.Action != rebuildInstallerISOActionAddKernelModules {
		t.Fatalf("unexpected saved rebuild action: %q", savedPlan.Action)
	}
}

func TestNewBackendPrefersRepoAssetsFromCurrentWorkingDirectory(t *testing.T) {
	tempDir := t.TempDir()
	for _, path := range []string{
		filepath.Join(tempDir, "configs"),
		filepath.Join(tempDir, "scripts"),
	} {
		if err := os.MkdirAll(path, 0755); err != nil {
			t.Fatalf("mkdir %s: %v", path, err)
		}
	}
	configPath := filepath.Join(tempDir, "configs", "debian-usb.conf")
	pythonHelperPath := filepath.Join(tempDir, "scripts", "debian-usb-python")
	writeHelperPath := filepath.Join(tempDir, "scripts", "write_usb.sh")
	for _, target := range []string{configPath, pythonHelperPath, writeHelperPath} {
		if err := os.WriteFile(target, []byte("placeholder\n"), 0644); err != nil {
			t.Fatalf("write %s: %v", target, err)
		}
	}

	oldCwd, err := os.Getwd()
	if err != nil {
		t.Fatalf("getwd: %v", err)
	}
	defer func() {
		if chdirErr := os.Chdir(oldCwd); chdirErr != nil {
			t.Fatalf("restore cwd: %v", chdirErr)
		}
	}()
	if err := os.Chdir(tempDir); err != nil {
		t.Fatalf("chdir: %v", err)
	}

	for _, name := range []string{"DEBIAN_USB_CONFIG", "DEBIAN_USB_PYTHON_HELPER", "DEBIAN_USB_WRITE_HELPER"} {
		oldValue, hadValue := os.LookupEnv(name)
		if hadValue {
			defer os.Setenv(name, oldValue)
		} else {
			defer os.Unsetenv(name)
		}
		if err := os.Unsetenv(name); err != nil {
			t.Fatalf("unset %s: %v", name, err)
		}
	}

	backend := NewBackend()
	if backend.configPath != configPath {
		t.Fatalf("expected repo config path %q, got %q", configPath, backend.configPath)
	}
	if backend.pythonHelper != pythonHelperPath {
		t.Fatalf("expected repo python helper %q, got %q", pythonHelperPath, backend.pythonHelper)
	}
	if backend.writeHelper != writeHelperPath {
		t.Fatalf("expected repo write helper %q, got %q", writeHelperPath, backend.writeHelper)
	}
}
