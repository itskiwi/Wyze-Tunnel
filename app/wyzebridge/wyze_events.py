import time
from collections import deque
from threading import Thread
from typing import Any

from wyzebridge import event_archive, phrases, visitors
from wyzebridge.config import MOTION_INT, MOTION_START
from wyzebridge.logging import logger
from wyzebridge.mqtt import update_preview
from wyzebridge.webhooks import ALERTS, SOUND_EVENTS, URGENT, send_webhook
from wyzebridge.wyze_api import display_name
from wyzebridge.wyze_stream import WyzeStream


# shortcut: kept in memory, so the history clears on restart; persist it if people want older events.
EVENT_LOG: deque[dict] = deque(maxlen=100)
# Wyze's own AI tags (Cam Plus); without them an event is plain movement.
# Sound tags 8000xx: the app's GmCommonData.AiTagCons. Smoke/CO are event_value 4/5, not tags (parse adds them).
WYZE_TAGS = {101: "person", 102: "vehicle", 103: "pet", 104: "package", 800001: "crying", 800002: "barking",
             800003: "meowing", 800005: "talking", 800006: "glass", 800007: "gunshot", 4: "smoke", 5: "co"}


SOUND_LABELS = SOUND_EVENTS + tuple(URGENT)
SIREN_SECONDS = 30
SIREN_COOLDOWN = 120
_last_siren: dict[str, float] = {}


def sound_siren(stream) -> bool:
    """Sound the camera's siren for a short burst. Runs in a thread: camera commands block for seconds."""
    if time.time() - _last_siren.get(stream.uri, 0) < SIREN_COOLDOWN:
        return False
    _last_siren[stream.uri] = time.time()

    def burst():
        logger.info(f"[MOTION] Sounding the siren on {stream.uri}")
        stream.send_cmd("alarm", "on")
        time.sleep(SIREN_SECONDS)
        stream.send_cmd("alarm", "off")

    Thread(target=burst, daemon=True).start()
    return True


class WyzeEvents:
    __slots__ = "api", "streams", "events", "last_check", "last_ts"

    def __init__(self, streams: dict[str, WyzeStream | Any]):
        self.streams = streams
        self.api = next(iter(streams.values())).api
        self.events: deque[str] = deque(maxlen=20)
        self.last_check: float = 0
        self.last_ts: int = 0
        logger.info(f"API Motion Events Enabled [interval={MOTION_INT}]")

    def enabled_cams(self) -> list:
        return [s.camera.mac for s in self.streams.values() if s.enabled]

    def get_events(self) -> list:
        if time.time() - self.last_check < MOTION_INT:
            return []
        self.last_check, resp = self.api.get_events(self.enabled_cams(), self.last_ts)
        if resp:
            logger.debug(f"[MOTION] Got {len(resp)} events")
        return resp

    def log_entry(self, stream, ts: int, files: list, ring: bool, tags: list | None) -> dict:
        img = next((f["url"] for f in files if f["type"] == 1), None)
        clip = next((f["url"] for f in files if f["type"] == 2), None)  # Wyze's own short cloud video
        name = display_name(stream.camera)
        label = "ring" if ring else next((WYZE_TAGS[t] for t in tags or [] if t in WYZE_TAGS), "motion")
        msg = phrases.describe(label, name, phrases.place_of(stream.uri, name))
        return {"uri": stream.uri, "name": name, "ts": ts, "img": img, "clip": clip, "text": msg} | (
            {"label": "rang the doorbell"} if ring else {"label": label} if label != "motion" else {}
        )

    def set_motion(self, mac: str, files: list, ring: bool = False, tags: list | None = None, event_id: str = "") -> None:
        for stream in self.streams.values():
            if stream.camera.mac == mac and not stream.options.substream:
                entry = self.log_entry(stream, self.last_ts, files, ring, tags)
                img, name, msg = entry["img"], entry["name"], entry["text"]
                if img:
                    stream.camera.thumbnail = img
                stream.motion = self.last_ts
                logger.info(f"[MOTION] {msg} ({name})")
                EVENT_LOG.appendleft(entry)
                if event_id:  # Wyze deletes its copies after 14 days; ours stay. A thread: the downloads take seconds.
                    Thread(target=event_archive.save_event, args=(self.api, event_id, entry, img, entry["clip"]), daemon=True).start()
                sound = entry.get("label") in SOUND_LABELS
                send_webhook(entry["label"] if sound else "motion", stream.uri, msg, img, title=f"{URGENT[entry['label']]}: {name}" if entry.get("label") in URGENT else name)
                if ALERTS.get("siren_away") and ALERTS.get("mode") == "away":
                    sound_siren(stream)
                if MOTION_START:
                    stream.start()
                if img and self.api.save_thumbnail(stream.camera.name_uri, img):
                    update_preview(stream.camera.name_uri)
                if ring:
                    visitors.add(stream.uri, name, self.last_ts)

    @staticmethod
    def parse(event: dict) -> tuple:
        # v2 uses device_mac and v4 uses device_id
        # event_value 10 is a doorbell press (wyze-sdk's EventAlarmType.DOORBELL_RANG)
        tags = [t for t in event.get("tag_list") or [] if isinstance(t, int)]
        if str(event.get("event_value")) in ("4", "5"):  # smoke, CO: first, so they win over other tags
            tags.insert(0, int(event["event_value"]))
        return event["device_id"], event["file_list"], str(event.get("event_value")) == "10", tags

    def process_event(self, event: dict):
        if event["event_id"] in self.events:
            return
        logger.debug(f"[MOTION] New motion event: {event['event_id']}")
        self.events.append(event["event_id"])
        self.last_ts = int(event["event_ts"] / 1000)
        if time.time() - self.last_ts < 30:
            self.set_motion(*self.parse(event), str(event["event_id"]))

    def backfill(self):
        """After a restart, fill the timeline with the last day of Wyze events. Timeline only: no alerts for old events."""
        self.last_check, resp = self.api.get_events(self.enabled_cams(), history=86400)
        streams = {s.camera.mac: s for s in self.streams.values() if not s.options.substream}
        for event in resp:  # newest first, and older than anything already logged
            mac, files, ring, tags = self.parse(event)
            if len(EVENT_LOG) >= EVENT_LOG.maxlen:
                break
            if stream := streams.get(mac):
                EVENT_LOG.append(self.log_entry(stream, int(event["event_ts"] / 1000), files, ring, tags))
        if resp:
            self.last_ts = int(resp[0]["event_ts"] / 1000)
            logger.info(f"[MOTION] Loaded {len(resp)} Wyze events from the last day into the timeline")

    def check_motion(self):
        if not self.last_check:
            return self.backfill()
        if time.time() - self.last_check < MOTION_INT:
            return
        if time.time() - event_archive._last_poll >= event_archive.SENSOR_POLL:  # sensor history is a few calls; a thread keeps motion quick
            Thread(target=event_archive.poll_sensors, args=(self.api,), daemon=True).start()
        for event in self.get_events():
            self.process_event(event)
