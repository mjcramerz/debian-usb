package app

import "testing"

func TestGrubEntryGroupsLabel(t *testing.T) {
	if got := grubEntryGroupsLabel(true, false); got != "Custom GRUB replacement" {
		t.Fatalf("unexpected custom-menu entry group label: %q", got)
	}
	if got := grubEntryGroupsLabel(true, true); got != "Custom + preserved entry groups" {
		t.Fatalf("unexpected combined-menu entry group label: %q", got)
	}
	if got := grubEntryGroupsLabel(false, false); got != "Preserved entry groups" {
		t.Fatalf("unexpected standard-menu entry group label: %q", got)
	}
}

func TestGrubBootEntriesSummary(t *testing.T) {
	if got := grubBootEntriesSummary(true, false); got != "Replaced with curated custom GRUB entries derived from the ISO boot assets" {
		t.Fatalf("unexpected custom-menu boot summary: %q", got)
	}
	if got := grubBootEntriesSummary(true, true); got != "Curated custom GRUB entries plus preserved upstream-managed trees derived from the ISO boot assets" {
		t.Fatalf("unexpected combined-menu boot summary: %q", got)
	}
	if got := grubBootEntriesSummary(false, false); got != "Preserved from the ISO boot config" {
		t.Fatalf("unexpected standard-menu boot summary: %q", got)
	}
}

func TestGrubEntryGroupsSummary(t *testing.T) {
	if got := grubEntryGroupsSummary([]string{"Install", "Advanced options ..."}, true, false); got != "2 source ISO boot groups detected and replaced by curated custom GRUB entries" {
		t.Fatalf("unexpected custom-menu entry group summary: %q", got)
	}
	if got := grubEntryGroupsSummary([]string{"Install", "Advanced options ..."}, true, true); got != "2 source ISO boot groups detected; curated custom GRUB entries are rendered alongside preserved upstream-managed trees" {
		t.Fatalf("unexpected combined-menu entry group summary: %q", got)
	}
	if got := grubEntryGroupsSummary([]string{"Install", "Advanced options ..."}, false, false); got != "Install, Advanced options ..." {
		t.Fatalf("unexpected standard-menu entry group summary: %q", got)
	}
}
