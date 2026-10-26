import json
import os
import shutil
from pathlib import Path
from time import sleep, time
from types import SimpleNamespace
from typing import Callable, Generator, Optional
from urllib.parse import urlparse

from flask import g, request
from flask import url_for as _url_for
from flask_httpauth import HTTPBasicAuth
from werkzeug.security import check_password_hash
from wyzebridge import config
from wyzebridge import guests, integrations, phrases, pocket, tamper
from wyzebridge.auth import WbAuth
from wyzebridge.bridge_utils import env_bool
from wyzebridge.logging import logger
from wyzebridge.stream import Stream, StreamManager
from wyzebridge.wyze_commands import OWNER_CMDS

auth = HTTPBasicAuth()

API_ENDPOINTS = "/api", "/img", "/snapshot", "/thumb", "/photo", "/recordings", "/detections", "/metrics.prom", "/signaling"
# What a guest login can open: the camera grid and live video, nothing that changes anything.
GUEST_PATHS = "/hls/", "/img/", "/snapshot/", "/thumb/", "/signaling/", "/webrtc/", "/api/sse_status"


@auth.verify_password
def verify_password(username, password):
    if pocket.public_request():
        # The no-app away link is on the internet: only phones paired by QR code get in, never a typed password.
        username, password = pocket.from_cookie(request.cookies.get(pocket.COOKIE, ""))
        return bool(WbAuth.enabled and username) and guest_login(username, password)
    if token := bearer_token():
        return verify_app_token(token)
    if config.HASS_TOKEN and request.remote_addr == "172.30.32.2":
        return True
    if WbAuth.api and WbAuth.api in (request.args.get("api"), request.headers.get("api")):
        return request.path.startswith(API_ENDPOINTS)
    if username == WbAuth.username:
        return check_password_hash(WbAuth.hashed_password(), password)
    if not username and (paired := request.cookies.get(pocket.COOKIE)):
        username, password = pocket.from_cookie(paired)
    if WbAuth.enabled and username:
        return guest_login(username, password)
    return WbAuth.enabled == False


def guest_login(username: str, password: str):
    if not (guest := guests.check_guest(username, password)):
        return False
    if guest["family"] or request.method == "GET" and (request.path == "/" or request.path.startswith(GUEST_PATHS)):
        return guest
    g.guest_denied = True
    return False


def bearer_token() -> str:
    scheme, _, token = request.headers.get("Authorization", "").partition(" ")
    return token.strip() if scheme.lower() == "bearer" else ""


def verify_app_token(token: str):
    """Apps only reach /api/v1. View-only tokens can read; MCP checks each tool itself."""
    if not (app := integrations.check_token(token)):
        return False
    if not request.path.startswith("/api/v1/"):
        g.app_denied = "App tokens only work with /api/v1."
    elif app["scope"] == "control" or request.method == "GET" or request.path == "/api/v1/mcp":
        return SimpleNamespace(app=app["name"], scope=app["scope"])
    else:
        g.app_denied = "This token can only view. Make a control token on the Apps page to change things."
    return False


def api_scope() -> str:
    """control for the owner, family and control tokens; view for view-only tokens."""
    user = auth.current_user()
    return user.scope if isinstance(user, SimpleNamespace) else "control"


def owner_password() -> bool:
    """Signed in with the owner's own password: not the API key, Home Assistant, an app token, a guest or family."""
    creds = request.authorization
    return bool(WbAuth.enabled and creds and creds.username == WbAuth.username and check_password_hash(WbAuth.hashed_password(), creds.password or ""))


def current_guest() -> Optional[dict]:
    """The guest or family member viewing this page, or None for the owner."""
    user = auth.current_user()
    return user if isinstance(user, dict) else None


def owner_cmd_denied(cmd: str, value) -> bool:
    """True for a guest or family member trying to change a camera setting (reads are fine)."""
    return cmd in OWNER_CMDS and value not in ("", None) and current_guest() is not None


def cross_site_request() -> bool:
    """True when a browser sent this request from another website (CSRF)."""
    if site := request.headers.get("Sec-Fetch-Site"):
        return site not in ("same-origin", "none")
    # Older browsers: compare the Origin header with the host they were talking to.
    if origin := request.headers.get("Origin"):
        host = request.headers.get("X-Forwarded-Host") or request.host
        return urlparse(origin).netloc != host
    return False


@auth.error_handler
def unauthorized():
    if g.get("app_denied"):
        return {"error": g.app_denied}, 403
    if g.get("guest_denied"):
        # 403, not 401, so the browser doesn't ask a guest to log in again.
        return {"error": "Guests can only watch."}, 403
    return {"error": "Unauthorized"}, 401


