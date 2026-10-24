#!/usr/bin/env python3
"""C3f / C3c-retest / C3g / C3h on the CORRECT Playground model.

Sim only, headless, CPU. Every reported pose/speed is read from mjData AFTER mj_step.
Reuses PolicySim (measure_c3.py) and the Playground loader (measure_c3de.py).
"""
from __future__ import annotations
import argparse, csv, json, math, sys
from pathlib import Path

import mujoco
import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from measure_c3 import (  # noqa: E402
    PolicySim, yaw_q, wrap, qrot, qconj, drift_table, LOGS, ROOT,
    SIM_DT, CTRL_DT, N_SUBSTEPS, TICK_DT, ACTION_SCALE, GAIT_FREQ, SAMPLE_EVERY,
)
from measure_c3de import load_model, _common  # noqa: E402

OUT = ROOT / "c3fgh_measurements.json"
MODEL_NAME = "playground_feetonly_flat_terrain"

# Arm task parameters (documented, fixed before the runs).
REACH_FWD_M = 0.10
REACH_T0, REACH_T1 = 1.0, 3.0
SOFT_ALPHA = 0.15          # F3: blend 15% of the IK target into the policy ctrl
IK_JOINT_RATE = 2.0        # rad/s, same as E3/C3a DLS rate limit


def clamp(x, lo, hi):
    return max(lo, min(hi, x))


def heading_travel_deg(samples):
    yaw = np.unwrap(np.radians([s["head_yaw_deg"] for s in samples]))
    return math.degrees(yaw[-1] - yaw[0]) if len(yaw) else float("nan")


def reach_ramp(t):
    """0 -> 0.10 m forward over [1,3] s (cosine ease), then hold 0.10 m."""
    if t < REACH_T0:
        return 0.0
    if t >= REACH_T1:
        return REACH_FWD_M
    u = (t - REACH_T0) / (REACH_T1 - REACH_T0)
    return REACH_FWD_M * (0.5 - 0.5 * math.cos(math.pi * u))


