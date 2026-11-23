"""Start Byditors Cut:  python -m bcut   then open http://127.0.0.1:8777

Exit codes the start scripts understand: 0 = stopped on purpose, 3 = already running (don't restart),
anything else = crashed (start.bat restarts it after a few seconds).
"""
import shutil
from pathlib import Path
import socket
import sys
import threading
import time
import traceback
import webbrowser

import uvicorn

from . import config, db, live, winsys
from .server import app, start_background


def _already_running() -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex((config.HOST, config.PORT)) == 0


def _awake_loop() -> None:
    """Keep the PC awake while there are streamers to watch or a stream is recording."""
    while True:
        try:
            busy = bool(live.active()) or bool(db.query("SELECT id FROM streamers WHERE auto_live=1 LIMIT 1"))
            winsys.keep_awake(busy and (db.kv_get("keep_awake", True) is not False))
        except Exception:  # noqa: BLE001
            pass
        time.sleep(60)


def _sync_requirements() -> None:
    """After an update, install any new packages in the background. start.bat only does this on a fresh start,
    and the app also restarts itself (crash restarts, /restart from Telegram), so it checks here too."""
    import subprocess

    root = Path(__file__).resolve().parent.parent
    req, stamp = root / "requirements.txt", root / ".venv" / "installed.ok"
    if not req.exists() or not stamp.parent.exists():
        return
    try:
        if stamp.exists() and stamp.read_bytes() == req.read_bytes():
            return
        r = subprocess.run([sys.executable, "-m", "pip", "install", "-q", "--no-input", "-r", str(req)],
                           capture_output=True, text=True, timeout=1800,
                           creationflags=0x08000000 if sys.platform == "win32" else 0)
        if r.returncode == 0:
            shutil.copyfile(req, stamp)
            db.log("Installed the packages this update needs")
        else:
            db.log(f"Couldn't install this update's packages: {(r.stderr or r.stdout)[-200:]}", "warn")
    except Exception as e:  # noqa: BLE001
        db.log(f"Couldn't install this update's packages: {str(e)[:200]}", "warn")


def _gpu_speech() -> None:
    """Windows PC with an NVIDIA card but no CUDA libraries: speech to text quietly runs on the processor with a
    small model. Install the libraries once (about 1 GB) so it can use the card and the much better model."""
    import subprocess

    if sys.platform != "win32" or not shutil.which("nvidia-smi"):
        return
    try:
        import nvidia.cublas  # noqa: F401
        import nvidia.cudnn  # noqa: F401
        return
    except ImportError:
        pass
    try:
        import ctranslate2
        if ctranslate2.get_cuda_device_count() < 1:
            return
    except Exception:  # noqa: BLE001
        return
    db.log("Setting up your NVIDIA card for speech to text (one-time download, about 1 GB)")
    r = subprocess.run([sys.executable, "-m", "pip", "install", "-q", "--no-input", "nvidia-cublas-cu12",
                        "nvidia-cudnn-cu12>=9,<10"], capture_output=True, text=True, timeout=3600,
                       creationflags=0x08000000)
    if r.returncode == 0:
        db.log("Speech to text can use your NVIDIA card now: it switches over at the next restart")
    else:
        db.log(f"Couldn't set up the NVIDIA card for speech: {(r.stderr or r.stdout)[-200:]}", "warn")


def main() -> None:
    url = f"http://{config.HOST}:{config.PORT}"
    background = "--background" in sys.argv
    if _already_running():
        print(f"\n  {config.APP_NAME} is already running → {url}\n")
        if not background:
            webbrowser.open(url)
        sys.exit(3)
    if not shutil.which("ffmpeg"):
        sys.exit("ffmpeg isn't installed (or not on PATH). Install it first: https://ffmpeg.org/download.html")
    config.ensure_dirs()
    db.conn()
    moved = db.rebase_paths()          # the app folder was moved: point saved clips at the new place
    if moved:
        db.log(f"App folder moved: updated {moved} saved file paths to {config.DATA}")
    db.kv_set("started_at", time.time())
    start_background()
    threading.Thread(target=lambda: (_sync_requirements(), _gpu_speech()), daemon=True, name="requirements").start()
    threading.Thread(target=_awake_loop, daemon=True, name="awake").start()
    print(f"\n  {config.APP_NAME} is running → {url}\n  Data folder: {config.DATA}\n"
          f"  Keep this window open (you can minimise it). Close it to stop.\n")
    if "--no-browser" not in sys.argv and not background:
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    try:
        uvicorn.run(app, host=config.HOST, port=config.PORT, log_level="warning")
    except Exception:  # noqa: BLE001
        log = config.DATA / "logs" / "crash.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("a", encoding="utf-8") as f:
            f.write(f"\n--- {time.ctime()}\n{traceback.format_exc()}")
        raise


if __name__ == "__main__":
    main()
