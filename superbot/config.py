from __future__ import annotations

import math
import os
import shutil
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping


class ConfigError(ValueError):
    """Raised when config.toml or a SUPERBOT_ override is invalid."""


@dataclass(frozen=True)
class PathsConfig:
    monado_bin: str
    weston_bin: str
    weston_screenshooter_bin: str
    weston_debug_bin: str
    steam_bin: str
    novnc_bin: str
    websockify_bin: str
    novnc_web_root: str
    game_exe: str
    pose_client: str
    openxr_active_runtime: str
    session_root: str


@dataclass(frozen=True)
class LaunchConfig:
    shortcut_id: str
    proton_app_id: str
    steam_extra_args: tuple[str, ...]
    steam_settle_sec: float
    steam_ready_timeout_sec: float
    game_process_timeout_sec: float
    openxr_client_timeout_sec: float
    startup_timeout_sec: float


@dataclass(frozen=True)
class PreviewConfig:
    width: int
    height: int
    panel_height: int
    fps: float
    vnc_port: int
    proxy_port: int


@dataclass(frozen=True)
class DashboardConfig:
    host: str
    port: int


@dataclass(frozen=True)
class TimeoutsConfig:
    monado_startup_sec: float
    weston_startup_sec: float
    pose_handshake_sec: float
    stop_sec: float


@dataclass(frozen=True)
class Config:
    paths: PathsConfig
    launch: LaunchConfig
    preview: PreviewConfig
    dashboard: DashboardConfig
    timeouts: TimeoutsConfig


DEFAULTS: dict[str, dict[str, Any]] = {
    "paths": {
        "monado_bin": "/run/wrappers/bin/monado-service",
        "weston_bin": "weston",
        "weston_screenshooter_bin": "weston-screenshooter",
        "weston_debug_bin": "weston-debug",
        "steam_bin": os.path.expanduser("~/.local/bin/steam"),
        "novnc_bin": "novnc",
        "websockify_bin": "websockify",
        "novnc_web_root": "",
        "game_exe": "SUPERHOT_VR.exe",
        "pose_client": "spike/c0/c0_client.py",
        "openxr_active_runtime": os.path.expanduser("~/.config/openxr/1/active_runtime.json"),
        "session_root": os.path.expanduser("~/.local/state/SuperBot/sessions"),
    },
    "launch": {
        "shortcut_id": "18251483899710406656",
        "proton_app_id": "4249504744",
        "steam_extra_args": ("-cef-disable-gpu",),
        "steam_settle_sec": 60.0,
        "steam_ready_timeout_sec": 120.0,
        "game_process_timeout_sec": 60.0,
        "openxr_client_timeout_sec": 60.0,
        "startup_timeout_sec": 300.0,
    },
    "preview": {
        "width": 1280,
        "height": 720,
        "panel_height": 32,
        "fps": 30.0,
        "vnc_port": 5901,
        "proxy_port": 6080,
    },
    "dashboard": {
        "host": "127.0.0.1",
        "port": 8765,
    },
    "timeouts": {
        "monado_startup_sec": 20.0,
        "weston_startup_sec": 20.0,
        "pose_handshake_sec": 10.0,
        "stop_sec": 60.0,
    },
}


def _coerce(value: Any, default: Any, where: str) -> Any:
    if isinstance(default, bool):
        if not isinstance(value, bool):
            raise ConfigError(f"{where} must be a boolean")
        return value
    if isinstance(default, int):
        if isinstance(value, bool) or not isinstance(value, int):
            raise ConfigError(f"{where} must be an integer")
        return value
    if isinstance(default, float):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ConfigError(f"{where} must be a number")
        return float(value)
    if isinstance(default, str):
        if not isinstance(value, str):
            raise ConfigError(f"{where} must be a string")
        return value
    if isinstance(default, (list, tuple)):
        if not isinstance(value, (list, tuple)) or not all(isinstance(item, str) for item in value):
            raise ConfigError(f"{where} must be a list of strings")
        return tuple(value)
    raise ConfigError(f"unsupported default for {where}")


