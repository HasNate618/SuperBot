#!/usr/bin/env python3
"""Classical floating-base task-space WBC spike for the Unitree G1.

Usage:
  /home/nate/Projects/SuperBot/spike/e3/.venv/bin/python wbc_spike.py --scenario all

The controller is a weighted task-space IK over the full floating-base model.
Task weights approximate the priority stack:
  feet contact > base/CoM support > head > wrists > posture.
The base is a decision variable in the IK; only the 29 joint targets are
applied to the position actuators. The physical base follows through contact.
Every reported pose is read from mjData after mj_step.
"""

import argparse
import csv
import json
import math
import time
from pathlib import Path

import mujoco
import mink
import numpy as np
from scipy.spatial import ConvexHull

HERE = Path(__file__).resolve().parent
SPIKE = HERE.parent
MODEL_PATH = SPIKE / "e3" / "vendor" / "mujoco_menagerie" / "unitree_g1" / "scene.xml"

DT = 0.02
SUBSTEPS = 10
MAX_JOINT_SPEED = 2.0
AIM_POINT = np.array([0.8, 0.0, 1.0])

DEFAULT_WEIGHTS = {
    "foot_pos": 5000.0,
    "foot_ori": 5000.0,
    "base_pos": 5000.0,
    "base_ori": 5000.0,
    "com": 100.0,
    "head_pos": 10.0,
    "head_ori": 100.0,
    "wrist_pos": 500.0,
    "wrist_ori": 500.0,
    "posture": 0.1,
}


def wrap_deg(a):
    while a > 180.0:
        a -= 360.0
    while a < -180.0:
        a += 360.0
    return a


def quat_to_mat(q_wxyz):
    R = np.zeros(9)
    mujoco.mju_quat2Mat(R, np.asarray(q_wxyz, dtype=float))
    return R.reshape(3, 3)


def Rz(a):
    c, s = math.cos(a), math.sin(a)
    return np.array([[c, -s, 0.0], [s, c, 0.0], [0.0, 0.0, 1.0]])


def rotvec_R(axis, angle):
    axis = np.asarray(axis, dtype=float)
    axis = axis / np.linalg.norm(axis)
    K = np.array(
        [
            [0.0, -axis[2], axis[1]],
            [axis[2], 0.0, -axis[0]],
            [-axis[1], axis[0], 0.0],
        ]
    )
    return np.eye(3) + math.sin(angle) * K + (1.0 - math.cos(angle)) * (K @ K)


def se3(R, p):
    return mink.SE3.from_rotation_and_translation(
        mink.SO3.from_matrix(np.asarray(R, dtype=float)),
        np.asarray(p, dtype=float),
    )


def ori_err_deg(R_target, R_actual):
    Re = np.asarray(R_target, dtype=float).T @ np.asarray(R_actual, dtype=float)
    c = max(-1.0, min(1.0, (np.trace(Re) - 1.0) * 0.5))
    return math.degrees(math.acos(c))


def yaw_of_R(R):
    return math.atan2(R[1, 0], R[0, 0])


def tilt_deg(R):
    c = max(-1.0, min(1.0, float(R[2, 2])))
    return math.degrees(math.acos(c))


def aim_R_min(p_target, R_base, aim_point=AIM_POINT):
    z0 = np.asarray(R_base, dtype=float)[:, 2]
    z1 = np.asarray(aim_point, dtype=float) - np.asarray(p_target, dtype=float)
    z1 = z1 / np.linalg.norm(z1)
    axis = np.cross(z0, z1)
    s = float(np.linalg.norm(axis))
    c = float(np.dot(z0, z1))
    if s < 1e-9:
        return np.asarray(R_base, dtype=float).copy()
    return rotvec_R(axis, math.atan2(s, c)) @ np.asarray(R_base, dtype=float)


def support_margin(p_xy, foot_points):
    if len(foot_points) < 3:
        return float("nan")
    hull = ConvexHull(np.asarray(foot_points, dtype=float))
    vals = (
        hull.equations[:, 0] * p_xy[0]
        + hull.equations[:, 1] * p_xy[1]
        + hull.equations[:, 2]
    )
    return float(-np.max(vals))


def find_head_geom(model):
    mesh_id = model.mesh("head_link").id
    return next(g for g in range(model.ngeom) if model.geom_dataid[g] == mesh_id)


def find_foot_geoms(model):
    foot_bodies = {
        model.body("left_ankle_roll_link").id,
        model.body("right_ankle_roll_link").id,
    }
    return [
        g
        for g in range(model.ngeom)
        if model.geom_type[g] == mujoco.mjtGeom.mjGEOM_SPHERE
        and model.geom_bodyid[g] in foot_bodies
    ]


