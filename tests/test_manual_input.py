from __future__ import annotations

import json
import select
import socket
import tempfile
import unittest
from pathlib import Path

from clank.manual_input import (
    ManualInputBridge,
    ManualInputError,
    ManualInputUnavailable,
)


class FakeClock:
    def __init__(self, now: float = 100.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class Receiver:
    def __init__(self, path: Path) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        self.sock.bind(str(path))
        self.sock.setblocking(False)

    def recv(self, timeout: float = 1.0):
        ready, _, _ = select.select([self.sock], [], [], timeout)
        if not ready:
            return None
        data, _ = self.sock.recvfrom(65536)
        return json.loads(data)

    def close(self) -> None:
        self.sock.close()


class ManualInputBridgeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.path = self.base / "manual.sock"
        self.receiver = Receiver(self.path)
        self.clock = FakeClock()
        self.bridge = ManualInputBridge(clock=self.clock)
        self.bridge.configure(self.path)

    def tearDown(self) -> None:
        self.bridge.close()
        self.receiver.close()
        self.temp.cleanup()

    def batch(self, **overrides) -> dict:
        payload = {
            "client": "browser-1234",
            "seq": 1,
            "armed": True,
            "keys": ["w", "shift"],
            "buttons": [0],
            "dx": 12.5,
            "dy": -3.0,
            "wheel": 1,
        }
        payload.update(overrides)
        return payload

    def test_armed_batch_forwards_state_and_marks_armed(self) -> None:
        self.assertTrue(self.bridge.update(self.batch()))
        self.assertEqual(
            self.receiver.recv(),
            {
                "armed": True,
                "keys": ["shift", "w"],
                "buttons": [0],
                "dx": 12.5,
                "dy": -3.0,
                "wheel": 1,
            },
        )
        snapshot = self.bridge.snapshot()
        self.assertTrue(snapshot["armed"])
        self.assertTrue(snapshot["available"])
        self.assertEqual(snapshot["client"], "browser-1234")

    def test_disarm_batch_forwards_clear_and_marks_disarmed(self) -> None:
        self.assertTrue(self.bridge.update(self.batch()))
        self.assertIsNotNone(self.receiver.recv())
        self.assertFalse(self.bridge.update({"client": "browser-1234", "armed": False}))
        self.assertEqual(self.receiver.recv(), {"armed": False})
        snapshot = self.bridge.snapshot()
        self.assertFalse(snapshot["armed"])
        self.assertTrue(snapshot["available"])

    def test_lease_expires_without_new_batches(self) -> None:
        self.assertTrue(self.bridge.update(self.batch()))
        self.clock.now += 0.6
        self.assertFalse(self.bridge.snapshot()["armed"])

    def test_armed_batch_requires_client(self) -> None:
        with self.assertRaisesRegex(ManualInputError, "client"):
            self.bridge.update(self.batch(client=None))

    def test_rejects_unknown_keys(self) -> None:
        with self.assertRaisesRegex(ManualInputError, "key"):
            self.bridge.update(self.batch(keys=["nope"]))
        self.assertIsNone(self.receiver.recv(0.05))

    def test_rejects_bad_button(self) -> None:
        with self.assertRaisesRegex(ManualInputError, "button"):
            self.bridge.update(self.batch(buttons=[9]))

    def test_rejects_non_finite_delta(self) -> None:
        with self.assertRaisesRegex(ManualInputError, "dx"):
            self.bridge.update(self.batch(dx=float("inf")))

    def test_rejects_bad_armed_type(self) -> None:
        with self.assertRaisesRegex(ManualInputError, "armed"):
            self.bridge.update(self.batch(armed="yes"))

    def test_missing_socket_raises_for_armed_input(self) -> None:
        self.bridge.configure(self.base / "missing.sock")
        with self.assertRaises(ManualInputUnavailable):
            self.bridge.update(self.batch())
        snapshot = self.bridge.snapshot()
        self.assertFalse(snapshot["armed"])
        self.assertFalse(snapshot["available"])
        self.assertTrue(snapshot["last_error"])

    def test_missing_socket_disarm_is_best_effort(self) -> None:
        self.bridge.configure(self.base / "missing.sock")
        self.assertFalse(self.bridge.update({"client": "browser-1234", "armed": False}))
        self.assertFalse(self.bridge.snapshot()["armed"])

    def test_new_client_replaces_previous_client(self) -> None:
        self.assertTrue(self.bridge.update(self.batch(client="browser-aaaa")))
        self.assertTrue(self.bridge.update(self.batch(client="browser-bbbb")))
        self.assertEqual(self.bridge.snapshot()["client"], "browser-bbbb")

    def test_bad_client_id_is_rejected(self) -> None:
        with self.assertRaisesRegex(ManualInputError, "client"):
            self.bridge.update(self.batch(client="../bad"))


if __name__ == "__main__":
    unittest.main()
