from __future__ import annotations

import csv
import json
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path

from superbot import pose_relay, wire


def sample_state(root_p=(0.0, 0.0, 0.79), head_p=(-0.001, 0.02, 0.40)) -> dict:
    return {
        "root_p": list(root_p),
        "root_q": [1.0, 0.0, 0.0, 0.0],
        "head": {"p": list(head_p), "q_xyzw": [0.0, 0.0, 0.0, 1.0]},
        "left_hand": {"p": [0.12, 0.26, -0.23], "q_xyzw": [0.0, 0.0, 0.0, 1.0]},
        "right_hand": {"p": [0.16, -0.25, -0.21], "q_xyzw": [0.0, 0.0, 0.0, 1.0]},
    }


class StateSourceTests(unittest.TestCase):
    def test_missing_file_is_not_an_error(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            source = pose_relay.StateSource(Path(raw) / "nope.json")
            state, age = source.read(1.0)
        self.assertIsNone(state)
        self.assertIsNone(age)

    def test_reads_and_reports_age(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "body_state.json"
            path.write_text(json.dumps(sample_state()), encoding="utf-8")
            source = pose_relay.StateSource(path)
            state, age = source.read(10.0)
            self.assertIsNotNone(state)
            self.assertIsNotNone(age)
            self.assertEqual(age, 0.0)
            _, later = source.read(10.5)
            self.assertAlmostEqual(later or -1, 0.5)

    def test_new_content_replaces_the_cached_document(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "body_state.json"
            path.write_text(json.dumps(sample_state(root_p=(1.0, 0.0, 0.79))), encoding="utf-8")
            source = pose_relay.StateSource(path)
            first, _ = source.read(1.0)
            assert first is not None
            path.write_text(json.dumps(sample_state(root_p=(2.0, 0.0, 0.79))), encoding="utf-8")
            second, age = source.read(2.0)
            assert second is not None
            self.assertEqual(first["root_p"][0], 1.0)
            self.assertEqual(second["root_p"][0], 2.0)
            self.assertIsNotNone(age)
            self.assertEqual(age, 0.0)

    def test_broken_json_keeps_the_previous_state(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "body_state.json"
            path.write_text(json.dumps(sample_state()), encoding="utf-8")
            source = pose_relay.StateSource(path)
            source.read(1.0)
            path.write_text("{not json", encoding="utf-8")
            state, _ = source.read(2.0)
        self.assertIsNotNone(state)


class PacketSourceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "body_state.json"
        self.source = pose_relay.PacketSource(pose_relay.StateSource(self.path), stale_after=0.3)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_rest_pose_before_any_state_exists(self) -> None:
        poses, note, age = self.source.poses(1.0)
        self.assertEqual(note, "rest")
        self.assertIsNone(age)
        self.assertEqual(poses["head"][0], wire.REST_POSES["head"][0])

    def test_live_state_is_converted(self) -> None:
        self.path.write_text(json.dumps(sample_state()), encoding="utf-8")
        poses, note, age = self.source.poses(1.0)
        self.assertEqual(note, "live")
        self.assertIsNotNone(age)
        self.assertEqual(age, 0.0)
        # head world z = 0.79 + 0.40 -> xr_y
        self.assertAlmostEqual(poses["head"][0][1], 1.19, places=6)

    def test_state_goes_stale_and_the_last_pose_is_held(self) -> None:
        self.path.write_text(json.dumps(sample_state()), encoding="utf-8")
        live, _, _ = self.source.poses(1.0)
        held, note, age = self.source.poses(1.5)
        self.assertEqual(note, "stale")
        self.assertAlmostEqual(age or -1, 0.5)
        self.assertEqual(held, live)

    def test_malformed_state_keeps_the_last_good_pose(self) -> None:
        self.path.write_text(json.dumps(sample_state()), encoding="utf-8")
        live, _, _ = self.source.poses(1.0)
        self.path.write_text(json.dumps({"root_p": [0, 0, 0]}), encoding="utf-8")
        held, note, _ = self.source.poses(2.0)
        self.assertEqual(note, "malformed")
        self.assertEqual(held, live)


class HandshakeTests(unittest.TestCase):
    def test_accepts_two_packets(self) -> None:
        a, b = socket.socketpair()
        try:
            payload = bytes(wire.blank_packet()) * 2
            threading.Thread(target=lambda: b.sendall(payload), daemon=True).start()
            pose_relay.read_handshake(a)
        finally:
            a.close()
            b.close()

    def test_rejects_a_wrong_header(self) -> None:
        a, b = socket.socketpair()
        try:
            bad = bytes(wire.SIZE * 2)
            threading.Thread(target=lambda: b.sendall(bad), daemon=True).start()
            with self.assertRaises(ValueError):
                pose_relay.read_handshake(a)
        finally:
            a.close()
            b.close()

    def test_reports_a_closed_peer(self) -> None:
        a, b = socket.socketpair()
        try:
            b.close()
            with self.assertRaises(ConnectionError):
                pose_relay.read_handshake(a)
        finally:
            a.close()


class FakeDriver(threading.Thread):
    """Stands in for the Monado remote driver: handshake, then count packets."""

    def __init__(self) -> None:
        super().__init__(daemon=True)
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(1)
        self.port = self.listener.getsockname()[1]
        self.packets = 0
        self.closed = threading.Event()

    def run(self) -> None:
        conn, _ = self.listener.accept()
        try:
            conn.sendall(bytes(wire.blank_packet()))
            conn.sendall(bytes(wire.blank_packet()))
            buf = b""
            while True:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                buf += chunk
                self.packets += len(buf) // wire.SIZE
                buf = buf[len(buf) // wire.SIZE * wire.SIZE:]
        finally:
            conn.close()
            self.listener.close()
            self.closed.set()


class RelayRunTests(unittest.TestCase):
    def test_sends_packets_at_the_requested_rate(self) -> None:
        driver = FakeDriver()
        driver.start()
        with tempfile.TemporaryDirectory() as raw:
            state_path = Path(raw) / "body_state.json"
            state_path.write_text(json.dumps(sample_state()), encoding="utf-8")
            log_path = Path(raw) / "poses.csv"
            code = pose_relay.run("127.0.0.1", driver.port, state_path, log_path,
                                  duration=0.6, hz=50.0)
            driver.closed.wait(5)
            rows = list(csv.DictReader(log_path.read_text().splitlines()))
        self.assertEqual(code, 0)
        self.assertGreaterEqual(driver.packets, 15)
        self.assertGreaterEqual(len(rows), 15)
        self.assertEqual(rows[0]["note"], "live")
        self.assertGreater(float(rows[0]["hy"]), 1.0)  # head above the hands

    def test_log_is_readable_while_the_relay_runs(self) -> None:
        # A relay that runs for an hour must not hold its evidence in a buffer:
        # the CSV is the only record of what was actually sent.
        driver = FakeDriver()
        driver.start()
        header_bytes = len(",".join(pose_relay.LOG_HEADER))
        with tempfile.TemporaryDirectory() as raw:
            state_path = Path(raw) / "body_state.json"
            state_path.write_text(json.dumps(sample_state()), encoding="utf-8")
            log_path = Path(raw) / "poses.csv"
            threading.Thread(
                target=pose_relay.run,
                args=("127.0.0.1", driver.port, state_path, log_path, 3.0, 50.0),
                daemon=True,
            ).start()
            deadline = time.monotonic() + 8.0
            rows = 0
            while time.monotonic() < deadline:
                time.sleep(0.2)
                if log_path.exists() and log_path.stat().st_size > header_bytes + 20:
                    rows = len(log_path.read_text().splitlines())
                    break
        self.assertGreater(rows, 1, "the CSV stayed empty while the relay was running")

    def test_exits_cleanly_when_the_driver_hangs_up(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state_path = Path(raw) / "body_state.json"
            state_path.write_text(json.dumps(sample_state()), encoding="utf-8")
            listener = socket.socket()
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            port = listener.getsockname()[1]

            def serve() -> None:
                conn, _ = listener.accept()
                conn.sendall(bytes(wire.blank_packet()) * 2)
                conn.recv(wire.SIZE)
                conn.close()
                listener.close()

            threading.Thread(target=serve, daemon=True).start()
            code = pose_relay.run("127.0.0.1", port, state_path, None, duration=5.0, hz=50.0)
        self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()
