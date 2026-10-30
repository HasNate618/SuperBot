#!/usr/bin/env python3
"""Feed the body's *achieved* poses to the game, replacing the scripted client.

The chain is:

    SONIC body_view (one sim) -> body_state.json -> this relay -> TCP 4242
        -> Monado `remote` driver -> Superhot

This is the join. `spike/c0/c0_client.py` sends scripted constants; this sends
whatever the simulated humanoid actually did, so what the body tile shows and
what the game sees are the same body rather than two lookalikes.

Stdlib only, on purpose: it imports neither mujoco nor numpy, so it runs under
the same interpreter as the dashboard and the test suite can cover it.

Staleness is explicit. If the state file stops advancing the relay keeps sending
the last pose and marks the CSV `stale`, because Monado freezes the far end on
disconnect and a frozen-but-connected client is easier to diagnose than a game
that silently stops responding. Before any state has ever arrived it sends the
measured standing pose, so the game sees a humanoid at 1.221 m, not the origin.
"""

from __future__ import annotations

import argparse
import csv
import json
import socket
import struct
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

try:  # both shapes work: `-m superbot.pose_relay` and a direct file run
    from . import wire
except ImportError:  # pragma: no cover - only when executed as a script
    import wire  # type: ignore[no-redef]

#: How old body_state.json may be before the relay reports it stale, seconds.
STALE_AFTER_SEC = 0.30

#: Ways to force a button without the body cooperating, for the hub A/B.
FORCE_MODES = ("none", "left", "right", "both")

#: Which wire field a hand closure drives. The game decides which one it reads,
#: so this is a switch rather than a decision baked into the relay.
GRAB_MODES = ("squeeze", "curl", "both")

#: The driver closes the connection if it is not reading; a few seconds of retry
#: covers Monado still coming up when the session starts.
CONNECT_TIMEOUT_SEC = 20.0


def forced_pair(mode: str) -> tuple[float, float]:
    """(left, right) for a CLI force mode. 'none' releases both."""
    return {
        "none": (0.0, 0.0),
        "left": (1.0, 0.0),
        "right": (0.0, 1.0),
        "both": (1.0, 1.0),
    }[mode]


def buttons_from_state(
    state: Mapping[str, Any],
) -> tuple[tuple[float, float], tuple[float, float]]:
    """Read (closure, trigger) out of a state dict, as (left, right).

    Tolerates a body that predates the fields, and a malformed pair: a bad button
    field releases the button rather than killing the pipe.
    """

    def pair(key: str) -> tuple[float, float]:
        raw = state.get(key)
        if not isinstance(raw, (list, tuple)) or len(raw) != 2:
            return (0.0, 0.0)
        try:
            return (float(raw[0]), float(raw[1]))
        except (TypeError, ValueError):
            return (0.0, 0.0)

    return pair("closure"), pair("trigger")


class ClickEdge:
    """Turns a held trigger value into one click per press, per hand.

    The wire format carries a click as well as a value, and a click is an edge.
    Sending click=true on every packet while the trigger is down is what the old
    fill() did; Unity reads that as a fresh press every frame.
    """

    def __init__(self) -> None:
        self._prev: tuple[float, float] = (0.0, 0.0)

    def update(self, values: Sequence[float]) -> tuple[bool, bool]:
        clicks = tuple(
            v > 0.0 and p <= 0.0 for v, p in zip(values, self._prev, strict=True)
        )
        self._prev = (float(values[0]), float(values[1]))
        return (clicks[0], clicks[1])


