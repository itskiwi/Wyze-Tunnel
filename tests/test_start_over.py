from pathlib import Path
from unittest import mock

import pytest


@pytest.fixture
def setup(monkeypatch, tmp_path):
    import frontend
    from wyzebridge import config, recording, web_ui

    tokens, img, rec = tmp_path / "tokens", tmp_path / "img", tmp_path / "tokens" / "recordings"
    for folder in (tokens / "models", img, rec / "porch"):
        folder.mkdir(parents=True)
    monkeypatch.setattr(config, "TOKEN_PATH", f"{tokens}/")
    monkeypatch.setattr(config, "IMG_PATH", f"{img}/")
    monkeypatch.setattr(recording, "record_root", lambda: rec)
    monkeypatch.setattr(web_ui.auth, "verify_password_callback", lambda user, password: True)
    with mock.patch.object(frontend, "WyzeBridge") as bridge, mock.patch.object(frontend, "Thread") as thread:
        yield frontend.create_app().test_client(), tokens, img, rec, bridge.return_value, thread


def test_start_over_keeps_login_models_and_recordings(setup):
    client, tokens, img, rec, wb, thread = setup
    for name in ("wb_login", "wb_password", "wb_api", "auth.pickle", "alerts.json", "record.json"):
        (tokens / name).write_text("x")
    (tokens / "models" / "model.onnx").write_text("x")
    (tokens / "cache").mkdir()
    (tokens / "cache" / "a.json").write_text("x")
    (img / "porch.jpg").write_text("x")
    (rec / "porch" / "clip.mp4").write_text("x")

    resp = client.post("/start-over")
    assert resp.status_code == 200 and resp.json["status"] == "cleared"
    assert sorted(p.name for p in tokens.iterdir()) == sorted(
        ["wb_login", "wb_password", "wb_api", "models", "recordings", Path(resp.json["backup"]).name]
    )
    assert Path(resp.json["backup"]).exists()
    assert (tokens / "models" / "model.onnx").exists()
    assert (rec / "porch" / "clip.mp4").exists()
    assert list(img.iterdir()) == []
    wb.api.clear_cache.assert_called_once()
    assert thread.call_args.kwargs["target"] == wb.restart


def test_start_over_refuses_when_recordings_sit_in_the_picture_folder(setup, monkeypatch):
    from wyzebridge import recording

    client, _, img, *_ = setup
    monkeypatch.setattr(recording, "record_root", lambda: img)
    (img / "clip.mp4").write_text("x")
    assert client.post("/start-over").status_code == 200
    assert (img / "clip.mp4").exists()


def test_start_over_blocks_cross_site(setup):
    client, tokens, *_ = setup
    (tokens / "alerts.json").write_text("x")
    assert client.post("/start-over", headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
    assert (tokens / "alerts.json").exists()
