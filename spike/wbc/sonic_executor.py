#!/usr/bin/env python3
"""GEAR-SONIC behind the `BodyCommand` contract.

Lives on the spike side because it needs numpy + onnxruntime + mujoco, while
`superbot/` stays standard-library only. Phase 3 promotes it once the join
works; until then this is the measured adapter, not product code.

The executor owns feasibility: it clamps, solves, and reports what MuJoCo
actually achieved. It never reports what it was asked for.

Measured facts this file is built on (`research/c3m-results.md`,
`research/sonic-gate-results.md`, `research/wbc-policy-proposal.md`):

- Hold is **not** "freeze the current clip". It switches to the standing clip
  `squat_001__A359` frame 0 and freezes the *anchor index* (`freeze=0`), so the
  encoder's 10-frame lookahead never contains walking. Measured hold RMS
  1.3e-4 m/s. Freezing the wrong thing drifted 0.23-2.07 m.
- Wrist aims straight ahead track to 3.51 cm / 7.22 deg; 30 deg off-axis is
  8.63 cm / 28.33 deg. The body must turn to face a target, not aim sideways.
- Arm override *during* walking is not supported by this controller. Only a
  wrist-orientation-only refinement survives (7.22 -> 5.08 deg); the QP joint
  override is dead.

`vr_delta` is a 9-vector (head, left wrist, right wrist - each xyz) added to
the clip's own targets; `vr_orn_delta` is three quats left-multiplied onto
them. That is the only per-tick steering this controller exposes.
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

# numpy/mujoco/onnxruntime live in spike/e3/.venv, not in the system interpreter
# this repo's stdlib-only rule applies to `superbot/`, not to the spikes.
import numpy as np  # pyright: ignore[reportMissingImports]

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import sonic_runner as sr  # noqa: E402

from superbot.body import (  # noqa: E402
    AUTH_CLOSURE,
    AUTH_HEAD,
    AUTH_L_HAND,
    AUTH_R_HAND,
    AUTH_ROOT_HEIGHT,
    AUTH_TRIGGER,
    FLAG_FALL_RISK,
    FLAG_SATURATED,
    FLAG_UNSUPPORTED,
    UNSUPPORTED_IN_V1,
    BodyCommand,
    BodyState,
    Pose,
    commanded_buttons,
)

# Authority this controller can honour today. Walking needs a locomotion clip
# loaded and a live lookahead, so ROOT_VEL is not honoured in a hold session.
SUPPORTED_AUTH = (
    AUTH_HEAD | AUTH_L_HAND | AUTH_R_HAND | AUTH_CLOSURE | AUTH_ROOT_HEIGHT | AUTH_TRIGGER
)

HOLD_CLIP = "squat_001__A359"
HOLD_FRAME = 0

# Measured fall thresholds from the runner: base height and world-up z.
FALL_Z = 0.25
FALL_UP_Z = 0.3

# Point order in the VR 3-point interface, from `sonic_runner.vr_targets`:
# it iterates (BODY_L_WRIST, BODY_R_WRIST, BODY_TORSO). `pts` is flat (9,) and
# `orns` is flat (12,), not (3,3) / (3,4).
L_WRIST, R_WRIST, HEAD = 0, 1, 2

IDENTITY_WXYZ = np.array([1.0, 0.0, 0.0, 0.0])


def _pose_from(pos: np.ndarray, quat_wxyz: np.ndarray) -> Pose:
    """MuJoCo (w,x,y,z) -> contract (x,y,z,w)."""
    w, x, y, z = (float(v) for v in quat_wxyz)
    return Pose(p=(float(pos[0]), float(pos[1]), float(pos[2])), q_xyzw=(x, y, z, w))


class ExecutorUnavailable(RuntimeError):
    """Raised when the controller or its assets are not present."""


class SonicExecutor:
    """One SONIC tick per `step()`. Owns no timing policy of its own."""

    def __init__(self, clip: str = HOLD_CLIP, model: str = "default", verbose: bool = False) -> None:
        self.clip = clip
        self.runner = sr.SonicRunner(mode=1, model=model, verbose=verbose)
        self.motion = sr.load_motion(clip)
        self.hold_frame = HOLD_FRAME
        # Anchor-index freeze: the lookahead must never contain walking.
        self.runner.reset(self.motion, start=self.hold_frame)
        self.ticks = 0
        self.fell = False
        self.last_state: BodyState | None = None

    # ---- geometry -------------------------------------------------------

    def _clip_targets(self) -> tuple[np.ndarray, np.ndarray]:
        return sr.vr_targets(self.motion, self.hold_frame)

    def _achieved(self) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
        """Achieved root-relative head + both wrists, their quats, base and up."""
        r = self.runner
        base = r.data.qpos[0:3].copy()
        root_inv = sr.quat_conj(r.data.qpos[3:7])
        positions, quats = [], []
        for body, offset in (
            ("left_wrist_yaw_link", sr.VR_OFFSETS[0]),
            ("right_wrist_yaw_link", sr.VR_OFFSETS[1]),
            ("torso_link", sr.VR_OFFSETS[2]),
        ):
            bid = r.model.body(body).id
            pos = r.data.xpos[bid] + r.data.xmat[bid].reshape(3, 3) @ offset - base
            positions.append(sr.quat_rotate(root_inv, pos))
            quats.append(sr.quat_mul(root_inv, r.data.xquat[bid]))
        up = sr.quat_rotate(r.data.qpos[3:7], np.array([0.0, 0.0, 1.0]))
        return np.array(positions), np.array(quats), base, up

    def _targets(self, cmd: BodyCommand) -> tuple[np.ndarray | None, np.ndarray | None]:
        """Absolute VR targets, replacing the clip's own, for the commanded points.

        This is the channel the *measured* aim result used. `gate_common.AimTarget`
        overrides `build_encoder_obs` and REPLACES `vr_pos`/`vr_orn` with absolute
        root-relative targets; it recorded 3.51 cm ahead. Steering with additive
        `vr_delta` instead plateaued at 13.8 cm in a 10 s soak - the offset was
        simply not tracked. So: replace, do not nudge.
        """
        pts, orns = self._clip_targets()
        pts = np.asarray(pts, dtype=np.float64).copy()
        orns = np.asarray(orns, dtype=np.float64).copy()
        touched = False

        for bit, index, pose in (
            (AUTH_HEAD, HEAD, cmd.head),
            (AUTH_L_HAND, L_WRIST, cmd.left_hand),
            (AUTH_R_HAND, R_WRIST, cmd.right_hand),
        ):
            if not (cmd.authority & bit) or pose is None:
                continue
            pts[3 * index:3 * index + 3] = np.asarray(pose.p, dtype=np.float64)
            # Contract is xyzw; the controller wants wxyz.
            x, y, z, w = pose.q_xyzw
            orns[4 * index:4 * index + 4] = np.array([w, x, y, z], dtype=np.float64)
            touched = True

        if not touched:
            return None, None
        return pts, orns

    # ---- one tick -------------------------------------------------------

    def step(self, cmd: BodyCommand, now_ns: int = 0) -> BodyState:
        r = self.runner
        vr_pos, vr_orn = self._targets(cmd)

        base_quat = r.data.qpos[3:7].copy()
        enc_obs = r.build_encoder_obs(
            self.motion,
            self.hold_frame,
            base_quat,
            vr_pos=vr_pos,
            vr_orn=vr_orn,
        )
        # onnxruntime annotates run() as returning SparseTensor | Tensor, but it
        # actually returns a list holding one ndarray (checked at runtime), so the
        # index is fine and the stub is what is wrong here.
        tokens = r.enc.run(None, {r.enc_in: enc_obs[None, :]})[0][0]  # pyright: ignore[reportIndexIssue]
        dec_obs = r.build_decoder_obs(tokens)
        action = r.dec.run(None, {r.dec_in: dec_obs[None, :]})[0][0].astype(np.float64)  # pyright: ignore[reportIndexIssue]
        r.step_policy(action)

        entry = r._state_entry(action)  # noqa: SLF001 - spike exposes no public API
        r.hist.append(entry)
        if len(r.hist) > 20:
            r.hist.pop(0)

        positions, quats, base, up = self._achieved()
        self.ticks += 1

        flags = 0
        if base[2] < FALL_Z or up[2] < FALL_UP_Z:
            flags |= FLAG_FALL_RISK
            self.fell = True
        if (cmd.authority & ~SUPPORTED_AUTH) or (cmd.authority & UNSUPPORTED_IN_V1):
            flags |= FLAG_UNSUPPORTED

        residuals = [0.0, 0.0, 0.0]
        for bit, index, pose, slot in (
            (AUTH_HEAD, HEAD, cmd.head, 0),
            (AUTH_L_HAND, L_WRIST, cmd.left_hand, 1),
            (AUTH_R_HAND, R_WRIST, cmd.right_hand, 2),
        ):
            if (cmd.authority & bit) and pose is not None:
                residuals[slot] = float(np.linalg.norm(np.asarray(pose.p) - positions[index]))
                if residuals[slot] > cmd.tolerance_m:
                    flags |= FLAG_SATURATED

        root_q = r.data.qpos[3:7]
        # The body has no fingers and no trigger, so these are the values it was
        # told to hold, not measured contact. They are recomputed every tick from
        # the command in hand: an empty-mask hold therefore reports (0, 0) instead
        # of leaving the last grip closed.
        closure, trigger = commanded_buttons(cmd)
        state = BodyState(
            seq_ack=cmd.seq,
            t_mono_ns=now_ns,
            root_p=(float(base[0]), float(base[1]), float(base[2])),
            root_q=(float(root_q[0]), float(root_q[1]), float(root_q[2]), float(root_q[3])),
            v_xy=(float(r.data.qvel[0]), float(r.data.qvel[1])),
            yaw_rate=float(r.data.qvel[5]),
            q=tuple(map(float, np.asarray(r._q_policy()).ravel())),  # noqa: SLF001
            qd=tuple(map(float, np.asarray(r._dq_policy()).ravel())),  # noqa: SLF001
            head=_pose_from(positions[HEAD], quats[HEAD]),
            left_hand=_pose_from(positions[L_WRIST], quats[L_WRIST]),
            right_hand=_pose_from(positions[R_WRIST], quats[R_WRIST]),
            residual_head_m=residuals[0],
            residual_left_m=residuals[1],
            residual_right_m=residuals[2],
            flags=flags,
            supported=SUPPORTED_AUTH,
            closure=closure,
            trigger=trigger,
            invalid_count=0,
        )
        self.last_state = state
        return state

    # ---- command builders the reel uses --------------------------------

    def look_target(self, yaw_rad: float) -> Pose:
        """The clip's head pose, yawed. Position held so the neck does the work."""
        pts, orns = self._clip_targets()
        q = orns[4 * HEAD:4 * HEAD + 4]
        half = yaw_rad / 2.0
        # Yaw is about Z in a Z-up frame (MuJoCo/OpenXR stage): [w, 0, 0, z].
        # Building it about Y instead rotates pitch/roll and produced ~0.3 deg
        # of torso yaw for a 35 deg command - measured, not guessed.
        q_yaw = np.array([math.cos(half), 0.0, 0.0, math.sin(half)])
        return _pose_from(pts[3 * HEAD:3 * HEAD + 3], sr.quat_mul(q_yaw, q))

    def hand_target(
        self,
        side: str,
        forward_m: float,
        up_m: float = 0.0,
        lateral_m: float = 0.0,
        yaw_deg: float = 0.0,
    ) -> Pose:
        """A wrist pose offset from the clip's wrist, optionally re-oriented.

        The offset is applied in the pelvis frame: +Y is forward, +X is the
        robot's left, +Z is up. `yaw_deg` turns the wrist about Z so an "aim"
        can point somewhere other than the clip's palm direction.
        """
        index = L_WRIST if side == "left" else R_WRIST
        pts, orns = self._clip_targets()
        start = 3 * index
        target = pts[start:start + 3] + np.array([lateral_m, forward_m, up_m])
        quat = orns[4 * index:4 * index + 4]
        if yaw_deg:
            half = math.radians(yaw_deg) / 2.0
            quat = sr.quat_mul(np.array([math.cos(half), 0.0, 0.0, math.sin(half)]), quat)
        return _pose_from(target, quat)

    def aim_target(self, side: str, forward_m: float = 0.22, yaw_deg: float = 0.0) -> Pose:
        """Wrist raised toward the eye line and pointed - the aiming pose.

        Deliberately inside the measured ~0.20 m reliable envelope. Aiming is
        the axis the body is weakest on off-centre (8.63 cm / 28.33 deg at
        30 deg off-axis), which is why the body turns to face first.
        """
        return self.hand_target(side, forward_m=forward_m, up_m=0.12, yaw_deg=yaw_deg)

    def punch_target(self, side: str, forward_m: float = 0.28) -> Pose:
        """Maximum forward wrist reach. E3 measured 9.5 mm at +0.30 m with hard
        IK; under the learned policy this is unmeasured, so read the residual."""
        return self.hand_target(side, forward_m=forward_m, up_m=0.02)

    def dodge_target(self, lateral_m: float) -> Pose:
        """Lateral torso/head offset: a lean, not a step.

        AUTH_ROOT_VEL is not honoured in a hold session, so the base barely moves.
        Measured 2026-10-29 with a 0.14 m command: the head shifted 0.106-0.134 m
        laterally while the base moved 0.008 m or less. That is the whole dodge.

        The offset is on Y, not X. This frame is Z-up with +X forward and +Y left,
        so the first version leaned the head forwards and back while calling it a
        dodge - and the head channel barely honoured that, so the verb moved about
        a centimetre in any direction. Fixing the axis took it to 10-13 cm.
        """
        pts, orns = self._clip_targets()
        start = 3 * HEAD
        target = pts[start:start + 3] + np.array([0.0, lateral_m, -0.04])
        return _pose_from(target, orns[4 * HEAD:4 * HEAD + 4])
