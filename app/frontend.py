import os
import secrets
import subprocess
import time
from datetime import datetime, timezone
from threading import Thread
from functools import wraps
from pathlib import Path
from urllib.parse import quote_plus, urlencode

from flask import (
    Flask,
    Response,
    make_response,
    redirect,
    render_template,
    request,
    send_file,
    send_from_directory,
)
import requests
import yaml
from werkzeug.exceptions import NotFound
from wyze_bridge import WyzeBridge
from wyzebridge import api_v1, automations, backup, camera_settings, config, detect, devices, event_archive, floorplan, go2rtc, guests, homekit, integrations, masks, network, phone_cams, phrases, pocket, privacy, recording, sd_copy, sd_playback, shares, street_watch, tamper, timelapse, visitors, weather, web_ui, webhooks, wyze_api
from wyzebridge.auth import WbAuth
from wyzebridge.bridge_utils import env_bool, env_cam
from wyzebridge.diagnostics import build_report
from wyzebridge.ffmpeg import probe_stream, stream_warnings
from wyzebridge.logging import logger
from wyzebridge.mtx_server import PLAYBACK_ADDR, PLAYBACK_ENABLED, path_bytes
from wyzebridge.web_ui import url_for
from wyzebridge.wyze_events import EVENT_LOG
from wyzebridge.wyze_stream import WyzeStream
from wyzecam.api import API_STATS, AccessTokenError, RateLimitError, WyzeAPIError


# Links with any of these open the live grid, so wall displays and old bookmarks keep working.
GRID_ARGS = {"columns", "refresh", "video", "snapshot", "autoplay", "webrtc", "hls", "kvs", "fullscreen", "order"}


