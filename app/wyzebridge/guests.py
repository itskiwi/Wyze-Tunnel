"""Extra logins: guests who only watch and stop working at a set time, and family members with full access."""
import json
import secrets
import time
from typing import Optional

from werkzeug.security import check_password_hash, generate_password_hash
from wyzebridge.config import TOKEN_PATH
from wyzebridge.logging import logger
from wyzebridge.mtx_server import MtxInterface

GUESTS_FILE = TOKEN_PATH + "guests.json"
GUEST_HOURS = (1, 24, 72, 168, 0)  # 0: a family login, full access, never ends
# The stream server's read-only login for guests' live video. A new key each time a guest goes away.
STREAM_USER = "guest"


def _load() -> dict:
    try:
        with open(GUESTS_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return {"guests": []}


def _save(data: dict) -> None:
    with open(GUESTS_FILE, "w") as f:
        json.dump(data, f)


def _active(guest: dict) -> bool:
    return guest["expires"] == 0 or guest["expires"] > time.time()


def list_guests() -> list[dict]:
    """Active logins without their password hashes: family first, then guests soonest to end."""
    guests = [{"name": g["name"], "expires": g["expires"]} for g in _load()["guests"] if _active(g)]
    return sorted(guests, key=lambda g: g["expires"])


def add_guest(name: str, hours: int, taken: str) -> str:
    """Save a guest and return their new password. Raises ValueError with a message for the user."""
    name = str(name or "").strip()
    if not name or ":" in name or len(name) > 32:
        raise ValueError("Pick a name up to 32 characters, without a colon.")
    if name == taken:
        raise ValueError("That name is your own login. Pick another.")
    if type(hours) is not int or hours not in GUEST_HOURS:
        raise ValueError("Pick guest or family.")
    data = _load()
    data["guests"] = [g for g in data["guests"] if g["name"] != name and _active(g)]
    password = secrets.token_urlsafe(9)
    data["guests"].append(
        {"name": name, "hash": generate_password_hash(password), "expires": hours and int(time.time()) + hours * 3600}
    )
    data.setdefault("stream_key", secrets.token_urlsafe(24))
    _save(data)
    sync_stream_auth(data)
    logger.info(f"[GUEST] Added {name!r} " + (f"for {hours}h" if hours else "as family"))
    return password


def remove_guest(name: str) -> None:
    data = _load()
    data["guests"] = [g for g in data["guests"] if g["name"] != name]
    data["stream_key"] = secrets.token_urlsafe(24)
    _save(data)
    sync_stream_auth(data)
    logger.info(f"[GUEST] Removed guest {name!r}")


def check_guest(username: str, password: str) -> Optional[dict]:
    """The guest or family member, if the login is right and hasn't ended."""
    for g in _load()["guests"]:
        if g["name"] == username and _active(g) and check_password_hash(g["hash"], password):
            return {"guest": g["name"], "expires": g["expires"], "family": g["expires"] == 0}


def stream_key() -> str:
    return _load().get("stream_key", "")


def prune() -> None:
    """Drop ended guests and change the stream key, so a saved video link stops working too."""
    data = _load()
    active = [g for g in data["guests"] if _active(g)]
    if len(active) != len(data["guests"]):
        data["guests"] = active
        data["stream_key"] = secrets.token_urlsafe(24)
        _save(data)
        sync_stream_auth(data)


def sync_stream_auth(data: Optional[dict] = None) -> None:
    """Give the stream server a read-only login for guests, only while there are some."""
    data = data or _load()
    try:
        with MtxInterface() as mtx:
            users = [u for u in mtx.get("authInternalUsers") or [] if u.get("user") != STREAM_USER]
            if data["guests"] and data.get("stream_key"):
                users.append({"user": STREAM_USER, "pass": data["stream_key"], "permissions": [{"action": "read"}]})
            mtx.set("authInternalUsers", users)
    except OSError as ex:
        logger.error(f"[GUEST] Couldn't update the stream server login: {ex}")
