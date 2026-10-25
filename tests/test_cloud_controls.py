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