class G1WBC:
    def __init__(self, model_path=MODEL_PATH, weights=None):
        self.model_path = Path(model_path)
        self.weights = dict(DEFAULT_WEIGHTS)
        if weights:
            self.weights.update(weights)
        self.m = mujoco.MjModel.from_xml_path(str(self.model_path))
        self.d = mujoco.MjData(self.m)
        mujoco.mj_resetDataKeyframe(self.m, self.d, 0)
        mujoco.mj_forward(self.m, self.d)
        self.q0 = self.d.qpos.copy()
        self.config = mink.Configuration(self.m, q=self.q0)
        self.head_gid = find_head_geom(self.m)
        self.foot_gids = find_foot_geoms(self.m)
        self.act_qadr = np.array(
            [self.m.jnt_qposadr[self.m.actuator_trnid[a, 0]] for a in range(self.m.nu)]
        )
        self.head0 = self.config.get_transform_frame_to_world(self.head_gid, "geom")
        self.wrist0 = {
            s: self.config.get_transform_frame_to_world(f"{s}_wrist_yaw_link", "body")
            for s in ("left", "right")
        }
        self.foot0 = {
            s: self.config.get_transform_frame_to_world(f"{s}_ankle_roll_link", "body")
            for s in ("left", "right")
        }
        self.base0 = self.config.get_transform_frame_to_world("pelvis", "body")
        self.com0 = np.array(self.config.data.subtree_com[1], copy=True)
        self._build_tasks()
        self.targets = {}
        self.reset()

    def _build_tasks(self):
        w = self.weights
        self.foot_tasks = {
            s: mink.FrameTask(
                f"{s}_ankle_roll_link",
                "body",
                position_cost=w["foot_pos"],
                orientation_cost=w["foot_ori"],
            )
            for s in ("left", "right")
        }
        self.base_task = mink.FrameTask(
            "pelvis",
            "body",
            position_cost=w["base_pos"],
            orientation_cost=w["base_ori"],
        )
        self.com_task = mink.ComTask(cost=w["com"])
        self.head_task = mink.FrameTask(
            self.head_gid,
            "geom",
            position_cost=w["head_pos"],
            orientation_cost=w["head_ori"],
        )
        self.wrist_tasks = {
            s: mink.FrameTask(
                f"{s}_wrist_yaw_link",
                "body",
                position_cost=w["wrist_pos"],
                orientation_cost=w["wrist_ori"],
            )
            for s in ("left", "right")
        }
        self.posture_task = mink.PostureTask(self.m, cost=w["posture"])
        self.tasks = [
            self.foot_tasks["left"],
            self.foot_tasks["right"],
            self.base_task,
            self.com_task,
            self.head_task,
            self.wrist_tasks["left"],
            self.wrist_tasks["right"],
            self.posture_task,
        ]
        self.limits = [
            mink.ConfigurationLimit(self.m, min_distance_from_limits=0.01),
            mink.VelocityLimit(
                self.m,
                {self.m.joint(j).name: MAX_JOINT_SPEED for j in range(1, self.m.njnt)},
            ),
        ]

    def reset(self):
        mujoco.mj_resetDataKeyframe(self.m, self.d, 0)
        mujoco.mj_forward(self.m, self.d)
        self.config.update(q=self.d.qpos.copy())
        self.targets = {
            "left_foot": self.foot0["left"].copy(),
            "right_foot": self.foot0["right"].copy(),
            "base": self.base0.copy(),
            "com": self.com0.copy(),
            "head": self.head0.copy(),
            "left_wrist": self.wrist0["left"].copy(),
            "right_wrist": self.wrist0["right"].copy(),
        }
        self.set_costs("default")
        self.apply_targets()
        self.solver_failures = 0
        self.last_solve_ms = 0.0

    def set_costs(self, kind):
        w = self.weights
        for s in ("left", "right"):
            self.foot_tasks[s].set_position_cost(w["foot_pos"])
            self.foot_tasks[s].set_orientation_cost(w["foot_ori"])
            self.wrist_tasks[s].set_position_cost(w["wrist_pos"])
            self.wrist_tasks[s].set_orientation_cost(w["wrist_ori"])
        self.base_task.set_position_cost(w["base_pos"])
        self.base_task.set_orientation_cost(w["base_ori"])
        self.com_task.set_cost(w["com"])
        self.posture_task.set_cost(w["posture"])
        head_pos = w["head_pos"]
        head_ori = w["head_ori"]
        if kind == "look":
            head_pos = 0.0
            head_ori = 500.0
        elif kind == "dodge":
            head_pos = 500.0
            head_ori = 500.0
        self.head_task.set_position_cost(head_pos)
        self.head_task.set_orientation_cost(head_ori)

    def apply_targets(self):
        for s in ("left", "right"):
            self.foot_tasks[s].set_target(self.targets[f"{s}_foot"])
            self.wrist_tasks[s].set_target(self.targets[f"{s}_wrist"])
        self.base_task.set_target(self.targets["base"])
        self.com_task.set_target(self.targets["com"])
        self.head_task.set_target(self.targets["head"])
        self.posture_task.set_target(self.q0)

    def step(self):
        self.apply_targets()
        self.config.update(q=self.d.qpos.copy())
        t0 = time.perf_counter()
        try:
            dq = mink.solve_ik(
                self.config,
                self.tasks,
                dt=DT,
                solver="daqp",
                limits=self.limits,
            )
        except Exception:
            self.solver_failures += 1
            dq = np.zeros(self.m.nv)
        self.last_solve_ms = 1000.0 * (time.perf_counter() - t0)
        self.config.integrate_inplace(dq, DT)
        q_des = self.config.q[7:].copy()
        q_cur = self.d.qpos[7:].copy()
        q_cmd = q_cur + np.clip(q_des - q_cur, -MAX_JOINT_SPEED * DT, MAX_JOINT_SPEED * DT)
        for a in range(self.m.nu):
            self.d.ctrl[a] = q_cmd[self.act_qadr[a] - 7]
        for _ in range(SUBSTEPS):
            mujoco.mj_step(self.m, self.d)

    def measure(self):
        base_p = np.array(self.d.qpos[0:3], copy=True)
        base_q = np.array(self.d.qpos[3:7], copy=True)
        base_R = quat_to_mat(base_q)
        head_p = np.array(self.d.geom_xpos[self.head_gid], copy=True)
        head_R = np.array(self.d.geom_xmat[self.head_gid], copy=True).reshape(3, 3)
        com = np.array(self.d.subtree_com[1], copy=True)
        foot_pts = np.array(self.d.geom_xpos[self.foot_gids][:, :2], copy=True)
        rec = {
            "base_p": base_p,
            "base_R": base_R,
            "base_tilt": tilt_deg(base_R),
            "head_p": head_p,
            "head_R": head_R,
            "head_yaw": math.degrees(yaw_of_R(head_R)),
            "head_tilt": tilt_deg(head_R),
            "head_pos_err": float(np.linalg.norm(head_p - self.targets["head"].translation())),
            "head_ori_err": ori_err_deg(
                self.targets["head"].rotation().as_matrix(), head_R
            ),
            "head_target_yaw": math.degrees(
                yaw_of_R(self.targets["head"].rotation().as_matrix())
            ),
            "com": com,
            "margin": support_margin(com[:2], foot_pts),
            "solve_ms": self.last_solve_ms,
            "solver_failures": self.solver_failures,
            "fall": bool(
                base_p[2] < 0.5
                or tilt_deg(base_R) > 30.0
                or np.linalg.norm(
                    base_p[:2] - self.base0.translation()[:2]
                )
                > 0.25
            ),
        }
        for s in ("left", "right"):
            wp = np.array(self.d.body(f"{s}_wrist_yaw_link").xpos, copy=True)
            wR = np.array(self.d.body(f"{s}_wrist_yaw_link").xmat, copy=True).reshape(3, 3)
            wt = self.targets[f"{s}_wrist"]
            fp = np.array(self.d.body(f"{s}_ankle_roll_link").xpos, copy=True)
            fR = np.array(self.d.body(f"{s}_ankle_roll_link").xmat, copy=True).reshape(3, 3)
            f0 = self.foot0[s]
            to_aim = AIM_POINT - wp
            to_aim = to_aim / np.linalg.norm(to_aim)
            aim_dot = float(np.dot(wR[:, 2], to_aim))
            rec[f"{s}_wrist_p"] = wp
            rec[f"{s}_wrist_target_p"] = wt.translation()
            rec[f"{s}_wrist_res"] = float(np.linalg.norm(wp - wt.translation()))
            rec[f"{s}_wrist_ori_err"] = ori_err_deg(
                wt.rotation().as_matrix(), wR
            )
            rec[f"{s}_aim_err"] = math.degrees(
                math.acos(max(-1.0, min(1.0, aim_dot)))
            )
            rec[f"{s}_foot_dev"] = float(np.linalg.norm(fp - f0.translation()))
            rec[f"{s}_foot_z"] = float(fp[2])
            rec[f"{s}_foot_ori_err"] = ori_err_deg(
                f0.rotation().as_matrix(), fR
            )
        return rec


