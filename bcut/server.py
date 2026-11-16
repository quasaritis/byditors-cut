"""The local web server: the dashboard at http://127.0.0.1:8777 and the small API it talks to.

It listens on this computer only, answers only to its own address and refuses changes sent from other
websites, so a page you visit can't drive it.
"""
from __future__ import annotations

import secrets
import shutil
import os
import threading
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from fastapi import Depends, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse

from . import ai, config, db, guard, live, media, pipeline, publish, radar, telegram, tools, transcribe, winsys
from .guard import music, rules
from .publish import platforms

WEB = config.ROOT / "web"
app = FastAPI(title=config.APP_NAME, docs_url=None, redoc_url=None)
_oauth_states: dict[str, float] = {}


@app.middleware("http")
async def gate(request: Request, call_next):
    host = (request.headers.get("host") or "").split(":")[0]
    if host not in ("127.0.0.1", "localhost", config.HOST):
        return JSONResponse({"error": "Unknown host"}, status_code=400)
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        origin = request.headers.get("origin")
        if origin and urlparse(origin).hostname not in ("127.0.0.1", "localhost", config.HOST):
            return JSONResponse({"error": "Cross-site request refused"}, status_code=403)
    resp = await call_next(request)
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "no-referrer"
    return resp


def _need(table: str, rid: str) -> dict:
    row = db.get(table, rid)
    if not row:
        raise HTTPException(404, "Not found")
    return row


async def _body(request: Request) -> dict:
    try:
        data = await request.json()
    except Exception:  # noqa: BLE001
        data = {}
    return data if isinstance(data, dict) else {}


# ---------------------------------------------------------------- pages and media

@app.get("/", response_class=HTMLResponse)
def index():
    return (WEB / "index.html").read_text(encoding="utf-8")


@app.get("/static/{name}")
def static(name: str):
    path = (WEB / name).resolve()
    if path.parent != WEB.resolve() or not path.exists():
        raise HTTPException(404)
    return FileResponse(path)


@app.get("/media/clip/{cid}.mp4")
def clip_file(cid: str):
    c = _need("clips", cid)
    return FileResponse(c["file"], media_type="video/mp4")


@app.get("/media/thumb/{cid}.jpg")
def clip_thumb(cid: str):
    c = _need("clips", cid)
    if not c["thumb"] or not Path(c["thumb"]).exists():
        raise HTTPException(404)
    return FileResponse(c["thumb"], media_type="image/jpeg")


# ---------------------------------------------------------------- overview

def _clip_public(c: dict) -> dict:
    g = c.get("guard") or {}
    return {**{k: v for k, v in c.items() if k not in ("file", "thumb")},
            "duration": round(c["end"] - c["start"], 1),
            "guard": {k: v for k, v in g.items() if k != "text"},
            "transcript": g.get("text", "")[:1500],
            "streamer": (db.get("streamers", c["streamer_id"]) or {}).get("name") if c.get("streamer_id") else None}


@app.get("/api/state")
def state():
    since = time.time() - 86400
    clips = db.query("SELECT verdict, state, created FROM clips")
    posts = db.query("SELECT status, due, updated FROM posts")
    sessions = []
    for s in live.active():
        sessions.append({"job_id": s.job["id"], "streamer": (s.streamer or {}).get("name", "Stream"),
                         "url": s.url, "title": s.title, "recorded": round(max((x["t1"] for x in s.segs), default=0)),
                         "clips": s.clips, "chat": len(s.chat), "heat": [round(float(v), 2) for v in _heat(s)]})
    return {
        "app": config.APP_NAME,
        "counts": {
            "clips_today": sum(1 for c in clips if c["created"] > since),
            "to_review": sum(1 for c in clips if c["state"] == "new"),
            "blocked_today": sum(1 for c in clips if c["created"] > since and c["verdict"] == "block"),
            "scheduled": sum(1 for p in posts if p["status"] == "scheduled"),
            "posted_today": sum(1 for p in posts if p["status"] == "posted" and p["updated"] > since),
            "failed": sum(1 for p in posts if p["status"] == "failed"),
        },
        "live": sessions,
        "jobs": db.query("SELECT id, kind, url, title, status, step, progress, error, streamer_id, created "
                         "FROM jobs WHERE kind != 'live' ORDER BY created DESC LIMIT 8"),
        "events": db.query("SELECT * FROM events ORDER BY id DESC LIMIT 12"),
        "tools": {"ffmpeg": bool(shutil.which("ffmpeg")), "whisper": transcribe.available(),
                  "demucs": music.can_separate(), "ai": ai.enabled(), "ai_provider": ai.settings()["provider"],
                  "audd": bool(tools.key("audd")), "moderation": bool(tools.key("moderation")),
                  "installing": tools.status()},
        "accounts": len(db.query("SELECT id FROM accounts")),
        "streamers": len(db.query("SELECT id FROM streamers")),
    }


