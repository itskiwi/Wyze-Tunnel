import io
import os
import sys
import tempfile
import time
from pathlib import Path
from unittest import mock

os.environ["TOKEN_PATH"] = tempfile.mkdtemp()
sys.path.insert(0, str(Path(__file__).parents[1] / "app"))

import numpy as np
import pytest
import requests
from wyzebridge import detect


@pytest.fixture(autouse=True)
def fresh_db(monkeypatch, tmp_path):
    from wyzebridge import web_ui

    monkeypatch.setattr(detect, "DB_FILE", str(tmp_path / "detections.db"))
    monkeypatch.setattr(detect, "THUMB_DIR", tmp_path / "thumbs")
    monkeypatch.setattr(detect, "SETTINGS_FILE", str(tmp_path / "detect.json"))
    monkeypatch.setattr(web_ui.WbAuth, "api", "key")


def make_client():
    import frontend

    with mock.patch.object(frontend, "WyzeBridge"):
        app = frontend.create_app()
    return frontend, app.test_client()


class FakeModel:
    def __init__(self, hits):
        self.hits = hits  # [(class_id, score)]

    def detect(self, frame, min_score):
        hits = [h for h in self.hits if h[1] >= min_score]
        return [h[0] for h in hits], [h[1] for h in hits], [[0, 0, 1, 1]] * len(hits)


def test_search_filters_newest_first_and_prunes_old():
    now = time.time()
    detect.add("front", "person", 0.9, now - 20)
    detect.add("front", "car", 0.8, now - 10)
    detect.add("back", "person", 0.7, now)
    detect.add("back", "dog", 0.7, now - detect.KEEP.total_seconds() - 1)  # expired, pruned on insert
    assert [h["cam"] for h in detect.search("person")] == ["back", "front"]
    assert [h["label"] for h in detect.search(cam="front")] == ["car", "person"]
    assert [h["label"] for h in detect.search(before=now - 5)] == ["car", "person"]
    assert detect.labels() == ["person", "car"]


def test_frigate_events_saved_once_and_matched_to_cameras():
    start = time.time()
    events = [
        {
            "id": "e1",
            "camera": "front_door",
            "label": "package",
            "top_score": 0.88,
            "start_time": start,
        }
    ]
    resp = mock.Mock(json=lambda: events, raise_for_status=lambda: None)
    with mock.patch.object(detect.requests, "get", return_value=resp):
        assert detect.poll_frigate(["front-door"], 50) == start
        assert detect.poll_frigate(["front-door"], 50) == start
    assert [(h["cam"], h["label"], h["source"]) for h in detect.search()] == [("front-door", "package", "frigate")]
    assert detect.frigate_uri("Garage", ["front-door"]) == "Garage"


def test_watcher_saves_thumbnail_once_per_cooldown():
    seen = []
    watcher = detect.Watcher("front", "Front", lambda *args: seen.append(args))
    frame = np.zeros((detect.HEIGHT, detect.WIDTH, 3), np.uint8)
    model = FakeModel([(15, 0.9), (15, 0.7), (9, 0.95), (12, 0.3)])  # two people, a chair, a weak dog
    watcher.check(model, frame)
    watcher.check(model, frame)
    assert [(s[2], s[3]) for s in seen] == [("person", 1)]
    hit = detect.search()[0]
    assert hit["score"] == 0.9 and (detect.THUMB_DIR / "1.jpg").exists()


def test_api_links_local_hits_to_recordings_and_frigate_to_its_clips():
    detect.add("front", "person", 0.9, 1_800_000_000)
    detect.add("back", "package", 0.8, 1_800_000_100, "frigate", "e1")
    frontend, client = make_client()
    body = client.get("/api/detections?api=key").json
    assert sorted(body["labels"]) == ["package", "person"]
    frigate, local = body["detections"]
    assert local["clip"] == "recordings/front/play?start=2027-01-15T07%3A59%3A55%2B00%3A00&duration=30"
    assert frigate["clip"] == "detections/2.mp4" and frigate["thumb"] == "detections/2.jpg"
    assert client.get("/api/detections?api=key&before=x").status_code == 400

    resp = requests.Response()
    resp.status_code, resp.raw, resp.headers["content-type"] = (
        200,
        io.BytesIO(b"mp4"),
        "video/mp4",
    )
    with mock.patch.object(detect.requests, "get", return_value=resp) as get:
        media = client.get("/detections/2.mp4?api=key")
    assert media.data == b"mp4" and get.call_args.args[0].endswith("/api/events/e1/clip.mp4")
    assert client.get("/detections/1.mp4?api=key").status_code == 404


def test_settings_pick_cameras_and_frigate_turns_local_off():
    with pytest.raises(ValueError):
        detect.save_settings({"frigate_url": "frigate:5000"}, ["front"])
    saved = detect.save_settings({"cams": ["front", "gone"]}, ["front", "back"])
    assert saved == {"cams": ["front"], "frigate_url": ""}
    assert detect.wanted("front") and not detect.wanted("back")
    detect.save_settings({"cams": ["front"], "frigate_url": "http://frigate:5000/"}, ["front"])
    assert detect.frigate_url() == "http://frigate:5000" and not detect.wanted("front") and detect.enabled()


