# Sorts after test_security.py, which reloads wyzebridge.auth (see test_web_recordings.py).
from base64 import b64encode
from types import SimpleNamespace
from unittest import mock

import pytest
import yaml
from werkzeug.security import generate_password_hash


def basic(user, password):
    return {"Authorization": "Basic " + b64encode(f"{user}:{password}".encode()).decode()}


OWNER = basic("owner", "ownerpass")


def stream(name, connected=True, substream=False):
    return SimpleNamespace(connected=connected, camera=SimpleNamespace(nickname=name), options=SimpleNamespace(substream=substream))


class FakeCams:
    """Stands in for send_cmd: never touches a camera."""

    def __init__(self, state):
        self.state = state  # uri: {setting: value}
        self.calls = []

    def __call__(self, uri, cmd, payload=""):
        self.calls.append((uri, cmd, payload))
        if payload:
            if cmd == "irled" and uri == "old":
                return {"status": "error", "response": "invalid command"}
            if uri == "bad":
                return {"status": "error", "response": "timed out"}
            self.state[uri][cmd] = payload
            return {"status": "success", "value": payload}
        if cmd not in self.state[uri]:
            return {"status": "error", "response": "invalid command"}
        return {"status": "success", "value": self.state[uri][cmd]}


@pytest.fixture
def cs(monkeypatch, tmp_path):
    from wyzebridge import camera_settings

    monkeypatch.setattr(camera_settings, "SNAPSHOT_FILE", str(tmp_path / "snaps.json"))
    monkeypatch.setattr(camera_settings, "CACHE", {})
    monkeypatch.setattr(camera_settings, "PAUSE", 0)
    return camera_settings


@pytest.fixture
def fleet():
    streams = {u: stream(u.title()) for u in ("a", "b", "c")} | {"idle": stream("Idle", connected=False), "sub": stream("Sub", substream=True)}
    state = {u: {"night_vision": "on", "irled": "1", "bitrate": 120, "hor_flip": 1} for u in ("a", "b", "c", "idle", "sub", "old", "bad")}
    state["c"]["night_vision"] = 3
    return streams, FakeCams(state)


def test_read_flags_drift_and_skips_idle_cameras(cs, fleet):
    streams, send = fleet
    assert cs.refresh(streams, send, lambda s: None)
    assert {c[0] for c in send.calls} == {"a", "b", "c"}  # idle and substreams are never asked
    table = cs.table(streams)
    assert table["drift"] == {"night_vision": ["c"]}
    assert next(c for c in table["cams"] if c["uri"] == "a")["values"]["irled"] == "on"  # raw 1 reads as on
    assert next(c for c in table["cams"] if c["uri"] == "idle")["values"] == {}
    assert "motion_detection" not in table["cams"][0]["values"]  # unsupported settings are just blank


def test_no_majority_is_not_drift(cs):
    cs.CACHE.update({"a": {"ts": 0, "values": {"irled": "on"}}, "b": {"ts": 0, "values": {"irled": "off"}}})
    assert cs.drift(cs.CACHE, {"a": 1, "b": 1}) == {}


def test_camera_dropping_mid_read_stops_the_reads(cs):
    s = stream("A")
    calls = []

    def send(uri, cmd, payload=""):
        calls.append(cmd)
        s.connected = False
        return {"status": "success", "value": 1}

    cs.read_cam("a", s, send, lambda x: None)
    assert len(calls) == 1


def test_apply_reports_each_camera(cs, fleet):
    streams, send = fleet
    streams |= {"old": stream("Old"), "bad": stream("Bad")}
    out = {r["uri"]: r["result"] for r in cs.apply(streams, send, "irled", "off", ["a", "old", "bad", "idle"], lambda s: None)}
    assert out == {"a": "ok", "old": "not supported", "bad": "failed", "idle": "not connected"}
    assert ("idle", "irled", "off") not in send.calls


@pytest.mark.parametrize("key,value,cams", [
    ("format_sd", "on", ["a"]), ("reboot", "1", ["a"]), ("hor_flip", "on", ["a"]), ("irled", "toggle", ["a"]),
    ("bitrate", "9999", ["a"]), ("irled", "on", []), ("irled", "on", ["nope"]), ("irled", "on", "a"), ("irled", "on", ["sub"]),
])
def test_apply_refuses_anything_off_the_allowlist(cs, fleet, key, value, cams):
    streams, send = fleet
    with pytest.raises(ValueError):
        cs.apply(streams, send, key, value, cams, lambda s: None)
    assert send.calls == []


def test_allowlist_has_nothing_destructive(cs):
    assert set(cs.WRITABLE) <= set(cs.COLUMNS)
    assert not {"format_sd", "power", "reboot", "res", "fps", "hor_flip", "ver_flip", "rotary_action"} & set(cs.WRITABLE)


