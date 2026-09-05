package app

import (
	"bufio"
	"errors"
	"fmt"
	"io"
	"os"
	"strconv"
	"strings"
	"syscall"
	"unsafe"
)

const (
	leftArrowEscapeSequence  = "[D"
	rightArrowEscapeSequence = "[C"
)

var ErrUserInterrupt = errors.New("user interrupt")

func (a *App) promptChoice(label string) (string, error) {
	line, err := a.readPromptLine(fmt.Sprintf("%s: ", label))
	if err != nil {
		return "", err
	}
	return normalizePromptChoice(strings.TrimSpace(line)), nil
}

func (a *App) promptRequiredString(label string, current string) (string, error) {
	for {
		prompt := fmt.Sprintf("%s: ", label)
		if current != "" {
			prompt = fmt.Sprintf("%s [%s]: ", label, current)
		}
		line, err := a.readPromptLine(prompt)
		if err != nil {
			return "", err
		}
		value := strings.TrimSpace(line)
		if value == "" && current != "" {
			return current, nil
		}
		if value != "" {
			return value, nil
		}
		fmt.Println("A value is required.")
	}
}

func (a *App) promptOptionalString(label string) (string, error) {
	line, err := a.readPromptLine(fmt.Sprintf("%s: ", label))
	if err != nil {
		return "", err
	}
	return strings.TrimSpace(line), nil
}

func (a *App) promptEditableOptionalString(label, current string) (string, error) {
	line, err := a.readPromptLine(fmt.Sprintf("%s [%s] (Enter keeps current, '-' clears): ", label, displayValueOrNone(current)))
	if err != nil {
		return "", err
	}
	value := collapseWhitespace(strings.TrimSpace(line))
	switch value {
	case "":
		return current, nil
	case "-":
		return "", nil
	default:
		return value, nil
	}
}

func (a *App) promptPositiveInt(label string, defaultValue int) (int, error) {
	for {
		line, err := a.readPromptLine(fmt.Sprintf("%s [%d]: ", label, defaultValue))
		if err != nil {
			return 0, err
		}
		value := strings.TrimSpace(line)
		if value == "" {
			return defaultValue, nil
		}
		number, err := strconv.Atoi(value)
		if err != nil || number <= 0 {
			fmt.Println("Enter a positive integer.")
			continue
		}
		return number, nil
	}
}

func (a *App) promptNonNegativeInt(label string, defaultValue int) (int, error) {
	for {
		line, err := a.readPromptLine(fmt.Sprintf("%s [%d]: ", label, defaultValue))
		if err != nil {
			return 0, err
		}
		value := strings.TrimSpace(line)
		if value == "" {
			return defaultValue, nil
		}
		number, err := strconv.Atoi(value)
		if err != nil || number < 0 {
			fmt.Println("Enter zero or a positive integer.")
			continue
		}
		return number, nil
	}
}

func (a *App) promptYesNo(label string, defaultValue bool) (bool, error) {
	defaultLabel := "y/N"
	if defaultValue {
		defaultLabel = "Y/n"
	}
	for {
		line, err := a.readPromptLine(fmt.Sprintf("%s [%s]: ", label, defaultLabel))
		if err != nil {
			return false, err
		}
		value := strings.ToLower(strings.TrimSpace(line))
		switch value {
		case "":
			return defaultValue, nil
		case "y", "yes":
			return true, nil
		case "n", "no":
			return false, nil
		default:
			fmt.Println("Enter yes or no.")
		}
	}
}

type lineBuffer struct {
	chars  []rune
	cursor int
}

func newLineBuffer(initial string) lineBuffer {
	chars := []rune(initial)
	return lineBuffer{
		chars:  chars,
		cursor: len(chars),
	}
}

func (b *lineBuffer) String() string {
	return string(b.chars)
}

func (b *lineBuffer) Insert(value rune) {
	if b.cursor >= len(b.chars) {
		b.chars = append(b.chars, value)
		b.cursor = len(b.chars)
		return
	}
	b.chars = append(b.chars[:b.cursor], append([]rune{value}, b.chars[b.cursor:]...)...)
	b.cursor++
}

