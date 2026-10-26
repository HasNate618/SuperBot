from __future__ import annotations

import json
import os
import signal
import socket
import stat
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Iterator


class Aborted(RuntimeError):
    """Raised when a wait is interrupted by a session stop request."""


class ProcessError(RuntimeError):
    """Raised for process lifecycle failures."""


@dataclass
class ProcessHandle:
    name: str
    pid: int
    pgid: int
    log_path: Path
    _popen: subprocess.Popen | None = None
    _handles: tuple[IO[bytes], ...] = ()

    def alive(self) -> bool:
        if self._popen is not None:
            return self._popen.poll() is None
        try:
            os.kill(self.pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    def _group_alive(self) -> bool:
        if self.pgid <= 0:
            return False
        try:
            os.killpg(self.pgid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    def group_alive(self) -> bool:
        return self._group_alive()

    def signal_group(self, sig: int) -> None:
        if self.pgid <= 0:
            return
        try:
            os.killpg(self.pgid, sig)
        except (ProcessLookupError, PermissionError):
            pass

    def stop(self, timeout: float = 5.0) -> None:
        grace = 0.5
        deadline = time.monotonic() + max(0.0, timeout)
        self.signal_group(signal.SIGTERM)
        while self._group_alive() and time.monotonic() < deadline:
            if self._popen is not None:
                self._popen.poll()
            time.sleep(0.05)
        if self._group_alive():
            self.signal_group(signal.SIGKILL)
            kill_deadline = time.monotonic() + grace
            while self._group_alive() and time.monotonic() < kill_deadline:
                if self._popen is not None:
                    self._popen.poll()
                time.sleep(0.05)
        if self._popen is not None:
            remaining = max(0.0, deadline - time.monotonic()) + grace
            try:
                self._popen.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                pass
        self._close()

    def _close(self) -> None:
        for handle in self._handles:
            try:
                handle.close()
            except OSError:
                pass
        self._handles = ()


def tail_stream(handle: IO[bytes], lines: int = 200, block_size: int = 8192) -> str:
    if lines <= 0:
        return ""
    text = ""
    for _ in range(3):
        try:
            initial = os.fstat(handle.fileno())
            size = initial.st_size
            if size == 0:
                text = ""
            else:
                handle.seek(size - 1)
                trailing_newline = handle.read(1) == b"\n"
                target = lines + 1 if trailing_newline else lines
                position = size
                newlines = 0
                chunks: list[bytes] = []
                while position > 0 and newlines < target:
                    read_size = min(block_size, position)
                    position -= read_size
                    handle.seek(position)
                    chunk = handle.read(read_size)
                    if not chunk and position > 0:
                        break
                    chunks.append(chunk)
                    newlines += chunk.count(b"\n")
                data = b"".join(reversed(chunks))
                search_end = len(data)
                remaining = target
                start = 0
                while remaining > 0:
                    index = data.rfind(b"\n", 0, search_end)
                    if index < 0:
                        start = 0
                        break
                    search_end = index
                    remaining -= 1
                    if remaining == 0:
                        start = index + 1
                text = data[start:].decode("utf-8", errors="replace")
                text = text.replace("\r\n", "\n").replace("\r", "\n")
                if text.endswith("\n"):
                    text = text[:-1]
                if text:
                    text = "\n".join(text.split("\n")[-lines:])
            final = os.fstat(handle.fileno())
        except OSError:
            return ""
        if final.st_ino == initial.st_ino and final.st_size == initial.st_size:
            return text
    return text


def tail_text(path: Path, lines: int = 200, block_size: int = 8192) -> str:
    if lines <= 0:
        return ""
    try:
        with open(path, "rb") as handle:
            return tail_stream(handle, lines, block_size)
    except OSError:
        return ""


class ProcessRunner:
    def __init__(self, pointer_path: Path, proc_root: Path = Path("/proc")) -> None:
        self.pointer_path = Path(pointer_path)
        self.proc_root = Path(proc_root)

    def start(
        self,
        name: str,
        argv: list[str] | tuple[str, ...],
        env: dict[str, str],
        cwd: Path,
        log_path: Path,
        stderr_path: Path | None = None,
    ) -> ProcessHandle:
        log_path = Path(log_path)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        out = log_path.open("ab", buffering=0)
        handles: list[IO[bytes]] = [out]
        err = out
        if stderr_path is not None:
            stderr_path = Path(stderr_path)
            stderr_path.parent.mkdir(parents=True, exist_ok=True)
            err = stderr_path.open("ab", buffering=0)
            handles.append(err)
        try:
            popen = subprocess.Popen(
                [str(item) for item in argv],
                stdin=subprocess.DEVNULL,
                stdout=out,
                stderr=err,
                cwd=str(cwd),
                env={str(key): str(value) for key, value in env.items()},
                start_new_session=True,
            )
        except Exception:
            for handle in handles:
                handle.close()
            raise
        return ProcessHandle(name, popen.pid, popen.pid, log_path, popen, tuple(handles))

    def run(
        self,
        argv: list[str] | tuple[str, ...],
        env: dict[str, str] | None = None,
        cwd: Path | None = None,
        timeout: float = 10.0,
        check: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(item) for item in argv],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
            cwd=str(cwd) if cwd is not None else None,
            timeout=timeout,
            check=check,
        )

    def process_name_running(self, name: str) -> bool:
        result = subprocess.run(
            ["pgrep", "-x", name],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
        if result.returncode == 0:
            return True
        if result.returncode == 1:
            return False
        raise ProcessError(f"pgrep failed with exit code {result.returncode}")

    def systemd_unit_active(self, unit: str) -> bool:
        result = subprocess.run(
            ["systemctl", "--user", "is-active", "--quiet", unit],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
        if result.returncode == 0:
            return True
        if result.returncode == 3:
            return False
        raise ProcessError(f"systemctl is-active failed with exit code {result.returncode}")

    def port_in_use(self, host: str, port: int) -> bool:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.5)
            return sock.connect_ex((host, port)) == 0

    def wait_port(self, host: str, port: int, timeout: float, abort=None) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if abort is not None and abort.is_set():
                raise Aborted("session stop requested")
            if self.port_in_use(host, port):
                return
            time.sleep(0.25)
        raise TimeoutError(f"timed out waiting for {host}:{port}")

    def wait_log(self, path: Path, needle: str, timeout: float, abort=None) -> None:
        path = Path(path)
        deadline = time.monotonic() + timeout
        offset = 0
        carry = ""
        carry_len = max(len(needle) - 1, 0)
        inode: int | None = None
        fingerprint = b""
        while time.monotonic() < deadline:
            if abort is not None and abort.is_set():
                raise Aborted("session stop requested")
            try:
                stat_result = path.stat()
            except OSError:
                time.sleep(0.25)
                continue
            if inode != stat_result.st_ino or stat_result.st_size < offset:
                inode = stat_result.st_ino
                offset = 0
                carry = ""
                fingerprint = b""
            if offset > 0 and fingerprint:
                probe_start = max(0, offset - 64)
                try:
                    with path.open("rb") as handle:
                        handle.seek(probe_start)
                        probe = handle.read(offset - probe_start)
                except OSError:
                    time.sleep(0.25)
                    continue
                if probe != fingerprint:
                    offset = 0
                    carry = ""
                    fingerprint = b""
            try:
                with path.open("rb") as handle:
                    handle.seek(offset)
                    chunk = handle.read()
                    offset = handle.tell()
            except OSError:
                time.sleep(0.25)
                continue
            fingerprint = (fingerprint + chunk)[-64:]
            text = chunk.decode(errors="replace")
            window = carry + text
            if needle in window:
                return
            carry = window[-carry_len:] if carry_len else ""
            time.sleep(0.25)
        raise TimeoutError(f"timed out waiting for {needle!r} in {path}")

    def wait_pids(self, argv0: str, timeout: float, abort=None) -> list[int]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if abort is not None and abort.is_set():
                raise Aborted("session stop requested")
            pids = self.pids_by_argv0(argv0)
            if pids:
                return pids
            time.sleep(0.5)
        raise TimeoutError(f"timed out waiting for process {argv0}")

    def scan_processes(self) -> Iterator[tuple[int, list[str]]]:
        try:
            entries = list(self.proc_root.iterdir())
        except OSError:
            return
        for entry in entries:
            if not entry.name.isdigit():
                continue
            try:
                raw = (entry / "cmdline").read_bytes()
            except OSError:
                continue
            args = [part.decode(errors="replace") for part in raw.split(b"\0") if part]
            if args:
                yield int(entry.name), args

    def pids_by_argv0(self, argv0: str) -> list[int]:
        expected = argv0.lower()
        pids: list[int] = []
        for pid, args in self.scan_processes():
            actual = args[0].lower()
            if actual == expected or actual.endswith(expected):
                pids.append(pid)
        return sorted(pids)

    def pid_alive(self, pid: int) -> bool:
        return (self.proc_root / str(pid)).exists()

    def pid_cmdline(self, pid: int) -> list[str]:
        try:
            raw = (self.proc_root / str(pid) / "cmdline").read_bytes()
        except OSError:
            return []
        return [part.decode(errors="replace") for part in raw.split(b"\0") if part]

    def kill_pid(self, pid: int) -> None:
        try:
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass

    def debug_scene_graph(self, debug_bin: str, env: dict[str, str], timeout: float = 5.0) -> str | None:
        try:
            result = self.run([debug_bin, "scene-graph"], env=env, timeout=timeout)
        except (OSError, subprocess.TimeoutExpired):
            return None
        if result.returncode != 0:
            return None
        return result.stdout

    def remove_stale_socket(self, path: Path) -> bool:
        path = Path(path)
        try:
            mode = path.lstat().st_mode
        except FileNotFoundError:
            return False
        if not stat.S_ISSOCK(mode):
            return False
        try:
            path.unlink()
        except FileNotFoundError:
            return False
        return True

    def read_pointer(self) -> dict | None:
        try:
            data = json.loads(self.pointer_path.read_text())
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict):
            return None
        return data

    def write_pointer(self, data: dict) -> None:
        self.pointer_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.pointer_path.with_suffix(self.pointer_path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True))
        os.replace(tmp, self.pointer_path)

    def remove_pointer(self) -> None:
        try:
            self.pointer_path.unlink()
        except FileNotFoundError:
            pass

    def tail(self, path: Path, lines: int = 200) -> str:
        return tail_text(path, lines)
