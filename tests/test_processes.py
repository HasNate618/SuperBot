from __future__ import annotations

import collections
import json
import os
import signal
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from vrbot.processes import Aborted, ProcessRunner, tail_stream, tail_text


class ProcessTests(unittest.TestCase):
    def test_pointer_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            pointer = Path(raw) / "current-session.json"
            runner = ProcessRunner(pointer)
            self.assertIsNone(runner.read_pointer())
            runner.write_pointer({"session": "/tmp/x", "processes": {}})
            self.assertEqual(runner.read_pointer()["session"], "/tmp/x")
            runner.remove_pointer()
            self.assertIsNone(runner.read_pointer())

    def test_read_pointer_rejects_invalid_utf8_and_non_object(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            pointer = Path(raw) / "pointer.json"
            runner = ProcessRunner(pointer)
            pointer.write_bytes(b"\xff\xfe")
            self.assertIsNone(runner.read_pointer())
            pointer.write_text("[1, 2]")
            self.assertIsNone(runner.read_pointer())

    def test_proc_scan_and_cmdline(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            proc_root = Path(raw)
            pid_dir = proc_root / "123"
            pid_dir.mkdir()
            argv0 = r"Z:\home\nate\Games\SUPERHOT VR\SUPERHOT_VR.exe"
            (pid_dir / "cmdline").write_bytes(argv0.encode() + b"\0")
            runner = ProcessRunner(Path(raw) / "pointer.json", proc_root=proc_root)
            self.assertEqual(runner.pids_by_argv0(argv0), [123])
            self.assertEqual(runner.pid_cmdline(123)[0], argv0)
            self.assertTrue(runner.pid_alive(123))
            self.assertFalse(runner.pid_alive(999))

    def test_remove_stale_socket(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "monado_comp_ipc"
            sock = socket.socket(socket.AF_UNIX)
            sock.bind(str(path))
            sock.close()
            runner = ProcessRunner(Path(raw) / "pointer.json")
            self.assertTrue(runner.remove_stale_socket(path))
            self.assertFalse(path.exists())
            self.assertFalse(runner.remove_stale_socket(path))

    def test_remove_stale_socket_tolerates_concurrent_unlink(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "monado_comp_ipc"
            sock = socket.socket(socket.AF_UNIX)
            sock.bind(str(path))
            sock.close()
            runner = ProcessRunner(Path(raw) / "pointer.json")
            with mock.patch.object(Path, "unlink", side_effect=FileNotFoundError):
                self.assertFalse(runner.remove_stale_socket(path))

    def test_start_and_stop_process_group(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            runner = ProcessRunner(base / "pointer.json")
            handle = runner.start(
                "sleeper",
                ["/bin/sh", "-c", "sleep 30"],
                os.environ.copy(),
                base,
                base / "sleeper.log",
            )
            self.assertTrue(handle.alive())
            self.assertTrue((base / "sleeper.log").exists())
            handle.stop(timeout=1)
            self.assertFalse(handle.alive())

    def test_stop_kills_group_after_leader_exits(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            runner = ProcessRunner(base / "pointer.json")
            handle = runner.start(
                "orphan-group",
                ["/bin/sh", "-c", "sleep 30 & exit 0"],
                os.environ.copy(),
                base,
                base / "orphan.log",
            )
            try:
                deadline = time.monotonic() + 5
                while handle.alive() and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertFalse(handle.alive())
                handle.stop(timeout=1)
                with self.assertRaises(ProcessLookupError):
                    os.killpg(handle.pgid, 0)
            finally:
                try:
                    os.killpg(handle.pgid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                handle.stop(timeout=1)

    def test_stop_escalates_to_sigkill_for_sigterm_ignoring_group(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            runner = ProcessRunner(base / "pointer.json")
            handle = runner.start(
                "stubborn",
                ["/bin/sh", "-c", "trap '' TERM; sleep 30 & wait"],
                os.environ.copy(),
                base,
                base / "stubborn.log",
            )
            try:
                handle.stop(timeout=0.2)
                with self.assertRaises(ProcessLookupError):
                    os.killpg(handle.pgid, 0)
            finally:
                try:
                    os.killpg(handle.pgid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                handle.stop(timeout=1)

    def test_wait_port_aborts_when_event_set(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            runner = ProcessRunner(Path(raw) / "pointer.json")
            abort = threading.Event()
            abort.set()
            with self.assertRaises(Aborted):
                runner.wait_port("127.0.0.1", 9, timeout=1, abort=abort)

    def test_wait_log_times_out(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "log.txt"
            path.write_text("")
            runner = ProcessRunner(Path(raw) / "pointer.json")
            with self.assertRaises(TimeoutError):
                runner.wait_log(path, "absent", timeout=0.1)

    def test_wait_log_finds_needle_written_after_start(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "log.txt"
            path.write_text("abcnee")
            runner = ProcessRunner(Path(raw) / "pointer.json")

            def append_needle() -> None:
                with path.open("a") as handle:
                    handle.write("dle\n")

            timer = threading.Timer(0.2, append_needle)
            timer.start()
            try:
                runner.wait_log(path, "needle", timeout=2)
            finally:
                timer.cancel()
                timer.join()

    def test_wait_log_handles_truncate_and_regrow(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "log.txt"
            path.write_text("x" * 200)
            runner = ProcessRunner(Path(raw) / "pointer.json")
            thread, outcome = self._wait_log_in_thread(runner, path, "TARGET")
            time.sleep(0.3)
            path.write_text("a" * 120 + "TARGET" + "b" * 120)
            thread.join(timeout=3)
            self.assertFalse(
                thread.is_alive(), "wait_log did not return after truncate-and-regrow"
            )
            self.assertNotIn(
                "error", outcome, f"wait_log raised: {outcome.get('error')!r}"
            )
            self.assertTrue(outcome.get("ok"), "wait_log did not find needle")

    def test_wait_log_handles_shrink_to_zero(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "log.txt"
            path.write_text("y" * 200)
            runner = ProcessRunner(Path(raw) / "pointer.json")
            thread, outcome = self._wait_log_in_thread(runner, path, "NEW")
            time.sleep(0.3)
            path.write_text("NEW")
            thread.join(timeout=3)
            self.assertFalse(
                thread.is_alive(), "wait_log did not return after shrink-to-zero"
            )
            self.assertNotIn(
                "error", outcome, f"wait_log raised: {outcome.get('error')!r}"
            )
            self.assertTrue(outcome.get("ok"), "wait_log did not find needle")

    def test_tail(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "log.txt"
            path.write_text("\n".join(str(i) for i in range(300)))
            runner = ProcessRunner(Path(raw) / "pointer.json")
            text = runner.tail(path, 5)
            self.assertEqual(text.splitlines(), ["295", "296", "297", "298", "299"])

    def test_tail_zero_and_negative_return_empty(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "log.txt"
            path.write_text("one\ntwo\n")
            runner = ProcessRunner(Path(raw) / "pointer.json")
            self.assertEqual(runner.tail(path, 0), "")
            self.assertEqual(runner.tail(path, -1), "")

    def test_tail_text_returns_last_lines(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "log.txt"
            path.write_text("a\nb\nc\nd\ne\n")
            self.assertEqual(tail_text(path, 2), "d\ne")
            self.assertEqual(tail_text(path, 5), "a\nb\nc\nd\ne")
            self.assertEqual(tail_text(path, 50), "a\nb\nc\nd\ne")

    def test_tail_stream_matches_across_block_sizes(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "log.txt"
            path.write_text("alpha\nbeta\ngamma\ndelta\nepsilon")
            for block_size in (1, 3, 16):
                with path.open("rb") as handle:
                    self.assertEqual(
                        tail_stream(handle, 3, block_size=block_size),
                        "gamma\ndelta\nepsilon",
                    )

    def test_tail_stream_returns_consistent_state_after_shrink_during_read(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "log.txt"
            old = "x\n" * 500
            new = "y\n" * 450
            for block_size in (1, 3, 16, 8192):
                with self.subTest(block_size=block_size):
                    path.write_text(old)
                    real = path.open("rb")

                    class ShrinkingHandle:
                        def __init__(self, handle, path) -> None:
                            self.handle = handle
                            self.path = path
                            self.shrunk = False

                        def seek(self, *args):
                            return self.handle.seek(*args)

                        def tell(self):
                            return self.handle.tell()

                        def fileno(self):
                            return self.handle.fileno()

                        def read(self, amount: int = -1):
                            data = self.handle.read(amount)
                            if not self.shrunk and b"x" in data:
                                self.shrunk = True
                                self.path.write_text(new)
                            return data

                    handle = ShrinkingHandle(real, path)
                    try:
                        text = tail_stream(handle, 100, block_size=block_size)
                    finally:
                        real.close()
                    self.assertTrue(text)
                    self.assertEqual(set(text.splitlines()), {"y"})

    def test_tail_text_preserves_blank_lines_and_missing_final_newline(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "log.txt"
            path.write_text("a\n\nc")
            self.assertEqual(tail_text(path, 2), "\nc")
            self.assertEqual(tail_text(path, 3), "a\n\nc")

    def test_tail_text_replaces_invalid_utf8(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "log.txt"
            path.write_bytes(b"ok\n\xff\xfe\nlast")
            self.assertEqual(tail_text(path, 2), "\ufffd\ufffd\nlast")

    def test_tail_text_missing_file_and_nonpositive_lines(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "missing.log"
            self.assertEqual(tail_text(path, 5), "")
            self.assertEqual(tail_text(path, 0), "")
            self.assertEqual(tail_text(path, -1), "")

    def test_tail_text_matches_reference_semantics(self) -> None:
        contents = (
            "",
            "\n",
            "one",
            "one\n",
            "a\nb\nc\n",
            "a\nb\nc",
            "a\n\nb\n",
            "a\n\n\n",
            "a\r\nb\r\nc\r\n",
            "a\rb\rc\r",
        )
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "log.txt"
            for content in contents:
                for count in (1, 2, 5):
                    with self.subTest(content=content, count=count):
                        path.write_text(content)
                        self.assertEqual(tail_text(path, count), self._reference_tail(path, count))

    def test_tail_text_reads_bounded_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "large.log"
            path.write_text(("x" * 79 + "\n") * 20000)
            size = path.stat().st_size
            real_open = open
            bytes_read = 0

            class CountingFile:
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

                def fileno(self):
                    return self.handle.fileno()

                def read(self, amount: int = -1):
                    nonlocal bytes_read
                    data = self.handle.read(amount)
                    bytes_read += len(data)
                    return data

            def counting_open(*args, **kwargs):
                return CountingFile(real_open(*args, **kwargs))

            with mock.patch("builtins.open", side_effect=counting_open):
                text = tail_text(path, 200)
            self.assertEqual(len(text.splitlines()), 200)
            self.assertLess(bytes_read, size // 10)

    def test_pointer_is_json_on_disk(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            pointer = Path(raw) / "pointer.json"
            runner = ProcessRunner(pointer)
            runner.write_pointer({"a": 1})
            self.assertEqual(json.loads(pointer.read_text()), {"a": 1})

    def _reference_tail(self, path: Path, lines: int) -> str:
        if lines <= 0:
            return ""
        try:
            with path.open("r", errors="replace") as handle:
                data = collections.deque(handle, maxlen=lines)
        except OSError:
            return ""
        text = "".join(data)
        if text.endswith("\n"):
            text = text[:-1]
        return text

    def _wait_log_in_thread(
        self,
        runner: ProcessRunner,
        path: Path,
        needle: str,
        timeout: float = 2.0,
    ) -> tuple[threading.Thread, dict[str, object]]:
        outcome: dict[str, object] = {}

        def wait() -> None:
            try:
                runner.wait_log(path, needle, timeout=timeout)
                outcome["ok"] = True
            except BaseException as exc:
                outcome["error"] = exc

        thread = threading.Thread(target=wait, daemon=True)
        thread.start()
        return thread, outcome


if __name__ == "__main__":
    unittest.main()
