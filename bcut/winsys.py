"""Keeping Byditors Cut running on a Windows PC: start with Windows, and don't let the PC fall asleep while it is
watching streamers. Everything here is a no-op on macOS and Linux.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from . import config

IS_WINDOWS = sys.platform == "win32"
LINK_NAME = "Byditors Cut.lnk"


def _startup_dir() -> Path:
    return Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"


def autostart_enabled() -> bool:
    return IS_WINDOWS and (_startup_dir() / LINK_NAME).exists()


def set_autostart(on: bool) -> bool:
    """Add or remove a Startup shortcut that runs start.bat minimised in the background."""
    if not IS_WINDOWS:
        raise RuntimeError("Start with Windows only works on Windows")
    link = _startup_dir() / LINK_NAME
    if not on:
        link.unlink(missing_ok=True)
        return False
    bat = config.ROOT / "start.bat"
    ps = (
        "$s=(New-Object -ComObject WScript.Shell).CreateShortcut($env:LINK);"
        "$s.TargetPath=$env:BAT;$s.Arguments='--background';$s.WorkingDirectory=$env:DIR;"
        "$s.WindowStyle=7;$s.Description='Byditors Cut';$s.Save()"
    )
    env = {**os.environ, "LINK": str(link), "BAT": str(bat), "DIR": str(config.ROOT)}
    subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps], env=env,
                   check=True, capture_output=True, timeout=30)
    return link.exists()


_awake = False


def keep_awake(on: bool) -> None:
    """Stop Windows from sleeping (the screen may still turn off) while Byditors Cut has work to do."""
    global _awake
    if not IS_WINDOWS or on == _awake:
        return
    import ctypes

    es_continuous, es_system_required = 0x80000000, 0x00000001
    flags = es_continuous | (es_system_required if on else 0)
    ctypes.windll.kernel32.SetThreadExecutionState(flags)
    _awake = on
