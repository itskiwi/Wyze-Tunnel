"""Other apps: API tokens that can view or control, and signed webhooks to any URL. Set up on the Apps page, owner only."""
import hashlib
import hmac
import json
import re
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock, Thread
from typing import Optional
from urllib.parse import urlparse

import requests
from wyzebridge.config import TOKEN_PATH, VERSION
from wyzebridge.logging import logger
from wyzebridge.mtx_server import MtxInterface

APPS_FILE = TOKEN_PATH + "apps.json"
SCOPES = ("view", "control")
HOOK_EVENTS = ("motion", "person", "offline", "street", "update", "smoke", "co", "glass")
MAX_TOKENS, MAX_HOOKS = 20, 10
# The stream server's read-only login for apps. A new key each time a token is removed.
STREAM_USER = "apps"
TOKEN_PREFIX = "wt_"
_lock = Lock()


def _load() -> dict:
    try:
        with open(APPS_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return {"tokens": [], "hooks": []}


def _save(data: dict) -> None:
    # Holds webhook signing secrets, so only the bridge's user can read it.
    path = Path(APPS_FILE)
    path.touch(mode=0o600)
    path.write_text(json.dumps(data))


def _hash(token: str) -> str:
    # Tokens are 256-bit random, so a plain hash is enough (no salt or slow hash needed).
    return hashlib.sha256(token.encode()).hexdigest()


def _name(name) -> str:
    name = str(name or "").strip()
    if not name or len(name) > 32:
        raise ValueError("Give it a name up to 32 characters, like Home Assistant.")
    return name


def add_token(name, scope) -> str:
    """Save a new token and return it. It's only ever shown this once. Replaces a token with the same name."""
    name = _name(name)
    if scope not in SCOPES:
        raise ValueError("Pick view only or control.")
    with _lock:
        data = _load()
        if replaced := [t for t in data["tokens"] if t["name"] == name]:
            data["tokens"] = [t for t in data["tokens"] if t["name"] != name]
            data["stream_key"] = secrets.token_urlsafe(24)
        if len(data["tokens"]) >= MAX_TOKENS:
            raise ValueError(f"You can have up to {MAX_TOKENS} apps. Remove one you don't use first.")
        token = TOKEN_PREFIX + secrets.token_urlsafe(32)
        data["tokens"].append(
            {
                "id": secrets.token_hex(4),
                "name": name,
                "scope": scope,
                "hash": _hash(token),
                "hint": token[-4:],
                "created": int(time.time()),
                "last_used": 0,
            }
        )
        data.setdefault("stream_key", secrets.token_urlsafe(24))
        _save(data)
    sync_stream_auth(data)
    logger.info(f"[APPS] {'Replaced' if replaced else 'Added'} {scope} token for {name!r}")
    return token


def list_tokens() -> list[dict]:
    return [{k: v for k, v in t.items() if k != "hash"} for t in _load()["tokens"]]


def remove_token(token_id: str) -> None:
    with _lock:
        data = _load()
        data["tokens"] = [t for t in data["tokens"] if t["id"] != token_id]
        # Saved stream links stop working too.
        data["stream_key"] = secrets.token_urlsafe(24)
        _save(data)
    sync_stream_auth(data)
    logger.info(f"[APPS] Removed token {token_id}")


def check_token(token: str) -> Optional[dict]:
    """The token's entry if it's one of ours."""
    if not token.startswith(TOKEN_PREFIX):
        return None
    digest = _hash(token)
    data = _load()
    for entry in data["tokens"]:
        if hmac.compare_digest(entry["hash"], digest):
            if time.time() - entry.get("last_used", 0) > 60:  # don't write the file on every request
                with _lock:
                    data = _load()
                    for t in data["tokens"]:
                        if t["id"] == entry["id"]:
                            t["last_used"] = int(time.time())
                    _save(data)
            return entry
    return None


def stream_key() -> str:
    return _load().get("stream_key", "")


def sync_stream_auth(data: Optional[dict] = None) -> None:
    """Give the stream server a read-only login for apps, only while there are tokens."""
    data = data or _load()
    try:
        with MtxInterface() as mtx:
            users = [u for u in mtx.get("authInternalUsers") or [] if u.get("user") != STREAM_USER]
            if data["tokens"] and data.get("stream_key"):
                users.append({"user": STREAM_USER, "pass": data["stream_key"], "permissions": [{"action": "read"}]})
            mtx.set("authInternalUsers", users)
    except OSError as ex:
        logger.error(f"[APPS] Couldn't update the stream server login: {ex}")


def add_hook(url, events) -> str:
    """Save a webhook and return its signing secret. It's only ever shown this once."""
    url = str(url or "").strip()
    parts = urlparse(url)
    if parts.scheme not in ("http", "https") or not parts.hostname or len(url) > 500 or re.search(r"\s", url):
        raise ValueError("Paste the full link your app gave you, starting with https:// or http://")
    events = [e for e in HOOK_EVENTS if e in (events or [])]
    if not events:
        raise ValueError("Pick at least one thing to send.")
    with _lock:
        data = _load()
        if len(data["hooks"]) >= MAX_HOOKS:
            raise ValueError(f"You can have up to {MAX_HOOKS} webhooks. Remove one first.")
        secret = "whsec_" + secrets.token_urlsafe(24)
        data["hooks"].append(
            {"id": secrets.token_hex(4), "url": url, "events": events, "secret": secret, "created": int(time.time())}
        )
        _save(data)
    logger.info(f"[APPS] Added webhook to {parts.hostname}")
    return secret


def list_hooks() -> list[dict]:
    return [{k: v for k, v in h.items() if k != "secret"} for h in _load()["hooks"]]


def remove_hook(hook_id: str) -> None:
    with _lock:
        data = _load()
        data["hooks"] = [h for h in data["hooks"] if h["id"] != hook_id]
        _save(data)


def sign(secret: str, timestamp: str, body: bytes) -> str:
    """What X-Wyze-Tunnel-Signature holds: HMAC-SHA256 of "<timestamp>.<body>"."""
    return "sha256=" + hmac.new(secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256).hexdigest()


def payload(event: str, camera: str, msg: str, img: Optional[str] = None) -> dict:
    return {
        "id": secrets.token_hex(8),
        "event": event,
        "camera": camera,
        "message": msg,
        "image_url": img,
        "time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "version": VERSION,
    }


def dispatch(event: str, camera: str, msg: str, img: Optional[str] = None) -> None:
    """Send an event to every webhook that wants it, without waiting. Quiet hours don't apply: these are for automations."""
    body = payload(event, camera, msg, img)
    for hook in _load()["hooks"]:
        if event in hook["events"]:
            Thread(target=deliver, args=(hook, body), daemon=True).start()


def test_hook(hook_id: str) -> dict:
    if not (hook := next((h for h in _load()["hooks"] if h["id"] == hook_id), None)):
        raise ValueError("That webhook is gone.")
    return deliver(hook, payload("test", "Wyze Tunnel", "Test from Wyze Tunnel. Your webhook works!"))


def deliver(hook: dict, body: dict) -> dict:
    raw = json.dumps(body).encode()
    ts = str(int(time.time()))
    headers = {
        "Content-Type": "application/json",
        "User-Agent": f"WyzeTunnel/{VERSION}",
        "X-Wyze-Tunnel-Event": body["event"],
        "X-Wyze-Tunnel-Timestamp": ts,
        "X-Wyze-Tunnel-Signature": sign(hook["secret"], ts, raw),
    }
    try:
        # No redirects: a signed event goes only where the owner pointed it.
        resp = requests.post(hook["url"], data=raw, headers=headers, timeout=5, allow_redirects=False)
        result = {"ok": resp.ok, "status": f"{resp.status_code} {resp.reason or ''}".strip()}
    except requests.RequestException as ex:
        result = {"ok": False, "status": type(ex).__name__.replace("Error", " error").strip()}
    result["at"] = int(time.time())
    with _lock:
        data = _load()
        for h in data["hooks"]:
            if h["id"] == hook["id"]:
                h["last"] = result
        _save(data)
    if not result["ok"]:
        logger.warning(f"[APPS] Webhook to {urlparse(hook['url']).hostname} failed: {result['status']}")
    return result
