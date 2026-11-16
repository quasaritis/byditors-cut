"""Optional add-ons, installed from the dashboard with one click, and keys pasted in the dashboard.

Installs run with this app's own Python (the one in .venv), so nothing touches the rest of the PC. Keys pasted
in the dashboard are kept in the local database and win over `.env`.
"""
from __future__ import annotations

import importlib
import importlib.util
import os
import subprocess
import sys
import threading
import time

from . import config, db

# what the button installs. demucs 4.1+ reads and writes audio itself, so it doesn't need torchaudio.
PACKAGES = {
    "whisper": {"label": "Speech to text", "module": "faster_whisper", "pip": ["faster-whisper>=1.1"],
                "size": "about 200 MB"},
    "demucs": {"label": "Music removal", "module": "demucs", "pip": ["demucs>=4.1", "torch"],
               "size": "about 1-2 GB (it includes PyTorch)"},
}
KEYS = {"audd": "AUDD_TOKEN", "moderation": "OPENAI_MODERATION_KEY"}

_jobs: dict[str, dict] = {}
_lock = threading.Lock()


def installed(name: str) -> bool:
    importlib.invalidate_caches()
    try:
        return importlib.util.find_spec(PACKAGES[name]["module"]) is not None
    except (ImportError, ValueError):
        return False


def status() -> dict:
    with _lock:
        return {k: dict(v) for k, v in _jobs.items()}


def install(name: str) -> dict:
    if name not in PACKAGES:
        raise ValueError("Unknown add-on")
    with _lock:
        if _jobs.get(name, {}).get("state") == "installing":
            return dict(_jobs[name])
        _jobs[name] = {"state": "installing", "started": time.time(), "log": "Starting…"}

    def run():
        cmd = [sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "--no-input",
               "--progress-bar", "off", *PACKAGES[name]["pip"]]
        # UTF-8 everywhere: on Windows a pipe defaults to the old code page and pip's output can crash it.
        env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
        logfile = config.DATA / "logs" / f"install-{name}.log"
        logfile.parent.mkdir(parents=True, exist_ok=True)
        tail: list[str] = []
        code = -1
        try:
            p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env,
                                 encoding="utf-8", errors="replace")
            with logfile.open("w", encoding="utf-8") as lf:
                lf.write(" ".join(cmd) + "\n\n")
                for line in p.stdout:
                    lf.write(line)
                    line = line.strip()
                    if line:
                        tail = (tail + [line])[-6:]
                        with _lock:
                            _jobs[name]["log"] = line[:200]
            code = p.wait()
        except Exception as e:  # noqa: BLE001
            tail.append(f"{type(e).__name__}: {e}")
        ok = code == 0 and installed(name)
        reason = next((t for t in reversed(tail) if "error" in t.lower()), " | ".join(tail[-2:]))
        with _lock:
            _jobs[name] = {"state": "done" if ok else "failed", "finished": time.time(),
                           "log": "Installed." if ok else f"{reason[:300]} (full log: {logfile})"}
        db.log(f"{PACKAGES[name]['label']} {'installed' if ok else 'install failed: ' + reason[:200]}",
               "info" if ok else "error")

    threading.Thread(target=run, daemon=True, name=f"install-{name}").start()
    return status()[name]


def key(name: str) -> str:
    """A key pasted in the dashboard, else the one in .env."""
    saved = (db.kv_get("keys", {}) or {}).get(name, "")
    return saved or getattr(config, KEYS[name], "")


def set_key(name: str, value: str) -> None:
    keys = db.kv_get("keys", {}) or {}
    if value:
        keys[name] = value.strip()
    else:
        keys.pop(name, None)
    db.kv_set("keys", keys)
