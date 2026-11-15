"""The Rights & Safety Guard. Every clip passes through here before it can be posted anywhere.

It answers three questions and writes the answers into a report you can read in the Inbox:

1. **Are we allowed to post this at all?** (rights) — whose content is it, and do we have permission?
2. **Is anything inside it someone else's copyright?** (music, songs, someone else's video / TV / sports on screen)
3. **Would a platform take it down?** (community guidelines + each platform's technical limits)

Verdicts:
- `pass`   — can be auto-posted (if auto-post is on for that streamer)
- `review` — waits in the Inbox until you approve it
- `block`  — never posted unless you override it with a written reason (kept in the log)
"""
from __future__ import annotations

import time
from pathlib import Path

from .. import config, db
from . import content, music, rules

RIGHTS = {
    "own": "Your own content",
    "permission": "You have written permission / a licence",
    "clip_program": "The creator runs an open clipping program (paid or free) and allows reposts with credit",
    "unknown": "No permission on record",
}

CC_LICENCES = ("creative commons", "cc by", "cc-by")


def _check(cid: str, label: str, status: str, detail: str, fix: str = "") -> dict:
    return {"id": cid, "label": label, "status": status, "detail": detail, "fix": fix}


def rights_check(streamer: dict | None, source: dict) -> dict:
    licence = str(source.get("license") or "").lower()
    if any(c in licence for c in CC_LICENCES):
        return _check("rights", "Rights", "ok", f"Source is published under {source.get('license')}: reuse allowed "
                      "with credit. Credit is added automatically.")
    if not streamer:
        if source.get("uploaded_by_you"):
            return _check("rights", "Rights", "ok", "You uploaded this file yourself.")
        return _check("rights", "Rights", "review",
                      "This link isn't tied to a streamer you have permission for.",
                      "Add the creator under Streamers and record your permission, or only clip your own videos.")
    r = streamer.get("rights", "unknown")
    if r == "own":
        return _check("rights", "Rights", "ok", RIGHTS["own"])
    if r == "permission":
        if streamer.get("rights_note"):
            return _check("rights", "Rights", "ok", f"{RIGHTS['permission']}: {streamer['rights_note'][:160]}")
        return _check("rights", "Rights", "review", "Marked as permitted, but no proof is saved.",
                      "Paste a link to the email / contract / message in the streamer's permission note.")
    if r == "clip_program":
        note = streamer.get("rights_note") or ""
        return _check("rights", "Rights", "ok" if note else "review",
                      RIGHTS["clip_program"] + (f": {note[:160]}" if note else " — but the program link isn't saved."),
                      "" if note else "Save the link to the creator's clipping-program rules.")
    return _check("rights", "Rights", "review",
                  f"No permission on record for {streamer.get('name', 'this creator')}. Clips are made for you to "
                  "look at but are never posted automatically.",
                  "Ask the creator (many Kick/Twitch streamers run clipping programs), then set Rights on the "
                  "streamer.")


def music_check(m: dict, rights: str) -> tuple[dict, str | None]:
    """Returns the check and the risk it raises (if any)."""
    if m.get("song"):
        s = m["song"]
        return (_check("music", "Music", "block",
                       f"Copyrighted song found: {s.get('artist')} — {s.get('title')}"
                       + (f" ({s['label']})" if s.get("label") else ""),
                       "Strip the music (needs demucs) or cut a different moment."), "copyright_music")
    share = m.get("share", 0)
    if share >= config.MUSIC_BLOCK_SHARE:
        return (_check("music", "Music", "block" if rights != "own" else "review",
                       f"About {int(share * 100)}% of the clip sounds like music.",
                       "Strip the music or choose a talking moment."), "music_suspected")
    if share >= config.MUSIC_REVIEW_SHARE:
        return (_check("music", "Music", "review", f"Some background music (~{int(share * 100)}% of the clip).",
                       "Listen once. Use 'Strip music' if it's a commercial song."), "music_suspected")
    return _check("music", "Music", "ok", "No music detected."), None


