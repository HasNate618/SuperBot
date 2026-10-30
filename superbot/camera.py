"""Camera geometry for the third-person body view.

Standard library only, no MuJoCo import, so the framing can be tested by
projection maths on a machine with no GPU. `spike/wbc/body_view.py` reads these
values into a `MjvCamera`; nothing here knows about MuJoCo.

Design notes:

* The camera follows the body's root with exponential damping rather than
  snapping to it. Snapping makes the world jitter around the subject; damping
  makes the subject's own motion readable.
* `yaw_follow` defaults to 0: the azimuth stays locked to the world while the
  body turns. That is deliberate. A camera that rotates with the robot's facing
  removes all visible rotation, and "did the body turn to face the target" is
  one of the behaviours this view exists to show.
* `target_height` is measured from the root, not absolute, so the framing holds
  when the body squats or is lifted.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

# Azimuth in degrees, VERIFIED VISUALLY against the G1 model (not derived from
# the convention docs): 90 faces the chest, 180 shows the profile, 0 the back.
# An earlier revision had front and side swapped, caught by looking at renders.
# `three_quarter` is the default because it shows reach and gait at once.
PRESETS: dict[str, float] = {
    "three_quarter": 140.0,
    "front": 90.0,
    "side": 180.0,
    "back": 0.0,
}

DEFAULT_PRESET = "three_quarter"


def _lerp(a: float, b: float, alpha: float) -> float:
    return a + (b - a) * alpha


@dataclass
class FollowCamera:
    """Damped follow camera. `update()` once per frame, then read the values."""

    preset: str = DEFAULT_PRESET
    # Framing fills the tile: the head lands just inside the top and the
    # feet just inside the bottom (verified against live renders). The old
    # 2.3 m / -10 deg parked the body in the bottom half under a big black
    # sky (elevation below zero looks up).
    distance: float = 2.2
    elevation: float = 0.0
    # Frames the middle of the body. Measured on the hold pose: feet ~0.03 m,
    # head geoms top out at 1.217 m, so mid-body is ~0.62 m, i.e. 0.15 m BELOW
    # the root. An earlier revision aimed 0.75 m above the root and put the
    # lookat over empty air, parking the whole body in the bottom of the frame.
    target_height: float = -0.15
    tau: float = 0.25
    yaw_follow: float = 0.0
    lookat: tuple[float, float, float] = field(default=(0.0, 0.0, 0.64))

    def __post_init__(self) -> None:
        if self.preset not in PRESETS:
            raise ValueError(f"unknown view preset: {self.preset}")
        if self.tau <= 0:
            raise ValueError("tau must be positive")
        if self.distance <= 0:
            raise ValueError("distance must be positive")

    # ---- state ----------------------------------------------------------

    def update(self, root_p: tuple[float, float, float],
               yaw_rad: float = 0.0, dt: float = 0.02) -> tuple[float, float, float]:
        """Move the lookat toward the root. dt is clamped so a stall cannot teleport it."""
        step = min(max(dt, 0.0), 0.5)
        alpha = 1.0 - math.exp(-step / self.tau) if step > 0 else 0.0
        target = (float(root_p[0]), float(root_p[1]), float(root_p[2]) + self.target_height)
        self.lookat = tuple(  # type: ignore[assignment]
            _lerp(cur, want, alpha) for cur, want in zip(self.lookat, target, strict=True)
        )
        return self.lookat

    # ---- derived --------------------------------------------------------

    def azimuth(self, yaw_rad: float = 0.0) -> float:
        """World azimuth plus the (default zero) share of the body's yaw."""
        return PRESETS[self.preset] + self.yaw_follow * math.degrees(yaw_rad)

    def position(self, yaw_rad: float = 0.0) -> tuple[float, float, float]:
        """Camera position for the current lookat."""
        az = math.radians(self.azimuth(yaw_rad))
        el = math.radians(self.elevation)
        forward = (math.cos(el) * math.cos(az), math.cos(el) * math.sin(az), math.sin(el))
        return tuple(  # type: ignore[return-value]
            self.lookat[i] - self.distance * forward[i] for i in range(3)
        )

    def forward(self, yaw_rad: float = 0.0) -> tuple[float, float, float]:
        az = math.radians(self.azimuth(yaw_rad))
        el = math.radians(self.elevation)
        return (math.cos(el) * math.cos(az), math.cos(el) * math.sin(az), math.sin(el))


def yaw_from_quat(q_wxyz: tuple[float, float, float, float]) -> float:
    """Heading about Z from a wxyz quaternion."""
    w, x, y, z = q_wxyz
    return math.atan2(2.0 * (w * z + x * y), 1.0 - 2.0 * (y * y + z * z))


def project(point: tuple[float, float, float],
            lookat: tuple[float, float, float],
            camera_pos: tuple[float, float, float],
            fovy_deg: float,
            aspect: float) -> tuple[float, float, float]:
    """Project a world point into normalised device coordinates.

    Returns (x_ndc, y_ndc, depth). Inside the frame means |x|<1, |y|<1 and
    depth>0. This exists so framing can be asserted without rendering.
    """
    fwd = tuple(lookat[i] - camera_pos[i] for i in range(3))
    norm = math.sqrt(sum(c * c for c in fwd))
    if norm == 0:
        raise ValueError("camera at the lookat point")
    fwd = tuple(c / norm for c in fwd)

    world_up = (0.0, 0.0, 1.0)
    right = (
        fwd[1] * world_up[2] - fwd[2] * world_up[1],
        fwd[2] * world_up[0] - fwd[0] * world_up[2],
        fwd[0] * world_up[1] - fwd[1] * world_up[0],
    )
    rnorm = math.sqrt(sum(c * c for c in right)) or 1.0
    right = tuple(c / rnorm for c in right)
    up = (
        right[1] * fwd[2] - right[2] * fwd[1],
        right[2] * fwd[0] - right[0] * fwd[2],
        right[0] * fwd[1] - right[1] * fwd[0],
    )

    rel = tuple(point[i] - camera_pos[i] for i in range(3))
    depth = sum(rel[i] * fwd[i] for i in range(3))
    if depth <= 0:
        return (float("inf"), float("inf"), depth)
    half = math.tan(math.radians(fovy_deg) / 2.0)
    x_ndc = sum(rel[i] * right[i] for i in range(3)) / (depth * half * aspect)
    y_ndc = sum(rel[i] * up[i] for i in range(3)) / (depth * half)
    return (x_ndc, y_ndc, depth)
