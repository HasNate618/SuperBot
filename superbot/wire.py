"""OpenXR remote-driver wire format, and the MuJoCo -> OpenXR frame map.

The layout is the `r_remote_data` struct the Monado `remote` driver reads: 376
bytes, little-endian, header `mndrmt3\\0`. It is duplicated from
`spike/c0/c0_client.py` on purpose - that harness is frozen evidence for C0, and
`assert_layout()` in both places means a size drift fails loudly instead of
silently sending garbage to the compositor.

Frame map, measured in E3 and reused here: MuJoCo is Z-up with +X forward, the
driver wants Y-up with -Z forward, so

    xr_x = -mj_y     xr_y = mj_z     xr_z = -mj_x

Positions are metres. Quaternions travel as (x, y, z, w); MuJoCo's own root_q is
(w, x, y, z), so conversions are explicit at every boundary.
"""

from __future__ import annotations

import ctypes as C
import math
import struct
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

# ---------------------------------------------------------------- wire format


class Quat(C.Structure):
    _fields_ = [("x", C.c_float), ("y", C.c_float), ("z", C.c_float), ("w", C.c_float)]


class Vec3(C.Structure):
    _fields_ = [("x", C.c_float), ("y", C.c_float), ("z", C.c_float)]


class Vec2(C.Structure):
    _fields_ = [("x", C.c_float), ("y", C.c_float)]


class Vec1(C.Structure):
    _fields_ = [("x", C.c_float)]


class Pose(C.Structure):
    _fields_ = [("orientation", Quat), ("position", Vec3)]


class Fov(C.Structure):
    _fields_ = [
        ("angle_left", C.c_float),
        ("angle_right", C.c_float),
        ("angle_up", C.c_float),
        ("angle_down", C.c_float),
    ]


class View(C.Structure):
    _fields_ = [("fov", Fov), ("pose", Pose), ("_pad", C.c_uint32)]


class HeadData(C.Structure):
    _fields_ = [
        ("views", View * 2),
        ("center", Pose),
        ("per_view_data_valid", C.c_bool),
        ("_pad0", C.c_bool),
        ("_pad1", C.c_bool),
        ("_pad2", C.c_bool),
    ]


class ControllerData(C.Structure):
    _fields_ = [
        ("pose", Pose),
        ("linear_velocity", Vec3),
        ("angular_velocity", Vec3),
        ("hand_curl", C.c_float * 5),
        ("trigger_value", Vec1),
        ("squeeze_value", Vec1),
        ("squeeze_force", Vec1),
        ("thumbstick", Vec2),
        ("trackpad_force", Vec1),
        ("trackpad", Vec2),
        ("hand_tracking_active", C.c_bool),
        ("active", C.c_bool),
        ("system_click", C.c_bool),
        ("system_touch", C.c_bool),
        ("a_click", C.c_bool),
        ("a_touch", C.c_bool),
        ("b_click", C.c_bool),
        ("b_touch", C.c_bool),
        ("trigger_click", C.c_bool),
        ("trigger_touch", C.c_bool),
        ("thumbstick_click", C.c_bool),
        ("thumbstick_touch", C.c_bool),
        ("trackpad_touch", C.c_bool),
        ("_pad0", C.c_bool),
        ("_pad1", C.c_bool),
        ("_pad2", C.c_bool),
    ]


class RemoteData(C.Structure):
    _fields_ = [
        ("header", C.c_uint64),
        ("head", HeadData),
        ("left", ControllerData),
        ("right", ControllerData),
    ]


HEADER = struct.unpack("<Q", b"mndrmt3\0")[0]
SIZE = C.sizeof(RemoteData)

#: Sizes the driver expects. A mismatch means this file has drifted.
EXPECTED = {"r_head_data": (C.sizeof(HeadData), 128),
            "r_remote_controller_data": (C.sizeof(ControllerData), 120),
            "r_remote_data": (C.sizeof(RemoteData), 376)}


def assert_layout() -> None:
    """Raise when the struct no longer matches the driver's ABI."""
    bad = {k: (size, want) for k, (size, want) in EXPECTED.items() if size != want}
    if bad:
        raise ValueError(f"remote data layout mismatch: {bad}")


# ----------------------------------------------------------------- quaternions
# Internally w, x, y, z to match MuJoCo; converted to x, y, z, w at the packet.


def quat_mul(a: Sequence[float], b: Sequence[float]) -> tuple[float, float, float, float]:
    aw, ax, ay, az = a
    bw, bx, by, bz = b
    return (
        aw * bw - ax * bx - ay * by - az * bz,
        aw * bx + ax * bw + ay * bz - az * by,
        aw * by - ax * bz + ay * bw + az * bx,
        aw * bz + ax * by - ay * bx + az * bw,
    )


