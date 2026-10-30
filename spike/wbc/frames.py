#!/usr/bin/env python3
"""Frames on this side of the fence, so `superbot/` can stay standard-library only.

`superbot/framediff.py` and `superbot/glow.py` do the arithmetic over nested lists of
`(r, g, b)` ints. Something has to turn a PNG into those lists, and Pillow is not
allowed inside `superbot/`. So decoding lives here, in the spike, with the venv.

Capture goes through `superbot.preview.PreviewCapture.capture_once` rather than a
second screenshooter wrapper: that function already knows the two traps found the
hard way - the tool ignores its output argument and writes a *timestamped* filename
into the current directory, and the game only appears when its window is forward.

MEASURED 2026-10-30: `weston-screenshooter` is not blocked; an earlier note claiming
so was wrong. It writes `wayland-screenshot-<ts>.png` into the cwd whatever path you
pass, which is exactly what `newest_png` handles.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from superbot.preview import PreviewCapture  # noqa: E402


@dataclass(frozen=True)
class Frame:
    """Pixels as nested `[y][x] = (r, g, b)` ints, which is what the stdlib expects."""

    width: int
    height: int
    rows: list[list[tuple[int, int, int]]]

    def as_rows(self) -> list[list[tuple[int, int, int]]]:
        return self.rows


def load(path: str | Path) -> Frame:
    """Decode a PNG (or anything Pillow reads) into a `Frame`, in RGB."""
    from PIL import Image  # imported late so --help works without the venv

    with Image.open(path) as image:
        rgb = image.convert("RGB")
        width, height = rgb.size
        # `getdata()` is typed as an opaque ImagingCore in Pillow's stubs, so
        # `list(...)` reads as a protocol violation. `tobytes()` is plain bytes,
        # which indexes to ints and needs no suppression - and it is faster.
        raw = rgb.tobytes()
    stride = width * 3
    rows = [
        [(raw[i], raw[i + 1], raw[i + 2]) for i in range(y * stride, (y + 1) * stride, 3)]
        for y in range(height)
    ]
    return Frame(width=width, height=height, rows=rows)


def downscale(frame: Frame, step: int) -> Frame:
    """Box-average by an integer `step`, e.g. 4 turns 1920x1080 into 480x270.

    Averaging rather than nearest-neighbour because this feeds a difference against a
    noise baseline: averaging shrinks sensor and VNC grain, which is the thing the
    baseline is trying to characterise. The trailing remainder (if the size is not a
    multiple of `step`) is dropped, and the resulting size is reported so no caller
    has to guess it.
    """
    if step < 1:
        raise ValueError("step must be >= 1")
    if step == 1:
        return frame
    out: list[list[tuple[int, int, int]]] = []
    for y in range(0, frame.height - step + 1, step):
        row: list[tuple[int, int, int]] = []
        for x in range(0, frame.width - step + 1, step):
            r = g = b = 0
            for dy in range(step):
                source = frame.rows[y + dy]
                for dx in range(step):
                    pr, pg, pb = source[x + dx]
                    r += pr
                    g += pg
                    b += pb
            n = step * step
            row.append((r // n, g // n, b // n))
        out.append(row)
    width = len(out[0]) if out else 0
    return Frame(width=width, height=len(out), rows=out)


def capture(
    *,
    screenshooter: str,
    runtime_dir: str | Path,
    wayland_display: str,
    work_dir: str | Path,
    destination: str | Path,
) -> Path:
    """Capture one frame and publish it to `destination`.

    Returns the published path. Raises `RuntimeError` when the capture produced no
    new PNG, which is also how a dead compositor shows up.
    """
    shot = PreviewCapture(
        width=0,
        height=0,
        panel_height=0,
        fps=0.0,
        screenshooter=screenshooter,
        runtime_dir=Path(runtime_dir),
        wayland_display=wayland_display,
        work_dir=Path(work_dir),
        publish_path=Path(destination),
    )
    if not shot.capture_once():
        raise RuntimeError("capture_once reported failure")
    published = Path(destination)
    if not published.exists():
        raise RuntimeError(f"capture published nothing at {published}")
    return published
