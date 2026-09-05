package app

import (
	"bufio"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestInstallerSelectionUsesOnlyStageOverlayPrompt(t *testing.T) {
	for _, profile := range []string{profileDebian, profileKaliLinux} {
		for _, role := range []string{multiOSSourceRoleNetinst, multiOSSourceRoleNetboot} {
			for _, include := range []bool{false, true} {
				t.Run(fmt.Sprintf("%s/%s/include=%t", profile, role, include), func(t *testing.T) {
					root := t.TempDir()
					initrdRoot := filepath.Join(root, "initrd")
					overlay := filepath.Join(initrdRoot, initrdOverlayFamily(profile), role)
					if err := os.MkdirAll(overlay, 0755); err != nil {
						t.Fatal(err)
					}
					preseed := filepath.Join(overlay, "preseed.cfg")
					if err := os.WriteFile(preseed, []byte("# selected overlay preseed\n"), 0600); err != nil {
						t.Fatal(err)
					}
					answers := []string{"2"} // local assets, not a download
					for _, name := range []string{"installer.iso", "linux", "initrd.gz"} {
						path := filepath.Join(root, name)
						if err := os.WriteFile(path, []byte("source fixture\n"), 0644); err != nil {
							t.Fatal(err)
						}
						if name != "installer.iso" || role != multiOSSourceRoleNetboot {
							answers = append(answers, path)
						}
					}
					if profile == profileDebian && role == multiOSSourceRoleNetinst {
						answers = append(answers, fmt.Sprint(len(debianNetinstExtraModuleOptions)+1))
					}
					answer := "n"
					if include {
						answer = "y"
					}
					answers = append(answers, answer, "next-question")
					application := App{
						// A missing legacy preseed directory must not be read or
						// trigger a second prompt, regardless of overlay choice.
						backend: &Backend{initrdRoot: initrdRoot, preseedRoot: filepath.Join(root, "missing-preseed")},
						reader:  bufio.NewReader(strings.NewReader(strings.Join(answers, "\n") + "\n")),
					}
					selected, action, err := application.collectSourceSelection(profileSpecs[profile], role)
					if err != nil || action != menuStay {
						t.Fatalf("selection failed: %v, %v", action, err)
					}
					want := ""
					if include {
						want = overlay
					}
					if selected.InitrdOverlayDir != want || selected.InitrdPreseedPath != "" {
						t.Fatalf("unexpected root content selection: %#v", selected)
					}
					next, err := application.reader.ReadString('\n')
					if err != nil || next != "next-question\n" {
						t.Fatalf("unexpected extra prompt consumed input: %q, %v", next, err)
					}
					content, err := os.ReadFile(preseed)
					if err != nil || string(content) != "# selected overlay preseed\n" {
						t.Fatal("selection modified overlay content")
					}
				})
			}
		}
	}
}
