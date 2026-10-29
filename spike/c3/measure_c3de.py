#!/usr/bin/env python3
"""C3e/C3d: is the curve the model's, can an outer loop cancel it, and does park<->walk survive?

Sim only, headless, CPU. Every reported pose is read from mjData AFTER mj_step.
"""
from __future__ import annotations
import argparse, csv, json, math, sys
from pathlib import Path

import mujoco
import numpy as np
import onnxruntime as rt

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from measure_c3 import (  # noqa: E402
    PolicySim, yaw_q, wrap, qrot, qconj, drift_table, LOGS, ROOT,
    SIM_DT, CTRL_DT, N_SUBSTEPS, TICK_DT, ACTION_SCALE, GAIT_FREQ,
)

POLICY = ROOT / "policy/g1_policy.onnx"
PG_DIR = ROOT / "playground_g1"
PG_XML = PG_DIR / "xmls/scene_mjx_feetonly_flat_terrain.xml"
MEN_DIR = Path("/home/nate/Projects/SuperBot/spike/e3/vendor/mujoco_menagerie/unitree_g1")
MEN_SCENE = MEN_DIR / "scene.xml"
MEN_SCENE_MJX = MEN_DIR / "scene_mjx.xml"
OUT = ROOT / "c3de_measurements.json"


def _assets():
    assets = {}
    for d in (PG_DIR / "xmls", PG_DIR / "xmls/assets", MEN_DIR, MEN_DIR / "assets"):
        for f in d.iterdir():
            if f.is_file():
                assets[f.name] = f.read_bytes()
    return assets


def _common(m, sess):
    default_angles = m.keyframe("knees_bent").qpos[7:].copy()
    head_mesh = m.mesh("head_link").id
    head_gid = next(g for g in range(m.ngeom)
                    if m.geom_dataid[g] == head_mesh and m.geom_type[g] == mujoco.mjtGeom.mjGEOM_MESH)
    arm_names = {
        "left": ["left_shoulder_pitch_joint", "left_shoulder_roll_joint", "left_shoulder_yaw_joint",
                 "left_elbow_joint", "left_wrist_roll_joint", "left_wrist_pitch_joint", "left_wrist_yaw_joint"],
        "right": ["right_shoulder_pitch_joint", "right_shoulder_roll_joint", "right_shoulder_yaw_joint",
                  "right_elbow_joint", "right_wrist_roll_joint", "right_wrist_pitch_joint", "right_wrist_yaw_joint"],
    }
    waist_qadr = m.jnt_qposadr[m.joint("waist_yaw_joint").id]
    act_id = {m.actuator(i).name: i for i in range(m.nu)}
    return default_angles, head_gid, arm_names, waist_qadr, act_id["waist_yaw_joint"]


def load_model(which):
    sess = rt.InferenceSession(str(POLICY), providers=["CPUExecutionProvider"])
    if which == "playground":
        m = mujoco.MjModel.from_xml_string(PG_XML.read_text(), assets=_assets())
    elif which == "menagerie":
        m = mujoco.MjModel.from_xml_path(str(MEN_SCENE_MJX))
    else:
        raise ValueError(which)
    m.opt.timestep = SIM_DT
    return m, sess


def heading_unwrapped_deg(samples):
    yaw = np.unwrap(np.radians([s["head_yaw_deg"] for s in samples]))
    return math.degrees(yaw[-1] - yaw[0])


