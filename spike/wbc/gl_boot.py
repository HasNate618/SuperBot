#!/usr/bin/env python3
"""Import-time GL bootstrap. Import this before mujoco and nothing else.

NixOS ships `libEGL_nvidia.so` and `libEGL_mesa.so` but no plain `libEGL.so`,
which is the name `ld.so` and PyOpenGL look for. ld.so only reads
`LD_LIBRARY_PATH` at process start, so this cannot be fixed after import - the
process has to re-exec itself once with the paths set.

Without it PyOpenGL binds a null EGL platform and dies with
`'NoneType' object has no attribute 'eglQueryString'`. Without *that*, MuJoCo
cannot create an offscreen context and the body view renders nothing.
All measured on this machine, not guessed.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

GL_DIRS = ("/run/opengl-driver/lib",)


def _glvnd_dirs() -> list[str]:
    found: list[str] = []
    for pattern in ("/nix/store/*libglvnd-1.7.0/lib", "/nix/store/*libglvnd-*/lib"):
        found.extend(str(p) for p in sorted(Path("/").glob(pattern.lstrip("/"))))
    return found


def ensure() -> None:
    """Re-exec once with a library path that can actually resolve libEGL."""
    if os.environ.get("CLANK_GL_READY") == "1":
        return
    # A re-exec is only safe when argv names a real file. With `python -c` or
    # `python - <<HEREDOC` the program comes from the command line or stdin,
    # and execve would restart the interpreter with nothing to read - it hangs
    # instead of failing loudly. Callers in that shape must pre-set the paths.
    if len(sys.argv) < 1 or not sys.argv[0].endswith(".py"):
        return
    candidates = [d for d in (*GL_DIRS, *_glvnd_dirs()) if d and Path(d).is_dir()]
    if not candidates:
        return
    current = os.environ.get("LD_LIBRARY_PATH", "")
    missing = [d for d in candidates if d not in current.split(":")]
    if not missing:
        return
    env = dict(os.environ)
    env["LD_LIBRARY_PATH"] = ":".join(missing + ([current] if current else []))
    env["CLANK_GL_READY"] = "1"
    # Deliberate re-exec, argv is forward exactly - no shell involved.
    os.execve(sys.executable, [sys.executable, *sys.argv], env)  # noqa: S606


ensure()
