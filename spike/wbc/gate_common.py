#!/usr/bin/env python3
"""Shared helpers for the SONIC C3i gate and the C3j arm-override experiment.

Two things this module adds on top of `sonic_runner.py`:

1. `build_looped_motion` — a straight-line, periodic reference built from a
   window of `walk_forward_amateur_001__A001`.  The source motion is a 40 s
   out-and-back (root y goes 0 -> -7.7 -> +0.4 m), so it cannot supply 60 s of
   continuous forward walking on its own.  The loop window is chosen so the
   joint-space seam is small; the per-loop residual yaw of the reference is
   cancelled so the reference path is straight rather than creeping.

2. `HookRunner` — a `SonicRunner` whose VR 3-point targets are computed per
   tick by a callback (needed to command a *world*-fixed aim while the base
   moves), plus world-frame aim diagnostics that are recorded after `mj_step`.

Commanded VR targets and achieved `mjData` poses are kept separate everywhere:
targets live in `self.aim_cmd` / `self.last_vr`, achievements live in `self.diag`
and in the rows `SonicRunner.run` already writes.
"""

import os
import sys
from collections.abc import Callable

import mujoco
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import sonic_runner as S  # noqa: E402

I4 = np.array([1.0, 0.0, 0.0, 0.0])
ARM_MJ = list(range(15, 29))  # hardware joint indices of the 14 arm joints


# ---------------------------------------------------------------- quat helpers
def quat_from_mat(R):
    out = np.zeros(4)
    mujoco.mju_mat2Quat(out, np.asarray(R, dtype=np.float64).flatten())
    return out


def quat_angle_deg(a, b):
    return float(np.degrees(2.0 * np.arccos(np.clip(abs(float(np.dot(a, b))), 0.0, 1.0))))


def quat_z(angle):
    return np.array([np.cos(angle / 2.0), 0.0, 0.0, np.sin(angle / 2.0)])


def yaw_of(q):
    w, x, y, z = q
    return float(np.arctan2(2.0 * (w * z + x * y), 1.0 - 2.0 * (y * y + z * z)))


def rot_z(angle):
    c, s = np.cos(angle), np.sin(angle)
    return np.array([[c, -s, 0.0], [s, c, 0.0], [0.0, 0.0, 1.0]])


def look_at_x(direction, up_hint=(0.0, 0.0, 1.0)):
    """World quaternion whose +x axis points along `direction`.

    +x is the pointing axis for both the torso (gaze) and the wrist_yaw_link
    (fingertip direction) in this G1 model: measured dot(fingertip dir, wrist
    +x) = 0.99 and torso +x = walk heading.
    """
    x = np.asarray(direction, dtype=np.float64)
    x = x / np.linalg.norm(x)
    up = np.asarray(up_hint, dtype=np.float64)
    if abs(float(x @ up)) > 0.98:
        up = np.array([1.0, 0.0, 0.0])
    z = up - float(up @ x) * x
    z = z / np.linalg.norm(z)
    y = np.cross(z, x)
    return quat_from_mat(np.column_stack([x, y, z]))


# ------------------------------------------------------------- looped reference
def find_loop_window(motion, f_lo=560, f_hi=1230, min_span=60):
    """Best (f0, f1) with f1-f0 >= min_span whose full body state repeats.

    Cost = max joint-angle mismatch + half the max joint-velocity mismatch +
    yaw mismatch in degrees/90.  Returns (f0, f1, cost breakdown).
    """
    jp, jv, bq = motion["jp"], motion["jv"], motion["bq"]
    best = None
    for f0 in range(f_lo, f_hi, 2):
        for f1 in range(f0 + min_span, min(f_hi + 5, motion["n"]), 2):
            dj = float(np.abs(jp[f0] - jp[f1]).max())
            dv = float(np.abs(jv[f0] - jv[f1]).max())
            dq = quat_angle_deg(bq[f0, 0], bq[f1, 0])
            cost = dj + 0.5 * dv + dq / 90.0
            if best is None or cost < best[0]:
                best = (cost, f0, f1, dj, dv, dq)
    return best


