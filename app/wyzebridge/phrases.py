"""Plain sentences for what a camera saw: "Someone walked up to the door", not "Motion detected"."""

import json
import re

from wyzebridge.config import TOKEN_PATH

PLACES_FILE = TOKEN_PATH + "cam_places.json"
# What the camera page offers; "" means guess from the camera's name.
PLACE_CHOICES = ("door", "porch", "driveway", "garage", "backyard", "street", "indoors", "other")
OUTDOOR = {"door", "porch", "driveway", "garage", "backyard", "yard", "garden", "patio", "deck", "street"}
# First word that matches wins, so "Front Porch" is a porch and "Garage Door" a garage.
GUESSES = (
    ("porch", "porch"), ("driveway", "driveway"), ("carport", "driveway"), ("garage", "garage"),
    ("backyard", "backyard"), ("back yard", "backyard"), ("garden", "garden"), ("patio", "patio"),
    ("deck", "deck"), ("yard", "yard"), ("street", "street"), ("road", "street"), ("sidewalk", "street"),
    ("doorbell", "door"), ("door", "door"), ("entry", "door"), ("front", "door"),
)
PREP = {"door": "at the door", "porch": "on the porch", "street": "on the street", "deck": "on the deck", "patio": "on the patio"}
VEHICLES = {"car": "A car", "truck": "A truck", "bus": "A bus", "motorcycle": "A motorbike", "vehicle": "A vehicle"}
THINGS = {"dog": "A dog", "cat": "A cat", "bird": "A bird", "pet": "Pet movement", "package": "A package", "bicycle": "Someone on a bike"}


def load_places() -> dict[str, str]:
    """Places picked on the Cameras page, by stream name."""
    try:
        with open(PLACES_FILE) as f:
            places = json.load(f)
        return places if isinstance(places, dict) else {}
    except (FileNotFoundError, ValueError):
        return {}


def save_place(uri: str, place: str) -> None:
    if place and place not in PLACE_CHOICES:
        raise ValueError("Pick one of the listed places")
    places = load_places()
    places[uri] = place
    if not place:
        places.pop(uri)
    with open(PLACES_FILE, "w") as f:
        json.dump(places, f)


def guess(name: str) -> str:
    low = name.lower()
    return next((place for word, place in GUESSES if re.search(rf"\b{word}", low)), "")


def place_of(uri: str, name: str) -> str:
    return load_places().get(uri) or guess(name)


def _where(place: str, name: str) -> str:
    if place in PREP:
        return PREP[place]
    if place and place not in ("indoors", "other"):
        return f"in the {place}"
    room = re.sub(r"\s*\b(cam|camera)\b\s*$", "", name, flags=re.I) or name  # "Kitchen Cam" -> "Kitchen"
    return f"in the {room}" if place == "indoors" else f"on {name}"


def describe(label: str, name: str, place: str = "", state: str = "new") -> str:
    """One sentence for an event. label is a detection label, "motion", "ring" or "cleared".

    state "stayed" means a vehicle was still there a minute later, so it really pulled in.
    """
    where = _where(place, name)
    door = place in ("door", "porch")
    if label == "ring":
        return "Someone rang the doorbell"
    if label == "cleared":
        return f"The {'front door' if place == 'door' else place} has cleared" if place in OUTDOOR else f"{name} has cleared"
    if label == "person":
        if door:
            return f"Someone walked up to the {place}"
        return "Someone walked by on the street" if place == "street" else f"Someone's {where}"
    if label in VEHICLES:
        who = VEHICLES[label]
        if place == "street":
            return f"{who} went by"
        if door:
            return f"{who} stopped out front"
        if place in ("driveway", "garage") and state == "stayed":
            return f"{who} pulled into the {place}"
        return f"{who} {where}"
    if label == "package" and door:
        return f"A package was left {where}"
    return f"{THINGS.get(label) or 'Movement'} {where}"
