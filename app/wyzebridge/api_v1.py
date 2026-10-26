"""The documented API for other apps (/api/v1), and an MCP server on top of it for AI apps. See docs/apps-and-api.md."""
import base64
import json
from pathlib import Path

import requests
from flask import Blueprint, request, send_from_directory
from wyzebridge import config, integrations, web_ui, webhooks
from wyzebridge.auth import WbAuth
from wyzebridge.bridge_utils import env_bool
from wyzebridge.wyze_events import EVENT_LOG
from wyzebridge.wyze_stream import StreamStatus

MCP_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")


def event_type(label) -> str:
    """motion, doorbell, or what the detector saw (person, car, ...)."""
    return {None: "motion", "": "motion", "rang the doorbell": "doorbell"}.get(label, label)


def camera_json(uri: str, cam: dict) -> dict:
    """One camera in the shape the API documents. Stream links carry a read-only login."""
    if isinstance(web_ui.auth.current_user(), web_ui.SimpleNamespace):  # an app token
        user, key = integrations.STREAM_USER, integrations.stream_key()
    else:
        user, key = "wb", WbAuth.api
    login = f"{user}:{key}@" if key else ""
    status = cam.get("status")
    try:
        state = StreamStatus(status).name.lower()
    except ValueError:
        state = "unknown"
    info = cam.get("camera_info") or {}
    wifi = web_ui._int((info.get("basicInfo") or {}).get("wifidb"))
    last = next((e for e in EVENT_LOG if e.get("uri") == uri), None)
    return {
        "id": uri,
        "name": cam.get("nickname") or uri,
        "model": cam.get("model_name"),
        "state": state,
        "online": status != StreamStatus.OFFLINE,
        "streaming": status == StreamStatus.CONNECTED,
        "enabled": bool(cam.get("enabled")),
        "motion": bool(cam.get("motion")),
        "last_motion": cam.get("motion_ts") or None,
        "recording": bool(cam.get("record")),
        "wifi": wifi if wifi >= 0 else None,
        "issues": [i["issue"] for i in web_ui.bridge_issues({uri: cam})],
        "last_event": last and {"type": event_type(last.get("label")), "ts": last.get("ts")},
        "features": {f: bool(cam.get(f"is_{f}")) for f in ("doorbell", "pan_cam", "floodlight", "spotlight", "battery")},
        "streams": {
            "rtsp": cam["rtsp_url"].replace("rtsp://", f"rtsp://{login}", 1),
            "hls": cam["hls_url"],
            "webrtc": cam.get("webrtc_url"),
        },
        "stream_login": {"username": user, "password": key} if key else None,
        "snapshot": f"api/v1/cameras/{uri}/snapshot",
    }