def aggregate(records, wbc):
    base0_xy = np.array(wbc.base0.translation()[:2], copy=True)
    head0 = np.array(wbc.head0.translation(), copy=True)
    base_xy = np.array([r["base_p"][:2] for r in records])
    base_rel = base_xy - base0_xy
    head_rel = np.array([r["head_p"] for r in records]) - head0
    yaw_err = np.array(
        [
            abs(wrap_deg(r["head_yaw"] - r["head_target_yaw"]))
            for r in records
        ]
    )
    solve = np.array([r["solve_ms"] for r in records])
    fall_idx = next((i for i, r in enumerate(records) if r["fall"]), None)
    out = {
        "n_ticks": len(records),
        "base_net_xy_m": float(np.linalg.norm(base_rel[-1])),
        "base_max_xy_m": float(np.max(np.linalg.norm(base_rel, axis=1))),
        "base_max_x_m": float(np.max(np.abs(base_rel[:, 0]))),
        "base_max_y_m": float(np.max(np.abs(base_rel[:, 1]))),
        "head_max_xy_m": float(np.max(np.linalg.norm(head_rel[:, :2], axis=1))),
        "head_max_x_m": float(np.max(np.abs(head_rel[:, 0]))),
        "head_max_y_m": float(np.max(np.abs(head_rel[:, 1]))),
        "head_max_z_m": float(np.max(np.abs(head_rel[:, 2]))),
        "max_base_tilt_deg": float(max(r["base_tilt"] for r in records)),
        "max_head_tilt_deg": float(max(r["head_tilt"] for r in records)),
        "min_support_margin_m": float(min(r["margin"] for r in records)),
        "max_foot_dev_m": float(
            max(
                max(r["left_foot_dev"], r["right_foot_dev"])
                for r in records
            )
        ),
        "max_foot_ori_err_deg": float(
            max(
                max(r["left_foot_ori_err"], r["right_foot_ori_err"])
                for r in records
            )
        ),
        "max_wrist_res_m": float(
            max(max(r["left_wrist_res"], r["right_wrist_res"]) for r in records)
        ),
        "max_wrist_ori_err_deg": float(
            max(
                max(r["left_wrist_ori_err"], r["right_wrist_ori_err"])
                for r in records
            )
        ),
        "max_aim_err_deg": float(
            max(max(r["left_aim_err"], r["right_aim_err"]) for r in records)
        ),
        "max_head_yaw_err_deg": float(np.max(yaw_err)),
        "solve_ms_mean": float(np.mean(solve)),
        "solve_ms_p95": float(np.percentile(solve, 95)),
        "solve_ms_max": float(np.max(solve)),
        "solver_failures": int(records[-1]["solver_failures"]),
        "fall": fall_idx is not None,
        "fall_time_s": (fall_idx * DT) if fall_idx is not None else None,
        "final_head_pos_err_m": float(
            np.linalg.norm(records[-1]["head_p"] - head0)
        ),
        "final_head_tilt_deg": float(records[-1]["head_tilt"]),
        "final_base_net_xy_m": float(np.linalg.norm(base_rel[-1])),
    }
    return out


