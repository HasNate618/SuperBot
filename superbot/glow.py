"""The glow: Superhot's own "you are in grab range" signal, read as a pixel delta.

`research/superhot-play.md` records from the operator that an object glows when a
controller is close enough to grab it. That is the only ground truth we will get
for "this grab should have worked", which is why it matters more than it looks: it
turns the reviewer's worst case - a grab that misses is indistinguishable from a
button that never fired - into two distinguishable outcomes.

The game gives three different "you may act now" signals, and conflating them
produces a script that waits forever `[OPERATOR 2026-10-30]`:

1. **The hub floppy and the headset do not glow at all.** Proximity, then one press;
   the confirmation is the object appearing in the hand, which is
   `lit_fraction` below.
2. **The insertion slot glows green** - large and saturated. That is
   `green_centroid` / `green_fraction`: a hue gate, not perception.
3. **Other, in-level grabbables have a faint highlight**, which is `score_patch`: a
   local brightness delta against a **baseline** frame, scored at the 95th
   percentile of per-pixel max-channel. An outline lives in the tail; the mean
   usually does not see it at all.

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


# ---------------------------------------------------------------- appearance
#: A pixel counts as "the object arrived" at this max-channel delta. Above noise
#: and shading, below a lit object replacing a hand.
LIT_DELTA = 40


def lit_fraction(
    before: Frame,
    after: Frame,
    bounds: tuple[int, int, int, int],
    min_delta: int = LIT_DELTA,
) -> float:
    """Fraction of the window whose max-channel delta reaches `min_delta`.

    This is how a hub pickup is confirmed. The floppy has no glow, so the read-back
    is the flat rectangle replacing the hand: an *area* change. A maximum would call
    one specular pixel a pickup.
    """
    x0, y0, x1, y1 = bounds
    total = 0
    lit = 0
    for y in range(y0, y1):
        before_row = before[y]
        after_row = after[y]
        for x in range(x0, x1):
            total += 1
            b = before_row[x]
            a = after_row[x]
            if max(a[0] - b[0], a[1] - b[1], a[2] - b[2]) >= min_delta:
                lit += 1
    return lit / total if total else 0.0


# ---------------------------------------------------------------- green slot
#: Green must lead both other channels by this much, or white and grey pass.
#:
#: MEASURED 2026-10-30 on the hub frame
#: `/tmp/pi-clipboard-bafecc17-e9f7-4601-8b13-8cc5b272dd55.png` (1127x853, the
#: drive bay with the glowing slot at ~(0.76, 0.87)): at (25, 60) the centroid is
#: pulled to (0.53, 0.78) by sticky notes and foliage (23k px); at (40, 120) it
#: sits on the slot at (0.768, 0.890) over 9k px; at (60, 140) it tightens to
#: (0.763, 0.873) over 2.9k px. So 25 is too loose for the hub and 60 is usable
#: but close to the slot's own lead (~66 over its bluish B channel, mean G 222 /
#: R 86 / B 156). 40 is the operating point: it holds the slot with margin to
#: spare and still rejects grey (lead 5), white (lead 0) and dark green (G 12).
GREEN_MARGIN = 40
#: ...and clear this on its own, or a dark green surface passes. The slot's mean
#: green is 198-222 across the usable gates; 120 keeps it with room below.
GREEN_FLOOR = 120
#: Below this fraction of the scanned area the region is a speck, not a slot.
MIN_GREEN_FRACTION = 0.002


def is_green(rgb: RGB, margin: int = GREEN_MARGIN, floor: int = GREEN_FLOOR) -> bool:
    """True when a pixel is the slot's green: bright, and dominant over the rest."""
    g = rgb[1]
    return g >= floor and g - max(rgb[0], rgb[2]) >= margin


def _scan_region(
    frame: Frame, bounds: tuple[int, int, int, int] | None
) -> tuple[int, int, int, int]:
    if bounds is not None:
        return bounds
    if not frame or not frame[0]:
        raise ValueError("frame must have a positive size")
    return (0, 0, len(frame[0]), len(frame))


def green_fraction(
    frame: Frame, bounds: tuple[int, int, int, int] | None = None
) -> float:
    """Share of the scanned area that is the slot's green."""
    x0, y0, x1, y1 = _scan_region(frame, bounds)
    total = 0
    hits = 0
    for y in range(y0, y1):
        row = frame[y]
        for x in range(x0, x1):
            total += 1
            if is_green(row[x]):
                hits += 1
    return hits / total if total else 0.0


def green_centroid(
    frame: Frame,
    bounds: tuple[int, int, int, int] | None = None,
    min_fraction: float = MIN_GREEN_FRACTION,
) -> tuple[float, float] | None:
    """Normalised `(u, v)` centre of the green region, or None if there is none.

    None rather than a fallback point on purpose: a centroid invented from noise
    would servo the arm at a wall and report a confident failure. A centroid is
    also still a 2D target, so this stays inside the design's no-3D rule - the game
    paints where the slot is, and we point at the paint.
    """
    x0, y0, x1, y1 = _scan_region(frame, bounds)
    total = 0
    hits = 0
    sum_x = 0
    sum_y = 0
    for y in range(y0, y1):
        row = frame[y]
        for x in range(x0, x1):
            total += 1
            if is_green(row[x]):
                hits += 1
                sum_x += x
                sum_y += y
    if not total or not hits or (hits / total) < min_fraction:
        return None
    width = len(frame[0])
    height = len(frame)
    # Normalise against the last index so the far edge maps to 1.0, matching the
    # [0, 1] pixel convention in `superbot.intent`.
    cx = (sum_x / hits) / max(1, width - 1)
    cy = (sum_y / hits) / max(1, height - 1)
    return (cx, cy)
