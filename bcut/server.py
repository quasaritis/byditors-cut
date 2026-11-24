"""The local web server: the dashboard at http://127.0.0.1:8777 and the small API it talks to.

It listens on this computer only, answers only to its own address and refuses changes sent from other
websites, so a page you visit can't drive it.
"""
from __future__ import annotations

import json
import secrets
import shutil
import os
import threading
import subprocess
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse

from . import ai, brain, campaigns, config, db, fx, guard, live, media, pipeline, publish, radar, reframe, styles, telegram, tools, transcribe, winsys
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
            "guard": {k: v for k, v in g.items() if k not in ("text", "edit")},
            "can_relayout": bool((g.get("edit") or {}).get("src")) and Path((g.get("edit") or {}).get("src")).exists(),
            "window": _window(g.get("edit") or {}),
            "transcript": g.get("text", "")[:1500],
            "streamer": (db.get("streamers", c["streamer_id"]) or {}).get("name") if c.get("streamer_id") else None}


def _window(ed: dict) -> dict | None:
    if not ed.get("src"):
        return None
    w0 = max(0.0, float(ed["start"]) - 15)
    return {"from": w0, "to": float(ed["end"]) + 15, "start": ed["start"], "end": ed["end"], "hook": ed.get("hook", "")}


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
    if sid and not db.get("streamers", str(sid)):
        sid = None
    rights = b.get("rights") if b.get("rights") in ("own", "permission", "unknown") else "unknown"
    caps = b.get("captions") if b.get("captions") in brain.LANGS else "auto"
    camp = b.get("campaign_id") if b.get("campaign_id") and db.get("campaigns", str(b["campaign_id"])) else ""
    if camp and rights == "unknown":
        rights = "permission"           # joining a paid clipping campaign is the creator's permission
    if not sid:  # tie the link to a streamer you already added, so their rights + credit apply
        for s in db.query("SELECT * FROM streamers"):
            if any(radar.handle(u).lower().lstrip("@") == radar.handle(url).lower().lstrip("@")
                   and media.platform_of(u) == media.platform_of(url) for u in (s["channels"] or {}).values()):
                sid = s["id"]
                break
    return pipeline.create_job("video", url=url, streamer_id=sid,
                               meta={"rights": rights, "captions": caps, "campaign_id": camp,
                                     "style_id": _style_id(b.get("style_id")), "options": _clip_options(b)})


def _style_id(v) -> str:
    v = str(v or "")
    return v if v and v in {x["id"] for x in styles.all_styles()} else ""


def _clip_options(b: dict) -> dict:
    """The "Make clips" options: how many clips, shortest and longest (seconds), and which part of the
    video (from/to in seconds). Missing or nonsense values fall back to the usual ones."""
    def num(k, lo, hi):
        try:
            v = float(b.get(k))
        except (TypeError, ValueError):
            return None
        return None if v != v else min(hi, max(lo, v))
    out = {}
    c = num("count", 1, 30)
    if c:
        out["count"] = int(c)
    a, z = num("min_len", 5, 170), num("max_len", 8, 180)
    if a:
        out["min_len"] = int(a)
    if z:
        out["max_len"] = int(z)
    if a and z and a > z:
        out["min_len"], out["max_len"] = int(z), int(a)
    f, t = num("from", 0, 86400), num("to", 0, 86400)
    if f:
        out["from"] = f
    if t and t > (f or 0) + 10:
        out["to"] = t
    return out


@app.post("/api/upload")
async def upload(file: UploadFile = File(...), options: str = Form("")):
    name = Path(file.filename or "video.mp4").name
    if Path(name).suffix.lower() not in (".mp4", ".mov", ".mkv", ".webm", ".m4v", ".ts", ".avi"):
        raise HTTPException(400, "That isn't a video file this app can read")
    dest = config.DATA / "sources" / f"up_{int(time.time())}_{secrets.token_hex(3)}" / name
    dest.parent.mkdir(parents=True, exist_ok=True)
    with dest.open("wb") as f:
        shutil.copyfileobj(file.file, f, length=4 * 2**20)
    try:
        b = json.loads(options) if options else {}
    except ValueError:
        b = {}
    b = b if isinstance(b, dict) else {}
    sid = b.get("streamer_id") if b.get("streamer_id") and db.get("streamers", str(b["streamer_id"])) else None
    return pipeline.create_job("video", file=str(dest), title=name, streamer_id=sid,
                               meta={"captions": b.get("captions") if b.get("captions") in brain.LANGS else "auto",
                                     "style_id": _style_id(b.get("style_id")), "options": _clip_options(b)})


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


