from types import SimpleNamespace

from wyzebridge import wyze_stream


class FakeApi:
    def __init__(self):
        self.calls = []

    def run_action(self, cam, action):
        self.calls.append(("action", action))
        return {"status": "success"}

    def set_property(self, cam, pid, value):
        self.calls.append((pid, value))
        return {"status": "success"}

    def get_device_info(self, cam, pid):
        self.calls.append(("get", pid))
        return {"status": "success", "value": "1"}


def fake_stream(model="WYZE_CAKP2JFUS"):
    api = FakeApi()
    stream = SimpleNamespace(api=api, camera=SimpleNamespace(product_model=model, nickname="Front"))
    return stream, api


def control(stream, cmd, payload):
    return wyze_stream.WyzeStream.cloud_control(stream, cmd, payload)


def test_siren_and_lights_go_through_the_cloud():
    stream, api = fake_stream()
    assert control(stream, "alarm", "on")["value"] == "on"
    assert control(stream, "floodlight", "off")["value"] == "off"
    assert control(stream, "motion_detection", "on")["status"] == "success"
    assert api.calls == [("action", "siren_on"), ("P1056", "2"), ("P1047", "1"), ("P1001", "1")]


def test_bulb_cam_light_uses_an_action():
    stream, api = fake_stream("HL_BC")
    control(stream, "spotlight", "on")
    assert api.calls == [("action", "floodlight_on")]


def test_reading_state():
    stream, api = fake_stream()
    assert control(stream, "alarm", "")["value"] == "on"
    assert api.calls == [("get", "P1049")]


def test_sound_detection_sets_alarm_and_recording():
    stream, api = fake_stream()
    assert control(stream, "sound_detection", "off")["value"] == "off"
    assert control(stream, "sound_detection", "")["value"] == "on"
    assert api.calls == [("P1048", "0"), ("P1002", "0"), ("get", "P1048")]


def test_sensitivity_uses_the_apps_0_to_255_scale():
    from wyzebridge.wyze_control import parse_cmd
    from wyzecam.tutk.tutk_protocol import K10204GetMotionAlarmParam

    _, msg, _ = parse_cmd(("sound_sensitivity", "50"), "debug")
    assert (msg.code, msg.encode()[-2:]) == (10256, bytes([0, 127]))  # 0 keeps the on/off switch
    _, msg, _ = parse_cmd(("motion_sensitivity", "500"), "debug")
    assert msg.encode()[-2:] == bytes([0, 254])  # clamped to 100; the app's 100 * 2.55 truncates to 254 too
    assert K10204GetMotionAlarmParam().parse_response(bytes([1, 128])) == 50
