import socket
import sys
from os import getenv
from pathlib import Path
from subprocess import DEVNULL, Popen, run
from time import sleep
from typing import Optional

import requests
import yaml
from wyzebridge.bridge_utils import env_bool
from wyzebridge.logging import logger
from wyzebridge.mtx_event import event_cmd

# Not MTX_*: MediaMTX treats those env vars as its own config overrides.
MTX_BIN = getenv("MEDIAMTX_BIN", "/app/mediamtx")
MTX_CONFIG = getenv("MEDIAMTX_CONFIG", "/app/mediamtx.yml")

RECORD_LENGTH = env_bool("RECORD_LENGTH", "60s")
RECORD_KEEP = env_bool("RECORD_KEEP", "0s")
rec_file = env_bool("RECORD_FILE_NAME", style="original").strip("/")
rec_path = env_bool("RECORD_PATH", "record/%path/%Y-%m-%d_%H-%M-%S-%f", style="original")
PLAYBACK_ADDR = "127.0.0.1:9996"
API_ADDR = "127.0.0.1:9997"  # local only; feeds the data use meter
RECORD_PATH = f"{Path('/') / Path(rec_path) / Path(rec_file)}".removesuffix(".mp4")
# MediaMTX refuses to start with playback on unless the file name has every one of these.
PLAYBACK_ENABLED = all(f in RECORD_PATH for f in ("%Y", "%m", "%d", "%H", "%M", "%S", "%f"))


class MtxInterface:
    __slots__ = "data", "_modified"

    def __init__(self):
        self.data = {}
        self._modified = False

    def __enter__(self):
        self._load_config()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        if self._modified:
            self._save_config()

    def _load_config(self):
        with open(MTX_CONFIG, "r") as f:
            self.data = yaml.safe_load(f) or {}

    def _save_config(self):
        with open(MTX_CONFIG, "w") as f:
            yaml.safe_dump(self.data, f)

    def get(self, path: str):
        keys = path.split(".")
        current = self.data
        for key in keys:
            if current is None:
                return None
            current = current.get(key)
        return current

    def set(self, path: str, value):
        keys = path.split(".")
        current = self.data
        for key in keys[:-1]:
            current = current.setdefault(key, {})
        current[keys[-1]] = value
        self._modified = True

    def add(self, path: str, value):
        if not isinstance(value, list):
            value = [value]
        current = self.data.get(path)
        if isinstance(current, list):
            current.extend([item for item in value if item not in current])
        else:
            self.data[path] = value
        self._modified = True


class MtxServer:
    """Setup and interact with the backend mediamtx."""

    __slots__ = "sub_process"

    def __init__(self) -> None:
        self.sub_process: Optional[Popen] = None
        self._setup_path_defaults()

    def _setup_path_defaults(self):
        record_path = RECORD_PATH.format(cam_name="%path", CAM_NAME="%path")

        with MtxInterface() as mtx:
            mtx.set("paths", {})
            for event in {"Read", "Unread", "Ready", "NotReady"}:
                mtx.set(f"pathDefaults.runOn{event}", event_cmd(event))
            mtx.set("pathDefaults.runOnDemandStartTimeout", "30s")
            mtx.set("pathDefaults.runOnDemandCloseAfter", "60s")
            mtx.set("pathDefaults.recordPath", record_path)
            mtx.set("pathDefaults.recordSegmentDuration", RECORD_LENGTH)
            mtx.set("pathDefaults.recordDeleteAfter", RECORD_KEEP)
            # Local-only playback server; the web UI proxies it behind its own login.
            mtx.set("playback", PLAYBACK_ENABLED)
            mtx.set("playbackAddress", PLAYBACK_ADDR)
            mtx.set("api", True)
            mtx.set("apiAddress", API_ADDR)

    def setup_auth(self, api: Optional[str], stream: Optional[str]):
        publisher = [
            {
                "ips": ["127.0.0.1"],
                "permissions": [
                    {"action": "read"},
                    {"action": "publish"},
                    {"action": "playback"},
                    {"action": "api"},
                ],
            }
        ]
        with MtxInterface() as mtx:
            mtx.set("authInternalUsers", publisher)
            if api or not stream:
                client: dict = {"permissions": [{"action": "read"}]}
                if api:
                    client.update({"user": "wb", "pass": api})
                mtx.add("authInternalUsers", client)
            if stream:
                logger.info("[MTX] Custom stream auth enabled")
                for client in parse_auth(stream):
                    mtx.add("authInternalUsers", client)

    def add_path(self, uri: str, on_demand: bool = True):
        with MtxInterface() as mtx:
            mtx.set(f"paths.{uri}", {})  # drop settings from before a restart, like recording that was switched off
            if on_demand:
                mtx.set(f"paths.{uri}.runOnDemand", event_cmd("start"))
                mtx.set(f"paths.{uri}.runOnUnDemand", event_cmd("stop"))

    def add_source(self, uri: str, value: str):
        with MtxInterface() as mtx:
            mtx.set(f"paths.{uri}.source", value)

    def record(self, uri: str, keep: Optional[str] = None):
        record_path = RECORD_PATH.replace("%path", uri).format(
            cam_name=uri.lower(), CAM_NAME=uri.upper()
        )

        logger.info(f"[MTX] 📹 Will record {RECORD_LENGTH} clips to {record_path}.mp4")
        with MtxInterface() as mtx:
            mtx.set(f"paths.{uri}.record", True)
            mtx.set(f"paths.{uri}.recordPath", record_path)
            if keep:
                mtx.set(f"paths.{uri}.recordDeleteAfter", keep)

    def start(self):
        if self.sub_process:
            return
        kill_orphan(MTX_BIN, 8554)
        logger.info(f"[MTX] starting MediaMTX {getenv('MTX_TAG')}")
        self.sub_process = Popen([MTX_BIN, MTX_CONFIG])

    def stop(self):
        if not self.sub_process:
            return
        if self.sub_process.poll() is None:
            logger.info("[MTX] Stopping MediaMTX...")
            self.sub_process.kill()
            self.sub_process.communicate()
        self.sub_process = None

    def restart(self):
        self.stop()
        self.start()

    def health_check(self):
        if self.sub_process and self.sub_process.poll() is not None:
            logger.error(f"[MediaMTX] Process exited with {self.sub_process.poll()}")
            self.restart()

    def setup_webrtc(self, bridge_ip: Optional[str]):
        if not bridge_ip:
            logger.warning("SET WB_IP to allow WEBRTC connections.")
            return
        ips = bridge_ip.split(",")
        logger.debug(f"Using {' and '.join(ips)} for webrtc")
        with MtxInterface() as mtx:
            mtx.add("webrtcAdditionalHosts", ips)

    def setup_llhls(self, token_path: str = "/tokens/", hass: bool = False):
        logger.info("[MTX] Configuring LL-HLS")
        with MtxInterface() as mtx:
            mtx.set("hlsVariant", "lowLatency")
            mtx.set("hlsEncryption", True)
            if env_bool("mtx_hlsServerKey"):
                return

            key = "/ssl/privkey.pem"
            cert = "/ssl/fullchain.pem"
            if hass and Path(key).is_file() and Path(cert).is_file():
                logger.info(
                    "[MTX] 🔐 Using existing SSL certificate from Home Assistant"
                )
                mtx.set("hlsServerKey", key)
                mtx.set("hlsServerCert", cert)
                return

            cert_path = f"{token_path}hls_server"
            generate_certificates(cert_path)
            mtx.set("hlsServerKey", f"{cert_path}.key")
            mtx.set("hlsServerCert", f"{cert_path}.crt")


