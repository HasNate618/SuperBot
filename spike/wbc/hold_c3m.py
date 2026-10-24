#!/usr/bin/env python3
"""C3m — is `hold` a usable freeze if it is a switch to a standing reference?

C3i found that the 3.6 cm / 20 s hold (squat frozen at frame 0) and the
0.23-2.07 m / 10 s holds (walk frozen mid-stride) are the same controller
measured on different reference motions.  The design consequence was: implement
`hold` as a reference-motion *switch* to a standing clip, not a freeze of the
current clip.  This script tests whether that actually freezes.

Two hold mechanisms are measured separately, because they are not the same
thing and the difference turns out to matter:

  native freeze — the reference *index* is held at frame F while the encoder's
      10-frame lookahead still reads frames F, F+5, ..., F+45 of the clip.
      This is what `SonicRunner.run(freeze=F)` / `freeze_schedule` does, i.e.
      what the C3i soak did.  Freezing a walk mid-stride therefore still shows
      the policy 0.9 s of walking ahead of the frozen pose.

  flat hold — the whole lookahead is one standing pose.  This is what "switch
      to a standing reference clip" gives you when the standing clip is a
      single pose (or when the clip is short and the deploy player clamps).

Experiment 1 runs H1..H5 over 20 s holds plus the mechanism variants.
Experiment 2 runs the cycle you would ship, 5 times.
Experiment 3 reads base tilt and z for 1 s either side of every switch.

All poses are read from `mjData` after `mj_step`.  Commanded VR targets stay in
the row's `vr_t`/`vr_o` and are never reported as achievements.

Run:  spike/e3/.venv/bin/python spike/wbc/hold_c3m.py
Out:  spike/wbc/logs/c3m_*.jsonl, spike/wbc/logs/c3m_summary.json
"""

import json
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import gate_common as G  # noqa: E402
import sonic_runner as S  # noqa: E402

LOGS = os.path.join(HERE, "logs")
os.makedirs(LOGS, exist_ok=True)

WALK = "walk_forward_amateur_001__A001"
SQUAT = "squat_001__A359"
LOOP_F0, LOOP_F1 = 786, 942  # the walking window C3i used; period 156 frames
DT = S.CONTROL_DT
KEYS = ("jp", "jv", "bp", "bq", "bl", "ba")

# Reference frames the hold conditions use.  Why these:
#   looped index 250 is what C3i-1 cycle 0 froze: source frame 880, |jv| =
#     6.57 rad/s, mid-stride.  The known-bad baseline.
#   walk source frame 0 is the walking clip's own first frame: |jv| = 0.137,
#     root stationary, but the wrists sit at z = +0.35 m (arms-out capture pose).
#   squat source frame 0 is the frame the 3.6 cm hold was measured on.
#   walk source frame 400 is inside the walking clip's 10 s stationary prelude
#     (frames 0-509, root moves 0.5 cm) with the arms down: |jv| = 0.037, the
#     quietest standing frame in any shipped clip.
WALK_MIDSTRIDE = 250  # looped index; == walk source frame 880
WALK_F0 = 0
SQUAT_F0 = 0
WALK_STAND = 400

WARM_S = 5.0
HOLD_S = 20.0
REF_SPEED = 1.9576 / 3.12  # looped reference speed, m/s
HORIZON = 60  # build_encoder_obs reads 10 frames at step 5 -> frame + 45


# ------------------------------------------------------------ reference build
def looped(walk, seconds):
    return G.build_looped_motion(walk, LOOP_F0, LOOP_F1, int(seconds / DT) + HORIZON)