class BodySim(PolicySim):
    """Policy always runs. Optional arm task and waist override are layered on top."""

    def __init__(self, m, sess, defaults, arms, head_gid, waist_qadr, waist_act):
        super().__init__(m, sess, defaults, arms, head_gid, waist_qadr, waist_act)
        self.arm_mode = "policy"
        self.outer = None
        self.waist_hold = 0.0

    # --- arm authority -----------------------------------------------------
    def _apply_arms(self, t, reach):
        if self.arm_mode == "policy":
            return
        targets = {}
        for s in self.arm_names:
            local = self.neutral_local[s].copy()
            if reach and s == "left":
                local = local + np.array([reach_ramp(t), 0.0, 0.0])
            targets[s] = local
        blend = 1.0 if self.arm_mode == "ik_hold" else SOFT_ALPHA
        self._dls_targets(targets, blend)

    def _dls_targets(self, targets_local, blend):
        m, d = self.m, self.d
        tq = np.array(d.body(self.torso_bid).xquat, copy=True)
        tp = np.array(d.body(self.torso_bid).xpos, copy=True)
        for s in self.arm_names:
            target = tp + qrot(tq, targets_local[s])
            mujoco.mj_jacBody(m, d, self.jacp, self.jacr, self.wrist_bid[s])
            J = self.jacp[:, self.arm_dof[s]]
            actual = np.array(d.body(self.wrist_bid[s]).xpos, copy=True)
            e = target - actual
            A = J @ J.T + (0.05 ** 2) * np.eye(3)
            dq = J.T @ np.linalg.solve(A, e)
            nd = np.linalg.norm(dq)
            if nd > 0.25:
                dq *= 0.25 / nd
            desired = np.array(d.qpos[self.arm_qadr[s]], copy=True) + dq
            desired = np.clip(desired, m.jnt_range[self.arm_jid[s], 0],
                              m.jnt_range[self.arm_jid[s], 1])
            self.q_target[s] = self.q_target[s] + np.clip(
                desired - self.q_target[s], -IK_JOINT_RATE * TICK_DT, IK_JOINT_RATE * TICK_DT)
            self.q_target[s] = np.clip(self.q_target[s], m.jnt_range[self.arm_jid[s], 0],
                                       m.jnt_range[self.arm_jid[s], 1])
            if blend >= 0.999:
                d.ctrl[self.arm_act[s]] = self.q_target[s]
            else:
                d.ctrl[self.arm_act[s]] = ((1.0 - blend) * d.ctrl[self.arm_act[s]]
                                           + blend * self.q_target[s])
            self.ik_residual[s] = float(np.linalg.norm(target - actual))

    # --- generic program runner -------------------------------------------
    def _sample(self, wz_cmd, arms, waist):
        s = super()._sample(wz_cmd, arms, waist)
        d = self.d
        tq = np.array(d.body(self.torso_bid).xquat, copy=True)
        tp = np.array(d.body(self.torso_bid).xpos, copy=True)
        for side in ("left", "right"):
            p = np.array(d.body(self.wrist_bid[side]).xpos, copy=True)
            loc = qrot(qconj(tq), p - tp)
            s[f"wrist_{side}_x"] = float(p[0])
            s[f"wrist_{side}_y"] = float(p[1])
            s[f"wrist_{side}_z"] = float(p[2])
            s[f"wrist_{side}_local_x"] = float(loc[0])
            s[f"wrist_{side}_local_y"] = float(loc[1])
            s[f"wrist_{side}_local_z"] = float(loc[2])
        s["arm_mode"] = self.arm_mode
        return s

    def run_program(self, label, segments, arm_mode="policy", reach=False,
                    waist_target=None, outer=None, log_prefix=None, stop_on_fall=True):
        self.reset()
        m, d = self.m, self.d
        self.arm_mode = arm_mode
        self.outer = outer
        if outer is not None:
            outer.reset()
        self.waist_hold = float(d.qpos[self.waist_qadr])
        x0 = np.array(d.qpos[:3], copy=True)
        q0 = np.array(d.qpos[3:7], copy=True)
        yaw0 = yaw_q(q0)
        z_start = float(d.qpos[2])
        phase_dt = 2 * math.pi * GAIT_FREQ * CTRL_DT
        samples, seg_records, fall = [], [], None
        cmd_now = (0.0, 0.0, 0.0)

        for si, seg in enumerate(segments):
            dur = float(seg["dur"])
            vx = float(seg.get("vx", 0.0))
            vy = float(seg.get("vy", 0.0))
            wz = float(seg.get("wz", 0.0))
            loop_on = bool(seg.get("loop_on", False))
            mode = seg.get("mode", "walk")
            seg_start = float(d.time)
            seg_end = seg_start + dur
            if outer is not None:
                outer.engage(loop_on)
            max_tilt = 0.0
            min_z = 1e9
            reach_max = 0.0
            seg_fell = False

            while d.time < seg_end - 1e-9:
                self.counter += 1
                if self.counter % N_SUBSTEPS == 0:
                    user_cmd = np.array([vx, vy, wz], dtype=float)
                    if loop_on and outer is not None:
                        cmd = outer.step(user_cmd, d, self)
                    else:
                        cmd = user_cmd
                    cmd_now = (float(cmd[0]), float(cmd[1]), float(cmd[2]))
                    command = np.array([cmd[0] * 1.5, cmd[1] * 0.8, cmd[2] * 2 * math.pi],
                                       dtype=np.float32)
                    obs = self._obs(command)
                    raw = list(self.sess.run(["continuous_actions"], {"obs": obs.reshape(1, -1)}))
                    act = np.asarray(raw[0], dtype=np.float32).reshape(-1)
                    self.last_action = act.copy()
                    d.ctrl[:] = act * ACTION_SCALE + self.default_angles
                    self.phase = np.fmod(self.phase + phase_dt + np.pi, 2 * math.pi) - np.pi
                    self._apply_arms(float(d.time), reach)
                    if waist_target is not None:
                        delta = float(waist_target) - self.waist_hold
                        self.waist_hold += float(np.clip(delta, -IK_JOINT_RATE * TICK_DT,
                                                         IK_JOINT_RATE * TICK_DT))
                        d.ctrl[self.waist_act] = self.waist_hold
                    if reach:
                        reach_max = max(reach_max, reach_ramp(float(d.time)))

                mujoco.mj_step(m, d)
                xm = d.body(self.torso_bid).xmat.reshape(3, 3)
                tilt = math.degrees(math.acos(max(-1.0, min(1.0, float(xm[2, 2])))))
                max_tilt = max(max_tilt, tilt)
                min_z = min(min_z, float(d.qpos[2]))

                if self.counter % SAMPLE_EVERY == 0:
                    s = self._sample(cmd_now[2], arm_mode,
                                     waist_target if waist_target is not None else "policy")
                    s.update({"segment": si, "mode": mode, "loop_on": loop_on,
                              "cmd_vx": cmd_now[0], "cmd_vy": cmd_now[1], "cmd_wz": cmd_now[2],
                              "reach_cmd": reach_max})
                    samples.append(s)

                if d.qpos[2] < 0.6 * z_start or tilt > 60.0:
                    fall = {"time": float(d.time), "segment": si, "mode": mode,
                            "tilt_deg": tilt, "base_z": float(d.qpos[2]),
                            "segment_elapsed": float(d.time - seg_start)}
                    seg_fell = True
                    break

            seg_samples = [s for s in samples if s["segment"] == si]
            seg_records.append(self._seg_summary(
                si, mode, dur, seg_samples, float(d.time - seg_start), max_tilt, min_z,
                reach_max, seg_fell))
            if fall and stop_on_fall:
                break

        if samples:
            s = self._sample(cmd_now[2], arm_mode,
                             waist_target if waist_target is not None else "policy")
            s.update({"segment": len(segments) - 1, "mode": segments[-1].get("mode", "walk"),
                      "loop_on": bool(segments[-1].get("loop_on", False)),
                      "cmd_vx": cmd_now[0], "cmd_vy": cmd_now[1], "cmd_wz": cmd_now[2],
                      "reach_cmd": seg_records[-1]["reach_max_m"]})
            if abs(samples[-1]["t"] - s["t"]) > 1e-9:
                samples.append(s)

        metrics = self._metrics(samples, float(d.time), yaw0, x0)
        metrics["fell"] = bool(fall is not None or metrics["fell"])
        metrics.update({"label": label, "seconds": float(d.time), "arms": arm_mode,
                        "waist": ("policy" if waist_target is None else float(waist_target)),
                        "heading_travel_deg": heading_travel_deg(samples),
                        "model": MODEL_NAME})
        out = {"metrics": metrics, "segments": seg_records, "samples": samples, "fall": fall,
               "arm_mode": arm_mode, "reach": bool(reach),
               "waist_target_rad": waist_target,
               "loop": (outer.label if outer is not None else "off")}
        if log_prefix:
            self._write_csv(log_prefix, samples)
            with open(LOGS / f"{log_prefix}.json", "w") as f:
                json.dump(out, f, indent=1)
        return out

    def _seg_summary(self, si, mode, dur, ss, actual_dur, max_tilt, min_z, reach_max, fell):
        if ss:
            s0, s1 = ss[0], ss[-1]
            dxy = math.hypot(s1["base_x"] - s0["base_x"], s1["base_y"] - s0["base_y"])
            hxy = math.hypot(s1["head_x"] - s0["head_x"], s1["head_y"] - s0["head_y"])
            yaw = np.unwrap(np.radians([s["head_yaw_deg"] for s in ss]))
            yaw_travel = math.degrees(yaw[-1] - yaw[0])
            end_tilt = s1["torso_tilt_deg"]
            end_z = s1["base_z"]
            iks = [max(abs(s["ik_res_l"]), abs(s["ik_res_r"])) for s in ss
                   if np.isfinite(s["ik_res_l"])]
            max_ik = float(max(iks)) if iks else None
        else:
            dxy = hxy = yaw_travel = end_tilt = end_z = float("nan")
            max_ik = None
        return {"segment": si, "mode": mode, "duration_s": dur, "actual_duration_s": actual_dur,
                "fell": bool(fell), "xy_move_m": dxy,
                "path_speed_mps": (dxy / actual_dur) if actual_dur > 0 else float("nan"),
                "head_xy_move_m": hxy, "heading_travel_deg": yaw_travel,
                "max_tilt_deg": max_tilt, "min_base_z": min_z,
                "end_tilt_deg": end_tilt, "end_base_z": end_z,
                "reach_max_m": reach_max if reach_max > 0 else 0.0,
                "max_ik_residual_m": max_ik}


