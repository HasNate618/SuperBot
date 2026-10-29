from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class CliTests(unittest.TestCase):
    def run_cli(self, *args: str, timeout: float = 10) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "superbot", *args],
            cwd=Path(__file__).resolve().parent.parent,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
        )

    def test_help(self) -> None:
        result = self.run_cli("--help")
        self.assertEqual(result.returncode, 0)
        self.assertIn("--preview-smoke", result.stdout)

    def test_invalid_overrides_exit_2(self) -> None:
        cases = (("--port", "-1"), ("--port", "70000"), ("--host", ""))
        for args in cases:
            with self.subTest(args=args):
                result = self.run_cli(*args)
                self.assertEqual(result.returncode, 2)
                self.assertNotIn("Traceback", result.stderr)
                self.assertIn("superbot:", result.stderr)

    def test_invalid_overrides_fail_before_preview_smoke(self) -> None:
        cases = (("--port", "-1"), ("--port", "70000"), ("--host", ""))
        for args in cases:
            with self.subTest(args=args):
                result = self.run_cli(*args, "--preview-smoke")
                self.assertEqual(result.returncode, 2)
                self.assertNotIn("Traceback", result.stderr)
                self.assertIn("superbot:", result.stderr)

    def test_launch_bind_failure_exits_2(self) -> None:
        result = self.run_cli("--host", "192.0.2.1", "--port", "0")
        self.assertEqual(result.returncode, 2)
        self.assertNotIn("Traceback", result.stderr)
        self.assertIn("superbot:", result.stderr)

    def test_invalid_utf8_config_exits_2(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "config.toml"
            path.write_bytes(b"[preview]\nfps = 6 # \xff\n")
            result = self.run_cli("--config", str(path))
            self.assertEqual(result.returncode, 2)
            self.assertNotIn("Traceback", result.stderr)
            self.assertIn("superbot:", result.stderr)


if __name__ == "__main__":
    unittest.main()
