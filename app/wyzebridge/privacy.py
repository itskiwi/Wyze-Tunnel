"""Privacy schedule: turn chosen cameras off for a daily time window, then back on."""
import json
from datetime import datetime
from typing import Optional

from wyzebridge.config import TOKEN_PATH
from wyzebridge.logging import logger
from wyzebridge.webhooks import in_quiet_hours

PRIVACY_FILE = TOKEN_PATH + "privacy.json"


def load_privacy() -> dict:
    try:
        with open(PRIVACY_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return {}


def save_privacy(form: dict, macs: set[str]) -> dict:
    """Validate and save. Raises ValueError with a message for the user."""
    cams = form.get("cams") or []
    if not isinstance(cams, list) or not set(cams) <= macs:
        raise ValueError("Unknown camera.")
    times = {}
    for key in ("start", "end"):
        try:
            times[key] = datetime.strptime(str(form.get(key) or ""), "%H:%M").strftime("%H:%M")
        except ValueError:
            if cams:
                raise ValueError("Set a start and end time, like 22:00 to 07:00.") from None
    settings = {"cams": sorted(cams), **times}
    with open(PRIVACY_FILE, "w") as f:
        json.dump(settings, f)
    return settings


class PrivacySchedule:
    """Acts only when the window starts or ends, so a camera you turn on by hand stays on."""

    def __init__(self):
        self.was_private: Optional[bool] = None

    def check(self, streams: dict, now=None) -> None:
        settings = load_privacy()
        if not settings.get("cams"):
            self.was_private = None
            return
        private = in_quiet_hours({"quiet_start": settings.get("start"), "quiet_end": settings.get("end")}, now)
        # At startup only act if we're inside the window.
        if private == self.was_private or (self.was_private is None and not private):
            self.was_private = private
            return
        self.was_private = private
        action = "off" if private else "on"
        for stream in streams.values():
            if stream.camera.mac in settings["cams"] and not stream.options.substream:
                logger.info(f"[PRIVACY] Turning {stream.uri} {action}")
                stream.send_cmd("power", action)