class PIStation:
    """PI on (vx, vy, yaw) for zero-command station keeping.

    Output clamps are caller-configured; the C3g runs use vx,vy in
    [-1.0, 1.0] m/s and wz in [-0.6, 0.6] rad/s.  Integrator clamps:
    vx,vy in [-0.20, 0.20] m/s; yaw in [-0.40, 0.40] rad/s.
    Anti-windup: conditional (an integrator does not accumulate further while its
    output is saturated in the direction of the error) plus a hard clamp.
    Integrators are reset on every engage/disengage so no windup crosses modes.

    The vy channel is implemented but is NOT closed in the C3g sweep: a P-only
    vy loop at kv_vy=1.0 falls at 5.7 s, and kv_vy=0.2 survives but does not
    improve creep (1.356 m vs 1.341 m P-only).  The sweep integrates vx only.
    """

    def __init__(self, ky=4.0, kv_vx=1.0, kv_vy=1.0, ki_vx=0.0, ki_vy=0.0, ki_yaw=0.0,
                 vx_lim=0.25, vy_lim=0.25, wz_lim=0.6,
                 i_v_lim=0.20, i_yaw_lim=0.40, label=""):
        self.ky, self.kv_vx, self.kv_vy = ky, kv_vx, kv_vy
        self.ki_vx, self.ki_vy, self.ki_yaw = ki_vx, ki_vy, ki_yaw
        self.vx_lim, self.vy_lim, self.wz_lim = vx_lim, vy_lim, wz_lim
        self.i_v_lim, self.i_yaw_lim = i_v_lim, i_yaw_lim
        self.label = label or f"PI ky={ky:g} kv={kv_vx:g} ki={ki_vx:g}"
        self.reset()

    def reset(self):
        self.Ivx = self.Ivy = self.Iyaw = 0.0
        self.target_yaw = None
        self.on = False

    def engage(self, on):
        on = bool(on)
        if on != self.on:
            self.on = on
            self.Ivx = self.Ivy = self.Iyaw = 0.0
            self.target_yaw = None

    def step(self, user_cmd, d, sim):
        if self.target_yaw is None:
            self.target_yaw = yaw_q(np.array(d.qpos[3:7]))
        yaw = yaw_q(np.array(d.qpos[3:7]))
        vx = float(d.sensor("local_linvel_pelvis").data[0])
        vy = float(d.sensor("local_linvel_pelvis").data[1])
        evx = user_cmd[0] - vx
        evy = user_cmd[1] - vy
        eyaw = wrap(self.target_yaw - yaw)
        dt = CTRL_DT

        # conditional anti-windup
        prev = (self.Ivx, self.Ivy, self.Iyaw)
        self.Ivx += self.ki_vx * evx * dt
        self.Ivy += self.ki_vy * evy * dt
        self.Iyaw += self.ki_yaw * eyaw * dt
        ux_try = user_cmd[0] + self.kv_vx * evx + self.Ivx
        uy_try = user_cmd[1] + self.kv_vy * evy + self.Ivy
        uz_try = user_cmd[2] + self.ky * eyaw + self.Iyaw
        if (ux_try > self.vx_lim and evx > 0) or (ux_try < -self.vx_lim and evx < 0):
            self.Ivx = prev[0]
        if (uy_try > self.vy_lim and evy > 0) or (uy_try < -self.vy_lim and evy < 0):
            self.Ivy = prev[1]
        if (uz_try > self.wz_lim and eyaw > 0) or (uz_try < -self.wz_lim and eyaw < 0):
            self.Iyaw = prev[2]
        self.Ivx = clamp(self.Ivx, -self.i_v_lim, self.i_v_lim)
        self.Ivy = clamp(self.Ivy, -self.i_v_lim, self.i_v_lim)
        self.Iyaw = clamp(self.Iyaw, -self.i_yaw_lim, self.i_yaw_lim)
        ux = clamp(user_cmd[0] + self.kv_vx * evx + self.Ivx, -self.vx_lim, self.vx_lim)
        uy = clamp(user_cmd[1] + self.kv_vy * evy + self.Ivy, -self.vy_lim, self.vy_lim)
        uz = clamp(user_cmd[2] + self.ky * eyaw + self.Iyaw, -self.wz_lim, self.wz_lim)
        return (ux, uy, uz)


