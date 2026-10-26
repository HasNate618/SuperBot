from __future__ import annotations

import collections
import json
import os
import subprocess
import tempfile
import threading
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

    def alive(self) -> bool:
        return self._alive

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

        def blocking_preflight() -> None:
            entered.set()
            self.assertTrue(release.wait(2))
            original_preflight()

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

    def test_stop_before_start_worker_cleans_up_once(self) -> None:
        allow = threading.Event()
        finished = threading.Event()
        cleanup_calls: list[bool] = []
        original_start_worker = self.manager._start_worker
        original_cleanup = self.manager._cleanup

        def delayed_start_worker() -> None:
            self.assertTrue(allow.wait(2))
            original_start_worker()
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
            self.manager.stop()
            allow.set()
            self.assertTrue(finished.wait(2))
            self.assertTrue(self.manager.wait_idle(2))
        self.assertEqual(self.manager.state_value(), "stopped")
        self.assertEqual(len(cleanup_calls), 1)
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
