from __future__ import annotations

import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from vrbot.config import load_config
from vrbot.dashboard import DashboardServer
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
            return exc.code, exc.read()

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

    def test_malformed_json(self) -> None:
        status, _ = self.request("POST", "/api/session/mode", raw=b"{")
        self.assertEqual(status, 400)

    def test_unknown_route(self) -> None:
        status, _ = self.request("GET", "/nope")
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