@app.post("/api/clips/{cid}/layout")
def clip_layout(cid: str, b: dict = Depends(_body)):
    """Render the clip again in another layout (facecam split, follow the face, two people, whole frame...)."""
    _need("clips", cid)
    try:
        return _clip_public(pipeline.relayout(cid, str(b.get("layout") or "auto")))
    except RuntimeError as e:
        raise HTTPException(400, str(e)) from e


@app.post("/api/clips/{cid}/edit")
def clip_edit(cid: str, b: dict = Depends(_body)):
    """The trim editor: new start/end (seconds in the original video), hook, title and layout, then render."""
    _need("clips", cid)
    num = lambda k: float(b[k]) if b.get(k) not in (None, "") else None  # noqa: E731
    try:
        return _clip_public(pipeline.rerender(cid, layout=b.get("layout") or None, start=num("start"), end=num("end"),
                                              hook=b.get("hook"), title=b.get("title"),
                                              style_id=_style_id(b.get("style_id")) or None))
    except (RuntimeError, ValueError) as e:
        raise HTTPException(400, str(e)) from e


@app.get("/media/source/{cid}.mp4")
def clip_source(cid: str):
    """A light preview of the original around the clip (15 s either side), for the trim editor."""
    c = _need("clips", cid)
    ed = (c.get("guard") or {}).get("edit") or {}
    src = Path(ed.get("src") or "")
    if not src.exists():
        raise HTTPException(404, "The original video is gone")
    w0 = max(0.0, float(ed["start"]) - 15)
    w1 = float(ed["end"]) + 15
    out = Path(c["file"]).with_name(f"{cid}_src_{int(w0 * 10)}_{int(w1 * 10)}.mp4")
    if not out.exists():
        for old in Path(c["file"]).parent.glob(f"{cid}_src_*.mp4"):
            old.unlink(missing_ok=True)
        subprocess.run([media.need("ffmpeg"), "-v", "error", "-y", "-ss", f"{w0:.3f}", "-t", f"{w1 - w0:.3f}",
                        "-i", str(src), "-vf", "scale=-2:360", "-c:v", "libx264", "-preset", "ultrafast", "-crf", "30",
                        "-c:a", "aac", "-b:a", "96k", "-movflags", "+faststart", str(out)], check=False)
    if not out.exists():
        raise HTTPException(500, "Couldn't make the preview")
    return FileResponse(out, media_type="video/mp4")


@app.get("/api/learn")
def learned():
    from . import learn

    rs = learn.results()
    times = {}
    for a in db.query("SELECT id, platform, name FROM accounts"):
        st = learn.hour_stats(a["id"])
        n = sum(v["n"] for v in st.values())
        best = max((h for h in st if st[h]["n"] >= 1), key=lambda h: st[h]["lift"], default=None)
        times[a["id"]] = {"name": a["name"], "platform": a["platform"], "posts": n,
                          "best": best, "lift": st[best]["lift"] if best is not None else None,
                          "learning": n >= 8}
    return {"posts": len(rs), "styles": learn.style_stats(), "labels": learn.STYLES, "top_hooks": learn.top_hooks(),
            "views": sum(r["views"] for r in rs), "times": times}


@app.post("/api/learn/refresh")
def learn_refresh():
    from . import learn

    return {"updated": learn.refresh()}


# ---------------------------------------------------------------- campaigns

def _campaign_public(c: dict) -> dict:
    return {**c, "rules": campaigns.rules_of(c), "stats": campaigns.stats(c)}


@app.get("/api/campaigns")
def list_campaigns():
    return [_campaign_public(c) for c in db.query("SELECT * FROM campaigns ORDER BY created DESC")]


