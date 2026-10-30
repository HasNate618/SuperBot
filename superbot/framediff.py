"""What changed between two frames, and where.

The adapter's cheapest detector, and the one that needs no model: hold the view
still, move one thing, and the pixels that differ are the thing. That is how the
hand is located in the image without a hand detector - a `project_point` prediction
and a measured centroid become comparable.

Standard library only, because this is arithmetic over nested lists of `(r, g, b)`
ints. Decoding a PNG happens in `spike/wbc/frames.py`, which may import Pillow.

Why not fold this into `glow`: `glow` answers "is something in range" from colour,
against a baseline patch. This answers "what moved", against another whole frame.
They share a threshold idiom and nothing else, and merging them would make the
pickup rule and the localisation rule one function with two meanings.

Nothing here decides what a difference *means*. There is no "the hand is there"
verdict and no threshold that turns into an action: the caller compares a measured
centroid with a projected one, and the servo drives the residual.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

#: Default per-channel level at which a pixel counts as "changed". Matches the
#: pickup rule's `LIT_DELTA`: below this, the difference is compression grain and
#: the hub's CRT noise, both of which are real and neither of which is a hand.
DEFAULT_MIN_DELTA = 40

Pixel = Sequence[int]
Frame = Sequence[Sequence[Pixel]]


@dataclass(frozen=True)
class Delta:
    """The result of comparing two frames.

    `mean` is over every channel of every pixel, so a broad faint change is visible
    even when no single pixel crosses the threshold. `changed` and `fraction` are the
    area over the threshold. `centroid` is the centre of that area in normalised
    coordinates with the origin top-left, matching `superbot.intent/1`'s pixel
    convention, so a measured point and a projected point are directly comparable.

    `centroid` is `None` when nothing changed. It is never a fallback point: an
    invented position would drive an arm into a wall and report a confident success.
    """

    mean: float
    changed: int
    fraction: float
    centroid: tuple[float, float] | None
    bbox: tuple[int, int, int, int] | None


def _rectangular(frame: Frame, name: str) -> tuple[int, int]:
    height = len(frame)
    width = len(frame[0]) if height else 0
    for row in frame:
        if len(row) != width:
            raise ValueError(f"{name} is ragged: expected {width} pixels per row")
    return width, height


def delta(
    before: Frame, after: Frame, min_delta: int = DEFAULT_MIN_DELTA
) -> Delta:
    """Compare two equally sized frames. Raises `ValueError` on any shape mismatch.

    A silently resized or padded frame would shift every coordinate and turn a fit
    into a plausible-looking lie, so shape disagreement is an error rather than
    something to accommodate.
    """
    width_b, height_b = _rectangular(before, "before")
    width_a, height_a = _rectangular(after, "after")
    if (width_b, height_b) != (width_a, height_a):
        raise ValueError(
            f"frame sizes differ: {width_b}x{height_b} vs {width_a}x{height_a}"
        )

    total_pixels = width_b * height_b
    channel_total = 0
    changed = 0
    sum_x = 0.0
    sum_y = 0.0
    x0 = y0 = None
    x1 = y1 = 0

    for y in range(height_b):
        row_before = before[y]
        row_after = after[y]
        for x in range(width_b):
            pb = row_before[x]
            pa = row_after[x]
            d0 = abs(int(pa[0]) - int(pb[0]))
            d1 = abs(int(pa[1]) - int(pb[1]))
            d2 = abs(int(pa[2]) - int(pb[2]))
            channel_total += d0 + d1 + d2
            if max(d0, d1, d2) >= min_delta:
                changed += 1
                sum_x += x + 0.5
                sum_y += y + 0.5
                if x0 is None:
                    x0 = x
                    y0 = y
                    x1 = x
                    y1 = y
                else:
                    x0 = min(x0, x)
                    y0 = min(y0, y)  # type: ignore[type-var]
                    x1 = max(x1, x)
                    y1 = max(y1, y)

    mean = channel_total / (total_pixels * 3) if total_pixels else 0.0
    fraction = changed / total_pixels if total_pixels else 0.0
    centroid = (
        (sum_x / changed / width_b, sum_y / changed / height_b) if changed else None
    )
    bbox = None if x0 is None else (x0, int(y0 or 0), x1, y1)
    return Delta(
        mean=mean, changed=changed, fraction=fraction, centroid=centroid, bbox=bbox
    )