def pose_arrays(motion, frame, at_xy, yaw_deg=None):
    """One reference frame, translated onto `at_xy` and optionally re-yawed.

    Translation is invisible to the policy — every encoder position is
    root-relative — but it keeps `ref_x`/`ref_y` continuous, so the path error
    during a hold means "the body left the frozen reference point".  `yaw_deg`
    rigidly re-yaws the pose so a standing clip faces the way the walk went.
    """
    p = {k: motion[k][frame].copy() for k in KEYS}
    if yaw_deg is not None:
        Rz = G.rot_z(np.radians(yaw_deg))
        pelvis = p["bp"][S.BODY_PELVIS].copy()
        p["bp"] = (p["bp"] - pelvis) @ Rz.T + pelvis
        qy = G.quat_z(np.radians(yaw_deg))
        p["bq"] = np.array([S.quat_mul(qy, q) for q in p["bq"]])
        p["bl"] = p["bl"] @ Rz.T
        p["ba"] = p["ba"] @ Rz.T
    p["bp"] = p["bp"] + np.array([at_xy[0] - p["bp"][S.BODY_PELVIS, 0],
                                  at_xy[1] - p["bp"][S.BODY_PELVIS, 1], 0.0])
    return p


def place_region(reg, yaw_deg, at_xy):
    """`pose_arrays`' rigid placement applied to a whole (T, ...) region."""
    out = {k: np.array(v, dtype=np.float64, copy=True) for k, v in reg.items()}
    if yaw_deg is not None:
        Rz = G.rot_z(np.radians(yaw_deg))
        qy = G.quat_z(np.radians(yaw_deg))
        pelvis = out["bp"][0, S.BODY_PELVIS].copy()
        out["bp"] = (out["bp"] - pelvis) @ Rz.T + pelvis
        out["bq"] = np.array([S.quat_mul(qy, q) for q in out["bq"]])
        out["bl"] = out["bl"] @ Rz.T
        out["ba"] = out["ba"] @ Rz.T
    out["bp"] = out["bp"] + np.array([at_xy[0] - out["bp"][0, S.BODY_PELVIS, 0],
                                      at_xy[1] - out["bp"][0, S.BODY_PELVIS, 1], 0.0])
    return out


def build(walk, warm_s, pose, hold_s, resume_s=0.0, played=None):
    """Reference array: walk, then the hold region, then walk again.

    `pose` is the flat hold pose.  `played` = (motion, frame) makes the hold
    region keep playing that clip from `frame` instead of staying flat; the
    caller then holds the *index* with a freeze schedule, which is the native
    freeze.  On resume the walk reference continues from the frame it was at
    when the hold began, so the reference path jumps forward by the hold
    duration — the same convention the C3i soak used.
    """
    W = int(round(warm_s / DT))
    H = int(round(hold_s / DT))
    total = int(round((warm_s + hold_s + resume_s) / DT)) + HORIZON
    idx = np.array([i if i < W else (i - H if i >= W + H else 0) for i in range(total)])
    idx = np.minimum(idx, walk["n"] - 1)
    out = {k: walk[k][idx].copy() for k in KEYS}
    if played is None:
        for k in KEYS:
            out[k][W:W + H] = pose[k]
    else:
        src, f0 = played
        sidx = np.minimum(f0 + np.arange(H), src["n"] - 1)
        for k in KEYS:
            out[k][W:W + H] = src[k][sidx]
    out["n"] = total
    return out


# ------------------------------------------------------------------- metrics
def _ticks(a, t0, t1):
    return (a["t"] >= t0) & (a["t"] < t1)


