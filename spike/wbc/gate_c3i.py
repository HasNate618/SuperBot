#!/usr/bin/env python3
"""C3i — the SONIC gate.

Three tests, in the order the proposal demanded before route A is committed:

  1. five-cycle soak: walk 5 s -> hold 10 s -> walk 5 s, x5, one continuous run
  2. 60 s continuous walk
  3. aim while walking at a fixed world point (head + right wrist)

Everything is measured from `mjData` after `mj_step`.  Commanded VR targets are
stored separately (`vr_t` / `vr_o` in the rows, `runner.diag` for the world-frame
aim signal) and never reported as achievements.

Run:  spike/e3/.venv/bin/python spike/wbc/gate_c3i.py
Out:  spike/wbc/logs/c3i_*.jsonl, spike/wbc/logs/c3i_summary.json
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
LOOP_F0, LOOP_F1 = 786, 942  # from G.find_loop_window
CYCLE = [("walk", 5.0), ("hold", 10.0), ("walk", 5.0)]
N_CYCLE = 5
CYCLE_S = sum(d for _, d in CYCLE)
REF_SPEED = 1.9576 / 3.12  # looped reference: 0.63 m/s


def _tilt_deg(up):
    return np.degrees(np.arccos(np.clip(up, -1.0, 1.0)))


def _path_err(a):
    return np.hypot(a["x"] - a["ref_x"], a["y"] - a["ref_y"])


def _movavg(v, n):
    if len(v) < n:
        return np.asarray(v, dtype=float)
    k = np.ones(n) / n
    return np.convolve(np.asarray(v, dtype=float), k, mode="valid")


def _speed(a, t, win=1.0):
    """Base speed (m/s) over the trailing `win` seconds ending at time t."""
    i1 = int(np.searchsorted(a["t"], t))
    i0 = int(np.searchsorted(a["t"], t - win))
    if i1 - i0 < 2:
        return 0.0
    return float(np.hypot(a["x"][i1 - 1] - a["x"][i0], a["y"][i1 - 1] - a["y"][i0])
                 / (a["t"][i1 - 1] - a["t"][i0]))


# ------------------------------------------------------------------ test 1
def test1_soak(walk):
    total_s = N_CYCLE * CYCLE_S
    loop = G.build_looped_motion(walk, LOOP_F0, LOOP_F1,
                                 int((total_s + 12.0) / S.CONTROL_DT))
    sched, phases, t = [], [], 0.0
    for k in range(N_CYCLE):
        for name, dur in CYCLE:
            phases.append((k, name, t, t + dur))
            if name == "hold":
                # Freeze the reference at the frame the hold starts, so resuming
                # jumps forward by the hold duration instead of teleporting the
                # reference pose.  The hold is therefore "stop where you are".
                sched.append((t, t + dur, int(round(t / S.CONTROL_DT))))
            t += dur
    runner = G.HookRunner(1)
    rows = runner.run(loop, seconds=total_s, ramp=0.5, freeze_schedule=sched,
                      log_path=os.path.join(LOGS, "c3i_soak.jsonl"))
    a = G.series(rows)
    n_expect = int(round(total_s / S.CONTROL_DT))
    pe = _path_err(a)
    out: dict = {"n_ticks": len(rows), "n_expected": n_expect,
                 "survived": len(rows) >= n_expect,
                 "fall_t": None if len(rows) >= n_expect else float(a["t"][-1]),
                 "hold_reference": "reference frame at hold start (freeze-in-place)",
                 "freeze_frames": [s[2] for s in sched],
                 "loop": loop["loop"], "cycles": []}
    for k in range(N_CYCLE):
        seg = [p for p in phases if p[0] == k]
        c: dict = {"cycle": k}
        have = {name: (a["t"] >= t0) & (a["t"] < t1) for _, name, t0, t1 in seg}
        if not all(m.any() for m in have.values()):
            c["survived"] = False
            c["segments_present"] = {n: bool(m.any()) for n, m in have.items()}
            out["cycles"].append(c)
            continue
        m_hold, m_res = have["hold"], have[seg[-1][1]]
        m_pre = (a["t"] >= seg[0][3] - 1.0) & (a["t"] < seg[0][3])
        i0 = int(np.argmax(m_hold))
        p0 = np.array([a["x"][i0], a["y"][i0]])
        d = np.hypot(a["x"][m_hold] - p0[0], a["y"][m_hold] - p0[1])
        c["hold_net"] = float(d[-1])
        c["hold_max_exc"] = float(d.max())
        c["hold_tilt_max_deg"] = float(_tilt_deg(a["up_z"][m_hold]).max())
        c["hold_min_z"] = float(a["z"][m_hold].min())
        c["path_err_pre"] = float(pe[m_pre].mean())
        c["q_err_pre"] = float(a["q_err"][m_pre].mean())
        pe_res = pe[m_res]
        t_res = a["t"][m_res]
        qe_res = a["q_err"][m_res]
        c["path_err_resume_start"] = float(pe_res[0])
        c["path_err_resume_end"] = float(pe_res[-1])
        # The frozen reference stops in space while the body keeps its drift, so
        # the absolute cartesian error cannot "return".  What re-locking means:
        # the error stops growing, the body walks at reference speed again, and
        # joint-space tracking returns to its pre-hold level.
        c["path_err_growth_resume"] = float(pe_res[-1] - pe_res[0])
        relock_q = None
        for i in range(len(qe_res)):
            if np.all(qe_res[i:] <= 1.2 * c["q_err_pre"]):
                relock_q = float(max(0.0, t_res[i] - t_res[0]))
                break
        c["relock_qerr_s"] = relock_q
        c["q_err_resume_mean"] = float(qe_res.mean())
        sp = np.array([_speed(a, tt) for tt in t_res])
        c["resume_speed_mean"] = float(sp.mean())
        relock_sp = None
        for i in range(len(sp)):
            if np.all(sp[i:] >= 0.75 * REF_SPEED):
                relock_sp = float(max(0.0, t_res[i] - t_res[0] - 1.0))
                break
        c["relock_speed_s"] = relock_sp
        c["resume_dist"] = float(np.hypot(a["x"][m_res][-1] - a["x"][m_res][0],
                                          a["y"][m_res][-1] - a["y"][m_res][0]))
        c["resume_ref_dist"] = float(np.hypot(a["ref_x"][m_res][-1] - a["ref_x"][m_res][0],
                                              a["ref_y"][m_res][-1] - a["ref_y"][m_res][0]))
        m_cyc = (a["t"] >= k * CYCLE_S) & (a["t"] < (k + 1) * CYCLE_S)
        c["tilt_max_deg"] = float(_tilt_deg(a["up_z"][m_cyc]).max())
        c["min_z"] = float(a["z"][m_cyc].min())
        c["survived"] = True
        out["cycles"].append(c)
    out["tilt_max_deg_all"] = float(_tilt_deg(a["up_z"]).max())
    out["min_z_all"] = float(a["z"].min())
    out["cum_hold_net"] = float(sum(c.get("hold_net", 0.0) for c in out["cycles"]))
    out["path_err_end"] = float(pe[-1])
    return out, a


# ------------------------------------------------------------------ test 2
def test2_walk60(walk):
    seconds = 60.0
    loop = G.build_looped_motion(walk, LOOP_F0, LOOP_F1,
                                 int((seconds + 6.0) / S.CONTROL_DT))
    runner = G.HookRunner(1)
    rows = runner.run(loop, seconds=seconds, ramp=0.5,
                      log_path=os.path.join(LOGS, "c3i_walk60.jsonl"))
    a = G.series(rows)
    n_expect = int(round(seconds / S.CONTROL_DT))
    path_len = float(np.sum(np.hypot(np.diff(a["x"]), np.diff(a["y"]))))
    net = np.array([a["x"][-1] - a["x"][0], a["y"][-1] - a["y"][0]])
    th = np.array(runner.base_t)
    # 2 s moving average: the gait swings the pelvis yaw ~20 deg peak-to-peak, so
    # an instantaneous yaw is not a heading.
    yaw_s = _movavg(np.unwrap(np.radians(np.array(runner.base_yaw))), 100)
    th_s = _movavg(th, 100)
    i2 = int(np.searchsorted(th, 2.0))
    dirv = np.unwrap(np.arctan2(a["y"][i2:] - a["y"][:-i2], a["x"][i2:] - a["x"][:-i2])) if i2 else np.zeros(1)
    pe = _path_err(a)
    return {
        "n_ticks": len(rows), "n_expected": n_expect,
        "survived": len(rows) >= n_expect,
        "fall_t": None if len(rows) >= n_expect else float(a["t"][-1]),
        "duration": float(a["t"][-1]),
        "path_len": path_len,
        "net_disp": float(np.linalg.norm(net)),
        "mean_speed_path": float(path_len / a["t"][-1]),
        "mean_speed_net": float(np.linalg.norm(net) / a["t"][-1]),
        "ref_path_len": float(np.sum(np.hypot(np.diff(a["ref_x"]), np.diff(a["ref_y"])))),
        "base_yaw_drift_deg": float(np.degrees(yaw_s[-1] - yaw_s[0])),
        "base_yaw_step_10s_deg": [
            float(np.degrees(yaw_s[int(np.searchsorted(th_s, t0 + 10.0)) - 1]
                             - yaw_s[int(np.searchsorted(th_s, t0))]))
            for t0 in np.arange(0.0, th_s[-1] - 10.0, 10.0)],
        "base_yaw_smoothed_ptp_deg": float(np.degrees(yaw_s.max() - yaw_s.min())),
        "path_bearing_drift_deg": float(np.degrees(dirv[-1] - dirv[0])),
        "path_bearing_ptp_deg": float(np.degrees(dirv.max() - dirv.min())),
        "tilt_max_deg": float(_tilt_deg(a["up_z"]).max()),
        "min_z": float(a["z"].min()),
        "path_err_mean": float(pe.mean()), "path_err_max": float(pe.max()),
        "path_err_end": float(pe[-1]),
        "q_err_mean": float(a["q_err"].mean()),
        "loop": loop["loop"],
    }, a


# ------------------------------------------------------------------ test 3
def _aim_metrics(rows, diag, label, n_expect):
    a = G.series(rows)
    sel = [d for d in diag if d["t"] >= 1.0]  # skip the 0.5 s action ramp
    out: dict = {
        "label": label, "n_ticks": len(rows), "ticks_with_aim": len(sel),
        "survived": len(rows) >= n_expect,
        "fall_t": None if len(rows) >= n_expect else float(a["t"][-1]),
        "dist": float(np.hypot(a["x"][-1] - a["x"][0], a["y"][-1] - a["y"][0])),
        "tilt_max_deg": float(_tilt_deg(a["up_z"]).max()),
        "min_z": float(a["z"].min()),
    }
    for key in ("wr_err_pos", "wr_err_orn", "wr_aim", "hd_aim", "hd_err_orn"):
        v = np.array([d[key] for d in sel]) if sel else np.zeros(1)
        out[key + "_mean"] = float(v.mean())
        out[key + "_p95"] = float(np.percentile(v, 95))
        out[key + "_max"] = float(v.max())
    return out


def _make_hook(runner, aim):
    def hook(motion, frame, base_pos, base_quat):
        pts, orns = S.vr_targets(motion, frame)
        p2, o2 = aim.command(motion, frame, base_pos, base_quat, pts, orns)
        runner.aim_cmd = aim.last
        return p2, o2

    return hook


def test3_aim(walk, squat):
    """Aim while walking at a world point 20 m down the initial heading.

    Two bearings: straight ahead (0 deg) and 30 deg to the left, so the test
    covers a target the body points at and one it must hold off-axis.
    """
    base_pos = walk["bp"][600, S.BODY_PELVIS].copy()
    R0 = S.quat_to_mat(walk["bq"][600, S.BODY_PELVIS])
    fwd = R0[:, 0]
    results = []
    for label, bearing_deg, motion, kw in (
            ("walk_aim_0deg", 0.0, walk, {"seconds": 10.0, "start": 600}),
            ("walk_aim_30left", 30.0, walk, {"seconds": 10.0, "start": 600})):
        P = base_pos + G.rot_z(np.radians(bearing_deg)) @ (fwd * 20.0)
        P[2] = base_pos[2] + 0.15
        aim = G.AimTarget(P)
        runner = G.HookRunner(1)
        runner.vr_hook = _make_hook(runner, aim)
        rows = runner.run(motion, ramp=0.5,
                          log_path=os.path.join(LOGS, f"c3i_{label}.jsonl"), **kw)
        m = _aim_metrics(rows, runner.diag, label, 490)
        m["P"] = [float(v) for v in P]
        m["wrist_rel"] = [float(v) for v in aim.wrist_rel]
        results.append(m)
        print(label, json.dumps(m)[:340], flush=True)

    # hold reference: the same aim task with the body frozen (Superhot aims with
    # time stopped).  Same bearing convention as the walking cases.
    sb = squat["bp"][0, S.BODY_PELVIS].copy()
    sfwd = S.quat_to_mat(squat["bq"][0, S.BODY_PELVIS])[:, 0]
    for label, bearing_deg in (("hold_aim_0deg", 0.0), ("hold_aim_30left", 30.0)):
        P = sb + G.rot_z(np.radians(bearing_deg)) @ (sfwd * 20.0)
        P[2] = sb[2] + 0.15
        aim = G.AimTarget(P)
        runner = G.HookRunner(1)
        runner.vr_hook = _make_hook(runner, aim)
        rows = runner.run(squat, seconds=10.0, ramp=0.5, freeze=0,
                          log_path=os.path.join(LOGS, f"c3i_{label}.jsonl"))
        m = _aim_metrics(rows, runner.diag, label, 490)
        m["P"] = [float(v) for v in P]
        results.append(m)
        print(label, json.dumps(m)[:340], flush=True)
    return results


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="123", help="subset of tests, e.g. 2")
    args = ap.parse_args()
    walk = S.load_motion(WALK)
    squat = S.load_motion("squat_001__A359")
    summary: dict = {}

    if "1" in args.only:
        print("== test 1: five-cycle soak", flush=True)
        summary["soak"], _ = test1_soak(walk)
        for c in summary["soak"]["cycles"]:
            print(json.dumps(c)[:280], flush=True)
        print("survived", summary["soak"]["survived"], flush=True)

    if "2" in args.only:
        print("== test 2: 60 s walk", flush=True)
        summary["walk60"], _ = test2_walk60(walk)
        print(json.dumps({k: v for k, v in summary["walk60"].items() if k != "loop"})[:700],
              flush=True)

    if "3" in args.only:
        print("== test 3: aim while walking", flush=True)
        summary["aim"] = test3_aim(walk, squat)

    path = os.path.join(LOGS, "c3i_summary.json")
    if os.path.exists(path) and args.only != "123":
        with open(path) as f:
            old = json.load(f)
        old.update(summary)
        summary = old
    with open(path, "w") as f:
        json.dump(summary, f, indent=1)
    print("wrote", os.path.join(LOGS, "c3i_summary.json"))


if __name__ == "__main__":
    main()
