from __future__ import annotations

import argparse
import contextlib
import http.client
import json
import os
import signal
import sys
import time
import urllib.request
from dataclasses import replace
from pathlib import Path

from .config import Config, ConfigError, load_config, validate_config

#: Keys that must all be present for an /api/state response to count as ours.
STATE_KEYS = ("state", "session_id", "mode")

#: How long the preflight probe and the shutdown wait each get.
PROBE_TIMEOUT_SEC = 3.0
STOP_TIMEOUT_SEC = 10.0


def probe_dashboard(host: str, port: int, timeout: float = PROBE_TIMEOUT_SEC) -> dict | None:
    """Return the parsed /api/state when it answers as a clank dashboard.

    Anything else - refused, timeout, garbage, a foreign server - is None.
    """
    # validate_config already rejects wildcard hosts, so host is always dialable here.
    try:
        with urllib.request.urlopen(f"http://{host}:{port}/api/state", timeout=timeout) as response:
            doc = json.loads(response.read().decode("utf-8"))
    except (OSError, ValueError, http.client.HTTPException):
        return None
    if isinstance(doc, dict) and all(key in doc for key in STATE_KEYS):
        return doc
    return None


def read_own_pid(pidfile: Path) -> int | None:
    """The pid in the pidfile, when it is a live process running clank.

    Anything else - missing file, garbage, a dead pid, a foreign process, or
    ourselves - is None. Callers must not kill what this does not return.
    """
    try:
        pid = int(pidfile.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None
    if pid <= 0 or pid == os.getpid():
        return None
    try:
        cmdline = (
            Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", "replace")
        )
    except OSError:
        return None
    if "clank" not in cmdline:
        return None
    return pid


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def stop_pid(pid: int, timeout: float = STOP_TIMEOUT_SEC) -> bool:
    """SIGTERM a pid, escalating to SIGKILL. True when it is gone."""
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not pid_alive(pid):
            return True
        time.sleep(0.2)
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        if not pid_alive(pid):
            return True
        time.sleep(0.2)
    return False


def replace_dashboard(cfg: Config) -> str | None:
    """Stop whatever of ours answers on cfg's port. None when the port is free."""
    from .dashboard import dashboard_pidfile

    pidfile = dashboard_pidfile(cfg)
    pid = read_own_pid(pidfile)
    if pid is None:
        try:
            raw = pidfile.read_text(encoding="utf-8").strip()
        except OSError:
            raw = ""
        if raw:
            try:
                maybe = int(raw)
            except ValueError:
                maybe = -1
            if maybe > 0 and pid_alive(maybe):
                return (
                    f"dashboard pid file holds {raw!r}, which is not a running "
                    "clank dashboard; refusing to kill it"
                )
        with contextlib.suppress(OSError):
            pidfile.unlink()
        if probe_dashboard(cfg.dashboard.host, cfg.dashboard.port) is not None:
            return "dashboard already running but its pid is unknown; stop it manually"
        return None
    if not stop_pid(pid):
        return f"cannot stop dashboard pid {pid} (permission denied)"
    with contextlib.suppress(OSError):
        pidfile.unlink()
    deadline = time.monotonic() + STOP_TIMEOUT_SEC
    while time.monotonic() < deadline:
        if probe_dashboard(cfg.dashboard.host, cfg.dashboard.port) is None:
            return None
        time.sleep(0.3)
    return f"dashboard pid {pid} stopped but the port is still busy"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="clank")
    parser.add_argument("--config", default="config.toml", help="TOML config path")
    parser.add_argument("--host", help="override dashboard host")
    parser.add_argument("--port", type=int, help="override dashboard port")
    parser.add_argument("--preview-smoke", action="store_true", help="run the Weston/mpv preview smoke test")
    parser.add_argument(
        "--replace",
        action="store_true",
        help="stop a running clank dashboard on this port and start a new one",
    )
    args = parser.parse_args(argv)

    try:
        cfg = load_config(Path(args.config), env=os.environ)
    except (ConfigError, OSError) as exc:
        print(f"clank: {exc}", file=sys.stderr)
        return 2

    if args.host is not None or args.port is not None:
        cfg = replace(
            cfg,
            dashboard=replace(
                cfg.dashboard,
                host=args.host if args.host is not None else cfg.dashboard.host,
                port=args.port if args.port is not None else cfg.dashboard.port,
            ),
        )

    try:
        validate_config(cfg)
    except ConfigError as exc:
        print(f"clank: {exc}", file=sys.stderr)
        return 2

    host, port = cfg.dashboard.host, cfg.dashboard.port
    existing = probe_dashboard(host, port)
    if existing is not None and not args.replace:
        print(
            f"clank: dashboard already running at http://{host}:{port} "
            f"(session {existing.get('state')}); use --replace to restart it"
        )
        return 0
    if existing is not None:
        problem = replace_dashboard(cfg)
        if problem is not None:
            print(f"clank: {problem}", file=sys.stderr)
            return 2

    try:
        if args.preview_smoke:
            from .preview import preview_smoke

            return preview_smoke(cfg)

        from .dashboard import run_dashboard

        run_dashboard(cfg)
    except OSError as exc:
        print(f"clank: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
