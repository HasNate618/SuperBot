from __future__ import annotations

import http.client
import json
import socket
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

from vrbot.config import load_config
from vrbot.dashboard import DashboardHandler, DashboardServer
from vrbot.orchestrator import ConflictError, MODES, PreflightError


class FakeManager:
    def __init__(self, base: Path) -> None:
        self.base = base
        self.state = "stopped"
        self.session_id = "20261025T223900Z-a1b2c3"
        self.started_at = None
        self.mode = "hold"
        self.last_error = None
        self.fail_start = False
        self.start_calls = 0
        self.stop_calls = 0
        self.kill_calls: list[str] = []
        self.logs = {name: base / f"{name}.log" for name in ("session", "monado", "weston", "steam", "pose")}
        for path in self.logs.values():
            path.write_text("")
        self.latest = base / "latest.jpg"

    def start(self) -> None:
        self.start_calls += 1
        if self.fail_start and self.start_calls == 1:
            raise PreflightError("Close the host Steam client before starting a hidden C0 session.")
        if self.state not in ("stopped", "failed"):
            raise ConflictError("session is not stopped")
        self.state = "starting"

    def stop(self) -> None:
        self.stop_calls += 1
        self.state = "stopping"

    def set_mode(self, mode: str) -> None:
        if mode not in MODES:
            raise ValueError(f"unknown mode {mode!r}")
        if self.state != "running":
            raise ConflictError("session is not running")
        self.mode = mode

    def kill(self, target: str) -> None:
        if target not in ("game", "steam"):
            raise ValueError(f"unknown target {target!r}")
        if self.state != "running":
            raise ConflictError("session is not running")
        self.kill_calls.append(target)

    def state_value(self) -> str:
        return self.state

    def log_path(self, name: str) -> Path | None:
        return self.logs.get(name)

    def preview_path(self) -> Path:
        return self.latest

    def snapshot(self) -> dict:
        return {
            "state": self.state,
            "session_id": self.session_id,
            "started_at": self.started_at,
            "mode": self.mode,
            "processes": {
                "monado": {"alive": True, "pid": 1},
                "weston": {"alive": True, "pid": 2, "display": ":2"},
                "steam": {"alive": True, "pid": 3},
                "game": {"alive": True, "pid": 4, "exe": "SUPERHOT_VR.exe"},
                "pose": {"alive": True, "pid": 5},
            },
            "openxr_client": "Unity Application",
            "preview": {
                "width": 1280,
                "height": 688,
                "fps_target": 8,
                "fps_actual": 7.4,
                "last_frame_at": "2026-10-25T22:39:00Z",
                "errors": 0,
                "frames": 10,
            },
            "last_error": self.last_error,
            "logs": {name: str(path) for name, path in self.logs.items()},
        }


class DashboardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        (self.base / "index.html").write_text("<h1>C0</h1>")
        (self.base / "style.css").write_text("body{}")
        (self.base / "app.js").write_text("console.log('x')")
        self.manager = FakeManager(self.base)
        self.cfg = load_config(None, env={})
        self.server = DashboardServer(("127.0.0.1", 0), self.manager, self.cfg, self.base)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)
        self.temp.cleanup()

    def request(self, method: str, path: str, body=None, raw: bytes | None = None):
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=data,
            method=method,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as exc:
            try:
                return exc.code, exc.read()
            finally:
                exc.close()

    def raw_request(self, method: str, path: str, headers: dict[str, str] | None = None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=2)
        try:
            connection.putrequest(method, path)
            for name, value in (headers or {}).items():
                connection.putheader(name, value)
            connection.endheaders()
            response = connection.getresponse()
            return response.status, response.read()
        finally:
            connection.close()

    def test_state(self) -> None:
        status, body = self.request("GET", "/api/state")
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual(payload["openxr_client"], "Unity Application")
        self.assertEqual(payload["preview"]["height"], 688)

    def test_static_index(self) -> None:
        status, body = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(b"C0", body)

    def test_frame_404_then_200(self) -> None:
        status, _ = self.request("GET", "/api/frame.jpg")
        self.assertEqual(status, 404)
        self.manager.latest.write_bytes(b"JPEG")
        status, body = self.request("GET", "/api/frame.jpg")
        self.assertEqual(status, 200)
        self.assertEqual(body, b"JPEG")

    def test_start_returns_202_then_conflict(self) -> None:
        status, _ = self.request("POST", "/api/session/start")
        self.assertEqual(status, 202)
        status, body = self.request("POST", "/api/session/start")
        self.assertEqual(status, 409)
        self.assertIn("error", json.loads(body))

    def test_preflight_failure_returns_400(self) -> None:
        self.manager.fail_start = True
        status, body = self.request("POST", "/api/session/start")
        self.assertEqual(status, 400)
        self.assertIn("host Steam", json.loads(body)["error"])

    def test_stop_returns_202(self) -> None:
        status, _ = self.request("POST", "/api/session/stop")
        self.assertEqual(status, 202)
        self.assertEqual(self.manager.stop_calls, 1)

    def test_mode_and_kill_validation(self) -> None:
        status, _ = self.request("POST", "/api/session/mode", {"mode": "yaw"})
        self.assertEqual(status, 409)
        self.manager.state = "running"
        status, _ = self.request("POST", "/api/session/mode", {"mode": "yaw"})
        self.assertEqual(status, 200)
        self.assertEqual(self.manager.mode, "yaw")
        status, _ = self.request("POST", "/api/session/mode", {"mode": "nope"})
        self.assertEqual(status, 400)
        status, _ = self.request("POST", "/api/session/kill", {"target": "game"})
        self.assertEqual(status, 200)
        self.assertEqual(self.manager.kill_calls, ["game"])
        status, _ = self.request("POST", "/api/session/kill", {"target": "nope"})
        self.assertEqual(status, 400)

    def test_logs_allowlist(self) -> None:
        self.manager.logs["monado"].write_text("hello monado")
        status, body = self.request("GET", "/api/logs/monado")
        self.assertEqual(status, 200)
        self.assertEqual(body, b"hello monado")
        status, _ = self.request("GET", "/api/logs/nope")
        self.assertEqual(status, 404)

    def test_logs_reject_traversal(self) -> None:
        self.manager.logs["monado"].write_text("secret")
        for path in ("/api/logs/../monado", "/api/logs/%2e%2e%2fmonado"):
            with self.subTest(path=path):
                status, body = self.raw_request("GET", path)
                self.assertEqual(status, 404)
                self.assertNotIn(b"secret", body)

    def test_logs_missing_or_unreadable_returns_404(self) -> None:
        path = self.manager.logs["monado"]
        path.unlink()
        status, _ = self.request("GET", "/api/logs/monado")
        self.assertEqual(status, 404)
        path.mkdir()
        status, _ = self.request("GET", "/api/logs/monado")
        self.assertEqual(status, 404)

    def test_logs_returns_last_200_lines(self) -> None:
        self.manager.logs["monado"].write_text("\n".join(f"line {index}" for index in range(250)))
        status, body = self.request("GET", "/api/logs/monado")
        self.assertEqual(status, 200)
        lines = body.decode().splitlines()
        self.assertEqual(len(lines), 200)
        self.assertEqual(lines[0], "line 50")
        self.assertEqual(lines[-1], "line 249")

    def test_malformed_json(self) -> None:
        status, _ = self.request("POST", "/api/session/mode", raw=b"{")
        self.assertEqual(status, 400)

    def test_negative_content_length_returns_400(self) -> None:
        status, _ = self.raw_request(
            "POST",
            "/api/session/mode",
            {"Content-Type": "application/json", "Content-Length": "-1"},
        )
        self.assertEqual(status, 400)

    def test_mode_exception_returns_500_without_detail(self) -> None:
        self.manager.state = "running"
        with patch.object(self.manager, "set_mode", side_effect=RuntimeError("boom")):
            status, body = self.request("POST", "/api/session/mode", {"mode": "yaw"})
        self.assertEqual(status, 500)
        self.assertEqual(json.loads(body)["error"], "internal server error")
        self.assertNotIn(b"boom", body)

    def test_kill_exception_returns_500_without_detail(self) -> None:
        self.manager.state = "running"
        with patch.object(self.manager, "kill", side_effect=RuntimeError("boom")):
            status, body = self.request("POST", "/api/session/kill", {"target": "game"})
        self.assertEqual(status, 500)
        self.assertEqual(json.loads(body)["error"], "internal server error")
        self.assertNotIn(b"boom", body)

    def test_stop_exception_returns_500_without_detail(self) -> None:
        with patch.object(self.manager, "stop", side_effect=RuntimeError("boom")):
            status, body = self.request("POST", "/api/session/stop")
        self.assertEqual(status, 500)
        self.assertEqual(json.loads(body)["error"], "internal server error")
        self.assertNotIn(b"boom", body)

    def test_start_exception_returns_500_without_detail(self) -> None:
        with patch.object(self.manager, "start", side_effect=RuntimeError("boom")):
            status, body = self.request("POST", "/api/session/start")
        self.assertEqual(status, 500)
        self.assertEqual(json.loads(body)["error"], "internal server error")
        self.assertNotIn(b"boom", body)

    def test_stalled_body_returns_400_json(self) -> None:
        with patch.object(DashboardHandler, "timeout", 0.1):
            sock = socket.create_connection(("127.0.0.1", self.port), timeout=3)
            response = b""
            try:
                sock.sendall(
                    b"POST /api/session/mode HTTP/1.1\r\n"
                    b"Host: 127.0.0.1\r\n"
                    b"Content-Type: application/json\r\n"
                    b"Content-Length: 10\r\n\r\n"
                )
                while True:
                    chunk = sock.recv(4096)
                    if not chunk:
                        break
                    response += chunk
            finally:
                sock.close()
        self.assertIn(b" 400 ", response)
        self.assertIn(b"request body timeout", response)

    def test_peer_closed_detects_disconnect(self) -> None:
        server_sock, client_sock = socket.socketpair()
        try:
            handler = object.__new__(DashboardHandler)
            handler.connection = server_sock
            self.assertFalse(handler._peer_closed())
            client_sock.close()
            self.assertTrue(handler._peer_closed())
        finally:
            server_sock.close()
            client_sock.close()

    def test_frame_read_failure_returns_404(self) -> None:
        self.manager.latest.mkdir()
        status, _ = self.request("GET", "/api/frame.jpg")
        self.assertEqual(status, 404)

    def test_preview_stopped_returns_404(self) -> None:
        self.manager.state = "stopped"
        status, body = self.request("GET", "/preview.mjpg")
        self.assertEqual(status, 404)
        self.assertIn("error", json.loads(body))

    def _frame_part(self, data: bytes) -> bytes:
        return (
            b"--frame\r\n"
            b"Content-Type: image/jpeg\r\n"
            + f"Content-Length: {len(data)}\r\n\r\n".encode()
            + data
            + b"\r\n"
        )

    def test_preview_stream_ends_when_session_stops(self) -> None:
        self.manager.state = "running"
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=2)
        try:
            connection.request("GET", "/preview.mjpg")
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            self.manager.state = "stopped"
            started = time.monotonic()
            self.assertEqual(response.read(), b"")
            self.assertLess(time.monotonic() - started, 2)
        finally:
            connection.close()

    def test_preview_streams_frame_after_starting(self) -> None:
        self.manager.state = "starting"
        polled = threading.Event()
        original_preview_path = self.manager.preview_path

        def preview_path():
            polled.set()
            return original_preview_path()

        with patch.object(self.manager, "preview_path", side_effect=preview_path):
            connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
            try:
                connection.request("GET", "/preview.mjpg")
                response = connection.getresponse()
                self.assertEqual(response.status, 200)
                self.assertTrue(polled.wait(2), "stream did not poll for frames while starting")
                self.manager.state = "running"
                self.manager.latest.write_bytes(b"JPEGDATA")
                expected = self._frame_part(b"JPEGDATA")
                self.assertEqual(response.read(len(expected)), expected)
            finally:
                connection.close()

    def test_preview_stream_continues_while_stopping(self) -> None:
        self.manager.state = "running"
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        try:
            connection.request("GET", "/preview.mjpg")
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            self.manager.state = "stopping"
            self.manager.latest.write_bytes(b"JPEGDATA")
            expected = self._frame_part(b"JPEGDATA")
            self.assertEqual(response.read(len(expected)), expected)
        finally:
            connection.close()

    def test_unknown_route(self) -> None:
        status, _ = self.request("GET", "/nope")
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
