import contextlib
import json
import multiprocessing as mp
import socket
import zoneinfo
from collections import namedtuple
from ctypes import c_int
from os import getenv
from dataclasses import dataclass
from datetime import datetime
from enum import IntEnum
from queue import Empty, Full
from subprocess import PIPE, Popen
from threading import Lock, Thread
from time import sleep, time
from typing import Optional

from wyzebridge.bridge_utils import env_bool, env_cam
from wyzebridge.config import CHRONIC_FAILURES, COOLDOWN, COOLDOWN_MAX, MQTT_TOPIC, TOKEN_PATH
from wyzebridge.auth import WbAuth
from wyzebridge.ffmpeg import get_ffmpeg_cmd
from wyzebridge.go2rtc import FALLBACK_AFTER, Go2rtcServer, go2rtc_available, LanSession, lan_controls, lan_url, relay, source_chain, webrtc_url
from wyzebridge.logging import logger
from wyzebridge.mqtt import publish_discovery, publish_messages, update_mqtt_state
from wyzebridge.webhooks import send_webhook
from wyzebridge.wyze_api import WyzeApi, display_name
from wyzebridge.wyze_commands import GET_CMDS, PARAMS, SET_CMDS
from wyzebridge.wyze_control import camera_control, handle_cmd
from wyzecam import TutkError, WyzeAccount, WyzeCamera, WyzeIOTC, WyzeIOTCSession

NET_MODE = {0: "P2P", 1: "RELAY", 2: "LAN"}
# Commands the Wyze cloud can do when there's no direct (TUTK) connection, and the property each reads.
CLOUD_PROPS = {"alarm": "P1049", "floodlight": "P1056", "spotlight": "P1056", "motion_detection": "P1047", "sound_detection": "P1048"}
# How the web UI names each mode: on the home network, direct over the internet, or through Wyze's relay servers.
NET_MODE_LABEL = {0: "internet", 1: "relay", 2: "local"}
# Connection choices from the web UI, saved per camera; they override NET_MODE env vars.
NET_MODE_CHOICES = {"auto": "any", "no_relay": "p2p", "local": "lan"}
NET_MODES_FILE = TOKEN_PATH + "net_modes.json"


QUALITY_CHOICES = {"hd": "hd180", "sd": "sd30"}
QUALITY_FILE = TOKEN_PATH + "quality.json"


