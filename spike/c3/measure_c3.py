#!/usr/bin/env python3
"""C3 spike: pretrained MuJoCo-Playground G1 policy on Menagerie scene_mjx.xml.

Measurement only. Every pose reported is read from mjData AFTER mj_step.
"""
from __future__ import annotations
import argparse, csv, json, math, os, sys
from pathlib import Path

import mujoco
import numpy as np
import onnxruntime as rt

ROOT = Path(__file__).resolve().parent
MODEL_XML = Path("/home/nate/Projects/Clank/spike/e3/vendor/mujoco_menagerie/unitree_g1/scene_mjx.xml")
POLICY = ROOT / "policy/g1_policy.onnx"
LOGS = ROOT / "logs"

CTRL_DT = 0.02
SIM_DT = 0.002
N_SUBSTEPS = round(CTRL_DT / SIM_DT)
ACTION_SCALE = 0.5
GAIT_FREQ = 1.5
TICK_DT = CTRL_DT
SAMPLE_EVERY = 50  # sim steps -> 0.1 s


def wrap(a):
    return (a + np.pi) % (2 * np.pi) - np.pi


def yaw_q(q_wxyz):
    w, x, y, z = q_wxyz
    return math.atan2(2.0 * (w * z + x * y), 1.0 - 2.0 * (y * y + z * z))


def qrot(q, v):
    w, x, y, z = q
    qv = np.array([x, y, z])
    t = 2.0 * np.cross(qv, v)
    return v + w * t + np.cross(qv, t)


def qconj(q):
    return np.array([q[0], -q[1], -q[2], -q[3]])