def _campaign_fields(b: dict) -> dict:
    out = {}
    for k, n in (("name", 80), ("site", 60), ("url", 500), ("brief", 8000)):
        if k in b:
            out[k] = str(b[k] or "").strip()[:n]
    if "url" in out and out["url"] and not out["url"].startswith(("http://", "https://")):
        out["url"] = "https://" + out["url"]
    if "rules" in b and isinstance(b["rules"], dict):
        out["rules"] = campaigns.clean_rules(b["rules"])
    if "streamers" in b and isinstance(b["streamers"], list):
        out["streamers"] = [x for x in b["streamers"] if db.get("streamers", str(x))]
    if b.get("status") in ("active", "paused", "ended"):
        out["status"] = b["status"]
    if "ends" in b:
        try:
            out["ends"] = float(b["ends"] or 0)
        except (TypeError, ValueError):
            out["ends"] = 0.0
    return out


@app.post("/api/campaigns")
def add_campaign(b: dict = Depends(_body)):
    f = _campaign_fields(b)
    if not f.get("name"):
        raise HTTPException(400, "Give the campaign a name")
    c = db.insert("campaigns", {"id": db.new_id("k"), "created": time.time(), "rules": campaigns.clean_rules({}),
                                "streamers": [], **f})
    db.log(f"Joined campaign {c['name']}")
    return _campaign_public(c)


@app.patch("/api/campaigns/{kid}")
def edit_campaign(kid: str, b: dict = Depends(_body)):
    _need("campaigns", kid)
    return _campaign_public(db.update("campaigns", kid, **_campaign_fields(b)))


@app.delete("/api/campaigns/{kid}")
def delete_campaign(kid: str):
    _need("campaigns", kid)
    db.delete("campaigns", kid)
    return {"ok": True}


@app.post("/api/campaigns/read-brief")
def read_brief(b: dict = Depends(_body)):
    """Fill a campaign's rules from its pasted brief (the AI reads it)."""
    text = str(b.get("brief") or "").strip()
    if len(text) < 20:
        raise HTTPException(400, "Paste the campaign's brief first")
    try:
        got = campaigns.parse_brief(text)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"Couldn't read the brief: {str(e)[:200]}") from e
    # creators named in the brief that you already follow
    names = [n.lower().strip() for n in got.pop("creators", []) if len(n.strip()) > 2]
    got["streamers"] = [s["id"] for s in db.query("SELECT id, name, channels_json FROM streamers")
                        if any(s["name"].lower() in n or n in s["name"].lower() or
                               any(n in str(u).lower() for u in (s.get("channels") or {}).values()) for n in names)]
    return got


@app.get("/api/edit-style")
def edit_style():
    return {"style": fx.style(), "labels": fx.LABELS}


@app.post("/api/edit-style")
def save_edit_style(b: dict = Depends(_body)):
    return {"style": fx.set_style(b), "labels": fx.LABELS}


# ---------------------------------------------------------------- styles (how clips look)

def _font_info() -> dict:
    """Per caption font: its file (for the dashboard's live preview), its size at 100% and how big its em is
    next to the full height libass sizes by."""
    from . import render

    out = {}
    for name in styles.FONTS:
        m = render._advances(name) or {}
        if m.get("file"):
            out[name] = {"file": m["file"], "base": render._metrics(name)[0],
                         "em": round(m.get("upm", 1000) / max(1, m.get("win", 1000)), 4),
                         "cap": round((m.get("cap") or 0) / max(1, m.get("win", 1000)), 4)}
    return out


@app.get("/api/styles")
def list_styles():
    return {"styles": styles.all_styles(), "default": styles.default_id_(), "options": styles.options(),
            "languages": brain.LANGS, "fonts": _font_info()}


@app.get("/fonts/{name}")
def font_file(name: str):
    f = config.ROOT / "bcut" / "fonts" / Path(name).name
    if f.suffix.lower() != ".ttf" or not f.exists():
        raise HTTPException(404, "Not found")
    return FileResponse(f, media_type="font/ttf", headers={"Cache-Control": "max-age=86400"})


@app.post("/api/styles")
def add_style(b: dict = Depends(_body)):
    return styles.save(b)


