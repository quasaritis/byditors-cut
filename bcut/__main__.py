"""Start Byditors Cut:  python -m bcut   then open http://127.0.0.1:8777

Exit codes the start scripts understand: 0 = stopped on purpose, 3 = already running (don't restart),
anything else = crashed (start.bat restarts it after a few seconds).
"""
import shutil
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
    start_background()
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
