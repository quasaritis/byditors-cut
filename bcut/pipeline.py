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

from . import brain, campaigns, config, db, fx, guard, media, reframe, render, signals, styles, transcribe

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


def produce(src: Path, out: Path, start: float, end: float, w: int, h: int, has_audio: bool, words: list[dict],
            hook: str, credit: str, layout: str = "auto", st: dict | None = None, log=print,
            callout: str = "", look: dict | None = None) -> dict:
    """Edit and render one clip: cut dead air, cold open, layout, punch-ins, sound, captions, loop.
    Every render (new clips, a layout change, a trim from the Inbox) goes through here. `look` is the saved
    style (captions, heading, layout; bcut.styles), the default one when not given."""
    st = st or fx.style()
    look = look or styles.get(None)
    edit = fx.plan(src, start, end, words, has_audio, st)
    plain = (not edit.get("cold") and len(edit["pieces"]) == 1 and not st["loop"] and
             abs(edit["pieces"][0][0] - start) < 0.05 and abs(edit["pieces"][0][1] - end) < 0.05)
    work = out.with_name(out.stem + ".cut.mp4")
    try:
        if plain:
            body, b0, b1, ws, built = src, start, end, words, {"dur": end - start, "rewind": None, "joins": []}
        else:
            built = fx.build(src, edit, work, has_audio, loop=st["loop"])
            body, b0, b1 = work, 0.0, built["dur"]
            ws = fx.map_words([x for x in words if start - 0.05 <= x["s"] <= end + 0.05], built["timeline"])
        cuts = media.scene_cuts(body, start=b0, max_seconds=b1 - b0) + [b0 + j for j in built["joins"]]
        plan = reframe.analyse(body, b0, b1, w, h, sorted(cuts), layout, opts=look.get("layout"))
        ev = fx.events(body, b0, b1 - b0, built, st, has_audio)
        if built.get("freeze"):
            ev["callout"] = {"text": callout or fx.default_callout(hook), "at": built["freeze"]}
        render.render(body, out, b0, b1, plan, w, h, ws, hook=hook, credit=credit, has_audio=has_audio, fx=ev,
                      grade=st.get("original", False), style=look)
    finally:
        work.unlink(missing_ok=True)
    plan_info = {"mode": plan.get("mode"), "burned": bool(plan.get("burned")),
                 "removed": edit.get("removed", 0), "cold": bool(edit.get("cold")),
                 "punches": len(ev.get("punches") or []), "speakers": plan.get("speakers"),
                 "cams": [c.get("box") for c in plan.get("cams") or []]}
    return {"plan": plan, "edit": edit, "fx": ev, "info": plan_info, "dur": round(b1 - b0, 2)}


def caption_lang(job: dict, streamer: dict | None) -> str:
    """"auto" (as spoken: Hinglish stays Hinglish) or a language the captions, hook and title are translated
    into ("english", "spanish", "hindi", ... see brain.LANGS)."""
    pick = (job.get("meta") or {}).get("captions") or "auto"
    if pick == "auto":
        pick = (streamer or {}).get("captions") or "auto"
    return pick if pick in brain.TRANSLATE else "auto"