def summary(out):
    m = out["metrics"]
    s = out["samples"]
    return {
        "label": m["label"],
        "duration": m["seconds"],
        "fell": m["fell"],
        "dx": m["dx"], "dy": m["dy"],
        "path_m": math.hypot(m["dx"], m["dy"]),
        "forward_m": m["forward_m"], "lateral_m": m["lateral_m"],
        "forward_speed_mps": m["forward_speed_mps"],
        "path_speed_mps": m["path_speed_mps"],
        "max_abs_lateral_m": m["max_abs_lateral_m"],
        "lateral_rms_m": m["lateral_rms_m"],
        "heading_travel_deg": heading_unwrapped_deg(s),
        "heading_end_deg": m["head_yaw_end_deg"],
        "max_tilt_deg": m["max_tilt_deg"],
        "torso_pitch_std_deg": m["torso_pitch_std_deg"],
        "base_drift_m": math.hypot(m["base_end"][0] - m["base_start"][0],
                                   m["base_end"][1] - m["base_start"][1]),
        "head_drift_xy_m": m["head_disp_xy_m"],
        "head_drift_xyz_m": m["head_disp_xyz_m"],
        "head_z_mean_last3s": m["head_z_mean_last3s"],
        "base_z_end": m["base_z_end"],
    }


def run_c3e1(sess):
    results = {}
    for which, tag in (("playground", "pg"), ("menagerie", "men")):
        m, _ = load_model(which)
        defaults, head_gid, arms, waist_qadr, waist_act = _common(m, sess)
        sim = PolicySim(m, sess, defaults, arms, head_gid, waist_qadr, waist_act)
        print(f"\n=== C3e-1 {which.upper()} nq={m.nq} nu={m.nu} keyframes={[m.keyframe(i).name for i in range(m.nkey)]}")
        fwd10 = sim.run(f"C3e-1 {which}: +0.5 m/s 10 s", 10.0,
                        lambda t, d, s: (0.5, 0.0, 0.0), log_prefix=f"c3e1_{tag}_fwd10")
        zero20 = sim.run(f"C3e-1 {which}: zero command 20 s", 20.0,
                         lambda t, d, s: (0.0, 0.0, 0.0), log_prefix=f"c3e1_{tag}_zero20")
        zero20["drift_table"] = drift_table(zero20["samples"])
        results[which] = {"fwd10": summary(fwd10), "zero20": summary(zero20),
                          "zero20_drift_table": zero20["drift_table"],
                          "model": which}
        print(fwd10["metrics"]["label"], summary(fwd10))
        print(zero20["metrics"]["label"], summary(zero20))
    return results


def _clamp(x, lo, hi):
    return max(lo, min(hi, x))


def make_outer(kp_yaw, kp_vx, target_vx, target_yaw_deg=0.0, wz_limit=0.6, vx_lo=-1.0, vx_hi=1.2):
    """P-only outer loop. No integrator => no windup; clamps are the saturation guard."""
    def fn(t, d, s):
        yaw = yaw_q(np.array(d.qpos[3:7]))
        err = wrap(math.radians(target_yaw_deg) - yaw)
        wz = _clamp(kp_yaw * err, -wz_limit, wz_limit)
        vx_meas = float(d.sensor("local_linvel_pelvis").data[0])
        vx = target_vx + kp_vx * (target_vx - vx_meas)
        vx = _clamp(vx, vx_lo, vx_hi)
        return (vx, 0.0, wz)
    return fn


def _heading_stats(samples, target_deg=0.0):
    errs = []
    for s in samples:
        errs.append(math.degrees(wrap(math.radians(s["head_yaw_deg"] - target_deg))))
    errs = np.array(errs)
    t = np.array([s["t"] for s in samples])
    steady = t >= min(2.0, t[-1] / 2)
    yaw = np.unwrap(np.radians([s["head_yaw_deg"] for s in samples]))
    dt = np.maximum(np.diff(t), 1e-9)
    rate = np.degrees(np.diff(yaw) / dt)
    tm = 0.5 * (t[1:] + t[:-1])
    steady_rate = rate[tm >= min(2.0, t[-1] / 2)]
    return {
        "heading_err_rms_deg": float(np.sqrt(np.mean(errs**2))),
        "heading_err_max_deg": float(np.max(np.abs(errs))),
        "steady_heading_err_rms_deg": float(np.sqrt(np.mean(errs[steady]**2))),
        "yaw_rate_rms_deg_s": float(np.sqrt(np.mean(steady_rate**2))),
        "yaw_reversals": int(np.sum(np.diff(np.sign(steady_rate)) != 0)),
    }


