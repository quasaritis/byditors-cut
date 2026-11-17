"""One video in, finished and checked clips out.

source -> download -> listen (signals) -> transcribe -> pick moments -> reframe + render -> Guard -> Inbox / queue
"""
from __future__ import annotations

import queue
import shutil
import threading
import time
import traceback
from pathlib import Path

from . import brain, config, db, guard, media, reframe, render, signals, transcribe

_q: queue.Queue = queue.Queue()
_started = False


def retry_job(jid: str) -> dict:
    job = db.get("jobs", jid)
    db.update("jobs", jid, status="queued", step="Waiting", error="", progress=0)
    _q.put(jid)
    return db.get("jobs", jid) or job


def create_job(kind: str, url: str = "", file: str = "", streamer_id: str | None = None, title: str = "",
               meta: dict | None = None) -> dict:
    now = time.time()
    job = db.insert("jobs", {"id": db.new_id("j"), "kind": kind, "url": url, "title": title or url or file,
                             "streamer_id": streamer_id, "status": "queued", "step": "Waiting",
                             "meta": {**(meta or {}), "file": file}, "created": now, "updated": now})
    _q.put(job["id"])
    return job


def _targets(streamer: dict | None) -> list[str]:
    ids = (streamer or {}).get("post_to") or []
    plats = []
    for aid in ids:
        a = db.get("accounts", aid)
        if a and a["platform"] not in plats:
            plats.append(a["platform"])
    if not plats:
        plats = [a["platform"] for a in db.query("SELECT platform FROM accounts GROUP BY platform")]
    return plats or ["instagram", "youtube", "tiktok"]


def credit_for(streamer: dict | None, info: dict) -> str:
    if not config.CREDIT_OVERLAY:
        return ""
    if streamer and streamer.get("rights") == "own":
        return ""
    if streamer:
        return streamer.get("credit") or f"@{streamer['name']}"
    who = info.get("uploader") or info.get("channel") or ""
    return f"@{who}" if who else ""


def make_clip(job: dict, src: Path, probe: dict, info: dict, c: dict, words: list[dict], streamer: dict | None,
              cuts: list[float], log=print) -> dict:
    """Render one picked moment, run it through the Guard and store it. Shared by videos and lives."""
    cid = db.new_id("c")
    out_dir = config.DATA / "clips" / job["id"]
    out = out_dir / f"{cid}.mp4"
    plan = reframe.analyse(src, c["start"], c["end"], probe["width"], probe["height"], cuts,
                           (streamer or {}).get("layout", "auto"))
    cw = transcribe.words_between(words, c["start"], c["end"])
    render.render(src, out, c["start"], c["end"], plan, probe["width"], probe["height"], cw,
                  hook=c.get("hook", ""), credit=credit_for(streamer, info), has_audio=probe["has_audio"])
    thumb = render.thumbnail(out, out_dir / f"{cid}.jpg", at=min(1.5, (c["end"] - c["start"]) / 3))
    text = " ".join(w["w"] for w in cw)
    clip = {"id": cid, "job_id": job["id"], "streamer_id": job.get("streamer_id"), "title": c.get("title", ""),
            "hook": c.get("hook", ""), "start": c["start"], "end": c["end"], "score": c.get("score", 0),
            "why": c.get("why", ""), "file": str(out), "thumb": str(thumb),
            "captions": brain.captions(c, streamer, info.get("webpage_url") or job.get("url", ""), text),
            "guard": {}, "verdict": "pending", "state": "new", "created": time.time()}
    report = guard.review({**clip, "file": str(out)}, streamer, info, text, _targets(streamer), c.get("flags"))
    report["layout"] = plan.get("mode")
    report["edit"] = _edit_info(src, c, probe, cw, credit_for(streamer, info), plan)
    report["text"] = text[:4000]
    report["ai_flags"] = c.get("flags") or []
    clip["guard"] = report
    clip["verdict"] = report["verdict"]
    saved = db.insert("clips", clip)
    if streamer and streamer.get("auto_post") and report["auto_post_allowed"]:
        # accounts with a daily target are filled by the autopilot instead (spread out, best clips first)
        direct = [a for a in (streamer.get("post_to") or [])
                  if not int(((db.get("accounts", a) or {}).get("settings") or {}).get("daily_target") or 0)]
        if direct:
            from . import publish
            publish.enqueue(saved["id"], direct)
    log(f"clip ready: {clip['title'] or cid} → {report['verdict'].upper()} ({plan.get('mode')})")
    try:
        from . import telegram

        telegram.clip_ready(db.get("clips", saved["id"]) or saved)
    except Exception:  # noqa: BLE001
        pass
    return saved


