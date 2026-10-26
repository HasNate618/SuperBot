from __future__ import annotations

import codecs
import os
import re
import sys
import threading
import time
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Callable

from .config import Config, missing_requirements
from .preview import PreviewCapture
from .processes import Aborted, ProcessHandle, ProcessRunner


MODES = ("hold", "yaw", "trigger", "dodge", "walk", "clock")


class State(str, Enum):
    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    FAILED = "failed"


class ConflictError(RuntimeError):
    pass


class PreflightError(RuntimeError):
    pass


class StartupError(RuntimeError):
    pass


class _OpenXRLogParser:
    def __init__(self) -> None:
        self._active: dict[str, str | None] = {}
        self._pending: str | None = None
        self._buffer = ""

    def feed(self, text: str) -> str | None:
        self._buffer += text
        lines = self._buffer.split("\n")
        self._buffer = lines.pop()
        for line in lines:
            self.consume(line)
        return self.value()

    def flush(self) -> str | None:
        if self._buffer:
            line = self._buffer
            self._buffer = ""
            self.consume(line)
        return self.value()

    def value(self) -> str | None:
        parser = self
        if self._buffer:
            parser = self._clone()
            parser.consume(parser._buffer)
        for name in reversed(list(parser._active.values())):
            if name:
                return name
        return None

    def _clone(self) -> _OpenXRLogParser:
        clone = _OpenXRLogParser()
        clone._active = dict(self._active)
        clone._pending = self._pending
        clone._buffer = self._buffer
        return clone

    def consume(self, line: str) -> None:
        match = re.search(r"\[client_connected\]\s+Client\s+(\d+)\s+connected", line)
        if match:
            self._pending = match.group(1)
            return
        match = re.search(r"application_name:\s*'([^']*)'", line)
        if match and self._pending is not None:
            name = match.group(1).strip()
            self._active[self._pending] = name or None
            self._pending = None
            return
        match = re.search(r"\[client_disconnected\]\s+Client\s+(\d+)\s+disconnected", line)
        if match:
            self._active.pop(match.group(1), None)


def parse_openxr_client(text: str) -> str | None:
    parser = _OpenXRLogParser()
    for line in text.splitlines():
        parser.consume(line)
    return parser.value()


