package main

import (
	"errors"
	"fmt"
	"os"

	"debian-usb/internal/app"
)

func main() {
	application, err := app.New()
	if err != nil {
		fmt.Fprintf(os.Stderr, "debian-usb: %v\n", err)
		os.Exit(1)
	}
	if err := application.Run(os.Args[1:]); err != nil {
		if errors.Is(err, app.ErrUserInterrupt) {
			os.Exit(130)
		}
		fmt.Fprintf(os.Stderr, "debian-usb: %v\n", err)
		os.Exit(1)
	}
}
