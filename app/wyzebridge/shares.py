"""Links that let anyone watch one recorded clip, without a login, until they end."""
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


def create_share(cam: str, start: str, duration) -> str:
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
    shares[token] = {"cam": cam, "start": start, "duration": duration, "expires": int(now) + SHARE_HOURS * 3600}
    with open(SHARES_FILE, "w") as f:
        json.dump(shares, f)
    return token


def get_share(token: str) -> Optional[dict]:
    share = _load().get(token)
    return share if share and share["expires"] > time.time() else None