def quat_conj(q: Sequence[float]) -> tuple[float, float, float, float]:
    return (q[0], -q[1], -q[2], -q[3])


def quat_normalize(q: Sequence[float]) -> tuple[float, float, float, float]:
    norm = math.sqrt(sum(c * c for c in q))
    if norm == 0.0:
        return (1.0, 0.0, 0.0, 0.0)
    return (q[0] / norm, q[1] / norm, q[2] / norm, q[3] / norm)


def quat_rotate(q: Sequence[float], v: Sequence[float]) -> tuple[float, float, float]:
    """Rotate vector v by unit quaternion q (w, x, y, z)."""
    qw, qx, qy, qz = q
    vx, vy, vz = v
    # t = 2 * (q_vec x v); v' = v + qw * t + q_vec x t
    tx = 2.0 * (qy * vz - qz * vy)
    ty = 2.0 * (qz * vx - qx * vz)
    tz = 2.0 * (qx * vy - qy * vx)
    return (
        vx + qw * tx + qy * tz - qz * ty,
        vy + qw * ty + qz * tx - qx * tz,
        vz + qw * tz + qx * ty - qy * tx,
    )


def quat_from_matrix(m: Sequence[Sequence[float]]) -> tuple[float, float, float, float]:
    """Rotation matrix -> unit quaternion (w, x, y, z), Shepperd's method."""
    trace = m[0][0] + m[1][1] + m[2][2]
    if trace > 0.0:
        s = math.sqrt(trace + 1.0) * 2.0
        q = (0.25 * s, (m[2][1] - m[1][2]) / s, (m[0][2] - m[2][0]) / s, (m[1][0] - m[0][1]) / s)
    elif m[0][0] > m[1][1] and m[0][0] > m[2][2]:
        s = math.sqrt(1.0 + m[0][0] - m[1][1] - m[2][2]) * 2.0
        q = ((m[2][1] - m[1][2]) / s, 0.25 * s, (m[0][1] + m[1][0]) / s, (m[0][2] + m[2][0]) / s)
    elif m[1][1] > m[2][2]:
        s = math.sqrt(1.0 + m[1][1] - m[0][0] - m[2][2]) * 2.0
        q = ((m[0][2] - m[2][0]) / s, (m[0][1] + m[1][0]) / s, 0.25 * s, (m[1][2] + m[2][1]) / s)
    else:
        s = math.sqrt(1.0 + m[2][2] - m[0][0] - m[1][1]) * 2.0
        q = ((m[1][0] - m[0][1]) / s, (m[0][2] + m[2][0]) / s, (m[1][2] + m[2][1]) / s, 0.25 * s)
    return quat_normalize(q)


#: xr_x = -mj_y, xr_y = mj_z, xr_z = -mj_x, as a matrix.
MJ_TO_XR_M = ((0.0, -1.0, 0.0), (0.0, 0.0, 1.0), (-1.0, 0.0, 0.0))
MJ_TO_XR_Q = quat_from_matrix(MJ_TO_XR_M)


def mj_to_xr_vec(v: Sequence[float]) -> tuple[float, float, float]:
    return (-v[1], v[2], -v[0])


def mj_to_xr_quat(q_wxyz: Sequence[float]) -> tuple[float, float, float, float]:
    """Re-express an orientation in the driver's frame; returns (x, y, z, w)."""
    q = quat_normalize(q_wxyz)
    out = quat_mul(quat_mul(MJ_TO_XR_Q, q), quat_conj(MJ_TO_XR_Q))
    return (out[1], out[2], out[3], out[0])


# ------------------------------------------------------- BodyState -> XR poses


def _xyzw_to_wxyz(q: Iterable[float]) -> tuple[float, float, float, float]:
    x, y, z, w = q
    return (w, x, y, z)


def body_state_to_xr(
    state: Mapping[str, Any],
) -> dict[str, tuple[tuple[float, float, float], tuple[float, float, float, float]]]:
    """BodyState dict -> absolute OpenXR poses for head and both hands.

    `BodyState` poses are root-relative and the root itself carries a rotation
    (the hold clip starts near -90 deg about Z), so composing the root first is
    not optional: skipping it leaves the heading wrong while the position looks
    plausible, which is exactly the mistake that once hid a working turn.
    """
    root_p = tuple(float(c) for c in state["root_p"])
    # BodyState.root_q is (w, x, y, z). It is NOT the same order as Pose.q_xyzw,
    # and converting it as if it were turns an identity root into a 180 deg spin
    # about X: the heading comes out wrong while the positions still look sane.
    root_q = quat_normalize(tuple(float(c) for c in state["root_q"]))

    def compose(pose: dict[str, Any]):
        p_rel = tuple(float(c) for c in pose["p"])
        q_rel = quat_normalize(_xyzw_to_wxyz(pose["q_xyzw"]))
        rotated = quat_rotate(root_q, p_rel)
        p_world = (root_p[0] + rotated[0], root_p[1] + rotated[1], root_p[2] + rotated[2])
        q_world = quat_mul(root_q, q_rel)
        return mj_to_xr_vec(p_world), mj_to_xr_quat(q_world)

    return {
        "head": compose(state["head"]),
        "left": compose(state["left_hand"]),
        "right": compose(state["right_hand"]),
    }


