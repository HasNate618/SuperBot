#!/usr/bin/env python3
"""Find out what the robot can actually see, from the pixels.

Three questions, in the order they must be answered:

1. `shot`   - is the frame the game at all, and where is the green slot?
2. `motion` - does the view *respond* when the head turns?
3. `sweep`  - at which head yaw does the slot come closest to the centre?

Everything is printed as numbers with the verdict, because a verdict alone cannot be
argued with or re-checked.

An honest limitation, found while building this: a frame showing Steam's UI and a
frame showing the game are nearly identical by mean colour (measured today: Steam
73.1/105.8/117.1, game 76.3/108.8/120.8), so no colour heuristic separates them. What
does separate them is `motion`: a Steam window does not change when the robot turns
its head. So the verdict is "responding" or "not responding", and *not responding*
covers both a stale capture and the wrong window on top. Telling those two apart needs
a human glance - which is cheaper than a fake classifier.

Images are analysed in-process from a PNG (`--image`) so the maths can be checked
against a known frame without a session. Live modes capture through
`spike/wbc/frames.py`, which reuses the dashboard's own capture path.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent.parent))

import superbot.framediff as framediff  # noqa: E402
import superbot.glow as glow  # noqa: E402

#: Above this fraction of changed pixels the whole view moved, which is a failed
#: settle or a scene change - not a hand, and not a head turn we can attribute.
BIG_MOVE_FRACTION = 0.25

#: A head turn of 20 deg measured 15k-17k changed pixels at mean 3.3-4.3 on 1920x1080,
#: while a stale frame moved ~1k at 0.17 (MEASURED 2026-10-30). Thresholds sit an order
#: of magnitude on either side of that gap rather than on one of its edges.
RESPONDING_FRACTION = 0.002
RESPONDING_MEAN = 0.35


def _load(path: str, step: int):
    import frames  # spike-local: needs Pillow

    try:
        frame = frames.load(path)
    except OSError as exc:
        # A mistyped path is a user error, not a crash: say which file, then stop.
        raise SystemExit(f"cannot read {path}: {exc}") from exc
    if step > 1:
        frame = frames.downscale(frame, step)
    return frame


def analyse(frame, baseline=None) -> dict:
    """Everything measurable about one frame, as plain numbers."""
    rows = frame.rows
    report: dict = {
        "width": frame.width,
        "height": frame.height,
        "mean_rgb": [
            round(sum(p[0] for row in rows for p in row) / (frame.width * frame.height), 1),
            round(sum(p[1] for row in rows for p in row) / (frame.width * frame.height), 1),
            round(sum(p[2] for row in rows for p in row) / (frame.width * frame.height), 1),
        ],
    }
    green = glow.green_centroid(rows)
    report["green"] = (
        None
        if green is None
        else {"centroid": (round(green[0], 4), round(green[1], 4))}
    )
    report["green_fraction"] = round(glow.green_fraction(rows), 5)
    if baseline is not None:
        if (baseline.width, baseline.height) != (frame.width, frame.height):
            raise SystemExit(
                f"baseline is {baseline.width}x{baseline.height}, frame is "
                f"{frame.width}x{frame.height}; capture both at the same --step"
            )
        d = framediff.delta(baseline.rows, rows)
        report["delta"] = {
            "mean": round(d.mean, 3),
            "changed": d.changed,
            "fraction": round(d.fraction, 5),
            "centroid": None
            if d.centroid is None
            else (round(d.centroid[0], 4), round(d.centroid[1], 4)),
            "bbox": d.bbox,
        }
    return report


def responding(report: dict) -> str:
    """Verdict for a before/after pair, from the measured numbers."""
    d = report.get("delta")
    if d is None:
        raise SystemExit("this verdict needs a baseline (--baseline)")
    if d["fraction"] >= BIG_MOVE_FRACTION:
        return "whole-view change: a failed settle or a scene cut, not a head turn"
    if d["fraction"] >= RESPONDING_FRACTION and d["mean"] >= RESPONDING_MEAN:
        return "responding: the view follows the body"
    return "not responding: stale capture, or the game is not the window on top"


def cmd_shot(args) -> int:
    frame = _load(args.image, args.step)
    baseline = _load(args.baseline, args.step) if args.baseline else None
    report = analyse(frame, baseline)
    print(json.dumps(report, indent=2))
    if baseline is not None:
        print(f"verdict: {responding(report)}")
    return 0


def _capture(args, name: str) -> str:
    import frames

    destination = Path(args.work_dir) / name
    destination.parent.mkdir(parents=True, exist_ok=True)
    return str(
        frames.capture(
            screenshooter=args.screenshooter,
            runtime_dir=args.runtime_dir,
            wayland_display=args.wayland_display,
            work_dir=args.work_dir,
            destination=destination,
        )
    )


def _set_yaw(args, yaw_deg: float, seq: int) -> None:
    """Hold one head yaw with a raised hand, past the verb TTL."""
    import hold_pose
    from sonic_executor import SonicExecutor

    executor = SonicExecutor()
    cmd = hold_pose.build_command(
        executor, yaw_deg, args.side, args.forward_m, args.up_m, 0.0, seq
    )
    reply = hold_pose.send_command(Path(args.control), cmd)
    if not reply.get("ok", False):
        raise SystemExit(f"control rejected the pose: {reply}")


def cmd_motion(args) -> int:
    """Turn the head and ask whether the picture followed."""
    _set_yaw(args, args.yaw_a, 1)
    a = _capture(args, "motion_a.png")
    _set_yaw(args, args.yaw_b, 2)
    b = _capture(args, "motion_b.png")
    report = analyse(_load(b, args.step), _load(a, args.step))
    report["frames"] = [a, b]
    print(json.dumps(report, indent=2))
    print(f"verdict: {responding(report)}")
    return 0


def cmd_sweep(args) -> int:
    """Walk the head through a yaw range and report where the green actually is."""
    rows = []
    count = int(round((args.sweep_to - args.sweep_from) / args.sweep_step)) + 1
    for seq, yaw in enumerate(
        (args.sweep_from + i * args.sweep_step for i in range(count)), start=1
    ):
        _set_yaw(args, yaw, seq)
        path = _capture(args, f"sweep_{yaw:+.0f}.png")
        report = analyse(_load(path, args.step))
        green = report["green"]["centroid"] if report["green"] else None
        rows.append(
            {
                "yaw": yaw,
                "green_fraction": report["green_fraction"],
                "centroid": green,
                "off_centre": None
                if green is None
                else round(math.hypot(green[0] - 0.5, green[1] - 0.5), 4),
            }
        )
        print(json.dumps(rows[-1]))
    best = min(
        (r for r in rows if r["centroid"] is not None),
        key=lambda r: r["off_centre"],
        default=None,
    )
    if best is not None:
        print(f"FOUND yaw {best['yaw']:+.0f} deg, off-centre {best['off_centre']:.3f}")
    else:
        print("NOT FOUND: no yaw produced a green region above the gate")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="calibrate_view")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p):
        p.add_argument("--step", type=int, default=2, help="downscale factor (2 -> 960x540)")
        p.add_argument("--baseline", default=None, help="compare against this PNG")

    shot = sub.add_parser("shot", help="measure one frame")
    shot.add_argument("--image", required=True)
    common(shot)
    shot.set_defaults(func=cmd_shot)

    for name, func in (("motion", cmd_motion), ("sweep", cmd_sweep)):
        p = sub.add_parser(name, help="live: capture and measure")
        p.add_argument("--control", required=True, help="body control socket")
        p.add_argument("--screenshooter", required=True)
        p.add_argument("--runtime-dir", required=True)
        p.add_argument("--wayland-display", required=True)
        p.add_argument("--work-dir", required=True)
        p.add_argument("--side", default="right", choices=("left", "right"))
        p.add_argument("--forward-m", type=float, default=0.35)
        p.add_argument("--up-m", type=float, default=0.50)
        common(p)
        if name == "motion":
            p.add_argument("--yaw-a", type=float, default=0.0)
            p.add_argument("--yaw-b", type=float, default=20.0)
        else:
            p.add_argument("--sweep-from", type=float, default=-60.0)
            p.add_argument("--sweep-to", type=float, default=60.0)
            p.add_argument("--sweep-step", type=float, default=15.0)
        p.set_defaults(func=func)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
