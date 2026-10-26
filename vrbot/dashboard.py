from __future__ import annotations

import collections
import json
import select
import signal
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

from .config import Config
from .orchestrator import ConflictError, PreflightError, SessionManager


class DashboardServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, manager, config: Config, static_dir: Path) -> None:
        self.manager = manager
        self.config = config
        self.static_dir = Path(static_dir)
        self.shutdown_event = threading.Event()
        super().__init__(address, DashboardHandler)

    def shutdown(self) -> None:
        self.shutdown_event.set()
        super().shutdown()


class DashboardHandler(BaseHTTPRequestHandler):
    server_version = "vrbot/0.1"
    timeout = 10

    def log_message(self, format, *args) -> None:
        return

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            return self._static("index.html", "text/html; charset=utf-8")
        if path == "/style.css":
            return self._static("style.css", "text/css; charset=utf-8")
        if path == "/app.js":
            return self._static("app.js", "application/javascript; charset=utf-8")
        if path == "/api/state":
            return self._json(self.server.manager.snapshot())
        if path == "/api/frame.jpg":
            latest = self.server.manager.preview_path()
            if latest is None:
                return self._error(404, "no preview frame")
            try:
                data = latest.read_bytes()
            except OSError:
                return self._error(404, "no preview frame")
            return self._bytes(200, data, "image/jpeg")
        if path == "/preview.mjpg":
            return self._mjpeg()
        if path.startswith("/api/logs/"):
            name = path[len("/api/logs/"):]
            decoded = unquote(name)
            if "/" in decoded or "\\" in decoded or decoded in (".", ".."):
                return self._error(404, "unknown log")
            return self._log(name)
        return self._error(404, "not found")

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path == "/api/session/start":
            try:
                self.server.manager.start()
            except ConflictError as exc:
                return self._error(409, str(exc))
            except PreflightError as exc:
                return self._error(400, str(exc))
            except Exception as exc:
                return self._error(500, str(exc))
            return self._json({"accepted": True}, status=202)
        if path == "/api/session/stop":
            try:
                self.server.manager.stop()
            except Exception as exc:
                return self._error(500, str(exc))
            return self._json({"accepted": True}, status=202)
        try:
            body = self._read_body()
        except ValueError as exc:
            return self._error(400, str(exc))
        if path == "/api/session/mode":
            try:
                self.server.manager.set_mode(body.get("mode"))
            except ValueError as exc:
                return self._error(400, str(exc))
            except ConflictError as exc:
                return self._error(409, str(exc))
            except Exception as exc:
                return self._error(500, str(exc))
            return self._json({"mode": body.get("mode")})
        if path == "/api/session/kill":
            try:
                self.server.manager.kill(body.get("target"))
            except ValueError as exc:
                return self._error(400, str(exc))
            except ConflictError as exc:
                return self._error(409, str(exc))
            except Exception as exc:
                return self._error(500, str(exc))
            return self._json({"killed": body.get("target")})
        return self._error(404, "not found")

    def _read_body(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length", "0") or 0)
        except ValueError as exc:
            raise ValueError("invalid Content-Length") from exc
        if length < 0:
            raise ValueError("invalid Content-Length")
        if length > 1_000_000:
            raise ValueError("request body too large")
        raw = self.rfile.read(length) if length else b""
        if not raw:
            return {}
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError("invalid JSON") from exc
        if not isinstance(data, dict):
            raise ValueError("JSON object required")
        return data

    def _json(self, payload, status: int = 200) -> None:
        self._bytes(status, json.dumps(payload, ensure_ascii=False).encode(), "application/json; charset=utf-8")

    def _error(self, status: int, message: str) -> None:
        self._json({"error": message}, status=status)

    def _bytes(self, status: int, data: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _static(self, name: str, content_type: str) -> None:
        path = self.server.static_dir / name
        try:
            data = path.read_bytes()
        except OSError:
            return self._error(404, "not found")
        self._bytes(200, data, content_type)

    def _log(self, name: str) -> None:
        path = self.server.manager.log_path(name)
        if path is None:
            return self._error(404, "unknown log")
        try:
            with path.open("r", encoding="utf-8", errors="replace") as handle:
                lines = collections.deque(handle, maxlen=200)
        except OSError:
            return self._error(404, "log not found")
        data = "\n".join("".join(lines).splitlines()).encode()
        self._bytes(200, data, "text/plain; charset=utf-8")

    def _mjpeg(self) -> None:
        if self.server.manager.state_value() in ("stopped", "failed"):
            return self._error(404, "session is not running")
        self.send_response(200)
        self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.connection.settimeout(5)
        boundary = b"--frame\r\n"
        interval = 1.0 / max(1.0, self.server.config.preview.fps)
        while self.server.manager.state_value() == "running":
            if self.server.shutdown_event.is_set() or self._peer_closed():
                break
            latest = self.server.manager.preview_path()
            data = None
            if latest is not None:
                try:
                    data = latest.read_bytes()
                except OSError:
                    data = None
            if data:
                try:
                    self.wfile.write(boundary)
                    self.wfile.write(b"Content-Type: image/jpeg\r\n")
                    self.wfile.write(f"Content-Length: {len(data)}\r\n\r\n".encode())
                    self.wfile.write(data)
                    self.wfile.write(b"\r\n")
                    self.wfile.flush()
                except OSError:
                    break
            time.sleep(interval)

    def _peer_closed(self) -> bool:
        try:
            readable, _, _ = select.select([self.connection], [], [], 0)
            if not readable:
                return False
            return self.connection.recv(1, socket.MSG_PEEK) == b""
        except (OSError, ValueError):
            return True


def run_dashboard(cfg: Config) -> None:
    manager = SessionManager(cfg)
    static_dir = Path(__file__).resolve().parent / "static"
    server = DashboardServer((cfg.dashboard.host, cfg.dashboard.port), manager, cfg, static_dir)

    def request_shutdown(signum, frame) -> None:
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, request_shutdown)
    signal.signal(signal.SIGTERM, request_shutdown)
    host, port = server.server_address[:2]
    print(f"vrbot dashboard: http://{host}:{port}")
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        manager.stop()
        if not manager.wait_idle(70):
            print("vrbot dashboard: session did not stop within 70s", file=sys.stderr)
        server.server_close()