def url_for(endpoint, **values):
    proxy = (
        request.headers.get("X-Ingress-Path")
        or request.headers.get("X-Forwarded-Prefix")
        or ""
    ).rstrip("/")
    return proxy + _url_for(endpoint, **values)


def sse_generator(sse_status: Callable) -> Generator[str, str, str]:
    """Generator to return the status for enabled cameras."""
    cameras = {}
    while True:
        if cameras != (cameras := sse_status()):
            yield f"data: {json.dumps(cameras)}\n\n"
        sleep(1)


def sse_status(streams: StreamManager) -> dict:
    """Live status per camera, plus the plain-language reason when one is known."""
    return {
        uri: cam | {"reason": getattr(streams.streams.get(uri), "reason", None)}
        for uri, cam in streams.get_sse_status().items()
    }


def get_webrtc_signal(cam_name: str, api_key: str, user: str = "wb") -> dict:
    """Generate signaling for MediaMTX webrtc."""
    hostname = env_bool("DOMAIN", urlparse(request.root_url).hostname or "localhost")
    ssl = "s" if env_bool("MTX_WEBRTCENCRYPTION") else ""
    webrtc = config.WEBRTC_URL.lstrip("http") or f"{ssl}://{hostname}:8889"
    wep = {"result": "ok", "cam": cam_name, "whep": f"http{webrtc}/{cam_name}/whep"}

    if ice_server := validate_ice(env_bool("MTX_WEBRTCICESERVERS")):
        return wep | {"servers": ice_server}

    ice_server = {
        "credentialType": "password",
        "urls": ["stun:stun.l.google.com:19302"],
    }
    if api_key:
        ice_server |= {
            "username": user,
            "credential": api_key,
            "credentialType": "password",
        }
    return wep | {"servers": [ice_server]}


def validate_ice(data: str) -> Optional[list[dict]]:
    if not data:
        return
    try:
        json_data = json.loads(data)
        if "urls" in json_data:
            return [json_data]
    except ValueError:
        return


def format_stream(name_uri: str) -> dict:
    """
    Format stream with hostname.

    Parameters:
    - name_uri (str): camera name.

    Returns:
    - dict: Can be merged with camera info.
    """
    hostname = env_bool("DOMAIN", urlparse(request.root_url).hostname or "localhost")
    img = f"{name_uri}.{env_bool('IMG_TYPE','jpg')}"
    try:
        img_time = int(os.path.getmtime(config.IMG_PATH + img) * 1000)
    except FileNotFoundError:
        img_time = None

    webrtc_url = (config.WEBRTC_URL or f"http://{hostname}:8889") + f"/{name_uri}/"
    data = {
        "hls_url": (config.HLS_URL or f"http://{hostname}:8888") + f"/{name_uri}/",
        "hls_page_url": config.HLS_URL and f"{config.HLS_URL}/{name_uri}/" or f"hls/{name_uri}/",
        "webrtc_url": webrtc_url if config.BRIDGE_IP else None,
        "rtmp_url": (config.RTMP_URL or f"rtmp://{hostname}:1935") + f"/{name_uri}",
        "rtsp_url": (config.RTSP_URL or f"rtsp://{hostname}:8554") + f"/{name_uri}",
        "img_url": f"img/{img}" if img_time else None,
        "snapshot_url": f"snapshot/{img}",
        "thumbnail_url": f"thumb/{img}",
        "img_time": img_time,
    }
    if config.LLHLS:
        data["hls_url"] = data["hls_url"].replace("http:", "https:")
    return data


def format_streams(cams: dict) -> dict[str, dict]:
    """
    Format info for multiple streams with hostname.

    Parameters:
    - cams (dict): get_all_cam_info from StreamManager.

    Returns:
    - dict: cam info with hostname.
    """
    places = phrases.load_places()
    return {
        uri: cam | format_stream(uri) | {"outdoor": (places.get(uri) or phrases.guess(cam.get("nickname") or uri)) in phrases.OUTDOOR}
        for uri, cam in cams.items()
    }


def all_cams(streams: StreamManager, total: int) -> dict:
    return {
        "total": total,
        "available": streams.total,
        "enabled": streams.active,
        "cameras": format_streams(streams.get_all_cam_info()),
    }


def _int(value, default: int = -1) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def setup_steps(cams: dict[str, dict]) -> list[dict]:
    """The first-week checklist on Home: what's set up and where to do the rest."""
    from wyzebridge import backup, recording, webhooks

    rec = recording.load_settings()
    return [
        {"title": "Watch a camera", "href": "live", "done": any(c.get("connected") for c in cams.values())},
        {"title": "Get alerts on your phone", "href": "alerts", "done": bool(webhooks.ALERTS.get("topic") or webhooks.ALERTS.get("chat_url"))},
        {"title": "Record a camera", "href": "recordings#setup", "done": bool(rec.get("all") or rec.get("cams"))},
        {"title": "Pair your phone", "href": "account#phone", "done": any(g["expires"] == 0 for g in guests.list_guests())},
        {"title": "Back up your settings every day", "href": "account#backup", "done": bool(backup.load_auto().get("folder"))},
    ]

