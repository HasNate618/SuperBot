from __future__ import annotations

import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from vrbot.preview import PreviewCapture, atomic_replace, crop_filter, newest_png


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
            if self.fail == "magick":
                return subprocess.CompletedProcess(argv, 1, "", "magick failed")
            out = Path(argv[-1][4:])
            out.write_bytes(b"JPEG")
            return subprocess.CompletedProcess(argv, 0, "", "")
        raise AssertionError(f"unexpected command: {argv}")


class PreviewTests(unittest.TestCase):
    def make_capture(self, base: Path, run) -> PreviewCapture:
        return PreviewCapture(
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
            log=lambda message: None,
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


if __name__ == "__main__":
    unittest.main()