def build_looped_motion(motion, f0, f1, n_frames):
    """Tile frames [f0, f1) into a straight, periodic reference of n_frames.

    Every loop repeats the same joint trajectory and the same body poses, so the
    reference heading is periodic and cannot creep.  Body positions are advanced
    by the loop's own root displacement each period, so the accumulated path is
    a straight line.  The one artefact is the seam: the loop's start and end
    poses differ slightly, which shows up as a small step every `period_s`.
    That residual is reported in the returned `loop` dict.
    """
    P = f1 - f0
    n_loop = int(np.ceil(n_frames / P))
    idx = f0 + (np.arange(n_loop * P) % P)
    step = motion["bp"][f1, 0] - motion["bp"][f0, 0]
    step_z_removed = float(step[2])
    step = step.copy()
    step[2] = 0.0  # repeats forever, so a per-loop height offset would be an endless crouch
    seam_yaw = yaw_of(motion["bq"][f0, 0]) - yaw_of(motion["bq"][f1 - 1, 0])
    loc = motion["bp"][f0:f1].copy()  # absolute, so reset() gets a sane base height
    bp_out = loc[None, :, :, :] + (step * np.arange(n_loop)[:, None])[:, None, None, :]
    out = {
        "jp": motion["jp"][idx].copy(),
        "jv": motion["jv"][idx].copy(),
        "bp": bp_out.reshape(-1, 14, 3),
        "bq": np.tile(motion["bq"][f0:f1], (n_loop, 1, 1)),
        "bl": np.tile(motion["bl"][f0:f1], (n_loop, 1, 1)),
        "ba": np.tile(motion["ba"][f0:f1], (n_loop, 1, 1)),
        "n": n_loop * P,
        "loop": {
            "f0": f0, "f1": f1, "period": P,
            "period_s": P * S.CONTROL_DT,
            "seam_yaw_deg": float(np.degrees(seam_yaw)),
            "per_loop_step": [float(v) for v in step],
            "per_loop_step_z_removed": step_z_removed,
            "per_loop_step_m": float(np.linalg.norm(step)),
            "seam_max_joint_rad": float(np.abs(motion["jp"][f0] - motion["jp"][f1 - 1]).max()),
            "n_loops": n_loop,
        },
    }
    return out


# ------------------------------------------------------------------ aim target
class AimTarget:
    """Both the head and the right wrist point at one world point P.

    The commanded *arm stance* (`wrist_rel`, root-relative) is held fixed so the
    aim does not ride on the gait reference: this is "stand in an aiming pose
    and keep the muzzle on P while the legs walk", not "replay the reference
    arm swing".  The left wrist keeps the reference motion's target and is not
    part of the aim claim.
    """

    def __init__(self, P, wrist_rel=(0.33, -0.06, 0.10), head_rel=(0.0, 0.0, 0.35)):
        self.P = np.asarray(P, dtype=np.float64)
        self.wrist_rel = np.asarray(wrist_rel, dtype=np.float64)
        self.head_rel = np.asarray(head_rel, dtype=np.float64)

    def command(self, motion, frame, base_pos, base_quat, vr_pos, vr_orn):
        R_base = S.quat_to_mat(base_quat)
        R_base_inv = S.quat_conj(base_quat)
        wr_point = base_pos + R_base @ self.wrist_rel
        hd_point = base_pos + R_base @ self.head_rel
        q_wr_world = look_at_x(self.P - wr_point)
        q_hd_world = look_at_x(self.P - hd_point)
        pts = np.array(vr_pos, dtype=np.float64).copy()
        orns = np.array(vr_orn, dtype=np.float64).copy()
        pts[3:6] = self.wrist_rel
        pts[6:9] = self.head_rel
        orns[4:8] = S.quat_mul(R_base_inv, q_wr_world)
        orns[8:12] = S.quat_mul(R_base_inv, q_hd_world)
        self.last = {
            "P": self.P.copy(), "wr_point": wr_point, "hd_point": hd_point,
            "wr_body": wr_point - S.quat_rotate(q_wr_world, S.VR_OFFSETS[1]),
            "wr_q": q_wr_world.copy(), "hd_q": q_hd_world.copy(),
        }
        return pts, orns


