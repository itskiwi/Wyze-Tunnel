import os
import time
from collections import namedtuple

from wyzebridge import recording

Usage = namedtuple("Usage", "total used free")
GB = 1024**3


def setup(tmp_path, monkeypatch, total, free):
    monkeypatch.setattr(recording, "RECORD_PATH", str(tmp_path / "record" / "%path" / "%Y-%m-%d_%H-%M-%S-%f"))
    monkeypatch.setattr(recording, "SETTINGS_FILE", str(tmp_path / "record.json"))
    monkeypatch.setattr(recording.shutil, "disk_usage", lambda _: Usage(total, total - free, free))
    cam = tmp_path / "record" / "front"
    cam.mkdir(parents=True)
    old = time.time() - 2 * 86400
    for i in range(3):
        clip = cam / f"{i}.mp4"
        clip.write_bytes(b"x" * 1024**2)
        os.utime(clip, (old + i, old + i))
    return cam


def test_keeps_15_percent_of_the_drive_free(tmp_path, monkeypatch):
    # 100 GB drive with 15 GB minus 2 MB free: 15% (15 GB) beats the 5 GB floor
    cam = setup(tmp_path, monkeypatch, 100 * GB, 15 * GB - 2 * 1024**2)
    assert recording.disk_use()["total"] == 100 * GB
    assert recording.trim() == 2 * 1024**2
    assert [f.name for f in cam.iterdir()] == ["2.mp4"]


def test_big_drive_caps_the_floor_at_20gb(tmp_path, monkeypatch):
    # 2 TB drive with 251 GB free (13.5%): 15% would be 279 GB and delete every clip
    setup(tmp_path, monkeypatch, 1862 * GB, 251 * GB)
    assert recording.trim() == 0
    assert not recording.DRIVE_FULL


def test_small_drive_uses_5gb_floor(tmp_path, monkeypatch):
    setup(tmp_path, monkeypatch, 20 * GB, 6 * GB)  # 15% is 3 GB, under 5 GB, and 6 GB is free
    assert recording.trim() == 0


def test_unreadable_drive_deletes_nothing(tmp_path, monkeypatch):
    setup(tmp_path, monkeypatch, 1000 * GB, 0)

    def fail(_):
        raise OSError

    monkeypatch.setattr(recording.shutil, "disk_usage", fail)
    assert recording.trim() == 0


def test_other_files_filling_the_drive_keep_the_last_day(tmp_path, monkeypatch):
    # 100 GB drive, 6 GB free: under the 15 GB floor but over 5 GB, so today's clips stay
    cam = setup(tmp_path, monkeypatch, 100 * GB, 6 * GB)
    for name in ("1.mp4", "2.mp4"):
        os.utime(cam / name, (time.time() - 3600,) * 2)
    assert recording.trim() == 1024**2
    assert sorted(f.name for f in cam.iterdir()) == ["1.mp4", "2.mp4"]
    assert recording.DRIVE_FULL


def test_under_5gb_takes_recent_clips_too(tmp_path, monkeypatch):
    cam = setup(tmp_path, monkeypatch, 100 * GB, 5 * GB - 2 * 1024**2)
    for clip in cam.iterdir():
        os.utime(clip, (clip.stat().st_mtime + 2 * 86400 - 3600,) * 2)  # all from the last hour, oldest first
    assert recording.trim() == 2 * 1024**2
    assert [f.name for f in cam.iterdir()] == ["2.mp4"]
    assert recording.DRIVE_FULL  # still under the 15 GB floor
