"""Floor-plan camera map: a picture of the home with a pin for each camera."""

import json
from pathlib import Path

from wyzebridge.config import TOKEN_PATH

IMAGE_FILE = Path(TOKEN_PATH, "floorplan.img")
PINS_FILE = TOKEN_PATH + "floorplan.json"
MAX_BYTES = 8 * 1024 * 1024
TYPES = {b"\x89PNG": "image/png", b"\xff\xd8\xff": "image/jpeg", b"RIFF": "image/webp"}


def image_type(data: bytes) -> str | None:
    return next((t for magic, t in TYPES.items() if data.startswith(magic)), None)


def save_image(data: bytes) -> None:
    """Raises ValueError with a message for the user."""
    if len(data) > MAX_BYTES:
        raise ValueError("That picture is over 8 MB. Pick a smaller one.")
    if not image_type(data):
        raise ValueError("Pick a PNG, JPEG or WebP picture.")
    IMAGE_FILE.write_bytes(data)


def load_pins() -> dict[str, list[float]]:
    """{uri: [x, y]} as percentages of the picture's width and height."""
    try:
        with open(PINS_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return {}


def save_pins(pins, uris: list[str]) -> dict[str, list[float]]:
    if not isinstance(pins, dict):
        raise ValueError("Pins must be a map of camera to position")
    clean = {}
    for uri, pos in pins.items():
        if uri in uris and isinstance(pos, list) and len(pos) == 2 and all(isinstance(v, (int, float)) for v in pos):
            clean[uri] = [round(min(max(float(v), 0), 100), 2) for v in pos]
    with open(PINS_FILE, "w") as f:
        json.dump(clean, f)
    return clean