# --------------------------------------------------------------------- runners
class HookRunner(S.SonicRunner):
    """SonicRunner with a per-tick VR hook and world-frame aim diagnostics."""

    def __init__(self, mode=1, verbose=False, model="default"):
        super().__init__(mode, verbose=verbose, model=model)
        self.vr_hook: Callable | None = None
        self.aim_cmd: dict | None = None
        self.diag: list = []
        self.base_t: list = []
        self.base_yaw: list = []  # degrees, wrapped
        self.arm_log = []
        self._wr = self.model.body("right_wrist_yaw_link").id
        self._lw = self.model.body("left_wrist_yaw_link").id
        self._tr = self.model.body("torso_link").id

    def build_encoder_obs(self, motion, frame, base_quat, vr_pos=None, vr_orn=None,
                          vr_delta=None, vr_orn_delta=None):
        if self.vr_hook is not None:
            vr_pos, vr_orn = self.vr_hook(motion, frame,
                                          self.data.qpos[0:3].copy(), base_quat)
        return super().build_encoder_obs(motion, frame, base_quat, vr_pos, vr_orn,
                                         vr_delta, vr_orn_delta)

    def _state_entry(self, last_action=None):
        e = super()._state_entry(last_action)
        self.base_t.append(float(self.data.time))
        self.base_yaw.append(float(np.degrees(yaw_of(self.data.qpos[3:7]))))
        if self.aim_cmd is not None:
            self.diag.append(self._aim_diag())
            self.aim_cmd = None
        return e

    def _aim_diag(self):
        d = self.data
        c = self.aim_cmd
        assert c is not None
        R_wr = d.xmat[self._wr].reshape(3, 3)
        p_wr_vr = d.xpos[self._wr] + R_wr @ S.VR_OFFSETS[1]
        R_tr = d.xmat[self._tr].reshape(3, 3)
        p_hd_vr = d.xpos[self._tr] + R_tr @ S.VR_OFFSETS[2]
        q_wr = d.xquat[self._wr].copy()
        q_tr = d.xquat[self._tr].copy()
        P = c["P"]
        return {
            "t": float(d.time),
            "wr_err_pos": float(np.linalg.norm(p_wr_vr - c["wr_point"])),
            "wr_err_orn": quat_angle_deg(q_wr, c["wr_q"]),
            "wr_aim": float(np.degrees(np.arccos(np.clip(
                float(R_wr[:, 0] @ (P - p_wr_vr)) / np.linalg.norm(P - p_wr_vr), -1, 1)))),
            "hd_aim": float(np.degrees(np.arccos(np.clip(
                float(R_tr[:, 0] @ (P - p_hd_vr)) / np.linalg.norm(P - p_hd_vr), -1, 1)))),
            "hd_err_orn": quat_angle_deg(q_tr, c["hd_q"]),
        }

    def reset(self, motion, start=0):
        self.diag = []
        self.base_t = []
        self.base_yaw = []
        self.arm_log = []
        super().reset(motion, start=start)


# ------------------------------------------------------------------- metrics
KEYS_1D = ("t", "x", "y", "z", "up_z", "ref_x", "ref_y", "q_err")
KEYS_3D = ("lw", "rw", "hw", "lw_q", "rw_q", "torso_q")


def series(rows):
    out = {k: np.array([r[k] for r in rows]) for k in KEYS_1D}
    out.update({k: np.array([r[k] for r in rows]) for k in KEYS_3D})
    out["vr_t"] = np.array([r["vr_t"] for r in rows]).reshape(-1, 3, 3)
    out["vr_o"] = np.array([r["vr_o"] for r in rows]).reshape(-1, 3, 4)
    return out


def tilt_deg(up_z):
    return float(np.degrees(np.arccos(np.clip(up_z, -1.0, 1.0))))


def heading_deg(a):
    return float(np.degrees(np.unwrap(np.arctan2(a["y"] - a["y"][0], a["x"] - a["x"][0]))))
