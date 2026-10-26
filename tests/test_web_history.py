# Sorts after test_security.py, which reloads wyzebridge.auth (see test_web_recordings.py).
import io
from base64 import b64encode
from unittest import mock

import pytest
import requests
import yaml
from werkzeug.security import generate_password_hash


def basic(user, password):
    return {"Authorization": "Basic " + b64encode(f"{user}:{password}".encode()).decode()}


class FakeApi:
    def __init__(self, media=None):
        self.media = media or {}

    def get_media(self, url, **kwargs):
        resp = requests.Response()
        body = self.media.get(url)
        resp.status_code = 200 if body is not None else 404
        resp.headers["Content-Length"] = str(len(body or b""))
        resp.raw = io.BytesIO(body or b"")
        return resp


@pytest.fixture
def archive(monkeypatch, tmp_path):
    from wyzebridge import event_archive

    monkeypatch.setattr(event_archive, "ARCHIVE_DIR", tmp_path / "archive")
    monkeypatch.setattr(event_archive, "_last_poll", 0.0)
    return event_archive


ENTRY = {"uri": "front", "name": "Front", "ts": 1_700_000_000, "text": "Person at Front", "label": "person"}


def test_save_event_stores_files_and_index(archive):
    api = FakeApi({"https://x/a.jpg": b"jpg", "https://x/a.mp4": b"mp4data"})
    assert archive.save_event(api, "ev/../1", ENTRY, "https://x/a.jpg", "https://x/a.mp4")
    assert not archive.save_event(api, "ev/../1", ENTRY, "https://x/a.jpg", "https://x/a.mp4")  # already saved
    (ev,) = archive.search(kind="person")["events"]
    assert ev["img"] and ev["clip"] and ev["device"] == "Front"
    assert archive.media_file(ev["id"], "clip").read_bytes() == b"mp4data"
    # nothing was written outside the media folder, whatever the event id was
    assert {p.name for p in (archive.ARCHIVE_DIR / "media").iterdir()} == {ev["id"] + ".jpg", ev["id"] + ".mp4"}


def test_failed_or_oversized_download_leaves_no_file(archive, monkeypatch):
    monkeypatch.setattr(archive, "MAX_CLIP", 4)
    api = FakeApi({"https://x/a.mp4": b"too long"})
    archive.save_event(api, "e1", ENTRY, "https://x/missing.jpg", "https://x/a.mp4")
    (ev,) = archive.search()["events"]
    assert not ev["img"] and not ev["clip"]
    assert list((archive.ARCHIVE_DIR / "media").iterdir()) == []
    assert archive._download(api, "file:///etc/passwd", archive.ARCHIVE_DIR / "media" / "x.jpg", 10) == 0


def test_size_limit_deletes_oldest_first(archive, monkeypatch):
    monkeypatch.setattr(archive, "EVENT_ARCHIVE_GB", 2500 / 1024**3)
    api = FakeApi({"https://x/c": b"x" * 1000})
    for i in range(3):
        archive.save_event(api, f"e{i}", ENTRY | {"ts": 1000 + i}, None, "https://x/c")
    assert [e["ts"] for e in archive.search()["events"]] == [1002, 1001]
    assert len(list((archive.ARCHIVE_DIR / "media").iterdir())) == 2


def test_media_path_safety(archive):
    archive.save_event(FakeApi(), "e1", ENTRY, None, None)
    for bad in ("../index", "..%2f..%2fetc", "a" * 31, "A" * 32):
        assert archive.media_file(bad, "img") is None
        assert not archive.delete(bad)
    assert archive.media_file("a" * 32, "../x") is None


def test_sensor_events_parsed_and_deduped(archive):
    ev = {"event_id": "9", "device_mac": "AABB", "device_model": "DWS3U", "nickname": "Back door", "event_value": "1", "event_ts": 1_700_000_000_000}
    battery = ev | {"event_id": "10", "event_value": "87"}
    assert archive.save_sensor_events([ev, battery]) == 1
    assert archive.save_sensor_events([ev]) == 0
    (row,) = archive.search(kind="contact")["events"]
    assert row["text"] == "Back door opened" and row["ts"] == 1_700_000_000
    assert "AABB" not in str(archive.search())


def test_search_filters_and_escapes(archive):
    archive.save_sensor_events([
        {"event_id": "1", "device_mac": "A", "device_model": "PIR3U", "nickname": "Hall", "event_value": "1", "event_ts": 5000},
        {"event_id": "2", "device_mac": "B", "device_model": "DWS3U", "nickname": "Door", "event_value": "0", "event_ts": 9000},
    ])
    assert [e["device"] for e in archive.search(device="Hall")["events"]] == ["Hall"]
    assert [e["device"] for e in archive.search(since=6, until=8)["events"]] == []
    assert [e["device"] for e in archive.search(since=6)["events"]] == ["Door"]
    assert archive.search(q="%")["events"] == []
    assert archive.search()["devices"] == ["Door", "Hall"]


@pytest.fixture
def setup(monkeypatch, tmp_path, archive):
    import frontend
    from wyzebridge import guests, mtx_server, web_ui

    monkeypatch.setattr(guests, "GUESTS_FILE", str(tmp_path / "guests.json"))
    mtx = tmp_path / "mediamtx.yml"
    mtx.write_text(yaml.safe_dump({"authInternalUsers": [{"user": "wb", "pass": "api"}]}))
    monkeypatch.setattr(mtx_server, "MTX_CONFIG", str(mtx))
    for cls in {web_ui.WbAuth, frontend.WbAuth}:
        monkeypatch.setattr(cls, "enabled", True)
        monkeypatch.setattr(cls, "username", "owner")
        monkeypatch.setattr(cls, "_hashed_pass", generate_password_hash("ownerpass"))
        monkeypatch.setattr(cls, "api", "apikey")
    with mock.patch.object(frontend, "WyzeBridge"):
        yield frontend.create_app().test_client(), guests


def test_history_routes_by_role(setup, archive):
    client, guests = setup
    archive.save_event(FakeApi({"https://x/a.jpg": b"jpg"}), "e1", ENTRY, "https://x/a.jpg", None)
    eid = archive.search()["events"][0]["id"]
    owner = basic("owner", "ownerpass")
    watcher = basic("Sam", guests.add_guest("Sam", 24, "owner"))
    family = basic("Fay", guests.add_guest("Fay", 0, "owner"))

    assert client.get("/history", headers=owner).status_code == 200
    assert client.get(f"/history/media/{eid}/img", headers=owner).data == b"jpg"
    assert client.get("/history/media/nothex/img", headers=owner).status_code == 404
    assert client.get("/history/events?device=Front&since=abc", headers=owner).json["events"][0]["id"] == eid

    for path in ("/history", "/history/events", f"/history/media/{eid}/img"):
        assert client.get(path, headers=watcher).status_code == 403
    assert client.post("/history/clear", headers=watcher).status_code == 403

    assert client.get("/history/events", headers=family).status_code == 200
    assert client.post(f"/history/{eid}/delete", headers=family).status_code == 403
    assert client.post("/history/clear", headers=family).status_code == 403
    assert len(archive.search()["events"]) == 1

    assert client.post(f"/history/{eid}/delete", headers=owner).status_code == 200
    assert client.post("/history/clear", headers=owner).status_code == 200
