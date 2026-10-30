"""The glow: Superhot's own "you are in grab range" signal, read as a pixel delta.

`research/superhot-play.md` records from the operator that an object glows when a
controller is close enough to grab it. That is the only ground truth we will get
for "this grab should have worked", which is why it matters more than it looks: it
turns the reviewer's worst case - a grab that misses is indistinguishable from a
button that never fired - into two distinguishable outcomes.

What this module is: a local brightness delta against a **baseline** frame, scored
at the 95th percentile of per-pixel max-channel. An outline lives in the tail of
the distribution; the mean usually does not see it at all.

What it is deliberately not:

- `frame[t] - frame[t-1]`. The approaching hand is a huge diff, so consecutive
  differencing reports "glow" the entire way in.
- A full-frame mean. Superhot flashes red when time moves, and the controller mesh
  brightens as it enters the view; either swamps a global statistic.
- A model. A 62-175 ms chooser answering "is it grabbable" would gate a 200 ms
  reach behind a decision, and a wrong "no" is indistinguishable from a miss.

No threshold lives here. `score_patch` reports a number; the caller compares it to
a threshold measured from a real on/off pair (spec S4). If those two stills do not
separate, glow is not an oracle and the caller must stop, not tune.
"""

from __future__ import annotations

from collections.abc import Sequence

#: Patch side as a fraction of the frame's smaller dimension.
PATCH_FRACTION = 0.05
#: Floor in pixels, so a tiny preview still gets a usable window.
MIN_PATCH_PX = 16
#: Which point of the patch's brightness distribution counts as "lit".
GLOW_PERCENTILE = 95

RGB = Sequence[int]
Frame = Sequence[Sequence[RGB]]


def patch_side(width: int, height: int) -> int:
    """Window side in pixels for a frame of this size."""
    if width <= 0 or height <= 0:
        raise ValueError("frame must have a positive size")
    return max(MIN_PATCH_PX, round(PATCH_FRACTION * min(width, height)))


def patch_bounds(u: float, v: float, width: int, height: int) -> tuple[int, int, int, int]:
    """`(x0, y0, x1, y1)` of the window around a normalised point, clipped.

    Clipping rather than raising is the point: a target near the frame edge is a
    legitimate place to look, and `IndexError` inside a 10-30 Hz loop would kill
    the reach rather than report it.
    """
    side = patch_side(width, height)
    cx = int(round(u * (width - 1)))
    cy = int(round(v * (height - 1)))
    half = side // 2
    x0 = max(0, min(width - 1, cx - half))
    y0 = max(0, min(height - 1, cy - half))
    x1 = max(x0 + 1, min(width, cx + side - half))
    y1 = max(y0 + 1, min(height, cy + side - half))
    return (x0, y0, x1, y1)


def _percentile(values: list[int], pct: int) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    index = int((pct / 100.0) * (len(ordered) - 1))
    return ordered[max(0, min(index, len(ordered) - 1))]


def score_patch(
    before: Frame,
    after: Frame,
    bounds: tuple[int, int, int, int],
    percentile: int = GLOW_PERCENTILE,
) -> float:
    """`percentile`-th brightest positive delta inside a window.

    `before` is the baseline taken with the hand retracted, `after` is the current
    frame. Both are rows of RGB triples. Differences are clipped at zero, so an
    object that got *darker* scores nothing - we want a highlight, not any change.

    The percentile is taken over **every** pixel in the window, including the
    unchanged ones. An earlier version collected only the changed pixels first,
    which made the statistic collapse to "the brightest changed pixel": a single
    stray bright pixel then scored as high as a lit object, and the
    `one_stray_pixel_is_not_an_outline` test caught it. Over the whole window the
    score means what it claims - "how bright are the brightest few percent of this
    patch" - and a highlight has to cover a real fraction of it.

    `percentile` is a parameter so S4 can calibrate against a measured on/off pair
    without editing this module.
    """
    x0, y0, x1, y1 = bounds
    values: list[int] = []
    for y in range(y0, y1):
        before_row = before[y]
        after_row = after[y]
        for x in range(x0, x1):
            b = before_row[x]
            a = after_row[x]
            delta = max(a[0] - b[0], a[1] - b[1], a[2] - b[2])
            values.append(delta if delta > 0 else 0)
    return float(_percentile(values, percentile))
