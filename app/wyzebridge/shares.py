"""Links that let anyone watch one recorded clip, without a login, until they end."""
import hashlib
import hmac
import json
import re
import secrets
import time
from typing import Optional

from wyzebridge.config import TOKEN_PATH

SHARES_FILE = TOKEN_PATH + "shares.json"
SHARE_HOURS = 24
MAX_SECONDS = 600


def _load() -> dict:
    try:
        with open(SHARES_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return {}


def _save(shares: dict) -> None:
    with open(SHARES_FILE, "w") as f:
        json.dump(shares, f)


def create_share(cam: str, start: str, duration, **extra) -> str:
    """Save a share and return its token. Raises ValueError with a message for the user."""
    if not re.fullmatch(r"\d{4}-\d\d-\d\dT[\d:.]+(Z|[+-]\d\d:\d\d)", str(start)):
        raise ValueError("Pick a clip to share.")
    try:
        duration = float(duration)
    except (TypeError, ValueError):
        raise ValueError("Pick a clip to share.") from None
    if not 0 < duration <= MAX_SECONDS:
        raise ValueError("Clips can be up to 10 minutes.")
    now = time.time()
    shares = {t: s for t, s in _load().items() if s["expires"] > now}
    token = secrets.token_urlsafe(18)
    shares[token] = {"cam": cam, "start": start, "duration": duration, "expires": int(now) + SHARE_HOURS * 3600, **extra}
    _save(shares)
    return token


def get_share(token: str) -> Optional[dict]:
    share = _load().get(token)
    return share if share and share["expires"] > time.time() else None


def new_password() -> tuple[str, str]:
    """A password for a link, and the hash to keep. Random, so a plain hash is enough (and fast for every video range)."""
    password = secrets.token_urlsafe(6)
    return password, hashlib.sha256(password.encode()).hexdigest()


def unlocks(share: dict, password: str) -> bool:
    want = share.get("pw_hash")
    return not want or hmac.compare_digest(want, hashlib.sha256(password.encode()).hexdigest())


def count_view(token: str) -> None:
    shares = _load()
    if token in shares:
        shares[token]["views"] = shares[token].get("views", 0) + 1
        _save(shares)


def remove_share(token: str) -> bool:
    shares = _load()
    if shares.pop(token, None) is None:
        return False
    _save(shares)
    return True
