#!/usr/bin/env python3
import json, os, sys
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sonic_runner as S

HERE = os.path.dirname(os.path.abspath(__file__))
LOGS = os.path.join(HERE, "logs")
os.makedirs(LOGS, exist_ok=True)
I = np.array([1.0, 0, 0, 0])

def qz(a):
    return np.array([np.cos(a / 2), 0, 0, np.sin(a / 2)])

def qy(a):
    return np.array([np.cos(a / 2), 0, np.sin(a / 2), 0])

def qerr(a, b):
    return float(np.degrees(2 * np.arccos(np.clip(abs(np.dot(a, b)), 0, 1))))

def yaw_pitch(q):
    w, x, y, z = q
    yaw = np.degrees(np.arctan2(2 * (w * z + x * y), 1 - 2 * (y * y + z * z)))
    pitch = np.degrees(np.arcsin(np.clip(2 * (w * y - z * x), -1, 1)))
    return float(yaw), float(pitch)

def rows_to_arrays(rows):
    return dict(
        t=np.array([r["t"] for r in rows]),
        x=np.array([r["x"] for r in rows]), y=np.array([r["y"] for r in rows]),
        z=np.array([r["z"] for r in rows]), up=np.array([r["up_z"] for r in rows]),
        lw=np.array([r["lw"] for r in rows]), rw=np.array([r["rw"] for r in rows]),
        hw=np.array([r["hw"] for r in rows]),
        lw_q=np.array([r["lw_q"] for r in rows]), rw_q=np.array([r["rw_q"] for r in rows]),
        torso_q=np.array([r["torso_q"] for r in rows]),
        vr_t=np.array([r["vr_t"] for r in rows]).reshape(-1, 3, 3),
        vr_o=np.array([r["vr_o"] for r in rows]).reshape(-1, 3, 4),
        ref_x=np.array([r["ref_x"] for r in rows]), ref_y=np.array([r["ref_y"] for r in rows]),
    )

def tracking(a, mask):
    pos = np.stack([a["lw"], a["rw"], a["hw"]], axis=1)
    orn = np.stack([a["lw_q"], a["rw_q"], a["torso_q"]], axis=1)
    pe = np.linalg.norm(pos[mask] - a["vr_t"][mask], axis=2)
    oe = np.array([[qerr(orn[i, k], a["vr_o"][i, k]) for k in range(3)] for i in np.where(mask)[0]])
    return pe.mean(axis=0), oe.mean(axis=0)

def hold_metrics(rows):
    a = rows_to_arrays(rows)
    mask = a["t"] >= 1.0
    pe, oe = tracking(a, mask)
    x, y = a["x"], a["y"]
    d = np.hypot(x - x[0], y - y[0])
    vlast = float(np.hypot(x[-1] - x[-250], y[-1] - y[-250]) / 5.0) if len(x) >= 250 else float("nan")
    ty, tp = yaw_pitch(a["torso_q"][-1])
    return dict(n=len(rows), survived=len(rows) >= 1000, disp=float(d[-1]), max_exc=float(d.max()),
                v_last5=vlast, z=float(a["z"][-1]), up=float(a["up"][-1]),
                poserr=[float(v) for v in pe], ornerr=[float(v) for v in oe],
                torso_yaw=ty, torso_pitch=tp,
                lw=[float(v) for v in a["lw"][-1]], rw=[float(v) for v in a["rw"][-1]],
                hw=[float(v) for v in a["hw"][-1]])

def walk_metrics(rows):
    a = rows_to_arrays(rows)
    mask = a["t"] >= 1.0
    pe, oe = tracking(a, mask)
    dx = a["x"][-1] - a["x"][0]
    dy = a["y"][-1] - a["y"][0]
    rx = a["ref_x"][-1] - a["ref_x"][0]
    ry = a["ref_y"][-1] - a["ref_y"][0]
    return dict(n=len(rows), survived=len(rows) >= 500, z=float(a["z"][-1]), up=float(a["up"][-1]),
                disp=[float(dx), float(dy)], ref=[float(rx), float(ry)],
                poserr=float(np.hypot(dx - rx, dy - ry)),
                track_poserr=[float(v) for v in pe], track_ornerr=[float(v) for v in oe],
                minz=float(a["z"].min()), minup=float(a["up"].min()))

def stand_metrics(rows, cond):
    a = rows_to_arrays(rows)
    mask = a["t"] >= 1.0
    pe, oe = tracking(a, mask)
    ty, tp = yaw_pitch(a["torso_q"][-1])
    return dict(cond=cond, n=len(rows), survived=len(rows) >= 300,
                poserr=[float(v) for v in pe], ornerr=[float(v) for v in oe],
                lw=[float(v) for v in a["lw"][-1]], rw=[float(v) for v in a["rw"][-1]],
                hw=[float(v) for v in a["hw"][-1]], torso_yaw=ty, torso_pitch=tp,
                z=float(a["z"][-1]), up=float(a["up"][-1]))

