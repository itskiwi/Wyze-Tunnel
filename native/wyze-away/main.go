// Command wyze-away puts the web UI on the user's own private Tailscale network with embedded Tailscale (tsnet),
// so a phone with the Tailscale app can open it from anywhere. No router ports are opened and nothing is public.
package main

import (
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"log"
	"net"
	"os"
	"strings"
	"time"

	"tailscale.com/ipn/ipnstate"
	"tailscale.com/tsnet"
)

type status struct {
	State   string `json:"state"`
	AuthURL string `json:"auth_url"`
	Host    string `json:"host"`
}

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
	statusFile := flag.String("status", "", "where to keep {state, auth_url, host} as JSON")
	flag.Parse()

	srv := &tsnet.Server{Dir: *dir, Hostname: "wyze-tunnel", Logf: func(string, ...any) {}}
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
				writeJSON(*statusFile, statusOf(st))
			}
		}
	}()
	target := fmt.Sprintf("127.0.0.1:%d", *port)
	for {
		c, err := ln.Accept()
		if err != nil {
			log.Fatal(err)
		}
		go forward(c, target)
	}
}