def platform_checks(clip: dict, targets: list[str]) -> list[dict]:
    out = []
    dur = clip["end"] - clip["start"]
    size_mb = 0.0
    if clip.get("file") and Path(clip["file"]).exists():
        size_mb = Path(clip["file"]).stat().st_size / 1e6
    caps = clip.get("captions") or {}
    for p in targets:
        spec = rules.PLATFORMS.get(p)
        if not spec:
            continue
        problems = []
        if dur < spec["min_sec"]:
            problems.append(f"too short ({dur:.0f}s < {spec['min_sec']}s)")
        if dur > spec["max_sec"]:
            problems.append(f"too long ({dur:.0f}s > {spec['max_sec']}s)")
        if size_mb > spec["max_mb"]:
            problems.append(f"file too big ({size_mb:.0f} MB)")
        cap = caps.get(p)
        text = cap.get("description", "") if isinstance(cap, dict) else (cap or "")
        if spec.get("caption_max") and len(text) > spec["caption_max"]:
            problems.append("caption too long")
        if text.count("#") > spec.get("hashtags_max", 30):
            problems.append(f"more than {spec['hashtags_max']} hashtags")
        if isinstance(cap, dict) and len(cap.get("title", "")) > spec.get("title_max", 100):
            problems.append("title too long")
        out.append(_check(f"platform_{p}", spec["label"], "block" if problems else "ok",
                          "; ".join(problems) if problems else "Fits the platform's limits.",
                          "Shorten the clip or caption." if problems else ""))
    return out


def originality_check(clip: dict, source: dict, has_captions: bool) -> dict:
    src_dur = float(source.get("duration") or 0)
    dur = clip["end"] - clip["start"]
    whole = src_dur and dur >= 0.9 * src_dur
    if whole and not source.get("uploaded_by_you"):
        return _check("originality", "Originality", "review",
                      "This is basically the whole original video re-posted. Instagram and YouTube treat that as "
                      "reused content (no reach, possible strikes).",
                      "Clip a part of it and keep the captions/hook on.")
    bits = []
    if has_captions:
        bits.append("captions")
    if clip.get("hook"):
        bits.append("hook")
    if config.CREDIT_OVERLAY:
        bits.append("credit tag")
    return _check("originality", "Originality", "ok" if bits else "warn",
                  ("Edited with " + ", ".join(bits) + ".") if bits else "Plain cut with no edits.",
                  "" if bits else "Turn on captions or a hook so it's transformed, not re-uploaded.")


def review(clip: dict, streamer: dict | None, source: dict, words_text: str, targets: list[str],
           ai_flags: list[str] | None = None, music_report: dict | None = None) -> dict:
    """The full report for one clip. `source` is the downloaded video's info (title, license, duration...)."""
    policy = db.kv_get("guard_policy", {}) or {}
    checks = []
    risk_found: dict[str, str] = {}

    rc = rights_check(streamer, source)
    checks.append(rc)
    rights = (streamer or {}).get("rights", "own" if source.get("uploaded_by_you") else "unknown")

    if music_report is None and clip.get("file") and Path(clip["file"]).exists():
        music_report = music.analyse(Path(clip["file"]))
    if music_report is not None:
        mc, mrisk = music_check(music_report, rights)
        checks.append(mc)
        if mrisk:
            risk_found[mrisk] = mc["detail"]

    for risk, why in content.screen(words_text, ai_flags).items():
        if risk in ("music_suspected",) and "music_suspected" in risk_found:
            continue
        risk_found.setdefault(risk, why)

    for risk, why in risk_found.items():
        if risk in ("copyright_music", "music_suspected"):
            continue  # already reported by the music check
        acts = [rules.action(risk, p, policy) for p in (targets or [None])]
        act = rules.worst(acts)
        label = rules.RISK_POLICY.get(risk, {}).get("label", risk)
        fix = {
            "watching_media": "Only post if the on-screen video is theirs or licensed; otherwise pick another moment.",
            "gambling": "Gambling promotion is banned or restricted on these platforms (and in India). Skip it.",
            "personal_info": "Cut or bleep the part with the number/address.",
        }.get(risk, "Watch it before posting.")
        checks.append(_check(f"risk_{risk}", label, act, why, fix))

    checks.append(originality_check(clip, source, bool(words_text.strip())))
    checks.extend(platform_checks(clip, targets))

    statuses = [c["status"] for c in checks]
    verdict = "block" if "block" in statuses else ("review" if "review" in statuses else "pass")
    auto_ok = verdict == "pass" and rc["status"] == "ok"
    return {"verdict": verdict, "auto_post_allowed": auto_ok, "checks": checks,
            "music": {k: v for k, v in (music_report or {}).items() if k != "curve"},
            "music_curve": (music_report or {}).get("curve", []), "checked_at": time.time(),
            "can_strip_music": music.can_separate()}
