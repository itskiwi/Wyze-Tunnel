import json
import re
from datetime import datetime, time
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import requests
from wyzebridge import integrations
from wyzebridge.bridge_utils import env_cam
from wyzebridge.config import TOKEN_PATH, VERSION
from wyzebridge.logging import logger

ALERTS_FILE = TOKEN_PATH + "alerts.json"
ALERT_EVENTS = ("motion", "person", "offline", "summary", "cleared")
# Chat apps an alert can also go to, by webhook host. Only these, so a saved URL can't reach other servers.
CHAT_HOSTS = {"discord.com": "discord", "discordapp.com": "discord", "hooks.slack.com": "slack", "api.telegram.org": "telegram"}
# home: no motion alerts. away (holiday): motion alerts even in quiet hours. schedule: quiet hours apply.
ALERT_MODES = ("schedule", "home", "away")


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
    chat_url = str(form.get("chat_url") or "").strip()
    if chat_url and not chat_service(chat_url):
        raise ValueError("Paste a Discord or Slack webhook link, or a Telegram sendMessage link")
    settings = {"server": server, "topic": topic, "chat_url": chat_url, **quiet}
    settings |= {event: bool(form.get(event)) for event in ALERT_EVENTS}
    settings["siren_away"] = bool(form.get("siren_away"))
    settings |= {k: ALERTS[k] for k in ("mode", "lock_minutes") if k in ALERTS}
    _write_alerts(settings)
    return settings


def set_alert_mode(mode: str) -> None:
    if mode not in ALERT_MODES:
        raise ValueError(f"Use one of: {', '.join(ALERT_MODES)}")
    _write_alerts(ALERTS | {"mode": mode})


def set_lock_alert(minutes) -> int:
    """Alert when a lock stays unlocked this many minutes. 0 turns it off."""
    try:
        minutes = -1 if isinstance(minutes, bool) else int(minutes)
    except (TypeError, ValueError):
        minutes = -1
    if not 0 <= minutes <= 1440:
        raise ValueError("Use a number of minutes from 0 (off) to 1440")
    _write_alerts(ALERTS | {"lock_minutes": minutes})
    return minutes


def _write_alerts(settings: dict) -> None:
    with open(ALERTS_FILE, "w") as f:
        json.dump(settings, f)
    ALERTS.clear()
    ALERTS.update(settings)


ALERTS: dict = load_alerts()


def chat_service(url: str) -> str:
    """discord, slack or telegram for a webhook link we know how to post to, else ""."""
    parts = urlparse(url)
    return CHAT_HOSTS.get(parts.hostname or "", "") if parts.scheme == "https" else ""


def chat_payload(service: str, title: str, msg: str, img: Optional[str]) -> dict:
    if service == "discord":
        return {"content": f"**{title}**\n{msg}", "embeds": [{"image": {"url": img}}] if img else []}
    text = f"{title}: {msg}" + (f"\n{img}" if img else "")
    return {"text": text}


def alert_url(event: str) -> str:
    if ALERTS.get("topic") and (ALERTS.get(event) or event in ("test", "update", "street", "away", "lock")):
        return f"{ALERTS['server']}/{ALERTS['topic']}"
    return ""


def in_quiet_hours(settings: dict, now: Optional[time] = None) -> bool:
    if not (start := settings.get("quiet_start")) or not (end := settings.get("quiet_end")):
        return False
    now = now or datetime.now().time()
    start, end = time.fromisoformat(start), time.fromisoformat(end)
    # A window like 22:00-07:00 wraps past midnight.
    return start <= now < end if start <= end else now >= start or now < end


def send_webhook(event: str, camera: str, msg: str, img: Optional[str] = None, title: str = "") -> bool:
    if event != "test":
        integrations.dispatch(event, camera, msg, img)
    url = env_url = env_cam(f"{event}_webhooks", camera, style="original")
    if not url:
        mode = ALERTS.get("mode", "schedule")
        if event in ("motion", "person", "cleared") and (mode == "home" or (mode == "schedule" and in_quiet_hours(ALERTS))):
            return False
        url = alert_url(event)
    chat = ALERTS.get("chat_url") if not env_url and (ALERTS.get(event) or event in ("test", "update", "street", "away", "lock")) else ""
    if not url and not chat:
        return False
    title = title or {"update": "Wyze Tunnel updated", "street": "Street Watch request", "away": "Away access needs you", "lock": "Door left unlocked", "summary": "Last night at home"}.get(event) or f"{event} event".title()
    sent = bool(url) and _post(url, event, camera, msg, img, title)
    if chat:
        logger.debug(f"[WEBHOOKS] Sending {event.upper()} event for {camera} to {chat_service(chat)}")
        sent = _post_json(chat, chat_payload(chat_service(chat), title, msg, img)) or sent
    return sent


def announce_update() -> None:
    """Send one alert after the bridge starts on a different version, from the update button or overnight."""
    seen = Path(TOKEN_PATH, "last_version")
    try:
        last = seen.read_text().strip() if seen.exists() else ""
        seen.write_text(VERSION)
    except OSError:
        return
    if not last or last == VERSION:
        return
    msg = f"Now running v{VERSION} (was v{last})."
    try:
        body = requests.get(f"https://api.github.com/repos/itskiwi/Wyze-Tunnel/releases/tags/v{VERSION}", timeout=10).json()["body"]
        # GitHub's generated notes are "* Title by @user in https://…/pull/1"; keep the titles.
        notes = [re.sub(r" by @\S+.*$| in https:\S+$", "", line[2:]) for line in body.splitlines() if line.startswith("* ")]
        msg += "".join(f"\n• {n}" for n in notes[:3])
    except Exception:
        pass
    send_webhook("update", "Wyze Tunnel", msg)


def _post_json(url: str, payload: dict) -> bool:
    try:
        requests.post(url, json=payload, timeout=10).raise_for_status()
        return True
    except Exception as ex:
        logger.error(f"[WEBHOOKS] {redact_url(str(ex))}")
        return False


def redact_url(text: str) -> str:
    """Webhook links are secrets; keep them out of the logs."""
    return re.sub(r"https://\S+", "https://…", text)


def _post(url: str, event: str, camera: str, msg: str, img: Optional[str], title: str) -> bool:
    header = {
        "user-agent": f"wyzebridge/{VERSION}",
        "X-Title": title,
        "X-Attach": img,
        "X-Tags": f"{camera},{event}",
        "X-Camera": camera,
        "X-Event": event,
    }

    logger.debug(f"[WEBHOOKS] 📲 Triggering {event.upper()} event for {camera}")
    try:
        resp = requests.post(url, headers=header, data=msg.encode(), timeout=10)
        resp.raise_for_status()
        return True
    except Exception as ex:
        logger.error(f"[WEBHOOKS] {redact_url(str(ex))}")
        return False
