import contextlib
import json
import time
from subprocess import Popen, TimeoutExpired
from threading import Thread
from typing import Any, Callable, Optional, Protocol

from wyzebridge import automations, backup, guests, recording, summary
from wyzebridge.config import MOTION, MQTT_DISCOVERY, SNAPSHOT_INT, SNAPSHOT_TYPE
from wyzebridge.ffmpeg import rtsp_snap_cmd
from wyzebridge.logging import logger
from wyzebridge.mqtt import bridge_status, cam_control, publish_topic, update_preview
from wyzebridge.mtx_event import RtspEvent
from wyzebridge.mtx_server import path_bytes
from wyzebridge.privacy import BATTERY_FILE, PrivacySchedule
from wyzebridge.webhooks import ALERTS
from wyzebridge.wyze_events import EVENT_LOG, WyzeEvents


class Stream(Protocol):
    camera: Any
    options: Any
    start_time: float
    state: Any
    uri: str

    @property
    def connected(self) -> bool: ...

    @property
    def enabled(self) -> bool: ...

    @property
    def motion(self) -> bool: ...

    def start(self) -> bool: ...

    def stop(self) -> bool: ...

    def enable(self) -> bool: ...

    def disable(self) -> bool: ...

    def health_check(self) -> int: ...

    def get_info(self, item: Optional[str] = None) -> dict: ...

    def status(self) -> str: ...

    def send_cmd(self, cmd: str, payload: str | list | dict = "") -> dict: ...


STALL_SECS = 30


class Watchdog:
    """Spots connected streams whose video stopped arriving at MediaMTX."""

    def __init__(self) -> None:
        self.seen: dict[str, tuple[int, float]] = {}  # uri: (bytes in, when it last changed)

    def check(self, streams: dict, sizes: dict, now: float) -> list[str]:
        if not sizes:  # MediaMTX API unreachable: no evidence either way
            return []
        stalled = []
        for uri, stream in streams.items():
            got = sizes.get(uri, {}).get("in")
            if got is None or not stream.connected:
                self.seen.pop(uri, None)
                continue
            last = self.seen.get(uri)
            if not last or got != last[0]:  # != since the count restarts with the stream
                self.seen[uri] = (got, now)
            elif now - last[1] > STALL_SECS:
                stalled.append(uri)
                del self.seen[uri]
        return stalled


