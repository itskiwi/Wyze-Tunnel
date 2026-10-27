package main

import (
	"net/http"
	"net/http/httptest"
	"strings"
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

func TestPublicProxyMarksRequests(t *testing.T) {
	app := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Server", "Werkzeug/0.0")
		w.Write([]byte(r.Host + " " + r.Header.Get(PublicHeader)))
	}))
	defer app.Close()
	front := httptest.NewServer(publicProxy(strings.TrimPrefix(app.URL, "http://")))
	defer front.Close()
	req, _ := http.NewRequest("GET", front.URL, nil)
	req.Host = "wyze-tunnel.tail1234.ts.net"
	req.Header.Set(PublicHeader, "") // a visitor can't clear it
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	if resp.Header.Get("Server") != "" {
		t.Fatal("Server header leaked")
	}
	body := make([]byte, 100)
	n, _ := resp.Body.Read(body)
	if got := string(body[:n]); got != "wyze-tunnel.tail1234.ts.net 1" {
		t.Fatal(got)
	}
}
