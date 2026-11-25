"""The edit passes a clipping editor does by hand, done to every clip:

- tighten:   cut dead air between sentences (never a laugh, a scream or a reaction), trim a slow start and a
             long tail.
- cold open: the clip opens on its loudest 1.8 seconds, rewinds with a scratch, then plays from the setup.
- punch-ins: the camera pushes in on the big moments, toward the face; the biggest one also shakes.
- sound:     real recorded sounds (CC0, by Kenney) under the punch-ins and the rewind; your own sounds in
             data/sfx/ win over the bundled ones.
- loop:      the last quarter second dissolves into the first frame, so the replay feels seamless.

Each pass can be switched off in Guard & settings → Editing.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import numpy as np

from . import config, db, media

DEFAULTS = {"tighten": True, "cold_open": True, "punch": True, "sfx": True, "loop": True, "original": True}
LABELS = {
    "tighten": "Cut dead air between sentences",
    "cold_open": "Open on the best moment, then rewind",
    "punch": "Punch-in zooms on big moments",
    "sfx": "Sound hits (boom, whoosh, rewind)",
    "loop": "Seamless loop at the end",
    "original": "Freeze-frame callout on the big moment and a colour grade (makes a repost your own edit)",
}
FREEZE = 0.7         # seconds the picture holds on the big moment
CALLOUTS = ["WAIT WHAT", "HOLD UP", "LOOK AT HIM", "NAH NO WAY", "HE REALLY DID THAT", "WATCH THIS"]


def default_callout(seed: str = "") -> str:
    return CALLOUTS[sum(map(ord, seed or "x")) % len(CALLOUTS)]
BUNDLED = Path(__file__).with_name("sfx")
HOP = 0.05          # analysis step, seconds


def style() -> dict:
    saved = db.kv_get("edit_style", {}) or {}
    return {k: bool(saved.get(k, v)) for k, v in DEFAULTS.items()}


def set_style(changes: dict) -> dict:
    cur = style()
    for k in DEFAULTS:
        if k in changes:
            cur[k] = bool(changes[k])
    db.kv_set("edit_style", cur)
    return cur


def sound(name: str) -> Path | None:
    """Your own sound in data/sfx/ first, then the bundled one."""
    for d in (config.DATA / "sfx", BUNDLED):
        for ext in (".wav", ".mp3", ".ogg", ".m4a"):
            p = d / f"{name}{ext}"
            if p.exists():
                return p
    return None


def _curve(audio: np.ndarray) -> np.ndarray:
    step = int(media.SR * HOP)
    n = len(audio) // step
    if n == 0:
        return np.zeros(0)
    fr = audio[: n * step].reshape(n, step)
    return 20 * np.log10(np.sqrt((fr ** 2).mean(axis=1) + 1e-10) + 1e-10)


def _smooth(x: np.ndarray, k: int) -> np.ndarray:
    if len(x) < 3 or k < 2:
        return x
    return np.convolve(x, np.ones(k) / k, mode="same")


# ── deciding the cut ─────────────────────────────────────────────────────────────────────────────────────

def plan(src: Path, start: float, end: float, words: list[dict], has_audio: bool, st: dict | None = None) -> dict:
    """Which stretches of the source make the finished clip, in order: {"pieces": [[a, b], ...], "cold": [a, b]}."""
    st = st or style()
    ws = sorted((w for w in words if w["s"] >= start - 0.05 and w["e"] <= end + 0.05 and w["e"] > w["s"]),
                key=lambda w: w["s"])
    audio = media.read_audio(src, start, end - start) if has_audio else np.zeros(0, np.float32)
    curve = _curve(audio)

    def level(a: float, b: float, fn=np.max) -> float:
        i0, i1 = int((a - start) / HOP), int((b - start) / HOP) + 1
        seg = curve[max(0, i0):max(0, i1)]
        return float(fn(seg)) if len(seg) else -90.0

    speech = np.median([level(w["s"], w["e"], np.mean) for w in ws]) if ws else (
        float(np.median(curve)) if len(curve) else -30.0)
    a0, b0 = start, end
    cuts: list[tuple[float, float]] = []
    if st["tighten"] and len(ws) >= 4 and has_audio:
        keep = 0.18                                    # breath left on each side of a cut
        if ws[0]["s"] - start > 0.6 and level(start, ws[0]["s"] - 0.1, lambda x: np.percentile(x, 85)) < speech + 2:
            a0 = ws[0]["s"] - 0.15                     # slow start
        if end - ws[-1]["e"] > 0.8 and level(ws[-1]["e"] + 0.1, end, lambda x: np.percentile(x, 85)) < speech + 2:
            b0 = ws[-1]["e"] + 0.35                    # long tail
        for x, y in zip(ws, ws[1:]):
            g0, g1 = x["e"] + keep, y["s"] - keep
            if g1 - g0 < 0.2:
                continue
            if level(g0, g1, lambda x: np.percentile(x, 85)) > speech + 2:
                continue                               # louder than the talking: a scream, a laugh. Keep it.
            cuts.append((g0, g1))
        # never take out more than a quarter: it is a clip, not a supercut
        budget = 0.25 * (b0 - a0)
        cuts.sort(key=lambda c: c[1] - c[0], reverse=True)
        chosen, used = [], 0.0
        for c in cuts:
            if used + (c[1] - c[0]) <= budget:
                chosen.append(c)
                used += c[1] - c[0]
        cuts = sorted(chosen)
    pieces, cur = [], a0
    for g0, g1 in cuts:
        if g0 > cur + 0.25:
            pieces.append([round(cur, 3), round(g0, 3)])
        cur = g1
    if b0 > cur + 0.25:
        pieces.append([round(cur, 3), round(b0, 3)])
    if not pieces:
        pieces = [[round(a0, 3), round(b0, 3)]]

    cold = None
    total = sum(b - a for a, b in pieces)
    if st["cold_open"] and has_audio and total >= 14 and len(curve) > 40:
        win = int(1.8 / HOP)
        sm = np.convolve(curve, np.ones(win) / win, mode="valid")      # average level of each 1.8 s window
        lo, hi = int(0.3 * len(curve)), len(sm) - int(1.0 / HOP)
        if hi > lo:
            i = lo + int(np.argmax(sm[lo:hi]))
            peak_level = float(sm[i])
            if peak_level > float(np.median(curve)) + 4.5:             # a real spike, not just talking
                c0 = start + i * HOP
                c1 = c0 + 1.8
                inside = [p for p in pieces if p[0] - 0.01 <= c0 and c1 <= p[1] + 0.01]
                if inside:
                    cold = [round(c0, 3), round(c1, 3)]
    freeze = None
    if st.get("original") and has_audio and total >= 12 and len(curve) > 40:
        # the loudest half second in the second half of the clip: the moment to freeze on
        win = int(0.5 / HOP)
        sm = np.convolve(curve, np.ones(win) / win, mode="same")
        order = np.argsort(sm)[::-1]
        for i in order[:200]:
            t = start + i * HOP
            if sm[i] < float(np.median(curve)) + 4:
                break
            rel = sum(min(b, t) - a for a, b in pieces if a < t)
            if rel < 0.35 * total or rel > total - 2.0:
                continue
            if cold and cold[0] - 0.5 <= t <= cold[1] + 0.5:
                continue
            if any(a + 1.0 <= t <= b - 1.0 for a, b in pieces):
                freeze = round(float(t), 3)
                break
    return {"pieces": pieces, "cold": cold, "freeze": freeze, "removed": round((end - start) - total, 2)}


# ── making the tightened file ────────────────────────────────────────────────────────────────────────────

REWIND_SPEED = 3.0


def build(src: Path, edit: dict, out: Path, has_audio: bool, loop: bool) -> dict:
    """Cut the pieces out of the source into one file (at the source's own size). Returns the timeline:
    {"dur", "timeline": [[src_a, src_b, out_a], ...], "joins": [...], "cold_len", "rewind": [a, b]}."""
    pieces = edit["pieces"]
    cold = edit.get("cold")
    parts, timeline, joins = [], [], []
    t = 0.0
    if cold:
        parts.append(("seg", cold[0], cold[1]))
        timeline.append([cold[0], cold[1], 0.0])
        t += cold[1] - cold[0]
        rw = (cold[1] - cold[0]) / REWIND_SPEED * 0.6                 # rewind the last 60% of it, fast
        parts.append(("rewind", cold[1] - rw * REWIND_SPEED, cold[1]))
        rewind = [round(t, 3), round(t + rw, 3)]
        t += rw
        joins += [rewind[0], rewind[1]]
    else:
        rewind = None
    fz = edit.get("freeze")
    freeze = None
    body = []
    for a, b in pieces:
        if fz and a + 0.5 < fz < b - 0.5:
            body += [("seg", a, fz), ("freeze", fz, fz + 0.1), ("seg", fz, b)]
        else:
            body.append(("seg", a, b))
    after_freeze = False
    for kind, a, b in body:
        if kind == "freeze":
            freeze = [round(float(t), 3), round(float(t) + FREEZE, 3)]
            t += FREEZE
            after_freeze = True
            continue
        if timeline and not after_freeze:
            joins.append(round(t, 3))            # a real cut (dead air taken out): the camera may reframe
        after_freeze = False
        timeline.append([a, b, round(t, 3)])
        t += b - a
    dur = t
    fl = []
    vs, as_ = [], []
    allp = parts + body
    # each piece is its own input, opened at its own spot: only that stretch gets decoded (a single input
    # split many ways would hold every frame in memory until its turn)
    inputs: list[str] = []
    for i, (kind, a, b) in enumerate(allp):
        inputs += ["-ss", f"{max(0.0, a):.3f}", "-t", f"{b - a + 0.04:.3f}", "-i", str(src)]
        d = b - a
        if kind == "seg":
            fl.append(f"[{i}:v]trim=0:{d:.3f},setpts=PTS-STARTPTS,fps=30,format=yuv420p,setsar=1[v{i}]")
            if has_audio:
                fl.append(f"[{i}:a]atrim=0:{d:.3f},asetpts=PTS-STARTPTS,aresample=48000,"
                          f"aformat=sample_rates=48000:channel_layouts=stereo[a{i}]")
        elif kind == "freeze":  # the picture holds on the big moment; the sound drops out (the scratch fills it)
            fl.append(f"[{i}:v]trim=0:0.04,setpts=PTS-STARTPTS,fps=30,format=yuv420p,setsar=1,"
                      f"tpad=stop_mode=clone:stop_duration={FREEZE},trim=0:{FREEZE}[v{i}]")
            if has_audio:
                fl.append(f"anullsrc=r=48000:cl=stereo,atrim=0:{FREEZE},asetpts=PTS-STARTPTS[a{i}]")
        else:   # rewind: the end of the cold open played backwards, fast
            fl.append(f"[{i}:v]trim=0:{d:.3f},setpts=PTS-STARTPTS,reverse,setpts=PTS/{REWIND_SPEED},fps=30,"
                      f"format=yuv420p,setsar=1[v{i}]")
            if has_audio:
                fl.append(f"[{i}:a]atrim=0:{d:.3f},asetpts=PTS-STARTPTS,areverse,atempo=1.5,atempo=2,"
                          f"volume=0.35,aresample=48000,aformat=sample_rates=48000:channel_layouts=stereo[a{i}]")
        vs.append(f"[v{i}]")
        as_.append(f"[a{i}]")
    n = len(vs)
    if has_audio:
        fl.append("".join(v + a for v, a in zip(vs, as_)) + f"concat=n={n}:v=1:a=1[vc][ac]")
    else:
        fl.append("".join(vs) + f"concat=n={n}:v=1:a=0[vc]")
    xf = 0.25
    if loop and dur > 6:
        # the end dissolves into the first frame, so when the video loops it doesn't jump
        fl.append(f"[vc]split[vm][vh];[vh]trim=0:{xf + 0.05:.2f},setpts=PTS-STARTPTS[vhd];"
                  f"[vm][vhd]xfade=transition=fade:duration={xf}:offset={dur - xf:.3f}[vo]")
        if has_audio:   # the sound just fades out at the end (render does that); pad to the picture's length
            fl.append("[ac]apad=pad_dur=0.05[ao]")
        dur = dur + 0.05
    else:
        fl.append("[vc]null[vo]")
        if has_audio:
            fl.append("[ac]anull[ao]")
    cmd = [media.need("ffmpeg"), "-v", "error", "-y", *inputs, "-filter_complex", ";".join(fl), "-map", "[vo]"]
    if has_audio:
        cmd += ["-map", "[ao]", "-c:a", "aac", "-b:a", "256k"]
    cmd += ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "16", "-r", "30", str(out)]
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0 or not out.exists():
        raise RuntimeError("Edit pass failed: " + " | ".join(p.stderr.strip().splitlines()[-3:]))
    return {"dur": round(dur, 3), "timeline": timeline, "joins": joins, "rewind": rewind, "freeze": freeze,
            "cold_len": round(cold[1] - cold[0], 3) if cold else 0.0}


def map_words(words: list[dict], timeline: list[list[float]]) -> list[dict]:
    """Words in the new file's time (a word inside the cold open shows up twice: there and in place)."""
    out = []
    for a, b, o in timeline:
        for w in words:
            if w["s"] >= a - 0.02 and w["e"] <= b + 0.05:
                out.append({**w, "s": round(o + (w["s"] - a), 3), "e": round(o + (min(w["e"], b) - a), 3)})
    return sorted(out, key=lambda w: w["s"])


# ── punch-ins and sound hits ─────────────────────────────────────────────────────────────────────────────

def events(file: Path, start: float, dur: float, built: dict, st: dict, has_audio: bool) -> dict:
    """Punch-ins and sounds, in seconds from the clip's first frame."""
    punches, sounds = [], []
    if has_audio and st["punch"]:
        curve = _smooth(_curve(media.read_audio(file, start, dur)), 6)
        if len(curve):
            med = float(np.median(curve))
            gap = int(3.0 / HOP)
            order = np.argsort(curve)[::-1]
            chosen: list[int] = []
            lo = int(0.6 / HOP)
            hi = len(curve) - int(1.0 / HOP)
            rw = built.get("rewind")
            fzw = built.get("freeze")
            # something moves every few seconds: about one push-in per 4.5 s, on the loudest moments first
            limit = max(1, int(dur / 4.5))

            def free(i: int) -> bool:
                t = i * HOP
                return (lo <= i <= hi and not any(abs(i - j) < gap for j in chosen)
                        and not (rw and rw[0] - 0.4 <= t <= rw[1] + 0.3)
                        and not (fzw and fzw[0] - 1.5 <= t <= fzw[1] + 1.0))
            for i in order:
                if curve[i] < med + 6 or len(chosen) >= limit:
                    break
                if free(i):
                    chosen.append(i)
            # a calm stretch longer than ~7 s still gets a gentle push on its liveliest moment
            for i in order:
                if len(chosen) >= limit:
                    break
                if curve[i] < med and len(chosen) >= int(dur / 8):
                    break
                if free(i) and not any(abs(i - j) < int(7.0 / HOP) for j in chosen):
                    chosen.append(i)
            sizes = [1.1, 1.13, 1.08]
            for k, i in enumerate(sorted(chosen, key=lambda j: -curve[j])):
                t = round(float(max(0.0, i * HOP - 0.15)), 3)       # land just before the peak
                big = bool(k == 0 and curve[i] > med + 12)
                punches.append({"t": t, "zoom": 1.16 if big else sizes[k % 3], "shake": big})
            if fzw:      # the freeze pushes in on the moment
                punches.append({"t": fzw[0], "zoom": 1.2, "shake": False})
            punches.sort(key=lambda p: p["t"])
    if st["sfx"]:
        for p in punches:
            sounds.append({"t": round(max(0.0, p["t"] - 0.02), 3), "name": "boom" if p["shake"] else "hit",
                           "db": -9 if p["shake"] else -15})
        fzw = built.get("freeze")
        if fzw:
            sounds = [x for x in sounds if not (fzw[0] - 0.1 <= x["t"] <= fzw[0] + 0.1)]
            sounds.append({"t": round(fzw[0], 3), "name": "rewind", "db": -8})     # the record scratch
            sounds.append({"t": round(max(0.0, fzw[1] - 0.12), 3), "name": "whoosh", "db": -13})
        rw = built.get("rewind")
        if rw:
            sounds.append({"t": max(0.0, rw[0] - 0.05), "name": "rewind", "db": -10})
            sounds.append({"t": round(max(0.0, rw[1] - 0.18), 3), "name": "whoosh", "db": -12})
    return {"punches": punches, "sounds": [s for s in sounds if sound(s["name"])], "drift": bool(st["punch"])}


def zoom_filter(punches: list[dict], centre, dur: float = 0.0, drift: float = 0.0) -> str:
    """ffmpeg filters that push in on each punch (toward `centre(t)` -> (fx, fy) in 0..1) and shake on the
    big one, over a slow push-in across the whole clip (`drift`, e.g. 0.05 = 5% by the end) so the picture is
    never dead still. Works on the finished 1080x1920 picture, before captions go on."""
    if not punches and not (drift and dur):
        return ""
    base = f"{drift:.3f}*min(t/{max(dur, 1):.2f},1)" if drift and dur else "0"
    if not punches:
        z = f"(1+{base})"
        return (f"scale=w='2*trunc(540*{z})':h='2*trunc(960*{z})':eval=frame:flags=bicubic,"
                f"crop=1080:1920:x='(iw-1080)/2':y='(ih-1920)*0.45'")
    z_terms, fx_terms, fy_terms, shake = [base], [], [], []
    for p in punches:
        a = p["t"]
        amt = p["zoom"] - 1
        # fast in (0.12 s, eased), hold 0.55 s, slow out (0.5 s)
        up = f"clip((t-{a:.3f})/0.12,0,1)"
        down = f"clip(1-(t-{a + 0.67:.3f})/0.5,0,1)"
        z_terms.append(f"{amt:.3f}*({up})*({up})*(3-2*({up}))*({down})")
        fx, fy = centre(a + 0.3)
        win = f"between(t,{a - 0.05:.3f},{a + 1.25:.3f})"
        fx_terms.append(f"{win}*{fx:.3f}")
        fy_terms.append(f"{win}*{fy:.3f}")
        if p.get("shake"):
            shake.append(f"between(t,{a:.3f},{a + 0.4:.3f})*(1-(t-{a:.3f})/0.4)")
    z = "(1+" + "+".join(z_terms) + ")"
    anyw = "+".join(f"between(t,{p['t'] - 0.05:.3f},{p['t'] + 1.25:.3f})" for p in punches)
    fx = f"(({'+'.join(fx_terms)})+(1-min(1,{anyw}))*0.5)"
    fy = f"(({'+'.join(fy_terms)})+(1-min(1,{anyw}))*0.42)"
    sh = "+".join(shake) or "0"
    x = f"clip((iw-1080)*{fx}+14*({sh})*sin(t*83),0,iw-1080)"
    y = f"clip((ih-1920)*{fy}+10*({sh})*cos(t*71),0,ih-1920)"
    return (f"scale=w='2*trunc(540*{z})':h='2*trunc(960*{z})':eval=frame:flags=bicubic,"
            f"crop=1080:1920:x='{x}':y='{y}'")