def window_metrics(a, t0, t1):
    """Net, excursion, speeds, tilt, z for one window, all from logged mjData."""
    m = _ticks(a, t0, t1)
    t, x, y, z = a["t"][m], a["x"][m], a["y"][m], a["z"][m]
    up = a["up_z"][m]
    d = np.hypot(x - x[0], y - y[0])
    step = np.hypot(np.diff(x), np.diff(y))
    v = step / np.diff(t)
    dur = float(t[-1] - t[0])
    v10 = v[(t >= (t[-1] - 10.0))[1:]]
    return {
        "t0": float(t[0]), "t1": float(t[-1]), "n": int(m.sum()),
        "net": float(d[-1]), "max_exc": float(d.max()),
        "path": float(step.sum()), "mean_speed": float(step.sum() / dur),
        "rms_speed_all": float(np.sqrt(np.mean(v ** 2))),
        "rms_speed_last10": float(np.sqrt(np.mean(v10 ** 2))),
        "mean_speed_last10": float(np.mean(v10)),
        "p95_speed_last10": float(np.percentile(v10, 95)),
        "tilt_max_deg": float(np.degrees(np.arccos(np.clip(up.min(), -1.0, 1.0)))),
        "tilt_end_deg": float(np.degrees(np.arccos(np.clip(up[-1], -1.0, 1.0)))),
        "min_z": float(z.min()), "max_z": float(z.max()),
    }


def path_err(a):
    return np.hypot(a["x"] - a["ref_x"], a["y"] - a["ref_y"])


def q_err_mean(a, t0, t1):
    return float(a["q_err"][_ticks(a, t0, t1)].mean())


def _speed_at(a, t, win=1.0):
    i1 = int(np.searchsorted(a["t"], t))
    i0 = int(np.searchsorted(a["t"], t - win))
    if i1 - i0 < 2:
        return 0.0
    return float(np.hypot(a["x"][i1 - 1] - a["x"][i0], a["y"][i1 - 1] - a["y"][i0])
                 / (a["t"][i1 - 1] - a["t"][i0]))


