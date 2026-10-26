from __future__ import annotations

import collections
import json
import os
import socket
import subprocess
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from vrbot.config import load_config
from vrbot.orchestrator import (
    ConflictError,
    PreflightError,
    SessionManager,
    _OpenXRLogParser,
    parse_openxr_client,
)


class FakeHandle:
    def __init__(self, name: str, pid: int) -> None:
        self.name = name
        self.pid = pid
        self.pgid = pid
        self.log_path = Path("/tmp") / f"{name}.log"
        self._alive = True
        self._group_alive = False

    def alive(self) -> bool:
        return self._alive

    def group_alive(self) -> bool:
        return self._group_alive

    def stop(self, timeout: float = 5.0) -> None:
        self._alive = False

    def signal_group(self, sig: int) -> None:
        self._alive = False


class FakePreview:
    def __init__(self) -> None:
        self.started = False
        self.stopped = False

    def start(self) -> None:
        self.started = True

    def stop(self, timeout: float = 5.0) -> None:
        self.stopped = True

    def status(self) -> dict:
        return {
            "width": 1280,
            "height": 688,
            "fps_target": 8,
            "fps_actual": 7.4,
            "last_frame_at": "2026-10-25T22:39:00Z",
            "errors": 0,
            "frames": 10,
        }


class FakeRunner:
    def __init__(self) -> None:
        self.handles: dict[str, FakeHandle] = {}
        self.next_pid = 100
        self.host_steam = False
        self.units: set[str] = set()
        self.port = False
        self.pointer = None
        self.game_started = False
        self.scene_graph: str | None = "window: title: Steam"
        self.fail_at: str | None = None
        self.block_wait: threading.Event | None = None
        self.runs: list[list[str]] = []
        self.alive_pids: set[int] = set()
        self.removed_socket = False

    def start(self, name, argv, env, cwd, log_path, stderr_path=None):
        if self.fail_at == f"start:{name}":
            raise RuntimeError(f"{name} failed to start")
        log_path = Path(log_path)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.touch()
        if stderr_path is not None:
            stderr_path = Path(stderr_path)
            stderr_path.parent.mkdir(parents=True, exist_ok=True)
            stderr_path.touch()
        if name == "weston":
            for arg in argv:
                text = str(arg)
                if text.startswith("--log="):
                    Path(text[len("--log="):]).write_text("xserver listening on display :2\n")
        handle = FakeHandle(name, self.next_pid)
        self.next_pid += 1
        self.handles[name] = handle
        return handle

    def wait_port(self, host, port, timeout, abort=None):
        if self.fail_at == "wait_port":
            raise RuntimeError("monado port timeout")
        if self.block_wait is not None:
            self.block_wait.wait(5)

    def wait_log(self, path, needle, timeout, abort=None):
        if self.fail_at == f"wait_log:{needle}":
            raise RuntimeError(f"log timeout for {needle}")

    def wait_pids(self, argv0, timeout, abort=None):
        if self.fail_at == "wait_pids":
            raise RuntimeError("game process timeout")
        self.game_started = True
        return [555]

    def process_name_running(self, name):
        return self.host_steam

    def systemd_unit_active(self, unit):
        return unit in self.units

    def port_in_use(self, host, port):
        return self.port

    def remove_stale_socket(self, path):
        self.removed_socket = True
        return True

    def pids_by_argv0(self, argv0):
        return [555] if self.game_started else []

    def pid_alive(self, pid):
        if pid in self.alive_pids:
            return True
        return any(handle.pid == pid and handle.alive() for handle in self.handles.values())

    def pid_cmdline(self, pid):
        return ["/nix/store/example/bin/monado-service"]

    def kill_pid(self, pid):
        if pid == 555:
            self.game_started = False

    def run(self, argv, env=None, cwd=None, timeout=10, check=False):
        self.runs.append([str(item) for item in argv])
        return subprocess.CompletedProcess(argv, 0, "", "")

    def debug_scene_graph(self, debug_bin, env, timeout=5):
        return self.scene_graph

    def read_pointer(self):
        return self.pointer

    def write_pointer(self, data):
        self.pointer = json.loads(json.dumps(data))

    def remove_pointer(self):
        self.pointer = None

    def tail(self, path, lines=200):
        try:
            with Path(path).open("r", errors="replace") as handle:
                data = collections.deque(handle, maxlen=lines)
        except OSError:
            return ""
        text = "".join(data)
        if text.endswith("\n"):
            text = text[:-1]
        return text


class OrchestratorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        cfg = load_config(None, env={})
        cfg = replace(
            cfg,
            paths=replace(cfg.paths, session_root=str(self.base / "sessions")),
            launch=replace(
                cfg.launch,
                steam_settle_sec=0.01,
                steam_ready_timeout_sec=0.05,
                game_process_timeout_sec=1,
                openxr_client_timeout_sec=1,
            ),
            timeouts=replace(
                cfg.timeouts,
                monado_startup_sec=1,
                weston_startup_sec=1,
                pose_handshake_sec=1,
                stop_sec=1,
            ),
        )
        self.cfg = cfg
        self.runner = FakeRunner()
        self.preview = FakePreview()
        self.manager = SessionManager(
            cfg,
            runner=self.runner,
            preview_factory=lambda **kwargs: self.preview,
        )

    def tearDown(self) -> None:
        self.manager.stop()
        self.manager.wait_idle(2)
        self.temp.cleanup()

    def start(self) -> None:
        with patch("vrbot.orchestrator.missing_requirements", return_value=[]):
            self.manager.start()
        self.assertTrue(self.manager.wait_idle(2))
        self.assertEqual(self.manager.state_value(), "running")

    def test_start_reaches_running(self) -> None:
        self.start()
        self.assertEqual(set(self.runner.handles), {"monado", "weston", "steam", "pose"})
        self.assertTrue(self.preview.started)
        self.assertIsNotNone(self.runner.pointer)
        snapshot = self.manager.snapshot()
        self.assertIsNotNone(snapshot["session_id"])
        self.assertIsNotNone(snapshot["started_at"])

    def test_weston_stdout_log_is_written_but_not_public(self) -> None:
        self.start()
        snapshot = self.manager.snapshot()
        self.assertTrue((Path(snapshot["session_dir"]) / "weston-stdout.log").exists())
        self.assertIsNone(self.manager.log_path("weston-stdout"))

    def test_snapshot_reports_health(self) -> None:
        self.start()
        log_path = self.manager.log_path("monado")
        assert log_path is not None
        log_path.write_text(
            " INFO [client_connected] Client 1 connected\n"
            "\tapplication_name: 'Unity Application'\n"
        )
        snapshot = self.manager.snapshot()
        self.assertEqual(snapshot["state"], "running")
        self.assertEqual(snapshot["openxr_client"], "Unity Application")
        self.assertTrue(snapshot["processes"]["game"]["alive"])
        self.assertEqual(snapshot["preview"]["width"], 1280)
        self.assertTrue(snapshot["player_log"].endswith("Player.log"))

    def test_snapshot_keeps_openxr_client_across_log_growth(self) -> None:
        self.start()
        log_path = self.manager.log_path("monado")
        assert log_path is not None
        log_path.write_text(
            " INFO [client_connected] Client 1 connected\n"
            "\tapplication_name: 'Unity Application'\n"
        )
        self.assertEqual(self.manager.snapshot()["openxr_client"], "Unity Application")
        with log_path.open("a") as handle:
            for index in range(300):
                handle.write(f"chatter {index}\n")
        self.assertEqual(self.manager.snapshot()["openxr_client"], "Unity Application")

    def test_snapshot_reads_openxr_log_in_bounded_chunks(self) -> None:
        self.start()
        log_path = self.manager.log_path("monado")
        assert log_path is not None
        log_path.write_text(
            "chatter\n" * 20000
            + " INFO [client_connected] Client 1 connected\n"
            + "\tapplication_name: 'Unity Application'\n"
        )
        reads: list[int] = []
        real_open = Path.open

        class CountingHandle:
            def __init__(self, handle) -> None:
                self.handle = handle

            def __enter__(self):
                return self

            def __exit__(self, *args) -> None:
                self.handle.close()

            def seek(self, *args):
                return self.handle.seek(*args)

            def tell(self):
                return self.handle.tell()

            def read(self, amount: int = -1):
                reads.append(amount)
                return self.handle.read(amount)

        def counting_open(path, *args, **kwargs):
            return CountingHandle(real_open(path, *args, **kwargs))

        with patch.object(Path, "open", counting_open):
            client = self.manager.snapshot()["openxr_client"]
        self.assertEqual(client, "Unity Application")
        self.assertTrue(reads)
        self.assertTrue(all(0 <= amount <= 65536 for amount in reads))

    def test_snapshot_clears_openxr_client_on_disconnect(self) -> None:
        self.start()
        log_path = self.manager.log_path("monado")
        assert log_path is not None
        log_path.write_text(
            " INFO [client_connected] Client 1 connected\n"
            "\tapplication_name: 'Unity Application'\n"
        )
        self.assertEqual(self.manager.snapshot()["openxr_client"], "Unity Application")
        with log_path.open("a") as handle:
            handle.write(" INFO [client_disconnected] Client 1 disconnected\n")
        self.assertIsNone(self.manager.snapshot()["openxr_client"])

    def test_snapshot_parses_openxr_client_split_across_polls(self) -> None:
        self.start()
        log_path = self.manager.log_path("monado")
        assert log_path is not None
        log_path.write_text(" INFO [client_connected] Client 1 conn")
        self.assertIsNone(self.manager.snapshot()["openxr_client"])
        with log_path.open("a") as handle:
            handle.write("ected\n\tapplication_name: 'Unity Application'")
        self.assertEqual(self.manager.snapshot()["openxr_client"], "Unity Application")
        with log_path.open("a") as handle:
            handle.write("\n")
        self.assertEqual(self.manager.snapshot()["openxr_client"], "Unity Application")

    def test_snapshot_does_not_advance_offset_when_parser_feed_fails(self) -> None:
        self.start()
        log_path = self.manager.log_path("monado")
        assert log_path is not None
        log_path.write_text(
            " INFO [client_connected] Client 1 connected\n"
            "\tapplication_name: 'APP A'\n"
        )
        self.assertEqual(self.manager.snapshot()["openxr_client"], "APP A")
        parser = self.manager._openxr_parser
        real_feed = parser.feed
        calls = 0

        def failing_feed(text: str) -> str | None:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("parser failure")
            return real_feed(text)

        with log_path.open("a") as handle:
            handle.write(
                " INFO [client_connected] Client 2 connected\n"
                "\tapplication_name: 'APP B'\n"
            )
        with patch.object(parser, "feed", side_effect=failing_feed):
            with self.assertRaises(RuntimeError):
                self.manager.snapshot()
        self.assertEqual(self.manager.snapshot()["openxr_client"], "APP B")

    def test_snapshot_resets_openxr_state_after_truncation(self) -> None:
        self.start()
        log_path = self.manager.log_path("monado")
        assert log_path is not None
        log_path.write_text(
            " INFO [client_connected] Client 1 connected\n"
            "\tapplication_name: 'Unity Application'\n"
        )
        self.assertEqual(self.manager.snapshot()["openxr_client"], "Unity Application")
        log_path.write_text("")
        self.assertIsNone(self.manager.snapshot()["openxr_client"])
        log_path.write_text(
            " INFO [client_connected] Client 2 connected\n"
            "\tapplication_name: 'Other App'\n"
        )
        self.assertEqual(self.manager.snapshot()["openxr_client"], "Other App")

    def test_snapshot_detects_truncate_and_regrow(self) -> None:
        self.start()
        log_path = self.manager.log_path("monado")
        assert log_path is not None
        log_path.write_text(
            " INFO [client_connected] Client 1 connected\n"
            "\tapplication_name: 'APP A'\n"
        )
        self.assertEqual(self.manager.snapshot()["openxr_client"], "APP A")
        log_path.write_text(
            " INFO [client_connected] Client 2 connected\n"
            "\tapplication_name: 'APP B'\n"
            + "chatter\n" * 40
        )
        self.assertEqual(self.manager.snapshot()["openxr_client"], "APP B")

    def test_snapshot_detects_same_size_rewrite_with_identical_tail(self) -> None:
        self.start()
        log_path = self.manager.log_path("monado")
        assert log_path is not None
        filler = ("z" * 63 + "\n") * 4
        log_path.write_text(
            "[client_connected] Client 1 connected\n"
            "\tapplication_name: 'APP A'\n"
            + filler
        )
        self.assertEqual(self.manager.snapshot()["openxr_client"], "APP A")
        log_path.write_text(
            "[client_connected] Client 1 connected\n"
            "\tapplication_name: 'APP B'\n"
            + filler
        )
        self.assertEqual(self.manager.snapshot()["openxr_client"], "APP B")

    def test_snapshot_hides_previous_openxr_client_while_start_prepares(self) -> None:
        self.start()
        log_path = self.manager.log_path("monado")
        assert log_path is not None
        log_path.write_text(
            " INFO [client_connected] Client 1 connected\n"
            "\tapplication_name: 'OLD APP'\n"
        )
        self.assertEqual(self.manager.snapshot()["openxr_client"], "OLD APP")
        self.manager.stop()
        self.assertTrue(self.manager.wait_idle(2))

        entered = threading.Event()
        release = threading.Event()
        original_preflight = self.manager._preflight

        def blocking_preflight(gen) -> None:
            entered.set()
            self.assertTrue(release.wait(2))
            original_preflight(gen)

        with (
            patch.object(self.manager, "_preflight", side_effect=blocking_preflight),
            patch("vrbot.orchestrator.missing_requirements", return_value=[]),
        ):
            thread = threading.Thread(target=self.manager.start, daemon=True)
            thread.start()
            try:
                self.assertTrue(entered.wait(2))
                self.assertEqual(self.manager.state_value(), "starting")
                snapshot = self.manager.snapshot()
                self.assertIsNone(snapshot["openxr_client"])
                self.assertIsNone(snapshot["session_id"])
                self.assertIsNone(snapshot["started_at"])
            finally:
                release.set()
                thread.join(2)
        self.assertTrue(self.manager.wait_idle(2))

    def test_failed_preflight_clears_previous_session_paths(self) -> None:
        self.start()
        log_path = self.manager.log_path("monado")
        assert log_path is not None
        log_path.write_text(
            " INFO [client_connected] Client 1 connected\n"
            "\tapplication_name: 'OLD APP'\n"
        )
        self.assertEqual(self.manager.snapshot()["openxr_client"], "OLD APP")
        self.assertIsNotNone(self.manager.snapshot()["session_dir"])
        self.assertIsNotNone(self.manager.snapshot()["session_id"])
        self.assertIsNotNone(self.manager.snapshot()["started_at"])
        self.manager.stop()
        self.assertTrue(self.manager.wait_idle(2))

        self.runner.host_steam = True
        with patch("vrbot.orchestrator.missing_requirements", return_value=[]):
            with self.assertRaises(PreflightError):
                self.manager.start()
        snapshot = self.manager.snapshot()
        self.assertEqual(snapshot["state"], "failed")
        self.assertIsNone(snapshot["openxr_client"])
        self.assertIsNone(snapshot["session_dir"])
        self.assertIsNone(snapshot["session_id"])
        self.assertIsNone(snapshot["started_at"])
        self.assertIsNone(self.manager.preview_path())

    def test_snapshot_clears_openxr_client_after_stop(self) -> None:
        self.start()
        log_path = self.manager.log_path("monado")
        assert log_path is not None
        log_path.write_text(
            " INFO [client_connected] Client 1 connected\n"
            "\tapplication_name: 'Unity Application'\n"
        )
        self.assertEqual(self.manager.snapshot()["openxr_client"], "Unity Application")
        self.manager.stop()
        self.assertTrue(self.manager.wait_idle(2))
        self.assertIsNone(self.manager.snapshot()["openxr_client"])

    def test_snapshot_resets_openxr_state_after_rotation(self) -> None:
        self.start()
        log_path = self.manager.log_path("monado")
        assert log_path is not None
        log_path.write_text(
            " INFO [client_connected] Client 1 connected\n"
            "\tapplication_name: 'Unity Application'\n"
        )
        self.assertEqual(self.manager.snapshot()["openxr_client"], "Unity Application")
        rotated = log_path.with_name("monado.log.rotated")
        rotated.write_text("chatter " * 40)
        os.replace(rotated, log_path)
        self.assertIsNone(self.manager.snapshot()["openxr_client"])

    def test_host_steam_blocks_start(self) -> None:
        self.runner.host_steam = True
        with patch("vrbot.orchestrator.missing_requirements", return_value=[]):
            with self.assertRaises(PreflightError):
                self.manager.start()
        self.assertEqual(self.manager.state_value(), "failed")
        self.assertIsNone(self.runner.pointer)
        self.assertEqual(self.runner.handles, {})

    def test_probe_failure_blocks_start(self) -> None:
        with (
            patch.object(
                self.runner,
                "process_name_running",
                side_effect=OSError("pgrep missing"),
            ),
            patch("vrbot.orchestrator.missing_requirements", return_value=[]),
        ):
            with self.assertRaises(PreflightError) as raised:
                self.manager.start()
        self.assertIn("pgrep missing", str(raised.exception))
        self.assertEqual(self.manager.state_value(), "failed")
        self.assertIsNone(self.runner.pointer)
        self.assertEqual(self.runner.handles, {})

    def test_systemd_probe_failure_blocks_start(self) -> None:
        with (
            patch.object(
                self.runner,
                "systemd_unit_active",
                side_effect=OSError("systemctl missing"),
            ),
            patch("vrbot.orchestrator.missing_requirements", return_value=[]),
        ):
            with self.assertRaises(PreflightError) as raised:
                self.manager.start()
        self.assertIn("systemctl missing", str(raised.exception))
        self.assertEqual(self.manager.state_value(), "failed")
        self.assertIsNone(self.runner.pointer)
        self.assertEqual(self.runner.handles, {})

    def test_stale_pointer_blocks_start(self) -> None:
        self.runner.alive_pids.add(999)
        self.runner.pointer = {
            "session": "/old",
            "processes": {"monado": {"pid": 999, "command": "monado-service"}},
        }
        with patch("vrbot.orchestrator.missing_requirements", return_value=[]):
            with self.assertRaises(PreflightError) as raised:
                self.manager.start()
        self.assertIn("stale", str(raised.exception))
        self.assertEqual(self.manager.state_value(), "failed")

    def test_startup_failure_cleans_up(self) -> None:
        self.runner.fail_at = "wait_port"
        with patch("vrbot.orchestrator.missing_requirements", return_value=[]):
            self.manager.start()
        self.assertTrue(self.manager.wait_idle(2))
        self.assertEqual(self.manager.state_value(), "failed")
        self.assertIn("monado", self.manager.snapshot()["last_error"])
        self.assertIsNone(self.runner.pointer)
        self.assertTrue(all(not handle.alive() for handle in self.runner.handles.values()))

    def test_startup_failure_includes_step_and_log_tail(self) -> None:
        original_wait_log = self.runner.wait_log

        def failing_wait_log(path, needle, timeout, abort=None):
            if needle == "handshake ok":
                Path(path).write_text("pose handshake detail\n")
                raise RuntimeError("pose handshake timeout")
            return original_wait_log(path, needle, timeout, abort)

        with (
            patch.object(self.runner, "wait_log", side_effect=failing_wait_log),
            patch("vrbot.orchestrator.missing_requirements", return_value=[]),
        ):
            self.manager.start()
            self.assertTrue(self.manager.wait_idle(2))
        snapshot = self.manager.snapshot()
        self.assertEqual(snapshot["state"], "failed")
        self.assertIn("pose failed", snapshot["last_error"])
        self.assertIn("pose handshake detail", snapshot["last_error"])

    def test_startup_failure_uses_weston_stdout_fallback_tail(self) -> None:
        def failing_start_weston() -> None:
            self.manager._log("weston").write_text("")
            self.manager._log("weston-stdout").write_text("weston stdout detail\n")
            raise RuntimeError("weston display timeout")

        with (
            patch.object(self.manager, "_start_weston", side_effect=failing_start_weston),
            patch("vrbot.orchestrator.missing_requirements", return_value=[]),
        ):
            self.manager.start()
            self.assertTrue(self.manager.wait_idle(2))
        last_error = self.manager.snapshot()["last_error"]
        self.assertIn("weston failed", last_error)
        self.assertIn("weston stdout detail", last_error)
        self.assertIn("--- weston-stdout tail ---", last_error)
        self.assertNotIn("--- weston tail ---", last_error)

    def test_startup_timeout_at_step_boundary_includes_step_and_tail(self) -> None:
        self.manager.cfg = replace(
            self.cfg,
            launch=replace(self.cfg.launch, startup_timeout_sec=0.05),
        )

        def slow_wait_port(host, port, timeout, abort=None) -> None:
            self.manager._log("monado").write_text("monado startup detail\n")
            time.sleep(0.15)

        with (
            patch.object(self.runner, "wait_port", side_effect=slow_wait_port),
            patch("vrbot.orchestrator.missing_requirements", return_value=[]),
        ):
            self.manager.start()
            self.assertTrue(self.manager.wait_idle(2))
        last_error = self.manager.snapshot()["last_error"]
        self.assertEqual(self.manager.state_value(), "failed")
        self.assertIn("monado failed", last_error)
        self.assertIn("startup timeout exceeded", last_error)
        self.assertIn("monado startup detail", last_error)

    def test_startup_tail_read_failure_keeps_original_error(self) -> None:
        self.runner.fail_at = "wait_port"
        with (
            patch.object(self.runner, "tail", side_effect=RuntimeError("tail boom")),
            patch("vrbot.orchestrator.missing_requirements", return_value=[]),
        ):
            self.manager.start()
            self.assertTrue(self.manager.wait_idle(2))
        last_error = self.manager.snapshot()["last_error"]
        self.assertEqual(self.manager.state_value(), "failed")
        self.assertIn("monado failed", last_error)
        self.assertIn("monado port timeout", last_error)
        self.assertNotIn("tail boom", last_error)

    def test_startup_timeout_bounds_slow_step(self) -> None:
        self.manager.cfg = replace(
            self.cfg,
            launch=replace(self.cfg.launch, startup_timeout_sec=0.25),
            timeouts=replace(self.cfg.timeouts, monado_startup_sec=2),
        )

        def slow_wait_port(host, port, timeout, abort=None) -> None:
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                time.sleep(0.01)
            raise TimeoutError("monado port timeout")

        started = time.monotonic()
        with (
            patch.object(self.runner, "wait_port", side_effect=slow_wait_port),
            patch("vrbot.orchestrator.missing_requirements", return_value=[]),
        ):
            self.manager.start()
            self.assertTrue(self.manager.wait_idle(5))
        elapsed = time.monotonic() - started
        snapshot = self.manager.snapshot()
        self.assertEqual(snapshot["state"], "failed")
        self.assertIn("monado failed", snapshot["last_error"])
        self.assertLess(elapsed, 1.0)

    def test_concurrent_start_is_rejected(self) -> None:
        self.runner.block_wait = threading.Event()
        with patch("vrbot.orchestrator.missing_requirements", return_value=[]):
            self.manager.start()
            with self.assertRaises(ConflictError):
                self.manager.start()
        self.runner.block_wait.set()
        self.assertTrue(self.manager.wait_idle(2))

    def test_stop_is_idempotent(self) -> None:
        self.start()
        self.manager.stop()
        self.assertTrue(self.manager.wait_idle(2))
        self.manager.stop()
        self.assertEqual(self.manager.state_value(), "stopped")
        self.assertIsNone(self.runner.pointer)
        self.assertTrue(self.preview.stopped)

    def test_clean_stop_reports_no_survivors(self) -> None:
        self.start()
        self.manager.stop()
        self.assertTrue(self.manager.wait_idle(2))
        snapshot = self.manager.snapshot()
        self.assertEqual(snapshot["state"], "stopped")
        self.assertEqual(snapshot["survivors"], [])
        session_log = Path(snapshot["session_dir"]) / "session.log"
        self.assertNotIn("surviving processes", session_log.read_text())

    def test_stop_reports_surviving_process_group(self) -> None:
        self.start()
        self.runner.handles["steam"]._group_alive = True
        self.manager.stop()
        self.assertTrue(self.manager.wait_idle(2))
        snapshot = self.manager.snapshot()
        self.assertEqual(snapshot["state"], "stopped")
        self.assertIn("steam", snapshot["survivors"])
        session_log = Path(snapshot["session_dir"]) / "session.log"
        self.assertIn("warning: surviving processes: steam", session_log.read_text())

    def test_start_clears_previous_survivors(self) -> None:
        self.manager._survivors = ["steam"]
        self.start()
        self.assertEqual(self.manager.snapshot()["survivors"], [])

    def test_start_resets_stale_handles_and_game_pids(self) -> None:
        self.manager._handles = {"ghost": FakeHandle("ghost", 900)}
        self.manager._game_pids = [777]
        self.manager._survivors = ["ghost"]
        self.start()
        self.assertNotIn("ghost", self.manager._handles)
        self.assertNotIn(777, self.manager._game_pids)
        self.assertEqual(self.manager.snapshot()["survivors"], [])

    def test_cleanup_failure_still_clears_state(self) -> None:
        self.runner.fail_at = "wait_port"
        original_remove = self.runner.remove_stale_socket
        calls = 0

        def flaky_remove(path):
            nonlocal calls
            calls += 1
            if calls >= 2:
                raise RuntimeError("cleanup boom")
            return original_remove(path)

        with (
            patch.object(self.runner, "remove_stale_socket", side_effect=flaky_remove),
            patch("vrbot.orchestrator.missing_requirements", return_value=[]),
        ):
            self.manager.start()
            self.assertTrue(self.manager.wait_idle(2))
        snapshot = self.manager.snapshot()
        self.assertEqual(snapshot["state"], "failed")
        self.assertIsNone(self.runner.pointer)
        self.assertEqual(self.manager._handles, {})
        self.assertEqual(self.manager._game_pids, [])
        self.assertEqual(snapshot["survivors"], [])

    def test_cleanup_phase_failure_does_not_skip_remaining_phases(self) -> None:
        self.start()
        self.runner.handles["steam"]._group_alive = True
        with patch.object(
            self.runner,
            "remove_stale_socket",
            side_effect=RuntimeError("socket boom"),
        ):
            self.manager.stop()
            self.assertTrue(self.manager.wait_idle(2))
        snapshot = self.manager.snapshot()
        self.assertEqual(snapshot["state"], "stopped")
        self.assertTrue(self.preview.stopped)
        self.assertIsNone(self.manager._preview)
        self.assertTrue(all(not handle.alive() for handle in self.runner.handles.values()))
        self.assertEqual(snapshot["survivors"], ["steam"])
        session_log = Path(snapshot["session_dir"]) / "session.log"
        text = session_log.read_text()
        self.assertIn("socket boom", text)
        self.assertIn("warning: surviving processes: steam", text)

    def test_cleanup_first_phase_failure_still_stops_later_handles(self) -> None:
        self.start()
        pose = self.runner.handles["pose"]
        later = {
            name: self.runner.handles[name] for name in ("steam", "weston", "monado")
        }

        def failing_stop(timeout: float = 5.0) -> None:
            raise RuntimeError("pose stop boom")

        pose.stop = failing_stop
        self.manager.stop()
        self.assertTrue(self.manager.wait_idle(2))
        snapshot = self.manager.snapshot()
        self.assertEqual(snapshot["state"], "stopped")
        self.assertTrue(self.preview.stopped)
        self.assertIsNone(self.manager._preview)
        self.assertIn("pose", snapshot["survivors"])
        self.assertTrue(all(not handle.alive() for handle in later.values()))
        session_log = Path(snapshot["session_dir"]) / "session.log"
        text = session_log.read_text()
        self.assertIn("cleanup phase pose failed: pose stop boom", text)
        self.assertIn("warning: surviving processes: pose", text)

    def test_startup_failure_preserves_step_error_when_cleanup_fails(self) -> None:
        original_wait_log = self.runner.wait_log

        def failing_wait_log(path, needle, timeout, abort=None):
            if needle == "handshake ok":
                Path(path).write_text("pose handshake detail\n")
                raise RuntimeError("pose handshake timeout")
            return original_wait_log(path, needle, timeout, abort)

        original_remove = self.runner.remove_stale_socket
        calls = 0

        def flaky_remove(path):
            nonlocal calls
            calls += 1
            if calls >= 2:
                raise RuntimeError("cleanup boom")
            return original_remove(path)

        with (
            patch.object(self.runner, "wait_log", side_effect=failing_wait_log),
            patch.object(self.runner, "remove_stale_socket", side_effect=flaky_remove),
            patch("vrbot.orchestrator.missing_requirements", return_value=[]),
        ):
            self.manager.start()
            self.assertTrue(self.manager.wait_idle(2))
        snapshot = self.manager.snapshot()
        self.assertEqual(snapshot["state"], "failed")
        self.assertIn("pose failed", snapshot["last_error"])
        self.assertIn("pose handshake detail", snapshot["last_error"])
        self.assertNotIn("cleanup failed", snapshot["last_error"])
        session_log = Path(snapshot["session_dir"]) / "session.log"
        self.assertIn("cleanup boom", session_log.read_text())

    def test_stop_cleanup_failure_without_prior_error_reports_and_logs(self) -> None:
        self.start()
        with patch.object(
            self.runner,
            "remove_stale_socket",
            side_effect=RuntimeError("socket boom"),
        ):
            self.manager.stop()
            self.assertTrue(self.manager.wait_idle(2))
        snapshot = self.manager.snapshot()
        self.assertEqual(snapshot["state"], "stopped")
        self.assertIn("cleanup failed", snapshot["last_error"])
        self.assertIn("socket boom", snapshot["last_error"])
        session_log = Path(snapshot["session_dir"]) / "session.log"
        self.assertIn("socket boom", session_log.read_text())

    def test_stop_during_startup_clears_cleanup_error(self) -> None:
        entered = threading.Event()
        release = threading.Event()

        def blocking_wait_port(host, port, timeout, abort=None) -> None:
            entered.set()
            release.wait(2)

        original_remove = self.runner.remove_stale_socket
        calls = 0

        def flaky_remove(path):
            nonlocal calls
            calls += 1
            if calls >= 2:
                raise RuntimeError("cleanup boom")
            return original_remove(path)

        with (
            patch.object(self.runner, "wait_port", side_effect=blocking_wait_port),
            patch.object(self.runner, "remove_stale_socket", side_effect=flaky_remove),
            patch("vrbot.orchestrator.missing_requirements", return_value=[]),
        ):
            self.manager.start()
            self.assertTrue(entered.wait(2))
            self.manager.stop()
            release.set()
            self.assertTrue(self.manager.wait_idle(2))
        snapshot = self.manager.snapshot()
        self.assertEqual(snapshot["state"], "stopped")
        self.assertIsNone(snapshot["last_error"])
        session_log = Path(snapshot["session_dir"]) / "session.log"
        self.assertIn("cleanup boom", session_log.read_text())

    def test_stop_during_preflight_removes_pointer(self) -> None:
        entered = threading.Event()
        release = threading.Event()
        original_preflight = self.manager._preflight

        def blocking_preflight(gen) -> None:
            original_preflight(gen)
            entered.set()
            self.assertTrue(release.wait(2))

        with (
            patch.object(self.manager, "_preflight", side_effect=blocking_preflight),
            patch("vrbot.orchestrator.missing_requirements", return_value=[]),
        ):
            thread = threading.Thread(target=self.manager.start, daemon=True)
            thread.start()
            try:
                self.assertTrue(entered.wait(2))
                session_dir = self.manager._session_dir
                self.assertIsNotNone(session_dir)
                assert session_dir is not None
                self.assertTrue(session_dir.exists())
                self.manager.stop()
                self.assertTrue(self.manager.wait_idle(2))
                release.set()
                thread.join(2)
                worker = self.manager._thread
                self.assertIsNotNone(worker)
                worker.join(2)
            finally:
                release.set()
                thread.join(2)
        self.assertTrue(self.manager.wait_idle(2))
        snapshot = self.manager.snapshot()
        self.assertEqual(snapshot["state"], "stopped")
        self.assertIsNone(self.runner.pointer)
        self.assertIsNone(snapshot["session_dir"])
        self.assertFalse(session_dir.exists())

    def test_stop_after_final_startup_checkpoint_ends_stopped(self) -> None:
        reached = threading.Event()
        release = threading.Event()
        cleanup_calls: list[bool] = []
        original_startup = self.manager._startup
        original_cleanup = self.manager._cleanup

        def paused_startup() -> None:
            original_startup()
            reached.set()
            self.assertTrue(release.wait(2))

        def counting_cleanup() -> None:
            cleanup_calls.append(True)
            original_cleanup()

        with (
            patch.object(self.manager, "_startup", side_effect=paused_startup),
            patch.object(self.manager, "_cleanup", side_effect=counting_cleanup),
            patch("vrbot.orchestrator.missing_requirements", return_value=[]),
        ):
            self.manager.start()
            self.assertTrue(reached.wait(2))
            self.manager.stop()
            release.set()
            self.assertTrue(self.manager.wait_idle(2))
        self.assertEqual(self.manager.state_value(), "stopped")
        self.assertEqual(len(cleanup_calls), 1)
        self.assertTrue(self.preview.stopped)
        self.assertIsNone(self.runner.pointer)

    def test_stop_during_starting_cleans_up_once(self) -> None:
        entered = threading.Event()
        release = threading.Event()
        cleanup_calls: list[bool] = []
        original_cleanup = self.manager._cleanup

        def blocking_wait_port(host, port, timeout, abort=None) -> None:
            entered.set()
            release.wait(2)

        def counting_cleanup() -> None:
            cleanup_calls.append(True)
            original_cleanup()

        with (
            patch.object(self.runner, "wait_port", side_effect=blocking_wait_port),
            patch.object(self.manager, "_cleanup", side_effect=counting_cleanup),
            patch("vrbot.orchestrator.missing_requirements", return_value=[]),
        ):
            self.manager.start()
            self.assertTrue(entered.wait(2))
            self.manager.stop()
            release.set()
            self.assertTrue(self.manager.wait_idle(2))
        self.assertEqual(self.manager.state_value(), "stopped")
        self.assertEqual(len(cleanup_calls), 1)
        self.assertFalse(self.preview.started)
        self.assertIsNone(self.runner.pointer)

    def test_stop_during_starting_has_no_failure_tail(self) -> None:
        entered = threading.Event()
        release = threading.Event()

        def blocking_wait_port(host, port, timeout, abort=None) -> None:
            entered.set()
            release.wait(2)

        with (
            patch.object(self.runner, "wait_port", side_effect=blocking_wait_port),
            patch("vrbot.orchestrator.missing_requirements", return_value=[]),
        ):
            self.manager.start()
            self.assertTrue(entered.wait(2))
            self.manager.stop()
            release.set()
            self.assertTrue(self.manager.wait_idle(2))
        snapshot = self.manager.snapshot()
        self.assertEqual(snapshot["state"], "stopped")
        self.assertIsNone(snapshot["last_error"])

    def test_stop_before_start_worker_cleans_up_once(self) -> None:
        allow = threading.Event()
        finished = threading.Event()
        cleanup_calls: list[bool] = []
        original_start_worker = self.manager._start_worker
        original_cleanup = self.manager._cleanup

        def delayed_start_worker(gen, session_dir) -> None:
            self.assertTrue(allow.wait(2))
            original_start_worker(gen, session_dir)
            finished.set()

        def counting_cleanup() -> None:
            cleanup_calls.append(True)
            original_cleanup()

        with (
            patch.object(self.manager, "_start_worker", side_effect=delayed_start_worker),
            patch.object(self.manager, "_cleanup", side_effect=counting_cleanup),
            patch("vrbot.orchestrator.missing_requirements", return_value=[]),
        ):
            self.manager.start()
            captured = self.manager._session_dir
            self.assertIsNotNone(captured)
            assert captured is not None
            self.assertTrue(captured.exists())
            self.manager.stop()
            allow.set()
            self.assertTrue(finished.wait(2))
            self.assertTrue(self.manager.wait_idle(2))
        self.assertEqual(self.manager.state_value(), "stopped")
        self.assertEqual(len(cleanup_calls), 1)
        self.assertIsNone(self.runner.pointer)
        self.assertFalse(captured.exists())

    def test_stop_during_preflight_then_start_preserves_new_session(self) -> None:
        entered = threading.Event()
        release = threading.Event()
        original_preflight = self.manager._preflight
        calls = 0
        calls_lock = threading.Lock()

        def blocking_first_preflight(gen) -> None:
            nonlocal calls
            with calls_lock:
                calls += 1
                first = calls == 1
            if first:
                entered.set()
                self.assertTrue(release.wait(5))
            original_preflight(gen)

        with (
            patch.object(
                self.manager, "_preflight", side_effect=blocking_first_preflight
            ),
            patch("vrbot.orchestrator.missing_requirements", return_value=[]),
        ):
            thread_a = threading.Thread(target=self.manager.start, daemon=True)
            thread_a.start()
            self.assertTrue(entered.wait(2))
            self.manager.stop()
            self.assertTrue(self.manager.wait_idle(2))

            self.manager.start()
            self.assertTrue(self.manager.wait_idle(2))
            self.assertEqual(self.manager.state_value(), "running")
            session_dir = Path(self.manager.snapshot()["session_dir"])
            pointer = self.runner.pointer
            self.assertIsNotNone(pointer)
            marker = session_dir / "marker.log"
            marker.write_text("new session\n")

            release.set()
            thread_a.join(5)

        self.assertFalse(thread_a.is_alive())
        self.assertTrue(session_dir.exists())
        self.assertTrue(marker.exists())
        self.assertEqual(self.runner.pointer, pointer)
        self.assertEqual(self.manager.state_value(), "running")
        self.assertEqual(self.manager.snapshot()["session_dir"], str(session_dir))

    def test_late_preflight_failure_does_not_overwrite_successor(self) -> None:
        entered = threading.Event()
        release = threading.Event()
        discard_entered = threading.Event()
        allow_discard = threading.Event()
        a_errors: list[BaseException] = []
        original_preflight = self.manager._preflight
        original_discard = self.manager._discard_attempt

        def failing_preflight(gen) -> None:
            if gen == 1:
                entered.set()
                self.assertTrue(release.wait(5))
                raise PreflightError("late preflight failure")
            original_preflight(gen)

        def delayed_discard(gen, session_dir) -> None:
            if gen == 1 and not discard_entered.is_set():
                discard_entered.set()
                self.assertTrue(allow_discard.wait(5))
            original_discard(gen, session_dir)

        def run_a() -> None:
            try:
                self.manager.start()
            except BaseException as exc:
                a_errors.append(exc)

        with (
            patch.object(
                self.manager, "_preflight", side_effect=failing_preflight
            ),
            patch.object(
                self.manager, "_discard_attempt", side_effect=delayed_discard
            ),
            patch("vrbot.orchestrator.missing_requirements", return_value=[]),
        ):
            thread_a = threading.Thread(target=run_a, daemon=True)
            thread_a.start()
            self.assertTrue(entered.wait(2))
            self.manager.stop()
            self.assertTrue(self.manager.wait_idle(2))
            release.set()
            self.assertTrue(discard_entered.wait(2))

            self.manager.start()
            self.assertEqual(self.manager.state_value(), "starting")
            session_dir = Path(self.manager._session_dir or "")
            pointer = self.runner.pointer
            self.assertIsNotNone(pointer)
            marker = session_dir / "marker.log"
            marker.write_text("new session\n")

            allow_discard.set()
            thread_a.join(5)
            self.assertTrue(self.manager.wait_idle(2))

        self.assertFalse(thread_a.is_alive())
        self.assertEqual(a_errors, [])
        self.assertEqual(self.manager.state_value(), "running")
        self.assertTrue(session_dir.exists())
        self.assertTrue(marker.exists())
        self.assertIsNotNone(self.runner.pointer)
        self.assertEqual(self.runner.pointer["session"], pointer["session"])
        self.assertEqual(self.manager.snapshot()["session_dir"], str(session_dir))

    def test_stale_preflight_does_not_remove_successor_socket(self) -> None:
        runtime_dir = self.base / "runtime"
        runtime_dir.mkdir()
        self.manager.runtime_dir = runtime_dir
        socket_path = runtime_dir / "monado_comp_ipc"
        entered = threading.Event()
        release = threading.Event()
        a_errors: list[BaseException] = []
        sockets: list[socket.socket] = []
        original_start = self.runner.start
        calls = 0
        calls_lock = threading.Lock()

        def blocking_port(host, port) -> bool:
            nonlocal calls
            with calls_lock:
                calls += 1
                call = calls
            if call == 1:
                entered.set()
                self.assertTrue(release.wait(5))
            return False

        def removing_socket(path) -> bool:
            try:
                Path(path).unlink()
            except FileNotFoundError:
                pass
            return True

        def creating_start(name, argv, env, cwd, log_path, stderr_path=None):
            handle = original_start(name, argv, env, cwd, log_path, stderr_path)
            if name == "monado":
                sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                sock.bind(str(socket_path))
                sockets.append(sock)
            return handle

        def run_a() -> None:
            try:
                self.manager.start()
            except BaseException as exc:
                a_errors.append(exc)

        with (
            patch.object(self.runner, "port_in_use", side_effect=blocking_port),
            patch.object(self.runner, "remove_stale_socket", side_effect=removing_socket),
            patch.object(self.runner, "start", side_effect=creating_start),
            patch("vrbot.orchestrator.missing_requirements", return_value=[]),
        ):
            thread_a = threading.Thread(target=run_a, daemon=True)
            thread_a.start()
            self.assertTrue(entered.wait(2))
            self.manager.stop()
            self.assertTrue(self.manager.wait_idle(2))

            self.manager.start()
            self.assertTrue(self.manager.wait_idle(2))
            self.assertEqual(self.manager.state_value(), "running")
            self.assertTrue(socket_path.exists())
            session_dir = Path(self.manager.snapshot()["session_dir"])
            pointer = self.runner.pointer

            release.set()
            thread_a.join(5)

        try:
            self.assertFalse(thread_a.is_alive())
            self.assertTrue(a_errors)
            self.assertIsInstance(a_errors[0], ConflictError)
            self.assertTrue(socket_path.exists())
            self.assertEqual(self.manager.state_value(), "running")
            self.assertTrue(session_dir.exists())
            self.assertEqual(self.runner.pointer, pointer)
            self.assertEqual(self.manager.snapshot()["session_dir"], str(session_dir))
        finally:
            for sock in sockets:
                sock.close()

    def test_stale_monitor_does_not_tear_down_successor(self) -> None:
        self.start()
        old_monado = self.runner.handles["monado"]
        captured = threading.Event()
        release = threading.Event()
        first = True
        first_lock = threading.Lock()
        original_alive = old_monado.alive

        def blocking_alive() -> bool:
            nonlocal first
            with first_lock:
                block = first
                first = False
            if block:
                captured.set()
                self.assertTrue(release.wait(5))
                return False
            return original_alive()

        old_monado.alive = blocking_alive
        old_monitor = self.manager._monitor_thread
        self.assertIsNotNone(old_monitor)
        assert old_monitor is not None
        self.assertTrue(captured.wait(3))

        self.manager.stop()
        self.assertTrue(self.manager.wait_idle(2))
        self.start()

        successor_monado = self.manager._handles["monado"]
        session_dir = Path(self.manager.snapshot()["session_dir"])
        pointer = self.runner.pointer

        release.set()
        old_monitor.join(3)

        self.assertFalse(old_monitor.is_alive())
        self.assertEqual(self.manager.state_value(), "running")
        self.assertIs(self.manager._handles["monado"], successor_monado)
        self.assertTrue(successor_monado.alive())
        self.assertTrue(session_dir.exists())
        self.assertEqual(self.runner.pointer, pointer)
        self.assertEqual(self.manager.snapshot()["session_dir"], str(session_dir))
        self.assertIsNone(self.manager.snapshot()["last_error"])

    def test_stale_worker_removes_abandoned_directory(self) -> None:
        entered = threading.Event()
        release = threading.Event()
        finished = threading.Event()
        original_start_worker = self.manager._start_worker

        def delayed_first_worker(gen, session_dir) -> None:
            if gen == 1:
                entered.set()
                self.assertTrue(release.wait(5))
            original_start_worker(gen, session_dir)
            if gen == 1:
                finished.set()

        with (
            patch.object(
                self.manager, "_start_worker", side_effect=delayed_first_worker
            ),
            patch("vrbot.orchestrator.missing_requirements", return_value=[]),
        ):
            self.manager.start()
            self.assertTrue(entered.wait(2))
            abandoned = self.manager._session_dir
            self.assertIsNotNone(abandoned)
            assert abandoned is not None
            self.assertTrue(abandoned.exists())
            self.manager.stop()
            self.assertTrue(self.manager.wait_idle(2))

            self.manager.start()
            self.assertTrue(self.manager.wait_idle(2))
            self.assertEqual(self.manager.state_value(), "running")
            session_dir = Path(self.manager.snapshot()["session_dir"])
            pointer = self.runner.pointer

            release.set()
            self.assertTrue(finished.wait(2))

        self.assertFalse(abandoned.exists())
        self.assertTrue(session_dir.exists())
        self.assertEqual(self.runner.pointer, pointer)
        self.assertEqual(self.manager.state_value(), "running")
        self.assertEqual(self.manager.snapshot()["session_dir"], str(session_dir))

    def test_stop_start_idle_race_keeps_idle_clear_while_starting(self) -> None:
        self.start()
        entered_b = threading.Event()
        release_b = threading.Event()
        idle_done = threading.Event()
        allow_idle = threading.Event()
        original_preflight = self.manager._preflight
        original_idle_set = self.manager._idle.set

        def blocking_second_preflight(gen) -> None:
            if gen == 2:
                entered_b.set()
                self.assertTrue(release_b.wait(5))
            original_preflight(gen)

        def guarded_idle_set() -> None:
            if not self.manager._lock._is_owned():
                self.assertTrue(allow_idle.wait(5))
            original_idle_set()
            idle_done.set()

        with (
            patch.object(
                self.manager, "_preflight", side_effect=blocking_second_preflight
            ),
            patch.object(self.manager._idle, "set", side_effect=guarded_idle_set),
            patch("vrbot.orchestrator.missing_requirements", return_value=[]),
        ):
            self.manager.stop()
            self.assertTrue(self.manager.wait_for_state("stopped", 2))
            thread_b = threading.Thread(target=self.manager.start, daemon=True)
            thread_b.start()
            self.assertTrue(entered_b.wait(2))
            self.assertEqual(self.manager.state_value(), "starting")

            allow_idle.set()
            self.assertTrue(idle_done.wait(2))
            self.assertFalse(self.manager.wait_idle(0.2))
            self.assertEqual(self.manager.state_value(), "starting")

            release_b.set()
            thread_b.join(5)

        self.assertTrue(self.manager.wait_idle(2))
        self.assertEqual(self.manager.state_value(), "running")

    def test_discard_logs_directory_removal_failure(self) -> None:
        allow = threading.Event()
        finished = threading.Event()
        original_start_worker = self.manager._start_worker

        def delayed_start_worker(gen, session_dir) -> None:
            self.assertTrue(allow.wait(2))
            original_start_worker(gen, session_dir)
            finished.set()

        with (
            patch.object(self.manager, "_start_worker", side_effect=delayed_start_worker),
            patch(
                "vrbot.orchestrator.shutil.rmtree",
                side_effect=OSError("rmtree boom"),
            ),
            patch("vrbot.orchestrator.missing_requirements", return_value=[]),
        ):
            self.manager.start()
            captured = self.manager._session_dir
            self.assertIsNotNone(captured)
            assert captured is not None
            self.manager.stop()
            allow.set()
            self.assertTrue(finished.wait(2))
            self.assertTrue(self.manager.wait_idle(2))

        text = (captured / "session.log").read_text()
        self.assertIn("session directory removal failed: rmtree boom", text)

    def test_cleanup_pointer_failure_is_not_reported_complete(self) -> None:
        self.start()
        with patch.object(
            self.runner, "remove_pointer", side_effect=RuntimeError("pointer boom")
        ):
            self.manager.stop()
            self.assertTrue(self.manager.wait_idle(2))

        snapshot = self.manager.snapshot()
        text = (Path(snapshot["session_dir"]) / "session.log").read_text()
        self.assertIn("pointer removal failed: pointer boom", text)
        self.assertNotIn("cleanup complete", text)
        self.assertIn("cleanup finished (with", text)

    def test_stop_during_preflight_then_start_does_not_duplicate_processes(
        self,
    ) -> None:
        entered = threading.Event()
        release_preflight = threading.Event()
        worker_entered = threading.Event()
        release_worker = threading.Event()
        worker_finished = threading.Event()
        original_preflight = self.manager._preflight
        original_start_worker = self.manager._start_worker
        original_start = self.runner.start
        starts: list[str] = []

        def blocking_preflight(gen) -> None:
            entered.set()
            self.assertTrue(release_preflight.wait(5))
            original_preflight(gen)

        def delayed_first_worker(gen, session_dir) -> None:
            if gen == 1:
                worker_entered.set()
                self.assertTrue(release_worker.wait(5))
            original_start_worker(gen, session_dir)
            if gen == 1:
                worker_finished.set()

        def recording_start(name, argv, env, cwd, log_path, stderr_path=None):
            starts.append(name)
            return original_start(name, argv, env, cwd, log_path, stderr_path)

        with (
            patch.object(
                self.manager, "_preflight", side_effect=blocking_preflight
            ),
            patch.object(
                self.manager, "_start_worker", side_effect=delayed_first_worker
            ),
            patch.object(self.runner, "start", side_effect=recording_start),
            patch("vrbot.orchestrator.missing_requirements", return_value=[]),
        ):
            thread_a = threading.Thread(target=self.manager.start, daemon=True)
            thread_a.start()
            self.assertTrue(entered.wait(2))
            self.manager.stop()
            self.assertTrue(self.manager.wait_idle(2))
            release_preflight.set()
            thread_a.join(5)
            self.assertFalse(thread_a.is_alive())
            self.assertTrue(worker_entered.wait(2))

            self.manager.start()
            self.assertTrue(self.manager.wait_idle(2))
            self.assertEqual(self.manager.state_value(), "running")
            session_dir = Path(self.manager.snapshot()["session_dir"])
            pointer = self.runner.pointer
            self.assertIsNotNone(pointer)

            release_worker.set()
            self.assertTrue(worker_finished.wait(2))

        self.assertEqual(self.manager.state_value(), "running")
        self.assertTrue(session_dir.exists())
        self.assertEqual(self.runner.pointer, pointer)
        self.assertEqual(self.manager.snapshot()["session_dir"], str(session_dir))
        self.assertEqual(sorted(starts), ["monado", "pose", "steam", "weston"])

    def test_startup_failure_log_error_still_cleans_up(self) -> None:
        original_log = self.manager._session_log

        def failing_log(message: str) -> None:
            if message.startswith("startup failed:"):
                raise OSError("log failure")
            original_log(message)

        self.runner.fail_at = "wait_port"
        with (
            patch.object(self.manager, "_session_log", side_effect=failing_log),
            patch("vrbot.orchestrator.missing_requirements", return_value=[]),
        ):
            self.manager.start()
            self.assertTrue(self.manager.wait_idle(2))
        snapshot = self.manager.snapshot()
        self.assertIn(snapshot["state"], ("failed", "stopped"))
        self.assertNotEqual(snapshot["state"], "starting")
        self.assertIsNone(self.runner.pointer)
        self.assertEqual(self.manager._handles, {})

    def test_preflight_failure_after_stop_ends_stopped(self) -> None:
        entered = threading.Event()
        release = threading.Event()

        def failing_preflight(gen) -> None:
            entered.set()
            self.assertTrue(release.wait(5))
            raise PreflightError("late preflight failure")

        with (
            patch.object(self.manager, "_preflight", side_effect=failing_preflight),
            patch("vrbot.orchestrator.missing_requirements", return_value=[]),
        ):
            thread = threading.Thread(target=self.manager.start, daemon=True)
            thread.start()
            self.assertTrue(entered.wait(2))
            self.manager.stop()
            self.assertTrue(self.manager.wait_idle(2))
            release.set()
            thread.join(5)

        self.assertFalse(thread.is_alive())
        snapshot = self.manager.snapshot()
        self.assertEqual(snapshot["state"], "stopped")
        self.assertIsNone(snapshot["last_error"])
        self.assertIsNone(self.runner.pointer)

    def test_mode_switch_restarts_pose_only(self) -> None:
        self.start()
        old_pose = self.runner.handles["pose"]
        self.manager.set_mode("yaw")
        self.assertFalse(old_pose.alive())
        self.assertTrue(self.runner.handles["pose"].alive())
        self.assertEqual(self.manager.snapshot()["mode"], "yaw")

    def test_monado_death_fails_and_cleans_up(self) -> None:
        self.start()
        self.runner.handles["monado"]._alive = False
        self.assertTrue(self.manager.wait_for_state("failed", 3))
        self.assertIn("monado", self.manager.snapshot()["last_error"])
        self.assertIsNone(self.runner.pointer)
        self.assertTrue(all(not handle.alive() for handle in self.runner.handles.values()))


