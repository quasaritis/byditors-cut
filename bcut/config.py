"""Every setting in one place. Values come from the environment (normally a `.env` file next to README.md).

Nothing here is secret by default. Keys and tokens live in `.env` or in the data folder, never in the code.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv() -> None:
    env = ROOT / ".env"
    if not env.exists():
        return
    for line in env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        v = v.strip()
        if v[:1] in ('"', "'") and v[0] in v[1:]:
            v = v[1:v.index(v[0], 1)]          # quoted value: keep everything inside the quotes, # included
        else:
            v = re.split(r"\s+#", v, maxsplit=1)[0].strip()   # unquoted: drop an inline "  # comment"
        os.environ.setdefault(k.strip(), v)


_load_dotenv()


def _get(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _int(name: str, default: int) -> int:
    try:
        return int(_get(name, str(default)))
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(_get(name, str(default)))
    except ValueError:
        return default


def _bool(name: str, default: bool) -> bool:
    v = _get(name, "1" if default else "0").lower()
    return v in ("1", "true", "yes", "on")


APP_NAME = "Byditors Cut"
HOST = _get("BCUT_HOST", "127.0.0.1")
PORT = _int("BCUT_PORT", 8777)
DATA = Path(_get("BCUT_DATA", str(ROOT / "data"))).expanduser().resolve()
PUBLIC_URL = _get("BCUT_PUBLIC_URL", f"http://{HOST}:{PORT}")

# ---- speech to text (runs on this PC) ----
WHISPER_MODEL = _get("BCUT_WHISPER_MODEL", "small")          # tiny, base, small, medium, large-v3, distil-large-v3
WHISPER_DEVICE = _get("BCUT_WHISPER_DEVICE", "auto")         # auto, cpu, cuda
WHISPER_LANGUAGE = _get("BCUT_WHISPER_LANGUAGE", "")         # empty = detect (handles Hindi / Hinglish)

# ---- AI that picks clips and writes hooks (optional) ----
AI_PROVIDER = _get("BCUT_AI_PROVIDER", "")                   # openai, gemini, groq, openrouter, xai, anthropic, ollama, custom
AI_MODEL = _get("BCUT_AI_MODEL", "")
AI_KEY = _get("BCUT_AI_KEY", "")
AI_BASE_URL = _get("BCUT_AI_BASE_URL", "")

# ---- clipping ----
CLIP_MIN = _int("BCUT_CLIP_MIN", 15)
CLIP_MAX = _int("BCUT_CLIP_MAX", 60)
CLIPS_PER_VIDEO = _int("BCUT_CLIPS_PER_VIDEO", 6)
CAPTION_FONT = _get("BCUT_CAPTION_FONT", "Anton")
CREDIT_OVERLAY = _bool("BCUT_CREDIT_OVERLAY", True)

# ---- live + radar ----
RADAR_EVERY_SEC = _int("BCUT_RADAR_EVERY_SEC", 120)
LIVE_BUFFER_MIN = _int("BCUT_LIVE_BUFFER_MIN", 30)
LIVE_SEGMENT_SEC = _int("BCUT_LIVE_SEGMENT_SEC", 6)
LIVE_MAX_PARALLEL = _int("BCUT_LIVE_MAX_PARALLEL", 3)
LIVE_CLIP_COOLDOWN_SEC = _int("BCUT_LIVE_CLIP_COOLDOWN_SEC", 90)
KICK_CLIENT_ID = _get("KICK_CLIENT_ID")
KICK_CLIENT_SECRET = _get("KICK_CLIENT_SECRET")
TWITCH_CLIENT_ID = _get("TWITCH_CLIENT_ID")
TWITCH_CLIENT_SECRET = _get("TWITCH_CLIENT_SECRET")

# ---- guard ----
AUDD_TOKEN = _get("AUDD_API_TOKEN")              # optional: identifies copyrighted songs (audd.io)
OPENAI_MODERATION_KEY = _get("OPENAI_MODERATION_KEY") or (AI_KEY if AI_PROVIDER == "openai" else "")
MUSIC_REVIEW_SHARE = _float("BCUT_MUSIC_REVIEW_SHARE", 0.15)   # share of a clip that is music before it needs a look
MUSIC_BLOCK_SHARE = _float("BCUT_MUSIC_BLOCK_SHARE", 0.55)
STRIP_MUSIC = _bool("BCUT_STRIP_MUSIC", True)                  # try to remove background music automatically

# ---- posting ----
POST_GAP_MIN = _int("BCUT_POST_GAP_MIN", 90)
POST_DAILY_CAP = _int("BCUT_POST_DAILY_CAP", 6)
QUIET_HOURS = _get("BCUT_QUIET_HOURS", "01-07")                # local hours with no posting, "" for none
GRAPH_VERSION = _get("BCUT_GRAPH_VERSION", "v21.0")
GOOGLE_CLIENT_ID = _get("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = _get("GOOGLE_CLIENT_SECRET")
TIKTOK_CLIENT_KEY = _get("TIKTOK_CLIENT_KEY")
TIKTOK_CLIENT_SECRET = _get("TIKTOK_CLIENT_SECRET")
TIKTOK_REDIRECT_URI = _get("TIKTOK_REDIRECT_URI")

# testing hook: skip network-bound background loops
NO_BACKGROUND = _bool("BCUT_NO_BACKGROUND", False)


def ensure_dirs() -> None:
    for sub in ("sources", "clips", "live", "tmp", "accounts"):
        (DATA / sub).mkdir(parents=True, exist_ok=True)
