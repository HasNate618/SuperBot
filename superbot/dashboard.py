from __future__ import annotations

import contextlib
import json
import os
import select
import signal
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

from .body_preview import BODY_VERBS, BODY_VIEWS, BodyPreview, send_control
from .config import Config
from .manual_input import ManualInputError, ManualInputUnavailable
from .orchestrator import ConflictError, PreflightError, SessionManager
from .processes import tail_stream


class PayloadTooLarge(ValueError):
    pass


class DashboardServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, manager, config: Config, static_dir: Path,
                 body: BodyPreview | None = None) -> None:
        self.manager = manager
        self.config = config
        self.static_dir = Path(static_dir)
        self.body = body
        self.shutdown_event = threading.Event()
        super().__init__(address, DashboardHandler)

    def shutdown(self) -> None:
        self.shutdown_event.set()
        super().shutdown()


class DashboardHandler(BaseHTTPRequestHandler):
    # BaseHTTPRequestHandler types .server as BaseServer, which has neither of
    # ours. This annotation narrows it for the checker and costs nothing at runtime.
    server: DashboardServer

    server_version = "superbot/0.1"
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
            state = self.server.manager.snapshot()
            body = self.server.body
            if body is not None:
                state = {**state, "body": body.status()}
            return self._json(state)
        if path == "/api/frame.png":
            latest = self.server.manager.preview_path()
            if latest is None:
                return self._error(404, "no preview frame")
            try:
                data = latest.read_bytes()
            except OSError:
                return self._error(404, "no preview frame")
            return self._bytes(200, data, "image/png")
        if path == "/api/body.jpg":
            from urllib.parse import parse_qs
            body = self.server.body
            if body is None:
                return self._error(404, "no body frame")
            view = parse_qs(urlparse(self.path).query).get("view", [None])[0]
            if view is not None and view not in BODY_VIEWS:
                return self._error(400, f"unknown view: {view}")
            frame = body.frame(view)
            if frame is None:
                return self._error(404, "no body frame")
            try:
                return self._bytes(200, frame.read_bytes(), "image/jpeg")
            except OSError:
                return self._error(404, "no body frame")
        if path == "/preview.png":
            return self._preview_stream()
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
            except Exception:
                return self._error(500, "internal server error")
            return self._json({"accepted": True}, status=202)
        if path == "/api/session/stop":
            try:
                self.server.manager.stop()
            except Exception:
                return self._error(500, "internal server error")
            return self._json({"accepted": True}, status=202)
        try:
            body = self._read_body()
        except PayloadTooLarge as exc:
            return self._error(413, str(exc))
        except ValueError as exc:
            return self._error(400, str(exc))
        if path == "/api/manual/input":
            if not self._origin_allowed():
                return self._error(403, "cross-origin input rejected")
            try:
                armed = self.server.manager.manual_input(body)
            except ManualInputError as exc:
                return self._error(400, str(exc))
            except ConflictError as exc:
                return self._error(409, str(exc))
            except ManualInputUnavailable as exc:
                return self._error(503, str(exc))
            except Exception:
                return self._error(500, "internal server error")
            return self._json({"accepted": True, "armed": bool(armed)})
        if path == "/api/body/start":
            body = self.server.body
            if body is None:
                return self._error(400, "body view disabled")
            problem = body.start()
            if problem:
                return self._error(400, problem)
            return self._json({"ok": True, "body": body.status()}, status=202)
        if path == "/api/body/stop":
            body = self.server.body
            if body is None:
                return self._error(400, "body view disabled")
            body.stop()
            return self._json({"ok": True, "body": body.status()})
        if path == "/api/body/command":
            view = self.server.body
            if view is None:
                return self._error(400, "body view disabled")
            if not view.running():
                return self._error(503, "body view is not running")
            if "view" in body and "verb" not in body:
                name = body.get("view")
                if not isinstance(name, str) or name not in BODY_VIEWS:
                    return self._error(400, f"unknown view: {name}")
                try:
                    reply = send_control(view.control_path(), {"view": name})
                except (OSError, ValueError) as exc:
                    return self._error(503, f"body control unavailable: {exc}")
                return self._json(reply)
            verb = body.get("verb")
            if not isinstance(verb, str) or verb not in BODY_VERBS:
                return self._error(400, f"unknown verb: {verb}")
            try:
                reply = send_control(view.control_path(), {"verb": verb})
            except (OSError, ValueError) as exc:
                return self._error(503, f"body control unavailable: {exc}")
            return self._json(reply)
        if path == "/api/session/mode":
            try:
                self.server.manager.set_mode(body.get("mode"))
            except ValueError as exc:
                return self._error(400, str(exc))
            except ConflictError as exc:
                return self._error(409, str(exc))
            except Exception:
                return self._error(500, "internal server error")
            return self._json({"mode": body.get("mode")})
        if path == "/api/session/kill":
            try:
                self.server.manager.kill(body.get("target"))
            except ValueError as exc:
                return self._error(400, str(exc))
            except ConflictError as exc:
                return self._error(409, str(exc))
            except Exception:
                return self._error(500, "internal server error")
            return self._json({"killed": body.get("target")})
        return self._error(404, "not found")

    def _origin_allowed(self) -> bool:
        origin = self.headers.get("Origin")
        if origin is None:
            return True
        parsed = urlparse(origin)
        return parsed.scheme in ("http", "https") and parsed.netloc == self.headers.get("Host", "")

    def _read_body(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length", "0") or 0)
        except ValueError as exc:
            raise ValueError("invalid Content-Length") from exc
        if length < 0:
            raise ValueError("invalid Content-Length")
        if length > 1_000_000:
            raise PayloadTooLarge("request body too large")
        try:
            raw = self.rfile.read(length) if length else b""
        except (TimeoutError, OSError) as exc:
            raise ValueError("request body timeout") from exc
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
            with path.open("rb") as handle:
                data = tail_stream(handle).encode()
        except OSError:
            return self._error(404, "log not found")
        self._bytes(200, data, "text/plain; charset=utf-8")

    def _preview_stream(self) -> None:
        if self.server.manager.state_value() in ("stopped", "failed"):
            return self._error(404, "session is not running")
        self.send_response(200)
        self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.connection.settimeout(5)
        boundary = b"--frame\r\n"
        interval = 1.0 / max(1.0, self.server.config.preview.fps)
        while True:
            if self.server.manager.state_value() in ("stopped", "failed"):
                break
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
                    self.wfile.write(b"Content-Type: image/png\r\n")
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


def dashboard_pidfile(cfg: Config) -> Path:
    """Where a running dashboard records its pid. --replace reads this."""
    return Path(cfg.paths.session_root) / "dashboard.pid"


def run_dashboard(cfg: Config) -> None:
    manager = SessionManager(cfg)
    static_dir = Path(__file__).resolve().parent / "static"
    body = None
    if cfg.body.enabled:
        body = BodyPreview(cfg.body, out_dir=Path(cfg.paths.session_root) / "body")
        if cfg.body.autostart:
            problem = body.start()
            if problem:
                print(f"superbot dashboard: body view: {problem}", file=sys.stderr)
    server = DashboardServer(
        (cfg.dashboard.host, cfg.dashboard.port), manager, cfg, static_dir, body=body
    )
    pidfile = dashboard_pidfile(cfg)
    pidfile.parent.mkdir(parents=True, exist_ok=True)
    pidfile.write_text(str(os.getpid()), encoding="utf-8")

    def request_shutdown(signum, frame) -> None:
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, request_shutdown)
    signal.signal(signal.SIGTERM, request_shutdown)
    host, port = server.server_address[:2]
    print(f"superbot dashboard: http://{host}:{port}", flush=True)
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        print("superbot dashboard: shutting down", flush=True)
        if body is not None:
            print(f"superbot dashboard: stopping body view (running={body.running()})", flush=True)
            body.stop()
            print(f"superbot dashboard: body view stopped (running={body.running()})", flush=True)
        manager.stop()
        if not manager.wait_idle(70):
            print("superbot dashboard: session did not stop within 70s", file=sys.stderr)
        server.server_close()
        try:
            owned = pidfile.read_text(encoding="utf-8").strip() == str(os.getpid())
        except OSError:
            owned = False
        if owned:
            with contextlib.suppress(OSError):
                pidfile.unlink()