def create_app():
    app = Flask(__name__)
    app.jinja_env.globals["can_update"] = bool(os.getenv("WT_UPDATE_FILE"))
    app.jinja_env.globals["current_guest"] = web_ui.current_guest
    wb = WyzeBridge()
    Thread(target=webhooks.announce_update, daemon=True).start()
    pocket.Away.health_check()
    try:
        wb.start()
    except RuntimeError as ex:
        print(ex)
        print("Please ensure your host is up to date.")
        exit()

    @app.context_processor
    def page_version():
        # Every page's footer, update check and cache-busting asset links need it.
        return {"version": config.VERSION}

    def auth_required(view):
        @wraps(view)
        def wrapped_view(*args, **kwargs):
            if not wb.api.auth:
                return redirect(url_for("wyze_login"))
            return web_ui.auth.login_required(view)(*args, **kwargs)

        return wrapped_view

    def owner_only(view):
        @wraps(view)
        def wrapped_view(*args, **kwargs):
            if web_ui.current_guest():
                return {"error": "Only the owner can do this."}, 403
            return view(*args, **kwargs)

        return auth_required(wrapped_view)

    # One-time token so only the browser that ran the setup wizard sees the web password.
    setup = {"token": ""}

    @app.before_request
    def block_cross_site():
        # Images and links to the UI stay embeddable; everything else (commands, restarts, login) is same-site only.
        if request.path.startswith(("/static/", "/img/", "/snapshot/", "/thumb/", "/photo/", "/share/")):
            return
        # A neighbor opens their Street Watch link from a chat app; sending the form is still same-site only.
        # A pairing link arrives from the phone's QR scanner; its one-time code is the protection.
        if request.method == "GET" and (request.path in ("/", "/live", "/login", "/cams.m3u8") or request.path.startswith(("/street/", "/phone-cam/", "/pair/"))):
            return
        if web_ui.cross_site_request():
            return {"error": "Cross-site request blocked"}, 403

    @app.before_request
    def public_link_paired_only():
        # Pages without a login (Wyze sign-in, setup, shares...) stay home-only too: the public link only pairs.
        # No styles or scripts either, so a stranger can't tell what the link leads to.
        if pocket.public_request() and not request.path.startswith(("/pair/", "/robots.txt")) and not web_ui.verify_password("", ""):
            return render_template("pair.html", public_only=True), 403

    @app.after_request
    def no_indexing(resp):
        # Private cameras: no search engine or AI crawler may index, cache or train on any of it.
        resp.headers["X-Robots-Tag"] = "noindex, nofollow, noarchive, nosnippet, noimageindex, noai, noimageai"
        return resp

    @app.route("/robots.txt")
    def robots_txt():
        return Response("User-agent: *\nDisallow: /\n", mimetype="text/plain")

    @app.route("/login", methods=["GET", "POST"])
    def wyze_login():
        if wb.api.auth:
            return redirect(url_for("index"))
        if request.method == "GET":
            return render_template(
                "login.html",
                api=WbAuth.api,
                version=config.VERSION,
            )

        if code := request.form.get("mfaCode"):
            if not wb.api.mfa:
                return {"status": "no 2FA code was requested"}
            wb.api.mfa["verification_code"] = code.strip()
            return {"status": "success"}

        tokens = request.form.get("tokens")
        refresh = request.form.get("refresh")

        if tokens or refresh:
            wb.api.token_auth(tokens=tokens, refresh=refresh)
            setup["token"] = secrets.token_urlsafe()
            return {"status": "success", "setup": setup["token"]}

        credentials = {
            "email": request.form.get("email"),
            "password": request.form.get("password"),
            "key_id": request.form.get("keyId"),
            "api_key": request.form.get("apiKey"),
        }

        if all(credentials.values()):
            wb.api.login_error = ""
            wb.api.creds.update(**credentials)
            setup["token"] = secrets.token_urlsafe()
            return {"status": "success", "setup": setup["token"]}

        return {"status": "missing credentials"}

    @app.route("/login/status")
    def wyze_login_status():
        """Poll target for the setup wizard. Exposes no account or camera data."""
        if wb.api.auth and (WbAuth.api or not WbAuth.enabled):
            token = request.args.get("setup", "")
            if not (WbAuth.enabled and token and secrets.compare_digest(token, setup["token"])):
                return {"state": "done"}
            setup["token"] = ""
            return {"state": "done", "username": WbAuth.username, "password": WbAuth._pass}
        if wb.api.auth:
            return {"state": "signing_in"}
        if wb.api.mfa and "verification_code" not in wb.api.mfa:
            return {"state": "mfa", "mfa_type": wb.api.mfa["mfa_type"]}
        if wb.api.login_error:
            return {"state": "error", "error": wb.api.login_error, "retry_at": wb.api.login_retry_at}
        return {"state": "signing_in" if wb.api.creds.is_set else "idle"}

    @app.route("/")
    @auth_required
    def index():
        """Home: what happened today. Watch-only guests and wall displays (grid options in the link) get the live grid."""
        guest = web_ui.current_guest()
        if guest and not guest["family"]:
            return live()
        if request.args.keys() & GRID_ARGS or request.cookies.get("fullscreen"):
            return redirect(f"live?{request.query_string.decode()}")
        cams = web_ui.all_cams(wb.streams, wb.api.total_cams)["cameras"]
        return render_template(
            "home.html",
            cameras={uri: cam for uri, cam in cams.items() if cam.get("enabled")},
            events=list(EVENT_LOG),
            visits=visitors.load()[:6],
            steps=[] if guest else web_ui.setup_steps(cams),
            motion_on=bool(config.MOTION or webhooks.ALERTS.get("motion")),
            api="" if guest else WbAuth.api,
            guest=guest,
            version=config.VERSION,
        )

    @app.route("/glance")
    @auth_required
    def glance():
        """One screen to pin to a phone: each camera's state, picture and last motion, plus the alert mode.
        Watch-only guests can't open it: the mode tells when nobody is home."""
        cams = web_ui.all_cams(wb.streams, wb.api.total_cams)["cameras"]
        return render_template(
            "glance.html",
            cameras={uri: cam for uri, cam in cams.items() if cam.get("enabled")},
            last_motion={e["uri"]: e["ts"] for e in reversed(EVENT_LOG)},
            mode=webhooks.ALERTS.get("mode") or "schedule",
            guest=web_ui.current_guest(),
            version=config.VERSION,
        )

    @app.route("/live")
    @auth_required
    def live():
        if not (columns := request.args.get("columns")):
            columns = request.cookies.get("number_of_columns", "2")
        if not (refresh := request.args.get("refresh")):
            refresh = request.cookies.get("refresh_period", "30")
        number_of_columns = int(columns) if columns.isdigit() else 0
        refresh_period = int(refresh) if refresh.isdigit() else 0
        show_video = bool(request.cookies.get("show_video"))
        autoplay = bool(request.cookies.get("autoplay"))
        if "autoplay" in request.args:
            autoplay = True
        if "video" in request.args:
            show_video = True
        elif "snapshot" in request.args:
            show_video = False

        video_format = request.cookies.get("video", "webrtc")
        if req_video := ({"webrtc", "hls", "kvs"} & set(request.args)):
            video_format = req_video.pop()
        guest = web_ui.current_guest()
        resp = make_response(
            render_template(
                "live.html",
                cam_data=web_ui.all_cams(wb.streams, wb.api.total_cams),
                number_of_columns=number_of_columns,
                refresh_period=refresh_period,
                api="" if guest else WbAuth.api,
                guest=guest,
                version=config.VERSION,
                webrtc=bool(config.BRIDGE_IP),
                show_video=show_video,
                video_format=video_format.lower(),
                autoplay=autoplay,
                weather_on=weather.enabled(),
            )
        )

        YEARS = 10 * 365 * 86400  # remembered on this device, not just this browser session
        resp.set_cookie("number_of_columns", str(number_of_columns), max_age=YEARS)
        resp.set_cookie("refresh_period", str(refresh_period), max_age=YEARS)
        resp.set_cookie("show_video", "1" if show_video else "", max_age=YEARS)
        resp.set_cookie("video", video_format, max_age=YEARS)
        fullscreen = "fullscreen" in request.args or bool(
            request.cookies.get("fullscreen")
        )
        resp.set_cookie("fullscreen", "1" if fullscreen else "", max_age=YEARS)
        if order := request.args.get("order"):
            resp.set_cookie("camera_order", quote_plus(order), max_age=YEARS)

        return resp

    @app.route("/api/sse_status")
    @auth_required
    def sse_status():
        """Server sent event for camera status."""
        return Response(
            web_ui.sse_generator(lambda: web_ui.sse_status(wb.streams)),
            mimetype="text/event-stream",
        )

    @app.route("/api")
    @auth_required
    def api_all_cams():
        return web_ui.all_cams(wb.streams, wb.api.total_cams)

    @app.route("/api/health")
    @auth_required
    def api_health():
        cams = web_ui.all_cams(wb.streams, wb.api.total_cams)["cameras"]
        issues = web_ui.bridge_issues(cams)
        return {"status": "degraded" if issues else "ok", "cameras": len(cams), "issues": issues}

    @app.route("/api/usage")
    @auth_required
    def api_usage():
        """Byte counters per camera; the Health page turns two readings into a rate."""
        return {"time": time.time(), "paths": path_bytes()}

    @app.route("/metrics.prom")
    @auth_required
    def metrics_prom():
        cams = web_ui.all_cams(wb.streams, wb.api.total_cams)["cameras"]
        return Response(web_ui.prometheus_metrics(cams, API_STATS), mimetype="text/plain; version=0.0.4")

    @app.route("/api/<string:cam_name>")
    @auth_required
    def api_cam(cam_name: str):
        if cam := wb.streams.get_info(cam_name):
            return cam | web_ui.format_stream(cam_name)
        return {"error": f"Could not find camera [{cam_name}]"}

    @app.route("/api/<string:cam_name>/stream-test")
    @auth_required
    def api_stream_test(cam_name: str):
        """Watch the stream for a few seconds and report size, fps, bitrate and problems."""
        if not (info := wb.streams.get_info(cam_name)):
            return {"error": f"Could not find camera [{cam_name}]"}, 404
        probe = probe_stream(cam_name)
        return probe | {"warnings": stream_warnings(probe, wb.streams.get_info(cam_name) or info)}

    @app.route("/api/<string:cam_name>/firmware")
    @owner_only
    def api_firmware(cam_name: str):
        """Whether Wyze offers this camera a newer firmware (read-only)."""
        if not (cam := wb.api.get_camera(cam_name)):
            return {"error": f"Could not find camera [{cam_name}]"}, 404
        return wb.api.check_firmware(cam) or {"status": "error", "response": "Not signed in to Wyze."}

    @app.route("/api/<string:cam_name>/firmware/install", methods=["POST"])
    @owner_only
    def api_firmware_install(cam_name: str):
        """Start the firmware upgrade Wyze offers this camera. Needs the owner's own password, not the API key."""
        if not web_ui.owner_password():
            return {"error": "Sign in with the owner password to update firmware."}, 403
        if not (cam := wb.api.get_camera(cam_name)):
            return {"error": f"Could not find camera [{cam_name}]"}, 404
        body = request.get_json(silent=True)
        version = body.get("version") if isinstance(body, dict) else None
        if not isinstance(version, str) or not version:
            return {"error": "Say which firmware version to install."}, 400
        offer = wb.api.check_firmware(cam) or {}
        if offer.get("status") != "success" or not offer.get("update") or offer["latest"] != version:
            return {"error": "Wyze isn't offering that update for this camera."}, 409
        logger.warning(f"[FIRMWARE] {request.authorization.username} started {cam.name_uri} {cam.firmware_ver} -> {version}")
        return wb.api.begin_firmware_upgrade(cam, offer) or {"status": "error", "response": "Not signed in to Wyze."}

    @app.route("/api/<cam_name>/<cam_cmd>", methods=["GET", "PUT", "POST"])
    @app.route("/api/<cam_name>/<cam_cmd>/<path:payload>")
    @auth_required
    def api_cam_control(cam_name: str, cam_cmd: str, payload: str | dict = ""):
        """API Endpoint to send tutk commands to the camera."""
        if not payload and (args := request.values.to_dict()):
            args.pop("api", None)
            payload = next(iter(args.values())) if len(args) == 1 else args
        if not payload and request.is_json:
            json = request.get_json()
            if isinstance(json, dict):
                payload = json if len(json) > 1 else list(json.values())[0]
            else:
                payload = json
        elif not payload and request.data:
            payload = request.data.decode()

        if web_ui.owner_cmd_denied(cam_cmd.lower(), payload):
            return {"error": "Only the owner can do this."}, 403
        return wb.streams.send_cmd(cam_name, cam_cmd.lower(), payload)

    CHIME_TYPES = {"none": 1, "mechanical": 2, "digital": 3}

    def chime_input(data: dict) -> tuple[dict, str]:
        """Check a chime change from the page: ring sound, wired chime type and length, Wyze Chime sound and volume."""
        keys = {"ring_sound", "chime_type", "chime_seconds", "chime_sound", "chime_volume"}
        if not isinstance(data, dict) or not data or not keys.issuperset(data):
            return {}, "Nothing to change."

        def num(key: str, low: int, high: int) -> bool:
            return type(data[key]) is int and low <= data[key] <= high

        if data.get("ring_sound", "on") not in ("on", "off") or data.get("chime_type", "none") not in tuple(CHIME_TYPES):
            return {}, "Invalid ring sound or chime type."
        if ("chime_seconds" in data and not num("chime_seconds", 1, 9)) or ("chime_sound" in data and not num("chime_sound", 1, 19)):
            return {}, "Invalid chime length or sound."
        if "chime_volume" in data and not num("chime_volume", 0, 8):  # the app's slider (plugin_ddq_fragment_setting_voice.xml)
            return {}, "Invalid chime volume."
        if ("chime_sound" in data) != ("chime_volume" in data):
            return {}, "The Wyze Chime needs its sound and volume together."
        return data, ""

    def doorbell_stream(cam_name: str):
        stream = wb.streams.get(cam_name)
        return stream if stream and stream.camera.is_doorbell else None

    @app.route("/api/<string:cam_name>/chime")
    @auth_required
    def api_chime(cam_name: str):
        """What the doorbell v2 reports for its ring sound and wired chime."""
        if not doorbell_stream(cam_name):
            return {"error": "Not a doorbell."}, 404
        return {
            "ring_sound": wb.streams.send_cmd(cam_name, "ring_sound").get("value"),
            "doorbell_chime": wb.streams.send_cmd(cam_name, "doorbell_chime").get("value"),
        }

    @app.route("/api/<string:cam_name>/chime", methods=["POST"])
    @owner_only
    def api_chime_set(cam_name: str):
        if not (stream := doorbell_stream(cam_name)):
            return {"error": "Not a doorbell."}, 404
        data, error = chime_input(request.get_json(silent=True))
        if error:
            return {"error": error}, 400
        results = {}
        if "ring_sound" in data:
            results["ring_sound"] = wb.streams.send_cmd(cam_name, "ring_sound", data["ring_sound"])
        if "chime_type" in data:
            results["chime_type"] = wb.streams.send_cmd(cam_name, "doorbell_chime", f"{CHIME_TYPES[data['chime_type']]},0,0")
        if "chime_seconds" in data:
            results["chime_seconds"] = wb.streams.send_cmd(cam_name, "doorbell_chime", f"0,0,{data['chime_seconds']}")
        if "chime_sound" in data:
            results["wyze_chime"] = wb.api.set_chime(stream.camera, data["chime_sound"], data["chime_volume"]) or {"status": "error", "response": "Wyze isn't signed in"}
        return {"results": results}, 200 if all(r.get("status") == "success" for r in results.values()) else 502

    @app.route("/rules")
    @auth_required
    def rules():
        return render_template("rules.html", version=config.VERSION)

    @app.route("/api/rules")
    @auth_required
    def api_rules():
        """The Wyze account's rules and shortcuts (names only)."""
        return wb.api.get_rules() or {"status": "error", "response": "Wyze isn't signed in"}

    @app.route("/api/rules/<string:auto_id>/run", methods=["POST"])
    @owner_only
    def api_run_shortcut(auto_id: str):
        """Run a Wyze shortcut. A shortcut can unlock doors or switch plugs, so the API key and apps can't."""
        if not web_ui.owner_password():
            return {"error": "Sign in with the owner's own password to run shortcuts."}, 403
        found = wb.api.get_rules() or {}
        if not any(r["id"] == auto_id and r["shortcut"] for r in found.get("rules", [])):
            return {"error": "No such shortcut."}, 404
        result = wb.api.run_shortcut(auto_id) or {"status": "error", "response": "Wyze isn't signed in"}
        return result, 200 if result["status"] == "success" else 502

    def shared(name: str) -> bool:
        """Whether viewers can share the tunnel's own camera connection instead of opening one each.

        Matters for cameras that take few viewers, like the Cam OG (two at once).
        """
        return bool(config.BRIDGE_IP) and bool((stream := wb.streams.get(name)) and stream.enabled)

    @app.route("/signaling/<string:name>")
    @auth_required
    def webrtc_signaling(name):
        # "relay" is go2rtc fetching the one connection the tunnel holds; "talk" is the owner's push-to-talk,
        # a second Kinesis session that sends the browser's mic (MediaMTX's shared stream can't carry it back).
        # A relay session is a direct Wyze cloud session too, which could carry a mic, so it's not for guests either.
        if ("talk" in request.args or "relay" in request.args) and (guest := web_ui.current_guest()) and not guest["family"]:
            return {"result": "Guests can watch but not talk through cameras.", "cam": name}, 403
        if "kvs" in request.args and ("relay" in request.args or "talk" in request.args or not shared(name)):
            return wb.api.get_kvs_signal(name)
        if web_ui.current_guest():
            return web_ui.get_webrtc_signal(name, guests.stream_key(), guests.STREAM_USER)
        return web_ui.get_webrtc_signal(name, WbAuth.api)

    @app.route("/webrtc/<string:name>")
    @auth_required
    def webrtc(name):
        """View WebRTC, direct from the camera only when the tunnel isn't streaming it."""
        webrtc = web_ui.get_webrtc_signal(name, WbAuth.api) if shared(name) else wb.api.get_kvs_signal(name)
        if webrtc.get("result") == "ok":
            return make_response(render_template("webrtc.html", webrtc=webrtc))
        return webrtc

    @app.route("/snapshot/<string:img_file>")
    @auth_required
    def rtsp_snapshot(img_file: str):
        """Use ffmpeg to take a snapshot from the rtsp stream."""
        if wb.streams.get_rtsp_snap(Path(img_file).stem):
            return send_from_directory(config.IMG_PATH, img_file)
        return thumbnail(img_file)

    @app.route("/img/<string:img_file>")
    @auth_required
    def img(img_file: str):
        """
        Serve an existing local image or take a new snapshot from the rtsp stream.

        Use the exp parameter to fetch a new snapshot if the existing one is too old.
        """
        try:
            if exp := request.args.get("exp"):
                created_at = os.path.getmtime(config.IMG_PATH + img_file)
                if time.time() - created_at > int(exp):
                    raise NotFound
            return send_from_directory(config.IMG_PATH, img_file)
        except (NotFound, FileNotFoundError, ValueError):
            return rtsp_snapshot(img_file)

    @app.route("/thumb/<string:img_file>")
    @auth_required
    def thumbnail(img_file: str):
        if wb.api.save_thumbnail(Path(img_file).stem):
            return send_from_directory(config.IMG_PATH, img_file)
        return redirect("/static/notavailable.svg", code=307)

    @app.route("/photo/<string:img_file>")
    @auth_required
    def boa_photo(img_file: str):
        """Take a photo on the camera and grab it over the boa http server."""
        uri = Path(img_file).stem
        if not (cam := wb.streams.get(uri)):
            return redirect("/static/notavailable.svg", code=307)
        if photo := web_ui.boa_snapshot(cam):
            return send_from_directory(config.IMG_PATH, f"{uri}_{photo[0]}")
        return redirect(f"/img/{img_file}", code=307)

    @app.route("/diagnostics")
    @auth_required
    def diagnostics():
        """Download a redacted report to attach to bug reports."""
        report = build_report(
            config.VERSION,
            config.BUILD_STR,
            wb.streams.get_all_cam_info(),
            (WbAuth.api, WbAuth._pass, wb.api.auth and wb.api.auth.access_token or ""),
        )
        resp = make_response(report)
        resp.headers["Content-Type"] = "text/plain; charset=utf-8"
        resp.headers["Content-Disposition"] = (
            f"attachment; filename=wyze-tunnel-diagnostics-{time.strftime('%Y%m%d-%H%M%S')}.txt"
        )
        return resp

    @app.route("/update", methods=["GET", "POST"])
    @auth_required
    def self_update():
        """Native installs: ask wyze-tunnel-update.path to install the latest release. GET reports the running version."""
        if request.method == "GET":
            return {"version": config.VERSION, "can_update": bool(os.getenv("WT_UPDATE_FILE"))}
        if not (flag := os.getenv("WT_UPDATE_FILE")):
            return {"result": "error", "error": "This install can't update itself"}, 400
        Path(flag).touch()
        return {"result": "ok"}

    @app.route("/restart/<string:restart_cmd>")
    @auth_required
    def restart_bridge(restart_cmd: str):
        """
        Restart parts of the wyze-bridge.

        /restart/cameras:       Restart camera connections.
        /restart/rtsp_server:   Restart rtsp-simple-server.
        /restart/all:           Restart camera connections and rtsp-simple-server.
        """
        if restart_cmd == "cameras":
            wb.streams.stop_all()
            wb.streams.monitor_streams(wb.health_check)
        elif restart_cmd == "rtsp_server":
            wb.mtx.restart()
        elif restart_cmd == "cam_data":
            wb.refresh_cams()
            restart_cmd = "cameras"
        elif restart_cmd == "all":
            wb.restart(fresh_data=True)
            restart_cmd = "cameras,rtsp_server"
        else:
            return {"result": "error"}
        return {"result": "ok", "restart": restart_cmd.split(",")}

    @app.route("/health")
    @auth_required
    def health():
        cams = web_ui.all_cams(wb.streams, wb.api.total_cams)["cameras"]
        return render_template(
            "health.html",
            cameras=cams,
            disk=None if web_ui.current_guest() else recording.disk_use(),
            issues=web_ui.bridge_issues(cams),
            tamper_on=tamper.enabled(),
            tamper_state=tamper.STATE,
            now=time.time(),
            api=WbAuth.api,
            version=config.VERSION,
        )

    @app.route("/health/tamper", methods=["POST"])
    @owner_only
    def tamper_setting():
        try:
            return {"status": "saved", "on": tamper.set_enabled((request.get_json(silent=True) or {}).get("on"))}
        except ValueError as ex:
            return {"error": str(ex)}, 400

    @app.route("/health/tamper/<string:cam_name>/reset", methods=["POST"])
    @owner_only
    def tamper_reset(cam_name: str):
        if cam_name not in wb.streams.streams:
            return {"error": "Unknown camera"}, 404
        tamper.reset(cam_name)
        return {"status": "saved"}

    @app.route("/camera-settings")
    @owner_only
    def camera_settings_page():
        return render_template("camera_settings.html")

    def settings_send(uri: str, cmd: str, payload: str = "") -> dict:
        return wb.streams.send_cmd(uri, cmd, payload)

    @app.route("/camera-settings/data")
    @owner_only
    def camera_settings_data():
        return camera_settings.table(wb.streams.streams) | {"snapshots": camera_settings.list_snapshots()}

    @app.route("/camera-settings/refresh", methods=["POST"])
    @owner_only
    def camera_settings_refresh():
        # One command at a time takes a while, so the reads run in the background.
        if camera_settings.LOCK.locked():
            return {"error": "Already busy with the cameras."}, 409
        Thread(target=camera_settings.refresh, args=(wb.streams.streams, settings_send), daemon=True).start()
        return {"status": "started"}

    @app.route("/camera-settings/apply", methods=["POST"])
    @owner_only
    def camera_settings_apply():
        body = request.get_json(silent=True) or {}
        try:
            results = camera_settings.apply(wb.streams.streams, settings_send, str(body.get("setting")), str(body.get("value")), body.get("cams"))
        except ValueError as ex:
            return {"error": str(ex)}, 400
        except RuntimeError:
            return {"error": "Busy with the cameras. Try again in a minute."}, 409
        return {"results": results}

    @app.route("/camera-settings/snapshots", methods=["POST"])
    @owner_only
    def camera_settings_snapshot():
        try:
            return camera_settings.save_snapshot(wb.streams.streams)
        except ValueError as ex:
            return {"error": str(ex)}, 400

    @app.route("/camera-settings/restore", methods=["POST"])
    @owner_only
    def camera_settings_restore():
        body = request.get_json(silent=True) or {}
        try:
            return camera_settings.restore(wb.streams.streams, settings_send, str(body.get("id")), str(body.get("cam")), body.get("confirm") is True)
        except ValueError as ex:
            return {"error": str(ex)}, 400
        except RuntimeError:
            return {"error": "Busy with the cameras. Try again in a minute."}, 409

    @app.route("/network")
    @auth_required
    def network_page():
        if web_ui.current_guest():
            return {"error": "Only the owner can see the Network page."}, 403
        cams = web_ui.all_cams(wb.streams, wb.api.total_cams)["cameras"]
        return render_template(
            "network.html",
            seen=sorted(network.SEEN.values(), key=lambda e: -e["last"]),
            other=network.other_connections(cams),
            cloud_down=network.cloud_down(),
            keys_saved=network.lan_keys_saved(wyze_api.LAN_KEYS_FILE),
            now=time.time(),
        )

    @app.route("/visitors/<path:pic>")
    @auth_required
    def visitor_pic(pic: str):
        return send_from_directory(visitors.PIC_DIR, pic)

    @app.route("/map")
    @auth_required
    def map_page():
        cams = web_ui.all_cams(wb.streams, wb.api.total_cams)["cameras"]
        return render_template(
            "map.html",
            cameras={uri: {"name": c["nickname"], "connected": c.get("connected"), "offline": c.get("status") == -90} for uri, c in cams.items()},
            pins=floorplan.load_pins(),
            has_image=floorplan.IMAGE_FILE.exists(),
            api=WbAuth.api,
            version=config.VERSION,
        )

    @app.route("/map/image", methods=["GET", "POST"])
    @auth_required
    def map_image():
        if request.method == "GET":
            if not floorplan.IMAGE_FILE.exists():
                return {"error": "No floor plan yet"}, 404
            data = floorplan.IMAGE_FILE.read_bytes()
            return Response(data, mimetype=floorplan.image_type(data) or "application/octet-stream")
        if web_ui.current_guest():
            return {"error": "Only the owner can change this."}, 403
        try:
            floorplan.save_image(request.get_data(cache=False)[: floorplan.MAX_BYTES + 1])
        except ValueError as ex:
            return {"error": str(ex)}, 400
        return {"status": "saved"}

    @app.route("/map/pins", methods=["POST"])
    @owner_only
    def map_pins():
        try:
            return {"pins": floorplan.save_pins(request.get_json(silent=True), list(wb.streams.streams))}
        except ValueError as ex:
            return {"error": str(ex)}, 400

    @app.route("/wyze-clip")
    @auth_required
    def wyze_clip():
        """Play Wyze's cloud clip, or show the picture, of a motion event. Only Wyze's own hosts are fetched."""
        url = request.args.get("u", "")
        if not wyze_api.wyze_media_url(url):
            return {"error": "Not a Wyze clip"}, 400
        try:
            resp = wb.api.get_media(url, stream=True, timeout=15, allow_redirects=False)
        except requests.RequestException as ex:
            return {"error": str(ex)}, 502
        if not resp.ok:
            return {"error": "Wyze's clip has expired or isn't ready yet."}, 404
        return Response(resp.iter_content(64 * 1024), mimetype=resp.headers.get("Content-Type", "video/mp4"))

    @app.route("/events")
    def events():
        return redirect("./")  # The event list now lives on the home timeline.

    @app.route("/cameras")
    @auth_required
    def cameras_page():
        return render_template("cameras.html", api=WbAuth.api, version=config.VERSION)

    @app.route("/cameras/visibility", methods=["GET", "POST"])
    @auth_required
    def camera_visibility():
        """Every camera on the account, with which ones are hidden. Hidden cameras don't stream."""
        cams = wb.api.get_cameras() or []
        if request.method == "POST":
            hidden = set((request.get_json(silent=True) or {}).get("hidden") or [])
            macs = {cam.mac for cam in cams}
            if not hidden <= macs:
                return {"error": "Unknown camera."}, 400
            if cams and hidden == macs:
                return {"error": "Keep at least one camera showing."}, 400
            if hidden != wyze_api.load_hidden_cams():
                wyze_api.save_hidden_cams(hidden)
                # refresh_cams runs the stream monitor loop, so it can't run in this request.
                Thread(target=wb.refresh_cams, daemon=True).start()
            return {"status": "saved"}
        hidden, places = wyze_api.load_hidden_cams(), phrases.load_places()
        return [
            {"mac": c.mac, "nickname": wyze_api.display_name(c), "model": c.model_name, "hidden": c.mac in hidden,
             "uri": c.name_uri, "place": places.get(c.name_uri, ""), "guess": phrases.guess(wyze_api.display_name(c))}
            for c in cams
        ]

    @app.route("/cameras/place", methods=["POST"])
    @auth_required
    def camera_place():
        """Where a camera points, for alerts like "Someone walked up to the door". Empty goes back to the guess."""
        data = request.get_json(silent=True) or {}
        if data.get("uri") not in {cam.name_uri for cam in wb.api.get_cameras() or []}:
            return {"error": "Unknown camera."}, 400
        try:
            phrases.save_place(data["uri"], str(data.get("place") or ""))
        except ValueError as ex:
            return {"error": str(ex)}, 400
        return {"status": "saved"}

    def device_call(fn, *args):
        """Retries once with a fresh token if it expired since login."""
        try:
            return fn(wb.api.auth, *args)
        except AccessTokenError:
            wb.api.refresh_token()
            return fn(wb.api.auth, *args)

    @app.route("/devices")
    @auth_required
    def devices_page():
        return render_template("devices.html")

    @app.route("/api/devices", methods=["GET", "POST"])
    @auth_required
    def api_devices():
        """Plugs, bulbs, sensors, thermostat, locks and chimes. POST {"mac": ..., "on" | "lock" | ...: ...} changes one."""
        try:
            if request.method == "GET":
                return {"devices": device_call(devices.list_devices, bool(request.args.get("fresh"))), "lock_minutes": webhooks.ALERTS.get("lock_minutes") or 0}
            data = request.get_json(silent=True) or {}
            # Door locks only for the owner's password: API-keyed stream links and family logins can't unlock a door.
            if "lock" in data and not web_ui.owner_password():
                return {"error": "Only the owner, signed in with the web password, can lock or unlock doors."}, 403
            if not (dev := device_call(devices.find, str(data.get("mac", "")))):
                return {"error": "Unknown device."}, 404
            done = device_call(devices.control, dev, data)
        except ValueError as ex:
            return {"error": str(ex)}, 400
        except RateLimitError:
            return {"error": "Wyze is rate limiting this account. Try again in a minute."}, 429
        except (WyzeAPIError, requests.RequestException) as ex:
            return {"error": f"Wyze didn't accept that: {ex}"}, 502
        who = (web_ui.current_guest() or {}).get("guest", "owner")
        logger.warning(f"[DEVICES] {who} ({request.remote_addr}) {done}: {dev['name']} ({dev['model']}) at {datetime.now().isoformat(timespec='seconds')}")
        return {"status": done}

    def camera_choices() -> list[dict]:
        return [{"mac": cam.mac, "name": wyze_api.display_name(cam)} for cam in wb.api.get_cameras() or []]

    @app.route("/api/automations")
    @auth_required
    def api_automations():
        return {"settings": automations.load(), "cameras": camera_choices()}

    @app.route("/api/automations", methods=["POST"])
    @owner_only
    def save_automations():
        try:
            known = {d["mac"]: d["type"] for d in device_call(devices.list_devices)}
            return {"settings": automations.save(request.get_json(silent=True), {c["mac"] for c in camera_choices()}, known)}
        except ValueError as ex:
            return {"error": str(ex)}, 400
        except RateLimitError:
            return {"error": "Wyze is rate limiting this account. Try again in a minute."}, 429
        except (WyzeAPIError, requests.RequestException) as ex:
            return {"error": f"Couldn't read your devices from Wyze: {ex}"}, 502

    @app.route("/api/devices/lock-log")
    @owner_only  # who comes and goes, and code names/schedules: not for family
    def api_lock_log():
        """Recent activity and keypad code names for one lock. Never the code digits."""
        try:
            dev = device_call(devices.find, request.args.get("mac", ""))
            if not dev or dev["type"] != "Lock":
                return {"error": "Unknown lock."}, 404
            return device_call(devices.lock_log, dev["mac"], bool(request.args.get("fresh")))
        except RateLimitError:
            return {"error": "Wyze is rate limiting this account. Try again in a minute."}, 429
        except (WyzeAPIError, requests.RequestException) as ex:
            return {"error": f"Wyze didn't answer: {ex}"}, 502

    @app.route("/api/devices/history")
    @auth_required
    def api_device_history():
        """Readings the bridge has kept for the last 24 hours or 7 days; a thermostat's run time comes from Wyze when it answers."""
        hours = request.args.get("hours", "24")
        if hours not in ("24", "168"):
            return {"error": "hours must be 24 or 168."}, 400
        mac = request.args.get("mac", "")
        out = devices.history(mac, int(hours))
        try:
            if (dev := device_call(devices.find, mac)) and dev["model"] == "CO_EA1":
                out |= device_call(devices.wyze_run_minutes, mac, int(hours))
        except (WyzeAPIError, RateLimitError, AccessTokenError, requests.RequestException, KeyError, TypeError, ValueError) as ex:
            logger.warning(f"[DEVICES] Wyze thermostat run time failed, showing the bridge's own: {ex}")
        return out

    @app.route("/api/devices/usage")
    @auth_required
    def api_plug_usage():
        """Hours each day a plug was on, from Wyze's usage record (plugs only: no lock or camera data)."""
        try:
            dev = device_call(devices.find, request.args.get("mac", ""))
            if not dev or dev["type"] not in ("OutdoorPlug", "Plug"):
                return {"error": "Unknown plug."}, 404
            return {"days": device_call(devices.plug_usage, dev, bool(request.args.get("fresh")))}
        except RateLimitError:
            return {"error": "Wyze is rate limiting this account. Try again in a minute."}, 429
        except (WyzeAPIError, requests.RequestException) as ex:
            return {"error": f"Wyze didn't answer: {ex}"}, 502

    @app.route("/api/devices/filters")
    @auth_required
    def api_thermostat_filters():
        """The thermostat's air filters: name, size, life and how used each one is."""
        try:
            dev = device_call(devices.find, request.args.get("mac", ""))
            if not dev or dev["model"] != "CO_EA1":
                return {"error": "Unknown thermostat."}, 404
            return {"filters": device_call(devices.filters, dev["mac"])}
        except RateLimitError:
            return {"error": "Wyze is rate limiting this account. Try again in a minute."}, 429
        except (WyzeAPIError, requests.RequestException, KeyError, ValueError) as ex:
            return {"error": f"Wyze didn't answer: {ex}"}, 502

    @app.route("/api/devices/groups")
    @auth_required
    def api_device_groups():
        """The account's device groups (name and member macs), read-only."""
        try:
            return {"groups": device_call(devices.groups, bool(request.args.get("fresh")))}
        except RateLimitError:
            return {"error": "Wyze is rate limiting this account. Try again in a minute."}, 429
        except (WyzeAPIError, requests.RequestException) as ex:
            return {"error": f"Wyze didn't answer: {ex}"}, 502

    @app.route("/api/devices/schedules")
    @auth_required
    def api_device_schedules():
        """Saved plug and light schedules with each one's next run."""
        return devices.schedules()

    @app.route("/api/devices/schedules", methods=["POST"])
    @owner_only
    def api_device_schedules_save():
        """{"mac": ..., "schedules": [{"time": "07:30", "on": true, "days": [1, 2, 3]}]}. An empty list clears it."""
        data = request.get_json(silent=True) or {}
        try:
            if not (dev := device_call(devices.find, str(data.get("mac", "")))):
                return {"error": "Unknown device."}, 404
            devices.save_schedules(dev, data.get("schedules"))
        except ValueError as ex:
            return {"error": str(ex)}, 400
        except RateLimitError:
            return {"error": "Wyze is rate limiting this account. Try again in a minute."}, 429
        except (WyzeAPIError, requests.RequestException) as ex:
            return {"error": f"Wyze didn't answer: {ex}"}, 502
        logger.warning(f"[DEVICES] owner ({request.remote_addr}) saved the schedule for {dev['name']}")
        return devices.schedules().get(dev["mac"], [])

    @app.route("/api/devices/lock-alert", methods=["POST"])
    @owner_only
    def api_lock_alert():
        try:
            return {"lock_minutes": webhooks.set_lock_alert((request.get_json(silent=True) or {}).get("minutes"))}
        except ValueError as ex:
            return {"error": str(ex)}, 400

    @app.route("/privacy", methods=["GET", "POST"])
    @app.route("/battery-saver", methods=["GET", "POST"], endpoint="battery_saver")
    @auth_required
    def privacy_schedule():
        path = privacy.BATTERY_FILE if request.path == "/battery-saver" else privacy.PRIVACY_FILE
        if request.method == "POST":
            macs = {cam.mac for cam in wb.api.get_cameras() or []}
            try:
                privacy.save_privacy(request.get_json(silent=True) or {}, macs, path)
            except ValueError as ex:
                return {"error": str(ex)}, 400
            return {"status": "saved"}
        return privacy.load_privacy(path)

    @app.route("/cameras/name", methods=["POST"])
    @auth_required
    def camera_name():
        """Rename a camera in Wyze Tunnel. An empty name goes back to the name from the Wyze app."""
        data = request.get_json(silent=True) or {}
        mac, name = data.get("mac"), str(data.get("name") or "").strip()
        if mac not in {cam.mac for cam in wb.api.get_cameras() or []}:
            return {"error": "Unknown camera."}, 400
        if len(name) > 64:
            return {"error": "Keep the name under 64 characters."}, 400
        wyze_api.save_cam_name(mac, name)
        return {"status": "saved"}

    @app.route("/alerts", methods=["GET", "POST"])
    @auth_required
    def alerts():
        if request.method == "POST":
            if web_ui.current_guest():  # reading stays open to guests; changing alerts is the owner's
                return {"error": "Only the owner can do this."}, 403
            try:
                webhooks.save_alerts(request.get_json(silent=True) or request.form.to_dict())
            except ValueError as ex:
                return {"error": str(ex)}, 400
            return {"status": "saved"}
        settings = webhooks.ALERTS or {"server": "https://ntfy.sh", "motion": True, "offline": True}
        return render_template(
            "alerts.html",
            settings=settings,
            # Anyone who knows an ntfy.sh topic can read it, so suggest one nobody can guess.
            suggested_topic=f"wyze-tunnel-{secrets.token_hex(6)}",
            tamper_on=tamper.enabled(),
            api=WbAuth.api,
            version=config.VERSION,
        )

    @app.route("/account", methods=["GET", "POST"])
    @auth_required
    def account():
        if request.method == "POST":
            if not WbAuth.enabled:
                return {"error": "Login is turned off (WB_AUTH=False)."}, 400
            if web_ui.current_guest():
                return {"error": "Only the owner can change this login."}, 403
            data = request.get_json(silent=True) or request.form.to_dict()
            try:
                WbAuth.set_login(data.get("username", ""), data.get("password", ""))
            except ValueError as ex:
                return {"error": str(ex)}, 400
            return {"status": "saved"}
        return render_template(
            "account.html",
            guests=guests.list_guests(),
            location=weather.load(),
            auto_folder=backup.load_auto().get("folder", ""),
            folder_example=backup.FOLDER_EXAMPLE,
            member=web_ui.current_guest(),
            guest_hours=guests.GUEST_HOURS,
            username=WbAuth.username,
            enabled=WbAuth.enabled,
            api=WbAuth.api,
            away=pocket.Away.status(),
            version=config.VERSION,
        )

    @app.route("/weather")
    @auth_required
    def weather_now():
        return weather.current() or {}

    @app.route("/weather/settings", methods=["POST"])
    @owner_only
    def weather_settings():
        data = request.get_json(silent=True) or {}
        try:
            loc = weather.save(data.get("weather"), data.get("town"))
        except ValueError as ex:
            return {"error": str(ex)}, 400
        return {"status": "saved", "name": loc.get("name", "")}

    @app.route("/guests", methods=["POST"])
    @owner_only
    def add_guest():
        data = request.get_json(silent=True) or {}
        hours = data.get("hours")
        try:
            password = guests.add_guest(data.get("name"), hours, WbAuth.username)
        except ValueError as ex:
            return {"error": str(ex)}, 400
        return {"name": data["name"].strip(), "password": password}

    @app.route("/guests/<string:name>", methods=["DELETE"])
    @owner_only
    def remove_guest(name: str):
        guests.remove_guest(name)
        return {"status": "removed"}

    @app.route("/pair", methods=["POST"])
    @auth_required
    def pair_start():
        if not WbAuth.enabled:
            return {"error": "Login is turned off (WB_AUTH=False), so any phone on your network can already open it."}, 400
        if web_ui.current_guest():
            return {"error": "Only the owner can pair a phone."}, 403
        path = url_for("pair_phone", code=pocket.new_code())
        host, port = phone_host()
        codes = {"home": f"{request.scheme}://{host}{port}{path}"}
        if public := pocket.Away.status()["public"]:
            codes["anywhere"] = public + path  # the phone's cookie belongs to this address, so it works anywhere
        elif (ts_host := pocket.tailscale_host()) and ts_host != host:
            # The bundled Tailscale serves the web UI on port 80.
            codes["anywhere"] = f"http://{ts_host}{'' if ts_host == pocket.Away.status()['host'] else port}{path}"
        return {k: {"url": url, "qr": pocket.qr_svg(url)} for k, url in codes.items()}

    def phone_host() -> tuple[str, str]:
        """This page's host and ":port", with localhost swapped for the home network address a phone can reach."""
        host, _, port = request.host.rpartition(":") if request.host.count(":") == 1 else (request.host, "", "")
        port = f":{port}" if port else ""
        if host in ("localhost", "127.0.0.1", "[::1]"):
            host = pocket.lan_ip()
        return host, port

    @app.route("/away", methods=["GET", "POST"])
    @owner_only
    def away():
        if request.method == "POST":
            if not pocket.Away.available():
                return {"error": "Away access isn't included in this install."}, 400
            pocket.Away.turn(bool((request.get_json(silent=True) or {}).get("on")))
        return pocket.Away.status()

    @app.route("/pair/<string:code>", methods=["GET", "POST"])
    def pair_phone(code: str):
        if not WbAuth.enabled:
            return redirect(url_for("index"))
        # Scanners and link previews fetch the link too, so only the button press (a POST) uses up the code.
        if request.method == "GET":
            valid = pocket.code_valid(code)
            return render_template("pair.html", ready=valid), 200 if valid else 410
        if not (cookie := pocket.claim(code, request.user_agent.string, WbAuth.username)):
            return render_template("pair.html", version=config.VERSION), 410
        resp = redirect(url_for("index", pocket=1))
        resp.set_cookie(pocket.COOKIE, cookie, max_age=pocket.COOKIE_SECONDS, httponly=True, samesite="Lax", secure=request.is_secure)
        return resp

    @app.route("/phone-cams", methods=["GET", "POST"])
    @owner_only
    def phone_cams_page():
        if request.method == "POST":
            try:
                cam = phone_cams.add((request.get_json(silent=True) or {}).get("name"), set(wb.streams.streams))
            except ValueError as ex:
                return {"error": str(ex)}, 400
            wb.add_phone_path(cam["uri"])
            return {"uri": cam["uri"]}
        host, port = phone_host()
        live = {uri for uri, p in path_bytes().items() if p.get("ready")}
        cams = []
        for cam in phone_cams.list_cams():
            url = f"{request.scheme}://{host}{port}{url_for('phone_cam', token=cam['token'])}"
            cams.append(cam | {"url": url, "qr": pocket.qr_svg(url), "live": cam["uri"] in live})
        return render_template(
            "phone_cams.html",
            cams=cams,
            # Phones only share a camera with https pages. The QR link uses this page's scheme.
            secure=request.is_secure,
            api=WbAuth.api,
            version=config.VERSION,
        )

    @app.route("/phone-cams/<string:uri>", methods=["DELETE"])
    @owner_only
    def remove_phone_cam(uri: str):
        if uri not in phone_cams.uris():  # never a Wyze camera's path
            return {"error": "No phone camera by that name."}, 404
        phone_cams.remove(uri)
        wb.mtx.remove_path(uri)  # also drops a phone that's still sending
        return {"status": "removed"}

    @app.route("/phone-cam/<string:token>")
    def phone_cam(token: str):
        """The page the phone opens. The secret link is its only login, and it can only send video to its own camera."""
        cam = phone_cams.by_token(token)
        resp = make_response(render_template("phone_cam.html", name=cam and cam["name"]), 200 if cam else 404)
        resp.headers["X-Robots-Tag"] = "noindex, nofollow"
        resp.headers["Referrer-Policy"] = "no-referrer"
        return resp

    @app.route("/phone-cam/<string:token>/whip", methods=["POST"])
    def phone_cam_whip(token: str):
        """Hand the phone's WebRTC offer to MediaMTX. Through here it stays on the page's own (https) address,
        and MediaMTX only takes video from this computer, so nobody without a link can send to a camera."""
        if not (cam := phone_cams.by_token(token)):
            return {"error": "This camera was removed. Ask for a new link."}, 404
        scheme = "https" if env_bool("MTX_WEBRTCENCRYPTION") else "http"
        try:
            resp = requests.post(
                f"{scheme}://127.0.0.1:8889/{cam['uri']}/whip",
                data=request.get_data(),
                headers={"Content-Type": "application/sdp"},
                timeout=15,
                verify=False,
            )
        except requests.RequestException:
            return {"error": "Wyze Tunnel's video server isn't answering. Try again in a minute."}, 503
        if resp.status_code != 201:
            logger.warning(f"[PHONE] /{cam['uri']} refused: {resp.status_code} {resp.text.strip()[:200]}")
            return {"error": "Wyze Tunnel couldn't take the video. Restart Wyze Tunnel and try again."}, 502
        return Response(resp.content, 201, content_type="application/sdp")

    @app.route("/settings/backup", methods=["GET", "POST"])
    @owner_only
    def settings_backup():
        data = backup.export_settings()
        if request.method == "POST":
            try:
                data = backup.encrypt(data, str((request.get_json(silent=True) or {}).get("passphrase") or ""))
            except ValueError as ex:
                return {"error": str(ex)}, 400
        resp = make_response(data)
        resp.headers["Content-Disposition"] = "attachment; filename=wyze-tunnel-settings.json"
        return resp

    @app.route("/settings/auto-backup", methods=["POST"])
    @auth_required
    def settings_auto_backup():
        if web_ui.current_guest():
            return {"error": "Only the owner can choose where backups are saved."}, 403
        try:
            settings = backup.save_auto(request.get_json(silent=True) or {})
            path = settings and backup.auto_backup(force=True)
        except ValueError as ex:
            return {"error": str(ex)}, 400
        return {"status": "saved", "path": path or None}

    @app.route("/settings/restore", methods=["POST"])
    @owner_only
    def settings_restore():
        try:
            restored = backup.import_settings(backup.unlock(request.get_json(silent=True)))
        except ValueError as ex:
            return {"error": str(ex)}, 400
        # Hidden cameras, names, quality and connection choices apply when streams restart.
        Thread(target=wb.refresh_cams, daemon=True).start()
        return {"status": "restored", "restored": restored}

    @app.route("/start-over", methods=["POST"])
    @owner_only
    def start_over():
        try:
            saved = web_ui.start_over(config.TOKEN_PATH, config.IMG_PATH, recording.record_root())
        except OSError as ex:
            return {"error": f"Couldn't save a backup first, so nothing was cleared: {ex.strerror}"}, 500
        wb.api.creds.reset_creds()
        wb.api.login_error = ""
        wb.api.clear_cache()
        # Blocks in login until the setup wizard gets new Wyze credentials.
        Thread(target=wb.restart, daemon=True).start()
        return {"status": "cleared", "backup": saved, "redirect": url_for("wyze_login")}

    @app.route("/settings/from-docker-wyze-bridge", methods=["POST"])
    @owner_only
    def settings_from_dwb():
        try:
            out = backup.from_docker_wyze_bridge(str((request.get_json(silent=True) or {}).get("text") or ""))
        except ValueError as ex:
            return {"error": str(ex)}, 400
        if os.name == "nt":
            out["where"] = r"Save it as %LOCALAPPDATA%\Wyze Tunnel\wyze-tunnel.env, then choose Stop and then Start in the tray menu."
        elif os.path.exists("/.dockerenv"):
            out["where"] = "On Docker you can keep your compose file as it is. Just change the image to ghcr.io/itskiwi/wyze-tunnel."
        else:
            out["where"] = "Add these lines to /etc/wyze-tunnel/wyze-tunnel.env, then run: wyze-tunnel restart"
        return out

    @app.route("/alerts/mode", methods=["POST"])
    @auth_required
    def alerts_mode():
        try:
            webhooks.set_alert_mode(str((request.get_json(silent=True) or {}).get("mode")))
        except ValueError as ex:
            return {"error": str(ex)}, 400
        return {"status": "saved"}

    @app.route("/alerts/test", methods=["POST"])
    @auth_required
    def alerts_test():
        if not (webhooks.alert_url("test") or webhooks.ALERTS.get("chat_url")):
            return {"error": "Save a topic or a chat link first."}, 400
        if webhooks.send_webhook("test", "Wyze Tunnel", "Test alert from Wyze Tunnel. Alerts are working!"):
            return {"status": "sent"}
        return {"error": "Couldn't reach the alert server. Check the server address and your internet."}, 502

    @app.route("/recordings")
    @auth_required
    def recordings_page():
        cams = web_ui.all_cams(wb.streams, wb.api.total_cams)["cameras"]
        timelapse_from = {uri: cam["nickname"] for uri, cam in cams.items()}  # a timelapse needs snapshots, which phones don't make
        cams |= {c["uri"]: {"nickname": c["name"], "record": recording.wanted(c["uri"])} for c in phone_cams.list_cams()}
        # Cameras recording for another reason can't be switched off here, so the page says why.
        locked = {uri: "Always on in your config" for uri in cams if env_cam("record", uri)}
        locked.update({uri: "On for Search footage" for uri in cams if uri not in locked and detect.wanted(uri)})
        return render_template(
            "recordings.html",
            cameras={uri: cam for uri, cam in cams.items() if cam.get("record")},
            all_cams={uri: cam["nickname"] for uri, cam in cams.items()},
            locked=locked,
            settings=recording.load_settings(),
            keep_choices=recording.KEEP_CHOICES,
            max_gb_choices=recording.MAX_GB_CHOICES,
            min_free_gb=recording.MIN_FREE // 1024**3,
            default_keep=recording.DEFAULT_KEEP_DAYS,
            disk=recording.disk_use(),
            cloud=recording.load_cloud(),
            folder_example=backup.FOLDER_EXAMPLE,
            timelapses=timelapse.videos(),
            timelapse_cams=timelapse.load_cams(),
            timelapse_from=timelapse_from,
            selected=request.args.get("cam"),
            playback=PLAYBACK_ENABLED,
            api=WbAuth.api,
            version=config.VERSION,
        )

    @app.route("/history")
    @auth_required
    def history_page():
        return render_template("history.html", api=WbAuth.api, version=config.VERSION)

    @app.route("/history/events")
    @auth_required
    def history_events():
        a = request.args

        def num(key: str) -> int:
            return int(a[key]) if a.get(key, "").isdigit() else 0

        return event_archive.search(a.get("device", "")[:100], a.get("type", "")[:30], num("since"), num("until"), a.get("q", "")[:100], num("before"), num("limit") or 60)

    @app.route("/history/media/<string:eid>/<string:kind>")
    @auth_required
    def history_media(eid: str, kind: str):
        if not (path := event_archive.media_file(eid, kind)):
            return {"error": "Not found"}, 404
        return send_file(path, mimetype="image/jpeg" if kind == "img" else "video/mp4", conditional=True)

    @app.route("/history/<string:eid>/delete", methods=["POST"])
    @owner_only
    def history_delete(eid: str):
        return ({"status": "deleted"}, 200) if event_archive.delete(eid) else ({"error": "Not found"}, 404)

    @app.route("/history/clear", methods=["POST"])
    @owner_only
    def history_clear():
        event_archive.clear()
        return {"status": "cleared"}

    @app.route("/recordings/settings", methods=["POST"])
    @auth_required
    def recordings_settings():
        """Save which cameras record and for how long, then restart cameras so it takes effect."""
        if web_ui.current_guest():
            return {"error": "Only the owner can change this."}, 403
        try:
            recording.save_settings(request.get_json(silent=True) or {}, list(wb.streams.streams) + phone_cams.uris())
        except ValueError as ex:
            return {"error": str(ex)}, 400
        Thread(target=wb.restart, daemon=True).start()
        return {"status": "saved"}

    @app.route("/recordings/backup", methods=["POST"])
    @owner_only
    def recordings_backup():
        try:
            settings = recording.save_cloud(request.get_json(silent=True) or {}, list(wb.streams.streams))
        except ValueError as ex:
            return {"error": str(ex)}, 400
        if settings:  # start now rather than in up to 5 minutes
            Thread(target=recording.back_up_clips, daemon=True).start()
        return {"status": "saved", "on": bool(settings)}

    @app.route("/timelapse/settings", methods=["POST"])
    @auth_required
    def timelapse_settings():
        if web_ui.current_guest():
            return {"error": "Only the owner can change this."}, 403
        try:
            timelapse.save_cams((request.get_json(silent=True) or {}).get("cams"), list(wb.streams.streams))
        except ValueError as ex:
            return {"error": str(ex)}, 400
        return {"status": "saved"}

    @app.route("/timelapse/<string:cam_name>/<string:day>.mp4")
    @auth_required
    def timelapse_video(cam_name: str, day: str):
        if not (path := timelapse.video_path(cam_name, day)):
            return {"error": "No timelapse for that day"}, 404
        return send_file(path, mimetype="video/mp4", conditional=True)

    @app.route("/sd/<string:cam_name>")
    @owner_only
    def sd_day(cam_name: str):
        """A day on the camera's SD card as [start, end] epoch pairs. ?start= is the viewer's local midnight."""
        if not (start := request.args.get("start", "")).isdigit():
            return {"error": "start must be epoch seconds"}, 400
        sd_card, segments = False, []
        # shortcut: two 12-hour windows miss the last hour of a 25-hour DST day; ask for a third window if anyone notices.
        for half in (int(start), int(start) + 43200):
            resp = wb.streams.send_cmd(cam_name, "sd_minutes", half)
            if not isinstance(value := resp.get("value"), dict):
                return {"error": str(resp.get("response") or "The camera didn't answer.")}, 502
            sd_card |= value["sd_card"]
            for seg in value["segments"]:
                if segments and segments[-1][1] == seg[0]:  # a recording that runs across noon
                    segments[-1][1] = seg[1]
                else:
                    segments.append(seg)
        return {"sd_card": sd_card, "segments": segments}

    def start_sd(cam_name: str, ts) -> tuple[dict, int]:
        stream = wb.streams.get(cam_name)
        if not isinstance(stream, WyzeStream) or stream.source != "tutk":
            return {"error": "SD card playback needs the camera connected through the Wyze SDK."}, 400
        if not isinstance(ts, int) or not 0 < ts < time.time():
            return {"error": "ts must be epoch seconds in the past"}, 400
        if sd_copy.busy(cam_name):
            return {"error": "The overnight SD copy is using this camera right now."}, 409
        path = f"{cam_name}-sd"
        if path not in sd_playback.players:
            wb.mtx.add_path(path, False)
        sd_playback.start(cam_name, stream.user, stream.camera, stream.options, ts)
        return {"path": path, "hls": f"hls/{path}/index.m3u8"}, 200

    @app.route("/sd/<string:cam_name>/play", methods=["POST"])
    @owner_only
    def sd_play(cam_name: str):
        """Play the SD card from {"ts": epoch} at /<cam>-sd, or seek there if it's already playing."""
        return start_sd(cam_name, (request.get_json(silent=True) or {}).get("ts"))

    @app.route("/sd/<string:cam_name>/stop", methods=["POST"])
    @owner_only
    def sd_stop(cam_name: str):
        sd_playback.stop(f"{cam_name}-sd")
        return {"status": "stopped"}

    @app.route("/sd-copy/settings", methods=["POST"])
    @owner_only
    def sd_copy_settings():
        try:
            return {"status": "saved", "settings": sd_copy.save_settings(request.get_json(silent=True) or {}, list(wb.streams.streams))}
        except ValueError as ex:
            return {"error": str(ex)}, 400

    @app.route("/sd-copy/status")
    @owner_only
    def sd_copy_status():
        return {**sd_copy.status, "files": len(sd_copy.copied_files())}

    @app.route("/sd-sync")
    @owner_only
    def sd_sync():
        cams = web_ui.all_cams(wb.streams, wb.api.total_cams)["cameras"]
        return render_template(
            "sd_sync.html", cams={uri: cam["nickname"] for uri, cam in cams.items()}, sd_copy=sd_copy.load_settings(), api=WbAuth.api, version=config.VERSION
        )

    @app.route("/sd-sync/play", methods=["POST"])
    @owner_only
    def sd_sync_play():
        """Start 2-4 cameras' SD replay at the same {"ts": epoch}; each shows at /<cam>-sd."""
        data = request.get_json(silent=True) or {}
        cams = data.get("cams")
        if not isinstance(cams, list) or not 2 <= len(set(cams)) <= 4 or not all(isinstance(c, str) for c in cams):
            return {"error": "Pick 2 to 4 cameras."}, 400
        results = {cam: start_sd(cam, data.get("ts")) for cam in dict.fromkeys(cams)}
        return {"cams": {cam: r[0] for cam, r in results.items()}}, 200 if all(r[1] == 200 for r in results.values()) else 400

    @app.route("/sd-sync/stop", methods=["POST"])
    @owner_only
    def sd_sync_stop():
        for cam in (request.get_json(silent=True) or {}).get("cams") or []:
            if isinstance(cam, str):
                sd_playback.stop(f"{cam}-sd")
        return {"status": "stopped"}

    @app.route("/recordings/<string:cam_name>")
    @app.route("/api/v1/cameras/<string:cam_name>/recordings")  # for apps, like Home Assistant's media browser
    @auth_required
    def recordings(cam_name: str):
        """List recorded segments: [{"start": RFC3339, "duration": seconds}]."""
        resp = mtx_playback("list", path=cam_name)
        if not resp.ok:  # MediaMTX answers 404 when a camera has no recordings yet
            return [], 200 if resp.status_code == 404 else resp.status_code
        return [{"start": r["start"], "duration": r["duration"]} for r in resp.json()]

    @app.route("/recordings/<string:cam_name>/play")
    @app.route("/api/v1/cameras/<string:cam_name>/recordings/play")
    @auth_required
    def play_recording(cam_name: str):
        """Stream an MP4 of a recording. Needs ?start=<RFC3339>&duration=<seconds>."""
        args = {"start": request.args.get("start"), "duration": request.args.get("duration")}
        if not all(args.values()):
            return {"error": "start and duration are required"}, 400
        return recording_mp4(cam_name, args)

    @app.route("/recordings/<string:cam_name>/thumb")
    @auth_required
    def recording_thumb(cam_name: str):
        """A small picture from the start of a clip, made once and kept, for the clip list."""
        try:
            start = datetime.fromisoformat(request.args.get("start", "").replace("Z", "+00:00"))
        except ValueError:
            return {"error": "start must be a date and time"}, 400
        if not wb.streams.get(cam_name) and cam_name not in phone_cams.uris():
            return {"error": "Camera not found"}, 404
        thumb = Path(config.IMG_PATH, "clip-thumbs", cam_name, f"{int(start.timestamp())}.jpg")
        if not thumb.exists():
            thumb.parent.mkdir(parents=True, exist_ok=True)
            url = f"http://{PLAYBACK_ADDR}/get?" + urlencode({"path": cam_name, "format": "mp4", "start": request.args["start"], "duration": 2})
            cmd = ["ffmpeg", "-loglevel", "error", "-y", "-i", url, "-frames:v", "1", "-vf", "scale=320:-2", str(thumb)]
            try:
                subprocess.run(cmd, capture_output=True, timeout=20)
            except subprocess.TimeoutExpired:
                pass
            if not thumb.exists():
                return {"error": "No picture for that moment"}, 404
        return send_file(thumb, mimetype="image/jpeg", max_age=86400)

    @app.route("/recordings/<string:cam_name>/share", methods=["POST"])
    @auth_required
    def share_recording(cam_name: str):
        """A link anyone can open for 24 hours to watch one clip."""
        if not wb.streams.get(cam_name) and cam_name not in phone_cams.uris():
            return {"error": "Camera not found"}, 404
        data = request.get_json(silent=True) or {}
        try:
            token = shares.create_share(cam_name, data.get("start"), data.get("duration"))
        except ValueError as ex:
            return {"error": str(ex)}, 400
        return {"url": f"share/{token}", "hours": shares.SHARE_HOURS}

    @app.route("/share/<string:token>")
    def shared_recording(token: str):
        if not (share := shares.get_share(token)):
            return "This link has ended or doesn't exist.", 404
        if request.headers.get("Range", "bytes=0-").startswith("bytes=0-"):  # players fetch more ranges of one view
            shares.count_view(token)
        args = {"start": share["start"], "duration": share["duration"]}
        return recording_mp4(share["cam"], args, sound=share.get("sound", True), mask=masks.load_masks().get(share["cam"]))

    @app.route("/search")
    @auth_required
    def search_page():
        cams = web_ui.all_cams(wb.streams, wb.api.total_cams)["cameras"]
        return render_template(
            "search.html",
            cameras={uri: cam["nickname"] for uri, cam in cams.items()},
            enabled=detect.enabled(),
            settings=detect.load_settings(),
            env_frigate=bool(detect.FRIGATE_URL),
            can_detect=detect.cv2 is not None,
            keep_days=max(1, detect.KEEP.days),
            api=WbAuth.api,
            version=config.VERSION,
        )

    @app.route("/api/heatmap/<string:cam_name>")
    @auth_required
    def api_heatmap(cam_name: str):
        """Where things showed up on this camera: a grid of counts, row by row from the top left."""
        grid = detect.load_heat().get(cam_name) or detect.HEAT.get(cam_name) or []
        return {
            "w": detect.HEAT_W,
            "h": detect.HEAT_H,
            "grid": grid,
            "ignore": detect.load_zones().get(cam_name, []),
            "mask": masks.load_masks().get(cam_name, []),
        }

    @app.route("/api/zones/<string:cam_name>", methods=["POST"])
    @owner_only
    def api_zones(cam_name: str):
        """Save the squares of the picture detection should ignore on this camera."""
        if cam_name not in wb.streams.streams:
            return {"error": "No camera by that name"}, 404
        try:
            return {"ignore": detect.save_zone(cam_name, (request.get_json(silent=True) or {}).get("ignore"))}
        except ValueError as ex:
            return {"error": str(ex)}, 400

    @app.route("/api/masks/<string:cam_name>", methods=["POST"])
    @owner_only
    def api_masks(cam_name: str):
        """Save the squares of the picture to black out in shared clips of this camera."""
        if cam_name not in wb.streams.streams:
            return {"error": "No camera by that name"}, 404
        try:
            return {"mask": masks.save_mask(cam_name, (request.get_json(silent=True) or {}).get("mask"))}
        except ValueError as ex:
            return {"error": str(ex)}, 400

    @app.route("/search/settings", methods=["POST"])
    @auth_required
    def search_settings():
        """Save which cameras to watch and the Frigate address, then restart cameras so it takes effect."""
        if web_ui.current_guest():
            return {"error": "Only the owner can change this."}, 403
        try:
            detect.save_settings(request.get_json(silent=True) or {}, list(wb.streams.streams))
        except ValueError as ex:
            return {"error": str(ex)}, 400
        Thread(target=wb.restart, daemon=True).start()
        return {"status": "saved"}

    @app.route("/api/detections")
    @auth_required
    def detections():
        """What was spotted, newest first. Filter with ?label=person&cam=front-door; page with ?before=<ts>."""
        try:
            before = float(request.args.get("before") or 0)
        except ValueError:
            return {"error": "before should be a Unix time"}, 400
        hits = detect.search(request.args.get("label", ""), request.args.get("cam", ""), before)
        for hit in hits:
            hit["thumb"] = f"detections/{hit['id']}.jpg"
            if hit["source"] == "frigate":
                hit["clip"] = f"detections/{hit['id']}.mp4"
            else:  # a 30-second window of the recording, starting just before the detection
                start = datetime.fromtimestamp(hit["ts"] - 5, timezone.utc).isoformat()
                hit["clip"] = f"recordings/{quote_plus(hit['cam'])}/play?start={quote_plus(start)}&duration=30"
        return {"labels": detect.labels(), "detections": hits}

    @app.route("/detections/reel.mp4")
    @auth_required
    def detections_reel():
        """The last day's hits as a short video, for one camera or all of them."""
        cam = request.args.get("cam", "")
        if cam and cam not in wb.streams.streams:
            return {"error": "No camera by that name"}, 404
        if not (path := detect.reel(cam)):
            return {"error": "Nothing was spotted in the last day."}, 404
        return send_from_directory(path.parent, path.name, mimetype="video/mp4", max_age=0)

    @app.route("/detections/<int:det_id>.<any(jpg, mp4):ext>")
    @auth_required
    def detection_media(det_id: int, ext: str):
        if not (hit := detect.get(det_id)):
            return redirect("/static/notavailable.svg", code=307)
        if hit["source"] != "frigate":  # local clips play from the recordings
            return send_from_directory(detect.THUMB_DIR, f"{det_id}.jpg") if ext == "jpg" else ("", 404)
        try:
            resp = detect.frigate_media(hit["ext_id"], "thumbnail.jpg" if ext == "jpg" else "clip.mp4")
        except requests.RequestException:
            return {"error": "Couldn't reach Frigate"}, 502
        if not resp.ok:
            return {"error": "Frigate doesn't have this clip any more"}, resp.status_code
        return Response(resp.iter_content(64 * 1024), mimetype=resp.headers.get("content-type", f"image/{ext}"))

    def recording_mp4(cam_name: str, args: dict, sound: bool = True, mask: list[int] | None = None):
        name = f"{cam_name}_{args['start']}.mp4".replace(":", "-")
        headers = {"Content-Disposition": f'inline; filename="{name}"'}
        if not sound or mask:
            url = f"http://{PLAYBACK_ADDR}/get?" + urlencode({"path": cam_name, "format": "mp4", **args})
            return Response(remux_mp4(url, sound, masks.video_filter(mask or [])), mimetype="video/mp4", headers=headers)
        resp = mtx_playback("get", stream=True, path=cam_name, format="mp4", **args)
        if not resp.ok:
            return {"error": resp.text.strip()}, resp.status_code
        return Response(resp.iter_content(64 * 1024), mimetype="video/mp4", headers=headers)

    def remux_mp4(url: str, sound: bool = True, vf: str = ""):
        """The clip as it streams, without its sound track if asked, and re-encoded with privacy masks drawn on."""
        cmd = ["ffmpeg", "-loglevel", "error", "-i", url]
        cmd += ["-vf", vf, "-c:v", "libx264", "-preset", "veryfast", "-crf", "23"] if vf else ["-c:v", "copy"]
        cmd += ["-c:a", "copy"] if sound else ["-an"]
        cmd += ["-movflags", "frag_keyframe+empty_moov", "-f", "mp4", "pipe:1"]
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        try:
            while chunk := proc.stdout.read(64 * 1024):
                yield chunk
        finally:
            proc.kill()
            proc.wait()

    app.register_blueprint(api_v1.blueprint(wb, auth_required))

    @app.route("/apps")
    @owner_only
    def apps_page():
        return render_template(
            "apps.html",
            tokens=integrations.list_tokens(),
            hooks=integrations.list_hooks(),
            hook_events=integrations.HOOK_EVENTS,
            api=WbAuth.api,
            version=config.VERSION,
            homekit=homekit_cams(),
            homekit_ready=go2rtc.go2rtc_available(),
        )

    def homekit_cams() -> list[dict]:
        homekit.keep_pairings(go2rtc.GO2RTC_CONFIG)  # so a camera just added in the Home app shows as added
        saved = homekit.cameras()
        cams = []
        for uri, name in WyzeStream.go2rtc.names.items():
            cam = {"uri": uri, "name": name, "on": uri in saved}
            if hk := saved.get(uri):
                cam |= {"paired": bool(hk["pairings"]), "code": homekit.pin_text(hk["pin"]), "qr": homekit.qr_svg(uri, hk["pin"])}
            cams.append(cam)
        return cams

    @app.route("/apps/homekit/<string:uri>", methods=["POST"])
    @owner_only
    def set_homekit(uri: str):
        if uri not in WyzeStream.go2rtc.names:
            return {"error": "No camera with that name."}, 404
        homekit.keep_pairings(go2rtc.GO2RTC_CONFIG)
        homekit.set_enabled(uri, bool((request.get_json(silent=True) or {}).get("on")))
        WyzeStream.go2rtc.restart()
        return {"status": "saved"}

    @app.route("/apps/tokens", methods=["POST"])
    @owner_only
    def add_app_token():
        data = request.get_json(silent=True) or {}
        try:
            return {"token": integrations.add_token(data.get("name"), data.get("scope"))}
        except ValueError as ex:
            return {"error": str(ex)}, 400

    @app.route("/apps/tokens/<string:token_id>", methods=["DELETE"])
    @owner_only
    def remove_app_token(token_id: str):
        integrations.remove_token(token_id)
        return {"status": "removed"}

    @app.route("/apps/hooks", methods=["POST"])
    @owner_only
    def add_app_hook():
        data = request.get_json(silent=True) or {}
        try:
            return {"secret": integrations.add_hook(data.get("url"), data.get("events"))}
        except ValueError as ex:
            return {"error": str(ex)}, 400

    @app.route("/apps/hooks/<string:hook_id>", methods=["DELETE"])
    @owner_only
    def remove_app_hook(hook_id: str):
        integrations.remove_hook(hook_id)
        return {"status": "removed"}

    @app.route("/apps/hooks/<string:hook_id>/test", methods=["POST"])
    @owner_only
    def test_app_hook(hook_id: str):
        try:
            return integrations.test_hook(hook_id)
        except ValueError as ex:
            return {"error": str(ex)}, 404

    @app.route("/street-watch", methods=["GET", "POST"])
    @owner_only
    def street_watch_page():
        if request.method == "POST":
            street_watch.set_enabled(bool((request.get_json(silent=True) or {}).get("enabled")))
            return {"status": "saved"}
        cams = web_ui.all_cams(wb.streams, wb.api.total_cams)["cameras"]
        return render_template(
            "street_watch.html",
            street=street_watch.state(),
            cameras={uri: cam for uri, cam in cams.items() if cam.get("record")},
            api=WbAuth.api,
            version=config.VERSION,
        )

    @app.route("/street-watch/links", methods=["POST"])
    @owner_only
    def street_watch_link():
        try:
            token = street_watch.add_link((request.get_json(silent=True) or {}).get("name"))
        except ValueError as ex:
            return {"error": str(ex)}, 400
        return {"url": f"street/{token}"}

    @app.route("/street-watch/links/<string:token>", methods=["DELETE"])
    @owner_only
    def street_watch_end_link(token: str):
        street_watch.end_link(token)
        return {"status": "ended"}

    @app.route("/street-watch/requests/<string:req_id>/<string:action>", methods=["POST"])
    @owner_only
    def street_watch_answer(req_id: str, action: str):
        data = request.get_json(silent=True) or {}
        try:
            if action == "approve":
                if not (stream := wb.streams.get(cam := str(data.get("cam")))):
                    return {"error": "Pick a camera."}, 400
                token = street_watch.approve(
                    req_id, cam, stream.camera.mac, data.get("start"), data.get("duration"), data.get("sound") is True
                )
                return {"url": f"share/{token}", "hours": shares.SHARE_HOURS}
            if action not in ("decline", "revoke"):
                return {"error": "Unknown action"}, 404
            getattr(street_watch, action)(req_id)
        except ValueError as ex:
            return {"error": str(ex)}, 400
        return {"status": "saved"}

    @app.route("/street/<string:token>", methods=["GET", "POST"])
    def street_request(token: str):
        """A neighbor's request form. Shows nothing about the cameras, and the same reply whether or not there's footage."""
        if request.method == "POST":
            try:
                name = street_watch.submit(token, request.get_json(silent=True) or {}, request.remote_addr or "")
            except LookupError:
                return {"error": "This link has ended or doesn't exist."}, 404
            except ValueError as ex:
                return {"error": str(ex)}, 400
            Thread(target=webhooks.send_webhook, args=("street", "Street Watch", f"{name} sent a footage request."), daemon=True).start()
            return {"status": "sent"}
        name = street_watch.link_name(token)
        resp = make_response(render_template("street_request.html", name=name, hide_nav=True), 200 if name else 404)
        resp.headers["X-Robots-Tag"] = "noindex, nofollow"
        resp.headers["Referrer-Policy"] = "no-referrer"
        return resp

    @app.route("/hls/<path:path>")
    @auth_required
    def hls_proxy(path: str):
        """Live HLS behind the web login; MediaMTX only lets 127.0.0.1 in without the API key."""
        scheme = "https" if config.LLHLS else "http"
        try:  # on-demand cameras take a few seconds to start; MediaMTX waits up to 30s
            resp = requests.get(f"{scheme}://127.0.0.1:8888/{path}", params=request.args, stream=True, timeout=35, verify=False)
        except requests.RequestException as ex:
            return str(ex), 503
        return Response(resp.iter_content(64 * 1024), resp.status_code, content_type=resp.headers.get("Content-Type"))

    def mtx_playback(endpoint: str, stream: bool = False, **params) -> requests.Response:
        try:
            return requests.get(
                f"http://{PLAYBACK_ADDR}/{endpoint}", params=params, stream=stream, timeout=10
            )
        except requests.RequestException as ex:
            resp = requests.Response()
            resp.status_code, resp._content = 503, str(ex).encode()
            return resp

    @app.route("/ha-dashboard.yaml")
    @auth_required
    def ha_dashboard():
        """Paste into Home Assistant: Settings, Dashboards, Add, then Raw configuration editor."""
        cams = web_ui.all_cams(wb.streams, wb.api.total_cams)["cameras"]
        body = yaml.safe_dump(web_ui.ha_dashboard(cams), sort_keys=False, allow_unicode=True)
        return Response(body, mimetype="text/yaml")

    @app.route("/cams.m3u8")
    @auth_required
    def iptv_playlist():
        """
        Generate an m3u8 playlist with all enabled cameras.
        """
        cameras = web_ui.format_streams(wb.streams.get_all_cam_info())
        resp = make_response(render_template("m3u8.html", cameras=cameras))
        resp.headers.set("content-type", "application/x-mpegURL")
        return resp

    return app


if __name__ == "__main__":
    app = create_app()
    app.run(debug=False, host="0.0.0.0", port=5000)