def _parse_env_value(section: str, key: str, raw: str, default: Any) -> Any:
    where = f"{section}.{key}"
    try:
        if isinstance(default, bool):
            lowered = raw.strip().lower()
            if lowered in {"1", "true", "yes", "on"}:
                return True
            if lowered in {"0", "false", "no", "off"}:
                return False
            raise ConfigError(f"{where} must be a boolean")
        if isinstance(default, int):
            return int(raw)
        if isinstance(default, float):
            return float(raw)
        if isinstance(default, (list, tuple)):
            return tuple(raw.split())
        return raw
    except ValueError as exc:
        raise ConfigError(f"invalid value for {where}: {raw!r}") from exc


def _validate(cfg: Config) -> None:
    if cfg.preview.width <= 0 or cfg.preview.height <= 0:
        raise ConfigError("preview width and height must be positive")
    if cfg.preview.panel_height < 0 or cfg.preview.panel_height >= cfg.preview.height:
        raise ConfigError("preview.panel_height must be >= 0 and less than preview.height")
    if cfg.preview.fps <= 0 or not math.isfinite(cfg.preview.fps):
        raise ConfigError("preview.fps must be positive")
    if cfg.preview.fps != int(cfg.preview.fps):
        raise ConfigError("preview.fps must be a whole number of Hz")
    for field_name in ("vnc_port", "proxy_port"):
        value = getattr(cfg.preview, field_name)
        if not 1 <= value <= 65535:
            raise ConfigError(f"preview.{field_name} must be between 1 and 65535")
    if cfg.preview.vnc_port == cfg.preview.proxy_port:
        raise ConfigError("preview.vnc_port and preview.proxy_port must differ")
    if cfg.dashboard.port != 0 and cfg.dashboard.port in {
        cfg.preview.vnc_port,
        cfg.preview.proxy_port,
    }:
        raise ConfigError("preview ports must not equal dashboard.port")
    host = cfg.dashboard.host.strip()
    if not host:
        raise ConfigError("dashboard.host must not be empty")
    if host in {"0.0.0.0", "::", "[::]"}:
        raise ConfigError("dashboard.host must be a specific address, not a wildcard")
    if not 0 <= cfg.dashboard.port <= 65535:
        raise ConfigError("dashboard.port must be between 0 and 65535")
    for field_name in (
        "steam_settle_sec",
        "steam_ready_timeout_sec",
        "game_process_timeout_sec",
        "openxr_client_timeout_sec",
        "startup_timeout_sec",
    ):
        value = getattr(cfg.launch, field_name)
        if value <= 0 or not math.isfinite(value):
            raise ConfigError(f"launch.{field_name} must be positive")
    for field_name in (
        "monado_startup_sec",
        "weston_startup_sec",
        "pose_handshake_sec",
        "stop_sec",
    ):
        value = getattr(cfg.timeouts, field_name)
        if value <= 0 or not math.isfinite(value):
            raise ConfigError(f"timeouts.{field_name} must be positive")


def validate_config(cfg: Config) -> None:
    _validate(cfg)


