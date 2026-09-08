package app

import (
	"fmt"
	"os"
	"path/filepath"
	"strings"
)

func splitInstallerProfile(profile, role string) bool {
	return (profile == profileDebian || profile == profileKaliLinux) &&
		(role == multiOSSourceRoleNetinst || role == multiOSSourceRoleNetboot)
}

func cloneHDMediaPreseedDirs(values map[string]string) map[string]string {
	if len(values) == 0 {
		return nil
	}
	result := make(map[string]string, len(values))
	for key, value := range values {
		result[key] = value
	}
	return result
}

// A path is consent only when it is explicitly recorded by the user. Host
// configuration paths are neither defaults nor implicit staging requests.
func normalizeHDMediaPreseedDirs(profile, role string, values map[string]string) (map[string]string, error) {
	if len(values) == 0 {
		return nil, nil
	}
	if !splitInstallerProfile(profile, role) {
		return nil, fmt.Errorf("Desktop/Server HD-MEDIA content requires Debian/Kali netinst or netboot")
	}
	result := make(map[string]string)
	for flavor, raw := range values {
		if flavor != "desktop" && flavor != "server" {
			return nil, fmt.Errorf("unknown HD-MEDIA profile: %s", flavor)
		}
		if strings.TrimSpace(raw) == "" {
			continue
		}
		path := normalizePathInput(raw)
		absolute, err := filepath.Abs(path)
		if err != nil {
			return nil, err
		}
		path, err = filepath.EvalSymlinks(absolute)
		if err != nil {
			return nil, fmt.Errorf("%s preseed codebase: %w", flavor, err)
		}
		info, err := os.Stat(path)
		if err != nil {
			return nil, err
		}
		if info.Mode().IsRegular() {
			if filepath.Base(path) != "preseed.cfg" {
				return nil, fmt.Errorf("select a file named preseed.cfg")
			}
			path = filepath.Dir(path)
		} else if !info.IsDir() {
			return nil, fmt.Errorf("preseed codebase is not a regular file or directory: %s", path)
		}
		if path == string(filepath.Separator) {
			return nil, fmt.Errorf("refusing the filesystem root as a preseed codebase")
		}
		seed := filepath.Join(path, "preseed.cfg")
		seedInfo, err := os.Lstat(seed)
		if err != nil || !seedInfo.Mode().IsRegular() || seedInfo.Size() == 0 {
			return nil, fmt.Errorf("%s requires a nonempty regular preseed.cfg: %s", flavor, seed)
		}
		result[flavor] = path
	}
	return result, nil
}

func (a *App) promptHDMediaPreseedDirs(profile string) (map[string]string, error) {
	family := "DEBIAN"
	if profile == profileKaliLinux {
		family = "KALI"
	}
	selected := make(map[string]string)
	for _, choice := range []struct{ flavor, suffix, title string }{
		{"desktop", "DE", "DESKTOP"}, {"server", "SRV", "SERVER"},
	} {
		include, err := a.promptYesNo("Add HD-MEDIA preseed.cfg and its entire codebase for "+family+" "+choice.title, false)
		if err != nil {
			return nil, err
		}
		if !include {
			continue
		}
		for {
			raw, err := a.promptRequiredString("Path to preseed.cfg (ALL content in its parent directory will be copied)", "")
			if err != nil {
				return nil, err
			}
			normalized, err := normalizeHDMediaPreseedDirs(profile, multiOSSourceRoleNetinst, map[string]string{choice.flavor: raw})
			if err != nil {
				fmt.Println(err)
				continue
			}
			selected[choice.flavor] = normalized[choice.flavor]
			break
		}
	}
	return selected, nil
}

func hdMediaPreseedSummary(values map[string]string) string {
	parts := make([]string, 0, 2)
	for _, flavor := range []string{"desktop", "server"} {
		source := values[flavor]
		if source == "" {
			source = "not copied (USB menu remains available)"
		}
		parts = append(parts, flavor+": "+source)
	}
	return strings.Join(parts, "; ")
}
