package main

import (
	"testing"

	"tailscale.com/ipn/ipnstate"
)

func TestStatusOf(t *testing.T) {
	st := &ipnstate.Status{BackendState: "Running", Self: &ipnstate.PeerStatus{DNSName: "wyze-tunnel.tail1234.ts.net."}}
	if s := statusOf(st); s.State != "Running" || s.Host != "wyze-tunnel.tail1234.ts.net" {
		t.Fatal(s)
	}
	if s := statusOf(&ipnstate.Status{BackendState: "NeedsLogin", AuthURL: "https://login.example/a"}); s.Host != "" || s.AuthURL == "" {
		t.Fatal(s)
	}
}
