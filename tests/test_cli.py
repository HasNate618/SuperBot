from __future__ import annotations

import http.server
import json
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from pathlib import Path


class CliTests(unittest.TestCase):
    def run_cli(self, *args: str, timeout: float = 10) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "clank", *args],
            cwd=Path(__file__).resolve().parent.parent,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
        )

    def test_help(self) -> None:
        result = self.run_cli("--help")
        self.assertEqual(result.returncode, 0)
        self.assertIn("--preview-smoke", result.stdout)

    def test_invalid_overrides_exit_2(self) -> None:
        cases = (("--port", "-1"), ("--port", "70000"), ("--host", ""))
        for args in cases:
            with self.subTest(args=args):
                result = self.run_cli(*args)
                self.assertEqual(result.returncode, 2)
                self.assertNotIn("Traceback", result.stderr)
                self.assertIn("clank:", result.stderr)

    def test_invalid_overrides_fail_before_preview_smoke(self) -> None:
        cases = (("--port", "-1"), ("--port", "70000"), ("--host", ""))
        for args in cases:
            with self.subTest(args=args):
                result = self.run_cli(*args, "--preview-smoke")
                self.assertEqual(result.returncode, 2)
                self.assertNotIn("Traceback", result.stderr)
                self.assertIn("clank:", result.stderr)

    def test_launch_bind_failure_exits_2(self) -> None:
        result = self.run_cli("--host", "192.0.2.1", "--port", "0")
        self.assertEqual(result.returncode, 2)
        self.assertNotIn("Traceback", result.stderr)
        self.assertIn("clank:", result.stderr)

    def test_invalid_utf8_config_exits_2(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "config.toml"
            path.write_bytes(b"[preview]\nfps = 6 # \xff\n")
            result = self.run_cli("--config", str(path))
            self.assertEqual(result.returncode, 2)
            self.assertNotIn("Traceback", result.stderr)
            self.assertIn("clank:", result.stderr)


    # ---- port already in use: reuse ours, never touch a stranger ----

    @staticmethod
    def free_port() -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            return sock.getsockname()[1]

    def wait_state(self, port: int, timeout: float = 20.0) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/api/state", timeout=2
                ) as response:
                    return json.loads(response.read().decode("utf-8"))
            except Exception:
                time.sleep(0.3)
        raise AssertionError(f"nothing serving /api/state on {port}")

    def temp_config(self, raw: str) -> Path:
        path = Path(raw) / "config.toml"
        path.write_text(
            '[paths]\nsession_root = "' + str(Path(raw) / "sessions") + '"\n',
            encoding="utf-8",
        )
        return path

    def test_reuse_existing_dashboard_exits_0(self) -> None:
        doc = {"state": "stopped", "session_id": None, "mode": "hold"}

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 - http.server names it
                payload = json.dumps(doc).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args: object) -> None:
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as raw:
                cfg = self.temp_config(raw)
                result = self.run_cli("--config", str(cfg), "--port", str(port))
        finally:
            server.shutdown()
            server.server_close()
        self.assertEqual(result.returncode, 0)
        self.assertIn("already running", result.stdout)
        self.assertIn(str(port), result.stdout)
        self.assertNotIn("Traceback", result.stderr)

    def test_foreign_port_gives_clean_error(self) -> None:
        holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        holder.bind(("127.0.0.1", 0))
        holder.listen(1)
        try:
            result = self.run_cli("--port", str(holder.getsockname()[1]))
        finally:
            holder.close()
        self.assertEqual(result.returncode, 2)
        self.assertNotIn("Traceback", result.stderr)
        self.assertIn("clank:", result.stderr)

    def test_replace_ignores_stale_pidfile(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            cfg = self.temp_config(raw)
            sessions = Path(raw) / "sessions"
            sessions.mkdir(exist_ok=True)
            (sessions / "dashboard.pid").write_text("999999999", encoding="utf-8")
            port = self.free_port()
            try:
                self.run_cli("--config", str(cfg), "--port", str(port), "--replace", timeout=15)
            except subprocess.TimeoutExpired as exc:
                out = exc.stdout.decode() if isinstance(exc.stdout, bytes) else (exc.stdout or "")
                self.assertIn("clank dashboard: http://", out)
            else:
                self.fail("the replacement dashboard should still be serving")

    def test_replace_restarts_live_dashboard(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            cfg = self.temp_config(raw)
            port = self.free_port()
            old = subprocess.Popen(
                [sys.executable, "-m", "clank", "--config", str(cfg), "--port", str(port)],
                cwd=Path(__file__).resolve().parent.parent,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                self.wait_state(port)
                new = subprocess.Popen(
                    [sys.executable, "-m", "clank", "--config", str(cfg),
                     "--port", str(port), "--replace"],
                    cwd=Path(__file__).resolve().parent.parent,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                try:
                    deadline = time.monotonic() + 25.0
                    while old.poll() is None and time.monotonic() < deadline:
                        time.sleep(0.3)
                    self.assertIsNotNone(old.poll(), "the old dashboard was not stopped")
                    state = self.wait_state(port)
                    self.assertIn("state", state)
                finally:
                    new.kill()
                    new.wait(timeout=15)
            finally:
                if old.poll() is None:
                    old.kill()
                    old.wait(timeout=15)

    def test_read_own_pid_rejects_strangers(self) -> None:
        from clank.__main__ import read_own_pid

        with tempfile.TemporaryDirectory() as raw:
            pidfile = Path(raw) / "dashboard.pid"
            self.assertIsNone(read_own_pid(pidfile))  # missing
            pidfile.write_text("not-a-pid", encoding="utf-8")
            self.assertIsNone(read_own_pid(pidfile))  # garbage
            pidfile.write_text("999999999", encoding="utf-8")
            self.assertIsNone(read_own_pid(pidfile))  # dead
            import os

            pidfile.write_text(str(os.getpid()), encoding="utf-8")
            self.assertIsNone(read_own_pid(pidfile))  # self, or foreign


if __name__ == "__main__":
    unittest.main()