def load_qualities() -> dict[str, str]:
    """HD/SD choices from the web UI, by stream. They override QUALITY env vars."""
    try:
        with open(QUALITY_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return {}


def load_net_modes() -> dict[str, str]:
    try:
        with open(NET_MODES_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return {}


StreamTuple = namedtuple("stream", ["user", "camera", "options"])
QueueTuple = namedtuple("queue", ["cam_resp", "cam_cmd"])


class StreamStatus(IntEnum):
    OFFLINE = -90
    FAILED = -2
    STOPPING = -1
    DISABLED = 0
    STOPPED = 1
    CONNECTING = 2
    CONNECTED = 3


@dataclass(slots=True)
class WyzeStreamOptions:
    quality: str = "hd180"
    audio: bool = False
    record: bool = False
    reconnect: bool = False
    substream: bool = False
    frame_size: int = 0
    bitrate: int = 120

    def __post_init__(self):
        if self.record:
            self.reconnect = True

    def update_quality(self, hq_frame_size: int = 0) -> None:
        quality = (self.quality or "hd").lower().ljust(3, "0")
        bit = int(quality[2:] or "0")

        self.quality = quality
        self.bitrate = bit or 180
        self.frame_size = 1 if "sd" in quality else hq_frame_size


class WyzeStream:
    user: WyzeAccount
    api: WyzeApi
    go2rtc: Go2rtcServer = Go2rtcServer()
    __slots__ = (
        "camera",
        "options",
        "start_time",
        "_state",
        "_net_mode",
        "uri",
        "cam_resp",
        "cam_cmd",
        "process",
        "rtsp_fw_enabled",
        "_motion",
        "motion_ts",
        "failures",
        "sources",
        "source_idx",
        "stopped_at",
    )

    def __init__(self, camera: WyzeCamera, options: WyzeStreamOptions) -> None:
        self.camera: WyzeCamera = camera
        self.options: WyzeStreamOptions = options
        self.uri = camera.name_uri + ("-sub" if options.substream else "")

        self.rtsp_fw_enabled: bool = False
        self.start_time: float = 0
        self.cam_resp: mp.Queue
        self.cam_cmd: mp.Queue
        self.process: Optional[mp.Process] = None
        self._state: c_int = mp.Value("i", StreamStatus.STOPPED, lock=False)
        self._net_mode: c_int = mp.Value("i", -1, lock=False)
        self._motion: bool = False
        self.motion_ts: float = 0
        self.failures: int = 0
        self.sources: list[str] = []
        self.source_idx: int = 0
        self.stopped_at: float = 0
        self.setup()

    def setup(self):
        self.sources = source_chain(self.camera)
        if self.options.substream:
            self.sources = [s for s in self.sources if s != "webrtc"]
        if "lan" in self.sources:
            self.go2rtc.add(f"{self.uri}_lan", lan_url(self.camera, self.options.substream))
        if "webrtc" in self.sources:
            self.go2rtc.add(f"{self.uri}_webrtc", webrtc_url(self.camera.name_uri, WbAuth.api))
        if self.sources and not self.options.substream:
            self.go2rtc.names[self.uri] = self.camera.nickname
        if not self.sources:
            if self.camera.webrtc_support and not go2rtc_available():
                logger.warning(
                    f"[{self.camera.product_model}] {self.camera.nickname} needs go2rtc, which isn't installed. "
                    "Re-run the installer or update Wyze Tunnel to add it."
                )
            else:
                logger.info(f"[{self.camera.product_model}] {self.camera.nickname} not supported")
            self.state = StreamStatus.DISABLED
        elif self.sources != ["tutk"]:
            logger.info(f"{self.camera.nickname} will connect with: {', '.join(self.sources)}")
        if self.options.substream and not self.camera.can_substream:
            logger.error(f"{self.camera.nickname} may not support multiple streams!!")
            # self.state = StreamStatus.DISABLED

        if saved := load_qualities().get(self.uri):
            self.options.quality = saved
        self.options.update_quality(self.hq_frame_size)
        publish_discovery(self.uri, self.camera)

    @property
    def state(self) -> int:
        return self._state.value

    @state.setter
    def state(self, value) -> None:
        self._state.value = value.value if isinstance(value, StreamStatus) else value
        update_mqtt_state(self.uri, self.status())

    @property
    def net_mode(self) -> str:
        """local, internet or relay while connected, else ''."""
        return NET_MODE_LABEL.get(self._net_mode.value, "") if self.connected else ""

    @property
    def motion(self) -> bool:
        state = time() - self.motion_ts < 20
        if self._motion and not state:
            self._motion = state
            publish_messages([(f"{MQTT_TOPIC}/{self.uri}/motion", 2, 0, True)])
        return state

    @motion.setter
    def motion(self, value: float):
        self._motion = True
        self.motion_ts = value
        publish_messages(
            [
                (f"{MQTT_TOPIC}/{self.uri}/motion", 1, 0, True),
                (f"{MQTT_TOPIC}/{self.uri}/motion_ts", value, 0, True),
            ]
        )

    @property
    def connected(self) -> bool:
        return self.state == StreamStatus.CONNECTED

    @property
    def busy(self) -> bool:
        """A Cam OG takes two viewers at once, so failing to connect usually means Wyze app viewers hold them."""
        return self.camera.is_gwell and self.failures > 0 and not self.connected

    @property
    def enabled(self) -> bool:
        return self.state != StreamStatus.DISABLED

    @property
    def reason(self) -> str | None:
        """Why this camera isn't working right, in plain words, or None."""
        wifi = ((self.camera.camera_info or {}).get("basicInfo") or {}).get("wifidb")
        api = getattr(self, "api", None)
        return status_reason(
            self.state, self.failures, self.connected, wifi,
            bool(api and api.login_error), not self.connected or internet_up(),
        )

    def start(self) -> bool:
        # The monitor, MediaMTX hooks and the web UI all call this; two at once would open two camera sessions.
        with _start_lock:
            if self.health_check(False) != StreamStatus.STOPPED:
                return False
            self.state = StreamStatus.CONNECTING
        return self._start()

    def _start(self) -> bool:
        logger.info(
            f"🎉 Connecting to WyzeCam {self.camera.model_name} - {self.camera.nickname} on {self.camera.ip}"
        )
        # A Cam OG takes two viewers; give it time to free our last session so the Wyze app keeps its slot.
        wait = max(0, self.stopped_at + SLOT_GAP - time()) if self.camera.is_gwell else 0
        self.start_time = time() + wait
        self.cam_resp = mp.Queue(1)
        self.cam_cmd = mp.Queue(1)
        if self.source != "tutk":
            logger.info(f"[{self.source.upper()}] {self.camera.nickname} via go2rtc" + (f" in {wait:.0f}s" if wait else ""))
            self.process = mp.Process(
                target=start_relay_stream,
                args=(self.uri, f"{self.uri}_{self.source}", self._state, wait),
                name=self.uri,
            )
            self.process.start()
            return True
        self.process = mp.Process(
            target=start_tutk_stream,
            args=(
                self.uri,
                StreamTuple(self.user, self.camera, self.options),
                QueueTuple(self.cam_resp, self.cam_cmd),
                self._state,
                self._net_mode,
            ),
            name=self.uri,
        )
        self.process.start()
        return True

    def stop(self) -> bool:
        self._clear_mp_queue()
        self.start_time = 0
        self._net_mode.value = -1
        self.state = StreamStatus.STOPPING
        if self.process and self.process.is_alive():
            with contextlib.suppress(AttributeError):
                self.process.join(1)
            with contextlib.suppress(AttributeError):
                if self.process.is_alive():
                    self.process.kill()
                    self.process.join(1)

        self.process = None
        self.stopped_at = time()
        self.state = StreamStatus.STOPPED
        return True

    def enable(self) -> bool:
        if self.state == StreamStatus.DISABLED:
            logger.info(f"Enabling {self.uri}")
            self.state = StreamStatus.STOPPED
        return self.state > StreamStatus.DISABLED

    def disable(self) -> bool:
        if self.state == StreamStatus.DISABLED:
            return True
        logger.info(f"Disabling {self.uri}")
        if self.state != StreamStatus.STOPPED:
            self.stop()
        self.state = StreamStatus.DISABLED
        return True

    @property
    def source(self) -> str:
        return self.sources[self.source_idx] if self.sources else "tutk"

    def _failed(self) -> None:
        """Back off before the next try, and move to the next source after repeated failures."""
        self.failures += 1
        delay = backoff(self.failures)
        if len(self.sources) > 1 and self.failures % FALLBACK_AFTER == 0:
            old = self.source
            self.source_idx = (self.source_idx + 1) % len(self.sources)
            logger.warning(f"{self.camera.nickname}: {old} failed {self.failures}x, trying {self.source}")
            delay = 0
        self.start_time = time() + delay
        logger.info(f"{self.camera.nickname} failed {self.failures}x, retrying in {delay}s.")
        if self.failures == CHRONIC_FAILURES:
            auto_restart(self)

    def health_check(self, should_start: bool = True) -> int:
        self.motion
        if self.state == StreamStatus.OFFLINE:
            if env_bool("IGNORE_OFFLINE"):
                logger.info(f"🪦 {self.uri} is offline. WILL ignore.")
                self.disable()
                return self.state
            logger.info(f"👻 {self.camera.nickname} is offline.")
        if self.state in {-13, -19, -68}:
            self.refresh_camera()
        elif self.state < StreamStatus.DISABLED:
            state = self.state
            self.stop()
            if state < StreamStatus.STOPPING:
                self._failed()
        elif self.state == StreamStatus.CONNECTED:
            self.failures = 0
        elif (
            self.state == StreamStatus.STOPPED
            and self.options.reconnect
            and should_start
        ):
            self.start()
        elif self.state == StreamStatus.CONNECTING and is_timedout(self.start_time, 20):
            logger.warning(f"⏰ Timed out connecting to {self.camera.nickname}.")
            self.stop()
            self._failed()

        if should_start and self.camera.is_battery and self.state == 1:
            return 0
        return self.state if self.start_time < time() else 0

    def refresh_camera(self):
        self.stop()
        if not (cam := self.api.get_camera(self.camera.name_uri)):
            return False
        self.camera = cam
        return True

    def status(self) -> str:
        try:
            return StreamStatus(self._state.value).name.lower()
        except ValueError:
            return "error"

    def get_info(self, item: Optional[str] = None) -> dict:
        if item == "boa_info":
            return self.boa_info()
        data = {
            "name_uri": self.uri,
            "status": self.state,
            "net_mode": self.net_mode,
            "connected": self.connected,
            "enabled": self.enabled,
            "motion": self.motion,
            "motion_ts": self.motion_ts,
            "on_demand": not self.options.reconnect,
            "audio": self.options.audio,
            "record": self.options.record,
            "substream": self.options.substream,
            "model_name": self.camera.model_name,
            "is_2k": self.camera.is_2k,
            "rtsp_fw": self.camera.rtsp_fw,
            "rtsp_fw_enabled": self.rtsp_fw_enabled,
            "is_battery": self.camera.is_battery,
            "is_pan_cam": self.camera.is_pan_cam,
            "is_floodlight": self.camera.is_floodlight,
            "is_spotlight": self.camera.is_spotlight,
            "is_doorbell": self.camera.is_doorbell,
            "product_model": self.camera.product_model,
            "webrtc": self.camera.webrtc_support,
            "start_time": self.start_time,
            "quality": self.options.quality[:2],
            "req_frame_size": self.options.frame_size,
            "req_bitrate": self.options.bitrate,
            "failures": self.failures,
            "chronic": self.failures >= CHRONIC_FAILURES,
            "busy": self.busy,
            "reason": self.reason,
            "source": self.source,
            "sources": self.sources,
        }
        if self.connected and not self.camera.camera_info:
            self.update_cam_info()
        if self.camera.camera_info and "boa_info" in self.camera.camera_info:
            data["boa_url"] = f"http://{self.camera.ip}/cgi-bin/hello.cgi?name=/"
        return (
            data
            | self.camera.model_dump(exclude={"p2p_id", "enr", "parent_enr"})
            | {"nickname": display_name(self.camera)}
        )

    def update_cam_info(self) -> None:
        if not self.connected:
            return

        if (resp := self.send_cmd("caminfo")) and ("response" not in resp):
            self.camera.set_camera_info(resp)

    def boa_info(self) -> dict:
        self.update_cam_info()
        if not self.camera.camera_info:
            return {}
        return self.camera.camera_info.get("boa_info", {})

    @property
    def hq_frame_size(self) -> int:
        return 4 if self.camera.is_floodlight else 3 if self.camera.is_2k else 0

    def quality_control(self, payload: str) -> dict:
        """Switch this stream between HD and SD, save it, and reconnect with it."""
        if not payload:
            current = self.options.quality[:2]
            return {"status": "success", "response": current, "value": current}
        if payload not in QUALITY_CHOICES:
            return {"status": "error", "response": "Use hd or sd"}
        qualities = load_qualities()
        qualities[self.uri] = QUALITY_CHOICES[payload]
        with open(QUALITY_FILE, "w") as f:
            json.dump(qualities, f)
        self.options.quality = QUALITY_CHOICES[payload]
        self.options.update_quality(self.hq_frame_size)
        logger.info(f"[CONTROL] SET {self.uri} quality={payload}")
        if self.state >= StreamStatus.CONNECTING:
            self.stop()
            self.start()
        return {"status": "success", "response": payload, "value": payload}

    def net_mode_control(self, payload: str) -> dict:
        """Save how this camera may connect (auto, no_relay or local) and reconnect with it."""
        modes = load_net_modes()
        if not payload:
            choice = next((k for k, v in NET_MODE_CHOICES.items() if v == modes.get(self.uri)), "auto")
            return {"status": "success", "response": choice, "value": self.net_mode}
        if payload not in NET_MODE_CHOICES:
            return {"status": "error", "response": f"Use one of: {', '.join(NET_MODE_CHOICES)}"}
        modes[self.uri] = NET_MODE_CHOICES[payload]
        with open(NET_MODES_FILE, "w") as f:
            json.dump(modes, f)
        logger.info(f"[CONTROL] SET {self.uri} net_mode={payload}")
        if self.state >= StreamStatus.CONNECTING:
            self.stop()
            self.start()
        return {"status": "success", "response": payload, "value": payload}

    def state_control(self, payload) -> dict:
        if payload in {"start", "stop", "disable", "enable"}:
            logger.info(f"[CONTROL] SET {self.uri} state={payload}")
            response = getattr(self, payload)()
            return {
                "status": "success" if response else "error",
                "response": payload if response else self.status(),
                "value": payload,
            }
        logger.info(f"[CONTROL] GET {self.uri} state")
        return {"status": "success", "response": self.status()}

    def power_control(self, payload: str) -> dict:
        if payload not in {"on", "off", "restart"}:
            resp = self.api.get_device_info(self.camera, "P3")
            resp["value"] = "on" if resp["value"] == "1" else "off"
            return resp
        run_cmd = payload if payload == "restart" else f"power_{payload}"

        return dict(
            self.api.run_action(self.camera, run_cmd),
            value="on" if payload == "restart" else payload,
        )

    def notification_control(self, payload: str) -> dict:
        if payload not in {"on", "off", "1", "2", "true", "false"}:
            return self.api.get_device_info(self.camera, "P1")

        pvalue = "1" if payload in {"on", "1", "true"} else "2"
        resp = self.api.set_property(self.camera, "P1", pvalue)
        value = None if resp.get("status") == "error" else pvalue

        return dict(resp, value=value)

    def cloud_control(self, cmd: str, payload: str) -> dict:
        """Siren, lights and motion detection through the Wyze cloud, for cameras streamed by go2rtc (no TUTK commands).
        Endpoints and values as used by wyzeapy (GPL-3.0), reimplemented here."""
        pid = CLOUD_PROPS[cmd]
        if payload not in {"on", "off"}:
            resp = self.api.get_device_info(self.camera, pid)
            return dict(resp, value="on" if resp.get("value") == "1" else "off")
        on = payload == "on"
        if cmd == "alarm":
            resp = self.api.run_action(self.camera, f"siren_{payload}")
        elif cmd == "motion_detection":
            # Newer cams need both; older ones ignore P1001.
            resp = self.api.set_property(self.camera, "P1047", "1" if on else "0")
            self.api.set_property(self.camera, "P1001", "1" if on else "0")
        elif cmd == "sound_detection":
            # Same pairing as motion: P1048 is the alarm, P1002 its recording (the Wyze app sets both).
            resp = self.api.set_property(self.camera, "P1048", "1" if on else "0")
            self.api.set_property(self.camera, "P1002", "1" if on else "0")
        elif self.camera.product_model == "HL_BC":
            resp = self.api.run_action(self.camera, f"floodlight_{payload}")
        else:
            resp = self.api.set_property(self.camera, pid, "1" if on else "2")
        return dict(resp, value=None if resp.get("status") == "error" else payload)

    def tz_control(self, payload: str) -> dict:
        try:
            zone = zoneinfo.ZoneInfo(payload)
            offset = datetime.now(zone).utcoffset()
            assert offset is not None
        except (zoneinfo.ZoneInfoNotFoundError, AssertionError):
            return {"response": "invalid time zone"}

        return dict(
            self.api.set_device_info(self.camera, {"device_timezone_city": zone.key}),
            value=int(offset.total_seconds() / 3600),
        )

    def send_cmd(self, cmd: str, payload: str | list | dict = "") -> dict:
        if cmd in {"state", "start", "stop", "disable", "enable"}:
            return self.state_control(payload or cmd)

        if cmd == "device_info":
            return self.api.get_device_info(self.camera)
        if cmd == "device_setting":
            return self.api.get_device_info(self.camera, cmd="device_setting")

        if cmd == "battery":
            return self.api.get_device_info(self.camera, "P8")

        if cmd == "power":
            return self.power_control(str(payload).lower())

        if cmd == "net_mode":
            return self.net_mode_control(str(payload).lower())

        if cmd == "quality":
            return self.quality_control(str(payload).lower())

        if cmd == "notifications":
            return self.notification_control(str(payload).lower())

        if cmd in {"motion", "motion_ts"}:
            return {
                "status": "success",
                "response": {"motion": self.motion, "motion_ts": self.motion_ts},
                "value": self.motion if cmd == "motion" else self.motion_ts,
            }

        # Sound detection is a cloud setting on every camera; the rest go through the camera when it's local.
        if cmd in CLOUD_PROPS and (self.source != "tutk" or cmd == "sound_detection") and not env_bool("disable_control"):
            return self.cloud_control(cmd, str(payload).lower())

        if self.state < StreamStatus.STOPPED:
            return {"response": self.status()}

        if env_bool("disable_control"):
            return {"response": "control disabled"}

        if cmd == "time_zone" and payload and isinstance(payload, str):
            return self.tz_control(payload)

        if cmd == "bitrate" and isinstance(payload, (str, int)) and payload.isdigit():
            self.options.bitrate = int(payload)

        if cmd == "update_snapshot":
            return {"update_snapshot": True}

        if cmd == "cruise_point" and payload == "-":
            return {"status": "success", "value": "-"}

        if cmd not in GET_CMDS | SET_CMDS | PARAMS and cmd not in {"caminfo"}:
            return {"response": "invalid command"}

        if self.source != "tutk" and lan_controls(self.camera, self.sources):
            return handle_cmd(LanSession(self.camera), (cmd, payload))

        if self.source != "tutk":
            return {"response": f"Only the siren, lights and motion detection work for {self.camera.nickname} on this computer. Night vision and moving the camera need the camera on your home network"}

        if on_demand := not self.connected:
            logger.info(f"[CONTROL] Connecting to {self.uri}")
            self.start()
            while not self.connected and time() - self.start_time < 10:
                sleep(0.1)
        self._clear_mp_queue()
        try:
            self.cam_cmd.put_nowait((cmd, payload))
            cam_resp = self.cam_resp.get(timeout=10)
        except Full:
            return {"response": "camera busy"}
        except Empty:
            return {"response": "timed out"}
        finally:
            if on_demand:
                logger.info(f"[CONTROL] Disconnecting from {self.uri}")
                self.stop()

        return cam_resp.pop(cmd, None) or {"response": "could not get result"}

    def check_rtsp_fw(self, force: bool = False) -> Optional[str]:
        """Check and add rtsp."""
        if not self.camera.rtsp_fw:
            return
        logger.info(f"Checking {self.camera.nickname} for firmware RTSP")
        try:
            with WyzeIOTC() as iotc, WyzeIOTCSession(
                iotc.tutk_platform_lib, self.user, self.camera
            ) as session:
                if session.session_check().mode != 2:
                    logger.warning(
                        f"[{self.camera.nickname}] Camera is not on same LAN"
                    )
                    return
                return session.check_native_rtsp(start_rtsp=force)
        except TutkError:
            return

    def _clear_mp_queue(self):
        with contextlib.suppress(Empty, AttributeError):
            self.cam_cmd.get_nowait()
        with contextlib.suppress(Empty, AttributeError):
            self.cam_resp.get_nowait()


AUTO_RESTART_EVERY = 6 * 3600
_last_restart: dict[str, float] = {}
_start_lock = Lock()
# Seconds between Cam OG sessions: one opened sooner fails while the camera still holds the last (tested ~15s).
SLOT_GAP = env_bool("OG_RECONNECT_GAP", "15", style="int")


def auto_restart(stream: "WyzeStream") -> bool:
    """Reboot a camera that keeps failing, through the Wyze cloud, at most every 6 hours."""
    if getenv("AUTO_RESTART", "true").lower() in {"false", "no", "0"} or stream.options.substream:
        return False
    if time() - _last_restart.get(stream.uri, 0) < AUTO_RESTART_EVERY:
        return False
    _last_restart[stream.uri] = time()
    logger.warning(f"[AUTO RESTART] {stream.camera.nickname} keeps failing, restarting it")
    Thread(target=stream.power_control, args=("restart",), daemon=True).start()
    return True


def backoff(failures: int) -> int:
    """Seconds to wait before retry number `failures`: doubles, capped at COOLDOWN_MAX."""
    return min(COOLDOWN * 2 ** min(failures - 1, 16), COOLDOWN_MAX)


REASON_AUTH = "Wyze password changed or session expired"
REASON_OFFLINE = "Camera unplugged or lost power"
REASON_WIFI = "Wi-Fi signal too weak"
REASON_INTERNET = "Internet offline — local recording active"
AUTH_ERRORS = {-19, -68}  # ENR/auth errors, see health_check


def status_reason(status: int, failures: int, connected: bool, wifi, auth_error: bool, internet: bool) -> str | None:
    """The one reason worth showing, most important first: auth, offline, weak Wi-Fi, internet."""
    if auth_error or status in AUTH_ERRORS:
        return REASON_AUTH
    if status == StreamStatus.OFFLINE:
        return REASON_OFFLINE
    try:
        wifi = int(wifi)
    except (TypeError, ValueError):
        wifi = -1
    if failures > 0 and not connected and 0 <= wifi < 40:
        return REASON_WIFI
    if connected and not internet:
        return REASON_INTERNET
    return None


_internet = {"up": True, "checked": 0.0}


def internet_up() -> bool:
    """Last known internet state. Rechecks at most once a minute, in the background, so callers never wait."""
    if time() - _internet["checked"] > 60:
        _internet["checked"] = time()
        Thread(target=_check_internet, daemon=True).start()
    return _internet["up"]


def _check_internet() -> None:
    try:
        socket.create_connection(("api.wyzecam.com", 443), timeout=3).close()
        _internet["up"] = True
    except OSError:
        _internet["up"] = False


def start_relay_stream(uri: str, src: str, state: c_int, wait: float = 0):
    """Copy a go2rtc stream into MediaMTX. FAILED if no video ever arrived."""
    state.value = StreamStatus.CONNECTING
    sleep(wait)

    def flowing():
        if state.value == StreamStatus.CONNECTING:
            state.value = StreamStatus.CONNECTED

    try:
        relay(src, uri, flowing, lambda: state.value < StreamStatus.CONNECTING)
    except Exception as ex:
        logger.warning(f"[{uri}] relay error: {ex}")
    if state.value == StreamStatus.CONNECTING:
        state.value = StreamStatus.FAILED
    elif state.value == StreamStatus.CONNECTED:
        state.value = StreamStatus.STOPPING


def start_tutk_stream(
    uri: str, stream: StreamTuple, queue: QueueTuple, state: c_int, net_mode: c_int
):
    """Connect and communicate with the camera using TUTK."""
    was_offline = state.value == StreamStatus.OFFLINE
    state.value = StreamStatus.CONNECTING
    exit_code = StreamStatus.STOPPING
    control_thread = audio_thread = None
    try:
        with WyzeIOTC() as iotc, iotc.session(stream, state) as sess:
            assert state.value >= StreamStatus.CONNECTING, "Stream Stopped"
            v_codec, audio = get_cam_params(sess, uri)
            net_mode.value = sess.session_check().mode
            control_thread = setup_control(sess, queue, stream.options.substream)
            audio_thread = setup_audio(sess, uri)

            ffmpeg_cmd = get_ffmpeg_cmd(
                uri, v_codec, audio, stream.camera.is_vertical, sess.preferred_frame_rate
            )
            assert state.value >= StreamStatus.CONNECTING, "Stream Stopped"
            state.value = StreamStatus.CONNECTED
            with Popen(ffmpeg_cmd, stdin=PIPE) as ffmpeg:
                for frame in sess.recv_bridge_data():
                    ffmpeg.stdin.write(frame)

    except TutkError as ex:
        logger.warning(f"{[ex.code]} {ex}")
        set_cam_offline(uri, ex, was_offline)
        if ex.code in {-10, -13, -19, -68, -90}:
            exit_code = ex.code
    except ValueError as ex:
        logger.warning(ex)
        if ex.args[0] == "ENR_AUTH_FAILED":
            logger.warning("⏰ Expired ENR?")
            exit_code = -19
    except BrokenPipeError:
        logger.info("FFMPEG stopped")
    except Exception as ex:
        logger.warning(f"[{type(ex).__name__}] {ex}")
    else:
        logger.warning("Stream stopped")
    finally:
        state.value = exit_code
        stop_and_wait(audio_thread)
        stop_and_wait(control_thread)


def stop_and_wait(thread: Optional[Thread]):
    if thread and thread.is_alive():
        with contextlib.suppress(AttributeError, RuntimeError):
            thread.join()


def setup_audio(sess: WyzeIOTCSession, uri: str) -> Optional[Thread]:
    if not sess.enable_audio:
        return
    audio_thread = Thread(target=sess.recv_audio_pipe, name=f"{uri}_audio")
    audio_thread.start()
    return audio_thread


def setup_control(
    sess: WyzeIOTCSession, queue: QueueTuple, substream: bool = False
) -> Optional[Thread]:
    if substream:
        return
    control_thread = Thread(
        target=camera_control,
        args=(sess, queue.cam_resp, queue.cam_cmd),
        name=f"{sess.camera.name_uri}_control",
    )
    control_thread.start()
    return control_thread


def get_cam_params(sess: WyzeIOTCSession, uri: str) -> tuple[str, dict]:
    """Check session and return fps and audio codec from camera."""
    net_mode = check_net_mode(sess.session_check().mode, uri)
    v_codec, fps = get_video_params(sess)
    firmware, wifi = get_camera_info(sess)
    stream = (
        f"{sess.preferred_bitrate}kb/s {sess.resolution} stream ({v_codec}/{fps}fps)"
    )

    logger.info(f"📡 Getting {stream} via {net_mode} (WiFi: {wifi}%) FW: {firmware}")

    audio = get_audio_params(sess)
    mqtt = [
        (f"{MQTT_TOPIC}/{uri.lower()}/net_mode", net_mode),
        (f"{MQTT_TOPIC}/{uri.lower()}/wifi", wifi),
        (f"{MQTT_TOPIC}/{uri.lower()}/audio", json.dumps(audio) if audio else False),
        (f"{MQTT_TOPIC}/{uri.lower()}/ip", sess.camera.ip, 0, True),
    ]
    publish_messages(mqtt)
    return v_codec, audio


def get_camera_info(sess: WyzeIOTCSession) -> tuple[str, str]:
    if not (camera_info := sess.camera.camera_info):
        logger.warn("⚠️ cameraInfo is missing.")
        return "NA", "NA"
    logger.debug(f"[cameraInfo] {camera_info}")

    firmware = camera_info.get("basicInfo", {}).get("firmware", "NA")
    if sess.camera.dtls or sess.camera.parent_dtls:
        firmware += " 🔒"

    wifi = camera_info.get("basicInfo", {}).get("wifidb", "NA")
    if "netInfo" in camera_info:
        wifi = camera_info["netInfo"].get("signal", wifi)

    return firmware, wifi


def get_video_params(sess: WyzeIOTCSession) -> tuple[str, int]:
    cam_info = sess.camera.camera_info
    if not cam_info or not (video_param := cam_info.get("videoParm")):
        logger.warn("⚠️ camera_info is missing videoParm. Using default values.")
        video_param = {"type": "h264", "fps": 20}

    fps = int(video_param.get("fps", 0))

    if force_fps := int(env_cam("FORCE_FPS", sess.camera.name_uri, "0")):
        logger.info(f"Attempting to force fps={force_fps}")
        sess.update_frame_size_rate(fps=force_fps)
        fps = force_fps

    if fps % 5 != 0:
        logger.error(f"⚠️ Unusual FPS detected: {fps}")

    logger.debug(f"[videoParm] {video_param}")
    sess.preferred_frame_rate = fps

    return video_param.get("type", "h264"), fps


def get_audio_params(sess: WyzeIOTCSession) -> dict[str, str | int]:
    if not sess.enable_audio:
        return {}

    codec, rate = sess.identify_audio_codec()
    logger.info(f"🔊 Audio Enabled [Source={codec.upper()}/{rate:,}Hz]")

    if codec_out := env_bool("AUDIO_CODEC"):
        logger.info(f"[AUDIO] Re-Encode Enabled [AUDIO_CODEC={codec_out}]")
    elif rate > 8000 or codec.lower() == "s16le":
        codec_out = "pcm_mulaw"
        logger.info(f"[AUDIO] Re-Encode for RTSP compatibility [{codec_out=}]")

    return {"codec": codec, "rate": rate, "codec_out": codec_out.lower()}


def check_net_mode(session_mode: int, uri: str) -> str:
    """Check if the connection mode is allowed."""
    net_mode = load_net_modes().get(uri) or env_cam("NET_MODE", uri, "any")
    if "p2p" in net_mode and session_mode == 1:
        raise Exception("☁️ Connected via RELAY MODE! Reconnecting")
    if "lan" in net_mode and session_mode != 2:
        raise Exception("☁️ Connected via NON-LAN MODE! Reconnecting")

    mode = f'{NET_MODE.get(session_mode, f"UNKNOWN ({session_mode})")} mode'
    if session_mode != 2:
        logger.warning(f"☁️ Camera is connected via {mode}!!")
        logger.warning("Stream may consume additional bandwidth!")
    return mode


def set_cam_offline(uri: str, error: TutkError, was_offline: bool) -> None:
    """Do something when camera goes offline."""
    state = "offline" if error.code == -90 else error.name
    update_mqtt_state(uri.lower(), state)

    if str(error.code) not in env_bool("OFFLINE_ERRNO", "-90"):
        return
    if was_offline:  # Don't resend if previous state was offline.
        return

    send_webhook("offline", uri, f"{uri} is offline")


def is_timedout(start_time: float, timeout: int = 20) -> bool:
    return time() - start_time > timeout if start_time else False
