package main

import (
	"testing"

	"github.com/AlexxIT/go2rtc/pkg/tutk"
	"github.com/AlexxIT/go2rtc/pkg/wyze"
)

// Catches a go2rtc upgrade that renames the client's session field.
func TestConnOf(t *testing.T) {
	if connOf(&wyze.Client{}) != nil {
		t.Fatal("expected no session")
	}
}

func TestFindHL(t *testing.T) {
	frame := append(make([]byte, 32), tutk.HL(10041, []byte{1})...)
	if cmd, payload, ok := tutk.ParseHL(findHL(frame)); !ok || cmd != 10041 || payload[0] != 1 {
		t.Fatal(cmd, payload, ok)
	}
}