def run_c3e2(sess, which):
    m, _ = load_model(which)
    defaults, head_gid, arms, waist_qadr, waist_act = _common(m, sess)
    sim = PolicySim(m, sess, defaults, arms, head_gid, waist_qadr, waist_act)

    kp_yaws = [0.5, 1.0, 2.0, 4.0, 8.0]
    kp_vxs = [0.5, 1.0, 2.0, 4.0]
    rows = []
    for ky in kp_yaws:
        for kv in kp_vxs:
            out = sim.run(f"C3e-2 sweep ky={ky} kv={kv}", 10.0, make_outer(ky, kv, 0.5),
                          log_prefix=f"c3e2_ky{ky:g}_kv{kv:g}")
            sm = summary(out)
            sm.update({"kp_yaw": ky, "kp_vx": kv})
            sm.update(_heading_stats(out["samples"]))
            rows.append(sm)
            print(f"[sweep ky={ky:g} kv={kv:g}] heading_rms={sm['heading_err_rms_deg']:.2f} "
                  f"max={sm['heading_err_max_deg']:.2f} lat_rms={sm['lateral_rms_m']:.3f} "
                  f"fwd={sm['forward_speed_mps']:.3f} path={sm['path_speed_mps']:.3f} "
                  f"tilt={sm['max_tilt_deg']:.1f} fell={sm['fell']}", flush=True)

    # Winner: lowest steady heading RMS among non-falls, tie-break lateral RMS then path speed.
    ok = [r for r in rows if not r["fell"]]
    pool = ok if ok else rows
    best = min(pool, key=lambda r: (round(r["steady_heading_err_rms_deg"], 2),
                                    round(r["lateral_rms_m"], 2),
                                    -r["path_speed_mps"]))
    print("BEST", best["kp_yaw"], best["kp_vx"], best["steady_heading_err_rms_deg"],
          best["lateral_rms_m"], best["forward_speed_mps"])

    # 20 s zero-velocity run with the winning gains.
    zero = sim.run(f"C3e-2 zero 20 s winner ky={best['kp_yaw']} kv={best['kp_vx']}", 20.0,
                   make_outer(best["kp_yaw"], best["kp_vx"], 0.0),
                   log_prefix="c3e2_zero20_winner")
    zero_sum = summary(zero)
    zero_sum.update(_heading_stats(zero["samples"]))
    zero_sum.update({"kp_yaw": best["kp_yaw"], "kp_vx": best["kp_vx"]})
    zero["drift_table"] = drift_table(zero["samples"])

    # Stability margin: double each gain independently, and both together.
    margins = {}
    for name, ky, kv in (
        ("double_kp_yaw", 2 * best["kp_yaw"], best["kp_vx"]),
        ("double_kp_vx", best["kp_yaw"], 2 * best["kp_vx"]),
        ("double_both", 2 * best["kp_yaw"], 2 * best["kp_vx"]),
    ):
        out = sim.run(f"C3e-2 margin {name}", 10.0, make_outer(ky, kv, 0.5),
                      log_prefix=f"c3e2_{name}")
        sm = summary(out)
        sm.update({"kp_yaw": ky, "kp_vx": kv})
        sm.update(_heading_stats(out["samples"]))
        margins[name] = sm
        print(f"[margin {name}] heading_rms={sm['heading_err_rms_deg']:.2f} "
              f"max={sm['heading_err_max_deg']:.2f} lat_rms={sm['lateral_rms_m']:.3f} "
              f"fwd={sm['forward_speed_mps']:.3f} tilt={sm['max_tilt_deg']:.1f} "
              f"reversals={sm['yaw_reversals']} fell={sm['fell']}", flush=True)

    return {"model": which, "sweep": rows, "best": best, "zero20": zero_sum,
            "zero20_drift_table": zero["drift_table"], "margins": margins}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="all")
    ap.add_argument("--model", default="playground", choices=["playground", "menagerie"])
    ap.add_argument("--ramp", type=float, default=0.4)
    args = ap.parse_args()
    LOGS.mkdir(parents=True, exist_ok=True)
    out = {}
    if OUT.exists():
        try:
            out = json.loads(OUT.read_text())
        except Exception:
            out = {}

    if args.only in ("all", "c3e1"):
        sess = rt.InferenceSession(str(POLICY), providers=["CPUExecutionProvider"])
        out["c3e1"] = run_c3e1(sess)
        OUT.write_text(json.dumps(out, indent=1))

    if args.only in ("all", "c3e2"):
        sess = rt.InferenceSession(str(POLICY), providers=["CPUExecutionProvider"])
        key = "c3e2" if args.model == "playground" else f"c3e2_{args.model}"
        out[key] = run_c3e2(sess, args.model)
        OUT.write_text(json.dumps(out, indent=1))

    if args.only in ("all", "c3d"):
        sess = rt.InferenceSession(str(POLICY), providers=["CPUExecutionProvider"])
        out["c3d"] = run_c3d(sess, ramp=args.ramp)
        OUT.write_text(json.dumps(out, indent=1))

    print("WROTE", OUT)


