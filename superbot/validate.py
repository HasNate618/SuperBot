"""Cheap, strict validation of `BodyCommand` before it reaches the 50 Hz loop.

Design §6: "Wrong schema, non-finite numbers, unknown bits, workspace box
exceeded, joint/velocity limits, TTL expired on arrival -> reject, execute
hold, increment an invalid counter into the next brain digest."

Rejection never raises into the control loop: it returns a result the caller
turns into `hold`.

The workspace box here is a sanity cap, not a reach model. The authoritative
answer to "can the body reach that" is the executor's `SATURATED` flag. These
bounds only stop absurd values (a 5 m wrist target) from reaching the solver.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .body import (
    AUTH_L_HAND,
    AUTH_R_HAND,
    AUTH_ROOT_HEIGHT,
    AUTH_WAIST,
    DONE_WHEN,
    FRAMES,
    KNOWN_AUTH,
    SCHEMA,
    SOURCES,
    BodyCommand,
    Pose,
)

# Root-relative hand box per axis, (min, max) in metres, relative to the pelvis.
# ASSUMPTION: a sanity cap sized well outside the measured ~0.20 m reliable
# reach envelope, not a reach model. -Z is down (feet), +Z is up (overhead
# reach); the pelvis sits ~0.79 m up, so a hand at 0.9 m above the root is an
# ordinary raised arm, not an absurdity. Tighten in Phase 2 against achieved
# positions from SONIC.
HAND_BOX = (
    (-0.80, 0.80),  # x, left/right
    (-0.80, 0.80),  # y, forward/back
    (-0.70, 1.00),  # z, down/up
)

# Root-relative height range in metres.
# MEASURED context: HMD height is 1.221 m standing, 1.170 m knees-bent (E3).
HEIGHT_RANGE = (0.30, 1.60)

# Waist bounds in radians.
# ASSUMPTION: saturating head-yaw->torso coupling measured 27-31 deg; a 0.6 rad
# (~34 deg) waist cap keeps the command inside what the body demonstrably does.
WAIST_LIMIT_RAD = 0.6

MAX_JOINT_SPEED_LIMIT = 4.0  # rad/s, above this the command is nonsense
MAX_HAND_SPEED_LIMIT = 3.0  # m/s


@dataclass(frozen=True)
class ValidationResult:
    """`command` is None when the command must be rejected in favour of hold."""

    command: BodyCommand | None
    reason: str = ""

    @property
    def ok(self) -> bool:
        return self.command is not None


def _finite(value: float) -> bool:
    return isinstance(value, (int, float)) and math.isfinite(value)


def _check_pose(name: str, pose: Pose, box: tuple[tuple[float, float], ...]) -> str:
    if len(pose.p) != 3 or len(pose.q_xyzw) != 4:
        return f"{name}: bad pose shape"
    if not all(_finite(v) for v in pose.p) or not all(_finite(v) for v in pose.q_xyzw):
        return f"{name}: non-finite pose"
    for index, axis in enumerate("xyz"):
        low, high = box[index]
        value = pose.p[index]
        if not (low <= value <= high):
            return f"{name}: {axis}={value:.2f} outside [{low:.2f}, {high:.2f}] m box"
    norm = math.sqrt(sum(v * v for v in pose.q_xyzw))
    if abs(norm - 1.0) > 0.05:
        return f"{name}: quaternion norm {norm:.3f} is not unit"
    return ""


def validate_command(cmd: BodyCommand, now_ns: int) -> ValidationResult:
    """Accept or reject one command. Pure: no state, no clock, no I/O."""

    if cmd.schema != SCHEMA:
        return ValidationResult(None, f"schema {cmd.schema!r} is not {SCHEMA!r}")
    if cmd.source not in SOURCES:
        return ValidationResult(None, f"unknown source {cmd.source!r}")
    if cmd.frame not in FRAMES:
        return ValidationResult(None, f"unknown frame {cmd.frame!r}")
    if cmd.done_when not in DONE_WHEN:
        return ValidationResult(None, f"unknown done_when {cmd.done_when!r}")

    unknown = cmd.authority & ~KNOWN_AUTH
    if unknown:
        return ValidationResult(None, f"unknown authority bits 0x{unknown:x}")

    if cmd.ttl_ms < 0:
        return ValidationResult(None, "negative ttl_ms")
    # TTL gates acceptance. Expiry is checked at intake, not mid-skill.
    if cmd.expires_at_ns() < now_ns:
        return ValidationResult(None, "ttl expired on arrival")

    if cmd.duration_ms < 0:
        return ValidationResult(None, "negative duration_ms")
    if not _finite(cmd.tolerance_m) or cmd.tolerance_m <= 0:
        return ValidationResult(None, "tolerance_m must be finite and positive")
    if not _finite(cmd.max_joint_speed) or not (0 < cmd.max_joint_speed <= MAX_JOINT_SPEED_LIMIT):
        return ValidationResult(None, f"max_joint_speed outside (0, {MAX_JOINT_SPEED_LIMIT}]")
    if not _finite(cmd.max_hand_speed) or not (0 < cmd.max_hand_speed <= MAX_HAND_SPEED_LIMIT):
        return ValidationResult(None, f"max_hand_speed outside (0, {MAX_HAND_SPEED_LIMIT}]")

    if not _finite(cmd.v_xy[0]) or not _finite(cmd.v_xy[1]) or not _finite(cmd.yaw_rate):
        return ValidationResult(None, "non-finite base velocity")

    if cmd.height is not None:
        if not _finite(cmd.height):
            return ValidationResult(None, "non-finite height")
        low, high = HEIGHT_RANGE
        if not (low <= cmd.height <= high):
            return ValidationResult(None, f"height {cmd.height:.2f} outside [{low}, {high}] m")

    for name, bit in (("head", 1 << 2), ("left_hand", AUTH_L_HAND), ("right_hand", AUTH_R_HAND)):
        pose = getattr(cmd, name)
        if pose is not None and pose is not None and (cmd.authority & bit):
            problem = _check_pose(name, pose, HAND_BOX)
            if problem:
                return ValidationResult(None, problem)

    if cmd.closure is not None and (cmd.authority & (1 << 5)):
        for value in cmd.closure:
            if not _finite(value) or not (0.0 <= value <= 1.0):
                return ValidationResult(None, "closure outside [0, 1]")

    if cmd.waist is not None and (cmd.authority & AUTH_WAIST):
        if len(cmd.waist) != 3:
            return ValidationResult(None, "waist must have 3 components")
        for value in cmd.waist:
            if not _finite(value) or abs(value) > WAIST_LIMIT_RAD:
                return ValidationResult(None, f"waist outside +-{WAIST_LIMIT_RAD} rad")

    # A height inside its range but requested without authority is ignored, not
    # an error: unset fields are ignored, and that is how "mode" is expressed.
    _ = AUTH_ROOT_HEIGHT

    return ValidationResult(cmd, "")
