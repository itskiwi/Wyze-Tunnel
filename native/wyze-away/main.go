// Command wyze-away puts the web UI on the user's own private Tailscale network with embedded Tailscale (tsnet),
// so a phone with the Tailscale app can open it from anywhere. No router ports are opened. It answers on
// http (port 80) and, once the tailnet has HTTPS certificates turned on, https (443) for passkeys.
//
// With -funnel it also asks for a public https link (Tailscale Funnel) for phones without the app. Requests
// through it carry PublicHeader, and the bridge then lets in only phones paired by QR code.
package main

import (
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"log"
	"net"
	"net/http"
	"net/http/httputil"
	"net/url"
	"os"
	"strings"
	"sync"
	"time"

	"tailscale.com/client/local"
	"tailscale.com/ipn/ipnstate"
	"tailscale.com/tsnet"
)

// PublicHeader marks requests that came in over the public link. The proxy always sets it, so nobody on the
// internet can drop it, and adding it on the home network only locks that request down further.
const PublicHeader = "X-Wyze-Away-Public"

type status struct {
	State   string `json:"state"`
	AuthURL string `json:"auth_url"`
	Host    string `json:"host"`
	// Public is the no-app link once Funnel works; FunnelURL is Tailscale's page to allow it until then.
	Public    string `json:"public"`
	FunnelURL string `json:"funnel_url"`
}

var (
	mu        sync.Mutex
	public    string
	funnelURL string
)

func statusOf(st *ipnstate.Status) status {
	s := status{State: st.BackendState, AuthURL: st.AuthURL}
	if st.Self != nil {
		s.Host = strings.TrimSuffix(st.Self.DNSName, ".")
	}
	return s
}

func writeJSON(path string, v any) error {
	b, err := json.Marshal(v)
	if err != nil {
		return err
	}
	tmp := path + ".tmp"
	if err := os.WriteFile(tmp, b, 0o600); err != nil {
		return err
	}
	return os.Rename(tmp, path)
}

func publicProxy(target string) *httputil.ReverseProxy {
	u := &url.URL{Scheme: "http", Host: target}
	return &httputil.ReverseProxy{
		Rewrite: func(r *httputil.ProxyRequest) {
			r.SetURL(u)
			r.Out.Host = r.In.Host // so the bridge builds links with the public name
			r.Out.Header.Set(PublicHeader, "1")
		},
		FlushInterval: -1, // live status and video stream as they come
		// Don't tell strangers what's behind the link.
		ModifyResponse: func(r *http.Response) error { r.Header.Del("Server"); return nil },
	}
}

// httpsProxy serves the tailnet https address. Passkeys only work over https, so the bridge needs to know.
func httpsProxy(target string) *httputil.ReverseProxy {
	u := &url.URL{Scheme: "http", Host: target}
	return &httputil.ReverseProxy{
		Rewrite: func(r *httputil.ProxyRequest) {
			r.SetURL(u)
			r.Out.Host = r.In.Host
			r.Out.Header.Set("X-Forwarded-Proto", "https")
		},
		FlushInterval: -1,
	}
}

// serveHTTPS waits for the tailnet's HTTPS certificates, a one-time switch in Tailscale's DNS settings.
func serveHTTPS(srv *tsnet.Server, target string) {
	for ; ; time.Sleep(30 * time.Second) {
		if ln, err := srv.ListenTLS("tcp", ":443"); err == nil {
			log.Print(http.Serve(ln, httpsProxy(target)))
		}
	}
}

// serveFunnel keeps asking for the public link; it needs a one-time "allow" in Tailscale, which may come later.
func serveFunnel(srv *tsnet.Server, lc *local.Client, target string) {
	for ; ; time.Sleep(5 * time.Second) {
		ln, err := srv.ListenFunnel("tcp", ":443", tsnet.FunnelOnly())
		if err != nil {
			ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
			if info, qerr := lc.QueryFeature(ctx, "funnel"); qerr == nil {
				mu.Lock()
				funnelURL = info.URL
				mu.Unlock()
			}
			cancel()
			continue
		}
		mu.Lock()
		public, funnelURL = "https://"+strings.TrimSuffix(srv.CertDomains()[0], "."), ""
		mu.Unlock()
		log.Print(http.Serve(ln, publicProxy(target)))
		mu.Lock()
		public = ""
		mu.Unlock()
	}
}

func forward(c net.Conn, target string) {
	defer c.Close()
	up, err := net.Dial("tcp", target)
	if err != nil {
		return
	}
	defer up.Close()
	go io.Copy(up, c)
	io.Copy(c, up)
}

func main() {
	dir := flag.String("dir", "", "state folder; holds this computer's Tailscale key")
	port := flag.Int("port", 5000, "web UI port on this computer; Tailscale devices reach it on port 80")
	statusFile := flag.String("status", "", "where to keep the status as JSON")
	funnel := flag.Bool("funnel", false, "also serve a public https link for paired phones without the Tailscale app")
	flag.Parse()

	// Quiet: the sign-in link goes to the status file only; logging it every 5 s filled bridge.log with it.
	quiet := func(string, ...any) {}
	srv := &tsnet.Server{Dir: *dir, Hostname: "wyze-tunnel", Logf: quiet, UserLogf: quiet}
	// The bridge holds our stdin open; EOF means it's gone, and Windows doesn't end child processes for us.
	go func() { io.Copy(io.Discard, os.Stdin); srv.Close(); os.Exit(0) }()
	if err := srv.Start(); err != nil {
		log.Fatal(err)
	}
	lc, err := srv.LocalClient()
	if err != nil {
		log.Fatal(err)
	}
	ln, err := srv.Listen("tcp", ":80")
	if err != nil {
		log.Fatal(err)
	}
	go func() {
		for ; ; time.Sleep(2 * time.Second) {
			ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
			st, err := lc.StatusWithoutPeers(ctx)
			cancel()
			if err == nil && *statusFile != "" {
				s := statusOf(st)
				mu.Lock()
				s.Public, s.FunnelURL = public, funnelURL
				mu.Unlock()
				writeJSON(*statusFile, s)
			}
		}
	}()
	target := fmt.Sprintf("127.0.0.1:%d", *port)
	if *funnel {
		go serveFunnel(srv, lc, target)
	} else {
		go serveHTTPS(srv, target)
	}
	for {
		c, err := ln.Accept()
		if err != nil {
			log.Fatal(err)
		}
		go forward(c, target)
	}
}