def write_csv(path, records, scenario):
    path.parent.mkdir(parents=True, exist_ok=True)
    header = [
        "t",
        "scenario",
        "phase",
        "base_x",
        "base_y",
        "base_z",
        "base_tilt_deg",
        "head_x",
        "head_y",
        "head_z",
        "head_yaw_deg",
        "head_tilt_deg",
        "head_target_yaw_deg",
        "head_pos_err",
        "head_ori_err",
        "com_x",
        "com_y",
        "com_z",
        "support_margin_m",
        "lfoot_dev",
        "rfoot_dev",
        "lfoot_ori_err",
        "rfoot_ori_err",
        "lw_tx",
        "lw_ty",
        "lw_tz",
        "lw_x",
        "lw_y",
        "lw_z",
        "lw_res",
        "lw_ori_err",
        "lw_aim_err",
        "rw_tx",
        "rw_ty",
        "rw_tz",
        "rw_x",
        "rw_y",
        "rw_z",
        "rw_res",
        "rw_ori_err",
        "rw_aim_err",
        "solve_ms",
        "fall",
    ]
    with path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        for r in records:
            lwt = r["left_wrist_target_p"]
            lwp = r["left_wrist_p"]
            rwt = r["right_wrist_target_p"]
            rwp = r["right_wrist_p"]
            w.writerow(
                [
                    f"{r['t']:.4f}",
                    scenario,
                    r.get("phase", ""),
                    f"{r['base_p'][0]:.6f}",
                    f"{r['base_p'][1]:.6f}",
                    f"{r['base_p'][2]:.6f}",
                    f"{r['base_tilt']:.4f}",
                    f"{r['head_p'][0]:.6f}",
                    f"{r['head_p'][1]:.6f}",
                    f"{r['head_p'][2]:.6f}",
                    f"{r['head_yaw']:.4f}",
                    f"{r['head_tilt']:.4f}",
                    f"{r['head_target_yaw']:.4f}",
                    f"{r['head_pos_err']:.6f}",
                    f"{r['head_ori_err']:.4f}",
                    f"{r['com'][0]:.6f}",
                    f"{r['com'][1]:.6f}",
                    f"{r['com'][2]:.6f}",
                    f"{r['margin']:.6f}",
                    f"{r['left_foot_dev']:.6f}",
                    f"{r['right_foot_dev']:.6f}",
                    f"{r['left_foot_ori_err']:.4f}",
                    f"{r['right_foot_ori_err']:.4f}",
                    f"{lwt[0]:.6f}",
                    f"{lwt[1]:.6f}",
                    f"{lwt[2]:.6f}",
                    f"{lwp[0]:.6f}",
                    f"{lwp[1]:.6f}",
                    f"{lwp[2]:.6f}",
                    f"{r['left_wrist_res']:.6f}",
                    f"{r['left_wrist_ori_err']:.4f}",
                    f"{r['left_aim_err']:.4f}",
                    f"{rwt[0]:.6f}",
                    f"{rwt[1]:.6f}",
                    f"{rwt[2]:.6f}",
                    f"{rwp[0]:.6f}",
                    f"{rwp[1]:.6f}",
                    f"{rwp[2]:.6f}",
                    f"{r['right_wrist_res']:.6f}",
                    f"{r['right_wrist_ori_err']:.4f}",
                    f"{r['right_aim_err']:.4f}",
                    f"{r['solve_ms']:.4f}",
                    int(r["fall"]),
                ]
            )


def run_hold(wbc, logdir, duration=20.0):
    wbc.reset()
    wbc.set_costs("default")
    records = []
    for k in range(int(round(duration / DT))):
        wbc.step()
        r = wbc.measure()
        r["t"] = k * DT
        r["phase"] = "hold"
        records.append(r)
    write_csv(logdir / "hold_neutral.csv", records, "hold")
    return aggregate(records, wbc)


