#!/usr/bin/env python3
"""Manual keyboard/mouse control for the C0 Superhot session.

Reads the real keyboard and mouse straight from /dev/input (no window, no
X11/Wayland grab), turns them into an HMD pose plus two controller poses, and
streams the same 376-byte Monado `remote` packets as c0_client.py.

Controls
  mouse             look (yaw/pitch)
  W A S D           move
  Space / Z         up / down
  Shift / Ctrl      fast / slow
  arrows            move the active hand
  wheel             reach the active hand forward/back
  left mouse / T    trigger (fire)
  right mouse / G   grip (grab/hold)
  Tab               switch active hand
  1 / 2             active hand = right / left
  R                 reset body
  H                 reset hand offset
  Esc               quit

The default hand offset is (0.000, 0.056, -0.702) m in HMD space: that is the
pistol's measured position in The Fall, so looking at it and gripping should
pick it up.

Run it in a terminal, then focus the game window so keystrokes do not type into
the terminal. The controller still sees every key because it reads evdev.
"""

from __future__ import annotations

import argparse
import fcntl
import glob
import math
import os
import select
import signal
import socket
import struct
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import c0_client as c0

EV_SYN, EV_KEY, EV_REL, EV_ABS = 0, 1, 2, 3
BTN_LEFT, BTN_RIGHT, BTN_MIDDLE = 0x110, 0x111, 0x112
REL_X, REL_Y, REL_WHEEL = 0, 1, 8
ABS_X, ABS_Y = 0x00, 0x01
ABS_MT_SLOT, ABS_MT_POSITION_X, ABS_MT_POSITION_Y = 0x2F, 0x35, 0x36
EVENT_FMT = "llHHi"
EVENT_SIZE = struct.calcsize(EVENT_FMT)

KEY = {
    "esc": 1, "1": 2, "2": 3, "3": 4, "4": 5, "5": 6,
    "tab": 15, "q": 16, "w": 17, "e": 18, "r": 19, "t": 20,
    "u": 22, "i": 23, "o": 24, "p": 25, "enter": 28, "ctrl": 29,
    "a": 30, "s": 31, "d": 32, "f": 33, "g": 34, "h": 35,
    "j": 36, "k": 37, "l": 38, "shift": 42, "z": 44, "x": 45,
    "c": 46, "v": 47, "b": 48, "n": 49, "m": 50, "space": 57,
    "up": 103, "down": 108, "left": 105, "right": 106,
}
KEY_RIGHT_SHIFT, KEY_RIGHT_CTRL = 54, 97
EVIOCGNAME = 0x80000000 | (256 << 16) | (ord("E") << 8) | 0x06


def device_name(path: str) -> str:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        try:
            buf = bytearray(256)
            fcntl.ioctl(fd, EVIOCGNAME, buf, True)
            return buf.split(b"\0", 1)[0].decode(errors="replace")
        finally:
            os.close(fd)
    except OSError:
        return ""


def discover(kind: str) -> list[str]:
    found: list[str] = []
    pattern = f"/dev/input/by-id/*event-{kind}"
    for p in sorted(glob.glob(pattern)):
        found.append(os.path.realpath(p))
    for p in sorted(glob.glob("/dev/input/event*")):
        name = device_name(p).lower()
        if kind == "kbd" and ("keyboard" in name or "receiver" in name):
            found.append(p)
        if kind == "mouse" and (
            "mouse" in name or "touchpad" in name or "pointer" in name
        ):
            found.append(p)
    return list(dict.fromkeys(found))


