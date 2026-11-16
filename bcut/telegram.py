"""Phone alerts and remote control through your own Telegram bot.

Make a bot with @BotFather, put its token in Settings (or BCUT_TELEGRAM_TOKEN in .env), then send the bot
"/start <code>" with the pairing code the app shows. From then on only that Telegram chat can use it.

You get: streamer live / new upload / posted / failures as messages, every new clip as a video with Approve and
Reject buttons, and the commands /status, /today, /review, /restart.

The app only talks out to api.telegram.org (long polling), so nothing on your PC is opened to the internet.
"""
from __future__ import annotations

import json
import os
import secrets
import threading
import time
from pathlib import Path

import httpx

from . import config, db

API = "https://api.telegram.org"
MAX_VIDEO_MB = 49                      # bots can upload up to 50 MB
_sent: dict[str, float] = {}           # text -> last sent, to drop repeats
_thread: threading.Thread | None = None
_stop = threading.Event()
SKIP = ("Live clip from", "Autopilot scheduled", "Added streamer", "Telegram")


# ---------------------------------------------------------------- settings

def settings() -> dict:
    s = db.kv_get("telegram", {}) or {}
    token = s.get("token") or config.TELEGRAM_TOKEN
    return {**s, "token": token}


def _save(**changes) -> dict:
    s = db.kv_get("telegram", {}) or {}
    s.update(changes)
    db.kv_set("telegram", s)
    return s


def pairing_code() -> str:
    s = settings()
    if not s.get("code"):
        s = _save(code=f"{secrets.randbelow(10 ** 6):06d}", tries=0)
    return s["code"]


def public() -> dict:
    s = settings()
    return {"configured": bool(s.get("token")), "paired": bool(s.get("chat_id")), "bot": s.get("bot", ""),
            "owner": s.get("owner", ""), "code": "" if s.get("chat_id") else (pairing_code() if s.get("token") else "")}


def set_token(token: str) -> dict:
    token = token.strip()
    if token:
        me = _call("getMe", token=token)
        _save(token=token, bot=me.get("username", ""), chat_id=None, owner="", code=None, offset=0)
    else:
        _save(token="", chat_id=None, owner="", bot="")
    restart()
    return public()


def unpair() -> dict:
    _save(chat_id=None, owner="", code=None)
    return public()


# ---------------------------------------------------------------- Telegram calls

def _call(method: str, token: str | None = None, files=None, timeout: float = 30, **params):
    token = token or settings().get("token")
    if not token:
        raise RuntimeError("No Telegram bot token")
    data = {k: (json.dumps(v) if isinstance(v, (dict, list)) else v) for k, v in params.items() if v is not None}
    r = httpx.post(f"{API}/bot{token}/{method}", data=data, files=files, timeout=timeout)
    j = r.json()
    if not j.get("ok"):
        raise RuntimeError(j.get("description") or f"Telegram {method} failed")
    return j["result"]


def _owner() -> int | None:
    return settings().get("chat_id")


def say(text: str, chat_id: int | None = None, **extra) -> None:
    chat = chat_id or _owner()
    if not chat:
        return
    try:
        _call("sendMessage", chat_id=chat, text=text[:4000], disable_web_page_preview=True, **extra)
    except Exception:  # noqa: BLE001
        pass


def _buttons(clip: dict) -> dict | None:
    if clip.get("state") in ("approved", "rejected", "posted"):
        return None
    row = []
    if clip.get("verdict") != "block":
        row.append({"text": "✅ Approve", "callback_data": f"ap:{clip['id']}"})
    row.append({"text": "❌ Reject", "callback_data": f"rj:{clip['id']}"})
    return {"inline_keyboard": [row]}


def _clip_text(clip: dict) -> str:
    g = clip.get("guard") or {}
    who = (db.get("streamers", clip["streamer_id"]) or {}).get("name", "") if clip.get("streamer_id") else ""
    verdict = {"pass": "✅ Safe to post", "review": "👀 Needs your OK", "block": "⛔ Blocked by the Guard"}.get(
        clip.get("verdict"), clip.get("verdict", ""))
    lines = [f"🎬 {clip.get('title') or 'New clip'}", f"{verdict}" + (f" · {who}" if who else "")]
    if g.get("auto_post_allowed") and clip.get("verdict") == "pass":
        lines.append("Will post by itself on your schedule. Reject to stop it.")
    reasons = [r.get("text") or r.get("reason") or "" for r in (g.get("reasons") or g.get("issues") or [])
               if isinstance(r, dict)]
    if reasons and clip.get("verdict") != "pass":
        lines.append("Why: " + "; ".join(x for x in reasons[:3] if x))
    if clip.get("verdict") == "block":
        lines.append("To post it anyway, open the app and write why it's allowed.")
    return "\n".join(lines)[:1000]


