"""
Helpers for launching the local Tauri viewer from the claude-act CLI.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path


class ViewerLaunchError(RuntimeError):
    pass


def viewer_launch_env() -> tuple[dict[str, str], str | None]:
    env = os.environ.copy()

    is_linux = sys.platform.startswith("linux")
    wayland = bool(env.get("WAYLAND_DISPLAY")) or env.get("XDG_SESSION_TYPE") == "wayland"
    if not (is_linux and wayland):
        return env, None

    note = None
    if not env.get("WINIT_UNIX_BACKEND"):
        env["WINIT_UNIX_BACKEND"] = "x11"
        note = "claude-act viewer: Wayland detected, launching Tauri via X11 compatibility."
    if not env.get("GDK_BACKEND"):
        env["GDK_BACKEND"] = "x11"
    if not env.get("WEBKIT_DISABLE_DMABUF_RENDERER"):
        env["WEBKIT_DISABLE_DMABUF_RENDERER"] = "1"

    return env, note


def find_viewer_root(start: Path | None = None) -> Path | None:
    search_roots: list[Path] = []
    if start is not None:
        search_roots.extend([start, *start.parents])

    cwd = Path.cwd().resolve()
    search_roots.extend([cwd, *cwd.parents])

    module_root = Path(__file__).resolve()
    search_roots.extend([module_root.parent, *module_root.parents])

    seen: set[Path] = set()
    for root in search_roots:
        root = root.resolve()
        if root in seen:
            continue
        seen.add(root)
        candidate = root / "tauri-workbench-base"
        if (candidate / "package.json").exists():
            return candidate
    return None


def launch_viewer(*, start: Path | None = None) -> int:
    viewer_root = find_viewer_root(start)
    if viewer_root is None:
        raise ViewerLaunchError(
            "Could not find tauri-workbench-base next to this repo. "
            "Run this command from the claude-act workspace."
        )

    npm = shutil.which("npm")
    if not npm:
        raise ViewerLaunchError("npm is required to launch the desktop viewer.")

    cargo = shutil.which("cargo")
    if not cargo:
        raise ViewerLaunchError("cargo is required to launch the desktop viewer.")

    node_modules = viewer_root / "node_modules"
    if not node_modules.exists():
        install = subprocess.run([npm, "install"], cwd=viewer_root, check=False)
        if install.returncode != 0:
            raise ViewerLaunchError(
                f"npm install failed in {viewer_root} with exit code {install.returncode}."
            )

    env, note = viewer_launch_env()
    if note:
        print(note, file=sys.stderr)

    result = subprocess.run(
        [npm, "run", "tauri", "dev"],
        cwd=viewer_root,
        check=False,
        env=env,
    )
    return int(result.returncode)
