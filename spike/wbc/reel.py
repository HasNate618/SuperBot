#!/usr/bin/env python3
"""Body Reel v1: the stationary Superhot verbs, rendered and measured.

Runs `hold -> look -> aim -> punch -> dodge -> hold` through the real
`BodyCommand` contract, renders third person, and writes:

    <out>/reel.gif      the animation (Pillow; no ffmpeg dependency needed)
    <out>/report.json   per-phase residuals, drift, tilt, fall

Why a report and not just a video: a video proves something moved, not that it
moved where it was asked. Every phase records the achieved-vs-commanded
residual, so a body that ignores the command cannot pass. Residuals are
reported even when ugly - a miss is data, a hidden miss is a lie.

Usage:
    spike/e3/.venv/bin/python spike/wbc/reel.py --out /tmp/reel
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from pathlib import Path

os.environ.setdefault("MUJOCO_GL", "egl")
os.environ.setdefault("PYOPENGL_PLATFORM", "egl")

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

# Must precede mujoco: it re-execs with LD_LIBRARY_PATH so libEGL resolves.
import gl_boot  # pyright: ignore[reportMissingImports]  # noqa: E402,F401
import numpy as np  # pyright: ignore[reportMissingImports]  # noqa: E402
import sonic_runner as sr  # pyright: ignore[reportMissingImports]  # noqa: E402
from body_view import BodyRenderer  # pyright: ignore[reportMissingImports]  # noqa: E402
from sonic_executor import SonicExecutor  # pyright: ignore[reportMissingImports]  # noqa: E402

from superbot.body import (  # noqa: E402
    AUTH_HEAD,
    AUTH_R_HAND,
    FLAG_FALL_RISK,
    BodyCommand,
    hold,
)

CONTROL_DT = 0.02  # 50 Hz, matching the policy
RENDER_EVERY = 5  # -> 10 fps in the output


def tilt_deg(ex: SonicExecutor) -> float:
    """Angle of the body's up-axis away from vertical, in degrees."""
    up = sr.quat_rotate(np.asarray(ex.runner.data.qpos[3:7]), np.array([0.0, 0.0, 1.0]))
    return float(math.degrees(math.acos(max(-1.0, min(1.0, float(up[2]))))))


def phases(ex: SonicExecutor) -> list[tuple[str, float, BodyCommand]]:
    """The Superhot verb set, in a plausible play order."""
    seq = 0

    def cmd(authority: int, **kw) -> BodyCommand:
        nonlocal seq
        seq += 1
        return BodyCommand(seq=seq, authority=authority, source="script", **kw)

    return [
        ("hold", 2.0, hold(seq=0)),
        ("look_left35", 1.5, cmd(AUTH_HEAD, head=ex.look_target(math.radians(35)))),
        ("hold", 1.0, hold(seq=0)),
        ("look_right35", 1.5, cmd(AUTH_HEAD, head=ex.look_target(math.radians(-35)))),
        ("hold", 1.0, hold(seq=0)),
        ("aim_right", 2.0, cmd(AUTH_R_HAND, right_hand=ex.aim_target("right", forward_m=0.22))),
        ("punch_right", 1.5, cmd(AUTH_R_HAND, right_hand=ex.punch_target("right"))),
        ("dodge_left", 1.0, cmd(AUTH_HEAD, head=ex.dodge_target(0.14))),
        ("hold", 2.0, hold(seq=0)),
    ]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="reel")
    ap.add_argument("--out", required=True)
    ap.add_argument("--width", type=int, default=560)
    ap.add_argument("--height", type=int, default=420)
    args = ap.parse_args(argv)

    from PIL import Image  # pyright: ignore[reportMissingImports]

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    ex = SonicExecutor()
    renderer = BodyRenderer(ex.runner.model, width=args.width, height=args.height)

    frames = []
    report: list[dict] = []
    started = time.monotonic()

    for name, seconds, cmd in phases(ex):
        ticks = max(1, int(round(seconds / CONTROL_DT)))
        residuals: dict[str, list[float]] = {"head": [], "left": [], "right": []}
        start_xy = ex.runner.data.qpos[0:2].copy()
        start_wrist: np.ndarray | None = None
        max_tilt = 0.0

        for tick in range(ticks):
            state = ex.step(cmd, now_ns=time.monotonic_ns())
            if cmd.authority & AUTH_HEAD:
                residuals["head"].append(state.residual_head_m)
            if cmd.authority & AUTH_R_HAND:
                residuals["right"].append(state.residual_right_m)
            if start_wrist is None and ex.last_state is not None:
                start_wrist = np.asarray(ex.last_state.right_hand.p).copy()
            max_tilt = max(max_tilt, tilt_deg(ex))
            if tick % RENDER_EVERY == 0:
                frames.append(renderer.frame(ex.runner.data))
            if ex.fell or (state.flags & FLAG_FALL_RISK):
                break

        drift = float(np.linalg.norm(ex.runner.data.qpos[0:2] - start_xy))
        # The contract residual compares a command against an achieved pose, and
        # the exercised clip sits ~0.16 m away from where the model actually
        # settles, so that residual carries a standing bias. Displacement is the
        # bias-free question: did the wrist move by what was asked?
        moved = (
            float(np.linalg.norm(np.asarray(ex.last_state.right_hand.p) - start_wrist))
            if start_wrist is not None and ex.last_state is not None
            else None
        )
        entry = {
            "phase": name,
            "seconds": round(ticks * CONTROL_DT, 2),
            "mean_residual_m": {k: (round(float(np.mean(v)), 4) if v else None) for k, v in residuals.items()},
            "max_residual_m": {k: (round(float(np.max(v)), 4) if v else None) for k, v in residuals.items()},
            "drift_m": round(drift, 4),
            "right_wrist_moved_m": None if moved is None else round(moved, 4),
            "max_tilt_deg": round(max_tilt, 2),
            "fall": bool(ex.fell),
        }
        report.append(entry)
        print(
            f"{name:14s} {entry['seconds']:4.1f}s  "
            f"res head={entry['mean_residual_m']['head']} right={entry['mean_residual_m']['right']}  "
            f"drift={entry['drift_m']:.3f}m moved={entry['right_wrist_moved_m']} "
            f"tilt={entry['max_tilt_deg']:.1f}"
            + ("  FALL" if entry["fall"] else ""),
            flush=True,
        )

    elapsed = time.monotonic() - started
    gif_path = out / "reel.gif"
    if frames:
        imgs = [Image.fromarray(f) for f in frames]
        imgs[0].save(
            gif_path,
            save_all=True,
            append_images=imgs[1:],
            duration=int(1000 * CONTROL_DT * RENDER_EVERY),
            loop=0,
            optimize=True,
        )
    (out / "report.json").write_text(
        json.dumps(
            {
                "clip": ex.clip,
                "ticks": ex.ticks,
                "wall_s": round(elapsed, 1),
                "ms_per_tick": round(1000 * elapsed / max(ex.ticks, 1), 2),
                "fell": ex.fell,
                "phases": report,
            },
            indent=2,
        )
    )

    print(f"\n{len(frames)} frames -> {gif_path}")
    print(f"{ex.ticks} ticks in {elapsed:.1f}s "
          f"({1000 * elapsed / max(ex.ticks, 1):.1f} ms/tick), fell={ex.fell}")
    return 3 if ex.fell else 0


if __name__ == "__main__":
    raise SystemExit(main())
