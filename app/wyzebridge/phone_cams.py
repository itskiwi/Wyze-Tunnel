"""Spare phones and laptops as cameras: the browser sends its camera to MediaMTX over WebRTC (WHIP).
Set up on the Phone cameras page, owner only. Each phone gets its own secret link."""
import hmac
import json
import secrets
from pathlib import Path
from threading import Lock
from typing import Optional

from wyzebridge.config import TOKEN_PATH
from wyzebridge.logging import logger
from wyzecam.api_models import clean_name

PHONES_FILE = TOKEN_PATH + "phone_cams.json"
MAX_PHONES = 10
_lock = Lock()


def _load() -> list[dict]:
    try:
        with open(PHONES_FILE) as f:
            return json.load(f)["cams"]
    except (FileNotFoundError, ValueError, KeyError):
        return []


def _save(cams: list[dict]) -> None:
    # Holds each phone's link secret, so only the bridge's user can read it.
    path = Path(PHONES_FILE)
    path.touch(mode=0o600)
    path.write_text(json.dumps({"cams": cams}))


def list_cams() -> list[dict]:
    """[{"uri", "name", "token"}] in the order they were added."""
    return _load()


def uris() -> list[str]:
    return [c["uri"] for c in _load()]


def add(name, taken) -> dict:
    """Save a new phone camera and return it. `taken` holds the stream names already in use."""
    name = str(name or "").strip()
    if not name or len(name) > 32:
        raise ValueError("Give it a name up to 32 characters, like Nursery.")
    uri = "phone-" + clean_name(name, "-").lower()
    if uri == "phone-":
        raise ValueError("Use some letters or numbers in the name.")
    with _lock:
        cams = _load()
        if uri in taken or any(c["uri"] == uri for c in cams):
            raise ValueError("You already have a camera with that name. Pick another.")
        if len(cams) >= MAX_PHONES:
            raise ValueError(f"You can have up to {MAX_PHONES} phone cameras. Remove one first.")
        cam = {"uri": uri, "name": name, "token": secrets.token_urlsafe(24)}
        _save(cams + [cam])
    logger.info(f"[PHONE] Added phone camera /{uri}")
    return cam


def remove(uri: str) -> None:
    with _lock:
        _save([c for c in _load() if c["uri"] != uri])
    logger.info(f"[PHONE] Removed phone camera /{uri}")


def by_token(token: str) -> Optional[dict]:
    # Bytes: compare_digest refuses str with non-ASCII, and the token comes straight from the link.
    return next((c for c in _load() if hmac.compare_digest(c["token"].encode(), token.encode())), None)