# Kept by "Start over": the web login, the downloaded detection models, finished timelapse videos and the event archive.
KEEP_ON_START_OVER = {"wb_login", "wb_password", "wb_api", "models", "timelapse", "event_archive"}


def start_over(token_path: str, img_path: str, record_root: Path) -> str:
    """Save a settings backup, then clear every setting and cached picture. Never touches recordings. Returns the backup's path."""
    from wyzebridge import backup

    saved = Path(token_path, f"settings-before-start-over-{int(time())}.json")
    saved.write_text(json.dumps(backup.export_settings()))  # raises before anything is deleted
    rec = Path(record_root).resolve()
    keep = KEEP_ON_START_OVER | {saved.name}
    for folder in (token_path, img_path):
        if not os.path.isdir(folder):  # the picture folder can sit inside the settings folder
            continue
        for path in Path(folder).iterdir():
            real = path.resolve()
            if path.name in keep or real == rec or rec in real.parents or real in rec.parents:
                continue
            try:
                shutil.rmtree(path) if path.is_dir() and not path.is_symlink() else path.unlink()
            except OSError as ex:
                logger.warning(f"[START OVER] Couldn't delete {path}: {ex}")
    os.makedirs(img_path, exist_ok=True)
    return str(saved)


def bridge_issues(cams: dict[str, dict]) -> list[dict]:
    """Problems worth fixing, by camera. Feeds /api/health and the Health page."""
    issues = []
    for uri, cam in cams.items():
        info = cam.get("camera_info") or {}
        wifi = _int((info.get("basicInfo") or {}).get("wifidb"))
        sd = info.get("sdParm") or {}
        checks = {
            "offline": cam.get("status") == -90,
            "repeated_failures": cam.get("chronic"),
            "weak_wifi": 0 <= wifi < 40,
            "sd_card_full": sd.get("status") == "1" and _int(sd.get("capacity")) > 0
            and _int(sd.get("free")) / _int(sd.get("capacity")) < 0.1,
            "using_relay": cam.get("net_mode") == "relay",
            "lens_covered": tamper.STATE.get(uri) == "covered",
            "camera_moved": tamper.STATE.get(uri) == "moved",
            "night_vision_broken": tamper.STATE.get(uri) == "night_vision",
        }
        issues += [{"camera": uri, "issue": issue} for issue, hit in checks.items() if hit]
    return issues


def prometheus_metrics(cams: dict[str, dict], api_stats: dict) -> str:
    lines = [f"wyze_tunnel_cameras {len(cams)}"]
    for uri, cam in cams.items():
        label = f'{{camera="{uri}"}}'
        lines += [
            f"wyze_camera_streaming{label} {int(cam.get('status') == 3)}",
            f"wyze_camera_offline{label} {int(cam.get('status') == -90)}",
            f"wyze_camera_enabled{label} {int(bool(cam.get('enabled')))}",
            f"wyze_camera_motion{label} {int(bool(cam.get('motion')))}",
        ]
        if (wifi := _int(((cam.get("camera_info") or {}).get("basicInfo") or {}).get("wifidb"))) >= 0:
            lines.append(f"wyze_camera_wifi_percent{label} {wifi}")
    for result in ("ok", "error", "rate_limited"):
        lines.append(f'wyze_api_calls_total{{result="{result}"}} {api_stats.get(result, 0)}')
    if "rate_limit_remaining" in api_stats:
        lines.append(f"wyze_api_rate_limit_remaining {api_stats['rate_limit_remaining']}")
    return "\n".join(lines) + "\n"


def ha_dashboard(cams: dict[str, dict]) -> dict:
    """A Home Assistant dashboard with a live view per camera, using MediaMTX's own player pages."""
    cards = [
        {
            "type": "iframe",
            "title": cam.get("nickname") or uri,
            "url": cam.get("webrtc_url") or cam["hls_url"],
            "aspect_ratio": "16:9",
        }
        for uri, cam in cams.items()
        if cam.get("enabled", True)
    ]
    return {"title": "Wyze Tunnel", "views": [{"title": "Cameras", "path": "wyze-tunnel", "cards": cards}]}


def boa_snapshot(stream: Stream) -> Optional[dict]:
    """Take photo."""
    stream.send_cmd("take_photo")
    if boa_info := stream.get_info("boa_info"):
        return boa_info.get("last_photo")