def main():
    squat = S.load_motion("squat_001__A359")
    walk = S.load_motion("walk_forward_amateur_001__A001")
    out = {}
    models = ["default", "v11", "lowlat"]
    for model in models:
        print("==", model, flush=True)
        out[model] = {}
        cases = []
        cases.append(("hold_aim", dict(motion=squat, seconds=20, ramp=0.5, freeze=0,
                                       vr_delta=np.array([0.15, 0, 0.15, 0.15, 0, 0.15, 0, 0, 0]),
                                       vr_orn_delta=[I, I, qz(np.deg2rad(15))]), "hold"))
        cases.append(("walk_base", dict(motion=walk, seconds=10, ramp=0.5, start=600, vr_delta=None), "walk"))
        cases.append(("walk_Lfwd30", dict(motion=walk, seconds=10, ramp=0.5, start=600,
                                          vr_delta=np.array([0.30, 0, 0, 0, 0, 0, 0, 0, 0])), "walk"))
        cases.append(("walk_Rfwd30", dict(motion=walk, seconds=10, ramp=0.5, start=600,
                                          vr_delta=np.array([0, 0, 0, 0.30, 0, 0, 0, 0, 0])), "walk"))
        cases.append(("r4_walk_hold_walk", dict(motion=walk, seconds=12, ramp=0.5, start=600,
                                                freeze_schedule=[(4.0, 8.0, 0)]), "r4"))
        cases.append(("r5_head_left20", dict(motion=squat, seconds=3, ramp=0.5, freeze=0,
                                             vr_delta=np.zeros(9), step_at=1.0,
                                             step_delta=np.array([0, 0, 0, 0, 0, 0, 0, 0.20, 0])), "r5"))
        cases.append(("r5_head_right20", dict(motion=squat, seconds=3, ramp=0.5, freeze=0,
                                              vr_delta=np.zeros(9), step_at=1.0,
                                              step_delta=np.array([0, 0, 0, 0, 0, 0, 0, -0.20, 0])), "r5"))
        stand_conds = {
            "baseline": (None, None),
            "Lfwd25": (np.array([0.25, 0, 0, 0, 0, 0, 0, 0, 0]), None),
            "Rfwd25": (np.array([0, 0, 0, 0.25, 0, 0, 0, 0, 0]), None),
            "L_yaw30": (None, [qz(np.deg2rad(30)), I, I]),
            "R_yaw30": (None, [I, qz(np.deg2rad(30)), I]),
            "head_yaw30": (None, [I, I, qz(np.deg2rad(30))]),
            "head_pitch30": (None, [I, I, qy(np.deg2rad(30))]),
            "head_left20": (np.array([0, 0, 0, 0, 0, 0, 0, 0.20, 0]), None),
        }
        for name, (delta, orn) in stand_conds.items():
            cases.append(("stand_" + name, dict(motion=squat, seconds=6, ramp=0.5, freeze=0,
                                                vr_delta=delta, vr_orn_delta=orn), "stand"))
        for name, kw, kind in cases:
            r = S.SonicRunner(1, model=model)
            rows = r.run(log_path=os.path.join(LOGS, f"{model}_{name}.jsonl"), **kw)
            if kind == "hold":
                m = hold_metrics(rows)
            elif kind == "walk":
                m = walk_metrics(rows)
            elif kind == "r4":
                a = rows_to_arrays(rows)
                m = {"n": len(rows), "survived": len(rows) >= 600, "phases": {}}
                for t0, t1, label in [(0, 4, "walk"), (4, 8, "hold"), (8, 12, "resume")]:
                    mask = (a["t"] >= t0) & (a["t"] < t1)
                    if mask.any():
                        m["phases"][label] = dict(
                            drift=float(np.hypot(a["x"][mask][-1] - a["x"][mask][0], a["y"][mask][-1] - a["y"][mask][0])),
                            minz=float(a["z"][mask].min()), minup=float(a["up"][mask].min()))
                m["z_end"] = float(a["z"][-1])
            elif kind == "r5":
                a = rows_to_arrays(rows)
                base = a["hw"][a["t"] < 1.0, 1].mean()
                i05 = int(np.argmin(abs(a["t"] - 1.5)))
                m = dict(n=len(rows), survived=len(rows) >= 150, hy0=float(base),
                         hy_1p5=float(a["hw"][i05, 1]), d_1p5=float(a["hw"][i05, 1] - base),
                         max_d=float(np.max(np.abs(a["hw"][a["t"] >= 1.0, 1] - base))),
                         base_drift=float(np.hypot(a["x"][-1] - a["x"][0], a["y"][-1] - a["y"][0])),
                         minup=float(a["up"].min()))
            else:
                m = stand_metrics(rows, name)
            out[model][name] = m
            print(name, json.dumps(m)[:240], flush=True)
    with open(os.path.join(LOGS, "summary.json"), "w") as f:
        json.dump(out, f, indent=1)

if __name__ == "__main__":
    main()
