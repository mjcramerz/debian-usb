package app

import (
	"fmt"
	"io"
	"os"
	"strconv"
	"strings"
	"syscall"
	"unicode"
	"unsafe"
)

const ansiReset = "\x1b[0m"

func terminalColor() bool {
	if _, disabled := os.LookupEnv("NO_COLOR"); disabled {
		return false
	}
	return isTerminal(os.Stdout) && os.Getenv("TERM") != "dumb"
}
func styled(text, code string) string {
	if !terminalColor() {
		return text
	}
	return "\x1b[" + code + "m" + text + ansiReset
}
func safeTerminalText(text string) string {
	return strings.Map(func(r rune) rune {
		if unicode.IsControl(r) || unicode.Is(unicode.Cf, r) {
			return ' '
		}
		return r
	}, text)
}

func (a *App) printMenu(entries ...menuEntry) {
	a.menuEntries = append([]menuEntry(nil), entries...)
	if a.stdin == nil || !isTerminal(a.stdin) || !isTerminal(os.Stdout) || os.Getenv("TERM") == "dumb" {
		printMenu(entries...)
	}
}

func menuMove(index, delta, count int) int {
	if count <= 0 {
		return 0
	}
	return (index + delta%count + count) % count
}

// readMenuSequence does not block forever on a bare Escape key. It also works
// with bracketed terminal sequences split across reads.
func (a *App) readMenuSequence() (string, error) {
	sequence := ""
	for len(sequence) < 16 {
		if a.reader.Buffered() == 0 {
			fd := int(a.stdin.Fd())
			var set syscall.FdSet
			if fd < 0 || fd >= len(set.Bits)*64 {
				return sequence, nil
			}
			set.Bits[fd/64] |= 1 << uint(fd%64)
			timeout := syscall.Timeval{Usec: 100000}
			ready, err := syscall.Select(fd+1, &set, nil, nil, &timeout)
			if err != nil {
				return "", err
			}
			if ready == 0 {
				return sequence, nil
			}
		}
		c, _, err := a.reader.ReadRune()
		if err != nil {
			return "", err
		}
		sequence += string(c)
		if len(sequence) == 1 && c != '[' && c != 'O' {
			return sequence, nil
		}
		if len(sequence) > 1 && (c == '~' || c >= 'A' && c <= 'Z' || c >= 'a' && c <= 'z') {
			return sequence, nil
		}
	}
	return sequence, nil
}

func (a *App) interactiveMenuChoice(label string, entries []menuEntry) (string, error) {
	filtered := make([]menuEntry, 0, len(entries))
	for _, entry := range entries {
		if entry.Key != "" {
			filtered = append(filtered, entry)
		}
	}
	entries = filtered
	if len(entries) == 0 {
		return a.readPromptLine(label + ": ")
	}
	state, err := makeRaw(a.stdin.Fd())
	if err != nil {
		printMenu(entries...)
		return a.readBufferedPromptLine(label + ": ")
	}
	defer restoreTerminal(a.stdin.Fd(), state)
	fmt.Print("\x1b[?25l")
	defer fmt.Print("\x1b[?25h")
	active, drawn := 0, 0
	typed := ""
	render := func() {
		if drawn > 0 {
			fmt.Printf("\x1b[%dA", drawn)
		}
		start := 0
		columns, lines := terminalDimensions()
		visible := maxInt(3, minInt(12, lines-6))
		if active >= visible {
			start = active - visible + 1
		}
		end := start + visible
		if end > len(entries) {
			end = len(entries)
		}
		rows := make([]string, 0, visible+2)
		for i := start; i < end; i++ {
			e := entries[i]
			pointer, mark := "  ", " "
			if e.Selected {
				mark = "x"
			}
			if i == active {
				pointer = "> "
			}
			text := truncateTerminalLine(fmt.Sprintf("%s[%s] [%s] %s", pointer, e.Key, mark, safeTerminalText(e.Label)), columns-1)
			if i == active {
				text = styled(text, "1;30;46")
			} else if e.Selected {
				text = styled(text, "1;32")
			}
			rows = append(rows, text)
		}
		rows = append(rows, styled(truncateTerminalLine("  "+safeTerminalText(entries[active].Detail), columns-1), "2"))
		rows = append(rows, styled(truncateTerminalLine("  Up/Down  Enter/Space  Number+Enter  b Back  e Exit", columns-1), "2"))
		rows = append(rows, truncateTerminalLine(safeTerminalText(label)+": "+typed, columns-1))
		if drawn > len(rows) {
			for len(rows) < drawn {
				rows = append(rows, "")
			}
		}
		for _, row := range rows {
			fmt.Print("\r\x1b[2K", row, "\r\n")
		}
		drawn = len(rows)
	}
	render()
	for {
		c, _, err := a.reader.ReadRune()
		if err != nil {
			return "", err
		}
		switch c {
		case 3:
			return "", ErrUserInterrupt
		case 4:
			return "", io.EOF
		case 27:
			sequence, err := a.readMenuSequence()
			if err != nil {
				return "", err
			}
			typed = ""
			switch sequence {
			case "[A", "OA":
				active = menuMove(active, -1, len(entries))
			case "[B", "OB":
				active = menuMove(active, 1, len(entries))
			case "[H", "OH", "[1~":
				active = 0
			case "[F", "OF", "[4~":
				active = len(entries) - 1
			case "[5~":
				active = menuMove(active, -10, len(entries))
			case "[6~":
				active = menuMove(active, 10, len(entries))
			case "":
				for _, e := range entries {
					if e.Key == "b" {
						return "b", nil
					}
				}
			}
		case '\r', '\n', ' ':
			if typed == "" {
				return entries[active].Key, nil
			}
			for _, e := range entries {
				if e.Key == typed {
					return e.Key, nil
				}
			}
			typed = ""
		case 8, 127:
			if len(typed) > 0 {
				typed = typed[:len(typed)-1]
			}
		default:
			if c >= '0' && c <= '9' {
				if len(typed) < 6 {
					typed += string(c)
				}
				for i, e := range entries {
					if e.Key == typed {
						active = i
					}
				}
			} else {
				key := strings.ToLower(string(c))
				if key == "q" {
					key = "e"
				}
				for _, e := range entries {
					if e.Key == key {
						return e.Key, nil
					}
				}
				if c == 'j' {
					active = menuMove(active, 1, len(entries))
				}
				if c == 'k' {
					active = menuMove(active, -1, len(entries))
				}
			}
		}
		render()
	}
}

func compactGiB(size int64) string {
	if size <= 0 {
		return "unknown"
	}
	return strconv.FormatFloat(float64(size)/(1024*1024*1024), 'f', 1, 64) + " GiB"
}

func terminalDimensions() (int, int) {
	size := struct{ Rows, Cols, X, Y uint16 }{}
	_, _, errno := syscall.Syscall(syscall.SYS_IOCTL, os.Stdout.Fd(), uintptr(syscall.TIOCGWINSZ), uintptr(unsafe.Pointer(&size)))
	if errno != 0 || size.Cols < 20 || size.Rows < 5 {
		return 80, 24
	}
	return int(size.Cols), int(size.Rows)
}

func truncateTerminalLine(text string, width int) string {
	runes := []rune(text)
	if len(runes) <= width {
		return text
	}
	if width < 4 {
		return string(runes[:maxInt(0, width)])
	}
	return string(runes[:width-3]) + "..."
}

func minInt(a, b int) int {
	if a < b {
		return a
	}
	return b
}