class OpenXRTests(unittest.TestCase):
    def test_parser_handles_multiple_clients(self) -> None:
        log = (
            " INFO [client_connected] Client 1 connected\n"
            "\tapplication_name: 'steam'\n"
            " INFO [client_connected] Client 2 connected\n"
            "\tapplication_name: 'Unity Application'\n"
        )
        self.assertEqual(parse_openxr_client(log), "Unity Application")

    def test_parser_returns_none_after_disconnect(self) -> None:
        log = (
            " INFO [client_connected] Client 1 connected\n"
            "\tapplication_name: 'Unity Application'\n"
            " INFO [client_disconnected] Client 1 disconnected\n"
        )
        self.assertIsNone(parse_openxr_client(log))

    def test_parser_handles_final_line_without_newline(self) -> None:
        log = (
            " INFO [client_connected] Client 1 connected\n"
            "\tapplication_name: 'Unity Application'"
        )
        self.assertEqual(parse_openxr_client(log), "Unity Application")

    def test_incremental_parser_reports_unterminated_application_line(self) -> None:
        parser = _OpenXRLogParser()
        parser.feed(" INFO [client_connected] Client 1 connected\n")
        self.assertIsNone(parser.value())
        parser.feed("\tapplication_name: 'X'")
        self.assertEqual(parser.value(), "X")
        parser.feed("\n")
        self.assertEqual(parser.value(), "X")
        parser.feed(" INFO [client_disconnected] Client 1 disconnected\n")
        self.assertIsNone(parser.value())

    def test_incremental_parser_caps_unterminated_buffer(self) -> None:
        parser = _OpenXRLogParser()
        parser.feed("x" * (1024 * 1024 + 128))
        self.assertLessEqual(len(parser._buffer), 1024 * 1024)
        parser.feed(
            "\n INFO [client_connected] Client 1 connected\n"
            "\tapplication_name: 'Unity Application'\n"
        )
        self.assertEqual(parser.value(), "Unity Application")


if __name__ == "__main__":
    unittest.main()
