from __future__ import annotations

import importlib.util
import json
import socket
import tempfile
import unittest
from pathlib import Path

MANUAL_PATH = Path(__file__).resolve().parents[1] / "spike" / "c0" / "c0_manual.py"
SPEC = importlib.util.spec_from_file_location("c0_manual_test", MANUAL_PATH)
assert SPEC is not None and SPEC.loader is not None
manual = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(manual)


class FakeClock:
    def __init__(self, now: float = 100.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class BrowserInputTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.path = self.base / "manual.sock"
        self.clock = FakeClock()
        self.input = manual.BrowserInput(self.path, clock=self.clock)
        self.client = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        self.client.connect(str(self.path))

    def tearDown(self) -> None:
        self.client.close()
        self.input.close()
        self.temp.cleanup()

    def send(self, payload) -> None:
        self.client.send(json.dumps(payload).encode())

    def test_armed_batch_sets_keys_buttons_and_mouse(self) -> None:
        self.send(
            {
                "armed": True,
                "keys": ["w", "shift"],
                "buttons": [0, 2],
                "dx": 4,
                "dy": -2,
                "wheel": 1,
            }
        )
        self.input.read()
        self.assertTrue(self.input.armed)
        self.assertTrue(self.input.down("w"))
        self.assertTrue(self.input.down("shift"))
        self.assertEqual(self.input.buttons, {0, 2})
        self.assertEqual(self.input.take_mouse(), (4, -2, 1))
        self.assertEqual(self.input.take_mouse(), (0, 0, 0))

    def test_disarm_clears_state_and_ignores_input(self) -> None:
        self.send({"armed": True, "keys": ["w"], "buttons": [0], "dx": 2, "dy": 3, "wheel": 1})
        self.input.read()
        self.send({"armed": False})
        self.input.read()
        self.assertFalse(self.input.armed)
        self.assertFalse(self.input.down("w"))
        self.assertEqual(self.input.buttons, set())
        self.assertEqual(self.input.take_mouse(), (0, 0, 0))

    def test_stale_batch_disarms(self) -> None:
        self.send({"armed": True, "keys": ["w"], "buttons": [0], "dx": 1, "dy": 2, "wheel": 0})
        self.input.read()
        self.clock.now += manual.INPUT_LEASE_SECONDS + 0.1
        self.input.read()
        self.assertFalse(self.input.armed)
        self.assertFalse(self.input.down("w"))

    def test_malformed_and_unknown_keys_are_ignored(self) -> None:
        self.client.send(b"{")
        self.input.read()
        self.assertFalse(self.input.armed)
        self.send({"armed": True, "keys": ["nope", "w"], "buttons": [99], "dx": 1, "dy": 0, "wheel": 0})
        self.input.read()
        self.assertTrue(self.input.down("w"))
        self.assertEqual(self.input.buttons, set())

    def test_close_removes_socket(self) -> None:
        self.input.close()
        self.assertFalse(self.path.exists())


if __name__ == "__main__":
    unittest.main()
