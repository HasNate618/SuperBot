from __future__ import annotations

import codecs
import contextlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from .config import Config, missing_requirements
from .manual_input import ManualInputBridge
from .processes import Aborted, ProcessHandle, ProcessRunner


MODES = ("hold", "yaw", "trigger", "dodge", "walk", "clock", "manual", "body")


class State(StrEnum):
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
    _MAX_BUFFER = 1024 * 1024

    def __init__(self) -> None:
        self._active: dict[str, str | None] = {}
        self._pending: str | None = None
        self._buffer = ""

    def feed(self, text: str) -> str | None:
        self._buffer += text
        lines = self._buffer.split("\n")
        self._buffer = lines.pop()
        if len(self._buffer) > self._MAX_BUFFER:
            self._buffer = self._buffer[-self._MAX_BUFFER:]
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
        "proxy": "proxy.log",
        "steam": "steam.log",
        "pose": "pose.log",
        "session": "session.log",
    }
    INTERNAL_LOG_FILES = {**LOG_FILES, "weston-stdout": "weston-stdout.log"}

    def __init__(
        self,
        cfg: Config,
        runner: ProcessRunner | None = None,
    ) -> None:
        self.cfg = cfg
        self.runtime_dir = Path(os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}")
        session_root = Path(cfg.paths.session_root)
        self.runner = runner or ProcessRunner(session_root.parent / "current-session.json")
        self._lock = threading.RLock()
        self._condition = threading.Condition(self._lock)
        self._lifecycle_lock = threading.Lock()
        self._state = State.STOPPED
        self._session_id: str | None = None
        self._session_dir: Path | None = None
        self._started_at: datetime | None = None
        self._last_error: str | None = None
        self._survivors: list[str] = []
        self._mode = "hold"
        self._handles: dict[str, ProcessHandle] = {}
        self._game_pids: list[int] = []
        self._game_audio_muted: bool = False
        self._display: str | None = None
        self._socket_name: str | None = None
        self._pointer: dict = {}
        self._stop_event = threading.Event()
        self._idle = threading.Event()
        self._idle.set()
        self._start_generation = 0
        self._thread: threading.Thread | None = None
        self._monitor_thread: threading.Thread | None = None
        self._startup_deadline: float | None = None
        self._openxr_lock = threading.Lock()
        self._openxr_parser = _OpenXRLogParser()
        self._openxr_decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self._openxr_offset = 0
        self._openxr_inode: int | None = None
        self._openxr_path: Path | None = None
        self._openxr_head = b""
        self._openxr_mark = b""
        self._manual_input = ManualInputBridge()

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

    def start(self) -> None:
        with self._lock:
            if self._state not in (State.STOPPED, State.FAILED):
                raise ConflictError("session is not stopped")
            self._start_generation += 1
            generation = self._start_generation
            self._reset_openxr()
            self._session_id = None
            self._session_dir = None
            self._started_at = None
            self._display = None
            self._socket_name = None
            self._state = State.STARTING
            self._last_error = None
            self._survivors = []
            self._handles = {}
            self._game_pids = []
            self._stop_event.clear()
            self._idle.clear()
            self._manual_input.clear()
        try:
            session_dir = self._preflight(generation)
        except Exception as exc:
            with self._lifecycle_lock:
                with self._lock:
                    if generation != self._start_generation:
                        stale = True
                    else:
                        stale = False
                        stopped = self._stop_event.is_set() or self._state != State.STARTING
                        session_dir = self._session_dir
                        if stopped:
                            self._last_error = None
                            self._set_terminal_state(State.STOPPED)
                        else:
                            self._last_error = str(exc)
                if stale:
                    if isinstance(exc, ConflictError):
                        raise
                    return
                if not stopped:
                    self._cleanup_safe()
                    with self._lock:
                        if generation != self._start_generation:
                            current = False
                        else:
                            current = True
                            stopped = self._stop_event.is_set() or self._state != State.STARTING
                            if stopped:
                                self._last_error = None
                                self._set_terminal_state(State.STOPPED)
                            else:
                                self._set_terminal_state(State.FAILED)
                    if not current:
                        return
                    if not stopped:
                        self._discard_attempt(generation, session_dir)
                        if isinstance(exc, ConflictError):
                            raise
                        if isinstance(exc, PreflightError):
                            raise
                        raise PreflightError(str(exc)) from exc
                self._discard_attempt(generation, session_dir)
                if isinstance(exc, ConflictError):
                    raise
                return
        with self._lock:
            current = generation == self._start_generation
        if not current:
            self._discard_attempt(generation, session_dir)
            return
        self._thread = threading.Thread(
            target=self._start_worker,
            args=(generation, session_dir),
            name="superbot-start",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        with self._lock:
            if self._state == State.STOPPED:
                return
            self._manual_input.clear()
            if self._state == State.FAILED:
                session_dir = self._session_dir
                try:
                    self.runner.remove_pointer()
                except Exception as exc:
                    self._attempt_log(session_dir, f"pointer removal failed: {exc}")
                self._pointer = {}
                self._last_error = None
                self._set_terminal_state(State.STOPPED)
                return
            if self._state == State.STOPPING:
                return
            self._stop_event.set()
            self._idle.clear()
            self._state = State.STOPPING
            self._condition.notify_all()
            self._thread = threading.Thread(target=self._stop_worker, name="superbot-stop", daemon=True)
        self._thread.start()

    def set_mode(self, mode: str) -> None:
        if mode not in MODES:
            raise ValueError(f"unknown mode {mode!r}")
        with self._lifecycle_lock:
            with self._lock:
                if self._state != State.RUNNING:
                    raise ConflictError("session is not running")
            self._manual_input.clear()
            self._stop_handle("pose", 2)
            try:
                self._start_pose(mode)
            except Exception as exc:
                self._last_error = str(exc)
                self._cleanup_safe()
                self._set_terminal_state(State.FAILED)
                raise
            with self._lock:
                self._mode = mode

    def manual_input(self, payload: dict) -> bool:
        with self._lock:
            if self._state != State.RUNNING:
                raise ConflictError("session is not running")
            if self._mode != "manual":
                raise ConflictError("manual mode is not active")
        return self._manual_input.update(payload)

    def game_audio_muted(self) -> bool:
        with self._lock:
            return self._game_audio_muted

    def set_game_audio_muted(self, muted: bool) -> bool:
        muted = bool(muted)
        with self._lock:
            self._game_audio_muted = muted
        self._apply_game_audio_mute()
        return muted

    def mute_game_audio(self, muted: bool) -> bool:
        return self.set_game_audio_muted(muted)

    def toggle_game_audio_mute(self) -> bool:
        with self._lock:
            next_muted = not self._game_audio_muted
        return self.set_game_audio_muted(next_muted)

    def _game_audio_node_ids(self) -> list[int]:
        pids = {str(pid) for pid in (self._game_pids or self.runner.pids_by_argv0(self._game_argv0()))}
        if not pids:
            return []
        try:
            completed = subprocess.run(
                ["pw-dump"], capture_output=True, text=True, timeout=5
            )
            data = json.loads(completed.stdout or "[]")
        except Exception:
            return []
        client_ids: set[str] = set()
        for obj in data:
            if obj.get("type") != "PipeWire:Interface:Client":
                continue
            props = ((obj.get("info") or {}).get("props") or {})
            pid = props.get("application.process.id")
            if pid is not None and str(pid) in pids:
                client_ids.add(str(obj.get("id")))
        nodes: list[int] = []
        for obj in data:
            if obj.get("type") != "PipeWire:Interface:Node":
                continue
            props = ((obj.get("info") or {}).get("props") or {})
            if str(props.get("client.id")) not in client_ids:
                continue
            if props.get("media.class") != "Stream/Output/Audio":
                continue
            try:
                nodes.append(int(obj.get("id")))
            except (TypeError, ValueError):
                continue
        return nodes

    def _apply_game_audio_mute(self) -> None:
        muted = self.game_audio_muted()
        value = "1" if muted else "0"
        for node_id in self._game_audio_node_ids():
            with contextlib.suppress(Exception):
                subprocess.run(
                    ["wpctl", "set-mute", str(node_id), value],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )

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
            with contextlib.suppress(Exception):
                self.runner.run(
                    [self.cfg.paths.steam_bin, "-shutdown"],
                    env=self._steam_env(),
                    cwd=Path.home(),
                    timeout=20,
                )
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
            survivors = list(self._survivors)
            handles = dict(self._handles)
            display = self._display
            session_dir = self._session_dir
        game_pids = self.runner.pids_by_argv0(self._game_argv0()) if state in ("running", "stopping") else []
        processes: dict[str, dict] = {}
        for name in ("monado", "weston", "proxy", "steam", "pose"):
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
        weston_handle = handles.get("weston")
        proxy_handle = handles.get("proxy")
        preview_status = {
            "mode": "vnc",
            "width": self.cfg.preview.width,
            "height": self.cfg.preview.height - self.cfg.preview.panel_height,
            "fps_target": self.cfg.preview.fps,
            "vnc_port": self.cfg.preview.vnc_port,
            "proxy_port": self.cfg.preview.proxy_port,
            "vnc_alive": bool(weston_handle and weston_handle.alive()),
            "proxy_alive": bool(proxy_handle and proxy_handle.alive()),
        }
        logs = {
            name: str(self.log_path(name)) if self.log_path(name) is not None else None
            for name in ("session", "monado", "weston", "proxy", "steam", "pose")
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
            "survivors": survivors,
            "logs": logs,
            "session_dir": str(session_dir) if session_dir is not None else None,
            "player_log": str(player_log),
            "manual": self._manual_input.snapshot(),
            "audio": {
                "muted": self.game_audio_muted(),
                "available": bool(game_pids),
                "target": "game",
            },
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
        self._openxr_head = b""
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
            if not reset and self._openxr_offset > 0:
                head = self._openxr_head
                mark = self._openxr_mark
                try:
                    with path.open("rb") as handle:
                        if head:
                            handle.seek(0)
                            if handle.read(len(head)) != head:
                                reset = True
                        if not reset and mark:
                            handle.seek(self._openxr_offset - len(mark))
                            if handle.read(len(mark)) != mark:
                                reset = True
                except OSError:
                    return None
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
                        start = self._openxr_offset
                        text = self._openxr_decoder.decode(chunk)
                        self._openxr_parser.feed(text)
                        self._openxr_offset += len(chunk)
                        if len(self._openxr_head) < 64 and start == len(self._openxr_head):
                            self._openxr_head = (self._openxr_head + chunk)[:64]
                        self._openxr_mark = (self._openxr_mark + chunk)[-64:]
            except OSError:
                return None
            return self._openxr_parser.value()

    def _set_terminal_state(self, state: State) -> None:
        with self._lock:
            self._state = state
            self._condition.notify_all()
            self._idle.set()

    def _abort_if_stopped(self) -> None:
        if self._stop_event.is_set():
            raise Aborted("session stop requested")
        if self._startup_deadline is not None and time.monotonic() > self._startup_deadline:
            raise StartupError("startup timeout exceeded")

    def _startup_timeout(self, configured: float) -> float:
        if self._startup_deadline is None:
            return configured
        remaining = self._startup_deadline - time.monotonic()
        return max(0.0, min(configured, remaining))

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
        filename = self.INTERNAL_LOG_FILES.get(name)
        if filename is None or self._session_dir is None:
            raise StartupError("no active session directory")
        return self._session_dir / filename

    def _session_log(self, message: str) -> None:
        if self._session_dir is None:
            return
        path = self._session_dir / "session.log"
        timestamp = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        with path.open("a") as handle:
            handle.write(f"{timestamp} {message}\n")

    def _attempt_log(self, session_dir: Path | None, message: str) -> None:
        if session_dir is None:
            return
        timestamp = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        try:
            with (session_dir / "session.log").open("a") as handle:
                handle.write(f"{timestamp} {message}\n")
        except OSError:
            pass

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

    def _attempt_active(self, generation: int) -> bool:
        return (
            generation == self._start_generation
            and self._state == State.STARTING
            and not self._stop_event.is_set()
        )

    def _require_attempt_active(self, generation: int) -> None:
        if not self._attempt_active(generation):
            raise ConflictError("start attempt superseded")

    def _preflight(self, generation: int) -> Path | None:
        with self._lock:
            if not self._attempt_active(generation):
                return None

        pointer = self.runner.read_pointer()
        stale: list[str] = []
        if pointer:
            for name, record in pointer.get("processes", {}).items():
                pid = int(record.get("pid") or 0)
                if pid and self.runner.pid_alive(pid):
                    args = self.runner.pid_cmdline(pid)
                    command = str(record.get("command", ""))
                    if args and any(
                        self._matches_command(arg, command) for arg in args
                    ):
                        stale.append(f"{name} pid {pid}")
            if stale:
                raise PreflightError(
                    "stale session processes: " + ", ".join(stale) + "; stop them before starting"
                )
            with self._lock:
                self._require_attempt_active(generation)
                self.runner.remove_pointer()

        if self.runner.process_name_running("steam"):
            raise PreflightError("Close the host Steam client before starting a hidden C0 session.")
        # monado.socket is wired into sockets.target from /etc/systemd/user, so it is
        # listening again after every reboot and owns the IPC path this session's own
        # monado-service has to bind. systemctl --user disable cannot stick against a
        # NixOS-generated unit, so reclaim it instead of asking the operator to. These
        # units exist only for this project on this machine. Service first: it has
        # Requires=monado.socket.
        for unit in ("monado.service", "monado.socket"):
            if self.runner.systemd_unit_active(unit):
                print(f"superbot session: stopping user unit {unit}", file=sys.stderr, flush=True)
                self.runner.stop_systemd_unit(unit)
                if self.runner.systemd_unit_active(unit):
                    raise PreflightError(f"user unit {unit} is still active after stopping it")
        if self.runner.port_in_use("127.0.0.1", 4242):
            # Not an unconditional refusal: a previous session's monado-service can
            # outlive its teardown, get reparented to init and match neither unit
            # above, while it keeps 4242 and makes every start refuse. Reclaim it
            # when it is ours; refuse when it is somebody else's.
            print(
                "superbot session: TCP 4242 is in use; checking whether it is our monado",
                file=sys.stderr,
                flush=True,
            )
            try:
                reclaimed = self.runner.reclaim_port(4242, expect="monado")
            except Exception as exc:  # noqa: BLE001 - preflight turns it into a message
                raise PreflightError(str(exc)) from exc
            if reclaimed:
                print(
                    "superbot session: stopped leftover monado "
                    + ", ".join(str(pid) for pid in reclaimed),
                    file=sys.stderr,
                    flush=True,
                )
            if self.runner.port_in_use("127.0.0.1", 4242):
                raise PreflightError("TCP 4242 is already in use")
        with self._lock:
            self._require_attempt_active(generation)
            self.runner.remove_stale_socket(self.runtime_dir / "monado_comp_ipc")

        missing = missing_requirements(self.cfg)
        if missing:
            raise PreflightError("missing requirements: " + ", ".join(missing))

        with self._lock:
            self._require_attempt_active(generation)
            stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
            session_id = f"{stamp}-{os.urandom(3).hex()}"
            session_dir = Path(self.cfg.paths.session_root) / session_id
            self._session_id = session_id
            self._session_dir = session_dir
            created = False
            try:
                session_dir.mkdir(parents=True, exist_ok=False)
                created = True
                self._pointer = {"session": str(session_dir), "processes": {}}
                self.runner.write_pointer(self._pointer)
            except Exception:
                if self._session_dir == session_dir:
                    try:
                        self.runner.remove_pointer()
                    except Exception as exc:
                        self._attempt_log(
                            session_dir, f"pointer removal failed: {exc}"
                        )
                    self._pointer = {}
                    self._session_id = None
                    self._session_dir = None
                    self._started_at = None
                    self._socket_name = None
                if created:
                    try:
                        shutil.rmtree(session_dir)
                    except OSError as exc:
                        self._attempt_log(
                            session_dir, f"session directory removal failed: {exc}"
                        )
                raise
            return self._session_dir

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
            self._startup_timeout(self.cfg.timeouts.monado_startup_sec),
            self._stop_event,
        )

    def _start_weston(self) -> None:
        if self._session_id is None or self._session_dir is None:
            raise StartupError("no active session")
        self._socket_name = f"superbot-{self._session_id.split('-')[-1]}"
        weston_log = self._log("weston")
        weston_ini = self._session_dir / "weston.ini"
        weston_ini.write_text(
            "[core]\n"
            "idle-time=0\n"
            "\n"
            f"[vnc]\nrefresh-rate={int(self.cfg.preview.fps)}\n",
            encoding="utf-8",
        )
        vnc_port = self.cfg.preview.vnc_port
        if self.runner.port_in_use("127.0.0.1", vnc_port):
            raise StartupError(f"VNC port {vnc_port} is already in use")
        argv = [
            self.cfg.paths.weston_bin,
            "--backend=vnc",
            "--renderer=gl",
            f"--width={self.cfg.preview.width}",
            f"--height={self.cfg.preview.height}",
            f"--socket={self._socket_name}",
            f"--config={weston_ini}",
            "--xwayland",
            "--debug",
            f"--log={weston_log}",
            "--address=127.0.0.1",
            f"--port={vnc_port}",
            "--disable-transport-layer-security",
        ]
        handle = self._start("weston", argv, self._base_env(), Path.home(), self._log("weston-stdout"))
        self._wait_weston_display(handle, weston_log)
        self.runner.wait_port(
            "127.0.0.1",
            vnc_port,
            self._startup_timeout(self.cfg.timeouts.weston_startup_sec),
            self._stop_event,
        )

    def _wait_weston_display(self, handle: ProcessHandle, log_path: Path) -> None:
        deadline = time.monotonic() + self._startup_timeout(self.cfg.timeouts.weston_startup_sec)
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

    def _start_proxy(self) -> None:
        proxy_port = self.cfg.preview.proxy_port
        vnc_port = self.cfg.preview.vnc_port
        if self.runner.port_in_use("127.0.0.1", proxy_port):
            raise StartupError(f"websockify port {proxy_port} is already in use")
        argv = [
            self.cfg.paths.websockify_bin,
            f"127.0.0.1:{proxy_port}",
            f"127.0.0.1:{vnc_port}",
        ]
        self._start("proxy", argv, self._base_env(), Path.home(), self._log("proxy"))
        self.runner.wait_port(
            "127.0.0.1",
            proxy_port,
            self._startup_timeout(self.cfg.timeouts.weston_startup_sec),
            self._stop_event,
        )

    def _start_pose(self, mode: str) -> None:
        if self._session_dir is None:
            raise StartupError("no active session directory")
        pose_log = self._log("pose")
        pose_log.unlink(missing_ok=True)
        self._manual_input.clear()
        if mode == "manual":
            manual = Path(self.cfg.paths.pose_client).with_name("c0_manual.py")
            if not manual.exists():
                raise StartupError(f"manual pose client not found: {manual}")
            input_socket = self.runtime_dir / f"{self._socket_name}-manual.sock"
            self._manual_input.configure(input_socket)
            argv = [
                sys.executable,
                str(manual),
                "--input-socket",
                str(input_socket),
            ]
            needle = "connected 127.0.0.1"
        elif mode == "body":
            # The join: the pose client stops inventing poses and relays the ones
            # the simulated body actually achieved (see superbot/pose_relay.py).
            relay = Path(__file__).with_name("pose_relay.py")
            state = Path(self.cfg.paths.session_root) / "body" / "body_state.json"
            argv = [
                sys.executable,
                str(relay),
                "--state",
                str(state),
                "--duration",
                "3600",
                "--log",
                str(self._session_dir / "body_pose.csv"),
            ]
            needle = "handshake ok"
        else:
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
            needle = "handshake ok"
        self._start("pose", argv, self._base_env(), Path.cwd(), pose_log)
        self.runner.wait_log(
            pose_log,
            needle,
            self._startup_timeout(self.cfg.timeouts.pose_handshake_sec),
            self._stop_event,
        )

    def _start_steam(self) -> None:
        argv = [self.cfg.paths.steam_bin, *self.cfg.launch.steam_extra_args]
        self._start("steam", argv, self._steam_env(), Path.home(), self._log("steam"))
        self._wait_steam_ready()

    def _wait_steam_ready(self) -> None:
        handle = self._handles["steam"]
        started = time.monotonic()
        deadline = started + self._startup_timeout(self.cfg.launch.steam_ready_timeout_sec)
        failures = 0
        while time.monotonic() < deadline:
            self._abort_if_stopped()
            if not handle.alive():
                raise StartupError("Steam exited during startup")
            graph = self.runner.debug_scene_graph(
                self.cfg.paths.weston_debug_bin,
                self._steam_env(),
                timeout=self._startup_timeout(5.0),
            )
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
            self._sleep_abort(self._startup_timeout(remaining))
        if not handle.alive():
            raise StartupError("Steam exited during startup")

    def _launch_game(self) -> None:
        result = self.runner.run(
            [self.cfg.paths.steam_bin, f"steam://rungameid/{self.cfg.launch.shortcut_id}"],
            env=self._steam_env(),
            cwd=Path.home(),
            timeout=self._startup_timeout(15),
        )
        if result.returncode != 0:
            raise StartupError(f"steam rungameid failed: {result.stderr.strip()}")
        pids = self.runner.wait_pids(
            self._game_argv0(),
            self._startup_timeout(self.cfg.launch.game_process_timeout_sec),
            self._stop_event,
        )
        self._game_pids = pids
        self._apply_game_audio_mute()
        self._pointer.setdefault("processes", {})["game"] = {
            "pid": pids[0],
            "command": self._game_argv0(),
        }
        self.runner.write_pointer(self._pointer)
        self.runner.wait_log(
            self._log("monado"),
            "application_name: 'Unity Application'",
            self._startup_timeout(self.cfg.launch.openxr_client_timeout_sec),
            self._stop_event,
        )

    def _step(self, name: str, log_name: str, action: Callable[[], None], fallback_log_name: str | None = None) -> None:
        try:
            self._abort_if_stopped()
            action()
            self._abort_if_stopped()
        except Aborted:
            raise
        except Exception as exc:
            tail = self._step_tail(log_name)
            tail_log = log_name
            if not tail and fallback_log_name is not None:
                tail = self._step_tail(fallback_log_name)
                tail_log = fallback_log_name
            message = f"{name} failed: {exc}"
            if tail:
                message += f"\n--- {tail_log} tail ---\n{tail}"
            raise StartupError(message) from exc

    def _step_tail(self, name: str) -> str:
        try:
            path = self._log(name)
            return self.runner.tail(path, 200)
        except Exception:
            return ""

    def _startup(self) -> None:
        self._step("monado", "monado", self._start_monado)
        self._step("weston", "weston", self._start_weston, "weston-stdout")
        self._step("proxy", "proxy", self._start_proxy)
        self._step("pose", "pose", lambda: self._start_pose(self._mode))
        self._step("steam", "steam", self._start_steam)
        self._step("game", "monado", self._launch_game)

    def _discard_attempt(self, generation: int, session_dir: Path | None) -> None:
        with self._lock:
            current_dir = self._session_dir
            if generation != self._start_generation:
                owned = False
            else:
                owned = session_dir is not None and current_dir == session_dir
                try:
                    self.runner.remove_pointer()
                except Exception as exc:
                    self._attempt_log(session_dir, f"pointer removal failed: {exc}")
                self._pointer = {}
                if owned:
                    self._session_id = None
                    self._session_dir = None
                    self._started_at = None
                    self._socket_name = None
        if session_dir is not None and (owned or session_dir != current_dir):
            try:
                shutil.rmtree(session_dir)
            except OSError as exc:
                self._attempt_log(
                    session_dir, f"session directory removal failed: {exc}"
                )

    def _start_worker(self, generation: int, session_dir: Path | None) -> None:
        with self._lifecycle_lock:
            with self._lock:
                if generation != self._start_generation:
                    stale = True
                    stopped = False
                else:
                    stale = False
                    stopped = self._state != State.STARTING
            if stale:
                self._discard_attempt(generation, session_dir)
                return
            if stopped:
                self._discard_attempt(generation, session_dir)
                with self._lock:
                    if generation == self._start_generation:
                        self._set_terminal_state(State.STOPPED)
                return
            self._startup_deadline = time.monotonic() + self.cfg.launch.startup_timeout_sec
            try:
                self._startup()
            except Aborted:
                self._cleanup_safe()
                with self._lock:
                    if generation == self._start_generation:
                        self._last_error = None
                        self._set_terminal_state(State.STOPPED)
            except Exception as exc:
                with self._lock:
                    if generation == self._start_generation:
                        self._last_error = str(exc)
                with contextlib.suppress(Exception):
                    self._session_log(f"startup failed: {exc}")
                self._cleanup_safe()
                with self._lock:
                    if generation == self._start_generation:
                        stopped = self._stop_event.is_set() or self._state != State.STARTING
                        if stopped:
                            self._last_error = None
                            self._set_terminal_state(State.STOPPED)
                        else:
                            self._set_terminal_state(State.FAILED)
            else:
                with self._lock:
                    current = generation == self._start_generation
                    stopped = self._stop_event.is_set() or self._state != State.STARTING
                    if current and not stopped:
                        self._started_at = datetime.now(UTC)
                        self._state = State.RUNNING
                        self._condition.notify_all()
                if current and stopped:
                    self._cleanup_safe()
                    with self._lock:
                        if generation == self._start_generation:
                            self._last_error = None
                            self._set_terminal_state(State.STOPPED)
                elif current:
                    self._monitor_thread = threading.Thread(
                        target=self._monitor_worker,
                        args=(generation,),
                        name="superbot-monitor",
                        daemon=True,
                    )
                    self._monitor_thread.start()
            finally:
                self._startup_deadline = None
                with self._lock:
                    if (
                        generation == self._start_generation
                        and self._state not in (State.STARTING, State.STOPPING)
                    ):
                        self._idle.set()

    def _monitor_worker(self, generation: int) -> None:
        while not self._stop_event.wait(1.0):
            with self._lock:
                if generation != self._start_generation or self._state != State.RUNNING:
                    return
                monado = self._handles.get("monado")
                weston = self._handles.get("weston")
                proxy = self._handles.get("proxy")
            dead = None
            if monado is None or not monado.alive():
                dead = "monado"
            elif weston is None or not weston.alive():
                dead = "weston"
            elif proxy is None or not proxy.alive():
                dead = "proxy"
            if dead is None:
                continue
            with self._lifecycle_lock:
                with self._lock:
                    if generation != self._start_generation or self._state != State.RUNNING:
                        return
                    self._state = State.STOPPING
                    self._last_error = f"{dead} exited while running"
                    self._idle.clear()
                self._cleanup_safe()
                with self._lock:
                    if generation == self._start_generation:
                        self._set_terminal_state(State.FAILED)
                return

    def _stop_worker(self) -> None:
        with self._lifecycle_lock:
            with self._lock:
                if self._state != State.STOPPING:
                    return
            try:
                self._cleanup_safe()
            finally:
                with self._lock:
                    self._set_terminal_state(State.STOPPED)

    def _cleanup_safe(self) -> None:
        try:
            self._cleanup()
        except Exception as exc:
            with contextlib.suppress(Exception):
                self._session_log(f"cleanup failed: {exc}")
            with self._lock:
                if self._last_error is None:
                    self._last_error = f"cleanup failed: {exc}"

    def _stop_handle(self, name: str, timeout: float):
        handle = self._handles.pop(name, None)
        if handle is not None:
            handle.stop(timeout)
        return handle

    def _cleanup(self) -> None:
        self._manual_input.clear()
        failures: list[str] = []
        try:
            deadline = time.monotonic() + self.cfg.timeouts.stop_sec

            def budget(cap: float) -> float:
                return max(0.0, min(cap, deadline - time.monotonic()))

            def phase(name: str, action: Callable[[], None]) -> None:
                try:
                    action()
                except Exception as exc:
                    failures.append(f"{name}: {exc}")
                    with contextlib.suppress(Exception):
                        self._session_log(f"cleanup phase {name} failed: {exc}")

            stopped: dict[str, ProcessHandle] = {}

            def stop_handle(name: str, timeout: float) -> None:
                handle = self._handles.pop(name, None)
                if handle is None:
                    return
                try:
                    handle.stop(timeout)
                finally:
                    stopped[name] = handle

            def shutdown_steam() -> None:
                if "steam" in self._handles:
                    self.runner.run(
                        [self.cfg.paths.steam_bin, "-shutdown"],
                        env=self._steam_env(),
                        cwd=Path.home(),
                        timeout=budget(20.0),
                    )

            def kill_game() -> None:
                for pid in list(self._game_pids):
                    self.runner.kill_pid(pid)
                self._game_pids = []
                for pid in self.runner.pids_by_argv0(self._game_argv0()):
                    self.runner.kill_pid(pid)

            def remove_socket() -> None:
                monado = stopped.get("monado")
                if monado is None or not monado.alive():
                    self.runner.remove_stale_socket(self.runtime_dir / "monado_comp_ipc")

            phase("pose", lambda: stop_handle("pose", budget(2)))
            phase("steam-shutdown", shutdown_steam)
            phase("steam", lambda: stop_handle("steam", budget(5)))
            phase("game", kill_game)
            phase("proxy", lambda: stop_handle("proxy", budget(5)))
            phase("weston", lambda: stop_handle("weston", budget(5)))
            phase("monado", lambda: stop_handle("monado", budget(5)))
            phase("monado-socket", remove_socket)

            survivors: list[str] = []

            def probe_game() -> None:
                if self.runner.pids_by_argv0(self._game_argv0()):
                    survivors.append("game")

            def probe_handles() -> None:
                for name in ("pose", "steam", "proxy", "weston", "monado"):
                    handle = stopped.get(name)
                    if handle is None:
                        continue
                    try:
                        if handle.alive() or handle.group_alive():
                            survivors.append(name)
                    except Exception as exc:
                        failures.append(f"survivor {name}: {exc}")
                        with contextlib.suppress(Exception):
                            self._session_log(f"cleanup survivor {name} probe failed: {exc}")

            def report_survivors() -> None:
                with self._lock:
                    self._survivors = survivors
                if survivors:
                    self._session_log("warning: surviving processes: " + ", ".join(survivors))

            phase("survivor-game", probe_game)
            phase("survivor-handles", probe_handles)
            phase("survivor-report", report_survivors)
            if failures:
                raise RuntimeError("; ".join(failures))
        finally:
            try:
                self.runner.remove_pointer()
            except Exception as exc:
                failures.append(f"pointer removal: {exc}")
                with contextlib.suppress(Exception):
                    self._session_log(f"pointer removal failed: {exc}")
            self._pointer = {}
            with self._lock:
                self._handles.clear()
            self._game_pids = []
            self._display = None
            with contextlib.suppress(Exception):
                if failures:
                    self._session_log(f"cleanup finished (with {len(failures)} errors)")
                else:
                    self._session_log("cleanup complete")