class PolicySim:
    def __init__(self, model, sess, default_angles, arm_names, head_gid, waist_qadr, waist_act):
        self.m = model
        self.sess = sess
        self.default_angles = default_angles
        self.d = mujoco.MjData(model)
        self.arm_names = arm_names
        self.head_gid = head_gid
        self.waist_qadr = waist_qadr
        self.waist_act = waist_act
        self.site_id = model.site("imu_in_pelvis").id
        self.arm_qadr = {s: np.array([model.jnt_qposadr[model.joint(n).id] for n in ns]) for s, ns in arm_names.items()}
        self.arm_dof = {s: np.array([model.jnt_dofadr[model.joint(n).id] for n in ns]) for s, ns in arm_names.items()}
        self.arm_jid = {s: np.array([model.joint(n).id for n in ns]) for s, ns in arm_names.items()}
        act_id = {model.actuator(i).name: i for i in range(model.nu)}
        self.arm_act = {s: np.array([act_id[n] for n in ns]) for s, ns in arm_names.items()}
        self.wrist_bid = {s: model.body(f"{s}_wrist_yaw_link").id for s in ("left", "right")}
        self.torso_bid = model.body("torso_link").id
        self.jacp = np.zeros((3, model.nv))
        self.jacr = np.zeros((3, model.nv))
        self.reset()

    def reset(self):
        m, d = self.m, self.d
        mujoco.mj_resetDataKeyframe(m, d, 1)  # knees_bent
        mujoco.mj_forward(m, d)
        self.last_action = np.zeros(m.nu, dtype=np.float32)
        self.phase = np.array([0.0, np.pi])
        self.counter = 0
        self.q_target = {s: np.array(d.qpos[self.arm_qadr[s]], copy=True) for s in self.arm_names}
        self.neutral_local = {}
        torso_q = np.array(d.body(self.torso_bid).xquat, copy=True)
        torso_p = np.array(d.body(self.torso_bid).xpos, copy=True)
        for s in self.arm_names:
            wp = np.array(d.body(self.wrist_bid[s]).xpos, copy=True)
            self.neutral_local[s] = qrot(qconj(torso_q), wp - torso_p)
        self.waist_target = float(d.qpos[self.waist_qadr])
        self.ik_residual = {"left": float("nan"), "right": float("nan")}
        self.ik_status = "off"

    def _obs(self, command):
        m, d = self.m, self.d
        linvel = d.sensor("local_linvel_pelvis").data
        gyro = d.sensor("gyro_pelvis").data
        xmat = d.site_xmat[self.site_id].reshape(3, 3)
        gravity = xmat.T @ np.array([0.0, 0.0, -1.0])
        phase4 = np.concatenate([np.cos(self.phase), np.sin(self.phase)])
        obs = np.hstack([
            linvel, gyro, gravity, command,
            d.qpos[7:] - self.default_angles,
            d.qvel[6:],
            self.last_action,
            phase4,
        ]).astype(np.float32)
        return obs

    def _arm_neutral(self):
        m, d = self.m, self.d
        torso_q = np.array(d.body(self.torso_bid).xquat, copy=True)
        torso_p = np.array(d.body(self.torso_bid).xpos, copy=True)
        for s in self.arm_names:
            target = torso_p + qrot(torso_q, self.neutral_local[s])
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
            d.ctrl[self.arm_act[s]] = self.q_target[s]
            self.ik_residual[s] = float(np.linalg.norm(target - np.array(d.body(self.wrist_bid[s]).xpos, copy=True)))

    def run(self, label, seconds, cmd_fn, arms="policy", waist="policy", log_prefix=None, sample=True):
        """cmd_fn(t, d, sim) -> (vx, vy, wz) in m/s, m/s, rad/s. wz is desired yaw rate."""
        self.reset()
        m, d = self.m, self.d
        self.ik_status = arms
        x0 = np.array([d.qpos[0], d.qpos[1], d.qpos[2]], copy=True)
        q0 = np.array(d.qpos[3:7], copy=True)
        yaw0 = yaw_q(q0)
        samples = []
        n_steps = int(round(seconds / SIM_DT))
        phase_dt = 2 * np.pi * GAIT_FREQ * CTRL_DT
        cmd_now = (0.0, 0.0, 0.0)

        for k in range(n_steps):
            self.counter += 1
            if self.counter % N_SUBSTEPS == 0:
                vx, vy, wz = cmd_fn(d.time, d, self)
                cmd_now = (vx, vy, wz)
                command = np.array([vx * 1.5, vy * 0.8, wz * 2 * np.pi], dtype=np.float32)
                obs = self._obs(command)
                raw = list(self.sess.run(["continuous_actions"], {"obs": obs.reshape(1, -1)}))
                act = np.asarray(raw[0], dtype=np.float32).reshape(-1)
                self.last_action = act.copy()
                d.ctrl[:] = act * ACTION_SCALE + self.default_angles
                self.phase = np.fmod(self.phase + phase_dt + np.pi, 2 * np.pi) - np.pi
                if arms == "neutral":
                    self._arm_neutral()
                if isinstance(waist, (int, float)):
                    delta = float(waist) - self.waist_target
                    self.waist_target += float(np.clip(delta, -2.0 * TICK_DT, 2.0 * TICK_DT))
                    d.ctrl[self.waist_act] = self.waist_target

            mujoco.mj_step(m, d)

            if sample and self.counter % SAMPLE_EVERY == 0:
                samples.append(self._sample(cmd_now[2], arms, waist))

        if sample:
            samples.append(self._sample(cmd_now[2], arms, waist))
        metrics = self._metrics(samples, seconds, yaw0, x0)
        metrics.update({"label": label, "seconds": seconds, "arms": arms,
                        "waist": ("policy" if waist == "policy" else float(waist))})
        out = {"metrics": metrics, "samples": samples}
        if log_prefix:
            self._write_csv(log_prefix, samples)
            with open(LOGS / f"{log_prefix}.json", "w") as f:
                json.dump(out, f, indent=1)
        return out

    def _sample(self, wz_cmd, arms, waist):
        m, d = self.m, self.d
        bq = np.array(d.qpos[3:7], copy=True)
        tq = np.array(d.body(self.torso_bid).xquat, copy=True)
        xmat = np.array(d.body(self.torso_bid).xmat, copy=True).reshape(3, 3)
        xaxis, yaxis, zaxis = xmat[:, 0], xmat[:, 1], xmat[:, 2]
        tilt = math.degrees(math.acos(max(-1.0, min(1.0, float(zaxis[2])))))
        pitch = math.degrees(math.asin(max(-1.0, min(1.0, float(-xaxis[2])))))
        roll = math.degrees(math.atan2(float(yaxis[2]), float(zaxis[2])))
        head = np.array(d.geom_xpos[self.head_gid], copy=True)
        waist_names = ["waist_yaw_joint", "waist_roll_joint", "waist_pitch_joint"]
        waist_q = [float(d.qpos[m.jnt_qposadr[m.joint(n).id]]) for n in waist_names]
        return {
            "t": float(d.time),
            "base_x": float(d.qpos[0]), "base_y": float(d.qpos[1]), "base_z": float(d.qpos[2]),
            "base_yaw_deg": math.degrees(yaw_q(bq)),
            "torso_yaw_deg": math.degrees(yaw_q(tq)),
            "torso_tilt_deg": tilt, "torso_pitch_deg": pitch, "torso_roll_deg": roll,
            "head_x": float(head[0]), "head_y": float(head[1]), "head_z": float(head[2]),
            "head_yaw_deg": math.degrees(yaw_q(tq)),
            "head_rel_base_yaw_deg": math.degrees(wrap(yaw_q(tq) - yaw_q(bq))),
            "waist_yaw": waist_q[0], "waist_roll": waist_q[1], "waist_pitch": waist_q[2],
            "ik_res_l": self.ik_residual["left"], "ik_res_r": self.ik_residual["right"],
            "cmd_wz": wz_cmd,
        }

    def _metrics(self, samples, seconds, yaw0, x0):
        s0 = samples[0]
        s1 = samples[-1]
        dx = s1["base_x"] - s0["base_x"]
        dy = s1["base_y"] - s0["base_y"]
        c, sn = math.cos(yaw0), math.sin(yaw0)
        fwd = c * dx + sn * dy
        lat = -sn * dx + c * dy
        lats = []
        for s in samples:
            ex = s["base_x"] - s0["base_x"]
            ey = s["base_y"] - s0["base_y"]
            lats.append(-sn * ex + c * ey)
        tilts = [s["torso_tilt_deg"] for s in samples]
        zs = [s["base_z"] for s in samples]
        steady = [s for s in samples if s["t"] >= min(2.0, seconds / 2)]
        pitches = [s["torso_pitch_deg"] for s in steady]
        rolls = [s["torso_roll_deg"] for s in steady]
        heads = [s["head_z"] for s in samples]
        h0 = np.array([s0["head_x"], s0["head_y"], s0["head_z"]])
        h1 = np.array([s1["head_x"], s1["head_y"], s1["head_z"]])
        z0 = s0["base_z"]
        fell = (s1["base_z"] < z0 * 0.6) or (max(tilts) > 60.0)
        return {
            "base_start": [s0["base_x"], s0["base_y"], s0["base_z"]],
            "base_end": [s1["base_x"], s1["base_y"], s1["base_z"]],
            "dx": dx, "dy": dy, "forward_m": fwd, "lateral_m": lat,
            "forward_speed_mps": fwd / seconds, "path_speed_mps": math.hypot(dx, dy) / seconds,
            "max_abs_lateral_m": max(abs(v) for v in lats),
            "lateral_rms_m": float(np.sqrt(np.mean(np.square(lats)))),
            "base_z_min": min(zs), "base_z_max": max(zs), "base_z_end": s1["base_z"],
            "max_tilt_deg": max(tilts), "end_tilt_deg": tilts[-1],
            "torso_pitch_mean_deg": float(np.mean(pitches)),
            "torso_pitch_std_deg": float(np.std(pitches)),
            "torso_pitch_ptp_deg": float(max(pitches) - min(pitches)),
            "torso_roll_mean_deg": float(np.mean(rolls)),
            "torso_roll_std_deg": float(np.std(rolls)),
            "base_z_std_m": float(np.std([s["base_z"] for s in steady])),
            "head_start": h0.tolist(), "head_end": h1.tolist(),
            "head_disp_xyz_m": float(np.linalg.norm(h1 - h0)),
            "head_disp_xy_m": float(math.hypot(h1[0] - h0[0], h1[1] - h0[1])),
            "head_z_mean_last3s": float(np.mean(heads[-max(1, int(3.0 / 0.1)):])),
            "head_z_min": min(heads), "head_z_max": max(heads),
            "head_yaw_start_deg": s0["head_yaw_deg"], "head_yaw_end_deg": s1["head_yaw_deg"],
            "head_yaw_signed_travel_deg": math.degrees(wrap(math.radians(s1["head_yaw_deg"] - s0["head_yaw_deg"]))),
            "head_rel_base_yaw_end_deg": s1["head_rel_base_yaw_deg"],
            "waist_yaw_end": s1["waist_yaw"], "waist_yaw_max_abs": max(abs(s["waist_yaw"]) for s in samples),
            "fell": bool(fell),
            "max_ik_residual_m": float(np.nanmax([max(abs(s["ik_res_l"]), abs(s["ik_res_r"])) for s in samples]))
                                 if any(np.isfinite(s["ik_res_l"]) for s in samples) else None,
        }

    @staticmethod
    def _write_csv(prefix, samples):
        LOGS.mkdir(parents=True, exist_ok=True)
        keys = list(samples[0].keys())
        with open(LOGS / f"{prefix}.csv", "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=keys)
            w.writeheader()
            for s in samples:
                w.writerow(s)


def load():
    m = mujoco.MjModel.from_xml_path(str(MODEL_XML))
    m.opt.timestep = SIM_DT
    sess = rt.InferenceSession(str(POLICY), providers=["CPUExecutionProvider"])
    inp = sess.get_inputs()[0]
    out = sess.get_outputs()[0]
    default_angles = m.keyframe("knees_bent").qpos[7:].copy()
    head_mesh = m.mesh("head_link").id
    head_gid = next(g for g in range(m.ngeom) if m.geom_dataid[g] == head_mesh)
    arm_names = {
        "left": ["left_shoulder_pitch_joint", "left_shoulder_roll_joint", "left_shoulder_yaw_joint",
                 "left_elbow_joint", "left_wrist_roll_joint", "left_wrist_pitch_joint", "left_wrist_yaw_joint"],
        "right": ["right_shoulder_pitch_joint", "right_shoulder_roll_joint", "right_shoulder_yaw_joint",
                  "right_elbow_joint", "right_wrist_roll_joint", "right_wrist_pitch_joint", "right_wrist_yaw_joint"],
    }
    waist_qadr = m.jnt_qposadr[m.joint("waist_yaw_joint").id]
    act_id = {m.actuator(i).name: i for i in range(m.nu)}
    return m, sess, inp, out, default_angles, head_gid, arm_names, waist_qadr, act_id["waist_yaw_joint"]


def drift_table(samples):
    t0 = samples[0]["t"]
    rows = []
    for target in (5.0, 10.0, 15.0, 20.0):
        s = min(samples, key=lambda x: abs(x["t"] - t0 - target))
        dx = s["base_x"] - samples[0]["base_x"]
        dy = s["base_y"] - samples[0]["base_y"]
        hx = s["head_x"] - samples[0]["head_x"]
        hy = s["head_y"] - samples[0]["head_y"]
        hz = s["head_z"] - samples[0]["head_z"]
        rows.append({"t": target, "base_dx": dx, "base_dy": dy,
                     "base_drift_m": math.hypot(dx, dy),
                     "head_dx": hx, "head_dy": hy, "head_dz": hz,
                     "head_drift_xy_m": math.hypot(hx, hy),
                     "head_drift_xyz_m": math.sqrt(hx * hx + hy * hy + hz * hz),
                     "base_z": s["base_z"]})
    for i in range(1, len(rows)):
        rows[i]["base_increment_m"] = math.hypot(rows[i]["base_dx"] - rows[i-1]["base_dx"],
                                                 rows[i]["base_dy"] - rows[i-1]["base_dy"])
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="all")
    args = ap.parse_args()
    LOGS.mkdir(parents=True, exist_ok=True)
    m, sess, inp, out, default_angles, head_gid, arm_names, waist_qadr, waist_act = load()
    print(f"MODEL {MODEL_XML} nq={m.nq} nv={m.nv} nu={m.nu}")
    print(f"POLICY {POLICY} {POLICY.stat().st_size} bytes input={inp.name}{inp.shape} output={out.name}{out.shape}")

    sim = PolicySim(m, sess, default_angles, arm_names, head_gid, waist_qadr, waist_act)
    allruns = {}

    def rec(key, out):
        allruns[key] = out
        with open(ROOT / "measurements.json", "w") as f:
            json.dump(allruns, f, indent=1)
        mt = out["metrics"]
        print(f"[{key}] {mt['label']}: fell={mt['fell']} dx={mt['dx']:+.3f} dy={mt['dy']:+.3f} "
              f"fwd={mt['forward_m']:+.3f}m lat={mt['lateral_m']:+.3f}m "
              f"max_tilt={mt['max_tilt_deg']:.1f} z_end={mt['base_z_end']:.3f} "
              f"head_z={mt['head_z_mean_last3s']:.4f} pitch_std={mt['torso_pitch_std_deg']:.2f}", flush=True)

    zero = lambda t, d, s: (0.0, 0.0, 0.0)
    fwd = lambda t, d, s: (0.5, 0.0, 0.0)
    fwd_slow = lambda t, d, s: (0.4, 0.0, 0.0)

    if args.only in ("all", "c3a", "baseline"):
        rec("c3a_baseline_fwd30", sim.run("C3a baseline: +0.5 m/s 30 s, policy owns all 29", 30.0, fwd,
                                          log_prefix="c3a_baseline_fwd30"))
        rec("c3a_arms_zero20", sim.run("C3a arms check: zero command 20 s, arms neutral DLS IK", 20.0, zero,
                                        arms="neutral", log_prefix="c3a_arms_zero20"))

    if args.only in ("all", "c3a"):
        rec("c3a_repro_zero5", sim.run("C3a repro: zero command 5 s (policy owns all 29)", 5.0, zero, log_prefix="c3a_repro_zero5"))
        rec("c3a_repro_fwd5", sim.run("C3a repro: +0.5 m/s 5 s (policy owns all 29)", 5.0, fwd, log_prefix="c3a_repro_fwd5"))
        rec("c3a_arms_fwd30", sim.run("C3a: +0.5 m/s 30 s, arms neutral DLS IK, waist policy", 30.0, fwd,
                                      arms="neutral", log_prefix="c3a_arms_fwd30"))
        rec("c3a_head_height", sim.run("C3a: zero command 10 s, policy all 29 (head height)", 10.0, zero,
                                       log_prefix="c3a_head_height"))

    if args.only in ("all", "c3b"):
        out = sim.run("C3b: zero command 20 s, policy all 29 (drift)", 20.0, zero, log_prefix="c3b_zero20")
        out["drift_table"] = drift_table(out["samples"])
        rec("c3b_zero20", out)
        print("DRIFT TABLE")
        for r in out["drift_table"]:
            print(json.dumps(r))

    if args.only in ("all", "c3c"):
        rec("c3c_A_walk", sim.run("C3c-A walk: +0.5 m/s 10 s, policy all 29", 10.0, fwd, log_prefix="c3c_A_walk"))

        # A: no independent look. The only way to turn the head is to turn the whole base via wz.
        def a_look(t, d, s):
            if t < 1.0:
                return (0.0, 0.0, 0.0)
            yaw = yaw_q(np.array(d.qpos[3:7]))
            err = math.radians(30.0) - yaw
            if abs(err) < math.radians(2.0):
                return (0.0, 0.0, 0.0)
            wz = max(-0.35, min(0.35, 1.2 * err))
            return (0.0, 0.0, wz)
        rec("c3c_A_look", sim.run("C3c-A look: ask head +30 deg, base-yaw command only (no independent look)",
                                   8.0, a_look, log_prefix="c3c_A_look"))

        def a_look_walk(t, d, s):
            yaw = yaw_q(np.array(d.qpos[3:7]))
            err = math.radians(30.0) - yaw
            if abs(err) < math.radians(2.0):
                wz = 0.0
            else:
                wz = max(-0.35, min(0.35, 1.2 * err))
            return (0.5, 0.0, wz)
        rec("c3c_A_look_walk", sim.run("C3c-A look while walking: +0.5 m/s, head +30 deg via base yaw only",
                                       10.0, a_look_walk, log_prefix="c3c_A_look_walk"))

        rec("c3c_B_walk", sim.run("C3c-B walk: +0.5 m/s 10 s, waist adapter at 0, arms policy", 10.0, fwd,
                                   waist=0.0, log_prefix="c3c_B_walk"))
        rec("c3c_B_look", sim.run("C3c-B look: +0.5 m/s, waist adapter target +30 deg, arms policy",
                                   10.0, fwd, waist=math.radians(30.0), log_prefix="c3c_B_look"))

        # Extra honest comparison: same waist override while walking with arms on neutral IK.
        rec("c3c_B_look_armsIK", sim.run("C3c-B look: +0.5 m/s, waist +30 deg, arms neutral DLS IK",
                                          10.0, fwd, arms="neutral", waist=math.radians(30.0),
                                          log_prefix="c3c_B_look_armsIK"))

    print("WROTE", ROOT / "measurements.json")


if __name__ == "__main__":
    main()
