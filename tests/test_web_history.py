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


def mp4(seconds, scale=1000, version=0):
    """The smallest file mp4_seconds reads: a moov box holding an mvhd."""
    import struct
    times = struct.pack(">QQIQ", 0, 0, scale, seconds * scale) if version else struct.pack(">IIII", 0, 0, scale, seconds * scale)
    mvhd = struct.pack(">I", 8 + 4 + len(times)) + b"mvhd" + bytes([version, 0, 0, 0]) + times
    return b"\0\0\0\x10ftypisom\0\0\0\0" + struct.pack(">I", 8 + len(mvhd)) + b"moov" + mvhd


def test_clip_length_tags_and_uri_saved(archive, tmp_path):
    for v in (0, 1):
        (tmp_path / "c.mp4").write_bytes(mp4(23, version=v))
        assert archive.mp4_seconds(tmp_path / "c.mp4") == 23
    (tmp_path / "bad.mp4").write_bytes(b"not a video")
    assert archive.mp4_seconds(tmp_path / "bad.mp4") == 0
    api = FakeApi({"https://x/a.mp4": mp4(15)})
    archive.save_event(api, "e1", ENTRY | {"label": "vehicle", "tags": ["vehicle", "person"]}, None, "https://x/a.mp4")
    (ev,) = archive.search()["events"]
    assert (ev["dur"], ev["tags"], ev["uri"], ev["kept"]) == (15, ["vehicle", "person"], "front", False)


def test_old_archive_gets_new_columns(archive):
    import sqlite3

    (archive.ARCHIVE_DIR / "media").mkdir(parents=True)
    db = sqlite3.connect(archive.ARCHIVE_DIR / "index.db")
    db.execute("CREATE TABLE events (id TEXT PRIMARY KEY, ts INTEGER, device TEXT, type TEXT, text TEXT, img TEXT, clip TEXT, size INTEGER)")
    db.execute("INSERT INTO events VALUES ('" + "a" * 32 + "', 5, 'Hall', 'motion', 'x', NULL, NULL, 0)")
    db.commit()
    db.close()
    (ev,) = archive.search()["events"]
    assert (ev["dur"], ev["tags"], ev["kept"]) == (None, [], False)


def test_kept_events_survive_the_size_limit(archive, monkeypatch):
    monkeypatch.setattr(archive, "EVENT_ARCHIVE_GB", 2500 / 1024**3)
    api = FakeApi({"https://x/c": b"x" * 1000})
    archive.save_event(api, "e0", ENTRY | {"ts": 1000}, None, "https://x/c")
    assert archive.keep(archive.search()["events"][0]["id"], True)
    assert not archive.keep("nothex", True)
    for i in (1, 2):
        archive.save_event(api, f"e{i}", ENTRY | {"ts": 1000 + i}, None, "https://x/c")
    assert [e["ts"] for e in archive.search()["events"]] == [1002, 1000]


def test_counts_and_times_ignore_the_kind_filter(archive):
    for i, label in enumerate(("person", "pet", "pet")):
        archive.save_event(FakeApi(), f"e{i}", ENTRY | {"ts": 100 + i, "label": label}, None, None)
    got = archive.search(kind="pet", since=1)
    assert len(got["events"]) == 2 and got["counts"] == {"person": 1, "pet": 2} and got["times"] == [102, 101, 100]
    assert archive.search()["times"] == []  # only for a date range


def ev(i, ts, device, kind="pet"):
    return {"id": f"e{i}", "ts": ts, "device": device, "type": kind}


def test_group_folds_repeats_and_joins_visits(archive):
    events = [
        ev(1, 1000, "Yard"), ev(2, 1600, "Yard"), ev(3, 2500, "Yard"),  # a cat pacing: one group
        ev(4, 1650, "Porch"),  # a pet on another camera is not a visit
        ev(5, 5000, "Driveway", "person"), ev(6, 5060, "Porch", "person"), ev(7, 5100, "Gate", "person"),  # one person walking round
        ev(8, 5150, "Gate", "vehicle"),  # another kind stays apart
        ev(9, 9000, "Yard"),  # too long after: a new group
    ]
    groups = archive.group(events)
    assert [g["ids"] for g in groups] == [["e9"], ["e8"], ["e7", "e6", "e5"], ["e3", "e2", "e1"], ["e4"]]
    visit = groups[2]
    assert (visit["kind"], visit["first"], visit["last"], visit["count"]) == ("person", 5000, 5100, 3)
    assert list(visit["cameras"]) == ["Driveway", "Porch", "Gate"] and visit["cameras"]["Porch"] == [5060, 5060]
    assert groups[3]["cameras"] == {"Yard": [1000, 2500]}
    assert archive.group(events[:2], gap=60) == [
        {"kind": "pet", "first": 1600, "last": 1600, "count": 1, "ids": ["e2"], "cameras": {"Yard": [1600, 1600]}},
        {"kind": "pet", "first": 1000, "last": 1000, "count": 1, "ids": ["e1"], "cameras": {"Yard": [1000, 1000]}},
    ]


def test_keep_route_is_owner_only(setup, archive):
    client, guests = setup
    archive.save_event(FakeApi(), "e1", ENTRY, None, None)
    eid = archive.search()["events"][0]["id"]
    family = basic("Fay", guests.add_guest("Fay", 0, "owner"))
    assert client.post(f"/history/{eid}/keep", json={"kept": True}, headers=family).status_code == 403
    from wyzebridge import recording

    with mock.patch.object(recording, "star") as star:
        assert client.post(f"/history/{eid}/keep", json={"kept": True}, headers=basic("owner", "ownerpass")).json == {"kept": True}
    star.assert_called_once_with("front", ENTRY["ts"], on=True)
    assert archive.search()["events"][0]["kept"]
    assert client.post("/history/nothex/keep", headers=basic("owner", "ownerpass")).status_code == 404