def _heat(s) -> list:
    v = s.db[-480:]
    if len(v) < 4:
        return []
    import numpy as np
    arr = np.array(v)
    arr = (arr - arr.min()) / (arr.max() - arr.min() + 1e-6)
    idx = np.linspace(0, len(arr) - 1, 96).astype(int)
    return arr[idx].tolist()


@app.get("/api/events")
def events(limit: int = 60):
    return db.query("SELECT * FROM events ORDER BY id DESC LIMIT ?", (min(limit, 300),))


# ---------------------------------------------------------------- jobs

@app.post("/api/jobs")
def new_job(b: dict = Depends(_body)):
    url = str(b.get("url", "")).strip()
    if not url.startswith(("http://", "https://")):
        raise HTTPException(400, "Paste a full link that starts with https://")
    sid = b.get("streamer_id") or None
    rights = b.get("rights") if b.get("rights") in ("own", "permission", "unknown") else "unknown"
    if not sid:  # tie the link to a streamer you already added, so their rights + credit apply
        for s in db.query("SELECT * FROM streamers"):
            if any(radar.handle(u).lower().lstrip("@") == radar.handle(url).lower().lstrip("@")
                   and media.platform_of(u) == media.platform_of(url) for u in (s["channels"] or {}).values()):
                sid = s["id"]
                break
    return pipeline.create_job("video", url=url, streamer_id=sid, meta={"rights": rights})


@app.post("/api/upload")
async def upload(file: UploadFile = File(...)):
    name = Path(file.filename or "video.mp4").name
    if Path(name).suffix.lower() not in (".mp4", ".mov", ".mkv", ".webm", ".m4v", ".ts", ".avi"):
        raise HTTPException(400, "That isn't a video file this app can read")
    dest = config.DATA / "sources" / f"up_{int(time.time())}_{secrets.token_hex(3)}" / name
    dest.parent.mkdir(parents=True, exist_ok=True)
    with dest.open("wb") as f:
        shutil.copyfileobj(file.file, f, length=4 * 2**20)
    return pipeline.create_job("video", file=str(dest), title=name)


@app.get("/api/jobs")
def jobs():
    return db.query("SELECT * FROM jobs ORDER BY created DESC LIMIT 100")


@app.post("/api/jobs/{jid}/retry")
def retry_job(jid: str):
    j = _need("jobs", jid)
    if j["status"] in ("working", "queued", "recording") or j["kind"] == "live":
        raise HTTPException(409, "It's already running")
    return pipeline.retry_job(jid)


@app.delete("/api/jobs/{jid}")
def delete_job(jid: str):
    j = _need("jobs", jid)
    if j["status"] in ("working", "recording"):
        raise HTTPException(409, "It's still running")
    for c in db.query("SELECT id FROM clips WHERE job_id=?", (jid,)):
        db.delete("clips", c["id"])
    shutil.rmtree(config.DATA / "clips" / jid, ignore_errors=True)
    shutil.rmtree(config.DATA / "sources" / jid, ignore_errors=True)
    db.delete("jobs", jid)
    return {"ok": True}


# ---------------------------------------------------------------- clips

@app.get("/api/clips")
def clips(state: str = "", verdict: str = "", limit: int = 60):
    sql, args = "SELECT * FROM clips WHERE 1=1", []
    if state:
        sql += " AND state=?"
        args.append(state)
    if verdict:
        sql += " AND verdict=?"
        args.append(verdict)
    sql += " ORDER BY created DESC LIMIT ?"
    args.append(min(limit, 300))
    return [_clip_public(c) for c in db.query(sql, tuple(args))]


@app.get("/api/clips/{cid}")
def clip(cid: str):
    c = _clip_public(_need("clips", cid))
    c["posts"] = db.query("SELECT * FROM posts WHERE clip_id=?", (cid,))
    return c