def send_clip(clip: dict, chat_id: int | None = None) -> None:
    chat = chat_id or _owner()
    if not chat:
        return
    try:
        f = Path(clip.get("file") or "")
        kb = _buttons(clip)
        if f.exists() and f.stat().st_size < MAX_VIDEO_MB * 2 ** 20:
            with f.open("rb") as fh:
                _call("sendVideo", files={"video": (f.name, fh, "video/mp4")}, timeout=300, chat_id=chat,
                      caption=_clip_text(clip), supports_streaming="true", reply_markup=kb)
        else:
            say(_clip_text(clip), chat, reply_markup=kb)
    except Exception as e:  # noqa: BLE001
        say(f"{_clip_text(clip)}\n(couldn't attach the video: {str(e)[:120]})", chat)


# ---------------------------------------------------------------- hooks from the rest of the app

def on_event(text: str, level: str) -> None:
    """Every app event (db.log) comes here. Forward the useful ones, drop repeats for 10 minutes."""
    if not _owner() or text.startswith(SKIP):
        return
    now = time.time()
    if now - _sent.get(text, 0) < 600:
        return
    _sent[text] = now
    icon = {"error": "❗", "warn": "⚠️"}.get(level, "•")
    threading.Thread(target=say, args=(f"{icon} {text}",), daemon=True).start()


def clip_ready(clip: dict) -> None:
    if _owner():
        threading.Thread(target=send_clip, args=(clip,), daemon=True).start()


# ---------------------------------------------------------------- commands

HELP = ("Byditors Cut on your PC.\n"
        "/status — is it running, what's recording\n"
        "/today — clips and uploads today\n"
        "/review — clips waiting for your OK (with buttons)\n"
        "/restart — restart the app (loads updates)")


def _today_start() -> float:
    t = time.localtime()
    return time.mktime((t.tm_year, t.tm_mon, t.tm_mday, 0, 0, 0, 0, 0, -1))


def _status() -> str:
    from . import live

    started = db.kv_get("started_at") or time.time()
    up = int(time.time() - started)
    lines = [f"🟢 Running on {os.environ.get('COMPUTERNAME') or 'your PC'} for {up // 3600}h {up % 3600 // 60}m"]
    sess = live.active()
    for s in sess:
        lines.append(f"🔴 Recording {(s.streamer or {}).get('name', 'a stream')}: {s.title[:60] or 'live'} "
                     f"({int(s.now() // 60)} min, {s.clips} clips)")
    if not sess:
        lines.append("No live recording right now.")
    work = db.query("SELECT title, step FROM jobs WHERE status IN ('queued','working') ORDER BY created LIMIT 5")
    for j in work:
        lines.append(f"⚙️ {j['title'][:50] or 'video'}: {j['step']}")
    nxt = db.query("SELECT due FROM posts WHERE status='scheduled' ORDER BY due LIMIT 1")
    if nxt:
        lines.append("Next upload: " + time.strftime("%a %H:%M", time.localtime(nxt[0]["due"])))
    accs = db.query("SELECT platform, name FROM accounts")
    if accs:
        lines.append("Accounts: " + ", ".join(f"{a['platform'].title()}" for a in accs))
    return "\n".join(lines)


def _today() -> str:
    t0 = _today_start()
    clips = db.query("SELECT verdict, state FROM clips WHERE created >= ?", (t0,))
    posted = db.query("SELECT COUNT(*) AS n FROM posts WHERE status='posted' AND updated >= ?", (t0,))
    sched = db.query("SELECT COUNT(*) AS n FROM posts WHERE status='scheduled'")
    failed = db.query("SELECT COUNT(*) AS n FROM posts WHERE status='failed' AND updated >= ?", (t0,))
    by = {v: sum(1 for c in clips if c["verdict"] == v) for v in ("pass", "review", "block")}
    waiting = sum(1 for c in clips if c["state"] == "new" and c["verdict"] != "pass")
    return (f"Today: {len(clips)} clips ({by['pass']} safe, {by['review']} to check, {by['block']} blocked)\n"
            f"Uploaded: {posted[0]['n']} · Scheduled: {sched[0]['n']} · Failed: {failed[0]['n']}\n"
            + (f"{waiting} waiting for you: /review" if waiting else "Nothing waiting for you."))