def _edit_info(src: Path, c: dict, probe: dict, words: list[dict], credit: str, plan: dict) -> dict:
    """What it takes to render the clip again in another layout (Inbox → Layout)."""
    return {"src": str(src), "start": c["start"], "end": c["end"], "w": probe["width"], "h": probe["height"],
            "has_audio": probe["has_audio"], "hook": c.get("hook", ""), "credit": credit,
            "words": [{"w": w["w"], "s": round(w["s"], 2), "e": round(w["e"], 2)} for w in words][:600],
            "auto": plan.get("mode")}


def relayout(clip_id: str, layout: str) -> dict:
    """Render a clip again in the layout you picked (or let it decide again with "auto")."""
    clip = db.get("clips", clip_id)
    g = dict(clip["guard"] or {})
    ed = g.get("edit") or {}
    src = Path(ed.get("src") or "")
    if not ed or not src.exists():
        raise RuntimeError("The original video for this clip is gone (lives are kept for a day), so it can't be "
                           "rendered again.")
    want = reframe.ALIASES.get(layout, layout)
    if want not in reframe.LAYOUTS:
        raise RuntimeError(f"Unknown layout: {layout}")
    cuts = media.scene_cuts(src, start=ed["start"], max_seconds=ed["end"] - ed["start"])
    plan = reframe.analyse(src, ed["start"], ed["end"], ed["w"], ed["h"], cuts, want)
    out = Path(clip["file"])
    tmp = out.with_name(out.stem + ".relayout.mp4")
    render.render(src, tmp, ed["start"], ed["end"], plan, ed["w"], ed["h"], ed.get("words") or [],
                  hook=ed.get("hook", ""), credit=ed.get("credit", ""), has_audio=ed.get("has_audio", True))
    shutil.move(str(tmp), out)
    out.with_name(out.stem + "_tg.mp4").unlink(missing_ok=True)     # phone preview is stale now
    render.thumbnail(out, Path(clip["thumb"]), at=min(1.5, (ed["end"] - ed["start"]) / 3))
    g["layout"] = plan.get("mode")
    g["layout_choice"] = want
    return db.update("clips", clip_id, guard=g)


