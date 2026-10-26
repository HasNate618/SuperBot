from __future__ import annotations

import os
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from vrbot.config import load_config
from vrbot.preview import PreviewCapture, _smoke_http, atomic_replace, crop_filter, newest_png


class FakeRun:
    def __init__(self, fail: str | None = None) -> None:
        self.fail = fail
        self.calls: list[tuple[list[str], dict]] = []

    def __call__(self, argv, **kwargs):
        argv = list(argv)
        self.calls.append((argv, kwargs))
        if argv[0] == "weston-screenshooter":
            if self.fail == "screen":
                return subprocess.CompletedProcess(argv, 1, "", "screen failed")
            cwd = Path(kwargs["cwd"])
            shot = cwd / "wayland-screenshot.png"
            shot.write_bytes(b"PNG")
            now = time.time_ns()
            os.utime(shot, ns=(now, now))
            return subprocess.CompletedProcess(argv, 0, "", "")
        if argv[0] == "magick":
            out = Path(argv[-1][4:])
            if self.fail == "magick":
                out.write_bytes(b"PARTIAL")
                return subprocess.CompletedProcess(argv, 1, "", "magick failed")
            out.write_bytes(b"JPEG")
            return subprocess.CompletedProcess(argv, 0, "", "")
        raise AssertionError(f"unexpected command: {argv}")


class ExplodingCapture(PreviewCapture):
    def capture_once(self) -> bool:
        raise RuntimeError("boom")


class TimeoutRun:
    def __call__(self, argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, kwargs.get("timeout", 5))


class BlockingCapture(PreviewCapture):
    def _run(self) -> None:
        self.stop_event.wait(2)


class DirectoryOutputRun(FakeRun):
    def __call__(self, argv, **kwargs):
        argv = list(argv)
        if argv[0] == "magick":
            self.calls.append((argv, kwargs))
            out = Path(argv[-1][4:])
            out.mkdir()
            return subprocess.CompletedProcess(argv, 1, "", "magick failed")
        return super().__call__(argv, **kwargs)


