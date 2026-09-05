package app

import (
	"bufio"
	"errors"
	"strings"
	"testing"
)

func TestLineBufferSupportsMidLineInsertion(t *testing.T) {
	buffer := newLineBuffer("")
	buffer.Insert('a')
	buffer.Insert('b')
	buffer.Insert('c')
	if !buffer.ApplyEscapeSequence("[D") || !buffer.ApplyEscapeSequence("[D") {
		t.Fatalf("expected cursor movement to succeed")
	}
	buffer.Insert('Z')

	if got, want := buffer.String(), "aZbc"; got != want {
		t.Fatalf("expected %q, got %q", want, got)
	}
	if buffer.cursor != 2 {
		t.Fatalf("expected cursor at 2, got %d", buffer.cursor)
	}
}

func TestLineBufferBackspaceRemovesCharacterBeforeCursor(t *testing.T) {
	buffer := newLineBuffer("abc")
	if !buffer.ApplyEscapeSequence("[D") {
		t.Fatalf("expected left-arrow movement to succeed")
	}
	if !buffer.Backspace() {
		t.Fatalf("expected backspace to remove a character")
	}

	if got, want := buffer.String(), "ac"; got != want {
		t.Fatalf("expected %q, got %q", want, got)
	}
	if buffer.cursor != 1 {
		t.Fatalf("expected cursor at 1, got %d", buffer.cursor)
	}
}

func TestLineBufferDeleteAndHomeEndSequences(t *testing.T) {
	buffer := newLineBuffer("abcd")
	if !buffer.ApplyEscapeSequence("[D") || !buffer.ApplyEscapeSequence("[D") {
		t.Fatalf("expected left-arrow movement to succeed")
	}
	if !buffer.ApplyEscapeSequence("[3~") {
		t.Fatalf("expected delete sequence to be handled")
	}
	if got, want := buffer.String(), "abd"; got != want {
		t.Fatalf("expected %q, got %q", want, got)
	}
	if !buffer.ApplyEscapeSequence("[H") {
		t.Fatalf("expected home sequence to be handled")
	}
	if buffer.cursor != 0 {
		t.Fatalf("expected cursor at home position, got %d", buffer.cursor)
	}
	if !buffer.ApplyEscapeSequence("[F") {
		t.Fatalf("expected end sequence to be handled")
	}
	if buffer.cursor != len([]rune(buffer.String())) {
		t.Fatalf("expected cursor at end position, got %d", buffer.cursor)
	}
}

func TestPromptChoiceConsumesArrowKeyEscapeSequences(t *testing.T) {
	application := App{
		reader: bufio.NewReader(strings.NewReader("\x1b[D9\n")),
	}

	got, err := application.promptChoice("Select an option")
	if err != nil {
		t.Fatalf("promptChoice: %v", err)
	}
	if got != "9" {
		t.Fatalf("expected left-arrow escape to be swallowed, got %q", got)
	}
}

func TestPromptChoiceReturnsUserInterruptOnControlC(t *testing.T) {
	application := App{
		reader: bufio.NewReader(strings.NewReader("\x03")),
	}

	_, err := application.promptChoice("Select an option")
	if !errors.Is(err, ErrUserInterrupt) {
		t.Fatalf("expected ErrUserInterrupt, got %v", err)
	}
}