@app.patch("/api/styles/{sid}")
def edit_style_saved(sid: str, b: dict = Depends(_body)):
    if sid.startswith("t_"):
        raise HTTPException(400, "Templates can't be changed. Make a copy first.")
    try:
        return styles.save(b, sid)
    except KeyError as e:
        raise HTTPException(404, "Style not found") from e


@app.post("/api/styles/{sid}/copy")
def copy_style(sid: str):
    return styles.copy_of(sid)


@app.post("/api/styles/{sid}/default")
def default_style(sid: str):
    try:
        return {"default": styles.set_default(sid)}
    except KeyError as e:
        raise HTTPException(404, "Style not found") from e


@app.delete("/api/styles/{sid}")
def delete_style(sid: str):
    if sid.startswith("t_"):
        raise HTTPException(400, "Templates can't be removed")
    styles.delete(sid)
    for st in db.query("SELECT id FROM streamers WHERE style_id=?", (sid,)):
        db.update("streamers", st["id"], style_id="")
    return {"ok": True}


def _sample_clip() -> dict | None:
    """The newest clip whose original video is still here: the Styles screen previews on it."""
    for c in db.query("SELECT * FROM clips ORDER BY created DESC LIMIT 40"):
        ed = (c.get("guard") or {}).get("edit") or {}
        if ed.get("src") and Path(ed["src"]).exists():
            return {"clip": c, "edit": ed}
    return None


@app.get("/api/styles/sample")
def style_sample():
    got = _sample_clip()
    if not got:
        return {"clip_id": None}
    ed, c = got["edit"], got["clip"]
    mid = (float(ed["start"]) + float(ed["end"])) / 2
    words = [w for w in ed.get("words") or [] if mid - 1 <= w["s"] <= mid + 3][:6]
    made = (c.get("guard") or {}).get("made") or {}
    return {"clip_id": c["id"], "w": ed.get("w"), "h": ed.get("h"), "at": round(mid, 2), "hook": ed.get("hook", ""),
            "words": [w["w"] for w in words], "title": c.get("title", ""), "cams": made.get("cams") or []}


@app.get("/media/style-frame.jpg")
def style_frame(at: float = -1):
    """A still from the sample clip's original video (wide), for drawing the cam box and the part to show."""
    got = _sample_clip()
    if not got:
        raise HTTPException(404, "No video yet")
    ed = got["edit"]
    t = at if at >= 0 else (float(ed["start"]) + float(ed["end"])) / 2
    out = config.DATA / "tmp" / "style_frame.jpg"
    out.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([media.need("ffmpeg"), "-v", "error", "-y", "-ss", f"{t:.2f}", "-i", ed["src"], "-frames:v", "1",
                    "-vf", "scale=960:-2", "-q:v", "4", str(out)], check=False, timeout=60)
    if not out.exists():
        raise HTTPException(404, "Couldn't read a frame")
    return FileResponse(out, media_type="image/jpeg", headers={"Cache-Control": "no-store"})


_preview_lock = threading.Lock()


@app.post("/api/styles/preview")
def style_preview(b: dict = Depends(_body)):
    """Render 7 seconds of the sample clip in the style being edited (saved or not), to see it for real."""
    got = _sample_clip()
    if not got:
        raise HTTPException(400, "Make a clip first: the preview uses your newest clip's video.")
    if not _preview_lock.acquire(blocking=False):
        raise HTTPException(409, "A preview is already rendering")
    try:
        ed = got["edit"]
        s0 = float(ed["start"])
        s1 = min(float(ed["end"]), s0 + 7)
        words = [w for w in ed.get("words") or [] if s0 - 0.05 <= w["s"] <= s1]
        out = config.DATA / "tmp" / "style_preview.mp4"
        quick = {k: False for k in fx.DEFAULTS}      # the look only: no cuts, zooms or sounds
        pipeline.produce(Path(ed["src"]), out, s0, s1, ed["w"], ed["h"], ed.get("has_audio", True), words,
                         ed.get("hook", "") or "This is your heading", ed.get("credit", ""), "auto", st=quick,
                         look={**styles.clean(b), "id": ""})
        return {"url": f"/media/style-preview.mp4?v={int(time.time())}"}
    except RuntimeError as e:
        raise HTTPException(400, str(e)) from e
    finally:
        _preview_lock.release()