class SessionManager:
    LOG_FILES = {
        "monado": "monado.log",
        "weston": "weston.log",
        "weston-stdout": "weston-stdout.log",
        "steam": "steam.log",
        "pose": "pose.log",
        "session": "session.log",
    }

    def __init__(
        self,
        cfg: Config,
        runner: ProcessRunner | None = None,
        preview_factory: Callable[..., PreviewCapture] | None = None,
    ) -> None:
        self.cfg = cfg
        self.runtime_dir = Path(os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}")
        session_root = Path(cfg.paths.session_root)
        self.runner = runner or ProcessRunner(session_root.parent / "current-session.json")
        self.preview_factory = preview_factory or PreviewCapture
        self._lock = threading.RLock()
        self._condition = threading.Condition(self._lock)
        self._lifecycle_lock = threading.Lock()
        self._state = State.STOPPED
        self._session_id: str | None = None
        self._session_dir: Path | None = None
        self._started_at: datetime | None = None
        self._last_error: str | None = None
        self._mode = "hold"
        self._handles: dict[str, ProcessHandle] = {}
        self._game_pids: list[int] = []
        self._display: str | None = None
        self._socket_name: str | None = None
        self._preview: PreviewCapture | None = None
        self._pointer: dict = {}
        self._stop_event = threading.Event()
        self._idle = threading.Event()
        self._idle.set()
        self._thread: threading.Thread | None = None
        self._monitor_thread: threading.Thread | None = None
        self._startup_deadline: float | None = None
        self._openxr_lock = threading.Lock()
        self._openxr_parser = _OpenXRLogParser()
        self._openxr_decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self._openxr_offset = 0
        self._openxr_inode: int | None = None
        self._openxr_path: Path | None = None
        self._openxr_mark = b""

    def state_value(self) -> str:
        with self._lock:
            return self._state.value

    def wait_idle(self, timeout: float = 10.0) -> bool:
        return self._idle.wait(timeout)

    def wait_for_state(self, state: str, timeout: float = 10.0) -> bool:
        deadline = time.monotonic() + timeout
        with self._condition:
            while self._state.value != state:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(remaining)
        return True

    def log_path(self, name: str) -> Path | None:
        filename = self.LOG_FILES.get(name)
        if filename is None or self._session_dir is None:
            return None
        return self._session_dir / filename

    def preview_path(self) -> Path | None:
        if self._session_dir is None:
            return None
        return self._session_dir / "latest.jpg"

    def start(self) -> None:
        with self._lock:
            if self._state not in (State.STOPPED, State.FAILED):
                raise ConflictError("session is not stopped")
            self._reset_openxr()
            self._session_dir = None
            self._preview = None
            self._display = None
            self._state = State.STARTING
            self._last_error = None
            self._stop_event.clear()
            self._idle.clear()
        try:
            self._preflight()
        except Exception as exc:
            self._last_error = str(exc)
            self._cleanup_safe()
            self._set_state(State.FAILED)
            self._idle.set()
            if isinstance(exc, PreflightError):
                raise
            raise PreflightError(str(exc)) from exc
        self._thread = threading.Thread(target=self._start_worker, name="vrbot-start", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        with self._lock:
            if self._state == State.STOPPED:
                return
            if self._state == State.FAILED:
                self.runner.remove_pointer()
                self._pointer = {}
                self._set_state(State.STOPPED)
                return
            if self._state == State.STOPPING:
                return
            self._stop_event.set()
            self._idle.clear()
            self._state = State.STOPPING
            self._condition.notify_all()
            self._thread = threading.Thread(target=self._stop_worker, name="vrbot-stop", daemon=True)
        self._thread.start()

    def set_mode(self, mode: str) -> None:
        if mode not in MODES:
            raise ValueError(f"unknown mode {mode!r}")
        with self._lifecycle_lock:
            with self._lock:
                if self._state != State.RUNNING:
                    raise ConflictError("session is not running")
            self._stop_handle("pose", 2)
            try:
                self._start_pose(mode)
            except Exception as exc:
                self._last_error = str(exc)
                self._cleanup_safe()
                self._set_state(State.FAILED)
                raise
            with self._lock:
                self._mode = mode

    def kill(self, target: str) -> None:
        with self._lock:
            if self._state != State.RUNNING:
                raise ConflictError("session is not running")
        if target == "game":
            for pid in self.runner.pids_by_argv0(self._game_argv0()):
                self.runner.kill_pid(pid)
            self._game_pids = []
            return
        if target == "steam":
            try:
                self.runner.run(
                    [self.cfg.paths.steam_bin, "-shutdown"],
                    env=self._steam_env(),
                    cwd=Path.home(),
                    timeout=20,
                )
            except Exception:
                pass
            handle = self._handles.get("steam")
            if handle is not None:
                handle.stop(5)
            for pid in self.runner.pids_by_argv0(self._game_argv0()):
                self.runner.kill_pid(pid)
            self._game_pids = []
            return
        raise ValueError(f"unknown target {target!r}")

    def snapshot(self) -> dict:
        with self._lock:
            state = self._state.value
            session_id = self._session_id
            started_at = self._started_at
            mode = self._mode
            last_error = self._last_error
            handles = dict(self._handles)
            display = self._display
            preview = self._preview
            session_dir = self._session_dir
        game_pids = self.runner.pids_by_argv0(self._game_argv0()) if state in ("running", "stopping") else []
        processes: dict[str, dict] = {}
        for name in ("monado", "weston", "steam", "pose"):
            handle = handles.get(name)
            processes[name] = {
                "alive": bool(handle and handle.alive()),
                "pid": handle.pid if handle is not None else None,
            }
            if name == "weston":
                processes[name]["display"] = display
        processes["game"] = {
            "alive": bool(game_pids),
            "pid": game_pids[0] if game_pids else None,
            "exe": Path(self.cfg.paths.game_exe).name,
        }
        openxr_client = None
        if state in ("starting", "running", "stopping"):
            monado_log = self.log_path("monado")
            if monado_log is not None:
                openxr_client = self._openxr_client(monado_log)
        if preview is not None:
            preview_status = preview.status()
        else:
            preview_status = {
                "width": self.cfg.preview.width,
                "height": self.cfg.preview.height - self.cfg.preview.panel_height,
                "fps_target": self.cfg.preview.fps,
                "fps_actual": 0,
                "last_frame_at": None,
                "errors": 0,
                "frames": 0,
            }
        logs = {
            name: str(self.log_path(name)) if self.log_path(name) is not None else None
            for name in ("session", "monado", "weston", "steam", "pose")
        }
        player_log = (
            Path.home()
            / ".local/share/Steam/steamapps/compatdata"
            / self.cfg.launch.proton_app_id
            / "pfx/drive_c/users/steamuser/AppData/LocalLow/SUPERHOT_Team/SUPERHOT_VR/Player.log"
        )
        return {
            "state": state,
            "session_id": session_id,
            "started_at": started_at.isoformat().replace("+00:00", "Z") if started_at else None,
            "mode": mode,
            "processes": processes,
            "openxr_client": openxr_client,
            "preview": preview_status,
            "last_error": last_error,
            "logs": logs,
            "session_dir": str(session_dir) if session_dir is not None else None,
            "player_log": str(player_log),
        }

    def _reset_openxr(self) -> None:
        with self._openxr_lock:
            self._reset_openxr_locked()

    def _reset_openxr_locked(self) -> None:
        self._openxr_parser = _OpenXRLogParser()
        self._openxr_decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self._openxr_offset = 0
        self._openxr_inode = None
        self._openxr_path = None
        self._openxr_mark = b""

    def _openxr_client(self, path: Path) -> str | None:
        with self._openxr_lock:
            try:
                stat_result = path.stat()
            except OSError:
                return None
            reset = (
                path != self._openxr_path
                or stat_result.st_ino != self._openxr_inode
                or stat_result.st_size < self._openxr_offset
            )
            if not reset and self._openxr_offset > 0 and self._openxr_mark:
                mark = self._openxr_mark
                try:
                    with path.open("rb") as handle:
                        handle.seek(self._openxr_offset - len(mark))
                        probe = handle.read(len(mark))
                except OSError:
                    return None
                if probe != mark:
                    reset = True
            if reset:
                self._reset_openxr_locked()
                self._openxr_inode = stat_result.st_ino
                self._openxr_path = path
            try:
                with path.open("rb") as handle:
                    handle.seek(self._openxr_offset)
                    while True:
                        chunk = handle.read(64 * 1024)
                        if not chunk:
                            break
                        self._openxr_offset += len(chunk)
                        self._openxr_mark = (self._openxr_mark + chunk)[-64:]
                        self._openxr_parser.feed(self._openxr_decoder.decode(chunk))
            except OSError:
                return None
            return self._openxr_parser.value()

    def _set_state(self, state: State) -> None:
        with self._lock:
            self._state = state
            self._condition.notify_all()

    def _abort_if_stopped(self) -> None:
        if self._stop_event.is_set():
            raise Aborted("session stop requested")
        if self._startup_deadline is not None and time.monotonic() > self._startup_deadline:
            raise StartupError("startup timeout exceeded")

    def _sleep_abort(self, seconds: float) -> None:
        deadline = time.monotonic() + max(0.0, seconds)
        while True:
            self._abort_if_stopped()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            time.sleep(min(0.25, remaining))

    def _base_env(self) -> dict[str, str]:
        env = os.environ.copy()
        env["XDG_RUNTIME_DIR"] = str(self.runtime_dir)
        return env

    def _steam_env(self) -> dict[str, str]:
        env = self._base_env()
        if self._display:
            env["DISPLAY"] = self._display
        if self._socket_name:
            env["WAYLAND_DISPLAY"] = self._socket_name
        return env

    def _log(self, name: str) -> Path:
        path = self.log_path(name)
        if path is None:
            raise StartupError("no active session directory")
        return path

    def _session_log(self, message: str) -> None:
        if self._session_dir is None:
            return
        path = self._session_dir / "session.log"
        timestamp = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        with path.open("a") as handle:
            handle.write(f"{timestamp} {message}\n")

    def _game_argv0(self) -> str:
        linux_path = Path(self.cfg.paths.game_exe).as_posix()
        return ("Z:" + linux_path).replace("/", "\\")

    def _matches_command(self, actual: str, expected: str) -> bool:
        if not expected:
            return True
        return (
            actual == expected
            or os.path.basename(actual) == os.path.basename(expected)
            or actual.endswith(expected)
        )

    def _preflight(self) -> None:
        pointer = self.runner.read_pointer()
        stale: list[str] = []
        if pointer:
            for name, record in pointer.get("processes", {}).items():
                pid = int(record.get("pid") or 0)
                if pid and self.runner.pid_alive(pid):
                    args = self.runner.pid_cmdline(pid)
                    if args and self._matches_command(args[0], str(record.get("command", ""))):
                        stale.append(f"{name} pid {pid}")
            if stale:
                raise PreflightError(
                    "stale session processes: " + ", ".join(stale) + "; stop them before starting"
                )
            self.runner.remove_pointer()

        if self.runner.process_name_running("steam"):
            raise PreflightError("Close the host Steam client before starting a hidden C0 session.")
        for unit in ("monado.service", "monado.socket"):
            if self.runner.systemd_unit_active(unit):
                raise PreflightError(f"Stop the user unit {unit} before starting a hidden C0 session.")
        if self.runner.port_in_use("127.0.0.1", 4242):
            raise PreflightError("TCP 4242 is already in use")
        self.runner.remove_stale_socket(self.runtime_dir / "monado_comp_ipc")

        missing = missing_requirements(self.cfg)
        if missing:
            raise PreflightError("missing requirements: " + ", ".join(missing))

        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        self._session_id = f"{stamp}-{os.urandom(3).hex()}"
        self._session_dir = Path(self.cfg.paths.session_root) / self._session_id
        self._session_dir.mkdir(parents=True, exist_ok=False)
        self._pointer = {"session": str(self._session_dir), "processes": {}}
        self.runner.write_pointer(self._pointer)

    def _start(self, name: str, argv, env, cwd: Path, log_path: Path, stderr_path: Path | None = None):
        handle = self.runner.start(name, argv, env, cwd, log_path, stderr_path)
        with self._lock:
            self._handles[name] = handle
        self._pointer.setdefault("processes", {})[name] = {
            "pid": handle.pid,
            "pgid": handle.pgid,
            "command": str(argv[0]),
        }
        self.runner.write_pointer(self._pointer)
        return handle

    def _start_monado(self) -> None:
        env = self._base_env()
        env.update(
            {
                "XRT_COMPOSITOR_NULL": "1",
                "P_OVERRIDE_ACTIVE_CONFIG": "remote",
                "IPC_EXIT_ON_DISCONNECT": "off",
                "XRT_NO_STDIN": "1",
            }
        )
        self._start("monado", [self.cfg.paths.monado_bin], env, Path.home(), self._log("monado"))
        self.runner.wait_port(
            "127.0.0.1",
            4242,
            self.cfg.timeouts.monado_startup_sec,
            self._stop_event,
        )

    def _start_weston(self) -> None:
        self._socket_name = f"vrbot-{self._session_id.split('-')[-1]}"
        weston_log = self._log("weston")
        argv = [
            self.cfg.paths.weston_bin,
            "--backend=headless",
            "--renderer=gl",
            f"--width={self.cfg.preview.width}",
            f"--height={self.cfg.preview.height}",
            f"--socket={self._socket_name}",
            "--no-config",
            "--xwayland",
            "--debug",
            f"--log={weston_log}",
        ]
        handle = self._start("weston", argv, self._base_env(), Path.home(), self._log("weston-stdout"))
        self._wait_weston_display(handle, weston_log)

    def _wait_weston_display(self, handle: ProcessHandle, log_path: Path) -> None:
        deadline = time.monotonic() + self.cfg.timeouts.weston_startup_sec
        pattern = re.compile(r"xserver listening on display (:\d+)")
        while time.monotonic() < deadline:
            self._abort_if_stopped()
            if not handle.alive():
                raise StartupError("Weston exited during startup")
            try:
                text = log_path.read_text(errors="replace")
            except OSError:
                text = ""
            match = pattern.search(text)
            if match:
                self._display = match.group(1)
                return
            time.sleep(0.2)
        raise StartupError("Weston did not publish an Xwayland display")

    def _start_preview(self) -> None:
        self._preview = self.preview_factory(
            width=self.cfg.preview.width,
            height=self.cfg.preview.height,
            panel_height=self.cfg.preview.panel_height,
            fps=self.cfg.preview.fps,
            jpeg_quality=self.cfg.preview.jpeg_quality,
            screenshooter=self.cfg.paths.weston_screenshooter_bin,
            magick=self.cfg.paths.magick_bin,
            runtime_dir=self.runtime_dir,
            wayland_display=self._socket_name,
            work_dir=self._session_dir,
            publish_path=self._session_dir / "latest.jpg",
            stop_event=self._stop_event,
            log=self._session_log,
        )
        self._preview.start()

    def _start_pose(self, mode: str) -> None:
        pose_log = self._log("pose")
        pose_log.unlink(missing_ok=True)
        argv = [
            sys.executable,
            self.cfg.paths.pose_client,
            "--mode",
            mode,
            "--duration",
            "3600",
            "--log",
            str(self._session_dir / f"c0_{mode}.csv"),
        ]
        self._start("pose", argv, self._base_env(), Path.cwd(), pose_log)
        self.runner.wait_log(pose_log, "handshake ok", self.cfg.timeouts.pose_handshake_sec, self._stop_event)

    def _start_steam(self) -> None:
        argv = [self.cfg.paths.steam_bin, *self.cfg.launch.steam_extra_args]
        self._start("steam", argv, self._steam_env(), Path.home(), self._log("steam"))
        self._wait_steam_ready()

    def _wait_steam_ready(self) -> None:
        handle = self._handles["steam"]
        started = time.monotonic()
        deadline = started + self.cfg.launch.steam_ready_timeout_sec
        failures = 0
        while time.monotonic() < deadline:
            self._abort_if_stopped()
            if not handle.alive():
                raise StartupError("Steam exited during startup")
            graph = self.runner.debug_scene_graph(self.cfg.paths.weston_debug_bin, self._steam_env())
            if graph is None:
                failures += 1
                if failures >= 3:
                    break
            else:
                failures = 0
                if "steam" in graph.lower():
                    return
            self._sleep_abort(2)
        self._session_log("Steam window not found; continuing after settle period")
        remaining = self.cfg.launch.steam_settle_sec - (time.monotonic() - started)
        if remaining > 0:
            self._sleep_abort(remaining)
        if not handle.alive():
            raise StartupError("Steam exited during startup")

    def _launch_game(self) -> None:
        result = self.runner.run(
            [self.cfg.paths.steam_bin, f"steam://rungameid/{self.cfg.launch.shortcut_id}"],
            env=self._steam_env(),
            cwd=Path.home(),
            timeout=15,
        )
        if result.returncode != 0:
            raise StartupError(f"steam rungameid failed: {result.stderr.strip()}")
        pids = self.runner.wait_pids(
            self._game_argv0(),
            self.cfg.launch.game_process_timeout_sec,
            self._stop_event,
        )
        self._game_pids = pids
        self._pointer.setdefault("processes", {})["game"] = {
            "pid": pids[0],
            "command": self._game_argv0(),
        }
        self.runner.write_pointer(self._pointer)
        self.runner.wait_log(
            self._log("monado"),
            "application_name: 'Unity Application'",
            self.cfg.launch.openxr_client_timeout_sec,
            self._stop_event,
        )

    def _startup(self) -> None:
        self._abort_if_stopped()
        self._start_monado()
        self._abort_if_stopped()
        self._start_weston()
        self._abort_if_stopped()
        self._start_preview()
        self._abort_if_stopped()
        self._start_pose(self._mode)
        self._abort_if_stopped()
        self._start_steam()
        self._abort_if_stopped()
        self._launch_game()
        self._abort_if_stopped()

    def _start_worker(self) -> None:
        with self._lifecycle_lock:
            self._startup_deadline = time.monotonic() + self.cfg.launch.startup_timeout_sec
            try:
                with self._lock:
                    if self._state == State.STOPPED:
                        return
                self._startup()
            except Aborted:
                self._cleanup_safe()
                self._set_state(State.STOPPED)
            except Exception as exc:
                self._last_error = str(exc)
                self._session_log(f"startup failed: {exc}")
                self._cleanup_safe()
                with self._lock:
                    stopped = self._stop_event.is_set() or self._state != State.STARTING
                    if not stopped:
                        self._state = State.FAILED
                        self._condition.notify_all()
                if stopped:
                    self._set_state(State.STOPPED)
            else:
                with self._lock:
                    stopped = self._stop_event.is_set() or self._state != State.STARTING
                    if not stopped:
                        self._started_at = datetime.now(timezone.utc)
                        self._state = State.RUNNING
                        self._condition.notify_all()
                if stopped:
                    self._cleanup_safe()
                    self._set_state(State.STOPPED)
                else:
                    self._monitor_thread = threading.Thread(
                        target=self._monitor_worker, name="vrbot-monitor", daemon=True
                    )
                    self._monitor_thread.start()
            finally:
                self._startup_deadline = None
                self._idle.set()

    def _monitor_worker(self) -> None:
        while not self._stop_event.wait(1.0):
            with self._lock:
                if self._state != State.RUNNING:
                    return
                monado = self._handles.get("monado")
                weston = self._handles.get("weston")
            dead = None
            if monado is None or not monado.alive():
                dead = "monado"
            elif weston is None or not weston.alive():
                dead = "weston"
            if dead is None:
                continue
            with self._lock:
                if self._state != State.RUNNING:
                    return
                self._state = State.STOPPING
                self._last_error = f"{dead} exited while running"
                self._idle.clear()
            with self._lifecycle_lock:
                self._cleanup_safe()
            self._set_state(State.FAILED)
            self._idle.set()
            return

    def _stop_worker(self) -> None:
        with self._lifecycle_lock:
            with self._lock:
                if self._state != State.STOPPING:
                    return
            try:
                self._cleanup_safe()
            finally:
                self._set_state(State.STOPPED)
                self._idle.set()

    def _cleanup_safe(self) -> None:
        try:
            self._cleanup()
        except Exception as exc:
            self._last_error = f"cleanup failed: {exc}"

    def _stop_handle(self, name: str, timeout: float):
        handle = self._handles.pop(name, None)
        if handle is not None:
            handle.stop(timeout)
        return handle

    def _cleanup(self) -> None:
        deadline = time.monotonic() + self.cfg.timeouts.stop_sec

        def budget(cap: float) -> float:
            return max(0.1, min(cap, deadline - time.monotonic()))

        self._stop_handle("pose", budget(2))
        if "steam" in self._handles:
            try:
                self.runner.run(
                    [self.cfg.paths.steam_bin, "-shutdown"],
                    env=self._steam_env(),
                    cwd=Path.home(),
                    timeout=budget(20.0),
                )
            except Exception:
                pass
        self._stop_handle("steam", budget(5))
        for pid in list(self._game_pids):
            self.runner.kill_pid(pid)
        self._game_pids = []
        for pid in self.runner.pids_by_argv0(self._game_argv0()):
            self.runner.kill_pid(pid)
        self._stop_handle("weston", budget(5))
        monado = self._stop_handle("monado", budget(5))
        if monado is None or not monado.alive():
            self.runner.remove_stale_socket(self.runtime_dir / "monado_comp_ipc")
        if self._preview is not None:
            try:
                self._preview.stop(5)
            except Exception:
                pass
            self._preview = None
        self.runner.remove_pointer()
        self._pointer = {}
        self._handles.clear()
        self._display = None
        self._session_log("cleanup complete")
