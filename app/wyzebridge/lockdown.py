"""Tailscale lockdown: the Tailscale only switch, and the Health page's check that the app is locked down."""

import os
import socket
import subprocess
import sys
import time
from ipaddress import ip_address, ip_network
from pathlib import Path

from wyzebridge import vault
from wyzebridge.config import TOKEN_PATH

# The Windows tray reads it too: with it, the web page listens on this PC only.
FLAG = Path(TOKEN_PATH, "tailscale-only")
TAILNET = ip_network("100.64.0.0/10"), ip_network("fd7a:115c:a1e0::/48")
WEB_PORT = int(os.getenv("FLASK_RUN_PORT", "5000"))
PORTS = {WEB_PORT: "web page", 8554: "RTSP", 8888: "HLS", 8889: "WebRTC", 1935: "RTMP", 8890: "SRT", 1984: "Apple Home"}


def tailscale_only() -> bool:
    return FLAG.exists()


def set_tailscale_only(on: bool) -> None:
    FLAG.touch() if on else FLAG.unlink(missing_ok=True)


def allowed(addr) -> bool:
    """Who may connect while Tailscale only is on: this PC (Wyze Tunnel's own Tailscale arrives from here) and the tailnet."""
    try:
        ip = ip_address(addr or "")
    except ValueError:
        return False
    if ip.version == 6 and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_loopback or any(ip in net for net in TAILNET)


def secret_files() -> list[Path]:
    names = ("wb_password", "wb_api", "wb_login", "lan_keys.json", "apps.json", "passkeys.json")
    return [*Path(TOKEN_PATH).glob("*.pickle"), *(Path(TOKEN_PATH, n) for n in names)]


def seal_secrets() -> None:
    vault.seal_existing(secret_files())


def lan_ip() -> str:
    """This PC's address on the home network. Connecting a UDP socket sends nothing."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            s.connect(("192.0.2.1", 9))
            ip = s.getsockname()[0]
        except OSError:
            return ""
    return "" if ip.startswith("127.") or ip == "0.0.0.0" else ip


def open_ports(ip: str) -> list[int]:
    """Which of our ports answer on the home network address. A port bound to 127.0.0.1 only won't."""
    found = []
    for port in PORTS:
        with socket.socket() as s:
            s.settimeout(0.3)
            if s.connect_ex((ip, port)) == 0:
                found.append(port)
    return found


_fw = (0.0, False)


def firewall_too_open() -> bool:
    """Windows: an inbound allow rule lets any address reach the web page or video server (Windows' own prompt makes those).
    Checked here, not only by the tray, so the card is right whichever tray version did the update."""
    global _fw
    if time.monotonic() - _fw[0] < 60:
        return _fw[1]
    progs = [p for p in (sys.executable, os.getenv("MEDIAMTX_BIN", "")) if p and "'" not in p]
    ps = "; ".join(
        f"Get-NetFirewallApplicationFilter -Program '{p}' -ErrorAction SilentlyContinue | Get-NetFirewallRule -ErrorAction SilentlyContinue | "
        "Where-Object { $_.Direction -eq 'Inbound' -and $_.Enabled -eq 'True' -and $_.Action -eq 'Allow' } | "
        "Get-NetFirewallAddressFilter | Where-Object { $_.RemoteAddress -contains 'Any' } | ForEach-Object { 'open' }"
        for p in progs
    )
    try:
        out = subprocess.run(["powershell.exe", "-NoProfile", "-Command", ps], capture_output=True, text=True, timeout=30,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
    except (OSError, subprocess.SubprocessError):
        out = ""
    _fw = (time.monotonic(), "open" in out)
    return _fw[1]


def open_readers(users) -> list[dict]:
    """Stream server logins that let anyone watch: no password and not limited to this PC."""
    local = {"127.0.0.1", "::1"}
    return [
        u for u in users or []
        if not u.get("pass") and not (u.get("ips") and set(u["ips"]) <= local)
        and any(p.get("action") == "read" for p in u.get("permissions", []))
    ]


def checks(public_link: str, login_on: bool, stream_users) -> list[dict]:
    """Each line of the Lockdown card: ok, a title, and what to do when it isn't ok."""
    strict = tailscale_only()
    ip = lan_ip()
    ports = open_ports(ip) if ip else []
    names = ", ".join(PORTS[p] for p in ports)
    if strict:
        reach = {"ok": not ports, "title": "Only this PC and Tailscale can connect",
                 "fix": f"Still answering your home network ({names}) until Wyze Tunnel restarts."}
    else:
        reach = {"ok": 8890 not in ports, "title": "Only this PC, your home network and Tailscale can connect",
                 "fix": "The unused SRT video port is open. Restart Wyze Tunnel to close it."}
    rows = [reach]
    if os.name == "nt" and (update := os.getenv("WT_UPDATE_FILE")):
        ok = not Path(update).with_name("firewall-blocked").exists() and not firewall_too_open()
        rows.append({"ok": ok, "title": "Windows Firewall only lets in your home network", "action": "firewall",
                     "fix": "Not private yet: Windows Firewall lets any address reach the web page or video. "
                            "Click Fix it (or Fix phone and camera access in the tray menu), then Yes on the Windows prompt."})
    rows.append({"ok": not public_link, "title": "No public links or router ports",
                 "fix": "A public Tailscale Funnel link is still live. Restart Wyze Tunnel to close it."})
    rows.append({"ok": login_on and not open_readers(stream_users), "title": "Every video link needs a login",
                 "fix": "Login is turned off (WB_AUTH=False). Remove that setting so video needs a password." if not login_on
                 else "A stream login without a password lets anyone watch. Remove it from STREAM_AUTH."})
    if vault.enabled():
        files = [f for f in secret_files() if f.is_file()]
        rows.append({"ok": all(vault.sealed(f) for f in files), "title": "Saved sign-ins are encrypted by Windows",
                     "fix": "Some saved sign-ins are still plain files. Restart Wyze Tunnel to encrypt them."})
    return rows