func (b *lineBuffer) Backspace() bool {
	if b.cursor == 0 {
		return false
	}
	b.chars = append(b.chars[:b.cursor-1], b.chars[b.cursor:]...)
	b.cursor--
	return true
}

func (b *lineBuffer) Delete() bool {
	if b.cursor >= len(b.chars) {
		return false
	}
	b.chars = append(b.chars[:b.cursor], b.chars[b.cursor+1:]...)
	return true
}

func (b *lineBuffer) MoveLeft() bool {
	if b.cursor == 0 {
		return false
	}
	b.cursor--
	return true
}

func (b *lineBuffer) MoveRight() bool {
	if b.cursor >= len(b.chars) {
		return false
	}
	b.cursor++
	return true
}

func (b *lineBuffer) MoveHome() bool {
	if b.cursor == 0 {
		return false
	}
	b.cursor = 0
	return true
}

func (b *lineBuffer) MoveEnd() bool {
	if b.cursor == len(b.chars) {
		return false
	}
	b.cursor = len(b.chars)
	return true
}

func (b *lineBuffer) ApplyEscapeSequence(sequence string) bool {
	switch sequence {
	case leftArrowEscapeSequence:
		return b.MoveLeft()
	case rightArrowEscapeSequence:
		return b.MoveRight()
	case "[H", "[1~", "OH":
		return b.MoveHome()
	case "[F", "[4~", "OF":
		return b.MoveEnd()
	case "[3~":
		return b.Delete()
	default:
		return false
	}
}

type terminalState struct {
	termios syscall.Termios
}

func (a *App) readPromptLine(prompt string) (string, error) {
	if a.stdin == nil || !isTerminal(a.stdin) {
		return a.readBufferedPromptLine(prompt)
	}
	return a.readInteractivePromptLine(prompt)
}

func (a *App) readBufferedPromptLine(prompt string) (string, error) {
	fmt.Print(prompt)
	line, err := a.reader.ReadString('\n')
	if strings.ContainsRune(line, rune(3)) {
		return "", ErrUserInterrupt
	}
	if err != nil {
		if errors.Is(err, io.EOF) && line != "" {
			return strings.TrimRight(line, "\r\n"), nil
		}
		return "", err
	}
	return strings.TrimRight(line, "\r\n"), nil
}

func (a *App) readInteractivePromptLine(prompt string) (string, error) {
	state, err := makeRaw(a.stdin.Fd())
	if err != nil {
		return a.readBufferedPromptLine(prompt)
	}
	defer restoreTerminal(a.stdin.Fd(), state)

	buffer := newLineBuffer("")
	render := func() {
		fmt.Fprint(os.Stdout, "\r\x1b[2K", prompt, buffer.String())
		if trailing := len(buffer.chars) - buffer.cursor; trailing > 0 {
			fmt.Fprintf(os.Stdout, "\x1b[%dD", trailing)
		}
	}

	render()
	for {
		character, _, readErr := a.reader.ReadRune()
		if readErr != nil {
			fmt.Fprint(os.Stdout, "\n")
			return "", readErr
		}
		switch character {
		case '\r', '\n':
			fmt.Fprint(os.Stdout, "\r\x1b[2K", prompt, buffer.String(), "\n")
			return buffer.String(), nil
		case 3:
			fmt.Fprint(os.Stdout, "\n")
			return "", ErrUserInterrupt
		case 4:
			if len(buffer.chars) == 0 {
				fmt.Fprint(os.Stdout, "\n")
				return "", io.EOF
			}
		case 8, 127:
			if buffer.Backspace() {
				render()
			}
		case 1:
			if buffer.MoveHome() {
				render()
			}
		case 5:
			if buffer.MoveEnd() {
				render()
			}
		case 27:
			sequence, sequenceErr := readEscapeSequence(a.reader)
			if sequenceErr != nil {
				fmt.Fprint(os.Stdout, "\n")
				return "", sequenceErr
			}
			if buffer.ApplyEscapeSequence(sequence) {
				render()
			}
		default:
			if character < 32 {
				continue
			}
			buffer.Insert(character)
			render()
		}
	}
}