def run_job(jid: str) -> None:
    job = db.get("jobs", jid)
    if not job:
        return
    streamer = db.get("streamers", job["streamer_id"]) if job.get("streamer_id") else None

    def step(text, progress=None):
        fields = {"step": text[:200]}
        if progress is not None:
            fields["progress"] = progress
        db.update("jobs", jid, **fields)

    try:
        db.update("jobs", jid, status="working")
        work = config.DATA / "sources" / jid
        if job["meta"].get("file"):
            src = Path(job["meta"]["file"])
            info = {"title": job["title"], "uploaded_by_you": True, "webpage_url": ""}
        elif job["meta"].get("source") and Path(job["meta"]["source"]).exists() and job["meta"].get("info"):
            src, info = Path(job["meta"]["source"]), dict(job["meta"]["info"])   # retry: already downloaded
        else:
            step("Downloading", 0.05)
            src, info = media.download(job["url"], work, log=lambda m: step(m))
            db.update("jobs", jid, title=info.get("title") or job["title"],
                      meta={**job["meta"], "source": str(src), "info": info})
            job = db.get("jobs", jid)
        if job["meta"].get("rights"):
            info["rights"] = job["meta"]["rights"]           # what you said about this link when you pasted it
        pr = media.probe(src)
        info["duration"] = info.get("duration") or pr["duration"]
        step("Listening for the big moments", 0.2)
        cuts = media.scene_cuts(src)
        sig = signals.from_file(src, pr["duration"]) if pr["has_audio"] else \
            signals.combined(__import__("numpy").zeros(0, dtype="float32"), cuts)
        step("Transcribing", 0.3)
        tr = {"words": [], "language": ""}
        if pr["has_audio"]:
            try:
                tr = transcribe.transcribe_long(src, pr["duration"], (streamer or {}).get("language", ""),
                                                log=lambda m: step(m))
            except Exception as e:  # noqa: BLE001
                # Captions are a bonus: without them clips are still cut from sound, picture and chat.
                db.log(f"Speech to text failed, clipping without captions: {str(e)[:200]}", "warn",
                       job.get("streamer_id"))
        step("Picking clips", 0.55)
        picks = brain.find_clips(tr["words"], sig, pr["duration"], info.get("title", ""),
                                 (streamer or {}).get("name") or info.get("uploader", ""), log=lambda m: step(m))
        meta = {**job["meta"], "language": tr.get("language"), "duration": pr["duration"],
                "excite": brain.excite_preview(sig), "source": str(src)}
        db.update("jobs", jid, meta=meta)
        for i, c in enumerate(picks):
            step(f"Rendering clip {i + 1} of {len(picks)} + safety check", 0.6 + 0.38 * i / max(1, len(picks)))
            try:
                make_clip(job, src, pr, info, c, tr["words"], streamer, cuts, log=lambda m: step(m))
            except Exception as e:  # noqa: BLE001
                db.log(f"Clip {i + 1} failed: {str(e)[:300]}", "error", job.get("streamer_id"))
        db.update("jobs", jid, status="done", step=f"{len(picks)} clips", progress=1.0)
        db.log(f"Finished {info.get('title') or job['url']}: {len(picks)} clips", streamer_id=job.get("streamer_id"))
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        db.update("jobs", jid, status="failed", error=str(e)[:600])
        db.log(f"Job failed: {str(e)[:300]}", "error", job.get("streamer_id"))


def strip_music(clip_id: str) -> dict:
    """Remove music from a finished clip (Inbox button), then check it again."""
    clip = db.get("clips", clip_id)
    src = Path(clip["file"])
    tmp = src.with_suffix(".nomusic.mp4")
    how = guard.music.strip_music(src, tmp, [tuple(r) for r in (clip["guard"].get("music") or {}).get("ranges", [])])
    shutil.move(tmp, src)
    job = db.get("jobs", clip["job_id"]) or {}
    streamer = db.get("streamers", clip["streamer_id"]) if clip.get("streamer_id") else None
    info = {"duration": (job.get("meta") or {}).get("duration"), "uploaded_by_you": bool((job.get("meta") or {}).get("file"))}
    report = guard.review(clip, streamer, info, clip["guard"].get("text", ""), _targets(streamer),
                          clip["guard"].get("ai_flags"))
    report["text"] = clip["guard"].get("text", "")
    for k in ("layout", "layout_choice", "edit"):
        if k in clip["guard"]:
            report[k] = clip["guard"][k]
    report["music_fix"] = how
    if how == "separated":
        # the song is gone; a fingerprint hit from before no longer applies
        report["checks"] = [c if c["id"] != "music" else {**c, "status": "ok", "detail": "Music removed (voice kept)."}
                            for c in report["checks"]]
        st = [c["status"] for c in report["checks"]]
        report["verdict"] = "block" if "block" in st else ("review" if "review" in st else "pass")
    return db.update("clips", clip_id, guard=report, verdict=report["verdict"])


def _worker():
    while True:
        jid = _q.get()
        try:
            run_job(jid)
        finally:
            _q.task_done()


def start() -> None:
    global _started
    if _started:
        return
    _started = True
    for j in db.query("SELECT id FROM jobs WHERE status IN ('queued','working') AND kind != 'live'"):
        db.update("jobs", j["id"], status="queued")
        _q.put(j["id"])
    threading.Thread(target=_worker, daemon=True, name="pipeline").start()