# ---------------------------------------------------------------------------
# Reporting helpers
# ---------------------------------------------------------------------------

def base_yaw_travel_deg(out):
    ss = out.get("samples", [])
    if not ss:
        return float("nan")
    yaw = np.unwrap(np.radians([s["base_yaw_deg"] for s in ss]))
    return math.degrees(yaw[-1] - yaw[0])


def run_summary(out):
    m = out["metrics"]
    return {
        "label": m["label"], "seconds": m["seconds"], "fell": m["fell"],
        "fall": out["fall"], "dx": m["dx"], "dy": m["dy"],
        "forward_m": m["forward_m"], "path_m": math.hypot(m["dx"], m["dy"]),
        "forward_speed_mps": m["forward_speed_mps"],
        "path_speed_mps": math.hypot(m["dx"], m["dy"]) / m["seconds"] if m["seconds"] else float("nan"),
        "heading_travel_deg": m["heading_travel_deg"],
        "base_yaw_travel_deg": base_yaw_travel_deg(out),
        "heading_end_deg": m["head_yaw_end_deg"],
        "max_tilt_deg": m["max_tilt_deg"], "end_tilt_deg": m["end_tilt_deg"],
        "torso_pitch_std_deg": m["torso_pitch_std_deg"],
        "max_ik_residual_m": m["max_ik_residual_m"],
        "head_rel_base_yaw_end_deg": m["head_rel_base_yaw_end_deg"],
        "waist_yaw_end_deg": math.degrees(m["waist_yaw_end"]),
        "waist_yaw_max_abs_deg": math.degrees(m["waist_yaw_max_abs"]),
        "base_creep_m": math.hypot(m["dx"], m["dy"]),
        "head_creep_xy_m": m["head_disp_xy_m"],
        "reach_max_m": max((s["reach_max_m"] for s in out["segments"]), default=0.0),
    }


def station_summary(out, window=5.0):
    sm = run_summary(out)
    ss = out["samples"]
    t = np.array([s["t"] for s in ss])
    bx = np.array([s["base_x"] for s in ss])
    by = np.array([s["base_y"] for s in ss])
    x0, y0 = bx[0], by[0]
    drift = np.hypot(bx - x0, by - y0)
    mask = t >= t[-1] - window
    if mask.sum() >= 3:
        tt = t[mask]
        dt = np.maximum(np.diff(tt), 1e-9)
        vx = np.diff(bx[mask]) / dt
        vy = np.diff(by[mask]) / dt
        speed = np.hypot(vx, vy)
        sm["last5s_speed_rms_mps"] = float(np.sqrt(np.mean(speed ** 2)))
        sm["last5s_speed_max_mps"] = float(np.max(speed))
    else:
        sm["last5s_speed_rms_mps"] = sm["last5s_speed_max_mps"] = float("nan")
    sm["max_drift_m"] = float(np.max(drift))
    sm["drift_table"] = drift_table(ss)
    return sm


def show(tag, sm):
    print(f"[{tag}] fell={sm['fell']} fall={sm['fall']} "
          f"fwd_spd={sm['forward_speed_mps']:.3f} path={sm['path_speed_mps']:.3f} "
          f"heading={sm['heading_travel_deg']:+.1f}deg tilt={sm['max_tilt_deg']:.1f} "
          f"pitch_std={sm['torso_pitch_std_deg']:.2f} ik={sm['max_ik_residual_m']}", flush=True)


