package app

import (
	"fmt"
	"strings"
	"unicode/utf8"
)

type infoRow struct {
	Label string
	Value string
}

type menuEntry struct {
	Key    string
	Label  string
	Detail string
}

const (
	viewLabelWidth      = 28
	viewWrapWidth       = 104
	viewMenuLabelWidth  = 28
	viewSectionMinWidth = 12
	viewHeaderMinWidth  = 24
)

func printHeader(title string) {
	contentWidth := maxInt(viewHeaderMinWidth, utf8.RuneCountInString(title)+2)
	border := "+" + strings.Repeat("=", contentWidth+2) + "+"
	fmt.Printf("\n%s\n| %-*s |\n%s\n\n", border, contentWidth, title, border)
}

func printStepHeader(step, total int, title string) {
	printHeader(fmt.Sprintf("Step %d/%d - %s", step, total, title))
}

func printSection(title string, rows ...infoRow) {
	fmt.Printf("%s\n", title)
	fmt.Printf("%s\n", strings.Repeat("-", maxInt(viewSectionMinWidth, utf8.RuneCountInString(title))))
	for _, row := range rows {
		printInfoRow(row)
	}
	fmt.Println()
}

func printMenu(entries ...menuEntry) {
	for _, entry := range entries {
		if entry.Key == "" {
			continue
		}
		if entry.Detail != "" {
			printWrappedLines(
				fmt.Sprintf("[%s] %-*s ", entry.Key, viewMenuLabelWidth, entry.Label),
				entry.Detail,
				viewWrapWidth,
			)
			continue
		}
		fmt.Printf("[%s] %s\n", entry.Key, entry.Label)
	}
	fmt.Println()
}

func printBulletList(title string, items ...string) {
	filtered := make([]string, 0, len(items))
	for _, item := range items {
		if strings.TrimSpace(item) == "" {
			continue
		}
		filtered = append(filtered, strings.TrimSpace(item))
	}
	if len(filtered) == 0 {
		return
	}
	fmt.Printf("%s\n", title)
	fmt.Printf("%s\n", strings.Repeat("-", maxInt(viewSectionMinWidth, utf8.RuneCountInString(title))))
	for _, item := range filtered {
		printWrappedLines("  - ", item, viewWrapWidth)
	}
	fmt.Println()
}

func printInfoRow(row infoRow) {
	label := strings.TrimSpace(row.Label)
	if label != "" {
		label += ":"
	}
	printWrappedLines(
		fmt.Sprintf("  %-*s ", viewLabelWidth, label),
		displayValueOrNone(row.Value),
		viewWrapWidth,
	)
}

func printWrappedLines(prefix, value string, width int) {
	lines := wrapText(value, maxInt(24, width-utf8.RuneCountInString(prefix)))
	if len(lines) == 0 {
		fmt.Printf("%s\n", prefix)
		return
	}
	fmt.Printf("%s%s\n", prefix, lines[0])
	for _, line := range lines[1:] {
		fmt.Printf("%s%s\n", strings.Repeat(" ", utf8.RuneCountInString(prefix)), line)
	}
}

func wrapText(value string, width int) []string {
	width = maxInt(8, width)
	trimmed := strings.TrimSpace(value)
	if trimmed == "" {
		return []string{"<none>"}
	}

	var lines []string
	for _, paragraph := range strings.Split(trimmed, "\n") {
		paragraph = strings.TrimSpace(paragraph)
		if paragraph == "" {
			if len(lines) == 0 || lines[len(lines)-1] != "" {
				lines = append(lines, "")
			}
			continue
		}
		current := ""
		for _, token := range strings.Fields(paragraph) {
			chunks := splitToken(token, width)
			for _, chunk := range chunks {
				if current == "" {
					current = chunk
					continue
				}
				candidate := current + " " + chunk
				if utf8.RuneCountInString(candidate) <= width {
					current = candidate
					continue
				}
				lines = append(lines, current)
				current = chunk
			}
		}
		if current != "" {
			lines = append(lines, current)
		}
	}
	if len(lines) == 0 {
		return []string{"<none>"}
	}
	return lines
}

func splitToken(token string, width int) []string {
	if utf8.RuneCountInString(token) <= width {
		return []string{token}
	}
	runes := []rune(token)
	chunks := make([]string, 0, (len(runes)/width)+1)
	for len(runes) > width {
		chunks = append(chunks, string(runes[:width]))
		runes = runes[width:]
	}
	if len(runes) > 0 {
		chunks = append(chunks, string(runes))
	}
	return chunks
}

func maxInt(values ...int) int {
	maximum := values[0]
	for _, value := range values[1:] {
		if value > maximum {
			maximum = value
		}
	}
	return maximum
}

func joinOrNone(values []string) string {
	filtered := make([]string, 0, len(values))
	for _, value := range values {
		if strings.TrimSpace(value) == "" {
			continue
		}
		filtered = append(filtered, strings.TrimSpace(value))
	}
	if len(filtered) == 0 {
		return "<none>"
	}
	return strings.Join(filtered, ", ")
}

func mapBoolLabel(value bool, whenTrue, whenFalse string) string {
	if value {
		return whenTrue
	}
	return whenFalse
}
