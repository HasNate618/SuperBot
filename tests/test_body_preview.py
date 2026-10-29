import tempfile
import unittest
from pathlib import Path

from superbot.body_preview import BodyPreview
from superbot.config import BodyConfig


def make_cfg(
    *,
    enabled: bool = True,
    python: str = "spike/e3/.venv/bin/python",
    script: str = "spike/wbc/body_view.py",
) -> BodyConfig:
    return BodyConfig(
        enabled=enabled,
        autostart=False,
        python=python,
        script=script,
        fps=10.0,
        width=560,
        height=420,
        reel=True,
    )


class BodyPreviewTestCase(unittest.TestCase):
    def test_argv_carries_output_and_size(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            view = BodyPreview(make_cfg(), out_dir=Path(tmp))
            argv = view.argv()
            self.assertIn("--out", argv)
            self.assertIn(str(Path(tmp)), argv)
            self.assertIn("--fps", argv)
            self.assertIn("--width", argv)

    def test_argv_asks_for_the_reel_when_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIn("--reel", BodyPreview(make_cfg(), out_dir=Path(tmp)).argv())

    def test_argv_omits_the_reel_when_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            view = BodyPreview(make_cfg(), out_dir=Path(tmp))
            view.cfg = BodyConfig(**{**view.cfg.__dict__, "reel": False})
            self.assertNotIn("--reel", view.argv())

    def test_preflight_reports_missing_python(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            view = BodyPreview(make_cfg(python="/nonexistent/python"), out_dir=Path(tmp))
            self.assertIn("python not found", view.preflight())

    def test_preflight_reports_missing_script(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            view = BodyPreview(make_cfg(script="/nonexistent/body_view.py"), out_dir=Path(tmp))
            self.assertIn("body view not found", view.preflight())

    def test_start_refuses_when_preflight_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            view = BodyPreview(make_cfg(python="/nonexistent/python"), out_dir=Path(tmp))
            problem = view.start()
            self.assertIn("python not found", problem)
            self.assertFalse(view.running())
            self.assertTrue(view.status()["error"])

    def test_status_before_any_frame(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            view = BodyPreview(make_cfg(), out_dir=Path(tmp))
            status = view.status()
            self.assertFalse(status["running"])
            self.assertIsNone(status["frame"])
            self.assertIsNone(status["frame_age_s"])

    def test_latest_finds_written_frame(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            view = BodyPreview(make_cfg(), out_dir=Path(tmp))
            self.assertIsNone(view.latest())
            (Path(tmp) / "body_latest.jpg").write_bytes(b"\xff\xd8\xff\xd9")
            found = view.latest()
            self.assertIsNotNone(found)
            if found is None:  # keeps the checker honest about the assertion above
                self.fail("latest() should have found the frame")
            self.assertEqual(found.name, "body_latest.jpg")
            self.assertIsNotNone(view.status()["frame_age_s"])

    def test_stop_is_idempotent_when_never_started(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            view = BodyPreview(make_cfg(), out_dir=Path(tmp))
            view.stop()
            view.stop()
            self.assertFalse(view.running())


if __name__ == "__main__":
    unittest.main()
