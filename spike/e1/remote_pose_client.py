#!/usr/bin/env python3
"""Throwaway E1 spike client for Monado's `remote` driver.

Speaks the r_remote_data wire format from Monado commit
47f9d39e686d6bbd7dad914b4dbe21f784c41911 (src/xrt/drivers/remote/r_interface.h).

Server (monado-service with P_OVERRIDE_ACTIVE_CONFIG=remote) listens on TCP 4242,
accepts one client, writes an initial r_remote_data (reset) and another (latest),
then blocks reading r_remote_data packets and adopting each as `latest`.

This client streams a yaw oscillation around the Y axis so an OpenXR app's
view space visibly rotates.
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
    ap.add_argument("--hz", type=float, default=90.0)
    ap.add_argument("--yaw-deg", type=float, default=35.0)
    ap.add_argument("--yaw-hz", type=float, default=0.25)
    ap.add_argument("--handshake-read", action="store_true", default=True)
    args = ap.parse_args()

    assert_layout()
    print(f"wire size: {SIZE} bytes, header=0x{HEADER:016x}")

    s = socket.create_connection((args.host, args.port), timeout=5)
    s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    print(f"connected to {args.host}:{args.port}")

    if args.handshake_read:
        reset = recv_exact(s, SIZE)
        latest = recv_exact(s, SIZE)
        rh = struct.unpack_from("<Q", reset)[0]
        lh = struct.unpack_from("<Q", latest)[0]
        print(f"handshake reset header=0x{rh:016x} latest header=0x{lh:016x}")
        if rh != HEADER or lh != HEADER:
            sys.exit("server handshake header mismatch")

    sock_file = s.makefile("wb")
    t0 = time.monotonic()
    n = 0
    try:
        while True:
            t = time.monotonic() - t0
            if t >= args.duration:
                break
            angle = math.radians(args.yaw_deg) * math.sin(2 * math.pi * args.yaw_hz * t)
            half = angle / 2.0
            d = blank_packet()
            d.head.center.orientation.x = 0.0
            d.head.center.orientation.y = math.sin(half)
            d.head.center.orientation.z = 0.0
            d.head.center.orientation.w = math.cos(half)

            lx = -0.2 + 0.1 * math.sin(2 * math.pi * args.yaw_hz * t)
            d.left.pose.position.x = lx
            d.left.pose.position.y = 1.3
            d.left.pose.position.z = -0.5
            d.right.pose.position.x = -lx
            d.right.pose.position.y = 1.3
            d.right.pose.position.z = -0.5

            sock_file.write(bytes(d))
            sock_file.flush()
            n += 1
            if n % int(args.hz) == 0:
                print(
                    f"t={t:6.2f}s packet={n:5d} yaw={math.degrees(angle):7.2f} deg "
                    f"quat=({d.head.center.orientation.x:+.4f},{d.head.center.orientation.y:+.4f},"
                    f"{d.head.center.orientation.z:+.4f},{d.head.center.orientation.w:+.4f})"
                )
            time.sleep(1.0 / args.hz)
    except KeyboardInterrupt:
        pass
    finally:
        print(f"sent {n} packets; closing")
        s.close()


if __name__ == "__main__":
    main()
