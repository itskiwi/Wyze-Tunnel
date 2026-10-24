import os
import signal
import sys
from dataclasses import replace
from threading import Thread

from wyzebridge import config, detect, guests, integrations, webhooks
from wyzebridge.auth import STREAM_AUTH, WbAuth
from wyzebridge.bridge_utils import env_bool, env_cam, is_livestream
from wyzebridge.logging import logger
from wyzebridge.mtx_server import MtxServer
from wyzebridge.stream import StreamManager
from wyzebridge.wyze_api import WyzeApi, display_name
from wyzebridge.wyze_events import EVENT_LOG
from wyzebridge.wyze_stream import WyzeStream, WyzeStreamOptions
from wyzecam.api_models import WyzeCamera


class WyzeBridge(Thread):
    __slots__ = "api", "streams", "mtx"

    def __init__(self) -> None:
        Thread.__init__(self)
        for sig in {"SIGTERM", "SIGINT"}:
            signal.signal(getattr(signal, sig), self.clean_up)
        print(f"\n🚀 DOCKER-WYZE-BRIDGE v{config.VERSION} {config.BUILD_STR}\n")
        self.api: WyzeApi = WyzeApi()
        self.streams: StreamManager = StreamManager()
        self.mtx: MtxServer = MtxServer()
        self.mtx.setup_webrtc(config.BRIDGE_IP)
        if config.LLHLS:
            self.mtx.setup_llhls(config.TOKEN_PATH, bool(config.HASS_TOKEN))
        detect.start_frigate(lambda: list(self.streams.streams))

    def run(self, fresh_data: bool = False) -> None:
        self._initialize(fresh_data)

    def _initialize(self, fresh_data: bool = False) -> None:
        self.api.login(fresh_data=fresh_data)
        WbAuth.set_email(email=self.api.get_user().email, force=fresh_data)
        self.mtx.setup_auth(WbAuth.api, STREAM_AUTH)
        guests.sync_stream_auth()
        integrations.sync_stream_auth()
        self.setup_streams()
        if self.streams.total < 1:
            return signal.raise_signal(signal.SIGINT)
        self.mtx.start()
        WyzeStream.go2rtc.start()
        self.streams.monitor_streams(self.health_check)

    def health_check(self) -> None:
        self.mtx.health_check()
        WyzeStream.go2rtc.health_check()

    def restart(self, fresh_data: bool = False) -> None:
        self.mtx.stop()
        WyzeStream.go2rtc.stop()
        self.streams.stop_all()
        self._initialize(fresh_data)

    def refresh_cams(self) -> None:
        self.mtx.stop()
        WyzeStream.go2rtc.stop()
        self.streams.stop_all()
        self.streams.streams.clear()  # so cameras hidden since the last start drop out
        WyzeStream.go2rtc.streams.clear()
        self.api.get_cameras(fresh_data=True)
        self._initialize(False)

    def setup_streams(self):
        """Gather and setup streams for each camera."""
        WyzeStream.user = self.api.get_user()
        WyzeStream.api = self.api

        for cam in self.api.filtered_cams():
            logger.info(f"[+] Adding {cam.nickname} [{cam.product_model}]")
            if config.SNAPSHOT_TYPE == "api":
                self.api.save_thumbnail(cam.name_uri)
            options = WyzeStreamOptions(
                quality=env_cam("quality", cam.name_uri),
                audio=bool(env_cam("enable_audio", cam.name_uri)),
                record=bool(env_cam("record", cam.name_uri)),
                reconnect=is_livestream(cam.name_uri) or not config.ON_DEMAND,
            )
            self.add_substream(cam, options)
            stream = WyzeStream(cam, options)
            stream.rtsp_fw_enabled = self.rtsp_fw_proxy(cam, stream)

            self.mtx.add_path(stream.uri, not options.reconnect)
            if env_cam("record", cam.name_uri):
                self.mtx.record(stream.uri)
                # Only recording cameras: they stay connected anyway, and every hit has a clip to play.
                if env_cam("detect", cam.name_uri):
                    detect.watch(stream.uri, display_name(cam), on_detect)
            self.streams.add(stream)

    def rtsp_fw_proxy(self, cam: WyzeCamera, stream: WyzeStream) -> bool:
        if rtsp_fw := env_bool("rtsp_fw").lower():
            if rtsp_path := stream.check_rtsp_fw(rtsp_fw == "force"):
                rtsp_uri = f"{cam.name_uri}-fw"
                logger.info(f"Adding /{rtsp_uri} as a source")
                self.mtx.add_source(rtsp_uri, rtsp_path)
                return True
        return False

    def add_substream(self, cam: WyzeCamera, options: WyzeStreamOptions):
        """Setup and add substream if enabled for camera."""
        if env_bool(f"SUBSTREAM_{cam.name_uri}") or (
            env_bool("SUBSTREAM") and cam.can_substream
        ):
            quality = env_cam("sub_quality", cam.name_uri, "sd30")
            record = bool(env_cam("sub_record", cam.name_uri))
            sub_opt = replace(options, substream=True, quality=quality, record=record)
            sub = WyzeStream(cam, sub_opt)
            self.mtx.add_path(sub.uri, not options.reconnect)
            self.streams.add(sub)

    def clean_up(self, *_):
        """Stop all streams and clean up before shutdown."""
        if self.streams.stop_flag:
            os._exit(0)
        if self.streams:
            self.streams.stop_all()
        detect.stop_all()
        self.mtx.stop()
        WyzeStream.go2rtc.stop()
        logger.info("👋 goodbye!")
        # sys.exit would wait on the login, monitor and camera threads, which never end, until systemd kills us 90s later.
        os._exit(0)


def on_detect(uri: str, name: str, label: str, det_id: int, ts: float) -> None:
    img = f"detections/{det_id}.jpg"
    EVENT_LOG.appendleft({"uri": uri, "name": name, "ts": int(ts), "img": img, "label": label})
    if label == "person":
        webhooks.send_webhook("person", uri, f"Person spotted on {name}")


if __name__ == "__main__":
    wb = WyzeBridge()
    wb.run()
    sys.exit(0)
