#!/usr/bin/env python3
"""C0 client — scripted HMD + controller input for the Superhot smoke test.

Speaks Monado's `remote` driver wire format (r_remote_data, 376 bytes,
header "mndrmt3\\0") exactly as established in E1.

Why this exists: C0 answers, in order,
  1. does Superhot accept a synthetic HMD through Monado's remote driver,
  2. does scripted controller input (trigger) reach the game,
  3. does grim capture the *game* window,
  4. what is the game's clock floor in practice,
  5. how far is the pistol / exit pyramid / first enemy.

Geometry is the real G1, not a placeholder: HMD at the measured 1.221 m
(E3 `stand`), wrists at the measured rest pose converted through the E3
frame map (xr_x = -mj_y, xr_y = mj_z, xr_z = -mj_x). E1 used a fake
(0.2, 1.3, -0.5); do not reuse that, the game's scale is part of the claim.

OpenXR convention: right-handed, +Y up, -Z forward. Positions in metres.

Modes
  hold      static pose, zero motion, no input          (clock + pose-follow)
  yaw       +-N deg yaw sweep at --yaw-hz               (does the view follow)
  trigger   periodic trigger_value + trigger_click       (does input reach)
  dodge     lateral HMD step out and back                (game's dodge envelope)
  walk      forward translation at --walk-speed          (does traversal register)
  clock     hold, with START/END markers on stdout+CSV   (clock-ratio protocol)

Every packet is logged to CSV: t, head pos/quat, both controller pos/quat,
trigger, squeeze, thumbstick. That CSV is the evidence that the poses the
game saw are the poses we commanded.
"""

import argparse
import contextlib
import csv
import ctypes as C
import math
import socket
import struct
import sys
import time

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

# ------------------------------------------- measured G1 geometry (E3 / WBC)
#
# HMD height 1.221 m is the E3 `stand` measurement. The wrist rest pose comes
# from the WBC probe: MuJoCo L [-0.017, 0.222, 0.721], R [-0.017, -0.222, 0.721]
# (Z up, wxyz) mapped to OpenXR (Y up, -Z forward) by
#   xr_x = -mj_y,  xr_y = mj_z,  xr_z = -mj_x
HMD_HEIGHT = 1.221
WRIST_L = (-0.222, 0.721, 0.017)
WRIST_R = (0.222, 0.721, 0.017)


def assert_layout():
    checks = {
        "r_head_data": (C.sizeof(HeadData), 128),
        "r_remote_controller_data": (C.sizeof(ControllerData), 120),
        "r_remote_data": (SIZE, 376),
    }
    bad = {k: (v, want) for k, (v, want) in checks.items() if v != want}
    if bad:
        sys.exit(f"layout mismatch (struct, expected): {bad}")


# ---------------------------------------------------------------- pose helpers


def yaw_quat(deg):
    """Yaw about +Y in OpenXR (xyzw)."""
    half = math.radians(deg) / 2.0
    return (0.0, math.sin(half), 0.0, math.cos(half))


def identity_quat():
    return (0.0, 0.0, 0.0, 1.0)


class State:
    """One tick of intended input. Everything is explicit so the CSV is honest."""

    def __init__(self):
        self.head_p = (0.0, HMD_HEIGHT, 0.0)
        self.head_q = identity_quat()
        self.l_p = WRIST_L
        self.l_q = identity_quat()
        self.r_p = WRIST_R
        self.r_q = identity_quat()
        self.trigger = 0.0
        self.trigger_click = False
        self.squeeze = 0.0
        self.stick = (0.0, 0.0)
        self.note = ""


def smoothstep(x):
    x = max(0.0, min(1.0, x))
    return x * x * (3.0 - 2.0 * x)


# ------------------------------------------------------------------- scripting


def script_hold(t, a, st):
    return st


def script_yaw(t, a, st):
    deg = a.yaw_deg * math.sin(2 * math.pi * a.yaw_hz * t)
    st.head_q = yaw_quat(deg)
    st.note = f"yaw={deg:.1f}"
    return st


def script_trigger(t, a, st):
    # 0.5 s pulled, 0.5 s released; click on the leading edge only.
    phase = (t % 1.0)
    pulled = phase < 0.5
    st.trigger = 1.0 if pulled else 0.0
    st.trigger_click = pulled and (phase < a.dt_click)
    st.squeeze = 1.0 if pulled else 0.0
    st.note = f"trigger={st.trigger:.0f}"
    return st


def script_dodge(t, a, st):
    # out over 0.4 s, hold 0.2 s, back over 0.4 s, then rest.
    amp = a.dodge_m
    if t < 0.4:
        s = smoothstep(t / 0.4)
    elif t < 0.6:
        s = 1.0
    elif t < 1.0:
        s = 1.0 - smoothstep((t - 0.6) / 0.4)
    else:
        s = 0.0
    dx = amp * s
    st.head_p = (dx, HMD_HEIGHT, 0.0)
    st.l_p = (WRIST_L[0] + dx, WRIST_L[1], WRIST_L[2])
    st.r_p = (WRIST_R[0] + dx, WRIST_R[1], WRIST_R[2])
    st.note = f"dx={dx * 100:.1f}cm"
    return st


def script_walk(t, a, st):
    dz = -a.walk_speed * t  # -Z is forward
    st.head_p = (0.0, HMD_HEIGHT, dz)
    st.l_p = (WRIST_L[0], WRIST_L[1], WRIST_L[2] + dz)
    st.r_p = (WRIST_R[0], WRIST_R[1], WRIST_R[2] + dz)
    st.note = f"dz={dz:.2f}"
    return st


def script_clock(t, a, st):
    st.note = "START" if t < a.dt_click else ("END" if a.duration - t < a.dt_click else "hold")
    return st


