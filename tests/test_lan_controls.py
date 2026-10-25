import sys

from wyzebridge import go2rtc, wyze_control
from wyzecam.api_models import WyzeCamera
from wyzecam.tutk import tutk_protocol


def cam():
    return WyzeCamera(p2p_id="UID123", p2p_type=None, ip="192.168.1.20", enr="secret", mac="AABBCC", product_model="HL_PAN3",
                      nickname="Front", timezone_name=None, firmware_ver="4.50.1.1", dtls=1, parent_dtls=None,
                      parent_enr=None, parent_mac=None, thumbnail=None)


def fake_helper(tmp_path, monkeypatch, reply: bytes = b"", fail: bool = False):
    """A stand-in for native/wyze-ioctl that logs its arguments and answers with reply."""
    log = tmp_path / "calls"
    script = tmp_path / "wyze-ioctl"
    script.write_text(
        f"#!{sys.executable}\nimport sys\n"
        f"open({str(log)!r}, 'a').write(' '.join(sys.argv[1:]) + '\\n')\n"
        + ("sys.exit('wyze: connect failed: discovery timeout')\n" if fail else f"print({reply.hex()!r})\n")
    )
    script.chmod(0o755)
    monkeypatch.setattr(go2rtc, "IOCTL_BIN", str(script))
    return log


def test_night_vision_over_lan(tmp_path, monkeypatch):
    log = fake_helper(tmp_path, monkeypatch, tutk_protocol.encode(10041, b"\x01"))
    resp = wyze_control.handle_cmd(go2rtc.LanSession(cam()), ("night_vision", ""))
    assert resp["status"] == "success" and resp["value"] == 1
    url, request, code = log.read_text().split()
    assert url.startswith("wyze://192.168.1.20?") and "dtls=true" in url
    assert bytes.fromhex(request) == tutk_protocol.K10040GetNightVisionStatus().encode() and code == "10041"


def test_pan_without_a_reply(tmp_path, monkeypatch):
    log = fake_helper(tmp_path, monkeypatch)
    resp = wyze_control.handle_cmd(go2rtc.LanSession(cam()), ("rotary_degree", "left"))
    assert resp["status"] == "success"
    assert log.read_text().split()[2] == "0"  # the camera doesn't answer rotate commands


def test_camera_unreachable(tmp_path, monkeypatch):
    fake_helper(tmp_path, monkeypatch, fail=True)
    resp = wyze_control.handle_cmd(go2rtc.LanSession(cam()), ("night_vision", ""))
    assert resp["status"] == "success" and resp["response"] is None  # like a TUTK timeout


def test_lan_controls_needs_lan_and_the_helper(tmp_path, monkeypatch):
    fake_helper(tmp_path, monkeypatch)
    assert go2rtc.lan_controls(cam(), ["lan", "webrtc"])
    assert not go2rtc.lan_controls(cam(), ["webrtc"])
    monkeypatch.setattr(go2rtc, "IOCTL_BIN", str(tmp_path / "missing"))
    assert not go2rtc.lan_controls(cam(), ["lan"])
