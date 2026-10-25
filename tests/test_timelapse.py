import shutil
from datetime import date, datetime
from pathlib import Path

import pytest
from wyzebridge import timelapse


@pytest.fixture(autouse=True)
def root(tmp_path, monkeypatch):
    monkeypatch.setattr(timelapse, "ROOT", tmp_path / "timelapse")
    monkeypatch.setattr(timelapse, "IMG_PATH", str(tmp_path) + "/")
    monkeypatch.setattr(timelapse, "SETTINGS_FILE", str(tmp_path / "timelapse.json"))
    (tmp_path / "front.jpg").write_bytes(b"jpg")
    return tmp_path


def test_frames_are_numbered():
    now = datetime(2026, 10, 24, 12)
    assert timelapse.add_frame("front", now).name == "0000.jpg"
    assert timelapse.add_frame("front", now).name == "0001.jpg"


def test_settings_keep_only_known_cams():
    assert timelapse.save_cams(["front", "nope"], ["front"]) == ["front"]
    assert timelapse.load_cams() == ["front"]
    with pytest.raises(ValueError):
        timelapse.save_cams("front", ["front"])


def test_tidy_stitches_finished_days_and_prunes(monkeypatch):
    timelapse.add_frame("front", datetime(2026, 10, 23, 12))
    timelapse.add_frame("front", datetime(2026, 10, 24, 12))
    old = timelapse.ROOT / "front" / "2026-09-01.mp4"
    old.write_bytes(b"")
    made = []
    monkeypatch.setattr(timelapse, "stitch", lambda day: made.append(day.name))
    timelapse.tidy(date(2026, 10, 24))
    assert made == ["2026-10-23"]
    assert not old.exists()


def test_video_path_rejects_odd_names():
    (timelapse.ROOT / "front").mkdir(parents=True)
    (timelapse.ROOT / "front" / "2026-10-23.mp4").write_bytes(b"")
    assert timelapse.video_path("front", "2026-10-23")
    assert timelapse.video_path("front", "../../x") is None
    assert timelapse.video_path("..", "2026-10-23") is None
    assert timelapse.videos() == {"front": ["2026-10-23"]}


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")
def test_stitch_makes_a_video(root):
    import subprocess
    day = timelapse.ROOT / "front" / "2026-10-23"
    day.mkdir(parents=True)
    for i in range(3):
        subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=blue:s=64x48", "-frames:v", "1", str(day / f"{i:04d}.jpg")], check=True)
    assert timelapse.stitch(day)
    assert Path(str(day) + ".mp4").stat().st_size > 0 and not day.exists()
