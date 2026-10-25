"""Privacy masks: squares of a camera's picture blacked out in clips shared by link, like a neighbor's window."""

import json

from wyzebridge.config import TOKEN_PATH
from wyzebridge.detect import HEAT_H, HEAT_W

MASKS_FILE = TOKEN_PATH + "masks.json"


def load_masks() -> dict[str, list[int]]:
    try:
        with open(MASKS_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return {}


def save_mask(uri: str, cells) -> list[int]:
    """Save the squares to black out on one camera. Raises ValueError with a message for the user."""
    if not isinstance(cells, list) or not all(isinstance(i, int) and 0 <= i < HEAT_W * HEAT_H for i in cells):
        raise ValueError("Pick squares on the picture to hide.")
    masks = load_masks()
    masks[uri] = sorted(set(cells))
    if not masks[uri]:
        del masks[uri]
    with open(MASKS_FILE, "w") as f:
        json.dump(masks, f)
    return masks.get(uri, [])


def video_filter(cells: list[int]) -> str:
    """ffmpeg drawbox filters filling the masked squares black, one box per run of squares in a row."""
    boxes, cells = [], set(cells)
    for row in range(HEAT_H):
        col = 0
        while col < HEAT_W:
            if row * HEAT_W + col not in cells:
                col += 1
                continue
            start = col
            while col < HEAT_W and row * HEAT_W + col in cells:
                col += 1
            # +2 pixels so neighboring boxes overlap and no sliver shows through from rounding.
            boxes.append(
                f"drawbox=x=iw*{start}/{HEAT_W}:y=ih*{row}/{HEAT_H}:w=iw*{col - start}/{HEAT_W}+2:h=ih/{HEAT_H}+2"
                ":color=black:t=fill"
            )
    return ",".join(boxes)
