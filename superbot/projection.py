"""Wrist -> pixel, through the camera we ourselves are telling the game to use.

This is the arithmetic that makes a slow brain sufficient. We emit the HMD pose,
so the camera is not an unknown we must estimate: a wrist from `mjData` and a
pixel named by a writer become comparable with a pinhole projection. No detector,
no depth, no 3D reconstruction.

Two facts this module depends on, both worth stating because they are easy to
break later:

1. **The root cancels.** `BodyState.head` and the wrist poses are all root-relative,
   so the projection subtracts one root-relative point from another. Composing the
   root first is not merely unnecessary, it would reintroduce the ordering trap
   that already cost a session (`root_q` is `(w, x, y, z)`, not `(x, y, z, w)`).
2. **The FOV is an input, not a constant.** `wire.fill` sets `per_view_data_valid =
   False` and writes no FOV, so the compositor's live field of view is currently
   unknown here. Tests pass a fixture; production code must pass a measured value.
   Inventing a default would make every projection a consistent lie.

Frame conventions. Positions are MuJoCo's Z-up frame with +X forward, so in the
head's own frame the gaze is +X, up is +Z, and the robot's left is +Y. The output
is normalised to the HMD view, origin top-left, matching `superbot.intent/1`.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from .body import Pose

#: Head-frame axes. ASSUMPTION: the derived head pose is the clip's head body
#: orientation, whose forward axis is MuJoCo's conventional +X. If the live
#: pose->pixel measurement disagrees, change these three vectors and nothing else.
FORWARD = (1.0, 0.0, 0.0)
UP = (0.0, 0.0, 1.0)
LEFT = (0.0, 1.0, 0.0)

#: Below this forward component a point is at or behind the image plane. It is not
#: projected to a mirrored pixel, because a mirror would drive a servo away.
_MIN_FORWARD = 1e-6


@dataclass(frozen=True)
class Fov:
    """An OpenXR-style asymmetric frustum, in radians.

    Magnitudes are used at projection time: implementations disagree about whether
    `angle_right` is signed, and a symmetric frustum must project identically either
    way, so a caller's sign convention cannot change the answer.
    """

    angle_left: float
    angle_right: float
    angle_up: float
    angle_down: float

    @staticmethod
    def from_degrees(left: float, right: float, up: float, down: float) -> Fov:
        angles = (left, right, up, down)
        if not all(isinstance(a, (int, float)) and math.isfinite(a) for a in angles):
            raise ValueError("fov angles must be finite")
        if abs(left) + abs(right) <= 0.0:
            raise ValueError("horizontal fov must be non-zero")
        if abs(up) + abs(down) <= 0.0:
            raise ValueError("vertical fov must be non-zero")
        return Fov(*[math.radians(abs(a)) for a in angles])

    @property
    def horizontal(self) -> float:
        return self.angle_left + self.angle_right

    @property
    def vertical(self) -> float:
        return self.angle_up + self.angle_down


def _rotate_inverse(q_xyzw: Sequence[float], v: Sequence[float]) -> tuple[float, float, float]:
    """Rotate `v` by the inverse of unit quaternion `q` (world -> body frame)."""
    x, y, z, w = q_xyzw
    vx, vy, vz = v
    # Inverse of a unit quaternion is its conjugate: (-x, -y, -z, w).
    tx = 2.0 * (-y * vz + z * vy)
    ty = 2.0 * (-z * vx + x * vz)
    tz = 2.0 * (-x * vy + y * vx)
    return (
        vx + w * tx + (-y * tz + z * ty),
        vy + w * ty + (-z * tx + x * tz),
        vz + w * tz + (-x * ty + y * tx),
    )


def project_point(
    head: Pose, point_p: Iterable[float], fov: Fov
) -> tuple[float, float] | None:
    """Normalised `(u, v)` of a root-relative point seen from `head`, or None.

    None means "not in this view": behind the camera, or on the image plane where
    the direction is undefined. Callers must treat it as "no answer", never as a
    pixel near the centre.
    """
    px, py, pz = point_p
    dx = px - head.p[0]
    dy = py - head.p[1]
    dz = pz - head.p[2]
    fx, fy, fz = _rotate_inverse(head.q_xyzw, (dx, dy, dz))

    if fx <= _MIN_FORWARD:
        return None

    yaw = math.atan2(fy, fx)      # + toward the robot's left
    pitch = math.atan2(fz, fx)    # + up

    u = (fov.angle_left - yaw) / fov.horizontal
    v = (fov.angle_up - pitch) / fov.vertical
    return (u, v)


def pixel_error(target: tuple[float, float], actual: tuple[float, float]) -> float:
    """Euclidean distance in normalised pixels. The servo drives this to zero."""
    return math.hypot(target[0] - actual[0], target[1] - actual[1])