# ---------------------------------------------------------------------------
# C3f
# ---------------------------------------------------------------------------

def run_c3f(sim):
    out = {"model": MODEL_NAME, "speed_mps": 0.5, "duration_s": 30.0}
    variants = [
        ("F0", "policy", False, "policy owns all 29 (baseline)"),
        ("F1", "ik_hold", False, "wrists hard DLS IK at neutral"),
        ("F2", "ik_hold", True, "wrists hard DLS IK, left reach +0.10 m (1-3 s, hold)"),
        ("F3", "soft", True, f"wrists soft posture blend alpha={SOFT_ALPHA:g}, same reach"),
    ]
    for tag, arm_mode, reach, desc in variants:
        r = sim.run_program(f"C3f {tag}: {desc}", [{"mode": "walk", "dur": 30.0, "vx": 0.5}],
                            arm_mode=arm_mode, reach=reach, log_prefix=f"c3f_{tag}")
        out[tag] = {"summary": run_summary(r), "segments": r["segments"], "desc": desc}
        show(tag, out[tag]["summary"])

    r = sim.run_program("C3f F1-zero: hard DLS IK at neutral, zero command 20 s",
                        [{"mode": "hold", "dur": 20.0, "vx": 0.0}],
                        arm_mode="ik_hold", log_prefix="c3f_F1_zero20")
    out["F1_zero20"] = {"summary": station_summary(r), "segments": r["segments"],
                        "desc": "F1 arms, zero command 20 s"}
    show("F1_zero20", out["F1_zero20"]["summary"])
    out["F1_zero20"]["drift_table"] = out["F1_zero20"]["summary"]["drift_table"]
    return out


# ---------------------------------------------------------------------------
# C3c re-test
# ---------------------------------------------------------------------------

def run_c3c_retest(sim):
    out = {"model": MODEL_NAME, "waist_target_deg": 30.0, "speed_mps": 0.5, "duration_s": 10.0}
    r = sim.run_program("C3c-retest G1: walk 0.5 m/s, waist_yaw -> +30 deg, arms policy",
                        [{"mode": "walk", "dur": 10.0, "vx": 0.5}], arm_mode="policy",
                        waist_target=math.radians(30.0), log_prefix="c3c_G1_waist30")
    out["G1"] = {"summary": run_summary(r), "segments": r["segments"],
                 "desc": "waist +30 deg, arms policy"}
    show("G1", out["G1"]["summary"])

    r = sim.run_program("C3c-retest G2: walk 0.5 m/s, waist_yaw -> +30 deg, arms hard DLS IK",
                        [{"mode": "walk", "dur": 10.0, "vx": 0.5}], arm_mode="ik_hold",
                        waist_target=math.radians(30.0), log_prefix="c3c_G2_waist30_armsIK")
    out["G2"] = {"summary": run_summary(r), "segments": r["segments"],
                 "desc": "waist +30 deg, arms hard DLS IK"}
    show("G2", out["G2"]["summary"])
    return out


# ---------------------------------------------------------------------------
# C3g
# ---------------------------------------------------------------------------

def _station_run(sim, name, ki_vx=0.0, ki_vy=0.0, ki_yaw=0.0, kv_vy=0.0, ky=4.0,
                 kv_vx=1.0, dur=20.0, prefix=None):
    ctrl = PIStation(ky=ky, kv_vx=kv_vx, kv_vy=kv_vy, ki_vx=ki_vx, ki_vy=ki_vy,
                     ki_yaw=ki_yaw, vx_lim=1.0, vy_lim=1.0,
                     label=name)
    r = sim.run_program(name,
                        [{"mode": "hold", "dur": dur, "vx": 0.0, "loop_on": True}],
                        outer=ctrl, log_prefix=prefix or name.replace(" ", "_"))
    sm = station_summary(r)
    sm.update({"ki_vx": ki_vx, "ki_vy": ki_vy, "ki_yaw": ki_yaw, "kv_vy": kv_vy,
               "ky": ky, "kv_vx": kv_vx})
    return sm