@app.get("/media/style-preview.mp4")
def style_preview_file():
    out = config.DATA / "tmp" / "style_preview.mp4"
    if not out.exists():
        raise HTTPException(404, "No preview yet")
    return FileResponse(out, media_type="video/mp4", headers={"Cache-Control": "no-store"})


@app.get("/api/layouts")
def layouts():
    return {"layouts": [{"id": k, "label": v} for k, v in reframe.LAYOUTS.items()]}


@app.post("/api/clips/{cid}/recheck")
def recheck(cid: str):
    c = _need("clips", cid)
    s = db.get("streamers", c["streamer_id"]) if c.get("streamer_id") else None
    j = db.get("jobs", c["job_id"]) or {}
    info = {"duration": (j.get("meta") or {}).get("duration"), "uploaded_by_you": bool((j.get("meta") or {}).get("file"))}
    old = c["guard"] or {}
    rep = guard.review(c, s, info, old.get("text", ""), pipeline._targets(s), old.get("ai_flags"))
    rep.update({k: old[k] for k in ("text", "ai_flags", "layout", "layout_choice", "edit") if k in old})
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
    if "captions" in b:
        out["captions"] = b["captions"] if b["captions"] in brain.LANGS else "auto"
    if "style_id" in b:
        out["style_id"] = _style_id(b["style_id"])
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


IDENTITY = {"youtube": "channel_id", "instagram": "user_id", "facebook": "page_id", "tiktok": "open_id"}


def _save_account(platform: str, res: dict) -> dict:
    """Any number of accounts per platform. Connecting one that's already here refreshes its login and keeps
    its schedule and autopilot settings instead of adding a duplicate."""
    key = IDENTITY.get(platform)
    ident = (res["secrets"] or {}).get(key) if key else None
    if ident:
        for old in db.query("SELECT * FROM accounts WHERE platform=?", (platform,)):
            if (old.get("secrets") or {}).get(key) == ident:
                db.update("accounts", old["id"], name=res["name"], secrets=res["secrets"])
                db.log(f"Reconnected {platform.title()}: {res['name']}")
                return _acc_public(db.get("accounts", old["id"]))
    name = res["name"]
    same = [x["name"] for x in db.query("SELECT name FROM accounts WHERE platform=?", (platform,))]
    if name in same:  # e.g. Google didn't share the channel name: keep the names apart
        name = f"{name} {len(same) + 1}"
    a = db.insert("accounts", {"id": db.new_id("a"), "platform": platform, "name": name,
                               "secrets": res["secrets"], "settings": {}, "created": time.time()})
    db.log(f"Connected {platform.title()}: {a['name']}")
    return _acc_public(a)


@app.post("/api/accounts/instagram")
def connect_ig(b: dict = Depends(_body)):
    try:
        res = platforms.ig_connect(str(b.get("token", "")).strip(), b.get("host") or "", str(b.get("username") or ""))
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
            ai.ask_json("Reply with JSON.", 'Return {"ok": true}', max_tokens=400)   # room for reasoning models
        except Exception as e:  # noqa: BLE001
            raise HTTPException(400, f"Saved, but the test failed: {str(e)[:200]}")
    return ai.public_settings()


@app.post("/api/settings/ai/backup")
def save_ai_backup(b: dict = Depends(_body)):
    """The backup AI: takes over while the main one's free daily limit is used up."""
    cur = db.kv_get("ai_backup", {}) or {}
    if b.get("provider") and b["provider"] not in ai.PROVIDERS:
        raise HTTPException(400, "Unknown AI service")
    for k in ("provider", "model", "base_url"):
        if k in b:
            cur[k] = str(b[k]).strip()
    if b.get("key"):
        cur["key"] = str(b["key"]).strip()
    if not cur.get("provider"):
        cur = {}
    db.kv_set("ai_backup", cur)
    if cur and b.get("test"):
        try:
            ai.test(ai.backup())
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