@app.patch("/api/clips/{cid}")
def edit_clip(cid: str, b: dict = Depends(_body)):
    _need("clips", cid)
    fields = {}
    if "title" in b:
        fields["title"] = str(b["title"])[:120]
    if isinstance(b.get("captions"), dict):
        fields["captions"] = b["captions"]
    return _clip_public(db.update("clips", cid, **fields))


@app.post("/api/clips/{cid}/approve")
def approve(cid: str, b: dict = Depends(_body)):
    c = _need("clips", cid)
    g = dict(c["guard"] or {})
    if c["verdict"] == "block":
        reason = str(b.get("override_reason", "")).strip()
        if len(reason) < 12:
            raise HTTPException(400, "This clip is blocked. To post it anyway, write why it's allowed "
                                     "(for example: 'it's my own song, I own the rights').")
        g["override_reason"] = reason[:500]
        db.log(f"Guard block overridden for '{c['title'][:60]}': {reason[:200]}", "warn", c.get("streamer_id"))
    g["approved_by_you"] = True
    g["approved_at"] = time.time()
    db.update("clips", cid, guard=g, state="approved")
    accounts = b.get("accounts")
    if accounts is None and c.get("streamer_id"):
        accounts = (db.get("streamers", c["streamer_id"]) or {}).get("post_to") or []
    posts = publish.enqueue(cid, [a for a in (accounts or []) if isinstance(a, str)],
                            at=time.time() + 5 if b.get("now") else None)
    return {"clip": _clip_public(db.get("clips", cid)), "posts": posts}


@app.post("/api/clips/{cid}/reject")
def reject(cid: str):
    _need("clips", cid)
    for p in db.query("SELECT id FROM posts WHERE clip_id=? AND status='scheduled'", (cid,)):
        db.update("posts", p["id"], status="cancelled")
    return _clip_public(db.update("clips", cid, state="rejected"))


@app.post("/api/clips/{cid}/strip-music")
def strip(cid: str):
    _need("clips", cid)
    return _clip_public(pipeline.strip_music(cid))


@app.post("/api/clips/{cid}/recheck")
def recheck(cid: str):
    c = _need("clips", cid)
    s = db.get("streamers", c["streamer_id"]) if c.get("streamer_id") else None
    j = db.get("jobs", c["job_id"]) or {}
    info = {"duration": (j.get("meta") or {}).get("duration"), "uploaded_by_you": bool((j.get("meta") or {}).get("file"))}
    old = c["guard"] or {}
    rep = guard.review(c, s, info, old.get("text", ""), pipeline._targets(s), old.get("ai_flags"))
    rep.update({k: old[k] for k in ("text", "ai_flags", "layout") if k in old})
    return _clip_public(db.update("clips", cid, guard=rep, verdict=rep["verdict"]))


# ---------------------------------------------------------------- streamers

def _streamer_fields(b: dict) -> dict:
    out = {}
    if "name" in b:
        out["name"] = str(b["name"]).strip()[:60]
    if "channels" in b:
        ch = b["channels"]
        if isinstance(ch, str):
            ch = radar.parse_channels(ch)
        out["channels"] = {k: radar.channel_url(k, str(v)) for k, v in (ch or {}).items()
                           if k in ("kick", "twitch", "youtube", "tiktok") and v}
    if "rights" in b:
        if b["rights"] not in guard.RIGHTS:
            raise HTTPException(400, "Unknown rights value")
        out["rights"] = b["rights"]
    for k in ("rights_note", "credit", "layout", "language"):
        if k in b:
            out[k] = str(b[k])[:500]
    for k in ("auto_live", "auto_uploads", "auto_post"):
        if k in b:
            out[k] = 1 if b[k] else 0
    if "post_to" in b and isinstance(b["post_to"], list):
        out["post_to"] = [a for a in b["post_to"] if db.get("accounts", a)]
    return out


@app.get("/api/streamers")
def streamers():
    out = []
    for s in db.query("SELECT * FROM streamers ORDER BY name"):
        sess = live.active_for(s["id"])
        n = db.query("SELECT COUNT(*) AS n FROM clips WHERE streamer_id=?", (s["id"],))[0]["n"]
        out.append({**{k: v for k, v in s.items() if k != "seen"}, "live_job": sess.job["id"] if sess else None,
                    "clips": n})
    return out