#: Measured G1 geometry, in the driver's frame: E3 stood the HMD at 1.221 m and
#: the WBC probe put the wrists at (+-0.222, 0.721, 0.017). Used only before the
#: body has published a state, so the game sees a standing humanoid, not a black
#: view at the origin.
REST_POSES = {
    "head": ((0.0, 1.221, 0.0), (0.0, 0.0, 0.0, 1.0)),
    "left": ((-0.222, 0.721, 0.017), (0.0, 0.0, 0.0, 1.0)),
    "right": ((0.222, 0.721, 0.017), (0.0, 0.0, 0.0, 1.0)),
}


def blank_packet() -> RemoteData:
    d = RemoteData()
    d.header = HEADER
    d.head.per_view_data_valid = False
    for c in (d.left, d.right):
        c.active = True
        c.hand_tracking_active = True
        c.pose.orientation.w = 1.0
    return d


def fill(
    d: RemoteData,
    poses: Mapping[str, tuple[Sequence[float], Sequence[float]]],
    *,
    grip: tuple[float, float] = (0.0, 0.0),
    curl: tuple[float, float] = (0.0, 0.0),
    trigger: tuple[float, float] = (0.0, 0.0),
    trigger_click: tuple[bool, bool] = (False, False),
    fov: tuple[float, float] | None = None,
) -> RemoteData:
    """Write `{"head"|"left"|"right": (position, quaternion_xyzw)}` into a packet.

    Button tuples are (left, right). Grip, curl and trigger are separate on
    purpose: a tracker's squeeze is not a finger curl, and which of them a game
    reads is a question only the game can answer - so they travel independently
    and the caller decides.

    `trigger_click` is an edge, not a level. The previous version derived it from
    `trigger > 0.0`, which re-clicked on every packet while the button was held;
    the caller now owns the rising edge.

    `fov` is `(horizontal_half_angle, vertical_half_angle)` in radians and declares
    a symmetric frustum via `head.views[].fov` + `head.per_view_data_valid`. We are
    the ones emitting the HMD data, so the field of view can be a stated number
    rather than something inferred from images. Default `None` leaves the
    compositor's own value in charge, which is the behaviour every earlier
    measurement was taken under. ASSUMPTION: that this build's driver honours the
    flag - the struct documents it (upstream `drivers/remote/r_interface.h`:
    "Is the per view data valid and should be used?") but the consuming code path
    was not readable from the installed store, so it is measured, not assumed, by
    the two-FOV experiment in the plan.
    """
    if fov is not None:
        half_h, half_v = float(fov[0]), float(fov[1])
        if not (math.isfinite(half_h) and math.isfinite(half_v)):
            raise ValueError("fov angles must be finite")
        if half_h <= 0.0 or half_v <= 0.0:
            raise ValueError("fov half-angles must be positive")
        for view in d.head.views:
            view.fov.angle_left = half_h
            view.fov.angle_right = half_h
            view.fov.angle_up = half_v
            view.fov.angle_down = half_v
        d.head.per_view_data_valid = True

    p, q = poses["head"]
    d.head.center.position.x, d.head.center.position.y, d.head.center.position.z = p
    d.head.center.orientation.x, d.head.center.orientation.y, \
        d.head.center.orientation.z, d.head.center.orientation.w = q
    for i, side in enumerate(("left", "right")):
        pos, quat = poses[side]
        c = getattr(d, side)
        c.pose.position.x, c.pose.position.y, c.pose.position.z = pos
        c.pose.orientation.x, c.pose.orientation.y, c.pose.orientation.z, c.pose.orientation.w = quat
        c.squeeze_value.x = grip[i]
        c.squeeze_force.x = grip[i]
        for k in range(5):
            c.hand_curl[k] = curl[i]
        c.trigger_value.x = trigger[i]
        c.trigger_touch = trigger[i] > 0.0
        c.trigger_click = bool(trigger_click[i])
        c.thumbstick.x = 0.0
        c.thumbstick.y = 0.0
    return d