class Input:
    def __init__(self, paths: list[str], abs_scale: float = 0.25):
        self.fds: dict[int, bytes] = {}
        for p in paths:
            try:
                fd = os.open(p, os.O_RDONLY | os.O_NONBLOCK)
                self.fds[fd] = b""
                print(f"[manual] {p}: {device_name(p)}")
            except OSError as exc:
                print(f"[manual] cannot open {p}: {exc}", file=sys.stderr)
        self.keys: set[int] = set()
        self.buttons: set[int] = set()
        self.dx = self.dy = self.wheel = 0
        self.abs_scale = abs_scale
        self.abs_last: dict[tuple[int, int], int] = {}

    def readable(self):
        return list(self.fds)

    def read(self):
        for fd in list(self.fds):
            try:
                chunk = os.read(fd, 8192)
            except BlockingIOError:
                continue
            except OSError:
                chunk = b""
            if not chunk:
                self.fds.pop(fd, None)
                os.close(fd)
                continue
            buf = self.fds[fd] + chunk
            n = len(buf) // EVENT_SIZE
            for i in range(n):
                _, _, typ, code, value = struct.unpack_from(EVENT_FMT, buf, i * EVENT_SIZE)
                if typ == EV_KEY:
                    if value:
                        self.keys.add(code) if code < BTN_LEFT else self.buttons.add(code)
                    else:
                        self.keys.discard(code) if code < BTN_LEFT else self.buttons.discard(code)
                elif typ == EV_REL:
                    if code == REL_X:
                        self.dx += value
                    elif code == REL_Y:
                        self.dy += value
                    elif code == REL_WHEEL:
                        self.wheel += value
                elif typ == EV_ABS:
                    if code == ABS_MT_SLOT:
                        continue
                    if code not in (ABS_X, ABS_Y, ABS_MT_POSITION_X, ABS_MT_POSITION_Y):
                        continue
                    key = (fd, code)
                    previous = self.abs_last.get(key)
                    self.abs_last[key] = value
                    if previous is None:
                        continue
                    delta = int((value - previous) * self.abs_scale)
                    if code in (ABS_X, ABS_MT_POSITION_X):
                        self.dx += delta
                    else:
                        self.dy += delta
            self.fds[fd] = buf[n * EVENT_SIZE:]

    def take_mouse(self):
        dx, dy, wheel = self.dx, self.dy, self.wheel
        self.dx = self.dy = self.wheel = 0
        return dx, dy, wheel

    def down(self, name: str) -> bool:
        return KEY[name] in self.keys

    def close(self):
        for fd in self.fds:
            os.close(fd)
        self.fds.clear()