def run_c3g(sim, best_ki_hint=0.5):
    out = {"model": MODEL_NAME, "duration_s": 20.0,
           "control_law": ("vx_cmd=clamp(kv_vx*(0-vx_meas)+I_vx, +-1.0); "
                           "vy_cmd=clamp(kv_vy*(0-vy_meas)+I_vy, +-1.0); "
                           "wz_cmd=clamp(ky*(yaw_ref-yaw)+I_yaw, +-0.6)"),
           "p_gains": {"ky": 4.0, "kv_vx": 1.0, "kv_vy": 0.0},
           "output_clamp": {"vx": [-1.0, 1.0], "vy": [-1.0, 1.0], "wz": [-0.6, 0.6]},
           "integrator_clamp": {"v": [-0.20, 0.20], "yaw": [-0.40, 0.40]},
           "anti_windup": "conditional on output saturation + hard clamp; reset on engage",
           "sweep_note": ("vy loop at kv_vy=1.0 falls at 5.7 s; a SMALL vy loop "
                          "(kv_vy=0.2, ki_vy=0.15) is stable and cuts lateral drift. "
                          "The main sweep is vx-only; see vy_sweep and combined_best."),
           "combined_gains": {"ky": 4.0, "kv_vx": 1.0, "kv_vy": 0.2,
                              "ki_vx": 0.5, "ki_vy": 0.15, "ki_yaw": 0.0}}

    # Full vx-integral sweep at the previously stable P gains (ky=4, kv=1).
    rows = []
    for ki in (0.0, 0.05, 0.1, 0.2, 0.3, 0.5, 1.0):
        sm = _station_run(sim, f"C3g vx-integral ki={ki:g}, 20 s", ki_vx=ki,
                          prefix=f"c3g_ki{ki:g}")
        rows.append(sm)
        show(f"ki={ki:g}", sm)
    out["sweep"] = rows

    # Small vy-integral sub-sweep at ki_vx=0.5.  A full vy P loop (kv_vy=1.0)
    # falls; the stable region is a small vy gain plus a small vy integral.
    vy_rows = []
    for kvv, kivy in ((0.10, 0.10), (0.15, 0.10), (0.20, 0.05), (0.20, 0.10),
                      (0.20, 0.15), (0.25, 0.10), (0.30, 0.10)):
        sm = _station_run(sim, f"C3g vy kv_vy={kvv:g} ki_vy={kivy:g}, 20 s",
                          ki_vx=0.5, kv_vy=kvv, ki_vy=kivy,
                          prefix=f"c3g_vy_kv{kvv:g}_ki{kivy:g}")
        vy_rows.append(sm)
        show(f"vy kv={kvv:g} ki={kivy:g}", sm)
    out["vy_sweep"] = vy_rows

    # Channel probes: yaw integral alone, and the vy loop at two gains.
    probes = {}
    probes["yaw_integral_0.1"] = _station_run(
        sim, "C3g yaw integral only ki_yaw=0.1, 20 s", ki_yaw=0.1,
        prefix="c3g_yaw_int")
    probes["vy_loop_kv1.0"] = _station_run(
        sim, "C3g P-only vy loop kv_vy=1.0, 20 s", kv_vy=1.0, prefix="c3g_vy_kv1")
    probes["vy_loop_kv0.2"] = _station_run(
        sim, "C3g P-only vy loop kv_vy=0.2, 20 s", kv_vy=0.2, prefix="c3g_vy_kv02")
    out["probes"] = probes
    for k, v in probes.items():
        show(k, v)

    ok = [r for r in rows if not r["fell"]]
    pool = ok if ok else rows
    best = min(pool, key=lambda r: (round(r["base_creep_m"], 3),
                                    round(abs(r["heading_travel_deg"]), 2)))
    out["best_20s"] = best
    print("BEST20", best["ki_vx"], best["base_creep_m"], best["heading_travel_deg"], flush=True)

    # 60 s runs: literal best, the margin-retaining candidate, and P-only control.
    runs60 = {}
    for tag, ki in (("best_20s", best["ki_vx"]), ("ki0.5", 0.5), ("p_only", 0.0)):
        sm = _station_run(sim, f"C3g {tag} 60 s", ki_vx=ki, dur=60.0,
                          prefix=f"c3g_{tag}_60s")
        sm["segments"] = [{"mode": "hold", "dur": 60.0}]
        runs60[tag] = sm
        show(f"60s {tag}", sm)
    out["runs_60s"] = runs60

    # One-step margins for the literal best and for the margin-retaining ki=0.5.
    margins = {}
    for tag, ki in (("best_20s", best["ki_vx"]), ("ki0.5", 0.5)):
        margins[tag] = {}
        for name, ky, kv, ki_m in (
            ("double_ki", 4.0, 1.0, 2.0 * ki),
            ("double_kv", 4.0, 2.0, ki),
            ("double_ky", 8.0, 1.0, ki),
        ):
            sm = _station_run(sim, f"C3g {tag} margin {name}", ki_vx=ki_m,
                              ky=ky, kv_vx=kv, prefix=f"c3g_{tag}_margin_{name}")
            sm["name"] = name
            margins[tag][name] = sm
            show(f"{tag} {name}", sm)
    out["margins"] = margins

    # Combined vx+vy PI: the best steady-state hold found.  ki_vx=0.5,
    # kv_vy=0.2, ki_vy=0.15.  Run 20/60/120 s and one-step margins.
    comb = _station_run(sim, "C3g combined ki_vx=0.5 kv_vy=0.2 ki_vy=0.15, 20 s",
                        ki_vx=0.5, kv_vy=0.2, ki_vy=0.15,
                        prefix="c3g_combined_20s")
    out["combined_best"] = comb
    show("combined 20s", comb)
    comb_runs = {}
    for dur in (60.0, 120.0):
        sm = _station_run(sim, f"C3g combined {dur:g} s", ki_vx=0.5, kv_vy=0.2,
                          ki_vy=0.15, dur=dur, prefix=f"c3g_combined_{dur:g}s")
        comb_runs[f"{dur:g}s"] = sm
        show(f"combined {dur:g}s", sm)
    out["combined_runs"] = comb_runs
    out["vxonly_120s"] = _station_run(
        sim, "C3g vx-only ki=0.5, 120 s", ki_vx=0.5, dur=120.0,
        prefix="c3g_vxonly_120s")
    show("vx-only 120s", out["vxonly_120s"])
    comb_margins = {}
    for name, ky, kvx, kvy, kivx, kivy in (
        ("double_ki", 4.0, 1.0, 0.2, 1.0, 0.15),
        ("double_kv_vx", 4.0, 2.0, 0.2, 0.5, 0.15),
        ("double_kv_vy", 4.0, 1.0, 0.4, 0.5, 0.15),
        ("double_ky", 8.0, 1.0, 0.2, 0.5, 0.15),
    ):
        sm = _station_run(sim, f"C3g combined margin {name}", ki_vx=kivx,
                          kv_vy=kvy, ki_vy=kivy, ky=ky, kv_vx=kvx,
                          prefix=f"c3g_combined_margin_{name}")
        sm["name"] = name
        comb_margins[name] = sm
        show(f"combined {name}", sm)
    out["combined_margins"] = comb_margins

    # Two carried configs for C3h: the best steady-state hold (combined) and the
    # vx-only config, which has a wider walk->hold settle window (C3h §5.2).
    out["carried_steady"] = {"ki_vx": 0.5, "kv_vy": 0.2, "ki_vy": 0.15,
                             "ki_yaw": 0.0, "ky": 4.0, "kv_vx": 1.0,
                             "reason": "best steady-state hold; 20 s 0.274 m, 60 s 0.326 m, 120 s 0.361 m; retains double-ki and double-kv_vy margins, loses double-kv_vx and double-ky"}
    out["carried_transition"] = {"ki_vx": 0.5, "kv_vy": 0.0, "ki_vy": 0.0,
                                 "ki_yaw": 0.0, "ky": 4.0, "kv_vx": 1.0,
                                 "reason": "wider walk->hold settle window (0.75-1.25 s) than the combined config; used for C3h"}
    return out