# ---------------------------------------------------------------------------
# C3d: park <-> walk mode machine on the Playground model with E3 park PD gains.
# ---------------------------------------------------------------------------

class ModeSim(PolicySim):
    """Playground model + policy walk mode + E3 stand-pose park mode."""

    def __init__(self, m, sess, defaults, head_gid, arms, waist_qadr, waist_act):
        self.stand_model = mujoco.MjModel.from_xml_path(str(MEN_SCENE))
        self.stand_q = self.stand_model.keyframe("stand").qpos.copy()
        self.stand_ctrl = self.stand_model.keyframe("stand").ctrl.copy()
        self.knees_q = m.keyframe("knees_bent").qpos.copy()
        self.knees_ctrl = m.keyframe("knees_bent").ctrl.copy()
        self.walk_gain = m.actuator_gainprm.copy()
        self.walk_bias = m.actuator_biasprm.copy()
        self.park_gain = self.walk_gain.copy()
        self.park_bias = self.walk_bias.copy()
        for i in range(m.nu):
            n = m.actuator(i).name
            j = self.stand_model.actuator(n).id
            self.park_gain[i] = self.stand_model.actuator_gainprm[j]
            self.park_bias[i] = self.stand_model.actuator_biasprm[j]
        super().__init__(m, sess, defaults, arms, head_gid, waist_qadr, waist_act)
        self.m = m
        self.torso_bid = m.body("torso_link").id
        # Park neutral wrist targets, expressed in the stand torso frame.
        sd = mujoco.MjData(m)
        sd.qpos[:] = self.stand_q
        sd.qvel[:] = 0
        mujoco.mj_forward(m, sd)
        tq = np.array(sd.body(self.torso_bid).xquat, copy=True)
        tp = np.array(sd.body(self.torso_bid).xpos, copy=True)
        self.park_neutral = {s: qrot(qconj(tq), np.array(sd.body(self.wrist_bid[s]).xpos) - tp)
                             for s in self.arm_names}
        self.transition = None
        self.mode = "park"

    def _set_gains(self, park_frac):
        p = float(max(0.0, min(1.0, park_frac)))
        self.m.actuator_gainprm[:] = (1 - p) * self.walk_gain + p * self.park_gain
        self.m.actuator_biasprm[:] = (1 - p) * self.walk_bias + p * self.park_bias

    def reset_to(self, qpos, ctrl0=None):
        d = self.d
        d.qpos[:] = qpos
        d.qvel[:] = 0
        d.ctrl[:] = ctrl0 if ctrl0 is not None else 0.0
        mujoco.mj_forward(self.m, d)
        self.last_action = np.zeros(self.m.nu, dtype=np.float32)
        self.phase = np.array([0.0, np.pi])
        self.counter = 0
        self.q_target = {s: np.array(d.qpos[self.arm_qadr[s]], copy=True) for s in self.arm_names}
        self.waist_target = float(d.qpos[self.waist_qadr])
        self.ik_residual = {"left": float("nan"), "right": float("nan")}
        self.transition = None

    def begin_mode(self, mode, ramp):
        if mode == self.mode:
            return
        self.transition = {"from": np.array(self.d.ctrl, copy=True), "mode": mode,
                           "from_mode": self.mode,
                           "start": float(self.d.time), "ramp": float(ramp)}
        if mode == "park":
            for s in self.arm_names:
                self.q_target[s] = np.array(self.d.qpos[self.arm_qadr[s]], copy=True)
        self.mode = mode

    def _dls_into(self, ctrl, reach_left=None):
        m, d = self.m, self.d
        tq = np.array(d.body(self.torso_bid).xquat, copy=True)
        tp = np.array(d.body(self.torso_bid).xpos, copy=True)
        for s in self.arm_names:
            local = self.park_neutral[s].copy()
            if s == "left" and reach_left is not None:
                local = local + reach_left
            target = tp + qrot(tq, local)
            mujoco.mj_jacBody(m, d, self.jacp, self.jacr, self.wrist_bid[s])
            J = self.jacp[:, self.arm_dof[s]]
            e = target - np.array(d.body(self.wrist_bid[s]).xpos, copy=True)
            A = J @ J.T + (0.05 ** 2) * np.eye(3)
            dq = J.T @ np.linalg.solve(A, e)
            nd = np.linalg.norm(dq)
            if nd > 0.25:
                dq *= 0.25 / nd
            desired = np.array(d.qpos[self.arm_qadr[s]], copy=True) + dq
            desired = np.clip(desired, m.jnt_range[self.arm_jid[s], 0], m.jnt_range[self.arm_jid[s], 1])
            self.q_target[s] = self.q_target[s] + np.clip(desired - self.q_target[s], -2.0 * TICK_DT, 2.0 * TICK_DT)
            self.q_target[s] = np.clip(self.q_target[s], m.jnt_range[self.arm_jid[s], 0], m.jnt_range[self.arm_jid[s], 1])
            ctrl[self.arm_act[s]] = self.q_target[s]
            self.ik_residual[s] = float(np.linalg.norm(target - np.array(d.body(self.wrist_bid[s]).xpos, copy=True)))

    def control_tick(self, mode, ramp, park_elapsed, reach_left=None, vx=0.5):
        d = self.d
        if mode in ("walk", "stop"):
            command = np.array([vx * 1.5, 0.0, 0.0], dtype=np.float32)
            obs = self._obs(command)
            raw = list(self.sess.run(["continuous_actions"], {"obs": obs.reshape(1, -1)}))
            act = np.asarray(raw[0], dtype=np.float32).reshape(-1)
            self.last_action = act.copy()
            target = act * ACTION_SCALE + self.default_angles
            self.phase = np.fmod(self.phase + 2 * np.pi * GAIT_FREQ * CTRL_DT + np.pi, 2 * np.pi) - np.pi
        else:
            target = np.array(self.stand_ctrl if mode == "park" else self.knees_ctrl, copy=True)
            self._dls_into(target, reach_left)

        if self.transition is not None:
            alpha = min(1.0, (d.time - self.transition["start"]) / self.transition["ramp"])
            ctrl = (1.0 - alpha) * self.transition["from"] + alpha * target
            if self.transition["mode"] == "park":
                park_frac = alpha
            elif self.transition["from_mode"] == "park":
                park_frac = 1.0 - alpha
            else:
                park_frac = 0.0
            self._set_gains(park_frac)
            if alpha >= 1.0:
                self.transition = None
                self._set_gains(1.0 if mode == "park" else 0.0)
        else:
            ctrl = target
            self._set_gains(1.0 if mode == "park" else 0.0)
        d.ctrl[:] = ctrl

    def run_schedule(self, label, schedule, ramp=0.4, reach=False, stop_on_fall=True,
                     log_prefix=None):
        self.reset_to(self.stand_q if schedule[0][0] == "park" else self.knees_q,
                      self.stand_ctrl if schedule[0][0] == "park" else None)
        self.mode = schedule[0][0]
        self._set_gains(1.0 if self.mode == "park" else 0.0)
        self.transition = None
        samples, segments, fall = [], [], None
        z_start = float(self.d.qpos[2])
        seg_start = float(self.d.time)
        seg = 0
        still_since = None
        settling = None
        seg_pose0 = np.array(self.d.qpos[:3], copy=True)
        if log_prefix:
            LOGS.mkdir(parents=True, exist_ok=True)

        for item in schedule:
            mode, dur = item[0], item[1]
            vx = item[2] if len(item) > 2 else None
            self.begin_mode(mode, ramp)
            seg_end = seg_start + dur
            still_since = None
            settling = None
            seg_pose0 = np.array(self.d.qpos[:3], copy=True)
            reach_max = 0.0
            max_tilt = 0.0
            min_z = 1e9
            while self.d.time < seg_end - 1e-9:
                self.counter += 1
                pe = float(self.d.time - seg_start)
                if self.counter % N_SUBSTEPS == 0:
                    reach_vec = None
                    if reach and mode == "park":
                        # 0 -> 0.10 m forward -> 0 over seconds 1..3 of the park.
                        if 1.0 <= pe <= 3.0:
                            u = (pe - 1.0) / 2.0
                            s = 0.5 - 0.5 * math.cos(2 * math.pi * u)
                            reach_vec = np.array([0.10 * s, 0.0, 0.0])
                            reach_max = max(reach_max, 0.10 * s)
                    vx_cmd = vx if vx is not None else (0.0 if mode == "stop" else 0.5)
                    self.control_tick(mode, ramp, pe, reach_vec, vx=vx_cmd)
                mujoco.mj_step(self.m, self.d)
                xm = self.d.body(self.torso_bid).xmat.reshape(3, 3)
                tilt = math.degrees(math.acos(max(-1.0, min(1.0, float(xm[2, 2])))))
                max_tilt = max(max_tilt, tilt)
                min_z = min(min_z, float(self.d.qpos[2]))
                # Park settling: 0.5 s continuously still.
                if mode in ("park", "knees"):
                    lin = float(np.linalg.norm(self.d.qvel[0:3]))
                    ang = float(np.linalg.norm(self.d.qvel[3:6]))
                    jv = float(np.max(np.abs(self.d.qvel[6:])))
                    still = (lin < 0.05 and ang < 0.1 and jv < 0.2 and tilt < 5.0)
                    if still:
                        if still_since is None:
                            still_since = float(self.d.time)
                        if settling is None and self.d.time - still_since >= 0.5:
                            settling = float(self.d.time - seg_start - 0.5)
                    else:
                        still_since = None
                if self.counter % 50 == 0:
                    s = self._sample(0.0, "policy" if mode == "walk" else "stand", "policy" if mode == "walk" else 0.0)
                    s["mode"] = mode
                    s["segment"] = seg
                    s["park_elapsed"] = pe
                    s["reach_cmd"] = float(reach_vec[0]) if (reach and mode == "park" and reach_vec is not None) else 0.0
                    s["wrist_l_x"] = float(self.d.body(self.wrist_bid["left"]).xpos[0])
                    s["wrist_l_y"] = float(self.d.body(self.wrist_bid["left"]).xpos[1])
                    s["wrist_l_z"] = float(self.d.body(self.wrist_bid["left"]).xpos[2])
                    samples.append(s)
                if self.d.qpos[2] < 0.6 * z_start or tilt > 60.0:
                    fall = {"time": float(self.d.time), "mode": mode, "segment": seg,
                            "tilt_deg": tilt, "base_z": float(self.d.qpos[2]),
                            "segment_elapsed": float(self.d.time - seg_start)}
                    break
            seg_samples = [s for s in samples if s["segment"] == seg]
            if seg_samples:
                s0, s1 = seg_samples[0], seg_samples[-1]
                dxy = math.hypot(s1["base_x"] - s0["base_x"], s1["base_y"] - s0["base_y"])
                head_xy = math.hypot(s1["head_x"] - s0["head_x"], s1["head_y"] - s0["head_y"])
                head_z = s1["head_z"] - s0["head_z"]
                yaw = np.unwrap(np.radians([s["head_yaw_deg"] for s in seg_samples]))
                yaw_travel = math.degrees(yaw[-1] - yaw[0])
            else:
                dxy = head_xy = head_z = yaw_travel = float("nan")
            end_pose = np.array(self.d.qpos[:3], copy=True)
            xm = self.d.body(self.torso_bid).xmat.reshape(3, 3)
            end_tilt = math.degrees(math.acos(max(-1.0, min(1.0, float(xm[2, 2])))))
            segments.append({
                "segment": seg, "mode": mode, "duration": dur,
                "start_t": seg_start, "end_t": float(self.d.time),
                "fell": bool(fall and fall["segment"] == seg),
                "max_tilt_deg": max_tilt, "min_base_z": min_z,
                "end_base": end_pose.tolist(),
                "xy_move_m": dxy, "head_xy_move_m": head_xy, "head_dz_m": head_z,
                "heading_travel_deg": yaw_travel,
                "end_tilt_deg": end_tilt,
                "end_base_z": float(self.d.qpos[2]),
                "settling_time_s": settling,
                "recovered_xy_from_seg_start_m": float(math.hypot(end_pose[0] - seg_pose0[0],
                                                                      end_pose[1] - seg_pose0[1])),
                "joint_err_to_stand_rms_rad": float(np.sqrt(np.mean(
                    (np.array(self.d.qpos[7:]) - self.stand_q[7:]) ** 2))),
                "reach_max_m": reach_max if (reach and mode == "park") else None,
                "max_ik_residual_m": float(np.nanmax([max(abs(s["ik_res_l"]), abs(s["ik_res_r"]))
                                                      for s in seg_samples]))
                                          if any(np.isfinite(s["ik_res_l"]) for s in seg_samples) else None,
            })
            seg += 1
            seg_start = float(self.d.time)
            if fall and stop_on_fall:
                break

        out = {"label": label, "schedule": schedule, "ramp_s": ramp, "reach": reach,
               "fall": fall, "segments": segments,
               "max_tilt_deg": max((s["max_tilt_deg"] for s in segments), default=None),
               "samples": samples}
        if log_prefix:
            keys = list(samples[0].keys())
            with open(LOGS / f"{log_prefix}.csv", "w", newline="") as f:
                w = csv.DictWriter(f, fieldnames=keys)
                w.writeheader()
                for s in samples:
                    w.writerow(s)
            with open(LOGS / f"{log_prefix}.json", "w") as f:
                json.dump(out, f, indent=1)
        return out


