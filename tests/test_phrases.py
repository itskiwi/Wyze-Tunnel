import json

import pytest
from wyzebridge import phrases


@pytest.mark.parametrize(
    "name, place",
    [("Front Door", "door"), ("Front Porch", "porch"), ("Garage Door", "garage"), ("Back Yard", "backyard"),
     ("Driveway Cam", "driveway"), ("Doorbell", "door"), ("Kitchen", ""), ("Frontier Room", "door")],
)
def test_guess(name, place):
    assert phrases.guess(name) == place


@pytest.mark.parametrize(
    "label, name, place, state, text",
    [
        ("person", "Front Door", "door", "new", "Someone walked up to the door"),
        ("person", "Porch", "porch", "new", "Someone walked up to the porch"),
        ("person", "Backyard", "backyard", "new", "Someone's in the backyard"),
        ("person", "Kitchen Cam", "indoors", "new", "Someone's in the Kitchen"),
        ("person", "Street", "street", "new", "Someone walked by on the street"),
        ("car", "Driveway", "driveway", "new", "A car in the driveway"),
        ("car", "Driveway", "driveway", "stayed", "A car pulled into the driveway"),
        ("truck", "Street", "street", "new", "A truck went by"),
        ("vehicle", "Front Door", "door", "new", "A vehicle stopped out front"),
        ("dog", "Backyard", "backyard", "new", "A dog in the backyard"),
        ("pet", "Backyard", "backyard", "new", "Pet movement in the backyard"),
        ("package", "Porch", "porch", "new", "A package was left on the porch"),
        ("motion", "Side Gate", "", "new", "Movement on Side Gate"),
        ("motion", "Office", "other", "new", "Movement on Office"),
        ("ring", "Doorbell", "door", "new", "Someone rang the doorbell"),
        ("cleared", "Porch", "porch", "new", "The porch has cleared"),
        ("cleared", "Front Door", "door", "new", "The front door has cleared"),
        ("cleared", "Office", "indoors", "new", "Office has cleared"),
    ],
)
def test_describe(label, name, place, state, text):
    assert phrases.describe(label, name, place, state) == text


def test_saved_place_beats_the_guess(tmp_path, monkeypatch):
    monkeypatch.setattr(phrases, "PLACES_FILE", str(tmp_path / "places.json"))
    assert phrases.place_of("front-door", "Front Door") == "door"
    phrases.save_place("front-door", "porch")
    assert phrases.place_of("front-door", "Front Door") == "porch"
    phrases.save_place("front-door", "")
    assert json.loads((tmp_path / "places.json").read_text()) == {}
    with pytest.raises(ValueError):
        phrases.save_place("front-door", "moon")