@app.post("/api/streamers")
def add_streamer(b: dict = Depends(_body)):
    f = _streamer_fields(b)
    if not f.get("name"):
        raise HTTPException(400, "Give the streamer a name")
    if not f.get("channels"):
        raise HTTPException(400, "Add at least one Kick, Twitch, YouTube or TikTok link")
    s = db.insert("streamers", {"id": db.new_id("s"), "created": time.time(), "seen": [], "status": {},
                                "rights": "unknown", **f})
    db.log(f"Added streamer {s['name']} ({', '.join(s['channels'])})", streamer_id=s["id"])
    radar.check_now(s["id"])
    return s


@app.patch("/api/streamers/{sid}")
def edit_streamer(sid: str, b: dict = Depends(_body)):
    _need("streamers", sid)
    f = _streamer_fields(b)
    if f.get("auto_post") and (db.get("streamers", sid) or {}).get("rights", "unknown") == "unknown" \
            and f.get("rights", "unknown") == "unknown":
        raise HTTPException(400, "Auto-post needs permission on record. Set Rights first.")
    return db.update("streamers", sid, **f)


@app.delete("/api/streamers/{sid}")
def delete_streamer(sid: str):
    _need("streamers", sid)
    sess = live.active_for(sid)
    if sess:
        sess.stop()
    db.delete("streamers", sid)
    return {"ok": True}


@app.post("/api/streamers/{sid}/check")
def check_streamer(sid: str):
    _need("streamers", sid)
    radar.check_now(sid)
    return {"ok": True}


@app.post("/api/streamers/{sid}/record")
def record(sid: str, b: dict = Depends(_body)):
    s = _need("streamers", sid)
    url = b.get("url") or next(iter((s["channels"] or {}).values()), "")
    if live.active_for(sid):
        raise HTTPException(409, "Already recording")
    try:
        sess = live.start(s, url)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, str(e)[:200])
    return {"job_id": sess.job["id"]}


# ---------------------------------------------------------------- live

@app.post("/api/live/{jid}/clip")
def live_clip(jid: str, b: dict = Depends(_body)):
    s = live.get(jid)
    if not s or not s.running:
        raise HTTPException(404, "That recording isn't running")
    s.clip_last(float(b.get("seconds", 45)))
    return {"ok": True}


@app.post("/api/live/{jid}/stop")
def live_stop(jid: str):
    s = live.get(jid)
    if not s:
        raise HTTPException(404)
    s.stop()
    return {"ok": True}


# ---------------------------------------------------------------- accounts

def _acc_public(a: dict) -> dict:
    return {k: v for k, v in a.items() if k != "secrets"} | {"connected": bool(a["secrets"])}


@app.get("/api/accounts")
def accounts():
    return {"accounts": [_acc_public(a) for a in db.query("SELECT * FROM accounts ORDER BY platform, name")],
            "can": {"youtube": bool(platforms.google_client()[0]), "tiktok": bool(config.TIKTOK_CLIENT_KEY),
                    "instagram": True, "facebook": True},
            "limits": rules.PLATFORMS}


def _save_account(platform: str, res: dict) -> dict:
    a = db.insert("accounts", {"id": db.new_id("a"), "platform": platform, "name": res["name"],
                               "secrets": res["secrets"], "settings": {}, "created": time.time()})
    db.log(f"Connected {platform.title()}: {a['name']}")
    return _acc_public(a)


@app.post("/api/accounts/instagram")
def connect_ig(b: dict = Depends(_body)):
    try:
        res = platforms.ig_connect(str(b.get("token", "")).strip(), b.get("host") or "graph.instagram.com")
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"Instagram didn't accept that token: {str(e)[:200]}")
    return _save_account("instagram", res)


@app.post("/api/accounts/facebook")
def connect_fb(b: dict = Depends(_body)):
    try:
        res = platforms.fb_connect(str(b.get("page_id", "")).strip(), str(b.get("token", "")).strip())
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"Facebook didn't accept that page token: {str(e)[:200]}")
    return _save_account("facebook", res)


