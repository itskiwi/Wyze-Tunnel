import pytest
from wyzebridge import floorplan


@pytest.fixture(autouse=True)
def files(tmp_path, monkeypatch):
    monkeypatch.setattr(floorplan, "IMAGE_FILE", tmp_path / "floorplan.img")
    monkeypatch.setattr(floorplan, "PINS_FILE", str(tmp_path / "floorplan.json"))


def test_only_pictures_are_saved():
    floorplan.save_image(b"\x89PNG rest")
    assert floorplan.image_type(floorplan.IMAGE_FILE.read_bytes()) == "image/png"
    with pytest.raises(ValueError):
        floorplan.save_image(b"<svg onload=alert(1)>")


def test_pins_are_clamped_and_filtered():
    pins = floorplan.save_pins({"front": [120, -5], "nope": [1, 1], "back": ["x", 1]}, ["front", "back"])
    assert pins == {"front": [100.0, 0.0]}
    assert floorplan.load_pins() == pins

