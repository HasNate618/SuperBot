"""Third-person body view as a dashboard sidecar.

Deliberately **not** part of `SessionManager`. The body view is a MuJoCo process
that has nothing to do with Monado, Weston, Steam or the game: it can run with
no session at all, it must not delay a session start, and if it dies the session
must not notice. Keeping it out of the session state machine also keeps that
machine's tested process invariants intact.

What it owns: one child process, one output directory, and a health snapshot.
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import time
from pathlib import Path

from .config import BodyConfig


class BodyPreview:
    """Runs `body_view.py` and exposes the newest frame it wrote."""

    def __init__(self, cfg: BodyConfig, out_dir: Path, log_path: Path | None = None) -> None:
        self.cfg = cfg
        self.out_dir = Path(out_dir)
        self.log_path = Path(log_path) if log_path else self.out_dir / "body_view.log"
        self._proc: subprocess.Popen[bytes] | None = None
        self._error = ""

    # ---- lifecycle ------------------------------------------------------

    def argv(self) -> list[str]:
        return [
            self.cfg.python,
            self.cfg.script,
            "--out", str(self.out_dir),
            "--fps", f"{self.cfg.fps:g}",
            "--width", str(self.cfg.width),
            "--height", str(self.cfg.height),
        ] + (["--reel"] if self.cfg.reel else [])

    def preflight(self) -> str:
        """Empty string when the sidecar can start, else why it cannot."""
        if not Path(self.cfg.python).exists():
            return f"body python not found: {self.cfg.python}"
        if not Path(self.cfg.script).exists():
            return f"body view not found: {self.cfg.script}"
        return ""

    def start(self) -> str:
        if self._proc is not None and self._proc.poll() is None:
            return ""
        problem = self.preflight()
        if problem:
            self._error = problem
            return problem
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self._error = ""
        with self.log_path.open("ab") as log:
            self._proc = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
                self.argv(),
                stdout=log,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                start_new_session=True,  # own process group, so stop() is clean
                cwd=str(Path.cwd()),
            )
        return ""

    def stop(self) -> None:
        proc = self._proc
        self._proc = None
        if proc is None or proc.poll() is not None:
            return
        with contextlib.suppress(ProcessLookupError):
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            with contextlib.suppress(subprocess.TimeoutExpired):
                proc.wait(timeout=5)

    # ---- reads ----------------------------------------------------------

    def running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def latest(self) -> Path | None:
        path = self.out_dir / "body_latest.jpg"
        try:
            if path.is_file():
                return path
        except OSError:
            return None
        return None

    def status(self) -> dict:
        frame = self.latest()
        return {
            "enabled": self.cfg.enabled,
            "running": self.running(),
            "frame": str(frame) if frame else None,
            "frame_age_s": None
            if frame is None
            else round(time.time() - frame.stat().st_mtime, 2),
            "error": self._error,
            "log": str(self.log_path),
        }
