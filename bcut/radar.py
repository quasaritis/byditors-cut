"""Streamer Radar: keeps an eye on every streamer you added.

Every couple of minutes, for each channel (Kick, Twitch, YouTube, TikTok):
- **went live?** → start recording and clipping live (live.py)
- **posted something new?** → download it and clip it like any video (pipeline.py)

The first time a streamer is added, their existing uploads are only noted, not clipped, so you don't wake up to
their whole back catalogue being processed.
"""
from __future__ import annotations

import re
import threading
import time
from urllib.parse import urlparse

import httpx

from . import config, db, live, pipeline
from .media import platform_of

_tokens: dict[str, tuple[str, float]] = {}
_lock = threading.Lock()


def _ydl(url: str, flat: bool = False, end: int = 5) -> dict | None:
    import yt_dlp

    opts = {"quiet": True, "no_warnings": True, "skip_download": True, "playlistend": end, "socket_timeout": 20}
    if flat:
        opts["extract_flat"] = "in_playlist"
    try:
        from . import media

        with yt_dlp.YoutubeDL(media.ytdlp_opts(**opts)) as y:
            return y.extract_info(url, download=False)
    except Exception:  # noqa: BLE001
        return None


def handle(url: str) -> str:
    """The channel's name/slug from its URL: kick.com/xqc -> xqc, youtube.com/@mkbhd -> @mkbhd."""
    path = [p for p in urlparse(url).path.split("/") if p]
    return path[0] if path else ""


def channel_url(platform: str, h: str) -> str:
    h = h.strip()
    if h.startswith("http"):
        return h
    return {
        "kick": f"https://kick.com/{h}",
        "twitch": f"https://www.twitch.tv/{h}",
        "youtube": f"https://www.youtube.com/{h if h.startswith('@') else '@' + h}",
        "tiktok": f"https://www.tiktok.com/{h if h.startswith('@') else '@' + h}",
    }.get(platform, h)


# ---------------------------------------------------------------- official APIs (optional, more reliable)

def _app_token(kind: str) -> str | None:
    with _lock:
        tok = _tokens.get(kind)
        if tok and tok[1] > time.time() + 60:
            return tok[0]
    try:
        if kind == "kick" and config.KICK_CLIENT_ID:
            r = httpx.post("https://id.kick.com/oauth/token", timeout=20, data={
                "grant_type": "client_credentials", "client_id": config.KICK_CLIENT_ID,
                "client_secret": config.KICK_CLIENT_SECRET})
        elif kind == "twitch" and config.TWITCH_CLIENT_ID:
            r = httpx.post("https://id.twitch.tv/oauth2/token", timeout=20, params={
                "grant_type": "client_credentials", "client_id": config.TWITCH_CLIENT_ID,
                "client_secret": config.TWITCH_CLIENT_SECRET})
        else:
            return None
        r.raise_for_status()
        j = r.json()
        with _lock:
            _tokens[kind] = (j["access_token"], time.time() + float(j.get("expires_in", 3600)))
        return j["access_token"]
    except Exception:  # noqa: BLE001
        return None


def is_live(url: str) -> bool:
    plat = platform_of(url)
    h = handle(url)
    if plat == "kick":
        tok = _app_token("kick")
        if tok:
            try:
                r = httpx.get("https://api.kick.com/public/v1/channels", params={"slug": h}, timeout=20,
                              headers={"authorization": f"Bearer {tok}"})
                data = (r.json().get("data") or [{}])[0]
                return bool((data.get("stream") or {}).get("is_live"))
            except Exception:  # noqa: BLE001
                pass
    if plat == "twitch":
        tok = _app_token("twitch")
        if tok:
            try:
                r = httpx.get("https://api.twitch.tv/helix/streams", params={"user_login": h}, timeout=20,
                              headers={"authorization": f"Bearer {tok}", "client-id": config.TWITCH_CLIENT_ID})
                return bool(r.json().get("data"))
            except Exception:  # noqa: BLE001
                pass
    info = _ydl(live.stream_page(url))
    return bool(info and (info.get("is_live") or info.get("live_status") == "is_live"))


