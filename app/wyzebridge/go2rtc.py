"""Optional go2rtc sidecar: reaches cameras without the Wyze SDK.

Two kinds of source, both pulled by a small ffmpeg relay into MediaMTX:
- "lan": go2rtc's native Wyze P2P client (wyze://), for cameras on the same network
  with DTLS firmware.
- "webrtc": Wyze's cloud WebRTC (Kinesis), using this app's /signaling endpoint.
"""

import ctypes
import os
import signal
import socket
import sys
from pathlib import Path
from subprocess import DEVNULL, PIPE, Popen
from typing import Optional
from urllib.parse import urlencode

import yaml
from wyzebridge.bridge_utils import env_bool
from wyzebridge.logging import logger
from wyzebridge.mtx_server import kill_orphan
from wyzecam.api_models import WyzeCamera

GO2RTC_BIN = os.getenv("GO2RTC_BIN", "/app/go2rtc")
GO2RTC_CONFIG = os.getenv("GO2RTC_CONFIG", "/tmp/go2rtc.yaml")
TUTK_LIB = os.getenv("TUTK_LIB", "/usr/local/lib/libIOTCAPIs_ALL.so")
RTSP_PORT = 8556
API_PORT = 1984
SOURCES = ("tutk", "lan", "webrtc")
# Consecutive failed connects before trying the camera's next source.
FALLBACK_AFTER = env_bool("SOURCE_FALLBACK_AFTER", "5", style="int")


def go2rtc_available() -> bool:
    return os.access(GO2RTC_BIN, os.X_OK)


def tutk_available() -> bool:
    return Path(TUTK_LIB).exists()


def local_ipv4s() -> set[str]:
    ips = set()
    try:
        ips.update(socket.gethostbyname_ex(socket.gethostname())[2])
    except OSError:
        pass
    try:  # the address of the default route; nothing is sent
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            ips.add(s.getsockname()[0])
    except OSError:
        pass
    return ips


def on_local_network(ip: str) -> bool:
    """Whether go2rtc's LAN discovery can reach this camera."""
    # shortcut: compares /16, since routers use /24 to /22 and Python can't read the mask on Windows.
    # A second site on the same /16 just tries LAN first, as before, then falls back to webrtc.
    nets = {a.rsplit(".", 2)[0] for a in local_ipv4s() if not a.startswith(("169.254.", "127."))}
    return ip.rsplit(".", 2)[0] in nets


def source_chain(cam: WyzeCamera) -> list[str]:
    """Sources to try for this camera, in order.

    Override with SOURCE_<CAM_NAME>, SOURCE_<MODEL> or SOURCE, e.g. "webrtc" or "lan,tutk".
    """
    uri = cam.name_uri.upper()
    override = env_bool(f"SOURCE_{uri}") or env_bool(f"SOURCE_{cam.product_model}") or env_bool("SOURCE")
    if override and override != "auto":
        chain = [s for s in override.replace(" ", "").split(",") if s in SOURCES]
        if not go2rtc_available():
            chain = [s for s in chain if s == "tutk"]
        return chain

    chain = []
    tutk = tutk_available()
    if tutk and not cam.is_gwell:
        chain.append("tutk")
    if go2rtc_available():
        # Without the Wyze SDK, a camera at another site would fail LAN discovery before reaching the cloud stream.
        reachable = tutk or on_local_network(cam.ip or "")
        if cam.ip and cam.dtls and not cam.parent_dtls and not cam.is_gwell and reachable:
            chain.append("lan")
        # shortcut: unverified whether Gwell cameras (OG) answer Wyze's Kinesis WebRTC; if not, port the Gwell P2P client.
        if cam.webrtc_support:
            chain.append("webrtc")
    return chain


def lan_url(cam: WyzeCamera, substream: bool = False) -> str:
    query = {
        "uid": cam.p2p_id,
        "enr": cam.enr,
        "mac": cam.mac,
        "model": cam.product_model,
        "subtype": "sd" if substream else "hd",
        "dtls": "true",
    }
    return f"wyze://{cam.ip}?{urlencode(query)}"


def webrtc_url(cam_name: str, api_key: Optional[str]) -> str:
    query = "kvs&relay" + (f"&{urlencode({'api': api_key})}" if api_key else "")
    port = os.getenv("FLASK_RUN_PORT", "5000")
    return f"webrtc:http://127.0.0.1:{port}/signaling/{cam_name}?{query}#format=wyze"


def relay_cmd(src: str, uri: str) -> list[str]:
    """ffmpeg that copies a go2rtc stream into MediaMTX, reporting progress on stdout."""
    return (
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin"]
        + ["-rtsp_transport", "tcp", "-timeout", "15000000"]
        + ["-i", f"rtsp://127.0.0.1:{RTSP_PORT}/{src}"]
        + ["-map", "0", "-c", "copy", "-progress", "pipe:1", "-nostats"]
        + ["-f", "rtsp", "-rtsp_transport", "tcp", f"rtsp://127.0.0.1:8554/{uri}"]
    )


def _die_with_parent():
    # Linux only: stop ffmpeg if the relay process is killed.
    ctypes.CDLL("libc.so.6", use_errno=True).prctl(1, signal.SIGTERM)


def relay(src: str, uri: str, on_frames, should_stop) -> None:
    """Run the relay until it ends or should_stop(); call on_frames() once video flows."""
    flowing = False
    with Popen(relay_cmd(src, uri), stdout=PIPE, stdin=DEVNULL, text=True, preexec_fn=_die_with_parent if sys.platform == "linux" else None) as ff:
        for line in iter(ff.stdout.readline, ""):  # readline: don't wait for a full buffer
            # With stream copy, ffmpeg reports no frame count, only how much time it has written.
            if not flowing and line.startswith("out_time_us=") and line.strip()[12:] not in ("0", "N/A"):
                flowing = True
                on_frames()
            if should_stop():
                ff.terminate()
                break


class Go2rtcServer:
    __slots__ = "sub_process", "streams"

    def __init__(self) -> None:
        self.sub_process: Optional[Popen] = None
        self.streams: dict[str, str] = {}

    def add(self, name: str, url: str) -> str:
        self.streams[name] = url
        return name

    def config(self) -> dict:
        return {
            "api": {"listen": f"127.0.0.1:{API_PORT}"},
            "rtsp": {"listen": f"127.0.0.1:{RTSP_PORT}"},
            "webrtc": {"listen": ""},
            "srtp": {"listen": ""},
            "log": {"level": env_bool("GO2RTC_LOGLEVEL", "warn")},
            "streams": self.streams,
        }

    def start(self) -> None:
        if self.sub_process or not self.streams or not go2rtc_available():
            return
        with open(GO2RTC_CONFIG, "w") as f:
            yaml.safe_dump(self.config(), f)
        kill_orphan(GO2RTC_BIN, RTSP_PORT)
        logger.info(f"[GO2RTC] starting go2rtc for {len(self.streams)} stream(s)")
        self.sub_process = Popen([GO2RTC_BIN, "-config", GO2RTC_CONFIG])

    def stop(self) -> None:
        if self.sub_process and self.sub_process.poll() is None:
            logger.info("[GO2RTC] Stopping go2rtc...")
            self.sub_process.terminate()
            try:
                self.sub_process.wait(5)
            except Exception:
                self.sub_process.kill()
        self.sub_process = None

    def health_check(self) -> None:
        if self.sub_process and self.sub_process.poll() is not None:
            logger.error(f"[GO2RTC] go2rtc exited with code {self.sub_process.returncode}, restarting")
            self.sub_process = None
            self.start()