class PreviewTests(unittest.TestCase):
    def make_capture(
        self,
        base: Path,
        run,
        log=None,
        capture_type=PreviewCapture,
    ) -> PreviewCapture:
        return capture_type(
            width=1280,
            height=720,
            panel_height=32,
            fps=8,
            jpeg_quality=80,
            screenshooter="weston-screenshooter",
            magick="magick",
            runtime_dir=base,
            wayland_display="wayland-test",
            work_dir=base,
            publish_path=base / "latest.jpg",
            run=run,
            log=log or (lambda message: None),
        )

    def test_crop_filter(self) -> None:
        self.assertEqual(crop_filter(1280, 720, 32), "1280x688+0+32")

    def test_newest_png_rejects_stale_files(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            stale = base / "old.png"
            stale.write_bytes(b"old")
            old = time.time_ns() - 10_000_000_000
            os.utime(stale, ns=(old, old))
            self.assertIsNone(newest_png(base, time.time_ns()))

    def test_atomic_replace(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            tmp = base / "latest.jpg.tmp"
            dest = base / "latest.jpg"
            tmp.write_bytes(b"JPEG")
            atomic_replace(tmp, dest)
            self.assertEqual(dest.read_bytes(), b"JPEG")
            self.assertFalse(tmp.exists())

    def test_frame_directory_failure_is_counted(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            work_file = base / "not-a-directory"
            work_file.write_bytes(b"x")
            run = FakeRun()
            capture = self.make_capture(work_file, run)
            self.assertFalse(capture.capture_once())
            self.assertEqual(capture.status()["errors"], 1)
            self.assertEqual(run.calls, [])

    def test_loop_survives_capture_crash(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            capture = self.make_capture(
                base,
                FakeRun(),
                capture_type=ExplodingCapture,
            )
            capture.start()
            try:
                deadline = time.monotonic() + 2
                while time.monotonic() < deadline and capture.status()["errors"] < 1:
                    time.sleep(0.01)
                self.assertIsNotNone(capture._thread)
                self.assertTrue(capture._thread.is_alive())
                self.assertGreaterEqual(capture.status()["errors"], 1)
            finally:
                capture.stop()

    def test_start_twice_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            capture = self.make_capture(base, FakeRun())
            capture.start()
            try:
                with self.assertRaisesRegex(RuntimeError, "already started"):
                    capture.start()
            finally:
                capture.stop()

    def test_start_after_stop_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            capture = self.make_capture(base, FakeRun())
            capture.start()
            capture.stop()
            with self.assertRaisesRegex(RuntimeError, "already started"):
                capture.start()

    def test_concurrent_start_allows_only_one(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            capture = self.make_capture(
                base,
                FakeRun(),
                capture_type=BlockingCapture,
            )
            barrier = threading.Barrier(8)
            results: list[object] = [None] * 8

            def attempt(index: int) -> None:
                barrier.wait()
                try:
                    capture.start()
                except Exception as exc:
                    results[index] = exc

            threads = [threading.Thread(target=attempt, args=(index,)) for index in range(8)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(5)
            self.assertTrue(all(not thread.is_alive() for thread in threads))
            self.assertEqual(sum(result is None for result in results), 1)
            self.assertEqual(sum(isinstance(result, RuntimeError) for result in results), 7)
            capture.stop(timeout=2)

    def test_stop_before_start_prevents_start(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            capture = self.make_capture(base, FakeRun())
            capture.stop()
            self.assertIsNone(capture._thread)
            with self.assertRaisesRegex(RuntimeError, "already started"):
                capture.start()
            self.assertIsNone(capture._thread)

    def test_stop_joins_thread(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            capture = self.make_capture(base, FakeRun())
            capture.start()
            capture.stop(timeout=2)
            self.assertIsNotNone(capture._thread)
            self.assertFalse(capture._thread.is_alive())

    def test_screenshooter_failure_logs_stderr(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            logs: list[str] = []
            capture = self.make_capture(base, FakeRun("screen"), log=logs.append)
            self.assertFalse(capture.capture_once())
            self.assertTrue(any("screenshooter exited 1" in entry for entry in logs))
            self.assertTrue(any("screen failed" in entry for entry in logs))

    def test_magick_failure_logs_stderr(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            run = FakeRun()
            logs: list[str] = []
            capture = self.make_capture(base, run, log=logs.append)
            self.assertTrue(capture.capture_once())
            logs.clear()
            run.fail = "magick"
            self.assertFalse(capture.capture_once())
            self.assertTrue(any("magick exited 1" in entry for entry in logs))
            self.assertTrue(any("magick failed" in entry for entry in logs))

    def test_run_timeout_is_counted(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            capture = self.make_capture(base, TimeoutRun())
            self.assertFalse(capture.capture_once())
            self.assertEqual(capture.status()["errors"], 1)

    def test_capture_once_publishes_frame(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            run = FakeRun()
            capture = self.make_capture(base, run)
            self.assertTrue(capture.capture_once())
            self.assertEqual((base / "latest.jpg").read_bytes(), b"JPEG")
            self.assertEqual(capture.status()["errors"], 0)
            self.assertEqual(capture.status()["frames"], 1)
            magick_call = next(call for call in run.calls if call[0][0] == "magick")
            self.assertIn("1280x688+0+32", magick_call[0])
            self.assertTrue(magick_call[0][-1].startswith("jpg:"))
            self.assertEqual(list(base.glob("latest.jpg*.tmp")), [])

    def test_failed_frame_keeps_previous_frame(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            run = FakeRun()
            capture = self.make_capture(base, run)
            self.assertTrue(capture.capture_once())
            before = (base / "latest.jpg").read_bytes()
            run.fail = "screen"
            self.assertFalse(capture.capture_once())
            self.assertEqual((base / "latest.jpg").read_bytes(), before)
            self.assertEqual(capture.status()["errors"], 1)

    def test_failed_encode_keeps_previous_frame(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            run = FakeRun()
            capture = self.make_capture(base, run)
            self.assertTrue(capture.capture_once())
            before = (base / "latest.jpg").read_bytes()
            run.fail = "magick"
            self.assertFalse(capture.capture_once())
            self.assertEqual((base / "latest.jpg").read_bytes(), before)
            self.assertEqual(capture.status()["errors"], 1)
            self.assertEqual(list(base.glob("latest.jpg*.tmp")), [])

    def test_tmp_cleanup_oserror_does_not_escape(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            capture = self.make_capture(base, DirectoryOutputRun())
            self.assertFalse(capture.capture_once())
            self.assertEqual(capture.status()["errors"], 1)


class SmokeHttpTests(unittest.TestCase):
    def test_smoke_http_serves_state_and_frame(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            latest = Path(raw) / "latest.jpg"
            latest.write_bytes(b"JPEGDATA")
            cfg = load_config(None, env={})
            self.assertTrue(_smoke_http(cfg, latest))
            self.assertFalse(
                any(
                    thread.name == "vrbot-smoke-http" and thread.is_alive()
                    for thread in threading.enumerate()
                )
            )

    def test_smoke_http_missing_frame_fails(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            cfg = load_config(None, env={})
            self.assertFalse(_smoke_http(cfg, Path(raw) / "latest.jpg"))

    def test_smoke_http_bind_failure_returns_false(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            latest = Path(raw) / "latest.jpg"
            latest.write_bytes(b"JPEGDATA")
            cfg = load_config(None, env={})
            with mock.patch(
                "vrbot.dashboard.DashboardServer",
                side_effect=OSError("bind failed"),
            ):
                self.assertFalse(_smoke_http(cfg, latest))
            self.assertFalse(
                any(
                    thread.name == "vrbot-smoke-http" and thread.is_alive()
                    for thread in threading.enumerate()
                )
            )


if __name__ == "__main__":
    unittest.main()
