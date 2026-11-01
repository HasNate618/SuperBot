#!/usr/bin/env python3
"""Standing-reach probe: can the measured WBC touch The Fall's pedestal?

The Fall's pistol and exit pyramid sit 0.70 m in front of the player at eye
height.  The WBC's published reach targets are only +0.20 m from the neutral
wrist, so this probe tests the unmeasured 0.70 m case with the same controller,
model and default weights.

Run:
  /home/nate/Projects/Clank/spike/e3/.venv/bin/python reach_far_test.py

Output is one line per (target, side).  "res" is the final wrist position
residual, "margin" the 2D support margin, "fall" the controller's fall flag.
This is a probe, not a full scenario; the targets are world points relative to
the head geom, held for 2 s with a 1.5 s ramp.
"""

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "wbc_classical"))
import wbc_spike as W  # noqa: E402


def main():
    wbc = W.G1WBC()
    head_p = np.array(wbc.head0.translation(), copy=True)
    w0 = {s: np.array(wbc.wrist0[s].translation(), copy=True) for s in ("left", "right")}
    R0 = {
        s: np.array(wbc.wrist0[s].rotation().as_matrix(), copy=True)
        for s in ("left", "right")
    }
    targets = [
        ("gun_eye_0.70", head_p + np.array([0.70, 0.0, 0.06])),
        ("pyramid_eye_0.70", head_p + np.array([0.70, 0.0, -0.04])),
        ("eye_0.80", head_p + np.array([0.80, 0.0, 0.06])),
    ]
    print("head0", head_p)
    print("wrist0 L", w0["left"], "R", w0["right"])
    print("base0", wbc.base0.translation(), "com0", wbc.com0)
    for label, target in targets:
        for side in ("right", "left"):
            other = "left" if side == "right" else "right"
            wbc.reset()
            wbc.set_costs("default")
            records = []
            for k in range(100):
                t = k * W.DT
                a = min(1.0, t / 1.5)
                p = w0[side] + a * (target - w0[side])
                wbc.targets[f"{side}_wrist"] = W.se3(R0[side], p)
                wbc.targets[f"{other}_wrist"] = wbc.wrist0[other].copy()
                wbc.step()
                r = wbc.measure()
                r["t"] = t
                records.append(r)
            agg = W.aggregate(records, wbc)
            final = records[-1]
            print(
                f"{label:18s} {side:5s} "
                f"res={final[side + '_wrist_res'] * 100:.2f}cm "
                f"ori={final[side + '_wrist_ori_err']:.2f}deg "
                f"base_max={agg['base_max_xy_m'] * 100:.2f}cm "
                f"tilt={agg['max_base_tilt_deg']:.2f}deg "
                f"margin={agg['min_support_margin_m'] * 100:.2f}cm "
                f"fall={agg['fall']}"
            )


if __name__ == "__main__":
    main()