class Manual:
    def __init__(self, args):
        self.args = args
        self.pos = [0.0, 1.221, 0.0]
        self.yaw = 0.0
        self.pitch = 0.0
        self.hand = list(args.hand)
        self.active = "R"
        self.trigger = False
        self.grip = False
        self.prev_trigger = False
        self.prev_squeeze = False
        self.prev_keys: set[str] = set()
        self.crouching = False
        self.reset()

    def reset(self):
        self.pos = [0.0, 1.221, 0.0]
        self.yaw = self.pitch = 0.0
        self.hand = list(self.args.hand)
        self.trigger = self.grip = False
        self.prev_trigger = False
        self.crouching = False

    def quat(self):
        cy, sy = math.cos(self.yaw / 2), math.sin(self.yaw / 2)
        cp, sp = math.cos(self.pitch / 2), math.sin(self.pitch / 2)
        return (cy * sp, sy * cp, -sy * sp, cy * cp)

    @staticmethod
    def rot(q, v):
        x, y, z, w = q
        vx, vy, vz = v
        tx = 2 * (y * vz - z * vy)
        ty = 2 * (z * vx - x * vz)
        tz = 2 * (x * vy - y * vx)
        return (vx + w * tx + (y * tz - z * ty),
                vy + w * ty + (z * tx - x * tz),
                vz + w * tz + (x * ty - y * tx))

    def update(self, inp: Input, dt: float):
        def pressed(name):
            return inp.down(name) and name not in self.prev_keys

        if pressed("r"):
            self.reset()
        if pressed("h"):
            self.hand = list(self.args.hand)
        if pressed("tab"):
            self.active = "L" if self.active == "R" else "R"
        if pressed("1"):
            self.active = "R"
        if pressed("2"):
            self.active = "L"

        dx, dy, wheel = inp.take_mouse()
        self.yaw -= dx * math.radians(self.args.sens)
        sign = 1.0 if self.args.invert_y else -1.0
        self.pitch += sign * dy * math.radians(self.args.sens)
        self.pitch = max(-math.radians(85), min(math.radians(85), self.pitch))
        if self.yaw > math.pi:
            self.yaw -= 2 * math.pi
        elif self.yaw < -math.pi:
            self.yaw += 2 * math.pi

        speed = self.args.speed
        if inp.down("shift") or KEY_RIGHT_SHIFT in inp.keys:
            speed *= 2.5
        if inp.down("ctrl") or KEY_RIGHT_CTRL in inp.keys:
            speed *= 0.3
        fwd = (-math.sin(self.yaw), 0.0, -math.cos(self.yaw))
        right = (math.cos(self.yaw), 0.0, -math.sin(self.yaw))
        if inp.down("w"):
            self.pos = [self.pos[i] + fwd[i] * speed * dt for i in range(3)]
        if inp.down("s"):
            self.pos = [self.pos[i] - fwd[i] * speed * dt for i in range(3)]
        if inp.down("d"):
            self.pos = [self.pos[i] + right[i] * speed * dt for i in range(3)]
        if inp.down("a"):
            self.pos = [self.pos[i] - right[i] * speed * dt for i in range(3)]
        if inp.down("space"):
            self.pos[1] += speed * 0.5 * dt
        if inp.down("z"):
            self.pos[1] -= speed * 0.5 * dt
        if inp.down("c"):
            if not self.crouching:
                self.pos[1] = 0.9
                self.crouching = True
        elif self.crouching:
            self.pos[1] = 1.221
            self.crouching = False
        self.pos[0] = max(-20.0, min(20.0, self.pos[0]))
        self.pos[1] = max(0.3, min(2.5, self.pos[1]))
        self.pos[2] = max(-20.0, min(20.0, self.pos[2]))

        hspeed = self.args.hand_speed
        if inp.down("up"):
            self.hand[1] += hspeed * dt
        if inp.down("down"):
            self.hand[1] -= hspeed * dt
        if inp.down("left"):
            self.hand[0] -= hspeed * dt
        if inp.down("right"):
            self.hand[0] += hspeed * dt
        self.hand[2] += wheel * 0.03
        self.hand[0] = max(-0.8, min(0.8, self.hand[0]))
        self.hand[1] = max(-0.8, min(0.8, self.hand[1]))
        self.hand[2] = max(-1.2, min(-0.15, self.hand[2]))

        self.trigger = inp.down("t") or BTN_LEFT in inp.buttons
        self.grip = inp.down("g") or BTN_RIGHT in inp.buttons
        self.prev_keys = {name for name in KEY if inp.down(name)}

    def packet(self):
        q = self.quat()
        hp = self.pos
        active_off = self.hand
        rest_l = (-0.222, -0.5, 0.017)
        rest_r = (0.222, -0.5, 0.017)
        hand = {
            "L": ([hp[i] + rest_l[i] for i in range(3)], (0.0, 0.0, 0.0, 1.0)),
            "R": ([hp[i] + rest_r[i] for i in range(3)], (0.0, 0.0, 0.0, 1.0)),
        }
        off = self.rot(q, active_off)
        hand[self.active] = ([hp[i] + off[i] for i in range(3)], q)

        st = c0.State()
        st.head_p = tuple(hp)
        st.head_q = q
        st.l_p, st.l_q = hand["L"]
        st.r_p, st.r_q = hand["R"]
        st.trigger = 1.0 if self.trigger else 0.0
        st.trigger_click = self.trigger and not self.prev_trigger
        st.squeeze = 1.0 if self.grip else 0.0
        d = c0.fill(c0.blank_packet(), st)
        for side in (d.left, d.right):
            side.active = True
            side.hand_tracking_active = False
            side.thumbstick.x = side.thumbstick.y = 0.0
            side.squeeze_force.x = 1.0 if self.grip else 0.0
            side.trigger_touch = self.trigger
            for i in range(5):
                side.hand_curl[i] = 1.0 if self.grip else 0.0
        inactive = d.left if self.active == "R" else d.right
        inactive.trigger_value.x = 0.0
        inactive.squeeze_value.x = 0.0
        inactive.squeeze_force.x = 0.0
        inactive.trigger_click = False
        inactive.trigger_touch = False
        inactive.thumbstick.x = inactive.thumbstick.y = 0.0
        for i in range(5):
            inactive.hand_curl[i] = 0.0
        self.prev_trigger = self.trigger
        self.prev_squeeze = self.grip
        return d

    def status(self):
        return (f"yaw={math.degrees(self.yaw):+6.1f} pitch={math.degrees(self.pitch):+5.1f} "
                f"pos=({self.pos[0]:+.2f},{self.pos[1]:.2f},{self.pos[2]:+.2f}) "
                f"hand={self.active} off=({self.hand[0]:+.2f},{self.hand[1]:+.2f},{self.hand[2]:+.2f}) "
                f"trig={int(self.trigger)} grip={int(self.grip)}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=4242)
    ap.add_argument("--hz", type=float, default=90.0)
    ap.add_argument("--sens", type=float, default=0.04, help="mouse degrees per count")
    ap.add_argument("--abs-scale", type=float, default=0.25, help="touchpad ABS counts to mouse counts")
    ap.add_argument("--speed", type=float, default=0.8, help="move speed, m/s")
    ap.add_argument("--hand-speed", type=float, default=0.5, help="hand speed, m/s")
    ap.add_argument("--hand", type=float, nargs=3, default=(0.0, 0.056, -0.702),
                    metavar=("X", "Y", "Z"), help="hand offset in HMD space")
    ap.add_argument("--invert-y", action="store_true")
    ap.add_argument("--kbd", action="append", help="keyboard event device (default: all by-id keyboards)")
    ap.add_argument("--mouse", action="append", help="mouse event device (default: all by-id mice)")
    ap.add_argument("--list-devices", action="store_true")
    ap.add_argument("--duration", type=float, default=0.0, help="exit after N seconds (0 = forever)")
    args = ap.parse_args()

    if args.list_devices:
        for kind in ("kbd", "mouse"):
            for p in discover(kind):
                print(f"{kind:5s} {p}  {device_name(p)}")
        return

    kbd = args.kbd or discover("kbd")
    mouse = args.mouse or discover("mouse")
    if not kbd:
        sys.exit("no keyboard event device found; pass --kbd /dev/input/eventN")
    if not mouse:
        sys.exit("no mouse event device found; pass --mouse /dev/input/eventN")

    inp = Input(kbd + mouse, abs_scale=args.abs_scale)
    if not inp.fds:
        sys.exit("could not open any input device")
    print("[manual] controls: mouse look | WASD move | Space/Z up/down | arrows hand | wheel reach")
    print("[manual]           LMB/T fire | RMB/G grip | Tab/1/2 hand | R reset | H hand | Esc quit")

    c0.assert_layout()
    sock = socket.create_connection((args.host, args.port), timeout=5)
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    print(f"[manual] connected {args.host}:{args.port}", flush=True)
    c0.recv_exact(sock, c0.SIZE)
    c0.recv_exact(sock, c0.SIZE)
    out = sock.makefile("wb")

    m = Manual(args)
    running = True

    def stop(*_):
        nonlocal running
        running = False

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    dt = 1.0 / args.hz
    next_t = time.monotonic()
    t0 = time.monotonic()
    last_status = 0.0
    n = 0
    tty = sys.stdout.isatty()
    try:
        while running:
            now = time.monotonic()
            if args.duration and now - t0 >= args.duration:
                break
            wait = next_t - now
            if wait > 0:
                r, _, _ = select.select(inp.readable(), [], [], wait)
                if r:
                    inp.read()
                continue
            inp.read()
            m.update(inp, dt)
            out.write(bytes(m.packet()))
            out.flush()
            n += 1
            next_t += dt
            if next_t < now:
                next_t = now + dt
            if now - last_status >= 0.2:
                last_status = now
                line = "[manual] " + m.status()
                if tty:
                    print("\x1b[2K\r" + line, end="", flush=True)
                else:
                    print(line, flush=True)
    finally:
        inp.close()
        if tty:
            print("\x1b[2K\r", end="")
        print(f"[manual] sent {n} packets in {time.monotonic() - t0:.2f} s")
        sock.close()


if __name__ == "__main__":
    main()
