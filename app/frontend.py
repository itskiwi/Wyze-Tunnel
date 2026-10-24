import os
import secrets
import time
from threading import Thread
from functools import wraps
from pathlib import Path
from urllib.parse import quote_plus

from flask import (
    Flask,
    Response,
    make_response,
    redirect,
    render_template,
    request,
    send_from_directory,
)
import requests
from werkzeug.exceptions import NotFound
from wyze_bridge import WyzeBridge
from wyzebridge import config, web_ui, webhooks, wyze_api
from wyzebridge.auth import WbAuth
from wyzebridge.diagnostics import build_report
from wyzebridge.ffmpeg import probe_stream, stream_warnings
from wyzebridge.mtx_server import PLAYBACK_ADDR, PLAYBACK_ENABLED
from wyzebridge.web_ui import url_for
from wyzebridge.wyze_events import EVENT_LOG


def create_app():
    app = Flask(__name__)
    app.jinja_env.globals["can_update"] = bool(os.getenv("WT_UPDATE_FILE"))
    wb = WyzeBridge()
    try:
        wb.start()
    except RuntimeError as ex:
        print(ex)
        print("Please ensure your host is up to date.")
        exit()

    def auth_required(view):
        @wraps(view)
        def wrapped_view(*args, **kwargs):
            if not wb.api.auth:
                return redirect(url_for("wyze_login"))
            return web_ui.auth.login_required(view)(*args, **kwargs)

        return wrapped_view

    # One-time token so only the browser that ran the setup wizard sees the web password.
    setup = {"token": ""}

    @app.before_request
    def block_cross_site():
        # Images and links to the UI stay embeddable; everything else (commands, restarts, login) is same-site only.
        if request.path.startswith(("/static/", "/img/", "/snapshot/", "/thumb/", "/photo/")):
            return
        if request.method == "GET" and request.path in ("/", "/login", "/cams.m3u8"):
            return
        if web_ui.cross_site_request():
            return {"error": "Cross-site request blocked"}, 403

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
        resp = make_response(
            render_template(
                "index.html",
                cam_data=web_ui.all_cams(wb.streams, wb.api.total_cams),
                number_of_columns=number_of_columns,
                refresh_period=refresh_period,
                api=WbAuth.api,
                version=config.VERSION,
                webrtc=bool(config.BRIDGE_IP),
                show_video=show_video,
                video_format=video_format.lower(),
                autoplay=autoplay,
            )
        )

        resp.set_cookie("number_of_columns", str(number_of_columns))
        resp.set_cookie("refresh_period", str(refresh_period))
        resp.set_cookie("show_video", "1" if show_video else "")
        resp.set_cookie("video", video_format)
        fullscreen = "fullscreen" in request.args or bool(
            request.cookies.get("fullscreen")
        )
        resp.set_cookie("fullscreen", "1" if fullscreen else "")
        if order := request.args.get("order"):
            resp.set_cookie("camera_order", quote_plus(order))

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

    @app.route("/signaling/<string:name>")
    @auth_required
    def webrtc_signaling(name):
        if "kvs" in request.args:
            return wb.api.get_kvs_signal(name)
        return web_ui.get_webrtc_signal(name, WbAuth.api)

    @app.route("/webrtc/<string:name>")
    @auth_required
    def webrtc(name):
        """View WebRTC direct from camera."""
        if (webrtc := wb.api.get_kvs_signal(name)).get("result") == "ok":
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

    @app.route("/update", methods=["POST"])
    @auth_required
    def self_update():
        """Native installs: ask wyze-tunnel-update.path to install the latest release."""
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
            wb.streams.monitor_streams(wb.mtx.health_check)
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
        return render_template(
            "health.html",
            cameras=web_ui.all_cams(wb.streams, wb.api.total_cams)["cameras"],
            now=time.time(),
            api=WbAuth.api,
            version=config.VERSION,
        )

    @app.route("/events")
    @auth_required
    def events():
        return render_template(
            "events.html",
            events=list(EVENT_LOG),
            motion_on=bool(config.MOTION or webhooks.ALERTS.get("motion")),
            api=WbAuth.api,
            version=config.VERSION,
        )

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
            {"mac": c.mac, "nickname": c.nickname, "model": c.model_name, "hidden": c.mac in hidden}
            for c in cams
        ]

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
            data = request.get_json(silent=True) or request.form.to_dict()
            try:
                WbAuth.set_login(data.get("username", ""), data.get("password", ""))
            except ValueError as ex:
                return {"error": str(ex)}, 400
            return {"status": "saved"}
        return render_template(
            "account.html",
            username=WbAuth.username,
            enabled=WbAuth.enabled,
            api=WbAuth.api,
            version=config.VERSION,
        )

    @app.route("/alerts/test", methods=["POST"])
    @auth_required
    def alerts_test():
        if not webhooks.alert_url("test"):
            return {"error": "Save a topic first."}, 400
        if webhooks.send_webhook("test", "Wyze Tunnel", "Test alert from Wyze Tunnel. Alerts are working!"):
            return {"status": "sent"}
        return {"error": "Couldn't reach the alert server. Check the server address and your internet."}, 502

    @app.route("/recordings")
    @auth_required
    def recordings_page():
        cams = web_ui.all_cams(wb.streams, wb.api.total_cams)["cameras"]
        return render_template(
            "recordings.html",
            cameras={uri: cam for uri, cam in cams.items() if cam.get("record")},
            selected=request.args.get("cam"),
            playback=PLAYBACK_ENABLED,
            api=WbAuth.api,
            version=config.VERSION,
        )

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
        resp = mtx_playback("get", stream=True, path=cam_name, format="mp4", **args)
        if not resp.ok:
            return {"error": resp.text.strip()}, resp.status_code
        name = f"{cam_name}_{args['start']}.mp4".replace(":", "-")
        return Response(
            resp.iter_content(64 * 1024),
            mimetype="video/mp4",
            headers={"Content-Disposition": f'inline; filename="{name}"'},
        )

    def mtx_playback(endpoint: str, stream: bool = False, **params) -> requests.Response:
        try:
            return requests.get(
                f"http://{PLAYBACK_ADDR}/{endpoint}", params=params, stream=stream, timeout=10
            )
        except requests.RequestException as ex:
            resp = requests.Response()
            resp.status_code, resp._content = 503, str(ex).encode()
            return resp

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