class StateSource:
    """Reads a BodyState JSON file, re-parsing only when its mtime changes."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._mtime_ns: int | None = None
        self._doc: dict[str, Any] | None = None
        self._seen_at: float | None = None

    def read(self, now: float) -> tuple[dict[str, Any] | None, float | None]:
        """Return (state, age_seconds); age is None when nothing was ever read."""
        try:
            mtime_ns = self.path.stat().st_mtime_ns
        except OSError:
            return self._doc, (now - self._seen_at if self._seen_at is not None else None)
        if mtime_ns != self._mtime_ns:
            try:
                self._doc = json.loads(self.path.read_text(encoding="utf-8"))
                self._mtime_ns = mtime_ns
                self._seen_at = now
            except (OSError, ValueError):
                pass
        age = now - self._seen_at if self._seen_at is not None else None
        return self._doc, age


class PacketSource:
    """Turns body states into XR poses, holding through gaps."""

    def __init__(self, source: StateSource, stale_after: float = STALE_AFTER_SEC) -> None:
        self.source = source
        self.stale_after = stale_after
        self._last = wire.REST_POSES
        self._last_note = "rest"
        #: The most recent state that parsed, so buttons can be forwarded even
        #: when the pose is held or stale. A dead body must not release a grab.
        self.last_state: dict[str, Any] | None = None

    def poses(self, now: float) -> tuple[dict[str, Any], str, float | None]:
        state, age = self.source.read(now)
        if state is not None:
            self.last_state = state
        if state is None:
            return self._last, self._last_note if self._last_note != "live" else "stale", age
        try:
            poses = wire.body_state_to_xr(state)
        except (KeyError, TypeError, ValueError):
            # A half-written or shape-changed file must not kill the pipe.
            return self._last, "malformed", age
        if age is not None and age > self.stale_after:
            self._last_note = "stale"
            return self._last, "stale", age
        self._last = poses
        self._last_note = "live"
        return poses, "live", age


def read_handshake(sock: socket.socket, size: int = wire.SIZE) -> None:
    """The driver replies with two packets on connect; both must carry our header."""
    for name in ("reset", "latest"):
        buf = bytearray()
        while len(buf) < size:
            chunk = sock.recv(size - len(buf))
            if not chunk:
                raise ConnectionError(f"peer closed during the {name} handshake")
            buf.extend(chunk)
        header = struct.unpack_from("<Q", bytes(buf))[0]
        if header != wire.HEADER:
            raise ValueError(f"handshake {name} header 0x{header:016x} != expected")


def connect(host: str, port: int, timeout: float = CONNECT_TIMEOUT_SEC) -> socket.socket:
    deadline = time.monotonic() + timeout
    while True:
        try:
            sock = socket.create_connection((host, port), timeout=5)
        except OSError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.25)
            continue
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        read_handshake(sock)
        return sock


def log_row(writer: Any, t: float, poses: dict[str, Any], note: str, age: float | None,
            grip: tuple[float, float] = (0.0, 0.0),
            fire: tuple[float, float] = (0.0, 0.0)) -> None:
    hp, hq = poses["head"]
    lp, _ = poses["left"]
    rp, _ = poses["right"]
    writer.writerow([
        f"{t:.4f}",
        f"{hp[0]:.4f}", f"{hp[1]:.4f}", f"{hp[2]:.4f}",
        f"{hq[0]:.5f}", f"{hq[1]:.5f}", f"{hq[2]:.5f}", f"{hq[3]:.5f}",
        f"{lp[0]:.4f}", f"{lp[1]:.4f}", f"{lp[2]:.4f}",
        f"{rp[0]:.4f}", f"{rp[1]:.4f}", f"{rp[2]:.4f}",
        f"{grip[0]:.1f}", f"{grip[1]:.1f}", f"{fire[0]:.1f}", f"{fire[1]:.1f}",
        "" if age is None else f"{age:.3f}",
        note,
    ])


LOG_HEADER = ["t", "hx", "hy", "hz", "hqx", "hqy", "hqz", "hqw",
              "lx", "ly", "lz", "rx", "ry", "rz",
              "grip_l", "grip_r", "fire_l", "fire_r", "state_age", "note"]


def run(host: str, port: int, state_path: Path, log_path: Path | None,
        duration: float, hz: float, force_grab: str = "none",
        force_trigger: str = "none", grab_mode: str = "both") -> int:
    wire.assert_layout()
    source = PacketSource(StateSource(state_path))
    sock = connect(host, port)
    print(f"pose_relay: connected {host}:{port}", flush=True)
    print("pose_relay: handshake ok", flush=True)
    print(f"pose_relay: state {state_path} at {hz:g} Hz", flush=True)

    period = 1.0 / max(hz, 1.0)
    started = time.monotonic()
    sent = 0
    notes: dict[str, int] = {}
    edge = ClickEdge()
    grip_force = forced_pair(force_grab)
    fire_force = forced_pair(force_trigger)
    handle = None
    writer = None
    try:
        if log_path is not None:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            # Line buffered: a 3600 s relay only closes the file at the end, so an
            # unflushed CSV is empty exactly while someone is trying to read it.
            handle = log_path.open("w", newline="", buffering=1)
            writer = csv.writer(handle)
            writer.writerow(LOG_HEADER)
        stream = sock.makefile("wb")
        next_tick = started
        while True:
            now = time.monotonic()
            if now - started >= duration:
                break
            poses, note, age = source.poses(now)
            # Buttons: a forced mode wins, otherwise the body's own state. Both
            # are per hand, and the two channels stay separate so a gun can be
            # held while the trigger is pulled.
            grip_state, fire_state = (
                buttons_from_state(source.last_state)
                if isinstance(source.last_state, Mapping)
                else ((0.0, 0.0), (0.0, 0.0))
            )
            grip = grip_force if grip_force != (0.0, 0.0) else grip_state
            fire = fire_force if fire_force != (0.0, 0.0) else fire_state
            packet = wire.fill(
                wire.blank_packet(),
                poses,
                grip=grip if grab_mode in ("squeeze", "both") else (0.0, 0.0),
                curl=grip if grab_mode in ("curl", "both") else (0.0, 0.0),
                trigger=fire,
                trigger_click=edge.update(fire),
            )
            stream.write(bytes(packet))
            stream.flush()
            sent += 1
            notes[note] = notes.get(note, 0) + 1
            if writer is not None:
                log_row(writer, now - started, poses, note, age, grip, fire)
            next_tick += period
            sleep = next_tick - time.monotonic()
            if sleep > 0:
                time.sleep(sleep)
            else:
                next_tick = time.monotonic()
    except (BrokenPipeError, ConnectionResetError) as exc:
        print(f"pose_relay: driver closed the connection: {exc}", flush=True)
        return 1
    except KeyboardInterrupt:
        print("\npose_relay: interrupted", flush=True)
    finally:
        if handle is not None:
            handle.close()
    elapsed = time.monotonic() - started
    print(f"pose_relay: sent {sent} packets in {elapsed:.2f} s "
          f"({sent / elapsed:.1f} Hz), notes {notes}", flush=True)
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="pose_relay", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=4242)
    ap.add_argument("--state", required=True, help="body_state.json written by the body view")
    ap.add_argument("--log", default=None, help="CSV of every emitted pose")
    ap.add_argument("--duration", type=float, default=3600.0)
    ap.add_argument("--hz", type=float, default=50.0)
    ap.add_argument("--force-grab", choices=FORCE_MODES, default="none",
                    help="hold one hand closed regardless of the body, for the hub A/B")
    ap.add_argument("--force-trigger", choices=FORCE_MODES, default="none",
                    help="hold one trigger pressed regardless of the body")
    ap.add_argument("--grab-mode", choices=GRAB_MODES, default="both",
                    help="which wire field a closure drives (the game decides which it reads)")
    args = ap.parse_args(argv)
    log_path = Path(args.log) if args.log else None
    return run(args.host, args.port, Path(args.state), log_path, args.duration, args.hz,
               force_grab=args.force_grab, force_trigger=args.force_trigger,
               grab_mode=args.grab_mode)


if __name__ == "__main__":
    raise SystemExit(main())
