#!/usr/bin/env python3
"""E2 variant of the E1 remote pose client.

Adds per-packet logging and a square-wave ("step") yaw mode so a pose step
can be correlated with captured pixels.
"""

import argparse
import ctypes as C
import math
import socket
import struct
import sys
import time


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


def assert_layout():
    checks = {
        "r_head_data": (C.sizeof(HeadData), 128),
        "r_remote_controller_data": (C.sizeof(ControllerData), 120),
        "r_remote_data": (SIZE, 376),
    }
    bad = {k: v for k, (v, want) in checks.items() if v != want}
    if bad:
        sys.exit(f"layout mismatch: {bad}")


def blank_packet():
    d = RemoteData()
    d.header = HEADER
    d.head.center.position.y = 1.6
    d.head.center.orientation.w = 1.0
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=4242)
    ap.add_argument("--duration", type=float, default=20.0)
    ap.add_argument("--hz", type=float, default=120.0)
    ap.add_argument("--yaw-deg", type=float, default=30.0)
    ap.add_argument("--yaw-hz", type=float, default=0.25, help="sine mode frequency")
    ap.add_argument("--mode", choices=("sine", "step"), default="sine")
    ap.add_argument("--step-period", type=float, default=1.5, help="seconds per step half-cycle")
    ap.add_argument("--log", default="")
    args = ap.parse_args()

    assert_layout()
    s = socket.create_connection((args.host, args.port), timeout=5)
    s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    reset = recv_exact(s, SIZE)
    latest = recv_exact(s, SIZE)
    if struct.unpack_from("<Q", reset)[0] != HEADER or struct.unpack_from("<Q", latest)[0] != HEADER:
        sys.exit("server handshake header mismatch")
    print(f"connected {args.host}:{args.port} size={SIZE} mode={args.mode}", flush=True)

    log = open(args.log, "w", buffering=1) if args.log else None
    if log:
        log.write("idx,mono,wall,yaw_deg\n")

    sock_file = s.makefile("wb")
    t0 = time.monotonic()
    wall0 = time.time()
    n = 0
    last_step = None
    try:
        while True:
            mono = time.monotonic()
            t = mono - t0
            if t >= args.duration:
                break

            if args.mode == "sine":
                angle = math.radians(args.yaw_deg) * math.sin(2 * math.pi * args.yaw_hz * t)
            else:
                step = int(t / args.step_period)
                angle = math.radians(args.yaw_deg if (step % 2 == 0) else -args.yaw_deg)
                if step != last_step:
                    print(
                        f"STEP t={t:7.4f} yaw={math.degrees(angle):+7.2f}deg wall={time.time():.6f}",
                        flush=True,
                    )
                    last_step = step

            half = angle / 2.0
            d = blank_packet()
            d.head.center.orientation.y = math.sin(half)
            d.head.center.orientation.w = math.cos(half)

            sock_file.write(bytes(d))
            sock_file.flush()

            if log:
                log.write(f"{n},{mono:.6f},{time.time():.6f},{math.degrees(angle):.4f}\n")
            n += 1
            time.sleep(1.0 / args.hz)
    except KeyboardInterrupt:
        pass
    finally:
        if log:
            log.close()
        print(f"sent {n} packets wall_end={time.time():.6f}", flush=True)
        s.close()


if __name__ == "__main__":
    main()