def run_look(wbc, logdir, duration=20.0):
    wbc.reset()
    wbc.set_costs("look")
    hp0 = wbc.head0.translation()
    Rh0 = wbc.head0.rotation().as_matrix()
    records = []
    for k in range(int(round(duration / DT))):
        t = k * DT
        if t < 2.0:
            yaw = 0.0
        elif t < 4.0:
            yaw = math.radians(35.0) * (t - 2.0) / 2.0
        elif t < 8.0:
            yaw = math.radians(35.0)
        elif t < 12.0:
            yaw = math.radians(35.0) - math.radians(70.0) * (t - 8.0) / 4.0
        elif t < 16.0:
            yaw = math.radians(-35.0)
        elif t < 18.0:
            yaw = math.radians(-35.0) + math.radians(70.0) * (t - 16.0) / 2.0
        else:
            yaw = math.radians(35.0)
        wbc.targets["head"] = se3(Rh0 @ Rz(yaw), hp0)
        wbc.step()
        r = wbc.measure()
        r["t"] = t
        r["phase"] = "look"
        records.append(r)
    write_csv(logdir / "hold_look.csv", records, "look")
    return aggregate(records, wbc)


REACH_TARGETS = [
    ("forward", np.array([0.20, 0.05, 0.05])),
    ("up_out", np.array([0.05, 0.10, 0.18])),
    ("cross", np.array([0.18, -0.10, 0.10])),
]


def run_reach(wbc, logdir, kind="reach", duration=2.0):
    wbc.reset()
    wbc.set_costs("default" if kind == "reach" else "default")
    w0 = {s: np.array(wbc.wrist0[s].translation(), copy=True) for s in ("left", "right")}
    R0 = {
        s: np.array(wbc.wrist0[s].rotation().as_matrix(), copy=True)
        for s in ("left", "right")
    }
    rows = []
    for label, off in REACH_TARGETS:
        for config in ("left", "right", "both"):
            wbc.reset()
            wbc.set_costs("default")
            sides = ("left", "right") if config == "both" else (config,)
            targets = {}
            Rt = {}
            for s in ("left", "right"):
                sign = 1.0 if s == "left" else -1.0
                p = w0[s] + np.array([off[0], off[1] * sign, off[2]])
                targets[s] = p
                Rt[s] = R0[s] if kind == "reach" else aim_R_min(p, R0[s])
            records = []
            for k in range(int(round(duration / DT))):
                t = k * DT
                a = min(1.0, t / 1.5)
                for s in ("left", "right"):
                    if s in sides:
                        p = w0[s] + a * (targets[s] - w0[s])
                        R_use = Rt[s] if kind == "reach" else aim_R_min(p, R0[s])
                        wbc.targets[f"{s}_wrist"] = se3(R_use, p)
                    else:
                        wbc.targets[f"{s}_wrist"] = wbc.wrist0[s].copy()
                wbc.step()
                r = wbc.measure()
                r["t"] = t
                r["phase"] = f"{label}_{config}"
                records.append(r)
            agg = aggregate(records, wbc)
            row = {
                "target": label,
                "config": config,
                "duration_s": duration,
                "base_max_xy_m": agg["base_max_xy_m"],
                "max_base_tilt_deg": agg["max_base_tilt_deg"],
                "min_support_margin_m": agg["min_support_margin_m"],
                "max_foot_dev_m": agg["max_foot_dev_m"],
                "max_wrist_res_m": agg["max_wrist_res_m"],
                "max_wrist_ori_err_deg": agg["max_wrist_ori_err_deg"],
                "max_aim_err_deg": max(
                    r[f"{s}_aim_err"] for r in records for s in sides
                ),
                "fall": agg["fall"],
                "solve_ms_mean": agg["solve_ms_mean"],
                "solver_failures": agg["solver_failures"],
            }
            for s in ("left", "right"):
                row[f"{s}_res_m"] = float(records[-1][f"{s}_wrist_res"])
                row[f"{s}_ori_err_deg"] = float(records[-1][f"{s}_wrist_ori_err"])
                row[f"{s}_aim_err_deg"] = float(records[-1][f"{s}_aim_err"])
                row[f"{s}_target_m"] = [float(x) for x in targets[s]]
                row[f"{s}_achieved_m"] = [
                    float(x) for x in records[-1][f"{s}_wrist_p"]
                ]
            rows.append(row)
            fname = f"{kind}_{label}_{config}.csv"
            write_csv(logdir / fname, records, kind)
    return rows


def run_punch(wbc, logdir, duration=2.0):
    w0 = {s: np.array(wbc.wrist0[s].translation(), copy=True) for s in ("left", "right")}
    R0 = {
        s: np.array(wbc.wrist0[s].rotation().as_matrix(), copy=True)
        for s in ("left", "right")
    }
    rows = []
    for s in ("left", "right"):
        wbc.reset()
        wbc.set_costs("default")
        sign = 1.0 if s == "left" else -1.0
        target = w0[s] + np.array([0.30, 0.06 * sign, 0.10])
        records = []
        for k in range(int(round(duration / DT))):
            t = k * DT
            a = min(1.0, t / 0.5)
            p = w0[s] + a * (target - w0[s])
            Rt = aim_R_min(p, R0[s])
            wbc.targets[f"{s}_wrist"] = se3(Rt, p)
            wbc.step()
            r = wbc.measure()
            r["t"] = t
            r["phase"] = f"punch_{s}"
            records.append(r)
        agg = aggregate(records, wbc)
        rows.append(
            {
                "side": s,
                "target_m": [float(x) for x in target],
                "achieved_m": [float(x) for x in records[-1][f"{s}_wrist_p"]],
                "res_m": float(records[-1][f"{s}_wrist_res"]),
                "ori_err_deg": float(records[-1][f"{s}_wrist_ori_err"]),
                "aim_err_deg": float(records[-1][f"{s}_aim_err"]),
                "max_aim_err_deg": float(
                    max(r[f"{s}_aim_err"] for r in records)
                ),
                "base_max_xy_m": agg["base_max_xy_m"],
                "max_base_tilt_deg": agg["max_base_tilt_deg"],
                "min_support_margin_m": agg["min_support_margin_m"],
                "max_foot_dev_m": agg["max_foot_dev_m"],
                "fall": agg["fall"],
                "solve_ms_mean": agg["solve_ms_mean"],
                "solver_failures": agg["solver_failures"],
            }
        )
        write_csv(logdir / f"punch_{s}.csv", records, "punch")
    return rows