SCRIPTS = {
    "hold": script_hold,
    "yaw": script_yaw,
    "trigger": script_trigger,
    "dodge": script_dodge,
    "walk": script_walk,
    "clock": script_clock,
}


def fill(d, st):
    d.head.center.position.x = st.head_p[0]
    d.head.center.position.y = st.head_p[1]
    d.head.center.position.z = st.head_p[2]
    d.head.center.orientation.x, d.head.center.orientation.y, \
        d.head.center.orientation.z, d.head.center.orientation.w = st.head_q

    for side, pos, quat in (
        (d.left, st.l_p, st.l_q),
        (d.right, st.r_p, st.r_q),
    ):
        side.pose.position.x = pos[0]
        side.pose.position.y = pos[1]
        side.pose.position.z = pos[2]
        side.pose.orientation.x, side.pose.orientation.y, \
            side.pose.orientation.z, side.pose.orientation.w = quat
        side.trigger_value.x = st.trigger
        side.squeeze_value.x = st.squeeze
        side.trigger_click = st.trigger_click
        side.trigger_touch = st.trigger > 0.0
        side.thumbstick.x = st.stick[0]
        side.thumbstick.y = st.stick[1]
    return d


def blank_packet():
    d = RemoteData()
    d.header = HEADER
    d.head.per_view_data_valid = False
    for c in (d.left, d.right):
        c.active = True
        c.hand_tracking_active = True
        c.pose.orientation.w = 1.0
    return d


def recv_exact(sock, n):
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError(f"peer closed after {len(buf)}/{n} bytes")
        buf.extend(chunk)
    return bytes(buf)


# ------------------------------------------------------------------------ main


@contextlib.contextmanager
def _open_log(path):
    """Open the tick CSV, turning a filesystem failure into a clean exit."""
    try:
        with open(path, "w", newline="") as fh:
            yield fh
    except OSError as exc:
        sys.exit(f"cannot write log file {path}: {exc}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mode", default="hold", choices=sorted(SCRIPTS))
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=4242)
    ap.add_argument("--duration", type=float, default=20.0)
    ap.add_argument("--hz", type=float, default=90.0)
    ap.add_argument("--yaw-deg", type=float, default=35.0)
    ap.add_argument("--yaw-hz", type=float, default=0.25)
    ap.add_argument("--dodge-m", type=float, default=0.15)
    ap.add_argument("--walk-speed", type=float, default=0.64, help="G1 SONIC measured walk speed, m/s")
    ap.add_argument("--dt-click", type=float, default=0.05, help="click pulse width, s")
    ap.add_argument("--log", default=None, help="CSV path (default spike/c0/logs/c0_<mode>.csv)")
    args = ap.parse_args()

    assert_layout()
    script = SCRIPTS[args.mode]
    log_path = args.log or f"spike/c0/logs/с0_{args.mode}.csv".replace("с", "c")

    print(f"[c0] wire size {SIZE} B, header 0x{HEADER:016x}, mode={args.mode}", flush=True)
    print(f"[c0] HMD height {HMD_HEIGHT} m, wrist L {WRIST_L} R {WRIST_R}", flush=True)

    s = socket.create_connection((args.host, args.port), timeout=5)
    s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    print(f"[c0] connected {args.host}:{args.port}", flush=True)

    reset = recv_exact(s, SIZE)
    latest = recv_exact(s, SIZE)
    for name, pkt in (("reset", reset), ("latest", latest)):
        h = struct.unpack_from("<Q", pkt)[0]
        if h != HEADER:
            sys.exit(f"handshake {name} header 0x{h:016x} != expected")
    print("[c0] handshake ok", flush=True)

    import os

    try:
        log_dir = os.path.dirname(log_path)
        if log_dir:
            os.makedirs(log_dir, exist_ok=True)
    except OSError as exc:
        sys.exit(f"cannot create log directory for {log_path}: {exc}")

    sock_file = s.makefile("wb")
    dt = 1.0 / args.hz
    t0 = time.monotonic()
    n = 0
    with _open_log(log_path) as fh:
        w = csv.writer(fh)
        w.writerow([
            "t", "hx", "hy", "hz", "hqx", "hqy", "hqz", "hqw",
            "lx", "ly", "lz", "rx", "ry", "rz",
            "trigger", "squeeze", "note",
        ])
        try:
            while True:
                t = time.monotonic() - t0
                if t >= args.duration:
                    break
                st = script(t, args, State())
                d = fill(blank_packet(), st)
                sock_file.write(bytes(d))
                sock_file.flush()
                w.writerow([
                    f"{t:.4f}",
                    f"{st.head_p[0]:.4f}", f"{st.head_p[1]:.4f}", f"{st.head_p[2]:.4f}",
                    f"{st.head_q[0]:.5f}", f"{st.head_q[1]:.5f}",
                    f"{st.head_q[2]:.5f}", f"{st.head_q[3]:.5f}",
                    f"{st.l_p[0]:.4f}", f"{st.l_p[1]:.4f}", f"{st.l_p[2]:.4f}",
                    f"{st.r_p[0]:.4f}", f"{st.r_p[1]:.4f}", f"{st.r_p[2]:.4f}",
                    f"{st.trigger:.2f}", f"{st.squeeze:.2f}", st.note,
                ])
                n += 1
                target = t0 + n * dt
                sleep = target - time.monotonic()
                if sleep > 0:
                    time.sleep(sleep)
        except KeyboardInterrupt:
            print("\n[c0] interrupted", flush=True)

    print(f"[c0] sent {n} packets in {time.monotonic() - t0:.2f} s -> {log_path}", flush=True)


if __name__ == "__main__":
    main()
