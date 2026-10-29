#!/usr/bin/env python3
"""Third-person body view: SONIC in MuJoCo, rendered to JPEG, state to JSON.

Runs beside a session and writes two files that a dashboard tile can read:

    <out>/body_latest.jpg    third-person render, atomically replaced
    <out>/body_state.json    the achieved BodyState, same cadence

Why a separate process: the renderer and the policy both want the CPU, and the
orchestrator must not block on EGL. If this process dies the session survives,
which is the same rule the preview capture follows.

Camera is deliberately a fixed free camera, not a tracked one: a moving camera
makes it impossible to judge whether the *body* moved or the view did.

Usage:
    spike/e3/.venv/bin/python spike/wbc/body_view.py --out /tmp/body --seconds 10
"""

from __future__ import annotations

import argparse
import contextlib
from collections.abc import Callable
import io
import json
import socket
import threading
import math
import os
import sys
import time
from pathlib import Path

os.environ.setdefault("MUJOCO_GL", "egl")
# MUJOCO_GL is not enough on its own: PyOpenGL picks its backend separately.
os.environ.setdefault("PYOPENGL_PLATFORM", "egl")


sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

# gl_boot is imported for its side effect: it re-execs with LD_LIBRARY_PATH so
# libEGL resolves, which must happen before mujoco is imported.
import gl_boot  # pyright: ignore[reportMissingImports]  # noqa: E402,F401

# mujoco / PIL live in spike/e3/.venv only; `superbot/` stays stdlib-only.
import mujoco  # pyright: ignore[reportMissingImports]  # noqa: E402
from PIL import Image  # pyright: ignore[reportMissingImports]  # noqa: E402

from sonic_executor import SonicExecutor  # pyright: ignore[reportMissingImports]  # noqa: E402
from superbot.body import AUTH_HEAD, AUTH_R_HAND, BodyCommand, hold  # noqa: E402
from superbot.bus import CommandBus  # noqa: E402


class BodyRenderer:
    """Fixed free camera on the robot, so motion in frame is the body's."""

    def __init__(self, model: mujoco.MjModel, width: int = 640, height: int = 480) -> None:
        self.renderer = mujoco.Renderer(model, height=height, width=width)
        self.camera = mujoco.MjvCamera()
        self.camera.type = mujoco.mjtCamera.mjCAMERA_FREE
        self.camera.lookat[:] = [0.0, 0.0, 0.85]
        self.camera.distance = 2.6
        self.camera.azimuth = 140.0
        self.camera.elevation = -12.0

    def frame(self, data: mujoco.MjData):
        self.renderer.update_scene(data, camera=self.camera)
        # The policy's model has nlight == 0, so a free camera renders the scene
        # nearly black (measured mean RGB 18.7, 95% near-black). MuJoCo 3.17 does
        # not let us write model.nlight, but the MjvScene is writable, and a
        # headlight follows the camera for free. Measured after: mean RGB 56.7.
        scene = self.renderer.scene
        scene.nlight = 1
        light = scene.lights[0]
        light.headlight = 1
        light.diffuse[:] = [1.0, 1.0, 1.0]
        light.ambient[:] = [0.35, 0.35, 0.35]
        light.specular[:] = [0.3, 0.3, 0.3]
        light.castshadow = 0
        return self.renderer.render()


def write_atomic(path: Path, payload: bytes) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(payload)
    tmp.replace(path)


def reel_plan(
    executor: SonicExecutor,
) -> list[tuple[str, float, Callable[[], BodyCommand] | None]]:
    """The verb reel, as (name, seconds, command factory) triples.

    `hold` is the empty authority mask, so it is `None` rather than a command -
    there is no hold verb to send. Everything else reuses the executor's own
    target builders, so the reel cannot drift from what the executor supports.
    """
    return [
        ("hold", 1.5, None),
        ("look left 35", 3.0, lambda: BodyCommand(
            seq=0, authority=AUTH_HEAD, head=executor.look_target(math.radians(-35.0)))),
        ("hold", 1.5, None),
        ("look right 35", 3.0, lambda: BodyCommand(
            seq=0, authority=AUTH_HEAD, head=executor.look_target(math.radians(35.0)))),
        ("hold", 1.5, None),
        ("aim right", 2.5, lambda: BodyCommand(
            seq=0, authority=AUTH_R_HAND,
            right_hand=executor.aim_target("right", forward_m=0.22))),
        ("punch right", 2.0, lambda: BodyCommand(
            seq=0, authority=AUTH_R_HAND, right_hand=executor.punch_target("right"))),
        ("dodge left", 1.5, lambda: BodyCommand(
            seq=0, authority=AUTH_HEAD, head=executor.dodge_target(0.14))),
        ("hold", 2.0, None),
    ]


