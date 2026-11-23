"""Learning from what your posts actually do.

Every few hours the views, likes and comments of recent posts are read back from Instagram and YouTube. That
feeds three things:

- the autopilot posts the clip most like your winners first (same streamer, same hook style);
- the AI is shown your best-performing hooks when it writes new ones;
- hook styles are tested against each other: each video gets one style, chosen mostly from what has won so far
  and sometimes at random so a new favourite can still emerge.
"""
from __future__ import annotations

import random
import time

import httpx

from . import db

STYLES = {
    "question": "a question the viewer needs answered (\"Why did he do THAT?\")",
    "shock": "the shocking outcome stated flat, no setup (\"He crashed the boss's car\")",
    "quote": "the best line said in the clip, word for word, as the hook",
    "stakes": "what's at stake or the challenge (\"1 minute to escape the cops\")",
    "pov": "a POV / you-are-there line (\"POV: your friend gets caught\")",
}
EXPLORE = 0.25


def _metrics() -> dict:
    return db.kv_get("post_metrics", {}) or {}


def _ig_stats(account: dict, media_id: str) -> dict:
    from .publish import platforms

    token = (account.get("secrets") or {}).get("token", "")
    base = f"https://{platforms.ig_host(account)}/{platforms.V}"
    out: dict = {}
    r = httpx.get(f"{base}/{media_id}", params={"fields": "like_count,comments_count", "access_token": token},
                  timeout=20)
    if r.status_code == 200:
        d = r.json()
        out["likes"] = int(d.get("like_count") or 0)
        out["comments"] = int(d.get("comments_count") or 0)
    for metric in ("views", "plays"):          # "views" is the current name; older accounts still answer "plays"
        r = httpx.get(f"{base}/{media_id}/insights", params={"metric": f"{metric},reach,shares,saved",
                                                              "access_token": token}, timeout=20)
        if r.status_code == 200:
            for m in r.json().get("data") or []:
                v = (m.get("values") or [{}])[0].get("value", m.get("total_value", {}).get("value", 0))
                out["views" if m.get("name") in ("views", "plays") else m.get("name")] = int(v or 0)
            break
    return out


def _yt_stats(account: dict, ids: list[str]) -> dict:
    from .publish import platforms

    token = platforms._yt_token(account)
    r = httpx.get("https://www.googleapis.com/youtube/v3/videos", params={"part": "statistics", "id": ",".join(ids)},
                  headers={"authorization": f"Bearer {token}"}, timeout=20)
    r.raise_for_status()
    out = {}
    for it in r.json().get("items") or []:
        st = it.get("statistics") or {}
        out[it["id"]] = {"views": int(st.get("viewCount") or 0), "likes": int(st.get("likeCount") or 0),
                         "comments": int(st.get("commentCount") or 0)}
    return out


def refresh(now: float | None = None) -> int:
    """Read back stats for posts from the last 21 days. Returns how many were updated."""
    now = now or time.time()
    posts = db.query("SELECT * FROM posts WHERE status='posted' AND remote_id IS NOT NULL AND remote_id != '' "
                     "AND updated > ?", (now - 21 * 86400,))
    if not posts:
        return 0
    store = _metrics()
    done = 0
    by_acc: dict[str, list[dict]] = {}
    for p in posts:
        by_acc.setdefault(p["account_id"], []).append(p)
    for acc_id, ps in by_acc.items():
        acc = db.get("accounts", acc_id)
        if not acc:
            continue
        try:
            if acc["platform"] == "instagram":
                for p in ps:
                    st = _ig_stats(acc, p["remote_id"])
                    if st:
                        store[p["id"]] = {**st, "at": now, "clip": p["clip_id"], "platform": "instagram"}
                        done += 1
            elif acc["platform"] == "youtube":
                for i in range(0, len(ps), 50):
                    chunk = ps[i:i + 50]
                    got = _yt_stats(acc, [p["remote_id"] for p in chunk])
                    for p in chunk:
                        if p["remote_id"] in got:
                            store[p["id"]] = {**got[p["remote_id"]], "at": now, "clip": p["clip_id"],
                                              "platform": "youtube"}
                            done += 1
        except Exception as e:  # noqa: BLE001
            db.log(f"Couldn't read stats from {acc.get('name') or acc['platform']}: {str(e)[:160]}", "warn")
    db.kv_set("post_metrics", store)
    return done


def _score(m: dict) -> float:
    """One number per post: views, with likes, comments, shares and saves weighted in."""
    return (m.get("views", 0) + 20 * m.get("likes", 0) + 60 * m.get("comments", 0) + 80 * m.get("shares", 0)
            + 80 * m.get("saved", 0))


def results() -> list[dict]:
    """Every post with stats, joined with its clip: [{"clip", "score", "hook", "style", "streamer_id", ...}]"""
    out = []
    for pid, m in _metrics().items():
        c = db.get("clips", m.get("clip", ""))
        if not c:
            continue
        post = db.get("posts", pid) or {}
        out.append({"post": pid, "clip": c["id"], "score": _score(m), "views": m.get("views", 0),
                    "account": post.get("account_id"), "at": post.get("due") or 0,
                    "likes": m.get("likes", 0), "hook": c.get("hook", ""), "title": c.get("title", ""),
                    "style": (c.get("guard") or {}).get("hook_style", ""), "streamer_id": c.get("streamer_id"),
                    "platform": m.get("platform")})
    return out


