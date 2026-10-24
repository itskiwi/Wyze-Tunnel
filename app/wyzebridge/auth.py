import os
import secrets
from base64 import urlsafe_b64encode
from hashlib import sha256
from pathlib import Path
from typing import Optional

from werkzeug.security import generate_password_hash
from wyzebridge.bridge_utils import env_bool
from wyzebridge.config import TOKEN_PATH
from wyzebridge.logging import logger


def get_secret(name: str, default: str = "") -> str:
    if not name:
        return ""
    try:
        with open(f"/run/secrets/{name.upper()}", "r") as f:
            return f.read().strip("'\" \n\t\r")
    except FileNotFoundError:
        return env_bool(name, default, style="original")


def get_credential(file_name: str) -> str:
    if env_pass := get_secret(file_name):
        return env_pass

    file_path = f"{TOKEN_PATH}{file_name}"
    if os.path.exists(file_path) and os.path.getsize(file_path) > 0:
        with open(file_path, "r") as file:
            logger.info(f"[AUTH] Using {file_name} from {file_path}")
            return file.read().strip()
    return ""


def save_credential(file_name: str, value: str) -> None:
    file_path = Path(TOKEN_PATH, file_name)
    file_path.touch(mode=0o600)
    file_path.write_text(value)


# A login chosen in the web UI, as "username\npassword hash". Wins over WB_USERNAME/WB_PASSWORD.
LOGIN_FILE = Path(TOKEN_PATH, "wb_login")

# Installs from before random credentials already have cached Wyze data; they keep their old ones.
LEGACY_INSTALL: bool = any(Path(TOKEN_PATH).glob("*.pickle"))


def gen_api_key(email):
    hash_bytes = sha256(email.encode()).digest()
    return urlsafe_b64encode(hash_bytes).decode()[:40]


class WbAuth:
    enabled: bool = bool(env_bool("WB_AUTH") if os.getenv("WB_AUTH") else True)
    username: str = get_secret("wb_username", "wbadmin")
    api: str = ""
    _pass: str = get_credential("wb_password")
    _hashed_pass: Optional[str] = None

    @classmethod
    def load_login(cls) -> None:
        if LOGIN_FILE.exists():
            cls.username, _, cls._hashed_pass = LOGIN_FILE.read_text().partition("\n")
            cls._pass = ""

    @classmethod
    def set_login(cls, username: str, password: str) -> None:
        username = username.strip()
        if not username or ":" in username:
            raise ValueError("Pick a username without a colon.")
        if len(password) < 8:
            raise ValueError("Use at least 8 characters for the password.")
        hashed = generate_password_hash(password)
        save_credential(LOGIN_FILE.name, f"{username}\n{hashed}")
        cls.username, cls._pass, cls._hashed_pass = username, "", hashed
        logger.info(f"[AUTH] Web login changed to {username=}")

    @classmethod
    def hashed_password(cls) -> str:
        if cls._hashed_pass:
            return cls._hashed_pass

        cls._hashed_pass = generate_password_hash(cls._pass)
        return cls._hashed_pass

    @classmethod
    def set_email(cls, email: str, force: bool = False):
        logger.info(f"[AUTH] WB_AUTH={cls.enabled}")
        if not cls.enabled:
            return

        cls._update_credentials(email, force)

        logger.info(f"[AUTH] WB_USERNAME={cls.username}")
        if cls._pass:
            logger.info(f"[AUTH] WB_PASSWORD={redact_password(cls._pass)}")
        logger.info(f"[AUTH] WB_API={redact_password(cls.api)}")

    @classmethod
    def _update_credentials(cls, email: str, force: bool = False) -> None:
        if LOGIN_FILE.exists():
            cls.load_login()
        else:
            password = get_credential("wb_password") or cls._new_credential(
                "wb_password", email.partition("@")[0]
            )
            if password != cls._pass:
                cls._pass = password
                cls._hashed_pass = generate_password_hash(password)

        cls.api = get_credential("wb_api") or cls._new_credential(
            "wb_api", gen_api_key(email)
        )

        if cls._pass == email.partition("@")[0] or cls.api == gen_api_key(email):
            logger.warning(
                "[AUTH] WB_PASSWORD or WB_API is still derived from your Wyze email. "
                f"Change the password on the Login page, or set your own in {TOKEN_PATH}wb_password and wb_api."
            )

    @staticmethod
    def _new_credential(file_name: str, legacy: str) -> str:
        if LEGACY_INSTALL:
            value = legacy
        else:
            value = secrets.token_urlsafe(16 if file_name == "wb_password" else 30)
            # Only time the full value is logged; it is saved for later runs.
            logger.warning(f"[AUTH] Generated {file_name.upper()}: {value}")
        save_credential(file_name, value)
        return value


WbAuth.load_login()


def redact_password(password: Optional[str]):
    return f"{password[0]}{'*' * (len(password) - 1)}" if password else "NOT SET"


STREAM_AUTH: str = env_bool("STREAM_AUTH", style="original")