def make_clip(job: dict, src: Path, probe: dict, info: dict, c: dict, words: list[dict], streamer: dict | None,
              cuts: list[float], log=print) -> dict:
    """Render one picked moment, run it through the Guard and store it. Shared by videos and lives."""
    cid = db.new_id("c")
    out_dir = config.DATA / "clips" / job["id"]
    out = out_dir / f"{cid}.mp4"
    out_dir.mkdir(parents=True, exist_ok=True)
    camp = campaigns.for_job(job, streamer)
    lang = caption_lang(job, streamer)
    # the clip plus 15 s either side (for the trim editor), with Hindi/Hinglish spelling fixed once
    near = brain.fix_spellings(transcribe.words_between(words, c["start"] - 15, c["end"] + 15), log)
    spoken = " ".join(w["w"] for w in transcribe.words_between(near, c["start"], c["end"]))
    if lang != "auto":
        near = brain.translate(near, lang, log)
    cw = transcribe.words_between(near, c["start"], c["end"])
    credit = (campaigns.rules_of(camp)["credit"] if camp else "") or credit_for(streamer, info)
    look = styles.for_job(job, streamer)
    made = produce(src, out, c["start"], c["end"], probe["width"], probe["height"], probe["has_audio"], cw,
                   c.get("hook", ""), credit, (streamer or {}).get("layout", "auto"), log=log,
                   callout=c.get("callout", ""), look=look)
    plan = made["plan"]
    thumb = cover(out, out_dir / f"{cid}.jpg", made)
    text = spoken
    clip = {"id": cid, "job_id": job["id"], "streamer_id": job.get("streamer_id"), "title": c.get("title", ""),
            "hook": c.get("hook", ""), "start": c["start"], "end": c["end"], "score": c.get("score", 0),
            "why": c.get("why", ""), "file": str(out), "thumb": str(thumb),
            "captions": campaigns.apply_captions(
                brain.captions(c, streamer, info.get("webpage_url") or job.get("url", ""), text, lang), camp),
            "guard": {}, "verdict": "pending", "state": "new", "created": time.time()}
    src_info = {**info, "campaign": f"{camp.get('name') or 'campaign'} ({camp.get('url') or camp.get('site') or ''})"
                .replace(" ()", "")} if camp else info
    report = guard.review({**clip, "file": str(out)}, streamer, src_info, text, _targets(streamer), c.get("flags"))
    if camp:
        chk = campaigns.check(camp, made["dur"], text, clip["captions"])
        report["checks"].append(chk)
        if chk["status"] == "review" and report["verdict"] == "pass":
            report["verdict"], report["auto_post_allowed"] = "review", False
        report["campaign"] = {"id": camp["id"], "name": camp.get("name", "")}
    report["captions_lang"] = lang
    report["style_id"] = look.get("id") or ""
    report["style_name"] = look.get("name") or ""
    report["callout"] = c.get("callout", "")
    report["layout"] = plan.get("mode")
    report["edit"] = _edit_info(src, c, probe, near,
                                credit, plan)
    report["made"] = made["info"]
    report["cover_at"] = made.get("cover_at")
    report["hook_style"] = c.get("hook_style", "")
    report["text"] = text[:4000]
    report["ai_flags"] = c.get("flags") or []
    report["own_captions"] = bool(plan.get("burned"))
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
    mi = made["info"]
    extras = [plan.get("mode")]
    if mi["removed"] > 0.5:
        extras.append(f"{mi['removed']:.1f}s of dead air cut")
    if mi["cold"]:
        extras.append("cold open")
    if mi["punches"]:
        extras.append(f"{mi['punches']} punch-in{'s' if mi['punches'] != 1 else ''}")
    if plan.get("burned"):
        extras.append("kept the video's own subtitles")
    log(f"clip ready: {clip['title'] or cid} → {report['verdict'].upper()} ({', '.join(extras)})")
    try:
        from . import telegram

        telegram.clip_ready(db.get("clips", saved["id"]) or saved)
    except Exception:  # noqa: BLE001
        pass
    return saved


def cover(clip: Path, out: Path, made: dict) -> Path:
    """The cover image: a frame on the biggest punch-in (a face mid-reaction) or, without one, the first
    second after the hook lands. Stored as the thumbnail and used as the Instagram/TikTok cover."""
    dur = made.get("dur") or 3.0
    punches = (made.get("fx") or {}).get("punches") or []
    big = [p for p in punches if p.get("shake")] or punches
    at = (big[0]["t"] + 0.45) if big else min(1.5, dur / 3)
    at = max(0.2, min(at, dur - 0.3))
    made["cover_at"] = round(at, 2)
    return render.thumbnail(clip, out, at=at)


def _edit_info(src: Path, c: dict, probe: dict, words: list[dict], credit: str, plan: dict) -> dict:
    """What it takes to render the clip again (another layout, a new trim) from the Inbox. Words cover 15 s
    either side, so the trim can be widened."""
    return {"src": str(src), "start": c["start"], "end": c["end"], "w": probe["width"], "h": probe["height"],
            "has_audio": probe["has_audio"], "hook": c.get("hook", ""), "credit": credit,
            "words": [{"w": w["w"], "s": round(w["s"], 2), "e": round(w["e"], 2)} for w in words][:1500],
            "auto": plan.get("mode")}


