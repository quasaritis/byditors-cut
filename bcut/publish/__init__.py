"""The posting queue: spacing between posts, a daily cap per account, quiet hours, retries — and one last Guard
check right before anything leaves your PC.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta

from .. import config, db
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


def _loop():
    while True:
        for p in db.query("SELECT * FROM posts WHERE status='scheduled' AND due <= ? ORDER BY due LIMIT 5",
                          (time.time(),)):
            post_one(p)
        time.sleep(20)


def start() -> None:
    for p in db.query("SELECT id FROM posts WHERE status='posting'"):
        db.update("posts", p["id"], status="scheduled")
    threading.Thread(target=_loop, daemon=True, name="publisher").start()
