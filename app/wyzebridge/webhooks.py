import json
import re
from datetime import datetime, time
from typing import Optional

import requests
from wyzebridge.bridge_utils import env_cam
from wyzebridge.config import TOKEN_PATH, VERSION
from wyzebridge.logging import logger

ALERTS_FILE = TOKEN_PATH + "alerts.json"
ALERT_EVENTS = ("motion", "offline")


def load_alerts() -> dict:
    """Phone alert settings saved from the web UI's Alerts page."""
    try:
        with open(ALERTS_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return {}


def save_alerts(form: dict) -> dict:
    """Validate and save alert settings. Raises ValueError with a message for the user."""
    server = str(form.get("server") or "https://ntfy.sh").strip().rstrip("/")
    topic = str(form.get("topic") or "").strip()
    if not re.fullmatch(r"https?://[^\s/]+(/[^\s]*)?", server):
        raise ValueError("The server should look like https://ntfy.sh")
    if topic and not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", topic):
        raise ValueError("The topic can only use letters, numbers, - and _ (up to 64)")
    quiet = {}
    for key in ("quiet_start", "quiet_end"):
        if value := str(form.get(key) or "").strip():
            try:
                quiet[key] = datetime.strptime(value, "%H:%M").strftime("%H:%M")
            except ValueError:
                raise ValueError("Quiet hours should be times like 22:00") from None
    if len(quiet) == 1:
        raise ValueError("Set both a start and an end for quiet hours, or neither")
    settings = {"server": server, "topic": topic, **quiet}
    settings |= {event: bool(form.get(event)) for event in ALERT_EVENTS}
    with open(ALERTS_FILE, "w") as f:
        json.dump(settings, f)
    ALERTS.clear()
    ALERTS.update(settings)
    return settings


ALERTS: dict = load_alerts()


def alert_url(event: str) -> str:
    if ALERTS.get("topic") and (ALERTS.get(event) or event == "test"):
        return f"{ALERTS['server']}/{ALERTS['topic']}"
    return ""


def in_quiet_hours(settings: dict, now: Optional[time] = None) -> bool:
    if not (start := settings.get("quiet_start")) or not (end := settings.get("quiet_end")):
        return False
    now = now or datetime.now().time()
    start, end = time.fromisoformat(start), time.fromisoformat(end)
    # A window like 22:00-07:00 wraps past midnight.
    return start <= now < end if start <= end else now >= start or now < end


def send_webhook(event: str, camera: str, msg: str, img: Optional[str] = None) -> bool:
    url = env_cam(f"{event}_webhooks", camera, style="original")
    if not url:
        if event == "motion" and in_quiet_hours(ALERTS):
            return False
        url = alert_url(event)
    if not url:
        return False

    header = {
        "user-agent": f"wyzebridge/{VERSION}",
        "X-Title": f"{event} event".title(),
        "X-Attach": img,
        "X-Tags": f"{camera},{event}",
        "X-Camera": camera,
        "X-Event": event,
    }

    logger.debug(f"[WEBHOOKS] 📲 Triggering {event.upper()} event for {camera}")
    try:
        resp = requests.post(url, headers=header, data=msg.encode(), verify=False, timeout=10)
        resp.raise_for_status()
        return True
    except Exception as ex:
        print(f"[WEBHOOKS] {ex}")
        return False