def _review(chat: int) -> None:
    rows = db.query("SELECT * FROM clips WHERE state='new' AND verdict!='pass' ORDER BY created DESC LIMIT 5")
    if not rows:
        say("Nothing waiting for your OK.", chat)
        return
    say(f"{len(rows)} newest clip{'s' if len(rows) != 1 else ''} waiting:", chat)
    for c in rows:
        send_clip(db.get("clips", c["id"]), chat)


def _approve(cid: str) -> str:
    from fastapi import HTTPException

    from . import server

    try:
        r = server.approve(cid, {})
    except HTTPException as e:
        return str(e.detail)
    n = len(r.get("posts") or [])
    return "Approved" + (f", {n} upload{'s' if n != 1 else ''} scheduled." if n else
                         ". It goes out with your daily uploads.")


def _reject(cid: str) -> str:
    from . import server

    server.reject(cid)
    return "Rejected. It won't be posted."


def _handle(u: dict) -> None:
    s = settings()
    owner = s.get("chat_id")
    if "callback_query" in u:
        q = u["callback_query"]
        chat = ((q.get("message") or {}).get("chat") or {}).get("id")
        if not owner or chat != owner:
            return
        act, _, cid = (q.get("data") or "").partition(":")
        if not db.get("clips", cid):
            msg = "That clip is gone."
        else:
            msg = _approve(cid) if act == "ap" else _reject(cid) if act == "rj" else "?"
        try:
            _call("answerCallbackQuery", callback_query_id=q["id"], text=msg[:190])
            m = q.get("message") or {}
            _call("editMessageReplyMarkup", chat_id=chat, message_id=m.get("message_id"),
                  reply_markup={"inline_keyboard": []})
        except Exception:  # noqa: BLE001
            pass
        say(msg, chat)
        return
    m = u.get("message") or {}
    chat = (m.get("chat") or {}).get("id")
    text = (m.get("text") or "").strip()
    if not chat or not text:
        return
    if not owner:
        code = text.replace("/start", "").strip()
        if code and code == s.get("code"):
            who = (m.get("from") or {}).get("username") or (m.get("from") or {}).get("first_name") or ""
            _save(chat_id=chat, owner=who, code=None)
            db.log(f"Telegram connected ({'@' + who if who else 'your chat'})")
            say("✅ Connected. You'll get alerts and new clips here.\n\n" + HELP, chat)
        else:
            tries = int(s.get("tries") or 0) + 1
            if tries >= 10:
                _save(code=None, tries=0)
            else:
                _save(tries=tries)
            say("Send the pairing code shown in Byditors Cut → Settings, like: /start 123456", chat)
        return
    if chat != owner:
        return  # strangers get nothing
    cmd = text.split()[0].split("@")[0].lower()
    if cmd in ("/status", "status"):
        say(_status(), chat)
    elif cmd in ("/today", "today"):
        say(_today(), chat)
    elif cmd in ("/review", "review"):
        _review(chat)
    elif cmd in ("/restart", "restart"):
        say("Restarting Byditors Cut… I'll message you when it's back.", chat)
        _save(restart_note=chat)
        threading.Thread(target=lambda: (time.sleep(2), os._exit(75)), daemon=True).start()
    else:
        say(HELP, chat)


# ---------------------------------------------------------------- loop

def _loop() -> None:
    backoff = 5
    note = settings().get("restart_note")
    if note:
        _save(restart_note=None)
        say("✅ Byditors Cut is back up.", note)
    while not _stop.is_set():
        s = settings()
        if not s.get("token"):
            _stop.wait(30)
            continue
        try:
            ups = _poll(int(s.get("offset") or 0))
            backoff = 5
            for u in ups:
                _save(offset=u["update_id"] + 1)   # saved first, so a /restart isn't handled twice
                try:
                    _handle(u)
                except Exception as e:  # noqa: BLE001
                    say(f"That didn't work: {str(e)[:200]}")
        except Exception:  # noqa: BLE001
            _stop.wait(backoff)
            backoff = min(backoff * 2, 300)


def _poll(offset: int) -> list[dict]:
    token = settings().get("token")
    r = httpx.post(f"{API}/bot{token}/getUpdates", timeout=70,
                   data={"offset": offset, "timeout": 50,
                         "allowed_updates": json.dumps(["message", "callback_query"])})
    j = r.json()
    if not j.get("ok"):
        raise RuntimeError(j.get("description") or "getUpdates failed")
    return j["result"]


def start() -> None:
    global _thread
    db.add_hook(on_event)
    if _thread and _thread.is_alive():
        return
    _stop.clear()
    _thread = threading.Thread(target=_loop, daemon=True, name="telegram")
    _thread.start()


def restart() -> None:
    """New token: the running loop picks it up on its next poll."""
    _save(offset=0)