def run_c3d(sess, ramp=0.4):
    m, _ = load_model("playground")
    defaults, head_gid, arms, waist_qadr, waist_act = _common(m, sess)
    sim = ModeSim(m, sess, defaults, head_gid, arms, waist_qadr, waist_act)
    out = {"model": "playground_xml_with_e3_park_gains", "ramp_s": ramp}

    def show(tag, r):
        print(f"\n[{tag}] fall={r['fall']} ramp={r['ramp_s']}")
        for s in r["segments"]:
            print(f" seg {s['segment']:2d} {s['mode']:4s} {s['duration']}s fell={s['fell']} "
                  f"end_z={s['end_base_z']:.3f} tilt={s['end_tilt_deg']:.1f} "
                  f"xy={s['recovered_xy_from_seg_start_m']:.3f} settle={s['settling_time_s']} "
                  f"head_dxy={s['head_xy_move_m']:.3f} yaw={s['heading_travel_deg']:+.1f} "
                  f"ik={s['max_ik_residual_m']}")

    t1 = sim.run_schedule("T1 park5 -> (walk2 -> park5) x5", [("park", 5.0)] + [("walk", 2.0), ("park", 5.0)] * 5,
                          ramp=ramp, log_prefix=f"c3d_T1_ramp{ramp:g}")
    out["T1"] = t1
    show("T1", t1)

    t2 = sim.run_schedule("T2 walk10 -> park10", [("walk", 10.0), ("park", 10.0)],
                          ramp=ramp, log_prefix=f"c3d_T2_ramp{ramp:g}")
    out["T2"] = t2
    show("T2", t2)

    t3 = sim.run_schedule("T3 park5 -> (walk2 -> park5 + left reach) x5",
                          [("park", 5.0)] + [("walk", 2.0), ("park", 5.0)] * 5,
                          ramp=ramp, reach=True, log_prefix=f"c3d_T3_ramp{ramp:g}")
    out["T3"] = t3
    show("T3", t3)

    # Brake variant: walk -> stop (zero-command walk policy) -> park.
    t4 = sim.run_schedule("T4 walk10 -> stop2 -> park10", [("walk", 10.0), ("stop", 2.0), ("park", 10.0)],
                          ramp=ramp, log_prefix=f"c3d_T4_brake2_ramp{ramp:g}")
    out["T4"] = t4
    show("T4", t4)

    t5 = sim.run_schedule("T5 walk10 -> stop3 -> park10", [("walk", 10.0), ("stop", 3.0), ("park", 10.0)],
                          ramp=ramp, log_prefix=f"c3d_T5_brake3_ramp{ramp:g}")
    out["T5"] = t5
    show("T5", t5)

    # Longer-ramp variants of the shipped-style transition.
    for tag, ramp_v in (("T6", 1.0), ("T7", 2.0)):
        r = sim.run_schedule(f"{tag} walk10 -> park10 ramp{ramp_v:g}", [("walk", 10.0), ("park", 10.0)],
                             ramp=ramp_v, log_prefix=f"c3d_{tag}_ramp{ramp_v:g}")
        out[tag] = r
        show(tag, r)

    # Short ramps: snap to the static stand target rather than freezing the last walk action.
    for tag, ramp_v in (("T8", 0.1), ("T9", 0.05)):
        r = sim.run_schedule(f"{tag} walk10 -> park10 ramp{ramp_v:g}", [("walk", 10.0), ("park", 10.0)],
                             ramp=ramp_v, log_prefix=f"c3d_{tag}_ramp{ramp_v:g}")
        out[tag] = r
        show(tag, r)

    # Slow approach: walk at 0.05 m/s, then park.
    t10 = sim.run_schedule("T10 walk5@0.05 -> park10", [("walk", 5.0, 0.05), ("park", 10.0)],
                           ramp=ramp, log_prefix=f"c3d_T10_slow_ramp{ramp:g}")
    out["T10"] = t10
    show("T10", t10)

    # Knees-bent static pose with walk gains, as a control for "any static pose".
    t11 = sim.run_schedule("T11 walk10 -> knees10", [("walk", 10.0), ("knees", 10.0)],
                           ramp=ramp, log_prefix=f"c3d_T11_knees_ramp{ramp:g}")
    out["T11"] = t11
    show("T11", t11)
    return out


if __name__ == "__main__":
    main()
