"""Street Watch: neighbors ask for footage through a link you gave them, and you approve each clip by hand.

Nothing leaves this box except a clip you chose, through an expiring share link (see shares.py).
"""
import json
import secrets
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

from wyzebridge import shares
from wyzebridge.config import TOKEN_PATH
from wyzebridge.logging import logger
from wyzebridge.privacy import load_privacy
from wyzebridge.webhooks import in_quiet_hours

STREET_FILE = TOKEN_PATH + "street_watch.json"
MAX_WINDOW = 3600  # a request covers at most 1 hour
LOOKBACK_DAYS = 7
MAX_OPEN = 3  # open requests per neighbor
KEEP_DAYS = 30
SUBMITS_PER_HOUR = 10  # per visitor address, across all links
_submits: dict[str, list[float]] = {}


def _load() -> dict:
    try:
        with open(STREET_FILE) as f:
            data = json.load(f)
    except (FileNotFoundError, ValueError):
        data = {}
    cutoff = time.time() - KEEP_DAYS * 86400
    return {
        "enabled": data.get("enabled", False),
        "links": data.get("links", {}),
        "requests": [r for r in data.get("requests", []) if r["created"] > cutoff],
        "log": [e for e in data.get("log", []) if e["ts"] > cutoff],
    }


def _save(data: dict, event: str) -> None:
    data["log"].append({"ts": int(time.time()), "text": event})
    with open(STREET_FILE, "w") as f:
        json.dump(data, f)
    logger.info(f"[STREET] {event}")


def _parse(value) -> datetime:
    try:
        when = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("Pick a date and time.") from None
    if not when.tzinfo:
        raise ValueError("Pick a date and time.")
    return when


def state() -> dict:
    """Everything the owner's page shows, with share links and view counts."""
    data = _load()
    for req in data["requests"]:
        share = shares.get_share(req.get("share", ""))
        req["views"] = share.get("views", 0) if share else 0
        req["live"] = bool(share)
    return data


def set_enabled(on: bool) -> None:
    """Turning it off ends every request link, every request, and every clip link it made."""
    data = _load()
    if not on:
        for req in data["requests"]:
            shares.remove_share(req.get("share", ""))
        data["links"], data["requests"] = {}, []
    data["enabled"] = bool(on)
    _save(data, "Turned Street Watch " + ("on" if on else "off, ending every link"))


def add_link(name: str) -> str:
    """A request link for one named neighbor. Returns its token. Raises ValueError with a message for the user."""
    data = _load()
    if not data["enabled"]:
        raise ValueError("Turn Street Watch on first.")
    name = str(name or "").strip()
    if not name or len(name) > 32:
        raise ValueError("Pick a name up to 32 characters.")
    token = secrets.token_urlsafe(18)
    data["links"][token] = {"name": name, "created": int(time.time())}
    _save(data, f"Made a request link for {name}")
    return token


def end_link(token: str) -> None:
    data = _load()
    if link := data["links"].pop(token, None):
        data["requests"] = [r for r in data["requests"] if r["link"] != token or r["status"] != "open"]
        _save(data, f"Ended {link['name']}'s request link")


def link_name(token: str) -> Optional[str]:
    data = _load()
    return data["enabled"] and (link := data["links"].get(token)) and link["name"] or None


def submit(token: str, form: dict, visitor: str) -> str:
    """Save a neighbor's request and return their name. Raises ValueError with a message for the neighbor."""
    now = time.time()
    recent = _submits[visitor] = [t for t in _submits.get(visitor, []) if t > now - 3600]
    if len(recent) >= SUBMITS_PER_HOUR:
        raise ValueError("Too many requests. Try again in an hour.")
    data = _load()
    if not data["enabled"] or not (link := data["links"].get(token)):
        raise LookupError
    what, contact = str(form.get("what") or "").strip(), str(form.get("contact") or "").strip()
    if not what or len(what) > 500:
        raise ValueError("Say what happened, in up to 500 characters.")
    if not contact or len(contact) > 200:
        raise ValueError("Say how to reach you, in up to 200 characters.")
    start, end = _parse(form.get("start")), _parse(form.get("end"))
    if not 0 < (end - start).total_seconds() <= MAX_WINDOW:
        raise ValueError("Pick a window of up to 1 hour, with the end after the start.")
    utcnow = datetime.now(timezone.utc)
    if start < utcnow - timedelta(days=LOOKBACK_DAYS) or end > utcnow + timedelta(minutes=5):
        raise ValueError(f"Pick a time in the last {LOOKBACK_DAYS} days.")
    if sum(r["link"] == token and r["status"] == "open" for r in data["requests"]) >= MAX_OPEN:
        raise ValueError(f"You already have {MAX_OPEN} requests waiting. Wait for an answer first.")
    recent.append(now)
    data["requests"].append({
        "id": secrets.token_hex(6), "link": token, "name": link["name"], "what": what, "contact": contact,
        "start": start.isoformat(), "end": end.isoformat(), "created": int(now), "status": "open",
    })
    _save(data, f"{link['name']} asked for footage")
    return link["name"]


def _find(data: dict, req_id: str) -> dict:
    for req in data["requests"]:
        if req["id"] == req_id:
            return req
    raise ValueError("That request is gone.")


def approve(req_id: str, cam: str, mac: str, start: str, duration, sound: bool = False) -> str:
    """Share one clip for a request and return its share token. Raises ValueError with a message for the owner."""
    data = _load()
    req = _find(data, req_id)
    if req["status"] != "open":
        raise ValueError("You already answered this request.")
    private = load_privacy()
    if mac in private.get("cams", []):
        window = {"quiet_start": private.get("start"), "quiet_end": private.get("end")}
        first = _parse(start).astimezone()  # the privacy schedule is in this box's local time
        try:
            minutes = range(int(float(duration) // 60) + 1)
        except (TypeError, ValueError):
            minutes = range(1)
        if any(in_quiet_hours(window, (first + timedelta(minutes=m)).time()) for m in minutes):
            raise ValueError("That clip is in this camera's privacy schedule, so it can't be shared.")
    token = shares.create_share(cam, start, duration, street=True, sound=bool(sound))
    req.update(status="approved", share=token, cam=cam)
    _save(data, f"Shared a {cam} clip with {req['name']}" + (" with sound" if sound else ""))
    return token


def decline(req_id: str) -> None:
    data = _load()
    req = _find(data, req_id)
    if req["status"] == "open":
        req["status"] = "declined"
        _save(data, f"Declined {req['name']}'s request")


def revoke(req_id: str) -> None:
    data = _load()
    req = _find(data, req_id)
    if shares.remove_share(req.get("share", "")):
        _save(data, f"Ended the clip link shared with {req['name']}")