def verb_command(executor: SonicExecutor, name: str) -> BodyCommand | None:
    """Map a pushed button to a `BodyCommand`, using the executor's own builders.

    This is an adapter, exactly like the future game adapters: the contract stays
    `BodyCommand`, and a verb is only a shortcut for the pose it implies. Returns
    None for an unknown verb so the caller can report it rather than guess.
    """
    head_left = lambda: BodyCommand(
        seq=0, authority=AUTH_HEAD, head=executor.look_target(math.radians(-35.0)))
    head_right = lambda: BodyCommand(
        seq=0, authority=AUTH_HEAD, head=executor.look_target(math.radians(35.0)))
    verbs = {
        "hold": hold,
        "look_left": head_left,
        "look_right": head_right,
        "aim": lambda: BodyCommand(
            seq=0, authority=AUTH_R_HAND,
            right_hand=executor.aim_target("right", forward_m=0.22)),
        "punch": lambda: BodyCommand(
            seq=0, authority=AUTH_R_HAND, right_hand=executor.punch_target("right")),
        "dodge_left": lambda: BodyCommand(
            seq=0, authority=AUTH_HEAD, head=executor.dodge_target(0.14)),
        "dodge_right": lambda: BodyCommand(
            seq=0, authority=AUTH_HEAD, head=executor.dodge_target(-0.14)),
    }
    make = verbs.get(name)
    if make is None:
        return None
    return make()