def dodge_profile(t):
    if t < 0.4:
        return t / 0.4
    if t < 0.6:
        return 1.0
    if t < 1.0:
        return 1.0 - (t - 0.6) / 0.4
    return 0.0


def smoothstep(t, t0, t1):
    if t <= t0:
        return 0.0
    if t >= t1:
        return 1.0
    u = (t - t0) / (t1 - t0)
    return u * u * (3.0 - 2.0 * u)


def run_dodge(wbc, logdir):
    hp0 = np.array(wbc.head0.translation(), copy=True)
    Rh0 = wbc.head0.rotation().as_matrix()
    base0 = wbc.base0
    com0 = np.array(wbc.com0, copy=True)
    rows = []
    for amp in (0.10, 0.15, 0.20, 0.25):
        for variant in ("planted", "weight_shift"):
            wbc.reset()
            wbc.set_costs("dodge")
            records = []
            for k in range(int(round(1.5 / DT))):
                t = k * DT
                a = dodge_profile(t)
                wbc.targets["head"] = se3(Rh0, hp0 + np.array([0.0, amp * a, 0.0]))
                if variant == "weight_shift":
                    ys = min(0.5 * amp, 0.06) * a
                    wbc.targets["base"] = se3(
                        base0.rotation().as_matrix(),
                        base0.translation() + np.array([0.0, ys, 0.0]),
                    )
                    wbc.targets["com"] = com0 + np.array([0.0, ys, 0.0])
                else:
                    wbc.targets["base"] = base0.copy()
                    wbc.targets["com"] = com0.copy()
                wbc.step()
                r = wbc.measure()
                r["t"] = t
                r["phase"] = f"dodge_{amp:.2f}_{variant}"
                records.append(r)
            agg = aggregate(records, wbc)
            hy = np.array([r["head_p"][1] - hp0[1] for r in records])
            peak = float(np.max(np.abs(hy)))
            idx90 = next(
                (i for i, v in enumerate(np.abs(hy)) if v >= 0.9 * peak),
                len(hy) - 1,
            )
            idx_peak = int(np.argmax(np.abs(hy)))
            rows.append(
                {
                    "amplitude_m": amp,
                    "variant": variant,
                    "peak_head_y_m": peak,
                    "time_to_90pct_peak_s": idx90 * DT,
                    "time_to_peak_s": idx_peak * DT,
                    "base_max_xy_m": agg["base_max_xy_m"],
                    "max_base_tilt_deg": agg["max_base_tilt_deg"],
                    "max_head_tilt_deg": agg["max_head_tilt_deg"],
                    "min_support_margin_m": agg["min_support_margin_m"],
                    "max_foot_dev_m": agg["max_foot_dev_m"],
                    "final_head_pos_err_m": agg["final_head_pos_err_m"],
                    "final_head_tilt_deg": agg["final_head_tilt_deg"],
                    "fall": agg["fall"],
                    "solve_ms_mean": agg["solve_ms_mean"],
                    "solver_failures": agg["solver_failures"],
                }
            )
            fname = f"dodge_{int(round(amp*100)):02d}_{variant}.csv"
            write_csv(logdir / fname, records, "dodge")
    return rows


def run_shift(wbc, logdir, shifts=(0.06, 0.08, 0.10, 0.12), duration=3.0):
    rows = []
    for shift in shifts:
        for variant in ("default", "soft_base"):
            wbc.reset()
            wbc.set_costs("default")
            if variant == "soft_base":
                wbc.base_task.set_position_cost(500.0)
                wbc.base_task.set_orientation_cost(500.0)
                wbc.com_task.set_cost(500.0)
            records = []
            fell = False
            for k in range(int(round((duration + 0.5) / DT))):
                t = k * DT
                ys = shift * smoothstep(t, 0.0, duration)
                wbc.targets["base"] = se3(
                    wbc.base0.rotation().as_matrix(),
                    wbc.base0.translation() + np.array([0.0, ys, 0.0]),
                )
                wbc.targets["com"] = wbc.com0 + np.array([0.0, ys, 0.0])
                wbc.targets["left_foot"] = wbc.foot0["left"].copy()
                wbc.targets["right_foot"] = wbc.foot0["right"].copy()
                wbc.step()
                r = wbc.measure()
                r["t"] = t
                r["phase"] = f"shift_{shift:.2f}_{variant}"
                records.append(r)
                if r["fall"]:
                    fell = True
                    break
            agg = aggregate(records, wbc)
            rows.append(
                {
                    "shift_m": shift,
                    "variant": variant,
                    "duration_s": duration,
                    "fall": fell,
                    "fall_time_s": agg["fall_time_s"],
                    "max_base_xy_m": agg["base_max_xy_m"],
                    "final_base_y_m": float(
                        records[-1]["base_p"][1] - wbc.base0.translation()[1]
                    ),
                    "final_com_y_m": float(records[-1]["com"][1] - wbc.com0[1]),
                    "max_base_tilt_deg": agg["max_base_tilt_deg"],
                    "min_support_margin_m": agg["min_support_margin_m"],
                    "max_foot_dev_m": agg["max_foot_dev_m"],
                    "solver_failures": agg["solver_failures"],
                }
            )
            fname = f"shift_{int(round(shift*100)):02d}_{variant}.csv"
            write_csv(logdir / fname, records, "shift")
    return rows


