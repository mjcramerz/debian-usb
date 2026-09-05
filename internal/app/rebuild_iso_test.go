package app

import "testing"

func TestDefaultRebuildInstallerISOImageNameUsesActionSuffix(t *testing.T) {
	got := defaultRebuildInstallerISOImageName("/tmp/debian-13-netinst.iso", rebuildInstallerISOActionUpdateKernel)
	if got != "debian-13-netinst-di-kernel.iso" {
		t.Fatalf("unexpected rebuilt ISO name: %q", got)
	}
}

func TestNewRebuildInstallerISOPlanDefaultsOutputAndArchitecture(t *testing.T) {
	inspection := RebuildInstallerISOInspection{
		SourcePath:   "/tmp/source.iso",
		Architecture: "amd64",
	}
	plan := newRebuildInstallerISOPlan(inspection, rebuildInstallerISOScopeDI, rebuildInstallerISOActionAddUdebPackages)
	if plan.OutputDir != defaultRebuildInstallerISOOutputDir {
		t.Fatalf("unexpected output dir: %q", plan.OutputDir)
	}
	if plan.Architecture != "amd64" {
		t.Fatalf("unexpected architecture: %q", plan.Architecture)
	}
	if plan.ImageName != "source-di-udebs.iso" {
		t.Fatalf("unexpected image name: %q", plan.ImageName)
	}
}