class VerbServer:
    """Serve newline-JSON on a Unix socket: a verb name, or a full BodyCommand.

    Verbs are stamped here with a sequence number and a timestamp, so a UI never
    has to reason about clocks, and a stale press cannot rewind the body.
    """

    def __init__(self, path: Path, bus, executor: SonicExecutor) -> None:
        self.path = Path(path)
        self.bus = bus
        self.executor = executor
        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._seq = 0
        self._stop = False

    def start(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(FileNotFoundError):
            self.path.unlink()
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.bind(str(self.path))
        self._sock.listen(4)
        self._sock.settimeout(0.5)
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _handle(self, line: str) -> dict:
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as exc:
            return {"ok": False, "error": f"bad json: {exc}"}
        if not isinstance(raw, dict):
            return {"ok": False, "error": "expected an object"}

        if "verb" not in raw:
            result = self.bus.push_json(line)       # a full BodyCommand
            return {"ok": bool(result.ok), "error": result.reason or ""}

        cmd = verb_command(self.executor, str(raw["verb"]))
        if cmd is None:
            return {"ok": False, "error": f"unknown verb: {raw['verb']}"}
        self._seq += 1
        stamped = BodyCommand(**{
            **cmd.__dict__,
            "seq": self._seq,
            "t_mono_ns": time.monotonic_ns(),
            "ttl_ms": int(raw.get("ttl_ms", 2000)),
        })
        self.bus.push(stamped)
        return {"ok": True, "verb": raw["verb"], "seq": self._seq,
                "authority": stamped.authority}

    def _serve(self) -> None:
        sock = self._sock
        if sock is None:
            return
        while not self._stop:
            try:
                conn, _ = sock.accept()
            except (TimeoutError, OSError):
                continue
            with conn:
                buf = b""
                while not self._stop:
                    try:
                        chunk = conn.recv(65536)
                    except OSError:
                        break
                    if not chunk:
                        break
                    buf += chunk
                    while b"\n" in buf:
                        line, buf = buf.split(b"\n", 1)
                        if not line.strip():
                            continue
                        reply = self._handle(line.decode("utf-8", "replace"))
                        with contextlib.suppress(OSError):
                            conn.sendall((json.dumps(reply) + "\n").encode())

    def stop(self) -> None:
        self._stop = True
        if self._sock is not None:
            with contextlib.suppress(OSError):
                self._sock.close()
            self._sock = None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="body_view")
    ap.add_argument("--out", required=True, help="directory for body_latest.jpg and body_state.json")
    ap.add_argument("--seconds", type=float, default=0.0, help="0 = run forever")
    ap.add_argument("--fps", type=float, default=10.0, help="render cadence (policy still runs at 50 Hz)")
    ap.add_argument("--clip", default=None, help="motion clip (default: the hold clip)")
    ap.add_argument("--control", default=None,
                    help="Unix socket that accepts {\"verb\": ...} or a full BodyCommand")
    ap.add_argument("--no-render", action="store_true",
                    help="physics and state only; needs no GPU")
    ap.add_argument("--reel", action="store_true",
                    help="cycle the verb reel (hold, look, aim, punch, dodge) instead of holding")
    ap.add_argument("--look-deg", type=float, default=0.0, help="hold a fixed head yaw, for testing")
    ap.add_argument("--width", type=int, default=640)
    ap.add_argument("--height", type=int, default=480)
    args = ap.parse_args(argv)

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    executor = SonicExecutor(clip=args.clip) if args.clip else SonicExecutor()
    renderer = None
    if not args.no_render:
        renderer = BodyRenderer(executor.runner.model, width=args.width, height=args.height)

    bus = None
    server = None
    if args.control:
        bus = CommandBus()
        server = VerbServer(Path(args.control), bus, executor)
        server.start()
        print(f"body_view: control socket {args.control}", flush=True)

    if args.look_deg:
        cmd = BodyCommand(seq=0, authority=AUTH_HEAD,
                          head=executor.look_target(math.radians(args.look_deg)))
    else:
        cmd = hold()

    period = 1.0 / max(args.fps, 0.1)
    # The policy was designed for 50 Hz (see the three-loop design). Without a
    # sleep the loop free-ran at ~90 Hz and pinned a core for no benefit.
    control_period = 1.0 / 50.0
    started = time.monotonic()
    next_render = started
    renders = 0

    plan = reel_plan(executor) if args.reel else None
    phase_name = ""
    print(
        f"body_view: {'reel' if plan else 'hold'} clip {executor.clip} -> {out}",
        flush=True,
    )
    if plan:
        print("body_view: reel = " + " -> ".join(p[0] for p in plan), flush=True)
        phase_total = sum(p[1] for p in plan)
    try:
        while True:
            loop_start = time.monotonic()
            if bus is not None:
                cmd = bus.take(now_ns=time.monotonic_ns())
            elif plan:
                t = (time.monotonic() - started) % phase_total
                acc = 0.0
                for name, secs, factory in plan:
                    acc += secs
                    if t < acc:
                        cmd = factory() if factory else hold()
                        if name != phase_name:
                            phase_name = name
                            print(f"body_view: phase {name}", flush=True)
                        break
            state = executor.step(cmd, now_ns=time.monotonic_ns())
            now = time.monotonic()
            if bus is not None:
                bus.publish_state(state)
            if now >= next_render:
                if renderer is not None:
                    img = renderer.frame(executor.runner.data)
                    buf = io.BytesIO()
                    Image.fromarray(img).save(buf, format="JPEG", quality=85)
                    write_atomic(out / "body_latest.jpg", buf.getvalue())
                write_atomic(
                    out / "body_state.json",
                    json.dumps(state.to_dict()).encode("utf-8"),
                )
                next_render = now + period
                renders += 1
            if executor.fell:
                print("body_view: FALL detected, stopping", file=sys.stderr, flush=True)
                return 3
            if args.seconds and (now - started) >= args.seconds:
                break
            spare = control_period - (time.monotonic() - loop_start)
            if spare > 0:
                time.sleep(spare)
    except KeyboardInterrupt:
        pass

    if server is not None:
        server.stop()
    elapsed = time.monotonic() - started
    print(f"body_view: {renders} frames in {elapsed:.1f}s "
          f"({renders / max(elapsed, 1e-9):.1f} fps), ticks={executor.ticks}, "
          f"fell={executor.fell}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