def run_lift(wbc, logdir):
    configs = [
        ("shift0.08_lift0.04_fwd0.00", 0.08, 0.04, 0.00),
        ("shift0.10_lift0.04_fwd0.00", 0.10, 0.04, 0.00),
        ("shift0.10_lift0.04_fwd0.10", 0.10, 0.04, 0.10),
        ("shift0.10_lift0.06_fwd0.10", 0.10, 0.06, 0.10),
    ]
    rows = []
    for label, shift, lift_h, fwd in configs:
        wbc.reset()
        wbc.set_costs("default")
        wbc.base_task.set_position_cost(500.0)
        wbc.base_task.set_orientation_cost(500.0)
        wbc.com_task.set_cost(500.0)
        foot0_z = float(wbc.foot0["right"].translation()[2])
        records = []
        fell = False
        for k in range(int(round(6.0 / DT))):
            t = k * DT
            ys = shift * smoothstep(t, 0.0, 3.0)
            s = smoothstep(t, 3.2, 4.2)
            wbc.targets["base"] = se3(
                wbc.base0.rotation().as_matrix(),
                wbc.base0.translation() + np.array([0.0, ys, 0.0]),
            )
            wbc.targets["com"] = wbc.com0 + np.array([0.0, ys, 0.0])
            wbc.targets["left_foot"] = wbc.foot0["left"].copy()
            fr = wbc.foot0["right"]
            wbc.targets["right_foot"] = se3(
                fr.rotation().as_matrix(),
                fr.translation()
                + np.array([fwd * s, 0.0, lift_h * math.sin(math.pi * s)]),
            )
            wbc.step()
            r = wbc.measure()
            r["t"] = t
            r["phase"] = label
            records.append(r)
            if r["fall"]:
                fell = True
                break
        agg = aggregate(records, wbc)
        rows.append(
            {
                "config": label,
                "shift_m": shift,
                "lift_target_m": lift_h,
                "forward_target_m": fwd,
                "fall": fell,
                "fall_time_s": agg["fall_time_s"],
                "max_lift_achieved_m": float(
                    max(r["right_foot_z"] - foot0_z for r in records)
                ),
                "max_base_xy_m": agg["base_max_xy_m"],
                "max_base_tilt_deg": agg["max_base_tilt_deg"],
                "min_support_margin_m": agg["min_support_margin_m"],
                "max_foot_dev_m": agg["max_foot_dev_m"],
                "final_base_y_m": float(
                    records[-1]["base_p"][1] - wbc.base0.translation()[1]
                ),
                "solver_failures": agg["solver_failures"],
            }
        )
        write_csv(logdir / f"lift_{label}.csv", records, "lift")
    return rows


