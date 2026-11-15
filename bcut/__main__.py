"""Start Byditors Cut:  python -m bcut   then open http://127.0.0.1:8777"""
import shutil
import sys
import threading
import webbrowser

import uvicorn

from . import config, db
from .server import app, start_background


def main() -> None:
    if not shutil.which("ffmpeg"):
        sys.exit("ffmpeg isn't installed (or not on PATH). Install it first: https://ffmpeg.org/download.html")
    config.ensure_dirs()
    db.conn()
    start_background()
    url = f"http://{config.HOST}:{config.PORT}"
    print(f"\n  {config.APP_NAME} is running → {url}\n  Data folder: {config.DATA}\n  Stop with Ctrl+C\n")
    if "--no-browser" not in sys.argv:
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    uvicorn.run(app, host=config.HOST, port=config.PORT, log_level="warning")


if __name__ == "__main__":
    main()