def blueprint(wb, auth_required) -> Blueprint:
    api = Blueprint("api_v1", __name__, url_prefix="/api/v1")

    def cameras() -> dict:
        cams = web_ui.all_cams(wb.streams, wb.api.total_cams)["cameras"]
        return {uri: camera_json(uri, cam) for uri, cam in cams.items()}

    def health() -> dict:
        cams = web_ui.all_cams(wb.streams, wb.api.total_cams)["cameras"]
        issues = web_ui.bridge_issues(cams)
        return {"status": "degraded" if issues else "ok", "cameras": len(cams), "issues": issues}

    def events(limit: int = 50) -> list:
        return list(EVENT_LOG)[: max(1, min(limit, 100))]

    def command(cam: str, cmd: str, value=None) -> tuple[dict, int]:
        if web_ui.api_scope() != "control":
            return {"error": "This token can only view."}, 403
        if not wb.streams.get(cam):
            return {"error": f"No camera called {cam}"}, 404
        return wb.streams.send_cmd(cam, str(cmd).lower(), "" if value is None else value), 200

    def set_mode(mode) -> tuple[dict, int]:
        """Home, Away or Normal (schedule), for phone shortcuts and location automations."""
        if web_ui.api_scope() != "control":
            return {"error": "This token can only view."}, 403
        try:
            webhooks.set_alert_mode(str(mode).lower())
        except ValueError as ex:
            return {"error": str(ex)}, 400
        return {"mode": webhooks.ALERTS.get("mode", "schedule")}, 200

    def snapshot_file(cam: str) -> str:
        """A fresh picture when the camera is streaming, else the latest one from the Wyze cloud."""
        img = f"{cam}.{env_bool('IMG_TYPE', 'jpg')}"
        if wb.streams.get_rtsp_snap(cam) or wb.api.save_thumbnail(cam) or Path(config.IMG_PATH, img).exists():
            return img
        return ""

    @api.get("/")
    @auth_required
    def index():
        return {"name": "Wyze Tunnel", "version": config.VERSION, "scope": web_ui.api_scope()}

    @api.get("/cameras")
    @auth_required
    def list_cameras():
        return list(cameras().values())

    @api.get("/cameras/<string:cam>")
    @auth_required
    def get_camera(cam: str):
        if found := cameras().get(cam):
            return found
        return {"error": f"No camera called {cam}"}, 404

    @api.get("/cameras/<string:cam>/snapshot")
    @auth_required
    def get_snapshot(cam: str):
        if not wb.streams.get(cam):
            return {"error": f"No camera called {cam}"}, 404
        if img := snapshot_file(cam):
            return send_from_directory(config.IMG_PATH, img, max_age=0)
        return {"error": "No picture yet. Try again once the camera is online."}, 503

    @api.get("/cameras/<string:cam>/last-event.jpg")
    @auth_required
    def get_last_event_image(cam: str):
        """The picture from this camera's newest event: Wyze's cloud thumbnail or the detector's crop."""
        last = next((e for e in EVENT_LOG if e.get("uri") == cam), None)
        img = (last or {}).get("img") or ""
        if img.startswith("https://"):
            try:
                resp = wb.api.get_media(img, timeout=10)
            except requests.RequestException:
                return {"error": "Couldn't get the picture from Wyze."}, 502
            if resp.ok:
                return resp.content, 200, {"Content-Type": "image/jpeg", "Cache-Control": "no-store"}
        elif img and Path(config.IMG_PATH, img).is_file():
            return send_from_directory(config.IMG_PATH, img, max_age=0)
        return {"error": "No picture for the last event."}, 404

    @api.post("/cameras/<string:cam>/commands/<string:cmd>")
    @auth_required
    def post_command(cam: str, cmd: str):
        body = request.get_json(silent=True) or {}
        return command(cam, cmd, body.get("value") if isinstance(body, dict) else None)

    @api.get("/events")
    @auth_required
    def list_events():
        return events(request.args.get("limit", 50, type=int))

    @api.get("/health")
    @auth_required
    def get_health():
        return health()

    @api.get("/mode")
    @auth_required
    def get_mode():
        return {"mode": webhooks.ALERTS.get("mode", "schedule"), "modes": list(webhooks.ALERT_MODES)}

    @api.post("/mode")
    @auth_required
    def post_mode():
        body = request.get_json(silent=True)
        return set_mode(body.get("mode") if isinstance(body, dict) else None)

    # MCP over Streamable HTTP, plain JSON replies (no server-sent stream).
    tools = [
        {
            "name": "list_cameras",
            "description": "List every camera with whether it's online, streaming or seeing motion.",
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "get_snapshot",
            "description": "Take a picture from a camera right now.",
            "inputSchema": {
                "type": "object",
                "properties": {"camera": {"type": "string", "description": "Camera id from list_cameras"}},
                "required": ["camera"],
            },
        },
        {
            "name": "recent_events",
            "description": "Recent motion events, newest first.",
            "inputSchema": {"type": "object", "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 100}}},
        },
        {
            "name": "bridge_health",
            "description": "Whether Wyze Tunnel is healthy, with any camera problems (offline, weak Wi-Fi, full SD card).",
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "set_mode",
            "description": "Switch alerts to home (no motion alerts), away (alerts even in quiet hours) or schedule "
            "(normal, quiet hours apply). Needs a control token.",
            "inputSchema": {
                "type": "object",
                "properties": {"mode": {"type": "string", "enum": ["home", "away", "schedule"]}},
                "required": ["mode"],
            },
        },
        {
            "name": "send_command",
            "description": "Change a camera setting, like night_vision on/off, spotlight on/off, alarm on/off, "
            "rotary_action left/right/up/down, or state start/stop. Needs a control token.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "camera": {"type": "string"},
                    "command": {"type": "string"},
                    "value": {"type": ["string", "number", "boolean", "object", "array"]},
                },
                "required": ["camera", "command"],
            },
        },
    ]

    def call_tool(name: str, args: dict) -> dict:
        def text(data, error: bool = False) -> dict:
            return {"content": [{"type": "text", "text": json.dumps(data)}], "isError": error}

        if name == "list_cameras":
            return text(list(cameras().values()))
        if name == "recent_events":
            limit = args.get("limit")
            return text(events(limit if isinstance(limit, int) else 20))
        if name == "bridge_health":
            return text(health())
        if name == "get_snapshot":
            cam = str(args.get("camera"))
            if not wb.streams.get(cam):
                return text({"error": f"No camera called {cam}"}, True)
            if not (img := snapshot_file(cam)):
                return text({"error": "No picture yet."}, True)
            data = base64.b64encode(Path(config.IMG_PATH, img).read_bytes()).decode()
            return {"content": [{"type": "image", "data": data, "mimeType": "image/jpeg"}], "isError": False}
        if name == "set_mode":
            result, code = set_mode(args.get("mode"))
            return text(result, code != 200)
        if name == "send_command":
            result, code = command(str(args.get("camera")), str(args.get("command")), args.get("value"))
            return text(result, code != 200)
        return text({"error": f"Unknown tool {name}"}, True)

    @api.route("/mcp", methods=["GET", "POST", "DELETE"])
    @auth_required
    def mcp():
        if request.method != "POST":
            return {"error": "Send JSON-RPC with POST."}, 405, {"Allow": "POST"}
        msg = request.get_json(silent=True)
        if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
            return {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid request"}}, 400
        if "id" not in msg:  # a notification, like notifications/initialized
            return "", 202
        method, params = msg.get("method"), msg.get("params") or {}
        if method == "initialize":
            asked = params.get("protocolVersion")
            result = {
                "protocolVersion": asked if asked in MCP_VERSIONS else MCP_VERSIONS[0],
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "wyze-tunnel", "version": config.VERSION},
                "instructions": "Cameras from Wyze Tunnel. Use list_cameras first to get camera ids.",
            }
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            visible = tools if web_ui.api_scope() == "control" else tools[:-2]
            result = {"tools": visible}
        elif method == "tools/call":
            result = call_tool(str(params.get("name")), params.get("arguments") or {})
        else:
            return {"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32601, "message": f"Unknown method {method}"}}
        return {"jsonrpc": "2.0", "id": msg["id"], "result": result}

    return api
