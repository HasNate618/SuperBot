#!/usr/bin/env python3
"""Convert a GEAR-SONIC robot_filtered pkl into the deploy CSV format.

The pkl dof array is in MuJoCo/hardware order; deploy joint_pos.csv is in
IsaacLab/CSV order (G1_MUJOCO_TO_ISAACLAB_DOF). Body order is the 14-body
subset from metadata.txt: pelvis, L/R hip_roll, knee, ankle_roll, torso,
L/R shoulder_roll, elbow, wrist_yaw.
"""
import argparse
import os
import sys

import joblib
import mujoco
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sonic_runner as S

M2I = [0, 6, 12, 1, 7, 13, 2, 8, 14, 3, 9, 15, 22, 4, 10, 16, 23, 5, 11,
       17, 24, 18, 25, 19, 26, 20, 27, 21, 28]
BODY_NAMES = ["pelvis", "left_hip_roll_link", "left_knee_link",
              "left_ankle_roll_link", "right_hip_roll_link", "right_knee_link",
              "right_ankle_roll_link", "torso_link", "left_shoulder_roll_link",
              "left_elbow_link", "left_wrist_yaw_link",
              "right_shoulder_roll_link", "right_elbow_link",
              "right_wrist_yaw_link"]
JOINT_NAMES = [
    "left_hip_pitch_joint", "left_hip_roll_joint", "left_hip_yaw_joint",
    "left_knee_joint", "left_ankle_pitch_joint", "left_ankle_roll_joint",
    "right_hip_pitch_joint", "right_hip_roll_joint", "right_hip_yaw_joint",
    "right_knee_joint", "right_ankle_pitch_joint", "right_ankle_roll_joint",
    "waist_yaw_joint", "waist_roll_joint", "waist_pitch_joint",
    "left_shoulder_pitch_joint", "left_shoulder_roll_joint",
    "left_shoulder_yaw_joint", "left_elbow_joint", "left_wrist_roll_joint",
    "left_wrist_pitch_joint", "left_wrist_yaw_joint",
    "right_shoulder_pitch_joint", "right_shoulder_roll_joint",
    "right_shoulder_yaw_joint", "right_elbow_joint", "right_wrist_roll_joint",
    "right_wrist_pitch_joint", "right_wrist_yaw_joint"]


def slerp(q0, q1, a):
    q0 = q0 / np.linalg.norm(q0)
    q1 = q1 / np.linalg.norm(q1)
    d = np.dot(q0, q1)
    if d < 0:
        q1 = -q1
        d = -d
    if d > 0.9995:
        q = q0 + a * (q1 - q0)
        return q / np.linalg.norm(q)
    th = np.arccos(np.clip(d, -1.0, 1.0))
    return (np.sin((1 - a) * th) * q0 + np.sin(a * th) * q1) / np.sin(th)


def resample(data, src_fps, tgt_fps):
    n = len(data["dof"])
    duration = (n - 1) / src_fps
    t = np.arange(0, duration, 1.0 / tgt_fps)
    idx = np.clip(np.floor(t * src_fps).astype(int), 0, n - 1)
    idx1 = np.minimum(idx + 1, n - 1)
    a = (t * src_fps - idx)[:, None]
    out = {}
    out["dof"] = data["dof"][idx] * (1 - a) + data["dof"][idx1] * a
    out["root_trans"] = data["root_trans_offset"][idx] * (1 - a) + data["root_trans_offset"][idx1] * a
    # pkl root_rot is xyzw -> wxyz for slerp
    q0 = data["root_rot"][idx][:, [3, 0, 1, 2]]
    q1 = data["root_rot"][idx1][:, [3, 0, 1, 2]]
    out["root_quat"] = np.array([slerp(q0[i], q1[i], float(a[i, 0])) for i in range(len(t))])
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pkl")
    ap.add_argument("--name", required=True)
    ap.add_argument("--src-fps", type=float, default=30.0)
    ap.add_argument("--fps", type=float, default=50.0)
    ap.add_argument("--out", default=S.MOTION_ROOT)
    args = ap.parse_args()

    raw = joblib.load(args.pkl)
    key = next(iter(raw))
    data = raw[key]
    if args.src_fps != args.fps:
        data = resample(data, args.src_fps, args.fps)
    else:
        data = dict(dof=data["dof"], root_trans=data["root_trans_offset"],
                    root_quat=data["root_rot"][:, [3, 0, 1, 2]])
    n = len(data["dof"])
    dt = 1.0 / args.fps

    model = mujoco.MjModel.from_xml_path(S.XML)
    mj = mujoco.MjData(model)
    qadr = [model.jnt_qposadr[mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_JOINT, n_)]
            for n_ in JOINT_NAMES]
    bids = [model.body(n_).id for n_ in BODY_NAMES]

    bp = np.zeros((n, 14, 3))
    bq = np.zeros((n, 14, 4))
    for f in range(n):
        mj.qpos[:] = 0
        mj.qpos[0:3] = data["root_trans"][f]
        mj.qpos[3:7] = data["root_quat"][f]
        for j in range(29):
            mj.qpos[qadr[j]] = data["dof"][f, j]
        mujoco.mj_kinematics(model, mj)
        bp[f] = mj.xpos[bids]
        bq[f] = mj.xquat[bids]

    dof_csv = data["dof"][:, M2I]
    jv_mj = np.gradient(data["dof"], dt, axis=0)
    jv_csv = jv_mj[:, M2I]
    bl = np.gradient(bp, dt, axis=0)
    ba = np.zeros_like(bl)
    for f in range(1, n):
        for b in range(14):
            q0 = bq[f - 1, b]
            q1 = bq[f, b]
            if np.dot(q0, q1) < 0:
                q1 = -q1
            dq = np.zeros(4)
            dq[0] = q1[0] - q0[0]
            dq[1:] = q1[1:] - q0[1:]
            w = 2 * dq / dt
            ba[f, b] = w[1:]
    ba[0] = ba[1] if n > 1 else 0

    out = os.path.join(args.out, args.name)
    os.makedirs(out, exist_ok=True)

    def wcsv(path, arr):
        np.savetxt(path, arr, delimiter=",", fmt="%.9g",
                   header=",".join([f"c{i}" for i in range(arr.shape[1])]),
                   comments="")

    wcsv(os.path.join(out, "joint_pos.csv"), dof_csv)
    wcsv(os.path.join(out, "joint_vel.csv"), jv_csv)
    wcsv(os.path.join(out, "body_pos.csv"), bp.reshape(n, -1))
    wcsv(os.path.join(out, "body_quat.csv"), bq.reshape(n, -1))
    wcsv(os.path.join(out, "body_lin_vel.csv"), bl.reshape(n, -1))
    wcsv(os.path.join(out, "body_ang_vel.csv"), ba.reshape(n, -1))
    with open(os.path.join(out, "metadata.txt"), "w") as f:
        f.write(f"Converted from {os.path.basename(args.pkl)}\n")
        f.write(f"Source fps {args.src_fps}, deploy fps {args.fps}, frames {n}\n")
        f.write(f"Body names: {BODY_NAMES}\n")
    print(f"wrote {out}: {n} frames at {args.fps} fps")


if __name__ == "__main__":
    main()