class StreamManager:
    __slots__ = "stop_flag", "streams", "rtsp_snapshots", "last_snap", "thread"

    def __init__(self):
        self.stop_flag: bool = False
        self.streams: dict[str, Stream] = {}
        self.rtsp_snapshots: dict[str, Popen] = {}
        self.last_snap: float = 0
        self.thread: Optional[Thread] = None

    @property
    def total(self):
        return len(self.streams)

    @property
    def active(self):
        return len([s for s in self.streams.values() if s.enabled])

    def add(self, stream: Stream) -> str:
        uri = stream.uri
        self.streams[uri] = stream
        return uri

    def get(self, uri: str) -> Optional[Stream]:
        return self.streams.get(uri)

    def get_info(self, uri: str) -> dict:
        return stream.get_info() if (stream := self.get(uri)) else {}

    def get_all_cam_info(self) -> dict:
        return {uri: s.get_info() for uri, s in self.streams.items()}

    def stop_all(self) -> None:
        logger.info(f"Stopping {self.total} stream{'s'[:self.total^1]}")
        self.stop_flag = True
        for stream in self.streams.values():
            stream.stop()
        if self.thread and self.thread.is_alive():
            with contextlib.suppress(AttributeError):
                self.thread.join()

    def monitor_streams(self, mtx_health: Callable) -> None:
        self.stop_flag = False
        if MQTT_DISCOVERY:
            self.thread = Thread(target=self.monitor_snapshots)
            self.thread.start()
        mqtt = cam_control(self.streams, self.send_cmd)
        logger.info(f"🎬 {self.total} stream{'s'[:self.total^1]} enabled")
        event = RtspEvent(self.streams)
        events = None
        last_health = 0.0
        privacy = PrivacySchedule()
        battery_saver = PrivacySchedule(BATTERY_FILE, ("state", "disable", "enable"))
        watchdog = Watchdog()
        while not self.stop_flag:
            # One bad iteration must not end monitoring, or no stream ever reconnects.
            try:
                event.read(timeout=1)
                self.snap_all(self.active_streams())
                # Created lazily so turning on motion alerts in the web UI needs no restart.
                if not events and self.streams and (MOTION or ALERTS.get("motion") or ALERTS.get("siren_away") or ALERTS.get("summary") or ALERTS.get("topic") or ALERTS.get("chat_url") or (automations.load().get("lights") or {}).get("on")):
                    events = WyzeEvents(self.streams)
                if events:
                    events.check_motion()
                if time.time() - last_health >= 15:
                    last_health = time.time()
                    mtx_health()
                    self.restart_stalled(watchdog.check(self.streams, path_bytes(), time.time()))
                    settings = recording.load_settings()
                    recording.keep_awake(bool(settings.get("all") or settings.get("cams")))
                    bridge_status(mqtt, self.streams)
                    privacy.check(self.streams)
                    battery_saver.check(self.streams)
                    guests.prune()
                    backup.auto_backup()
                    summary.check(list(EVENT_LOG))
                    if self.streams:  # Wyze calls can take seconds, so not on this loop
                        Thread(target=automations.tick, args=(next(iter(self.streams.values())).api, self.streams), daemon=True).start()
            except Exception:
                logger.exception("[MONITOR] Error in stream monitor loop")
                time.sleep(1)
        recording.keep_awake(False)
        if mqtt:
            mqtt.loop_stop()
        logger.info("Stream monitoring stopped")

    def restart_stalled(self, uris: list[str]) -> None:
        for uri in uris:
            stream = self.streams[uri]
            logger.warning(f"[WATCHDOG] {stream.camera.nickname} stopped sending video for {STALL_SECS}s, reconnecting it")
            stream.stop()  # health_check starts it again
            source = getattr(stream, "source", "tutk")
            if source != "tutk" and (go2rtc := getattr(stream, "go2rtc", None)):
                go2rtc.reset(f"{uri}_{source}")

    def monitor_snapshots(self) -> None:
        for cam in self.streams:
            update_preview(cam)
        while not self.stop_flag:
            for cam, ffmpeg in list(self.rtsp_snapshots.items()):
                if (returncode := ffmpeg.returncode) is not None:
                    if returncode == 0:
                        update_preview(cam)
                    del self.rtsp_snapshots[cam]
            time.sleep(1)

    def active_streams(self) -> list[str]:
        """
        Health check on all streams and return a list of enabled
        streams that are NOT battery powered.

        Returns:
        - list(str): uri-friendly name of streams that are enabled.
        """
        if self.stop_flag:
            return []
        return [cam for cam, s in self.streams.items() if s.health_check() > 0]

    def snap_all(self, cams: Optional[list[str]] = None, force: bool = False):
        """
        Take an rtsp snapshot of the streams in the list.

        Args:
        - cams (list[str], optional): names of the streams to take a snapshot of.
        - force (bool, optional): Ignore interval and force snapshot. Defaults to False.
        """
        if force or self._should_snap():
            self.last_snap = time.time()
            for cam in cams or self.active_streams():
                stop_subprocess(self.rtsp_snapshots.get(cam))
                self.rtsp_snap_popen(cam, True)

    def _should_snap(self):
        return SNAPSHOT_TYPE == "rtsp" and time.time() - self.last_snap >= SNAPSHOT_INT

    def get_sse_status(self) -> dict:
        return {
            uri: {
                "status": cam.status(),
                "motion": cam.motion,
                "net_mode": getattr(cam, "net_mode", ""),
                "busy": getattr(cam, "busy", False),
            }
            for uri, cam in self.streams.items()
        }

    def send_cmd(
        self, cam_name: str, cmd: str, payload: str | list | dict = ""
    ) -> dict:
        """
        Send a command directly to the camera and wait for a response.

        Parameters:
        - cam_name (str): uri-friendly name of the camera.
        - cmd (str): The camera/tutk command to send.
        - payload (str): value for the tutk command.

        Returns:
        - dictionary: Results that can be converted to JSON.
        """
        resp = {"status": "error", "command": cmd, "payload": payload}

        if cam_name == "all" and cmd == "update_snapshot":
            self.snap_all(force=True)
            return resp | {"status": "success"}

        if cam_name == "all" and cmd == "quality":
            # Data saver: one tap switches every camera between HD and SD.
            results = [s.send_cmd(cmd, payload) for s in self.streams.values() if not s.options.substream]
            ok = all(r.get("status") == "success" for r in results)
            return resp | {"status": "success" if ok else "error"}

        if not (stream := self.get(cam_name)):
            return resp | {"response": "Camera not found"}

        if cam_resp := stream.send_cmd(cmd, payload):
            status = cam_resp.get("value") if cam_resp.get("status") == "success" else 0
            if isinstance(status, dict):
                status = json.dumps(status)

            if "update_snapshot" in cam_resp:
                on_demand = not stream.connected
                snap = self.get_rtsp_snap(cam_name)
                if on_demand:
                    stream.stop()
                publish_topic(f"{cam_name}/{cmd}", int(time.time()) if snap else 0)
                return dict(resp, status="success", value=snap, response=snap)

            publish_topic(f"{cam_name}/{cmd}", status)

        return cam_resp if "status" in cam_resp else resp | cam_resp

    def rtsp_snap_popen(self, cam_name: str, interval: bool = False) -> Optional[Popen]:
        if not (stream := self.get(cam_name)):
            return
        stream.start()
        ffmpeg = self.rtsp_snapshots.get(cam_name)
        if not ffmpeg or ffmpeg.poll() is not None:
            ffmpeg = Popen(rtsp_snap_cmd(cam_name, interval))
            self.rtsp_snapshots[cam_name] = ffmpeg
        return ffmpeg

    def get_rtsp_snap(self, cam_name: str) -> bool:
        if not (stream := self.get(cam_name)) or stream.health_check() < 1:
            return False
        if not (ffmpeg := self.rtsp_snap_popen(cam_name)):
            return False
        try:
            if ffmpeg.wait(timeout=15) == 0:
                return True
        except TimeoutExpired:
            logger.error(f"[{cam_name}] Snapshot timed out")
        except Exception as ex:
            logger.error(ex)
        stop_subprocess(ffmpeg)

        return False


def stop_subprocess(ffmpeg: Optional[Popen]):
    if ffmpeg and ffmpeg.poll() is None:
        ffmpeg.kill()
        ffmpeg.communicate()
