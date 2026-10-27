# Sorts after test_security.py, which reloads wyzebridge.auth (see test_web_recordings.py).
from base64 import b64encode
from unittest import mock

import pytest
from werkzeug.security import generate_password_hash

cv2 = pytest.importorskip("cv2")
import numpy as np  # noqa: E402

from wyzebridge import tamper, webhooks  # noqa: E402


def scene(seed=1):
    """A synthetic 640x360 yard: a gradient with some boxes. A different seed is a different place."""
    rng = np.random.default_rng(seed)
    img = np.tile(np.linspace(60, 140, 640, dtype=np.uint8), (360, 1))
    img = cv2.merge([img, img // 2 + 40, 255 - img])
    for _ in range(12):
        x, y, w, h = int(rng.integers(0, 560)), int(rng.integers(0, 280)), int(rng.integers(30, 90)), int(rng.integers(30, 90))
        cv2.rectangle(img, (x, y), (x + w, y + h), [int(c) for c in rng.integers(0, 255, 3)], -1)
    return img


def ir(img):
    """Night vision: a gray picture."""
    return cv2.cvtColor(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY), cv2.COLOR_GRAY2BGR)


@pytest.fixture(autouse=True)
def fresh(tmp_path, monkeypatch):
    monkeypatch.setattr(tamper, "SETTINGS_FILE", str(tmp_path / "tamper.json"))
    for state in (tamper.STATE, tamper._streak, tamper._alerted):
        state.clear()
    sent = mock.Mock()
    monkeypatch.setattr(tamper, "send_webhook", sent)
    return sent


def run(tmp_path, uri, img, times=3, start=1000.0, gap=300):
    path = tmp_path / "snap.jpg"
    cv2.imwrite(str(path), img)
    return [tamper.observe(uri, path, start + i * gap) for i in range(times)]


FLAT = np.full((360, 640, 3), 90, np.uint8)


def test_covered_lens_alerts_after_three_checks(tmp_path, fresh):
    run(tmp_path, "yard", scene(), times=1)  # learns the baseline
    assert run(tmp_path, "yard", FLAT, times=2) == ["covered", "covered"]
    fresh.assert_not_called()  # two in a row isn't enough
    assert run(tmp_path, "yard", FLAT, times=1, start=2000.0) == ["covered"]
    assert fresh.call_count == 1 and fresh.call_args.args[:2] == ("tamper", "yard")
    assert tamper.STATE == {"yard": "covered"}


def test_reaimed_scene_alerts(tmp_path, fresh):
    run(tmp_path, "yard", scene(), times=1)
    assert run(tmp_path, "yard", np.roll(scene(), 120, axis=1)) == ["moved"] * 3  # panned by a sixth of the frame
    assert fresh.call_count == 1


def test_lighting_changes_do_not_alert(tmp_path, fresh):
    base = scene()
    run(tmp_path, "yard", base, times=1)
    run(tmp_path, "yard", ir(base), times=1)  # night baseline
    dim = (base * 0.4).astype(np.uint8)
    bright = np.clip(base.astype(int) * 1.4 + 20, 0, 255).astype(np.uint8)
    noisy_ir = np.clip(ir(base).astype(int) * 0.7 + np.random.default_rng(3).normal(0, 4, base.shape), 0, 255).astype(np.uint8)
    for img in (dim, bright, ir((base * 0.6).astype(np.uint8)), noisy_ir):
        assert run(tmp_path, "yard", img, times=4) == [""] * 4
    fresh.assert_not_called()
    assert tamper.STATE == {}


def test_night_vision_broken(tmp_path, fresh):
    base = scene()
    run(tmp_path, "yard", ir(base), times=1)  # lit by IR
    black = np.random.default_rng(1).integers(0, 4, base.shape, dtype=np.uint8)
    assert run(tmp_path, "yard", black) == ["night_vision"] * 3
    assert fresh.call_count == 1 and "Night vision" in fresh.call_args.args[2]
    assert run(tmp_path, "other", black) == [""] * 3  # no IR history: only a dark room


def test_a_good_check_resets_the_count(tmp_path, fresh):
    run(tmp_path, "yard", scene(), times=1)
    run(tmp_path, "yard", FLAT, times=2)
    run(tmp_path, "yard", scene(), times=1)
    run(tmp_path, "yard", FLAT, times=2)
    fresh.assert_not_called()


def test_same_alert_is_not_repeated_for_hours(tmp_path, fresh):
    run(tmp_path, "yard", scene(), times=1)
    run(tmp_path, "yard", FLAT, times=10)  # 50 minutes
    assert fresh.call_count == 1
    run(tmp_path, "yard", FLAT, times=1, start=1000 + tamper.REPEAT_AFTER + 600)
    assert fresh.call_count == 2
    run(tmp_path, "back", scene(), times=1)
    run(tmp_path, "back", FLAT, times=3)  # another camera still alerts
    assert fresh.call_count == 3


def test_tamper_alerts_use_the_alert_channels(monkeypatch):
    monkeypatch.setattr(webhooks, "ALERTS", {"server": "https://ntfy.sh", "topic": "t"})
    with mock.patch.object(webhooks, "_post", return_value=True) as post, mock.patch.object(webhooks.integrations, "dispatch"):
        assert webhooks.send_webhook("tamper", "yard", "covered", title="Camera tampering?")
    assert post.call_args.args[0] == "https://ntfy.sh/t"


def basic(user, password):
    return {"Authorization": "Basic " + b64encode(f"{user}:{password}".encode()).decode()}


@pytest.fixture
def client(monkeypatch, tmp_path):
    import frontend
    from wyzebridge import guests, mtx_server, web_ui

    monkeypatch.setattr(guests, "GUESTS_FILE", str(tmp_path / "guests.json"))
    (tmp_path / "mediamtx.yml").write_text("authInternalUsers: []\n")
    monkeypatch.setattr(mtx_server, "MTX_CONFIG", str(tmp_path / "mediamtx.yml"))
    for cls in {web_ui.WbAuth, frontend.WbAuth}:
        monkeypatch.setattr(cls, "enabled", True)
        monkeypatch.setattr(cls, "username", "owner")
        monkeypatch.setattr(cls, "_hashed_pass", generate_password_hash("ownerpass"))
        monkeypatch.setattr(cls, "api", "apikey")
    cams = {"yard": {"nickname": "Yard", "status": 3, "enabled": True, "start_time": 0}}
    monkeypatch.setattr(web_ui, "all_cams", lambda *a: {"cameras": cams})
    with mock.patch.object(frontend, "WyzeBridge") as wb:
        wb.return_value.streams.streams = {"yard": object()}
        yield frontend.create_app().test_client()


def test_health_shows_tamper_and_only_owner_resets_or_toggles(client, tmp_path):
    owner = basic("owner", "ownerpass")
    password = client.post("/guests", json={"name": "Sam", "hours": 24}, headers=owner).json["password"]
    sam = basic("Sam", password)

    assert client.post("/health/tamper", json={"on": True}, headers=sam).status_code == 403
    assert not tamper.enabled()
    assert client.post("/health/tamper", json={"on": "yes"}, headers=owner).status_code == 400
    assert client.post("/health/tamper", json={"on": True}, headers=owner).json["on"] is True

    run(tmp_path, "yard", scene(), times=1)
    run(tmp_path, "yard", FLAT, times=3)
    page = client.get("/health", headers=owner).get_data(as_text=True)
    assert "looks covered or blacked out" in page and "This is the new normal" in page
    assert "lens_covered" in str(client.get("/api/health", headers=owner).json)
    assert "This is the new normal" not in client.get("/health", headers=sam).get_data(as_text=True)

    assert client.post("/health/tamper/yard/reset", headers=sam).status_code == 403
    assert tamper.STATE == {"yard": "covered"}
    assert client.post("/health/tamper/nope/reset", headers=owner).status_code == 404
    assert client.post("/health/tamper/yard/reset", headers=owner).json == {"status": "saved"}
    assert tamper.STATE == {} and "yard" not in tamper._load().get("base", {})