def rerender(clip_id: str, layout: str | None = None, start: float | None = None, end: float | None = None,
             hook: str | None = None, title: str | None = None, style_id: str | None = None) -> dict:
    """Render a clip again: in another layout or style, with a new start/end, or a new hook (Inbox → Edit)."""
    clip = db.get("clips", clip_id)
    if not clip:
        raise RuntimeError("Clip not found")
    g = dict(clip["guard"] or {})
    ed = dict(g.get("edit") or {})
    src = Path(ed.get("src") or "")
    if not ed or not src.exists():
        raise RuntimeError("The original video for this clip is gone (lives are kept for a day), so it can't be "
                           "rendered again.")
    want = reframe.ALIASES.get(layout or g.get("layout_choice") or "auto", layout or g.get("layout_choice") or "auto")
    if want not in reframe.LAYOUTS:
        raise RuntimeError(f"Unknown layout: {layout}")
    dur = media.probe(src)["duration"] or 1e9
    s0 = ed["start"] if start is None else max(0.0, float(start))
    s1 = ed["end"] if end is None else min(dur, float(end))
    if s1 - s0 < 3:
        raise RuntimeError("A clip needs at least 3 seconds.")
    if s1 - s0 > 180:
        raise RuntimeError("Keep it under 3 minutes.")
    if hook is not None:
        ed["hook"] = hook.strip()[:120]
    words = [w for w in (ed.get("words") or []) if s0 - 0.05 <= w["s"] <= s1 + 0.05]
    out = Path(clip["file"])
    tmp = out.with_name(out.stem + ".redo.mp4")
    look = styles.get(style_id if style_id is not None else g.get("style_id"))
    made = produce(src, tmp, s0, s1, ed["w"], ed["h"], ed.get("has_audio", True), words, ed.get("hook", ""),
                   ed.get("credit", ""), want, callout=g.get("callout", ""), look=look)
    shutil.move(str(tmp), out)
    out.with_name(out.stem + "_tg.mp4").unlink(missing_ok=True)     # phone preview is stale now
    cover(out, Path(clip["thumb"]), made)
    ed["start"], ed["end"] = s0, s1
    g.update(edit=ed, layout=made["plan"].get("mode"), layout_choice=want, made=made["info"],
             style_id=look.get("id") or "", style_name=look.get("name") or "",
             cover_at=made.get("cover_at"), own_captions=bool(made["plan"].get("burned")))
    fields = {"guard": g, "start": s0, "end": s1, "hook": ed.get("hook", "")}
    if title is not None and title.strip():
        fields["title"] = title.strip()[:120]
    return db.update("clips", clip_id, **fields)


def relayout(clip_id: str, layout: str) -> dict:
    """Render a clip again in the layout you picked (or let it decide again with "auto")."""
    return rerender(clip_id, layout=layout)


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
        camp = campaigns.for_job(job, streamer)
        opt = job["meta"].get("options") or {}
        lo, hi = campaigns.lengths(camp, int(opt.get("min_len") or config.CLIP_MIN),
                                   int(opt.get("max_len") or config.CLIP_MAX))
        words_in, sig_in = tr["words"], sig
        a, b = _range(opt, pr["duration"])
        if a > 0 or b < pr["duration"]:
            # only this part of the video: clips are picked inside it
            words_in = [w for w in words_in if a <= w["s"] and w["e"] <= b]
            sig_in = _only(sig, a, b)
            step(f"Picking clips between {a / 60:.0f} and {b / 60:.0f} min", 0.55)
        picks = brain.find_clips(words_in, sig_in, pr["duration"], info.get("title", ""),
                                 (streamer or {}).get("name") or info.get("uploader", ""), log=lambda m: step(m),
                                 n=int(opt.get("count") or 0) or None,
                                 min_len=lo, max_len=hi, lang=caption_lang(job, streamer),
                                 brief=campaigns.rules_of(camp)["notes"] if camp else "")
        picks = [c for c in picks if c["start"] >= a - 1 and c["end"] <= b + 1]
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


def _range(opt: dict, duration: float) -> tuple[float, float]:
    """The part of the video to clip from ("from"/"to" in seconds; empty means the start / the end)."""
    try:
        a = max(0.0, float(opt.get("from") or 0))
        b = float(opt.get("to") or 0) or duration
    except (TypeError, ValueError):
        return 0.0, duration
    b = min(duration, b) if duration else b
    return (a, b) if b - a >= 10 else (0.0, duration)


def _only(sig: dict, a: float, b: float) -> dict:
    """Signals with everything outside a..b silenced, so no clip is picked there."""
    import numpy as np

    out = dict(sig)
    for k, v in sig.items():
        if k != "db" and isinstance(v, np.ndarray) and v.ndim == 1 and len(v):
            t = np.arange(len(v)) * signals.HOP
            out[k] = np.where((t >= a) & (t <= b), v, 0 if v.dtype != bool else False).astype(v.dtype)
    return out


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
    if (clip["guard"].get("campaign") or {}).get("name"):
        info["campaign"] = clip["guard"]["campaign"]["name"]
    report = guard.review(clip, streamer, info, clip["guard"].get("text", ""), _targets(streamer),
                          clip["guard"].get("ai_flags"))
    report["text"] = clip["guard"].get("text", "")
    for k in ("layout", "layout_choice", "edit", "campaign", "captions_lang", "callout", "made", "cover_at"):
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
