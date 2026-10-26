from __future__ import annotations

import argparse
import os
import sys
from dataclasses import replace
from pathlib import Path

from .config import ConfigError, load_config, validate_config


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="vrbot")
    parser.add_argument("--config", default="config.toml", help="TOML config path")
    parser.add_argument("--host", help="override dashboard host")
    parser.add_argument("--port", type=int, help="override dashboard port")
    parser.add_argument("--preview-smoke", action="store_true", help="run the Weston/mpv preview smoke test")
    args = parser.parse_args(argv)

    try:
        cfg = load_config(Path(args.config), env=os.environ)
    except (ConfigError, OSError) as exc:
        print(f"vrbot: {exc}", file=sys.stderr)
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
        print(f"vrbot: {exc}", file=sys.stderr)
        return 2

    try:
        if args.preview_smoke:
            from .preview import preview_smoke

            return preview_smoke(cfg)

        from .dashboard import run_dashboard

        run_dashboard(cfg)
    except OSError as exc:
        print(f"vrbot: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