func readEscapeSequence(reader *bufio.Reader) (string, error) {
	next, _, err := reader.ReadRune()
	if err != nil {
		return "", err
	}
	switch next {
	case '[':
		sequence := []rune{'['}
		for {
			character, _, readErr := reader.ReadRune()
			if readErr != nil {
				return "", readErr
			}
			sequence = append(sequence, character)
			if character == '~' || (character >= 'A' && character <= 'Z') || (character >= 'a' && character <= 'z') {
				return string(sequence), nil
			}
		}
	case 'O':
		character, _, readErr := reader.ReadRune()
		if readErr != nil {
			return "", readErr
		}
		return "O" + string(character), nil
	default:
		return string(next), nil
	}
}

func normalizePromptChoice(value string) string {
	normalized := strings.TrimSpace(value)
	for _, sequence := range []string{
		"\x1b" + leftArrowEscapeSequence,
		"\x1b" + rightArrowEscapeSequence,
		"\x1b[A",
		"\x1b[B",
		"\x1b[H",
		"\x1b[F",
		"\x1b[1~",
		"\x1b[3~",
		"\x1b[4~",
		"\x1bOH",
		"\x1bOF",
	} {
		normalized = strings.ReplaceAll(normalized, sequence, "")
	}
	normalized = strings.TrimSpace(strings.ToLower(normalized))
	switch normalized {
	case "b", "back":
		return "b"
	case "e", "exit", "quit", "q":
		return "e"
	default:
		return normalized
	}
}

func isTerminal(file *os.File) bool {
	if file == nil {
		return false
	}
	_, err := getTerminalState(file.Fd())
	return err == nil
}

func makeRaw(fd uintptr) (*terminalState, error) {
	state, err := getTerminalState(fd)
	if err != nil {
		return nil, err
	}
	raw := state.termios
	raw.Iflag &^= syscall.BRKINT | syscall.ICRNL | syscall.INPCK | syscall.ISTRIP | syscall.IXON
	raw.Oflag &^= syscall.OPOST
	raw.Cflag |= syscall.CS8
	raw.Lflag &^= syscall.ECHO | syscall.ICANON | syscall.IEXTEN | syscall.ISIG
	raw.Cc[syscall.VMIN] = 1
	raw.Cc[syscall.VTIME] = 0
	if err := setTerminalState(fd, &raw); err != nil {
		return nil, err
	}
	return state, nil
}

func restoreTerminal(fd uintptr, state *terminalState) error {
	if state == nil {
		return nil
	}
	return setTerminalState(fd, &state.termios)
}

func getTerminalState(fd uintptr) (*terminalState, error) {
	state := &terminalState{}
	if err := ioctlTermios(fd, syscall.TCGETS, &state.termios); err != nil {
		return nil, err
	}
	return state, nil
}

func setTerminalState(fd uintptr, termios *syscall.Termios) error {
	return ioctlTermios(fd, syscall.TCSETS, termios)
}

func ioctlTermios(fd uintptr, request uintptr, termios *syscall.Termios) error {
	_, _, errno := syscall.Syscall6(
		syscall.SYS_IOCTL,
		fd,
		request,
		uintptr(unsafe.Pointer(termios)),
		0,
		0,
		0,
	)
	if errno != 0 {
		return errno
	}
	return nil
}

func onOffLabel(value bool) string {
	if value {
		return "On"
	}
	return "Off"
}

func memGiBLabel(value int) string {
	if value <= 0 {
		return "Disabled"
	}
	return fmt.Sprintf("%d GiB", value)
}

func displayValueOrNone(value string) string {
	if strings.TrimSpace(value) == "" {
		return "<none>"
	}
	return strings.TrimSpace(value)
}

func configuredState(value string) string {
	if strings.TrimSpace(value) == "" {
		return "Default"
	}
	return "Configured"
}

func summarizeValue(value string, maxLen int) string {
	trimmed := strings.TrimSpace(value)
	if trimmed == "" {
		return "<none>"
	}
	if maxLen <= 3 || len(trimmed) <= maxLen {
		return trimmed
	}
	return trimmed[:maxLen-3] + "..."
}

func blankIfEmpty(primary, fallback string) string {
	if strings.TrimSpace(primary) != "" {
		return strings.TrimSpace(primary)
	}
	return fallback
}