def test_snapshot_save_and_restore_diff(cs, fleet):
    streams, send = fleet
    with pytest.raises(ValueError):
        cs.save_snapshot(streams)  # nothing read yet
    cs.refresh(streams, send, lambda s: None)
    snap = cs.save_snapshot(streams)
    assert snap["cams"] == 3 and cs.list_snapshots()[0]["cams"] == {"a": "A", "b": "B", "c": "C"}

    send.state["a"].update(night_vision="off", irled="2", bitrate=30)  # factory reset
    send.calls.clear()
    preview = cs.restore(streams, send, snap["id"], "a", False, lambda s: None)
    assert preview["diff"] == {"night_vision": {"saved": "on", "current": "off"}, "irled": {"saved": "on", "current": "off"},
                               "bitrate": {"saved": "120", "current": "30"}}
    assert not [c for c in send.calls if c[2]]  # preview writes nothing

    done = cs.restore(streams, send, snap["id"], "a", True, lambda s: None)
    assert done["applied"]["result"] == "ok"
    assert send.state["a"]["night_vision"] == "on" and send.state["a"]["bitrate"] == "120"
    assert "hor_flip" not in [c[1] for c in send.calls if c[2]]  # flips are read-only


def test_restore_needs_a_streaming_camera_and_a_known_snapshot(cs, fleet):
    streams, send = fleet
    cs.refresh(streams, send, lambda s: None)
    snap = cs.save_snapshot(streams)
    for sid, uri in [(snap["id"], "idle"), (snap["id"], "zzz"), ("nope", "a")]:
        with pytest.raises(ValueError):
            cs.restore(streams, send, sid, uri, True, lambda s: None)


@pytest.fixture
def web(monkeypatch, cs, fleet):
    import frontend
    from wyzebridge import guests, mtx_server, web_ui

    streams, send = fleet
    monkeypatch.setattr(guests, "GUESTS_FILE", str(cs.SNAPSHOT_FILE) + ".guests")
    mtx = cs.SNAPSHOT_FILE + ".mtx"
    open(mtx, "w").write(yaml.safe_dump({"authInternalUsers": [{"user": "wb", "pass": "api"}]}))
    monkeypatch.setattr(mtx_server, "MTX_CONFIG", mtx)
    for cls in {web_ui.WbAuth, frontend.WbAuth}:
        monkeypatch.setattr(cls, "enabled", True)
        monkeypatch.setattr(cls, "username", "owner")
        monkeypatch.setattr(cls, "_hashed_pass", generate_password_hash("ownerpass"))
        monkeypatch.setattr(cls, "api", "apikey")

    class Inline:  # run the background read right away
        def __init__(self, target, args, daemon):
            self.run = lambda: target(*args)

        def start(self):
            self.run()

    with mock.patch.object(frontend, "WyzeBridge") as bridge:
        bridge.return_value.streams.streams = streams
        bridge.return_value.streams.send_cmd.side_effect = send
        with mock.patch.object(frontend, "Thread"):
            client = frontend.create_app().test_client()
        with mock.patch.object(frontend, "Thread", Inline):
            yield client, guests, send


def test_routes_round_trip(web):
    client, _, send = web
    assert client.get("/camera-settings", headers=OWNER).status_code == 200
    assert client.post("/camera-settings/refresh", headers=OWNER).json == {"status": "started"}
    data = client.get("/camera-settings/data", headers=OWNER).json
    assert data["drift"] == {"night_vision": ["c"]} and "irled" in data["writable"]

    bad = client.post("/camera-settings/apply", json={"setting": "format_sd", "value": "on", "cams": ["a"]}, headers=OWNER)
    assert bad.status_code == 400 and not [c for c in send.calls if c[2]]
    ok = client.post("/camera-settings/apply", json={"setting": "night_vision", "value": "on", "cams": ["c"]}, headers=OWNER)
    assert ok.json["results"][0]["result"] == "ok"
    assert client.post("/camera-settings/snapshots", headers=OWNER).status_code == 200
    sid = client.get("/camera-settings/data", headers=OWNER).json["snapshots"][0]["id"]
    assert client.post("/camera-settings/restore", json={"id": sid, "cam": "a"}, headers=OWNER).json["diff"] == {}
    assert client.post("/camera-settings/restore", json={"id": sid, "cam": "idle"}, headers=OWNER).status_code == 400


def test_guest_and_family_are_refused(web):
    client, guests, send = web
    for hours, family in [(24, False), (0, True)]:
        name = "Fam" if family else "Sam"
        password = client.post("/guests", json={"name": name, "hours": hours}, headers=OWNER).json["password"]
        who = basic(name, password)
        assert client.get("/camera-settings", headers=who).status_code == 403
        assert client.get("/camera-settings/data", headers=who).status_code == 403
        for path, body in [("refresh", {}), ("snapshots", {}), ("apply", {"setting": "irled", "value": "on", "cams": ["a"]}),
                           ("restore", {"id": "x", "cam": "a", "confirm": True})]:
            assert client.post(f"/camera-settings/{path}", json=body, headers=who).status_code == 403
    assert send.calls == []