# ---------------------------------------------------------------------------
# C3h
# ---------------------------------------------------------------------------

def _switch_stats(out):
    """Speed just before/after each loop-on/off boundary, and the max speed spike."""
    ss = out["samples"]
    if len(ss) < 4:
        return {}
    t = np.array([s["t"] for s in ss])
    bx = np.array([s["base_x"] for s in ss])
    by = np.array([s["base_y"] for s in ss])
    on = np.array([s["loop_on"] for s in ss])
    dt = np.maximum(np.diff(t), 1e-9)
    speed = np.hypot(np.diff(bx), np.diff(by)) / dt
    tm = 0.5 * (t[1:] + t[:-1])
    edges = np.where(np.diff(on.astype(int)) != 0)[0]
    rows = []
    for e in edges:
        before = speed[(tm >= t[e] - 0.5) & (tm <= t[e])]
        after = speed[(tm >= t[e]) & (tm <= t[e] + 0.5)]
        rows.append({
            "t": float(t[e]), "to_loop_on": bool(on[e + 1]),
            "speed_before_mean": float(np.mean(before)) if len(before) else float("nan"),
            "speed_after_mean": float(np.mean(after)) if len(after) else float("nan"),
            "speed_after_max": float(np.max(after)) if len(after) else float("nan"),
        })
    return {"edges": rows, "overall_speed_max": float(np.max(speed))}


