from __future__ import annotations

import json
import math
import re
import socket
import threading
import time
from pathlib import Path
from typing import Callable


LEASE_SECONDS = 0.5

MANUAL_KEYS = frozenset(
    {
        "1",
        "2",
        "3",
        "4",
        "5",
        "tab",
        "q",
        "w",
        "e",
        "r",
        "t",
        "u",
        "i",
        "o",
        "p",
        "enter",
        "ctrl",
        "a",
        "s",
        "d",
        "f",
        "g",
        "h",
        "j",
        "k",
        "l",
        "shift",
        "z",
        "x",
        "c",
        "v",
        "b",
        "n",
        "m",
        "space",
        "up",
        "down",
        "left",
        "right",
    }
)
MANUAL_BUTTONS = frozenset({0, 1, 2})
CLIENT_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")


class ManualInputError(ValueError):
    pass


class ManualInputUnavailable(RuntimeError):
    pass


def _number(name: str, value) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ManualInputError(f"{name} must be a number")
    number = float(value)
    if not math.isfinite(number) or abs(number) > 10_000:
        raise ManualInputError(f"{name} must be finite and bounded")
    return number


def _keys(value) -> list[str]:
    if not isinstance(value, list) or len(value) > 64:
        raise ManualInputError("keys must be a list of at most 64 names")
    result: set[str] = set()
    for item in value:
        if not isinstance(item, str) or item not in MANUAL_KEYS:
            raise ManualInputError(f"unknown key: {item!r}")
        result.add(item)
    return sorted(result)


def _buttons(value) -> list[int]:
    if not isinstance(value, list) or len(value) > 8:
        raise ManualInputError("buttons must be a list of at most 8 numbers")
    result: set[int] = set()
    for item in value:
        if isinstance(item, bool) or not isinstance(item, int) or item not in MANUAL_BUTTONS:
            raise ManualInputError(f"unknown button: {item!r}")
        result.add(item)
    return sorted(result)


def _client(value) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not CLIENT_RE.fullmatch(value):
        raise ManualInputError("client must be an 8-64 character identifier")
    return value


class ManualInputBridge:
    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._path: Path | None = None
        self._socket: socket.socket | None = None
        self._client: str | None = None
        self._armed = False
        self._last_input_at: float | None = None
        self._last_error: str | None = None

    def configure(self, path: Path) -> None:
        with self._lock:
            self._close_locked()
            self._path = Path(path)
            self._client = None
            self._armed = False
            self._last_input_at = None
            self._last_error = None

    def clear(self) -> None:
        with self._lock:
            self._close_locked()
            self._client = None
            self._armed = False
            self._last_input_at = None
            self._last_error = None

    def close(self) -> None:
        with self._lock:
            self._close_locked()

    def update(self, payload: dict) -> bool:
        if not isinstance(payload, dict):
            raise ManualInputError("payload must be an object")
        armed = payload.get("armed")
        if not isinstance(armed, bool):
            raise ManualInputError("armed must be a boolean")
        client = _client(payload.get("client"))
        if armed:
            if client is None:
                raise ManualInputError("client is required when armed")
            keys = _keys(payload.get("keys", []))
            buttons = _buttons(payload.get("buttons", []))
            dx = _number("dx", payload.get("dx", 0))
            dy = _number("dy", payload.get("dy", 0))
            wheel = _number("wheel", payload.get("wheel", 0))
            message = {
                "armed": True,
                "keys": keys,
                "buttons": buttons,
                "dx": dx,
                "dy": dy,
                "wheel": wheel,
            }
        else:
            message = {"armed": False}

        with self._lock:
            self._client = client or self._client
            if not armed:
                self._armed = False
                self._last_input_at = None
                self._send_locked(message, required=False)
                return False
            self._armed = True
            self._last_input_at = self._clock()
            self._send_locked(message, required=True)
            return True

    def snapshot(self) -> dict:
        with self._lock:
            if (
                self._armed
                and self._last_input_at is not None
                and self._clock() - self._last_input_at > LEASE_SECONDS
            ):
                self._armed = False
                self._last_input_at = None
            available = (
                self._path is not None
                and self._path.exists()
                and self._last_error is None
            )
            return {
                "armed": self._armed,
                "available": available,
                "client": self._client,
                "last_error": self._last_error,
            }

    def _send_locked(self, message: dict, *, required: bool) -> None:
        if self._path is None:
            if required:
                raise ManualInputUnavailable("manual input socket is not configured")
            return
        if self._socket is None:
            self._socket = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
            self._socket.setblocking(False)
        try:
            self._socket.sendto(
                json.dumps(message, separators=(",", ":")).encode(),
                str(self._path),
            )
        except OSError as exc:
            self._last_error = str(exc)
            self._armed = False
            if required:
                raise ManualInputUnavailable(f"manual input unavailable: {exc}") from exc
            return
        self._last_error = None

    def _close_locked(self) -> None:
        if self._socket is not None:
            self._socket.close()
            self._socket = None
