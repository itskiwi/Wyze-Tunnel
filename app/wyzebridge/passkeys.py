"""Passkey sign-in for the owner (py_webauthn). Browsers only allow passkeys over https (the Tailscale address) or on localhost."""

import hashlib
import hmac
import json
import secrets
import time
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import webauthn
from flask import request
from webauthn.helpers import base64url_to_bytes, bytes_to_base64url
from webauthn.helpers.exceptions import InvalidAuthenticationResponse, InvalidRegistrationResponse
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)
from wyzebridge import vault
from wyzebridge.config import TOKEN_PATH

# Public keys plus the secret that signs sign-in cookies; sealed like the other login files.
FILE = Path(TOKEN_PATH, "passkeys.json")
COOKIE = "wt_passkey"
COOKIE_SECONDS = 30 * 86400
CHALLENGE_SECONDS = 300

_challenges: dict[str, float] = {}


def _load() -> dict:
    try:
        return json.loads(vault.read_text(FILE))
    except (FileNotFoundError, ValueError):
        return {"secret": secrets.token_hex(32), "keys": []}


def _save(data: dict) -> None:
    vault.write_text(FILE, json.dumps(data))


def list_keys() -> list[dict]:
    return [{k: key[k] for k in ("id", "name", "host", "added", "used")} for key in _load()["keys"]]


def origin() -> str:
    """The address the browser is on. Wyze Tunnel's Tailscale proxy says when that was https."""
    scheme = "https" if request.headers.get("X-Forwarded-Proto") == "https" else request.scheme
    return f"{scheme}://{request.host}"


def rp_id() -> str:
    return urlparse(origin()).hostname or ""


def secure() -> bool:
    return origin().startswith("https://") or rp_id() == "localhost"


def offered() -> bool:
    """A passkey was added for this address, so the sign-in page can offer it before the password box."""
    return secure() and any(key["host"] == rp_id() for key in _load()["keys"])


def _new_challenge(options) -> str:
    now = time.time()
    for c in [c for c, t in _challenges.items() if t < now]:
        del _challenges[c]
    if len(_challenges) >= 100:  # anyone may ask for sign-in options, so keep the list short
        del _challenges[next(iter(_challenges))]
    _challenges[bytes_to_base64url(options.challenge)] = now + CHALLENGE_SECONDS
    return webauthn.options_to_json(options)


def _take_challenge(credential: dict) -> bytes:
    """The challenge this browser signed, used up so it can't be replayed."""
    try:
        client = json.loads(base64url_to_bytes(credential["response"]["clientDataJSON"]))
        challenge = client["challenge"]
    except (KeyError, TypeError, ValueError):
        raise ValueError("That passkey answer wasn't readable. Try again.")
    if _challenges.pop(challenge, 0) < time.time():
        raise ValueError("That took too long. Try again.")
    return base64url_to_bytes(challenge)


def register_options(username: str) -> str:
    if not secure():
        raise ValueError("Passkeys only work over https. Open Wyze Tunnel at its Tailscale https address.")
    data = _load()
    options = webauthn.generate_registration_options(
        rp_id=rp_id(),
        rp_name="Wyze Tunnel",
        user_name=username,
        # One owner per install: a new passkey on the same device replaces the old one instead of piling up.
        user_id=hashlib.sha256(f"owner:{data['secret']}".encode()).digest()[:16],
        exclude_credentials=[PublicKeyCredentialDescriptor(id=base64url_to_bytes(k["id"])) for k in data["keys"]],
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.REQUIRED, user_verification=UserVerificationRequirement.REQUIRED
        ),
    )
    return _new_challenge(options)


def register(credential: dict, name: str) -> dict:
    try:
        verified = webauthn.verify_registration_response(
            credential=credential,
            expected_challenge=_take_challenge(credential),
            expected_rp_id=rp_id(),
            expected_origin=origin(),
            require_user_verification=True,
        )
    except InvalidRegistrationResponse as ex:
        raise ValueError(f"That passkey couldn't be added: {ex}")
    data = _load()
    key = {
        "id": bytes_to_base64url(verified.credential_id),
        "public_key": bytes_to_base64url(verified.credential_public_key),
        "sign_count": verified.sign_count,
        "host": rp_id(),
        "origin": origin(),
        "name": (name or "").strip()[:40] or "Passkey",
        "added": int(time.time()),
        "used": 0,
    }
    data["keys"] = [k for k in data["keys"] if k["id"] != key["id"]] + [key]
    _save(data)
    return {k: key[k] for k in ("id", "name", "host", "added", "used")}


def remove(key_id: str) -> bool:
    data = _load()
    keys = [k for k in data["keys"] if k["id"] != key_id]
    if len(keys) == len(data["keys"]):
        return False
    data["keys"] = keys
    _save(data)
    return True


def sign_in_options() -> str:
    # No allow list: the device offers its own passkey, and strangers don't learn which ones exist.
    return _new_challenge(webauthn.generate_authentication_options(rp_id=rp_id(), user_verification=UserVerificationRequirement.REQUIRED))


def sign_in(credential: dict) -> str:
    """Check the passkey and return the sign-in cookie's value."""
    data = _load()
    key = next((k for k in data["keys"] if k["id"] == (credential or {}).get("rawId")), None)
    challenge = _take_challenge(credential)
    if not key:
        raise ValueError("That passkey isn't on this Wyze Tunnel. Sign in with your password.")
    try:
        # The address and site name saved with the key, never this request's headers.
        verified = webauthn.verify_authentication_response(
            credential=credential,
            expected_challenge=challenge,
            expected_rp_id=key["host"],
            expected_origin=key["origin"],
            credential_public_key=base64url_to_bytes(key["public_key"]),
            credential_current_sign_count=key["sign_count"],
            require_user_verification=True,
        )
    except InvalidAuthenticationResponse as ex:
        raise ValueError(f"That passkey didn't work: {ex}")
    key["sign_count"], key["used"] = verified.new_sign_count, int(time.time())
    _save(data)
    expires = int(time.time()) + COOKIE_SECONDS
    return f"{key['id']}.{expires}.{_sign(data['secret'], key['id'], expires)}"


def _sign(secret: str, key_id: str, expires) -> str:
    return hmac.new(secret.encode(), f"{key_id}.{expires}".encode(), hashlib.sha256).hexdigest()


def signed_in(cookie: str) -> bool:
    """A sign-in cookie from a passkey that hasn't been removed, and isn't too old."""
    try:
        key_id, expires, sig = (cookie or "").rsplit(".", 2)
        if int(expires) < time.time():
            return False
    except ValueError:
        return False
    data = _load()
    return hmac.compare_digest(sig, _sign(data["secret"], key_id, expires)) and any(k["id"] == key_id for k in data["keys"])


def safe_next(path: Optional[str]) -> str:
    """Only a page on this site, never another website."""
    return path if path and path.startswith("/") and not path.startswith(("//", "/\\")) else "/"