def load_config(
    path: Path | str | None = None,
    env: Mapping[str, str] | None = None,
    base_dir: Path | str | None = None,
) -> Config:
    base = Path.cwd()
    file_data: dict[str, Any] = {}
    candidate: Path | None = None
    if path is not None:
        candidate = Path(path)
        if not candidate.exists():
            raise ConfigError(f"config file not found: {candidate}")
        with candidate.open("rb") as handle:
            try:
                file_data = tomllib.load(handle)
            except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
                raise ConfigError(f"invalid TOML in {candidate}: {exc}") from exc

    if base_dir is not None:
        base = Path(base_dir)
    elif candidate is not None:
        base = candidate.parent

    unknown_sections = set(file_data) - set(DEFAULTS)
    if unknown_sections:
        raise ConfigError(f"unknown config sections: {', '.join(sorted(unknown_sections))}")

    merged = {section: dict(values) for section, values in DEFAULTS.items()}
    for section, values in file_data.items():
        if not isinstance(values, dict):
            raise ConfigError(f"{section} must be a table")
        unknown = set(values) - set(DEFAULTS[section])
        if unknown:
            raise ConfigError(f"unknown keys in {section}: {', '.join(sorted(unknown))}")
        for key, value in values.items():
            merged[section][key] = _coerce(value, DEFAULTS[section][key], f"{section}.{key}")

    for env_key, raw in (env or {}).items():
        if not env_key.startswith("SUPERBOT_"):
            continue
        rest = env_key[len("SUPERBOT_") :]
        if "_" not in rest:
            raise ConfigError(f"invalid environment override {env_key}")
        raw_section, raw_key = rest.split("_", 1)
        section = raw_section.lower()
        key = raw_key.lower()
        if section not in DEFAULTS or key not in DEFAULTS[section]:
            raise ConfigError(f"unknown environment override {env_key}")
        default = DEFAULTS[section][key]
        value = _parse_env_value(section, key, raw, default)
        merged[section][key] = _coerce(value, default, f"{section}.{key}")

    paths = merged["paths"]
    if os.sep in paths["weston_bin"]:
        parent = Path(paths["weston_bin"]).parent
        for key, sibling in (
            ("weston_screenshooter_bin", "weston-screenshooter"),
            ("weston_debug_bin", "weston-debug"),
        ):
            if paths[key] == DEFAULTS["paths"][key]:
                paths[key] = str(parent / sibling)

    for key in ("pose_client", "session_root"):
        candidate = Path(paths[key])
        if not candidate.is_absolute():
            paths[key] = str((base / candidate).resolve())

    if paths["novnc_web_root"]:
        candidate = Path(paths["novnc_web_root"])
        if not candidate.is_absolute():
            paths["novnc_web_root"] = str((base / candidate).resolve())

    # An explicit noVNC web root wins over the one derived from novnc_bin.
    if paths["novnc_web_root"] in ("", DEFAULTS["paths"]["novnc_web_root"]):
        resolved_novnc = _resolve_executable(paths["novnc_bin"])
        if resolved_novnc is not None:
            paths["novnc_web_root"] = str(
                Path(resolved_novnc).resolve().parent.parent / "share" / "webapps" / "novnc"
            )

    cfg = Config(
        paths=PathsConfig(**merged["paths"]),
        launch=LaunchConfig(**merged["launch"]),
        preview=PreviewConfig(**merged["preview"]),
        dashboard=DashboardConfig(**merged["dashboard"]),
        timeouts=TimeoutsConfig(**merged["timeouts"]),
    )
    _validate(cfg)
    return cfg


def _resolve_executable(value: str) -> Path | None:
    if os.sep in value:
        path = Path(value)
        return path if path.exists() else None
    found = shutil.which(value)
    return Path(found) if found else None


def missing_requirements(cfg: Config) -> list[str]:
    checks = [
        ("monado", cfg.paths.monado_bin, os.X_OK),
        ("weston", cfg.paths.weston_bin, os.X_OK),
        ("weston-debug", cfg.paths.weston_debug_bin, os.X_OK),
        ("steam", cfg.paths.steam_bin, os.X_OK),
        ("novnc", cfg.paths.novnc_bin, os.X_OK),
        ("websockify", cfg.paths.websockify_bin, os.X_OK),
        ("game", cfg.paths.game_exe, os.R_OK),
        ("openxr runtime", cfg.paths.openxr_active_runtime, os.R_OK),
        ("pose client", cfg.paths.pose_client, os.R_OK),
    ]
    missing: list[str] = []
    for label, value, mode in checks:
        resolved = _resolve_executable(value)
        if resolved is None or not os.access(resolved, mode):
            missing.append(f"{label} ({value})")
    web_root = cfg.paths.novnc_web_root
    rfb = Path(web_root) / "core" / "rfb.js" if web_root else None
    if rfb is None or not os.access(rfb, os.R_OK):
        missing.append(f"noVNC assets ({web_root or 'not found'})")
    return missing