def test_settings_page_saves_and_restarts_cameras(monkeypatch):
    import frontend
    from wyzebridge import web_ui

    monkeypatch.setattr(web_ui.WbAuth, "enabled", False)

    with mock.patch.object(frontend, "WyzeBridge") as bridge:
        client = frontend.create_app().test_client()
    bridge.return_value.streams.streams = {"front": None}
    assert client.post("/search/settings", json={"frigate_url": "nope"}).status_code == 400
    with mock.patch.object(frontend, "Thread") as thread:
        assert client.post("/search/settings", json={"cams": ["front"]}).json == {"status": "saved"}
    assert thread.call_args.kwargs["target"] == bridge.return_value.restart
    assert detect.load_settings()["cams"] == ["front"]


def test_old_cpu_turns_detection_off():
    """NumPy raises RuntimeError on CPUs older than x86-64-v2; the bridge must still start (Tunnel Box under QEMU)."""
    import subprocess

    code = (
        "import sys\n"
        "class Old:\n"
        "    def find_spec(self, name, *a):\n"
        "        if name.split('.')[0] in ('cv2', 'numpy'): raise RuntimeError('X86_V2')\n"
        "sys.meta_path.insert(0, Old())\n"
        "from wyzebridge import detect\n"
        "assert detect.cv2 is None\n"
    )
    subprocess.run([sys.executable, "-c", code], check=True, cwd=Path(__file__).parents[1] / "app")


def test_heatmap_counts_object_centers():
    heat = [0] * detect.HEAT_W * detect.HEAT_H
    model = FakeModel([(15, 0.9)])  # its box is the top-left corner
    detect.find_objects(model, None, heat)
    assert heat[0] == 1 and sum(heat) == 1


def test_clip_thumb_is_made_once_and_kept(tmp_path, monkeypatch):
    frontend, client = make_client()
    monkeypatch.setattr(frontend.config, "IMG_PATH", str(tmp_path) + "/")
    calls = []

    def fake_ffmpeg(cmd, **kw):
        calls.append(cmd)
        Path(cmd[-1]).write_bytes(b"jpg")

    monkeypatch.setattr(frontend.subprocess, "run", fake_ffmpeg)
    assert client.get("/recordings/front/thumb?api=key&start=nope").status_code == 400
    url = "/recordings/front/thumb?api=key&start=2026-10-24T12:00:00.123456Z"
    assert client.get(url).data == b"jpg"
    assert client.get(url).data == b"jpg"
    assert len(calls) == 1


def test_ignored_zone_hides_objects_but_keeps_heat():
    heat = [0] * detect.HEAT_W * detect.HEAT_H
    model = FakeModel([(15, 0.9)])  # top-left corner, cell 0
    assert detect.find_objects(model, None, heat, frozenset({0})) == {}
    assert heat[0] == 1
    assert detect.find_objects(model, None, None, frozenset({1})) == {"person": 0.9}


def test_zone_routes(tmp_path, monkeypatch):
    import frontend
    from wyzebridge import web_ui

    monkeypatch.setattr(detect, "ZONES_FILE", str(tmp_path / "zones.json"))
    monkeypatch.setattr(web_ui.WbAuth, "enabled", False)
    with mock.patch.object(frontend, "WyzeBridge") as bridge:
        bridge.return_value.streams.streams = {"front": None}
        client = frontend.create_app().test_client()
    assert client.post("/api/zones/front", json={"ignore": [3, 1, 3]}).json == {"ignore": [1, 3]}
    assert client.get("/api/heatmap/front").json["ignore"] == [1, 3]
    assert client.post("/api/zones/front", json={"ignore": [999]}).status_code == 400
    assert client.post("/api/zones/front", json={"ignore": list(range(144))}).status_code == 400
    assert client.post("/api/zones/back", json={"ignore": []}).status_code == 404
    assert client.post("/api/zones/front", json={"ignore": []}).json == {"ignore": []}
    assert detect.load_zones() == {}


def test_map_routes(tmp_path, monkeypatch):
    import frontend
    from wyzebridge import floorplan

    monkeypatch.setattr(floorplan, "IMAGE_FILE", tmp_path / "floorplan.img")
    monkeypatch.setattr(floorplan, "PINS_FILE", str(tmp_path / "floorplan.json"))
    from wyzebridge import web_ui

    monkeypatch.setattr(web_ui.WbAuth, "enabled", False)  # pages need the web login, not the API key
    with mock.patch.object(frontend, "WyzeBridge") as bridge:
        bridge.return_value.streams.streams = {"front": None}
        client = frontend.create_app().test_client()
    assert client.get("/map/image").status_code == 404
    assert client.post("/map/image", data=b"\xff\xd8\xff jpeg").status_code == 200
    assert client.get("/map/image").mimetype == "image/jpeg"
    assert client.post("/map/pins", json={"front": [10, 20]}).json == {"pins": {"front": [10.0, 20.0]}}