def path_bytes() -> dict:
    """Bytes from and to each camera since its stream last started, from MediaMTX's local API."""
    try:
        items = requests.get(f"http://{API_ADDR}/v3/paths/list", timeout=3).json()["items"]
    except (requests.RequestException, ValueError, KeyError):
        return {}
    return {p["name"]: {"in": p.get("bytesReceived", 0), "out": p.get("bytesSent", 0)} for p in items}


def mtx_version() -> str:
    try:
        with open("/MTX_TAG", "r") as tag:
            return tag.read().strip()
    except FileNotFoundError:
        return ""


def generate_certificates(cert_path):
    if not Path(f"{cert_path}.key").is_file():
        logger.info("[MTX] 🔐 Generating key for LL-HLS")
        Popen(
            ["openssl", "genrsa", "-out", f"{cert_path}.key", "2048"],
            stdout=DEVNULL,
            stderr=DEVNULL,
        ).wait()
    if not Path(f"{cert_path}.crt").is_file():
        logger.info("[MTX] 🔏 Generating certificate for LL-HLS")
        dns = getenv("SUBJECT_ALT_NAME")
        Popen(
            ["openssl", "req", "-new", "-x509", "-sha256"]
            + ["-key", f"{cert_path}.key"]
            + ["-subj", "/C=US/ST=WA/L=Kirkland/O=WYZE BRIDGE/CN=wyze-bridge"]
            + (["-addext", f"subjectAltName = DNS:{dns}"] if dns else [])
            + ["-out", f"{cert_path}.crt"]
            + ["-days", "3650"],
            stdout=DEVNULL,
            stderr=DEVNULL,
        ).wait()


def parse_auth(auth: str) -> list[dict[str, str]]:
    entries = []
    for entry in auth.split("|"):
        creds, *endpoints = entry.split("@")
        if ":" not in creds:
            continue
        user, password, *ips = creds.split(":", 2)
        if ips:
            ips = ips[0].split(",")
        data = {"user": user or "any", "pass": password, "ips": ips, "permissions": []}
        if endpoints:
            paths = []
            for endpoint in endpoints[0].split(","):
                paths.append(endpoint)
                data["permissions"].append({"action": "read", "path": endpoint})
        else:
            paths = "all"
            data["permissions"].append({"action": "read"})
        logger.info(f"[MTX] Auth [{data['user']}:{data['pass'][:1]}***] {paths=}")
        entries.append(data)
    return entries


def port_in_use(port: int) -> bool:
    with socket.socket() as sock:
        return sock.connect_ex(("127.0.0.1", port)) == 0


def kill_orphan(binary: str, port: int) -> None:
    """Stop a copy of `binary` left over from a crash that still holds `port`."""
    if not port_in_use(port):
        return
    name = Path(binary).name
    logger.warning(f"Port {port} is busy. Stopping any leftover {name}...")
    cmd = ["taskkill", "/F", "/IM", name] if sys.platform == "win32" else ["pkill", "-x", name]
    try:
        run(cmd, stdout=DEVNULL, stderr=DEVNULL, check=False)
    except OSError as ex:
        logger.error(f"Could not stop {name}: {ex}")
    for _ in range(20):
        if not port_in_use(port):
            return
        sleep(0.1)
    logger.error(f"Port {port} is still in use by another program.")