# ------------------------------------------------------------- experiment 1
def experiment1(walk, squat):
    loop = looped(walk, WARM_S + HOLD_S + 10.0)
    W = int(round(WARM_S / DT))
    at = loop["bp"][W - 1, S.BODY_PELVIS, :2].copy()
    walk_heading = G.yaw_of(loop["bq"][W - 1, S.BODY_PELVIS])
    stand_yaw = float(np.degrees(
        walk_heading - G.yaw_of(walk["bq"][WALK_STAND, S.BODY_PELVIS])))

    mid = pose_arrays(loop, WALK_MIDSTRIDE, at)
    wf0 = pose_arrays(walk, WALK_F0, at)
    sq0 = pose_arrays(squat, SQUAT_F0, at)
    stand = pose_arrays(walk, WALK_STAND, at, yaw_deg=stand_yaw)
    end = WARM_S + HOLD_S

    def native(motion, frame):
        """Hold the reference index at `frame` for the whole hold window."""
        return [(WARM_S, end, frame)]

    rows_spec = [
        # H1: what C3i measured.  Native freeze, so the lookahead still walks.
        ("H1", "freeze walk mid-stride (native freeze)",
         "reference index held at looped frame 250 == walk source frame 880, |jv|=6.57. Lookahead still shows the next 0.9 s of walking.",
         loop, native(loop, WALK_MIDSTRIDE), "c3m_H1_walk_midstride_native.jsonl"),
        # H1f: same pose, whole lookahead flat -> isolates pose from lookahead.
        ("H1f", "freeze walk mid-stride (flat horizon)",
         "same frozen pose as H1 but the 10-frame lookahead is the same pose repeated, i.e. a hard freeze.",
         build(loop, WARM_S, mid, HOLD_S), None, "c3m_H1f_walk_midstride_flat.jsonl"),
        # H2: the walking clip's own frame 0.  Its lookahead (frames 0-45) is
        # inside the clip's stationary prelude, so native == flat here.
        ("H2", "freeze walk frame 0 (native freeze)",
         "walk source frame 0: |jv|=0.137, root stationary, wrists at z=+0.35 m. Lookahead frames 0-45 are all in the same standing prelude, so this is also the flat hold.",
         build(loop, WARM_S, wf0, HOLD_S, played=(walk, WALK_F0)), native(loop, W), "c3m_H2_walk_f0.jsonl"),
        # H3: the 3.6 cm protocol, unchanged: start on squat, freeze frame 0.
        ("H3", "freeze squat frame 0 from a squat start",
         "the 3.6 cm / 20 s protocol re-run unchanged: reset on squat frame 0, freeze. No walk before the hold.",
         squat, [(0.0, HOLD_S, SQUAT_F0)], "c3m_H3_squat_f0_from_squat.jsonl"),
        # H4: the proposal.  Switch to squat frame 0 and hold it flat.
        ("H4", "switch to squat frame 0 (flat hold)",
         "walk 5 s, then switch the reference to squat_001__A359 frame 0 held flat for 20 s. This is the proposal.",
         build(loop, WARM_S, sq0, HOLD_S), None, "c3m_H4_switch_squat_f0_flat.jsonl"),
        # H4n: the same switch, but the deploy player keeps the index at the
        # clip's frame 0, so the lookahead shows the squat starting to descend.
        ("H4n", "switch to squat frame 0 (native freeze on switch)",
         "walk 5 s, switch to squat frame 0, index held there: lookahead is squat frames 0-45, the start of a squat descent.",
         build(loop, WARM_S, sq0, HOLD_S, played=(squat, SQUAT_F0)), native(loop, W), "c3m_H4n_switch_squat_f0_native.jsonl"),
        # H5: the quietest standing frame available in any shipped clip.
        ("H5", "switch to walk prelude frame 400 (flat hold)",
         "walk source frame 400: |jv|=0.037, root stationary, arms down, yaw-aligned to the walk heading. The quietest standing frame in any shipped clip.",
         build(loop, WARM_S, stand, HOLD_S), None, "c3m_H5_switch_walk_prelude_flat.jsonl"),
        ("H5n", "switch to walk prelude frame 400 (native freeze)",
         "same switch, index held at frame 400: lookahead is walk frames 400-445, all inside the stationary prelude.",
         build(loop, WARM_S, stand, HOLD_S, played=(walk, WALK_STAND)), native(loop, W), "c3m_H5n_switch_walk_prelude_native.jsonl"),
    ]

    out: list[dict] = []
    for cid, label, note, motion, sched, log in rows_spec:
        runner = G.HookRunner(1)
        seconds = HOLD_S if cid == "H3" else WARM_S + HOLD_S
        rows = runner.run(motion, seconds=seconds, ramp=0.5, freeze_schedule=sched,
                          log_path=os.path.join(LOGS, log))
        n_expect = int(round(seconds / DT))
        a = G.series(rows)
        h0 = 0.0 if cid == "H3" else WARM_S
        res = {
            "id": cid, "label": label, "note": note,
            "n_ticks": len(rows), "n_expected": n_expect,
            "survived": len(rows) >= n_expect,
            "fall_t": None if len(rows) >= n_expect else float(a["t"][-1]),
            "hold": window_metrics(a, h0, h0 + HOLD_S),
            "log": log,
        }
        res["warm"] = window_metrics(a, 0.0, WARM_S) if cid != "H3" else None
        out.append(res)
        h = res["hold"]
        print(f"{cid:4s} net={h['net']:.3f} exc={h['max_exc']:.3f} "
              f"rms10={h['rms_speed_last10']:.4f} mean_v={h['mean_speed']:.4f} "
              f"tilt={h['tilt_max_deg']:.1f} minz={h['min_z']:.3f} "
              f"surv={res['survived']}  {label}", flush=True)
    return loop, out