@app.post("/api/settings/google")
def save_google(b: dict = Depends(_body)):
    cid, sec = str(b.get("id", "")).strip(), str(b.get("secret", "")).strip()
    if cid and not cid.endswith(".apps.googleusercontent.com"):
        raise HTTPException(400, "That isn't a Google client ID. It ends in .apps.googleusercontent.com")
    if cid and not sec:
        raise HTTPException(400, "Paste the client secret too (it starts with GOCSPX-)")
    db.kv_set("google_client", {"id": cid, "secret": sec} if cid else {})
    return {"youtube": bool(platforms.google_client()[0]), "redirect": platforms.yt_redirect()}


@app.get("/oauth/youtube/start")
def yt_start():
    if not platforms.google_client()[0]:
        raise HTTPException(400, "Paste your Google client ID and secret in Accounts → YouTube first")
    st = secrets.token_urlsafe(16)
    _oauth_states[st] = time.time()
    return RedirectResponse(platforms.yt_auth_url(st))


@app.get("/oauth/youtube")
def yt_back(code: str = "", state: str = "", error: str = ""):
    if error or not code or _oauth_states.pop(state, 0) < time.time() - 900:
        return HTMLResponse("<p>YouTube connection cancelled or expired. Close this tab and try again.</p>", 400)
    try:
        _save_account("youtube", platforms.yt_connect(code))
    except Exception as e:  # noqa: BLE001
        return HTMLResponse(f"<p>YouTube connection failed: {str(e)[:200]}</p>", 400)
    return RedirectResponse("/#accounts")


@app.get("/api/accounts/tiktok/start")
def tt_start():
    if not config.TIKTOK_CLIENT_KEY or not config.TIKTOK_REDIRECT_URI:
        raise HTTPException(400, "Set TIKTOK_CLIENT_KEY, TIKTOK_CLIENT_SECRET and TIKTOK_REDIRECT_URI in .env first")
    st = secrets.token_urlsafe(16)
    _oauth_states[st] = time.time()
    return {"url": platforms.tt_auth_url(st)}


@app.post("/api/accounts/tiktok")
def tt_finish(b: dict = Depends(_body)):
    code = str(b.get("code", "")).strip()
    if code.startswith("http"):
        code = (parse_qs(urlparse(code).query).get("code") or [""])[0]
    if not code:
        raise HTTPException(400, "Paste the address TikTok sent you back to (it contains ?code=...)")
    try:
        return _save_account("tiktok", platforms.tt_connect(code))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"TikTok connection failed: {str(e)[:200]}")


@app.patch("/api/accounts/{aid}")
def edit_account(aid: str, b: dict = Depends(_body)):
    a = _need("accounts", aid)
    st = dict(a["settings"] or {})
    for k in ("gap_min", "daily_cap", "daily_target"):
        if k in b:
            st[k] = max(0, min(50, int(b[k] or 0)))
    if "privacy" in b:
        st["privacy"] = str(b["privacy"])[:40]
    return _acc_public(db.update("accounts", aid, settings=st))


@app.delete("/api/accounts/{aid}")
def delete_account(aid: str):
    _need("accounts", aid)
    db.delete("accounts", aid)
    return {"ok": True}


# ---------------------------------------------------------------- posting queue

@app.get("/api/posts")
def posts():
    out = []
    for p in db.query("SELECT * FROM posts ORDER BY due DESC LIMIT 200"):
        c = db.get("clips", p["clip_id"]) or {}
        a = db.get("accounts", p["account_id"]) or {}
        out.append({**p, "clip_title": c.get("title", ""), "platform": a.get("platform", ""),
                    "account": a.get("name", "")})
    return out


@app.post("/api/autopilot/run")
def autopilot_now():
    return {"scheduled": len(publish.autopilot_tick())}


@app.post("/api/posts/{pid}/{action}")
def post_action(pid: str, action: str):
    p = _need("posts", pid)
    if action == "cancel" and p["status"] in ("scheduled", "failed"):
        return db.update("posts", pid, status="cancelled")
    if action in ("retry", "now") and p["status"] in ("scheduled", "failed"):
        return db.update("posts", pid, status="scheduled", due=time.time(), tries=0 if action == "retry" else p["tries"])
    raise HTTPException(400, "Can't do that to this post now")


# ---------------------------------------------------------------- settings