def test_live_monitoring_view_by_role(setup, monkeypatch):
    from wyzebridge import web_ui

    client, guests = setup
    cams = {"front": {"nickname": "Front", "enabled": True, "connected": True, "on_demand": False}}
    monkeypatch.setattr(web_ui, "all_cams", lambda *_: {"enabled": 1, "available": 1, "total": 1, "cameras": cams})
    owner = client.get("/live", headers=basic("owner", "ownerpass")).text
    assert 'id="rewind"' in owner and 'id="watchMode"' in owner and "static/watch.js" in owner
    assert 'data-owner="1"' in owner and "static/events.js" in owner
    family = client.get("/live", headers=basic("Fay", guests.add_guest("Fay", 0, "owner"))).text
    assert 'id="rewind"' in family and 'id="watchMode"' not in family  # family sees the mode but can't change it
    assert 'data-owner="0"' in family  # and can't keep or delete events from the visit player
    watcher = client.get("/live", headers=basic("Sam", guests.add_guest("Sam", 24, "owner"))).text
    assert 'id="rewind"' not in watcher and "static/watch.js" not in watcher


def at(day, hour, minute=0):
    """A local time `day` days from a fixed Wednesday noon."""
    import time

    return int(time.mktime((2026, 10, 21 + day, hour, minute, 0, 0, 0, -1)))


def test_judge_lifts_the_unusual_and_folds_the_usual(archive):
    api = FakeApi()
    for d in range(-20, 0):  # three weeks of the mail truck at noon and a cat at night
        archive.save_event(api, f"v{d}", ENTRY | {"name": "Driveway", "ts": at(d, 12), "label": "vehicle"}, None, None)
        archive.save_event(api, f"c{d}", ENTRY | {"name": "Yard", "ts": at(d, 23, 30), "label": "pet"}, None, None)
        archive.save_event(api, f"g{d}", ENTRY | {"name": "Back Gate", "ts": at(d, 16), "label": "pet"}, None, None)
    archive.save_event(api, "p0", ENTRY | {"name": "Back Gate", "ts": at(0, 3), "label": "person"}, None, None)
    archive.save_event(api, "v0", ENTRY | {"name": "Driveway", "ts": at(0, 12, 20), "label": "vehicle"}, None, None)
    archive.save_event(api, "c0", ENTRY | {"name": "Yard", "ts": at(0, 0, 10), "label": "pet"}, None, None)  # wraps round midnight
    archive.save_event(api, "x0", ENTRY | {"name": "Driveway", "ts": at(0, 3, 5), "label": "vehicle"}, None, None)
    groups = {g["first"]: g for g in archive.search(since=at(0, 0), until=at(1, 0))["groups"]}
    assert groups[at(0, 3)]["why"] == "Nobody has been at Back Gate at this time of day in the last 20 days."
    assert groups[at(0, 12, 20)].get("usual") and groups[at(0, 0, 10)].get("usual")
    assert groups[at(0, 3, 5)]["why"].startswith("No vehicle has been at Driveway at this time")
    archive.save_event(api, "q0", ENTRY | {"name": "Shed", "ts": at(0, 9), "label": "person"}, None, None)
    assert "why" not in next(g for g in archive.search(since=at(0, 0), until=at(1, 0))["groups"] if "Shed" in g["cameras"])  # a camera with no history
    # under a week of history marks nothing
    assert not any("why" in g or "usual" in g for g in archive.search(since=at(-15, 0), until=at(-14, 0))["groups"])


def test_ask_answers_plain_questions(archive):
    api = FakeApi()
    archive.save_event(api, "a", ENTRY | {"name": "Front Porch", "ts": at(0, 10, 1), "label": "package", "text": "Package left at the front porch"}, None, None)
    archive.save_event(api, "b", ENTRY | {"name": "Driveway", "ts": at(-1, 17, 7), "label": "vehicle", "text": "A car left the driveway"}, None, None)
    archive.save_event(api, "c", ENTRY | {"name": "Driveway", "ts": at(0, 8), "label": "vehicle", "text": "A car pulled in"}, None, None)
    now = at(0, 22)
    got = archive.ask("Did a package come today?", now)
    assert got["answer"] == "Yes. Package left at the front porch at 10:01 AM." and got["kind"] == "package" and len(got["events"]) == 1
    assert archive.ask("did anyone ring the doorbell last night", now)["answer"] == "No, no doorbell rings last night."
    assert archive.ask("When did the car leave yesterday?", now)["answer"] == "A car left the driveway at 5:07 PM on Tuesday."
    got = archive.ask("how many cars at the driveway this week?", now)
    assert got["device"] == "Driveway" and got["answer"].startswith("2 vehicle events at Driveway this week. The latest: A car pulled in at 8:00 AM")
    assert archive.ask("anything yesterday?", now)["answer"] == "Yes. A car left the driveway at 5:07 PM on Tuesday."
    assert archive.ask("anything this morning at the garage?", now)["answer"].startswith("Yes, 2 events this morning")
    assert archive.ask("did a package come today", now + 3600 * 3, tz=0)["since"] % 86400 == 0  # UTC midnight
    assert archive.is_question("when did the car leave") and archive.is_question("package?") and not archive.is_question("driveway")
