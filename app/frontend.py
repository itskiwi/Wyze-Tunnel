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
from wyzebridge import api_v1, backup, config, detect, floorplan, go2rtc, guests, homekit, integrations, masks, network, pocket, privacy, recording, shares, street_watch, timelapse, visitors, web_ui, webhooks, wyze_api
from wyzebridge.auth import WbAuth
from wyzebridge.bridge_utils import env_cam
from wyzebridge.diagnostics import build_report
from wyzebridge.ffmpeg import probe_stream, stream_warnings
from wyzebridge.mtx_server import PLAYBACK_ADDR, PLAYBACK_ENABLED, path_bytes
from wyzebridge.web_ui import url_for
from wyzebridge.wyze_events import EVENT_LOG
from wyzebridge.wyze_stream import WyzeStream
from wyzecam.api import API_STATS


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
        if request.method == "GET" and (request.path in ("/", "/live", "/login", "/cams.m3u8") or request.path.startswith("/street/")):
            return
        if web_ui.cross_site_request():
            return {"error": "Cross-site request blocked"}, 403

    @app.before_request
    def public_link_paired_only():
        # Pages without a login (Wyze sign-in, setup, shares...) stay home-only too: the public link only pairs.
        if pocket.public_request() and not request.path.startswith(("/static/", "/pair/")) and not web_ui.verify_password("", ""):
            return render_template("pair.html", public_only=True), 403

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
            web_ui.sse_generator(wb.streams.get_sse_status),
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

        return wb.streams.send_cmd(cam_name, cam_cmd.lower(), payload)

    def shared(name: str) -> bool:
        """Whether viewers can share the tunnel's own camera connection instead of opening one each.

        Matters for cameras that take few viewers, like the Cam OG (two at once).
        """
        return bool(config.BRIDGE_IP) and bool((stream := wb.streams.get(name)) and stream.enabled)

    @app.route("/signaling/<string:name>")
    @auth_required
    def webrtc_signaling(name):
        # "relay" is go2rtc fetching the one connection the tunnel holds.
        if "kvs" in request.args and ("relay" in request.args or not shared(name)):
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
            now=time.time(),
            api=WbAuth.api,
            version=config.VERSION,
        )

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
        """Play Wyze's cloud clip of a motion event. Only Wyze's own hosts are fetched."""
        url = request.args.get("u", "")
        if not wyze_api.wyze_media_url(url):
            return {"error": "Not a Wyze clip"}, 400
        try:
            resp = requests.get(url, stream=True, timeout=15, allow_redirects=False)
            if resp.status_code == 401 and wb.api.auth:
                headers = {"Authorization": wb.api.auth.access_token}
                resp = requests.get(url, stream=True, timeout=15, allow_redirects=False, headers=headers)
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
        hidden = wyze_api.load_hidden_cams()
        return [
            {"mac": c.mac, "nickname": wyze_api.display_name(c), "model": c.model_name, "hidden": c.mac in hidden}
            for c in cams
        ]

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
            auto_folder=backup.load_auto().get("folder", ""),
            folder_example=r"D:\Wyze Tunnel backups" if os.name == "nt" else "/media/backups",
            member=web_ui.current_guest(),
            guest_hours=guests.GUEST_HOURS,
            username=WbAuth.username,
            enabled=WbAuth.enabled,
            api=WbAuth.api,
            away=pocket.Away.status(),
            version=config.VERSION,
        )

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
        host, _, port = request.host.rpartition(":") if request.host.count(":") == 1 else (request.host, "", "")
        port = f":{port}" if port else ""
        if host in ("localhost", "127.0.0.1", "[::1]"):
            host = pocket.lan_ip()
        codes = {"home": f"{request.scheme}://{host}{port}{path}"}
        if public := pocket.Away.status()["public"]:
            codes["anywhere"] = public + path  # the phone's cookie belongs to this address, so it works anywhere
        elif (ts_host := pocket.tailscale_host()) and ts_host != host:
            # The bundled Tailscale serves the web UI on port 80.
            codes["anywhere"] = f"http://{ts_host}{'' if ts_host == pocket.Away.status()['host'] else port}{path}"
        return {k: {"url": url, "qr": pocket.qr_svg(url)} for k, url in codes.items()}

    @app.route("/away", methods=["GET", "POST"])
    @owner_only
    def away():
        if request.method == "POST":
            if not pocket.Away.available():
                return {"error": "Away access isn't included in this install."}, 400
            pocket.Away.turn(bool((request.get_json(silent=True) or {}).get("on")))
        return pocket.Away.status()

    @app.route("/pair/<string:code>")
    def pair_phone(code: str):
        if not WbAuth.enabled:
            return redirect(url_for("index"))
        if not (cookie := pocket.claim(code, request.user_agent.string, WbAuth.username)):
            return render_template("pair.html", version=config.VERSION), 410
        resp = redirect(url_for("index", pocket=1))
        resp.set_cookie(pocket.COOKIE, cookie, max_age=pocket.COOKIE_SECONDS, httponly=True, samesite="Lax", secure=request.is_secure)
        return resp

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
            timelapses=timelapse.videos(),
            timelapse_cams=timelapse.load_cams(),
            selected=request.args.get("cam"),
            playback=PLAYBACK_ENABLED,
            api=WbAuth.api,
            version=config.VERSION,
        )

    @app.route("/recordings/settings", methods=["POST"])
    @auth_required
    def recordings_settings():
        """Save which cameras record and for how long, then restart cameras so it takes effect."""
        if web_ui.current_guest():
            return {"error": "Only the owner can change this."}, 403
        try:
            recording.save_settings(request.get_json(silent=True) or {}, list(wb.streams.streams))
        except ValueError as ex:
            return {"error": str(ex)}, 400
        Thread(target=wb.restart, daemon=True).start()
        return {"status": "saved"}

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

    @app.route("/recordings/<string:cam_name>")
    @auth_required
    def recordings(cam_name: str):
        """List recorded segments: [{"start": RFC3339, "duration": seconds}]."""
        resp = mtx_playback("list", path=cam_name)
        if not resp.ok:  # MediaMTX answers 404 when a camera has no recordings yet
            return [], 200 if resp.status_code == 404 else resp.status_code
        return [{"start": r["start"], "duration": r["duration"]} for r in resp.json()]

    @app.route("/recordings/<string:cam_name>/play")
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
        if not wb.streams.get(cam_name):
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
        if not wb.streams.get(cam_name):
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