def latest_uploads(url: str, n: int = 4) -> list[dict]:
    """Newest uploads as [{"id", "url", "title"}], newest first. Empty list if the site won't say."""
    plat = platform_of(url)
    h = handle(url)
    items: list[dict] = []
    if plat == "kick":
        try:
            r = httpx.get(f"https://kick.com/api/v2/channels/{h}/videos", timeout=20,
                          headers={"accept": "application/json", "user-agent": "Mozilla/5.0"})
            for v in (r.json() or [])[:n]:
                uid = (v.get("video") or {}).get("uuid") or v.get("uuid")
                if uid:
                    items.append({"id": f"kick:{uid}", "url": f"https://kick.com/{h}/videos/{uid}",
                                  "title": v.get("session_title") or "Kick VOD"})
        except Exception:  # noqa: BLE001
            pass
        return items
    feeds = {
        "youtube": [url.rstrip("/") + "/videos", url.rstrip("/") + "/shorts"],
        "twitch": [f"https://www.twitch.tv/{h}/videos?filter=all&sort=time"],
        "tiktok": [url],
    }.get(plat, [])
    for feed in feeds:
        info = _ydl(feed, flat=True, end=n)
        for e in (info or {}).get("entries") or []:
            if not e or not e.get("id"):
                continue
            items.append({"id": f"{plat}:{e['id']}", "url": e.get("url") or e.get("webpage_url") or "",
                          "title": e.get("title") or ""})
    return items[: n * 2]


# ---------------------------------------------------------------- the loop

def check(streamer: dict) -> dict:
    status = dict(streamer.get("status") or {})
    seen = list(streamer.get("seen") or [])
    first_time = not status.get("checked_once")
    for plat, url in (streamer.get("channels") or {}).items():
        if not url:
            continue
        url = channel_url(plat, url)
        if streamer.get("auto_live") and not live.active_for(streamer["id"]) and not live.backing_off(streamer["id"]):
            try:
                if is_live(url):
                    live.start(streamer, live.stream_page(url))
                    status[f"{plat}_live"] = time.time()
                    db.log(f"{streamer['name']} went live on {plat.title()} — recording + clipping", streamer_id=streamer["id"])
            except Exception as e:  # noqa: BLE001
                db.log(f"Live check failed on {plat}: {str(e)[:160]}", "warn", streamer["id"])
        if streamer.get("auto_uploads"):
            recent_live = any(time.time() - float(v) < 12 * 3600 for k, v in status.items() if k.endswith("_live"))
            for item in latest_uploads(url):
                if item["id"] in seen:
                    continue
                seen.append(item["id"])
                if first_time:
                    continue
                if plat == "twitch" and recent_live:
                    continue  # that's the VOD of a live we already clipped
                if item["url"]:
                    pipeline.create_job("upload", url=item["url"], streamer_id=streamer["id"], title=item["title"])
                    db.log(f"New upload from {streamer['name']} on {plat.title()}: {item['title'][:80]}",
                           streamer_id=streamer["id"])
    status["checked_once"] = True
    status["last_check"] = time.time()
    db.update("streamers", streamer["id"], status=status, seen=seen[-300:])
    return status


def check_now(streamer_id: str) -> None:
    s = db.get("streamers", streamer_id)
    if s:
        threading.Thread(target=check, args=(s,), daemon=True).start()


def _loop():
    time.sleep(10)
    while True:
        t0 = time.time()
        for s in db.query("SELECT * FROM streamers"):
            try:
                check(s)
            except Exception as e:  # noqa: BLE001
                db.log(f"Radar error for {s['name']}: {str(e)[:200]}", "warn", s["id"])
        time.sleep(max(20, config.RADAR_EVERY_SEC - (time.time() - t0)))


def start() -> None:
    threading.Thread(target=_loop, daemon=True, name="radar").start()


def parse_channels(text: str) -> dict:
    """Turn pasted links/handles into {"kick": url, "twitch": url, ...}."""
    out = {}
    for tok in re.split(r"[\s,]+", text or ""):
        if not tok:
            continue
        plat = platform_of(tok if tok.startswith("http") else "https://" + tok)
        if plat in ("kick", "twitch", "youtube", "tiktok"):
            out[plat] = tok if tok.startswith("http") else "https://" + tok
    return out
