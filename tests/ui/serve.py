"""The web UI with six made-up cameras and a day of made-up events, for tests/ui/check.js. Nothing here is real."""
import os, sys, tempfile, time
from pathlib import Path
from unittest import mock
APP = Path(__file__).resolve().parents[2] / "app"
TMP = Path(tempfile.mkdtemp(prefix="wt-demo-"))
os.environ.update(TOKEN_PATH=str(TMP/"tokens"), IMG_DIR=str(TMP/"img"), MEDIAMTX_CONFIG=str(TMP/"mtx.yml"))
(TMP/"mtx.yml").write_text("paths: {}\n")
sys.path.insert(0, str(APP)); os.chdir(APP)
import frontend
from wyzebridge import web_ui
from wyzebridge.wyze_events import EVENT_LOG
from wyzecam.api_models import WyzeCamera
web_ui.auth.verify_password_callback = lambda u, p: True
names = ["Front Door", "Driveway", "Back Yard", "Garage", "Living Room", "Nursery"]
models = ["WYZEDB3", "WYZE_CAKP2JFUS", "HL_CAM3P", "HL_PAN3", "WYZE_CAKP2JFUS", "GW_BE1"]
def info(i, n, m):
    c = WyzeCamera(p2p_id=None, p2p_type=None, ip="192.168.1.%d" % (20+i), enr=None, mac="AABBCC0000%02d" % i, product_model=m,
        nickname=n, timezone_name=None, firmware_ver="4.36.11.1", dtls=None, parent_dtls=None, parent_enr=None, parent_mac=None, thumbnail=None)
    off = i == 3
    d = dict(name_uri=c.name_uri, status=0 if off else 2, net_mode="lan", connected=not off, enabled=True, motion=i == 1, motion_ts=time.time()-60*i,
        on_demand=False, audio=True, record=i < 2, substream=False, model_name=c.model_name, is_2k=c.is_2k, rtsp_fw=False, rtsp_fw_enabled=False,
        is_battery=c.is_battery, is_pan_cam=c.is_pan_cam, is_floodlight=False, is_spotlight=False, is_doorbell=c.is_doorbell, product_model=m,
        webrtc=c.webrtc_support, start_time=time.time()-3600, quality="hd", req_frame_size=0, req_bitrate=180, failures=6 if off else 0, chronic=off,
        busy=False, reason="This camera isn't answering. Check its power and Wi-Fi." if off else None, source="lan", sources=["lan"])
    return c.name_uri, d | c.model_dump(exclude={"p2p_id", "enr", "parent_enr"})
cams = dict(info(i, n, m) for i, (n, m) in enumerate(zip(names, models)))
with mock.patch.object(frontend, "WyzeBridge") as WB, mock.patch.object(frontend, "Thread"):
    wb = WB.return_value
    wb.api.total_cams = len(cams); wb.api.auth = True
    wb.streams.total = len(cams); wb.streams.active = len(cams)-1
    wb.streams.get_all_cam_info.return_value = cams
    wb.streams.streams = {}
    wb.streams.get_sse_status.return_value = {u: {"status": c["status"], "motion": c["motion"]} for u, c in cams.items()}
    app = frontend.create_app()
now = int(time.time())
for k, (u, lab, txt) in enumerate([("front-door", "person", "Person at Front Door"), ("driveway", "vehicle", "Car in Driveway"), ("back-yard", "pet", "Dog in Back Yard"),
    ("front-door", "package", "Package at Front Door"), ("driveway", "person", "Person in Driveway"), ("living-room", None, "Motion in Living Room")]*3):
    EVENT_LOG.append({"uri": u, "name": cams[u]["nickname"], "ts": now-k*1400-90, "img": None, "label": lab, "text": txt})
app.run(port=5055, threaded=True)
