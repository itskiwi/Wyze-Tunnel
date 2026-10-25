from wyzebridge import visitors


def test_rings_are_kept_with_a_picture(tmp_path, monkeypatch):
    monkeypatch.setattr(visitors, "VISITORS_FILE", str(tmp_path / "visitors.json"))
    monkeypatch.setattr(visitors, "PIC_DIR", tmp_path / "visitors")
    monkeypatch.setattr(visitors, "IMG_PATH", str(tmp_path) + "/")
    monkeypatch.setattr(visitors, "KEEP", 2)
    (tmp_path / "door.jpg").write_bytes(b"jpg")
    for ts in (1, 2, 3):
        visitors.add("door", "Front door", ts)
    assert [v["ts"] for v in visitors.load()] == [3, 2]
    assert sorted(p.name for p in (tmp_path / "visitors").iterdir()) == ["door_2.jpg", "door_3.jpg"]
    assert visitors.add("gone", "No picture", 4)["pic"] is None