@app.get("/api/settings")
def get_settings():
    return {"ai": ai.public_settings(), "guard_policy": db.kv_get("guard_policy", {}) or {},
            "risks": rules.RISK_POLICY, "rights": guard.RIGHTS,
            "clip": {"min": config.CLIP_MIN, "max": config.CLIP_MAX, "per_video": config.CLIPS_PER_VIDEO},
            "posting": {"gap_min": config.POST_GAP_MIN, "daily_cap": config.POST_DAILY_CAP,
                        "quiet_hours": config.QUIET_HOURS}}


@app.post("/api/settings/ai")
def save_ai(b: dict = Depends(_body)):
    cur = db.kv_get("ai", {}) or {}
    if b.get("provider") and b["provider"] not in ai.PROVIDERS:
        raise HTTPException(400, "Unknown AI service")
    for k in ("provider", "model", "base_url"):
        if k in b:
            cur[k] = str(b[k]).strip()
    if b.get("key"):
        cur["key"] = str(b["key"]).strip()
    db.kv_set("ai", cur)
    if b.get("test"):
        try:
            ai.ask_json("Reply with JSON.", 'Return {"ok": true}', max_tokens=20)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(400, f"Saved, but the test failed: {str(e)[:200]}")
    return ai.public_settings()


@app.post("/api/settings/ai/models")
def ai_models(b: dict = Depends(_body)):
    """List the models a key can use. Uses the key typed in the form, or the saved one."""
    saved = ai.settings()
    prov = b.get("provider") or saved["provider"]
    if prov not in ai.PROVIDERS:
        raise HTTPException(400, "Pick an AI service first")
    key = str(b.get("key") or (saved["key"] if prov == saved["provider"] else "")).strip()
    if not key and prov != "ollama":
        raise HTTPException(400, "Paste the API key first")
    try:
        return {"models": ai.list_models(prov, key, str(b.get("base_url") or ""))}
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"Couldn't get the model list: {str(e)[:200]}")


@app.post("/api/tools/{name}/install")
def install_tool(name: str):
    if name not in tools.PACKAGES:
        raise HTTPException(404, "Unknown add-on")
    return tools.install(name)


@app.get("/api/telegram")
def telegram_state():
    return telegram.public()


@app.post("/api/telegram")
def telegram_save(b: dict = Depends(_body)):
    try:
        if b.get("unpair"):
            return telegram.unpair()
        return telegram.set_token(str(b.get("token", "")))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"Telegram didn't accept that token: {str(e)[:200]}")


@app.post("/api/telegram/test")
def telegram_test():
    if not telegram.public()["paired"]:
        raise HTTPException(400, "Pair your Telegram first")
    telegram.say("👋 Test from Byditors Cut. Alerts are working.")
    return {"ok": True}


@app.post("/api/restart")
def restart_app():
    """Restart to load updates. start.bat/start.sh bring the app straight back (any exit code but 0 and 3)."""
    def bye():
        time.sleep(1)
        os._exit(75)
    threading.Thread(target=bye, daemon=True).start()
    return {"restarting": True}


@app.get("/api/settings/pc")
def pc_settings():
    return {"windows": winsys.IS_WINDOWS, "autostart": winsys.autostart_enabled(),
            "keep_awake": db.kv_get("keep_awake", True) is not False}


@app.post("/api/settings/pc")
def save_pc_settings(b: dict = Depends(_body)):
    if "keep_awake" in b:
        db.kv_set("keep_awake", bool(b["keep_awake"]))
    if "autostart" in b:
        try:
            winsys.set_autostart(bool(b["autostart"]))
        except Exception as e:  # noqa: BLE001
            raise HTTPException(400, f"Couldn't change start-with-Windows: {str(e)[:200]}")
    return pc_settings()


@app.post("/api/settings/keys")
def save_keys(b: dict = Depends(_body)):
    for name in tools.KEYS:
        if name in b:
            tools.set_key(name, str(b[name] or ""))
    return {k: bool(tools.key(k)) for k in tools.KEYS}


@app.post("/api/settings/guard")
def save_guard(b: dict = Depends(_body)):
    pol = {k: v for k, v in b.items() if k in rules.RISK_POLICY and v in ("warn", "review", "block")}
    for k in ("copyright_music", "sexual", "personal_info"):
        pol.pop(k, None)  # these stay blocked: no setting turns them off
    db.kv_set("guard_policy", pol)
    return pol


def start_background() -> None:
    live.recover()
    if config.NO_BACKGROUND:
        return
    pipeline.start()
    publish.start()
    radar.start()
    telegram.start()