def _rel(rs: list[dict]) -> dict[str, float]:
    """How each post did against the median post (1.0 = typical)."""
    if not rs:
        return {}
    med = sorted(r["score"] for r in rs)[len(rs) // 2] or 1.0
    return {r["post"]: r["score"] / med for r in rs}


def style_stats() -> dict[str, dict]:
    rs = results()
    rel = _rel(rs)
    out = {k: {"n": 0, "lift": 1.0} for k in STYLES}
    for k in STYLES:
        mine = [rel[r["post"]] for r in rs if r["style"] == k]
        if mine:
            out[k] = {"n": len(mine), "lift": round(sum(mine) / len(mine), 2)}
    return out


def pick_style(rng: random.Random | None = None) -> str:
    """The hook style for the next video: mostly the best so far, sometimes another to keep testing."""
    rng = rng or random.Random()
    st = style_stats()
    untested = [k for k, v in st.items() if v["n"] < 3]
    if untested and rng.random() < 0.5:
        return rng.choice(untested)
    if rng.random() < EXPLORE:
        return rng.choice(list(STYLES))
    return max(st, key=lambda k: (st[k]["lift"] if st[k]["n"] >= 3 else 0.0, st[k]["n"]))


def top_hooks(n: int = 5) -> list[str]:
    rs = sorted((r for r in results() if r["hook"]), key=lambda r: -r["score"])
    seen, out = set(), []
    for r in rs:
        if r["hook"].lower() not in seen:
            seen.add(r["hook"].lower())
            out.append(r["hook"])
        if len(out) >= n:
            break
    return out if len(rs) >= 4 else []


def boost(clip: dict) -> float:
    """Added to a clip's score when the autopilot picks what to post next: clips like your winners go first."""
    rs = results()
    if len(rs) < 4:
        return 0.0
    rel = _rel(rs)
    b = 0.0
    style = (clip.get("guard") or {}).get("hook_style", "")
    same_style = [rel[r["post"]] for r in rs if style and r["style"] == style]
    if len(same_style) >= 3:
        b += 10 * (sum(same_style) / len(same_style) - 1)
    same_streamer = [rel[r["post"]] for r in rs if clip.get("streamer_id") and r["streamer_id"] == clip["streamer_id"]]
    if len(same_streamer) >= 3:
        b += 8 * (sum(same_streamer) / len(same_streamer) - 1)
    return max(-15.0, min(15.0, b))


def prompt_extra(style: str) -> str:
    """Lines added to the clip-picking prompt."""
    lines = [f"Hook style for this video: {style}: {STYLES[style]}."] if style in STYLES else []
    hooks = top_hooks()
    if hooks:
        lines.append("Hooks that got the most views on this account (match their energy, don't copy them): "
                     + " | ".join(hooks))
    return "\n".join(lines)


# ── when to post ─────────────────────────────────────────────────────────────────────────────────────────

WINDOWS = list(range(9, 23, 2))          # 2-hour windows starting 9:00, 11:00 ... 21:00 (local time)


def hour_stats(account_id: str) -> dict[int, dict]:
    """How posts did by the 2-hour window they went out in, for one account: {start_hour: {"n", "lift"}}."""
    from datetime import datetime

    rs = [r for r in results() if r.get("account") == account_id and r.get("at")]
    # against this account's average post, so a window nobody tried (counted as average) can't outrank a proven one
    mean = (sum(r["score"] for r in rs) / len(rs)) if rs else 1.0
    rel = {r["post"]: r["score"] / (mean or 1.0) for r in rs}
    out = {h: {"n": 0, "lift": 1.0} for h in WINDOWS}
    groups: dict[int, list[float]] = {}
    for r in rs:
        h = datetime.fromtimestamp(r["at"]).hour
        if not WINDOWS[0] <= h < WINDOWS[-1] + 2:
            continue                       # posted overnight: not a window the autopilot would use
        w = max(x for x in WINDOWS if x <= h)
        groups.setdefault(w, []).append(rel[r["post"]])
    for w, v in groups.items():
        out[w] = {"n": len(v), "lift": round(sum(v) / len(v), 2)}
    return out


def best_hours(account_id: str, target: int, day_seed: int = 0) -> list[float] | None:
    """The local hours to post at for this account, best windows first, once there's enough to go on (8+ posts
    with views). Windows with few posts get a small bonus so they keep getting tried; one slot in four is
    sometimes spent on a random window to keep learning. None = not enough data yet (spread evenly)."""
    st = hour_stats(account_id)
    if sum(v["n"] for v in st.values()) < 8 or target <= 0:
        return None
    rng = random.Random(f"{account_id}:{day_seed}")
    score = {h: v["lift"] + 0.25 / (1 + v["n"]) ** 0.5 for h, v in st.items()}
    order = sorted(score, key=lambda h: -score[h])
    pick = order[:target]
    if len(order) > target and rng.random() < 0.25:
        pick[-1] = rng.choice(order[target:])
    # the middle of each window, with a few minutes of jitter so posts don't land on the hour
    return sorted(h + 1 + rng.uniform(-0.4, 0.4) for h in pick)
