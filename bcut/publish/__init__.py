"""The posting queue: spacing between posts, a daily cap per account, quiet hours, retries — and one last Guard
check right before anything leaves your PC.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta

from .. import config, db, learn
from .platforms import POSTERS


def _quiet(ts: float) -> bool:
    if not config.QUIET_HOURS or "-" not in config.QUIET_HOURS:
        return False
    a, b = (int(x) for x in config.QUIET_HOURS.split("-"))
    h = datetime.fromtimestamp(ts).hour
    return (a <= h < b) if a < b else (h >= a or h < b)


def _next_slot(account: dict) -> float:
    st = account.get("settings") or {}
    gap = float(st.get("gap_min", config.POST_GAP_MIN)) * 60
    cap = int(st.get("daily_cap", config.POST_DAILY_CAP))
    taken = sorted(p["due"] for p in db.query(
        "SELECT due FROM posts WHERE account_id=? AND status IN ('scheduled','posting','posted') AND due > ?",
        (account["id"], time.time() - 86400 * 2)))
    t = time.time() + 30
    for _ in range(2000):
        day = datetime.fromtimestamp(t).date()
        same_day = [x for x in taken if datetime.fromtimestamp(x).date() == day]
        clash = any(abs(x - t) < gap for x in taken)
        if _quiet(t) or clash:
            t += 15 * 60
            continue
        if len(same_day) >= cap:
            t = (datetime.combine(day + timedelta(days=1), datetime.min.time())).timestamp()
            continue
        return t
    return t


def enqueue(clip_id: str, account_ids: list[str], at: float | None = None) -> list[dict]:
    out = []
    for aid in account_ids:
        acc = db.get("accounts", aid)
        if not acc:
            continue
        exists = db.query("SELECT id FROM posts WHERE clip_id=? AND account_id=? AND status!='cancelled'",
                          (clip_id, aid))
        if exists:
            continue
        now = time.time()
        out.append(db.insert("posts", {"id": db.new_id("p"), "clip_id": clip_id, "account_id": aid,
                                       "status": "scheduled", "due": at or _next_slot(acc), "created": now,
                                       "updated": now}))
    if out:
        db.update("clips", clip_id, state="queued")
    return out


def allowed(clip: dict) -> tuple[bool, str]:
    """The last gate. A clip goes out only if the Guard passed it, or you approved it after review, or you
    overrode a block with a written reason."""
    if clip["verdict"] == "pass":
        return True, ""
    if clip["verdict"] == "review" and clip["state"] in ("approved", "queued", "posted") and \
            (clip["guard"] or {}).get("approved_by_you"):
        return True, ""
    if clip["verdict"] == "block" and (clip["guard"] or {}).get("override_reason"):
        return True, ""
    return False, f"Guard verdict is {clip['verdict']}: approve it in the Inbox first"


def caption_for(clip: dict, platform: str):
    caps = clip.get("captions") or {}
    c = caps.get(platform)
    if c is None:
        c = caps.get("instagram", clip.get("title", ""))
    return c


def post_one(p: dict) -> None:
    clip = db.get("clips", p["clip_id"])
    acc = db.get("accounts", p["account_id"])
    if not clip or not acc:
        db.update("posts", p["id"], status="failed", error="Clip or account was deleted")
        return
    ok, why = allowed(clip)
    if not ok:
        db.update("posts", p["id"], status="failed", error=why)
        return
    db.update("posts", p["id"], status="posting", tries=p["tries"] + 1)
    try:
        rid, url = POSTERS[acc["platform"]](acc, clip, caption_for(clip, acc["platform"]))
        db.update("posts", p["id"], status="posted", remote_id=str(rid), remote_url=url or "", error="")
        db.update("clips", clip["id"], state="posted")
        db.log(f"Posted '{clip['title'][:60]}' to {acc['platform'].title()} ({acc['name']})", streamer_id=clip.get("streamer_id"))
    except Exception as e:  # noqa: BLE001
        msg = str(e)[:500]
        if p["tries"] + 1 < 3:
            db.update("posts", p["id"], status="scheduled", due=time.time() + 600 * (p["tries"] + 1), error=msg)
        else:
            db.update("posts", p["id"], status="failed", error=msg)
            db.log(f"Posting to {acc['platform']} failed: {msg[:200]}", "error", clip.get("streamer_id"))


# ---------------------------------------------------------------- autopilot: N posts a day per account

def day_slots(day, target: int, account_id: str = "") -> list[float]:
    """`target` posting times on `day`: the windows that got this account the most views, once it has enough
    posts with views; until then spread evenly over the waking part of the day (after quiet hours, before 23:00)."""
    if account_id:
        hours = learn.best_hours(account_id, target, day.toordinal())
        if hours:
            base = datetime.combine(day, datetime.min.time()).timestamp()
            return [base + h * 3600 for h in hours]
    start_h = 9
    if config.QUIET_HOURS and "-" in config.QUIET_HOURS:
        try:
            start_h = max(6, int(config.QUIET_HOURS.split("-")[1]) + 2)
        except ValueError:
            pass
    end_h = 23
    span = (end_h - start_h) * 3600
    base = datetime.combine(day, datetime.min.time()).timestamp() + start_h * 3600
    return [base + span * (i + 0.5) / target for i in range(target)]


def eligible(clip: dict) -> bool:
    """May autopilot post this clip without asking? Only clips the Guard passed with permission on record, or
    clips you approved yourself."""
    g = clip.get("guard") or {}
    if clip["state"] not in ("new", "approved", "queued"):
        return False
    if g.get("approved_by_you"):
        return allowed(clip)[0]
    if clip["verdict"] != "pass" or not g.get("auto_post_allowed"):
        return False
    if clip.get("streamer_id"):
        s = db.get("streamers", clip["streamer_id"])
        return bool(s and s.get("auto_post"))
    return True  # a link you pasted and said is yours / permitted


def autopilot_tick(now: float | None = None) -> list[dict]:
    now = now or time.time()
    made = []
    for acc in db.query("SELECT * FROM accounts"):
        target = int((acc.get("settings") or {}).get("daily_target") or 0)
        if target <= 0:
            continue
        taken = [p["due"] for p in db.query(
            "SELECT due FROM posts WHERE account_id=? AND status IN ('scheduled','posting','posted') AND due > ?",
            (acc["id"], now - 86400 * 2))]
        done = {p["clip_id"] for p in db.query(
            "SELECT clip_id FROM posts WHERE account_id=? AND status != 'cancelled'", (acc["id"],))}
        pool = [c for c in db.query(
            "SELECT * FROM clips WHERE created > ? ORDER BY score DESC LIMIT 300", (now - 14 * 86400,))
            if c["id"] not in done and eligible(c)]
        # clips like the ones that got views go first (same streamer, same hook style)
        pool.sort(key=lambda c: -((c.get("score") or 0) + learn.boost(c)))
        today = datetime.fromtimestamp(now).date()
        for day in (today, today + timedelta(days=1)):
            on_day = [t for t in taken if datetime.fromtimestamp(t).date() == day]
            for slot in day_slots(day, target, acc["id"]):
                if not pool or len(on_day) >= target:
                    break
                if slot < now + 120 or any(abs(t - slot) < 30 * 60 for t in taken):
                    continue
                clip = pool.pop(0)
                made += enqueue(clip["id"], [acc["id"]], at=slot)
                taken.append(slot)
                on_day.append(slot)
    if made:
        db.log(f"Autopilot scheduled {len(made)} post{'s' if len(made) != 1 else ''}")
    return made


def _loop():
    last_auto = 0.0
    last_stats = time.time() - 6 * 3600 + 600          # first stats read 10 minutes after start
    while True:
        try:
            for p in db.query("SELECT * FROM posts WHERE status='scheduled' AND due <= ? ORDER BY due LIMIT 5",
                              (time.time(),)):
                post_one(p)
            if time.time() - last_auto > 300:
                last_auto = time.time()
                autopilot_tick()
            if time.time() - last_stats > 6 * 3600:
                last_stats = time.time()
                n = learn.refresh()
                if n:
                    db.log(f"Read the views on {n} post{'s' if n != 1 else ''}; the autopilot and hooks learn from them")
        except Exception as e:  # noqa: BLE001  never let the poster thread die
            db.log(f"Posting loop error: {str(e)[:200]}", "error")
        time.sleep(20)


def start() -> None:
    for p in db.query("SELECT id FROM posts WHERE status='posting'"):
        db.update("posts", p["id"], status="scheduled")
    threading.Thread(target=_loop, daemon=True, name="publisher").start()
