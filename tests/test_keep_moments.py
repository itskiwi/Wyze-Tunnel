import os
import time

from wyzebridge import recording

DAY = 86400


def setup(tmp_path, monkeypatch, settings):
    monkeypatch.setattr(recording, "RECORD_PATH", str(tmp_path / "record" / "%path" / "%Y-%m-%d_%H-%M-%S-%f"))
    monkeypatch.setattr(recording, "SETTINGS_FILE", str(tmp_path / "record.json"))
    monkeypatch.setattr(recording, "MOMENTS_FILE", str(tmp_path / "moments.db"))
    monkeypatch.setattr(recording, "shrink_encoder", lambda: "")
    recording.save_settings(settings, ["front"])
    (tmp_path / "record" / "front").mkdir(parents=True)


def clip(tmp_path, name, age, cam="front"):
    path = tmp_path / "record" / cam / f"{name}.mp4"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * 1024**2)
    t = time.time() - age
    os.utime(path, (t, t))
    return path


def names(tmp_path, cam="front"):
    return sorted(p.stem for p in (tmp_path / "record" / cam).glob("*.mp4"))


def test_old_footage_keeps_only_moments_and_stars(tmp_path, monkeypatch):
    setup(tmp_path, monkeypatch, {"cams": ["front"], "keep_days": 7})  # moments default to 30 days
    now = time.time()
    clip(tmp_path, "today", 3600)
    clip(tmp_path, "week-plain", 8 * DAY + 1000)
    moment = clip(tmp_path, "week-moment", 8 * DAY)
    recording.mark("front", moment.stat().st_mtime - 20)  # in the clip
    clip(tmp_path, "week-near", 8 * DAY - 100)  # 100s after that moment: past PAD and a 60s clip
    old = clip(tmp_path, "old-moment", 31 * DAY)
    recording.mark("front", old.stat().st_mtime)
    starred = clip(tmp_path, "old-star", 200 * DAY)
    recording.star("front", starred.stat().st_mtime - 5)
    clip(tmp_path, "other-plain", 8 * DAY, cam="side")  # not picked on this page: MediaMTX's job
    assert recording.keep() == "0s"
    deleted = recording.expire(now)
    assert names(tmp_path) == ["old-star", "today", "week-moment"], names(tmp_path)
    assert deleted == 3
    assert names(tmp_path) == ["old-star", "today", "week-moment"]
    assert names(tmp_path, "side") == ["other-plain"]
    recording.star("front", starred.stat().st_mtime - 5, on=False)
    assert recording.expire(now) == 1 and names(tmp_path) == ["today", "week-moment"]


def test_off_deletes_everything_at_keep_days(tmp_path, monkeypatch):
    setup(tmp_path, monkeypatch, {"all": True, "keep_days": 7, "event_days": 0})
    moment = clip(tmp_path, "week-moment", 8 * DAY)
    recording.mark("front", moment.stat().st_mtime)
    clip(tmp_path, "today", 3600)
    assert recording.keep_days() == (7, 7)
    assert recording.expire() == 1 and names(tmp_path) == ["today"]


def test_space_goes_from_empty_footage_first_and_never_stars(tmp_path, monkeypatch):
    setup(tmp_path, monkeypatch, {"cams": ["front"], "keep_days": 14})
    monkeypatch.setattr(recording, "MIN_FREE", 0)
    monkeypatch.setattr(recording, "MIN_FREE_SHARE", 0)
    recent = clip(tmp_path, "a-recent-moment", 3600)
    recording.mark("front", recent.stat().st_mtime)
    clip(tmp_path, "b-recent-plain", 1800)
    moment = clip(tmp_path, "c-moment", 3 * DAY)
    recording.mark("front", moment.stat().st_mtime)
    clip(tmp_path, "d-plain", 2 * DAY)  # newer than the moment, still goes first
    starred = clip(tmp_path, "e-star", 10 * DAY)
    recording.star("front", starred.stat().st_mtime)
    for over, left in ((1, ["a-recent-moment", "b-recent-plain", "c-moment", "e-star"]),
                       (1, ["a-recent-moment", "b-recent-plain", "e-star"]),
                       (1, ["b-recent-plain", "e-star"]),  # the last day goes last, oldest first
                       (5, ["e-star"])):
        monkeypatch.setattr(recording, "disk_use", lambda: {"used": 0, "free": 10**12, "total": 0})
        monkeypatch.setattr(recording, "MIN_FREE", 10**12 + over * 1024**2)
        recording.trim()
        assert names(tmp_path) == left


def test_settings_check_choices(tmp_path, monkeypatch):
    setup(tmp_path, monkeypatch, {})
    assert recording.load_settings()["event_days"] == recording.DEFAULT_EVENT_DAYS
    assert recording.load_settings()["shrink"] is False  # no graphics chip here
    for bad in (7, "x"):
        try:
            recording.save_settings({"event_days": bad}, [])
            raise AssertionError("accepted")
        except ValueError:
            pass


def test_shrink_keeps_age_and_only_replaces_when_smaller(tmp_path, monkeypatch):
    import shutil
    import subprocess

    import pytest

    if not shutil.which("ffmpeg"):
        pytest.skip("no ffmpeg")
    setup(tmp_path, monkeypatch, {"cams": ["front"]})
    monkeypatch.setattr(recording, "shrink_encoder", lambda: "libx264")  # a test stand-in for a graphics chip
    path = tmp_path / "record" / "front" / "a.mp4"
    cmd = ["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=15:duration=3", "-c:v", "libx264",
           "-b:v", "4000k", "-movflags", "frag_keyframe+empty_moov+default_base_moof", str(path)]
    subprocess.run(cmd, check=True)
    os.utime(path, (1000, 1000))
    before = path.stat().st_size
    assert recording.shrink(path) and path.stat().st_size < before * 0.8 and path.stat().st_mtime == 1000
    assert not recording.shrink(path)  # once only
    assert list(path.parent.iterdir()) == [path]
