# Slot-green calibration

**Date:** 2026-10-30
**Status:** measured on one hub frame. Thresholds in `superbot/glow.py` now come from here.

The hub's disk-insertion slot glows green when it is ready `[OPERATOR 2026-10-30]`.
Frame: `/tmp/pi-clipboard-bafecc17-e9f7-4601-8b13-8cc5b272dd55.png` (1127x853),
drive bay, slot visually at about (0.76, 0.87) in normalised HMD pixels. Same
scene also contains green sticky notes and foliage, which is what makes the
calibration non-trivial.

## Gate sweep (`green_centroid`, whole frame)

| margin | floor | px | fraction | centroid | verdict |
|---|---|---|---|---|---|
| 25 | 60 | 23,540 | 0.0245 | (0.533, 0.776) | notes + foliage; centroid off the slot |
| 25 | 100 | 15,519 | 0.0161 | (0.728, 0.867) | moving toward the slot |
| 40 | 120 | 9,363 | 0.0097 | (0.768, 0.890) | **on the slot** |
| 60 | 140 | 2,889 | 0.0030 | (0.763, 0.873) | on the slot, tighter box x[754,956] y[709,819] |

Top-2000 most green-dominant pixels: centroid (0.763, 0.867). Same place.

Slot colour at the tight gate: mean G 222 / R 86 / B 156. The blue channel is
high (cyan-ish glow), so the slot's own lead over `max(R, B)` is only ~66. A
margin of 60 works but sits close to the paint; 40 holds the slot over 9k px
with room to spare.

## Operating point

`GREEN_MARGIN = 40`, `GREEN_FLOOR = 120`, `MIN_GREEN_FRACTION = 0.002` (slot at
0.003-0.010 across the usable gates, so the floor is below the signal, not on
it). Rejections confirmed on synthetic frames: grey with a 5-level green lead,
white, dark green, red — all `None` (`tests/test_glow.py`).

## Caveats

- One frame, one lighting state. Superhot's time-slow red flash was not in it;
  re-measure if the gate ever fires during a flash.
- The frame is a desktop screenshot of the preview, i.e. VNC-decoded pixels, so
  the margins already include whatever the VNC path does to colour.
- A fresh `weston-screenshooter` capture of the live hub agrees on the scene
  layout (sticky notes, CRT static) but was taken at a different head pose and
  does not contain the slot; it was not used for numbers.