def run_mixed(wbc, logdir, paused=False, tau=0.3):
    wbc.reset()
    wbc.set_costs("default")
    w0 = {s: np.array(wbc.wrist0[s].translation(), copy=True) for s in ("left", "right")}
    R0 = {
        s: np.array(wbc.wrist0[s].rotation().as_matrix(), copy=True)
        for s in ("left", "right")
    }
    hp0 = np.array(wbc.head0.translation(), copy=True)
    Rh0 = np.array(wbc.head0.rotation().as_matrix(), copy=True)
    base0 = wbc.base0
    com0 = np.array(wbc.com0, copy=True)
    base0_xy = np.array(base0.translation()[:2], copy=True)
    reach = {
        "left": w0["left"] + np.array([0.20, 0.05, 0.05]),
        "right": w0["right"] + np.array([0.20, -0.05, 0.05]),
    }
    punch = {
        "left": w0["left"] + np.array([0.30, 0.06, 0.10]),
        "right": w0["right"] + np.array([0.30, -0.06, 0.10]),
    }
    dodge_amp = 0.15
    phases = [
        ("stand", 1.5, None),
        ("look_left", 1.5, ("look", math.radians(35.0))),
        ("look_right", 1.5, ("look", math.radians(-35.0))),
        ("reach_left", 2.0, ("reach", "left")),
        ("reach_right", 2.0, ("reach", "right")),
        ("punch_left", 1.5, ("punch", "left")),
        ("punch_right", 1.5, ("punch", "right")),
        ("dodge_right", 1.5, ("dodge", dodge_amp)),
        ("stand", 1.5, None),
    ]
    if paused:
        expanded = []
        for name, dur, act in phases:
            if expanded and act is not None:
                expanded.append(("neutral", 0.8, None))
            expanded.append((name, dur, act))
        phases = expanded
    alpha = 1.0 - math.exp(-DT / tau)
    wcur = {s: wbc.wrist0[s].copy() for s in ("left", "right")}
    hcur = wbc.head0.copy()
    records = []
    t = 0.0
    for name, dur, act in phases:
        t0 = t
        while t < t0 + dur - 1e-9:
            wbc.set_costs("default")
            des = {s: wbc.wrist0[s].copy() for s in ("left", "right")}
            des_head = wbc.head0.copy()
            base_t = base0.copy()
            com_t = com0.copy()
            if act is not None and act[0] in ("reach", "punch"):
                kind, which = act
                p = reach[which] if kind == "reach" else punch[which]
                R_use = R0[which] if kind == "reach" else aim_R_min(p, R0[which])
                des[which] = se3(R_use, p)
            elif act is not None and act[0] == "look":
                yaw = act[1]
                des_head = se3(Rh0 @ Rz(yaw), hp0)
                wbc.set_costs("look")
            elif act is not None and act[0] == "dodge":
                amp = act[1]
                a = dodge_profile(t - t0)
                des_head = se3(Rh0, hp0 + np.array([0.0, amp * a, 0.0]))
                ys = min(0.5 * amp, 0.06) * a
                base_t = se3(
                    base0.rotation().as_matrix(),
                    base0.translation() + np.array([0.0, ys, 0.0]),
                )
                com_t = com0 + np.array([0.0, ys, 0.0])
                wbc.set_costs("dodge")
            for s in ("left", "right"):
                wcur[s] = wcur[s].interpolate(des[s], alpha)
                wbc.targets[f"{s}_wrist"] = wcur[s]
            hcur = hcur.interpolate(des_head, alpha)
            wbc.targets["head"] = hcur
            wbc.targets["base"] = base_t
            wbc.targets["com"] = com_t
            wbc.step()
            r = wbc.measure()
            r["t"] = t
            r["phase"] = name
            records.append(r)
            if r["fall"]:
                break
            t += DT
        if records[-1]["fall"]:
            break
    fname = "mixed_paused.csv" if paused else "mixed_fast.csv"
    write_csv(logdir / fname, records, "mixed")
    overall = aggregate(records, wbc)
    phases_out = {}
    for name in dict.fromkeys(r["phase"] for r in records):
        rs = [r for r in records if r["phase"] == name]
        phases_out[name] = {
            "ticks": len(rs),
            "base_max_xy_m": float(
                max(np.linalg.norm(r["base_p"][:2] - base0_xy) for r in rs)
            ),
            "min_support_margin_m": float(min(r["margin"] for r in rs)),
            "max_wrist_res_m": float(
                max(max(r["left_wrist_res"], r["right_wrist_res"]) for r in rs)
            ),
            "max_head_yaw_err_deg": float(
                max(
                    abs(wrap_deg(r["head_yaw"] - r["head_target_yaw"]))
                    for r in rs
                )
            ),
            "fall": bool(any(r["fall"] for r in rs)),
        }
    return {"overall": overall, "phases": phases_out}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--scenario",
        default="all",
        choices=[
            "all",
            "hold",
            "look",
            "reach",
            "aim",
            "punch",
            "dodge",
            "shift",
            "lift",
            "mixed",
        ],
    )
    ap.add_argument("--out", default=str(HERE / "logs"))
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    wbc = G1WBC()
    print(
        f"MODEL {wbc.model_path} nq={wbc.m.nq} nv={wbc.m.nv} nu={wbc.m.nu} "
        f"head_gid={wbc.head_gid} foot_gids={wbc.foot_gids}",
        flush=True,
    )
    summary = {"model": str(wbc.model_path), "dt": DT, "substeps": SUBSTEPS}

    def run(name, fn):
        t0 = time.perf_counter()
        result = fn()
        wall = time.perf_counter() - t0
        summary[name] = result
        print(f"\n=== {name.upper()} (wall {wall:.2f} s) ===", flush=True)
        if isinstance(result, dict):
            for k, v in result.items():
                if isinstance(v, dict):
                    print(f"  {k}:", flush=True)
                    for kk, vv in v.items():
                        print(f"    {kk}: {vv}", flush=True)
                else:
                    print(f"  {k}: {v}", flush=True)
        else:
            for row in result:
                print("  " + json.dumps(row, sort_keys=True), flush=True)

    if args.scenario in ("all", "hold"):
        run("hold", lambda: run_hold(wbc, out))
    if args.scenario in ("all", "look"):
        run("look", lambda: run_look(wbc, out))
    if args.scenario in ("all", "reach"):
        run("reach", lambda: run_reach(wbc, out, kind="reach"))
    if args.scenario in ("all", "aim"):
        run("aim", lambda: run_reach(wbc, out, kind="aim"))
    if args.scenario in ("all", "punch"):
        run("punch", lambda: run_punch(wbc, out))
    if args.scenario in ("all", "dodge"):
        run("dodge", lambda: run_dodge(wbc, out))
    if args.scenario in ("all", "shift"):
        run("shift", lambda: run_shift(wbc, out))
    if args.scenario in ("all", "lift"):
        run("lift", lambda: run_lift(wbc, out))
    if args.scenario in ("all", "mixed"):
        run("mixed_paused", lambda: run_mixed(wbc, out, paused=True))
        run("mixed_fast", lambda: run_mixed(wbc, out, paused=False))

    with (out / "summary.json").open("w") as f:
        json.dump(summary, f, indent=2)
    print(f"\nWROTE {out / 'summary.json'}", flush=True)


if __name__ == "__main__":
    main()
