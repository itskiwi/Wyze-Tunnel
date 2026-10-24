from unittest import mock

import pytest
from wyzebridge import go2rtc
from wyzecam.api_models import WyzeCamera


def cam(model="HL_CAM4", dtls=1, ip="192.168.1.20", parent_dtls=None):
    return WyzeCamera(p2p_id="UID123", p2p_type=None, ip=ip, enr="secret+/=", mac="AABBCC", product_model=model,
                      nickname="Front", timezone_name=None, firmware_ver=None, dtls=dtls, parent_dtls=parent_dtls,
                      parent_enr=None, parent_mac=None, thumbnail=None)


@pytest.fixture
def both(monkeypatch):
    monkeypatch.setattr(go2rtc, "go2rtc_available", lambda: True)
    monkeypatch.setattr(go2rtc, "tutk_available", lambda: True)


def test_auto_chain(both, monkeypatch):
    assert go2rtc.source_chain(cam()) == ["tutk", "lan", "webrtc"]
    assert go2rtc.source_chain(cam(dtls=0)) == ["tutk", "webrtc"]  # old firmware: no go2rtc LAN
    assert go2rtc.source_chain(cam(ip=None)) == ["tutk", "webrtc"]
    assert go2rtc.source_chain(cam(model="WYZEDB3")) == ["tutk", "lan"]  # no WebRTC on this model
    assert go2rtc.source_chain(cam(model="GW_GC1")) == ["webrtc"]  # Gwell (OG): cloud WebRTC only
    assert go2rtc.source_chain(cam(model="GW_BE1")) == []  # Doorbell Pro: no WebRTC
    monkeypatch.setattr(go2rtc, "tutk_available", lambda: False)
    monkeypatch.setattr(go2rtc, "local_ipv4s", lambda: {"169.254.1.7", "192.168.3.10"})
    assert go2rtc.source_chain(cam(ip="192.168.0.27")) == ["lan", "webrtc"]  # same /22 network
    assert go2rtc.source_chain(cam(ip="10.0.0.21")) == ["webrtc"]  # another site: LAN discovery can't reach it
    assert go2rtc.source_chain(cam(ip="169.254.1.2")) == ["webrtc"]
    monkeypatch.setattr(go2rtc, "go2rtc_available", lambda: False)
    assert go2rtc.source_chain(cam()) == []


def test_overrides(both, monkeypatch):
    monkeypatch.setenv("SOURCE", "lan")
    assert go2rtc.source_chain(cam()) == ["lan"]
    monkeypatch.setenv("SOURCE_HL_CAM4", "webrtc, tutk")
    assert go2rtc.source_chain(cam()) == ["webrtc", "tutk"]
    monkeypatch.setenv("SOURCE_FRONT", "auto")
    assert go2rtc.source_chain(cam()) == ["tutk", "lan", "webrtc"]
    monkeypatch.setenv("SOURCE_FRONT", "webrtc")
    assert go2rtc.source_chain(cam(model="GW_GC1")) == ["webrtc"]
    monkeypatch.setattr(go2rtc, "go2rtc_available", lambda: False)
    assert go2rtc.source_chain(cam()) == []


def test_urls():
    assert go2rtc.lan_url(cam(), substream=True) == (
        "wyze://192.168.1.20?uid=UID123&enr=secret%2B%2F%3D&mac=AABBCC&model=HL_CAM4&subtype=sd&dtls=true")
    assert go2rtc.webrtc_url("front", "k y") == "webrtc:http://127.0.0.1:5000/signaling/front?kvs&api=k+y#format=wyze"
    assert go2rtc.webrtc_url("front", "") == "webrtc:http://127.0.0.1:5000/signaling/front?kvs#format=wyze"


def test_fallback_moves_to_next_source_after_5_failures():
    from wyzebridge.wyze_stream import WyzeStream
    stream = WyzeStream.__new__(WyzeStream)
    stream.camera, stream.failures, stream.source_idx = cam(), 0, 0
    stream.sources = ["tutk", "lan", "webrtc"]
    seen = []
    with mock.patch("wyzebridge.wyze_stream.auto_restart"):  # covered in test_auto_restart.py
        for _ in range(15):
            stream._failed()
            seen.append(stream.source)
    assert seen[:4] == ["tutk"] * 4 and seen[4] == "lan" and seen[9] == "webrtc" and seen[14] == "tutk"


def test_cam_og_uses_get_streams(monkeypatch):
    from unittest import mock

    from wyzecam import api
    from wyzecam.api_models import WyzeCredential

    body = {"code": "1", "data": [{"params": {
        "signaling_url": "wss://wyze-mars-webcsrv.wyzecam.com/x?X-Amz-Credential=a%252Fb",
        "ice_servers": [{"url": "turn:t", "username": "u", "credential": "c"}],
    }}]}
    monkeypatch.setattr(api, "API_STATS", api.Counter())
    resp = mock.Mock(headers={}, json=lambda: body)
    with mock.patch.object(api, "post", return_value=resp) as post:
        out = api.get_cam_webrtc(WyzeCredential(access_token="tok", phone_id="p"), "MAC", "GW_GC1")
    assert post.call_args.args[0].endswith("/v4/camera/get_streams")
    assert '"device_model":"GW_GC1"' in post.call_args.kwargs["data"].replace(" ", "")
    assert out == {"ClientId": "p", "signalingUrl": "wss://wyze-mars-webcsrv.wyzecam.com/x?X-Amz-Credential=a%2Fb",
                   "servers": [{"urls": "turn:t", "username": "u", "credential": "c"}]}
