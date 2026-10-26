from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from vrbot.config import ConfigError, load_config, missing_requirements, validate_config


class ConfigTests(unittest.TestCase):
    def test_defaults(self) -> None:
        cfg = load_config(None, env={})
        self.assertEqual(cfg.preview.width, 1280)
        self.assertEqual(cfg.preview.height, 720)
        self.assertEqual(cfg.preview.panel_height, 32)
        self.assertEqual(cfg.preview.fps, 8.0)
        self.assertEqual(cfg.dashboard.port, 8765)
        self.assertEqual(cfg.launch.shortcut_id, "18251483899710406656")
        self.assertTrue(cfg.paths.pose_client.endswith("spike/c0/c0_client.py"))

    def test_steam_extra_args_default_is_tuple(self) -> None:
        cfg = load_config(None, env={})
        self.assertIsInstance(cfg.launch.steam_extra_args, tuple)
        self.assertEqual(cfg.launch.steam_extra_args, ("-cef-disable-gpu",))

    def test_toml_and_env_overrides(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "config.toml"
            path.write_text("[preview]\nfps = 6\n\n[dashboard]\nport = 9000\n")
            cfg = load_config(path, env={"VRBOT_PREVIEW_FPS": "12"})
            self.assertEqual(cfg.preview.fps, 12.0)
            self.assertEqual(cfg.dashboard.port, 9000)

    def test_unknown_toml_key_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "config.toml"
            path.write_text("[preview]\nnope = 1\n")
            with self.assertRaises(ConfigError):
                load_config(path, env={})

    def test_malformed_toml_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "config.toml"
            path.write_text("[preview\nfps = 6\n")
            with self.assertRaises(ConfigError):
                load_config(path, env={})

    def test_missing_explicit_config_path_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "missing.toml"
            with self.assertRaises(ConfigError):
                load_config(path, env={})

    def test_relative_paths_resolve_from_config_directory(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            path = base / "config.toml"
            path.write_text('[paths]\npose_client = "pose.py"\n')
            cfg = load_config(path, env={})
            self.assertEqual(cfg.paths.pose_client, str((base / "pose.py").resolve()))

    def test_explicit_base_dir_wins_for_relative_paths(self) -> None:
        with tempfile.TemporaryDirectory() as raw, tempfile.TemporaryDirectory() as other:
            path = Path(raw) / "config.toml"
            path.write_text('[paths]\npose_client = "pose.py"\n')
            cfg = load_config(path, env={}, base_dir=other)
            self.assertEqual(cfg.paths.pose_client, str((Path(other) / "pose.py").resolve()))

    def test_unknown_env_key_is_rejected(self) -> None:
        with self.assertRaises(ConfigError):
            load_config(None, env={"VRBOT_NOPE_X": "1"})

    def test_wrong_type_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "config.toml"
            path.write_text('[preview]\nfps = "fast"\n')
            with self.assertRaises(ConfigError):
                load_config(path, env={})

    def test_bounds_are_enforced(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "config.toml"
            path.write_text("[preview]\npanel_height = 720\n")
            with self.assertRaises(ConfigError):
                load_config(path, env={})

    def test_empty_dashboard_host_is_rejected(self) -> None:
        cfg = load_config(None, env={})
        for host in ("", "   "):
            with self.subTest(host=host):
                with self.assertRaises(ConfigError):
                    validate_config(
                        replace(cfg, dashboard=replace(cfg.dashboard, host=host))
                    )

    def test_wildcard_dashboard_host_is_rejected(self) -> None:
        cfg = load_config(None, env={})
        for host in ("0.0.0.0", "::", "[::]"):
            with self.subTest(host=host):
                with self.assertRaises(ConfigError):
                    validate_config(
                        replace(cfg, dashboard=replace(cfg.dashboard, host=host))
                    )

    def test_specific_dashboard_host_is_accepted(self) -> None:
        cfg = load_config(None, env={})
        for host in ("127.0.0.1", "192.168.1.10"):
            with self.subTest(host=host):
                validate_config(
                    replace(cfg, dashboard=replace(cfg.dashboard, host=host))
                )

    def test_non_finite_floats_are_rejected(self) -> None:
        float_env_keys = [
            "VRBOT_PREVIEW_FPS",
            "VRBOT_LAUNCH_STEAM_SETTLE_SEC",
            "VRBOT_LAUNCH_STEAM_READY_TIMEOUT_SEC",
            "VRBOT_LAUNCH_GAME_PROCESS_TIMEOUT_SEC",
            "VRBOT_LAUNCH_OPENXR_CLIENT_TIMEOUT_SEC",
            "VRBOT_LAUNCH_STARTUP_TIMEOUT_SEC",
            "VRBOT_TIMEOUTS_MONADO_STARTUP_SEC",
            "VRBOT_TIMEOUTS_WESTON_STARTUP_SEC",
            "VRBOT_TIMEOUTS_POSE_HANDSHAKE_SEC",
            "VRBOT_TIMEOUTS_STOP_SEC",
        ]
        for env_key in float_env_keys:
            for raw in ("nan", "inf", "-inf"):
                with self.subTest(env_key=env_key, raw=raw):
                    with self.assertRaises(ConfigError):
                        load_config(None, env={env_key: raw})

    def test_weston_siblings_are_derived(self) -> None:
        cfg = load_config(
            None,
            env={"VRBOT_PATHS_WESTON_BIN": "/opt/weston/bin/weston"},
        )
        self.assertEqual(cfg.paths.weston_screenshooter_bin, "/opt/weston/bin/weston-screenshooter")
        self.assertEqual(cfg.paths.weston_debug_bin, "/opt/weston/bin/weston-debug")

    def test_missing_requirements_are_reported(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            names = [
                "monado-service",
                "weston",
                "weston-screenshooter",
                "weston-debug",
                "magick",
                "steam",
                "game.exe",
                "pose.py",
                "runtime.json",
            ]
            for name in names:
                path = base / name
                path.write_text("")
                path.chmod(0o755)
            env = {
                "VRBOT_PATHS_MONADO_BIN": str(base / "monado-service"),
                "VRBOT_PATHS_WESTON_BIN": str(base / "weston"),
                "VRBOT_PATHS_MAGICK_BIN": str(base / "magick"),
                "VRBOT_PATHS_STEAM_BIN": str(base / "steam"),
                "VRBOT_PATHS_GAME_EXE": str(base / "game.exe"),
                "VRBOT_PATHS_POSE_CLIENT": str(base / "pose.py"),
                "VRBOT_PATHS_OPENXR_ACTIVE_RUNTIME": str(base / "runtime.json"),
                "VRBOT_PATHS_SESSION_ROOT": str(base / "sessions"),
            }
            cfg = load_config(None, env=env)
            self.assertEqual(missing_requirements(cfg), [])
            (base / "magick").unlink()
            self.assertTrue(any("magick" in item for item in missing_requirements(cfg)))


if __name__ == "__main__":
    unittest.main()