def run_c3h(sim, best_ki, arm_mode="policy"):
    out = {"model": MODEL_NAME, "ki": best_ki,
           "p_gains": {"ky": 4.0, "kv_vx": 1.0, "kv_vy": 0.0},
           "output_clamp": {"vx": [-1.0, 1.0], "vy": [-1.0, 1.0], "wz": [-0.6, 0.6]},
           "switch_threshold": "external command decides the schedule; loop ON only in hold "
                               "segments (|vx_cmd|<0.05, |vy_cmd|<0.05, |wz_cmd|<0.05). "
                               "Same gains in walk and hold; no policy swap, no integrator "
                               "carry across engage (reset on engage).",
           "arm_mode": arm_mode}

    def ctrl():
        return PIStation(ky=4.0, kv_vx=1.0, kv_vy=0.0, ki_vx=best_ki, ki_vy=0.0,
                         ki_yaw=0.0, vx_lim=1.0, vy_lim=1.0,
                         label=f"PI ki={best_ki:g} scheduled")

    def ctrl_combined():
        return PIStation(ky=4.0, kv_vx=1.0, kv_vy=0.2, ki_vx=0.5, ki_vy=0.15,
                         ki_yaw=0.0, vx_lim=1.0, vy_lim=1.0,
                         label="PI combined scheduled")

    def schedule(settle=0.0):
        segs = [{"mode": "walk", "dur": 10.0, "vx": 0.5, "loop_on": False}]
        if settle > 0:
            segs.append({"mode": "walk", "dur": float(settle), "vx": 0.0, "loop_on": False})
        segs += [{"mode": "hold", "dur": 20.0, "vx": 0.0, "loop_on": True},
                 {"mode": "walk", "dur": 10.0, "vx": 0.5, "loop_on": False}]
        return segs

    def rec(tag, segs, arms=None, reach=False, ctrl_fn=None):
        r = sim.run_program(tag, segs, arm_mode=arms or arm_mode, reach=reach,
                            outer=(ctrl_fn or ctrl)(),
                            log_prefix="c3h_" + tag.replace(" ", "_"))
        d = {"summary": run_summary(r), "segments": r["segments"],
             "switch": _switch_stats(r), "fall": r["fall"]}
        show(tag, d["summary"])
        return d

    # The task's schedule, unchanged: walk 10 s -> hold 20 s (loop ON) -> walk 10 s.
    out["H1"] = rec("C3h H1: walk10 -> hold20 -> walk10", schedule())
    out["H2"] = rec("C3h H2: (walk10 -> hold20 -> walk10) x3",
                    [seg for _ in range(3) for seg in schedule()])

    # H3: the same schedule with an independent arm controller.  In policy mode
    # the arms are already owned by the policy, so H3 is identical to H1; record
    # it explicitly so the comparison is not silently missing.
    out["H3"] = rec(f"C3h H3: H1 with arms={arm_mode}", schedule(),
                    arms=arm_mode, reach=(arm_mode != "policy"))
    out["H3"]["note"] = ("identical to H1: policy owns the arms" if arm_mode == "policy"
                         else "independent arm controller; reach ramps 1-3 s")

    # Exploratory, TUNED: a short zero-command walk segment before the loop
    # engages lets the policy slow down without swapping controllers.  Report
    # the whole window; it is not part of the untuned H1 result.
    if arm_mode == "policy":
        out["settle_sweep"] = {}
        for z in (0.0, 0.5, 0.75, 1.0, 1.25, 1.5, 2.0):
            out["settle_sweep"][f"{z:g}s"] = rec(f"C3h H1 settle={z:g}s", schedule(z))
        out["H2_settle1.0"] = rec("C3h H2 settle=1.0s x3",
                                  [seg for _ in range(3) for seg in schedule(1.0)])
        out["H3_soft_settle1.0"] = rec("C3h H3 soft settle=1.0s", schedule(1.0),
                                       arms="soft", reach=True)
        out["H3_ik_settle1.0"] = rec("C3h H3 ik_hold settle=1.0s", schedule(1.0),
                                     arms="ik_hold", reach=True)
        out["tuned_settle_s"] = 1.0
        out["tuned_note"] = ("settle window is 0.75-1.25 s and 2.0 s; 0.5 s and 1.5 s fall; "
                             "H2 x3 survives with a 1.0 s settle but heading drifts ~7.5 deg/cycle")
        # The best steady-state C3g config (combined vx+vy) is worse at the
        # transition: H1 falls earlier and the settle window narrows to 0.75/2.0 s.
        out["H1_combined"] = rec("C3h H1 combined gains, no settle", schedule(),
                                 ctrl_fn=ctrl_combined)
        out["combined_settle_sweep"] = {}
        for z in (0.75, 1.0, 1.25, 1.5, 2.0):
            out["combined_settle_sweep"][f"{z:g}s"] = rec(
                f"C3h H1 combined settle={z:g}s", schedule(z), ctrl_fn=ctrl_combined)
    return out


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="all", choices=["all", "c3f", "c3c", "c3g", "c3h"])
    ap.add_argument("--ki", type=float, default=0.5, help="best C3g ki for C3h")
    ap.add_argument("--h-arms", default="policy", choices=["policy", "ik_hold", "soft"])
    args = ap.parse_args()

    LOGS.mkdir(parents=True, exist_ok=True)
    m, sess = load_model("playground")
    defaults, head_gid, arms, waist_qadr, waist_act = _common(m, sess)
    sim = BodySim(m, sess, defaults, arms, head_gid, waist_qadr, waist_act)
    print(f"MODEL {MODEL_NAME} nq={m.nq} nu={m.nu} keyframes="
          f"{[m.keyframe(i).name for i in range(m.nkey)]}")

    out = {}
    if OUT.exists():
        try:
            out = json.loads(OUT.read_text())
        except Exception:
            out = {}

    if args.only in ("all", "c3f"):
        out["c3f"] = run_c3f(sim)
        OUT.write_text(json.dumps(out, indent=1))
    if args.only in ("all", "c3c"):
        out["c3c_retest"] = run_c3c_retest(sim)
        OUT.write_text(json.dumps(out, indent=1))
    if args.only in ("all", "c3g"):
        out["c3g"] = run_c3g(sim)
        OUT.write_text(json.dumps(out, indent=1))
    if args.only in ("all", "c3h"):
        out.setdefault("c3h", {})
        out["c3h"][args.h_arms] = run_c3h(sim, args.ki, arm_mode=args.h_arms)
        OUT.write_text(json.dumps(out, indent=1))
    print("WROTE", OUT)


if __name__ == "__main__":
    main()