# ------------------------------------------------------------- experiment 2
def experiment2(walk, hold_motion, hold_frame, yaw_deg, label, note, log,
                n_cycle=5, warm=5.0, hold_s=10.0, resume=5.0, native=False):
    """walk -> switch to the standing reference and hold -> walk, x5.

    `native` builds the shippable form: the reference index is pinned at the
    standing clip's frame while the encoder's 10-frame lookahead still reads
    that clip's next 0.9 s (Experiment 1's H*n rows, the ones that go dead
    still).  Otherwise the hold region is the single pose repeated, which is
    the flat form C3m first tested.
    """
    cycle_s = warm + hold_s + resume
    total = cycle_s * n_cycle
    loop = looped(walk, total + 10.0)
    W = int(round(warm / DT))
    Hh = int(round(hold_s / DT))
    n = int(round(total / DT)) + HORIZON
    at = loop["bp"][min(W, loop["n"] - 1) - 1, S.BODY_PELVIS, :2].copy()
    base_pose = pose_arrays(hold_motion, hold_frame, at, yaw_deg=yaw_deg)

    # Walk windows are consecutive loop frames; the hold windows are the
    # standing reference translated onto the position the walk reached.
    C = int(round(cycle_s / DT))
    idx = np.array([i - Hh * ((i // C) + (1 if (i % C) >= W + Hh else 0))
                    if (i % C) >= W + Hh else (i if (i % C) < W else 0)
                    for i in range(n)])
    idx = np.maximum(np.minimum(idx, loop["n"] - 1), 0)
    motion = {k: loop[k][idx].copy() for k in KEYS}
    sched: list[tuple] = []
    for k in range(n_cycle):
        base = k * C
        i0, i1 = base + W, base + W + Hh
        tgt = loop["bp"][min(i0 - 1, loop["n"] - 1), S.BODY_PELVIS, :2]
        if native:
            sidx = np.minimum(hold_frame + np.arange(Hh), hold_motion["n"] - 1)
            reg = place_region({kk: hold_motion[kk][sidx] for kk in KEYS}, yaw_deg, tgt)
            sched.append((base * DT + warm, base * DT + warm + hold_s, i0))
        else:
            hk = base_pose
            hk = {kk: vv + np.array([tgt[0] - vv[S.BODY_PELVIS, 0],
                                     tgt[1] - vv[S.BODY_PELVIS, 1], 0.0]) if kk == "bp" else vv
                  for kk, vv in hk.items()}
            reg = {kk: np.repeat(hk[kk][None], Hh, axis=0) for kk in KEYS}
        for kk in KEYS:
            motion[kk][i0:i1] = reg[kk]
    motion["n"] = n

    # The flat hold repeats its pose, so the index can advance freely; the
    # native hold pins the index, otherwise its lookahead would run into the
    # walking part of the clip.
    runner = G.HookRunner(1)
    rows = runner.run(motion, seconds=total, ramp=0.5,
                      freeze_schedule=sched or None,
                      log_path=os.path.join(LOGS, log))
    n_expect = int(round(total / DT))
    a = G.series(rows)
    pe = path_err(a)
    res = {"label": label, "note": note, "n_ticks": len(rows),
           "n_expected": n_expect, "survived": len(rows) >= n_expect, "native": native,
           "fall_t": None if len(rows) >= n_expect else float(a["t"][-1]),
           "duration": float(a["t"][-1]), "cycles": [], "switches": [], "log": log}
    for k in range(n_cycle):
        t_w0 = k * cycle_s
        t_h0, t_h1 = t_w0 + warm, t_w0 + warm + hold_s
        t_r0, t_r1 = t_h1, t_h1 + resume
        h = window_metrics(a, t_h0, t_h1)
        w = window_metrics(a, t_w0, t_h0)
        r = window_metrics(a, t_r0, t_r1)
        m_res = _ticks(a, t_r0, t_r1)
        qe_pre = q_err_mean(a, t_h0 - 1.0, t_h0)
        qe_res, t_res = a["q_err"][m_res], a["t"][m_res]
        relock = None
        for i in range(len(qe_res)):
            if np.all(qe_res[i:] <= 1.2 * qe_pre):
                relock = float(max(0.0, t_res[i] - t_res[0]))
                break
        sp = np.array([_speed_at(a, tt) for tt in t_res])
        relock_sp = None
        for i in range(len(sp)):
            if np.all(sp[i:] >= 0.75 * REF_SPEED):
                relock_sp = float(max(0.0, t_res[i] - t_res[0] - 1.0))
                break
        res["cycles"].append({
            "cycle": k,
            "walk_dist": w["path"], "walk_mean_speed": w["mean_speed"],
            "hold_net": h["net"], "hold_max_exc": h["max_exc"],
            "hold_rms_speed_last10": h["rms_speed_last10"],
            "hold_mean_speed": h["mean_speed"],
            "hold_tilt_max_deg": h["tilt_max_deg"], "hold_min_z": h["min_z"],
            "resume_dist": r["path"],
            "resume_ref_dist": float(np.hypot(
                a["ref_x"][m_res][-1] - a["ref_x"][m_res][0],
                a["ref_y"][m_res][-1] - a["ref_y"][m_res][0])),
            "resume_mean_speed": r["mean_speed"],
            "q_err_pre": qe_pre, "relock_qerr_s": relock,
            "relock_speed_s": relock_sp,
            "path_err_resume_start": float(pe[m_res][0]),
            "path_err_resume_end": float(pe[m_res][-1]),
            "cycle_tilt_max_deg": float(max(h["tilt_max_deg"], r["tilt_max_deg"])),
            "cycle_min_z": float(min(h["min_z"], r["min_z"])),
        })
        for edge, t_e in (("to_hold", t_h0), ("to_walk", t_h1)):
            pre = window_metrics(a, t_e - 1.0, t_e)
            post = window_metrics(a, t_e, t_e + 1.0)
            ipost = _ticks(a, t_e, t_e + 1.0)
            ipre = _ticks(a, t_e - 1.0, t_e)
            res["switches"].append({
                "cycle": k, "edge": edge, "t": float(t_e),
                "tilt_pre_max": pre["tilt_max_deg"],
                "tilt_post_max": post["tilt_max_deg"],
                "z_pre_min": pre["min_z"], "z_post_min": post["min_z"],
                "z_post_range": post["max_z"] - post["min_z"],
                "dz_end_to_end": float(a["z"][ipost][-1] - a["z"][ipre][-1]),
                "speed_post_rms": post["rms_speed_all"],
            })
    res["cum_hold_net"] = float(sum(c["hold_net"] for c in res["cycles"]))
    res["hold_rms_worst"] = float(max(c["hold_rms_speed_last10"] for c in res["cycles"]))
    res["hold_rms_mean"] = float(np.mean([c["hold_rms_speed_last10"] for c in res["cycles"]]))
    res["hold_net_worst"] = float(max(c["hold_net"] for c in res["cycles"]))
    res["tilt_max_deg_all"] = float(np.degrees(np.arccos(np.clip(a["up_z"].min(), -1, 1))))
    res["min_z_all"] = float(a["z"].min())
    res["walk_dist_total"] = float(sum(c["walk_dist"] for c in res["cycles"]))
    res["switch_tilt_max"] = float(max(max(s["tilt_pre_max"], s["tilt_post_max"])
                                       for s in res["switches"]))
    res["switch_z_min"] = float(min(min(s["z_pre_min"], s["z_post_min"])
                                    for s in res["switches"]))
    for c in res["cycles"]:
        print(f"  cyc{c['cycle']} walk={c['walk_dist']:.2f}m hold net={c['hold_net']:.3f} "
              f"exc={c['hold_max_exc']:.3f} rms10={c['hold_rms_speed_last10']:.4f} "
              f"tilt={c['hold_tilt_max_deg']:.1f} resume={c['resume_dist']:.2f}/"
              f"{c['resume_ref_dist']:.2f} relock={c['relock_qerr_s']}", flush=True)
    print(f"  {label}: survived={res['survived']} cum_net={res['cum_hold_net']:.3f} "
          f"rms_worst={res['hold_rms_worst']:.4f} rms_mean={res['hold_rms_mean']:.4f} "
          f"switch_tilt={res['switch_tilt_max']:.1f}", flush=True)
    return res


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="all", choices=["all", "e1", "e2", "e2native"],
                    help="e2native reruns only the 5-cycle switch in the shippable "
                         "pinned-index form, reusing the Experiment 1 results")
    args = ap.parse_args()
    walk = S.load_motion(WALK)
    squat = S.load_motion(SQUAT)
    path = os.path.join(LOGS, "c3m_summary.json")
    summary: dict = {}
    if args.only != "all" and os.path.exists(path):
        with open(path) as f:
            summary = json.load(f)

    if args.only in ("all", "e1"):
        print("== experiment 1: which hold is actually still", flush=True)
        _, e1 = experiment1(walk, squat)
        summary["experiment1"] = e1
    e1 = summary["experiment1"]
    best = min((r for r in e1 if r["id"].startswith("H5")),
               key=lambda r: r["hold"]["rms_speed_last10"])
    worst = max(e1, key=lambda r: r["hold"]["rms_speed_last10"])
    sq = [r for r in e1 if r["id"] == "H4"][0]
    wl = looped(walk, 130.0)
    yaw = float(np.degrees(G.yaw_of(wl["bq"][int(WARM_S / DT) - 1, S.BODY_PELVIS])
                           - G.yaw_of(walk["bq"][WALK_STAND, S.BODY_PELVIS])))

    def five_cycle(tag, native):
        shape = "pinned-index (native freeze)" if native else "flat (single pose repeated)"
        out = []
        out.append(experiment2(
            walk, squat, SQUAT_F0, None,
            f"5-cycle: squat frame 0, {shape}",
            f"walk 5 s -> switch to squat_001__A359 frame 0 ({shape}), hold 10 s "
            "-> walk 5 s, x5",
            f"c3m_E2_cycle_squat_f0{'_native' if native else ''}.jsonl", native=native))
        out[0]["id"] = "E2_squat" + ("_native" if native else "")
        out.append(experiment2(
            walk, walk, WALK_STAND, yaw,
            f"5-cycle: walk prelude frame 400, {shape}",
            "walk 5 s -> switch to walk_forward_amateur_001__A001 frame 400 "
            f"(stationary prelude, arms down, yaw-aligned; {shape}), hold 10 s "
            "-> walk 5 s, x5",
            f"c3m_E2_cycle_walk_prelude{'_native' if native else ''}.jsonl", native=native))
        out[1]["id"] = "E2_best" + ("_native" if native else "")
        return out

    if args.only in ("all", "e2"):
        print(f"== experiment 2: 5-cycle switch, flat form (best H5 pose: {best['id']})", flush=True)
        summary["experiment2"] = five_cycle("flat", native=False)
    if args.only in ("all", "e2native"):
        print("== experiment 2b: 5-cycle switch, pinned-index form", flush=True)
        summary.setdefault("experiment2", [])
        summary["experiment2"] = [r for r in summary["experiment2"]
                                  if not r["id"].endswith("_native")] + five_cycle("native", native=True)

    summary["verdict"] = {
        "best_hold_id": best["id"],
        "best_hold_rms_last10": best["hold"]["rms_speed_last10"],
        "best_hold_net": best["hold"]["net"],
        "best_hold_exc": best["hold"]["max_exc"],
        "worst_hold_id": worst["id"],
        "worst_hold_rms_last10": worst["hold"]["rms_speed_last10"],
        "H4_switch_rms_last10": sq["hold"]["rms_speed_last10"],
        "H4_switch_net": sq["hold"]["net"],
        "H1_freeze_rms_last10": [r for r in e1 if r["id"] == "H1"][0]["hold"]["rms_speed_last10"],
        "H1_freeze_net": [r for r in e1 if r["id"] == "H1"][0]["hold"]["net"],
    }
    with open(path, "w") as f:
        json.dump(summary, f, indent=1)
    print("wrote", path, flush=True)


if __name__ == "__main__":
    main()
