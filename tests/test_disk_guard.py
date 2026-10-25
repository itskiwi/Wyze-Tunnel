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
    old = time.time() - 3600
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


def test_small_drive_uses_5gb_floor(tmp_path, monkeypatch):
    setup(tmp_path, monkeypatch, 20 * GB, 6 * GB)  # 15% is 3 GB, under 5 GB, and 6 GB is free
    assert recording.trim() == 0


def test_unreadable_drive_deletes_nothing(tmp_path, monkeypatch):
    setup(tmp_path, monkeypatch, 1000 * GB, 0)

    def fail(_):
        raise OSError

    monkeypatch.setattr(recording.shutil, "disk_usage", fail)
    assert recording.trim() == 0
