// wyze-ioctl sends one TUTK command ("K" message) to a Wyze camera on the local network and prints the reply.
// It reuses go2rtc's pure-Go Wyze client (MIT), so camera controls work on Windows without the Linux-only Wyze SDK.
//
// Usage: wyze-ioctl <wyze:// url> <request as hex> <reply code, 0 for none>
// Prints the reply's HL frame as hex. The request is a full HL frame, as app/wyzecam/tutk/tutk_protocol.py encodes it.
package main

import (
	"encoding/hex"
	"fmt"
	"os"
	"reflect"
	"strconv"
	"time"
	"unsafe"

	"github.com/AlexxIT/go2rtc/pkg/tutk"
	"github.com/AlexxIT/go2rtc/pkg/tutk/dtls"
	"github.com/AlexxIT/go2rtc/pkg/wyze"
)

func main() {
	if len(os.Args) != 4 {
		fail(fmt.Errorf("usage: wyze-ioctl <wyze url> <hex request> <reply code>"))
	}
	req, err := hex.DecodeString(os.Args[2])
	if err != nil {
		fail(err)
	}
	code, err := strconv.ParseUint(os.Args[3], 10, 16)
	if err != nil {
		fail(err)
	}
	client, err := wyze.Dial(os.Args[1])
	if err != nil {
		fail(err)
	}
	defer client.Close()
	conn := connOf(client)

	if code == 0 {
		fail(conn.WriteIOCtrl(req))
		time.Sleep(300 * time.Millisecond) // let the camera take it before the session closes
		return
	}
	match := func(data []byte) bool {
		cmd, _, ok := tutk.ParseHL(findHL(data))
		return ok && uint64(cmd) == code
	}
	data, err := conn.WriteAndWaitIOCtrl(req, match, 5*time.Second)
	fail(err)
	hl := findHL(data)
	_, payload, _ := tutk.ParseHL(hl)
	fmt.Println(hex.EncodeToString(hl[:16+len(payload)])) // without any padding after the payload
}

// connOf reads the client's unexported session; go2rtc has no public way to send other commands.
func connOf(c *wyze.Client) *dtls.DTLSConn {
	f := reflect.ValueOf(c).Elem().FieldByName("conn")
	return reflect.NewAt(f.Type(), unsafe.Pointer(f.UnsafeAddr())).Elem().Interface().(*dtls.DTLSConn)
}

// findHL mirrors go2rtc's (unexported) Client.extractHL.
func findHL(data []byte) []byte {
	if hl := tutk.FindHL(data, 32); hl != nil {
		return hl
	}
	if len(data) >= 36 && data[16] == 0 {
		return tutk.FindHL(data, 36)
	}
	return nil
}

func fail(err error) {
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}
